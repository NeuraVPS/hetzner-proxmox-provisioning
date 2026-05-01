#!/usr/bin/env bash

############################################################
# Migrate existing Proxmox nodes from "DHCPv6 stateful + pins + ::<vmid_hex>"
# to "ra-stateless SLAAC + EUI-64 + ::5054:ff:fe00:<vmid_hex>".
#
# Per node (always):
#   - Pull latest sync-dnat.py from GitHub master
#   - Move stale /etc/dnsmasq.d/vmbr0.conf.bak* out of the dir
#   - Rewrite /etc/dnsmasq.d/vmbr0.conf as ra-stateless (SLAAC)
#   - Remove /etc/dnsmasq.d/vm-pins.conf (no longer used)
#   - systemctl restart dnsmasq
#
# Per Windows VM that is RUNNING and not yet on the new MAC scheme:
#   - Compute new MAC: 52:54:00:00:<HH>:<LL> where HHLL = vmid in hex
#   - Compute expected IPv6: <node_/64_prefix>::5054:ff:fe00:<vmid_hex>
#   - Run netsh in the guest (via guest-agent) to disable RFC 7217 + privacy
#     persistently, so future SLAAC uses EUI-64 instead of stable-privacy
#   - qm set --net0 with the new MAC — Proxmox hot-replaces the virtio NIC
#     (device_del + device_add via QMP, NO VM reboot)
#   - Cycle the physical adapter inside the guest to force fresh SLAAC with
#     the new MAC (Windows auto-derives EUI-64 from the new MAC)
#   - Verify the expected IPv6 is bound on the guest's interface list
#   - Update servers/{id}.ipv6 in Firestore
#
# On failure at any per-VM step: log RUNREMOTES_FAIL and continue. Do NOT
# update Firestore for that VM (so existing NAT46 keeps pointing at whatever
# IPv6 was there before — fail-safe).
#
# Stopped VMs are SKIPPED entirely. Re-run the script after they are running.
#
# Idempotent: VMs already migrated (MAC matches the new scheme) are detected
# and only verified + Firestore-updated, no second migration.
############################################################

remote_task() {
  set +e

  local NODE_NAME SYNC_DNAT SYNC_DNAT_URL PER_VM_SLEEP
  NODE_NAME="$(hostname)"
  SYNC_DNAT="/var/lib/svz/snippets/sync-dnat.py"
  SYNC_DNAT_URL="https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/snippets/sync-dnat.py"
  PER_VM_SLEEP=5

  # ----- Helpers (nested so they're picked up by `declare -f remote_task`) -----

  # Run a PowerShell snippet via guest-agent using -EncodedCommand (UTF-16LE base64).
  # Sidesteps all bash/SSH/PowerShell quoting concerns. Returns 0 if exec exits
  # within $3 seconds (default 90), 1 otherwise.
  _run_ps_via_agent() {
    local _vmid="$1" _ps="$2" _timeout="${3:-90}"
    local _b64
    _b64=$(printf '%s' "$_ps" | iconv -t UTF-16LE | base64 -w0)
    [[ -n "$_b64" ]] || { echo "  _run_ps: iconv/base64 failed" >&2; return 1; }

    local _exec_out _pid _attempt=0
    while (( _attempt < 18 )); do
      _exec_out=$(pvesh create "/nodes/${NODE_NAME}/qemu/${_vmid}/agent/exec" \
        --output-format json \
        --command powershell.exe \
        --command -NoProfile \
        --command -NonInteractive \
        --command -EncodedCommand \
        --command "$_b64" 2>&1)
      _pid=$(echo "$_exec_out" | sed -n 's/.*"pid"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' | head -1)
      if [[ -n "$_pid" && "$_pid" -gt 0 ]]; then
        break
      fi
      if echo "$_exec_out" | grep -qi "guest agent is not running"; then
        (( _attempt++ )) || true
        sleep 5
        continue
      fi
      echo "  _run_ps: pvesh exec error: ${_exec_out:0:200}" >&2
      return 1
    done
    [[ -n "$_pid" && "$_pid" -gt 0 ]] || { echo "  _run_ps: guest agent never came up" >&2; return 1; }

    local _elapsed=0 _st
    while (( _elapsed < _timeout )); do
      _st=$(pvesh get "/nodes/${NODE_NAME}/qemu/${_vmid}/agent/exec-status" \
        --output-format json --pid "$_pid" 2>/dev/null || true)
      if echo "$_st" | grep -qE '"exited"\s*:\s*1'; then
        return 0
      fi
      sleep 3
      (( _elapsed += 3 )) || true
    done
    return 1
  }

  # Verify VM has the expected IPv6 bound. Uses canonical IPv6 comparison.
  # Returns 0 if found within $3 seconds (default 90).
  _verify_vmid_ipv6() {
    local _vmid="$1" _expected="$2" _timeout="${3:-90}" _elapsed=0 _ifaces
    while (( _elapsed < _timeout )); do
      _ifaces=$(qm guest cmd "$_vmid" network-get-interfaces 2>/dev/null || true)
      if [[ -n "$_ifaces" ]] && EXPECTED="$_expected" JSON="$_ifaces" python3 -c '
import ipaddress, json, os, sys
try:
    target = ipaddress.IPv6Address(os.environ["EXPECTED"])
except ValueError: sys.exit(2)
try:
    data = json.loads(os.environ["JSON"])
except Exception: sys.exit(1)
ifaces = data.get("result", data) if isinstance(data, dict) else data
if not isinstance(ifaces, list): sys.exit(1)
for iface in ifaces:
    if not isinstance(iface, dict): continue
    for addr in iface.get("ip-addresses") or []:
        if not isinstance(addr, dict): continue
        ip = str(addr.get("ip-address", "") or "").strip().split("%")[0]
        if not ip: continue
        try:
            if ipaddress.IPv6Address(ip) == target: sys.exit(0)
        except ValueError: continue
sys.exit(1)
' 2>/dev/null; then
        return 0
      fi
      sleep 5
      (( _elapsed += 5 )) || true
    done
    return 1
  }

  # ----- Main flow -----
  echo "== Running remote task on ${NODE_NAME} =="

  # 0) Fetch latest sync-dnat.py from GitHub
  mkdir -p /var/lib/svz/snippets
  if ! curl -fsSL "$SYNC_DNAT_URL" -o "${SYNC_DNAT}.new"; then
    echo "RUNREMOTES_FAIL	${NODE_NAME}	-	failed to download sync-dnat.py"
    return 1
  fi
  if ! python3 -c "import ast; ast.parse(open('${SYNC_DNAT}.new').read())" 2>/dev/null; then
    echo "RUNREMOTES_FAIL	${NODE_NAME}	-	downloaded sync-dnat.py has syntax errors"
    rm -f "${SYNC_DNAT}.new"
    return 1
  fi
  mv "${SYNC_DNAT}.new" "$SYNC_DNAT"
  chmod +x "$SYNC_DNAT"
  echo "Updated $SYNC_DNAT from GitHub ($(wc -l < "$SYNC_DNAT") lines)"

  # 1) Move stale .bak files out of /etc/dnsmasq.d/
  mkdir -p /root/dnsmasq-backups
  for f in /etc/dnsmasq.d/vmbr0.conf.bak /etc/dnsmasq.d/vmbr0.conf.bak.*; do
    [ -f "$f" ] && mv "$f" /root/dnsmasq-backups/ 2>/dev/null || true
  done

  # 2) Apply new dnsmasq config: ra-stateless (SLAAC for addresses, DHCPv6 for DNS)
  cat > /etc/dnsmasq.d/vmbr0.conf <<'CONF'
interface=vmbr0
bind-dynamic
dhcp-authoritative
dhcp-rapid-commit

# IPv4 DHCP pool for guests
dhcp-range=10.0.0.100,10.0.255.254,255.255.0.0,720h
dhcp-option=3,10.0.0.1
dhcp-option=6,185.12.64.1,185.12.64.2

# ==== IPv6: stateless RA (SLAAC) — guests self-configure via EUI-64 ====
# VMs use VMID-encoded MACs (52:54:00:00:HH:LL) and Windows is configured to
# disable RFC 7217 stable-privacy, so SLAAC produces a deterministic
# <prefix>::5054:ff:fe00:<vmid_hex>. No DHCPv6 server roundtrip on boot.
enable-ra
dhcp-range=::100,::1ff,constructor:vmbr0,ra-stateless,720h
dhcp-option=option6:dns-server,[2a01:4ff:ff00::add:1],[2a01:4ff:ff00::add:2]
CONF

  # 3) Remove old DHCPv6 pin file (no longer used in the new model)
  rm -f /etc/dnsmasq.d/vm-pins.conf

  # 4) Restart dnsmasq
  if ! systemctl restart dnsmasq; then
    echo "RUNREMOTES_FAIL	${NODE_NAME}	-	dnsmasq restart failed (check journalctl -u dnsmasq)"
    return 1
  fi
  if ! systemctl is-active --quiet dnsmasq; then
    echo "RUNREMOTES_FAIL	${NODE_NAME}	-	dnsmasq not active after restart"
    return 1
  fi

  # 5) Self-check: dnsmasq subscribed to ff02::2 (RA-on-RS works)
  if ! ip -6 maddr show dev vmbr0 2>/dev/null | grep -q 'ff02::2'; then
    echo "RUNREMOTES_FAIL	${NODE_NAME}	-	vmbr0 not subscribed to ff02::2 — check bind-dynamic"
  fi

  # 6) Derive /64 prefix from vmbr0
  local NODE_PREFIX
  NODE_PREFIX="$(ip -6 -o addr show dev vmbr0 scope global \
                 | head -1 | awk '{print $4}' | cut -d/ -f1 \
                 | awk -F: '{printf "%s:%s:%s:%s::", $1, $2, $3, $4}')"
  if [[ -z "$NODE_PREFIX" || "$NODE_PREFIX" == "::"* ]]; then
    echo "RUNREMOTES_FAIL	${NODE_NAME}	-	cannot derive vmbr0 /64 prefix"
    return 1
  fi
  echo "node_prefix=$NODE_PREFIX"

  # 7) Per-VM migration loop
  local migrated=0 already=0 skipped=0 failed=0
  while read -r vmid status; do
    [[ "$vmid" =~ ^[0-9]+$ ]] || continue
    [[ "$vmid" -ge 100 && "$vmid" -le 9999 ]] || continue

    if [[ "$status" != "running" ]]; then
      echo "VM $vmid: stopped, skipping (re-run script after VM is started)"
      (( skipped++ )) || true
      continue
    fi

    # Compute new MAC + expected IPv6
    local VMID_HEX VMID_HI VMID_LO NEW_MAC EXPECTED_IPV6
    VMID_HEX=$(printf '%04x' "$vmid")
    VMID_HI=${VMID_HEX:0:2}
    VMID_LO=${VMID_HEX:2:2}
    NEW_MAC="52:54:00:00:${VMID_HI}:${VMID_LO}"
    EXPECTED_IPV6="${NODE_PREFIX}5054:ff:fe00:$(printf '%x' "$vmid")"

    # Read current net0 + MAC
    local CURRENT_NET0 CURRENT_MAC
    CURRENT_NET0=$(qm config "$vmid" 2>/dev/null | sed -nE 's/^net0:[[:space:]]+(.*)$/\1/p')
    if [[ -z "$CURRENT_NET0" ]]; then
      echo "RUNREMOTES_FAIL	${NODE_NAME}	${vmid}	cannot read net0 from qm config"
      (( failed++ )) || true
      continue
    fi
    CURRENT_MAC=$(echo "$CURRENT_NET0" | grep -oE '[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}' | head -1 | tr 'A-F' 'a-f')

    if [[ "$CURRENT_MAC" == "$NEW_MAC" ]]; then
      # Already migrated — just verify + update Firestore (idempotent re-runs)
      echo "VM $vmid: already on new MAC scheme; verifying ipv6=$EXPECTED_IPV6"
      if _verify_vmid_ipv6 "$vmid" "$EXPECTED_IPV6" 30; then
        if "$SYNC_DNAT" set-server-ipv6 "$vmid" "$EXPECTED_IPV6" >/dev/null 2>&1; then
          echo "  ✅ VM $vmid: ipv6 verified + Firestore synced"
          (( already++ )) || true
        else
          echo "RUNREMOTES_FAIL	${NODE_NAME}	${vmid}	firestore set-server-ipv6 failed (already-migrated VM)"
          (( failed++ )) || true
        fi
      else
        echo "RUNREMOTES_FAIL	${NODE_NAME}	${vmid}	expected $EXPECTED_IPV6 not present (MAC matches but ipv6 doesn't — Windows registry not updated?)"
        (( failed++ )) || true
      fi
      sleep "$PER_VM_SLEEP"
      continue
    fi

    # Skip non-Windows VMs — handle Linux/other manually
    local OSTYPE
    OSTYPE=$(qm config "$vmid" 2>/dev/null | awk -F': ' '/^ostype:/{print $2; exit}' | tr -d '\r')
    if [[ "$OSTYPE" != win* ]]; then
      echo "VM $vmid: ostype='$OSTYPE' (not Windows); skipping (handle non-Windows manually)"
      (( skipped++ )) || true
      continue
    fi

    echo "--- VM $vmid: migrating ${CURRENT_MAC} → ${NEW_MAC} (expected ipv6=${EXPECTED_IPV6}) ---"

    # Step A: disable RFC 7217 + privacy in Windows BEFORE changing MAC.
    # store=persistent so the change survives reboots.
    local PS_PREP='netsh interface ipv6 set global randomizeidentifiers=disabled store=persistent | Out-Null; netsh interface ipv6 set privacy state=disabled store=persistent | Out-Null'
    if ! _run_ps_via_agent "$vmid" "$PS_PREP" 60; then
      echo "RUNREMOTES_FAIL	${NODE_NAME}	${vmid}	netsh prep (disable RFC 7217 + privacy) failed"
      (( failed++ )) || true
      continue
    fi

    # Step B: change MAC live via qm set. Proxmox hot-replaces the virtio NIC
    # via QMP device_del + device_add — the guest sees a brief "NIC removed,
    # new NIC added" event but does NOT reboot.
    local NEW_NET0
    NEW_NET0=$(echo "$CURRENT_NET0" | sed -E "s/=([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}/=$NEW_MAC/")
    if ! qm set "$vmid" --net0 "$NEW_NET0" >/dev/null 2>&1; then
      echo "RUNREMOTES_FAIL	${NODE_NAME}	${vmid}	qm set --net0 failed"
      (( failed++ )) || true
      continue
    fi
    echo "  net0 updated live: $NEW_NET0"

    # Brief settle so Windows registers the new NIC
    sleep 5

    # Step C: cycle the physical adapter so Windows applies the netsh change
    # and re-derives SLAAC with the new MAC. We disable + enable all physical
    # adapters; with the new MAC + RFC 7217 disabled, Windows generates EUI-64
    # SLAAC: <prefix>::5054:ff:fe00:<vmid_hex>.
    local PS_RESET='$a = Get-NetAdapter -Physical; if ($a) { $a | Disable-NetAdapter -Confirm:$false; Start-Sleep -Seconds 4; $a | Enable-NetAdapter -Confirm:$false; Start-Sleep -Seconds 4 }'
    if ! _run_ps_via_agent "$vmid" "$PS_RESET" 90; then
      echo "RUNREMOTES_FAIL	${NODE_NAME}	${vmid}	adapter cycle (Disable/Enable-NetAdapter) failed"
      (( failed++ )) || true
      continue
    fi

    # Step D: verify expected ipv6 is bound
    if ! _verify_vmid_ipv6 "$vmid" "$EXPECTED_IPV6" 90; then
      echo "RUNREMOTES_FAIL	${NODE_NAME}	${vmid}	expected ipv6 $EXPECTED_IPV6 not present after migration"
      (( failed++ )) || true
      continue
    fi

    # Step E: update Firestore. Only do this AFTER verification — keeps NAT46
    # pointing at the previous IPv6 if the migration didn't fully succeed.
    if ! "$SYNC_DNAT" set-server-ipv6 "$vmid" "$EXPECTED_IPV6" >/dev/null 2>&1; then
      echo "RUNREMOTES_FAIL	${NODE_NAME}	${vmid}	firestore set-server-ipv6 failed after verify"
      (( failed++ )) || true
      continue
    fi
    echo "  ✅ VM $vmid: migrated. ipv6=$EXPECTED_IPV6"
    (( migrated++ )) || true
    sleep "$PER_VM_SLEEP"
  done < <(qm list 2>/dev/null | tail -n +2 | awk '{print $1, $3}')

  echo "== Finished: migrated=${migrated} already-on-new-scheme=${already} skipped=${skipped} failed=${failed} =="
}
############################################################

# Extract function body into a string
FUNC_CONTENT=$(declare -f remote_task)

NODES_FILE="/var/lib/base-nat/pve_nodes.json"
if [[ ! -f "$NODES_FILE" ]]; then
    echo "Missing $NODES_FILE"
    exit 1
fi

# Where to log per-node failure lines (RUNREMOTES_FAIL <hostname> <vmid> <reason>)
FAILURE_LOG="${FAILURE_LOG:-$(pwd)/migrate_to_deterministic_ipv6.failures.log}"
echo "Failure log: $FAILURE_LOG"

# Run only on these host numbers (e.g. 2 5 7). Leave empty to run on all (still subject to SKIP/GTE/LTE below).
ONLY_HOST_NUMS=()
# Skip these host numbers (e.g. 2 5 7). Leave empty to run on all.
SKIP_HOST_NUMS=()
# Only run when host_num >= N, or host_num <= N. Leave empty to ignore.
HOST_NUM_GTE=
HOST_NUM_LTE=

while read -r hostname ip; do
    host_num=$((10#${hostname%%-*}))
    if (( ${#ONLY_HOST_NUMS[@]} > 0 )); then
        in_only=0
        for o in "${ONLY_HOST_NUMS[@]}"; do
            if [[ $host_num -eq $o ]]; then in_only=1; break; fi
        done
        if [[ $in_only -eq 0 ]]; then continue; fi
    fi
    skip=0
    for s in "${SKIP_HOST_NUMS[@]}"; do
        if [[ $host_num -eq $s ]]; then skip=1; break; fi
    done
    if [[ $skip -eq 1 ]]; then echo "Skipping $hostname (host_num=$host_num)"; continue; fi
    if [[ -n "${HOST_NUM_GTE:-}" && $host_num -lt $HOST_NUM_GTE ]]; then echo "Skipping $hostname (host_num $host_num not >= $HOST_NUM_GTE)"; continue; fi
    if [[ -n "${HOST_NUM_LTE:-}" && $host_num -gt $HOST_NUM_LTE ]]; then echo "Skipping $hostname (host_num $host_num not <= $HOST_NUM_LTE)"; continue; fi

    echo "------------------------------------------------"
    echo "Connecting to $hostname ($ip)"

    # Send function and execute it (-n so ssh does not consume the loop stdin).
    # Tee output: full to stdout, only RUNREMOTES_FAIL lines (with timestamp + hostname)
    # appended to the failure log.
    ssh -n -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ForwardAgent=yes "root@$ip" \
        "$FUNC_CONTENT; remote_task" \
        2>&1 \
        | tee >(grep -F 'RUNREMOTES_FAIL' \
                | awk -v ts="$(date -u +'%Y-%m-%dT%H:%M:%SZ')" '{print ts "\t" $0}' \
                >> "$FAILURE_LOG") \
        || echo "❌ Failed to connect to $hostname ($ip)"
done < <(jq -r 'to_entries[] | "\(.key) \(.value)"' "$NODES_FILE" | sort)

echo "------------------------------------------------"
if [[ -s "$FAILURE_LOG" ]]; then
    echo "⚠️  Some VMs/nodes failed. Triage:"
    echo "    $FAILURE_LOG"
    echo "    ($(wc -l < "$FAILURE_LOG") line(s))"
else
    echo "✅ Run complete with no failures."
fi

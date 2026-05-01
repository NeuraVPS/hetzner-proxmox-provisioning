#!/usr/bin/env bash

############################################################
# Migrate existing Proxmox nodes to "DHCPv6 stateful + per-VM MAC pins" model.
#
# Per node (always):
#   - Pull latest sync-dnat.py from GitHub master (has regen_dhcp_pins())
#   - Move stale /etc/dnsmasq.d/vmbr0.conf.bak* out of the dir
#   - Rewrite /etc/dnsmasq.d/vmbr0.conf for stateful DHCPv6 (M=1, A=0)
#   - systemctl restart dnsmasq
#   - sync-dnat.py regen-pins → populate /etc/dnsmasq.d/vm-pins.conf with
#     `dhcp-host=<mac>,[<prefix>::<vmid_hex>],vm<vmid>,infinite` for every
#     local VM 100..9999.
#
# Per Windows VM that is RUNNING:
#   - Compute expected IPv6: <node_/64_prefix>::<vmid_hex>
#   - Via guest-agent PowerShell:
#       * Re-enable DHCPv6 on the active physical adapter (the previous
#         static-in-guest attempt set Dhcp Disabled).
#       * Remove any leftover Manual IPv6 in PolicyStore (cleanup).
#       * ipconfig /release6 + /renew6 → force DHCPv6 SOLICIT, dnsmasq
#         returns the pinned <prefix>::<vmid_hex>.
#   - Verify the expected IPv6 is bound on the guest's interface list
#   - Update servers/{id}.ipv6 in Firestore
#
# On failure at any per-VM step: log RUNREMOTES_FAIL and continue. Do NOT
# update Firestore for that VM (so existing NAT46 keeps pointing at whatever
# IPv6 was there before — fail-safe).
#
# Stopped VMs and non-Windows VMs are LOGGED to RUNREMOTES_FAIL for review.
# Re-run the script after stopped VMs are started.
#
# Idempotent: if the expected IPv6 is already bound, just sync Firestore.
############################################################

remote_task() {
  set +e

  local NODE_NAME SYNC_DNAT SYNC_DNAT_URL PER_VM_SLEEP
  NODE_NAME="$(hostname)"
  SYNC_DNAT="/var/lib/svz/snippets/sync-dnat.py"
  SYNC_DNAT_URL="https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/snippets/sync-dnat.py"
  PER_VM_SLEEP=1

  # ----- Helpers (nested so they're picked up by `declare -f remote_task`) -----

  # Run a PowerShell snippet via guest-agent using -EncodedCommand (UTF-16LE base64).
  # Sidesteps all bash/SSH/PowerShell quoting concerns. Returns 0 if exec exits
  # within $3 seconds (default 30), 1 otherwise.
  _run_ps_via_agent() {
    local _vmid="$1" _ps="$2" _timeout="${3:-30}"
    local _b64
    _b64=$(printf '%s' "$_ps" | iconv -t UTF-16LE | base64 -w0)
    [[ -n "$_b64" ]] || { echo "  _run_ps: iconv/base64 failed" >&2; return 1; }

    local _exec_out _pid _attempt=0
    while (( _attempt < 6 )); do
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
        sleep 3
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
      sleep 2
      (( _elapsed += 2 )) || true
    done
    return 1
  }

  # Verify VM has the expected IPv6 bound. Uses canonical IPv6 comparison.
  # Returns 0 if found within $3 seconds (default 15).
  _verify_vmid_ipv6() {
    local _vmid="$1" _expected="$2" _timeout="${3:-15}" _elapsed=0 _ifaces
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
      sleep 3
      (( _elapsed += 3 )) || true
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

  # 2) Apply dnsmasq config: stateful DHCPv6 (M=1, A=0) + IPv4 DHCP + DNS.
  # vm-pins.conf (separate file) holds per-VM MAC pins, written by regen-pins.
  cat > /etc/dnsmasq.d/vmbr0.conf <<'CONF'
interface=vmbr0
bind-dynamic
dhcp-authoritative
dhcp-rapid-commit

# IPv4 DHCP pool for guests (v4 is DHCP-only; address may change).
dhcp-range=10.0.0.100,10.0.255.254,255.255.0.0,12h
dhcp-option=3,10.0.0.1
dhcp-option=6,185.12.64.1,185.12.64.2

# IPv6: stateful DHCPv6 with per-VM MAC pins (in /etc/dnsmasq-vm-pins.hosts,
# regenerated by sync-dnat.py). Pin file lives OUTSIDE /etc/dnsmasq.d/ so
# SIGHUP (systemctl reload) re-reads it — files inside /etc/dnsmasq.d/ only
# take effect on a full restart. RA flags default: M=1, A=0, O=1.
enable-ra
dhcp-range=::64,::ffff,constructor:vmbr0,12h
dhcp-hostsfile=/etc/dnsmasq-vm-pins.hosts
dhcp-option=option6:dns-server,[2a01:4ff:ff00::add:1],[2a01:4ff:ff00::add:2]
CONF

  # 3) Ensure pin file exists (sync-dnat regen-pins will populate it below).
  # Also remove legacy pin file from when pins lived inside /etc/dnsmasq.d/.
  : > /etc/dnsmasq-vm-pins.hosts
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

  # 4.5) Populate vm-pins.conf for every local VM (and reload dnsmasq).
  # Run BEFORE the per-VM loop so DHCPv6 SOLICITs from VMs find their pin.
  if ! "$SYNC_DNAT" regen-pins >/dev/null 2>&1; then
    echo "RUNREMOTES_FAIL	${NODE_NAME}	-	sync-dnat.py regen-pins failed"
    return 1
  fi

  # 5) Self-check: dnsmasq subscribed to ff02::2 (RA-on-RS works)
  if ! ip -6 maddr show dev vmbr0 2>/dev/null | grep -q 'ff02::2'; then
    echo "RUNREMOTES_FAIL	${NODE_NAME}	-	vmbr0 not subscribed to ff02::2 — check bind-dynamic"
  fi

  # 6) Derive /64 prefix from vmbr0's first global v6 address (drop CIDR, take
  # first 4 hextets, append "::"). Form: "2a01:4f9:6a:44eb::".
  local NODE_PREFIX
  NODE_PREFIX="$(ip -6 -o addr show dev vmbr0 scope global \
                  | head -1 | awk '{print $4}' | cut -d/ -f1 \
                  | awk -F: '{printf "%s:%s:%s:%s::", $1, $2, $3, $4}')"
  if [[ -z "$NODE_PREFIX" || "$NODE_PREFIX" == "::"* ]]; then
    echo "RUNREMOTES_FAIL	${NODE_NAME}	-	cannot derive vmbr0 /64 prefix"
    return 1
  fi
  echo "node_prefix=${NODE_PREFIX}"

  # 7) Per-VM static IPv6 assignment loop
  local assigned=0 already=0 skipped=0 failed=0
  while read -r vmid status; do
    [[ "$vmid" =~ ^[0-9]+$ ]] || continue
    [[ "$vmid" -ge 100 && "$vmid" -le 9999 ]] || continue

    local EXPECTED_IPV6
    EXPECTED_IPV6="${NODE_PREFIX}$(printf '%x' "$vmid")"

    if [[ "$status" != "running" ]]; then
      echo "RUNREMOTES_FAIL	${NODE_NAME}	${vmid}	stopped (re-run script after VM is started; expected ipv6=${EXPECTED_IPV6})"
      (( skipped++ )) || true
      continue
    fi

    # Skip non-Windows VMs — handle Linux/other manually
    local OSTYPE
    OSTYPE=$(qm config "$vmid" 2>/dev/null | awk -F': ' '/^ostype:/{print $2; exit}' | tr -d '\r')
    if [[ "$OSTYPE" != win* ]]; then
      echo "RUNREMOTES_FAIL	${NODE_NAME}	${vmid}	ostype='${OSTYPE}' (not Windows; handle manually; expected ipv6=${EXPECTED_IPV6})"
      (( skipped++ )) || true
      continue
    fi

    # Fast path: expected v6 already bound → just sync Firestore
    if _verify_vmid_ipv6 "$vmid" "$EXPECTED_IPV6" 3; then
      if "$SYNC_DNAT" set-server-ipv6 "$vmid" "$EXPECTED_IPV6" >/dev/null 2>&1; then
        echo "VM $vmid: ${EXPECTED_IPV6} already bound; Firestore synced"
        (( already++ )) || true
      else
        echo "RUNREMOTES_FAIL	${NODE_NAME}	${vmid}	firestore set-server-ipv6 failed (already-bound VM)"
        (( failed++ )) || true
      fi
      sleep "$PER_VM_SLEEP"
      continue
    fi

    echo "--- VM $vmid: forcing DHCPv6 acquisition of pinned ${EXPECTED_IPV6} ---"

    # PowerShell:
    #   1. Find active physical adapter
    #   2. Re-enable DHCPv6 (the previous static-in-guest attempt set
    #      Dhcp Disabled, which would prevent Windows from using DHCPv6 now).
    #   3. Remove any leftover Manual IPv6 in PersistentStore/ActiveStore from
    #      the previous attempt (clean state).
    #   4. ipconfig /release6 + /renew6 → fresh DHCPv6 SOLICIT to dnsmasq,
    #      which returns the pinned <prefix>::<vmid_hex>.
    local PS_ACQUIRE
    PS_ACQUIRE=$(cat <<'PSEOF'
$ErrorActionPreference = 'SilentlyContinue'
$a = Get-NetAdapter -Physical | Where-Object Status -eq 'Up' | Select-Object -First 1
if (-not $a) { Write-Error 'no active physical adapter'; exit 1 }
$idx = $a.ifIndex
Set-NetIPInterface -InterfaceIndex $idx -AddressFamily IPv6 -Dhcp Enabled -ErrorAction SilentlyContinue | Out-Null
foreach ($st in 'PersistentStore', 'ActiveStore') {
  Get-NetIPAddress -InterfaceIndex $idx -AddressFamily IPv6 -PolicyStore $st -ErrorAction SilentlyContinue |
    Where-Object { $_.PrefixOrigin -eq 'Manual' } |
    ForEach-Object {
      Remove-NetIPAddress -InterfaceIndex $idx -IPAddress $_.IPAddress -PolicyStore $st -Confirm:$false -ErrorAction SilentlyContinue
    }
}
ipconfig /release6 | Out-Null
ipconfig /renew6 | Out-Null
exit 0
PSEOF
)

    if ! _run_ps_via_agent "$vmid" "$PS_ACQUIRE" 60; then
      echo "RUNREMOTES_FAIL	${NODE_NAME}	${vmid}	guest-agent DHCPv6 release/renew failed (agent down or PS error)"
      (( failed++ )) || true
      continue
    fi

    # Verify expected v6 is bound
    if ! _verify_vmid_ipv6 "$vmid" "$EXPECTED_IPV6" 15; then
      echo "RUNREMOTES_FAIL	${NODE_NAME}	${vmid}	${EXPECTED_IPV6} not bound after assignment"
      (( failed++ )) || true
      continue
    fi

    # Update Firestore (only after verification — keeps NAT46 pointing at the
    # previous IPv6 if anything went wrong above).
    if ! "$SYNC_DNAT" set-server-ipv6 "$vmid" "$EXPECTED_IPV6" >/dev/null 2>&1; then
      echo "RUNREMOTES_FAIL	${NODE_NAME}	${vmid}	firestore set-server-ipv6 failed after verify"
      (( failed++ )) || true
      continue
    fi

    echo "  ✅ VM $vmid: static ipv6=${EXPECTED_IPV6}"
    (( assigned++ )) || true
    sleep "$PER_VM_SLEEP"
  done < <(qm list 2>/dev/null | tail -n +2 | awk '{print $1, $3}')

  echo "== Finished: assigned=${assigned} already=${already} skipped=${skipped} failed=${failed} =="
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
ONLY_HOST_NUMS=(3)
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

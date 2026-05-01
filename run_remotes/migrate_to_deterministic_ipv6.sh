#!/usr/bin/env bash

############################################################
# Migrate existing Proxmox nodes to "manual static IPv6 in-guest" model.
#
# Per node:
#   - Pull latest sync-dnat.py from GitHub master
#   - Move stale /etc/dnsmasq.d/vmbr0.conf.bak* out of the dir
#   - Rewrite /etc/dnsmasq.d/vmbr0.conf to IPv4-only (no RA, no DHCPv6).
#     Cleans up any IPv6 pin files from prior approaches.
#   - systemctl restart dnsmasq
#
# Per Windows VM that is RUNNING:
#   - Compute expected IPv6: <node_/64_prefix>::<vmid_hex>
#   - Compute gateway: the node's actual vmbr0 global v6 (whatever it is —
#     :1, :2, etc. — not assumed)
#   - Via guest-agent PowerShell, all writes persistent:
#       1. Reset RFC 7217 / privacy / randomizeidentifiers to defaults
#          (irrelevant once RouterDiscovery is off, but keeps the VM clean)
#       2. Disable RouterDiscovery on the IPv6 interface (Windows ignores
#          ANY RA — no SLAAC, no RA-derived gateway, no auto-config to fight)
#       3. Disable DHCPv6 on the IPv6 interface (no SOLICIT, no INFORMATION-
#          REQUEST — Windows DHCPv6 client doesn't run for v6 on this iface)
#       4. Remove ALL non-link-local IPv6 addresses from PolicyStore (clean
#          state — drops leftover SLAAC, manual, and Manual-store entries
#          from prior attempts)
#       5. Add static IPv6 via legacy `netsh interface ipv6 add address ...
#          store=persistent` (writes to Tcpip6\Parameters\Interfaces\<guid>
#          registry — same path the GUI uses, more reliable on Server 2025
#          than New-NetIPAddress -PolicyStore PersistentStore)
#       6. Add default IPv6 route via `netsh interface ipv6 add route ... store=persistent`
#       7. Set static IPv6 DNS via `netsh interface ipv6 set/add dnsserver`
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

  local NODE_NAME SYNC_DNAT SYNC_DNAT_URL PER_VM_SLEEP DNS6_PRIMARY DNS6_SECONDARY
  NODE_NAME="$(hostname)"
  SYNC_DNAT="/var/lib/svz/snippets/sync-dnat.py"
  SYNC_DNAT_URL="https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/snippets/sync-dnat.py"
  PER_VM_SLEEP=1
  DNS6_PRIMARY="2a01:4ff:ff00::add:1"
  DNS6_SECONDARY="2a01:4ff:ff00::add:2"

  # ----- Helpers -----

  # Run a PowerShell snippet via guest-agent using -EncodedCommand (UTF-16LE base64).
  # Returns 0 only if PS exits with exitcode==0 within $3 seconds (default 60).
  # On any non-success path, sets _PS_LAST_ERROR to a single-line description
  # (exitcode + truncated stdout/stderr from agent exec-status, or the agent
  # error reason) so the caller can include it in RUNREMOTES_FAIL log lines.
  _PS_LAST_ERROR=""
  _run_ps_via_agent() {
    _PS_LAST_ERROR=""
    local _vmid="$1" _ps="$2" _timeout="${3:-60}"
    local _b64
    _b64=$(printf '%s' "$_ps" | iconv -t UTF-16LE | base64 -w0)
    [[ -n "$_b64" ]] || { _PS_LAST_ERROR="iconv/base64 failed"; return 1; }

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
      _PS_LAST_ERROR="pvesh exec error: ${_exec_out:0:200}"
      return 1
    done
    if [[ -z "$_pid" || "$_pid" -le 0 ]]; then
      _PS_LAST_ERROR="guest agent never came up after 6 retries"
      return 1
    fi

    local _elapsed=0 _st
    while (( _elapsed < _timeout )); do
      _st=$(pvesh get "/nodes/${NODE_NAME}/qemu/${_vmid}/agent/exec-status" \
        --output-format json --pid "$_pid" 2>/dev/null || true)
      if echo "$_st" | grep -qE '"exited"\s*:\s*1'; then
        # Parse exitcode + out-data + err-data robustly (JSON has escapes).
        local _parsed
        _parsed=$(echo "$_st" | python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
except Exception:
    print("0||||"); sys.exit(0)
ec = d.get("exitcode", 0) or 0
od = (d.get("out-data") or "")[:200].replace("\n"," ").replace("\r"," ").replace("|","/")
ed = (d.get("err-data") or "")[:200].replace("\n"," ").replace("\r"," ").replace("|","/")
print(f"{ec}||||{od}||||{ed}")
' 2>/dev/null)
        local _ec="${_parsed%%\|\|\|\|*}"
        local _rest="${_parsed#*\|\|\|\|}"
        local _out_data="${_rest%%\|\|\|\|*}"
        local _err_data="${_rest##*\|\|\|\|}"
        if [[ "$_ec" == "0" ]]; then
          return 0
        fi
        _PS_LAST_ERROR="PS exitcode=${_ec} stderr='${_err_data}' stdout='${_out_data}'"
        return 1
      fi
      sleep 2
      (( _elapsed += 2 )) || true
    done
    _PS_LAST_ERROR="PS timed out after ${_timeout}s"
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

  # 2) Apply IPv4-only dnsmasq config. Each VM is configured manually with a
  # static IPv6 in-guest below, with RouterDiscovery + DHCPv6 disabled so
  # Windows has zero auto-config to compete with the static.
  cat > /etc/dnsmasq.d/vmbr0.conf <<'CONF'
interface=vmbr0
bind-dynamic
dhcp-authoritative
dhcp-rapid-commit

# IPv4 DHCP pool for guests.
dhcp-range=10.0.0.100,10.0.255.254,255.255.0.0,12h
dhcp-option=3,10.0.0.1
dhcp-option=6,185.12.64.1,185.12.64.2

# IPv6: NOTHING from the server. Each VM has a static <prefix>::<vmid_hex>
# configured in-guest via legacy netsh (store=persistent).
CONF

  # 3) Clean up leftovers from previous IPv6 dnsmasq models
  rm -f /etc/dnsmasq-vm-pins.hosts /etc/dnsmasq.d/vm-pins.conf

  # 4) Restart dnsmasq
  if ! systemctl restart dnsmasq; then
    echo "RUNREMOTES_FAIL	${NODE_NAME}	-	dnsmasq restart failed (check journalctl -u dnsmasq)"
    return 1
  fi
  if ! systemctl is-active --quiet dnsmasq; then
    echo "RUNREMOTES_FAIL	${NODE_NAME}	-	dnsmasq not active after restart"
    return 1
  fi

  # 5) Derive /64 prefix and gateway from vmbr0's first global v6 address.
  # Gateway = whatever IPv6 the node has bound on vmbr0 (could be ::1, ::2,
  # or anything else — we don't assume). Prefix = first 4 hextets + "::".
  local NODE_GATEWAY NODE_PREFIX
  NODE_GATEWAY="$(ip -6 -o addr show dev vmbr0 scope global \
                  | head -1 | awk '{print $4}' | cut -d/ -f1)"
  if [[ -z "$NODE_GATEWAY" ]]; then
    echo "RUNREMOTES_FAIL	${NODE_NAME}	-	cannot derive vmbr0 global v6 address (gateway)"
    return 1
  fi
  NODE_PREFIX="$(echo "$NODE_GATEWAY" | awk -F: '{printf "%s:%s:%s:%s::", $1, $2, $3, $4}')"
  if [[ -z "$NODE_PREFIX" || "$NODE_PREFIX" == "::"* ]]; then
    echo "RUNREMOTES_FAIL	${NODE_NAME}	-	cannot derive vmbr0 /64 prefix"
    return 1
  fi
  echo "node_prefix=${NODE_PREFIX} gateway=${NODE_GATEWAY}"

  # 6) Per-VM static IPv6 configuration loop
  local configured=0 already=0 skipped=0 failed=0
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

    echo "--- VM $vmid: configuring static ${EXPECTED_IPV6}/64 gw=${NODE_GATEWAY} ---"

    # PowerShell: full manual IPv6 config via legacy netsh + Set-NetIPInterface,
    # all writes persistent. Adapter alias is read dynamically from the active
    # physical adapter (no hardcoded "Ethernet" — it may be "Ethernet 2",
    # "Local Area Connection", etc. on customer-customized VMs).
    local PS_CONFIGURE
    PS_CONFIGURE=$(cat <<PSEOF
\$ErrorActionPreference = 'SilentlyContinue'
\$exp   = '${EXPECTED_IPV6}'
\$gw    = '${NODE_GATEWAY}'
\$dns1  = '${DNS6_PRIMARY}'
\$dns2  = '${DNS6_SECONDARY}'

\$a = Get-NetAdapter -Physical | Where-Object Status -eq 'Up' | Select-Object -First 1
if (-not \$a) { Write-Error 'no active physical adapter'; exit 1 }
\$idx   = \$a.ifIndex
\$alias = \$a.InterfaceAlias

# 1) Reset RFC 7217 / randomizeidentifiers / privacy to Windows defaults.
# Irrelevant once RouterDiscovery is Disabled (no SLAAC happens), but keeps
# the VM clean of toggles left over from previous experiments.
Set-ItemProperty -Path "HKLM:\\SYSTEM\\CurrentControlSet\\Services\\Tcpip6\\Parameters" -Name "EnableStableAddresses" -Value 1 -Type DWord -Force -ErrorAction SilentlyContinue | Out-Null
& netsh interface ipv6 set global randomizeidentifiers=enabled store=persistent | Out-Null
& netsh interface ipv6 set privacy state=enabled store=persistent | Out-Null

# 2) Disable RouterDiscovery + Dhcp on the IPv6 interface, BOTH stores.
# Without this, Windows still processes RA from any other source and may
# flush manual addresses on RA events.
foreach (\$store in 'PersistentStore', 'ActiveStore') {
  Set-NetIPInterface -InterfaceIndex \$idx -AddressFamily IPv6 -RouterDiscovery Disabled -PolicyStore \$store -ErrorAction SilentlyContinue | Out-Null
  Set-NetIPInterface -InterfaceIndex \$idx -AddressFamily IPv6 -Dhcp Disabled -PolicyStore \$store -ErrorAction SilentlyContinue | Out-Null
}

# 3) Remove ALL non-link-local IPv6 from BOTH stores. Cleans up SLAAC, manual,
# DHCPv6, and PolicyStore leftovers from previous attempts.
foreach (\$store in 'PersistentStore', 'ActiveStore') {
  Get-NetIPAddress -InterfaceIndex \$idx -AddressFamily IPv6 -PolicyStore \$store -ErrorAction SilentlyContinue |
    Where-Object { \$_.PrefixOrigin -ne 'WellKnown' -and \$_.IPAddress -notlike 'fe80*' } |
    ForEach-Object {
      Remove-NetIPAddress -InterfaceIndex \$idx -IPAddress \$_.IPAddress -PolicyStore \$store -Confirm:\$false -ErrorAction SilentlyContinue
    }
}

# Also remove any stale default IPv6 route (we'll add our own below)
Get-NetRoute -InterfaceIndex \$idx -AddressFamily IPv6 -ErrorAction SilentlyContinue |
  Where-Object { \$_.DestinationPrefix -eq '::/0' } |
  ForEach-Object {
    Remove-NetRoute -InterfaceIndex \$idx -DestinationPrefix '::/0' -Confirm:\$false -ErrorAction SilentlyContinue
  }

# 4) Static address + default route + DNS via LEGACY netsh (store=persistent).
# This writes to HKLM\\SYSTEM\\CurrentControlSet\\Services\\Tcpip6\\Parameters\\Interfaces\\<guid>
# — the same registry path the Network Properties GUI uses. More reliable on
# Server 2025 than New-NetIPAddress -PolicyStore PersistentStore (NSI).
& netsh interface ipv6 add address  "\$alias" "\$exp/64"  store=persistent | Out-Null
& netsh interface ipv6 add route    "::/0" "\$alias" "\$gw" store=persistent | Out-Null
& netsh interface ipv6 set dnsserver "\$alias" static \$dns1 primary validate=no | Out-Null
& netsh interface ipv6 add dnsserver "\$alias"        \$dns2 index=2 validate=no | Out-Null

exit 0
PSEOF
)

    if ! _run_ps_via_agent "$vmid" "$PS_CONFIGURE" 60; then
      echo "RUNREMOTES_FAIL	${NODE_NAME}	${vmid}	netsh static IPv6 config failed: ${_PS_LAST_ERROR}"
      (( failed++ )) || true
      continue
    fi

    # Verify expected v6 is bound. If not, dump current ifaces into the failure
    # log so the operator can see what Windows actually has bound.
    if ! _verify_vmid_ipv6 "$vmid" "$EXPECTED_IPV6" 15; then
      local _current_v6
      _current_v6=$(qm guest cmd "$vmid" network-get-interfaces 2>/dev/null | python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
except Exception:
    print(""); sys.exit(0)
ifaces = d.get("result", d) if isinstance(d, dict) else d
if not isinstance(ifaces, list): print(""); sys.exit(0)
out = []
for i in ifaces:
    if not isinstance(i, dict): continue
    for a in (i.get("ip-addresses") or []):
        if not isinstance(a, dict): continue
        if a.get("ip-address-type") == "ipv6":
            ip = str(a.get("ip-address",""))
            if ip and not ip.lower().startswith("fe80"): out.append(ip)
print(",".join(out) or "none")
' 2>/dev/null)
      echo "RUNREMOTES_FAIL	${NODE_NAME}	${vmid}	expected ${EXPECTED_IPV6} not bound after netsh config; current_v6=[${_current_v6:-unknown}]"
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
    (( configured++ )) || true
    sleep "$PER_VM_SLEEP"
  done < <(qm list 2>/dev/null | tail -n +2 | awk '{print $1, $3}')

  echo "== Finished: configured=${configured} already=${already} skipped=${skipped} failed=${failed} =="
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
SKIP_HOST_NUMS=(3)
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

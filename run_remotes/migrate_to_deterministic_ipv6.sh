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

  local NODE_NAME SYNC_DNAT SYNC_DNAT_URL DNS6_PRIMARY DNS6_SECONDARY MAX_PARALLEL
  NODE_NAME="$(hostname)"
  SYNC_DNAT="/var/lib/svz/snippets/sync-dnat.py"
  SYNC_DNAT_URL="https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/snippets/sync-dnat.py"
  DNS6_PRIMARY="2a01:4ff:ff00::add:1"
  DNS6_SECONDARY="2a01:4ff:ff00::add:2"
  MAX_PARALLEL=5

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
    while (( _attempt < 3 )); do
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
        sleep 2
        continue
      fi
      _PS_LAST_ERROR="pvesh exec error: ${_exec_out:0:200}"
      return 1
    done
    if [[ -z "$_pid" || "$_pid" -le 0 ]]; then
      _PS_LAST_ERROR="guest agent never came up after 3 retries"
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
      sleep 1
      (( _elapsed += 1 )) || true
    done
    _PS_LAST_ERROR="PS timed out after ${_timeout}s"
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

  # Per-VM worker — runs in a subshell. Stdout is captured to a per-VM log
  # file by the caller; the VM's outcome (configured/already/skipped/failed)
  # is written to a per-VM kind file. The kind file is what the parent uses
  # to aggregate counters at the end.
  _process_vm() {
    local vmid="$1" status="$2" tmpdir="$3"
    local kind_file="$tmpdir/${vmid}.kind"
    local EXPECTED_IPV6 OSTYPE
    EXPECTED_IPV6="${NODE_PREFIX}$(printf '%x' "$vmid")"

    if [[ "$status" != "running" ]]; then
      echo "RUNREMOTES_FAIL	${NODE_NAME}	${vmid}	stopped (re-run script after VM is started; expected ipv6=${EXPECTED_IPV6})"
      echo skipped > "$kind_file"
      return
    fi

    OSTYPE=$(qm config "$vmid" 2>/dev/null | awk -F': ' '/^ostype:/{print $2; exit}' | tr -d '\r')
    if [[ "$OSTYPE" != win* ]]; then
      echo "RUNREMOTES_FAIL	${NODE_NAME}	${vmid}	ostype='${OSTYPE}' (not Windows; handle manually; expected ipv6=${EXPECTED_IPV6})"
      echo skipped > "$kind_file"
      return
    fi

    local PS_CONFIGURE
    PS_CONFIGURE=$(cat <<PSEOF
\$ErrorActionPreference = 'SilentlyContinue'
\$exp   = '${EXPECTED_IPV6}'
\$gw    = '${NODE_GATEWAY}'
\$dns1  = '${DNS6_PRIMARY}'
\$dns2  = '${DNS6_SECONDARY}'

\$a = Get-NetAdapter -Physical | Where-Object Status -eq 'Up' | Select-Object -First 1
if (-not \$a) { [Console]::Error.WriteLine('no active physical adapter'); exit 1 }
\$idx   = \$a.ifIndex
\$alias = \$a.InterfaceAlias

function Test-Configured {
  foreach (\$store in 'ActiveStore', 'PersistentStore') {
    \$ac = Get-NetIPAddress -InterfaceIndex \$idx -IPAddress \$exp -PolicyStore \$store -ErrorAction SilentlyContinue
    if (-not (\$ac -and \$ac.PrefixOrigin -eq 'Manual')) { return \$false }
    \$rc = Get-NetRoute -InterfaceIndex \$idx -DestinationPrefix '::/0' -AddressFamily IPv6 -PolicyStore \$store -ErrorAction SilentlyContinue | Where-Object { \$_.NextHop -eq \$gw }
    if (-not \$rc) { return \$false }
  }
  \$d = (Get-DnsClientServerAddress -InterfaceIndex \$idx -AddressFamily IPv6 -ErrorAction SilentlyContinue).ServerAddresses
  if (-not \$d -or -not (\$d -contains \$dns1)) { return \$false }
  return \$true
}

if (Test-Configured) { exit 10 }

Set-ItemProperty -Path "HKLM:\\SYSTEM\\CurrentControlSet\\Services\\Tcpip6\\Parameters" -Name "EnableStableAddresses" -Value 1 -Type DWord -Force -ErrorAction SilentlyContinue | Out-Null
& netsh interface ipv6 set global randomizeidentifiers=enabled store=persistent 2>&1 | Out-Null
& netsh interface ipv6 set privacy state=enabled store=persistent 2>&1 | Out-Null

foreach (\$store in 'PersistentStore', 'ActiveStore') {
  Set-NetIPInterface -InterfaceIndex \$idx -AddressFamily IPv6 -RouterDiscovery Disabled -PolicyStore \$store -ErrorAction SilentlyContinue | Out-Null
  Set-NetIPInterface -InterfaceIndex \$idx -AddressFamily IPv6 -Dhcp Disabled -PolicyStore \$store -ErrorAction SilentlyContinue | Out-Null
}

foreach (\$store in 'PersistentStore', 'ActiveStore') {
  Get-NetIPAddress -InterfaceIndex \$idx -AddressFamily IPv6 -PolicyStore \$store -ErrorAction SilentlyContinue |
    Where-Object { \$_.PrefixOrigin -ne 'WellKnown' -and \$_.IPAddress -notlike 'fe80*' } |
    ForEach-Object {
      Remove-NetIPAddress -InterfaceIndex \$idx -IPAddress \$_.IPAddress -PolicyStore \$store -Confirm:\$false -ErrorAction SilentlyContinue
    }
}
Get-NetRoute -InterfaceIndex \$idx -AddressFamily IPv6 -ErrorAction SilentlyContinue |
  Where-Object { \$_.DestinationPrefix -eq '::/0' } |
  ForEach-Object {
    Remove-NetRoute -InterfaceIndex \$idx -DestinationPrefix '::/0' -Confirm:\$false -ErrorAction SilentlyContinue
  }

foreach (\$store in 'active', 'persistent') {
  & netsh interface ipv6 delete address "\$alias" "\$exp"        store=\$store 2>&1 | Out-Null
  & netsh interface ipv6 add    address "\$alias" "\$exp/64"     store=\$store 2>&1 | Out-Null
  & netsh interface ipv6 delete route   "::/0" "\$alias" "\$gw"  store=\$store 2>&1 | Out-Null
  & netsh interface ipv6 add    route   "::/0" "\$alias" "\$gw"  store=\$store 2>&1 | Out-Null
}
& netsh interface ipv6 set dnsserver "\$alias" static \$dns1 primary validate=no 2>&1 | Out-Null
& netsh interface ipv6 add dnsserver "\$alias" \$dns2 index=2 validate=no 2>&1 | Out-Null

# 4b) ALSO write via NSI cmdlets so Get-NetIPAddress/Get-NetRoute see the
# entry. netsh's persistent store and NSI's PersistentStore aren't always
# synced on Server 2025 — netsh provides reboot-reliable persistence, but
# NSI is what we verify against. New-NetIP* fail silently if the entry
# already exists (-ErrorAction SilentlyContinue swallows it).
foreach (\$store in 'PersistentStore', 'ActiveStore') {
  New-NetIPAddress -InterfaceIndex \$idx -IPAddress \$exp -PrefixLength 64 -AddressFamily IPv6 -PolicyStore \$store -ErrorAction SilentlyContinue | Out-Null
  New-NetRoute     -InterfaceIndex \$idx -DestinationPrefix '::/0' -NextHop \$gw -AddressFamily IPv6 -PolicyStore \$store -ErrorAction SilentlyContinue | Out-Null
}

\$errors = @()
foreach (\$pair in @(@{Store='ActiveStore'; Tag='active'}, @{Store='PersistentStore'; Tag='persistent'})) {
  \$ac = Get-NetIPAddress -InterfaceIndex \$idx -IPAddress \$exp -PolicyStore \$pair.Store -ErrorAction SilentlyContinue
  if (-not \$ac) {
    \$errors += "addr-\$(\$pair.Tag): not present"
  } elseif (\$ac.PrefixOrigin -ne 'Manual') {
    \$errors += "addr-\$(\$pair.Tag): origin=\$(\$ac.PrefixOrigin) (expected Manual)"
  }
  \$rc = Get-NetRoute -InterfaceIndex \$idx -DestinationPrefix '::/0' -AddressFamily IPv6 -PolicyStore \$pair.Store -ErrorAction SilentlyContinue |
    Where-Object { \$_.NextHop -eq \$gw }
  if (-not \$rc) {
    \$errors += "route-\$(\$pair.Tag): no ::/0 via \$gw"
  }
}
\$d = (Get-DnsClientServerAddress -InterfaceIndex \$idx -AddressFamily IPv6 -ErrorAction SilentlyContinue).ServerAddresses
if (-not \$d -or -not (\$d -contains \$dns1)) {
  \$errors += "dns: \$dns1 missing (have='\$(\$d -join ',')')"
}

if (\$errors.Count -gt 0) {
  \$msg = (\$errors -join ' || ')
  if (\$msg.Length -gt 400) { \$msg = \$msg.Substring(0, 400) }
  [Console]::Error.WriteLine(\$msg)
  exit 2
}

exit 0
PSEOF
)

    _run_ps_via_agent "$vmid" "$PS_CONFIGURE" 60
    local _ps_rc=$? _kind=""
    if [[ "$_ps_rc" -eq 0 ]]; then
      _kind="configured"
    elif [[ "$_PS_LAST_ERROR" == *"exitcode=10"* ]]; then
      _kind="already"
    else
      echo "RUNREMOTES_FAIL	${NODE_NAME}	${vmid}	netsh static IPv6 config failed: ${_PS_LAST_ERROR}"
      echo failed > "$kind_file"
      return
    fi

    if ! "$SYNC_DNAT" set-server-ipv6 "$vmid" "$EXPECTED_IPV6" >/dev/null 2>&1; then
      echo "RUNREMOTES_FAIL	${NODE_NAME}	${vmid}	firestore set-server-ipv6 failed (kind=${_kind})"
      echo failed > "$kind_file"
      return
    fi

    if [[ "$_kind" == "already" ]]; then
      echo "VM $vmid: ${EXPECTED_IPV6} already configured; Firestore synced"
      echo already > "$kind_file"
    else
      echo "  ✅ VM $vmid: static ipv6=${EXPECTED_IPV6}"
      echo configured > "$kind_file"
    fi
  }

  # 6) Per-VM static IPv6 configuration loop — parallelized, max MAX_PARALLEL
  # workers concurrently. Each worker writes its stdout to a per-VM log file
  # and its outcome to a per-VM kind file in TMPDIR. After all workers finish
  # we replay the logs in qm-list order (so output isn't interleaved) and
  # aggregate the counters.
  local TMPDIR jobs=0
  TMPDIR=$(mktemp -d /tmp/migv6.XXXXXX)
  declare -a vm_order=()

  while read -r vmid status; do
    [[ "$vmid" =~ ^[0-9]+$ ]] || continue
    [[ "$vmid" -ge 100 && "$vmid" -le 9999 ]] || continue
    vm_order+=("$vmid")

    ( _process_vm "$vmid" "$status" "$TMPDIR" ) > "$TMPDIR/${vmid}.log" 2>&1 &
    (( jobs++ )) || true
    if (( jobs >= MAX_PARALLEL )); then
      wait -n 2>/dev/null
      (( jobs-- )) || true
    fi
  done < <(qm list 2>/dev/null | tail -n +2 | awk '{print $1, $3}')

  wait 2>/dev/null

  # Replay logs in order; tally kinds.
  local configured=0 already=0 skipped=0 failed=0
  for vmid in "${vm_order[@]}"; do
    [[ -f "$TMPDIR/${vmid}.log" ]] && cat "$TMPDIR/${vmid}.log"
    local kind=""
    [[ -f "$TMPDIR/${vmid}.kind" ]] && kind=$(<"$TMPDIR/${vmid}.kind")
    case "$kind" in
      configured) (( configured++ )) || true ;;
      already)    (( already++ ))    || true ;;
      skipped)    (( skipped++ ))    || true ;;
      failed|"")  (( failed++ ))     || true ;;
    esac
  done
  rm -rf "$TMPDIR"

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
ONLY_HOST_NUMS=(62)
# Skip these host numbers (e.g. 2 5 7). Leave empty to run on all.
SKIP_HOST_NUMS=(3 26 35)
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

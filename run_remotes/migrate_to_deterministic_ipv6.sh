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
# Stopped Windows VMs are auto-handled: started → configured → gracefully
# shutdown. Only failures in that flow (qm start, agent ping, PS_CONFIGURE,
# Firestore sync, or shutdown) are logged to RUNREMOTES_FAIL.
#
# Non-Windows VMs are skipped + logged to RUNREMOTES_FAIL for manual handling.
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
import json, re, sys
try:
    d = json.loads(sys.stdin.read())
except Exception:
    print("0||||"); sys.exit(0)
ec = d.get("exitcode", 0) or 0
def clean(s):
    s = (s or "")
    if "#< CLIXML" in s:
        # PowerShell wraps error/warning streams in CLIXML XML when emitted via
        # the agent. The actual error text lives inside <S>...</S> elements
        # (and sometimes <ToString>...</ToString>). Extract only those, ignore
        # progress objects + XML structure.
        parts = re.findall(r"<(?:S|ToString)(?:\s+[^>]*)?>([^<]*)</(?:S|ToString)>", s)
        s = " ".join(parts)
    s = re.sub(r"<[^>]+>", " ", s)
    s = (s.replace("&lt;","<").replace("&gt;",">").replace("&amp;","&")
           .replace("&quot;","\"").replace("&apos;","\x27"))
    s = re.sub(r"_x([0-9a-fA-F]{4})_", lambda m: chr(int(m.group(1),16)), s)
    s = re.sub(r"[\x00-\x08\x0e-\x1f]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s.replace("|","/")[:500]
od = clean(d.get("out-data"))
ed = clean(d.get("err-data"))
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

  # Poll `qm status` until it reports the desired value. Returns 0 on match,
  # 1 on timeout. Used to confirm graceful shutdown completed.
  _qm_wait_status() {
    local _vmid="$1" _want="$2" _timeout="${3:-300}" _elapsed=0 _cur
    while (( _elapsed < _timeout )); do
      _cur=$(qm status "$_vmid" 2>/dev/null | awk '{print $2}' | tr -d '\r')
      if [[ "$_cur" == "$_want" ]]; then return 0; fi
      sleep 2
      (( _elapsed += 2 )) || true
    done
    return 1
  }

  # Poll the qemu-guest-agent ping until it responds. Returns 0 once the agent
  # is up, 1 on timeout. Used after `qm start` for stopped VMs we need to
  # configure on a one-shot basis.
  _qm_wait_agent() {
    local _vmid="$1" _timeout="${2:-300}" _elapsed=0
    while (( _elapsed < _timeout )); do
      if qm guest cmd "$_vmid" ping >/dev/null 2>&1; then return 0; fi
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

  # Per-VM worker — runs in a subshell. Stdout is captured to a per-VM log
  # file by the caller; the VM's outcome (configured/already/skipped/failed)
  # is written to a per-VM kind file. The kind file is what the parent uses
  # to aggregate counters at the end.
  _process_vm() {
    local vmid="$1" status="$2" tmpdir="$3"
    local kind_file="$tmpdir/${vmid}.kind"
    local EXPECTED_IPV6 OSTYPE was_stopped=0
    EXPECTED_IPV6="${NODE_PREFIX}$(printf '%x' "$vmid")"

    # ostype check works for both running and stopped VMs — read from config.
    OSTYPE=$(qm config "$vmid" 2>/dev/null | awk -F': ' '/^ostype:/{print $2; exit}' | tr -d '\r')
    if [[ "$OSTYPE" != win* ]]; then
      echo "RUNREMOTES_FAIL	${NODE_NAME}	${vmid}	ostype='${OSTYPE}' (not Windows; handle manually; expected ipv6=${EXPECTED_IPV6})"
      echo skipped > "$kind_file"
      return
    fi

    # Stopped Windows VM: start it, apply config, then gracefully shut it back
    # down. Any failure in this flow → log + return without further side-effects
    # (Firestore is updated only after PS_CONFIGURE succeeds, below).
    if [[ "$status" != "running" ]]; then
      echo "VM $vmid: stopped → starting to apply static IPv6 config"
      if ! qm start "$vmid" >/dev/null 2>&1; then
        echo "RUNREMOTES_FAIL	${NODE_NAME}	${vmid}	qm start failed"
        echo failed > "$kind_file"
        return
      fi
      if ! _qm_wait_agent "$vmid" 300; then
        echo "RUNREMOTES_FAIL	${NODE_NAME}	${vmid}	guest-agent never responded after start (5m timeout); leaving VM running for triage"
        echo failed > "$kind_file"
        return
      fi
      was_stopped=1
    fi

    local PS_CONFIGURE
    PS_CONFIGURE=$(cat <<PSEOF
\$ErrorActionPreference = 'SilentlyContinue'
\$ProgressPreference = 'SilentlyContinue'
\$exp   = '${EXPECTED_IPV6}'
\$gw    = '${NODE_GATEWAY}'
\$dns1  = '${DNS6_PRIMARY}'
\$dns2  = '${DNS6_SECONDARY}'

\$a = Get-NetAdapter -Physical | Where-Object Status -eq 'Up' | Select-Object -First 1
if (-not \$a) { [Console]::Error.WriteLine('no active physical adapter'); exit 1 }
\$idx   = \$a.ifIndex
\$alias = \$a.InterfaceAlias

function Test-Configured {
  # Reboot persistence requires the address to be in NSI PolicyStore
  # PersistentStore (verified empirically on Server 2025: Windows reads from
  # NSI Persistent, NOT from HKLM\\...\\Tcpip6\\Parameters\\Interfaces\\<guid>
  # legacy registry). Active store is what's bound right now.
  \$pp = Get-NetIPAddress -InterfaceIndex \$idx -IPAddress \$exp -PolicyStore PersistentStore -ErrorAction SilentlyContinue
  if (-not (\$pp -and \$pp.PrefixOrigin -eq 'Manual')) { return \$false }
  \$ac = Get-NetIPAddress -InterfaceIndex \$idx -IPAddress \$exp -PolicyStore ActiveStore -ErrorAction SilentlyContinue
  if (-not (\$ac -and \$ac.PrefixOrigin -eq 'Manual')) { return \$false }
  \$rc = Get-NetRoute -InterfaceIndex \$idx -DestinationPrefix '::/0' -AddressFamily IPv6 -PolicyStore ActiveStore -ErrorAction SilentlyContinue | Where-Object { \$_.NextHop -eq \$gw }
  if (-not \$rc) { return \$false }
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

# A) Address + route via New-NetIPAddress / New-NetRoute WITHOUT -PolicyStore.
# Server 2025's NetTCPIP CIM provider rejects 'New-NetIPAddress -PolicyStore
# PersistentStore' with "Invalid parameter PolicyStore PersistentStore", but
# the default (no -PolicyStore) writes to BOTH ActiveStore AND PersistentStore
# — that's what Network Properties GUI does internally and what produces
# entries that survive reboot in NSI Persistent (verified in a known-working
# VM on another node).
\$createErrors = @()
try {
  New-NetIPAddress -InterfaceIndex \$idx -IPAddress \$exp -PrefixLength 64 -AddressFamily IPv6 -ErrorAction Stop | Out-Null
} catch {
  \$createErrors += "New-NetIPAddress: \$(\$_.Exception.Message)"
}
try {
  New-NetRoute -InterfaceIndex \$idx -DestinationPrefix '::/0' -NextHop \$gw -AddressFamily IPv6 -ErrorAction Stop | Out-Null
} catch {
  \$createErrors += "New-NetRoute: \$(\$_.Exception.Message)"
}

# B) DNS via netsh (set/add dnsserver are persistent by default and survive
# reboot in this build — confirmed by VM diagnostics).
& netsh interface ipv6 set dnsserver "\$alias" static \$dns1 primary validate=no 2>&1 | Out-Null
& netsh interface ipv6 add dnsserver "\$alias" \$dns2 index=2 validate=no 2>&1 | Out-Null

# Verify both PersistentStore (what survives reboot) and ActiveStore (live).
\$errors = @()
\$errors += \$createErrors
\$pp = Get-NetIPAddress -InterfaceIndex \$idx -IPAddress \$exp -PolicyStore PersistentStore -ErrorAction SilentlyContinue
if (-not (\$pp -and \$pp.PrefixOrigin -eq 'Manual')) {
  \$errors += "addr-persistent: not in NSI PersistentStore as Manual"
}
\$ac = Get-NetIPAddress -InterfaceIndex \$idx -IPAddress \$exp -PolicyStore ActiveStore -ErrorAction SilentlyContinue
if (-not \$ac) {
  \$errors += "addr-active: not bound"
} elseif (\$ac.PrefixOrigin -ne 'Manual') {
  \$errors += "addr-active: origin=\$(\$ac.PrefixOrigin) (expected Manual)"
}
\$rc = Get-NetRoute -InterfaceIndex \$idx -DestinationPrefix '::/0' -AddressFamily IPv6 -PolicyStore ActiveStore -ErrorAction SilentlyContinue |
  Where-Object { \$_.NextHop -eq \$gw }
if (-not \$rc) {
  \$errors += "route-active: no ::/0 via \$gw"
}
\$d = (Get-DnsClientServerAddress -InterfaceIndex \$idx -AddressFamily IPv6 -ErrorAction SilentlyContinue).ServerAddresses
if (-not \$d -or -not (\$d -contains \$dns1)) {
  \$errors += "dns-active: \$dns1 missing"
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

    # If we started this VM ourselves (was_stopped=1), shut it back down to
    # restore the original power state. Failure to shut down → log + fail (the
    # IPv6 was configured + persisted, but the VM is left running for triage).
    if (( was_stopped )); then
      echo "VM $vmid: gracefully shutting down (was originally stopped)"
      qm shutdown "$vmid" --timeout 300 >/dev/null 2>&1 || true
      if ! _qm_wait_status "$vmid" "stopped" 60; then
        echo "RUNREMOTES_FAIL	${NODE_NAME}	${vmid}	graceful shutdown failed (still running after qm shutdown 5m + 1m wait); IPv6 configured, VM left running for triage"
        echo failed > "$kind_file"
        return
      fi
      echo "  ✅ VM $vmid: static ipv6=${EXPECTED_IPV6} (started + configured + shut down)"
      echo configured > "$kind_file"
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
  #
  # Optional VMID filter: if the env var ONLY_VMIDS is set (whitespace-
  # separated list, e.g. "237 261"), only those VMIDs are processed. Useful
  # for re-running specific VMs that failed in a previous pass.
  local TMPDIR jobs=0
  TMPDIR=$(mktemp -d /tmp/migv6.XXXXXX)
  declare -a vm_order=()
  declare -a _ONLY_VMIDS=()
  if [[ -n "${ONLY_VMIDS:-}" ]]; then
    read -ra _ONLY_VMIDS <<< "$ONLY_VMIDS"
    echo "VMID filter active: only processing ${_ONLY_VMIDS[*]}"
  fi

  while read -r vmid status; do
    [[ "$vmid" =~ ^[0-9]+$ ]] || continue
    [[ "$vmid" -ge 100 && "$vmid" -le 9999 ]] || continue

    if (( ${#_ONLY_VMIDS[@]} > 0 )); then
      local _match=0
      for _v in "${_ONLY_VMIDS[@]}"; do
        if [[ "$_v" == "$vmid" ]]; then _match=1; break; fi
      done
      (( _match == 0 )) && continue
    fi

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
ONLY_HOST_NUMS=()
# Skip these host numbers (e.g. 2 5 7). Leave empty to run on all.
SKIP_HOST_NUMS=(3 26 35 52 62)
# Only run when host_num >= N, or host_num <= N. Leave empty to ignore.
HOST_NUM_GTE=
HOST_NUM_LTE=
# Optional per-node VMID filter: only process these VMIDs on each node.
# Useful for re-running specific VMs that failed in a previous pass.
# Leave empty to process all VMs on the node.
ONLY_VMIDS=()

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

    # Inject ONLY_VMIDS as an env var on the remote shell (single-quoted so
    # the local array doesn't get re-expanded). Empty if unset.
    only_vmids_str="${ONLY_VMIDS[*]:-}"

    ssh -n -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ForwardAgent=yes "root@$ip" \
        "ONLY_VMIDS='${only_vmids_str}'; $FUNC_CONTENT; remote_task" \
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

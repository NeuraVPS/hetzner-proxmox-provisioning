#!/usr/bin/env bash

############################################################
# One-shot rollout of deterministic IPv6 across the cluster.
#
# For every Proxmox node:
#   - Apply the new dnsmasq vmbr0.conf (stateful DHCPv6, no SLAAC)
#   - Reload dnsmasq
#   - Run sync-dnat.py regen-pins to populate /etc/dnsmasq.d/vm-pins.conf
#
# For every VM on each node:
#   - Compute deterministic ipv6 = <node_/64_prefix>::<vmid_hex>
#   - Running Windows: reset network via guest agent, verify the new ipv6
#     is up, then update servers/{id}.ipv6 in Firestore via sync-dnat.py.
#   - Stopped, or running non-Windows: pre-stage Firestore directly
#     (NAT46 will follow on next document write).
#   - On failure (no agent / verification timeout): print a structured
#     RUNREMOTES_FAIL line; the orchestrator captures these to a local
#     log so you can triage afterwards.
#
# Pulls the latest sync-dnat.py from GitHub master at the start of each
# remote_task run, same pattern as first_boot.sh — so each node always
# operates with the deterministic-ipv6 version regardless of how stale
# the previously-deployed copy is.
############################################################

remote_task() {
  set +e

  local NODE_NAME
  NODE_NAME="$(hostname)"
  local SYNC_DNAT="/var/lib/svz/snippets/sync-dnat.py"
  local SYNC_DNAT_URL="https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/snippets/sync-dnat.py"
  local PER_VM_SLEEP=5

  echo "== Running remote task on ${NODE_NAME} =="

  # 0) Fetch latest sync-dnat.py from GitHub (matches first_boot.sh pattern)
  mkdir -p /var/lib/svz/snippets
  if ! curl -fsSL "$SYNC_DNAT_URL" -o "${SYNC_DNAT}.new"; then
    echo "RUNREMOTES_FAIL	${NODE_NAME}	-	failed to download sync-dnat.py from GitHub"
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

  if [[ ! -x "$SYNC_DNAT" ]]; then
    echo "RUNREMOTES_FAIL	${NODE_NAME}	-	sync-dnat.py not executable at ${SYNC_DNAT}"
    return 1
  fi

  # 1) Apply new dnsmasq config (stateful DHCPv6, no SLAAC)
  cat > /etc/dnsmasq.d/vmbr0.conf <<'CONF'
interface=vmbr0
bind-interfaces
dhcp-authoritative
dhcp-rapid-commit

# IPv4 DHCP pool for guests
dhcp-range=10.0.0.100,10.0.255.254,255.255.0.0,720h
dhcp-option=3,10.0.0.1
dhcp-option=6,185.12.64.1,185.12.64.2

# ==== IPv6: stateful DHCPv6, no SLAAC. Per-VM addresses pinned in vm-pins.conf ====
# `static` modifier => only dhcp-host pinned addresses are handed out, AND the
# advertised prefix in the RA has A=0 (no SLAAC). M=1 stays set, so Windows
# will run its DHCPv6 client and receive the pin instead of generating a
# stable-privacy SLAAC address.
enable-ra
dhcp-range=::ff00,::fffe,constructor:vmbr0,static,64,720h
dhcp-option=option6:dns-server,[2a01:4ff:ff00::add:1],[2a01:4ff:ff00::add:2]
CONF
  touch /etc/dnsmasq.d/vm-pins.conf
  # Must restart, not reload: dnsmasq SIGHUP doesn't re-parse main config files,
  # so a `systemctl reload` would silently keep the previous vmbr0.conf in memory.
  if ! systemctl restart dnsmasq; then
    echo "RUNREMOTES_FAIL	${NODE_NAME}	-	dnsmasq restart failed (check journalctl -u dnsmasq)"
    return 1
  fi

  # 2) Regenerate dhcp-host pins
  if ! "$SYNC_DNAT" regen-pins; then
    echo "RUNREMOTES_FAIL	${NODE_NAME}	-	sync-dnat.py regen-pins failed"
  fi

  # 3) Derive /64 prefix from vmbr0 (e.g. 2a01:4f9:6a:44eb::)
  local node_prefix
  node_prefix="$(ip -6 -o addr show dev vmbr0 scope global \
                 | head -1 \
                 | awk '{print $4}' \
                 | cut -d/ -f1 \
                 | awk -F: '{printf "%s:%s:%s:%s::", $1, $2, $3, $4}')"
  if [[ -z "${node_prefix}" || "${node_prefix}" == "::"* ]]; then
    echo "RUNREMOTES_FAIL	${NODE_NAME}	-	cannot derive vmbr0 /64 prefix"
    return 1
  fi
  echo "node_prefix=${node_prefix}"

  # 4) Iterate every VM on this node
  while read -r vmid status; do
    [[ "$vmid" =~ ^[0-9]+$ ]] || continue
    [[ "$vmid" -ge 100 && "$vmid" -le 9999 ]] || continue

    local hex expected
    hex="$(printf '%x' "$vmid")"
    expected="${node_prefix}${hex}"

    echo "--- VM ${vmid} (status=${status}) → ${expected} ---"

    # Stopped: pre-stage Firestore (no live RDP traffic, NAT will be ready on next boot).
    if [[ "$status" != "running" ]]; then
      "$SYNC_DNAT" set-server-ipv6 "$vmid" "$expected" \
        || echo "RUNREMOTES_FAIL	${NODE_NAME}	${vmid}	firestore set-server-ipv6 failed (stopped pre-stage)"
      sleep "$PER_VM_SLEEP"
      continue
    fi

    # Running: branch on ostype. Non-Windows guests we just pre-stage; their DHCPv6
    # client will pick up the pin on the next renew without us needing to disturb them.
    local ostype
    ostype="$(qm config "$vmid" 2>/dev/null | awk -F': ' '/^ostype:/{print $2; exit}' | tr -d '\r')"
    if [[ "$ostype" != win* ]]; then
      "$SYNC_DNAT" set-server-ipv6 "$vmid" "$expected" \
        || echo "RUNREMOTES_FAIL	${NODE_NAME}	${vmid}	firestore set-server-ipv6 failed (non-windows running)"
      sleep "$PER_VM_SLEEP"
      continue
    fi

    # Windows + running: reset network via guest agent.
    local exec_out agent_pid agent_attempt=0 agent_poll_max=90 agent_poll_interval=5
    agent_pid=""
    while (( agent_attempt * agent_poll_interval < agent_poll_max )); do
      exec_out="$(pvesh create "/nodes/${NODE_NAME}/qemu/${vmid}/agent/exec" \
                  --output-format json \
                  --command powershell.exe \
                  --command -NoProfile \
                  --command -NonInteractive \
                  --command -Command \
                  --command 'ipconfig /release; ipconfig /release6; Disable-NetAdapter -Name Ethernet -Confirm:$false; Start-Sleep -Seconds 3; Enable-NetAdapter -Name Ethernet -Confirm:$false; Start-Sleep -Seconds 2; ipconfig /renew; ipconfig /renew6' \
                  2>&1)" || true
      agent_pid="$(echo "$exec_out" | sed -n 's/.*"pid"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' | head -1)"
      if [[ -n "$agent_pid" && "$agent_pid" -gt 0 ]]; then
        break
      fi
      if echo "$exec_out" | grep -qi "guest agent is not running"; then
        agent_attempt=$((agent_attempt + 1))
        sleep "$agent_poll_interval"
        continue
      fi
      # Other agent error — bail
      agent_pid=""
      break
    done

    if [[ -z "$agent_pid" ]]; then
      echo "RUNREMOTES_FAIL	${NODE_NAME}	${vmid}	guest agent not available for network reset"
      sleep "$PER_VM_SLEEP"
      continue
    fi

    # Wait for the agent exec to finish (release/disable/enable/renew sequence)
    local exit_elapsed=0 exit_timeout=120 status_out exec_done=0
    while (( exit_elapsed < exit_timeout )); do
      status_out="$(pvesh get "/nodes/${NODE_NAME}/qemu/${vmid}/agent/exec-status" \
                    --output-format json --pid "$agent_pid" 2>/dev/null || true)"
      if echo "$status_out" | grep -qE '"exited"\s*:\s*1'; then
        exec_done=1
        break
      fi
      sleep 5
      exit_elapsed=$((exit_elapsed + 5))
    done
    if (( exec_done == 0 )); then
      echo "RUNREMOTES_FAIL	${NODE_NAME}	${vmid}	guest agent exec did not finish within ${exit_timeout}s"
      sleep "$PER_VM_SLEEP"
      continue
    fi

    # Verify the deterministic ipv6 actually shows up on the guest's interface list.
    local verify_elapsed=0 verify_max=90 verify_interval=5 verify_ok=0 ifaces_json
    while (( verify_elapsed < verify_max )); do
      ifaces_json="$(qm guest cmd "$vmid" network-get-interfaces 2>/dev/null || true)"
      if [[ -n "$ifaces_json" ]] \
         && EXPECTED="$expected" JSON="$ifaces_json" python3 -c '
import ipaddress, json, os, sys
try:
    target = ipaddress.IPv6Address(os.environ["EXPECTED"])
except ValueError:
    sys.exit(2)
try:
    data = json.loads(os.environ["JSON"])
except Exception:
    sys.exit(1)
ifaces = data.get("result", data) if isinstance(data, dict) else data
if not isinstance(ifaces, list):
    sys.exit(1)
for iface in ifaces:
    if not isinstance(iface, dict): continue
    for addr in iface.get("ip-addresses", []):
        if not isinstance(addr, dict): continue
        ip = str(addr.get("ip-address", "") or "").strip().split("%")[0]
        if not ip: continue
        try:
            if ipaddress.IPv6Address(ip) == target:
                sys.exit(0)
        except ValueError:
            continue
sys.exit(1)
' 2>/dev/null; then
        verify_ok=1
        break
      fi
      sleep "$verify_interval"
      verify_elapsed=$((verify_elapsed + verify_interval))
    done

    if (( verify_ok == 0 )); then
      echo "RUNREMOTES_FAIL	${NODE_NAME}	${vmid}	deterministic ipv6 ${expected} not present after network reset"
      sleep "$PER_VM_SLEEP"
      continue
    fi

    if ! "$SYNC_DNAT" set-server-ipv6 "$vmid" "$expected"; then
      echo "RUNREMOTES_FAIL	${NODE_NAME}	${vmid}	firestore set-server-ipv6 failed after verification"
    fi
    sleep "$PER_VM_SLEEP"
  done < <(qm list 2>/dev/null | tail -n +2 | awk '{print $1, $3}')

  echo "== Finished =="
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

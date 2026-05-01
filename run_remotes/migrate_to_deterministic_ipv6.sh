#!/usr/bin/env bash

############################################################
# Fix dnsmasq config on existing Proxmox nodes for the
# deterministic-IPv6 setup. NODE-LEVEL ONLY — does not touch
# any VM (no network reset, no Firestore writes per VM).
#
# After this runs on a node, when its VMs reboot they will
# acquire their pinned <node_/64_prefix>::<vmid_hex> via DHCPv6
# automatically.
#
# For every Proxmox node:
#   - Fetch the latest sync-dnat.py from GitHub master
#   - Move any stale /etc/dnsmasq.d/vmbr0.conf.bak* out of the
#     dnsmasq.d directory (Debian's CONFIG_DIR only excludes
#     .dpkg-* — backups would otherwise be parsed and conflict)
#   - Rewrite /etc/dnsmasq.d/vmbr0.conf with:
#       * bind-dynamic (NOT bind-interfaces — required for RA-on-RS
#         multicast to work on Proxmox where fwbr<vmid> bridges flap)
#       * static dhcp-range (suppresses A flag → no SLAAC pollution)
#       * ra-param=vmbr0,30,1800 (unsolicited RA every 30s)
#   - systemctl restart dnsmasq
#   - sync-dnat.py regen-pins (rebuild /etc/dnsmasq.d/vm-pins.conf
#     from /etc/pve/qemu-server/*.conf so all current VMs are pinned)
#
# Idempotent: safe to re-run.
############################################################

remote_task() {
  set +e

  local NODE_NAME
  NODE_NAME="$(hostname)"
  local SYNC_DNAT="/var/lib/svz/snippets/sync-dnat.py"
  local SYNC_DNAT_URL="https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/snippets/sync-dnat.py"

  echo "== Running remote task on ${NODE_NAME} =="

  # 0) Fetch latest sync-dnat.py from GitHub
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

  # 1) Move stale .bak files out of /etc/dnsmasq.d/ — dnsmasq parses every file
  #    in there regardless of extension; backups conflict with the live config.
  mkdir -p /root/dnsmasq-backups
  for f in /etc/dnsmasq.d/vmbr0.conf.bak /etc/dnsmasq.d/vmbr0.conf.bak.*; do
    [ -f "$f" ] && mv "$f" /root/dnsmasq-backups/ 2>/dev/null || true
  done

  # 2) Apply new dnsmasq config (stateful DHCPv6, no SLAAC, RA-on-RS working)
  cat > /etc/dnsmasq.d/vmbr0.conf <<'CONF'
interface=vmbr0
# bind-dynamic (NOT bind-interfaces): bind-interfaces breaks RA/RS multicast
# on Proxmox where fwbr<vmid> bridges come and go. With bind-dynamic dnsmasq
# subscribes to ff02::2 and replies to Router Solicitations within ms.
bind-dynamic
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
# `ra-param=vmbr0,30,1800` => unsolicited RA every ~30s (default would be 200-600s);
# protects against Windows missing the first RA on boot.
enable-ra
ra-param=vmbr0,30,1800
dhcp-range=::ff00,::fffe,constructor:vmbr0,static,64,720h
dhcp-option=option6:dns-server,[2a01:4ff:ff00::add:1],[2a01:4ff:ff00::add:2]
CONF
  touch /etc/dnsmasq.d/vm-pins.conf

  # 3) Restart dnsmasq (NOT reload — SIGHUP doesn't re-parse main config files)
  if ! systemctl restart dnsmasq; then
    echo "RUNREMOTES_FAIL	${NODE_NAME}	-	dnsmasq restart failed (check journalctl -u dnsmasq)"
    return 1
  fi
  if ! systemctl is-active --quiet dnsmasq; then
    echo "RUNREMOTES_FAIL	${NODE_NAME}	-	dnsmasq not active after restart"
    return 1
  fi

  # 4) Regenerate dhcp-host pins from /etc/pve/qemu-server/*.conf
  if ! "$SYNC_DNAT" regen-pins; then
    echo "RUNREMOTES_FAIL	${NODE_NAME}	-	sync-dnat.py regen-pins failed"
    return 1
  fi

  # 5) Quick self-check: confirm dnsmasq is subscribed to ff02::2 (all-routers)
  if ip -6 maddr show dev vmbr0 2>/dev/null | grep -q 'ff02::2'; then
    echo "OK: vmbr0 subscribed to ff02::2 (RA-on-RS will work)"
  else
    echo "RUNREMOTES_FAIL	${NODE_NAME}	-	vmbr0 not subscribed to ff02::2 — check bind-dynamic"
  fi

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
    echo "⚠️  Some nodes failed. Triage:"
    echo "    $FAILURE_LOG"
    echo "    ($(wc -l < "$FAILURE_LOG") line(s))"
else
    echo "✅ Run complete with no failures."
fi

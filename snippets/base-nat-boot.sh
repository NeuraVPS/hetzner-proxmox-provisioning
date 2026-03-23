#!/usr/bin/env bash
# Boot orchestrator for dynamic BASE forwarding sync.
# Base nftables/Jool topology is expected to be provided by persistent setup
# (for example jool-nat46.service + /etc/nftables.conf).
set -euo pipefail

DEFAULT=/etc/default/base-nat
if [[ ! -f "$DEFAULT" ]]; then
  echo "Missing $DEFAULT" >&2
  exit 1
fi

# shellcheck source=/dev/null
set -a
source "$DEFAULT"
set +a

: "${MAIN_IPV4:?MAIN_IPV4 required}"
: "${MAIN_IPV6:?MAIN_IPV6 required}"
: "${FAILOVER_IPV4:?FAILOVER_IPV4 required}"
: "${FAILOVER_IPV6:?FAILOVER_IPV6 required}"
: "${SYNC_PVE_NODES_ON_BOOT:=auto}"
: "${WAIT_FOR_IPS_SEC:=120}"

has_ipv4_addr() {
  local want="$1"
  ip -4 -o addr show 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | grep -Fxq "$want"
}

has_ipv6_addr() {
  local want="$1"
  ip -6 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | grep -Fxq "$want"
}

wait_for_host_ip_addrs() {
  local deadline=$((SECONDS + WAIT_FOR_IPS_SEC))
  while ((SECONDS < deadline)); do
    if has_ipv4_addr "$MAIN_IPV4" \
      && has_ipv4_addr "$FAILOVER_IPV4" \
      && has_ipv6_addr "$MAIN_IPV6" \
      && has_ipv6_addr "$FAILOVER_IPV6"; then
      return 0
    fi
    sleep 2
  done

  echo "Timed out waiting for required host IPs after ${WAIT_FOR_IPS_SEC}s" >&2
  ip -4 -o addr show >&2 || true
  ip -6 -o addr show scope global >&2 || true
  return 1
}

mkdir -p /var/lib/base-nat
wait_for_host_ip_addrs

# Sync dynamic VM forwardings from Firestore -> nftables (ip6 nat prerouting).
python3 /usr/local/sbin/sync-base-nat.py sync

# Optional PVE wildcard proxy backend sync from Firestore proxmox_nodes.
case "${SYNC_PVE_NODES_ON_BOOT,,}" in
1|true|yes|on)
  python3 /usr/local/sbin/sync-base-nat.py sync nodes || true
  ;;
0|false|no|off)
  ;;
*)
  if grep -Rqs '\$pve_node_from_host' /etc/nginx/conf.d /etc/nginx/sites-enabled /etc/nginx/sites-available 2>/dev/null; then
    python3 /usr/local/sbin/sync-base-nat.py sync nodes || true
  fi
  ;;
esac

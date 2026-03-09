#!/usr/bin/env bash
# Push cluster.fw from BASE to all nodes. Run from BASE (or any host with
# /etc/nginx/pve-node-backends.conf and SSH to all nodes).
# Optional env: PVE_NODE_BACKENDS_PATH, BASE_PUBLIC_IPV6

set -euo pipefail
BACKENDS="${PVE_NODE_BACKENDS_PATH:-/etc/nginx/pve-node-backends.conf}"
[[ -f "$BACKENDS" ]] || { echo "ERROR: $BACKENDS not found" >&2; exit 1; }
BASE_PUBLIC_IPV6="${BASE_PUBLIC_IPV6:-$(ip -6 route get 2001:4860:4860::8888 2>/dev/null | grep -oP 'src \K\S+' || true)}"
[[ -n "$BASE_PUBLIC_IPV6" ]] || { echo "ERROR: BASE_PUBLIC_IPV6 not set and could not detect" >&2; exit 1; }

while IFS= read -r line; do
  [[ -z "$line" || "$line" =~ ^# ]] && continue
  ipv6=$(echo "$line" | sed -n 's|.*https://\[\([^]]*\)\]:8006;|\1|p')
  [[ -n "$ipv6" ]] || continue
  node_id="${line%%[[:space:]]*}"
  echo "Pushing cluster.fw to $node_id ($ipv6)"
  ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no -o BatchMode=yes "root@[${ipv6}]" \
    "scp -o StrictHostKeyChecking=no root@[${BASE_PUBLIC_IPV6}]:/etc/pve/firewall/cluster.fw /etc/pve/firewall/cluster.fw && pve-firewall restart" \
    || echo "WARNING: Failed $node_id"
done < "$BACKENDS"
echo "Finished pushing cluster.fw to all nodes"

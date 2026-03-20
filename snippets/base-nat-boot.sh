#!/bin/bash
# Prepare BASE nftables + nginx stream state, then run full sync-base-nat.
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
: "${SNAT_IPV6:=$MAIN_IPV6}"

mkdir -p /var/lib/base-nat /etc/nginx/stream.d

# Ensure nginx knows about stream configs (fresh Debian default does not).
if [[ ! -f /etc/nginx/stream-base-nat.conf ]]; then
  cat >/etc/nginx/stream-base-nat.conf <<'EOF'
stream {
    include /etc/nginx/stream.d/*.conf;
}
EOF
fi
if ! grep -q '^include /etc/nginx/stream-base-nat.conf;$' /etc/nginx/nginx.conf; then
  sed -i '/^http {/i include /etc/nginx/stream-base-nat.conf;' /etc/nginx/nginx.conf
fi

# Persist router settings needed for forwarding + SNAT.
cat >/etc/sysctl.d/99-base-nat-router.conf <<'EOF'
net.ipv4.ip_forward=1
net.ipv6.conf.all.forwarding=1
net.ipv4.conf.all.rp_filter=0
net.ipv4.conf.default.rp_filter=0
EOF
sysctl --system >/dev/null

# Keep nginx active; sync script validates and reloads it.
systemctl start nginx >/dev/null 2>&1 || true

python3 /usr/local/sbin/sync-base-nat.py sync
# PVE wildcard proxy backend map from Firestore proxmox_nodes (optional here).
python3 /usr/local/sbin/sync-base-nat.py sync nodes || true

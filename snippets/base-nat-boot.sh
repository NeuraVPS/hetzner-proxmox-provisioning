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
: "${SYSCTL_RMEM_MAX:=16777216}"
: "${SYSCTL_WMEM_MAX:=16777216}"
: "${NGINX_LIMIT_NOFILE:=262144}"
: "${SYNC_PVE_NODES_ON_BOOT:=auto}"
: "${TCP_CONGESTION_CONTROL:=bbr}"
: "${WAIT_FOR_IPS_SEC:=120}"

ulimit -n "${NGINX_LIMIT_NOFILE}" 2>/dev/null || true

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
  while (( SECONDS < deadline )); do
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

mkdir -p /var/lib/base-nat /etc/nginx/stream.d
mkdir -p /etc/systemd/system/nginx.service.d

wait_for_host_ip_addrs

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

# Ensure nginx service has enough open-file limit for thousands of listeners.
cat >/etc/systemd/system/nginx.service.d/override.conf <<EOF
[Service]
LimitNOFILE=${NGINX_LIMIT_NOFILE}
EOF

# Persist router settings needed for forwarding + SNAT.
cat >/etc/sysctl.d/99-base-nat-router.conf <<'EOF'
net.ipv4.ip_forward=1
net.ipv6.conf.all.forwarding=1
net.ipv4.conf.all.rp_filter=0
net.ipv4.conf.default.rp_filter=0
net.core.default_qdisc=fq
net.core.somaxconn=65535
net.ipv4.tcp_max_syn_backlog=262144
net.core.netdev_max_backlog=250000
EOF
cat >>/etc/sysctl.d/99-base-nat-router.conf <<EOF
net.core.rmem_max=${SYSCTL_RMEM_MAX}
net.core.wmem_max=${SYSCTL_WMEM_MAX}
EOF
sysctl --system >/dev/null

# Prefer BBR when available for better queue/latency behavior.
if ! sysctl net.ipv4.tcp_available_congestion_control 2>/dev/null | grep -qw "$TCP_CONGESTION_CONTROL"; then
  modprobe "tcp_${TCP_CONGESTION_CONTROL}" 2>/dev/null || true
fi
if sysctl net.ipv4.tcp_available_congestion_control 2>/dev/null | grep -qw "$TCP_CONGESTION_CONTROL"; then
  sysctl -w "net.ipv4.tcp_congestion_control=${TCP_CONGESTION_CONTROL}" >/dev/null || true
fi

# Keep nginx active; sync script validates and reloads it.
systemctl daemon-reload >/dev/null 2>&1 || true
systemctl start nginx >/dev/null 2>&1 || true

python3 /usr/local/sbin/sync-base-nat.py sync

# PVE wildcard proxy backend map from Firestore proxmox_nodes (optional here).
# Modes:
#   auto: run only if nginx config references $pve_node_from_host
#   1/true/yes/on: always run
#   0/false/no/off: skip
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

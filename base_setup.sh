# Clean Debian 13 bootstrap for BASE ingress (nginx stream + nftables, no Jool)

# 1) Install Debian 13 (Hetzner installimage), then install packages.
apt update && apt upgrade -y
apt install -y nftables nginx libnginx-mod-stream python3 python3-pip curl ca-certificates
pip3 install --break-system-packages firebase-admin

# 2) Keep host addressing stable (main + failover IPv4/IPv6).
# Edit /etc/network/interfaces to include failover IPs for your host.
#
# Example:
# iface enp1s0 inet static
#   address 46.62.188.207
#   netmask 255.255.255.128
#   gateway 46.62.188.129
#   up ip addr add 77.42.49.79/32 dev enp1s0 preferred_lft 0
#   down ip addr del 77.42.49.79/32 dev enp1s0
#
# iface enp1s0 inet6 static
#   address 2a01:4f9:3090:2488::2
#   netmask 64
#   gateway fe80::1
#   up ip addr add 2a01:4f9:fff1:5f::2/64 dev enp1s0 preferred_lft 0
#   down ip addr del 2a01:4f9:fff1:5f::2/64 dev enp1s0
#   # Optional dedicated SNAT source inside main /64:
#   up ip addr add 2a01:4f9:3090:2488::1/64 dev enp1s0 preferred_lft 0
#   down ip addr del 2a01:4f9:3090:2488::1/64 dev enp1s0

# 3) Router sysctl (persisted).
cat >/etc/sysctl.d/99-base-nat-router.conf <<'EOF'
net.ipv4.ip_forward=1
net.ipv6.conf.all.forwarding=1
net.ipv4.conf.all.rp_filter=0
net.ipv4.conf.default.rp_filter=0
net.core.default_qdisc=fq
net.core.somaxconn=65535
net.ipv4.tcp_max_syn_backlog=262144
net.core.netdev_max_backlog=250000
net.core.rmem_max=16777216
net.core.wmem_max=16777216
EOF
sysctl --system
modprobe tcp_bbr 2>/dev/null || true
if sysctl net.ipv4.tcp_available_congestion_control | grep -qw bbr; then
  sysctl -w net.ipv4.tcp_congestion_control=bbr
fi

# 4) Ensure nginx stream include exists (fresh Debian does not include stream.d by default).
mkdir -p /etc/nginx/stream.d /var/lib/base-nat
cat >/etc/nginx/stream-base-nat.conf <<'EOF'
stream {
    include /etc/nginx/stream.d/*.conf;
}
EOF

if ! grep -q '^include /etc/nginx/stream-base-nat.conf;$' /etc/nginx/nginx.conf; then
  sed -i '/^http {/i include /etc/nginx/stream-base-nat.conf;' /etc/nginx/nginx.conf
fi

# 5) Runtime configuration for sync-base-nat.py.
cat >/etc/default/base-nat <<'EOF'
# Public ingress addresses.
MAIN_IPV4=46.62.188.207
MAIN_IPV6=2a01:4f9:3090:2488::2
FAILOVER_IPV4=77.42.49.79
FAILOVER_IPV6=2a01:4f9:fff1:5f::2

# Source IPv6 for SNAT in postrouting (should be in main routed /64).
SNAT_IPV6=2a01:4f9:3090:2488::1

# Port contract:
# SMB external port = SAMBA_PORT_BASE + proxmoxId -> VM 445 (TCP)
# RDP external port = RDP_PORT_BASE + proxmoxId -> VM 3389 (TCP/UDP)
SAMBA_PORT_BASE=10000
RDP_PORT_BASE=20000
VMID_MAX=9999
INCLUDE_UDP_RDP=1
SSH_PORT=22

# Stream tuning (Samba/RDP). proxy_buffers is http-only, stream uses proxy_buffer_size.
NGINX_PROXY_BUFFER_SIZE=128k
NGINX_TCP_NODELAY=1
NGINX_TCP_REUSEPORT=1
NGINX_MIN_WORKER_CONNECTIONS=16384
NGINX_WORKER_CONN_HEADROOM=1024
NGINX_WORKER_RLIMIT_NOFILE=262144
NGINX_TEST_NOFILE=262144
NGINX_LIMIT_NOFILE=262144
TCP_CONGESTION_CONTROL=bbr
SYSCTL_RMEM_MAX=16777216
SYSCTL_WMEM_MAX=16777216
SYNC_PVE_NODES_ON_BOOT=auto
WAIT_FOR_IPS_SEC=120

# Firebase + local state.
FIREBASE_CREDENTIALS_FILE=/etc/firebase-credentials.json
STATE_FILE=/var/lib/base-nat/state.json
PVE_NODES_STATE_FILE=/var/lib/base-nat/pve_nodes.json
PVE_NGINX_MAP_FILE=/etc/nginx/conf.d/pve-proxy-backends.map.conf

# Generated config outputs.
NFT_CONFIG_FILE=/etc/nftables.base-nat.generated.conf
NGINX_STREAM_FILE=/etc/nginx/stream.d/base-nat.conf
EOF

# 6) Install Firebase credentials before starting service.
# install -m 600 firebase-credentials.json /etc/firebase-credentials.json

# 7) Install runtime scripts from this repository.
curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/snippets/sync-base-nat.py \
  -o /usr/local/sbin/sync-base-nat.py
curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/snippets/base-nat-boot.sh \
  -o /usr/local/sbin/base-nat-boot.sh
curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/snippets/base-nat-boot.service \
  -o /etc/systemd/system/base-nat-boot.service
chmod +x /usr/local/sbin/base-nat-boot.sh /usr/local/sbin/sync-base-nat.py

# 8) Enable boot-time sync/apply.
mkdir -p /etc/systemd/system/nginx.service.d
cat >/etc/systemd/system/nginx.service.d/override.conf <<'EOF'
[Service]
LimitNOFILE=262144
EOF

mkdir -p /etc/systemd/system/base-nat-boot.service.d
cat >/etc/systemd/system/base-nat-boot.service.d/override.conf <<'EOF'
[Service]
LimitNOFILE=262144
EOF

cat >/etc/security/limits.d/99-neuravps-nofile.conf <<'EOF'
root soft nofile 262144
root hard nofile 262144
www-data soft nofile 262144
www-data hard nofile 262144
EOF

systemctl daemon-reload
systemctl enable --now base-nat-boot.service

# 9) Validation.
systemctl status --no-pager base-nat-boot.service
python3 /usr/local/sbin/sync-base-nat.py sync
nft list ruleset
nginx -T | sed -n '/^stream {/,/^}/p'
sysctl net.core.rmem_max
sysctl net.core.wmem_max
ulimit -n

# Manual sync examples:
# /usr/local/sbin/sync-base-nat.py sync
# /usr/local/sbin/sync-base-nat.py sync 400
# /usr/local/sbin/sync-base-nat.py sync 400 2001:db8::1
# /usr/local/sbin/sync-base-nat.py sync 400 del
# /usr/local/sbin/sync-base-nat.py sync nodes
# /usr/local/sbin/sync-base-nat.py sync nodes add 0000009-EX44 2a01:4f9:...
# /usr/local/sbin/sync-base-nat.py sync nodes del 0000009-EX44

# =============================================================================
# BASE: PVE proxy (*.pve.neuravps.com) + set-ticket + Firestore proxmox_nodes
# =============================================================================
# Node firewalls must allow BASE -> Proxmox :8006. Backends are built from
# Firestore proxmox_nodes (document ID = server id, field ip = public IPv6).
# base-nat-boot runs "sync nodes" after Jool. Re-run after node changes (Cloud Function SSH:
# sync nodes | sync nodes add <id> <ip> | sync nodes del <id>) — no periodic timer.

apt update
apt install -y nginx libnginx-mod-http-js certbot

mkdir -p /var/www/letsencrypt /etc/nginx/njs /opt/pve-set-ticket

curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/snippets/pve-proxy-map.conf \
  -o /etc/nginx/conf.d/pve-proxy-map.conf
curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/snippets/pve-proxy-backends.map.conf.example \
  -o /etc/nginx/conf.d/pve-proxy-backends.map.conf

curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/snippets/neuravps-redirects.conf \
  -o /etc/nginx/sites-available/neuravps-redirects.conf
ln -sf /etc/nginx/sites-available/neuravps-redirects.conf /etc/nginx/sites-enabled/neuravps-redirects.conf
rm -f /etc/nginx/sites-enabled/default
#
# Wildcard TLS (DNS-01): scripts/certbot-namecheap-dns-hooks.py — see deprecated doc
# docs/pve-proxy-base-server-setup.md (section 7) for Namecheap + certbot renew.

rsync -avz -e ssh root@[2a01:4f9:3070:3984::2]:/etc/letsencrypt/ /etc/letsencrypt/

# Until certs exist, comment out the ssl_certificate server blocks in neuravps-redirects.conf
# or obtain certs first, then:

curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/scripts/pve-set-ticket.py \
  -o /opt/pve-set-ticket/pve-set-ticket.py
chmod 755 /opt/pve-set-ticket/pve-set-ticket.py
tee /opt/pve-set-ticket/env << 'EOF'
PVE_REDEEM_SECRET=...
REDEEM_FUNCTION_URL=https://.../redeem_pve_ticket_token
EOF
chmod 600 /opt/pve-set-ticket/env
curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/snippets/pve-set-ticket.service \
  -o /etc/systemd/system/pve-set-ticket.service
systemctl daemon-reload && systemctl enable --now pve-set-ticket

# After deploy or when Firestore proxmox_nodes changes (boot already syncs):
#   /usr/local/sbin/sync-base-nat.py sync nodes
#   nginx -t && systemctl reload nginx   # sync nodes reloads nginx itself if ok
#
# Cloud Function on node add/change/delete: SSH to BASE and run e.g.
#   sync-base-nat.py sync nodes add <nodeId> <ip>
#   sync-base-nat.py sync nodes del <nodeId>
#   sync-base-nat.py sync nodes   (full refresh from Firestore)
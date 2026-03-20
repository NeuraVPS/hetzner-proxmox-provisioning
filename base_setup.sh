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
EOF
sysctl --system

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
systemctl daemon-reload
systemctl enable --now base-nat-boot.service

# 9) Validation.
systemctl status --no-pager base-nat-boot.service
python3 /usr/local/sbin/sync-base-nat.py sync
nft list ruleset
nginx -T | sed -n '/^stream {/,/^}/p'

# Manual sync examples:
# /usr/local/sbin/sync-base-nat.py sync
# /usr/local/sbin/sync-base-nat.py sync 400
# /usr/local/sbin/sync-base-nat.py sync 400 2001:db8::1
# /usr/local/sbin/sync-base-nat.py sync 400 del
# /usr/local/sbin/sync-base-nat.py sync nodes
# /usr/local/sbin/sync-base-nat.py sync nodes add 0000009-EX44 2a01:4f9:...
# /usr/local/sbin/sync-base-nat.py sync nodes del 0000009-EX44

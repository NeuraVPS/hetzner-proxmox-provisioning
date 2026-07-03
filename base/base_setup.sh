# BASE bootstrap for dynamic forwarding sync (Jool + static nftables already persistent)
#
# Assumptions:
# - Base topology is already installed and persistent:
#   - jool-nat46.service builds netns/Jool plumbing
#   - /etc/nftables.conf contains static/global rules from docs/netns-jool-nat46-nat66-guide.md
# - This setup only installs runtime sync services that:
#   - reconcile dynamic VM forwardings in `ip6 nat prerouting` from Firestore
#   - keep nginx proxmox_nodes map + firewall sync commands

# 0) Interfaces example

### Hetzner Online GmbH installimage

# source /etc/network/interfaces.d/*

# auto lo
# iface lo inet loopback
# iface lo inet6 loopback

# auto enp6s0
# iface enp6s0 inet static
#   address 37.27.135.250
#   netmask 255.255.255.128
#   gateway 37.27.135.129
#   # route 37.27.135.128/25 via 37.27.135.129
#   up route add -net 37.27.135.128 netmask 255.255.255.128 gw 37.27.135.129 dev enp6s0
#   # failover 0
#   up ip addr add 94.130.3.118/32 dev enp6s0 preferred_lft 0
#   down ip addr del 94.130.3.118/32 dev enp6s0
#   # failover 1
#   up ip addr add 77.42.49.79/32 dev enp6s0 preferred_lft 0
#   down ip addr del 77.42.49.79/32 dev enp6s0

# iface enp6s0 inet6 static
#   address 2a01:4f9:3070:3984::2
#   netmask 64
#   gateway fe80::1
#   # failover 0
#   up ip addr add 2a01:4f8:fff2:95::2/64 dev enp6s0 preferred_lft 0
#   down ip addr del 2a01:4f8:fff2:95::5f::2/64 dev enp6s0
#   # failover 1
#   up ip addr add 2a01:4f9:fff1:5f::2/64 dev enp6s0 preferred_lft 0
#   down ip addr del 2a01:4f9:fff1:5f::2/64 dev enp6s0

# 1) Install runtime dependencies.
apt update && apt upgrade -y
apt install -y nftables nginx libnginx-mod-http-js python3 python3-pip curl ca-certificates certbot ethtool sshpass smbclient netcat-openbsd freerdp3-x11 xvfb imagemagick
pip3 install --break-system-packages firebase-admin

# 1b) sshd rate limits for fleet sweeps. node_health_check and run_remotes/*
# hop through this BASE with bursts of short SSH connections; the default
# MaxStartups 10:30:100 resets part of each burst (kex_exchange_identification).
curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/base/snippets/50-neuravps-maxstartups.conf \
  -o /etc/ssh/sshd_config.d/50-neuravps-maxstartups.conf
sshd -t && systemctl reload ssh

# 2) Runtime configuration for sync-base-nat.py.
cat >/etc/default/base-nat <<'EOF'
# Host addresses (used by boot checks only).
MAIN_IPV4=37.27.135.250
MAIN_IPV6=2a01:4f9:3070:3984::2
FAILOVER_IPV4=77.42.49.79
FAILOVER_IPV6=2a01:4f9:fff1:5f::2

# Port contract:
# SMB external port = SAMBA_PORT_BASE + proxmoxId -> VM 445 (TCP)
# RDP external port = RDP_PORT_BASE + proxmoxId -> VM 3389 (TCP/UDP)
SAMBA_PORT_BASE=10000
RDP_PORT_BASE=20000
VMID_MAX=9999
INCLUDE_UDP_RDP=1

# Dynamic nft destination for managed forwarding entries.
NFT_DNAT_FAMILY=ip6
NFT_DNAT_TABLE=nat
NFT_DNAT_CHAIN=prerouting
NFT_MANAGED_RULE_COMMENT_PREFIX=sync-base-nat

# Boot behavior.
SYNC_PVE_NODES_ON_BOOT=auto
WAIT_FOR_IPS_SEC=120

# Firebase + local state.
FIREBASE_CREDENTIALS_FILE=/etc/firebase-credentials.json
STATE_FILE=/var/lib/base-nat/state.json
PVE_NODES_STATE_FILE=/var/lib/base-nat/pve_nodes.json
PVE_NGINX_MAP_FILE=/etc/nginx/conf.d/pve-proxy-backends.map.conf

# Storage Box source for cluster.fw (used by: sync-base-nat.py sync nodes sync-firewall).
FIREWALL_STORAGE_USER=u560363
FIREWALL_STORAGE_HOST=u560363.your-storagebox.de
FIREWALL_REMOTE_PATH=/home/firewall/cluster.fw
FIREWALL_SCP_PORT=23

# Comma-separated main IPv6 of every BASE in stable order.
# sync-base-nat.py rewrites /etc/hosts on every node sync, giving each
# entry a positional alias (b0, b1, ...) so `ssh b1` works from any BASE.
# Keep the order identical on every BASE.
BASE_HOSTS=2a01:4f9:3090:2488::2,2a01:4f9:3070:3984::2
EOF

# 3) Install Firebase credentials before starting service.
# install -m 600 firebase-credentials.json /etc/firebase-credentials.json

# 4) Install runtime scripts from this repository.
curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/base/snippets/sync-base-nat.py \
  -o /usr/local/sbin/sync-base-nat.py
curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/base/snippets/base-nat-boot.sh \
  -o /usr/local/sbin/base-nat-boot.sh
curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/base/snippets/base-nat-boot.service \
  -o /etc/systemd/system/base-nat-boot.service
chmod +x /usr/local/sbin/base-nat-boot.sh /usr/local/sbin/sync-base-nat.py

# 4a) nftables include file for persisted DNAT map elements.
# /etc/nftables.conf has `include "/etc/nftables.d/base-nat-elements.nft"`
# inside `table ip6 nat` (see docs/netns-jool-nat46-nat66-guide.md §7).
# nftables fails to load if the include target is missing, so create an
# empty file before the first reload. sync-base-nat.py rewrites it
# atomically on every reconcile.
mkdir -p /etc/nftables.d
[ -f /etc/nftables.d/base-nat-elements.nft ] || : > /etc/nftables.d/base-nat-elements.nft

# 4b) veth-host.service: tiny early-boot oneshot that creates the
# `veth-host` netdev pair before nftables.service loads. The flowtable
# in /etc/nftables.conf references this device; loading the ruleset
# before it exists fails the entire load. See guide §7.1 + §8.0.
curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/base/snippets/veth-host-setup.sh \
  -o /usr/local/sbin/veth-host-setup.sh
curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/base/snippets/veth-host.service \
  -o /etc/systemd/system/veth-host.service
chmod +x /usr/local/sbin/veth-host-setup.sh

# 4c) nftables.service drop-in: order after veth-host.service so the
# flowtable can resolve `devices = { enp*, veth-host }` on first try,
# and re-run sync after every (re)start so map elements are always
# populated. See guide §7.1.
mkdir -p /etc/systemd/system/nftables.service.d
curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/base/snippets/nftables-base-nat.conf \
  -o /etc/systemd/system/nftables.service.d/10-base-nat.conf

# 5) Enable boot-time dynamic sync.
mkdir -p /var/lib/base-nat
systemctl daemon-reload
systemctl enable --now veth-host.service
systemctl restart nftables.service
systemctl enable --now base-nat-boot.service

# 6) Validation.
systemctl status --no-pager jool-nat46.service
systemctl status --no-pager base-nat-boot.service
python3 /usr/local/sbin/sync-base-nat.py sync
nft -a list chain ip6 nat prerouting

# Manual sync examples (VM forwardings):
# /usr/local/sbin/sync-base-nat.py sync
# /usr/local/sbin/sync-base-nat.py sync 400
# /usr/local/sbin/sync-base-nat.py sync 400 2001:db8::1
# /usr/local/sbin/sync-base-nat.py sync 400 del

# Manual sync examples (PVE nodes map + firewall):
# /usr/local/sbin/sync-base-nat.py sync nodes
# /usr/local/sbin/sync-base-nat.py sync nodes add 0000009-EX44 2a01:4f9:...
# /usr/local/sbin/sync-base-nat.py sync nodes del 0000009-EX44
# /usr/local/sbin/sync-base-nat.py sync nodes sync-firewall

# =============================================================================
# BASE: PVE proxy (*.pve.neuravps.com) + set-ticket + Firestore proxmox_nodes
# =============================================================================
# Node firewalls must allow BASE -> Proxmox :8006.
# Backends are built from Firestore proxmox_nodes (document ID = server ID, field ip = public IPv6).

mkdir -p /var/www/letsencrypt /etc/nginx/njs /opt/pve-set-ticket

curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/base/snippets/pve-proxy-map.conf \
  -o /etc/nginx/conf.d/pve-proxy-map.conf
curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/base/snippets/pve-proxy-backends.map.conf.example \
  -o /etc/nginx/conf.d/pve-proxy-backends.map.conf
curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/base/snippets/neuravps-redirects.conf \
  -o /etc/nginx/sites-available/neuravps-redirects.conf

ln -sf /etc/nginx/sites-available/neuravps-redirects.conf /etc/nginx/sites-enabled/neuravps-redirects.conf
rm -f /etc/nginx/sites-enabled/default

# Wildcard TLS (DNS-01): scripts/certbot-namecheap-dns-hooks.py
# See docs/pve-proxy-base-server-setup.md section 7.

rsync -avz -e ssh root@[2a01:4f9:3070:3984::2]:/etc/letsencrypt/ /etc/letsencrypt/

curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/base/snippets/pve-set-ticket.py \
  -o /opt/pve-set-ticket/pve-set-ticket.py
chmod 755 /opt/pve-set-ticket/pve-set-ticket.py

tee /opt/pve-set-ticket/env << 'EOF'
PVE_REDEEM_SECRET=...
REDEEM_FUNCTION_URL=https://.../redeem_pve_ticket_token
EOF
chmod 600 /opt/pve-set-ticket/env

curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/base/snippets/pve-set-ticket.service \
  -o /etc/systemd/system/pve-set-ticket.service
systemctl daemon-reload
systemctl enable --now pve-set-ticket

# Re-sync nodes whenever proxmox_nodes changes (or run on-demand):
#   /usr/local/sbin/sync-base-nat.py sync nodes

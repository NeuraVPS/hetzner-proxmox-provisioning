# 1) Install Debian 13 through the Hetzner installimage script
# Leave 23 GB per disk for raw swap later on

apt update && apt upgrade -y
reboot
apt install ufw
ufw allow OpenSSH
ufw enable

# Allow forwarding (required for DNAT + Jool toward VMs)
sudo sed -i 's/^DEFAULT_FORWARD_POLICY=.*/DEFAULT_FORWARD_POLICY="ACCEPT"/' /etc/default/ufw
sudo ufw reload

# configure SSH to allow only Neura SSH keys

tasksel --new-install
tasksel

# List block devices and types
lsblk -o NAME,TYPE,FSTYPE,SIZE,UUID
# nvme0n1: create partition 3 in remaining space, type Linux swap (8200)
sgdisk -n "3:0:0" -t "3:8200" /dev/nvme0n1
# nvme1n1: same
sgdisk -n "3:0:0" -t "3:8200" /dev/nvme1n1
# Reload partition tables
reboot
# Format swap partitions
mkswap -f /dev/nvme0n1p3
mkswap -f /dev/nvme1n1p3
blkid /dev/nvme0n1p3 /dev/nvme1n1p3

# Add both to /etc/fstab
#UUID=<uuid-of-nvme0n1p3>  none  swap  sw  0  0
#UUID=<uuid-of-nvme1n1p3>  none  swap  sw  0  0

swapon -a

# =============================================================================
# Network: failover + forwarding (post-up/post-down on enp1s0)
# =============================================================================
# Edit /etc/network/interfaces — example matching Hetzner installimage + failover:
#
#### Hetzner Online GmbH installimage

# source /etc/network/interfaces.d/*

# auto lo
# iface lo inet loopback
# iface lo inet6 loopback

# auto enp1s0
# iface enp1s0 inet static
#   address 46.62.188.207
#   netmask 255.255.255.128
#   gateway 46.62.188.129
#   # route 46.62.188.128/25 via 46.62.188.129
#   up route add -net 46.62.188.128 netmask 255.255.255.128 gw 46.62.188.129 dev enp1s0
#   # Failover IPv4
#   up ip addr add 77.42.49.79/32 dev enp1s0 preferred_lft 0
#   down ip addr del 77.42.49.79/32 dev enp1s0
#   post-up sysctl -w net.ipv4.ip_forward=1 net.ipv6.conf.all.forwarding=1
#   post-down sysctl -w net.ipv4.ip_forward=0 net.ipv6.conf.all.forwarding=0

# iface enp1s0 inet6 static
#   address 2a01:4f9:3090:2488::2
#   netmask 64
#   gateway fe80::1
#   # Failover IPv6
#   up ip addr add 2a01:4f9:fff1:5f::2/64 dev enp1s0 preferred_lft 0
#   down ip addr del 2a01:4f9:fff1:5f::2/64 dev enp1s0
#
# POOL6 for Jool must be a /96 inside the MAIN inet6 /64 (same machine), e.g.:
#   2a01:4f9:3090:2488:64:ff9b::/96
# On another BASE, change only the first four hextets to match that host’s main /64.

# =============================================================================
# BASE NAT64 + IPv6 DNAT (Jool + sync-base-nat.py)
# =============================================================================
apt update
apt install -y jool-tools jool-dkms linux-headers-amd64 python3-pip sshpass
pip3 install --break-system-packages firebase-admin

# Firebase (same as Proxmox nodes)
# install -m 600 firebase-credentials.json /etc/firebase-credentials.json
# or: cp /path/to/firebase-credentials.json /etc/firebase-credentials.json && chmod 600 ...

# Config — MAIN_* = NAT ingress (SMB/RDP); FAILOVER_* = passthrough-only (DNAT to MAIN).
# POOL6 = main /64 + :64:ff9b::/96 (Jool NAT64).
cat >/etc/default/base-nat <<'EOF'
JOOL_INSTANCE=base
# Main/public IPs: NAT46 (Jool) and NAT66 (ip6tables DNAT) listen here.
MAIN_IPV4=46.62.188.207
MAIN_IPV6=2a01:4f9:3090:2488::2
# Failover VIPs: traffic is DNAT'd to MAIN_IPV4/MAIN_IPV6 (passthrough).
FAILOVER_IPV4=77.42.49.79
FAILOVER_IPV6=2a01:4f9:fff1:5f::2
# NAT64 pool: must lie inside iface inet6 static /64 (Hetzner-routed to this server)
POOL6=2a01:4f9:3090:2488:64:ff9b::/96
# SMB: ports SAMBA_PORT_BASE .. SAMBA_PORT_BASE+VMID_MAX (TCP only; default 10000-19999)
# RDP: ports RDP_PORT_BASE .. RDP_PORT_BASE+VMID_MAX (TCP+UDP; default 20000-29999)
SAMBA_PORT_BASE=10000
RDP_PORT_BASE=20000
VMID_MAX=9999
FIREBASE_CREDENTIALS_FILE=/etc/firebase-credentials.json
STATE_FILE=/var/lib/base-nat/state.json
EOF

# IMPORTANT: /etc/default/base-nat must exist BEFORE systemctl start (see heredoc above).

# Scripts from repo snippets/
curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/snippets/sync-base-nat.py \
    -o /usr/local/sbin/sync-base-nat.py
curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/snippets/base-nat-boot.sh \
    -o /usr/local/sbin/base-nat-boot.sh
curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/snippets/base-nat-boot.service \
    -o /etc/systemd/system/base-nat-boot.service
chmod +x /usr/local/sbin/base-nat-boot.sh /usr/local/sbin/sync-base-nat.py
mkdir -p /var/lib/base-nat
systemctl daemon-reload
systemctl enable base-nat-boot.service
systemctl start base-nat-boot.service
# Logs: journalctl -u base-nat-boot.service ; /var/log/sync-base-nat.log
#
# Jool on Debian: instance must use --netfilter (not --iptables). If you ever
# see "Netfilter is the only available instance framework", redeploy base-nat-boot.sh
# or run: jool instance remove base 2>/dev/null; systemctl start base-nat-boot.service

# -----------------------------------------------------------------------------
# Failover passthrough: traffic to FAILOVER_IPV4/FAILOVER_IPV6 is DNAT'd to
# MAIN_IPV4/MAIN_IPV6. Hetzner panel decides which BASE receives failover
# traffic; both BASEs stay identically configured.
# -----------------------------------------------------------------------------

# Manual sync examples (after boot)
# /usr/local/sbin/sync-base-nat.py sync
# /usr/local/sbin/sync-base-nat.py sync 400
# /usr/local/sbin/sync-base-nat.py sync 400 2001:db8::1
# /usr/local/sbin/sync-base-nat.py sync 400 del
#
# PVE wildcard proxy (Firestore collection proxmox_nodes: doc id = node slug e.g. 0000009-EX44, field ip = IPv6)
# /usr/local/sbin/sync-base-nat.py sync nodes
# /usr/local/sbin/sync-base-nat.py sync nodes 0000009-EX44
# /usr/local/sbin/sync-base-nat.py sync nodes add 0000009-EX44 2a01:4f9:...
# /usr/local/sbin/sync-base-nat.py sync nodes del 0000009-EX44

# =============================================================================
# BASE: PVE proxy (*.pve.neuravps.com) + set-ticket + Firestore proxmox_nodes
# =============================================================================
# Node firewalls must allow BASE -> Proxmox :8006. Backends are built from
# Firestore proxmox_nodes (document ID = server id, field ip = public IPv6).
# base-nat-boot runs "sync nodes" after Jool. Re-run after node changes (Cloud Function SSH:
# sync nodes | sync nodes add <id> <ip> | sync nodes del <id>) — no periodic timer.

ufw allow "WWW Full"

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

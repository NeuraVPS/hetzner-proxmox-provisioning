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
#   up ip addr add 77.42.49.79/32 dev enp1s0
#   down ip addr del 77.42.49.79/32 dev enp1s0
#   post-up sysctl -w net.ipv4.ip_forward=1 net.ipv6.conf.all.forwarding=1
#   post-down sysctl -w net.ipv4.ip_forward=0 net.ipv6.conf.all.forwarding=0

# iface enp1s0 inet6 static
#   address 2a01:4f9:3090:2488::2
#   netmask 64
#   gateway fe80::1
#   # Failover IPv6
#   up ip addr add 2a01:4f9:fff1:5f::2/64 dev enp1s0
#   down ip addr del 2a01:4f9:fff1:5f::2/64 dev enp1s0
#   # Force main IPv6 as source for default-route traffic (egress)
#   post-up ip -6 route replace default via fe80::1 dev enp1s0 src 2a01:4f9:3090:2488::2
#   pre-down ip -6 route del default via fe80::1 dev enp1s0 src 2a01:4f9:3090:2488::2
#
# POOL6 for Jool must be a /96 inside the MAIN inet6 /64 (same machine), e.g.:
#   2a01:4f9:3090:2488:64:ff9b::/96
# On another BASE, change only the first four hextets to match that host’s main /64.

# =============================================================================
# BASE NAT64 + IPv6 DNAT (Jool + sync-base-nat.py)
# =============================================================================
apt update
apt install -y jool-tools jool-dkms linux-headers-amd64 python3-pip
pip3 install --break-system-packages firebase-admin

# Firebase (same as Proxmox nodes)
# install -m 600 firebase-credentials.json /etc/firebase-credentials.json
# or: cp /path/to/firebase-credentials.json /etc/firebase-credentials.json && chmod 600 ...

# Config — adjust FAILOVER + POOL6 to this host (POOL6 = main /64 + :64:ff9b::/96)
cat >/etc/default/base-nat <<'EOF'
JOOL_INSTANCE=base
FAILOVER_IPV4=77.42.49.79
FAILOVER_IPV6=2a01:4f9:fff1:5f::2
# Primary inet6 on enp1s0 (same as iface inet6 static ::2). Enables SNAT for IPv6 DNAT
# so forwarded SYNs leave with a source in your /64 (Hetzner drops other sources).
MAIN_IPV6=2a01:4f9:3090:2488::2
# NAT64 pool: must lie inside iface inet6 static /64 (Hetzner-routed to this server)
POOL6=2a01:4f9:3090:2488:64:ff9b::/96
# SMB: ports SAMBA_PORT_BASE .. SAMBA_PORT_BASE+VMID_MAX (default 10000-19999)
# RDP: ports RDP_PORT_BASE .. RDP_PORT_BASE+VMID_MAX (default 20000-29999)
SAMBA_PORT_BASE=10000
RDP_PORT_BASE=20000
VMID_MAX=9999
FIREBASE_CREDENTIALS_FILE=/etc/firebase-credentials.json
STATE_FILE=/var/lib/base-nat/state.json
EOF

# IMPORTANT: /etc/default/base-nat must exist BEFORE systemctl start (see heredoc above).
# If you only curl scripts, create config first, e.g.:
#   curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/snippets/base-nat.default.example -o /etc/default/base-nat
#   nano /etc/default/base-nat

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

# UFW: SMB 10000-19999 and RDP 20000-29999 on failover (VMID_MAX=9999)
# ufw allow proto tcp from any to 77.42.49.79 port 10000:19999
# ufw allow proto tcp from any to 77.42.49.79 port 20000:29999
# (same for FAILOVER_IPV6 if ufw supports per-rule IPv6)

# -----------------------------------------------------------------------------
# Failover routing: Hetzner Cloud / Robot panel decides which BASE receives
# traffic to the failover IPs. Both BASEs stay identically configured; the
# standby host simply gets no failover traffic until you switch the panel.
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# Optional: test via MAIN IP when failover points at the other BASE
# (ephemeral — repeat after reboot if needed)
#
# Enable — main -> failover (SMB 10000-19999 + RDP 20000-29999)
# iptables -t nat -I PREROUTING 1 -d 46.62.188.207 -p tcp -m multiport --dports 10000:19999,20000:29999 \
#   -j DNAT --to-destination 77.42.49.79
# ip6tables -t nat -I PREROUTING 1 -d 2a01:4f9:3090:2488::2 -p tcp -m multiport --dports 10000:19999,20000:29999 \
#   -j DNAT --to-destination 2a01:4f9:fff1:5f::2
#
# Disable
# iptables -t nat -D PREROUTING -d 46.62.188.207 -p tcp -m multiport --dports 10000:19999,20000:29999 -j DNAT --to-destination 77.42.49.79
# ip6tables -t nat -D PREROUTING -d 2a01:4f9:3090:2488::2 -p tcp -m multiport --dports 10000:19999,20000:29999 -j DNAT --to-destination 2a01:4f9:fff1:5f::2
#
# While testing, allow the same ports toward the main addresses (ufw or INPUT).
# -----------------------------------------------------------------------------

# Manual sync examples (after boot)
# /usr/local/sbin/sync-base-nat.py sync
# /usr/local/sbin/sync-base-nat.py sync 400
# /usr/local/sbin/sync-base-nat.py sync 400 2001:db8::1
# /usr/local/sbin/sync-base-nat.py sync 400 del

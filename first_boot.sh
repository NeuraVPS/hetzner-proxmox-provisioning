#!/usr/bin/env bash
set -euo pipefail

log() { echo "[$(date +'%F %T')] $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

# Restrict SSH
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl restart sshd

# Add Proxmox repositories and keys
rm /etc/apt/sources.list.d/pve-enterprise.sources || true

cat > /etc/apt/sources.list.d/pve-no-subscription.sources << EOF
Types: deb
URIs: http://download.proxmox.com/debian/pve
Suites: trixie
Components: pve-no-subscription
Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg
EOF

cat > /etc/apt/sources.list.d/ceph.sources << EOF
Types: deb
URIs: http://download.proxmox.com/debian/ceph-squid
Suites: trixie
Components: no-subscription
Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg
EOF

# Replace Debian APT sources with Hetzner mirrors
log "Replacing Debian APT sources with Hetzner mirrors"
if [ -f /etc/apt/sources.list.d/debian.sources ]; then
  sed -i 's|http://deb.debian.org/debian/|https://mirror.hetzner.com/debian/packages|g' /etc/apt/sources.list.d/debian.sources
  sed -i 's|http://security.debian.org/debian-security/|https://mirror.hetzner.com/debian/security|g' /etc/apt/sources.list.d/debian.sources
fi

apt-get update && apt-get full-upgrade -y
#apt-get purge -y proxmox-first-boot
apt-get install -y python3-pip
pip3 install firebase_admin --break-system-packages --root-user-action=ignore

# Configure chrony with Hetzner NTP servers
log "Configuring chrony with Hetzner NTP servers"
if [ -f /etc/chrony/chrony.conf ]; then
  # Comment out the Debian pool line
  sed -i 's/^pool 2.debian.pool.ntp.org iburst/#pool 2.debian.pool.ntp.org iburst/' /etc/chrony/chrony.conf
  # Add Hetzner NTP servers at the end
  cat >> /etc/chrony/chrony.conf <<EOF
server  ntp1.hetzner.de  iburst
server  ntp2.hetzner.com iburst
server  ntp3.hetzner.net iburst
EOF
  systemctl restart chronyd || true
fi


############################################
# dnsmasq for IPv4 DHCP only
############################################
log "Installing and configuring dnsmasq"
apt-get install -y dnsmasq

cat > /etc/dnsmasq.d/vmbr0.conf <<EOF
interface=vmbr0
bind-interfaces
dhcp-authoritative
dhcp-rapid-commit

# IPv4 DHCP pool for guests
dhcp-range=10.0.0.100,10.0.255.254,255.255.0.0,720h
dhcp-option=3,10.0.0.1
dhcp-option=6,185.12.64.1,185.12.64.2

# ==== IPv6 DHCPv6 (stateful only; SLAAC disabled) ====
enable-ra
dhcp-range=::100,::1ff,constructor:vmbr0,ra-stateless,720h
ra-param=constructor:vmbr0,0,0
dhcp-option=option6:dns-server,[2a01:4ff:ff00::add:1],[2a01:4ff:ff00::add:2]
EOF

systemctl restart dnsmasq

# Script to preserve VM status on reboot
cat >/etc/systemd/system/pve-guests-hooks.service <<EOF
[Unit]
Description=Custom hooks to suspend/resume VMs around pve-guests lifecycle
# Run AFTER guests have started
After=pve-guests.service
# Tie our lifetime to pve-guests
PartOf=pve-guests.service

[Service]
Type=oneshot
TimeoutStartSec=60min
RemainAfterExit=yes
TimeoutStopSec=60min

# --- resume hook ---
ExecStart=/var/lib/svz/snippets/pve-post-boot-resume.sh

# --- suspend hook ---
ExecStop=/var/lib/svz/snippets/pve-pre-reboot-suspend.sh

[Install]
WantedBy=pve-guests.service
EOF

systemctl daemon-reload
systemctl enable pve-guests-hooks.service

# Allow replacement of disks
apt-get install -y pv

# Add Swap for security
zfs create -V 32G \
  -b 16384 \
  -o compression=off \
  -o primarycache=metadata \
  -o logbias=throughput \
  -o sync=always \
  -o redundant_metadata=most \
  rpool/swapvol

# Esperar a que udev cree el dispositivo
for i in {1..30}; do
    if [ -e /dev/zvol/rpool/swapvol ]; then
        break
    fi
    udevadm settle
    sleep 0.2
done

mkswap /dev/zvol/rpool/swapvol
swapon /dev/zvol/rpool/swapvol
echo "/dev/zvol/rpool/swapvol none swap defaults 0 0" >> /etc/fstab

############## CLUSTER SPECIFIC CONFIGURATION ##############
# Proxmox firewall: datacenter baseline with IPv6 ipset gating
echo "==> Configuring Proxmox firewall (datacenter baseline)"
cat >/etc/pve/firewall/cluster.fw <<'EOF'
[OPTIONS]

policy_out: ACCEPT
policy_in: DROP
enable: 1
policy_forward: DROP

[ALIASES]

NAT-Gateway 10.0.0.1

[IPSET hetzner-internal]

10.64.0.0/12
fd00:4000::/108

[IPSET hosts-ipv6]

2a01:4f9:3071:17a7::/64
2a01:4f9:6a:44eb::/64

[RULES]

GROUP management
IN ACCEPT -source +dc/hetzner-internal -log nolog
IN DHCPfwd(ACCEPT) -i vmbr0 -log nolog
IN DHCPv6(ACCEPT) -i vmbr0 -log nolog

[group management]

IN PMG(ACCEPT) -log nolog
IN SSH(ACCEPT) -log nolog

[group vm-default]

IN SSH(ACCEPT) -dest 0.0.0.0/0 -log nolog
IN RDP(ACCEPT) -dest 0.0.0.0/0 -log nolog
IN SMB(ACCEPT) -dest 0.0.0.0/0 -log nolog
IN SMB(ACCEPT) -source +dc/hosts-ipv6 -dest +dc/hosts-ipv6 -log nolog

[group vm-no-internet]

IN SMB(ACCEPT) -dest 0.0.0.0/0 -log nolog
IN SSH(ACCEPT) -dest 0.0.0.0/0 -log nolog
IN RDP(ACCEPT) -dest 0.0.0.0/0 -log nolog
IN DROP -log nolog
OUT DROP -log nolog

[group vm-no-rdp]

IN RDP(DROP) -log nolog

[group vm-no-samba]

IN SMB(ACCEPT) -source +dc/hosts-ipv6 -dest +dc/hosts-ipv6 -log nolog
IN SMB(DROP) -log nolog

[group vm-no-ssh]

IN SSH(DROP) -log nolog

[group vm-public-ipv6]

IN ACCEPT -source ::/0 -log nolog
EOF

# Enable firewall at both scopes and start it
pve-firewall restart || true

# ---- Cluster-wide snippets storage + dynamic RDP hookscript ----
log "Installing cluster-wide hookscript for dynamic RDP DNAT + INPUT open/close"
mkdir -p /var/lib/svz
if ! pvesm status | awk '{print $1}' | grep -x shared; then
  pvesm add dir shared --path /var/lib/svz --content snippets --shared true || true
fi

# Always overwrite to keep latest version
curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/snippets/sync-dnat.py \
    -o /var/lib/svz/snippets/sync-dnat.py

curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/snippets/pve-pre-reboot-suspend.sh \
    -o /var/lib/svz/snippets/pve-pre-reboot-suspend.sh

curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/snippets/pve-post-boot-resume.sh \
    -o /var/lib/svz/snippets/pve-post-boot-resume.sh

curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/snippets/restore-vm-disk-from-vma.sh \
    -o /var/lib/svz/snippets/restore-vm-disk-from-vma.sh

curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/snippets/reset-vm-conntrack.py \
    -o /var/lib/svz/snippets/reset-vm-conntrack.py

curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/snippets/patch_proxmox_api.py \
    -o /var/lib/svz/snippets/patch_proxmox_api.py

chmod +x /var/lib/svz/snippets/sync-dnat.py
chmod +x /var/lib/svz/snippets/pve-pre-reboot-suspend.sh
chmod +x /var/lib/svz/snippets/pve-post-boot-resume.sh
chmod +x /var/lib/svz/snippets/restore-vm-disk-from-vma.sh
chmod +x /var/lib/svz/snippets/reset-vm-conntrack.py
chmod +x /var/lib/svz/snippets/patch_proxmox_api.py

/var/lib/svz/snippets/patch_proxmox_api.py || true
systemctl restart pveproxy
systemctl restart pvedaemon

# manually add with: qm set 100 --hookscript shared:snippets/sync-dnat.py
#sleep 120 # wait for the system to be ready
reboot
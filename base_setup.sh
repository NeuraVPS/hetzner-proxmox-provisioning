# 1) Install Debian 13 through the Hetzner installimage script
# Leave 23 GB per disk for raw swap later on

apt update && apt upgrade -y
reboot
apt install ufw
ufw allow OpenSSH
ufw enable
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

# Edit /etc/network/interfaces to add:
#  # Failover IPv4
#  up ip addr add 77.42.49.79/32 dev enp1s0
#  down ip addr del 77.42.49.79/32 dev enp1s0
#
#  # Failover IPv6
#  up ip addr add 2a01:4f9:fff1:5f::2/64 dev enp1s0
#  down ip addr del 2a01:4f9:fff1:5f::2/64 dev enp1s0

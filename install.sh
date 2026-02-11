#!/usr/bin/env bash
#set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <server-id> <server_type>" >&2
  echo "Example: $0 2 AX162-R-384" >&2
  echo "" >&2
  echo "This will generate:" >&2
  echo "  - Hostname: AX162-R-384-0000002" >&2
  echo "  - Private IPv4: 10.64.0.2" >&2
  echo "  - Private IPv6: fd00:4000::2" >&2
  exit 1
fi

SERVER_ID="$1"
SERVER_TYPE="$2"

# Validate server ID is a number
if ! [[ $SERVER_ID =~ ^[0-9]+$ ]]; then
  echo "Error: Server ID must be a number" >&2
  exit 1
fi

# Validate range (1 to ~1M for 10.64.0.0/12 network)
# 10.64.0.0/12 allows for 2^20 = 1,048,576 addresses
# Usable range: 1 to 1,048,574 (avoiding .0.0 and last address)
if [[ $SERVER_ID -lt 1 || $SERVER_ID -gt 1048574 ]]; then
  echo "Error: Server ID must be between 1 and 1,048,574" >&2
  exit 1
fi

# Generate values from server ID and server type
NAME="$(printf "%07d" "$SERVER_ID")-${SERVER_TYPE}"

# Calculate IP octets for 10.64.0.0/12 network
# The /12 network spans 10.64.0.0 to 10.79.255.255
second_octet=$((64 + (SERVER_ID / 65536)))
third_octet=$(((SERVER_ID / 256) % 256))
fourth_octet=$((SERVER_ID % 256))
PRIVATE_IPV4="10.${second_octet}.${third_octet}.${fourth_octet}"

# For IPv6, convert server ID to hex
PRIVATE_IPV6=$(printf "fd00:4000::%x" "$SERVER_ID")

echo "Server ID: $SERVER_ID"
echo "Server Type: $SERVER_TYPE"
echo "Hostname: $NAME"
echo "Private IPv4: $PRIVATE_IPV4"
echo "Private IPv6: $PRIVATE_IPV6"

### --- CONFIG -------------------------------------------------------

PVE_VERSION="9.1-1"
PVE_ISO_URL="https://enterprise.proxmox.com/iso/proxmox-ve_${PVE_VERSION}.iso"
ISO_PATH="/root/proxmox-ve_${PVE_VERSION}.iso"
AUTO_ISO_PATH="/root/proxmox-ve_${PVE_VERSION}-auto-from-iso.iso"
ANSWER_FILE="/root/answer.toml"

# Must be set before running, or edit here:
: "${PVE_FQDN:=$NAME.neuravps.com}"
: "${PVE_EMAIL:=soporte@neuravps.com}"
: "${PVE_TIMEZONE:=Europe/Madrid}"

NETWORK_FUNCS="/root/.oldroot/nfs/install/network_config.functions.sh"

### --- UTILS --------------------------------------------------------

log() { echo "[$(date +'%F %T')] $*"; }
#die() { echo "ERROR: $*" >&2; exit 1; }
die() { echo "ERROR: $*" >&2; }

require_cmd() {
  for c in "$@"; do
    command -v "$c" >/dev/null 2>&1 || die "Missing command: $c"
  done
}

detect_disks() {
  # All non-USB disks; adjust if you want something stricter
  lsblk -dpno NAME,TYPE,TRAN | awk '$2=="disk" && $3!="usb"{print $1}'
}

detect_firmware() {
  if [ -d /sys/firmware/efi ]; then
    echo "UEFI"
  else
    echo "BIOS"
  fi
}

### --- STEP 1: prepare environment ----------------------------------

log "Checking required tools"
require_cmd wget lsblk awk ip zpool zfs dmidecode udevadm

log "Installing qemu, OVMF, and proxmox-auto-install-assistant (this may take a bit)..."

# Add Proxmox repositories and keys
echo "deb [arch=amd64] http://download.proxmox.com/debian/pve bookworm pve-no-subscription" > /etc/apt/sources.list.d/pve-install-repo.list
wget https://enterprise.proxmox.com/debian/proxmox-release-bookworm.gpg -O /etc/apt/trusted.gpg.d/proxmox-release-bookworm.gpg 

apt-get update -y
apt-get install -y qemu-system-x86 ovmf proxmox-auto-install-assistant

[ -f "$NETWORK_FUNCS" ] || die "network_config.functions.sh not found at $NETWORK_FUNCS"

### --- STEP 2: select disks & wipe signatures -----------------------

log "Detecting candidate disks"
mapfile -t DISKS < <(detect_disks)
[ "${#DISKS[@]}" -gt 0 ] || die "No disks detected"

log "Using disks: ${DISKS[*]}"

log "Wiping old partition signatures to avoid confusion"
for d in "${DISKS[@]}"; do
  log "  wipefs -a $d"
  wipefs -a "$d"
done

# Reserve 23 GB per disk for raw swap: limit ZFS to (min_disk_gb - 23) so first_boot can create swap partitions
log "Computing ZFS hdsize (reserve 23 GB per disk for raw swap)"
MIN_GB=""
for d in "${DISKS[@]}"; do
  bytes=$(blockdev --getsize64 "$d" 2>/dev/null || true)
  [ -n "$bytes" ] || continue
  gb=$((bytes / 1024 / 1024 / 1024))
  if [ -z "$MIN_GB" ] || [ "$gb" -lt "$MIN_GB" ]; then
    MIN_GB=$gb
  fi
done
if [ -z "$MIN_GB" ] || [ "$MIN_GB" -lt 1 ]; then
  log "WARNING: Could not get disk sizes, not setting zfs.hdsize (installer will use full disks)"
  ZFS_HDSIZE_LINE=""
else
  ZFS_HDSIZE=$((MIN_GB - 23))
  if [ "$ZFS_HDSIZE" -lt 32 ]; then
    log "WARNING: Disks too small (min ${MIN_GB} GB); not setting zfs.hdsize (no swap reservation)"
    ZFS_HDSIZE_LINE=""
  else
    log "Using zfs.hdsize = ${ZFS_HDSIZE} GB (23 GB free per disk for raw swap)"
    ZFS_HDSIZE_LINE="zfs.hdsize = ${ZFS_HDSIZE}"
  fi
fi

### --- STEP 3: download Proxmox ISO --------------------------------

if [ ! -f "$ISO_PATH" ]; then
  log "Downloading Proxmox VE ${PVE_VERSION} ISO from $PVE_ISO_URL"
  wget -O "$ISO_PATH" "$PVE_ISO_URL"
else
  log "ISO already present at $ISO_PATH, skipping download"
fi

### --- STEP 4: build answer.toml for automated install --------------

log "Building Proxmox automated installation answer file at $ANSWER_FILE"

# We let the installer use DHCP inside QEMU user-mode networking.
# Final host networking will be overwritten via network_config.functions.sh.
cat >"$ANSWER_FILE" <<EOF
[global]
keyboard = "en-us"
country = "es"
fqdn = "${PVE_FQDN}"
mailto = "${PVE_EMAIL}"
timezone = "${PVE_TIMEZONE}"
root-password-hashed = "\$6\$snErXYs0ZtgB1odl\$1SfK3X4p59VX9wQ1S8xi.nt1qfjzrdRtfG0nk/trcz1gV.vAqvvfgT6l1U4VbvTKQWOpBBunF3OFfMuP.ulfd1"
root-ssh-keys = [
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOOE6gYT2jDMJhKgQ7O1A8lsrLz5apOMlNDK2iSIMVpI"
]
reboot-mode = "power-off"

[network]
source = "from-dhcp"

[disk-setup]
filesystem = "zfs"
zfs.raid = "raid1"
${ZFS_HDSIZE_LINE}
disk-list = ["vda", "vdb"]

[first-boot]
source = "from-url"
ordering = "fully-up"
url = "https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/first_boot.sh"
EOF

log "Validating answer file"
proxmox-auto-install-assistant validate-answer "$ANSWER_FILE"

log "Preparing auto-install ISO"
# The assistant will output something like proxmox-ve_9.0-1-auto-from-iso.iso
proxmox-auto-install-assistant prepare-iso \
  --fetch-from iso \
  --answer-file "$ANSWER_FILE" \
  --output "$AUTO_ISO_PATH" \
  "$ISO_PATH"

[ -f "$AUTO_ISO_PATH" ] || die "Auto ISO not created at $AUTO_ISO_PATH"

### --- STEP 5: run Proxmox installer inside QEMU --------------------

FIRMWARE=$(detect_firmware)
log "Detected firmware: $FIRMWARE"

BIOS_ARGS=()
if [ "$FIRMWARE" = "UEFI" ]; then
  BIOS_ARGS+=(-bios /usr/share/OVMF/OVMF_CODE.fd)
fi

DRIVE_ARGS=()
for d in "${DISKS[@]}"; do
  DRIVE_ARGS+=(-drive "file=${d},format=raw,if=virtio")
done

log "Starting QEMU installer; this will run unattended and exit when done"
qemu-system-x86_64 \
  -enable-kvm \
  -cpu host \
  -m 16384 \
  -boot d \
  -cdrom "$AUTO_ISO_PATH" \
  "${DRIVE_ARGS[@]}" \
  "${BIOS_ARGS[@]}" \
  -vnc 127.0.0.1:0 \
  -no-reboot

log "QEMU exited; assuming installation finished"

### --- STEP 6: mount ZFS root and generate Hetzner-style networking -

log "Importing ZFS pool"
echo y | zpool list || die "Failed to install zpool"
zpool import -a -f || die "Failed to import any ZFS pools"

ROOT_DS=$(zfs list -H -o name | awk '/\/ROOT\//{print $1; exit}')
[ -n "$ROOT_DS" ] || die "Could not detect root ZFS dataset (*/ROOT/*)"

log "Using root dataset: $ROOT_DS"

log "Temporarily setting mountpoint=/mnt and mounting root dataset"
zfs set mountpoint=/mnt "$ROOT_DS"

# Prepare layout expected by network_config.functions.sh: $FOLD/hdd/...
export FOLD="/mnt"
if [ ! -e "/mnt/hdd" ]; then
  ln -s . /mnt/hdd
fi

log "Generating /etc/network/interfaces using Hetzner network_config.functions.sh"
# This will look at the current Rescue networking and predict the final
# NIC name (via predict-check), then write the interfaces file with both
# IPv4 and IPv6 in Hetzner's routed style.

# Set up required environment variables for network_config.functions.sh
export COMPANY="Hetzner Online GmbH"
export IAM="debian"  # Required for setup_etc_network_interfaces to work
export IMG_VERSION=0  # Not critical for Debian/Ubuntu
export IPV4_ONLY=0
export DNSRESOLVER=("185.12.64.1" "185.12.64.2")
export DNSRESOLVER_V6=("2a01:4ff:ff00::add:1" "2a01:4ff:ff00::add:2")
export DEBUGFILE="/tmp/installimage_debug.log"

# Detect system type for is_virtual_machine() function
export SYSTYPE="$(dmidecode -s system-product-name 2>/dev/null | tail -n1 || echo '')"
export SYSMFC="$(dmidecode -s system-manufacturer 2>/dev/null | tail -n1 || echo '')"

# Simple debug function (network_config.functions.sh requires it)
debug() {
  local line="${@}"
  printf '[%(%H:%M:%S)T] %s\n' -1 "${line}" >> "${DEBUGFILE}" 2>/dev/null || true
}

# Simple debugoutput function (for stderr redirection)
debugoutput() {
  while read -r line; do
    printf '[%(%H:%M:%S)T] :   %s\n' -1 "${line}" >> "${DEBUGFILE}" 2>/dev/null || true
  done
}

# is_virtual_machine function (required by network_config.functions.sh)
is_virtual_machine() {
  case "$SYSTYPE" in
    vServer|Bochs|Xen|KVM|VirtualBox|'VMware,Inc.')
      return 0;;
    *)
      case "$SYSMFC" in
        QEMU)
          return 0;;
        *)
          return 1;;
      esac
      return 1;;
  esac
}

# Stub functions that might be referenced but aren't critical for Debian
# (predict_network_interface_name uses early return for Debian, so these won't be called)
image_i40e_driver_exposes_port_name() { return 1; }
image_ice_driver_exposes_port_name() { return 1; }
installed_os_systemd_version() { echo "0"; }
systemd_nspawn_wo_debug() { return 1; }

# Source the network functions
log "Sourcing network functions from $NETWORK_FUNCS"
source "$NETWORK_FUNCS" || die "Failed to source network functions from $NETWORK_FUNCS"

# Verify required functions are available after sourcing
if ! declare -f physical_network_interfaces >/dev/null 2>&1; then
  die "physical_network_interfaces function not found after sourcing $NETWORK_FUNCS"
fi

# Override predict_network_interface_name to use udevadm directly (like predict-check)
# The original function tries to use systemd_nspawn which doesn't work in our context
# We'll use udevadm test-builtin net_id directly on the host system
predict_network_interface_name() {
  local network_interface="$1"
  
  # Use udevadm directly on the host system (same as predict-check script)
  local network_interface_driver=""
  if [ -d "/sys/class/net/$network_interface/device/driver" ]; then
    network_interface_driver="$(basename "$(readlink -f "/sys/class/net/$network_interface/device/driver")" 2>/dev/null || echo "")"
  fi
  
  # Run udevadm test-builtin net_id (same as predict-check)
  local d="$(echo; udevadm test-builtin net_id "/sys/class/net/$network_interface" 2>/dev/null)"
  
  # Try to extract predicted name from udevadm output (same priority as predict-check)
  local predicted_name=""
  
  [[ "$d" =~ $'\n'ID_NET_NAME_ONBOARD=([^$'\n']+) ]] && predicted_name="${BASH_REMATCH[1]}" && echo "$predicted_name" && return 0
  [[ "$d" =~ $'\n'ID_NET_NAME_SLOT=([^$'\n']+) ]] && predicted_name="${BASH_REMATCH[1]}" && echo "$predicted_name" && return 0
  
  # Convert ID_NET_NAME_PATH to ID_NET_NAME_SLOT for e1000 and 8139cp
  if [[ "$network_interface_driver" =~ ^(e1000|8139cp)$ ]] && [[ "$d" =~ $'\n'ID_NET_NAME_PATH=([a-z]{2})p0([^$'\n']+) ]]; then
    predicted_name="${BASH_REMATCH[1]}${BASH_REMATCH[2]}"
    echo "$predicted_name"
    return 0
  fi
  
  [[ "$d" =~ $'\n'ID_NET_NAME_PATH=([^$'\n']+) ]] && predicted_name="${BASH_REMATCH[1]}" && echo "$predicted_name" && return 0
  [[ "$d" =~ $'\n'ID_NET_NAME_MAC=([^$'\n']+) ]] && predicted_name="${BASH_REMATCH[1]}" && echo "$predicted_name" && return 0
  
  # Fallback: if udevadm fails or returns nothing, use the original interface name
  echo "$network_interface"
  return 0
}

# Verify we can detect network interfaces
log "Detecting network interfaces..."
if ! physical_network_interfaces | head -1 > /dev/null; then
  die "Failed to detect any physical network interfaces"
fi

log "Found network interfaces: $(physical_network_interfaces | tr '\n' ' ')"

# Test the function to ensure it works
FIRST_IF=$(physical_network_interfaces | head -1)
if [ -z "$FIRST_IF" ]; then
  die "No network interface found"
fi
PREDICTED=$(predict_network_interface_name "$FIRST_IF")
log "Interface '$FIRST_IF' will be configured as: '$PREDICTED'"
if [ -z "$PREDICTED" ]; then
  die "predict_network_interface_name returned empty string for '$FIRST_IF'"
fi

# Generate the network configuration
setup_etc_network_interfaces

# Verify the file was created and has content
if [ ! -s "/mnt/etc/network/interfaces" ]; then
  die "Generated /etc/network/interfaces is empty!"
fi

log "Configuring VLAN 4000 + vmbr0 (IPv4 NAT + IPv6)"

# Grab first global IPv6 on that interface (e.g. 2a01:4f9:3071:1529::2/64)
WAN_V6_INFO=$(ip -6 addr show dev "$FIRST_IF" scope global | awk '/inet6/{print $2; exit}')
WAN_V6_ADDR=$(echo "$WAN_V6_INFO" | cut -d'/' -f1)       # 2a01:4f9:3071:1529::2
WAN_V6_PREFIXLEN=$(echo "$WAN_V6_INFO" | cut -d'/' -f2)  # 64
# Extract the routed /64 prefix (drop the host bits after the last "::")
WAN_V6_PREFIX=$(echo "$WAN_V6_ADDR" | sed -E 's/::[0-9a-fA-F]*$//')  # -> 2a01:4f9:3071:1529

# We are doing "Option B": guests live in the SAME /64 as the host uplink.
VM_V6_SUBNET="${WAN_V6_PREFIX}"           # e.g. 2a01:4f9:3071:1529
VM_V6_PREFIXLEN="${WAN_V6_PREFIXLEN}"     # almost certainly 64 from Hetzner
VM_V6_GATEWAY="${VM_V6_SUBNET}::1"        # we give vmbr0 ::1 in that /64

log "Detected uplink IPv6:       ${WAN_V6_ADDR}/${WAN_V6_PREFIXLEN}"
log "Using guest prefix:         ${VM_V6_SUBNET}::/${VM_V6_PREFIXLEN}"
log "vmbr0 gateway IPv6 will be: ${VM_V6_GATEWAY}/${VM_V6_PREFIXLEN}"

if ! grep -q "auto eth0.4000" /mnt/etc/network/interfaces; then
cat >> /mnt/etc/network/interfaces <<EOF

auto eth0.4000
iface eth0.4000 inet static
    address ${PRIVATE_IPV4}
    netmask 255.240.0.0
    vlan-raw-device ${PREDICTED}
    mtu 1400

iface eth0.4000 inet6 static
    address ${PRIVATE_IPV6}
    netmask 108
EOF
fi

if ! grep -q "auto vmbr0" /mnt/etc/network/interfaces; then
sed -i -e "/^iface ${PREDICTED} inet6 static$/,/^$/{
    s/^\([[:space:]]*netmask[[:space:]]\)64$/\1128/
}" /mnt/etc/network/interfaces
cat >> /mnt/etc/network/interfaces <<EOF

auto vmbr0
iface vmbr0 inet static
    address 10.0.0.1/16
    bridge-ports none
    bridge-stp off
    bridge-fd 0
    post-up   echo 1 > /proc/sys/net/ipv4/ip_forward
    post-up   iptables -t nat -A POSTROUTING -s '10.0.0.0/16' -o ${PREDICTED} -j MASQUERADE
    post-up   iptables -t raw -I PREROUTING -i fwbr+ -j CT --zone 1
    post-down iptables -t nat -D POSTROUTING -s '10.0.0.0/16' -o ${PREDICTED} -j MASQUERADE
    post-down iptables -t raw -D PREROUTING -i fwbr+ -j CT --zone 1

iface vmbr0 inet6 static
    address ${VM_V6_GATEWAY}/${VM_V6_PREFIXLEN}
    post-up   echo 1 > /proc/sys/net/ipv6/conf/all/forwarding
EOF
fi

log "Successfully generated /etc/network/interfaces ($(wc -l < /mnt/etc/network/interfaces) lines)"

# Configure DNS resolvers in /etc/resolv.conf
# This is critical for hostname resolution (e.g., being able to ping google.com)
log "Configuring DNS resolvers in /etc/resolv.conf"
generate_resolvconf() {
  # For Debian/Ubuntu, check if resolv.conf is a symlink (systemd-resolved)
  local resolv_conf="/mnt/etc/resolv.conf"
  local resolv_file="$resolv_conf"
  
  # Check if systemd-resolved is managing DNS (symlink to stub-resolv.conf)
  if [[ -L "$resolv_conf" ]]; then
    local link_target="$(readlink "$resolv_conf")"
    log "Detected /etc/resolv.conf is a symlink to: $link_target"
    
    # If it points to systemd-resolved stub, we need to configure systemd-resolved
    if [[ "$link_target" == *"systemd/resolve/stub-resolv.conf"* ]]; then
      log "systemd-resolved detected, configuring /etc/systemd/resolved.conf"
      local resolved_conf="/mnt/etc/systemd/resolved.conf"
      
      # Backup original if exists
      if [ -f "$resolved_conf" ]; then
        cp "$resolved_conf" "${resolved_conf}.bak"
      fi
      
      # Configure systemd-resolved with Hetzner DNS servers
      {
        echo "[Resolve]"
        echo "DNS=$(IFS=' '; echo "${DNSRESOLVER[*]}")"
        echo "FallbackDNS=$(IFS=' '; echo "${DNSRESOLVER_V6[*]}")"
        echo "Domains=~."
      } > "$resolved_conf"
      
      log "systemd-resolved configured with DNS servers"
    else
      # If it's a symlink to something else, write to resolvconf base file
      resolv_file="/mnt/etc/resolvconf/resolv.conf.d/base"
      mkdir -p "$(dirname "$resolv_file")"
    fi
  fi
  
  # Also write to the actual file if it's not a systemd-resolved symlink
  if [[ "$resolv_file" == "$resolv_conf" ]] || [[ "$resolv_file" != *"systemd"* ]]; then
    log "Writing DNS resolvers to $resolv_file"
    {
      echo "### ${COMPANY} installimage"
      echo "# nameserver config"
      # Use randomized_nsaddrs to get nameservers in random order
      while read nsaddr; do
        echo "nameserver ${nsaddr}"
      done < <(randomized_nsaddrs)
    } > "$resolv_file"
    
    log "DNS configuration:"
    cat "$resolv_file" | while read line; do
      log "  $line"
    done
  fi
}

generate_resolvconf

# Verify DNS configuration
if [ ! -s "/mnt/etc/resolv.conf" ] && [ ! -L "/mnt/etc/resolv.conf" ]; then
  log "WARNING: /etc/resolv.conf not found or empty, but may be handled by systemd-resolved"
fi

# Generate /etc/hosts in the same format as installimage
log "Generating /etc/hosts (installimage-style)"

# Get main IP addresses - try using network_config.functions.sh functions first,
# fall back to manual extraction from the main interface if needed
MAIN_IPV4=""
MAIN_IPV6=""

# Try to use v4_main_ip() and v6_main_ip() if available (from network_config.functions.sh)
if declare -f v4_main_ip >/dev/null 2>&1; then
  MAIN_IPV4="$(v4_main_ip 2>/dev/null || true)"
fi
if declare -f v6_main_ip >/dev/null 2>&1; then
  MAIN_IPV6="$(v6_main_ip 2>/dev/null || true)"
fi

# Fallback: extract IPs directly from the main network interface
if [[ -z "$MAIN_IPV4" ]] && [[ -n "$FIRST_IF" ]]; then
  MAIN_IPV4_INFO=$(ip -4 addr show dev "$FIRST_IF" scope global | awk '/inet/{print $2; exit}')
  if [[ -n "$MAIN_IPV4_INFO" ]]; then
    MAIN_IPV4="$MAIN_IPV4_INFO"
    log "Extracted IPv4 from interface $FIRST_IF: $MAIN_IPV4"
  fi
fi

if [[ -z "$MAIN_IPV6" ]] && [[ -n "$FIRST_IF" ]]; then
  MAIN_IPV6_INFO=$(ip -6 addr show dev "$FIRST_IF" scope global | awk '/inet6/{print $2; exit}')
  if [[ -n "$MAIN_IPV6_INFO" ]]; then
    MAIN_IPV6="$MAIN_IPV6_INFO"
    log "Extracted IPv6 from interface $FIRST_IF: $MAIN_IPV6"
  fi
fi

# Extract shortname from FQDN
SHORTNAME="${PVE_FQDN%%.*}"
FQDN_NAME="${PVE_FQDN}"

# If FQDN equals shortname, set FQDN_NAME to empty (like installimage does)
if [[ "${FQDN_NAME}" == "${SHORTNAME}" ]]; then
  FQDN_NAME=""
fi

# Generate /etc/hosts file (same format as installimage's set_hostname function)
HOSTSFILE="/mnt/etc/hosts"
{
  echo "### ${COMPANY} installimage"
  echo "127.0.0.1 localhost.localdomain localhost"
  if [[ -n "$MAIN_IPV4" ]]; then
    # Strip CIDR suffix if present
    IPV4_WITHOUT_SUFFIX="${MAIN_IPV4%%/*}"
    if [[ -n "$FQDN_NAME" ]]; then
      echo "$IPV4_WITHOUT_SUFFIX $FQDN_NAME $SHORTNAME"
    else
      echo "$IPV4_WITHOUT_SUFFIX $SHORTNAME"
    fi
  fi
  echo "::1     ip6-localhost ip6-loopback"
  echo "fe00::0 ip6-localnet"
  echo "ff00::0 ip6-mcastprefix"
  echo "ff02::1 ip6-allnodes"
  echo "ff02::2 ip6-allrouters"
  echo "ff02::3 ip6-allhosts"
  if [[ -n "$MAIN_IPV6" ]]; then
    # Strip CIDR suffix if present
    IPV6_WITHOUT_SUFFIX="${MAIN_IPV6%%/*}"
    if [[ -n "$FQDN_NAME" ]]; then
      echo "$IPV6_WITHOUT_SUFFIX $FQDN_NAME $SHORTNAME"
    else
      echo "$IPV6_WITHOUT_SUFFIX $SHORTNAME"
    fi
  fi
} > "$HOSTSFILE"

log "Generated /etc/hosts with IPv4: ${MAIN_IPV4:-none}, IPv6: ${MAIN_IPV6:-none}, FQDN: ${FQDN_NAME:-$SHORTNAME}"

### --- STEP 6.5: disable Intel integrated GPU -------------------------

log "Configuring GRUB to disable Intel integrated GPU (i915)"

# Configure GRUB kernel parameters to disable Intel GPU
GRUB_DEFAULT="/mnt/etc/default/grub"
if [ -f "$GRUB_DEFAULT" ]; then
  # Read current GRUB_CMDLINE_LINUX if it exists
  if grep -q "^GRUB_CMDLINE_LINUX=" "$GRUB_DEFAULT"; then
    # Extract existing parameters and add i915 disabling parameters
    CURRENT_CMDLINE=$(grep "^GRUB_CMDLINE_LINUX=" "$GRUB_DEFAULT" | sed 's/^GRUB_CMDLINE_LINUX="\(.*\)"/\1/')
    # Remove deprecated i915.modeset=0 if present (replaced by nomodeset)
    CURRENT_CMDLINE=$(echo "$CURRENT_CMDLINE" | sed 's/i915\.modeset=0//g')
    # Add nomodeset if not already present (disables all GPU mode setting)
    if [[ "$CURRENT_CMDLINE" != *"nomodeset"* ]]; then
      CURRENT_CMDLINE="${CURRENT_CMDLINE} nomodeset"
    fi
    # Add i915.enable_guc=0 if not already present (disables GuC firmware)
    if [[ "$CURRENT_CMDLINE" != *"i915.enable_guc=0"* ]]; then
      CURRENT_CMDLINE="${CURRENT_CMDLINE} i915.enable_guc=0"
    fi
    # Update GRUB_CMDLINE_LINUX
    sed -i "s|^GRUB_CMDLINE_LINUX=.*|GRUB_CMDLINE_LINUX=\"${CURRENT_CMDLINE}\"|" "$GRUB_DEFAULT"
  else
    # If GRUB_CMDLINE_LINUX doesn't exist, add it
    echo "GRUB_CMDLINE_LINUX=\"nomodeset i915.enable_guc=0\"" >> "$GRUB_DEFAULT"
  fi
  log "Updated GRUB_CMDLINE_LINUX in $GRUB_DEFAULT"
else
  log "WARNING: $GRUB_DEFAULT not found, creating it"
  mkdir -p "$(dirname "$GRUB_DEFAULT")"
  cat > "$GRUB_DEFAULT" <<EOF
GRUB_DEFAULT=0
GRUB_TIMEOUT=5
GRUB_CMDLINE_LINUX="nomodeset i915.enable_guc=0"
EOF
fi

# Blacklist i915 module to prevent it from loading
log "Blacklisting i915 kernel module"
MODPROBE_BLACKLIST="/mnt/etc/modprobe.d/blacklist-i915.conf"
mkdir -p "$(dirname "$MODPROBE_BLACKLIST")"
cat > "$MODPROBE_BLACKLIST" <<EOF
# Disable Intel integrated GPU to prevent system crashes
blacklist i915
options i915 enable_guc=0
EOF
log "Created blacklist file: $MODPROBE_BLACKLIST"

# Also update initramfs blacklist if the directory exists
if [ -d "/mnt/etc/initramfs-tools" ]; then
  INITRAMFS_BLACKLIST="/mnt/etc/initramfs-tools/modules.blacklist"
  if [ -f "$INITRAMFS_BLACKLIST" ]; then
    if ! grep -q "^i915$" "$INITRAMFS_BLACKLIST"; then
      echo "i915" >> "$INITRAMFS_BLACKLIST"
      log "Added i915 to initramfs blacklist"
    fi
  else
    echo "i915" > "$INITRAMFS_BLACKLIST"
    log "Created initramfs blacklist with i915"
  fi
fi

log "Intel GPU (i915) disabled via GRUB kernel parameters and module blacklist"

log "Cleaning up /mnt/hdd symlink"
rm -f /mnt/hdd

log "Unmounting root dataset and restoring mountpoint=/"
zfs umount "$ROOT_DS"
zfs set mountpoint=/ "$ROOT_DS"

log "Exporting ZFS pool"
zpool export "$(echo "$ROOT_DS" | cut -d'/' -f1)"

### --- STEP 7: reboot into Proxmox ---------------------------------

log "All done. Rebooting into installed Proxmox VE..."
reboot
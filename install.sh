#!/usr/bin/env bash
#set -euo pipefail

if [[ $# -ge 1 ]]; then
  NAME="$1"
elif [[ -n "${NEURAVPS_SERVER_NAME:-}" ]]; then
  NAME="$NEURAVPS_SERVER_NAME"
else
  echo "Usage: $0 <server-name>" >&2
  echo "Example:  $0 0000002-AX162-R" >&2
  echo "Or:       NEURAVPS_SERVER_NAME=0000002-AX162-R $0   (used by SSH jump / automation)" >&2
  echo "" >&2
  echo "From URL (pass hostname or curl error 23 when piping):" >&2
  echo "  curl -fsSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/install.sh -o /tmp/install.sh \\" >&2
  echo "    && bash /tmp/install.sh 0000002-AX162-R" >&2
  echo "" >&2
  echo "This will generate:" >&2
  echo "  - Hostname: <server-name>" >&2
  exit 1
fi

echo "Hostname: $NAME"

### --- CONFIG -------------------------------------------------------

PVE_VERSION="9.2-1"
PVE_ISO_URL="https://enterprise.proxmox.com/iso/proxmox-ve_${PVE_VERSION}.iso"
ISO_PATH="/root/proxmox-ve_${PVE_VERSION}.iso"
AUTO_ISO_PATH="/root/proxmox-ve_${PVE_VERSION}-auto-from-iso.iso"
ANSWER_FILE="/root/answer.toml"
FIRST_BOOT_SCRIPT_PATH="/root/neuravps-first-boot.sh"

# Must be set before running, or edit here:
: "${PVE_FQDN:=$NAME.neuravps.com}"
: "${PVE_EMAIL:=soporte@neuravps.com}"
: "${PVE_TIMEZONE:=Europe/Madrid}"

# Per-disk raw swap reserved OUTSIDE the ZFS pool (GB). install reserves this much
# free space per ZFS disk via zfs.hdsize; first_boot.sh turns it into a swap
# partition (one per pool disk, swapon'd at equal priority = striped).
# AUTO-DETECTED from the hardware below (after the log helpers) when not passed
# explicitly — an explicit SWAP_GB_PER_DISK env always wins.

NETWORK_FUNCS="/root/.oldroot/nfs/install/network_config.functions.sh"

### --- UTILS --------------------------------------------------------

log() { echo "[$(date +'%F %T')] $*"; }
# die() must actually ABORT. It was a print-and-continue no-op until
# 2026-07-07, when 0000148 sailed past "Auto ISO not created" into the
# QEMU/zpool/network steps over already-wiped disks. Every call site is a
# cannot-proceed condition; log any FIX hint BEFORE calling die.
die() { echo "ERROR: $*" >&2; exit 1; }

# ---- SWAP_GB_PER_DISK auto-detection (fleet standard 2026-07-02) --------------
# The fleet converges on exactly four hardware configs; swap sizing follows the
# class's RAM-overcommit vs disk-thin-provisioning balance (measured fleet data):
#   EPYC 9454(P) + >=320 GB RAM (AX162/384, SQX <=135t) -> 75   150 GB swap; DISK is the scarce
#                                                              resource (~3.8 TB thin-sold on a 1.7 TB pool)
#   EPYC 9454(P) + <320 GB RAM  (AX162/256, SQX <=110t) -> 100  200 GB swap; most RAM-overcommitted SQX tier (~1.6x)
#   Ryzen 9 7950X3D            (AX102/128, MT 48 VMs)  -> 100  200 GB swap; 1.5x chronic RAM overcommit
#                                                              + reboot storms while KSM re-warms
#   i5-13500                   (EX44/64, legacy VPS E) -> 23   46 GB; the pool is the tightest (512 GB sold
#                                                              on ~450 GB usable); no new signups on this class
#   Ryzen 7 PRO 8700GE         (AX42/64, 2x512 GB)     -> 23   46 GB; same shape as the EX44 above (64 GB RAM,
#                                                              ~448 GB usable pool) -> same reserve
#   Xeon 6787P                 (R470 — being retired)  -> 256  unchanged until the class is gone
# Detection keys on SILICON + RAM, never the hostname — hostnames have lied
# before (0000186/192/193 "AX162-R" are Naples/48-thread boxes). Unknown
# hardware falls back to a conservative 23 with a loud warning: pass
# SWAP_GB_PER_DISK explicitly when installing a brand-new hardware class.
if [ -z "${SWAP_GB_PER_DISK:-}" ]; then
  _SWAP_CPU_MODEL="$(awk -F': ' '/^model name/{print $2; exit}' /proc/cpuinfo 2>/dev/null)"
  _SWAP_RAM_GB="$(awk '/^MemTotal/{print int($2/1024/1024)}' /proc/meminfo 2>/dev/null)"
  case "$_SWAP_CPU_MODEL" in
    # Match WITHOUT the P suffix: the AX162-2-LTD line ships the non-P
    # "AMD EPYC 9454" (same Genoa 25/17 silicon) — the literal "9454P" match
    # sent 0000203/204/205 to the unknown-hardware 23 GB fallback (2026-07-02).
    *"EPYC 9454"*)
      if [ "${_SWAP_RAM_GB:-0}" -ge 320 ]; then SWAP_GB_PER_DISK=75; else SWAP_GB_PER_DISK=100; fi ;;
    *"Ryzen 9 7950X3D"*) SWAP_GB_PER_DISK=100 ;;
    *"i5-13500"*)        SWAP_GB_PER_DISK=23 ;;
    # AX42-1: 64 GB RAM on a ~448 GB pool — the EX44's profile, so the EX44's
    # reserve. Measured 2026-07-27 on 0000184 (the evaluation box).
    *"Ryzen 7 PRO 8700GE"*) SWAP_GB_PER_DISK=23 ;;
    *"Xeon"*"6787P"*)    SWAP_GB_PER_DISK=256 ;;
    *)
      SWAP_GB_PER_DISK=23
      log "WARNING: unknown hardware class for swap sizing (cpu='${_SWAP_CPU_MODEL:-?}', ram=${_SWAP_RAM_GB:-?}GB) — using conservative ${SWAP_GB_PER_DISK} GB/disk. Pass SWAP_GB_PER_DISK explicitly for new hardware classes." ;;
  esac
  log "SWAP_GB_PER_DISK auto-detected: ${SWAP_GB_PER_DISK} GB/disk (cpu='${_SWAP_CPU_MODEL:-?}', ram=${_SWAP_RAM_GB:-?}GB)"
else
  log "SWAP_GB_PER_DISK explicitly set: ${SWAP_GB_PER_DISK} GB/disk (auto-detection skipped)"
fi

require_cmd() {
  for c in "$@"; do
    command -v "$c" >/dev/null 2>&1 || die "Missing command: $c"
  done
}

detect_disks() {
  # All non-USB disks; adjust if you want something stricter
  lsblk -dpno NAME,TYPE,TRAN | awk '$2=="disk" && $3!="usb"{print $1}'
}

disk_size_bytes() {
  local d="$1"
  blockdev --getsize64 "$d" 2>/dev/null || echo ""
}

select_zfs_mirror_disks() {
  local -a candidates=("$@")
  local -a sized=()
  local d bytes

  [ "${#candidates[@]}" -ge 2 ] || return 1

  for d in "${candidates[@]}"; do
    bytes="$(disk_size_bytes "$d")"
    [ -n "$bytes" ] || continue
    sized+=("${bytes}:${d}")
  done

  [ "${#sized[@]}" -ge 2 ] || return 1

  # Choose the largest size bucket that has at least 2 disks.
  local -A size_count=()
  local entry size
  for entry in "${sized[@]}"; do
    size="${entry%%:*}"
    size_count["$size"]=$(( ${size_count["$size"]:-0} + 1 ))
  done

  local best_size=""
  local count
  for size in "${!size_count[@]}"; do
    count="${size_count[$size]}"
    if [ "$count" -ge 2 ] && { [ -z "$best_size" ] || [ "$size" -gt "$best_size" ]; }; then
      best_size="$size"
    fi
  done

  [ -n "$best_size" ] || return 1

  # Use 4 same-size disks when available (ZFS raid10), otherwise 2 (raid1). A
  # 3-disk node still resolves to 2 here; its 3rd disk becomes a dedicated swap
  # disk in first_boot.
  local want=2
  [ "${size_count[$best_size]}" -ge 4 ] && want=4

  local picked=0
  for d in "${candidates[@]}"; do
    bytes="$(disk_size_bytes "$d")"
    if [ "$bytes" = "$best_size" ]; then
      echo "$d"
      picked=$((picked + 1))
      [ "$picked" -eq "$want" ] && break
    fi
  done

  [ "$picked" -eq "$want" ] || return 1
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

# --- Require UEFI boot (NeuraVPS fleet standard) -------------------
# The whole fleet boots UEFI. The QEMU/SeaBIOS (Legacy BIOS) path leaves
# the node with an empty/non-functional bootloader (empty ESPs, no
# zfs.mod): it installs fine but never boots and stays unreachable. Abort
# before touching any disk instead of silently shipping a dead node
# (root cause of 0000192, 2026-06-23).
FIRMWARE="$(detect_firmware)"
log "Detected firmware (rescue): $FIRMWARE"
if [ "$FIRMWARE" != "UEFI" ]; then
  log "FIX: Hetzner KVM/LARA console -> BIOS setup -> enable UEFI, disable CSM/Legacy boot; re-activate the rescue (must boot UEFI: '[ -d /sys/firmware/efi ]'), then retry."
  die "Rescue booted in Legacy BIOS, but NeuraVPS installs in UEFI only — installing now would produce a node that never boots."
fi

# --- Require the fleet SSH key in rescue ---------------------------
# install.sh copies /root/.ssh/neuravps_id into the new system and
# first_boot.sh needs it to pull firebase-credentials.json + cluster.fw
# from the Storage Box. Without it the node comes up half-configured (no
# Firestore creds for sync-dnat.py, no smartd wear filter, ESP-first
# BootOrder) — root cause of the 14 half-configured nodes on 2026-07-04.
# The admin "Instalar Proxmox" flow seeds it automatically; manual rescue
# runs must scp it in first.
if [ ! -s /root/.ssh/neuravps_id ]; then
  log "FIX: seed it from a BASE first: scp /root/.ssh/neuravps_id root@<rescue-ip>:/root/.ssh/ — then retry."
  die "Missing /root/.ssh/neuravps_id in rescue — the installed node could not pull its credentials/firewall from the Storage Box."
fi

# Fetch with the rescue host's native networking, before wiping disks. The
# installer runs behind QEMU user networking, whose IPv4 NAT cannot reach the
# Internet on IPv6-only servers. Embedding the hook keeps installation offline.
log "Fetching and validating the first-boot script for inclusion in the ISO"
curl -fsSL --retry 3 \
  https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/first_boot.sh \
  -o "$FIRST_BOOT_SCRIPT_PATH" \
  || die "Cannot fetch first-boot script — refusing to wipe disks"
bash -n "$FIRST_BOOT_SCRIPT_PATH" \
  || die "Invalid first-boot script — refusing to wipe disks"
chmod 755 "$FIRST_BOOT_SCRIPT_PATH"

log "Installing qemu, OVMF, and proxmox-auto-install-assistant (this may take a bit)..."

# Add Proxmox repositories and keys
echo "deb [arch=amd64] http://download.proxmox.com/debian/pve bookworm pve-no-subscription" > /etc/apt/sources.list.d/pve-install-repo.list
wget https://enterprise.proxmox.com/debian/proxmox-release-bookworm.gpg -O /etc/apt/trusted.gpg.d/proxmox-release-bookworm.gpg 

apt-get update -y
# mdadm: to tear down Hetzner's default MD RAID before wiping (see STEP 2).
# gdisk: sgdisk --zap-all to clear residual GPT. Both are usually present in
# the Hetzner rescue, but install explicitly so the disk-prep never no-ops.
apt-get install -y qemu-system-x86 ovmf proxmox-auto-install-assistant mdadm gdisk \
  || die "apt-get install failed (qemu/OVMF/proxmox-auto-install-assistant/mdadm/gdisk) — cannot build or run the auto-installer. Check rescue apt sources/network and retry."

[ -f "$NETWORK_FUNCS" ] || die "network_config.functions.sh not found at $NETWORK_FUNCS"

### --- STEP 2: select disks & wipe signatures -----------------------

log "Detecting candidate disks"
mapfile -t DISKS < <(detect_disks)
[ "${#DISKS[@]}" -gt 0 ] || die "No disks detected"

log "Using disks: ${DISKS[*]}"

# Tear down any pre-existing Linux MD RAID before wiping. Hetzner's default
# installimage ships mdadm RAID1 across the disks (md0=ESP, md1=swap,
# md2=/boot, md3=root). Those arrays auto-assemble in the rescue and HOLD the
# member disks, so wipefs/sgdisk and the Proxmox auto-installer cannot
# repartition them — the ZFS rpool is never created and the install fails
# later with "no pools available to import" / empty root dataset (root cause of
# the 0000148-EX44 reinstall failure, 2026-07-05). Stop every array and zero
# the member superblocks so they cannot re-assemble mid-install.
if command -v mdadm >/dev/null 2>&1; then
  for md in /dev/md?*; do
    [ -b "$md" ] || continue
    log "  mdadm --stop $md"
    mdadm --stop "$md" 2>/dev/null || true
  done
  for d in "${DISKS[@]}"; do
    for part in "$d"p[0-9]* "$d"[0-9]*; do
      [ -b "$part" ] || continue
      mdadm --zero-superblock "$part" 2>/dev/null || true
    done
  done
fi

log "Wiping old partition signatures to avoid confusion"
for d in "${DISKS[@]}"; do
  log "  wipefs -a $d"
  wipefs -a "$d" || true
  # Clear residual GPT/MBR so the auto-installer starts from a clean table.
  if command -v sgdisk >/dev/null 2>&1; then sgdisk --zap-all "$d" 2>/dev/null || true; fi
done

[ "${#DISKS[@]}" -ge 2 ] || die "At least 2 disks required for ZFS mirror"

# Disks used for ZFS mirror: pick a same-size pair (required by installer).
mapfile -t ZFS_DISKS < <(select_zfs_mirror_disks "${DISKS[@]}" || true)
if [ "${#ZFS_DISKS[@]}" != 2 ] && [ "${#ZFS_DISKS[@]}" != 4 ]; then
  log "Detected disk sizes:"
  for d in "${DISKS[@]}"; do
    bytes="$(disk_size_bytes "$d")"
    if [ -n "$bytes" ]; then
      gib=$((bytes / 1024 / 1024 / 1024))
      log "  $d -> ${gib} GiB (${bytes} bytes)"
    else
      log "  $d -> unknown size"
    fi
  done
  die "Could not find 2 or 4 equal-sized disks for the ZFS pool (raid1 needs 2, raid10 needs 4)"
  exit 1
fi
log "Selected ZFS disks: ${ZFS_DISKS[*]}"

# RAID level + answer.toml disk-list derive from the ZFS disk count: 2 -> raid1
# (2-way mirror), 4 -> raid10 (stripe of two mirrors: ~half the raw capacity,
# still mirror-redundant). Override with ZFS_RAID=... only if you need otherwise.
NUM_ZFS_DISKS="${#ZFS_DISKS[@]}"
if [ "$NUM_ZFS_DISKS" -eq 4 ]; then
  : "${ZFS_RAID:=raid10}"
  DISK_LIST='["vda", "vdb", "vdc", "vdd"]'
else
  : "${ZFS_RAID:=raid1}"
  DISK_LIST='["vda", "vdb"]'
fi
log "ZFS layout: ${NUM_ZFS_DISKS} disk(s) -> ${ZFS_RAID}; disk-list=${DISK_LIST}; swap reserve=${SWAP_GB_PER_DISK} GB/disk"

# Ensure the selected ZFS disks are first in QEMU, so they map to vda/vdb (disk-list in answer.toml).
REMAINING_DISKS=()
for d in "${DISKS[@]}"; do
  skip=0
  for zd in "${ZFS_DISKS[@]}"; do
    if [ "$d" = "$zd" ]; then
      skip=1
      break
    fi
  done
  [ "$skip" -eq 1 ] || REMAINING_DISKS+=("$d")
done
QEMU_DISKS=( "${ZFS_DISKS[@]}" "${REMAINING_DISKS[@]}" )
log "QEMU disk order (vda,vdb first): ${QEMU_DISKS[*]}"

# Reserve SWAP_GB_PER_DISK per disk for raw swap on ZFS disks (carved from free space by zfs.hdsize; with 3 disks we use the first two fully and the 3rd as a swap disk)
log "Computing ZFS hdsize for disks: ${ZFS_DISKS[*]}"
MIN_GB=""
for d in "${ZFS_DISKS[@]}"; do
  bytes=$(blockdev --getsize64 "$d" 2>/dev/null || true)
  [ -n "$bytes" ] || continue
  gb=$((bytes / 1024 / 1024 / 1024))
  if [ -z "$MIN_GB" ] || [ "$gb" -lt "$MIN_GB" ]; then
    MIN_GB=$gb
  fi
done
if [ "${#DISKS[@]}" -eq 3 ]; then
  log "Three disks: no swap reserve on main disks (third disk will be used for swap in first_boot)"
  ZFS_HDSIZE_LINE=""
elif [ -z "$MIN_GB" ] || [ "$MIN_GB" -lt 1 ]; then
  log "WARNING: Could not get disk sizes, not setting zfs.hdsize (installer will use full disks)"
  ZFS_HDSIZE_LINE=""
else
  ZFS_HDSIZE=$((MIN_GB - SWAP_GB_PER_DISK))
  if [ "$ZFS_HDSIZE" -lt 32 ]; then
    log "WARNING: Disks too small (min ${MIN_GB} GB); not setting zfs.hdsize (no swap reservation)"
    ZFS_HDSIZE_LINE=""
  else
    log "Using zfs.hdsize = ${ZFS_HDSIZE} GB (${SWAP_GB_PER_DISK} GB free per disk for raw swap)"
    ZFS_HDSIZE_LINE="zfs.hdsize = ${ZFS_HDSIZE}"
  fi
fi

# disk-list (DISK_LIST) was set above from the ZFS disk count. The installer runs
# inside QEMU (Step 5) where the -drive arguments appear as /dev/vda, vdb, vdc,
# vdd in the guest, in QEMU_DISKS order (ZFS disks first).

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
zfs.raid = "${ZFS_RAID}"
${ZFS_HDSIZE_LINE}
disk-list = ${DISK_LIST}

[first-boot]
source = "from-iso"
ordering = "fully-up"
EOF

log "Validating answer file"
proxmox-auto-install-assistant validate-answer "$ANSWER_FILE"

log "Preparing auto-install ISO"
# The assistant will output something like proxmox-ve_9.0-1-auto-from-iso.iso
proxmox-auto-install-assistant prepare-iso \
  --fetch-from iso \
  --answer-file "$ANSWER_FILE" \
  --on-first-boot "$FIRST_BOOT_SCRIPT_PATH" \
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
for d in "${QEMU_DISKS[@]}"; do
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

if ! grep -q "auto vmbr0" /mnt/etc/network/interfaces; then
# Determine the ACTUAL uplink NIC from the interfaces Hetzner just generated,
# instead of trusting ${PREDICTED} (= physical_network_interfaces | head -1).
# On multi-NIC hosts the active uplink isn't always first, and a mismatch
# silently breaks guest networking — the netmask fix, the IPv6-gateway
# derivation and the IPv4 MASQUERADE all key off the NIC name, so a wrong guess
# yields a node with no guest IPv6 + NAT on the wrong NIC (every VM then fails
# to provision: no egress for the in-guest installer). Pick the iface that
# actually carries the routed global IPv6.
UPLINK_IF="$(awk '
    $1=="iface" && $3=="inet6" && $4=="static" {ifc=$2; next}
    ifc && $1=="address" && $2 ~ /::/ && $2 !~ /^fe80/ {print ifc; exit}
    $1=="iface" {ifc=""}
  ' /mnt/etc/network/interfaces)"
[[ -z "$UPLINK_IF" ]] && UPLINK_IF="$PREDICTED"
log "Uplink NIC for vmbr0 NAT/routing: ${UPLINK_IF} (predicted was ${PREDICTED})"

# Routed-/64 model: the host main IPv6 must be /128 on the uplink so the whole
# /64 can live on vmbr0 for the guests.
sed -i -e "/^iface ${UPLINK_IF} inet6 static$/,/^$/{
    s/^\([[:space:]]*netmask[[:space:]]\)64$/\1128/
}" /mnt/etc/network/interfaces

# Derive vmbr0 IPv6 gateway (…::1/64) from the first GLOBAL inet6 address in the
# generated file — NIC-name agnostic, so a prediction mismatch can't skip it.
VM_V6_GATEWAY=""
VM_V6_PREFIXLEN="64"
PRIMARY_V6_ADDR="$(awk '
    $1=="iface" && $3=="inet6" && $4=="static" {inblk=1; next}
    inblk && $1=="address" && $2 ~ /::/ && $2 !~ /^fe80/ {print $2; exit}
    inblk && $1=="iface" {inblk=0}
  ' /mnt/etc/network/interfaces)"
if [[ -n "$PRIMARY_V6_ADDR" ]]; then
  ADDR_ONLY="${PRIMARY_V6_ADDR%%/*}"
  ADDR_ONLY="${ADDR_ONLY//[[:space:]]/}"
  # Hetzner main IP is usually n:n:n:n::h → routed /64 is n:n:n:n::/64; vmbr0 = n:n:n:n::1/64
  if [[ "$ADDR_ONLY" == *::* ]]; then
    PREFIX64="${ADDR_ONLY%%::*}"
    COLONS="${PREFIX64//[^:]/}"
    if [[ "${#COLONS}" -eq 3 ]]; then
      VM_V6_GATEWAY="${PREFIX64}::1"
      log "vmbr0 inet6: ${VM_V6_GATEWAY}/${VM_V6_PREFIXLEN} (from ${PRIMARY_V6_ADDR} on ${UPLINK_IF})"
    fi
  fi
fi
# A node without guest IPv6 has no working egress and fails to provision every
# VM. Fail loudly here rather than silently shipping a broken node into the pool.
[[ -z "$VM_V6_GATEWAY" ]] && die "Could not derive vmbr0 IPv6 gateway from /etc/network/interfaces (uplink=${UPLINK_IF}); refusing to ship a node without guest IPv6."

# A port-less bridge must not inherit a guest TAP MAC: removing that VM would
# change the gateway MAC for every remaining guest. Stable, locally administered
# per-node value for new installs; existing nodes keep their current MAC.
NVPS_BRIDGE_MAC_HEX="$(printf '%s' "$PRIMARY_V6_ADDR" | sha256sum)"
NVPS_BRIDGE_MAC="02:${NVPS_BRIDGE_MAC_HEX:0:2}:${NVPS_BRIDGE_MAC_HEX:2:2}:${NVPS_BRIDGE_MAC_HEX:4:2}:${NVPS_BRIDGE_MAC_HEX:6:2}:${NVPS_BRIDGE_MAC_HEX:8:2}"
{
  echo ""
  echo "auto vmbr0"
  echo "iface vmbr0 inet static"
  echo "    hwaddress ${NVPS_BRIDGE_MAC}"
  # ⚠️ El MASQUERADE de estos dos rangos se retiro el 16-08-2026. Salia por
  # ${UPLINK_IF}, que ya NO tiene IPv4 (bajas de Hetzner de ese dia), asi que
  # no podia funcionar. Medido antes de quitarlo: contadores a cero y 0
  # paquetes en 150 s en un nodo con 40 VMs. La salida IPv4 del invitado va por
  # el tunel a la base, no por el nodo.
  # La 10.0.0.1/16 se queda: no cuesta nada y da puerta de enlace a cualquier
  # invitado viejo que apareciera; el rango ya no lo usa ninguna VM.
  echo "    address 10.0.0.1/16"
  echo "    bridge-ports none"
  echo "    bridge-stp off"
  echo "    bridge-fd 0"
  echo "    post-up   echo 1 > /proc/sys/net/ipv4/ip_forward"
  echo "    post-up   iptables -t raw -I PREROUTING -i fwbr+ -j CT --zone 1"
  echo "    post-down iptables -t raw -D PREROUTING -i fwbr+ -j CT --zone 1"
  # --- Esquema de direccionamiento por-VM (2026-08-13) ---------------------
  # La IPv4 del invitado pasa a ser 10.64.<vmid_hi>.<vmid_lo>/16 con puerta de
  # enlace 10.64.255.1, IDÉNTICA en todos los nodos, para que la dirección
  # VIAJE CON LA VM y deje de derivarse del nodo. Ver
  # base/docs/egress-failover-e-ipv6-estable.md §11bis.
  #
  # Es ADITIVO a propósito: el 10.0.0.1/16 y su MASQUERADE se quedan sirviendo
  # a los invitados que aún estén en el esquema viejo, así que un nodo se puede
  # convertir sin tocar a ninguna de sus VMs y la flota se migra en secuencia
  # sin ventana de colisión (10.64.0.0/16 no solapa con 10.0.0.0/16).
  echo "    post-up   ip addr add 10.64.255.1/16 dev vmbr0"
  echo ""
  echo "iface vmbr0 inet6 static"
  echo "    address ${VM_V6_GATEWAY}/${VM_V6_PREFIXLEN}"
  echo "    post-up   echo 1 > /proc/sys/net/ipv6/conf/all/forwarding"
  # Puerta de enlace IPv6 UNIFORME para el invitado. Hoy migrate_vm.sh calcula
  # DST_GATEWAY="<prefijo_del_nodo>::1", así que la ruta por defecto del
  # invitado depende del nodo y hay que reescribirla dentro de Windows en cada
  # migración — justo lo que el proyecto elimina. Con fe80::1 en el bridge de
  # TODOS los nodos, la puerta de enlace deja de cambiar.
  #
  # No colisiona con el fe80::1 del uplink (Hetzner usa esa misma dirección
  # como gateway v6 del nodo): las link-local tienen ámbito por interfaz.
  echo "    post-up   ip -6 addr add fe80::1/64 dev vmbr0 || true"
} >> /mnt/etc/network/interfaces
fi

# Persist forwarding independently of the vmbr0 post-up hooks, so the node keeps
# forwarding even if the interfaces stanza is ever regenerated or reloaded.
mkdir -p /mnt/etc/sysctl.d
cat > /mnt/etc/sysctl.d/99-neuravps-forwarding.conf <<'SYSCTL'
net.ipv4.ip_forward = 1
net.ipv6.conf.all.forwarding = 1
# proxy_arp en el bridge: con el esquema 10.64.<vmid>/16 la puerta de enlace
# (10.64.255.1) es la misma en todos los nodos, así que el invitado considera
# on-link todo 10.64.0.0/16 — incluidas VMs de OTROS nodos, que nunca
# contestarían al ARP. Con proxy_arp el nodo responde por lo que sepa enrutar.
net.ipv4.conf.vmbr0.proxy_arp = 1
SYSCTL

# --- tuneles base<->nodo anclados a las VIPs (egress-failover §4.5) ---

# El nodo ya no depende de su IPv4 publica: sale por un tunel ip6gre hacia la
# VIP de failover de su region, y la base hace el SNAT. Anclar a la VIP y no a
# la IP principal de la base es lo que hace que un mantenimiento de base sea
# invisible: al moverse la VIP el tunel la sigue hasta la superviviente.
# Ver base/docs/egress-failover-e-ipv6-estable.md §4.5, §15, §17.
mkdir -p /mnt/usr/local/sbin /mnt/etc/systemd/system /mnt/etc/default
NODE_NUM=$((10#$(echo "$NAME" | cut -d- -f1)))

cat > /mnt/usr/local/sbin/neuravps-tunnels.sh <<'NVXEOF'
#!/usr/bin/env bash
# Lado NODO de los tuneles base<->nodo. Idempotente.
# Ver base/docs/egress-failover-e-ipv6-estable.md §4.5.
#
# v2 (2026-08-14): ANCLADO A LAS VIPs DE FAILOVER, no a las IPs principales de
# las bases. El tunel deja de apuntar a una MAQUINA y apunta a un SERVICIO: al
# moverse una VIP, el tunel la sigue sin tocar nada y termina en la
# superviviente. Es lo que hace invisible el mantenimiento de una base.
set -u

. /etc/default/neuravps-tunnels

: "${NODE_ID:?falta NODE_ID}"
SLOT="${SLOT:-$NODE_ID}"
TRANSIT_BASE="${TRANSIT_BASE:-2a01:4f9:c01f:e:ffff::}"
VIP_FSN="${VIP_FSN:-2a01:4f8:fff2:95::2}"
VIP_HEL="${VIP_HEL:-2a01:4f9:fff1:5f::2}"
IDENT6="${IDENT6:-2a01:4f9:c01f:e::/64}"
HOME_REGION="${HOME_REGION:-fsn}"

# El /112 da 4096 huecos de 16 direcciones. El hueco 0 esta RESERVADO para las
# direcciones canonicas de las bases, asi que un nodo nunca puede ocuparlo.
[ "$SLOT" -ge 1 ] && [ "$SLOT" -le 4095 ] || { echo "SLOT $SLOT fuera de rango 1-4095" >&2; exit 1; }

transit() { printf '%s%x' "$TRANSIT_BASE" $(( $1 * 16 + $2 )); }

NODE_V6="$(ip -6 addr show scope global | sed -n 's#.*inet6 \([0-9a-f:]*\)/128.*#\1#p' | head -1)"
[ -n "$NODE_V6" ] || NODE_V6="$(ip -6 addr show dev "$(ip -6 route show default | sed -n 's/.*dev \([^ ]*\).*/\1/p' | head -1)" scope global | sed -n 's#.*inet6 \([0-9a-f:]*\)/.*#\1#p' | head -1)"
NODE_V4="10.65.$(( NODE_ID / 256 )).$(( NODE_ID % 256 ))"

# Direcciones canonicas: son DE LA VIP, no de la maquina. Por eso el nodo puede
# enrutarlas siempre por el mismo tunel aunque la base de detras cambie.
CANON_FSN="$(transit 0 0)"
CANON_HEL="$(transit 0 2)"

mk() { # iface remoto direccion_local/127
  local show cur
  show="$(ip -6 tunnel show "$1" 2>/dev/null)"
  cur="$(printf '%s' "$show" | sed -n 's/.*remote \([0-9a-f:]*\) .*/\1/p')"
  if [ "$cur" != "$2" ] || ! printf '%s' "$show" | grep -q 'encaplimit none'; then
    ip link del "$1" 2>/dev/null
    # `encaplimit none` es OBLIGATORIO: por defecto ip6gre mete una cabecera
    # DSTOPT, el nexthdr pasa a 60 en vez de 47 y las reglas de firewall que
    # casan GRE dejan de casar. El tunel muere EN SILENCIO: UP en los dos
    # extremos, TX subiendo, RX clavado a 0.
    ip link add name "$1" type ip6gre local "$NODE_V6" remote "$2" encaplimit none || return 1
  fi
  ip link set "$1" mtu 1456 up
  ip -6 addr replace "$3" dev "$1"
  # rp_filter=0 en los tuneles. Con anclaje a VIP el camino es asimetrico A
  # PROPOSITO: el trafico entra por el tunel de la VIP que la base posee y sale
  # por el de casa. El filtro de ruta inversa lo tira como "martian source" y el
  # sintoma es desconcertante — IPv6 pasa (Linux no tiene rp_filter para v6) y
  # IPv4 no. El modo laxo (2) NO basta: tiene que ser 0.
  # Riesgo acotado: estos enlaces solo llevan GRE entre maquinas nuestras, ya
  # filtrado por [IPSET base]; la uplink conserva su rp_filter.
  sysctl -qw "net.ipv4.conf.$1.rp_filter=0" 2>/dev/null
}

[ -x /usr/local/sbin/nvx-mss.sh ] && /usr/local/sbin/nvx-mss.sh >/dev/null 2>&1

mk tun-fsn "$VIP_FSN" "$(transit "$SLOT" 1)/127"
mk tun-hel "$VIP_HEL" "$(transit "$SLOT" 3)/127"

# Direccion propia del nodo para su trafico saliente. NO 10.64.255.1: esa es la
# puerta de enlace de los invitados y es identica en todos los nodos, asi que la
# base no sabria a que tunel devolver el retorno.
ip addr replace "${NODE_V4}/32" dev lo

ip -6 route replace "$IDENT6" dev vmbr0

# Cada canonica por SU tunel. Esto es lo que hace que el retorno acabe siempre
# en la base que emitio, sea cual sea.
#
# Y los DOS /127 de transito de este nodo, cada uno por su tunel. Son
# imprescindibles en la tabla 100: el transito vive DENTRO del /64 de identidad,
# asi que una respuesta del invitado hacia la direccion de transito de una base
# casa la regla de politica y cae en la tabla 100. Sin estas rutas solo encuentra
# la ruta por defecto y sale por el tunel de CASA — de modo que todo lo que
# origine la base REMOTA (sondeo cruzado de conncheck, file-bridge) se pierde.
for T in main 100; do
  ip -6 route replace "${CANON_FSN}/128" dev tun-fsn table $T
  ip -6 route replace "${CANON_HEL}/128" dev tun-hel table $T
  ip -6 route replace "$(transit "$SLOT" 0)/127" dev tun-fsn table $T
  ip -6 route replace "$(transit "$SLOT" 2)/127" dev tun-hel table $T
done

# Tablas de politica. Identificadores NUMERICOS a proposito: /etc/iproute2/
# rt_tables no existe en todos los nodos y con nombre falla en silencio.
# `iif vmbr0` es OBLIGATORIO: las direcciones de transito viven DENTRO del /64
# de identidad, asi que sin acotar por interfaz el trafico que llega DE la base
# tambien casaria y el nodo lo devolveria por el tunel: bucle.
ip -6 rule del from "$IDENT6" iif vmbr0 lookup 100 2>/dev/null
ip -6 rule add from "$IDENT6" iif vmbr0 lookup 100 priority 100
ip rule del from 10.64.0.0/16 iif vmbr0 lookup 101 2>/dev/null
ip rule add from 10.64.0.0/16 iif vmbr0 lookup 101 priority 101
ip rule del from "$NODE_V4" lookup 102 2>/dev/null
ip rule add from "$NODE_V4" lookup 102 priority 102

# --- region de casa ----------------------------------------------------------
# `auto` = la mas cercana, medida. Es el criterio correcto (el trafico del
# cliente debe salir por la base LOCAL) y ademas evita que install.sh tenga que
# averiguar en que centro de datos esta el nodo: la separacion es de ~1 ms
# contra ~20 ms, asi que no hay ambiguedad posible.
if [ "$HOME_REGION" = auto ]; then
  best=""; bestms=999999
  for r in fsn hel; do
    c=$([ "$r" = fsn ] && echo "$CANON_FSN" || echo "$CANON_HEL")
    t0=$(date +%s%N)
    if timeout 3 bash -c "</dev/tcp/$c/443" 2>/dev/null; then
      ms=$(( ($(date +%s%N) - t0) / 1000000 ))
      [ "$ms" -lt "$bestms" ] && { bestms=$ms; best=$r; }
    fi
  done
  HOME_REGION="${best:-fsn}"
  logger -t neuravps-tunnels "region de casa detectada: $HOME_REGION (${bestms} ms)"
  # Cachearla: si en el proximo arranque la base local esta caida, la medicion
  # elegiria la remota y el nodo se quedaria ahi para siempre.
  sed -i "s/^HOME_REGION=.*/HOME_REGION=$HOME_REGION/" /etc/default/neuravps-tunnels
fi

# Rutas por defecto hacia el tunel de casa. El sondeo
# (neuravps-tunnel-probe.sh) las reapunta al otro si esta base deja de contestar.
/usr/local/sbin/neuravps-tunnel-select.sh "$HOME_REGION"

exit 0
NVXEOF

cat > /mnt/usr/local/sbin/neuravps-tunnel-select.sh <<'NVXEOF'
#!/usr/bin/env bash
# Apunta las rutas por defecto del nodo (las suyas y las de sus invitados) al
# tunel de una region. Idempotente y sin efecto si ya esta apuntando ahi.
# Uso: neuravps-tunnel-select.sh <fsn|hel>
set -u
. /etc/default/neuravps-tunnels
REGION="$1"
TUN="tun-$REGION"
NODE_V4="10.65.$(( NODE_ID / 256 )).$(( NODE_ID % 256 ))"

ip link show "$TUN" >/dev/null 2>&1 || { echo "$TUN no existe" >&2; exit 1; }

# Si ya estamos ahi, no tocar nada: reescribir la ruta por defecto tira las
# conexiones en curso de los invitados.
cur="$(ip -4 route show default | sed -n 's/.*dev \([^ ]*\).*/\1/p' | head -1)"
[ "$cur" = "$TUN" ] && exit 0

ip -6 route replace default dev "$TUN" table 100
ip route    replace default dev "$TUN" table 101
ip route    replace default dev "$TUN" src "$NODE_V4" table 102

# El nodo ya no tiene IPv4 publica: TODA su salida v4 va por el tunel y la base
# hace el SNAT. El `src` es obligatorio: sin direccion de origen el kernel no
# puede elegir una y la conexion ni sale.
if [ "${DEFAULT_V4_VIA_TUNNEL:-0}" = "1" ]; then
  ip route replace default dev "$TUN" src "$NODE_V4"
fi

logger -t neuravps-tunnels "salida por defecto -> $TUN"
echo "salida por defecto -> $TUN"
NVXEOF

cat > /mnt/usr/local/sbin/neuravps-tunnel-probe.sh <<'NVXEOF'
#!/usr/bin/env bash
# Sondeo del tunel activo. GRE es SIN ESTADO: el tunel NO se cae cuando la base
# muere, sigue UP para siempre apuntando a una caja muerta. Sin esto, la unica
# recuperacion posible es que alguien mueva la VIP (~5-6 min en una caida no
# planificada). Con esto, el nodo se pasa al otro tunel en ~30 s.
#
# El anclado a VIPs y el sondeo son ORTOGONALES y cubren casos distintos:
#   - mantenimiento PLANIFICADO: movemos la VIP antes -> 0 s, sin sondeo.
#   - muerte NO planificada     : el sondeo salta a la otra region -> ~30 s.
set -u
. /etc/default/neuravps-tunnels

TRANSIT_BASE="${TRANSIT_BASE:-2a01:4f9:c01f:e:ffff::}"
HOME_REGION="${HOME_REGION:-fsn}"
OTHER_REGION=$([ "$HOME_REGION" = fsn ] && echo hel || echo fsn)
STATE=/run/neuravps-tunnel-probe.state
FAILS_TO_SWITCH=2      # ~30 s con el timer a 15 s
OKS_TO_RETURN=4        # volver a casa cuesta mas: evita el ping-pong

canon() { [ "$1" = fsn ] && printf '%s0' "$TRANSIT_BASE" || printf '%s2' "$TRANSIT_BASE"; }

# La base contesta 443 (nginx) por su direccion canonica. Un TCP completo prueba
# ida Y vuelta; el ping NO sirve: PVE tira el eco ICMPv6 y da falso negativo.
alive() { timeout 4 bash -c "</dev/tcp/$(canon "$1")/443" 2>/dev/null; }

cur="$(ip -4 route show default | sed -n 's/.*dev tun-\([a-z]*\).*/\1/p' | head -1)"
[ -n "$cur" ] || exit 0

read -r st n < "$STATE" 2>/dev/null || { st=""; n=0; }
[ "$st" = "$cur" ] || n=0

if [ "$cur" = "$HOME_REGION" ]; then
  if alive "$HOME_REGION"; then n=0; else
    n=$((n+1))
    if [ "$n" -ge "$FAILS_TO_SWITCH" ] && alive "$OTHER_REGION"; then
      logger -t neuravps-tunnel-probe "casa ($HOME_REGION) no responde x$n -> me paso a $OTHER_REGION"
      /usr/local/sbin/neuravps-tunnel-select.sh "$OTHER_REGION" && { echo "$OTHER_REGION 0" > "$STATE"; exit 0; }
    fi
  fi
else
  # Estamos fuera de casa: volver solo cuando casa lleve un rato estable.
  if alive "$HOME_REGION"; then
    n=$((n+1))
    if [ "$n" -ge "$OKS_TO_RETURN" ]; then
      logger -t neuravps-tunnel-probe "casa ($HOME_REGION) estable x$n -> vuelvo"
      /usr/local/sbin/neuravps-tunnel-select.sh "$HOME_REGION" && { echo "$HOME_REGION 0" > "$STATE"; exit 0; }
    fi
  else n=0; fi
fi

echo "$cur $n" > "$STATE"
NVXEOF

chmod 755 /mnt/usr/local/sbin/neuravps-tunnel*.sh

# Clamp de MSS. SIN esto el invitado anuncia MSS 1460 (su interfaz es 1500) pero
# el camino real pasa por un tunel de 1456: los paquetes grandes se pierden EN
# SILENCIO y el handshake TLS se cuelga. Medido el 2026-08-15: 5 clientes en 13 h
# con "Program cannot connect to internet". neuravps-tunnels.sh lo invoca al
# arrancar, asi que basta con dejar el fichero.
cat > /mnt/usr/local/sbin/nvx-mss.sh <<'NVXEOF'
#!/usr/bin/env bash
# Clamp de MSS para el trafico de los invitados que sale por el tunel.
#
# EL PROBLEMA QUE RESUELVE (medido en produccion el 2026-08-15)
# El invitado tiene la interfaz a MTU 1500 y anuncia MSS 1460, pero su camino
# real pasa por un ip6gre de MTU 1456 (1500 - 40 de cabecera IPv6 - 4 de GRE).
# Sin clamp, los paquetes grandes se pierden EN SILENCIO: el TCP conecta, y
# luego el handshake TLS se cuelga con retransmisiones (3,2 s y 6,5 s = backoff
# 1+2 y 1+2+4) o da timeout. StrategyQuant sale solo por IPv4
# (-Djava.net.preferIPv4Stack=true) asi que se lo come entero, mientras el
# navegador del mismo servidor va por IPv6 y funciona — lo que despista mucho.
# Prueba controlada: api.strategyquant.com daba 000 a los 25 s en nodos sin
# clamp y 200 en 0,24 s en nodos con clamp, con el MISMO MTU en el invitado.
#
# ⚠️ POR QUE LOS DOS SENTIDOS SON DISTINTOS
# La opcion MSS es POR SENTIDO: la que va en el SYN le dice al SERVIDOR cuanto
# puede mandar, y la del SYN-ACK se lo dice al INVITADO. Hay que tocar las dos.
#   - saliente (oifname tun-*): la decision de ruta ya esta tomada y es la de la
#     tabla de politica, que apunta al tunel -> `rt mtu` resuelve 1456. Correcto
#     y se autoajusta si cambia el MTU del tunel.
#   - entrante (iifname tun-*): la ruta de SALIDA es vmbr0, que es 1500, asi que
#     `rt mtu` daria MSS 1460 y seria un NO-OP. Hay que fijarlo al MTU real del
#     tunel. Se recalcula en cada ejecucion, asi que sigue atado a la realidad.
#
# Idempotente: reconstruye su propia tabla, no toca el resto del cortafuegos.
set -u

TUN=$(ip -br link show type ip6gre 2>/dev/null | grep -oE '^tun-[a-z]+' | head -1)
[ -n "$TUN" ] || { echo "nvx-mss: sin tuneles, nada que hacer"; exit 0; }
MTU=$(cat "/sys/class/net/$TUN/mtu" 2>/dev/null)
[ -n "$MTU" ] && [ "$MTU" -gt 100 ] 2>/dev/null || { echo "nvx-mss: MTU de $TUN ilegible" >&2; exit 1; }

MSS4=$(( MTU - 40 ))   # 20 IP + 20 TCP
MSS6=$(( MTU - 60 ))   # 40 IPv6 + 20 TCP

nft delete table inet nvxmss 2>/dev/null
nft -f - <<EOF
table inet nvxmss {
    chain clamp {
        type filter hook forward priority mangle; policy accept;

        # Saliente por el tunel: rt mtu resuelve la ruta REAL del invitado
        # (la de la tabla de politica), no la principal.
        oifname "tun-*" tcp flags syn tcp option maxseg size set rt mtu

        # Entrante del tunel hacia el invitado: aqui rt mtu daria el MTU de
        # vmbr0 (1500) y no serviria de nada. Valor explicito, recalculado.
        iifname "tun-*" meta nfproto ipv4 tcp flags syn tcp option maxseg size set $MSS4
        iifname "tun-*" meta nfproto ipv6 tcp flags syn tcp option maxseg size set $MSS6
    }
}
EOF
echo "nvx-mss: clamp puesto sobre $TUN (mtu $MTU -> mss v4 $MSS4 / v6 $MSS6)"
NVXEOF
chmod 755 /mnt/usr/local/sbin/nvx-mss.sh

cat > /mnt/etc/systemd/system/neuravps-tunnels.service <<'NVXEOF'
[Unit]
Description=NeuraVPS: tuneles base<->nodo + direccionamiento propio
Wants=network-online.target
After=network-online.target
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/neuravps-tunnels.sh
[Install]
WantedBy=multi-user.target
NVXEOF

# ⚠️ El clamp NECESITA su propia unidad, no basta con que lo llame el script de
# tuneles. `/etc/nftables.conf` empieza por `flush ruleset`, asi que cuando
# nftables carga BORRA la tabla del clamp que los tuneles acababan de poner. En
# un nodo recien arrancado el resultado es 0 reglas maxseg y los invitados con
# agujero negro de PMTU: TCP conecta y luego se cuelga. Medido el 16-08-2026 en
# 10 nodos reiniciados ese dia, con 9 VMs de clientes afectadas — y el sintoma
# despista porque el navegador (IPv6) va bien y solo falla lo que sale por IPv4.
# Por eso esta unidad va DESPUES de nftables, no solo despues de los tuneles.
cat > /mnt/etc/systemd/system/nvx-mss.service <<'NVXEOF'
[Unit]
Description=NeuraVPS: clamp de MSS del trafico de invitados por el tunel
# ⚠️ NI Requires= NI Wants= sobre nftables.service: el 16-08-2026 un
# `enable --now` con Requires= arranco nftables —inactivo en los nodos— en los
# 227 a la vez, y su `flush ruleset` borro el clamp MSS de toda la flota.
# PartOf= repone la tabla si alguien reinicia nftables.
After=neuravps-tunnels.service nftables.service network-online.target
Wants=neuravps-tunnels.service
PartOf=nftables.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/nvx-mss.sh
Restart=on-failure
RestartSec=15s
StartLimitBurst=10
[Install]
WantedBy=multi-user.target
NVXEOF


# --- Aislamiento VM<->VM en el puente ----------------------------------------
# Las VMs de un nodo cuelgan del mismo puente. El encaminamiento manda su
# trafico por el tunel, pero eso es capa 3 y el cliente es Administrador de su
# Windows: puede ponerse una direccion on-link y hablar con sus vecinas por
# capa 2, saltandose el nodo y la base. Comprobado el 16-08-2026 entre dos VMs
# del operador: con las dos en /16 el 3389 respondia; con la regla puesta, no
# (contador: 1 paquete ARP descartado).
cat > /mnt/usr/local/sbin/nvx-aisla.sh <<'NVXEOF'
#!/usr/bin/env bash
# Aisla a las VMs entre si dentro del puente vmbr0.
#
# QUE RESUELVE
# Las VMs de un mismo nodo cuelgan todas del mismo puente. El encaminamiento
# manda su trafico por el tunel a la base —incluso el que va de una VM a su
# vecina— pero eso es una decision de CAPA 3, y el cliente es Administrador de
# su propio Windows: puede ponerse una direccion on-link y hablar con sus
# vecinas por capa 2, saltandose el nodo y la base enteros.
#
# Hasta 2026-08-16 eso lo tapaba el firewall POR VM (el `fwbr` que crea PVE con
# `firewall=1`). Esta regla lo sustituye por algo mucho mas barato: una tabla
# estatica por nodo en vez de tres interfaces de red por VM (5628 en la flota).
#
# ⚠️ NO basta con el /32 del invitado. El /32 quita la vecindad on-link, pero es
# configuracion DENTRO de la VM y el cliente puede deshacerla en diez segundos,
# a proposito o al reparar la red. El control tiene que vivir donde el cliente
# no llega: aqui.
#
# ⚠️ `bridge link set ... isolated on` NO sirve. Se probo el 16-08-2026 y dejo a
# la VM sin salida: el puerto aislado pierde tambien el camino hacia el propio
# puente, que es por donde sale TODO su trafico legitimo.
#
# POR QUE ESTAS CUATRO REGLAS Y NO MAS
# `vmbr0` se declara con `bridge-ports none` en los 227 nodos (comprobado en
# muestra): sus unicos puertos son los de las VMs. Asi que lo UNICO que ese
# puente reenvia es trafico VM<->VM. Todo lo legitimo —salida a Internet, y el
# SMB entre las VMs de un mismo cliente, que viaja por la base— es entrega
# LOCAL al puente y lo enruta el nodo, no pasa por el hook de forward.
#
# Los nombres dependen de si la VM lleva `firewall=1` (el puerto en vmbr0 es
# `fwpr<vmid>p0`) o `firewall=0` (es `tap<vmid>i0`). Se cubren las cuatro
# combinaciones para que la flota pueda estar mezclada durante la transicion.
# El puente interno `fwbr<vmid>i0` de cada VM reenvia tap<->fwln, que no casa
# con ninguna regla: no se toca.
#
# `policy accept` a proposito: si algun nombre se me escapa, el trafico pasa en
# vez de cortarse. Una regla de aislamiento que se equivoca hacia "abierto" es
# un fallo; hacia "cerrado" es una averia de cliente.
#
# Idempotente: reconstruye su propia tabla y no toca el resto del cortafuegos.
set -u

nft delete table bridge nvxaisla 2>/dev/null
nft -f - <<'EOF'
table bridge nvxaisla {
    chain aisla {
        type filter hook forward priority filter; policy accept;

        # `counter` a proposito: en condiciones normales estas reglas NUNCA
        # deben casar, porque el /32 del invitado ya evita que lo intente. Un
        # contador que sube significa que alguien esta hablando con sus vecinas
        # por capa 2 — o una VM mal configurada, o alguien probando. Convierte
        # la regla en detector ademas de control, y cuesta nada.
        iifname "tap*"  oifname "tap*"  counter drop
        iifname "tap*"  oifname "fwpr*" counter drop
        iifname "fwpr*" oifname "tap*"  counter drop
        iifname "fwpr*" oifname "fwpr*" counter drop
    }
}
EOF

N=$(nft list chain bridge nvxaisla aisla 2>/dev/null | grep -c drop)
[ "$N" = "4" ] || { echo "nvx-aisla: esperaba 4 reglas, hay $N" >&2; exit 1; }
echo "nvx-aisla: aislamiento VM<->VM puesto en el puente ($N reglas)"
NVXEOF
chmod 755 /mnt/usr/local/sbin/nvx-aisla.sh

# ⚠️ DESPUES de nftables por el mismo motivo que nvx-mss: `flush ruleset`
# borraria la tabla. Y aqui el fallo seria peor de detectar: nada se rompe,
# solo deja de haber aislamiento, y eso no da sintomas hasta que se aprovecha.
cat > /mnt/etc/systemd/system/nvx-aisla.service <<'NVXEOF'
[Unit]
Description=NeuraVPS: aislamiento VM<->VM en el puente vmbr0
# ⚠️ NI Requires= NI Wants= sobre nftables.service: el 16-08-2026 un
# `enable --now` con Requires= arranco nftables —inactivo en los nodos— en los
# 227 a la vez, y su `flush ruleset` borro el clamp MSS de toda la flota.
# PartOf= repone la tabla si alguien reinicia nftables.
After=nftables.service network-online.target
PartOf=nftables.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/nvx-aisla.sh
Restart=on-failure
RestartSec=15s
StartLimitBurst=10
[Install]
WantedBy=multi-user.target
NVXEOF

cat > /mnt/etc/systemd/system/neuravps-tunnel-probe.service <<'NVXEOF'
[Unit]
Description=NeuraVPS: sondeo del tunel activo del nodo
After=neuravps-tunnels.service
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/neuravps-tunnel-probe.sh
NVXEOF

cat > /mnt/etc/systemd/system/neuravps-tunnel-probe.timer <<'NVXEOF'
[Unit]
Description=NeuraVPS: sondeo del tunel activo cada 15 s
[Timer]
OnBootSec=60
OnUnitActiveSec=15
AccuracySec=1s
[Install]
WantedBy=timers.target
NVXEOF


# HOME_REGION=auto: el nodo mide a que base llega antes y se queda con ella,
# cacheando el resultado en este mismo fichero. Asi install.sh no necesita saber
# en que centro de datos esta la maquina (la separacion es ~1 ms contra ~20 ms).
cat > /mnt/etc/default/neuravps-tunnels <<NVXEOF
# Parametros de ESTE nodo. Ver docs/egress-failover-e-ipv6-estable.md §4.5.
NODE_ID=${NODE_NUM}
SLOT=${NODE_NUM}
HOME_REGION=auto
IDENT6=2a01:4f9:c01f:e::/64
TRANSIT_BASE=2a01:4f9:c01f:e:ffff::
VIP_FSN=2a01:4f8:fff2:95::2
VIP_HEL=2a01:4f9:fff1:5f::2
DEFAULT_V4_VIA_TUNNEL=0
NVXEOF

cat > /mnt/etc/sysctl.d/zz-neuravps-rpfilter.conf <<'NVXEOF'
# Filtro de ruta inversa: los tuneles base<->nodo lo necesitan a 0.
#
# Con los tuneles anclados a las VIPs de failover el camino es ASIMETRICO A
# PROPOSITO: el trafico entra por el tunel de la VIP que la base posee y sale por
# el de casa. rp_filter lo tira como "martian source", y el sintoma engana
# muchisimo porque Linux NO tiene rp_filter para IPv6: v6 pasa y v4 no, con
# tcpdump viendo el paquete en LOS DOS extremos y cero conntrack.
# El modo laxo (2) NO basta. Tiene que ser 0.
#
# El kernel usa el MAXIMO de conf.all y conf.<iface>, asi que poner `all` a 0 no
# afloja nada por si solo: cada interfaz conserva el suyo. Las que miran al
# cliente (vmbr0 en el nodo, enp6s0 en la base) siguen con el valor que tenian, y
# `default` se queda en 2 para que toda interfaz nueva nazca filtrada. El 0 lo
# pone explicitamente neuravps-tunnels.sh / neuravps-base-tunnels.sh sobre cada
# tunel, y solo sobre ellos: solo llevan GRE entre maquinas nuestras, ya filtrado
# por gre_peers y por el [IPSET base] del nodo.
#
# ⚠️ NO BASTA con que el script ponga el 0 sobre cada tunel: udev lo pisa. Al
# aparecer una interfaz, udev lanza `systemd-sysctl --prefix=.../<iface>`, que
# reaplica el comodin `net.ipv4.conf.*.rp_filter = 2` de 50-default.conf. Es
# ASINCRONO y gana casi siempre al `sysctl -w` del script. En geo-split no se
# nota (camino simetrico); solo muerde DURANTE UN FAILOVER, cuando una maquina
# sostiene la VIP de la otra region, y entonces los invitados se quedan sin
# Internet con el RDP de entrada funcionando, asi que no alerta nada. El comodin
# acotado a `tun-*` gana a 50-default.conf por el orden de nombre de fichero y
# deja `default` en 2. Medido 2026-08-27: 162 s sin salida en b1 recien
# reiniciada.
net.ipv4.conf.all.rp_filter = 0
net.ipv4.conf.default.rp_filter = 2
net.ipv4.conf.tun-*.rp_filter = 0
NVXEOF


# Los enlaces se activan al arrancar; el sondeo cada 15 s reapunta la salida a
# la otra region si la base de casa deja de contestar (GRE es SIN ESTADO: el
# tunel no se cae solo cuando la base muere).
chroot /mnt systemctl enable neuravps-tunnels.service >/dev/null 2>&1 || true
chroot /mnt systemctl enable neuravps-tunnel-probe.timer >/dev/null 2>&1 || true
chroot /mnt systemctl enable nvx-mss.service >/dev/null 2>&1 || true
chroot /mnt systemctl enable nvx-aisla.service >/dev/null 2>&1 || true
log "Tuneles anclados a VIP + sondeo instalados (nodo ${NODE_NUM}, region auto)"

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

# Integrated GPU (Intel xe/i915, AMD amdgpu) disabled in first_boot.sh

# Copy SSH key from rescue environment into new install (for first_boot SSH config)
if [ -f /root/.ssh/neuravps_id ]; then
  mkdir -p /mnt/root/.ssh
  cp -a /root/.ssh/neuravps_id /mnt/root/.ssh/neuravps_id
  chmod 600 /mnt/root/.ssh/neuravps_id
  log "Copied /root/.ssh/neuravps_id into new install"
fi

log "Cleaning up /mnt/hdd symlink"
rm -f /mnt/hdd

# --- Verify the bootloader actually installed before rebooting -----
# A completed OS install with EMPTY ESPs reboots into nothing and the
# node is unreachable (root cause of 0000192, 2026-06-23). Confirm at
# least one EFI System Partition holds a UEFI bootloader; otherwise fail
# loudly here instead of rebooting into a dead system.
log "Verifying bootloader on ESP(s) before reboot"
udevadm settle 2>/dev/null || true
ESP_MNT="/tmp/esp-verify"
mkdir -p "$ESP_MNT"
mapfile -t ESP_PARTS < <(lsblk -rno NAME,PARTTYPE 2>/dev/null | awk 'tolower($2)=="c12a7328-f81f-11d2-ba4b-00a0c93ec93b"{print "/dev/"$1}')
if [ "${#ESP_PARTS[@]}" -eq 0 ]; then
  die "No EFI System Partition found on the installed disks — the installer produced no UEFI bootloader. NOT rebooting (node would be unreachable)."
  exit 1
fi
ESP_OK=0
for _dev in "${ESP_PARTS[@]}"; do
  if mount -o ro "$_dev" "$ESP_MNT" 2>/dev/null; then
    if find "$ESP_MNT" -iname '*.efi' -print -quit 2>/dev/null | grep -q .; then
      log "  ESP $_dev: bootloader present"
      ESP_OK=1
    else
      log "  ESP $_dev: EMPTY (no .efi found)"
    fi
    umount "$ESP_MNT" 2>/dev/null || true
  else
    log "  ESP $_dev: mount failed"
  fi
done
rmdir "$ESP_MNT" 2>/dev/null || true
if [ "$ESP_OK" -ne 1 ]; then
  die "All ESPs are empty (no UEFI bootloader) — the install did not produce a bootable system. NOT rebooting. Re-run the install; if it recurs, inspect proxmox-boot-tool / the QEMU installer step."
  exit 1
fi
log "Bootloader verified on at least one ESP."

# Put the PXE/network entries FIRST in the permanent BootOrder. Hetzner's
# rescue engages via PXE: with disk-first order an armed rescue is silently
# skipped and the box boots the local OS again (hit on 0000008, 2026-07-03 —
# two reboots wasted). Hetzner PXE chainloads the local disk when no rescue
# is armed, so normal boots are unaffected. Non-fatal on quirky firmware.
if command -v efibootmgr >/dev/null 2>&1; then
  PXE_IDS=$(efibootmgr | sed -n 's/^Boot\([0-9A-Fa-f]\{4\}\)\*\{0,1\}[[:space:]].*PXE.*/\1/p' | paste -sd, -)
  CUR_ORDER=$(efibootmgr | sed -n 's/^BootOrder:[[:space:]]*//p')
  if [ -n "$PXE_IDS" ] && [ -n "$CUR_ORDER" ]; then
    REST=$(echo "$CUR_ORDER" | tr ',' '\n' | grep -v -F -x -f <(echo "$PXE_IDS" | tr ',' '\n') | paste -sd, -)
    NEW_ORDER="${PXE_IDS}${REST:+,$REST}"
    if efibootmgr -o "$NEW_ORDER" >/dev/null 2>&1; then
      log "BootOrder set network-first: $NEW_ORDER (was: $CUR_ORDER)"
    else
      log "WARN: efibootmgr -o $NEW_ORDER failed — BootOrder unchanged ($CUR_ORDER)"
    fi
  else
    log "WARN: no PXE entries or no BootOrder visible; leaving boot order as-is"
  fi
else
  log "WARN: efibootmgr not available; leaving boot order as-is"
fi

log "Unmounting root dataset and restoring mountpoint=/"
zfs umount "$ROOT_DS"
zfs set mountpoint=/ "$ROOT_DS"

log "Exporting ZFS pool"
zpool export "$(echo "$ROOT_DS" | cut -d'/' -f1)"

### --- STEP 7: reboot into Proxmox ---------------------------------

log "All done. Rebooting into installed Proxmox VE..."
reboot

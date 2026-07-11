#!/usr/bin/env bash
set -euo pipefail

log() { echo "[$(date +'%F %T')] $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

# Non-fatal failures accumulate here; the script still finishes every step
# and reboots, but exits non-zero so the unit shows failed (node_health warns).
FIRST_BOOT_FAILED=0

# Restrict SSH
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl restart sshd

# Root SSH client config for cluster network
mkdir -p /root/.ssh
SSH_CONFIG="/root/.ssh/config"
if ! grep -q 'IdentityFile ~/.ssh/neuravps_id' "$SSH_CONFIG" 2>/dev/null; then
  cat >> "$SSH_CONFIG" <<'EOF'

# NeuraVPS private network (Hetzner VLAN / IPv6)
Host *
    IdentityFile ~/.ssh/neuravps_id
    IdentitiesOnly yes
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
EOF
  chmod 600 "$SSH_CONFIG"
  log "Added neuravps_id SSH config"
fi

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

# ntfs-3g (userspace NTFS via FUSE). The preserveNeuraData reinstall merge writes
# C:\NeuraData host-side and MUST use ntfs-3g, never the in-kernel ntfs3 driver,
# which kernel-BUGs on buffered writes (fs/iomap/buffered-io.c) on current kernels
# and crashes the merge mid-copy. Required on every node.
apt-get install -y ntfs-3g

# Install firebase_admin into system site-packages. --ignore-installed prevents pip from
# trying to uninstall the apt-provided requests (no RECORD file -> "uninstall-no-record-file"
# error that previously failed proxmox-first-boot); pip installs its own copies instead.
pip3 install firebase_admin --break-system-packages --root-user-action=ignore --ignore-installed

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
# bind-dynamic (NOT bind-interfaces): bind-interfaces breaks RS/NS multicast on
# Proxmox where fwbr<vmid> bridges come and go.
bind-dynamic
dhcp-authoritative
dhcp-rapid-commit

# IPv4 DHCP pool for guests.
dhcp-range=10.0.0.100,10.0.255.254,255.255.0.0,12h
dhcp-option=3,10.0.0.1
dhcp-option=6,185.12.64.1,185.12.64.2

# IPv6: NOTHING. No RA, no DHCPv6. Each VM is configured in-guest with a
# static <prefix>::<vmid_hex> + manual default route + manual DNS via the
# legacy netsh "store=persistent" path (run_remotes/migrate_to_deterministic_ipv6.sh).
# Both DHCPv6 client and RouterDiscovery are persistently DISABLED on the
# adapter, so Windows has zero auto-config to compete with the manual IPv6.
EOF

# Clean up leftovers from previous models (stateful DHCPv6 pin files).
rm -f /etc/dnsmasq-vm-pins.hosts /etc/dnsmasq.d/vm-pins.conf

systemctl restart dnsmasq

############################################
# Raw swap: one partition per rpool disk, filling the free space install.sh
# reserved via zfs.hdsize (SWAP_GB_PER_DISK). Whatever was reserved becomes swap
# (legacy ~23 GB, big-RAM boxes 256 GB), so the two scripts stay in sync with no
# value to pass between them and old nodes are unaffected.
############################################
log "Setting up raw swap partitions (filling reserved free space) on rpool disks"
if command -v zpool >/dev/null 2>&1 && zpool list -H -o name rpool >/dev/null 2>&1; then
  apt-get install -y gdisk parted
  # Get rpool vdev block devices. Pool may show /dev/ paths, by-id paths, or short names (sda1, vda1).
  vdevs=()
  # 1) Full paths from zpool status -P (and -L to resolve by-id to /dev/sdX or similar)
  while IFS= read -r line; do
    [[ -n "$line" ]] && vdevs+=("$line")
  done < <(zpool status -P -L rpool 2>/dev/null | grep -oE '/dev/[^[:space:]]+' || true)
  # 2) If no /dev/ paths, try short names from zpool status (sda1, vda1, nvme0n1p1)
  if [[ ${#vdevs[@]} -eq 0 ]]; then
    while IFS= read -r line; do
      [[ -n "$line" ]] && vdevs+=("/dev/$line")
    done < <(zpool status rpool 2>/dev/null | grep -oE '\b(sd[a-z][0-9]+|vd[a-z][0-9]+|nvme[0-9]+n[0-9]+p[0-9]+)\b' | sort -u || true)
  fi
  # Resolve symlinks (e.g. /dev/disk/by-id/...-part1 -> /dev/sda1) so parent derivation works
  for i in "${!vdevs[@]}"; do
    dev="${vdevs[i]}"
    [[ -b "$dev" ]] || continue
    canon=$(readlink -f "$dev" 2>/dev/null)
    [[ -n "$canon" ]] && [[ -b "$canon" ]] && vdevs[i]="$canon"
  done
  log "rpool vdevs found: ${vdevs[*]:-(none)}"
  # Dedupe parent disks (sda1 and sdb1 -> /dev/sda, /dev/sdb)
  declare -A parent_disks
  for dev in "${vdevs[@]}"; do
    [[ -b "$dev" ]] || continue
    # Parent disk: /dev/nvme0n1p1 -> /dev/nvme0n1 (strip pN); /dev/sda1 -> /dev/sda (strip trailing digits)
    if [[ "$dev" =~ /nvme[0-9]+n[0-9]+p[0-9]+ ]]; then
      parent="${dev%p*}"
    else
      parent="${dev%%[0-9]*}"
    fi
    [[ -b "$parent" ]] && parent_disks["$parent"]=1
  done
  # Build list without ${!array[*]} (that can be parsed as indirect expansion "1 1" -> invalid variable name)
  parent_list=""
  for d in "${!parent_disks[@]}"; do
    [[ -n "$parent_list" ]] && parent_list+=" "
    parent_list+="$d"
  done
  log "parent disks for swap: ${parent_list:-(none)}"
  for disk in "${!parent_disks[@]}"; do
    disk_base=$(basename "$disk")
    # Find next partition number (installer may use p1/p2/p3 for EFI+boot+ZFS, so we need p4 for swap)
    next_num=1
    if [[ "$disk_base" == nvme* ]]; then
      for p in /sys/block/"$disk_base"/"$disk_base"p*; do
        [[ -e "$p" ]] || continue
        n="${p##*p}"
        n="${n//[^0-9]/}"
        if [[ -n "$n" ]]; then
          num=$((10#"$n"))
          [[ "$num" -ge "$next_num" ]] && next_num=$((num + 1))
        fi
      done
      part_swap="${disk}p${next_num}"
    else
      for p in /sys/block/"$disk_base"/"$disk_base"[0-9]*; do
        [[ -e "$p" ]] || continue
        n="${p##*"$disk_base"}"
        n="${n//[^0-9]/}"
        if [[ -n "$n" ]]; then
          num=$((10#"$n"))
          [[ "$num" -ge "$next_num" ]] && next_num=$((num + 1))
        fi
      done
      part_swap="${disk}${next_num}"
    fi
    if [[ -b "$part_swap" ]]; then
      # Partition already exists; ensure swap is on and in fstab
      if blkid -o value -s TYPE "$part_swap" 2>/dev/null | grep -qx swap; then
        uuid=$(blkid -o value -s UUID "$part_swap" 2>/dev/null)
        if [[ -n "$uuid" ]] && ! grep -q "UUID=$uuid" /etc/fstab 2>/dev/null; then
          echo "UUID=$uuid none swap sw 0 0" >> /etc/fstab
          log "Added existing swap $part_swap (UUID=$uuid) to fstab"
        fi
        swapon "$part_swap" 2>/dev/null || true
      fi
      continue
    fi
    # Create next partition for swap (installer left free space via zfs.hdsize)
    next_num=$((next_num + 0))
    log "Creating swap partition ${next_num} (filling reserved free space) on $disk"
    sgdisk -n "${next_num}:0:0" -t "${next_num}:8200" "$disk" || { log "WARNING: sgdisk failed on $disk"; continue; }
    partprobe "$disk" 2>/dev/null || true
    udevadm settle -t 5 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      [[ -b "$part_swap" ]] && break
      sleep 1
      partprobe "$disk" 2>/dev/null || true
      udevadm settle -t 2 2>/dev/null || true
    done
    [[ -b "$part_swap" ]] || { log "WARNING: Partition $part_swap not found after sgdisk"; continue; }
    mkswap -f "$part_swap" || { log "WARNING: mkswap failed on $part_swap"; continue; }
    uuid=$(blkid -o value -s UUID "$part_swap" 2>/dev/null)
    if [[ -z "$uuid" ]]; then
      log "WARNING: Could not get UUID for $part_swap"
      echo "$part_swap none swap sw 0 0" >> /etc/fstab
    else
      grep -q "UUID=$uuid" /etc/fstab 2>/dev/null || echo "UUID=$uuid none swap sw 0 0" >> /etc/fstab
    fi
    swapon "$part_swap" || true
    log "Swap enabled on $part_swap (UUID=${uuid:-N/A})"
  done
  if [[ ${#parent_disks[@]} -eq 0 ]]; then
    log "No rpool vdevs found (whole-disk rpool?), skipping raw swap setup"
  else
    log "Raw swap setup finished for ${#parent_disks[@]} disk(s)"
  fi
else
  log "rpool not found or zpool not available, skipping raw swap setup"
fi

############################################
# Spare-pair data mirror (fleet refresh 2026-07: AX162/384 = 2x1.92TB rpool
# + 2x960GB extension pair)
############################################
# When the box ships with TWO equal-size empty spare disks beyond the rpool
# pair, they are a CAPACITY EXPANSION, not swap: add them to rpool as a second
# mirror vdev (mixed vdev sizes are fine in ZFS — allocation spreads
# proportionally across vdevs). Swap already lives on the rpool-disk carve
# (SWAP_GB_PER_DISK, auto-detected in install.sh). This runs BEFORE the
# third-disk-swap check below so a 960 pair is never half-grabbed as a
# full-disk swap. Idempotent: after `zpool add` the disks carry ZFS labels and
# no longer count as empty. A SINGLE spare disk still becomes the legacy
# dedicated swap disk (next section). After extending a node, re-run the
# max_disk backfill so its placement budget reflects the bigger pool.
log "Checking for an equal-size empty spare disk PAIR to extend rpool"
if command -v zpool >/dev/null 2>&1 && zpool list -H -o name rpool >/dev/null 2>&1; then
  pair_rpool_parents=()
  pair_vdevs=()
  while IFS= read -r line; do
    [[ -n "$line" ]] && pair_vdevs+=("$line")
  done < <(zpool status -P -L rpool 2>/dev/null | grep -oE '/dev/[^[:space:]]+' || true)
  for dev in "${pair_vdevs[@]}"; do
    [[ -b "$dev" ]] || continue
    canon=$(readlink -f "$dev" 2>/dev/null)
    [[ -n "$canon" ]] && [[ -b "$canon" ]] && dev="$canon"
    if [[ "$dev" =~ /nvme[0-9]+n[0-9]+p[0-9]+ ]]; then
      parent="${dev%p*}"
    else
      parent="${dev%%[0-9]*}"
    fi
    [[ -b "$parent" ]] && pair_rpool_parents+=("$parent")
  done
  pair_spares=()
  while IFS= read -r d; do
    [[ -b "$d" ]] || continue
    skip=
    for p in "${pair_rpool_parents[@]}"; do
      [[ "$d" == "$p" ]] && skip=1 && break
    done
    [[ -n "$skip" ]] && continue
    # Only COMPLETELY empty disks qualify for the pair (a disk with any
    # partition — swap, zfs_member, anything — is not ours to take).
    part_count=0
    if [[ "$(basename "$d")" == nvme* ]]; then
      for part in "${d}"p*; do [[ -b "$part" ]] && part_count=$((part_count + 1)); done
    else
      for part in "${d}"[0-9]*; do [[ -b "$part" ]] && part_count=$((part_count + 1)); done
    fi
    [[ "$part_count" -eq 0 ]] && pair_spares+=("$d")
  done < <(lsblk -dpno NAME,TYPE 2>/dev/null | awk '$2=="disk"{print $1}')
  if [[ ${#pair_spares[@]} -eq 2 ]]; then
    s0=$(blockdev --getsize64 "${pair_spares[0]}" 2>/dev/null || true)
    s1=$(blockdev --getsize64 "${pair_spares[1]}" 2>/dev/null || true)
    if [[ -n "$s0" && "$s0" == "$s1" ]]; then
      log "Adding spare pair as second mirror vdev: ${pair_spares[*]} ($((s0 / 1024 / 1024 / 1024)) GiB each)"
      if zpool add rpool mirror "${pair_spares[0]}" "${pair_spares[1]}"; then
        log "rpool extended; new size: $(zpool list -H -o size rpool 2>/dev/null)"
      else
        log "WARNING: zpool add failed for ${pair_spares[*]} — disks left untouched"
      fi
    else
      log "Two spare disks with different sizes (${s0:-?} vs ${s1:-?}) — leaving them to the third-disk-swap logic below"
    fi
  elif [[ ${#pair_spares[@]} -gt 2 ]]; then
    log "WARNING: ${#pair_spares[@]} empty spare disks found — ambiguous layout, leaving them to the third-disk-swap logic below"
  else
    log "No empty spare disk pair (${#pair_spares[@]} spare disk[s]) — nothing to extend"
  fi
else
  log "rpool not found, skipping spare-pair check"
fi

############################################
# Third-disk swap (when 3 disks: third is dedicated NVMe for swap)
############################################
log "Checking for third empty disk to use as full-disk swap"
if command -v zpool >/dev/null 2>&1 && zpool list -H -o name rpool >/dev/null 2>&1; then
  # Build set of rpool parent disks (same logic as above)
  rpool_parents=()
  vdevs=()
  while IFS= read -r line; do
    [[ -n "$line" ]] && vdevs+=("$line")
  done < <(zpool status -P -L rpool 2>/dev/null | grep -oE '/dev/[^[:space:]]+' || true)
  if [[ ${#vdevs[@]} -eq 0 ]]; then
    while IFS= read -r line; do
      [[ -n "$line" ]] && vdevs+=("/dev/$line")
    done < <(zpool status rpool 2>/dev/null | grep -oE '\b(sd[a-z][0-9]+|vd[a-z][0-9]+|nvme[0-9]+n[0-9]+p[0-9]+)\b' | sort -u || true)
  fi
  for dev in "${vdevs[@]}"; do
    [[ -b "$dev" ]] || continue
    canon=$(readlink -f "$dev" 2>/dev/null)
    [[ -n "$canon" ]] && [[ -b "$canon" ]] && dev="$canon"
    if [[ "$dev" =~ /nvme[0-9]+n[0-9]+p[0-9]+ ]]; then
      parent="${dev%p*}"
    else
      parent="${dev%%[0-9]*}"
    fi
    [[ -b "$parent" ]] && rpool_parents+=("$parent")
  done
  # All block devices that are disks (exclude loop, etc.)
  while IFS= read -r d; do
    [[ -b "$d" ]] || continue
    # Skip if this disk is an rpool parent
    skip=
    for p in "${rpool_parents[@]}"; do
      [[ "$d" == "$p" ]] && skip=1 && break
    done
    [[ -n "$skip" ]] && continue
    # Check if disk is empty: no partition table, or single partition that is swap (idempotent)
    part_count=0
    has_swap_part=
    has_other_part=
    if [[ "$(basename "$d")" == nvme* ]]; then
      for part in "${d}"p*; do
        [[ -b "$part" ]] || continue
        part_count=$((part_count + 1))
        fstype=$(blkid -o value -s TYPE "$part" 2>/dev/null || true)
        if [[ "$fstype" == "swap" ]]; then
          has_swap_part=$part
        else
          has_other_part=1
        fi
      done
    else
      for part in "${d}"[0-9]*; do
        [[ -b "$part" ]] || continue
        part_count=$((part_count + 1))
        fstype=$(blkid -o value -s TYPE "$part" 2>/dev/null || true)
        if [[ "$fstype" == "swap" ]]; then
          has_swap_part=$part
        else
          has_other_part=1
        fi
      done
    fi
    if [[ -n "$has_other_part" ]]; then
      continue
    fi
    if [[ -n "$has_swap_part" ]]; then
      uuid=$(blkid -o value -s UUID "$has_swap_part" 2>/dev/null)
      if [[ -n "$uuid" ]] && ! grep -q "UUID=$uuid" /etc/fstab 2>/dev/null; then
        echo "UUID=$uuid none swap sw 0 0" >> /etc/fstab
        log "Added existing third-disk swap $has_swap_part (UUID=$uuid) to fstab"
      fi
      swapon "$has_swap_part" 2>/dev/null || true
      log "Third-disk swap already present on $d, enabled"
      break
    fi
    if [[ "$part_count" -eq 0 ]]; then
      apt-get install -y gdisk parted
      log "Using empty third disk $d for full-disk swap"
      sgdisk -n "1:0:0" -t "1:8200" "$d" || { log "WARNING: sgdisk failed on $d"; continue; }
      partprobe "$d" 2>/dev/null || true
      udevadm settle -t 5 2>/dev/null || true
      if [[ "$(basename "$d")" == nvme* ]]; then
        part_swap="${d}p1"
      else
        part_swap="${d}1"
      fi
      for _ in 1 2 3 4 5 6 7 8 9 10; do
        [[ -b "$part_swap" ]] && break
        sleep 1
        partprobe "$d" 2>/dev/null || true
        udevadm settle -t 2 2>/dev/null || true
      done
      [[ -b "$part_swap" ]] || { log "WARNING: Partition $part_swap not found after sgdisk"; continue; }
      mkswap -f "$part_swap" || { log "WARNING: mkswap failed on $part_swap"; continue; }
      uuid=$(blkid -o value -s UUID "$part_swap" 2>/dev/null)
      if [[ -z "$uuid" ]]; then
        echo "$part_swap none swap sw 0 0" >> /etc/fstab
      else
        grep -q "UUID=$uuid" /etc/fstab 2>/dev/null || echo "UUID=$uuid none swap sw 0 0" >> /etc/fstab
      fi
      swapon "$part_swap" || true
      log "Third-disk swap enabled on $part_swap (UUID=${uuid:-N/A})"
      break
    fi
  done < <(lsblk -dpno NAME,TYPE 2>/dev/null | awk '$2=="disk"{print $1}')
else
  log "rpool not found, skipping third-disk swap check"
fi

# Memory profile per node class (see NeuraVPS/NeuraVPS docs/NEURAVPS_FLEET_TUNING_PLAN.md).
# A fixed 32G ARC used to be set on every class; on 64G EX44 nodes (one dedicated
# 61G VPS-E VM) that evicted customer VM RAM into host swap fleet-wide. Same
# table as run_remotes/tune_memory_profile.sh — keep both in sync.
HOSTCLASS="$(hostname)"
# Profile e also lowers zfs_arc_min: the default is RAM/32 (= 2 GiB on a 64 GB
# EX44) and ZFS silently CLAMPS c_max to arc_min — a 1 GiB cap written alone
# stays at 2 GiB with no error. Write min before max, persist both.
# (2026-06-12 retune: 1G ARC + memory=61440 VPS-E VM → cold-swap floor 0.5 GB.)
# v2 (2026-07-02): RAM-pressure retune for the overcommit era. swappiness=100
# on multi-VM classes is paired with zswap (below): cold guest pages compress
# into the in-RAM pool early instead of burst-swapping at allocation spikes.
# page-cluster=0 kills swap readahead (pointless for zswap/NVMe). New r470
# class (172t/503G deep-overcommit): KSM ON — same identical-Windows dedup as
# ad, measured ~80 GB/node on AX162.
case "$HOSTCLASS" in
  *-EX44*)    MEM_PROFILE="e";    ARC_BYTES=$((1024 * 1024 * 1024)); ARC_MIN_BYTES=$((512 * 1024 * 1024)); SWAPPINESS=1; KSM_ON=0 ;;
  *-AX102-U*) MEM_PROFILE="mt";   ARC_BYTES=$((8  * 1024 * 1024 * 1024)); ARC_MIN_BYTES=0; SWAPPINESS=100; KSM_ON=1 ;;
  *-AX102*)   MEM_PROFILE="mt";   ARC_BYTES=$((8  * 1024 * 1024 * 1024)); ARC_MIN_BYTES=0; SWAPPINESS=100; KSM_ON=1 ;;
  *-AX162*)   MEM_PROFILE="ad";   ARC_BYTES=$((16 * 1024 * 1024 * 1024)); ARC_MIN_BYTES=0; SWAPPINESS=100; KSM_ON=1 ;;
  *-R470*)    MEM_PROFILE="r470"; ARC_BYTES=$((16 * 1024 * 1024 * 1024)); ARC_MIN_BYTES=0; SWAPPINESS=100; KSM_ON=1 ;;
  *)          MEM_PROFILE="vps";  ARC_BYTES=$((16 * 1024 * 1024 * 1024)); ARC_MIN_BYTES=0; SWAPPINESS=5;   KSM_ON=0 ;;
esac
log "Memory profile: $MEM_PROFILE (host=$HOSTCLASS) — zfs_arc_max=$ARC_BYTES, arc_min=$ARC_MIN_BYTES, swappiness=$SWAPPINESS, ksm=$KSM_ON, zswap=on"

mkdir -p /etc/sysctl.d
printf 'vm.swappiness=%s\nvm.page-cluster=0\n' "$SWAPPINESS" > /etc/sysctl.d/99-neuravps-memprofile.conf
rm -f /etc/sysctl.d/99-proxmox-swap.conf
sysctl -p /etc/sysctl.d/99-neuravps-memprofile.conf 2>/dev/null || true

# Kernel self-recovery from a hard freeze / panic. Silent-freeze mitigation
# (incident 2026-07-08, node 0000008: a hard lockup with no trace; the fleet
# default kernel.panic=0 = "halt forever", so it took a ~23-min watchdog/manual
# reset instead of self-recovering). With these:
#   panic=10          -> reboot 10s after any panic
#   hardlockup_panic  -> a hard lockup (NMI watchdog) BECOMES a panic -> reboot
#   panic_on_oops     -> an oops BECOMES a panic -> reboot
# Combined with crashkernel/kdump (below) the reboot also leaves a vmcore, so the
# NEXT freeze is finally root-causeable. nmi_watchdog is on by default.
# NOTE: softlockup_panic is intentionally NOT set (transient under heavy IO).
cat > /etc/sysctl.d/99-neuravps-stability.conf <<'SYSCTL'
kernel.panic = 10
kernel.hardlockup_panic = 1
kernel.panic_on_oops = 1
SYSCTL
sysctl -p /etc/sysctl.d/99-neuravps-stability.conf 2>/dev/null || true

# zswap (ALL classes): compressed in-RAM swap cache in front of the swap
# partitions. Built into the kernel (no modprobe.d), so a oneshot unit
# re-applies the params each boot. Keep in sync with
# run_remotes/tune_memory_profile.sh.
log "Enabling zswap (zstd->lz4->lzo, zsmalloc, max_pool_percent=20)"
cat > /usr/local/sbin/neuravps-zswap.sh <<'ZSWAPEOF'
#!/bin/bash
# NeuraVPS: enable zswap (compressed in-RAM swap cache). Run at boot by
# neuravps-zswap.service; safe to re-run any time. See
# run_remotes/tune_memory_profile.sh (v2) for the why.
P=/sys/module/zswap/parameters
[ -d "$P" ] || exit 0
modprobe zsmalloc 2>/dev/null || true
for c in zstd lz4 lzo; do
  modprobe "$c" 2>/dev/null || true
  echo "$c" > "$P/compressor" 2>/dev/null && break
done
echo zsmalloc > "$P/zpool" 2>/dev/null || echo zbud > "$P/zpool" 2>/dev/null || true
echo 20 > "$P/max_pool_percent" 2>/dev/null
echo Y > "$P/enabled"
ZSWAPEOF
chmod +x /usr/local/sbin/neuravps-zswap.sh
cat > /etc/systemd/system/neuravps-zswap.service <<'UNITEOF'
[Unit]
Description=NeuraVPS: enable zswap (compressed swap cache)

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/neuravps-zswap.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNITEOF
systemctl daemon-reload
systemctl enable --now neuravps-zswap.service || log "WARNING: neuravps-zswap enable failed"

if [ "$ARC_MIN_BYTES" -gt 0 ]; then
  echo "$ARC_MIN_BYTES" > /sys/module/zfs/parameters/zfs_arc_min
  echo "$ARC_BYTES" > /sys/module/zfs/parameters/zfs_arc_max
  echo "options zfs zfs_arc_max=$ARC_BYTES zfs_arc_min=$ARC_MIN_BYTES" > /etc/modprobe.d/zfs.conf
else
  echo "$ARC_BYTES" > /sys/module/zfs/parameters/zfs_arc_max
  echo "options zfs zfs_arc_max=$ARC_BYTES" > /etc/modprobe.d/zfs.conf
fi
update-initramfs -u

if [ "$KSM_ON" = "1" ]; then
  log "$MEM_PROFILE profile: enabling ksmtuned (KSM dedup across identical Windows VMs)"
  # Default KSM_THRES_COEF=20 deadlocks on swapped many-identical-VM nodes (mt
  # and ad): VM RAM exiled to swap lowers qemu RSS, ksmtuned concludes "ksm not
  # needed" (run=0), nothing dedups, swap never recovers (found live on all 9
  # AX102-U 2026-06-12; AX162 pilot confirmed the same 2026-06-13).
  # 85 = effectively always-on at MT density; NPAGES_MAX 2500 = 2x scan rate.
  [ -x /usr/sbin/ksmtuned ] || DEBIAN_FRONTEND=noninteractive apt-get install -y ksm-control-daemon || log "WARNING: ksm-control-daemon install failed"
  sed -i "/^#* *KSM_THRES_COEF=/d; /^#* *KSM_NPAGES_MAX=/d" /etc/ksmtuned.conf
  printf "KSM_THRES_COEF=85\nKSM_NPAGES_MAX=2500\n" >> /etc/ksmtuned.conf
  systemctl enable --now ksmtuned || log "WARNING: ksmtuned enable failed"
  systemctl restart ksmtuned 2>/dev/null || true
else
  # KSM off for this class (e = dedicated VPS-E, vps = unknown/catch-all).
  # Actively DISABLE ksmtuned: it ships enabled and would auto-toggle KSM under
  # memory pressure, burning the customer's dedicated cores on pointless single-
  # VM dedup (found 49/124 EX44 running KSM 2026-06-13). run=0 stops scanning
  # without unmerging (no memory spike); residual merges COW-split naturally.
  log "$MEM_PROFILE profile: disabling ksmtuned (KSM off — dedicated/unknown class)"
  systemctl disable --now ksmtuned 2>/dev/null || true
  echo 0 > /sys/kernel/mm/ksm/run 2>/dev/null || true
fi

# Disable integrated GPUs (Intel + AMD) on headless Proxmox to avoid kernel issues
# Intel: i915 (older), xe (Alder Lake+). AMD: amdgpu
log "Disabling integrated GPUs (blacklist xe, i915, amdgpu)"
cat > /etc/modprobe.d/blacklist-headless-gpu.conf <<'EOF'
# Avoid kernel panics / issues with integrated GPUs on headless Proxmox
blacklist xe
blacklist i915
blacklist amdgpu
EOF

log "Configuring GRUB to disable integrated GPUs (nomodeset + blacklist)"
GRUB_DEFAULT="/etc/default/grub"
if [ -f "$GRUB_DEFAULT" ]; then
  if grep -q "^GRUB_CMDLINE_LINUX=" "$GRUB_DEFAULT"; then
    CURRENT_CMDLINE=$(grep "^GRUB_CMDLINE_LINUX=" "$GRUB_DEFAULT" | sed 's/^GRUB_CMDLINE_LINUX="\(.*\)"/\1/')
    CURRENT_CMDLINE=$(echo "$CURRENT_CMDLINE" | sed 's/i915\.modeset=0//g')
    [[ "$CURRENT_CMDLINE" != *"nomodeset"* ]] && CURRENT_CMDLINE="${CURRENT_CMDLINE} nomodeset"
    [[ "$CURRENT_CMDLINE" != *"i915.enable_guc=0"* ]] && CURRENT_CMDLINE="${CURRENT_CMDLINE} i915.enable_guc=0"
    # Prevent Intel (xe, i915) and AMD (amdgpu) integrated GPU drivers from loading
    [[ "$CURRENT_CMDLINE" != *"modprobe.blacklist=xe"* ]] && CURRENT_CMDLINE="${CURRENT_CMDLINE} modprobe.blacklist=xe,i915,amdgpu"
    sed -i "s|^GRUB_CMDLINE_LINUX=.*|GRUB_CMDLINE_LINUX=\"${CURRENT_CMDLINE}\"|" "$GRUB_DEFAULT"
  else
    echo 'GRUB_CMDLINE_LINUX="nomodeset i915.enable_guc=0 modprobe.blacklist=xe,i915,amdgpu"' >> "$GRUB_DEFAULT"
  fi
  log "Updated GRUB_CMDLINE_LINUX in $GRUB_DEFAULT"
  update-grub
  log "GRUB updated with integrated GPUs disabled (nomodeset + blacklist xe,i915,amdgpu)"
else
  log "WARNING: $GRUB_DEFAULT not found, skipping GRUB configuration"
fi

# ---- Effective kernel cmdline: systemd-boot / proxmox-boot-tool ----
# These ZFS-root UEFI nodes boot via proxmox-boot-tool (systemd-boot), NOT GRUB:
# the GRUB_CMDLINE_LINUX above does NOT reach /proc/cmdline (verified 2026-07-09;
# the GPU blacklist still applies via /etc/modprobe.d). Kernel params that must
# actually take effect therefore go in /etc/kernel/cmdline + a refresh:
#   processor.max_cstate=1  C-STATE MITIGATION for the silent AX162/EPYC freeze —
#                           caps idle at C1 (no deep CC6). VALIDATED on canary
#                           0000032 (2026-07-09): removed the C2 idle state.
#                           AMD hosts only. Small idle-power cost; drop if the
#                           freeze ever proves unrelated to C-states.
#
# NOTE: kdump/crashkernel deliberately NOT added. Canary 2026-07-09 (0000032):
# with kdump armed, a panic kexecs into a crash kernel that HANGS on this AX162
# hardware — captures nothing (/var/crash empty) AND leaves the node dead (kexec
# bypasses panic=10), needing a manual Hetzner hw-reset. Worse than no kdump. The
# clean auto-reboot (panic=10, below) is what we rely on. See
# docs/INCIDENT_2026-07-08_node0000008_freeze.md for the full canary write-up +
# untested capture follow-ups (efi_pstore / netconsole).
KCMDLINE=/etc/kernel/cmdline
if [ -f "$KCMDLINE" ] && command -v proxmox-boot-tool >/dev/null 2>&1; then
  if grep -qi 'AuthenticAMD\|AMD EPYC' /proc/cpuinfo && ! grep -qw 'processor.max_cstate=1' "$KCMDLINE"; then
    CUR="$(tr -s ' ' < "$KCMDLINE" | sed 's/[[:space:]]*$//')"
    printf '%s processor.max_cstate=1\n' "$CUR" > "$KCMDLINE"
    proxmox-boot-tool refresh || log "WARNING: proxmox-boot-tool refresh failed"
    log "Kernel cmdline: added processor.max_cstate=1 (active after the first_boot reboot)"
  fi
else
  log "WARNING: /etc/kernel/cmdline or proxmox-boot-tool missing — processor.max_cstate NOT applied"
fi

# HARDWARE watchdog (AMD only): the SP5100/SB800 chipset TCO hardware-resets a
# fully-dead node in seconds — softdog needs a half-alive kernel and took 23 min
# on the 0000008 freeze. Validated 2026-07-10 (drill on 0000032: unrecoverable
# kernel -> HW reset -> back in <4 min) on both AX162/EPYC-Genoa and
# AX102/Ryzen-7950X3D; sp5100_tco binds on all our AMD hardware. watchdog-mux
# adopts it on the first_boot reboot below. Intel hosts stay on softdog.
if grep -qi 'AuthenticAMD\|AMD EPYC' /proc/cpuinfo; then
  PHAM=/etc/default/pve-ha-manager
  [ -f "$PHAM" ] || touch "$PHAM"
  if ! grep -q '^WATCHDOG_MODULE=sp5100_tco$' "$PHAM"; then
    { grep -vE '^WATCHDOG_MODULE=' "$PHAM"; echo 'WATCHDOG_MODULE=sp5100_tco'; } > "$PHAM.tco" && mv "$PHAM.tco" "$PHAM"
    log "Watchdog: set WATCHDOG_MODULE=sp5100_tco (HW watchdog; active after the first_boot reboot)"
  fi
fi

############## CLUSTER SPECIFIC CONFIGURATION ##############
# Proxmox firewall: datacenter baseline with IPv6 ipset gating
echo "==> Configuring Proxmox firewall (datacenter baseline)"
cat >/etc/pve/firewall/cluster.fw <<'EOF'
[OPTIONS]

policy_in: DROP
enable: 1
policy_out: ACCEPT
policy_forward: DROP

[IPSET base]

2a01:4f9:3090:2488::/64 # BASE 0 IPv6
2a01:4f9:3070:3984::/64 # BASE 1 IPv6

[IPSET hosts-ipv6]

2a01:4f9:6a:44eb::/64 # 0000002-AX162-R

[RULES]

IN PMG(ACCEPT) -source +dc/base -log nolog
IN PMG(ACCEPT) -source +dc/hosts-ipv6 -log nolog
IN SSH(ACCEPT) -source +dc/base -log nolog
IN SSH(ACCEPT) -source +dc/hosts-ipv6 -log nolog
IN DHCPfwd(ACCEPT) -i vmbr0 -log nolog

[group vm-default]

IN SSH(ACCEPT) -source +dc/base -log nolog
IN RDP(ACCEPT) -source +dc/base -log nolog
IN ACCEPT -source +dc/base -p udp -dport 3389 -log nolog
IN SMB(ACCEPT) -source +dc/base -log nolog
IN SMB(ACCEPT) -source +dc/hosts-ipv6 -log nolog

[group vm-no-internet]

IN SSH(ACCEPT) -source +dc/base -log nolog
IN RDP(ACCEPT) -source +dc/base -log nolog
IN ACCEPT -source +dc/base -p udp -dport 3389 -log nolog
IN SMB(ACCEPT) -source +dc/base -log nolog
IN SMB(ACCEPT) -source +dc/hosts-ipv6 -log nolog
IN DROP -log nolog
OUT DROP -log nolog

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

chmod +x /var/lib/svz/snippets/sync-dnat.py

# Systemd oneshot: at every boot, update lastNodeBootAt and sync VM statuses (all stopped after reboot)
log "Installing node-boot-sync-dnat.service (runs sync-dnat.py node-boot after network is up)"
cat > /etc/systemd/system/node-boot-sync-dnat.service <<'NODEBOOTEOF'
[Unit]
Description=Update lastNodeBootAt and sync VM status at boot
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/var/lib/svz/snippets/sync-dnat.py node-boot
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
NODEBOOTEOF
systemctl daemon-reload
systemctl enable node-boot-sync-dnat.service

# Pull node credentials + canonical firewall from the Storage Box.
# Tolerant on purpose: under set -e a failure here used to abort the whole
# script, silently skipping everything below (smartd wear filter, PXE-first
# BootOrder, final reboot) and leaving the pending-first-boot marker armed —
# 14 half-configured nodes on 2026-07-04 when the fleet key was never seeded
# in rescue. Retry, then carry the failure to the exit code instead of dying.
pull_from_storagebox() {
  sftp -i /root/.ssh/neuravps_id -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -oBatchMode=yes -P 23 u560363@u560363.your-storagebox.de <<EOF
get /home/id/firebase-credentials.json /etc/firebase-credentials.json
get /home/firewall/cluster.fw /etc/pve/firewall/cluster.fw
bye
EOF
}
if [ ! -s /root/.ssh/neuravps_id ]; then
  log "WARNING: /root/.ssh/neuravps_id missing (install.sh copies it from rescue) — cannot pull firebase-credentials.json/cluster.fw"
  FIRST_BOOT_FAILED=1
else
  STORAGEBOX_PULL_OK=0
  for _sb_attempt in 1 2 3; do
    if pull_from_storagebox; then STORAGEBOX_PULL_OK=1; break; fi
    log "WARNING: storagebox pull attempt ${_sb_attempt}/3 failed; retrying in 10s"
    sleep 10
  done
  if [ "$STORAGEBOX_PULL_OK" -eq 1 ] && [ -s /etc/firebase-credentials.json ]; then
    chmod 600 /etc/firebase-credentials.json
  else
    log "WARNING: storagebox pull failed after 3 attempts — node is missing firebase-credentials.json (sync-dnat.py Firestore updates) and the canonical cluster.fw (BASE push also delivers it)"
    FIRST_BOOT_FAILED=1
  fi
fi

############################################
# netconsole: stream this node's kernel console (the panic run-up) to the region
# BASE, so a silent AX162/EPYC freeze can be diagnosed instead of leaving us
# blind again (see docs/INCIDENT_2026-07-08_node0000008_freeze.md). Same helper
# (v3, IPv6 over configfs) + unit as run_remotes/apply_freeze_mitigations.sh, so
# a freshly reinstalled node matches a live-patched one. Region BASE = the node's
# Firestore `location` (authoritative), falling back to the nearest base by RTT
# when creds/Firestore are unavailable, then to HEL. errexit-safe on purpose.
# netconsole streams over IPv6; the base only accepts our nodes' /64s.
############################################
log "Configuring netconsole (kernel console -> region BASE over IPv6)"
NC_HEL=2a01:4f9:3070:3984::2   # b1 (Helsinki) IPv6
NC_FSN=2a01:4f8:2b03:18a9::2   # b0 (Falkenstein) IPv6
NC_BASE=""
NC_LOC=""
if [ -s /etc/firebase-credentials.json ]; then
  NC_LOC="$(python3 - <<'PY' 2>/dev/null || true
import subprocess
try:
    import firebase_admin
    from firebase_admin import credentials, firestore
    host = subprocess.run(["hostname"], capture_output=True, text=True).stdout.strip()
    firebase_admin.initialize_app(credentials.Certificate("/etc/firebase-credentials.json"))
    d = firestore.client().collection("proxmox_nodes").document(host).get()
    print(((d.to_dict() or {}).get("location") or "").strip())
except Exception:
    pass
PY
)"
fi
case "$NC_LOC" in
  Helsinki)    NC_BASE=$NC_HEL; log "netconsole: location=Helsinki -> $NC_BASE" ;;
  Falkenstein) NC_BASE=$NC_FSN; log "netconsole: location=Falkenstein -> $NC_BASE" ;;
  ?*)          log "netconsole: unrecognized location '$NC_LOC'; probing by RTT" ;;
esac
if [ -z "$NC_BASE" ]; then
  NC_BEST=""; NC_BESTMS=100000
  for ip in "$NC_HEL" "$NC_FSN"; do
    ms="$(ping6 -c2 -w3 "$ip" 2>/dev/null | sed -n 's#.*= [0-9.]*/\([0-9.]*\)/.*#\1#p' || true)"
    [ -n "$ms" ] || continue
    msi="$(awk -v m="$ms" 'BEGIN{printf "%d", m*100}')"
    if [ "$msi" -lt "$NC_BESTMS" ]; then NC_BESTMS=$msi; NC_BEST=$ip; fi
  done
  [ -n "$NC_BEST" ] && { NC_BASE=$NC_BEST; log "netconsole: nearest base by RTT -> $NC_BASE"; }
fi
[ -n "$NC_BASE" ] || { NC_BASE=$NC_HEL; log "netconsole: defaulting to HEL base $NC_BASE"; }
printf 'BASE_IP=%s\nPORT=6666\n' "$NC_BASE" > /etc/neuravps-netconsole.conf
cat > /usr/local/sbin/neuravps-netconsole.sh <<'NCH'
#!/bin/bash
#NCVER=5
. /etc/neuravps-netconsole.conf 2>/dev/null
[ -n "$BASE_IP" ] || exit 0
PORT=${PORT:-6666}
# BASE_IP is the region base's IPv6. Resolve v6 src/dev/gateway + the gateway
# MAC (NDP), retrying while routes/neighbours settle at boot. Generous loop
# (network-online.target can fire before IPv6/NDP is ready); the unit also
# restarts on failure as a backstop.
MAC=""; SRC=""; DEV=""; GW=""
for _try in $(seq 1 60); do
  read -r SRC DEV GW < <(ip -6 route get "$BASE_IP" 2>/dev/null | awk '{for(i=1;i<=NF;i++){if($i=="src")s=$(i+1); if($i=="dev")d=$(i+1); if($i=="via")g=$(i+1)}} END{print s, d, g}')
  [ -n "$GW" ] || GW="$BASE_IP"
  ping6 -c1 -W1 "$GW" >/dev/null 2>&1
  MAC=$(ip -6 neigh show "$GW" dev "$DEV" 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="lladdr"){print $(i+1); exit}}')
  [ -n "$MAC" ] && [ -n "$SRC" ] && [ -n "$DEV" ] && break
  sleep 3
done
[ -n "$MAC" ] && [ -n "$SRC" ] && [ -n "$DEV" ] || { logger "neuravps-netconsole: v6 params incompletos (gw=$GW src=$SRC dev=$DEV mac=$MAC)"; exit 1; }
# IPv6 netconsole must go through configfs — the module-param path does not
# transmit v6 on this kernel (validated 2026-07-09). Reload the module clean
# (drop any stale param target), then (re)create our dynamic target.
modprobe -r netconsole 2>/dev/null
modprobe netconsole 2>/dev/null
CG=/sys/kernel/config/netconsole
[ -d "$CG" ] || { logger "neuravps-netconsole: configfs netconsole no disponible"; exit 1; }
T="$CG/neuravps"
[ -d "$T" ] && { echo 0 > "$T/enabled" 2>/dev/null; rmdir "$T" 2>/dev/null; }
mkdir -p "$T" 2>/dev/null || { logger "neuravps-netconsole: no pude crear target configfs"; exit 1; }
echo "$DEV"     > "$T/dev_name"
echo "$SRC"     > "$T/local_ip"
echo "$BASE_IP" > "$T/remote_ip"
echo "$MAC"     > "$T/remote_mac"
echo "$PORT"    > "$T/remote_port"
if echo 1 > "$T/enabled" 2>/dev/null; then
  logger "neuravps-netconsole: [v6/configfs] $SRC/$DEV -> $BASE_IP/$MAC (gw $GW)"
else
  logger "neuravps-netconsole: enable configfs FALLO"; exit 1
fi
NCH
chmod +x /usr/local/sbin/neuravps-netconsole.sh
cat > /etc/systemd/system/neuravps-netconsole.service <<'UNIT'
[Unit]
Description=NeuraVPS netconsole (stream kernel console to region BASE)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=600
StartLimitBurst=60
[Service]
# Type=simple + Restart=on-failure, and NO RemainAfterExit: the helper configures
# netconsole then exits; if IPv6/NDP isn't ready at boot it exits 1 and systemd
# retries. RemainAfterExit=yes SUPPRESSES Restart (systemd treats the exit as a
# completed run) — that is what left units 'failed' with no retry after the
# 2026-07 fleet reboots. On success the helper exits 0 -> the unit goes inactive
# (dead): correct for a run-once setup and NOT reported by `systemctl --failed`.
Type=simple
ExecStart=/usr/local/sbin/neuravps-netconsole.sh
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable neuravps-netconsole.service >/dev/null 2>&1 || true
/usr/local/sbin/neuravps-netconsole.sh >/dev/null 2>&1 || true

pve-firewall restart || true

############################################
# smartd: suppress NVMe wear/reliability alerts (0x04)
# Samsung NVMe drives report Critical Warning 0x04 once they hit their
# manufacturer TBW limit. The drive continues to work fine (spare capacity
# intact, zero media errors), but smartd sends emails every 30 min.
# This filter silences exactly that alert on all detected NVMe devices
# while forwarding every other SMART alert normally.
############################################
log "Installing smartd NVMe wear-alert filter"

cat >/usr/local/sbin/smartd-filter.sh <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail

MESSAGE="${SMARTD_MESSAGE:-}"
FAILTYPE="${SMARTD_FAILTYPE:-}"
DEVICE="${SMARTD_DEVICE:-}"
DEVICESTRING="${SMARTD_DEVICESTRING:-}"
FULLMESSAGE="${SMARTD_FULLMESSAGE:-}"

combined_text="$(printf '%s\n%s\n%s\n%s\n%s\n' \
  "$MESSAGE" "$FULLMESSAGE" "$FAILTYPE" "$DEVICE" "$DEVICESTRING")"

if grep -qiE 'Critical Warning \(0x04\): Reliability|NVM subsystem reliability has been degraded' <<<"$combined_text"; then
  logger -t smartd-filter "Suppressed known NVMe reliability/wear alert for ${DEVICE:-unknown}: ${MESSAGE:-$FAILTYPE}"
  exit 0
fi

exec /usr/share/smartmontools/smartd-runner
EOF

chmod 755 /usr/local/sbin/smartd-filter.sh
cp -a /etc/smartd.conf /etc/smartd.conf.bak.$(date +%Y%m%d-%H%M%S) 2>/dev/null || true

{
  # Namespaces only (nvme0n1, nvme1n1): the old /dev/nvme* glob also matched
  # partitions (nvme0n1p1..p4) and char devices, generating redundant smartd
  # entries per disk.
  for dev in /dev/nvme[0-9]n[0-9]; do
    [[ -b "$dev" ]] || continue
    echo "${dev} -d nvme -a -n standby -m root -M exec /usr/local/sbin/smartd-filter.sh"
  done
} >/etc/smartd.conf

if [[ ! -s /etc/smartd.conf ]]; then
  log "WARNING: no NVMe block devices found; smartd.conf left empty"
else
  log "smartd.conf written for: $(awk '{print $1}' /etc/smartd.conf | tr '\n' ' ')"
fi

smartd -q onecheck 2>/dev/null || true
systemctl restart smartd || true
log "smartd NVMe filter installed"

# Put PXE entries FIRST in the permanent BootOrder. install.sh already does
# this in rescue, but the first REAL boot makes the firmware register fresh
# entries for the new ESPs and PREPEND them (seen on 0000008: BootOrder became
# 0009,0008,… with PXE third) — so the authoritative reorder must happen HERE,
# when the final entry set exists. An armed Hetzner rescue then always engages;
# normal boots are unaffected (Hetzner PXE chainloads the local disk).
if command -v efibootmgr >/dev/null 2>&1; then
  PXE_IDS=$(efibootmgr | sed -n 's/^Boot\([0-9A-Fa-f]\{4\}\)\*\{0,1\}[[:space:]].*PXE.*/\1/p' | paste -sd, -)
  CUR_ORDER=$(efibootmgr | sed -n 's/^BootOrder:[[:space:]]*//p')
  if [[ -n "$PXE_IDS" && -n "$CUR_ORDER" ]]; then
    REST=$(echo "$CUR_ORDER" | tr ',' '\n' | grep -v -F -x -f <(echo "$PXE_IDS" | tr ',' '\n') | paste -sd, -)
    if efibootmgr -o "${PXE_IDS}${REST:+,$REST}" >/dev/null 2>&1; then
      log "BootOrder set network-first: ${PXE_IDS}${REST:+,$REST}"
    else
      log "WARNING: efibootmgr reorder failed; BootOrder unchanged ($CUR_ORDER)"
    fi
  else
    log "WARNING: no PXE entries/BootOrder visible; boot order left as-is"
  fi
fi

if [ "$FIRST_BOOT_FAILED" -ne 0 ]; then
  # Clear the marker ourselves: ExecStartPost (which normally removes it)
  # only runs on success, and a full re-run on the next boot is worse than a
  # visibly failed unit once VMs land on the node (it would replay the whole
  # setup and schedule a surprise reboot). Failed unit -> node_health warns.
  rm -f /var/lib/proxmox-first-boot/pending-first-boot-setup
  log "first_boot.sh finished WITH ERRORS — marker cleared, unit left failed for node_health visibility"
  shutdown -r +1
  exit 1
fi

log "first_boot.sh finished"

# manually add with: qm set 100 --hookscript shared:snippets/sync-dnat.py
shutdown -r +1

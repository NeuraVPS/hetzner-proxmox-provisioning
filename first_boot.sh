#!/usr/bin/env bash
set -euo pipefail

log() { echo "[$(date +'%F %T')] $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

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
# Raw swap: 23 GB per disk (reserved by install via zfs.hdsize)
############################################
SWAP_RESERVE_GB=23
log "Setting up raw swap partitions (${SWAP_RESERVE_GB} GB per disk) on rpool disks"
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
    log "Creating ${SWAP_RESERVE_GB} GB swap partition ${next_num} on $disk"
    sgdisk -n "${next_num}:0:+${SWAP_RESERVE_GB}G" -t "${next_num}:8200" "$disk" || { log "WARNING: sgdisk failed on $disk"; continue; }
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
case "$HOSTCLASS" in
  *-EX44*)    MEM_PROFILE="e";   ARC_BYTES=$((1024 * 1024 * 1024)); ARC_MIN_BYTES=$((512 * 1024 * 1024)); SWAPPINESS=1; KSM_ON=0 ;;
  *-AX102-U*) MEM_PROFILE="mt";  ARC_BYTES=$((8  * 1024 * 1024 * 1024)); ARC_MIN_BYTES=0; SWAPPINESS=5; KSM_ON=1 ;;
  *-AX102*)   MEM_PROFILE="mt";  ARC_BYTES=$((8  * 1024 * 1024 * 1024)); ARC_MIN_BYTES=0; SWAPPINESS=5; KSM_ON=1 ;;
  *-AX162*)   MEM_PROFILE="ad";  ARC_BYTES=$((16 * 1024 * 1024 * 1024)); ARC_MIN_BYTES=0; SWAPPINESS=5; KSM_ON=1 ;;
  *)          MEM_PROFILE="vps"; ARC_BYTES=$((16 * 1024 * 1024 * 1024)); ARC_MIN_BYTES=0; SWAPPINESS=5; KSM_ON=0 ;;
esac
log "Memory profile: $MEM_PROFILE (host=$HOSTCLASS) — zfs_arc_max=$ARC_BYTES, arc_min=$ARC_MIN_BYTES, swappiness=$SWAPPINESS, ksm=$KSM_ON"

mkdir -p /etc/sysctl.d
echo "vm.swappiness=$SWAPPINESS" > /etc/sysctl.d/99-neuravps-memprofile.conf
rm -f /etc/sysctl.d/99-proxmox-swap.conf
sysctl -p /etc/sysctl.d/99-neuravps-memprofile.conf 2>/dev/null || true

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

sftp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -oBatchMode=yes -P 23 u560363@u560363.your-storagebox.de <<EOF
get /home/id/firebase-credentials.json /etc/firebase-credentials.json
get /home/firewall/cluster.fw /etc/pve/firewall/cluster.fw
bye
EOF

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

log "first_boot.sh finished"

# manually add with: qm set 100 --hookscript shared:snippets/sync-dnat.py
shutdown -r +1

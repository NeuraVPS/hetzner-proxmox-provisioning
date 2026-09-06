#!/usr/bin/env bash
# Prepare real ZFS tools before install.sh touches disks. Hetzner's command
# stubs download through IPv4-only GitHub endpoints and fail in IPv6 rescue.
set -euo pipefail

[[ -d /root/.oldroot/nfs/install ]] || { echo 'Hetzner rescue required' >&2; exit 1; }
kernel="$(uname -r)"
[[ -d "/lib/modules/$kernel/build" ]] || { echo 'Rescue kernel headers missing' >&2; exit 1; }

zpool_path="$(command -v zpool || true)"
if [[ -n "$zpool_path" ]] && file -b "$zpool_path" | grep -q ELF; then
  modprobe zfs
  zpool --version
  exit 0
fi

version=2.4.4
checksum=2a3c70d55a37cc71618a95a60e81ad66530201eb118d37741dc92efcf848c8b1
archive="/root/zfs-$version.tar.gz"
url="https://github.com/openzfs/zfs/releases/download/zfs-$version/zfs-$version.tar.gz"
if ! printf '%s  %s\n' "$checksum" "$archive" | sha256sum -c - >/dev/null 2>&1; then
  tmp="$(mktemp /root/neuravps-zfs-download.XXXXXX)"
  # Native HTTPS first; the fleet base supplies IPv4 egress when unavailable.
  # SSH authenticates with the same key already required by install.sh.
  if ! curl -fsSL --connect-timeout 10 --max-time 180 "$url" -o "$tmp"; then
    fetched=0
    for base in b1.neuravps.com b0.neuravps.com; do
      if ssh -6 -i /root/.ssh/neuravps_id -o BatchMode=yes \
          -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new "root@$base" \
          "curl -fsSL --connect-timeout 10 --max-time 180 '$url'" > "$tmp"; then
        fetched=1
        break
      fi
    done
    [[ "$fetched" = 1 ]] || { rm -f "$tmp"; echo 'ZFS download failed' >&2; exit 1; }
  fi
  printf '%s  %s\n' "$checksum" "$tmp" | sha256sum -c -
  mv "$tmp" "$archive"
fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  build-essential pkg-config gawk libssl-dev uuid-dev zlib1g-dev libblkid-dev

build_dir="$(mktemp -d /root/neuravps-zfs-build.XXXXXX)"
tar -xzf "$archive" -C "$build_dir"
cd "$build_dir/zfs-$version"
./configure
make -j "$(nproc)"
# Preserve the rescue stubs for diagnosis; they are not functional ZFS tools.
backup=/root/neuravps-rescue-zfs-stubs
mkdir -p "$backup"
for name in zfs zpool zdb zed zstream; do
  prior="$(command -v "$name" || true)"
  if [[ -n "$prior" && ! -e "$backup/$name" ]]; then cp -a "$prior" "$backup/$name"; fi
done
make install
ldconfig
depmod -a "$kernel"
modprobe zfs
hash -r
zpool --version
zfs --version

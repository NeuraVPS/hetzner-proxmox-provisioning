#!/usr/bin/env bash
set -euo pipefail

JOOL_VERSION="v4.1.15"
SRC_DIR="/usr/local/src/Jool"

systemctl stop jool 2>/dev/null || true
systemctl stop jool_siit 2>/dev/null || true
systemctl disable jool 2>/dev/null || true
systemctl disable jool_siit 2>/dev/null || true

apt purge -y jool-dkms jool-tools || true
apt autoremove -y || true

apt update
DEBIAN_FRONTEND=noninteractive apt install -y \
  build-essential \
  pkg-config \
  linux-headers-$(uname -r) \
  libnl-genl-3-dev \
  libxtables-dev \
  dkms \
  git \
  autoconf \
  automake \
  libtool \
  iptables \
  nftables \
  tcpdump \
  iproute2

rm -rf "${SRC_DIR}"
git clone https://github.com/NICMx/Jool.git "${SRC_DIR}"
cd "${SRC_DIR}"
git checkout "${JOOL_VERSION}"

/sbin/dkms remove jool/4.1.13 --all 2>/dev/null || true
/sbin/dkms remove jool/4.1.14 --all 2>/dev/null || true
/sbin/dkms remove jool/4.1.15 --all 2>/dev/null || true

/sbin/dkms install .

./autogen.sh
./configure
make -j"$(nproc)"
make install
ldconfig

modprobe jool_siit

echo "Jool version:"
jool_siit --version
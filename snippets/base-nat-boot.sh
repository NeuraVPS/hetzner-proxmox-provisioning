#!/bin/bash
# Prepare Jool NAT64 + firewall for BASE; then run full sync-base-nat.
# Source: hetzner-proxmox-provisioning (install to /usr/local/sbin).
set -euo pipefail

DEFAULT=/etc/default/base-nat
if [[ ! -f "$DEFAULT" ]]; then
  echo "Missing $DEFAULT" >&2
  exit 1
fi
# shellcheck source=/dev/null
set -a
source "$DEFAULT"
set +a

: "${JOOL_INSTANCE:=base}"
: "${POOL6:?POOL6 required}"
: "${MAIN_IPV4:?MAIN_IPV4 required}"
: "${MAIN_IPV6:?MAIN_IPV6 required}"
: "${FAILOVER_IPV4:?FAILOVER_IPV4 required}"
: "${FAILOVER_IPV6:?FAILOVER_IPV6 required}"
: "${MAIN_IPV4_TARGET_IPV6:=$MAIN_IPV6}"
: "${FAILOVER_IPV4_TARGET_IPV6:=$FAILOVER_IPV6}"
: "${SAMBA_PORT_BASE:=10000}"
: "${RDP_PORT_BASE:=20000}"
: "${VMID_MAX:=9999}"
SAMBA_END=$((SAMBA_PORT_BASE + VMID_MAX))
RDP_END=$((RDP_PORT_BASE + VMID_MAX))

modprobe jool

# Debian jool-dkms is usually Netfilter-only (--iptables fails with:
# "Netfilter is the only available instance framework."). Netfilter instances
# hook in the stack automatically — do NOT add iptables mangle JOOL rules for them.
if ! jool instance display 2>/dev/null | grep -qw "$JOOL_INSTANCE"; then
  jool instance add "$JOOL_INSTANCE" --netfilter --pool6 "$POOL6"
fi

# Pool4 on both ingress IPv4s (single-instance kernel, anchor-based IPv6 fanout).
jool -i "$JOOL_INSTANCE" pool4 add --tcp "$MAIN_IPV4" "${SAMBA_PORT_BASE}-${RDP_END}" --force 2>/dev/null || true
jool -i "$JOOL_INSTANCE" pool4 add --udp "$MAIN_IPV4" "${RDP_PORT_BASE}-${RDP_END}" --force 2>/dev/null || true
jool -i "$JOOL_INSTANCE" pool4 add --tcp "$FAILOVER_IPV4" "${SAMBA_PORT_BASE}-${RDP_END}" --force 2>/dev/null || true
jool -i "$JOOL_INSTANCE" pool4 add --udp "$FAILOVER_IPV4" "${RDP_PORT_BASE}-${RDP_END}" --force 2>/dev/null || true

# Base host firewall with nftables (no UFW): keep host services simple.
nft delete table inet base_filter 2>/dev/null || true
nft -f - <<'EOF'
add table inet base_filter
add chain inet base_filter input { type filter hook input priority 0; policy drop; }
add chain inet base_filter forward { type filter hook forward priority 0; policy accept; }
add chain inet base_filter output { type filter hook output priority 0; policy accept; }
add rule inet base_filter input iif lo accept
add rule inet base_filter input ct state established,related accept
add rule inet base_filter input ip protocol icmp accept
add rule inet base_filter input ip6 nexthdr ipv6-icmp accept
add rule inet base_filter input tcp dport { 22, 80, 443 } accept
EOF

python3 /usr/local/sbin/sync-base-nat.py sync
# PVE wildcard proxy backends from Firestore proxmox_nodes (nginx map); ok if nginx absent
python3 /usr/local/sbin/sync-base-nat.py sync nodes || true

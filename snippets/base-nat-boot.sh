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
: "${FAILOVER_IPV4:?FAILOVER_IPV4 required}"
: "${FAILOVER_IPV6:?FAILOVER_IPV6 required}"
: "${SAMBA_PORT_BASE:=10000}"
: "${RDP_PORT_BASE:=20000}"
: "${VMID_MAX:=9999}"
SAMBA_END=$((SAMBA_PORT_BASE + VMID_MAX))
RDP_END=$((RDP_PORT_BASE + VMID_MAX))
DPORTS="${SAMBA_PORT_BASE}:${SAMBA_END},${RDP_PORT_BASE}:${RDP_END}"

modprobe jool

if ! jool instance display 2>/dev/null | grep -qw "$JOOL_INSTANCE"; then
  jool instance add "$JOOL_INSTANCE" --iptables --pool6 "$POOL6"
fi

# Pool4: one contiguous range SMB..RDP (10000-29999 for VMID_MAX=9999). Same coverage as two
# separate adds; one entry is simpler and marginally less work for Jool (single pool4 row).
jool -i "$JOOL_INSTANCE" pool4 add --tcp "$FAILOVER_IPV4" "${SAMBA_PORT_BASE}-${RDP_END}" --force 2>/dev/null || true

# Netfilter hooks (iptables + ip6tables mangle)
if ! iptables -t mangle -C PREROUTING -j JOOL --instance "$JOOL_INSTANCE" 2>/dev/null; then
  iptables -t mangle -A PREROUTING -j JOOL --instance "$JOOL_INSTANCE"
fi
if ! ip6tables -t mangle -C PREROUTING -j JOOL --instance "$JOOL_INSTANCE" 2>/dev/null; then
  ip6tables -t mangle -A PREROUTING -j JOOL --instance "$JOOL_INSTANCE"
fi

# Accept SMB (10000-19999) + RDP (20000-29999) on failover when VMID_MAX=9999
if ! iptables -C INPUT -d "$FAILOVER_IPV4" -p tcp -m multiport --dports "$DPORTS" -j ACCEPT 2>/dev/null; then
  iptables -I INPUT 1 -d "$FAILOVER_IPV4" -p tcp -m multiport --dports "$DPORTS" -j ACCEPT
fi
if ! ip6tables -C INPUT -d "$FAILOVER_IPV6" -p tcp -m multiport --dports "$DPORTS" -j ACCEPT 2>/dev/null; then
  ip6tables -I INPUT 1 -d "$FAILOVER_IPV6" -p tcp -m multiport --dports "$DPORTS" -j ACCEPT
fi

exec python3 /usr/local/sbin/sync-base-nat.py sync

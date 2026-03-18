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
: "${SAMBA_PORT_BASE:=10000}"
: "${RDP_PORT_BASE:=20000}"
: "${VMID_MAX:=9999}"
SAMBA_END=$((SAMBA_PORT_BASE + VMID_MAX))
RDP_END=$((RDP_PORT_BASE + VMID_MAX))
DPORTS_TCP="${SAMBA_PORT_BASE}:${SAMBA_END},${RDP_PORT_BASE}:${RDP_END}"
DPORTS_RDP_UDP="${RDP_PORT_BASE}:${RDP_END}"

modprobe jool

# Debian jool-dkms is usually Netfilter-only (--iptables fails with:
# "Netfilter is the only available instance framework."). Netfilter instances
# hook in the stack automatically — do NOT add iptables mangle JOOL rules for them.
if ! jool instance display 2>/dev/null | grep -qw "$JOOL_INSTANCE"; then
  jool instance add "$JOOL_INSTANCE" --netfilter --pool6 "$POOL6"
fi

# Pool4 on main IPv4: TCP SMB+RDP range; UDP RDP range only.
jool -i "$JOOL_INSTANCE" pool4 add --tcp "$MAIN_IPV4" "${SAMBA_PORT_BASE}-${RDP_END}" --force 2>/dev/null || true
jool -i "$JOOL_INSTANCE" pool4 add --udp "$MAIN_IPV4" "${RDP_PORT_BASE}-${RDP_END}" --force 2>/dev/null || true

# Failover -> main passthrough (PREROUTING DNAT so failover traffic hits main NAT rules).
if ! iptables -t nat -C PREROUTING -d "$FAILOVER_IPV4" -j DNAT --to-destination "$MAIN_IPV4" 2>/dev/null; then
  iptables -t nat -A PREROUTING -d "$FAILOVER_IPV4" -j DNAT --to-destination "$MAIN_IPV4"
fi
if ! ip6tables -t nat -C PREROUTING -d "$FAILOVER_IPV6" -j DNAT --to-destination "$MAIN_IPV6" 2>/dev/null; then
  ip6tables -t nat -A PREROUTING -d "$FAILOVER_IPV6" -j DNAT --to-destination "$MAIN_IPV6"
fi

# INPUT: main IPs only. TCP SMB+RDP; UDP RDP only.
if ! iptables -C INPUT -d "$MAIN_IPV4" -p tcp -m multiport --dports "$DPORTS_TCP" -j ACCEPT 2>/dev/null; then
  iptables -I INPUT 1 -d "$MAIN_IPV4" -p tcp -m multiport --dports "$DPORTS_TCP" -j ACCEPT
fi
if ! iptables -C INPUT -d "$MAIN_IPV4" -p udp -m multiport --dports "$DPORTS_RDP_UDP" -j ACCEPT 2>/dev/null; then
  iptables -I INPUT 1 -d "$MAIN_IPV4" -p udp -m multiport --dports "$DPORTS_RDP_UDP" -j ACCEPT
fi
if ! ip6tables -C INPUT -d "$MAIN_IPV6" -p tcp -m multiport --dports "$DPORTS_TCP" -j ACCEPT 2>/dev/null; then
  ip6tables -I INPUT 1 -d "$MAIN_IPV6" -p tcp -m multiport --dports "$DPORTS_TCP" -j ACCEPT
fi
if ! ip6tables -C INPUT -d "$MAIN_IPV6" -p udp -m multiport --dports "$DPORTS_RDP_UDP" -j ACCEPT 2>/dev/null; then
  ip6tables -I INPUT 1 -d "$MAIN_IPV6" -p udp -m multiport --dports "$DPORTS_RDP_UDP" -j ACCEPT
fi

python3 /usr/local/sbin/sync-base-nat.py sync
# PVE wildcard proxy backends from Firestore proxmox_nodes (nginx map); ok if nginx absent
python3 /usr/local/sbin/sync-base-nat.py sync nodes || true

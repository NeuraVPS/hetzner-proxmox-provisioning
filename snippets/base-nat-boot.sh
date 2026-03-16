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
: "${MAIN_IPV4:=}"
: "${MAIN_IPV6:=}"
: "${SAMBA_PORT_BASE:=10000}"
: "${RDP_PORT_BASE:=20000}"
: "${VMID_MAX:=9999}"
SAMBA_END=$((SAMBA_PORT_BASE + VMID_MAX))
RDP_END=$((RDP_PORT_BASE + VMID_MAX))
DPORTS="${SAMBA_PORT_BASE}:${SAMBA_END},${RDP_PORT_BASE}:${RDP_END}"

modprobe jool

# Debian jool-dkms is usually Netfilter-only (--iptables fails with:
# "Netfilter is the only available instance framework."). Netfilter instances
# hook in the stack automatically — do NOT add iptables mangle JOOL rules for them.
if ! jool instance display 2>/dev/null | grep -qw "$JOOL_INSTANCE"; then
  jool instance add "$JOOL_INSTANCE" --netfilter --pool6 "$POOL6"
fi

# Pool4: one contiguous range SMB..RDP (10000-29999 for VMID_MAX=9999).
jool -i "$JOOL_INSTANCE" pool4 add --tcp "$FAILOVER_IPV4" "${SAMBA_PORT_BASE}-${RDP_END}" --force 2>/dev/null || true
# Native listen on main IP; add MAIN_IPV4 to pool4 when set (required for BIB entries in sync-base-nat).
if [[ -n "${MAIN_IPV4:-}" ]]; then
  jool -i "$JOOL_INSTANCE" pool4 add --tcp "$MAIN_IPV4" "${SAMBA_PORT_BASE}-${RDP_END}" --force
fi

# Accept SMB (10000-19999) + RDP (20000-29999) on failover when VMID_MAX=9999
if ! iptables -C INPUT -d "$FAILOVER_IPV4" -p tcp -m multiport --dports "$DPORTS" -j ACCEPT 2>/dev/null; then
  iptables -I INPUT 1 -d "$FAILOVER_IPV4" -p tcp -m multiport --dports "$DPORTS" -j ACCEPT
fi
if ! ip6tables -C INPUT -d "$FAILOVER_IPV6" -p tcp -m multiport --dports "$DPORTS" -j ACCEPT 2>/dev/null; then
  ip6tables -I INPUT 1 -d "$FAILOVER_IPV6" -p tcp -m multiport --dports "$DPORTS" -j ACCEPT
fi

# Option B: accept SMB/RDP on main IPs (PREROUTING for MAIN_IPV6 is in sync-base-nat.py).
if [[ -n "${MAIN_IPV4:-}" ]]; then
  if ! iptables -C INPUT -d "$MAIN_IPV4" -p tcp -m multiport --dports "$DPORTS" -j ACCEPT 2>/dev/null; then
    iptables -I INPUT 1 -d "$MAIN_IPV4" -p tcp -m multiport --dports "$DPORTS" -j ACCEPT
  fi
fi
if [[ -n "${MAIN_IPV6:-}" ]]; then
  if ! ip6tables -C INPUT -d "$MAIN_IPV6" -p tcp -m multiport --dports "$DPORTS" -j ACCEPT 2>/dev/null; then
    ip6tables -I INPUT 1 -d "$MAIN_IPV6" -p tcp -m multiport --dports "$DPORTS" -j ACCEPT
  fi
fi

python3 /usr/local/sbin/sync-base-nat.py sync
# PVE wildcard proxy backends from Firestore proxmox_nodes (nginx map); ok if nginx absent
python3 /usr/local/sbin/sync-base-nat.py sync nodes || true

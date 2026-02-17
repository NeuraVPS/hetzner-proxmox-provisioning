#!/usr/bin/env bash

############################################################
# DEFINE LOCAL FUNCTION (runs remotely)
############################################################
remote_task() {
  echo "== Running remote task =="
  log() { echo "[$(date +'%F %T')] $*"; }

  # Ensure NAT64 return route (64:ff9b::/96 via BASE) is in /etc/network/interfaces and applied now
  # Match the fd00:4000:: inet6 stanza by address (works whether interface is eth0.4000 or enp5s0.4000 etc.)
  if ! grep -q '64:ff9b::/96' /etc/network/interfaces 2>/dev/null; then
    log "NAT64: Adding post-up/post-down for 64:ff9b::/96 to fd00 inet6 stanza in /etc/network/interfaces"
    awk '
      /iface .* inet6 static/ { in_block=1; has_fd00=0; print; next }
      in_block && /fd00:4000::/ { has_fd00=1 }
      in_block && has_fd00 && /^[[:space:]]*netmask 108/ {
        print
        print "    post-up   ip -6 route add 64:ff9b::/96 via fd00:4000::1"
        print "    post-down ip -6 route del 64:ff9b::/96 via fd00:4000::1"
        in_block=0; has_fd00=0
        next
      }
      in_block && /^[^[:space:]]/ { in_block=0; has_fd00=0 }
      { print }
    ' /etc/network/interfaces > /tmp/interfaces.nat64 && mv /tmp/interfaces.nat64 /etc/network/interfaces
    ifreload -a 2>/dev/null || { log "WARNING: ifreload -a failed (e.g. interface name mismatch), adding route manually"; ip -6 route add 64:ff9b::/96 via fd00:4000::1 2>/dev/null || true; }
  fi

  log "NAT64: Registering this node's VM subnet on BASE (return route 64:ff9b::/96 is in /etc/network/interfaces post-up)"
  # If fd00 interface is missing, fix eth0.4000 -> <parent>.4000 mismatch (e.g. vlan-raw-device enp5s0 => use enp5s0.4000)
  FD00_IFACE=$(ip -6 addr show | awk '/^[0-9]+:.*state/ { iface=$2; gsub(/:$/,"",iface) } /inet6 fd00:4000::/ && !/::1\// { print iface; exit }')
  if [[ -z "$FD00_IFACE" ]] && [[ -f /etc/network/interfaces ]]; then
    # Get vlan-raw-device (parent) from file: last field on line containing vlan-raw-device
    PARENT=$(awk '/vlan-raw-device/ { print $NF; exit }' /etc/network/interfaces)
    # Fallback: kernel may have created PARENT.4000 (e.g. enp5s0.4000); get parent from ip link
    if [[ -z "$PARENT" ]]; then
      REAL_VLAN=$(ip link show 2>/dev/null | awk -F: '/\.4000:/ { gsub(/^ /,"",$2); print $2; exit }')
      [[ -n "$REAL_VLAN" ]] && PARENT="${REAL_VLAN%.4000}"
    fi
    if [[ -n "$PARENT" ]] && grep -q 'eth0\.4000' /etc/network/interfaces; then
      log "NAT64: Fixing interface name eth0.4000 -> ${PARENT}.4000 in /etc/network/interfaces"
      sed -i "s/eth0\.4000/${PARENT}.4000/g" /etc/network/interfaces
      ifreload -a 2>/dev/null || true
      FD00_IFACE=$(ip -6 addr show | awk '/^[0-9]+:.*state/ { iface=$2; gsub(/:$/,"",iface) } /inet6 fd00:4000::/ && !/::1\// { print iface; exit }')
    fi
    # If still no fd00 and file already has PARENT.4000, try ifreload to bring the interface up
    if [[ -z "$FD00_IFACE" && -n "$PARENT" ]] && grep -q "${PARENT}\.4000" /etc/network/interfaces; then
      log "NAT64: Running ifreload -a to bring up ${PARENT}.4000"
      ifreload -a 2>/dev/null || true
      FD00_IFACE=$(ip -6 addr show | awk '/^[0-9]+:.*state/ { iface=$2; gsub(/:$/,"",iface) } /inet6 fd00:4000::/ && !/::1\// { print iface; exit }')
    fi
  fi
  # Register this node's VM subnet on BASE so BASE can route to our VMs
  # ip may list the interface as eth0.4000@enp5s0; use the name without @parent for ip commands
  if [[ -n "$FD00_IFACE" ]]; then
    FD00_DEV="${FD00_IFACE%%@*}"
    NODE_FD00=$(ip -6 addr show dev "$FD00_DEV" 2>/dev/null | awk '/inet6 fd00:4000::/ { print $2; exit }' | cut -d/ -f1)
    VM_PREFIX=$(ip -6 addr show vmbr0 2>/dev/null | awk '/inet6 .* scope global/ { print $2; exit }' | sed 's/::1\/64/::\/64/')
    if [[ -n "$NODE_FD00" && -n "$VM_PREFIX" ]]; then
      if ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o BatchMode=yes root@fd00:4000::1 "grep -q '^${VM_PREFIX}' /etc/sync-dnat/nat64-routes.conf 2>/dev/null || echo '${VM_PREFIX} ${NODE_FD00}' >> /etc/sync-dnat/nat64-routes.conf; /usr/local/bin/apply-nat64-routes.sh"; then
        log "NAT64: Registered VM subnet ${VM_PREFIX} on BASE via ${NODE_FD00}"
      else
        log "WARNING: NAT64: Could not register VM subnet on BASE (SSH or apply failed)"
      fi
    else
      log "WARNING: NAT64: Could not detect NODE_FD00 or VM_PREFIX, skipping BASE registration"
    fi
  else
    log "WARNING: NAT64: Could not detect fd00:4000 interface (no address on any interface). If you see 'Device eth0.4000@enp5s0 does not exist', fix /etc/network/interfaces to use the real VLAN name (e.g. enp5s0.4000). Skipping BASE registration."
  fi
  echo "== Finished =="
}
############################################################

# Extract function body into a string
FUNC_CONTENT=$(declare -f remote_task)

for ((i=2; i<=53; i++)); do
    HEX=$(printf "%x" "$i")
    IP="fd00:4000::${HEX}"

    echo "------------------------------------------------"
    echo "Connecting to $IP ($i)"

    # Send function and execute it
    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ForwardAgent=yes "root@$IP" \
        "$FUNC_CONTENT; remote_task" \
        || echo "❌ Failed to connect to $IP"
done

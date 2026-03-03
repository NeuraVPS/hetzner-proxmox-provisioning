#!/usr/bin/env bash
# Remove legacy node-side iptables DNAT and FORWARD rules (ssh-vmid-*, rdp-vmid-*, samba-vmid-*).
# NAT64 is now handled on BASE only. Run on each Proxmox node to clean up deprecated rules.
# Idempotent: safe to run multiple times; no-op on nodes (or BASE) that have no such rules.

############################################################
# DEFINE LOCAL FUNCTION (runs remotely)
############################################################
remote_task() {
  echo "== Removing legacy node DNAT/FORWARD rules =="
  for table in nat filter; do
    while IFS= read -r line; do
      [ -z "$line" ] && continue
      read -ra args <<< "$line"
      chain=${args[1]}
      iptables -t "$table" -D "$chain" "${args[@]:2}" 2>/dev/null || true
    done < <(iptables-save -t "$table" 2>/dev/null | grep -E 'ssh-vmid-|rdp-vmid-|samba-vmid-')
  done
  echo "== Finished =="
}
############################################################

# Extract function body into a string
FUNC_CONTENT=$(declare -f remote_task)

for ((i=1; i<=53; i++)); do
    HEX=$(printf "%x" "$i")
    IP="fd00:4000::${HEX}"

    echo "------------------------------------------------"
    echo "Connecting to $IP ($i)"

    # Send function and execute it
    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ForwardAgent=yes "root@$IP" \
        "$FUNC_CONTENT; remote_task" \
        || echo "❌ Failed to connect to $IP"
done

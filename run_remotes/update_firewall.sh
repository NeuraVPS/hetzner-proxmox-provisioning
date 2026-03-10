#!/usr/bin/env bash

############################################################
# DEFINE LOCAL FUNCTION (runs remotely)
############################################################
remote_task() {
  echo "== Running remote task =="

  scp [fd00:4000::1]:/etc/pve/firewall/cluster.fw /etc/pve/firewall/
  pve-firewall restart || true

  echo "== Finished =="
}
############################################################

# Extract function body into a string
FUNC_CONTENT=$(declare -f remote_task)

for ((i=2; i<=77; i++)); do
    HEX=$(printf "%x" "$i")
    IP="fd00:4000::${HEX}"

    echo "------------------------------------------------"
    echo "Connecting to $IP ($i)"

    # Send function and execute it
    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ForwardAgent=yes "root@$IP" \
        "$FUNC_CONTENT; remote_task" \
        || echo "❌ Failed to connect to $IP"
done

#!/usr/bin/env bash

############################################################
# DEFINE LOCAL FUNCTION (runs remotely)
############################################################
remote_task() {
    echo "== Running remote task =="
    # /var/lib/svz/snippets/sync-dnat.py
    # apt-get update && apt-get full-upgrade -y
    
    echo "== Finished =="
}
############################################################

# Extract function body into a string
FUNC_CONTENT=$(declare -f remote_task)

for ((i=1; i<=20; i++)); do
    HEX=$(printf "%x" "$i")
    IP="fd00:4000::${HEX}"

    echo "------------------------------------------------"
    echo "Connecting to $IP"

    # Send function and execute it
    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ForwardAgent=yes "root@$IP" \
        "$FUNC_CONTENT; remote_task" \
        || echo "❌ Failed to connect to $IP"
done
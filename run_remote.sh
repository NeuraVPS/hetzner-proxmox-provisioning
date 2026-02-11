#!/usr/bin/env bash

############################################################
# DEFINE LOCAL FUNCTION (runs remotely)
############################################################
remote_task() {
#     echo "== Running remote task =="
#     # /var/lib/svz/snippets/sync-dnat.py
#     # apt-get update && apt-get full-upgrade -y
    
    sftp -oBatchMode=yes  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@[fd00:4000::2] <<EOF
get /etc/pve/firewall/cluster.fw /etc/pve/firewall/cluster.fw
bye
EOF

#     sftp -oBatchMode=yes  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@[fd00:4000::20] <<EOF
# get /var/lib/svz/dump/vzdump-qemu-100-es.vma.zst /var/lib/svz/dump/vzdump-qemu-100-es.vma.zst.2
# bye
# EOF

pve-firewall restart || true

  # mv /var/lib/svz/dump/vzdump-qemu-100-es.vma.zst.2 /var/lib/svz/dump/vzdump-qemu-100-es.vma.zst

#     systemctl restart corosync
#     echo "== Finished =="
}
############################################################

# Extract function body into a string
FUNC_CONTENT=$(declare -f remote_task)

for ((i=3; i<=48; i++)); do
    HEX=$(printf "%x" "$i")
    IP="fd00:4000::${HEX}"

    echo "------------------------------------------------"
    echo "Connecting to $IP"

    # Send function and execute it
    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ForwardAgent=yes "root@$IP" \
        "$FUNC_CONTENT; remote_task" \
        || echo "❌ Failed to connect to $IP"
done
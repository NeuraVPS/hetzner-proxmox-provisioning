#!/usr/bin/env bash
# Run on the provisioning host to update package lists and upgrade packages on each
# Proxmox node. Keeps qemu-server, pve-edk2-firmware, and other PVE packages current
# (e.g. for EFI/Secure Boot and ms-cert support). Use with care; schedule during low traffic.

############################################################
# DEFINE LOCAL FUNCTION (runs remotely)
############################################################
remote_task() {
  echo "== Running package update on $(hostname) =="
  apt-get update -qq
  apt-get upgrade -y -qq
  echo "== Finished =="
}
############################################################

FUNC_CONTENT=$(declare -f remote_task)

for ((i=2; i<=53; i++)); do
    HEX=$(printf "%x" "$i")
    IP="fd00:4000::${HEX}"

    echo "------------------------------------------------"
    echo "Connecting to $IP ($i)"

    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ForwardAgent=yes "root@$IP" \
        "$FUNC_CONTENT; remote_task" \
        || echo "❌ Failed to connect to $IP"
done

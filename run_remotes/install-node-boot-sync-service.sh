#!/usr/bin/env bash

############################################################
# Migration: install node-boot-sync-dnat.service on existing nodes
#
# WHY: nodes provisioned before this service was added to first_boot.sh have
# no `node-boot-sync-dnat.service`. That unit runs `sync-dnat.py node-boot` at
# boot, which is the ONLY thing that:
#   - stamps proxmox_nodes/{node}.lastNodeBootAt,
#   - reconciles VM running/stopped status, and
#   - finalizes user-requested node maintenance (clears
#     maintenance + pendingPostBootAction, auto-starts the VM).
# Without it, a panel "upgrade & restart" on a VPS E node gets stuck forever on
# "Actualizando…/Reiniciando…" and the VM is never auto-started after the
# reboot. Symptom seen on 0000026-EX44 (VM 626): lastNodeBootAt frozen weeks in
# the past, `systemctl status node-boot-sync-dnat.service` => "could not be
# found", and no `node-boot:` line ever in /var/log/sync-dnat.log.
#
# This installs (idempotently): the latest sync-dnat.py, the unit file, then
# daemon-reload + enable. It intentionally does NOT start the unit now — doing
# so would run the post-boot action and could `qm start` a user VM out from
# under them. The unit fires on the node's next legitimate boot, which is the
# intended trigger.
############################################################

############################################################
# DEFINE LOCAL FUNCTION (runs remotely)
############################################################
remote_task() {
  echo "== Installing node-boot-sync-dnat.service =="

  # 1. Make sure the latest sync-dnat.py is present (the unit is useless without
  #    the up-to-date finalize logic). Same source as run_remotes/generic.sh.
  mkdir -p /var/lib/svz/snippets
  curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/snippets/sync-dnat.py \
    -o /var/lib/svz/snippets/sync-dnat.py
  chmod +x /var/lib/svz/snippets/sync-dnat.py

  # 2. Write the unit file (verbatim copy of the first_boot.sh definition).
  cat > /etc/systemd/system/node-boot-sync-dnat.service <<'NODEBOOTEOF'
[Unit]
Description=Update lastNodeBootAt and sync VM status at boot
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/var/lib/svz/snippets/sync-dnat.py node-boot
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
NODEBOOTEOF

  # 3. Reload + enable so it runs at the next boot. NOT started now on purpose
  #    (see header: avoids an unexpected qm start of a user VM).
  systemctl daemon-reload
  systemctl enable node-boot-sync-dnat.service

  # 4. Verify and report.
  echo "-- is-enabled: $(systemctl is-enabled node-boot-sync-dnat.service 2>&1)"
  echo "-- sync-dnat.py: $(ls -l /var/lib/svz/snippets/sync-dnat.py 2>&1)"
  echo "== Finished =="
}
############################################################

# Extract function body into a string
FUNC_CONTENT=$(declare -f remote_task)

NODES_FILE="/var/lib/base-nat/pve_nodes.json"
if [[ ! -f "$NODES_FILE" ]]; then
    echo "Missing $NODES_FILE"
    exit 1
fi

# Run only on these host numbers (e.g. 2 5 7). Leave empty to run on all (still subject to SKIP/GTE/LTE below).
ONLY_HOST_NUMS=()
# Skip these host numbers (e.g. 2 5 7). Leave empty to run on all.
SKIP_HOST_NUMS=()
# Only run when host_num >= N, or host_num <= N. Leave empty to ignore.
HOST_NUM_GTE=
HOST_NUM_LTE=

while read -r hostname ip; do
    host_num=$((10#${hostname%%-*}))
    if (( ${#ONLY_HOST_NUMS[@]} > 0 )); then
        in_only=0
        for o in "${ONLY_HOST_NUMS[@]}"; do
            if [[ $host_num -eq $o ]]; then in_only=1; break; fi
        done
        if [[ $in_only -eq 0 ]]; then continue; fi
    fi
    skip=0
    for s in "${SKIP_HOST_NUMS[@]}"; do
        if [[ $host_num -eq $s ]]; then skip=1; break; fi
    done
    if [[ $skip -eq 1 ]]; then echo "Skipping $hostname (host_num=$host_num)"; continue; fi
    if [[ -n "${HOST_NUM_GTE:-}" && $host_num -lt $HOST_NUM_GTE ]]; then echo "Skipping $hostname (host_num $host_num not >= $HOST_NUM_GTE)"; continue; fi
    if [[ -n "${HOST_NUM_LTE:-}" && $host_num -gt $HOST_NUM_LTE ]]; then echo "Skipping $hostname (host_num $host_num not <= $HOST_NUM_LTE)"; continue; fi

    echo "------------------------------------------------"
    echo "Connecting to $hostname ($ip)"

    # Send function and execute it (-n so ssh does not consume the loop stdin)
    ssh -n -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ForwardAgent=yes "root@$ip" \
        "$FUNC_CONTENT; remote_task" \
        || echo "❌ Failed to connect to $hostname ($ip)"
done < <(jq -r 'to_entries[] | "\(.key) \(.value)"' "$NODES_FILE" | sort)

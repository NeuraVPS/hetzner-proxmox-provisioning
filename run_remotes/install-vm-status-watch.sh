#!/usr/bin/env bash

############################################################
# Migration: install the VM status watchdog on existing nodes
#
# WHY: the post-start / post-stop hooks are the only thing that tells Firestore
# a VM changed state, and a hook is a process — it can die between "Firebase
# initialized" and the write landing. Nothing anywhere reconciles that, so the
# panel stays wrong FOREVER: on 2026-07-30 VMs 1980/1985/1986 showed "stopped"
# while running, and the only fix was a manual full sync. For a customer that
# reads as "my server won't start" about a server that is running.
#
# The obvious watchdog — re-read Firestore every few minutes and compare —
# would cost one query per VM per run, roughly a million reads a day across the
# fleet. So `sync-dnat.py watch` compares against a LOCAL file recording what
# we last successfully wrote. A run where nothing changed makes NO Firestore
# call at all: it costs one `qm list` (~0.7 s of CPU, Nice=19, IO idle) and
# stops there. Firestore is touched only when a hook actually missed something.
#
# Installs idempotently: latest sync-dnat.py, the unit + timer, daemon-reload,
# enable --now. Starting it immediately is safe — unlike the node-boot unit it
# never starts or stops a guest; the worst it can do is correct a status.
############################################################

############################################################
# DEFINE LOCAL FUNCTION (runs remotely)
############################################################
remote_task() {
  echo "== Installing neuravps-vm-status-watch =="

  mkdir -p /var/lib/svz/snippets
  curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/snippets/sync-dnat.py \
    -o /var/lib/svz/snippets/sync-dnat.py
  chmod +x /var/lib/svz/snippets/sync-dnat.py

  curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/run_remotes/neuravps-vm-status-watch.service \
    -o /etc/systemd/system/neuravps-vm-status-watch.service
  curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/run_remotes/neuravps-vm-status-watch.timer \
    -o /etc/systemd/system/neuravps-vm-status-watch.timer

  # Seed the cache from the CURRENT reality before the timer can fire, so the
  # first run is a no-op instead of re-pushing every VM on the node.
  /usr/bin/python3 /var/lib/svz/snippets/sync-dnat.py watch >/dev/null 2>&1 || true

  systemctl daemon-reload
  systemctl enable --now neuravps-vm-status-watch.timer

  echo "-- timer:  $(systemctl is-enabled neuravps-vm-status-watch.timer 2>&1) / $(systemctl is-active neuravps-vm-status-watch.timer 2>&1)"
  echo "-- next:   $(systemctl list-timers neuravps-vm-status-watch.timer --no-pager 2>/dev/null | sed -n 2p | cut -c1-60)"
  echo "-- cache:  $(wc -c < /var/lib/neuravps/vm_status_cache.json 2>/dev/null || echo 0) bytes"
  echo "== Finished =="
}
############################################################

# Extract function body into a string
FUNC_CONTENT=$(declare -f remote_task)

# The other run_remotes scripts read the node list with jq. b0 does not have
# jq, so this falls back to python3 (always present — sync-dnat.py is python)
# rather than making a fleet migration depend on installing a package on the
# base server.
node_list() {
    if command -v jq >/dev/null 2>&1; then
        jq -r 'to_entries[] | "\(.key) \(.value)"' "$1"
    else
        python3 -c 'import json,sys
for k, v in json.load(open(sys.argv[1])).items():
    print(k, v)' "$1"
    fi
}

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
done < <(node_list "$NODES_FILE" | sort)

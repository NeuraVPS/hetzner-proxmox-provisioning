#!/usr/bin/env bash

############################################################
# Migration: install the weekly EX44 guest ReTrim timer (Sunday 04:00 + jitter)
#
# WHY: incident 2026-11-09 (EX44 0000129): deleted space inside Windows was
# never returned to the zvol, rpool filled, VM paused. Installs ONLY the
# timer on EX44 hosts; it does not run a trim now.
############################################################

############################################################
# DEFINE LOCAL FUNCTION (runs remotely)
############################################################
remote_task() {
  echo "== Installing neuravps-ex44-retrim =="
  local base=https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/run_remotes
  curl -sSL "$base/neuravps-ex44-retrim.sh" -o /usr/local/sbin/neuravps-ex44-retrim.sh.new
  # Verify we got the real script, not a stale/error body.
  if ! grep -q "NEXRETRIM=1" /usr/local/sbin/neuravps-ex44-retrim.sh.new; then
    echo "❌ downloaded script lacks version marker — aborting this node"
    rm -f /usr/local/sbin/neuravps-ex44-retrim.sh.new
    return 1
  fi
  mv /usr/local/sbin/neuravps-ex44-retrim.sh.new /usr/local/sbin/neuravps-ex44-retrim.sh
  chmod +x /usr/local/sbin/neuravps-ex44-retrim.sh
  curl -sSL "$base/neuravps-ex44-retrim.service" -o /etc/systemd/system/neuravps-ex44-retrim.service
  curl -sSL "$base/neuravps-ex44-retrim.timer" -o /etc/systemd/system/neuravps-ex44-retrim.timer
  systemctl daemon-reload
  # Only the TIMER is enabled; this installer never runs a trim.
  systemctl enable --now neuravps-ex44-retrim.timer
  echo "-- timer: $(systemctl is-enabled neuravps-ex44-retrim.timer 2>&1) / $(systemctl is-active neuravps-ex44-retrim.timer 2>&1) next: $(systemctl list-timers neuravps-ex44-retrim.timer --no-legend 2>/dev/null | awk '{print $1,$2,$3}')"
  echo "== Finished =="
}
############################################################

# Extract function body into a string
FUNC_CONTENT=$(declare -f remote_task)

# b0 has no jq; python3 is always present (same fallback as
# install-vm-status-watch.sh).
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
    # This timer is for the dedicated EX44 fleet only.
    if [[ "$hostname" != *EX44* ]]; then continue; fi
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

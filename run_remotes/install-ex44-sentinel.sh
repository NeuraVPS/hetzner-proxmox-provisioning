#!/usr/bin/env bash

############################################################
# Migration: install the EX44 swap-grind sentinel on the dedicated EX44 fleet
#
# WHY: operator decision 2026-07-31 — the 127 grandfathered vps-e stay on
# their cheap EX44 boxes; a customer that suffers gets moved (or respec'd)
# ON COMPLAINT. That policy needs the complaint to be VISIBLE before the
# customer writes it: these hosts run a 60 GB guest on 62 GB usable, park
# cold guest pages in swap, and the guest grinds when it sweeps them back.
# salud deliberately never alerts there (daily sample + the single-tenant
# "load, not fault" carve-out), so the sentinel samples the REAL disk
# swap-in rate every minute locally and reports sustained grinding to
# Firestore `ex44_distress`; node_liveness mails the 🟠 within 5 minutes.
#
# Cost when healthy: two /proc reads per minute, zero Firestore calls.
# The script self-guards on hostname, so a stray install is a no-op.
############################################################

############################################################
# DEFINE LOCAL FUNCTION (runs remotely)
############################################################
remote_task() {
  echo "== Installing neuravps-ex44-sentinel =="

  curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/run_remotes/neuravps-ex44-sentinel.sh \
    -o /usr/local/sbin/neuravps-ex44-sentinel.sh.new
  # Verify we got the real script, not a stale/error body (GitHub raw served
  # a pre-fix file mid-rollout once — 2026-07-16 lesson: check, then install).
  if ! grep -q "NEXSVER=1" /usr/local/sbin/neuravps-ex44-sentinel.sh.new; then
    echo "❌ downloaded sentinel lacks version marker — aborting this node"
    rm -f /usr/local/sbin/neuravps-ex44-sentinel.sh.new
    return 1
  fi
  mv /usr/local/sbin/neuravps-ex44-sentinel.sh.new /usr/local/sbin/neuravps-ex44-sentinel.sh
  chmod +x /usr/local/sbin/neuravps-ex44-sentinel.sh

  curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/run_remotes/neuravps-ex44-sentinel.service \
    -o /etc/systemd/system/neuravps-ex44-sentinel.service
  curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/run_remotes/neuravps-ex44-sentinel.timer \
    -o /etc/systemd/system/neuravps-ex44-sentinel.timer

  # Seed the state from current counters so the first timer run measures a
  # real delta instead of alerting on garbage.
  /bin/bash /usr/local/sbin/neuravps-ex44-sentinel.sh >/dev/null 2>&1 || true

  systemctl daemon-reload
  systemctl enable --now neuravps-ex44-sentinel.timer

  echo "-- timer: $(systemctl is-enabled neuravps-ex44-sentinel.timer 2>&1) / $(systemctl is-active neuravps-ex44-sentinel.timer 2>&1)"
  echo "-- state: $(cat /var/lib/neuravps-ex44-sentinel/state 2>/dev/null || echo none)"
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
    # This sentinel is for the dedicated EX44 fleet only.
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

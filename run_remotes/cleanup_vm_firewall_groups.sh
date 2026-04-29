#!/usr/bin/env bash

############################################################
# Delete the deprecated VM firewall group rules:
#   vm-no-rdp, vm-no-samba, vm-no-ssh
# from every QEMU VM on every Proxmox node.
#
# These groups were replaced by per-service control on BASE
# (sync-base-nat reads firewall.{rdpEnabled,sambaEnabled} from
# Firestore and only adds DNAT entries for enabled services).
#
# The script only touches VM firewall rules. The cluster-level
# [group ...] definitions in /etc/pve/firewall/cluster.fw are
# left alone (clean those up manually in the storagebox copy).
############################################################
remote_task() {
  echo "== Running remote task =="

  local groups=("vm-no-rdp" "vm-no-samba" "vm-no-ssh")
  local total_disabled=0
  local total_deleted=0

  # Iterate all QEMU VMs on this node.
  while read -r vmid; do
    [[ -z "$vmid" ]] && continue

    # Match rules referencing any of the deprecated groups. Each line
    # from `pvesh ... rules` is a JSON object with at least pos/type/action.
    local rules_json
    rules_json=$(pvesh get "/nodes/$(hostname)/qemu/${vmid}/firewall/rules" --output-format json 2>/dev/null || echo "[]")
    [[ "$rules_json" == "[]" || -z "$rules_json" ]] && continue

    # Collect (pos, action) pairs to act on, sorted DESC so deletions
    # don't shift the positions of later targets.
    local targets
    targets=$(echo "$rules_json" \
      | jq -r --argjson g '["vm-no-rdp","vm-no-samba","vm-no-ssh"]' \
          '.[] | select(.type=="group") | select(.action as $a | $g | index($a)) | "\(.pos) \(.action)"' \
      | sort -k1,1nr)
    [[ -z "$targets" ]] && continue

    echo "VM ${vmid}: cleaning $(echo "$targets" | wc -l) deprecated rule(s)"
    while read -r pos action; do
      [[ -z "$pos" ]] && continue
      # Disable first (idempotent + safe if delete fails halfway).
      if pvesh set "/nodes/$(hostname)/qemu/${vmid}/firewall/rules/${pos}" --enable 0 >/dev/null 2>&1; then
        total_disabled=$((total_disabled + 1))
      fi
      if pvesh delete "/nodes/$(hostname)/qemu/${vmid}/firewall/rules/${pos}" >/dev/null 2>&1; then
        echo "  - deleted pos=${pos} action=${action}"
        total_deleted=$((total_deleted + 1))
      else
        echo "  ! failed to delete pos=${pos} action=${action}"
      fi
    done <<<"$targets"
  done < <(pvesh get /cluster/resources --type vm --output-format json 2>/dev/null \
            | jq -r --arg n "$(hostname)" '.[] | select(.type=="qemu") | select(.node==$n) | .vmid')

  echo "Summary: disabled=${total_disabled} deleted=${total_deleted}"
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
        if [[ $in_only -eq 0 ]]; then echo "Skipping $hostname (host_num=$host_num not in ONLY_HOST_NUMS)"; continue; fi
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

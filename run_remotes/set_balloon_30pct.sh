#!/usr/bin/env bash
# Run on the provisioning host. For each Proxmox node, for each VM: set balloon as follows.
# If max RAM >= 60416 MiB: no ballooning (balloon=memory), including when balloon was
# previously set lower. Else if max equals min (no ballooning): 50% when max<=5120 MiB,
# else 30%. No reboot needed.
# Usage: DRY_RUN=1 ./set_balloon_30pct.sh  # only print what would be done

DRY_RUN=${DRY_RUN:-0}

############################################################
# DEFINE LOCAL FUNCTION (runs remotely)
############################################################
remote_task() {
  echo "== Setting balloon (no-balloon if max>=60416, else 50% if max<=5120, else 30%) on $(hostname) (DRY_RUN=${DRY_RUN}) =="
  while read -r vmid; do
    [[ "$vmid" =~ ^[0-9]+$ ]] || continue
    config=$(qm config "$vmid" 2>/dev/null) || continue
    echo "$config" | grep -q '^template:' && continue
    memory=$(echo "$config" | awk -F: '/^memory:/{gsub(/^[ \t]+/,"",$2); print $2+0; exit}')
    balloon=$(echo "$config" | awk -F: '/^balloon:/{gsub(/^[ \t]+/,"",$2); print $2+0; exit}')
    [[ -n "$memory" && "$memory" -gt 0 ]] || continue
    need_set=0
    if [[ "$memory" -ge 60416 ]]; then
      new_balloon=$memory
      need_set=1
    elif [[ -z "$balloon" || "$balloon" -eq "$memory" ]]; then
      need_set=1
      if [[ "$memory" -le 5120 ]]; then
        new_balloon=$(( memory * 50 / 100 ))
        [[ "$new_balloon" -lt 1 ]] && new_balloon=1
      else
        new_balloon=$(( memory * 30 / 100 ))
        [[ "$new_balloon" -lt 1 ]] && new_balloon=1
      fi
    fi
    if [[ "$need_set" -eq 1 ]]; then
      if [[ "$DRY_RUN" -eq 1 ]]; then
        echo "  Would set VMID $vmid balloon to $new_balloon MiB (memory=$memory MiB)"
      else
        qm set "$vmid" --balloon "$new_balloon" && echo "  VMID $vmid: memory=$memory MiB -> balloon=$new_balloon MiB"
      fi
    fi
  done < <(qm list 2>/dev/null | awk 'NR>1 {print $1}')
  echo "== Finished =="
}
############################################################

FUNC_CONTENT=$(declare -f remote_task)

# Host range: same as other run_remotes scripts; adjust if your environment differs
for ((i=1; i<=55; i++)); do
  HEX=$(printf "%x" "$i")
  IP="fd00:4000::${HEX}"

  echo "------------------------------------------------"
  echo "Connecting to $IP ($i)"

  ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ForwardAgent=yes "root@$IP" \
    "export DRY_RUN='$DRY_RUN'; $FUNC_CONTENT; remote_task" \
    || echo "❌ Failed to connect to $IP"
done

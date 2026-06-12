#!/usr/bin/env bash

############################################################
# Phase-1 fleet memory retune (NeuraVPS/NeuraVPS docs/NEURAVPS_FLEET_TUNING_PLAN.md)
#
# WHY: first_boot.sh historically set a FIXED 32 GB ZFS ARC + swappiness=10 on
# every node class. On 64 GB EX44 nodes (one dedicated 61 GB VPS-E VM) the ARC
# competes with the VM and the kernel swaps out customer RAM — measured live:
# VMs with 2-13 GB of guest RAM sitting in host swap across ALL node classes,
# paying disk latency on every touched page (slow SQX Monte Carlo / retests).
#
# WHAT (per-class profiles):
#   profile e   (*-EX44, 1 dedicated VM):  zfs_arc_max=1G (arc_min=0.5G), swappiness=1
#   profile mt  (*-AX102 y *-AX102-U, 38-48 MT VMs): zfs_arc_max=8G, swappiness=5, ksmtuned ON
#   profile vps (*-AX162*):                zfs_arc_max=16G, swappiness=5
# (2026-06-12: AX102-U reclassified vps->mt — live audit found all 9 nodes
#  host exclusively 38-48 x 4096 MiB MT VMs; mt profile applied fleet-wide.)
# plus optional swap drain (swapoff/swapon) so already-swapped customer RAM
# returns to memory NOW instead of on next page-touch.
#
# HOW TO RUN (from a BASE host, like the other run_remotes):
#   DRY RUN (default — prints current vs target, changes nothing):
#       bash tune_memory_profile.sh
#   APPLY:
#       APPLY=1 bash tune_memory_profile.sh
#   APPLY + DRAIN SWAP (only drains when MemAvailable > swap-used + 8 GB):
#       APPLY=1 DESWAP=1 bash tune_memory_profile.sh
#   Limit scope with the standard filters below (ONLY_HOST_NUMS etc.).
#   Force a profile (skip autodetect):  FORCE_PROFILE=e APPLY=1 ...
#
# RECOMMENDED ROLLOUT ORDER:
#   1) EX44 first (zero risk: tiny ARC needed, swap drain is ~4 GB)
#   2) MT (AX102)
#   3) A-D (AX162/AX102-U) staged, old high-RAM nodes first (e.g. 0000002 has
#      80 GB available vs 51 GB swapped — trivially safe), off-peak hours.
#
# Everything is reversible: re-apply old values the same way. Persisted via
# /etc/modprobe.d/zfs.conf + /etc/sysctl.d/99-neuravps-memprofile.conf.
# Verification: node_health Phase-0 probes (per-VM swap → 0 and stays 0).
############################################################

APPLY="${APPLY:-0}"
DESWAP="${DESWAP:-0}"
FORCE_PROFILE="${FORCE_PROFILE:-}"

############################################################
# DEFINE LOCAL FUNCTION (runs remotely on each node)
############################################################
remote_task() {
  echo "== Memory profile retune (APPLY=${APPLY:-0} DESWAP=${DESWAP:-0}) =="
  local host; host=$(hostname)

  # ---- profile detection ------------------------------------------------
  local profile="${FORCE_PROFILE:-}"
  if [[ -z "$profile" ]]; then
    case "$host" in
      *-EX44*)    profile="e"   ;;
      *-AX102-U*) profile="mt"  ;;
      *-AX102*)   profile="mt"  ;;
      *-AX162*)   profile="vps" ;;
      *)          profile="vps" ;;
    esac
  fi

  # Profile e also lowers zfs_arc_min: the default is RAM/32 (= 2 GiB on a
  # 64 GB EX44) and ZFS silently CLAMPS c_max to arc_min — writing a 1 GiB
  # cap alone leaves the ARC at 2 GiB with no error. Always write min first.
  # (2026-06-12 retune: 1 GiB ARC + memory=61440 VM frees the EX44 budget;
  # measured cold-swap floor drops 2-4.5 GB -> 0.54 GB, Windows pagefile 0.)
  local arc_bytes arc_min_bytes=0 swappiness ksm_on=0
  case "$profile" in
    e)   arc_bytes=$((1024 * 1024 * 1024)); arc_min_bytes=$((512 * 1024 * 1024)); swappiness=1 ;;
    mt)  arc_bytes=$((8 * 1024 * 1024 * 1024));  swappiness=5; ksm_on=1 ;;
    vps) arc_bytes=$((16 * 1024 * 1024 * 1024)); swappiness=5 ;;
    *)   echo "ERROR: unknown profile '$profile'"; return 1 ;;
  esac

  # ---- current state ----------------------------------------------------
  local cur_max cur_size cur_swappiness
  cur_max=$(awk '$1=="c_max"{print $3}' /proc/spl/kstat/zfs/arcstats)
  cur_size=$(awk '$1=="size"{print $3}' /proc/spl/kstat/zfs/arcstats)
  cur_swappiness=$(cat /proc/sys/vm/swappiness)
  local mem_avail_mb swap_used_mb
  mem_avail_mb=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)
  swap_used_mb=$(free -m | awk '/^Swap:/{print $3}')

  local vm_swap_total_kb=0 vm_swap_max_kb=0 vm_swap_max_vmid=""
  local f vmid pid s
  for f in /var/run/qemu-server/*.pid; do
    [[ -e "$f" ]] || continue
    vmid=$(basename "$f" .pid)
    pid=$(cat "$f" 2>/dev/null) || continue
    s=$(awk '/^VmSwap:/{print $2}' "/proc/$pid/status" 2>/dev/null) || continue
    [[ -n "$s" ]] || continue
    vm_swap_total_kb=$((vm_swap_total_kb + s))
    if (( s > vm_swap_max_kb )); then vm_swap_max_kb=$s; vm_swap_max_vmid=$vmid; fi
  done

  echo "host=$host profile=$profile"
  echo "  ARC:        c_max=$((cur_max / 1024 / 1024 / 1024))G size=$((cur_size / 1024 / 1024 / 1024))G  ->  target c_max=$((arc_bytes / 1024 / 1024 / 1024))G"
  echo "  swappiness: $cur_swappiness  ->  $swappiness"
  echo "  host swap used: ${swap_used_mb}M | MemAvailable: ${mem_avail_mb}M"
  echo "  VM RAM in host swap: total=$((vm_swap_total_kb / 1024))M worst=vm${vm_swap_max_vmid}:$((vm_swap_max_kb / 1024))M"
  [[ "$ksm_on" == "1" ]] && echo "  KSM: will ensure ksmtuned enabled (MT profile)"

  if [[ "${APPLY:-0}" != "1" ]]; then
    echo "[DRY-RUN] no changes made. Re-run with APPLY=1 to apply."
    return 0
  fi

  # ---- apply: ARC cap (live + persisted) ---------------------------------
  # arc_min BEFORE arc_max, or the max write is clamped (see profile table).
  local min_note=""
  (( arc_min_bytes > 0 )) && min_note=" (arc_min=$arc_min_bytes)"
  echo "Applying zfs_arc_max=$arc_bytes$min_note"
  if (( arc_min_bytes > 0 )); then
    echo "$arc_min_bytes" > /sys/module/zfs/parameters/zfs_arc_min
  fi
  echo "$arc_bytes" > /sys/module/zfs/parameters/zfs_arc_max
  if (( arc_min_bytes > 0 )); then
    echo "options zfs zfs_arc_max=$arc_bytes zfs_arc_min=$arc_min_bytes" > /etc/modprobe.d/zfs.conf
  else
    echo "options zfs zfs_arc_max=$arc_bytes" > /etc/modprobe.d/zfs.conf
  fi
  update-initramfs -u >/dev/null 2>&1 || echo "WARNING: update-initramfs failed (live value still applied)"

  # Wait (bounded) for the ARC to shrink toward the new cap so a subsequent
  # swap drain has room. ZFS releases asynchronously under its own pacing.
  local i
  for i in $(seq 1 24); do
    cur_size=$(awk '$1=="size"{print $3}' /proc/spl/kstat/zfs/arcstats)
    (( cur_size <= arc_bytes + arc_bytes / 10 )) && break
    sleep 5
  done
  echo "  ARC size now: $((cur_size / 1024 / 1024 / 1024))G"

  # ---- apply: swappiness (live + persisted) ------------------------------
  echo "Applying vm.swappiness=$swappiness"
  sysctl -q -w vm.swappiness="$swappiness"
  echo "vm.swappiness=$swappiness" > /etc/sysctl.d/99-neuravps-memprofile.conf
  # Retire the old first_boot file so there's a single source of truth.
  rm -f /etc/sysctl.d/99-proxmox-swap.conf

  # ---- apply: KSM (MT profile only) --------------------------------------
  if [[ "$ksm_on" == "1" ]]; then
    echo "Ensuring ksmtuned is enabled (KSM dedup across ~48 identical Windows VMs)"
    systemctl enable --now ksmtuned >/dev/null 2>&1 || echo "WARNING: ksmtuned enable failed"
  fi

  # ---- optional: drain swap ----------------------------------------------
  if [[ "${DESWAP:-0}" == "1" ]]; then
    mem_avail_mb=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)
    swap_used_mb=$(free -m | awk '/^Swap:/{print $3}')
    if (( swap_used_mb == 0 )); then
      echo "Swap already empty; nothing to drain."
    elif (( mem_avail_mb > swap_used_mb + 8192 )); then
      echo "Draining swap (${swap_used_mb}M used; ${mem_avail_mb}M available)... this can take minutes"
      if swapoff -a; then
        swapon -a
        echo "  swap drained and re-enabled"
      else
        swapon -a || true
        echo "WARNING: swapoff failed (not enough memory mid-drain?); swap re-enabled"
      fi
    else
      echo "SKIP swap drain: MemAvailable(${mem_avail_mb}M) <= swap-used(${swap_used_mb}M) + 8G margin."
      echo "  Re-run later (after ARC shrink / off-peak) or drain manually."
    fi
  fi

  echo "== Done: $host profile=$profile applied =="
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

    # Send vars + function and execute it (-n so ssh does not consume the loop stdin)
    ssh -n -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ForwardAgent=yes "root@$ip" \
        "APPLY='${APPLY}' DESWAP='${DESWAP}' FORCE_PROFILE='${FORCE_PROFILE}'; $FUNC_CONTENT; remote_task" \
        || echo "❌ Failed to connect to $hostname ($ip)"
done < <(jq -r 'to_entries[] | "\(.key) \(.value)"' "$NODES_FILE" | sort)

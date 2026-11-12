#!/usr/bin/env bash

############################################################
# Migration: EX44 zvol readahead 128 KB + vm.watermark_scale_factor=100
#
# WHY (incident analysis 2026-11-11, nodes 120/5/169/99): the EX44 VPS-E guests
# hang for minutes with PSI-memory "full" at 90-99 % WITHOUT the swap growing.
# Cause: every VM disk is cache=writeback, so QEMU (io_uring) does buffered I/O
# on /dev/zdN -> host page cache. ZFS 2.4.x declares optimal_io_size = 32 MiB
# on the zvol and the kernel derives read_ahead_kb = 65536 from it. Each
# readahead asks for order-8/9 folios; with memory full and fragmented (0 free
# blocks >= order 8) every one falls into DIRECT COMPACTION inside the QEMU
# iou-wrk threads (30 s trace on node 5: 100 % of compactions came from
# blkdev_read_iter -> filemap_readahead -> page_cache_ra_order, 96 % failed).
# The guest's I/O stops, its vCPUs wait, zvol_tq/txg_sync end up in D > 122 s.
#
# WHAT:
#   * udev rule: every zvol (zd*, whole disk) gets queue/read_ahead_kb=128
#     (persists across reboot and across VM/zvol creation: "add" event)
#   * sysctl vm.watermark_scale_factor=100 (default 10): kswapd wakes earlier
#     (low ~0.7 GiB / high ~1.3 GiB on 64 GB) so the fragmented-memory cliff is
#     reached less often. Costs ~0.6 GiB of permanently free memory.
#   * applies both LIVE to the current zvols (no VM/node restart, no cache=none)
# Pilot: node 0000120 from 2026-11-11 20:07Z -> compact_stall 123k/day -> 23k/day.
#
# Does NOT touch guest RAM, balloon or cache= (cache=none needs a VM stop).
# Self-guards on hostname (*-EX44* only): a stray run elsewhere is a no-op.
# Idempotent. Revert: rm both files, `udevadm control --reload`, then
#   for d in /sys/block/zd*; do echo 65536 > $d/queue/read_ahead_kb; done
#   sysctl -w vm.watermark_scale_factor=10
#
# Run from a BASE host:  bash install-ex44-readahead.sh
#   ONLY_HOST_NUMS="5 169 167" bash install-ex44-readahead.sh   (staged rollout)
#   DRY_RUN=1 ...                                              (print, change nothing)
############################################################

############################################################
# DEFINE LOCAL FUNCTION (runs remotely)
############################################################
remote_task() {
  case "$(hostname)" in
    *-EX44*) ;;
    *) echo "skip: $(hostname) is not an EX44"; return 0 ;;
  esac
  local RULE=/etc/udev/rules.d/61-neuravps-zvol-readahead.rules
  local SYSCTL=/etc/sysctl.d/99-neuravps-ex44-watermark.conf
  local RA=128 WSF=100

  echo "== EX44 readahead/watermark ($(hostname)) DRY_RUN=${DRY_RUN:-0} =="
  echo "-- before: wsf=$(cat /proc/sys/vm/watermark_scale_factor) zvol ra: $(for d in /sys/block/zd*; do [ -e "$d/queue/read_ahead_kb" ] && echo -n "$(basename "$d")=$(cat "$d/queue/read_ahead_kb") "; done)"
  if [ "${DRY_RUN:-0}" = "1" ]; then echo "[DRY-RUN] no changes"; return 0; fi

  cat > "$RULE.new" <<RULEEOF
# NeuraVPS: zvol readahead 128 KB (ZFS 2.4 io_opt=32 MiB makes the kernel pick 64 MB,
# which drives direct compaction in QEMU I/O threads on 64 GB EX44 hosts).
ACTION=="add|change", SUBSYSTEM=="block", KERNEL=="zd*", ENV{DEVTYPE}=="disk", ATTR{queue/read_ahead_kb}="$RA"
RULEEOF
  mv "$RULE.new" "$RULE"

  printf '# NeuraVPS EX44: wake kswapd earlier (default 10) — see install-ex44-readahead.sh\nvm.watermark_scale_factor = %s\n' "$WSF" > "$SYSCTL.new"
  mv "$SYSCTL.new" "$SYSCTL"

  udevadm control --reload
  sysctl -q -p "$SYSCTL"
  # Apply live to the existing zvols. A direct write is enough and touches
  # nothing else; the udev trigger below then proves the rule itself works.
  local d
  for d in /sys/block/zd*; do
    [ -e "$d/queue/read_ahead_kb" ] && echo "$RA" > "$d/queue/read_ahead_kb"
  done
  # Prove persistence of the rule: reset one value, re-trigger, expect 128 again.
  local probe; probe=$(ls -d /sys/block/zd* 2>/dev/null | head -1)
  if [ -n "$probe" ]; then
    echo 65536 > "$probe/queue/read_ahead_kb"
    udevadm trigger --action=change --subsystem-match=block --sysname-match='zd*'
    udevadm settle --timeout=30
  fi

  local bad=0
  for d in /sys/block/zd*; do
    [ -e "$d/queue/read_ahead_kb" ] || continue
    [ "$(cat "$d/queue/read_ahead_kb")" = "$RA" ] || { echo "❌ $(basename "$d") ra=$(cat "$d/queue/read_ahead_kb")"; bad=1; }
  done
  [ "$(cat /proc/sys/vm/watermark_scale_factor)" = "$WSF" ] || { echo "❌ wsf=$(cat /proc/sys/vm/watermark_scale_factor)"; bad=1; }
  echo "-- after: wsf=$(cat /proc/sys/vm/watermark_scale_factor) zvol ra: $(for d in /sys/block/zd*; do [ -e "$d/queue/read_ahead_kb" ] && echo -n "$(basename "$d")=$(cat "$d/queue/read_ahead_kb") "; done)"
  if [ "$bad" = "0" ]; then echo "== OK =="; else echo "== FAILED =="; return 1; fi
}
############################################################

FUNC_CONTENT=$(declare -f remote_task)

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

# Space-separated host numbers, e.g. ONLY_HOST_NUMS="5 169". Empty = all EX44.
read -r -a ONLY_HOST_NUMS <<< "${ONLY_HOST_NUMS:-}"

while read -r hostname ip; do
    if [[ "$hostname" != *EX44* ]]; then continue; fi
    host_num=$((10#${hostname%%-*}))
    if (( ${#ONLY_HOST_NUMS[@]} > 0 )); then
        in_only=0
        for o in "${ONLY_HOST_NUMS[@]}"; do
            if [[ $host_num -eq $o ]]; then in_only=1; break; fi
        done
        if [[ $in_only -eq 0 ]]; then continue; fi
    fi
    echo "------------------------------------------------"
    echo "Connecting to $hostname ($ip)"
    ssh -n -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 "root@$ip" \
        "DRY_RUN=${DRY_RUN:-0}; $FUNC_CONTENT; remote_task" \
        || echo "❌ Failed on $hostname ($ip)"
done < <(node_list "$NODES_FILE" | sort)

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
#   profile e    (*-EX44, 1 dedicated VM):  zfs_arc_max=1G (arc_min=0.5G), swappiness=1
#   profile mt   (*-AX102 y *-AX102-U, 38-48 MT VMs): zfs_arc_max=8G, swappiness=100, ksmtuned ON
#   profile ad   (*-AX162*, A-D shared, identical Win+SQX): zfs_arc_max=16G, swappiness=100, ksmtuned ON
#   profile r470 (*-R470*, 172t/503G deep-overcommit shared): zfs_arc_max=16G, swappiness=100, ksmtuned ON
#   profile vps  (catch-all / unknown class):       zfs_arc_max=16G, swappiness=5
#   ALL profiles: zswap ON (zstd->lz4->lzo, zsmalloc, max_pool_percent=20,
#   persisted via neuravps-zswap.service) + vm.page-cluster=0.
# (2026-06-12: AX102-U reclassified vps->mt — live audit found all 9 nodes
#  host exclusively 38-48 x 4096 MiB MT VMs; mt profile applied fleet-wide.)
# (2026-06-13: AX162 split vps->ad — KSM pilot proved A-D dedups like MT
#  (96/174 self-drained 60-72G); ksmtuned rolled out to all 39, run=1.)
# (2026-07-02 v2: RAM-pressure retune for the overcommit era — fleet survey
#  found 4.4 TB of guest RAM in disk swap with zswap OFF on all 201 nodes.
#  zswap turns most of that latency into in-RAM decompression (Windows/JVM
#  pages compress 2-3x) and shields the data NVMe from swap I/O (R470 swap
#  shares the raid10 disks). swappiness 5->100 on multi-VM classes so cold
#  pages trickle into the compressed pool EARLY instead of burst-swapping at
#  allocation spikes (the 0000127 exhaustion-freeze shape). page-cluster=0
#  kills swap readahead (pointless for zswap/NVMe, lowers page-in latency).
#  New r470 profile: KSM ON — same identical-Windows dedup as ad, measured
#  ~80 GB/node on AX162; R470s were stuck on the conservative vps profile.
#  e/vps keep their low swappiness deliberately (dedicated/unknown class).)
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
      *-EX44*)    profile="e"    ;;
      *-AX102-U*) profile="mt"   ;;
      *-AX102*)   profile="mt"   ;;
      *-AX162*)   profile="ad"   ;;
      *-R470*)    profile="r470" ;;
      *)          profile="vps"  ;;
    esac
  fi

  # Profile e also lowers zfs_arc_min: the default is RAM/32 (= 2 GiB on a
  # 64 GB EX44) and ZFS silently CLAMPS c_max to arc_min — writing a 1 GiB
  # cap alone leaves the ARC at 2 GiB with no error. Always write min first.
  # (2026-06-12 retune: 1 GiB ARC + memory=61440 VM frees the EX44 budget;
  # measured cold-swap floor drops 2-4.5 GB -> 0.54 GB, Windows pagefile 0.)
  # swappiness=100 on multi-VM overcommitted classes is deliberate and paired
  # with zswap: cold guest pages compress into the in-RAM pool early, keeping
  # free headroom for allocation spikes. e (dedicated single-VM) and vps
  # (unknown) stay conservative.
  local arc_bytes arc_min_bytes=0 swappiness ksm_on=0
  case "$profile" in
    e)    arc_bytes=$((1024 * 1024 * 1024)); arc_min_bytes=$((512 * 1024 * 1024)); swappiness=1 ;;
    mt)   arc_bytes=$((8 * 1024 * 1024 * 1024));  swappiness=100; ksm_on=1 ;;
    ad)   arc_bytes=$((16 * 1024 * 1024 * 1024)); swappiness=100; ksm_on=1 ;;
    r470) arc_bytes=$((16 * 1024 * 1024 * 1024)); swappiness=100; ksm_on=1 ;;
    vps)  arc_bytes=$((16 * 1024 * 1024 * 1024)); swappiness=5 ;;
    *)    echo "ERROR: unknown profile '$profile'"; return 1 ;;
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

  local cur_zswap cur_ksm_run cur_ksm_sharing_g
  cur_zswap=$(cat /sys/module/zswap/parameters/enabled 2>/dev/null || echo '?')
  cur_ksm_run=$(cat /sys/kernel/mm/ksm/run 2>/dev/null || echo '?')
  # pages_sharing * 4 KiB -> GiB == pages / 262144 (mawk-safe: no ** operator)
  cur_ksm_sharing_g=$(awk '{printf "%.1f", $1 / 262144}' /sys/kernel/mm/ksm/pages_sharing 2>/dev/null || echo '?')

  echo "host=$host profile=$profile"
  echo "  ARC:        c_max=$((cur_max / 1024 / 1024 / 1024))G size=$((cur_size / 1024 / 1024 / 1024))G  ->  target c_max=$((arc_bytes / 1024 / 1024 / 1024))G"
  echo "  swappiness: $cur_swappiness  ->  $swappiness"
  echo "  zswap:      enabled=$cur_zswap  ->  Y (pool 20%)"
  echo "  KSM:        run=$cur_ksm_run sharing=${cur_ksm_sharing_g}G  ->  $( [[ "$ksm_on" == "1" ]] && echo 'ksmtuned ON' || echo 'off' )"
  echo "  host swap used: ${swap_used_mb}M | MemAvailable: ${mem_avail_mb}M"
  echo "  VM RAM in host swap: total=$((vm_swap_total_kb / 1024))M worst=vm${vm_swap_max_vmid}:$((vm_swap_max_kb / 1024))M"

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
  local zfs_conf_line
  if (( arc_min_bytes > 0 )); then
    zfs_conf_line="options zfs zfs_arc_max=$arc_bytes zfs_arc_min=$arc_min_bytes"
  else
    zfs_conf_line="options zfs zfs_arc_max=$arc_bytes"
  fi
  # Idempotence: only rewrite + regenerate the initramfs when the persisted
  # line actually changes (update-initramfs is ~30-60s; re-runs would pay it
  # on every node for nothing).
  if [[ "$(cat /etc/modprobe.d/zfs.conf 2>/dev/null)" != "$zfs_conf_line" ]]; then
    echo "$zfs_conf_line" > /etc/modprobe.d/zfs.conf
    update-initramfs -u >/dev/null 2>&1 || echo "WARNING: update-initramfs failed (live value still applied)"
  else
    echo "  modprobe.d/zfs.conf already correct; skipping update-initramfs"
  fi

  # Wait (bounded) for the ARC to shrink toward the new cap so a subsequent
  # swap drain has room. ZFS releases asynchronously under its own pacing.
  local i
  for i in $(seq 1 24); do
    cur_size=$(awk '$1=="size"{print $3}' /proc/spl/kstat/zfs/arcstats)
    (( cur_size <= arc_bytes + arc_bytes / 10 )) && break
    sleep 5
  done
  echo "  ARC size now: $((cur_size / 1024 / 1024 / 1024))G"

  # ---- apply: swappiness + page-cluster (live + persisted) ----------------
  # page-cluster=0 disables swap readahead: with zswap most page-ins are RAM
  # decompression and the tail lands on NVMe — clustered readahead only adds
  # latency and decompresses pages nobody asked for.
  echo "Applying vm.swappiness=$swappiness vm.page-cluster=0"
  sysctl -q -w vm.swappiness="$swappiness" vm.page-cluster=0
  printf 'vm.swappiness=%s\nvm.page-cluster=0\n' "$swappiness" > /etc/sysctl.d/99-neuravps-memprofile.conf
  # Retire the old first_boot file so there's a single source of truth.
  rm -f /etc/sysctl.d/99-proxmox-swap.conf

  # ---- apply: zswap (ALL profiles; live + boot-persistent via unit) -------
  # zswap is built into the kernel (not a module), so its params can't be
  # persisted via modprobe.d — a tiny oneshot service re-applies them each
  # boot. Compressor prefers zstd (best ratio; Windows/JVM guest pages
  # compress 2-3x), falls back lz4 then lzo depending on kernel. Only NEW
  # swap-outs flow through zswap; already-swapped pages stay on disk until
  # next touch, so enabling is disturbance-free on a live node.
  echo "Applying zswap (zstd->lz4->lzo, zsmalloc, max_pool_percent=20)"
  cat > /usr/local/sbin/neuravps-zswap.sh <<'ZSWAPEOF'
#!/bin/bash
# NeuraVPS: enable zswap (compressed in-RAM swap cache). Run at boot by
# neuravps-zswap.service; safe to re-run any time. See
# run_remotes/tune_memory_profile.sh (v2) for the why.
P=/sys/module/zswap/parameters
[ -d "$P" ] || exit 0
modprobe zsmalloc 2>/dev/null || true
for c in zstd lz4 lzo; do
  modprobe "$c" 2>/dev/null || true
  echo "$c" > "$P/compressor" 2>/dev/null && break
done
echo zsmalloc > "$P/zpool" 2>/dev/null || echo zbud > "$P/zpool" 2>/dev/null || true
echo 20 > "$P/max_pool_percent" 2>/dev/null
echo Y > "$P/enabled"
ZSWAPEOF
  chmod +x /usr/local/sbin/neuravps-zswap.sh
  cat > /etc/systemd/system/neuravps-zswap.service <<'UNITEOF'
[Unit]
Description=NeuraVPS: enable zswap (compressed swap cache)

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/neuravps-zswap.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNITEOF
  systemctl daemon-reload
  systemctl enable --now neuravps-zswap.service >/dev/null 2>&1 || /usr/local/sbin/neuravps-zswap.sh
  echo "  zswap: enabled=$(cat /sys/module/zswap/parameters/enabled 2>/dev/null) compressor=$(cat /sys/module/zswap/parameters/compressor 2>/dev/null) zpool=$(cat /sys/module/zswap/parameters/zpool 2>/dev/null)"

  # ---- apply: KSM (mt/ad/r470 profiles enable; e/vps disable) -------------
  if [[ "$ksm_on" == "1" ]]; then
    echo "Ensuring ksmtuned is enabled (KSM dedup across identical Windows VMs)"
    # THRES_COEF=85: the default 20 never triggers when VM RAM sits in swap
    # (low qemu RSS -> run=0 deadlock, found live 2026-06-12). Set + restart.
    [[ -x /usr/sbin/ksmtuned ]] || DEBIAN_FRONTEND=noninteractive apt-get install -y ksm-control-daemon >/dev/null 2>&1 || echo "WARNING: ksm-control-daemon install failed"
    sed -i "/^#* *KSM_THRES_COEF=/d; /^#* *KSM_NPAGES_MAX=/d" /etc/ksmtuned.conf
    printf "KSM_THRES_COEF=85\nKSM_NPAGES_MAX=2500\n" >> /etc/ksmtuned.conf
    systemctl enable --now ksmtuned >/dev/null 2>&1 || echo "WARNING: ksmtuned enable failed"
    systemctl restart ksmtuned >/dev/null 2>&1 || true
  else
    # e (dedicated VPS-E) / vps (catch-all): actively disable ksmtuned so it
    # can't auto-toggle KSM under pressure and burn dedicated cores on pointless
    # single-VM dedup (found 49/124 EX44 running KSM 2026-06-13). run=0 stops
    # scanning without unmerging (no spike); residual merges COW-split naturally.
    echo "Disabling ksmtuned (KSM off for $profile profile)"
    systemctl disable --now ksmtuned >/dev/null 2>&1 || true
    echo 0 > /sys/kernel/mm/ksm/run 2>/dev/null || true
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
# Overridable from the environment (handy for pilots: HOST_NUM_GTE=197 HOST_NUM_LTE=200 APPLY=1 ...).
HOST_NUM_GTE="${HOST_NUM_GTE:-}"
HOST_NUM_LTE="${HOST_NUM_LTE:-}"

# PARALLEL=N runs up to N nodes concurrently (default 1 = original serial
# behavior). Each node's output is captured and printed as one atomic block so
# parallel runs stay readable. The per-node work is host-local (sysfs/sysctl/
# systemd), so concurrency across nodes is safe.
PARALLEL="${PARALLEL:-1}"

_run_one_node() {
    local hostname="$1" ip="$2" out rc
    out=$(ssh -n -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 -o ForwardAgent=yes "root@$ip" \
        "APPLY='${APPLY}' DESWAP='${DESWAP}' FORCE_PROFILE='${FORCE_PROFILE}'; $FUNC_CONTENT; remote_task" 2>&1)
    rc=$?
    printf -- '------------------------------------------------\n%s (%s)\n%s\n' "$hostname" "$ip" "$out"
    (( rc != 0 )) && printf '❌ Failed on %s (%s) rc=%s\n' "$hostname" "$ip" "$rc"
    return 0
}
export -f _run_one_node
export FUNC_CONTENT APPLY DESWAP FORCE_PROFILE

FILTERED_LIST=$(mktemp)
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
    printf '%s %s\n' "$hostname" "$ip" >> "$FILTERED_LIST"
done < <(jq -r 'to_entries[] | "\(.key) \(.value)"' "$NODES_FILE" | sort)

echo "Nodes to process: $(wc -l < "$FILTERED_LIST") (PARALLEL=$PARALLEL)"
xargs -a "$FILTERED_LIST" -P "$PARALLEL" -n 2 bash -c '_run_one_node "$@"' _
rm -f "$FILTERED_LIST"

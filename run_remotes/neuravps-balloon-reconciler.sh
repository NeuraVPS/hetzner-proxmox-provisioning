#!/bin/bash
# neuravps-balloon-reconciler — "RAM follows activity" for over-committed SQX
# (AX162) nodes. PVE's autoballooning distributes RAM by shares, BLIND to guest
# distress: a guest squeezed below its working set pages to its OWN pagefile
# (zvol I/O — invisible to host PSI/swap; the pressure behind the VM 201
# 0x1A reset, 2026-07-11). This closes the loop:
#   * every run (1-min systemd timer) sample each ballooning VM's virtio-balloon
#     major_page_faults counter -> SUSTAINED faults/s since the previous run
#   * guest thrashing  -> RAISE its balloon floor (qm set --balloon) in +4G
#     steps up to its configured max — IF the node has budget:
#       sum(all floors)+step <= 60% node RAM  AND  MemAvailable >= 12G margin
#   * guest idle >= 30 consecutive runs -> lower the floor -4G/run back toward
#     its ORIGINAL (plan) floor, never below.
# Original floors persist in /var/lib/neuravps-balloon/floors.json. A VM that
# arrives already-bumped (migrated in) with no record: original is estimated as
# min(current_floor, 30% of max) — observed plan floors are ~30% of max.
# Per-run sustained rates are exported to /var/run/neuravps-balloon-rates.tsv
# for the node_health @@GUESTPAGING probe (sustained > point-sample).
# Per-run STATUS is exported to /var/run/neuravps-balloon-status.json for the
# fleet thrash-relief pass (defrag --relief on the base): floors/budget plus
# the VMs whose blocked LEAKY counter reached BLOCKED_RUNS_MIN: +1 per
# thrashing-with-NO-BUDGET run, -1 (decay, not reset) per calm run. This
# catches both SUSTAINED blockage (~10 min) and OSCILLATING thrashers (bursts
# every few minutes on a starved node — vm 808/1892 pattern, 2026-07-17 —
# which a consecutive counter never catches), while one-off blips (a
# neighbour's idle-lower about to free budget) decay away. Hard reset only on
# a successful raise / floor==max (local automation worked or is exhausted
# entitlement-side — only moving a VM off this node can help otherwise).
# Config knobs via /etc/default/neuravps-balloon-reconciler.
#NBRVER=4
set -u
RAISE_FAULTS_PS=${RAISE_FAULTS_PS:-100}   # sustained faults/s to trigger a raise
IDLE_FAULTS_PS=${IDLE_FAULTS_PS:-5}       # below this counts as idle
IDLE_RUNS_TO_LOWER=${IDLE_RUNS_TO_LOWER:-30}  # ~30 min idle before lowering
STEP_MB=${STEP_MB:-4096}
FLOOR_BUDGET_PCT=${FLOOR_BUDGET_PCT:-60}  # sum(floors) cap as % of node RAM
HOST_FREE_MIN_MB=${HOST_FREE_MIN_MB:-12288}
BLOCKED_RUNS_MIN=${BLOCKED_RUNS_MIN:-10}  # leaky blocked-counter level (+1 blocked/-1 calm run) that asks for relief
[ -f /etc/default/neuravps-balloon-reconciler ] && . /etc/default/neuravps-balloon-reconciler

STATE_DIR=/var/lib/neuravps-balloon
RUN_RATES=/var/run/neuravps-balloon-rates.tsv
RUN_STATUS=/var/run/neuravps-balloon-status.json
mkdir -p "$STATE_DIR"
FLOORS="$STATE_DIR/floors.json"; [ -f "$FLOORS" ] || echo '{}' > "$FLOORS"
SAMPLES="$STATE_DIR/samples.tsv"   # vmid<TAB>faults<TAB>epoch<TAB>idle_runs<TAB>blocked_runs
touch "$SAMPLES"

ram_mb=$(( $(awk '/MemTotal/{print $2}' /proc/meminfo) / 1024 ))
avail_mb=$(( $(awk '/MemAvailable/{print $2}' /proc/meminfo) / 1024 ))
now=$(date +%s)

write_status() {  # $1 = blocked-entries JSON fragment ("" when none)
  local tmp
  tmp=$(mktemp /var/run/.neuravps-balloon-status.XXXXXX) || return 0
  chmod 644 "$tmp"
  printf '{"ts":%s,"ram_mb":%s,"avail_mb":%s,"committed_mb":%s,"floors_sum_mb":%s,"budget_mb":%s,"blocked":[%s]}\n' \
    "$now" "$ram_mb" "$avail_mb" "${committed:-0}" "${floors_sum:-0}" "${budget_mb:-0}" "$1" > "$tmp"
  mv "$tmp" "$RUN_STATUS"
}

# --- collect running ballooning VMs: vmid|floor|max ---
declare -A CUR_FLOOR CUR_MAX
committed=0; floors_sum=0
for pf in /var/run/qemu-server/*.pid; do
  [ -e "$pf" ] || continue
  v=$(basename "$pf" .pid); conf="/etc/pve/qemu-server/$v.conf"
  [ -f "$conf" ] || continue
  mx=$(awk -F': ' '/^memory:/{print $2;exit}' "$conf"); mx=${mx:-0}
  fl=$(awk -F': ' '/^balloon:/{print $2;exit}' "$conf"); fl=${fl:-0}
  committed=$(( committed + mx ))
  [ "$fl" -gt 0 ] || continue   # balloon:0 = ballooning disabled
  # NB: fl==mx stays managed (a VM we raised to max must lower again later);
  # deliberately-fixed VMs are protected by the floors.json-only LOWER rule.
  CUR_FLOOR[$v]=$fl; CUR_MAX[$v]=$mx
  floors_sum=$(( floors_sum + fl ))
done
budget_mb=$(( ram_mb * FLOOR_BUDGET_PCT / 100 ))
# not over-committed -> nothing can squeeze; still export empty rates/status + exit
if [ "$committed" -le $(( ram_mb * 105 / 100 )) ] || [ "${#CUR_FLOOR[@]}" -eq 0 ]; then
  : > "$RUN_RATES"; write_status ""; exit 0
fi

# --- previous samples ---
declare -A P_F P_T P_IDLE P_BLOCKED
while IFS=$'\t' read -r v f t i b; do
  [ -n "${v:-}" ] || continue
  P_F[$v]=$f; P_T[$v]=$t; P_IDLE[$v]=${i:-0}; P_BLOCKED[$v]=${b:-0}
done < "$SAMPLES"
# temps in the SAME dir as their target: mv is then an atomic rename (a /tmp
# mktemp crosses filesystems -> copy+rm -> readers can catch a half/empty file)
tmp_samples=$(mktemp "$STATE_DIR/.samples.XXXXXX")
tmp_rates=$(mktemp /var/run/.neuravps-balloon-rates.XXXXXX)
chmod 644 "$tmp_rates"
blocked_json=""

for v in "${!CUR_FLOOR[@]}"; do
  f1=$(printf 'info balloon\n' | timeout 3 qm monitor "$v" 2>/dev/null | tr -d '\r' \
        | grep -oE 'major_page_faults=[0-9]+' | cut -d= -f2)
  [ -n "$f1" ] || { # QMP hiccup: carry forward prior sample unchanged
    [ -n "${P_F[$v]:-}" ] && printf '%s\t%s\t%s\t%s\t%s\n' "$v" "${P_F[$v]}" "${P_T[$v]}" "${P_IDLE[$v]:-0}" "${P_BLOCKED[$v]:-0}" >> "$tmp_samples"
    continue; }
  rate=-1
  if [ -n "${P_F[$v]:-}" ] && [ -n "${P_T[$v]:-}" ] && [ "$now" -gt "${P_T[$v]}" ] && [ "$f1" -ge "${P_F[$v]}" ]; then
    rate=$(( (f1 - P_F[$v]) / (now - P_T[$v]) ))
  fi
  idle=${P_IDLE[$v]:-0}
  blocked=${P_BLOCKED[$v]:-0}
  if [ "$rate" -ge 0 ]; then
    printf '%s\t%s\n' "$v" "$rate" >> "$tmp_rates"
    if [ "$rate" -ge "$RAISE_FAULTS_PS" ]; then
      idle=0
      fl=${CUR_FLOOR[$v]}; mx=${CUR_MAX[$v]}
      new=$(( fl + STEP_MB )); [ "$new" -gt "$mx" ] && new=$mx
      if [ "$new" -gt "$fl" ]; then
        if [ $(( floors_sum + new - fl )) -le "$budget_mb" ] && [ "$avail_mb" -ge "$HOST_FREE_MIN_MB" ]; then
          # remember the ORIGINAL floor before our first touch
          python3 - "$FLOORS" "$v" "$fl" <<'PY'
import json,sys
p,v,fl=sys.argv[1],sys.argv[2],int(sys.argv[3])
d=json.load(open(p))
if v not in d: d[v]=fl; json.dump(d,open(p,"w"))
PY
          if qm set "$v" --balloon "$new" >/dev/null 2>&1; then
            floors_sum=$(( floors_sum + new - fl ))
            blocked=0
            logger -t neuravps-balloon " RAISE vm $v floor ${fl}->${new}MB (thrash ${rate}/s; floors_sum=${floors_sum}MB budget=${budget_mb}MB)"
          fi
        else
          blocked=$(( blocked + 1 ))
          logger -t neuravps-balloon " vm $v thrashing ${rate}/s but NO BUDGET (floors_sum=${floors_sum}+${STEP_MB}>${budget_mb}MB or avail=${avail_mb}MB<${HOST_FREE_MIN_MB}; blocked level ${blocked}) — placement should relieve this node"
        fi
      else
        blocked=0   # floor already at max: nothing placement can add (entitlement case)
      fi
    elif [ "$rate" -lt "$IDLE_FAULTS_PS" ]; then
      [ "$blocked" -gt 0 ] && blocked=$(( blocked - 1 ))   # decay, not reset (oscillators)
      idle=$(( idle + 1 ))
      if [ "$idle" -ge "$IDLE_RUNS_TO_LOWER" ]; then
        fl=${CUR_FLOOR[$v]}; mx=${CUR_MAX[$v]}
        orig=$(python3 - "$FLOORS" "$v" "$mx" "$fl" <<'PY'
import json,sys
d=json.load(open(sys.argv[1])); v=sys.argv[2]; mx=int(sys.argv[3]); fl=int(sys.argv[4])
print(d.get(v, ""))
PY
)
        # only lower what WE raised: no floors.json entry -> hands off
        if [ -n "$orig" ] && [ "$fl" -gt "$orig" ]; then
          new=$(( fl - STEP_MB )); [ "$new" -lt "$orig" ] && new=$orig
          if qm set "$v" --balloon "$new" >/dev/null 2>&1; then
            floors_sum=$(( floors_sum - fl + new ))
            logger -t neuravps-balloon " LOWER vm $v floor ${fl}->${new}MB (idle ${idle} runs; original=${orig}MB)"
          fi
        fi
      fi
    else
      idle=0
      [ "$blocked" -gt 0 ] && blocked=$(( blocked - 1 ))   # decay, not reset (oscillators)
    fi
  fi
  # emit on LEVEL, not on this-run state: an oscillator at level >= MIN must be
  # visible to the hourly relief tick even if this exact minute was calm
  if [ "$blocked" -ge "$BLOCKED_RUNS_MIN" ]; then
    [ -n "$blocked_json" ] && blocked_json="${blocked_json},"
    blocked_json="${blocked_json}{\"vmid\":${v},\"rate\":${rate:--1},\"floor_mb\":${CUR_FLOOR[$v]},\"max_mb\":${CUR_MAX[$v]},\"blocked_runs\":${blocked}}"
  fi
  printf '%s\t%s\t%s\t%s\t%s\n' "$v" "$f1" "$now" "$idle" "$blocked" >> "$tmp_samples"
done
mv "$tmp_samples" "$SAMPLES"
mv "$tmp_rates" "$RUN_RATES"
write_status "$blocked_json"
exit 0

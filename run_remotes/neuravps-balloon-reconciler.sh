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
#   * guest calm -> LEAKY idle credit: +1 per calm run, -1 per mildly-busy
#     run, hard reset to 0 ONLY on a thrashing (>= RAISE) run. At >= 30
#     credit, lower the floor -4G toward its ORIGINAL (plan) floor, never
#     below, then hold LOWER_COOLDOWN_RUNS calm runs before the next step.
#
#     v5 (2026-07-18) made that credit leaky. v4 required 30 CONSECUTIVE calm
#     runs and reset to 0 on ANY run >= IDLE_FAULTS_PS (5/s -- barely above
#     noise), so floors only ever ratcheted UP. Measured fleet-wide before the
#     change: 92 VMs still pinned above their original floor holding 976 GB,
#     20 of them at their configured max; those raised VMs run >= 5 faults/s
#     in 21.8% of minutes (vs 4.1% fleet-wide -- the reconciler raises exactly
#     the busy ones), so P(30 consecutive calm) = 0.06% per attempt. They
#     could never give the memory back. Raising the threshold alone does not
#     fix it (10/s -> 1.05%, 25/s -> 2.53%); dropping the "consecutive"
#     requirement does: at 21.8% busy the credit drifts +0.56/run, reaching 30
#     in ~53 min. Same leak the `blocked` counter got in v4, same reason.
#     Walk-back is deliberately PACED (cooldown) rather than 4G every calm
#     run: a guest that just went quiet may still have a large working set
#     (SQX between backtests), so we probe downward slowly and let the raise
#     path -- which reacts in ~1 min -- veto. Slow-decrease/fast-increase.
#
#     v6 (2026-07-19) TURNED THE WALK-BACK OFF. 24h on a 4-node canary: it does
#     not work, and the premise behind it was wrong.
#       * It did not converge. LOWER 216 vs RAISE 223 over 24h, 21 VMs both
#         lowered AND raised (vm 1347: 28/25, vm 219: 28/27). Net floors HELD
#         went UP, 183 -> 198 GB. A treadmill, not a give-back.
#       * The premise was wrong. The 976 GB above "original" is not memory the
#         reconciler forgot to return -- it is a LEARNED working set. Those
#         floors were raised because the plan floor was already proven too
#         small. `orig` is therefore the one level we know is inadequate, and
#         it is exactly where the walk-back aims. v4's ratchet was not purely
#         a bug: it was accumulating a measurement.
#       * And it is not symmetric, which is what makes it unsafe. Lowering is
#         unconditional; RAISING NEEDS FLOOR BUDGET. On a floor-starved node
#         there is none, so the walk-back can strip memory it can never give
#         back. Observed on 0000053: vm 1347 walked 23.5 -> 9.5 GB in 4 steps
#         over 17 min, then thrashed at 129-2824 faults/s with "NO BUDGET",
#         blocked level climbing to 4. The guest was left in distress by the
#         mechanism that was supposed to be free. 7 VMs, 45.3 GB stripped.
#     Anything that revisits this needs a per-VM floor-of-the-floor that
#     REMEMBERS a bounce and backs off (exponentially), not a fixed cooldown --
#     and it must refuse to lower at all on a node without the budget to undo
#     it. The real win stays what phase 1.5/relief already do: move VMs off
#     starved nodes. Packing was never the bottleneck; see the memory note.
#
#     v7 (2026-08-16) IS that revisit, built exactly on the v6 autopsy. Three
#     changes, each answering one failure mode:
#       * FLOOR-OF-THE-FLOOR: the walk-back no longer aims at `orig` (the plan
#         ram_min at creation -- the one level PROVEN inadequate). It aims at
#         the plan's measured young-settle floor (pricingPlans
#         ram_min_observed, the same number placement reserves): a 19 GB
#         vps-a decays to 9.7 GB, not 5.7; a 48 GB vps-e to 46 -- barely any
#         decay, correct, they use it. Mapped from the VM's configured max.
#       * BOUNCE MEMORY: a RAISE landing within REBOUND_WINDOW_S of our own
#         LOWER on that VM is a bounce -- we probed below its real working
#         set. Bounces are counted per VM and block further lowering for
#         BACKOFF_BASE_S * 2^(bounces-1), capped. vm 1347 (28 lowers / 25
#         raises in 24h under v5) becomes: one bounce, 48h hands-off.
#       * BUDGET-GATED: never START a lower on a node that could not give the
#         step back (budget headroom < STEP_MB or host below HOST_FREE_MIN).
#         The 0.80 sales gate vs 0.90 reconciler ceiling band means released
#         budget can be resold only down to 0.80 -- the last 0.10 stays
#         reconciler-only, so the give-back path structurally exists.
#     Timing also reshaped for the SQX-between-backtests gap that burned v5:
#     first step needs 12h of NET idle credit (leaky, was 30 min) and steps
#     pace 1h apart -- a vacation VM fully decays in ~15h, a backtest pause
#     of 2-4h never earns the credit. The return path is no longer pain-
#     driven either: the welcome-boost daemon restores the plan floor on RDP
#     login (~30 s) and the boot RAM guard covers restarts, so decay's cost
#     when we get it wrong is minutes of sluggishness, self-corrected by the
#     ~1-min RAISE path plus a bounce block.
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
#NBRVER=7
set -u
# v7 (2026-08-16): the walk-back exists again (see header) but stays OFF BY
# DEFAULT until the 2-node canary (0000008/0000054) proves it converges.
# first_boot.sh curls THIS FILE from master onto every new node, so the
# default has to be the safe one; set LOWER_ENABLED=1 in
# /etc/default/neuravps-balloon-reconciler per node to enable.
LOWER_ENABLED=${LOWER_ENABLED:-0}
RAISE_FAULTS_PS=${RAISE_FAULTS_PS:-100}   # sustained faults/s to trigger a raise
IDLE_FAULTS_PS=${IDLE_FAULTS_PS:-5}       # below this counts as idle
IDLE_RUNS_TO_LOWER=${IDLE_RUNS_TO_LOWER:-720}  # idle CREDIT (leaky) before the FIRST
# lower: 12h of NET idle. v5's 30 min was inside the normal gap between SQX
# backtests, so it kept probing working sets that were coming right back.
LOWER_COOLDOWN_RUNS=${LOWER_COOLDOWN_RUNS:-60}  # calm runs to re-earn between -4G steps (1h)
REBOUND_WINDOW_S=${REBOUND_WINDOW_S:-86400}   # RAISE within this of our LOWER = bounce
BACKOFF_BASE_S=${BACKOFF_BASE_S:-172800}      # lower-block after 1st bounce (48h), x2 each
BACKOFF_MAX_S=${BACKOFF_MAX_S:-1209600}       # bounce-block cap (14d)
FLOOR_TARGET_FALLBACK_PCT=${FLOOR_TARGET_FALLBACK_PCT:-60}  # unknown plan size -> % of max
STEP_MB=${STEP_MB:-4096}
FLOOR_BUDGET_PCT=${FLOOR_BUDGET_PCT:-90}  # sum(floors) cap as % of node RAM
# 90 since 2026-08-16. The 07-29 note below called 80 "the last notch", but that
# was PAPER saturation, not physical: the 08-16 live sweep (86 AX162, 0 fails)
# measured 40% of physical RAM idle (8.6 TB avail, median 85 GB/node), KSM
# deduplicating 6.25 TB (~73 GB/node), zswap at 2.25x and memory PSI 0.00
# fleet-wide; nodes 186/190/193 have run for months at 1.31-1.39x floors over
# physical RAM without pressure. Floors are balloon targets, not consumption —
# the real guard is HOST_FREE_MIN_MB (unchanged). Rolled to all 86 AX162 via
# /etc/default on 2026-08-16, together with defrag FLOOR 0.90 and the sales
# gate capacityGates 0.80.
# History below kept for the shape of previous raises:
# 80 since 2026-07-29 (60 -> 70 on the 28th, 70 -> 80 the next day). At 60 the cap was refusing customers RAM they
# already pay for on nodes with ~50 GB of physically FREE memory: 5 of 56 SQX
# nodes were budget-blocked with 251 GB idle between them, and the hourly relief
# escalated "no destination" because every candidate node was equally capped
# (13 nodes >=95% of budget, 23 more at 85-95%). Guiding case: 0000056 vm1835, a
# vps-d pinned at 14,848 MB of its contracted 35,840 MB, thrashing while its host
# sat on 50 GB free.
# Safe because the REAL starvation guard is HOST_FREE_MIN_MB below (12 GB), which
# is unchanged and independent: the reconciler still refuses to raise a floor
# when the host is genuinely short. The 60% was a conservative proxy in front of
# a gate that already exists. Rolled out fleet-wide via
# /etc/default/neuravps-balloon-reconciler (sourced by v4/v5/v6 alike, so it did
# not require redeploying the script); after the change no node in the fleet has
# a blocked guest and floors sit at 81% of the new budget.
# 2026-07-29: the fleet absorbed the whole 60->70 raise in ONE DAY — 37 of 56
# nodes reached >=95% of the new ceiling and the best destination in the entire
# fleet fell 1.1 GB short of accepting the smallest plan that exists, so relief
# escalated "no destination" hourly with real customers thrashing (0000204
# vm1868 2h at up to 3023 faults/s, 0000056 vm1835 pinned at 41% of its
# contracted RAM, 0000054 vm492). Simulated before applying: if every floor grew
# to the new 80% ceiling — a worst case that cannot actually happen — only 3 of
# 56 nodes would approach HOST_FREE_MIN_MB, and that guard is what stops them.
# NOTE: this is NOT auto_provision's SQX_FLOOR_BUDGET_FACTOR (0.80 since
# 2026-08-16), which
# gates how many VMs are SOLD per node. Two knobs on purpose — raising the sales
# gate would spend this headroom on new VMs and bring the thrashing back.
# And defrag.py's FLOOR must move WITH this one: it measures destination room
# against the same ceiling, and leaving them out of step broke relief on the 28th.
HOST_FREE_MIN_MB=${HOST_FREE_MIN_MB:-12288}
BLOCKED_RUNS_MIN=${BLOCKED_RUNS_MIN:-10}  # leaky blocked-counter level (+1 blocked/-1 calm run) that asks for relief
[ -f /etc/default/neuravps-balloon-reconciler ] && . /etc/default/neuravps-balloon-reconciler

STATE_DIR=/var/lib/neuravps-balloon
RUN_RATES=/var/run/neuravps-balloon-rates.tsv
RUN_STATUS=/var/run/neuravps-balloon-status.json
mkdir -p "$STATE_DIR"
FLOORS="$STATE_DIR/floors.json"; [ -f "$FLOORS" ] || echo '{}' > "$FLOORS"
# vmid<TAB>faults<TAB>epoch<TAB>idle_runs<TAB>blocked_runs<TAB>last_lower_epoch<TAB>bounces<TAB>lower_block_until
# (columns 6-8 are v7's bounce memory; a v6 file reads fine, they default 0)
SAMPLES="$STATE_DIR/samples.tsv"
touch "$SAMPLES"

# v7: walk-back target for a VM — the plan's measured young-settle floor
# (pricingPlans ram_min_observed, the same number placement reserves), mapped
# from the configured max; never below the recorded original. `orig` (plan
# ram_min at creation) is the level PROVEN inadequate — see the v6 autopsy.
floor_target() {  # $1 = max MB, $2 = orig MB -> echoes target MB
  local mx=$1 orig=$2 t
  case "$mx" in
    16384) t=8397  ;;   # vps-a 16G  -> 8.2G (gen 2026-08-16)
    19456) t=9932  ;;   # vps-a 19G / vps-b 19G (gen 2026-08-16) -> 9.7G
    23552) t=12186 ;;   # vps-b 23G  -> 11.9G
    31744) t=15872 ;;   # vps-c 31G  -> 15.5G
    33792) t=24371 ;;   # vps-d 33G  -> 23.8G (gen 2026-08-16)
    35840) t=25805 ;;   # vps-d 35G  -> 25.2G
    46080) t=41574 ;;   # vps-d 45G  -> 40.6G (legacy 16c gen, measured mean)
    49152) t=47104 ;;   # vps-e 48G  -> 46G
    61440) t=53453 ;;   # vps-e 60G  -> 52.2G (grandfathered gen)
    *)     t=$(( mx * FLOOR_TARGET_FALLBACK_PCT / 100 )) ;;
  esac
  [ "$t" -lt "$orig" ] && t=$orig
  echo "$t"
}

ram_mb=$(( $(awk '/MemTotal/{print $2}' /proc/meminfo) / 1024 ))
avail_mb=$(( $(awk '/MemAvailable/{print $2}' /proc/meminfo) / 1024 ))
now=$(date +%s)

# --- node-level REAL disk swap-in + IO pressure ----------------------------
# The per-VM `rate` below comes from major_page_faults, and that counter scores
# a zswap hit exactly like a disk read. This fleet runs zswap on purpose, so
# most "faults" are compressed RAM costing microseconds, not I/O.
#
# Measured 2026-07-29 across 40 SQX nodes: pswpin was 0 on 38 of them (max
# 12/s) while zswpin reached 2030/s on one node whose PSI io was 0.00. On
# 0000214 the same day a guest read 1216 faults/s with 0 pages/s from disk.
# That is enough to escalate a LIVE CUSTOMER MIGRATION (defrag thrash-relief)
# on a node doing no disk I/O at all — which is exactly what happened.
#
# So export the two signals that do separate harm from cheap paging. The floor
# RAISE path deliberately keeps using faults: raising a floor is harmless and
# self-correcting, and it is what actually helped the guest above. Only the
# escalation needs the stricter evidence.
SWAPST="$STATE_DIR/swapin.tsv"
pswpin_now=$(awk '/^pswpin/{print $2}' /proc/vmstat)
pswpin_ps=0
if [ -r "$SWAPST" ]; then
  read -r p_prev t_prev < "$SWAPST" 2>/dev/null || true
  if [ -n "${p_prev:-}" ] && [ -n "${t_prev:-}" ] && [ "$now" -gt "${t_prev:-0}" ] \
     && [ "${pswpin_now:-0}" -ge "${p_prev:-0}" ]; then
    pswpin_ps=$(( (pswpin_now - p_prev) / (now - t_prev) ))
  fi
fi
printf '%s %s\n' "${pswpin_now:-0}" "$now" > "$SWAPST" 2>/dev/null || true
psi_io_pct=$(awk -F'avg300=' '/^some/{split($2,x," ");print x[1];exit}' \
             /proc/pressure/io 2>/dev/null)
psi_io_pct=${psi_io_pct:-0}

write_status() {  # $1 = blocked-entries JSON fragment ("" when none)
  local tmp
  tmp=$(mktemp /var/run/.neuravps-balloon-status.XXXXXX) || return 0
  chmod 644 "$tmp"
  printf '{"ts":%s,"ram_mb":%s,"avail_mb":%s,"committed_mb":%s,"floors_sum_mb":%s,"budget_mb":%s,"pswpin_ps":%s,"psi_io_pct":%s,"blocked":[%s]}\n' \
    "$now" "$ram_mb" "$avail_mb" "${committed:-0}" "${floors_sum:-0}" "${budget_mb:-0}" \
    "${pswpin_ps:-0}" "${psi_io_pct:-0}" "$1" > "$tmp"
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
declare -A P_F P_T P_IDLE P_BLOCKED P_LLOW P_BNC P_BLKU
while IFS=$'\t' read -r v f t i b ll bo bu; do
  [ -n "${v:-}" ] || continue
  P_F[$v]=$f; P_T[$v]=$t; P_IDLE[$v]=${i:-0}; P_BLOCKED[$v]=${b:-0}
  P_LLOW[$v]=${ll:-0}; P_BNC[$v]=${bo:-0}; P_BLKU[$v]=${bu:-0}
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
    [ -n "${P_F[$v]:-}" ] && printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$v" "${P_F[$v]}" "${P_T[$v]}" "${P_IDLE[$v]:-0}" "${P_BLOCKED[$v]:-0}" "${P_LLOW[$v]:-0}" "${P_BNC[$v]:-0}" "${P_BLKU[$v]:-0}" >> "$tmp_samples"
    continue; }
  rate=-1
  if [ -n "${P_F[$v]:-}" ] && [ -n "${P_T[$v]:-}" ] && [ "$now" -gt "${P_T[$v]}" ] && [ "$f1" -ge "${P_F[$v]}" ]; then
    rate=$(( (f1 - P_F[$v]) / (now - P_T[$v]) ))
  fi
  idle=${P_IDLE[$v]:-0}
  blocked=${P_BLOCKED[$v]:-0}
  last_lower=${P_LLOW[$v]:-0}
  bounces=${P_BNC[$v]:-0}
  block_until=${P_BLKU[$v]:-0}
  if [ "$rate" -ge 0 ]; then
    printf '%s\t%s\n' "$v" "$rate" >> "$tmp_rates"
    if [ "$rate" -ge "$RAISE_FAULTS_PS" ]; then
      idle=0   # genuine distress: hard reset is deliberate (only band that does)
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
            # v7 bounce memory: a raise soon after OUR lower means we probed
            # below the real working set — back off exponentially.
            if [ "$last_lower" -gt 0 ] && [ $(( now - last_lower )) -le "$REBOUND_WINDOW_S" ]; then
              bounces=$(( bounces + 1 ))
              if [ "$bounces" -ge 8 ]; then
                backoff=$BACKOFF_MAX_S   # shift would overflow long before this
              else
                backoff=$(( BACKOFF_BASE_S << (bounces - 1) ))
                [ "$backoff" -gt "$BACKOFF_MAX_S" ] && backoff=$BACKOFF_MAX_S
              fi
              block_until=$(( now + backoff ))
              logger -t neuravps-balloon " BOUNCE vm $v (raise $(( (now - last_lower) / 60 ))min after lower; bounce #${bounces}) — lower blocked $(( backoff / 3600 ))h"
            fi
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
      if [ "$LOWER_ENABLED" = 1 ] && [ "$idle" -ge "$IDLE_RUNS_TO_LOWER" ] \
         && [ "$now" -ge "$block_until" ]; then
        # v7 budget gate: never START a lower this node could not undo — a
        # re-raise needs a step of budget headroom AND a healthy host. The
        # 0.80 sales gate under the 0.90 ceiling keeps the last band
        # reconciler-only, so released budget cannot ALL be resold.
        if [ $(( budget_mb - floors_sum )) -ge "$STEP_MB" ] \
           && [ "$avail_mb" -ge "$HOST_FREE_MIN_MB" ]; then
          fl=${CUR_FLOOR[$v]}; mx=${CUR_MAX[$v]}
          orig=$(python3 - "$FLOORS" "$v" "$mx" "$fl" <<'PY'
import json,sys
d=json.load(open(sys.argv[1])); v=sys.argv[2]; mx=int(sys.argv[3]); fl=int(sys.argv[4])
print(d.get(v, ""))
PY
)
          # only lower what WE raised: no floors.json entry -> hands off.
          # v7: the target is the plan's measured settle floor, never `orig`.
          if [ -n "$orig" ]; then
            tgt=$(floor_target "$mx" "$orig")
            if [ "$fl" -gt "$tgt" ]; then
              new=$(( fl - STEP_MB )); [ "$new" -lt "$tgt" ] && new=$tgt
              if qm set "$v" --balloon "$new" >/dev/null 2>&1; then
                floors_sum=$(( floors_sum - fl + new ))
                last_lower=$now
                logger -t neuravps-balloon " LOWER vm $v floor ${fl}->${new}MB (idle credit ${idle}; target=${tgt}MB bounces=${bounces})"
                # PACE the walk-back: spend the credit so the next -4G step
                # needs LOWER_COOLDOWN_RUNS more calm runs.
                idle=$(( IDLE_RUNS_TO_LOWER - LOWER_COOLDOWN_RUNS ))
                [ "$idle" -lt 0 ] && idle=0
              fi
            fi
          fi
        fi
      fi
    else
      # MIDDLE BAND (IDLE_FAULTS_PS <= rate < RAISE_FAULTS_PS): mild activity,
      # NOT distress. v4 hard-reset idle here; with IDLE_FAULTS_PS=5 (barely
      # above noise) that is what made floors a one-way ratchet -- a raised VM
      # sits in this band 22% of minutes, so 30 CONSECUTIVE calm runs never
      # happened (p=0.06%). Decay instead: a busy minute COSTS a calm minute,
      # it does not erase the credit earned so far.
      [ "$idle" -gt 0 ] && idle=$(( idle - 1 ))
      [ "$blocked" -gt 0 ] && blocked=$(( blocked - 1 ))   # decay, not reset (oscillators)
    fi
  fi
  # emit on LEVEL, not on this-run state: an oscillator at level >= MIN must be
  # visible to the hourly relief tick even if this exact minute was calm
  if [ "$blocked" -ge "$BLOCKED_RUNS_MIN" ]; then
    [ -n "$blocked_json" ] && blocked_json="${blocked_json},"
    blocked_json="${blocked_json}{\"vmid\":${v},\"rate\":${rate:--1},\"floor_mb\":${CUR_FLOOR[$v]},\"max_mb\":${CUR_MAX[$v]},\"blocked_runs\":${blocked}}"
  fi
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$v" "$f1" "$now" "$idle" "$blocked" "$last_lower" "$bounces" "$block_until" >> "$tmp_samples"
done
mv "$tmp_samples" "$SAMPLES"
mv "$tmp_rates" "$RUN_RATES"
write_status "$blocked_json"
exit 0

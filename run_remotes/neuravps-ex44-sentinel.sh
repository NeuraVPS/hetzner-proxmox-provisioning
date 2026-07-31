#!/bin/bash
# neuravps-ex44-sentinel — "que me entere cuanto antes si algo va mal"
# (operador, 2026-07-31, tras decidir MANTENER los 127 vps-e en EX44).
#
# The dedicated EX44 boxes run a 60 GB guest on 62 GB usable: ~0 GB of margin,
# so the host parks cold guest pages in swap. That occupancy is HARMLESS and
# deliberately never alerts (salud philosophy). The guest only hurts when it
# re-sweeps that dormant heap and the pages grind back from disk — and before
# this file the first signal of that was the customer's ticket, or their
# cancellation: salud samples once a day and carves out psi-io as "load, not
# fault" on single-tenant boxes.
#
# Detector: REAL disk swap-in rate ((Δpswpin−Δzswpin)×4KiB/s — the same metric
# as salud's fleet guardrail) sampled every minute. A leaky level (+1 over
# threshold, −1 under; same shape as the balloon reconciler's blocked counter)
# means only SUSTAINED grinding trips — a one-minute blip decays away. At
# level >= LEVEL_MIN it writes ONE doc to Firestore `ex44_distress` (per-node
# cooldown ALERT_COOLDOWN_S so a bad afternoon is one alert, not sixty);
# node_liveness (5-min cloud schedule, proven mail path, alertedAt dedup)
# turns it into a 🟠 with the playbook.
#
# Config overrides in /etc/default/neuravps-ex44-sentinel. --test: evaluate
# and PRINT the would-be doc, never write, never touch the alert cooldown.
#NEXSVER=1
set -u

case "$(hostname)" in
  *EX44*) ;;
  *) exit 0 ;;   # safety: a stray install on a shared node must do nothing
esac

THRESH_MBS=${THRESH_MBS:-1.5}        # sustained real swap-in that counts as grinding
LEVEL_MIN=${LEVEL_MIN:-10}           # minutes of leaky level before alerting (~10 min)
ALERT_COOLDOWN_S=${ALERT_COOLDOWN_S:-86400}
CREDS=${FIREBASE_CREDENTIALS_FILE:-/etc/firebase-credentials.json}
[ -f /etc/default/neuravps-ex44-sentinel ] && . /etc/default/neuravps-ex44-sentinel

TEST=0
[ "${1:-}" = "--test" ] && TEST=1

STATE_DIR=/var/lib/neuravps-ex44-sentinel
STATE="$STATE_DIR/state"             # pswpin zswpin epoch level last_alert_epoch
mkdir -p "$STATE_DIR"

now=$(date +%s)
pswpin=$(awk '/^pswpin/{print $2}' /proc/vmstat)
zswpin=$(awk '/^zswpin/{print $2}' /proc/vmstat)
zswpin=${zswpin:-0}

p_psw=""; p_zsw=""; p_ts=""; level=0; last_alert=0
[ -r "$STATE" ] && read -r p_psw p_zsw p_ts level last_alert < "$STATE" 2>/dev/null
level=${level:-0}; last_alert=${last_alert:-0}

rate_mbs=0
if [ -n "${p_psw:-}" ] && [ -n "${p_ts:-}" ] && [ "$now" -gt "$p_ts" ] \
   && [ $(( now - p_ts )) -le 180 ] && [ "$pswpin" -ge "$p_psw" ] && [ "$zswpin" -ge "${p_zsw:-0}" ]; then
  # pages that came back from the actual swap DEVICE (zswap-pool hits are
  # RAM-speed and excluded), as MB/s x100 (integer math, bash has no floats)
  rate_c=$(( ( (pswpin - p_psw) - (zswpin - p_zsw) ) * 4096 * 100 / (now - p_ts) / 1048576 ))
  [ "$rate_c" -lt 0 ] && rate_c=0
else
  # first run / reboot / counter reset / stale sample: just record and leave
  printf '%s %s %s %s %s\n' "$pswpin" "$zswpin" "$now" "$level" "$last_alert" > "$STATE"
  exit 0
fi
thresh_c=$(awk -v t="$THRESH_MBS" 'BEGIN{printf "%d", t*100}')

if [ "$rate_c" -ge "$thresh_c" ]; then
  level=$(( level + 1 ))
else
  [ "$level" -gt 0 ] && level=$(( level - 1 ))
fi
rate_mbs=$(awk -v c="$rate_c" 'BEGIN{printf "%.1f", c/100}')

fire=0
if [ "$level" -ge "$LEVEL_MIN" ] && [ $(( now - last_alert )) -ge "$ALERT_COOLDOWN_S" ]; then
  fire=1
fi

if [ "$fire" = 1 ] || [ "$TEST" = 1 ]; then
  psi_io=$(awk -F'avg60=' '/^some/{split($2,x," ");print x[1];exit}' /proc/pressure/io 2>/dev/null)
  psi_mem=$(awk -F'avg60=' '/^some/{split($2,x," ");print x[1];exit}' /proc/pressure/memory 2>/dev/null)
  read -r swap_gb avail_gb <<EOF2
$(awk '/SwapTotal/{st=$2}/SwapFree/{sf=$2}/MemAvailable/{a=$2}END{printf "%.1f %.1f", (st-sf)/1048576, a/1048576}' /proc/meminfo)
EOF2
  vmid=$(ls /etc/pve/qemu-server/ 2>/dev/null | head -1 | cut -d. -f1)
  doc=$(printf '{"node":"%s","vmid":"%s","rateMbs":%s,"level":%s,"psiIo":%s,"psiMem":%s,"swapUsedGb":%s,"availGb":%s}' \
        "$(hostname)" "${vmid:-?}" "$rate_mbs" "$level" "${psi_io:-0}" "${psi_mem:-0}" "$swap_gb" "$avail_gb")
  if [ "$TEST" = 1 ]; then
    echo "TEST would-write: $doc (fire=$fire, thresh=${THRESH_MBS}MB/s, level_min=$LEVEL_MIN)"
  else
    if python3 - "$CREDS" "$doc" <<'PY'
import json, sys
import firebase_admin
from firebase_admin import credentials, firestore
firebase_admin.initialize_app(credentials.Certificate(sys.argv[1]))
db = firestore.client()
d = json.loads(sys.argv[2])
d["at"] = firestore.SERVER_TIMESTAMP
db.collection("ex44_distress").document().set(d)
PY
    then
      last_alert=$now
      logger -t neuravps-ex44-sentinel "DISTRESS reported: $doc"
    else
      logger -t neuravps-ex44-sentinel "distress detected but Firestore write FAILED: $doc"
    fi
  fi
fi

printf '%s %s %s %s %s\n' "$pswpin" "$zswpin" "$now" "$level" "$last_alert" > "$STATE"
exit 0

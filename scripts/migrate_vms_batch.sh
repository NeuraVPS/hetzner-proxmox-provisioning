#!/usr/bin/env bash
# migrate_vms_batch.sh — parallel batch runner for migrate_vm.sh.
#
# Usage:
#   ./migrate_vms_batch.sh [-f FILE] [-c PER_NODE] [-m MAX_TOTAL] [-l LOG_DIR]
#
# Reads "VMID NEW_NODE_ID" pairs (one per line) from FILE (or stdin if -f is
# omitted or "-"). Lines starting with # and blank lines are ignored.
#
# Schedules migrations in parallel with three safety caps so we don't
# saturate any single node's NIC / disk / RAM:
#   - PER_NODE     max in-flight migrations touching any single node (src OR dst).
#                  Live migration is mostly NIC-bound; 2 is safe on a 1 Gbps
#                  link, ~4 on 10 Gbps. Default 2.
#   - MAX_TOTAL    hard cap on overall in-flight migrations (default 8).
#   - per-VMID    same VMID is never in two slots at once (chained migrations
#                  like A→B then B→C are auto-serialised in input order).
#
# Logs (under LOG_DIR, default /var/log/migrate_vm):
#   batch-<ts>.log              master timeline (start/finish/result/summary)
#   jobs/vm-<vmid>-<ts>.log     per-job full stdout+stderr
#   errors-<ts>.log             aggregated content of every FAILED job
#                               + every WARN/ERROR line from successful jobs
#
# Exit code: 0 if every job succeeded, 1 if any failed, 2 on usage error.

# NOTE: `set -u` is intentionally OFF — bash 5.x trips on `${#assoc[@]}` in
# arithmetic context with unset/empty associative arrays, which we use heavily
# in the scheduler. We rely on `set -e` + explicit defaults instead.
set -eo pipefail

# ----- Defaults & args ---------------------------------------------------------
INPUT=""
PER_NODE=2
MAX_TOTAL=8
LOG_DIR="${LOG_DIR:-/var/log/migrate_vm}"
SCRIPT_DIR=$(dirname "$(readlink -f "$0")")
MIGRATE_SCRIPT="${MIGRATE_SCRIPT:-$SCRIPT_DIR/migrate_vm.sh}"
PVE_NODES_FILE="${PVE_NODES_FILE:-/var/lib/base-nat/pve_nodes.json}"
STATE_FILE="${STATE_FILE:-/var/lib/base-nat/state.json}"

usage() {
  cat >&2 <<EOF
Usage: $(basename "$0") [-f FILE] [-c PER_NODE] [-m MAX_TOTAL] [-l LOG_DIR]
  -f FILE        list of "VMID NEW_NODE_ID" pairs ("-" or omit for stdin)
  -c PER_NODE    max in-flight per node (default $PER_NODE)
  -m MAX_TOTAL   max total in-flight (default $MAX_TOTAL)
  -l LOG_DIR     log directory          (default $LOG_DIR)
  -h             show this help

Reads:
  $PVE_NODES_FILE  (nodeId -> vmbr0 IPv6, written by sync-base-nat.py sync nodes)
  $STATE_FILE      (vmid   -> current VM IPv6, written by sync-base-nat.py sync)

Example:
  cat > migrations.txt <<'TXT'
  # one VMID NEW_NODE_ID pair per line
  123 99
  111 44
  TXT
  ./$(basename "$0") -f migrations.txt
EOF
}

while getopts ":f:c:m:l:h" opt; do
  case "$opt" in
    f) INPUT="$OPTARG" ;;
    c) PER_NODE="$OPTARG" ;;
    m) MAX_TOTAL="$OPTARG" ;;
    l) LOG_DIR="$OPTARG" ;;
    h) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
done

# Need bash 5.1+ for `wait -n -p VAR` (Debian 12 ships 5.2).
if (( BASH_VERSINFO[0] < 5 || (BASH_VERSINFO[0] == 5 && BASH_VERSINFO[1] < 1) )); then
  echo "❌ bash 5.1+ required (have $BASH_VERSION)" >&2
  exit 1
fi

[[ -x "$MIGRATE_SCRIPT" ]]   || { echo "❌ migrate_vm.sh not found/executable: $MIGRATE_SCRIPT" >&2; exit 1; }
[[ "$PER_NODE"  =~ ^[0-9]+$ && "$PER_NODE"  -ge 1 ]] || { echo "❌ -c must be >= 1" >&2; exit 2; }
[[ "$MAX_TOTAL" =~ ^[0-9]+$ && "$MAX_TOTAL" -ge 1 ]] || { echo "❌ -m must be >= 1" >&2; exit 2; }
[[ -f "$PVE_NODES_FILE" ]]   || { echo "❌ missing $PVE_NODES_FILE — is this a BASE server?" >&2; exit 1; }
[[ -f "$STATE_FILE" ]]       || { echo "❌ missing $STATE_FILE — run sync-base-nat.py sync first." >&2; exit 1; }
command -v python3 >/dev/null || { echo "❌ python3 required" >&2; exit 1; }

mkdir -p "$LOG_DIR/jobs"
TS=$(date -u +%Y%m%dT%H%M%SZ)
MASTER_LOG="$LOG_DIR/batch-$TS.log"
ERRORS_LOG="$LOG_DIR/errors-$TS.log"
: > "$MASTER_LOG"
: > "$ERRORS_LOG"

log() {
  printf '%s %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" | tee -a "$MASTER_LOG"
}

# ----- Resolver ----------------------------------------------------------------
# Given (vmid, dst_num), prints "src_node dst_node" using the same logic as
# migrate_vm.sh. Exit codes:
#   0  OK (printed)
#   99 source == destination (no-op)
#   1  any other failure (message on stderr)
_resolve_pair() {
  VMID="$1" NEW_NODE_NUM="$2" \
  PVE_NODES_FILE="$PVE_NODES_FILE" STATE_FILE="$STATE_FILE" \
  python3 - <<'PY'
import ipaddress, json, os, sys

vmid       = int(os.environ["VMID"])
target_num = int(os.environ["NEW_NODE_NUM"])
nodes      = json.load(open(os.environ["PVE_NODES_FILE"]))
state      = json.load(open(os.environ["STATE_FILE"]))

def fail(msg, code=1):
    sys.stderr.write(msg + "\n"); sys.exit(code)

def prefix64(s):
    a = int(ipaddress.IPv6Address(s))
    return ipaddress.IPv6Network((a & ~((1 << 64) - 1), 64))

dst = None
for h, ip in nodes.items():
    head = h.split("-", 1)[0]
    if head.isdigit() and int(head) == target_num:
        dst = (h, ip); break
if not dst:
    fail(f"NEW_NODE_ID={target_num} not found in pve_nodes.json")
dst_node, dst_ipv6 = dst

vm = state.get(str(vmid)) or {}
old_ipv6 = (vm.get("ipv6") or "").strip()
if not old_ipv6:
    fail(f"vmid {vmid} not in state.json")
try:
    src_net = prefix64(old_ipv6)
except ValueError:
    fail(f"vmid {vmid} bad ipv6: {old_ipv6!r}")

src = None
for h, ip in nodes.items():
    try:
        if prefix64(ip) == src_net:
            src = (h, ip); break
    except ValueError:
        continue
if not src:
    fail(f"vmid {vmid}: no node shares /64 with {old_ipv6}")
src_node, _ = src

if src_node == dst_node:
    sys.exit(99)

print(src_node, dst_node)
PY
}

# ----- Parse input + resolve all jobs upfront ----------------------------------
declare -a JOBS=()
if [[ -z "$INPUT" || "$INPUT" == "-" ]]; then
  input_src=/dev/stdin
else
  [[ -r "$INPUT" ]] || { echo "❌ cannot read $INPUT" >&2; exit 1; }
  input_src="$INPUT"
fi

skipped=0
while IFS= read -r raw_line || [[ -n "$raw_line" ]]; do
  line="${raw_line%%#*}"
  line="${line#"${line%%[![:space:]]*}"}"
  line="${line%"${line##*[![:space:]]}"}"
  [[ -z "$line" ]] && continue

  read -r vmid dst_num extra <<< "$line" || true
  if [[ -n "${extra:-}" ]]; then
    log "SKIP malformed (extra fields): $raw_line"
    skipped=$((skipped + 1)); continue
  fi
  if ! [[ "$vmid" =~ ^[0-9]+$ && "$dst_num" =~ ^[0-9]+$ ]]; then
    log "SKIP malformed: $raw_line"
    skipped=$((skipped + 1)); continue
  fi

  res=$(_resolve_pair "$vmid" "$dst_num" 2>&1) && rc=0 || rc=$?
  if (( rc == 99 )); then
    log "SKIP vmid=$vmid: source == dest=$dst_num (already there)"
    skipped=$((skipped + 1)); continue
  fi
  if (( rc != 0 )); then
    log "SKIP vmid=$vmid dst=$dst_num: resolve failed: $res"
    skipped=$((skipped + 1)); continue
  fi
  read -r src_node dst_node <<< "$res"
  JOBS+=("$vmid|$src_node|$dst_node|$dst_num")
done < "$input_src"

if (( ${#JOBS[@]} == 0 )); then
  log "no runnable jobs (skipped=$skipped); exiting"
  exit 0
fi

log "Parsed ${#JOBS[@]} job(s), skipped=$skipped, per_node=$PER_NODE max_total=$MAX_TOTAL"
log "Master log:  $MASTER_LOG"
log "Errors log:  $ERRORS_LOG"
log "Job logs in: $LOG_DIR/jobs/"

# ----- Scheduler ---------------------------------------------------------------
declare -A in_flight    # node -> count
declare -A pid_meta     # pid  -> "vmid|src|dst|jlog"
declare -A vmid_busy    # vmid -> 1
pending=("${JOBS[@]}")

ok_count=0; fail_count=0

# Trap: on Ctrl-C or fatal, kill all in-flight children.
_kill_children() {
  for p in "${!pid_meta[@]}"; do
    kill -TERM "$p" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap _kill_children INT TERM

while (( ${#pending[@]} > 0 || ${#pid_meta[@]} > 0 )); do
  # Try to schedule as many jobs as possible from `pending`. Loop until no
  # more progress (covers the case where finishing one job lets several
  # waiting ones start).
  while :; do
    progressed=0
    new_pending=()
    for job in "${pending[@]}"; do
      IFS='|' read -r jvmid jsrc jdst jdstnum <<< "$job"
      if [[ -n "${vmid_busy[$jvmid]:-}" ]] \
         || (( ${in_flight[$jsrc]:-0} >= PER_NODE )) \
         || (( ${in_flight[$jdst]:-0} >= PER_NODE )) \
         || (( ${#pid_meta[@]} >= MAX_TOTAL )); then
        new_pending+=("$job")
        continue
      fi

      jts=$(date -u +%Y%m%dT%H%M%SZ)
      jlog="$LOG_DIR/jobs/vm-${jvmid}-${jts}.log"
      # Each child writes _die/_warn lines to the shared ERRORS_LOG so we
      # don't have to grep through 30 individual job logs to find them.
      ERROR_LOG="$ERRORS_LOG" "$MIGRATE_SCRIPT" "$jvmid" "$jdstnum" >"$jlog" 2>&1 &
      pid=$!

      pid_meta[$pid]="$jvmid|$jsrc|$jdst|$jlog"
      in_flight[$jsrc]=$(( ${in_flight[$jsrc]:-0} + 1 ))
      in_flight[$jdst]=$(( ${in_flight[$jdst]:-0} + 1 ))
      vmid_busy[$jvmid]=1
      progressed=1
      log "STARTED  vmid=$jvmid src=$jsrc dst=$jdst pid=$pid in_flight=${#pid_meta[@]} log=$jlog"
    done
    pending=("${new_pending[@]}")
    (( progressed )) || break
  done

  # Wait for any one child to finish; bash 5.1+ stores its pid in $finished.
  if (( ${#pid_meta[@]} > 0 )); then
    finished=""
    set +e
    wait -n -p finished
    jrc=$?
    set -e

    if [[ -z "$finished" ]]; then
      log "BUG: wait -n returned without -p pid; aborting"
      _kill_children
      exit 1
    fi

    meta="${pid_meta[$finished]}"
    unset 'pid_meta[$finished]'
    IFS='|' read -r jvmid jsrc jdst jlog <<< "$meta"
    in_flight[$jsrc]=$(( in_flight[$jsrc] - 1 ))
    in_flight[$jdst]=$(( in_flight[$jdst] - 1 ))
    unset 'vmid_busy[$jvmid]'

    if (( jrc == 0 )); then
      ok_count=$((ok_count + 1))
      log "OK       vmid=$jvmid src=$jsrc dst=$jdst rc=0  log=$jlog"
    else
      fail_count=$((fail_count + 1))
      log "FAIL     vmid=$jvmid src=$jsrc dst=$jdst rc=$jrc log=$jlog"
      {
        echo
        echo "========================================================================"
        echo "FAIL  vmid=$jvmid  src=$jsrc → dst=$jdst  rc=$jrc  $(date -u +'%Y-%m-%dT%H:%M:%SZ')"
        echo "      log=$jlog"
        echo "------------------------------------ output ------------------------------------"
        cat "$jlog" 2>/dev/null || echo "(could not read $jlog)"
        echo "----------------------------------- end output ---------------------------------"
      } >> "$ERRORS_LOG"
    fi
  fi
done

trap - INT TERM

# ----- Summary -----------------------------------------------------------------
log "========================================================================"
log "SUMMARY  ok=$ok_count fail=$fail_count skipped=$skipped total=${#JOBS[@]}"
log "Master log:  $MASTER_LOG"
log "Errors log:  $ERRORS_LOG"

if (( fail_count > 0 )); then
  log "Some migrations failed. Review $ERRORS_LOG for details."
  exit 1
fi
exit 0

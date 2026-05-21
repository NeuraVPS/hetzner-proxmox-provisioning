#!/usr/bin/env bash
# migrate_vms_batch.sh — parallel batch runner for migrate_vm.sh.
#
# Usage:
#   ./migrate_vms_batch.sh [-f FILE] [-c PER_NODE] [-m MAX_TOTAL] [-l LOG_DIR]
#   ./migrate_vms_batch.sh -n SRC:DST[,SRC:DST...] [-n …] [-c …] [-m …] [-l …]
#
# Reads "VMID NEW_NODE_ID" pairs (one per line) from FILE (or stdin if -f is
# omitted or "-"). Lines starting with # and blank lines are ignored.
#
# -n SRC:DST[,SRC:DST…]
#                       full-node drain mode: enumerate every VM currently in
#                       each source node's /64 (from state.json) and migrate
#                       them to its paired destination. Multiple pairs may be
#                       given either comma-separated within one -n or by
#                       repeating -n (or both). Mutually exclusive with
#                       -f/stdin.
#                       For each pair, BOTH nodes are FROZEN
#                       (proxmox_nodes.frozen=true) on entry so new orders
#                       can't land on either of them mid-drain. All resulting
#                       VM migrations share the same scheduler: PER_NODE /
#                       MAX_TOTAL / per-VMID caps apply across pairs so e.g.
#                       two drains converging on the same DST run serially
#                       on that DST while their independent SRCs proceed
#                       in parallel.
#                       On exit (success, failure, or Ctrl-C):
#                         - Every SRC stays frozen — drained nodes are kept
#                           out of auto_provision until an admin manually
#                           unfreezes them (re-frozen defensively even if
#                           the initial freeze write failed).
#                         - Every DST is restored to its pre-run frozen
#                           state (admin-frozen DST stays frozen; auto-
#                           frozen DST is unfrozen).
#                       A node may appear at most once as a SRC, and may not
#                       appear as both a SRC and a DST across pairs (chained
#                       drains aren't supported — run them as two separate
#                       invocations). A DST may repeat (N→1 fan-in).
#                       Requires firebase_admin + Firestore creds on BASE —
#                       same creds migrate_vm.sh already needs.
#
# Schedules migrations in parallel with three safety caps so we don't
# saturate any single node's NIC / disk / RAM:
#   - PER_NODE     max in-flight migrations touching any single node (src OR dst).
#                  Live migration is mostly NIC-bound; 1 is safe on a 1 Gbps
#                  link, ~2 on 10 Gbps. Default 1.
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
# Before scheduling, every unique node referenced (source or dest) is
# `apt dist-upgrade`d exactly once so dest pve-qemu-kvm >= source's (live
# migration is forward-compatible only). Jobs whose src/dst node fails the
# upgrade are dropped and counted as failures; unrelated nodes still run.
# Bypass with SKIP_NODE_APT_UPGRADE=1 (QEMU parity then NOT guaranteed).
#
# Exit code: 0 if every job succeeded, 1 if any failed, 2 on usage error.

# NOTE: `set -u` is intentionally OFF — bash 5.x trips on `${#assoc[@]}` in
# arithmetic context with unset/empty associative arrays, which we use heavily
# in the scheduler. We rely on `set -e` + explicit defaults instead.
set -eo pipefail

# ----- Defaults & args ---------------------------------------------------------
INPUT=""
declare -a NODE_MIGRATIONS=()  # accumulated "SRC:DST" specs from -n
PER_NODE=1
MAX_TOTAL=8
LOG_DIR="${LOG_DIR:-/var/log/migrate_vm}"
SCRIPT_DIR=$(dirname "$(readlink -f "$0")")
MIGRATE_SCRIPT="${MIGRATE_SCRIPT:-$SCRIPT_DIR/migrate_vm.sh}"
# Online-migration cutover downtime (s) handed to every child migrate_vm.sh.
# Default 90: empirically below the dest-QEMU-exit blackout threshold (~108 s
# survived, ~175 s killed the dest) while still forcing real pre-copy so large
# VMs don't go straight to one giant blackout. See migrate_vm.sh header for the
# full two-failure-mode rationale. Defined and exported here so a batch run
# always applies it identically across all parallel children regardless of
# inherited env. Override by setting MIGRATE_DOWNTIME before launching (env
# wins; MIGRATE_DOWNTIME=0 leaves the VM default).
export MIGRATE_DOWNTIME="${MIGRATE_DOWNTIME:-90}"
PVE_NODES_FILE="${PVE_NODES_FILE:-/var/lib/base-nat/pve_nodes.json}"
STATE_FILE="${STATE_FILE:-/var/lib/base-nat/state.json}"

usage() {
  cat >&2 <<EOF
Usage: $(basename "$0") [-f FILE] [-c PER_NODE] [-m MAX_TOTAL] [-l LOG_DIR]
       $(basename "$0") -n SRC:DST[,SRC:DST…] [-n …] [-c …] [-m …] [-l …]
  -f FILE          list of "VMID NEW_NODE_ID" pairs ("-" or omit for stdin)
  -n SRC:DST       full-node drain: migrate ALL VMs on SRC to DST. SRC stays
                   frozen at the end; DST is restored to its pre-run state.
                   May be repeated and/or comma-separated to drain multiple
                   sources in one batch (all share the scheduler caps).
                   Mutually exclusive with -f/stdin.
  -c PER_NODE      max in-flight per node (default $PER_NODE)
  -m MAX_TOTAL     max total in-flight (default $MAX_TOTAL)
  -l LOG_DIR       log directory          (default $LOG_DIR)
  -h               show this help

Reads:
  $PVE_NODES_FILE  (nodeId -> vmbr0 IPv6, written by sync-base-nat.py sync nodes)
  $STATE_FILE      (vmid   -> current VM IPv6, written by sync-base-nat.py sync)

Examples:
  cat > migrations.txt <<'TXT'
  # one VMID NEW_NODE_ID pair per line
  123 99
  111 44
  TXT
  ./$(basename "$0") -f migrations.txt

  # Drain every VM off node 7 onto node 99:
  ./$(basename "$0") -n 7:99

  # Drain three sources in one batch (7→99, 11→55, 15→99); 99 is shared so
  # those two drains will serialise on the 99 side while 11→55 runs in
  # parallel (subject to PER_NODE/MAX_TOTAL):
  ./$(basename "$0") -n 7:99,11:55,15:99
EOF
}

while getopts ":f:n:c:m:l:h" opt; do
  case "$opt" in
    f) INPUT="$OPTARG" ;;
    n)
      # Accept either repeated -n or comma-separated pairs within one -n
      # (or both). Skip empty fragments from stray commas.
      IFS=',' read -ra _ns <<< "$OPTARG"
      for _one in "${_ns[@]}"; do
        [[ -n "$_one" ]] && NODE_MIGRATIONS+=("$_one")
      done
      ;;
    c) PER_NODE="$OPTARG" ;;
    m) MAX_TOTAL="$OPTARG" ;;
    l) LOG_DIR="$OPTARG" ;;
    h) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
done

if (( ${#NODE_MIGRATIONS[@]} > 0 )); then
  if [[ -n "$INPUT" ]]; then
    echo "❌ -n and -f are mutually exclusive" >&2; exit 2
  fi
  # Validate each pair's format, forbid SRC==DST within a pair, forbid a SRC
  # appearing twice (one drain per source), and forbid a node being both a
  # SRC and a DST across pairs (chained drains aren't supported — they'd
  # require draining a node that's simultaneously receiving new VMs).
  declare -A _seen_src
  declare -A _seen_dst
  for spec in "${NODE_MIGRATIONS[@]}"; do
    if ! [[ "$spec" =~ ^([0-9]+):([0-9]+)$ ]]; then
      echo "❌ invalid -n spec '$spec': expected SRC:DST (e.g. 7:99)" >&2; exit 2
    fi
    _sn="${BASH_REMATCH[1]}"
    _dn="${BASH_REMATCH[2]}"
    if (( _sn == _dn )); then
      echo "❌ -n '$spec': source and destination are the same node" >&2; exit 2
    fi
    if [[ -n "${_seen_src[$_sn]:-}" ]]; then
      echo "❌ -n: source $_sn appears in multiple pairs (one drain per source)" >&2; exit 2
    fi
    _seen_src[$_sn]=1
    _seen_dst[$_dn]=1
  done
  for _sn in "${!_seen_src[@]}"; do
    if [[ -n "${_seen_dst[$_sn]:-}" ]]; then
      echo "❌ -n: node $_sn appears as both source and destination — chained drains not supported (run as separate invocations)" >&2; exit 2
    fi
  done
fi

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

# Freeze tracking maps. Populated by -n mode; file/stdin mode leaves them
# empty so the EXIT trap is a no-op.
#   FREEZE_KEEP    [node_name] -> 1            # write frozen=true at exit
#                                              # (drained sources stay frozen)
#   FREEZE_RESTORE [node_name] -> "true|false" # target state at exit
#                                              # (destinations restore to pre-run)
# A node will only ever be in one of these — cross-pair validation forbids a
# node being both a SRC and a DST.
declare -A FREEZE_KEEP=()
declare -A FREEZE_RESTORE=()

# Single EXIT trap doing two jobs: (1) reconcile each touched node's frozen
# state — sources stay frozen, destinations restore to their pre-run state —
# and (2) ALWAYS dump the aggregated errors log so the operator never has to
# chase down a separate file (clean run prints "(no errors recorded)", failed
# run prints every WARN/ERROR + every failed job's output). Registered here
# (right after the log paths are set) so it covers every exit path: early
# upgrade-gate exit, Ctrl-C through _kill_children, and `set -e` aborts.
# _node_set_frozen is defined below; bash resolves the name at call time and
# the trap only fires after definitions are in place.
_on_exit() {
  local rc=$?
  set +e
  for nd in "${!FREEZE_KEEP[@]}"; do
    log "Leaving $nd frozen=true (drained source stays frozen)"
    _node_set_frozen "$nd" "true" >/dev/null 2>&1 \
      || log "WARN: failed to ensure $nd frozen=true — verify in admin UI"
  done
  for nd in "${!FREEZE_RESTORE[@]}"; do
    if [[ "${FREEZE_RESTORE[$nd]}" == "false" ]]; then
      log "Restoring $nd → frozen=false (was unfrozen before run)"
      _node_set_frozen "$nd" "false" >/dev/null 2>&1 \
        || log "WARN: failed to restore $nd frozen=false — set manually"
    fi
  done
  echo
  echo "==================== ERRORS LOG ($(basename "$ERRORS_LOG")) ===================="
  if [[ -s "$ERRORS_LOG" ]]; then
    cat "$ERRORS_LOG"
  else
    echo "(no errors or warnings recorded)"
  fi
  echo "==================== END ERRORS LOG ===================="
  return "$rc"
}
trap _on_exit EXIT

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

# ----- Node-drain helpers (only used by -n) ------------------------------------
# _enumerate_node SRC_NUM DST_NUM
#   On success (stdout):
#     NODES<TAB>src_node<TAB>dst_node
#     VM<TAB>vmid          (one line per VMID currently in src's /64)
#   Exit 1 on resolve failure, 2 if SRC == DST.
_enumerate_node() {
  SRC_NUM="$1" DST_NUM="$2" \
  PVE_NODES_FILE="$PVE_NODES_FILE" STATE_FILE="$STATE_FILE" \
  python3 - <<'PY'
import ipaddress, json, os, sys

src_num = int(os.environ["SRC_NUM"])
dst_num = int(os.environ["DST_NUM"])
nodes   = json.load(open(os.environ["PVE_NODES_FILE"]))
state   = json.load(open(os.environ["STATE_FILE"]))

def find_node(num):
    for h, ip in nodes.items():
        head = h.split("-", 1)[0]
        if head.isdigit() and int(head) == num:
            return h, ip
    return None, None

src_node, src_ipv6 = find_node(src_num)
dst_node, dst_ipv6 = find_node(dst_num)
if not src_node:
    sys.stderr.write(f"src_num={src_num} not in pve_nodes.json\n"); sys.exit(1)
if not dst_node:
    sys.stderr.write(f"dst_num={dst_num} not in pve_nodes.json\n"); sys.exit(1)
if src_node == dst_node:
    sys.stderr.write("src and dst resolve to the same node\n"); sys.exit(2)

try:
    a = int(ipaddress.IPv6Address(src_ipv6))
    src_net = ipaddress.IPv6Network((a & ~((1 << 64) - 1), 64))
except ValueError:
    sys.stderr.write(f"bad src ipv6: {src_ipv6!r}\n"); sys.exit(1)

vmids = []
for vmid_s, vm in state.items():
    ipv6 = (vm.get("ipv6") or "").strip()
    if not ipv6:
        continue
    try:
        a = int(ipaddress.IPv6Address(ipv6))
        if ipaddress.IPv6Network((a & ~((1 << 64) - 1), 64)) == src_net:
            vmids.append(int(vmid_s))
    except ValueError:
        continue

vmids.sort()
print(f"NODES\t{src_node}\t{dst_node}")
for v in vmids:
    print(f"VM\t{v}")
PY
}

# _node_get_frozen <node_id>  → prints "true"/"false" on stdout.
# Used to remember a node's pre-run frozen state so we can restore it on exit.
_node_get_frozen() {
  python3 - "$1" <<'PY'
import os, sys
from pathlib import Path
try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except ImportError:
    sys.stderr.write("firebase_admin not installed\n"); sys.exit(3)
CREDS = os.environ.get("FIREBASE_CREDENTIALS_FILE", "/etc/firebase-credentials.json")
if not firebase_admin._apps:
    if not Path(CREDS).is_file():
        sys.stderr.write(f"missing creds: {CREDS}\n"); sys.exit(1)
    firebase_admin.initialize_app(credentials.Certificate(CREDS))
db = firestore.client()
doc = db.collection("proxmox_nodes").document(sys.argv[1]).get()
if not doc.exists:
    sys.stderr.write(f"no proxmox_nodes/{sys.argv[1]}\n"); sys.exit(2)
print("true" if (doc.to_dict() or {}).get("frozen") else "false")
PY
}

# _node_set_frozen <node_id> <true|false>
_node_set_frozen() {
  python3 - "$1" "$2" <<'PY'
import os, sys
from pathlib import Path
try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except ImportError:
    sys.stderr.write("firebase_admin not installed\n"); sys.exit(3)
CREDS = os.environ.get("FIREBASE_CREDENTIALS_FILE", "/etc/firebase-credentials.json")
if not firebase_admin._apps:
    if not Path(CREDS).is_file():
        sys.stderr.write(f"missing creds: {CREDS}\n"); sys.exit(1)
    firebase_admin.initialize_app(credentials.Certificate(CREDS))
db = firestore.client()
db.collection("proxmox_nodes").document(sys.argv[1]).update({"frozen": sys.argv[2] == "true"})
PY
}

# ----- Parse input + resolve all jobs upfront ----------------------------------
declare -a JOBS=()
skipped=0

if (( ${#NODE_MIGRATIONS[@]} > 0 )); then
  # --- Full-node drain mode: process every SRC:DST pair, build one shared
  # --- JOBS array; the existing scheduler then parallelises across pairs.
  log "FULL-NODE drain: ${#NODE_MIGRATIONS[@]} pair(s) requested"
  for spec in "${NODE_MIGRATIONS[@]}"; do
    # Format already validated; re-extract via `if` so a regex mismatch can't
    # cooperate-with-`set -e` to abort silently.
    if ! [[ "$spec" =~ ^([0-9]+):([0-9]+)$ ]]; then
      echo "❌ internal: -n spec '$spec' bypassed validation" >&2; exit 1
    fi
    src_num="${BASH_REMATCH[1]}"
    dst_num="${BASH_REMATCH[2]}"

    enum_out=$(_enumerate_node "$src_num" "$dst_num" 2>&1) || {
      echo "❌ -n enumeration failed for $spec: $enum_out" >&2; exit 1
    }

    src_name=""; dst_name=""
    declare -a pair_vmids=()
    while IFS=$'\t' read -r tag a b; do
      case "$tag" in
        NODES) src_name="$a"; dst_name="$b" ;;
        VM)    pair_vmids+=("$a") ;;
      esac
    done <<< "$enum_out"
    if [[ -z "$src_name" || -z "$dst_name" ]]; then
      echo "❌ -n enumeration returned no node ids for $spec" >&2; exit 1
    fi

    log "  pair $spec → $src_name → $dst_name — discovered ${#pair_vmids[@]} VM(s)"

    # Capture & freeze EVEN IF 0 VMs found: the operator's intent in passing
    # this pair was for src to end frozen, so we honour that even when the
    # drain itself is a no-op. _node_get_frozen hard-fails if it can't read
    # the doc (Firestore creds missing / node missing) — surfacing that here
    # is better than discovering it during the run.
    if [[ -z "${FREEZE_KEEP[$src_name]:-}" ]]; then
      src_was=$(_node_get_frozen "$src_name") \
        || { echo "❌ Could not read frozen state of $src_name from Firestore" >&2; exit 1; }
      FREEZE_KEEP[$src_name]=1
      log "    pre-run: $src_name=$src_was (will stay frozen)"
    fi
    # Same DST may appear in multiple pairs (N→1 fan-in); only record the
    # first reading. Subsequent _node_set_frozen calls below are idempotent.
    if [[ -z "${FREEZE_RESTORE[$dst_name]:-}" ]]; then
      dst_was=$(_node_get_frozen "$dst_name") \
        || { echo "❌ Could not read frozen state of $dst_name from Firestore" >&2; exit 1; }
      FREEZE_RESTORE[$dst_name]="$dst_was"
      log "    pre-run: $dst_name=$dst_was (will restore to that at end)"
    fi

    _node_set_frozen "$src_name" "true" >/dev/null 2>&1 \
      || log "    WARN: failed to freeze $src_name — continuing (new orders may still land on it)"
    _node_set_frozen "$dst_name" "true" >/dev/null 2>&1 \
      || log "    WARN: failed to freeze $dst_name — continuing (new orders may still land on it)"

    for vmid in "${pair_vmids[@]}"; do
      res=$(_resolve_pair "$vmid" "$dst_num" 2>&1) && rc=0 || rc=$?
      if (( rc == 99 )); then
        log "    SKIP vmid=$vmid: source == dest=$dst_num (already there)"
        skipped=$((skipped + 1)); continue
      fi
      if (( rc != 0 )); then
        log "    SKIP vmid=$vmid dst=$dst_num: resolve failed: $res"
        skipped=$((skipped + 1)); continue
      fi
      read -r r_src r_dst <<< "$res"
      JOBS+=("$vmid|$r_src|$r_dst|$dst_num")
    done

    # `declare -a pair_vmids` from previous iteration would persist as the
    # same name; reset for next loop iter so we don't carry over VMs.
    unset pair_vmids
  done
else
  # --- File/stdin mode (original behaviour) ---
  if [[ -z "$INPUT" || "$INPUT" == "-" ]]; then
    input_src=/dev/stdin
  else
    [[ -r "$INPUT" ]] || { echo "❌ cannot read $INPUT" >&2; exit 1; }
    input_src="$INPUT"
  fi

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
fi

if (( ${#JOBS[@]} == 0 )); then
  log "no runnable jobs (skipped=$skipped); exiting"
  exit 0
fi

TOTAL_JOBS=${#JOBS[@]}
log "Parsed ${#JOBS[@]} job(s), skipped=$skipped, per_node=$PER_NODE max_total=$MAX_TOTAL"
log "Master log:  $MASTER_LOG"
log "Errors log:  $ERRORS_LOG"
log "Job logs in: $LOG_DIR/jobs/"

# ----- Pre-migration node package convergence (once per unique node) -----------
# Live migration needs dest pve-qemu-kvm >= source's; we dist-upgrade every node
# involved exactly once here so the per-VM migrate_vm.sh check just sees "0
# pending" and skips fast. A node whose upgrade fails has ALL its jobs (src or
# dst) dropped and recorded as failures; unrelated nodes still proceed.
#
# IMPORTANT: payload identical to _APT_REMOTE_PAYLOAD in migrate_vm.sh — keep in
# sync. Set SKIP_NODE_APT_UPGRADE=1 to bypass (emergency / offline-repo).
node_fail_count=0
if [[ "${SKIP_NODE_APT_UPGRADE:-0}" == "1" ]]; then
  log "SKIP_NODE_APT_UPGRADE=1 — skipping pre-migration node upgrades; QEMU parity NOT guaranteed."
else
  declare -A NODE_IP
  while IFS=$'\t' read -r _n _ip; do NODE_IP["$_n"]="$_ip"; done < <(
    python3 -c 'import json,sys
d=json.load(open(sys.argv[1]))
[print(f"{k}\t{v}") for k,v in d.items()]' "$PVE_NODES_FILE"
  )

  APT_REMOTE_PAYLOAD='
set -o pipefail
export DEBIAN_FRONTEND=noninteractive
LOCK="-o DPkg::Lock::Timeout=300"
CONF="-o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold"
if ! apt-get $LOCK update -qq; then echo "APT_UPGRADE_FAIL apt-get update failed" >&2; exit 1; fi
n=$(LC_ALL=C apt-get -s dist-upgrade 2>/dev/null | awk "/^[0-9]+ upgraded,/{print \$1; exit}")
n=${n:-0}
if [ "$n" -eq 0 ]; then echo "APT_UPGRADE_SKIP already current"; exit 0; fi
echo "APT_UPGRADE_RUN $n package(s) pending"
if ! apt-get $LOCK $CONF -y dist-upgrade; then echo "APT_UPGRADE_FAIL dist-upgrade failed" >&2; exit 1; fi
echo "APT_UPGRADE_DONE"
'
  SSH_OPTS=(-o LogLevel=ERROR -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
            -o ConnectTimeout=10 -o ServerAliveInterval=60 -o ServerAliveCountMax=3 -o BatchMode=yes)

  # Unique set of nodes referenced as src or dst across all jobs.
  declare -A _seen_node
  declare -A NODE_BAD
  for job in "${JOBS[@]}"; do
    IFS='|' read -r _jv jsrc jdst _jn <<< "$job"
    for nd in "$jsrc" "$jdst"; do
      [[ -n "${_seen_node[$nd]:-}" ]] && continue
      _seen_node[$nd]=1
      nip="${NODE_IP[$nd]:-}"
      if [[ -z "$nip" ]]; then
        log "NODE-UPGRADE FAIL node=$nd: no IPv6 in $PVE_NODES_FILE"
        NODE_BAD[$nd]=1; continue
      fi
      log "NODE-UPGRADE start node=$nd ($nip)"
      if out=$(ssh "${SSH_OPTS[@]}" "root@${nip}" "$APT_REMOTE_PAYLOAD" 2>&1); then
        if grep -q 'APT_UPGRADE_SKIP' <<< "$out"; then
          log "NODE-UPGRADE ok    node=$nd (already current)"
        elif grep -q 'APT_UPGRADE_DONE' <<< "$out"; then
          log "NODE-UPGRADE ok    node=$nd (dist-upgrade applied)"
        else
          log "NODE-UPGRADE FAIL  node=$nd: no success marker"
          NODE_BAD[$nd]=1
        fi
      else
        log "NODE-UPGRADE FAIL  node=$nd: $(printf '%s' "$out" | tail -1)"
        NODE_BAD[$nd]=1
      fi
      if [[ -n "${NODE_BAD[$nd]:-}" ]]; then
        {
          echo
          echo "========================================================================"
          echo "NODE-UPGRADE FAIL  node=$nd  $(date -u +'%Y-%m-%dT%H:%M:%SZ')"
          echo "------------------------------------ output ------------------------------------"
          printf '%s\n' "$out"
          echo "----------------------------------- end output ---------------------------------"
        } >> "$ERRORS_LOG"
      fi
    done
  done

  # Drop every job whose src or dst node failed to upgrade; count as failures.
  if (( ${#NODE_BAD[@]} > 0 )); then
    declare -a _kept=()
    for job in "${JOBS[@]}"; do
      IFS='|' read -r jvmid jsrc jdst jdstnum <<< "$job"
      if [[ -n "${NODE_BAD[$jsrc]:-}" || -n "${NODE_BAD[$jdst]:-}" ]]; then
        node_fail_count=$((node_fail_count + 1))
        log "FAIL     vmid=$jvmid src=$jsrc dst=$jdst rc=- node-upgrade failed (migration not attempted)"
        {
          echo
          echo "========================================================================"
          echo "FAIL  vmid=$jvmid  src=$jsrc → dst=$jdst  rc=-  $(date -u +'%Y-%m-%dT%H:%M:%SZ')"
          echo "      reason: pre-migration node dist-upgrade failed; migration not attempted"
          echo "========================================================================"
        } >> "$ERRORS_LOG"
      else
        _kept+=("$job")
      fi
    done
    JOBS=("${_kept[@]}")
  fi
fi

if (( ${#JOBS[@]} == 0 )); then
  log "no runnable jobs after node-upgrade gate (node_fail=$node_fail_count); exiting"
  log "========================================================================"
  log "SUMMARY  ok=0 fail=$node_fail_count skipped=$skipped total=$TOTAL_JOBS"
  (( node_fail_count > 0 )) && exit 1
  exit 0
fi

# ----- Scheduler ---------------------------------------------------------------
declare -A in_flight    # node -> count
declare -A pid_meta     # pid  -> "vmid|src|dst|jlog"
declare -A vmid_busy    # vmid -> 1
pending=("${JOBS[@]}")

ok_count=0; fail_count=${node_fail_count:-0}

# Trap: on Ctrl-C or fatal, kill all in-flight children, wait for their
# rollbacks to finish, and exit. The trap *must* terminate the script — if it
# returns to the main loop, the next `wait -n -p` call has nothing to reap
# (the trap's `wait` already did that) and would trip the BUG path with an
# empty `finished` pid.
_kill_children() {
  log "Signal received — terminating ${#pid_meta[@]} in-flight job(s) and exiting."
  for p in "${!pid_meta[@]}"; do
    kill -TERM "$p" 2>/dev/null || true
  done
  wait 2>/dev/null || true
  pid_meta=()
  exit 130
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
log "SUMMARY  ok=$ok_count fail=$fail_count skipped=$skipped total=$TOTAL_JOBS"
log "Master log:  $MASTER_LOG"
log "Errors log:  $ERRORS_LOG"

# EXIT trap (registered above) dumps the aggregated errors log unconditionally.

if (( fail_count > 0 )); then
  log "Some migrations failed. Review $ERRORS_LOG above for details."
  exit 1
fi
exit 0

#!/usr/bin/env bash
# migrate_vm.sh — Migrate a Proxmox VM to a different node.
#
# Usage:  migrate_vm.sh <VMID> <NEW_NODE_ID>
#
# Run on a BASE server. Resolves source/dest nodes from the local state files
# written by sync-base-nat.py:
#   - $PVE_NODES_FILE  (nodeId -> vmbr0 IPv6)         "sync nodes"
#   - $STATE_FILE      (vmid   -> current VM IPv6)    "sync"
# then orchestrates a Proxmox `pvesh remote_migrate` over SSH and reconciles
# Firestore + local NAT.
#
# Idempotent. Re-running after success is a no-op (source == dest). Re-running
# after a partial failure resumes from wherever it left off — if the VM is
# already on the destination, only the post-migration sync runs.
#
# Stopped VMs are migrated offline (no --online), then briefly started on dest
# to apply the in-guest IPv6 reconfig, then gracefully shut back down so the
# original power state is preserved.
#
# For ONLINE migrations the cutover-downtime budget is STAGED (2026-07-31):
# start at MIGRATE_DOWNTIME_INITIAL (default 15 s), and a background escalator
# raises it toward the MIGRATE_DOWNTIME ceiling (default 90 s) only while QEMU's
# "dirty sync count" shows pre-copy failing to converge. QEMU cuts over as soon
# as remaining/bandwidth fits the budget, so the old flat 90 didn't just allow
# long freezes — it caused them (25/25 defrag cutovers froze 12-90 s, median
# ~38 s). Staged, a converging guest freezes seconds; only genuine churners earn
# the old budget. The ceiling semantics are unchanged, squeezed between TWO
# independent failure modes proven empirically over this ~180 MiB/s
# cross-DC WAN:
#   (a) TOO LOW → RAM never converges → many pre-copy rounds → the tiny efidisk0
#       drive-mirror (mirrored AFTER the big data disk) sits idle through the
#       whole RAM phase until the remote_migrate websocket tunnel reaps its
#       forwarded NBD socket → "mirror-efidisk0: Input/output error (io-status:
#       ok)" at finalize (the default of 10 was far too low — Proxmox even
#       auto-raised it to ~20 and it still looped).
#   (b) TOO HIGH → Proxmox decides it can finish within the budget, SKIPS
#       pre-copy, and does one giant stop-and-copy blackout ≈ RAM_state / WAN
#       rate. A ~108 s blackout (19 GiB-state VM) survived; a ~175 s blackout
#       (31 GiB-state VM) made the DESTINATION QEMU exit on resume
#       ("VM not running", migration "finished with problems"). There is a hard
#       dest-exit / tunnel-timeout threshold somewhere in (108 s, 175 s].
# NOTE: this is NOT a kernel-skew issue (dest nodes 57 and 13 ran the identical
# kernel with opposite outcomes) and NOT a removable bwlimit (there is none) —
# it is purely blackout duration vs. that dest-exit threshold. 90 keeps the
# worst-case blackout safely under the threshold while still forcing real
# pre-copy. It is a one-time HOT freeze at switchover, never a reboot/offline.
# Very large / fast-dirtying guests that can't converge under 90 s of budget
# will fail (a) — but the verification gate (step 6) makes that a SAFE rollback,
# not a corrupted/half-migrated VM. MIGRATE_DOWNTIME=0 leaves the VM default.
#
# Before the migration phase, BOTH source and dest are `apt dist-upgrade`d (in
# that order) so dest pve-qemu-kvm is >= source's — live migration is only
# forward-compatible and independently-patched nodes drift. Any upgrade failure
# aborts the migration before the VM is touched. This never reboots the node or
# the guest. Bypass with SKIP_NODE_APT_UPGRADE=1 (QEMU parity then NOT
# guaranteed). In batch runs migrate_vms_batch.sh does this once per node up
# front, so the per-VM check here just sees "0 pending" and skips in seconds.
#
# wget https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/scripts/migrate_vm.sh
#
# ----- Error handling & logging -----------------------------------------------
#
# Every _die (fatal, exits non-zero) and _warn (non-fatal, continues) line is
# mirrored to $ERROR_LOG with a UTC timestamp + VMID + PID prefix, e.g.:
#   2026-05-02T12:17:41Z [ERROR] vmid=123 pid=22867 pvesh remote_migrate failed.
#   2026-05-02T08:03:11Z [WARN]  vmid=455 pid=18120 Could not resolve dest public IPv4...
#
# Defaults:
#   ERROR_LOG=/var/log/migrate_vm/errors.log     (parent dir is auto-created)
#
# Override per run:
#   ERROR_LOG=/tmp/today.log ./migrate_vm.sh 123 99
#
# Logging never breaks the migration: if the log path can't be written, the
# message still goes to stderr and the script continues. stdout/stderr go to
# the terminal (or the calling process) unchanged — the file is purely an
# additive review trail.
#
# ----- Batch / parallel runs --------------------------------------------------
#
# For multiple migrations, use the companion migrate_vms_batch.sh which reads
# "VMID NEW_NODE_ID" pairs from a file or stdin, resolves all sources upfront,
# parallelises with per-node + total concurrency caps, and aggregates errors
# into one review log.
#
# Examples:
#   # 1) From a file with comments + blank lines
#   cat > migrations.txt <<'TXT'
#   # one VMID NEW_NODE_ID pair per line
#   123 99
#   111 44
#   145 7
#   TXT
#   ./migrate_vms_batch.sh -f migrations.txt              # defaults: -c 2 -m 8
#
#   # 2) Higher per-node concurrency on 10 Gbps fabric
#   ./migrate_vms_batch.sh -f migrations.txt -c 3
#
#   # 3) From stdin
#   echo "201 44
#   202 44" | ./migrate_vms_batch.sh
#
#   # 4) Custom log directory
#   ./migrate_vms_batch.sh -f migrations.txt -l /var/log/migrations/2026-05-02
#
# Batch log layout (default /var/log/migrate_vm/, override with -l DIR):
#   batch-<ts>.log              master timeline: STARTED / OK / FAIL lines + summary
#   jobs/vm-<vmid>-<ts>.log     full stdout+stderr of each job (success or fail)
#   errors-<ts>.log             aggregated review log:
#                                 - full body of every FAILED job
#                                 - every WARN/ERROR line from any job
#                                   (each child runs with ERROR_LOG pointed
#                                    at this file, so non-fatal warnings from
#                                    successful jobs land here too)
#
# Batch exits 0 if every job succeeded, 1 if any failed, 2 on usage error.

set -euo pipefail

# ----- Constants ----------------------------------------------------------------
PVE_NODES_FILE="${PVE_NODES_FILE:-/var/lib/base-nat/pve_nodes.json}"
STATE_FILE="${STATE_FILE:-/var/lib/base-nat/state.json}"
SYNC_BASE_NAT="${SYNC_BASE_NAT:-/usr/local/sbin/sync-base-nat.py}"
# NAT on the PEER base is NOT pushed from here: base->base SSH is deliberately
# unauthorized (2026-08-01 key rotation). The nat64 Cloud Function converges
# every base from Firestore within seconds of the server-doc update this
# script performs as its final step.
TARGET_STORAGE="${TARGET_STORAGE:-local-zfs}"
RDP_BASE_PORT="${RDP_BASE_PORT:-20000}"
MIGRATE_DOWNTIME="${MIGRATE_DOWNTIME:-90}"  # CEILING cutover freeze (s) for ONLINE migrations. Empirically derived: a ~108s blackout survived (19GiB-state VM) but a ~175s one killed the dest QEMU on resume (31GiB-state VM) — there is a hard dest-exit/tunnel-timeout threshold in (108,175]. 90 keeps the worst-case blackout safely under it AND forces real pre-copy so large VMs don't go straight to one giant blackout. Too-high (e.g. 300) makes Proxmox skip pre-copy → multi-min blackout → dest dies; too-low → never converges → efidisk0 reap (now a SAFE rollback via the verification gate). 0 = leave VM default AND disable the staged escalation below.
MIGRATE_DOWNTIME_INITIAL="${MIGRATE_DOWNTIME_INITIAL:-15}"  # STARTING cutover budget (s). QEMU cuts over as soon as remaining_dirty/bandwidth <= budget, so a 90s budget doesn't just ALLOW a 90s freeze — it CAUSES one: QEMU stops the guest with up to 90s of data still to copy instead of pre-copying it hot. Measured on 25 defrag migrations 2026-07-31: every single cutover froze the guest 12-90s (median ~38s) under the flat 90. Starting at 15s makes a converging guest freeze <=15s (typically 2-5s: it pre-copies until ~15s of data remain); guests whose dirty rate outruns the link are caught by the escalator below, which walks the budget back up to MIGRATE_DOWNTIME — so the worst case equals the old behaviour instead of failure mode (a). 0 = no staging (set the ceiling up front, old behaviour).
DOWNTIME_ESCALATE_HARD_S="${DOWNTIME_ESCALATE_HARD_S:-600}"  # RAM-phase seconds after which the escalator jumps straight to the MIGRATE_DOWNTIME ceiling — bounds the RAM pre-copy time (the efidisk0-reap window, failure mode (a)) to roughly what the flat 90s produced. Counted from the first poll that shows RAM stats, NOT from launch: the preceding disk mirror legitimately runs 10-25+ min and must not burn this budget.
TOKEN_NAME_PREFIX="${TOKEN_NAME_PREFIX:-migrate-full}"
HOOKSCRIPT="${HOOKSCRIPT:-shared:snippets/sync-dnat.py}"
DNS6_PRIMARY="${DNS6_PRIMARY:-2a01:4ff:ff00::add:1}"
DNS6_SECONDARY="${DNS6_SECONDARY:-2a01:4ff:ff00::add:2}"
RDP_GUEST_PORT="${RDP_GUEST_PORT:-3389}"
CONNECTIVITY_TIMEOUT="${CONNECTIVITY_TIMEOUT:-120}"  # seconds — RDP listener can take a moment after the in-guest IPv6 rebind
POST_MIGRATE_RUN_TIMEOUT="${POST_MIGRATE_RUN_TIMEOUT:-300}"  # seconds — after a COMMITTED online migration, how long to wait for the dest VM to reach `running` before downgrading to a warning + fix-forward. On a slow/degraded cutover the dest can sit in `inmigrate` for minutes; the old hard 120s here false-rolled-back migrations that had actually landed (VMs 1785/1790/1791, 2026-07-01). A timeout now NEVER rolls back — the VM is already on dest.
ERROR_LOG="${ERROR_LOG:-/var/log/migrate_vm/errors.log}"

# ----- Logging helpers ---------------------------------------------------------
# All warnings + errors are mirrored to $ERROR_LOG with a timestamp + VMID for
# post-run review. Failures during file writes are silently ignored — logging
# must never break the migration.
_log_to_file() {
  local tag="$1" msg="$2" dir
  dir=$(dirname "$ERROR_LOG")
  [[ -d "$dir" ]] || mkdir -p "$dir" 2>/dev/null || return 0
  printf '%s [%s] vmid=%s pid=%s %s\n' \
    "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$tag" "${VMID:-?}" "$$" "$msg" \
    >> "$ERROR_LOG" 2>/dev/null || true
}
_die()  { _log_to_file ERROR "$*"; printf '❌ %s\n' "$*" >&2; exit 1; }
_info() { printf 'ℹ️  %s\n' "$*" >&2; }
_ok()   { printf '✅ %s\n' "$*" >&2; }
_warn() { _log_to_file WARN  "$*"; printf '⚠️  %s\n' "$*" >&2; }

usage() {
  cat >&2 <<EOF
Usage: $(basename "$0") <VMID> <NEW_NODE_ID>
  VMID         Proxmox VM ID (integer)
  NEW_NODE_ID  Destination node host_num (integer; matches the leading number
               in pve_nodes.json keys, e.g. 7 -> "0000007-AX162-R")

Reads:
  - $PVE_NODES_FILE  (nodeId -> vmbr0 IPv6, written by sync-base-nat.py sync nodes)
  - $STATE_FILE      (vmid   -> current VM IPv6, written by sync-base-nat.py sync)
EOF
  exit 2
}

# ----- Argument parsing + preflight --------------------------------------------
[[ $# -eq 2 ]] || usage
[[ "$1" =~ ^[0-9]+$ ]] || _die "VMID must be a non-negative integer; got: $1"
[[ "$2" =~ ^[0-9]+$ ]] || _die "NEW_NODE_ID must be a non-negative integer; got: $2"
VMID=$1
NEW_NODE_NUM=$2

# Per-VMID token name avoids "Token already exists" races when multiple jobs
# target the same destination in parallel (batch runner). Honour an explicit
# TOKEN_NAME if the caller set one.
TOKEN_NAME="${TOKEN_NAME:-${TOKEN_NAME_PREFIX}-${VMID}}"

[[ -f "$PVE_NODES_FILE" ]] || _die "Missing $PVE_NODES_FILE — is this a BASE server? Run sync-base-nat.py sync nodes."
[[ -f "$STATE_FILE" ]]     || _die "Missing $STATE_FILE — run sync-base-nat.py sync first."
for cmd in python3 ssh iconv base64; do
  command -v "$cmd" >/dev/null || _die "$cmd is required."
done

# Per-VMID lock so two BASE operators can't migrate the same VM in parallel.
LOCKFILE="/run/migrate_vm.${VMID}.lock"
exec 9>"$LOCKFILE" || _die "Cannot open lock file: $LOCKFILE"
command -v flock >/dev/null && { flock -n 9 || _die "Another migration is already running for VMID ${VMID} (lock=$LOCKFILE)"; }

# ----- Resolve source + destination from local state --------------------------
# Single python call: looks up dest by host_num, source by /64-prefix match
# against the VM's current IPv6 in state.json. Prints space-separated:
#   SRC_NODE SRC_IPV6 DST_NODE DST_IPV6 EXPECTED_VM_IPV6 OLD_VM_IPV6
RESOLVED=$(VMID="$VMID" NEW_NODE_NUM="$NEW_NODE_NUM" \
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

def short_prefix64_str(s):
    """First four hextets joined with ':' then '::' (e.g. 2a01:4f9:3070:2ccf::)."""
    parts = ipaddress.IPv6Address(s).exploded.split(":")
    return ":".join(p.lstrip("0") or "0" for p in parts[:4]) + "::"

# --- Destination by leading host_num in nodeId ---
dst = None
for h, ip in nodes.items():
    head = h.split("-", 1)[0]
    if head.isdigit() and int(head) == target_num:
        dst = (h, ip)
        break
if not dst:
    fail(f"NEW_NODE_ID={target_num} not found in {os.environ['PVE_NODES_FILE']}")
dst_node, dst_ipv6 = dst
try:
    ipaddress.IPv6Address(dst_ipv6)
except ValueError:
    fail(f"Dest node {dst_node} has invalid ipv6 in pve_nodes.json: {dst_ipv6!r}")

# --- Source: VM's current ipv6 in state.json -> match /64 to a node ---
vm = state.get(str(vmid)) or {}
old_vm_ipv6 = (vm.get("ipv6") or "").strip()
if not old_vm_ipv6:
    fail(f"VMID {vmid} has no ipv6 in {os.environ['STATE_FILE']} — cannot infer source node")
try:
    src_net = prefix64(old_vm_ipv6)
except ValueError:
    fail(f"VMID {vmid} ipv6 in state.json is not a valid IPv6: {old_vm_ipv6!r}")

src = None
for h, ip in nodes.items():
    try:
        if prefix64(ip) == src_net:
            src = (h, ip)
            break
    except ValueError:
        continue
if not src:
    fail(f"No node in pve_nodes shares /64 ({src_net}) with VM {vmid} ipv6 {old_vm_ipv6}")
src_node, src_ipv6 = src

if src_node == dst_node:
    # Idempotent no-op: signal with sentinel exit code 99 (caller maps to "ok, nothing to do").
    sys.stderr.write(f"Source and destination resolve to the same node ({src_node}) — nothing to do.\n")
    sys.exit(99)

expected = f"{short_prefix64_str(dst_ipv6)}{vmid:x}"
print(src_node, src_ipv6, dst_node, dst_ipv6, expected, old_vm_ipv6)
PY
) || {
  rc=$?
  if [[ $rc -eq 99 ]]; then
    # state.json says VM is already in dest's /64. Defer the Firestore
    # reconcile until after _firestore_update_servers is defined further
    # below — covers the case where a prior run was interrupted between NAT
    # sync (which writes state.json) and the Firestore update.
    SRC_EQ_DST=1
  else
    _die "Could not resolve source/destination from local state (rc=$rc)."
  fi
}
SRC_EQ_DST="${SRC_EQ_DST:-0}"

if (( SRC_EQ_DST == 0 )); then
  [[ -n "$RESOLVED" ]] || _die "Resolver produced empty output."
  read -r SRC_NODE SRC_IPV6 DST_NODE DST_IPV6 EXPECTED_VM_IPV6 OLD_VM_IPV6 <<< "$RESOLVED"
  RDP_PORT=$((RDP_BASE_PORT + VMID))
  _info "VMID:           ${VMID}"
  _info "Source node:    ${SRC_NODE} (${SRC_IPV6})"
  _info "Dest node:      ${DST_NODE} (${DST_IPV6})"
  _info "Old VM IPv6:    ${OLD_VM_IPV6}"
  _info "New VM IPv6:    ${EXPECTED_VM_IPV6}"
  _info "Public RDP port: ${RDP_PORT}"
fi

# ----- SSH multiplexing: one persistent control connection per host ------------
# Only allocate the control dir when we'll actually do SSH (skipped in the
# SRC_EQ_DST short-circuit path which only touches Firestore).
if (( SRC_EQ_DST == 0 )); then
  SSH_CTL_DIR=$(mktemp -d -t mvm.XXXXXX)
fi
SSH_BASE_OPTS=(
  -o LogLevel=ERROR
  -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null
  -o ConnectTimeout=10
  -o ServerAliveInterval=60
  -o ServerAliveCountMax=3
  -o BatchMode=yes
)
src_ssh() {
  ssh "${SSH_BASE_OPTS[@]}" \
    -o ControlMaster=auto -o ControlPath="$SSH_CTL_DIR/src" -o ControlPersist=600 \
    "root@${SRC_IPV6}" "$@"
}
dst_ssh() {
  ssh "${SSH_BASE_OPTS[@]}" \
    -o ControlMaster=auto -o ControlPath="$SSH_CTL_DIR/dst" -o ControlPersist=600 \
    "root@${DST_IPV6}" "$@"
}
_ssh_close() {
  for sock in "$SSH_CTL_DIR"/*; do
    [[ -S "$sock" ]] && ssh -o ControlPath="$sock" -O exit . 2>/dev/null || true
  done
  rm -rf "$SSH_CTL_DIR"
}

# Reads "vendor|family|model" (from /proc/cpuinfo) of one node via the given
# multiplexed ssh fn. Used by the cpu=host live-migration pre-check below.
# /proc/cpuinfo is NOT localized (unlike lscpu) so this is locale-safe. Prints
# empty / not-"a|b|c" on error, which the caller treats as "couldn't verify".
# `model name` is deliberately NOT used (it's the marketing string, e.g. "EPYC
# 9454" vs "9224", which differs between migration-compatible same-gen CPUs).
_node_cpu_sig() {
  local ssh_fn="$1"
  "$ssh_fn" '
    v=$(grep -m1 "^vendor_id"  /proc/cpuinfo | cut -d: -f2 | tr -d " \t\r\n")
    f=$(grep -m1 "^cpu family" /proc/cpuinfo | cut -d: -f2 | tr -d " \t\r\n")
    m=$(grep -m1 -E "^model[[:space:]]*:" /proc/cpuinfo | cut -d: -f2 | tr -d " \t\r\n")
    printf "%s|%s|%s" "$v" "$f" "$m"
  ' 2>/dev/null
}

# Reads the raw CPU feature `flags` line (space-separated) of one node via the
# given multiplexed ssh fn. Used by the upgrade/downgrade direction decision:
# the destination is migration-safe iff its flags are a SUPERSET of the source's
# (the source guest can only be using flags the source host has). Prints empty
# on error.
_node_cpu_flags() {
  local ssh_fn="$1"
  "$ssh_fn" 'grep -m1 "^flags" /proc/cpuinfo | cut -d: -f2' 2>/dev/null
}

# ----- Pre-migration node package convergence ---------------------------------
# Live (online) migration requires the destination's pve-qemu-kvm to be at least
# as new as the source's; independently-patched nodes drift, and a newer source
# than dest fails the migration. We converge BOTH nodes before touching the VM.
#
# On Proxmox the blessed upgrade is `apt dist-upgrade` (plain `apt upgrade` holds
# back kernel/qemu and is explicitly discouraged in the Proxmox docs), so that is
# what actually moves pve-qemu-kvm forward.
#
# Safe w.r.t. running guests: a new qemu binary only applies to processes started
# AFTER the upgrade — already-running VMs keep their existing qemu until they are
# migrated/restarted, and dist-upgrade never reboots the node or any guest.
#
# IMPORTANT: this is identical to the per-node payload in migrate_vms_batch.sh —
# keep the two in sync. Markers (APT_UPGRADE_*) are parsed by both callers.
# Set SKIP_NODE_APT_UPGRADE=1 to bypass (emergency / offline-repo situations).
_APT_REMOTE_PAYLOAD='
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

# _apt_dist_upgrade <ssh_fn> <label> — runs the payload on one node via the
# given multiplexed SSH function. _die (abort this migration) on any failure.
_apt_dist_upgrade() {
  local ssh_fn="$1" label="$2" line
  if [[ "${SKIP_NODE_APT_UPGRADE:-0}" == "1" ]]; then
    _warn "SKIP_NODE_APT_UPGRADE=1 — not upgrading ${label}; QEMU parity NOT guaranteed."
    return 0
  fi
  _info "Pre-migration apt dist-upgrade on ${label}…"
  # Stream remote output to our log; capture it too so we can assert on markers.
  local out
  out=$("$ssh_fn" "$_APT_REMOTE_PAYLOAD" 2>&1) || {
    while IFS= read -r line; do [[ -n "$line" ]] && _warn "[${label}] $line"; done <<< "$out"
    _die "apt dist-upgrade failed on ${label} — aborting migration (QEMU parity not guaranteed)."
  }
  while IFS= read -r line; do [[ -n "$line" ]] && _info "[${label}] $line"; done <<< "$out"
  if grep -q 'APT_UPGRADE_SKIP' <<< "$out"; then
    _ok "${label} already current — nothing to upgrade."
  elif grep -q 'APT_UPGRADE_DONE' <<< "$out"; then
    _ok "${label} dist-upgrade complete."
  else
    _die "apt dist-upgrade on ${label} produced no success marker — aborting (QEMU parity not guaranteed)."
  fi
}

# ----- Embedded Firestore helper (writes servers/{proxmoxId == VMID}) ----------
# Requires firebase_admin + /etc/firebase-credentials.json on BASE.
_firestore_update_servers() {
  python3 - "$@" <<'PY'
import argparse, os, sys
from pathlib import Path
try:
    import firebase_admin
    from firebase_admin import credentials, firestore
    try:
        from google.cloud.firestore_v1 import FieldFilter
        FF = True
    except ImportError:
        FF = False
except ImportError:
    sys.stderr.write("firebase_admin not installed\n"); sys.exit(3)

CREDS = os.environ.get("FIREBASE_CREDENTIALS_FILE", "/etc/firebase-credentials.json")

def init():
    if firebase_admin._apps: return True
    if not Path(CREDS).is_file():
        sys.stderr.write(f"missing creds: {CREDS}\n"); return False
    try:
        firebase_admin.initialize_app(credentials.Certificate(CREDS))
        return True
    except Exception as e:
        sys.stderr.write(f"firebase init failed: {e}\n"); return False

def docs_for_vmid(db, vmid):
    ref = db.collection("servers")
    q = ref.where(filter=FieldFilter("proxmoxId", "==", vmid)) if FF else ref.where("proxmoxId", "==", vmid)
    return list(q.stream())

p = argparse.ArgumentParser()
p.add_argument("--vmid", type=int, required=True)
p.add_argument("--maintenance", choices=("true", "false"))
p.add_argument("--node-id", default=None)
p.add_argument("--ipv6", default=None)
# --connection-url REMOVED 2026-07-07: legacy field, URL is generated everywhere.
args = p.parse_args()

if not init(): sys.exit(1)
db = firestore.client()
docs = docs_for_vmid(db, args.vmid)
if not docs:
    sys.stderr.write(f"no servers/* with proxmoxId={args.vmid}\n"); sys.exit(2)

patch = {}
if args.maintenance is not None:    patch["maintenance"] = (args.maintenance == "true")
if args.node_id is not None:
    patch["nodeId"] = args.node_id
    # Denormalize the destination node's datacenter so the panel/emails show
    # the region-aware connection URL (sqx-hel / sqx-fsn) after a migration
    # that crosses regions.
    try:
        nsnap = db.collection("proxmox_nodes").document(args.node_id).get()
        if nsnap.exists:
            ndata = nsnap.to_dict() or {}
            patch["location"] = ndata.get("location") or ""
            # D2 (2026-07-07): the node's egress IPv4 is mirrored on the
            # server doc (panel "Acceso a Internet" / broker allowlists) —
            # refresh it IMMEDIATELY on migration, same rule as nodeId/ipv6.
            if ndata.get("server_ipv4"):
                patch["publicIpv4"] = ndata["server_ipv4"]
    except Exception:
        pass
if args.ipv6 is not None:           patch["ipv6"]        = args.ipv6
if not patch:
    sys.exit(0)

for d in docs:
    db.collection("servers").document(d.id).update(patch)
sys.exit(0)
PY
}

# Deferred from the resolver: state.json says VM is already in dest's /64,
# but Firestore may still be stale (e.g. previous run was interrupted between
# NAT sync and the Firestore update). Reconcile the doc and exit before any
# SSH or rollback machinery initialises.
if (( SRC_EQ_DST == 1 )); then
  RECONCILE=$(VMID="$VMID" NEW_NODE_NUM="$NEW_NODE_NUM" PVE_NODES_FILE="$PVE_NODES_FILE" \
              python3 - <<'PY' 2>/dev/null || true
import ipaddress, json, os, sys
vmid       = int(os.environ["VMID"])
target_num = int(os.environ["NEW_NODE_NUM"])
nodes      = json.load(open(os.environ["PVE_NODES_FILE"]))
for h, ip in nodes.items():
    head = h.split("-", 1)[0]
    if head.isdigit() and int(head) == target_num:
        parts = ipaddress.IPv6Address(ip).exploded.split(":")
        prefix = ":".join(p.lstrip("0") or "0" for p in parts[:4]) + "::"
        print(h, f"{prefix}{vmid:x}"); sys.exit(0)
sys.exit(1)
PY
)
  if [[ -n "$RECONCILE" ]]; then
    read -r RC_DST_NODE RC_EXPECTED_IPV6 <<< "$RECONCILE"
    _info "Source equals destination (${RC_DST_NODE}); reconciling Firestore in case a prior run was interrupted…"
    if _firestore_update_servers --vmid "$VMID" --node-id "$RC_DST_NODE" --ipv6 "$RC_EXPECTED_IPV6" --maintenance false 2>/dev/null; then
      _ok "Firestore reconciled (or was already up-to-date)."
    else
      _warn "Firestore reconcile failed; check /etc/firebase-credentials.json."
    fi
  else
    _ok "Source equals destination — nothing to do."
  fi
  exit 0
fi

# ----- Guest-agent helpers (run PowerShell on dest VM) -------------------------
# Returns: PS process exit code (0 success, anything else = failure / signal).
# 1 if the guest agent never came up.
_dst_run_ps() {
  local ps="$1" timeout="${2:-120}"
  local b64; b64=$(printf '%s' "$ps" | iconv -t UTF-16LE | base64 -w0)
  [[ -n "$b64" ]] || { _warn "iconv/base64 failed for PS"; return 1; }

  local exec_out pid attempt=0 max_attempts=30
  while (( attempt < max_attempts )); do
    exec_out=$(dst_ssh "pvesh create /nodes/${DST_NODE}/qemu/${VMID}/agent/exec --output-format json \
                          --command powershell.exe \
                          --command -NoProfile \
                          --command -NonInteractive \
                          --command -EncodedCommand \
                          --command '${b64}' 2>&1") || true
    pid=$(printf '%s' "$exec_out" | sed -n 's/.*"pid"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' | head -1)
    if [[ -n "$pid" && "$pid" -gt 0 ]]; then break; fi
    if printf '%s' "$exec_out" | grep -qi 'guest agent is not running\|agent.*not running\|not connected'; then
      sleep 3; (( attempt++ )); continue
    fi
    _info "agent exec failed (recoverable, caller retries): ${exec_out:0:200}"
    return 1
  done
  if [[ -z "$pid" || "$pid" -le 0 ]]; then
    _info "guest agent never came up after $((max_attempts * 3))s (recoverable, caller retries)"
    return 1
  fi

  local elapsed=0 status_out exitcode
  while (( elapsed < timeout )); do
    status_out=$(dst_ssh "pvesh get /nodes/${DST_NODE}/qemu/${VMID}/agent/exec-status --output-format json --pid '${pid}'" 2>/dev/null || true)
    if printf '%s' "$status_out" | grep -qE '"exited"\s*:\s*1'; then
      exitcode=$(printf '%s' "$status_out" | sed -n 's/.*"exitcode"[[:space:]]*:[[:space:]]*\([-0-9][0-9]*\).*/\1/p' | head -1)
      [[ -z "$exitcode" ]] && exitcode=0
      return "$exitcode"
    fi
    sleep 3; (( elapsed += 3 ))
  done
  _info "PS exec timed out after ${timeout}s (recoverable, caller retries)"
  return 1
}

_wait_agent_dst() {
  local timeout="${1:-300}" elapsed=0
  while (( elapsed < timeout )); do
    if dst_ssh "qm guest cmd '${VMID}' ping" >/dev/null 2>&1; then return 0; fi
    sleep 3; (( elapsed += 3 ))
  done
  return 1
}

_wait_status_dst() {
  local want="$1" timeout="${2:-60}" elapsed=0 cur
  while (( elapsed < timeout )); do
    cur=$(dst_ssh "qm status '${VMID}'" 2>/dev/null | awk -F': ' '/status:/{print $2; exit}' | tr -d '\r' || true)
    [[ "$cur" == "$want" ]] && return 0
    sleep 3; (( elapsed += 3 ))
  done
  return 1
}

# After a COMMITTED online migration the dest VM can sit in `inmigrate` well
# beyond a few seconds on a slow/degraded cutover (multi-minute downtime, laggy
# dest). Wait up to `timeout` for `running`; if the cutover left the VM
# `stopped` (dest QEMU exited on a too-long blackout), nudge it cold ONCE — the
# disks are already on dest, so a cold start is safe. Best-effort: returns
# non-zero WITHOUT dying, so the caller fix-forwards (commit routing to dest)
# instead of rolling back to a source that remote_migrate --delete already removed.
_ensure_running_dst() {
  # NB: do NOT reference `timeout` in arithmetic on the declaration line
  # (`deadline=$(( SECONDS + timeout ))`) — under `set -u` bash expands the
  # arithmetic before the sibling local is assigned → "timeout: unbound
  # variable" abort. Use the same elapsed-counter shape as _wait_status_dst.
  local timeout="${1:-300}" elapsed=0 nudged=0 cur=""
  while (( elapsed < timeout )); do
    cur=$(dst_ssh "qm status '${VMID}'" 2>/dev/null | awk -F': ' '/status:/{print $2; exit}' | tr -d '\r' || true)
    if [[ "$cur" == "running" ]]; then return 0; fi
    if [[ "$cur" == "stopped" && $nudged -eq 0 ]]; then
      _warn "VM ${VMID} is 'stopped' on dest after cutover — nudging with 'qm start' (disks are on dest; cold start is safe)."
      dst_ssh "qm start '${VMID}'" >/dev/null 2>&1 || true
      nudged=1
    fi
    sleep 3; (( elapsed += 3 ))
  done
  # Never reached 'running'. If we never saw 'stopped' to nudge (e.g. stuck in
  # 'inmigrate'/locked), try one cold start before handing off to manual triage.
  if (( nudged == 0 )); then
    dst_ssh "qm start '${VMID}'" >/dev/null 2>&1 || true
    if _wait_status_dst running 60; then return 0; fi
  fi
  return 1
}

_verify_dest_ipv6() {
  local timeout="${1:-60}" elapsed=0 ifaces
  while (( elapsed < timeout )); do
    ifaces=$(dst_ssh "pvesh get /nodes/${DST_NODE}/qemu/${VMID}/agent/network-get-interfaces --output-format json" 2>/dev/null | tr -d '\r' || true)
    if [[ -n "$ifaces" ]] \
       && EXPECTED="$EXPECTED_VM_IPV6" JSON="$ifaces" python3 - <<'PY' >/dev/null 2>&1; then
import ipaddress, json, os, sys
try:
    target = ipaddress.IPv6Address(os.environ["EXPECTED"])
except ValueError:
    sys.exit(2)
try:
    data = json.loads(os.environ["JSON"])
except Exception:
    sys.exit(1)
ifaces = data.get("result", data) if isinstance(data, dict) else data
for iface in (ifaces or []):
    if not isinstance(iface, dict): continue
    for addr in (iface.get("ip-addresses") or []):
        if not isinstance(addr, dict): continue
        ip = (addr.get("ip-address") or "").strip().split("%")[0]
        if not ip: continue
        try:
            if ipaddress.IPv6Address(ip) == target: sys.exit(0)
        except ValueError:
            continue
sys.exit(1)
PY
      return 0
    fi
    sleep 3; (( elapsed += 3 ))
  done
  return 1
}

# ----- Cleanup / rollback state machine ----------------------------------------
TOKEN_CREATED=0
HOOKSCRIPT_DETACHED=0
MAINTENANCE_SET=0
MIGRATION_DONE=0
WAS_STOPPED=0  # 1 if the source VM was stopped pre-migration (we started it temporarily on dest)
# 1 when the migration COMMITTED but left the VM in a state the customer cannot
# reach — in practice: the guest agent was down so the in-guest IPv6 could not
# be re-bound to the destination prefix, and/or the post-migration RDP probe
# never came up. Rolling back is not possible at that point (the VM already
# lives on dest), so we finish every reconciliation step and then exit NON-ZERO
# so the caller records a FAILURE. Before this (2026-07-24) those were plain
# _warns and the script still exited 0: migrate_vms_batch counted the move as
# ok, neuravps-defrag journalled ok=1/fail=0, and a customer was left
# unreachable with every layer reporting success.
MIGRATION_DEGRADED=0
DEGRADED_REASON=""

_rollback() {
  local rc=$?
  set +e
  if (( MIGRATION_DONE == 1 )); then
    _ssh_close
    return
  fi
  _warn "Aborting (rc=${rc}); rolling back transient state…"
  if (( HOOKSCRIPT_DETACHED == 1 )); then
    _info "Re-attaching hookscript on source…"
    src_ssh "qm set '${VMID}' --hookscript '${HOOKSCRIPT}'" >/dev/null 2>&1 || true
  fi
  if (( TOKEN_CREATED == 1 )); then
    _info "Removing migration token on dest…"
    dst_ssh "pveum user token remove root@pam '${TOKEN_NAME}'" >/dev/null 2>&1 || true
  fi
  if (( MAINTENANCE_SET == 1 )); then
    _info "Clearing Firestore maintenance flag…"
    _firestore_update_servers --vmid "$VMID" --maintenance false >/dev/null 2>&1 || true
  fi
  # A failed/aborted migration can leave the BASEs' NAT entry for this VM
  # missing or stale (e.g. the aborted dest stub's stop event clobbers it via
  # the hookscript chain) even though the VM kept running on source — the
  # customer's connectionUrl then dies silently while direct-IPv6 RDP still
  # works (VM 1648, 2026-07-03). Firestore routing is untouched on rollback
  # by design, so a plain per-VM sync restores truth on THIS base. The peer's
  # entry is only ever written by the Cloud Function converging from (correct,
  # untouched) Firestore, so there is nothing to undo there.
  if [[ -x "$SYNC_BASE_NAT" ]]; then
    _info "Restoring NAT entry for ${VMID} from Firestore (local)…"
    "$SYNC_BASE_NAT" sync "$VMID" >/dev/null 2>&1 \
      || _warn "Local NAT restore failed — run: $SYNC_BASE_NAT sync ${VMID}"
  fi
  _ssh_close
}
trap _rollback EXIT
# Signal traps make $? at EXIT-trap entry deterministic. Without these, a
# SIGTERM/SIGINT mid-command (e.g. during pvesh remote_migrate) leaves $?
# at the value of the last *completed* command — which is often 0 — so the
# rollback message would misleadingly read "Aborting (rc=0)".
trap 'exit 130' INT
trap 'exit 143' TERM

# ----- SSH reachability + tool checks ------------------------------------------
src_ssh 'command -v qm && command -v pvesh' >/dev/null \
  || _die "Source ${SRC_NODE} (${SRC_IPV6}) unreachable or missing qm/pvesh"
dst_ssh 'command -v qm && command -v pveum && command -v openssl' >/dev/null \
  || _die "Dest ${DST_NODE} (${DST_IPV6}) unreachable or missing qm/pveum/openssl"

# ----- Idempotency: detect where the VM currently lives ------------------------
# `qm config <vmid>` exits 0 iff VMID exists locally on that node.
_vm_on_src=0; _vm_on_dst=0
src_ssh "qm config '${VMID}' >/dev/null 2>&1" && _vm_on_src=1 || _vm_on_src=0
dst_ssh "qm config '${VMID}' >/dev/null 2>&1" && _vm_on_dst=1 || _vm_on_dst=0

if (( _vm_on_src == 0 && _vm_on_dst == 0 )); then
  _die "VM ${VMID} not found on source (${SRC_NODE}) or dest (${DST_NODE})."
fi
if (( _vm_on_src == 1 && _vm_on_dst == 1 )); then
  _die "VM ${VMID} exists on BOTH source and dest. Manual triage required."
fi

# ----- Migration phase (skipped if VM already on dest from a prior run) --------
if (( _vm_on_src == 1 )); then
  _info "VM is on source — performing migration."

  # 0) Detect source run state to decide online vs offline migration. Stopped
  # VMs are migrated offline (no --online); we'll start them on dest after
  # migration to apply the in-guest IPv6 reconfig, then shut them back down.
  # Read-only, so it runs FIRST: the cpu=host pre-check (0a) needs it to skip
  # offline migrations (a stopped guest cold-boots on dest and picks up dest's
  # CPU features — only a LIVE migration restores vCPU state and can mismatch).
  SRC_STATUS=$(src_ssh "qm status '${VMID}'" 2>/dev/null | awk -F': ' '/status:/{print $2; exit}' | tr -d '\r' || true)
  _info "Source VM status: ${SRC_STATUS:-unknown}"

  ONLINE_FLAG="--online"
  if [[ "$SRC_STATUS" != "running" ]]; then
    ONLINE_FLAG=""
    WAS_STOPPED=1
    _info "Source VM is not running — using offline migration."
  fi

  # 0b) Memory config-vs-running pre-check — ONLINE migrations only. A pending
  # memory change (e.g. `qm set --memory` on a running VM, like the 2026-06
  # vps-e RAM standardization) leaves the RUNNING qemu with a different -m than
  # the config. remote_migrate builds the dest VM from the CONFIG, so the RAM
  # stream dies at the very end with "kvm: Size mismatch: pc.ram" — after ~40
  # wasted minutes of disk mirror (VM 1648: 4 identical failures at 38m50s).
  # Default: auto-align the config to the RUNNING value (always safe — it just
  # describes reality; re-apply the intended value as pending AFTER migrating).
  # Set MEMORY_MISMATCH_AUTOFIX=0 to abort instead.
  if [[ -n "$ONLINE_FLAG" ]]; then
    _run_m=$(src_ssh "tr '\\0' ' ' < /proc/\$(cat /var/run/qemu-server/${VMID}.pid 2>/dev/null)/cmdline 2>/dev/null" \
             | grep -oE '\-m +(size=)?[0-9]+' | grep -oE '[0-9]+' | head -1 || true)
    _cfg_m=$(src_ssh "qm config '${VMID}' 2>/dev/null" | sed -n 's/^memory:[[:space:]]*//p' | head -1 | tr -d '\r' || true)
    if [[ -n "$_run_m" && -n "$_cfg_m" && "$_run_m" != "$_cfg_m" ]]; then
      if [[ "${MEMORY_MISMATCH_AUTOFIX:-1}" == "1" ]]; then
        _warn "Memory mismatch: running -m ${_run_m} != config ${_cfg_m} MiB (pending change without reboot). AUTO-FIXING: config -> ${_run_m} MiB (+ dropping a memory-only [PENDING] entry) so the dest VM is built to match the running guest."
        src_ssh "python3 - <<'PYEOF'
lines = open('/etc/pve/qemu-server/${VMID}.conf').read().splitlines()
out, pend = [], False
for l in lines:
    if l.strip() == '[PENDING]': pend = True; continue
    if pend and l.startswith('['): pend = False
    if pend:
        if l.startswith('memory:'): continue
        out.append('[PENDING]') if '[PENDING]' not in out else None
        out.append(l); continue
    if l.startswith('memory:'): l = 'memory: ${_run_m}'
    out.append(l)
open('/etc/pve/qemu-server/${VMID}.conf','w').write('\n'.join(out).rstrip()+'\n')
PYEOF" || _die "Memory mismatch auto-fix failed — align manually: config memory=${_cfg_m} vs running ${_run_m} MiB."
        _ok "Config memory aligned to running value (${_run_m} MiB)."
        # Remember the INTENDED value: after the migration commits we re-apply
        # it on the dest as a pending change, so the customer's next reboot
        # still lands on the standardized size (e.g. vps-e 61440).
        MEMORY_INTENDED_MB="$_cfg_m"
      else
        _die "Memory mismatch: running -m ${_run_m} != config ${_cfg_m} MiB — a live migration WILL fail at the RAM phase (kvm: Size mismatch: pc.ram). Fix: align the config to the running value, or power-cycle the VM to apply the pending change. Aborting before any data is copied."
      fi
    fi
  fi

  # 0c) Guest-agent pre-check — ONLINE Windows migrations only. The post-move
  # in-guest IPv6 reconfig NEEDS the agent; with a dead one the migration
  # "succeeds" into the worst failure mode there is: VM running on dest,
  # CUSTOMER UNREACHABLE (vms 854/1023, 2026-08-01 — caught hours later by
  # conncheck). Probe BEFORE any expensive work, with retries: a busy agent
  # answers late (1023's did at ~45s) while a dead one never does.
  # MIGRATE_SKIP_AGENT_CHECK=1 overrides for moves that deliberately need no
  # reconfig (e.g. rolling a VM BACK to the node whose prefix its guest still
  # holds — the 854 recovery).
  if [[ -n "$ONLINE_FLAG" && "${MIGRATE_SKIP_AGENT_CHECK:-0}" != "1" ]]; then
    _src_ostype=$(src_ssh "qm config '${VMID}' 2>/dev/null" | sed -n 's/^ostype:[[:space:]]*//p' | tr -d '\r' || true)
    if [[ "${_src_ostype:-win}" == win* ]]; then
      _agent_ok=0
      for _t in 1 2 3; do
        if src_ssh "timeout 20 qm agent '${VMID}' ping" >/dev/null 2>&1; then
          _agent_ok=1; break
        fi
        (( _t < 3 )) && _info "Guest agent not answering (try ${_t}/3) — retrying…"
      done
      if (( _agent_ok == 1 )); then
        _ok "Guest agent answers — the in-guest IPv6 reconfig will be possible on dest."
      else
        _die "Guest agent DEAD on VM ${VMID} (3 pings over ~60s). A live migration would complete but leave the guest holding the source-prefix IP: VM running, CUSTOMER UNREACHABLE (the 854/1023 mode). Fix the agent first (or power-cycle the VM), or set MIGRATE_SKIP_AGENT_CHECK=1 if this move deliberately needs no reconfig. Aborting before any data is copied."
      fi
    fi
  fi

  # 0a) CPU compatibility pre-check — ONLINE cpu=host migrations only. A VM with
  # `cpu: host` exposes the source CPU's exact feature set to the guest; KVM live
  # migration restores that vCPU state verbatim on the destination. The rule that
  # decides success is DIRECTIONAL: the migration restores iff the DEST CPU is a
  # feature SUPERSET of the source's.
  #   - SAME cpu / UPGRADE (dest has >= the source's features) → restores fine.
  #   - DOWNGRADE (dest is MISSING a feature the running guest has) → QEMU aborts
  #     at the resume handshake ON THE DEST ("kvm: Restoring registers after init:
  #     Failed to set special registers: Invalid argument") and the VM lands
  #     STOPPED on dest. This is exactly what hit VMs 708/1670: an EPYC 9454 Genoa
  #     guest pushed onto an EPYC 7401P Naples node (Naples lacks AVX-512 etc.).
  # `pvesh remote_migrate` does NO cross-node CPU check, so we do it here — BEFORE
  # the dist-upgrade and any disk copy, so a block is a clean no-op abort.
  #
  # How we decide: first compare vendor+family+model (from /proc/cpuinfo, NOT the
  # marketing name — EPYC 9454 and 9224 are both AMD family 25 model 17 "Genoa"
  # and identical here). If they match → identical CPU → allow. If they differ we
  # don't block blindly (older→newer is the SAFE direction): we compare the actual
  # CPU feature FLAGS — if dest is missing any non-trivial flag the source has it's
  # a DOWNGRADE → block; otherwise it's an upgrade/lateral → allow (with a warning;
  # the guest keeps presenting the source CPU until it's rebooted on dest). Cross-
  # vendor (AMD<->Intel) and unreadable flags → block. Offline (stopped) source
  # skips all of this — a cold boot on dest picks up dest's features in any
  # direction. Named/baseline cpu models (kvm64, x86-64-v2/-v3/-v4, qemu64,
  # EPYC-*, etc.) also skip — QEMU pins their feature set regardless of host, so
  # they're migration-safe by design; only `host`/`max` carry the live guest's
  # real CPU features and need the check.
  if [[ -n "$ONLINE_FLAG" ]]; then
    CPU_RAW=$(src_ssh "qm config '${VMID}' 2>/dev/null" | sed -n 's/^cpu:[[:space:]]*//p' | head -1 | tr -d '\r' || true)
    CPU_MODEL=""
    if [[ -n "$CPU_RAW" ]]; then
      _cpu_first="${CPU_RAW%%,*}"                       # strip ",flags=..." etc.
      if [[ "$_cpu_first" == cputype=* ]]; then CPU_MODEL="${_cpu_first#cputype=}"; else CPU_MODEL="$_cpu_first"; fi
    fi
    # The config is a statement of intent, not of fact: `qm set --cpu` on a
    # running VM only takes effect at its next start, so a guest whose config
    # already reads x86-64-v3 can still be EXECUTING with host-passthrough
    # CPUID. Deciding from the config alone would skip this entire check for
    # exactly those VMs — the ones mid-transition from `host` to a baseline
    # model, i.e. the ones being moved onto a different CPU type in the first
    # place. Trust the live QEMU cmdline instead (same source of truth as the
    # memory check in 0b); fall back to the config if it can't be read.
    _run_cpu=$(src_ssh "tr '\\0' '\\n' < /proc/\$(cat /var/run/qemu-server/${VMID}.pid 2>/dev/null)/cmdline 2>/dev/null" \
               | grep -A1 -x -- '-cpu' | tail -1 | tr -d '\r' || true)
    if [[ -n "$_run_cpu" && "$_run_cpu" != "-cpu" ]]; then
      _run_model="${_run_cpu%%,*}"
      if [[ -n "$_run_model" && "$_run_model" != "$CPU_MODEL" ]]; then
        _warn "VM config says cpu=${CPU_MODEL:-<proxmox default>} but the RUNNING guest was started with '${_run_model}' (pending change, applies on its next reboot) — the compatibility check follows what is actually running."
        CPU_MODEL="$_run_model"
      fi
    fi
    _cpu_lc=$(printf '%s' "$CPU_MODEL" | tr '[:upper:]' '[:lower:]')
    if [[ "$_cpu_lc" == "host" || "$_cpu_lc" == "max" ]]; then
      _info "VM uses cpu=${CPU_MODEL} (host passthrough) on a LIVE migration — verifying source/dest CPU compatibility…"
      SRC_CPU_SIG=$(_node_cpu_sig src_ssh || true)
      DST_CPU_SIG=$(_node_cpu_sig dst_ssh || true)
      [[ "$SRC_CPU_SIG" == *"|"*"|"* ]] || _die "Could not read source ${SRC_NODE} CPU signature for the cpu=host compatibility check (got: '${SRC_CPU_SIG}'). Aborting before any VM state is touched."
      [[ "$DST_CPU_SIG" == *"|"*"|"* ]] || _die "Could not read dest ${DST_NODE} CPU signature for the cpu=host compatibility check (got: '${DST_CPU_SIG}'). Aborting before any VM state is touched."
      if [[ "$SRC_CPU_SIG" == "$DST_CPU_SIG" ]]; then
        _ok "CPU identical — source and dest are both [${SRC_CPU_SIG//|/ }] (vendor family model); live cpu=host migration is safe."
      else
        # CPUs differ. Cross-vendor is never compatible; otherwise decide
        # upgrade-vs-downgrade by comparing actual feature flags.
        SRC_VENDOR="${SRC_CPU_SIG%%|*}"; DST_VENDOR="${DST_CPU_SIG%%|*}"
        if [[ "$SRC_VENDOR" != "$DST_VENDOR" ]]; then
          _die "CPU mismatch (cross-vendor) — refusing live cpu=host migration. Source ${SRC_NODE} is ${SRC_VENDOR}, dest ${DST_NODE} is ${DST_VENDOR}; a guest started on one vendor cannot resume on the other. Stop the VM and migrate offline, or use a baseline cpu model. Aborting before any VM state is touched."
        fi
        _info "CPUs differ (src [${SRC_CPU_SIG//|/ }] → dst [${DST_CPU_SIG//|/ }]); comparing feature flags to tell an upgrade from a downgrade…"
        SRC_FLAGS=$(_node_cpu_flags src_ssh || true)
        DST_FLAGS=$(_node_cpu_flags dst_ssh || true)
        [[ -n "${SRC_FLAGS// /}" && -n "${DST_FLAGS// /}" ]] || _die "Could not read CPU feature flags from source and/or dest for the upgrade/downgrade decision. Refusing to guess. Stop the VM and migrate OFFLINE (safe in any direction), or pick a same/newer-generation dest. Aborting before any VM state is touched."
        # Set-diff with a noise ignore-list: security mitigations, p-state/power,
        # and Linux-synthetic flags vary host-to-host for reasons unrelated to
        # migratable guest state. MISSING = real features the source has that the
        # dest lacks → non-empty means DOWNGRADE. Only flags we're certain are
        # non-ISA are ignored, so a real ISA gap (e.g. avx512*) is never masked.
        _flagcmp=$(SRC_FLAGS="$SRC_FLAGS" DST_FLAGS="$DST_FLAGS" python3 - <<'PY' 2>/dev/null || true
import os
IGNORE = set('''
ibpb ibrs ibrs_enhanced ibrs_fw stibp spec_ctrl intel_stibp amd_ibpb amd_ibrs
ssbd amd_ssbd amd_stibp virt_ssbd ssb_no pti kaiser md_clear flush_l1d
arch_capabilities tsx_async_abort srbds mmio_stale_data spec_store_bypass
gather_data_sampling bhi rfds reg_file_data_sampling rsb_ctxsw retpoline
cpb hw_pstate amd_pstate hwp hwp_notify hwp_act_window hwp_epp hwp_pkg_req
hwp_hint aperfmperf dtherm ida arat pln pts tm tm2 acpi est eist epb ibs irperf
cpuid cpuid_fault rep_good nopl eagerfpu xtopology amd_dcm extd_apicid
amd_lbr_v2 amd_lbr_pmc_freeze
sme sev sev_es sev_snp sme_coherent
'''.split())
src = set(os.environ.get("SRC_FLAGS", "").split())
dst = set(os.environ.get("DST_FLAGS", "").split())
print("MISSING:" + "".join(" " + f for f in sorted((src - dst) - IGNORE)))
print("GAINED:"  + "".join(" " + f for f in sorted((dst - src) - IGNORE)))
PY
)
        [[ "$_flagcmp" == *MISSING:* ]] || _die "Feature-flag comparison failed (could not run the flag diff). Refusing to guess direction. Stop the VM and migrate OFFLINE. Aborting before any VM state is touched."
        _missing=$(printf '%s\n' "$_flagcmp" | sed -n 's/^MISSING://p')
        _gained=$(printf '%s\n' "$_flagcmp" | sed -n 's/^GAINED://p')
        if [[ -n "$_missing" ]]; then
          _die "CPU DOWNGRADE blocked — dest ${DST_NODE} [${DST_CPU_SIG//|/ }] is MISSING feature(s) the running guest has:${_missing}. Live-migrating a cpu=host guest onto a CPU that lacks these makes QEMU die at the resume handshake and leaves VM ${VMID} STOPPED on dest (the VMs 708/1670 failure). Options: (1) migrate to a same-or-newer generation dest (a superset of the source); (2) stop the VM and re-run — OFFLINE migration is safe in any direction; or (3) set a baseline cpu model both hosts support, e.g. 'qm set ${VMID} --cpu x86-64-v2-AES'. Aborting before any VM state is touched."
        fi
        _warn "Cross-generation UPGRADE: dest ${DST_NODE} [${DST_CPU_SIG//|/ }] is a feature superset of source ${SRC_NODE} [${SRC_CPU_SIG//|/ }]${_gained:+ (dest adds:${_gained})} — live cpu=host migration permitted. NOTE: VM ${VMID} keeps presenting the SOURCE CPU until it is rebooted on dest, so it won't use the new features until then."
      fi
    else
      _info "VM cpu model=${CPU_MODEL:-<proxmox default>} (not host passthrough) — CPU compatibility pre-check not required."
    fi
  fi

  # 0b) Converge packages on BOTH nodes before migrating so dest pve-qemu-kvm is
  # >= source's (live-migration is forward-compatible only). Source first, dest
  # last, so dest's apt snapshot is the newest and can't end up behind source.
  # Aborts the migration on any failure.
  _apt_dist_upgrade src_ssh "source ${SRC_NODE}"
  _apt_dist_upgrade dst_ssh "dest ${DST_NODE}"

  # 0c) Set the cutover downtime budget — see the header for the full
  # failure-mode box (too low → efidisk0 reap; too high → giant blackout →
  # dest QEMU exit). STAGED since 2026-07-31: start at MIGRATE_DOWNTIME_INITIAL
  # (guest freezes seconds, not tens of seconds) and let the escalator walk it
  # up toward the MIGRATE_DOWNTIME ceiling only when the guest's dirty rate
  # keeps pre-copy from converging — the flat 90 s didn't just allow long
  # freezes, it CAUSED them (QEMU stops the guest as soon as remaining/bw fits
  # the budget: 25/25 defrag cutovers froze 12-90 s). Travels with the VM
  # config to dest. Irrelevant for offline migrations. Non-fatal;
  # MIGRATE_DOWNTIME=0 skips everything (leave VM default).
  _DT_STAGED=0
  if [[ -n "$ONLINE_FLAG" && "${MIGRATE_DOWNTIME:-0}" != "0" ]]; then
    _dt_start="$MIGRATE_DOWNTIME"
    if [[ "${MIGRATE_DOWNTIME_INITIAL:-0}" != "0" ]]; then
      _dt_start="$MIGRATE_DOWNTIME_INITIAL"
      _DT_STAGED=1
    fi
    if src_ssh "qm set '${VMID}' --migrate_downtime '${_dt_start}'" >/dev/null 2>&1; then
      _ok "Set migrate_downtime=${_dt_start}s on source (staged=${_DT_STAGED}, ceiling=${MIGRATE_DOWNTIME}s)."
    else
      _warn "Could not set migrate_downtime on source; continuing (busy VMs may fail to converge over a slow link)."
    fi
  fi

  # 1) Firestore: set maintenance=true (advisory; non-fatal)
  if _firestore_update_servers --vmid "$VMID" --maintenance true 2>/dev/null; then
    MAINTENANCE_SET=1
    _ok "Firestore maintenance=true"
  else
    _warn "Firestore maintenance=true failed (no creds or no doc); continuing."
  fi

  # 2) Detach hookscript on source — otherwise sync-dnat fires on remote_migrate's
  # implicit post-stop and clobbers Firestore status mid-migration.
  src_ssh "qm set '${VMID}' --delete hookscript" >/dev/null 2>&1 || true
  HOOKSCRIPT_DETACHED=1
  _ok "Hookscript detached on source."

  # 3) Token on dest (clean any stale, then create fresh)
  dst_ssh "pveum user token remove root@pam '${TOKEN_NAME}'" >/dev/null 2>&1 || true
  _info "Creating migration token on dest…"
  TOKEN_OUT=$(dst_ssh "pveum user token add root@pam '${TOKEN_NAME}' --privsep=0 2>&1") \
    || _die "pveum token add failed on dest: ${TOKEN_OUT}"
  TOKEN_SECRET=$(printf '%s' "$TOKEN_OUT" | grep -oE '[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}' | head -1)
  [[ -n "$TOKEN_SECRET" ]] || _die "Could not parse token secret from pveum output."
  TOKEN_CREATED=1
  _ok "Token created on dest."

  # 4) Dest pveproxy SSL fingerprint — read from cert file (faster + more reliable
  # than openssl s_client). Try uploaded cert first, fall back to default.
  _info "Reading dest pveproxy SSL fingerprint…"
  FINGERPRINT=$(dst_ssh '
    for f in /etc/pve/local/pveproxy-ssl.pem /etc/pve/local/pve-ssl.pem; do
      if [ -f "$f" ]; then
        openssl x509 -in "$f" -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2 && exit 0
      fi
    done
    exit 1' | tr -d '\r ' || true)
  [[ -n "$FINGERPRINT" ]] || _die "Could not read dest pveproxy SSL fingerprint."

  # 4b) Strip stale `snaptime` annotation from the TOP of the source VM config.
  # When a VM has been rolled back to a snapshot, Proxmox leaves a breadcrumb at
  # the top of the config (`snaptime: <unix_ts>` — when the snapshot the current
  # state came from was taken). This field is flagged `property_protected => 1`
  # in QemuServer's schema, which accepts ONLY a strict `root@pam` ticket — NEVER
  # an API token, regardless of `--privsep`. `pvesh remote_migrate` pushes the
  # source config to dest through the token we created in step 3
  # (`root@pam!<TOKEN_NAME>`), so dest aborts AFTER disks have already streamed
  # with:  failed to handle 'config' command - only root can set 'snaptime' config
  # The annotation is informational — removing it doesn't affect VM operation,
  # snapshot integrity, or rollback ability. We rewrite the TOP section only
  # (lines before the first `[snapshot_name]` header) so legitimate per-snapshot
  # snaptime inside those blocks is untouched. Idempotent.
  # NOTE: `parent: <snapname>` is NOT protected and migrates fine — leave it.
  _info "Checking source VM config for stale 'snaptime' (would abort remote_migrate via token auth)…"
  strip_out=$(src_ssh "
    CFG=/etc/pve/qemu-server/${VMID}.conf
    [ -f \"\$CFG\" ] || { echo NOFILE; exit 0; }
    TMP=\$(mktemp) || { echo NOTMP; exit 0; }
    if awk 'BEGIN{top=1; f=0} /^\\[/{top=0} top && /^snaptime:/{f=1; next} {print} END{exit f==0}' \"\$CFG\" > \"\$TMP\"; then
      if cat \"\$TMP\" > \"\$CFG\"; then echo STRIPPED; else echo WRITEFAIL; fi
    else
      echo CLEAN
    fi
    rm -f \"\$TMP\"
  " 2>&1)
  case "$strip_out" in
    STRIPPED) _ok   "Stripped stale 'snaptime' from source VM config (was a leftover from a prior snapshot rollback)." ;;
    CLEAN)    _info "No stale 'snaptime' in source VM config — nothing to strip." ;;
    NOFILE)   _warn "Source VM config not found at /etc/pve/qemu-server/${VMID}.conf; skipping strip." ;;
    *)        _warn "Could not strip 'snaptime' on source (result=${strip_out:-empty}); remote_migrate may fail with 'only root can set snaptime'." ;;
  esac

  # Downtime escalator (2026-07-31): runs BESIDE the migration and walks the
  # cutover budget up from MIGRATE_DOWNTIME_INITIAL toward the MIGRATE_DOWNTIME
  # ceiling only when the guest's dirty rate keeps pre-copy from converging.
  # Convergence signal = QEMU's "dirty sync count" (memory iterations): a calm
  # guest cuts over during rounds 1-2 and never meets the escalator; a churner
  # accumulates rounds and earns a bigger budget stepwise (3→30s, 5→60s,
  # 8→ceiling). DOWNTIME_ESCALATE_HARD_S bounds total pre-copy time — the
  # efidisk0-reap window (failure mode (a)) — by jumping to the ceiling
  # outright, so the worst case degrades to exactly the old flat-90 behaviour.
  # HMP `migrate_set_parameter downtime-limit` takes MILLISECONDS and applies
  # to the RUNNING migration (validated on PVE 9.2.5). Self-terminates when
  # the migration leaves the active states or the source VM disappears
  # (remote_migrate --delete); the parent also reaps it after pvesh returns.
  _downtime_escalator() {
    set +e   # parent runs -euo pipefail; in here a failed poll must never kill the loop
    local ram_ts cur target dirty status out elapsed
    ram_ts=""   # set when the RAM phase is first observed — the hard timer
                # counts from THERE, not from launch: the disk mirror that
                # precedes it ran 10-25 min on today's defrag jobs, and
                # counting it would jump every big VM straight to the ceiling
                # before a single RAM round had run.
    cur="$MIGRATE_DOWNTIME_INITIAL"
    for _ in $(seq 1 540); do   # ~3 h self-cap at 20 s cadence (covers the
      # disk phase of a full zvol; must outlive it to see the RAM rounds)
      sleep 20
      out=$(src_ssh "printf 'info migrate\n' | timeout 10 qm monitor '${VMID}'" 2>/dev/null)
      if [[ -z "$out" ]]; then
        continue   # SSH hiccup or source VM already deleted; parent reaps us
      fi
      status=$(grep -oE 'Migration status: [a-z-]+' <<<"$out" | awk '{print $3}')
      case "$status" in
        active|postcopy-active|device|setup|cancelling) ;;
        *) return 0 ;;   # completed/failed/none: nothing left to escalate
      esac
      grep -q 'transferred ram:' <<<"$out" && [[ -z "$ram_ts" ]] && ram_ts=$(date +%s)
      [[ -z "$ram_ts" ]] && continue   # still in the disk phase: nothing to escalate
      dirty=$(grep -oE 'dirty sync count: [0-9]+' <<<"$out" | grep -oE '[0-9]+$')
      elapsed=$(( $(date +%s) - ram_ts ))
      target="$cur"
      if [[ "$elapsed" -ge "$DOWNTIME_ESCALATE_HARD_S" ]] || [[ -n "$dirty" && "$dirty" -ge 8 ]]; then
        target="$MIGRATE_DOWNTIME"
      elif [[ -n "$dirty" && "$dirty" -ge 5 ]]; then
        target=60
      elif [[ -n "$dirty" && "$dirty" -ge 3 ]]; then
        target=30
      fi
      [[ "$target" -gt "$MIGRATE_DOWNTIME" ]] && target="$MIGRATE_DOWNTIME"
      if [[ "$target" -gt "$cur" ]]; then
        if src_ssh "printf 'migrate_set_parameter downtime-limit %s\n' '$(( target * 1000 ))' | timeout 10 qm monitor '${VMID}'" >/dev/null 2>&1; then
          _info "downtime-escalator: budget ${cur}s → ${target}s (dirty rounds=${dirty:-?}, elapsed=${elapsed}s — guest dirties memory faster than the link drains it)."
          cur="$target"
        fi
      fi
      if [[ "$cur" -ge "$MIGRATE_DOWNTIME" ]]; then
        return 0   # at ceiling: nothing left to do
      fi
    done
  }

  # 5) pvesh remote_migrate (deletes source after success; --online iff src running)
  TARGET_HOST="[${DST_IPV6}]"
  _info "Starting pvesh remote_migrate (${SRC_NODE} → ${DST_NODE}, mode=${ONLINE_FLAG:-offline})…"
  _ESC_PID=""
  if [[ -n "$ONLINE_FLAG" && "${_DT_STAGED:-0}" == "1" ]]; then
    _downtime_escalator &
    _ESC_PID=$!
    _info "downtime-escalator armed (pid ${_ESC_PID}): start ${MIGRATE_DOWNTIME_INITIAL}s, ceiling ${MIGRATE_DOWNTIME}s, hard-escalate at ${DOWNTIME_ESCALATE_HARD_S}s."
  fi
  _migrate_rc=0
  src_ssh "pvesh create '/nodes/${SRC_NODE}/qemu/${VMID}/remote_migrate' \
            --target-bridge=1 \
            --target-endpoint='apitoken=PVEAPIToken=root@pam!${TOKEN_NAME}=${TOKEN_SECRET},host=${TARGET_HOST},fingerprint=${FINGERPRINT}' \
            --target-storage='${TARGET_STORAGE}' \
            ${ONLINE_FLAG} \
            --delete" \
    || _migrate_rc=$?
  if [[ -n "$_ESC_PID" ]]; then
    kill "$_ESC_PID" 2>/dev/null || true
    wait "$_ESC_PID" 2>/dev/null || true
  fi
  [[ "$_migrate_rc" -eq 0 ]] || _die "pvesh remote_migrate failed."
  _ok "remote_migrate command returned."

  # 6) Verify the migration ACTUALLY landed. `pvesh remote_migrate` exits 0 even
  # when the task ends in "migration finished with problems" (the CLI returns
  # the worker UPID, not the task result), so exit code is not trustworthy.
  # Check ground truth instead: with --delete a successful migration leaves the
  # VM present on dest and gone from source. If that does not hold, the VM was
  # kept on source — _die here (BEFORE MIGRATION_DONE=1) so the existing
  # _rollback re-attaches the hookscript on source and leaves Firestore routing
  # (nodeId/ipv6) untouched; only the advisory maintenance flag is reverted.
  _post_on_dst=0; _post_on_src=0
  dst_ssh "qm config '${VMID}' >/dev/null 2>&1" && _post_on_dst=1 || _post_on_dst=0
  src_ssh "qm config '${VMID}' >/dev/null 2>&1" && _post_on_src=1 || _post_on_src=0
  if (( _post_on_dst == 0 || _post_on_src == 1 )); then
    _die "remote_migrate did not complete (on_dst=${_post_on_dst} on_src=${_post_on_src}) — VM ${VMID} was kept on source ${SRC_NODE}. See the migration log above for the underlying error. Rolling back transient changes; Firestore routing left unchanged."
  fi
  # GROUND TRUTH above proved the VM is on dest and GONE from source: the
  # migration is committed and IRREVERSIBLE (remote_migrate --delete removed the
  # source copy). Disarm the source rollback NOW — before the resume wait — so a
  # slow or failed *resume* is fixed forward on dest, never reverted to a source
  # that no longer holds the VM. Reverting routing to the deleted source is the
  # stale-routing bug that stranded VMs 1785/1790/1791 (2026-07-01): each had
  # actually migrated, but a long cutover kept them in `inmigrate` past the old
  # hard 120s gate, so the wrapper "rolled back" and left routing on the source.
  MIGRATION_DONE=1

  if [[ -n "$ONLINE_FLAG" ]]; then
    # After "migration finished successfully" the dest VM stays in `inmigrate`
    # until QEMU resumes it; on a slow/degraded cutover that can take minutes.
    # Wait generously and nudge a `stopped` VM with `qm start` (see helper). A
    # timeout is now a WARNING + fix-forward, not a rollback — the VM is on dest
    # regardless, and routing is committed to dest below (gated on NAT, not on
    # the guest being up).
    if _ensure_running_dst "$POST_MIGRATE_RUN_TIMEOUT"; then
      _ok "VM ${VMID} is running on dest ${DST_NODE}."
    else
      _post_run=$(dst_ssh "qm status '${VMID}'" 2>/dev/null | awk -F': ' '/status:/{print $2; exit}' | tr -d '\r' || true)
      _warn "VM ${VMID} landed on dest ${DST_NODE} but is '${_post_run:-unknown}', not 'running', after ${POST_MIGRATE_RUN_TIMEOUT}s (+ cold-start nudge). Migration is COMMITTED (the VM lives on dest); routing will still be pointed at dest below, but the in-guest IPv6 reconfig is skipped while the guest is down. MANUAL: bring VM ${VMID} up on ${DST_NODE}, then re-run 'migrate_vm.sh ${VMID} ${NEW_NODE_NUM}' to finish the in-guest reconfig."
    fi
  fi
  _ok "Verified VM ${VMID} is on dest ${DST_NODE}."

  _vm_on_dst=1; _vm_on_src=0
else
  _info "VM is already on dest — skipping migration; running post-migration sync."
fi

# Belt-and-suspenders: also disarm the rollback for the "already on dest" branch
# (the migrated branch already set MIGRATION_DONE=1 right after the ground-truth
# check, before the resume wait). Past this point any failure is a warning, never
# a source-side rollback.
MIGRATION_DONE=1

# ----- EFI 2023-cert enrollment (opportunistic, offline path only) -------------
# When an OFFLINE-migrated VM is momentarily stopped on dest (before we start it
# for the in-guest reconfig), enroll Microsoft's UEFI 2023 certificates into its
# efidisk if missing. Rationale: the 2011 certs shipped with older VMs have
# expired; an expired cert in `db` still BOOTS fine (UEFI ignores CA expiry at
# verification), but a future bootloader signed only with the 2023 CA would not
# validate. `qm enroll-efi-keys` is idempotent (skips when the ms-cert=2023k
# marker is already present) and requires the VM stopped + no config lock — both
# true here. Best-effort: any failure is a warning, never fatal (the VM boots
# with the old certs regardless). SAFETY: the only hazard is BitLocker (a
# Secure-Boot key change re-seals the TPM → recovery-key prompt), and the fleet
# runs Windows Server WITHOUT the BitLocker feature installed (audited
# 2026-07-04). If BitLocker is ever allowed, gate this on an in-guest check
# performed BEFORE the source shutdown. Toggle with ENROLL_EFI_ON_MIGRATE=0.
ENROLL_EFI_ON_MIGRATE="${ENROLL_EFI_ON_MIGRATE:-1}"
_enroll_efi_dst() {
  [[ "$ENROLL_EFI_ON_MIGRATE" == "1" ]] || return 0
  local _cfg
  _cfg=$(dst_ssh "qm config '${VMID}'" 2>/dev/null || true)
  # Only OVMF VMs with pre-enrolled keys and WITHOUT the 2023 marker qualify.
  grep -q "^efidisk0:.*pre-enrolled-keys=1" <<<"$_cfg" || return 0
  if grep -q "^efidisk0:.*ms-cert=2023" <<<"$_cfg"; then
    return 0  # already enrolled — nothing to do
  fi
  if dst_ssh "qm enroll-efi-keys '${VMID}'" >/dev/null 2>&1; then
    _ok "EFI: enrolled UEFI 2023 certs on dest (was missing)."
  else
    _warn "EFI: enroll-efi-keys failed on dest — VM boots with existing certs; retried on a future controlled shutdown."
  fi
}

# ----- Post-migration sync (always idempotent) ---------------------------------

# If we migrated a stopped VM (offline), start it on dest so the guest agent
# is reachable for the in-guest IPv6 reconfig. We restore "stopped" at the end.
if (( WAS_STOPPED == 1 )); then
  _enroll_efi_dst
  # --- Operator procedure (2026-07-13): the temporary power-on of a STOPPED
  # trading VM must not let MetaTrader/EAs reach the internet (an auto-started
  # terminal could open trades). Save the VM's original firewall config, force
  # the datacenter-wide `vm-no-internet` security group ON (+ NIC firewall=1),
  # start -> in-guest IPv6 reconfig -> shutdown, then restore EXACTLY the
  # original firewall state (file content and net0 string).
  NOINT_FW="/etc/pve/firewall/${VMID}.fw"
  NOINT_ORIG_B64=$(dst_ssh "[ -f '${NOINT_FW}' ] && base64 -w0 '${NOINT_FW}' || true" 2>/dev/null)
  NOINT_NET0_ORIG=$(dst_ssh "qm config '${VMID}' 2>/dev/null" | sed -n 's/^net0: //p' | tr -d '\r')
  NOINT_NET0_MODIFIED=0
  dst_ssh "grep -q '^\[group vm-no-internet\]' /etc/pve/firewall/cluster.fw 2>/dev/null" \
    || _warn "cluster.fw has no [group vm-no-internet] on dest — the guard rule will be a no-op."
  if [[ -n "$NOINT_NET0_ORIG" && "$NOINT_NET0_ORIG" != *firewall=1* ]]; then
    if dst_ssh "qm set '${VMID}' --net0 '${NOINT_NET0_ORIG},firewall=1'" >/dev/null 2>&1; then
      NOINT_NET0_MODIFIED=1
    else
      _warn "could not set firewall=1 on net0 — no_internet guard may not filter."
    fi
  fi
  dst_ssh "printf '[OPTIONS]\nenable: 1\n\n[RULES]\nGROUP vm-no-internet\n' > '${NOINT_FW}'" >/dev/null 2>&1 \
    && _ok "Firewall vm-no-internet ON for the temporary power-on." \
    || _warn "could not write ${NOINT_FW} — proceeding without the internet guard."
  _info "Starting VM ${VMID} on dest to apply in-guest reconfig (was stopped pre-migration)…"
  if dst_ssh "qm start '${VMID}'" >/dev/null 2>&1; then
    _ok "qm start issued."
    if _wait_agent_dst 300; then
      _ok "Guest agent is responding on dest."
    else
      _warn "Guest agent did not respond within 5m — in-guest reconfig will likely fail; continuing."
    fi
  else
    _warn "qm start failed on dest; in-guest reconfig will be skipped — VM will remain stopped."
  fi
fi

DST_STATUS=$(dst_ssh "qm status '${VMID}'" 2>/dev/null | awk -F': ' '/status:/{print $2; exit}' | tr -d '\r' || true)
OSTYPE=$(dst_ssh "qm config '${VMID}'" 2>/dev/null | awk -F': ' '/^ostype:/{print $2; exit}' | tr -d '\r' || true)
_info "Dest VM: status=${DST_STATUS:-unknown} ostype=${OSTYPE:-unknown}"

# In-guest IPv6 reconfig (Windows only, running VM only).
# Gateway is the canonical "<prefix>::1" of the dest /64. We derive it from
# EXPECTED_VM_IPV6 ("<prefix>::<vmid_hex>") rather than DST_IPV6 because
# pve_nodes.json sometimes records the node's host address (::2) which is
# NOT the gateway VMs should use. The PS script is idempotent: Test-Configured
# short-circuits the rewrite, but DHCPv4 is always renewed because the node
# changed and the old lease comes from a different dnsmasq.
DST_GATEWAY="${EXPECTED_VM_IPV6%::*}::1"

# If the memory pre-check auto-aligned the config to the running value, re-apply
# the ORIGINAL (intended) config value on the dest as a pending change — the VM
# keeps running at its current size, and the customer's next own reboot applies
# the standardized value (operator request 2026-07-03: "restaurar la RAM de 60
# por si el cliente decide reiniciar").
if [[ -n "${MEMORY_INTENDED_MB:-}" ]]; then
  if dst_ssh "qm set '${VMID}' --memory '${MEMORY_INTENDED_MB}'" >/dev/null 2>&1; then
    _ok "Intended memory (${MEMORY_INTENDED_MB} MiB) re-applied as PENDING on dest — applies at the guest's next power cycle."
  else
    _warn "Could not re-apply intended memory ${MEMORY_INTENDED_MB} MiB on dest — run: qm set ${VMID} --memory ${MEMORY_INTENDED_MB}"
  fi
fi

# VPS-E balloon adaptation across the EX44<->AX162 boundary (operator,
# 2026-07-03). Dedicated EX44s run the vps-e with balloon == memory (the whole
# box is theirs; no deflation). Shared AX162s pack vps-e with ballooning down
# to the plan's ram_min (18 GB) so KSM/zswap can breathe. Cross-family moves
# are offline-only (Intel<->AMD blocks live), so the qm set lands cleanly on a
# stopped VM. Same-family moves no-op. VM is a vps-e iff its name is "E-*".
VPSE_BALLOON_MIN_MB="${VPSE_BALLOON_MIN_MB:-18432}"
_vm_name=$(dst_ssh "qm config '${VMID}'" 2>/dev/null | sed -n 's/^name:[[:space:]]*//p' | head -1 | tr -d '\r' || true)
if [[ "$_vm_name" == E-* ]]; then
  _fam() { case "$1" in *EX44*) echo EX44 ;; *AX162*) echo AX162 ;; *) echo other ;; esac; }
  _src_fam=$(_fam "$SRC_NODE"); _dst_fam=$(_fam "$DST_NODE")
  if [[ "$_src_fam" != "$_dst_fam" && "$_dst_fam" != "other" ]]; then
    _mem_mb=$(dst_ssh "qm config '${VMID}'" 2>/dev/null | sed -n 's/^memory:[[:space:]]*//p' | head -1 | tr -d '\r' || true)
    if [[ "$_dst_fam" == "AX162" ]]; then _target_balloon="$VPSE_BALLOON_MIN_MB"; else _target_balloon="${_mem_mb:-61440}"; fi
    if dst_ssh "qm set '${VMID}' --balloon '${_target_balloon}'" >/dev/null 2>&1; then
      _ok "vps-e balloon adapted for ${_dst_fam} dest: balloon=${_target_balloon} MiB (memory=${_mem_mb:-?})."
    else
      _warn "Could not set balloon=${_target_balloon} on dest — set it manually: qm set ${VMID} --balloon ${_target_balloon}"
    fi
  fi
fi

if [[ "$DST_STATUS" == "running" && "${OSTYPE:-}" == win* ]]; then
  _info "Reconfiguring static IPv6 in guest: addr=${EXPECTED_VM_IPV6}/64 gw=${DST_GATEWAY}…"

  PS_RECONFIG=$(cat <<PSEOF
\$ErrorActionPreference = 'SilentlyContinue'
\$ProgressPreference   = 'SilentlyContinue'
\$exp  = '${EXPECTED_VM_IPV6}'
\$gw   = '${DST_GATEWAY}'
\$dns1 = '${DNS6_PRIMARY}'
\$dns2 = '${DNS6_SECONDARY}'

\$a = Get-NetAdapter -Physical | Where-Object Status -eq 'Up' | Select-Object -First 1
if (-not \$a) { [Console]::Error.WriteLine('no active physical adapter'); exit 1 }
\$idx   = \$a.ifIndex
\$alias = \$a.InterfaceAlias

function Test-Configured {
  \$pp = Get-NetIPAddress -InterfaceIndex \$idx -IPAddress \$exp -PolicyStore PersistentStore -ErrorAction SilentlyContinue
  if (-not (\$pp -and \$pp.PrefixOrigin -eq 'Manual')) { return \$false }
  \$ac = Get-NetIPAddress -InterfaceIndex \$idx -IPAddress \$exp -PolicyStore ActiveStore -ErrorAction SilentlyContinue
  if (-not (\$ac -and \$ac.PrefixOrigin -eq 'Manual')) { return \$false }
  \$rc = Get-NetRoute -InterfaceIndex \$idx -DestinationPrefix '::/0' -AddressFamily IPv6 -PolicyStore ActiveStore -ErrorAction SilentlyContinue | Where-Object { \$_.NextHop -eq \$gw }
  if (-not \$rc) { return \$false }
  \$d = (Get-DnsClientServerAddress -InterfaceIndex \$idx -AddressFamily IPv6 -ErrorAction SilentlyContinue).ServerAddresses
  if (-not \$d -or -not (\$d -contains \$dns1)) { return \$false }
  return \$true
}

if (-not (Test-Configured)) {
  foreach (\$store in 'PersistentStore', 'ActiveStore') {
    Set-NetIPInterface -InterfaceIndex \$idx -AddressFamily IPv6 -RouterDiscovery Disabled -PolicyStore \$store -ErrorAction SilentlyContinue | Out-Null
    Set-NetIPInterface -InterfaceIndex \$idx -AddressFamily IPv6 -Dhcp Disabled -PolicyStore \$store -ErrorAction SilentlyContinue | Out-Null
  }
  foreach (\$store in 'PersistentStore', 'ActiveStore') {
    Get-NetIPAddress -InterfaceIndex \$idx -AddressFamily IPv6 -PolicyStore \$store -ErrorAction SilentlyContinue |
      Where-Object { \$_.PrefixOrigin -ne 'WellKnown' -and \$_.IPAddress -notlike 'fe80*' } |
      ForEach-Object {
        Remove-NetIPAddress -InterfaceIndex \$idx -IPAddress \$_.IPAddress -PolicyStore \$store -Confirm:\$false -ErrorAction SilentlyContinue
      }
  }
  Get-NetRoute -InterfaceIndex \$idx -AddressFamily IPv6 -ErrorAction SilentlyContinue |
    Where-Object { \$_.DestinationPrefix -eq '::/0' } |
    ForEach-Object { Remove-NetRoute -InterfaceIndex \$idx -DestinationPrefix '::/0' -Confirm:\$false -ErrorAction SilentlyContinue }
  try { New-NetIPAddress -InterfaceIndex \$idx -IPAddress \$exp -PrefixLength 64 -AddressFamily IPv6 -ErrorAction Stop | Out-Null } catch { [Console]::Error.WriteLine("New-NetIPAddress: \$(\$_.Exception.Message)") }
  try { New-NetRoute     -InterfaceIndex \$idx -DestinationPrefix '::/0' -NextHop \$gw -AddressFamily IPv6 -ErrorAction Stop | Out-Null } catch { [Console]::Error.WriteLine("New-NetRoute: \$(\$_.Exception.Message)") }
  & netsh interface ipv6 set dnsserver "\$alias" static \$dns1 primary validate=no 2>&1 | Out-Null
  & netsh interface ipv6 add dnsserver "\$alias" \$dns2 index=2 validate=no 2>&1 | Out-Null
}

# Always refresh DHCPv4 — we changed nodes, so the lease must be reissued by
# the new dnsmasq (same /16 pool, but a different lease database).
& ipconfig /release 2>&1 | Out-Null
& ipconfig /renew   2>&1 | Out-Null

if (Test-Configured) { exit 0 } else { exit 2 }
PSEOF
)
  # Slow cutovers leave the guest agent down for minutes; a single reconfig
  # attempt then fails (qga exec timeout) and the guest keeps the SOURCE-prefix
  # IP → customer unreachable while everything else reports success (VMs
  # 1777/1841/1084/702/1257, 2026-07-03). Wait for the agent, then retry the
  # idempotent reconfig until the expected IPv6 is actually bound. Worst case
  # ~12 min before giving up loudly.
  RECONFIG_OK=0
  _wait_agent_dst 180 \
    || _info "Guest agent not answering after 180s — attempting reconfig anyway…"
  for _try in 1 2 3 4; do
    if (( _try > 1 )); then
      _info "In-guest IPv6 not confirmed yet; waiting for agent and retrying (${_try}/4)…"
      sleep 10
      _wait_agent_dst 120 || true
    fi
    _ps_rc=0
    _dst_run_ps "$PS_RECONFIG" 120 || _ps_rc=$?
    if (( _ps_rc != 0 )); then
      _info "Reconfig attempt ${_try}/4 failed (rc=${_ps_rc})."
      continue
    fi
    if _verify_dest_ipv6 30; then
      RECONFIG_OK=1
      break
    fi
  done
  if (( RECONFIG_OK == 1 )); then
    _ok "In-guest IPv6 reconfigured + verified: ${EXPECTED_VM_IPV6} bound on dest VM (attempt ${_try})."
  else
    MIGRATION_DEGRADED=1
    DEGRADED_REASON="in-guest IPv6 reconfig NOT verified (guest may still hold the source-prefix IP)"
    _warn "In-guest IPv6 reconfig NOT verified after 4 attempts — the guest may still hold the source-prefix IP (customer unreachable via connectionUrl AND direct RDP). Fix: re-run this script (idempotent) or apply the PS_RECONFIG netsh block via 'qm guest exec ${VMID}'. Continuing — NAT/Firestore will still point at ${EXPECTED_VM_IPV6}."
  fi
elif [[ "${OSTYPE:-}" != win* ]]; then
  _info "ostype=${OSTYPE:-unknown} (not Windows); skipping in-guest reconfig."
else
  _info "Dest VM not running (status=${DST_STATUS:-unknown}); skipping in-guest reconfig."
fi

# Hookscript on dest — idempotent (qm set is repeatable).
_info "Attaching hookscript on dest…"
dst_ssh "qm set '${VMID}' --hookscript '${HOOKSCRIPT}'" >/dev/null 2>&1 \
  || _warn "Failed to attach hookscript on dest (manual fix may be needed)."

# connectionUrl is NOT written anymore (legacy field, removed 2026-07-07):
# it stored an ip:port here, and the support agent quoted that stale value to
# a customer. Every consumer (panel/emails/agent) now GENERATES the URL from
# serverType + proxmoxId + location — the python helper below already
# refreshes `location` and `publicIpv4` from the dest node doc.

# Reconcile this BASE's local NAT immediately. We use the explicit-IPv6 path
# (sync <vmid> <ipv6>) so this works WITHOUT Firestore being updated yet —
# Firestore is the very last step, so a Ctrl-C between here and the Firestore
# update doesn't leave the doc claiming the VM lives on a node it isn't on.
NAT_OK=0
if [[ -x "$SYNC_BASE_NAT" ]]; then
  _info "Reconciling local NAT via $SYNC_BASE_NAT sync ${VMID} ${EXPECTED_VM_IPV6}…"
  if "$SYNC_BASE_NAT" sync "$VMID" "$EXPECTED_VM_IPV6" >/dev/null 2>&1; then
    _ok "Local NAT reconciled."
    NAT_OK=1
  else
    _warn "$SYNC_BASE_NAT sync ${VMID} ${EXPECTED_VM_IPV6} failed; run it manually."
  fi
  # The peer base is converged by the nat64 Cloud Function reacting to the
  # Firestore update just below (~2 s end-to-end, verified 2026-08-01 across
  # 6 defrag migrations: full map diff identical on both bases) — no push.
else
  _warn "$SYNC_BASE_NAT not executable; skipping local NAT reconcile."
fi

# Reachability check: probe the VM's IPv6 directly on RDP (3389) from BASE.
# BASE routes the VM's /48 (that's how NAT64 works), so no hairpin — this
# tests "Windows brought the new IPv6 up and RDP is listening". Soft warning;
# does not gate Firestore. Skipped for non-Windows guests (3389 is RDP).
if [[ "$DST_STATUS" == "running" && "${OSTYPE:-}" == win* ]] && command -v nc >/dev/null; then
  _info "Probing [${EXPECTED_VM_IPV6}]:${RDP_GUEST_PORT} from BASE (retrying up to ${CONNECTIVITY_TIMEOUT}s)…"
  conn_start=$SECONDS
  conn_rc=1
  while (( SECONDS - conn_start < CONNECTIVITY_TIMEOUT )); do
    if nc -w 5 -6 -z "$EXPECTED_VM_IPV6" "$RDP_GUEST_PORT" >/dev/null 2>&1; then
      _ok "VM reachable on [${EXPECTED_VM_IPV6}]:${RDP_GUEST_PORT} after $((SECONDS - conn_start))s."
      conn_rc=0
      # This probe SETTLES a failed in-guest verification. The verify step only
      # asks the guest agent whether the address is bound; this asks the network
      # whether the customer can actually connect, from the same BASE they come
      # through — strictly stronger evidence, and nothing but the guest itself
      # can be answering on that address. Leaving MIGRATION_DEGRADED set here
      # pages the operator with "the CUSTOMER CANNOT REACH IT" three lines under
      # this success (vm 248, 2026-07-29: red alert, customer perfectly fine),
      # which is exactly how an alert gets trained into noise.
      if (( MIGRATION_DEGRADED == 1 )); then
        MIGRATION_DEGRADED=0
        DEGRADED_REASON=""
        _ok "In-guest verification had failed, but RDP answers on [${EXPECTED_VM_IPV6}] — the address IS bound and serving. Clearing the degraded verdict."
      fi
      break
    fi
    sleep 5
  done
  if (( conn_rc != 0 )); then
    # A silent RDP port only ESCALATES to a hard failure when the in-guest
    # rebind ALSO failed — that pair is the stranded-customer signature. On its
    # own it is not proof of harm: the customer may have RDP disabled
    # (rdpEnabled=false is a normal per-VM setting) or Windows may still be
    # finishing its boot. Failing the run on that would abort defrag passes for
    # healthy migrations and train the operator to ignore the alert, which is
    # worse than the silence we are fixing.
    if (( MIGRATION_DEGRADED == 1 )); then
      DEGRADED_REASON="${DEGRADED_REASON}; RDP also never came up after ${CONNECTIVITY_TIMEOUT}s"
    fi
    _warn "VM ${VMID} unreachable on [${EXPECTED_VM_IPV6}]:${RDP_GUEST_PORT} after ${CONNECTIVITY_TIMEOUT}s. Most likely the in-guest IPv6 reconfig didn't apply — check the VM's network interface on ${DST_NODE}."
  fi
fi

# Firestore: nodeId + ipv6 + maintenance=false (LAST step). Gated on NAT_OK so
# we don't claim the VM lives on the new node until NAT actually points there.
# If we skip, the doc keeps the old nodeId + maintenance=true, signalling
# "in-flux" so a re-run can reconcile.
if (( NAT_OK == 1 )); then
  _info "Updating Firestore servers/{${VMID}}: nodeId=${DST_NODE}, ipv6=${EXPECTED_VM_IPV6}…"
  fs_args=(--vmid "$VMID" --node-id "$DST_NODE" --ipv6 "$EXPECTED_VM_IPV6" --maintenance false)
  if _firestore_update_servers "${fs_args[@]}"; then
    _ok "Firestore updated."
    MAINTENANCE_SET=0  # cleared by the update itself, no rollback needed
  else
    _warn "Firestore update failed. Re-run migrate_vm.sh ${VMID} ${NEW_NODE_NUM} to retry."
  fi
else
  _warn "Skipping Firestore nodeId update (NAT reconcile failed). Re-run migrate_vm.sh ${VMID} ${NEW_NODE_NUM} once $SYNC_BASE_NAT is working."
fi

# Cleanup the migration token (only if we created it this run).
if (( TOKEN_CREATED == 1 )); then
  dst_ssh "pveum user token remove root@pam '${TOKEN_NAME}'" >/dev/null 2>&1 \
    || _warn "Token cleanup had issues (may already be removed)."
  TOKEN_CREATED=0
  _ok "Token removed on dest."
fi

# Restore original power state: if we started this VM ourselves to apply
# in-guest config, gracefully shut it back down. The hookscript on dest will
# fire post-stop and update Firestore status accordingly.
if (( WAS_STOPPED == 1 )); then
  _info "Restoring original power state — gracefully shutting down VM ${VMID}…"
  dst_ssh "qm shutdown '${VMID}' --timeout 300" >/dev/null 2>&1 \
    || _warn "qm shutdown returned non-zero; will still poll for stopped state."
  if _wait_status_dst stopped 60; then
    _ok "VM gracefully shut down on dest."
  else
    _warn "VM ${VMID} still running after shutdown timeout — left running for triage."
  fi
  # Restore the ORIGINAL firewall state (the guard was only for the temporary
  # power-on). File first, then the net0 string if we added firewall=1.
  if [[ -n "${NOINT_ORIG_B64}" ]]; then
    dst_ssh "echo '${NOINT_ORIG_B64}' | base64 -d > '${NOINT_FW}'" >/dev/null 2>&1 \
      || _warn "could not restore original ${NOINT_FW} — review manually."
  else
    dst_ssh "rm -f '${NOINT_FW}'" >/dev/null 2>&1 || true
  fi
  if (( NOINT_NET0_MODIFIED == 1 )); then
    dst_ssh "qm set '${VMID}' --net0 '${NOINT_NET0_ORIG}'" >/dev/null 2>&1 \
      || _warn "could not restore original net0 (firewall=1 left on — harmless but review)."
  fi
  _ok "Firewall state restored to original."
fi

if (( MIGRATION_DEGRADED == 1 )); then
  # MIGRATION_DONE is already 1, so the EXIT trap only closes SSH — nothing is
  # rolled back and the VM stays where it is. We exit non-zero purely to tell
  # the caller (migrate_vms_batch / neuravps-defrag) that a HUMAN must finish
  # this one, instead of silently counting it as a success.
  _warn "Migration COMMITTED BUT DEGRADED: VMID=${VMID}  ${SRC_NODE} → ${DST_NODE}  ipv6=${EXPECTED_VM_IPV6}"
  # Page the operator through the liveness sweep (ex44_distress pattern).
  # Manual/batch runs create no defrag_runs doc, so before this the ONLY
  # detector was conncheck's hourly sweep — how 854/1023 were caught hours
  # late on 2026-08-01. Best-effort: never mask the _die below.
  python3 - "$VMID" "$DST_NODE" "$DEGRADED_REASON" <<'PYDEG' || true
import os, sys, socket
try:
    import firebase_admin
    from firebase_admin import credentials, firestore
    creds = os.environ.get("FIREBASE_CREDENTIALS_FILE", "/etc/firebase-credentials.json")
    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(creds))
    db = firestore.client()
    db.collection("migration_degraded").document().set({
        "vmid": int(sys.argv[1]), "dest": sys.argv[2], "reason": sys.argv[3],
        "host": socket.gethostname(), "at": firestore.SERVER_TIMESTAMP,
    })
except Exception as e:  # noqa: BLE001
    sys.stderr.write(f"degraded-page failed: {e}\n")
PYDEG
  _die "MIGRATION_DEGRADED vm=${VMID} dest=${DST_NODE}: ${DEGRADED_REASON}. The VM is running on the destination but the CUSTOMER CANNOT REACH IT. Manual fix: re-run 'migrate_vm.sh ${VMID} ${NEW_NODE_NUM}' (idempotent — it retries the in-guest rebind), or apply the netsh reconfig via 'qm guest exec ${VMID}' once the guest agent answers."
fi

_ok "Migration complete: VMID=${VMID}  ${SRC_NODE} → ${DST_NODE}  ipv6=${EXPECTED_VM_IPV6}"

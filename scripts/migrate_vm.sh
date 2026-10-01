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
# start at MIGRATE_DOWNTIME_INITIAL (default 1 s since 2026-10-01; it was 15 s,
# which froze every converging VM ~15 s), and a background escalator
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
MIGRATE_DOWNTIME_INITIAL="${MIGRATE_DOWNTIME_INITIAL:-1}"  # STARTING cutover budget (s). 15 → 1 on 2026-10-01: measured on a lab VM, the guest froze 14.9 / 11.6 / 13.2 s with 15 and 0.73 s with 1 — QEMU does not freeze "the minimum", it cuts over as soon as what is left fits the budget, so 15 s × ~110 MiB/s froze any VM with ≥1.6 GB of RAM ~15 s, enough to kill MetaTrader's broker sessions (memory/neuravps-migracion-corta-sesiones-2026-10-01.md). Original rationale, still valid: QEMU cuts over as soon as remaining_dirty/bandwidth <= budget, so a 90s budget doesn't just ALLOW a 90s freeze — it CAUSES one: QEMU stops the guest with up to 90s of data still to copy instead of pre-copying it hot. Measured on 25 defrag migrations 2026-07-31: every single cutover froze the guest 12-90s (median ~38s) under the flat 90. Starting at 15s makes a converging guest freeze <=15s (typically 2-5s: it pre-copies until ~15s of data remain); guests whose dirty rate outruns the link are caught by the escalator below, which walks the budget back up to MIGRATE_DOWNTIME — so the worst case equals the old behaviour instead of failure mode (a). 0 = no staging (set the ceiling up front, old behaviour).
DOWNTIME_ESCALATE_POLL_S="${DOWNTIME_ESCALATE_POLL_S:-10}"  # escalator cadence (s); was 20 — with a 1 s start the ladder has more, smaller steps.
DOWNTIME_ESCALATE_HARD_S="${DOWNTIME_ESCALATE_HARD_S:-600}"  # RAM-phase seconds after which the escalator jumps straight to the MIGRATE_DOWNTIME ceiling — bounds the RAM pre-copy time (the efidisk0-reap window, failure mode (a)) to roughly what the flat 90s produced. Counted from the first poll that shows RAM stats, NOT from launch: the preceding disk mirror legitimately runs 10-25+ min and must not burn this budget.
TOKEN_NAME_PREFIX="${TOKEN_NAME_PREFIX:-migrate-full}"
HOOKSCRIPT="${HOOKSCRIPT:-shared:snippets/sync-dnat.py}"
RDP_GUEST_PORT="${RDP_GUEST_PORT:-3389}"
CONNECTIVITY_TIMEOUT="${CONNECTIVITY_TIMEOUT:-420}"  # seconds — RDP listener can take a moment after the in-guest IPv6 rebind.
# 120 was too short and it cried wolf: the defrag run of 2026-08-19 logged 24
# "VM unreachable ... check the network interface" warnings on a run where every
# single VM was fine (all 30 verified answering RDP minutes later). Windows can
# take several minutes to bring the RDP listener back after a cutover, so the
# probe was reporting its own impatience as a customer outage — and an alert
# that its own evidence contradicts is worse than no alert, because it teaches
# whoever reads it to skip the real ones.
POST_MIGRATE_RUN_TIMEOUT="${POST_MIGRATE_RUN_TIMEOUT:-300}"  # seconds — after a COMMITTED online migration, how long to wait for the dest VM to reach `running` before downgrading to a warning + fix-forward. On a slow/degraded cutover the dest can sit in `inmigrate` for minutes; the old hard 120s here false-rolled-back migrations that had actually landed (VMs 1785/1790/1791, 2026-07-01). A timeout now NEVER rolls back — the VM is already on dest.
ERROR_LOG="${ERROR_LOG:-/var/log/migrate_vm/errors.log}"

# Controller state uses a non-blocking lock shared with the reconciler.
# Retry before copying, or after routing has recovered; never in the cutover.
_balloon_state_retry() {
  local ssh_fn="$1" command="$2" attempt output
  for ((attempt=1; attempt<=12; attempt++)); do
    if output=$("$ssh_fn" "$command" 2>/dev/null); then
      printf '%s\n' "$output"
      return 0
    fi
    if (( attempt < 12 )); then sleep 5; fi
  done
  return 1
}

# ----- Modelo de direccionamiento: se DEDUCE, no se pasa por parametro --------
# Una VM del modelo nuevo lleva su propia identidad de red, que NO se deriva del
# nodo: IPv6 `<IDENT>::<vmid>` y puerta de enlace `fe80::1`, iguales en toda la
# flota. Al migrar NO hay que tocar el invitado: lo unico que se mueve son las
# rutas en las bases.  Ver base/docs/egress-failover-e-ipv6-estable.md §11bis.
#
# Esto fue un flag NEW_IPS=0/1 que habia que acordarse de pasar. Durante
# una migracion de flota larga eso es una bomba en las dos direcciones:
#   - olvidarlo en una VM YA migrada -> el bloque de reconfiguracion le calcula
#     la direccion desde el prefijo del nodo DESTINO y le MACHACA su IDENT,
#     dejandola atada al nodo y al cliente fuera;
#   - ponerlo de mas sobre una VM del modelo VIEJO -> se queda sin reconfigurar
#     y pierde la red al aterrizar.
#
# Ahora se decide mirando la direccion que la VM tiene DE VERDAD: si cae dentro
# del /64 de identidad, es del modelo nuevo. La fuente es state.json, la misma
# que usa el resolver, asi que no puede desincronizarse de lo que ve el script.
#
# (Hecho el 16-08-2026: eliminados los caminos del modelo viejo.)
IDENT_PREFIX="${IDENT_PREFIX:-2a01:4f9:c01f:e::/64}"

_is_ident() {   # rc 0 si $1 cae dentro de $IDENT_PREFIX
  [[ -n "${1:-}" ]] || return 1
  python3 -c '
import ipaddress, sys
try:
    sys.exit(0 if ipaddress.ip_address(sys.argv[1]) in ipaddress.ip_network(sys.argv[2]) else 1)
except Exception:
    sys.exit(1)
' "$1" "$IDENT_PREFIX" 2>/dev/null
}

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

# La direccion ACTUAL de la VM decide el modelo. state.json es la misma fuente
# que usa el resolver, asi que ambos ven exactamente lo mismo. Se calcula aqui,
# antes de bifurcar, para que valga tanto en la migracion normal como en la
# reconciliacion SRC_EQ_DST.
CUR_VM_IPV6=$(VMID="$VMID" STATE_FILE="$STATE_FILE" python3 -c '
import json, os, sys
try:
    s = json.load(open(os.environ["STATE_FILE"]))
    sys.stdout.write(((s.get(os.environ["VMID"]) or {}).get("ipv6") or "").strip())
except Exception:
    pass
' 2>/dev/null || true)
# La IPv4 privada (10.64.x.y) sale de la misma fuente; la usa el corte temprano
# (ruta /32 de la BASE, ARP dirigido, blackhole en el nodo viejo). Vacía = solo IPv6.
CUR_VM_IPV4=$(VMID="$VMID" STATE_FILE="$STATE_FILE" python3 -c '
import ipaddress, json, os, sys
try:
    s = json.load(open(os.environ["STATE_FILE"]))
    v = ((s.get(os.environ["VMID"]) or {}).get("ipv4") or "").strip()
    sys.stdout.write(str(ipaddress.IPv4Address(v)) if v else "")
except Exception:
    pass
' 2>/dev/null || true)

# Toda la flota vive en <IDENT>::<vmid> desde el 16-08-2026. Si apareciera una
# VM fuera de ese prefijo seria un dato corrupto o una VM que nunca se convirtio:
# en cualquier caso, este script ya no sabe reconfigurar invitados y seguir seria
# peor que parar.
if ! _is_ident "$CUR_VM_IPV6"; then
  _die "la VM ${VMID} tiene ${CUR_VM_IPV6:-(sin ipv6)}, fuera de ${IDENT_PREFIX}. Este script solo migra VMs del modelo actual."
fi
_info "IPv6 del invitado: ${CUR_VM_IPV6} — no se toca al migrar."

# Per-VMID lock so two BASE operators can't migrate the same VM in parallel.
LOCKFILE="/run/migrate_vm.${VMID}.lock"
exec 9>"$LOCKFILE" || _die "Cannot open lock file: $LOCKFILE"
command -v flock >/dev/null && { flock -n 9 || _die "Another migration is already running for VMID ${VMID} (lock=$LOCKFILE)"; }

# ----- Resolve source + destination from local state --------------------------
# Single python call: looks up dest by host_num, source by /64-prefix match
# against the VM's current IPv6 in state.json. Prints space-separated:
#   SRC_NODE SRC_IPV6 DST_NODE DST_IPV6 EXPECTED_VM_IPV6 OLD_VM_IPV6
RESOLVED=$(VMID="$VMID" NEW_NODE_NUM="$NEW_NODE_NUM" IDENT_PREFIX="$IDENT_PREFIX" \
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

# El modelo NUEVO rompe el truco del /64: la direccion de la VM ya NO deriva
# del nodo — ese es justamente el objetivo del proyecto — asi que ningun nodo
# comparte /64 con ella y el emparejamiento no puede funcionar. El nodo de
# origen lo dice `nodeId`, que sync-base-nat.py escribe en state.json desde
# Firestore y que migrate_vm actualiza al terminar cada migracion.
ident_prefix = (os.environ.get("IDENT_PREFIX") or "").strip()
es_modelo_nuevo = False
if ident_prefix:
    try:
        es_modelo_nuevo = ipaddress.ip_address(old_vm_ipv6) in ipaddress.ip_network(ident_prefix)
    except ValueError:
        pass

src = None
if es_modelo_nuevo:
    node_id = (vm.get("nodeId") or "").strip()
    if not node_id:
        fail(f"VM {vmid} es del modelo nuevo ({old_vm_ipv6}) pero no tiene nodeId en "
             f"{os.environ['STATE_FILE']} — corre `sync-base-nat.py sync` y reintenta")
    if node_id not in nodes:
        fail(f"VM {vmid} dice estar en {node_id}, que no esta en "
             f"{os.environ['PVE_NODES_FILE']} — corre `sync-base-nat.py sync nodes`")
    src = (node_id, nodes[node_id])
else:
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
  # La dirección del invitado NO cambia al migrar. El resolver aún la calcula
  # desde el prefijo del nodo destino (herencia del modelo viejo); aquí se
  # descarta ese cálculo y se conserva la que ya tiene, de modo que todo lo que
  # viene después —NAT local, sonda RDP, Firestore— apunte a la dirección real
  # y no a una derivada del nodo.
  EXPECTED_VM_IPV6="$OLD_VM_IPV6"
  _info "VMID:           ${VMID}"
  _info "Source node:    ${SRC_NODE} (${SRC_IPV6})"
  _info "Dest node:      ${DST_NODE} (${DST_IPV6})"
  _info "VM IPv6:        ${OLD_VM_IPV6}  (no cambia al migrar)"
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

def selected_base_ipv4(config, location):
    """Closed selector shared with the application watchdog; no network changes."""
    selection = config.get('activeBases', {'b0': 'legacy', 'b1': 'legacy'})
    if not isinstance(selection, dict) or set(selection) != {'b0', 'b1'}:
        raise ValueError('activeBases must contain exactly b0 and b1')
    if any(not isinstance(v, str) or v not in ('legacy', 'ecc') for v in selection.values()):
        raise ValueError('activeBases values must be legacy or ecc')
    role = {'falkenstein': 'b0', 'helsinki': 'b1'}.get(str(location or '').strip().lower())
    if role is None:
        return None
    addresses = {
        'legacy': {'b0': '188.40.153.120', 'b1': '37.27.135.250'},
        'ecc': {'b0': '116.202.118.221', 'b1': '95.216.102.179'},
    }
    return addresses[selection[role]][role]

p = argparse.ArgumentParser()
p.add_argument("--vmid", type=int, required=True)
p.add_argument("--maintenance", choices=("true", "false"))
p.add_argument("--node-id", default=None)
p.add_argument("--ipv6", default=None)
# Escritor PRE-ARMADO (corte temprano, 01/10/2026): hace todas las lecturas y
# la inicialización de Firebase por adelantado y escribe en cuanto aparece este
# fichero. Arrancar Python + Firebase cuesta 1-2 s, y en el corte cada segundo
# cuenta: la otra BASE solo mueve la ruta cuando el disparador ve el `nodeId`.
p.add_argument("--wait-file", default=None)
p.add_argument("--wait-timeout", type=float, default=0)
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
            # ⚠️ Solo para el modelo VIEJO. Una VM del esquema nuevo NO sale por
            # la IP de su nodo sino por la de su BASE, asi que espejar la del
            # nodo aqui le enseñaria al cliente en el panel una IP que no es la
            # suya — y es la que copia en la lista blanca de su broker.
            _ip6_vm = (args.ipv6 or (docs[0].to_dict() or {}).get("ipv6") or "")
            _es_nuevo = str(_ip6_vm).strip().lower().startswith("2a01:4f9:c01f:e:")
            if _es_nuevo:
                _cfg = db.collection("config").document("failover_watchdog").get(retry=None, timeout=5)
                _base = selected_base_ipv4(
                    (_cfg.to_dict() or {}) if _cfg.exists else {}, ndata.get("location"))
                if _base:
                    patch["publicIpv4"] = _base
            elif ndata.get("server_ipv4"):
                patch["publicIpv4"] = ndata["server_ipv4"]
    except Exception:
        pass
if args.ipv6 is not None:           patch["ipv6"]        = args.ipv6
if not patch:
    sys.exit(0)

if args.wait_file:
    import time
    print("FS_ARMED", flush=True)
    deadline = time.monotonic() + args.wait_timeout
    while not os.path.exists(args.wait_file):
        if time.monotonic() > deadline:
            print("FS_WAIT_TIMEOUT", flush=True)
            sys.exit(4)
        time.sleep(0.05)

for d in docs:
    db.collection("servers").document(d.id).update(patch)
if args.wait_file:
    from datetime import datetime, timezone
    print("FS_WRITTEN " + datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3], flush=True)
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
    # NO tocar `ipv6` aquí. Este resolver la calcula desde el prefijo
    # del nodo destino, y como el script es idempotente y se re-ejecuta a
    # propósito tras un fallo parcial, escribirla machacaría la IDENT de una VM
    # del modelo nuevo — y la Cloud Function lo propagaría a las DOS bases,
    # dejando al cliente inalcanzable. Se reconcilia el nodo, que
    # sí cambian; la dirección no cambia nunca al migrar.
    _rc_fs_args=(--vmid "$VMID" --node-id "$RC_DST_NODE")
    _info "Se reconcilia nodeId pero NO ipv6: la dirección no depende del nodo."
    if _firestore_update_servers "${_rc_fs_args[@]}" 2>/dev/null; then
      _ok "Firestore reconciled (or was already up-to-date)."
    else
      _warn "Firestore reconcile failed; check /etc/firebase-credentials.json."
    fi
  else
    _ok "Source equals destination — nothing to do."
  fi
  exit 0
fi



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

# ----- Corte temprano (01/10/2026) ---------------------------------------------
# Medido en laboratorio: con todo al final, una migración en vivo dejaba la VM
# 25-35 s sin red útil también DENTRO de la región, y MetaTrader perdía el
# bróker. Aparte de la pausa de QEMU, el hueco lo hacían dos cosas nuestras:
#   1. La MAC de la puerta de enlace (`vmbr0`) es distinta en cada nodo. Al
#      reanudar, Windows sigue mandando a la del nodo viejo ~8 s, hasta que su
#      caché ARP caduca. El RARP de QEMU actualiza switches, no al invitado.
#   2. Las BASE mueven la ruta /128 y /32 de la VM al túnel del nodo nuevo solo
#      cuando el disparador ve el `nodeId` nuevo en Firestore — y este script lo
#      escribía al final: 12-18 s después de reanudar. Mientras tanto la vuelta
#      llegaba al nodo viejo, que la INUNDABA a todas las VMs de su `vmbr0`
#      (16-34 VMs ajenas en cada migración medida).
# Ahora, en las migraciones en vivo, un vigía en el nodo destino espera el RARP
# con el que QEMU anuncia la VM al terminar y, en ese mismo instante:
#   - le manda a la VM (solo a su MAC) un ARP gratuito de 10.64.255.1 y un NA de
#     fe80::1 con la MAC de este `vmbr0` → sale por la puerta nueva en ~20 ms;
#   - avisa a esta BASE, que mueve SU ruta al túnel del destino al momento,
#     suelta el escritor de Firestore ya armado (→ el disparador mueve la otra
#     BASE en 3-5 s) y pone un `blackhole` temporal de las direcciones de la VM
#     en el nodo viejo, que descarta la vuelta tardía en vez de inundarla.
# Si el vigía no llega a ver el RARP, se hace lo mismo en cuanto la verificación
# confirma que la VM está en destino (respaldo). EARLY_CUTOVER=0 lo apaga todo.
EARLY_CUTOVER="${EARLY_CUTOVER:-1}"
RESUME_WATCH_TTL_S="${RESUME_WATCH_TTL_S:-14400}"   # tope del vigía (disco grande + RAM)
SRC_BLACKHOLE_TTL_S="${SRC_BLACKHOLE_TTL_S:-300}"   # el blackhole del nodo viejo se retira solo; 0 = no ponerlo
BASE_NAT_ENV="${BASE_NAT_ENV:-/etc/default/base-nat}"
CUT_DIR=""; _CUT_WATCH_PID=""; _CUT_FS_PID=""; _CUT_LOOP_PID=""; VM_MAC=""
CUT_COMMITTED=0

# Corre en el nodo DESTINO. argv: nvx-resume-watch <vmid> <mac> <ipv4|""> <ttl>
read -r -d '' _RESUME_WATCH_PY <<'PY' || true
import os, select, socket, struct, sys, time
from datetime import datetime, timezone
VM_MAC = bytes.fromhex(sys.argv[3].replace(':', ''))
VM_IP = sys.argv[4]
TTL = float(sys.argv[5])
BR_NAME = os.environ.get('NVX_BRIDGE', 'vmbr0')
GW4 = socket.inet_aton(os.environ.get('NVX_GW4', '10.64.255.1'))
GW6 = socket.inet_pton(socket.AF_INET6, os.environ.get('NVX_GW6', 'fe80::1'))
BR = bytes.fromhex(open(f'/sys/class/net/{BR_NAME}/address').read().strip().replace(':', ''))
def ts(): return datetime.now(timezone.utc).strftime('%H:%M:%S.%f')[:-3]
def arp(op, tha, tpa):
    return VM_MAC + BR + b'\x08\x06' + struct.pack('!HHBBH', 1, 0x0800, 6, 4, op) + BR + GW4 + tha + tpa
def csum(b):
    if len(b) % 2: b += b'\0'
    s = sum(struct.unpack('!%dH' % (len(b) // 2), b))
    while s >> 16: s = (s & 0xffff) + (s >> 16)
    return (~s) & 0xffff
def na():
    dst = socket.inet_pton(socket.AF_INET6, 'ff02::1')
    body = struct.pack('!BBHI', 136, 0, 0, 0xA0000000) + GW6 + struct.pack('!BB', 2, 1) + BR
    pseudo = GW6 + dst + struct.pack('!I3xB', len(body), 58)
    body = body[:2] + struct.pack('!H', csum(pseudo + body)) + body[4:]
    return VM_MAC + BR + b'\x86\xdd' + struct.pack('!IHBB', 0x60000000, len(body), 58, 255) + GW6 + dst + body
socks = []
for proto in (0x8035, 0x0806):          # RARP de QEMU; ARP por si lo anuncia el invitado
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(proto))
    s.bind((BR_NAME, 0)); socks.append(s)
tx = socket.socket(socket.AF_PACKET, socket.SOCK_RAW); tx.bind((BR_NAME, 0))
print('ARMED', ts(), BR.hex(':'), flush=True)
end = time.time() + TTL
seen = None
while seen is None and time.time() < end:
    for s in select.select(socks, [], [], 1.0)[0]:
        f = s.recv(2048)
        if f[6:12] == VM_MAC:
            seen = 'rarp' if f[12:14] == b'\x80\x35' else 'arp'
            break
if seen is None:
    print('TIMEOUT', ts(), flush=True); sys.exit(3)
print('RESUMED', ts(), seen, flush=True)
for i in range(4):
    if VM_IP:
        vip = socket.inet_aton(VM_IP)
        tx.send(arp(2, VM_MAC, vip)); tx.send(arp(1, b'\0' * 6, GW4))
    tx.send(na())
    if i == 0: print('ANNOUNCED', ts(), flush=True)
    time.sleep(0.4)
PY

# ----- Regla de gracia entre regiones (01/10/2026) -----------------------------
# Entre regiones morían TODAS las conexiones aunque el corte fuera instantáneo:
# el nodo destino saca a la VM por el túnel de SU región (tabla 101), así que los
# flujos viejos llegan a una BASE que no tiene su conntrack (otra IP de salida) y
# las respuestas siguen volviendo por la vieja. La regla: en el destino, durante
# GRACE_TTL_S, un TCP de la VM que el nodo recoge A MITAD (sin SYN: es de antes
# de la migración) se marca con `ct mark` y sale por el túnel de la región VIEJA;
# esa BASE lo reconoce, lo traduce con la IP de siempre y devuelve las respuestas
# al nodo nuevo, porque su ruta /32 ya apunta allí. Los SYN nuevos no se marcan:
# salen por la tabla 101 con la IP de la región nueva. Solo TCP (UDP no se
# distingue) y solo IPv4 (la IPv6 IDENT no lleva NAT). Caduca sola: el set tiene
# timeout y cada flujo conserva su marca mientras viva. Si el túnel viejo cae,
# la tabla 111/112 se queda sin ruta y el flujo sigue por la 101: muere, como
# habría muerto sin la regla.
GRACE_CROSS_REGION="${GRACE_CROSS_REGION:-0}"  # OFF hasta validar HEL<->FSN en laboratorio (01/10: X1 bloqueada); activar con GRACE_CROSS_REGION=1
GRACE_TTL_S="${GRACE_TTL_S:-86400}"
SRC_TUN=""; DST_TUN=""; GRACE_ON=0
read -r -d '' _GRACE_NODE_SH <<'SH' || true
set -e
op="$1"; old="${2:-}"; ip4="${3:-}"; ttl="${4:-86400}"
if [ "$op" = clear ]; then
  nft delete element inet nvxgrace hel4 "{ $ip4 }" 2>/dev/null || true
  nft delete element inet nvxgrace fsn4 "{ $ip4 }" 2>/dev/null || true
  echo "GRACE_CLEAR $ip4"; exit 0
fi
case "$old" in
  tun-hel) mark=0x4e5601; tbl=111; set=hel4 ;;
  tun-fsn) mark=0x4e5602; tbl=112; set=fsn4 ;;
  *) echo "GRACE_BAD_TUN $old"; exit 1 ;;
esac
ip link show "$old" >/dev/null 2>&1 || { echo "GRACE_NO_TUN $old"; exit 1; }
ip route replace default dev "$old" table "$tbl"
ip rule show | grep -q "fwmark $mark lookup $tbl" || ip rule add pref 99 iif vmbr0 fwmark "$mark" lookup "$tbl"
nft list table inet nvxgrace >/dev/null 2>&1 || nft -f - <<'NFT'
table inet nvxgrace {
  set hel4 { type ipv4_addr; flags timeout; }
  set fsn4 { type ipv4_addr; flags timeout; }
  set privada4 { type ipv4_addr; flags interval; elements = { 10.0.0.0/8, 100.64.0.0/10, 172.16.0.0/12, 192.168.0.0/16 } }
  chain pre {
    type filter hook prerouting priority mangle; policy accept;
    iifname "vmbr0" ip saddr @hel4 ip daddr != @privada4 tcp flags & syn == 0 ct state new counter ct mark set 0x4e5601
    iifname "vmbr0" ip saddr @fsn4 ip daddr != @privada4 tcp flags & syn == 0 ct state new counter ct mark set 0x4e5602
    iifname "vmbr0" ct mark { 0x4e5601, 0x4e5602 } meta mark set ct mark
  }
}
NFT
nft delete element inet nvxgrace hel4 "{ $ip4 }" 2>/dev/null || true
nft delete element inet nvxgrace fsn4 "{ $ip4 }" 2>/dev/null || true
nft add element inet nvxgrace "$set" "{ $ip4 timeout ${ttl}s }"
echo "GRACE_ON $set $ip4 mark=$mark table=$tbl"
SH

_egress_tun() {   # <ssh_fn> → túnel de la tabla 101 del nodo (tun-hel|tun-fsn)
  "$1" "ip route show table 101" 2>/dev/null | sed -n 's/^default dev \([^ ]*\).*/\1/p' | head -1 | tr -d '\r'
}

# Antes de remote_migrate, en el destino: activa la gracia si cambia la región
# de salida; si no, borra cualquier resto de esta VM (vuelta a casa).
_grace_prepare() {
  [[ -n "$CUR_VM_IPV4" ]] || return 0
  SRC_TUN=$(_egress_tun src_ssh || true); DST_TUN=$(_egress_tun dst_ssh || true)
  local out
  if [[ "$GRACE_CROSS_REGION" == "1" && -n "$SRC_TUN" && -n "$DST_TUN" && "$SRC_TUN" != "$DST_TUN" ]]; then
    out=$(dst_ssh "bash -s -- on '${SRC_TUN}' '${CUR_VM_IPV4}' '${GRACE_TTL_S}'" <<<"$_GRACE_NODE_SH" 2>&1 || true)
    if [[ "$out" == *GRACE_ON* ]]; then
      GRACE_ON=1
      _ok "Gracia entre regiones: en ${DST_NODE} los TCP ya abiertos de ${CUR_VM_IPV4} saldrán por ${SRC_TUN} durante ${GRACE_TTL_S}s (${out##*GRACE_ON })."
    else
      _warn "No se pudo activar la gracia entre regiones en ${DST_NODE} (${out:-sin salida}); las conexiones abiertas de la VM se cortarán al cambiar de región."
    fi
  else
    dst_ssh "bash -s -- clear '' '${CUR_VM_IPV4}'" <<<"$_GRACE_NODE_SH" >/dev/null 2>&1 || true
  fi
}

# Orden de shell para el nodo viejo/destino: blackhole de las direcciones de la VM.
_blackhole_cmd() {
  local del="ip -6 route del blackhole ${EXPECTED_VM_IPV6}/128 2>/dev/null;"
  [[ -n "$CUR_VM_IPV4" ]] && del+=" ip route del blackhole ${CUR_VM_IPV4}/32 2>/dev/null;"
  del+=" true"
  if [[ "$1" == "del" ]]; then printf '%s' "$del"; return 0; fi
  local add="ip -6 route replace blackhole ${EXPECTED_VM_IPV6}/128;"
  [[ -n "$CUR_VM_IPV4" ]] && add+=" ip route replace blackhole ${CUR_VM_IPV4}/32;"
  # Retirada programada con systemd-run (no ata la sesión SSH); si faltara, un
  # subshell con TODOS sus descriptores a /dev/null, que si no la dejaría colgada.
  add+=" systemd-run --quiet --collect --unit=nvx-unbh-${VMID}-\$(date +%s) --on-active=${SRC_BLACKHOLE_TTL_S} /bin/sh -c '${del}' >/dev/null 2>&1"
  add+=" || { (sleep ${SRC_BLACKHOLE_TTL_S}; ${del}) </dev/null >/dev/null 2>&1 & }"
  printf '%s' "$add"
}

# Conntrack de la VM en un nodo. Al TRAERLA a un nodo donde ya estuvo, sus
# flujos largos pueden tener allí una entrada de hace días (ESTABLISHED dura
# 5 d) con secuencias viejas: los paquetes nuevos salen INVALID y el cortafuegos
# de Proxmox (PVEFW-FORWARD … --ctstate INVALID -j DROP) los tira — medido en
# laboratorio el 01/10: tcpbin murió 20 s después de volver la VM al nodo 242
# con la red perfecta. Sin entrada, el nodo recoge el flujo a mitad
# (nf_conntrack_tcp_loose=1) y sigue. Se borra en el destino antes de traerla
# (aún no hay nada vivo suyo allí) y en el origen al cortar (ya no está).
_ct_flush_cmd() {
  local c="command -v conntrack >/dev/null 2>&1 || exit 0;"
  if [[ -n "$CUR_VM_IPV4" ]]; then
    c+=" conntrack -D -s ${CUR_VM_IPV4} >/dev/null 2>&1; conntrack -D -d ${CUR_VM_IPV4} >/dev/null 2>&1;"
  fi
  c+=" conntrack -D -f ipv6 -s ${EXPECTED_VM_IPV6} >/dev/null 2>&1; conntrack -D -f ipv6 -d ${EXPECTED_VM_IPV6} >/dev/null 2>&1; true"
  printf '%s' "$c"
}

# Ruta de ESTA base al túnel del destino, ya. Mismo nombre que da sync-base-nat
# (`<TUNNEL_IFACE_PREFIX>p<N>`). La otra base la mueve el disparador.
_route_local_to_dst() {
  local pfx iface
  pfx=$(sed -n 's/^[[:space:]]*TUNNEL_IFACE_PREFIX=//p' "$BASE_NAT_ENV" 2>/dev/null | tail -1 | tr -d "\"' \r")
  pfx="${pfx:-tun-}"
  iface="${pfx}p$((10#$NEW_NODE_NUM))"
  if [[ ! -e "/sys/class/net/${iface}" ]]; then
    echo "NO_IFACE ${iface}"; return 1
  fi
  ip -6 route replace "${EXPECTED_VM_IPV6}/128" dev "$iface" || return 1
  if [[ -n "$CUR_VM_IPV4" ]]; then ip route replace "${CUR_VM_IPV4}/32" dev "$iface" || return 1; fi
  echo "ROUTE ${iface}"
}

_cut_now() { date -u +%H:%M:%S.%3N; }

# El corte en sí. Idempotente (marca commit.done). $1 = rarp|arp|respaldo.
# Cuerpo en subshell: el `set +e` no debe escaparse al script principal.
_cutover_commit() (
  set +e
  [[ -n "$CUT_DIR" && ! -e "$CUT_DIR/commit.done" ]] || exit 0
  local t0 r pp
  t0=$(_cut_now)
  r=$(_route_local_to_dst 2>&1)
  : >"$CUT_DIR/fs.go"
  # La ruta puesta a mano la desharía el siguiente sync de CUALQUIER VM en esta
  # base (reconcilia todas desde state.json, que aún dice el nodo viejo):
  # medido en laboratorio, rebotó 1,5 s al nodo viejo. Se persiste ya con
  # node=; un sync-base-nat sin ese parámetro sale con rc=2 y queda el disparador.
  ( "$SYNC_BASE_NAT" sync "$VMID" "$EXPECTED_VM_IPV6" "node=${DST_NODE}" >/dev/null 2>&1; echo "$?" >"$CUT_DIR/persist.rc" ) &
  pp=$!
  if [[ "$SRC_BLACKHOLE_TTL_S" != "0" ]]; then
    src_ssh "$(_blackhole_cmd add)" >/dev/null 2>&1 && r+=" BLACKHOLE_SRC" || r+=" BLACKHOLE_SRC_FAIL"
  fi
  src_ssh "$(_ct_flush_cmd)" >/dev/null 2>&1
  wait "$pp"
  r+=" PERSIST_RC=$(cat "$CUT_DIR/persist.rc" 2>/dev/null)"
  # Si la VM tenía gracia en el nodo que deja (llegó de otra región hace poco),
  # se retira allí: ese nodo ya no la aloja.
  [[ -n "$CUR_VM_IPV4" ]] && src_ssh "bash -s -- clear '' '${CUR_VM_IPV4}'" <<<"$_GRACE_NODE_SH" >/dev/null 2>&1
  printf '%s %s %s → %s\n' "$1" "$t0" "$r" "$(_cut_now)" >"$CUT_DIR/commit.done"
)

_cutover_loop() {
  set +e
  while :; do
    if grep -q '^RESUMED' "$CUT_DIR/watch.out" 2>/dev/null; then
      _cutover_commit "$(awk '/^RESUMED/{print $3; exit}' "$CUT_DIR/watch.out")"
      return 0
    fi
    [[ -e "$CUT_DIR/migrate.returned" ]] && return 0
    sleep 0.05
  done
}

# Antes de remote_migrate. Nunca aborta: si algo no arma, queda el respaldo.
_cutover_arm() {
  [[ "$EARLY_CUTOVER" == "1" && -n "$ONLINE_FLAG" ]] || return 0
  CUT_DIR="$SSH_CTL_DIR/cut"; mkdir -p "$CUT_DIR"
  # Si la VM salió de este destino hace menos de SRC_BLACKHOLE_TTL_S, su
  # blackhole seguiría ahí: fuera antes de traerla.
  dst_ssh "$(_blackhole_cmd del)" >/dev/null 2>&1 || true
  dst_ssh "$(_ct_flush_cmd)" >/dev/null 2>&1 || true
  _grace_prepare
  VM_MAC=$(src_ssh "qm config '${VMID}'" 2>/dev/null | sed -n 's/^net0:.*=\(\([0-9A-Fa-f]\{2\}:\)\{5\}[0-9A-Fa-f]\{2\}\).*/\1/p' | head -1 | tr -d '\r' || true)
  if [[ -n "$VM_MAC" ]]; then
    dst_ssh "python3 - nvx-resume-watch '${VMID}' '${VM_MAC}' '${CUR_VM_IPV4}' '${RESUME_WATCH_TTL_S}'" \
      <<<"$_RESUME_WATCH_PY" >"$CUT_DIR/watch.out" 2>&1 &
    _CUT_WATCH_PID=$!
    local i
    for ((i=0; i<100; i++)); do grep -q '^ARMED' "$CUT_DIR/watch.out" 2>/dev/null && break; sleep 0.1; done
    if grep -q '^ARMED' "$CUT_DIR/watch.out" 2>/dev/null; then
      _ok "Vigía de reanudación armado en ${DST_NODE} (MAC ${VM_MAC}): ARP/NA y corte de rutas al reanudar."
    else
      _warn "El vigía de reanudación no armó en ${DST_NODE} ($(head -c 300 "$CUT_DIR/watch.out" 2>/dev/null | tr '\n' ' ')); el corte se hará al confirmar la migración (respaldo)."
    fi
  else
    _warn "No se pudo leer la MAC de net0 de la VM ${VMID}; sin vigía, el corte se hará al confirmar la migración (respaldo)."
  fi
  _firestore_update_servers --vmid "$VMID" --node-id "$DST_NODE" \
    --wait-file "$CUT_DIR/fs.go" --wait-timeout "$RESUME_WATCH_TTL_S" >"$CUT_DIR/fs.out" 2>&1 &
  _CUT_FS_PID=$!
  _cutover_loop &
  _CUT_LOOP_PID=$!
}

# Tras remote_migrate (vaya bien o mal): para el bucle y el vigía.
_cutover_settle() {
  [[ -n "$CUT_DIR" ]] || return 0
  : >"$CUT_DIR/migrate.returned"
  if [[ -n "$_CUT_LOOP_PID" ]]; then wait "$_CUT_LOOP_PID" 2>/dev/null || true; _CUT_LOOP_PID=""; fi
  if [[ -n "$_CUT_WATCH_PID" ]]; then
    # Deja terminar los anuncios (1,6 s) si ya reanudó; si no, lo para.
    local i; for ((i=0; i<30; i++)); do kill -0 "$_CUT_WATCH_PID" 2>/dev/null || break; sleep 0.1; done
    dst_ssh "pkill -f 'nvx-resume-watc[h] ${VMID} '" >/dev/null 2>&1 || true
    kill "$_CUT_WATCH_PID" 2>/dev/null || true; wait "$_CUT_WATCH_PID" 2>/dev/null || true
    _CUT_WATCH_PID=""
  fi
  local line
  line=$(grep -E '^(RESUMED|ANNOUNCED|TIMEOUT)' "$CUT_DIR/watch.out" 2>/dev/null | tr '\n' ' ' || true)
  [[ -n "$line" ]] && _info "Vigía: ${line}"
  if [[ -e "$CUT_DIR/commit.done" ]]; then
    CUT_COMMITTED=1
    _ok "Corte temprano: $(cat "$CUT_DIR/commit.done")"
  fi
}

# Espera al escritor de Firestore ya soltado (o lo mata si nunca se soltó).
_cutover_fs_reap() {
  [[ -n "$_CUT_FS_PID" ]] || return 0
  local i
  if [[ -e "$CUT_DIR/fs.go" ]]; then
    for ((i=0; i<300; i++)); do kill -0 "$_CUT_FS_PID" 2>/dev/null || break; sleep 0.1; done
  fi
  kill "$_CUT_FS_PID" 2>/dev/null || true; wait "$_CUT_FS_PID" 2>/dev/null || true
  _CUT_FS_PID=""
  if grep -q '^FS_WRITTEN' "$CUT_DIR/fs.out" 2>/dev/null; then
    _ok "Firestore (corte temprano): nodeId=${DST_NODE} escrito a las $(awk '/^FS_WRITTEN/{print $2}' "$CUT_DIR/fs.out")."
  elif [[ -e "$CUT_DIR/fs.go" ]]; then
    _warn "El escritor temprano de Firestore no confirmó ($(tr '\n' ' ' <"$CUT_DIR/fs.out" | head -c 300)); lo reintenta la escritura final."
  fi
}

# Para la vuelta atrás y la salida: que no quede nada colgando.
_cutover_cleanup() {
  set +e
  [[ -n "$_CUT_LOOP_PID" ]] && { kill "$_CUT_LOOP_PID" 2>/dev/null; wait "$_CUT_LOOP_PID" 2>/dev/null; }
  [[ -n "$_CUT_FS_PID" && ! -e "${CUT_DIR}/fs.go" ]] && { kill "$_CUT_FS_PID" 2>/dev/null; wait "$_CUT_FS_PID" 2>/dev/null; }
  if [[ -n "$_CUT_WATCH_PID" ]]; then
    dst_ssh "pkill -f 'nvx-resume-watc[h] ${VMID} '" >/dev/null 2>&1
    kill "$_CUT_WATCH_PID" 2>/dev/null; wait "$_CUT_WATCH_PID" 2>/dev/null
  fi
  _CUT_LOOP_PID=""; _CUT_WATCH_PID=""
}


# ----- Cleanup / rollback state machine ----------------------------------------
TOKEN_CREATED=0
HOOKSCRIPT_DETACHED=0
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
  _cutover_cleanup
  if (( MIGRATION_DONE == 1 )); then
    _ssh_close
    return
  fi
  _warn "Aborting (rc=${rc}); rolling back transient state…"
  if [[ -n "$CUT_DIR" && -e "$CUT_DIR/commit.done" ]]; then
    # El vigía vio a la VM anunciarse EN EL DESTINO, así que la memoria ya pasó
    # y la ruta y Firestore apuntan allí. No se revierte: devolver la ruta al
    # origen dejaría sin red a una VM que corre en destino.
    _warn "OJO: la VM ${VMID} se anunció en ${DST_NODE} ($(cat "$CUT_DIR/commit.done")): rutas y Firestore YA apuntan al destino y no se revierten. Comprobar 'qm status ${VMID}' en ${SRC_NODE} y ${DST_NODE}."
  fi
  if (( HOOKSCRIPT_DETACHED == 1 )); then
    _info "Re-attaching hookscript on source…"
    src_ssh "qm set '${VMID}' --hookscript '${HOOKSCRIPT}'" >/dev/null 2>&1 || true
  fi
  if (( TOKEN_CREATED == 1 )); then
    _info "Removing migration token on dest…"
    dst_ssh "pveum user token remove root@pam '${TOKEN_NAME}'" >/dev/null 2>&1 || true
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

  # (Aqui habia una comprobacion previa del guest agent que ABORTABA la
  # migracion si no respondia. Existia porque despues habia que reconfigurar la
  # red DENTRO del invitado: con el agente muerto la VM llegaba al destino con
  # la IP del prefijo del nodo de origen y el cliente se quedaba inalcanzable
  # (vms 854/1023, 01-08).
  #
  # Desde el 16-08-2026 el invitado NO se toca al migrar: su IPv6 es
  # <IDENT>::<vmid> y su puerta fe80::1, iguales en toda la flota. Sin
  # reconfiguracion no hace falta el agente, asi que exigirlo solo servia para
  # bloquear migraciones perfectamente validas — y son justo las cajas mas
  # cargadas, donde el agente se cae, las que mas falta tienen de moverse.)

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

  # 1) NO se marca `maintenance`. Una migracion en caliente ya no interrumpe al
  # cliente: no se toca el invitado, no se pide el guest agent y el corte de
  # cutover son segundos. Marcarla solo servia para ensuciar el panel con un
  # estado que el usuario no llega a notar. El flag sigue existiendo para uso
  # MANUAL del operador (--maintenance en el ayudante de Firestore), pero este
  # script no lo escribe.

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
  # accumulates rounds and earns a bigger budget stepwise (see _downtime_ladder:
  # 3→2s … 12→ceiling). DOWNTIME_ESCALATE_HARD_S bounds total pre-copy time — the
  # efidisk0-reap window (failure mode (a)) — by jumping to the ceiling
  # outright, so the worst case degrades to exactly the old flat-90 behaviour.
  # HMP `migrate_set_parameter downtime-limit` takes MILLISECONDS and applies
  # to the RUNNING migration (validated on PVE 9.2.5). Self-terminates when
  # the migration leaves the active states or the source VM disappears
  # (remote_migrate --delete); the parent also reaps it after pvesh returns.
  # Escalones (01/10/2026, con la pausa inicial bajada de 15 s a 1 s): en vez de
  # saltar de golpe a 30 s en la tercera ronda, se sube por pasos pequeños, para
  # que una VM que solo necesita un par de rondas más se congele 2-8 s y no 30.
  # Las que de verdad no convergen llegan igual al techo: por rondas (12) o por
  # el reloj duro.
  _downtime_ladder() {   # <dirty_sync_count> → presupuesto (s) que le toca
    local d="${1:-0}"
    if   (( d >= 12 )); then echo "$MIGRATE_DOWNTIME"
    elif (( d >= 10 )); then echo 60
    elif (( d >= 8 ));  then echo 30
    elif (( d >= 6 ));  then echo 15
    elif (( d >= 5 ));  then echo 8
    elif (( d >= 4 ));  then echo 4
    elif (( d >= 3 ));  then echo 2
    else echo "$MIGRATE_DOWNTIME_INITIAL"
    fi
  }
  _downtime_escalator() {
    set +e   # parent runs -euo pipefail; in here a failed poll must never kill the loop
    local ram_ts cur target dirty status out elapsed live_ms want_ms
    ram_ts=""   # set when the RAM phase is first observed — the hard timer
                # counts from THERE, not from launch: the disk mirror that
                # precedes it ran 10-25 min on today's defrag jobs, and
                # counting it would jump every big VM straight to the ceiling
                # before a single RAM round had run.
    cur="$MIGRATE_DOWNTIME_INITIAL"
    for _ in $(seq 1 $(( 10800 / DOWNTIME_ESCALATE_POLL_S ))); do   # ~3 h
      # self-cap (covers the disk phase of a full zvol; must outlive it to see
      # the RAM rounds)
      sleep "$DOWNTIME_ESCALATE_POLL_S"
      # qm monitor adds a greeting even when QEMU returns an empty result
      # during block mirroring. Use the monitor API's JSON string to keep
      # that empty result distinct from an actual migration status.
      out=$(src_ssh "timeout 10 pvesh create '/nodes/${SRC_NODE}/qemu/${VMID}/monitor' --command 'info migrate' --output-format json" 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin))' 2>/dev/null)
      if [[ -z "$out" ]]; then
        continue   # SSH hiccup or source VM already deleted; parent reaps us
      fi
      status=$(grep -oE 'Migration status: [a-z-]+' <<<"$out" | awk '{print $3}')
      case "$status" in
        active|postcopy-active|device|setup|cancelling) ;;
        *)
          # Block mirroring precedes RAM migration. An empty/none status
          # (or the previous migration's terminal status) is normal then.
          [[ -z "$ram_ts" ]] && continue
          return 0 ;;   # RAM phase already observed: it is now terminal.
      esac
      grep -q 'transferred ram:' <<<"$out" && [[ -z "$ram_ts" ]] && ram_ts=$(date +%s)
      [[ -z "$ram_ts" ]] && continue   # still in the disk phase: nothing to escalate
      dirty=$(grep -oE 'dirty sync count: [0-9]+' <<<"$out" | grep -oE '[0-9]+$')
      elapsed=$(( $(date +%s) - ram_ts ))
      target=$(_downtime_ladder "${dirty:-0}")
      if [[ "$elapsed" -ge "$DOWNTIME_ESCALATE_HARD_S" ]]; then
        target="$MIGRATE_DOWNTIME"
      fi
      [[ "$target" -lt "$cur" ]] && target="$cur"
      [[ "$target" -gt "$MIGRATE_DOWNTIME" ]] && target="$MIGRATE_DOWNTIME"
      # El bucle de fase 2 de Proxmox (QemuMigrate.pm) lleva SU propia copia del
      # límite, que arranca en el migrate_downtime de la VM y DUPLICA cuando lo
      # pendiente crece seis veces — y la escribe en QEMU sin mirar la nuestra.
      # Puede BAJAR un presupuesto que subimos (con 1 s de partida: 1→2→4 por
      # debajo de nuestros 15) o pasarse del techo (1→…→128 s, y un apagón de
      # ~175 s mató el QEMU destino). Así que en cada vuelta se lee el valor VIVO
      # y se encaja en [nuestro escalón, techo]: si Proxmox lo subió sin pasarse,
      # se respeta.
      live_ms=$(src_ssh "timeout 10 pvesh create '/nodes/${SRC_NODE}/qemu/${VMID}/monitor' --command 'info migrate_parameters' --output-format json" 2>/dev/null \
        | python3 -c 'import json,re,sys; m=re.search(r"downtime-limit: ([0-9]+)", json.load(sys.stdin)); print(m.group(1) if m else "")' 2>/dev/null)
      want_ms=$(( target * 1000 ))
      if [[ -n "$live_ms" ]] && (( live_ms > want_ms )); then
        want_ms="$live_ms"
        (( want_ms > MIGRATE_DOWNTIME * 1000 )) && want_ms=$(( MIGRATE_DOWNTIME * 1000 ))
        target=$(( want_ms / 1000 ))
      fi
      if [[ "$target" -gt "$cur" ]] || [[ -n "$live_ms" && "$live_ms" != "$want_ms" ]]; then
        if src_ssh "timeout 10 pvesh create '/nodes/${SRC_NODE}/qemu/${VMID}/monitor' --command 'migrate_set_parameter downtime-limit ${want_ms}' --output-format json" 2>/dev/null \
          | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin) == "" else 1)' >/dev/null 2>&1; then
          if [[ "$target" -gt "$cur" ]]; then
            _info "downtime-escalator: budget ${cur}s → ${target}s (dirty rounds=${dirty:-?}, elapsed=${elapsed}s — guest dirties memory faster than the link drains it)."
          else
            _info "downtime-escalator: Proxmox había dejado el límite en ${live_ms} ms; reafirmado a ${want_ms} ms."
          fi
          cur="$target"
        fi
      fi
      # Ya no se sale al llegar al techo: hay que seguir vigilando que Proxmox
      # no lo rebase hasta que la migración termine.
    done
  }

  # 5) pvesh remote_migrate (deletes source after success; --online iff src running)
  BALLOON_HISTORY=""
  if [[ "$SRC_NODE" == *AX162* && "$DST_NODE" == *AX162* ]]; then
    BALLOON_HISTORY=$(_balloon_state_retry src_ssh "python3 /usr/local/sbin/neuravps-ram-guard.py export-state '${VMID}' '${SRC_NODE}'") || BALLOON_HISTORY=""
    [[ "$BALLOON_HISTORY" =~ ^[A-Za-z0-9+/=]+$ ]] || BALLOON_HISTORY=""
    [[ -n "$BALLOON_HISTORY" ]] || _warn "Balloon history unavailable; destination will re-learn conservatively."
  fi
  TARGET_HOST="[${DST_IPV6}]"
  _info "Starting pvesh remote_migrate (${SRC_NODE} → ${DST_NODE}, mode=${ONLINE_FLAG:-offline})…"
  _ESC_PID=""
  if [[ -n "$ONLINE_FLAG" && "${_DT_STAGED:-0}" == "1" ]]; then
    _downtime_escalator &
    _ESC_PID=$!
    _info "downtime-escalator armed (pid ${_ESC_PID}): start ${MIGRATE_DOWNTIME_INITIAL}s, ceiling ${MIGRATE_DOWNTIME}s, hard-escalate at ${DOWNTIME_ESCALATE_HARD_S}s."
  fi
  _cutover_arm
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
  _cutover_settle
  [[ "$_migrate_rc" -eq 0 ]] || _die "pvesh remote_migrate failed."
  _ok "remote_migrate command returned."

  # 6) Verify the migration ACTUALLY landed. `pvesh remote_migrate` exits 0 even
  # when the task ends in "migration finished with problems" (the CLI returns
  # the worker UPID, not the task result), so exit code is not trustworthy.
  # Check ground truth instead: with --delete a successful migration leaves the
  # VM present on dest and gone from source. If that does not hold, the VM was
  # kept on source — _die here (BEFORE MIGRATION_DONE=1) so the existing
  # _rollback re-attaches the hookscript on source and leaves Firestore routing
  # (nodeId/ipv6) untouched.
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

  # Corte temprano de respaldo: si el vigía no vio el RARP (o no armó), se
  # mueven aquí las rutas y Firestore — aún antes de esperar a `running`, el
  # hookscript y el NAT local, que antes iban primero.
  if [[ -n "$CUT_DIR" ]]; then
    if (( CUT_COMMITTED == 0 )); then
      _cutover_commit respaldo
      [[ -e "$CUT_DIR/commit.done" ]] && { CUT_COMMITTED=1; _warn "Corte hecho por el respaldo, no al reanudar: $(cat "$CUT_DIR/commit.done")"; }
    fi
    _cutover_fs_reap
  fi

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
# El invitado ya no se reconfigura al migrar (su IP no depende del nodo), asi
# que una VM parada YA NO SE ENCIENDE: antes habia que arrancarla en destino
# solo para hablarle por el guest agent, con todo el aparato de forzar
# `vm-no-internet` para que ningun MetaTrader abriera operaciones en esa
# ventana. Al desaparecer el motivo, desaparece el riesgo: la VM llega parada y
# se queda parada.
#
# `qm enroll-efi-keys` SI se hace, y precisamente requiere la VM parada.
if (( WAS_STOPPED == 1 )); then
  _enroll_efi_dst
  _info "La VM llego parada: se queda parada (ya no hace falta encenderla)."
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
# to the plan's ram_min so KSM/zswap can breathe. Cross-family moves
# are offline-only (Intel<->AMD blocks live), so the qm set lands cleanly on a
# stopped VM. Same-family moves no-op. VM is a vps-e iff its name is "E-*".
#
# 46 GB, NO 18. Este bloque nacio el 03-07-2026 con ram_min=18 GB, y el
# 25-07-2026 el operador subio ram_min a 46 precisamente porque 18 estaba mal:
# la colocacion reservaba 18 GB para un VPS E cuyo uso real es ~46 (mediana
# medida 41,8 GB, p90 55,5 sobre los 149 vivos), metia un tercero donde no
# cabia y el balloon-reconciler se quedaba sin presupuesto para alimentarlos
# -> cliente estrangulado. La constante se quedo con el valor refutado tres
# semanas despues de refutarlo. Corregida el 22-08-2026 al migrar la vm288,
# que aterrizo con suelo de 18 GB.
#
# El suelo es un MINIMO, no una asignacion: en un nodo sin sobrecomprometer
# dejarlo bajo no quita ni un MB (medido el mismo dia en la vecina vm1704,
# suelo 18 GB y actual=61340). El dano aparece cuando el nodo se llena, que
# es justo cuando el cliente menos puede permitirselo.
VPSE_BALLOON_MIN_MB="${VPSE_BALLOON_MIN_MB:-47104}"
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

# ---- El invitado NO se toca ------------------------------------------------
# Su IPv6 es <IDENT>::<vmid> y su puerta de enlace fe80::1, iguales en TODA la
# flota, asi que una migracion no cambia nada dentro de Windows. Ese era el
# objetivo del proyecto y desde el 16-08-2026 no queda ninguna VM del modelo
# viejo, de modo que el bloque de reconfiguracion por el guest agent
# (PS_RECONFIG, _wait_agent_dst, _verify_dest_ipv6) se ha eliminado: era la
# causa habitual del MIGRATION_DEGRADED y ya no reconfiguraba nada real.
#
# Las rutas de la VM en las DOS bases las mueve solo el disparador de Firestore
# al escribirse `nodeId` mas abajo. Comprobado en la vm1188 (16-08): aparecieron
# en tun-fp235 y tun-hp235 sin intervencion. El aviso que pedia hacerlo a mano
# se ha quitado porque mandaba al operador a repetir algo ya hecho.
_ok "El invitado conserva ${EXPECTED_VM_IPV6}; su red no se toca."

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
#
# `node=<destino>` (01/10/2026): sin él, esta vía releía `nodeId` de Firestore
# para calcular la ruta /128 — y Firestore aún decía el nodo VIEJO, así que la
# ruta de esta BASE se quedaba en el túnel viejo hasta que llegaba el
# disparador (medido: `Sync proxmoxId=243 done` sin mover nada). Un
# sync-base-nat anterior a ese parámetro lo rechaza con rc=2: entonces se
# repite sin él, como antes.
NAT_OK=0
if [[ -x "$SYNC_BASE_NAT" ]]; then
  _info "Reconciling local NAT via $SYNC_BASE_NAT sync ${VMID} ${EXPECTED_VM_IPV6} node=${DST_NODE}…"
  _nat_rc=0
  "$SYNC_BASE_NAT" sync "$VMID" "$EXPECTED_VM_IPV6" "node=${DST_NODE}" >/dev/null 2>&1 || _nat_rc=$?
  if [[ "$_nat_rc" -eq 2 ]]; then
    _info "sync-base-nat sin soporte de node= (rc=2); repito sin él."
    _nat_rc=0
    "$SYNC_BASE_NAT" sync "$VMID" "$EXPECTED_VM_IPV6" >/dev/null 2>&1 || _nat_rc=$?
  fi
  if [[ "$_nat_rc" -eq 0 ]]; then
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


# Firestore: nodeId + ipv6 (ultima ESCRITURA; debajo solo queda comprobar).
# Gated on NAT_OK so
# we don't claim the VM lives on the new node until NAT actually points there.
# If we skip, the doc keeps the old nodeId, signalling
# "in-flux" so a re-run can reconcile.
if (( NAT_OK == 1 )); then
  _info "Updating Firestore servers/{${VMID}}: nodeId=${DST_NODE}, ipv6=${EXPECTED_VM_IPV6}…"
  fs_args=(--vmid "$VMID" --node-id "$DST_NODE" --ipv6 "$EXPECTED_VM_IPV6")
  if _firestore_update_servers "${fs_args[@]}"; then
    _ok "Firestore updated."
  else
    _warn "Firestore update failed. Re-run migrate_vm.sh ${VMID} ${NEW_NODE_NUM} to retry."
  fi
else
  _warn "Skipping Firestore nodeId update (NAT reconcile failed). Re-run migrate_vm.sh ${VMID} ${NEW_NODE_NUM} once $SYNC_BASE_NAT is working."
fi

# Comprobacion de alcance: se llama al RDP (3389) del invitado por su IPv6
# desde la BASE.
#
# VA DESPUES DE ESCRIBIR EN FIRESTORE, Y ESE ORDEN ES TODO EL ASUNTO. La BASE
# llega a cada invitado por una ruta /128 clavada al tunel de su nodo
# (tun-fpNNN), y lo unico que reapunta esa ruta es el disparador de Firestore
# al ver el `nodeId` que se acaba de escribir arriba. Mientras la sonda corria
# ANTES de esa escritura estaba mandando paquetes por el tunel del nodo que la
# VM acababa de abandonar, asi que no podia salir bien nunca: 31/31, 35/35 y
# 39/39 migraciones la "fallaron" el 19, 20 y 21-08-2026, todas con el cliente
# perfectamente conectado. Ademas costaba 420s cada vez — mas de dos horas
# sumadas a cada pasada de defrag, esperando por una ruta que el propio script
# todavia no habia puesto.
#
# Un aviso que salta en el 100% de las pasadas no es un aviso estricto, es un
# aviso mudo: el cliente que SI se queda incomunicado tiene el mismo aspecto
# que los otros treinta. Aqui abajo, cuando avisa, quiere decir algo.
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
    _warn "La VM ${VMID} no contesta en [${EXPECTED_VM_IPV6}]:${RDP_GUEST_PORT} tras ${CONNECTIVITY_TIMEOUT}s, con la ruta ya reapuntada a ${DST_NODE}. Por si solo no prueba dano: rdpEnabled=false y un Windows que aun arranca se ven igual que esto. Pero si el cliente dice que no entra, esta es su VM."
  fi
fi

# Routes have now been committed and the connectivity probe has run. A busy
# reconciler may delay restoring its history, but cannot delay customer routing.
if [[ -n "${BALLOON_HISTORY:-}" ]]; then
  if _balloon_state_retry dst_ssh "python3 /usr/local/sbin/neuravps-ram-guard.py import-state '${VMID}' '${DST_NODE}' '${BALLOON_HISTORY}'" >/dev/null; then
    _ok "Balloon working-set history restored with a 24h decay hold."
  else
    _warn "Could not restore balloon history; inspect destination controller state."
  fi
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
# (Ya no hay nada que restaurar: la VM parada nunca se enciende, asi que no se
# le fuerza ningun cortafuegos ni hay que apagarla luego. El bloque que hacia
# `qm shutdown` + restaurar `vm-no-internet` se elimino con el encendido.)

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

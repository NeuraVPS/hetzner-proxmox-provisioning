#!/usr/bin/env python3
"""
sync-dnat: Sync VM running/stopped status to Firestore.

IPv6 model: each VM has a static <prefix>::<vmid_hex> configured in-guest via
legacy netsh (store=persistent), with DHCPv6 + RouterDiscovery DISABLED on the
adapter. dnsmasq serves only IPv4 — no RA, no DHCPv6 — so Windows has zero
auto-config to compete with the manual IPv6. This script does NOT manage any
DHCPv6 pin database (none exists in this model).

Commands:
  Hook (Proxmox):
    sync-dnat.py <VMID> post-start         — Set status running.
    sync-dnat.py <VMID> post-stop          — Set status stopped.
  Manual sync:
    sync-dnat.py                           — Sync all VM statuses (reads
                                              Firestore once per VM).
  Watchdog (systemd timer, every 5 min):
    sync-dnat.py watch                     — Same reconciliation, but compares
                                              against a LOCAL cache of what we
                                              last wrote, so a run where nothing
                                              changed costs ZERO Firestore
                                              operations. Catches the case a
                                              hook cannot: the hook process
                                              dying before its write lands.
  Node boot:
    sync-dnat.py node-boot                 — Set proxmox_nodes/<host>.lastNodeBootAt
                                              and sync all VM statuses.
  Firestore IPv6 update (one VM):
    sync-dnat.py set-server-ipv6 <VMID> <IPV6>
                                            — Set servers/{id}.ipv6 for the
                                              local-node VM matching
                                              (proxmoxId, nodeId).
"""
import fcntl
import logging
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path

# Firebase Admin SDK imports (optional - will be initialized if credentials available)
try:
    import firebase_admin
    from firebase_admin import credentials, firestore
    try:
        from google.cloud.firestore_v1 import FieldFilter
        FIELD_FILTER_AVAILABLE = True
    except ImportError:
        FIELD_FILTER_AVAILABLE = False
    FIREBASE_AVAILABLE = True
except ImportError:
    FIREBASE_AVAILABLE = False
    FIELD_FILTER_AVAILABLE = False

# Get hostname reliably - use subprocess.run to avoid capturing stderr
try:
    result = subprocess.run(["hostname"], capture_output=True, text=True, check=True)
    NODE_NAME = result.stdout.strip()
except (subprocess.CalledProcessError, FileNotFoundError):
    NODE_NAME = os.uname().nodename

# Set up logging
LOG_FILE = Path("/var/log/sync-dnat.log")
MAX_LOG_SIZE = 1024 * 1024  # 1 MB

def clean_log_if_needed():
    if LOG_FILE.exists() and LOG_FILE.stat().st_size > MAX_LOG_SIZE:
        LOG_FILE.unlink()
        return True
    return False

was_cleaned = clean_log_if_needed()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, mode='a'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

if was_cleaned:
    logger.info("Log file deleted due to size limit (> 1 MB)")

# ---------------------------------------------------------------
# Firebase initialization (lazy)
# ---------------------------------------------------------------
FIREBASE_INITIALIZED = False

def initialize_firebase():
    if not FIREBASE_AVAILABLE:
        logger.debug("Firebase Admin SDK not available (package not installed)")
        return False
    if firebase_admin._apps:
        return True
    creds_file = os.environ.get("FIREBASE_CREDENTIALS_FILE", "/etc/firebase-credentials.json")
    creds_path = Path(creds_file)
    if not creds_path.exists() or not creds_path.is_file():
        logger.debug(f"Firebase credentials file not found at {creds_file}, skipping Firestore sync")
        return False
    try:
        cred = credentials.Certificate(str(creds_path))
        firebase_admin.initialize_app(cred)
        logger.info(f"Firebase Admin SDK initialized with credentials from {creds_file}")
        return True
    except Exception as e:
        logger.warning(f"Failed to initialize Firebase Admin SDK: {e}, continuing without Firestore sync")
        return False

def ensure_firebase_initialized():
    global FIREBASE_INITIALIZED
    if FIREBASE_INITIALIZED:
        return True
    if FIREBASE_AVAILABLE:
        FIREBASE_INITIALIZED = initialize_firebase()
    return FIREBASE_INITIALIZED

# ---------------------------------------------------------------
# Helper utils
# ---------------------------------------------------------------
def is_running_under_backup():
    """Return True if this hook script is running during a vzdump/backup job."""
    try:
        ppid = os.getppid()
        cmdline = open(f"/proc/{ppid}/cmdline").read().replace("\x00", " ")
        comm = open(f"/proc/{ppid}/comm").read().strip()
        if any(word in cmdline for word in ("vzdump", "pve-zsync")) or "vzdump" in comm:
            return True
    except Exception:
        pass
    return False

def run(cmd, check=True):
    result = subprocess.run(cmd, capture_output=True, text=True, check=check)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
    return result.stdout.strip()

# ---------------------------------------------------------------
# VM status
# ---------------------------------------------------------------
def get_all_vms():
    """Return dict mapping vmid -> "running" or "stopped" for all VMs."""
    result = run(["qm", "list"])
    vm_status = {}
    for line in result.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 3:
            vmid = int(fields[0])
            vm_status[vmid] = "running" if fields[2] == "running" else "stopped"
    return vm_status

# ---------------------------------------------------------------
# Local mirror of what Firestore has been told
# ---------------------------------------------------------------
# The hooks are the fast path, but a hook is a process: it can be killed
# between "Firebase initialized" and the write landing, and then nothing ever
# corrects the document (there is no reconciliation anywhere else). VMs
# 1980/1985/1986 sat at "stopped" in the panel while running, on 2026-07-30.
#
# A timer that re-read Firestore to check would cost one query per VM per run —
# ~1M reads/day across the fleet. So the timer compares against this file
# instead: the status we last SUCCESSFULLY wrote. Nothing changed since the
# last run means no Firestore call at all, read or write.
#
# It can only ever be stale in the safe direction: it is written after the
# write succeeds, so a failed or killed write leaves it showing the old value
# and the next run retries. `sync-dnat.py` with no args still does the real
# thing (reads Firestore) for the rare out-of-band drift.
_STATUS_CACHE = Path("/var/lib/neuravps/vm_status_cache.json")


def _load_status_cache():
    try:
        import json
        with _STATUS_CACHE.open() as fh:
            raw = json.load(fh)
        return {int(k): str(v) for k, v in raw.items()}
    except Exception:
        # Missing or corrupt: treat as "we know nothing", which makes the next
        # run push every VM once and rebuild the file. Never fatal.
        return {}


def _save_status_cache(cache):
    try:
        import json
        _STATUS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATUS_CACHE.with_suffix(".tmp")
        with tmp.open("w") as fh:
            json.dump({str(k): v for k, v in cache.items()}, fh)
        tmp.replace(_STATUS_CACHE)
    except Exception as e:
        # A cache we cannot persist only costs a redundant write next run.
        logger.warning(f"Could not persist VM status cache: {e}")


def _remember_status(vmid, status):
    cache = _load_status_cache()
    if cache.get(int(vmid)) != status:
        cache[int(vmid)] = status
        _save_status_cache(cache)


# ---------------------------------------------------------------
# Firestore sync (status + node boot timestamp)
# ---------------------------------------------------------------
def _query_local_server_doc(db, vmid):
    servers_ref = db.collection('servers')
    if FIELD_FILTER_AVAILABLE:
        query = servers_ref \
            .where(filter=FieldFilter('proxmoxId', '==', vmid)) \
            .where(filter=FieldFilter('nodeId', '==', NODE_NAME))
    else:
        query = servers_ref.where('proxmoxId', '==', vmid).where('nodeId', '==', NODE_NAME)
    return list(query.stream())

def get_firestore_status(vmid):
    if not ensure_firebase_initialized():
        return None
    try:
        db = firestore.client()
        docs = _query_local_server_doc(db, vmid)
        if not docs:
            return None
        if len(docs) > 1:
            logger.warning(f"Multiple Firestore documents for VM {vmid}; using first")
        return (docs[0].to_dict() or {}).get('status')
    except Exception as e:
        logger.warning(f"Failed to get status for VM {vmid} from Firestore: {e}")
        return None

def set_server_ipv6(vmid, ipv6):
    """Update servers/{id}.ipv6 for the VM on this node. Returns True on success."""
    if not ensure_firebase_initialized():
        logger.error(f"Firebase not initialized; cannot set ipv6 for VM {vmid}")
        return False
    try:
        db = firestore.client()
        docs = _query_local_server_doc(db, vmid)
        if not docs:
            logger.warning(f"set-server-ipv6: no server doc for VM {vmid} (nodeId={NODE_NAME})")
            return False
        if len(docs) > 1:
            logger.warning(f"set-server-ipv6: multiple docs for VM {vmid}; updating all")
        for doc_snapshot in docs:
            db.collection('servers').document(doc_snapshot.id).update({'ipv6': ipv6})
            logger.info(f"set-server-ipv6: server {doc_snapshot.id} (VM {vmid}) ipv6={ipv6}")
        return True
    except Exception as e:
        logger.warning(f"set-server-ipv6 failed for VM {vmid}: {e}")
        return False


VM_CONFIG_DIR = Path("/etc/pve/qemu-server")


def status_is_current_and_local(vmid, status):
    """A migration's stopped source/incoming target is not a guest shutdown."""
    try:
        config = (VM_CONFIG_DIR / f"{int(vmid)}.conf").read_text().split("\n[", 1)[0]
        if re.search(r"^lock:\s*migrate\s*$", config, re.MULTILINE):
            return False
        actual = run(["qm", "status", str(vmid)]).strip()
        # Never publish a state sampled before a concurrent start/stop/move.
        return actual == f"status: {status}"
    except (OSError, subprocess.CalledProcessError):
        # A removed config means this node no longer owns the guest. An
        # inconclusive status read must not change its desired power state.
        return False


def update_status_in_firestore(vmid, status):
    if not status_is_current_and_local(vmid, status):
        logger.info(f"Status sync deferred for VM {vmid}: migrating, absent or state changed")
        return
    if not ensure_firebase_initialized():
        return
    # Bounded retry with backoff + jitter. The write is idempotent (setting
    # status=X repeatedly is safe), and a single attempt drops the update on any
    # transient failure — which shows up under load: many post-stop hooks firing
    # at once (e.g. a mass `qm shutdown` before draining a node) contend on
    # Firestore and some writes fail, leaving docs stuck at a stale "running"
    # while the VM is stopped. Retrying absorbs those blips at the source.
    attempts = 4
    for attempt in range(1, attempts + 1):
        try:
            db = firestore.client()
            docs = _query_local_server_doc(db, vmid)
            if not docs:
                logger.debug(f"No Firestore document found for VM {vmid} (nodeId={NODE_NAME}) to update status")
                return
            if len(docs) > 1:
                logger.warning(f"Multiple Firestore documents for VM {vmid}; updating all")
            for doc_snapshot in docs:
                if not status_is_current_and_local(vmid, status):
                    return
                db.collection('servers').document(doc_snapshot.id).update({
                    'status': status,
                    'lastStatusUpdate': firestore.SERVER_TIMESTAMP,
                }, option=db.write_option(last_update_time=doc_snapshot.update_time))
                logger.info(f"Updated Firestore server {doc_snapshot.id} (VM {vmid}): status={status}")
            # Only now, with the write acknowledged: the watchdog trusts this
            # file to mean "Firestore has it".
            _remember_status(vmid, status)
            return
        except Exception as e:
            if attempt == attempts:
                logger.warning(f"Failed to update status for VM {vmid} in Firestore after {attempts} attempts: {e}")
                return
            backoff = 0.5 * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            logger.info(f"update_status VM {vmid} attempt {attempt}/{attempts} failed ({e}); retrying in {backoff:.1f}s")
            time.sleep(backoff)

def update_proxmox_node_last_boot_at():
    if not ensure_firebase_initialized():
        logger.debug("Firebase not initialized, skipping lastNodeBootAt update")
        return
    try:
        db = firestore.client()
        db.collection("proxmox_nodes").document(NODE_NAME).update(
            {"lastNodeBootAt": firestore.SERVER_TIMESTAMP}
        )
        logger.info(f"Updated proxmox_nodes/{NODE_NAME} lastNodeBootAt")
    except Exception as e:
        logger.warning(f"Failed to update lastNodeBootAt for proxmox_nodes/{NODE_NAME}: {e}")


def handle_pending_post_boot_action():
    """
    Finalize a user-requested node maintenance cycle at boot.

    Reaching node-boot means the node has finished rebooting, so any maintenance
    window opened by the panel (proxmox_nodes/{NODE_NAME}.maintenance, set by the
    NeuraVPS cloud functions before the reboot) is OVER by definition. This
    function therefore ALWAYS clears `maintenance` + `pendingPostBootAction` when
    either is present — even if there is no VM to auto-start. Leaving `maintenance`
    set is what made the panel hang on "upgrading"/"rebooting" forever and locked
    the node out of all future maintenance (the claim transaction refuses any
    non-idle state).

    VM auto-start is the separate, opt-in part: only when
    pendingPostBootAction == "start-vm" on a dedicated node (max_vm == 1) do we
    `qm start` the single VM. This is intentionally NOT a permanent onboot=1, so
    unexpected reboots (kernel panic, hardware) leave the VM stopped and a
    crash-loop can't take down the node.
    """
    if not ensure_firebase_initialized():
        return
    try:
        db = firestore.client()
        node_ref = db.collection("proxmox_nodes").document(NODE_NAME)
        node_snap = node_ref.get()
        if not node_snap.exists:
            return
        node_data = node_snap.to_dict() or {}

        pending = node_data.get("pendingPostBootAction")
        has_maintenance = bool(node_data.get("maintenance"))
        if pending is None and not has_maintenance:
            return  # nothing to finalize

        # Auto-start the VM only when explicitly requested for a dedicated node.
        vm_started = None  # None = not attempted; True/False = result
        if pending == "start-vm":
            try:
                max_vm = int(node_data.get("max_vm") or 0)
            except (TypeError, ValueError):
                max_vm = 0
            if max_vm != 1:
                logger.warning(
                    f"pendingPostBootAction=start-vm on {NODE_NAME} but max_vm={max_vm}; "
                    f"refusing auto-start"
                )
            else:
                all_vms = get_all_vms()
                if not all_vms:
                    logger.warning(f"pendingPostBootAction=start-vm on {NODE_NAME} but `qm list` returned no VMs")
                elif len(all_vms) > 1:
                    logger.warning(
                        f"pendingPostBootAction=start-vm on {NODE_NAME} but {len(all_vms)} VMs present; "
                        f"refusing auto-start"
                    )
                else:
                    vmid = next(iter(all_vms.keys()))
                    if all_vms[vmid] == "running":
                        logger.info(f"VM {vmid} already running on {NODE_NAME}")
                        vm_started = True
                    else:
                        logger.info(f"Auto-starting VM {vmid} on {NODE_NAME} after user-requested reboot")
                        try:
                            run(["qm", "start", str(vmid)])
                            vm_started = True
                        except subprocess.CalledProcessError as e:
                            logger.error(f"qm start {vmid} failed: {e.stderr or e}")
                            vm_started = False
        elif pending is not None:
            logger.warning(f"Unknown pendingPostBootAction={pending!r} on {NODE_NAME}; clearing it")

        # Single finalize: ALWAYS clear both fields so the panel unsticks and the
        # node accepts future maintenance. Runs whether or not a VM was started.
        # A stopped VM is already reflected in servers/{id}.status by the
        # sync_all_statuses() call that ran just before this; the start outcome
        # is also recorded here for operator visibility.
        finalize = {
            "pendingPostBootAction": firestore.DELETE_FIELD,
            "maintenance": firestore.DELETE_FIELD,
            "lastUserRebootAt": firestore.SERVER_TIMESTAMP,
        }
        if vm_started is not None:
            finalize["lastUserRebootVmStarted"] = vm_started
        node_ref.update(finalize)
        logger.info(f"node-boot: finalized maintenance on {NODE_NAME} (vm_started={vm_started})")
    except Exception as e:
        logger.warning(f"handle_pending_post_boot_action failed on {NODE_NAME}: {e}")

def sync_all_statuses():
    """Reconcile every local VM's running/stopped state with Firestore."""
    all_vm_status = get_all_vms()
    logger.info(f"All VMs: {sorted(all_vm_status.keys())}")
    for vmid, actual_status in all_vm_status.items():
        firestore_status = get_firestore_status(vmid)
        if firestore_status != actual_status:
            logger.info(f"VM {vmid} status changed from {firestore_status} to {actual_status}, updating Firestore")
            update_status_in_firestore(vmid, actual_status)
        else:
            # Already correct — record it so the watchdog does not re-push what
            # this pass just confirmed.
            _remember_status(vmid, actual_status)


def watch_statuses():
    """Cheap reconciliation for the systemd timer.

    Compares the node's real VM states against the local cache of what we last
    wrote. Only a genuine difference reaches Firestore, so a quiet run costs one
    `qm list` (~0.4 s of CPU) and nothing else — no reads, no writes, no quota.

    This exists for one failure only: a hook that died before its write landed.
    Everything else is already handled by the hooks themselves.

    It must NEVER run while node_boot_reconcile() is walking the VM list: the
    guests it has not reached yet are legitimately stopped, and recording that
    as the desired state makes the reconcile skip them. See BOOT_RECONCILE_LOCK.
    """
    if _watch_should_stand_down():
        return 0
    actual = get_all_vms()
    cache = _load_status_cache()
    drifted = {vmid: st for vmid, st in actual.items() if cache.get(vmid) != st}

    # Forget VMs that no longer exist here (destroyed or migrated away) so the
    # file cannot grow without bound across a node's life.
    stale = [vmid for vmid in cache if vmid not in actual]
    if stale:
        for vmid in stale:
            cache.pop(vmid, None)
        _save_status_cache(cache)

    if not drifted:
        return 0
    logger.info(f"watch: {len(drifted)} VM(s) out of sync with Firestore: {drifted}")
    for vmid, status in drifted.items():
        update_status_in_firestore(vmid, status)
    return len(drifted)


# --- boot-reconcile mutual exclusion ------------------------------------------
# node_boot_reconcile() walks the node's VMs one at a time (~4 s each — 3 min 30
# for 47 guests on 0000202). While it walks, the guests it has not reached yet
# are LEGITIMATELY stopped. watch_statuses() has no way to tell that apart from
# "a hook died before its write landed", so it writes status=stopped for them —
# and the reconcile, which reads the desired state per VM inside its own loop,
# then sees "stopped", concludes the guest is meant to stay down, and skips it.
#
# That is exactly what happened on node 0000202-AX102-1 on 2026-07-30:
#   15:29:15  reconcile starts, begins starting 47 VMs one by one
#   15:32:41  the watch timer fires MID-WALK
#   15:32:42  it writes stopped for the 11 VMs not yet reached
#   15:32:52  reconcile finishes having started 36; the other 11 were skipped
# Ten customer VMs stayed down until a human started them 1 h 36 m later. One of
# them was a live MetaTrader box whose owner then asked us for an audit trail.
#
# The `onboot` flag is NOT the answer here (the reconcile is strictly better:
# it restores the DESIRED state and survives a VM being legitimately stopped).
# The bug is only that the two must not run at once.
BOOT_RECONCILE_LOCK = "/run/neuravps-node-boot-reconcile.lock"

# Backstop for the case where the lock cannot be taken at all (read-only /run,
# etc.): right after a boot the watch has nothing useful to contribute anyway —
# every hook write it could be "catching up" happened before the node went
# down — so it simply stands down and lets the next tick (6 min) handle it.
WATCH_BOOT_GRACE_SEC = 600


def _node_uptime_sec():
    """Seconds since boot, or None if /proc/uptime is unreadable."""
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as fh:
            return float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _watch_should_stand_down():
    """True when watch_statuses() must not touch Firestore this tick."""
    up = _node_uptime_sec()
    if up is not None and up < WATCH_BOOT_GRACE_SEC:
        logger.info(
            f"watch: node booted {up:.0f}s ago (< {WATCH_BOOT_GRACE_SEC}s) — standing down; "
            "node_boot_reconcile owns VM state right after a boot")
        return True
    try:
        fh = open(BOOT_RECONCILE_LOCK, "a+")  # pylint: disable=consider-using-with
    except OSError as e:
        # Cannot even test the lock. The reconcile could not have taken it
        # either, so this is no worse than the pre-lock behaviour — proceed,
        # but say so loudly.
        logger.warning(f"watch: cannot open {BOOT_RECONCILE_LOCK} ({e}); proceeding unguarded")
        return False
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        logger.info("watch: node_boot_reconcile is running — standing down this tick")
        return True
    # We hold it; keep the handle alive for the caller's whole run so a reconcile
    # cannot start underneath us either. Released when the process exits.
    _watch_should_stand_down.held = fh
    return False


def _boot_history_crashloop(max_boots=4, window_sec=900):
    """Record this boot and report whether the node is in a reboot loop.

    Returns True if there have been > max_boots boots within the last window_sec
    seconds — in which case node_boot_reconcile() must NOT auto-start VMs, so a
    guest (or a hardware fault) that keeps taking the node down can't drive a
    start -> crash -> reboot -> start loop. Fails SAFE (returns False) on any
    error so a transient FS problem never blocks recovery.
    """
    try:
        d = Path("/var/lib/neuravps")
        d.mkdir(parents=True, exist_ok=True)
        f = d / "boot_history"
        now = time.time()
        hist = []
        if f.exists():
            hist = [float(x) for x in f.read_text().split() if x.strip()]
        hist = [t for t in hist if 0 <= now - t < window_sec]
        hist.append(now)
        f.write_text(" ".join(f"{t:.0f}" for t in hist[-20:]))
        return len(hist) > max_boots
    except Exception as e:
        logger.warning(f"boot-history check failed ({e}); assuming NOT a reboot loop")
        return False


def node_boot_reconcile():
    """Bring VMs back to their DESIRED state at node boot, without erasing it.

    For each VM that Firestore says should be `running` but is physically stopped
    after this (unplanned) reboot, `qm start` it — guarded by a reboot-loop
    detector. A VM physically running but whose Firestore status lags is synced
    up. We NEVER downgrade running->stopped here.

    Why (incident 2026-07-08, node 0000008): the old node-boot path called
    sync_all_statuses(), which — seeing every VM physically stopped right after a
    reboot — overwrote Firestore running->stopped for ALL of them, ERASING the
    record of what should be running; combined with no auto-start, all 12 VMs
    stayed down until a human restarted each one. This restores them, and keeps
    the desired state intact even when we deliberately don't auto-start (loop).
    """
    try:
        lock_fh = open(BOOT_RECONCILE_LOCK, "a+")  # pylint: disable=consider-using-with
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
    except OSError as e:
        # Never let a lock problem stop the recovery itself — VMs coming back up
        # matters far more than the watch racing us.
        logger.warning(f"could not take {BOOT_RECONCILE_LOCK} ({e}); reconciling unguarded")
        lock_fh = None

    try:
        _node_boot_reconcile_locked()
    finally:
        if lock_fh is not None:
            # flock is released by the close (and by process death, which is why
            # a crashed reconcile can never wedge the watch permanently).
            lock_fh.close()


def _node_boot_reconcile_locked():
    all_vm_status = get_all_vms()
    logger.info(f"All VMs: {sorted(all_vm_status.keys())}")
    loop = _boot_history_crashloop()
    if loop:
        logger.warning(
            "REBOOT LOOP detected (many reboots in a short window) — NOT auto-starting "
            "VMs this boot. Desired state left intact for the operator to resolve."
        )
    for vmid, actual_status in all_vm_status.items():
        desired = get_firestore_status(vmid)
        if desired == "running" and actual_status != "running":
            if loop:
                logger.warning(f"VM {vmid}: desired=running but node in reboot loop — skipping auto-start (Firestore left 'running')")
                continue
            logger.info(f"VM {vmid}: desired=running, stopped after node reboot — auto-starting")
            try:
                run(["qm", "start", str(vmid)])
                time.sleep(2)  # small stagger so N guests don't all boot at once
            except subprocess.CalledProcessError as e:
                logger.error(f"auto-start VM {vmid} failed ({e.stderr or e}); Firestore desired state ('running') left intact")
        elif desired is not None and desired != "running" and actual_status == "running":
            logger.info(f"VM {vmid}: running on host but Firestore={desired}; syncing up to running")
            update_status_in_firestore(vmid, "running")
        # else: already matches, or desired stopped/None & VM stopped -> no-op (never clobber)


BOOT_RAM_GUARD_S = 180


def boot_ram_guard(vmid: int) -> None:
    """Present the FULL plan RAM while the guest boots (best-effort).

    `qm start` seeds the live balloon target from the config `balloon:` line
    (the floor), and pvestatd can squeeze managed VMs further while the host
    is busy. If that happens before Windows finishes memory init, the guest
    LATCHES a reduced TotalVisibleMemorySize — the "booted memory-constrained"
    support case (Task Manager shows 2 GB on a 4 GB plan) that previously only
    a full stop+start cleared. Holding floor==memory for BOOT_RAM_GUARD_S
    makes Windows boot seeing the full plan (pvestatd honours the floor as a
    hard minimum); the floor is then restored — unless something legitimately
    re-raised it meanwhile — and any later runtime squeeze is invisible to a
    booted guest. Must never fail the hook: everything is wrapped.
    """
    try:
        conf = f"/etc/pve/qemu-server/{vmid}.conf"
        memory = balloon = None
        with open(conf) as fh:
            for line in fh:
                if line.startswith("memory:"):
                    memory = int(line.split(":", 1)[1].strip())
                elif line.startswith("balloon:"):
                    balloon = int(line.split(":", 1)[1].strip())
                elif line.startswith("["):
                    break  # snapshot/pending sections follow the main block
        if not memory or balloon is None or balloon <= 0 or balloon >= memory:
            return  # ballooning disabled (0), unmanaged, or already full
        # This hook runs INSIDE `qm start`, which still holds the config lock
        # (a direct `qm set` here exits 4, "VM is locked"). Defer the raise 5 s
        # into a detached transient unit — by then the lock is gone and the
        # Windows memory init is still >10 s away — and restore in the same
        # unit after BOOT_RAM_GUARD_S unless something re-raised meanwhile.
        import shlex
        import socket
        helper = '/usr/local/sbin/neuravps-ram-guard.py'
        if not Path(helper).is_file():
            logger.warning('Boot RAM guard unavailable: missing resource helper')
            return
        token = f'{time.time_ns()}'
        args = ['python3',helper]
        identity = [str(vmid),socket.gethostname(),token]
        script = (shlex.join(args+['boot']+identity) + '; '
                  f'sleep {BOOT_RAM_GUARD_S}; ' +
                  shlex.join(args+['restore']+identity))
        run([
            "systemd-run", "--collect", "--on-active=5",
            f"--unit=nvps-bootram-{vmid}-{int(time.time())}",
            "sh", "-c", script,
        ], check=True)
        logger.info(
            f"Boot RAM guard armed: VM {vmid} floor {balloon}->{memory}MB at "
            f"+5s for {BOOT_RAM_GUARD_S}s (then back to {balloon} unless re-raised)"
        )
    except Exception as e:  # noqa: BLE001 — the hook must never fail on this
        logger.warning(f"Boot RAM guard failed for VM {vmid}: {e}")


# ---------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------
def main():
    if is_running_under_backup():
        return

    # set-server-ipv6 subcommand: update Firestore servers/{id}.ipv6 for one VM
    if len(sys.argv) >= 2 and sys.argv[1] == "set-server-ipv6":
        if len(sys.argv) != 4:
            logger.error("Usage: sync-dnat.py set-server-ipv6 <VMID> <IPV6>")
            sys.exit(2)
        try:
            target_vmid = int(sys.argv[2])
        except ValueError:
            logger.error(f"Invalid VMID: {sys.argv[2]}")
            sys.exit(2)
        ok = set_server_ipv6(target_vmid, sys.argv[3])
        sys.exit(0 if ok else 1)

    # node-boot: stamp lastNodeBootAt, sync all statuses, then honor any
    # user-requested post-boot action (VPS E dedicated nodes only).
    if len(sys.argv) >= 2 and sys.argv[1] == "node-boot":
        logger.info("node-boot: updating lastNodeBootAt and reconciling VMs to desired state")
        update_proxmox_node_last_boot_at()
        try:
            node_boot_reconcile()
        except Exception as e:
            # Never let a transient `qm list` / reconcile failure skip the
            # maintenance finalize below. If it did, the panel would stay stuck
            # on "upgrading"/"rebooting" and the node would be locked out of all
            # future maintenance — exactly the failure this whole path guards.
            logger.warning(f"node-boot: reconcile failed, continuing to finalize: {e}")
        handle_pending_post_boot_action()
        logger.info("Sync complete.")
        return

    # Hook mode: <vmid> <phase>
    if len(sys.argv) >= 3:
        try:
            triggered_vmid = int(sys.argv[1])
        except ValueError:
            logger.error(f"Invalid VMID: {sys.argv[1]}")
            return
        phase = sys.argv[2]

        if phase == "post-start":
            logger.info(f"Hook: VM {triggered_vmid} post-start")
            boot_ram_guard(triggered_vmid)
            update_status_in_firestore(triggered_vmid, "running")
            logger.info("Sync complete.")
            return

        if phase == "post-stop":
            logger.info(f"Hook: VM {triggered_vmid} post-stop")
            update_status_in_firestore(triggered_vmid, "stopped")
            logger.info("Sync complete.")
            return

        # Other phases (pre-start, pre-stop): nothing to do
        return

    # Watchdog mode: local diff only, for the systemd timer.
    if len(sys.argv) >= 2 and sys.argv[1] == "watch":
        fixed = watch_statuses()
        if fixed:
            logger.info(f"watch: corrected {fixed} status(es) a hook had missed")
        return

    # Manual mode (no args): sync all statuses
    logger.info("Manual sync: reconciling all VM statuses")
    sync_all_statuses()
    logger.info("Sync complete.")

if __name__ == "__main__":
    main()

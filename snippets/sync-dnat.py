#!/usr/bin/env python3
"""
sync-dnat: Sync VM running/stopped status to Firestore.

IPv6 model: each VM has a static <prefix>::<vmid_hex> configured in-guest via
legacy netsh (store=persistent), with DHCPv6 + RouterDiscovery DISABLED on the
adapter. dnsmasq serves only IPv4 — no RA, no DHCPv6 — so Windows has zero
auto-config to compete with the manual IPv6. This script does NOT manage any
DHCPv6 pin database (none exists in this model).

Where it writes:
  - With /etc/firebase-credentials.json: straight to Firestore (the fleet).
  - Without it, with /etc/neuravps-node-token: through the `node_sync_api` Cloud
    Function, which only lets a node touch its own VMs and its own node doc
    (nodes we do not hand the service account to; web repo functions/node_api.py).
    The file holds only the secret, generated on the node
    (scripts/node_token_admin.py in the web repo). NEURAVPS_NODE_API_URL and
    NEURAVPS_NODE_TOKEN_FILE override URL and path for local testing.
    In this mode an unreachable function never loses the desired state:
    node-boot records every VM as unresolved before its first call, `watch`
    never publishes `stopped` for those and hands them to `node-boot-retry`
    (a transient unit) until they are resolved or the node boots again.

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
    sync-dnat.py node-boot-retry           — Token mode only, spawned by `watch`:
                                              finish what node-boot could not read.
                                              Before working on VMs by hand, remove
                                              /var/lib/neuravps/node_boot_pending.json
                                              so none is started behind your back.
  Firestore IPv6 update (one VM):
    sync-dnat.py set-server-ipv6 <VMID> <IPV6>
                                            — Set servers/{id}.ipv6 for the
                                              local-node VM matching
                                              (proxmoxId, nodeId).
"""
import contextlib
import fcntl
import http.client
import json
import logging
import os
import random
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
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
# Node API: the same operations without the service account
# ---------------------------------------------------------------
# Standard library only: such a node needs neither firebase_admin nor requests.
NODE_TOKEN_FILE = Path(os.environ.get("NEURAVPS_NODE_TOKEN_FILE", "/etc/neuravps-node-token"))
NODE_API_URL = os.environ.get(
    "NEURAVPS_NODE_API_URL", "https://europe-west1-neuravps.cloudfunctions.net/node_sync_api")
# Generous for a cold start.
NODE_API_TIMEOUT_S = 15
NODE_API_RETRY_CODES = (429, 500, 502, 503, 504)
# A status write runs inside `qm start` (post-start): total time, all attempts
# together. Whatever does not fit is left to the watchdog.
HOOK_BUDGET_S = 12
HOOK_CALL_TIMEOUT_S = 8
# node-boot: time it may LOSE to a failing function. The unit has
# TimeoutStartSec=300 on nodes without the SQX drop-in, and the VM starts need
# most of that.
BOOT_CALL_TIMEOUT_S = 10
NODE_BOOT_API_BUDGET_S = 90
NODE_BOOT_ACTION_BUDGET_S = 30
NODE_BOOT_RETRY_WINDOW_S = 180  # no new retry round after this
ACTION_MAX_RETRIES = 24         # watchdog ticks (~2.5 h) for the maintenance flags
BOOT_PENDING_FILE = Path("/var/lib/neuravps/node_boot_pending.json")
BOOT_PENDING_LOCK = "/run/neuravps-boot-pending.lock"
_UNRESOLVED = object()


class NodeApiError(Exception):
    """Never carries the token."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code

    @property
    def rejected(self):
        return self.code in (401, 403)

    @property
    def transient(self):
        return self.code is None or self.code in NODE_API_RETRY_CODES


class _DeadlineExceeded(BaseException):
    """BaseException so no `except Exception` on the way swallows it."""


@contextlib.contextmanager
def _hard_deadline(seconds):
    """Wall-clock bound: socket timeouts do not cover DNS or a trickling reply."""
    def _expired(signum, frame):
        raise _DeadlineExceeded()
    previous = signal.signal(signal.SIGALRM, _expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib would carry the Authorization header to wherever it was sent.
    def redirect_request(self, *args, **kwargs):
        return None


_node_api_opener = urllib.request.build_opener(_NoRedirect)


def use_node_api():
    """True only when there is no service account and there is a node token."""
    creds = Path(os.environ.get("FIREBASE_CREDENTIALS_FILE", "/etc/firebase-credentials.json"))
    return not creds.is_file() and NODE_TOKEN_FILE.is_file()


def _node_api(op, attempts=3, timeout=None, **fields):
    """POST one operation and return the decoded reply, or raise NodeApiError.

    `attempts` covers what the Firestore SDK retries by itself on the fleet
    (network errors, 429, 5xx). Callers with their own retry loop pass 1.
    """
    try:
        secret = NODE_TOKEN_FILE.read_text().strip()
    except (OSError, UnicodeError) as e:
        raise NodeApiError(f"cannot read {NODE_TOKEN_FILE}: {type(e).__name__}") from None
    if not re.fullmatch(r"[A-Za-z0-9+/=_-]{32,128}", secret):
        # Checked here because http.client echoes an invalid header value.
        raise NodeApiError(f"{NODE_TOKEN_FILE} does not hold a valid token", code=401)
    request = urllib.request.Request(
        NODE_API_URL,
        data=json.dumps(dict(fields, op=op)).encode(),
        headers={"Authorization": f"Bearer {NODE_NAME}.{secret}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    for attempt in range(1, attempts + 1):
        try:
            with _node_api_opener.open(request, timeout=timeout or NODE_API_TIMEOUT_S) as reply:
                data = json.load(reply)
            if not isinstance(data, dict):
                raise ValueError("reply is not an object")
            return data
        except urllib.error.HTTPError as e:
            error, code = f"HTTP {e.code}", e.code
            retry = code in NODE_API_RETRY_CODES
        except (OSError, ValueError, http.client.HTTPException) as e:
            error, code, retry = f"{type(e).__name__}: {e}", None, True
        if not retry or attempt == attempts:
            raise NodeApiError(f"{op}: {error}", code=code)
        time.sleep(0.5 * (2 ** (attempt - 1)) + random.uniform(0, 0.5))


def _api_desired_status(vmid, timeout):
    """Status on the server doc, None if there is no doc. Raises if unknown."""
    docs = _node_api("get_server", attempts=1, timeout=timeout, vmid=vmid)["servers"]
    return docs[0].get("status") if docs else None


def _api_update_status(vmid, status):
    """update_status_in_firestore through the node API, within HOOK_BUDGET_S:
    same re-check of the local state between reading the doc and writing it, and
    the same update_time precondition (enforced by the function). Returns False
    if it could not be written."""
    # Until the write is acknowledged the watchdog must see this VM as drifted,
    # even if it ends up in the state the cache remembers.
    _forget_status(vmid)
    try:
        with _hard_deadline(HOOK_BUDGET_S):
            return _api_write_status(vmid, status)
    except _DeadlineExceeded:
        logger.warning(f"Status of VM {vmid} not written within {HOOK_BUDGET_S}s; the watchdog will retry")
        return False


def _api_write_status(vmid, status):
    began = time.monotonic()
    attempts = 4
    for attempt in range(1, attempts + 1):
        timeout = max(1.0, min(HOOK_CALL_TIMEOUT_S, HOOK_BUDGET_S - (time.monotonic() - began)))
        try:
            docs = _node_api("get_server", attempts=1, timeout=timeout, vmid=vmid)["servers"]
            if not docs:
                logger.debug(f"No Firestore document found for VM {vmid} (nodeId={NODE_NAME}) to update status")
                return True
            if len(docs) > 1:
                logger.warning(f"Multiple Firestore documents for VM {vmid}; updating all")
            for doc in docs:
                if not status_is_current_and_local(vmid, status):
                    return True
                _node_api("set_status", attempts=1, timeout=timeout, vmid=vmid, serverId=doc["id"],
                          updateTime=doc["updateTime"], status=status)
                logger.info(f"Updated Firestore server {doc['id']} (VM {vmid}): status={status}")
            _remember_status(vmid, status)
            return True
        except NodeApiError as e:
            if not (e.transient or e.code == 409):
                logger.warning(f"Status of VM {vmid} refused, not retrying: {e}")
                return False
            if attempt == attempts or time.monotonic() - began >= HOOK_BUDGET_S - 1:
                logger.warning(f"Failed to update status for VM {vmid} after {attempt} attempts: {e}")
                return False
            backoff = 0.5 * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            logger.info(f"update_status VM {vmid} attempt {attempt}/{attempts} failed ({e}); retrying in {backoff:.1f}s")
            time.sleep(backoff)
        except Exception as e:
            logger.warning(f"Failed to update status for VM {vmid}: {e}")
            return False
    return False


# --- what node-boot has not resolved yet (token mode only) -------------------
def _boot_id():
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


_boot_unsaved = None  # fallback if the file cannot be written: still start the VMs


def _load_boot_state():
    """This boot's unresolved work, or None. A previous boot's file is ignored."""
    if _boot_unsaved is not None:
        return json.loads(json.dumps(_boot_unsaved))
    try:
        state = json.loads(BOOT_PENDING_FILE.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict) or state.get("boot_id") != _boot_id():
        return None
    return state


def _write_boot_state(state):
    global _boot_unsaved
    try:
        if not state.get("vms") and state.get("action") is None:
            BOOT_PENDING_FILE.unlink(missing_ok=True)
            _boot_unsaved = None
            return
        BOOT_PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = BOOT_PENDING_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(BOOT_PENDING_FILE)
        _boot_unsaved = None
    except OSError as e:
        _boot_unsaved = state
        logger.warning(f"Could not persist {BOOT_PENDING_FILE}: {e}")


@contextlib.contextmanager
def _boot_state():
    """Locked read-modify-write (node-boot and the hooks run concurrently)."""
    try:
        lock = open(BOOT_PENDING_LOCK, "a+")  # pylint: disable=consider-using-with
        fcntl.flock(lock, fcntl.LOCK_EX)
    except OSError as e:
        logger.warning(f"could not take {BOOT_PENDING_LOCK} ({e})")
        lock = None
    try:
        state = _load_boot_state()
        yield state
        if state is not None:
            _write_boot_state(state)
    finally:
        if lock is not None:
            lock.close()


def _boot_pending_init():
    """Before the first call: if node-boot dies or the function is down, every
    VM is still known to be unresolved."""
    vms = sorted(int(p.stem) for p in VM_CONFIG_DIR.glob("*.conf") if p.stem.isdigit())
    state = {"boot_id": _boot_id(), "vms": vms, "loop": False,
             "action": {"tries": 0, "finalize": None}}
    _write_boot_state(state)
    return state


def _boot_pending_vms():
    state = _load_boot_state()
    return set(state.get("vms") or []) if state else set()


def _settle_boot_vm(vmid):
    """Its desired state was read and acted on, or a hook has since changed it."""
    with _boot_state() as state:
        if state and vmid in (state.get("vms") or []):
            state["vms"].remove(vmid)


class _ApiBudget:
    """Time that failing calls and the pauses between them may consume."""

    def __init__(self, seconds):
        self.left = float(seconds)

    def spent(self):
        return self.left < 1

    def call(self, fn):
        timeout = min(BOOT_CALL_TIMEOUT_S, max(1.0, self.left))
        began = time.monotonic()
        try:
            with _hard_deadline(timeout + 2):
                return fn(timeout)
        except _DeadlineExceeded:
            self.left -= time.monotonic() - began
            raise NodeApiError("deadline exceeded") from None
        except NodeApiError:
            self.left -= time.monotonic() - began
            raise

    def pause(self, seconds):
        seconds = min(seconds, max(0.0, self.left))
        time.sleep(seconds)
        self.left -= seconds

    def retry(self, fn, attempts=3):
        for attempt in range(1, attempts + 1):
            try:
                return self.call(fn)
            except NodeApiError as e:
                if not e.transient or attempt == attempts or self.spent():
                    raise
                self.pause(2 * attempt)


_boot_budget = None


def _api_boot_reconcile(loop=None):
    """node_boot_reconcile for token mode. A VM whose desired state cannot be
    read stays unresolved and is retried: here within the budget, afterwards by
    the watchdog's node-boot-retry."""
    budget = _boot_budget or _ApiBudget(NODE_BOOT_API_BUDGET_S)
    if _load_boot_state() is None:
        _boot_pending_init()
    if loop is None:
        loop = _boot_history_crashloop()
    present = get_all_vms()
    with _boot_state() as state:
        if state:
            # VMs the config listing missed join; VMs that are gone leave.
            if not state.get("listed"):
                state["vms"] = sorted(set(state["vms"]) | set(present))
            if present:  # an empty list may just be pmxcfs not being up yet
                state["vms"] = [v for v in state["vms"] if v in present]
            state["loop"] = loop
            state["listed"] = True

    window_end = time.monotonic() + NODE_BOOT_RETRY_WINDOW_S
    delay = 2.0
    rejected = False
    while True:
        pending = _boot_pending_vms()
        if not pending:
            return
        failures = 0

        def read(vmid):
            nonlocal failures, rejected
            # Settled meanwhile by a hook, or no point in calling any more.
            if vmid not in _boot_pending_vms() or rejected or failures >= 3 or budget.spent():
                return _UNRESOLVED
            try:
                status = budget.call(lambda t: _api_desired_status(vmid, t))
            except NodeApiError as e:
                failures += 1
                rejected = e.rejected
                logger.warning(f"VM {vmid}: desired state unknown ({e}); left unresolved")
                return _UNRESOLVED
            failures = 0
            return status

        _node_boot_reconcile_locked(read, _settle_boot_vm, only=pending, loop=loop)
        left = _boot_pending_vms()
        if not left:
            return
        if rejected or budget.spent() or time.monotonic() + delay > window_end:
            break
        budget.pause(delay)
        delay = min(delay * 2, 20.0)

    why = "the node token was REJECTED" if rejected else "the node API is not answering"
    logger.error(f"node-boot: {why}; desired state of VMs {sorted(left)} unresolved. "
                 "They are NOT published as stopped; the watchdog will retry")


def _api_post_boot_action(initial):
    """handle_pending_post_boot_action for token mode. `initial` is node-boot
    itself; later calls come from node-boot-retry."""
    state = _load_boot_state()
    if state is None and initial:
        state = _boot_pending_init()
    action = (state or {}).get("action")
    if action is None:
        return

    def done():
        with _boot_state() as current:
            if current:
                current["action"] = None

    budget = _ApiBudget(NODE_BOOT_ACTION_BUDGET_S)
    try:
        decided = action.get("finalize")  # set: the VM part is done, only the write is missing
        if not decided:
            node = budget.retry(lambda t: _node_api("get_node", attempts=1, timeout=t))
            if not node.get("exists") or (node.get("pendingPostBootAction") is None
                                          and not node.get("maintenance")):
                done()
                return
            started = node.get("maintenanceStartedAt")
            uptime = _node_uptime_sec()
            if not initial and started and uptime is not None and started > time.time() - uptime:
                logger.warning("node-boot: the open maintenance cycle started after this boot; leaving it")
                done()
                return
            vm_started = _auto_start_after_user_reboot(node)
            decided = {"cycle": node["cycle"], "vmStarted": vm_started}
            with _boot_state() as current:
                if current and current.get("action") is not None:
                    current["action"]["finalize"] = decided
        budget.retry(lambda t: _node_api("finalize_boot", attempts=1, timeout=t, **decided))
        logger.info(f"node-boot: finalized maintenance on {NODE_NAME} (vm_started={decided['vmStarted']})")
        done()
    except NodeApiError as e:
        if e.code == 409:
            logger.warning("node-boot: a newer maintenance cycle is open; leaving it")
            done()
        else:
            logger.error(f"node-boot: could not finalize maintenance on {NODE_NAME} ({e}); "
                         "maintenance/pendingPostBootAction may still be set. The watchdog will retry")


def node_boot_retry():
    """Finish, from the watchdog, what node-boot could not read or write."""
    global _boot_budget
    _boot_budget = _ApiBudget(NODE_BOOT_API_BUDGET_S)
    with open(BOOT_RECONCILE_LOCK, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # the watch stands down meanwhile
        state = _load_boot_state()
        if state is None:
            return
        if state.get("vms"):
            _api_boot_reconcile(loop=bool(state.get("loop")))
        with _boot_state() as current:
            action = (current or {}).get("action")
            if action is None:
                return
            action["tries"] = action.get("tries", 0) + 1
            if action["tries"] > ACTION_MAX_RETRIES:
                logger.error(f"node-boot: giving up on the maintenance flags of {NODE_NAME} after "
                             f"{ACTION_MAX_RETRIES} retries; clear them by hand")
                current["action"] = None
                return
        _api_post_boot_action(initial=False)


def _spawn_boot_retry_if_pending():
    """The watch unit has TimeoutStartSec=120, too short to start VMs in."""
    state = _load_boot_state()
    if not state or (not state.get("vms") and state.get("action") is None):
        return
    try:
        run(["systemd-run", "--collect", "--no-block", "--unit=nvps-boot-retry",
             "--property=Type=oneshot", "--property=TimeoutStartSec=1800",
             "/usr/bin/python3", os.path.abspath(__file__), "node-boot-retry"])
    except (OSError, subprocess.CalledProcessError) as e:
        logger.info(f"watch: node-boot-retry not launched (already running?): {e}")


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
        with _STATUS_CACHE.open() as fh:
            raw = json.load(fh)
        return {int(k): str(v) for k, v in raw.items()}
    except Exception:
        # Missing or corrupt: treat as "we know nothing", which makes the next
        # run push every VM once and rebuild the file. Never fatal.
        return {}


def _save_status_cache(cache):
    try:
        _STATUS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATUS_CACHE.with_suffix(".tmp")
        with tmp.open("w") as fh:
            json.dump({str(k): v for k, v in cache.items()}, fh)
        tmp.replace(_STATUS_CACHE)
    except Exception as e:
        # A cache we cannot persist only costs a redundant write next run.
        logger.warning(f"Could not persist VM status cache: {e}")


def _forget_status(vmid):
    cache = _load_status_cache()
    if cache.pop(int(vmid), None) is not None:
        _save_status_cache(cache)


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
    if use_node_api():
        try:
            docs = _node_api("get_server", vmid=vmid)["servers"]
            if not docs:
                return None
            if len(docs) > 1:
                logger.warning(f"Multiple Firestore documents for VM {vmid}; using first")
            return docs[0].get("status")
        except Exception as e:
            logger.warning(f"Failed to get status for VM {vmid} from Firestore: {e}")
            return None
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
    if use_node_api():
        try:
            # The function only accepts the deterministic address of the vmid.
            reply = _node_api("set_ipv6", vmid=vmid, ipv6=ipv6)
            if not reply.get("updated"):
                logger.warning(f"set-server-ipv6: no server doc for VM {vmid} (nodeId={NODE_NAME})")
                return False
            logger.info(f"set-server-ipv6: server {reply.get('servers')} (VM {vmid}) ipv6={ipv6}")
            return True
        except Exception as e:
            logger.warning(f"set-server-ipv6 failed for VM {vmid}: {e}")
            return False
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
    if use_node_api():
        return _api_update_status(vmid, status)
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
    if use_node_api():
        try:
            budget = _boot_budget or _ApiBudget(NODE_BOOT_API_BUDGET_S)
            budget.retry(lambda t: _node_api("node_booted", attempts=1, timeout=t), attempts=2)
            logger.info(f"Updated proxmox_nodes/{NODE_NAME} lastNodeBootAt")
        except Exception as e:
            logger.warning(f"Failed to update lastNodeBootAt for proxmox_nodes/{NODE_NAME}: {e}")
        return
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


def _auto_start_after_user_reboot(node_data):
    """Start the dedicated node's VM if the user asked for it. Returns
    None (not attempted) or whether the VM is up."""
    pending = node_data.get("pendingPostBootAction")
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
    return vm_started


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
    if use_node_api():
        try:
            _api_post_boot_action(initial=True)
        except Exception as e:
            logger.warning(f"handle_pending_post_boot_action failed on {NODE_NAME}: {e}")
        return
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

        vm_started = _auto_start_after_user_reboot(node_data)

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
    unresolved = _boot_pending_vms() if use_node_api() else set()
    for vmid, actual_status in all_vm_status.items():
        if actual_status == "stopped" and vmid in unresolved:
            logger.info(f"VM {vmid}: desired state not resolved since boot — not publishing stopped")
            continue
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

    if use_node_api():
        _spawn_boot_retry_if_pending()
        # A guest stopped only because node-boot could not read what it should
        # be: publishing that would erase the desired state.
        unresolved = _boot_pending_vms()
        drifted = {v: st for v, st in drifted.items() if not (st == "stopped" and v in unresolved)}

    if not drifted:
        return 0
    logger.info(f"watch: {len(drifted)} VM(s) out of sync with Firestore: {drifted}")
    failures = 0
    for vmid, status in drifted.items():
        # False only comes from the node API; give up after 3 in a row so the
        # unit's TimeoutStartSec is not spent on a function that is down.
        failures = failures + 1 if update_status_in_firestore(vmid, status) is False else 0
        if failures >= 3:
            logger.warning("watch: node API failing; the rest is left for the next tick")
            break
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
        if use_node_api():
            _api_boot_reconcile()
        else:
            _node_boot_reconcile_locked()
    finally:
        if lock_fh is not None:
            # flock is released by the close (and by process death, which is why
            # a crashed reconcile can never wedge the watch permanently).
            lock_fh.close()


BOOT_STAGGER_FILE = "/etc/default/neuravps-boot-stagger"


def _boot_stagger_config():
    """Memory-paced auto-start after a node reboot — OPT-IN per node.

    Why (pilot 0000260-AX102-3-LTD, 03/11/2026): a Windows guest touches its
    whole RAM while booting, before the balloon reclaims anything. 8 vps-b
    (19 GB) started 5 s apart on a 125 GB host took MemAvailable from 105 to
    4.5 GB, pushed 8.4 GB to host swap and PSI-mem to 6.5 for 1-2 min; in
    production the boot RAM guard also holds every guest at its full RAM for
    180 s, which makes it worse. On the 256/384 GB AX162s and the 4 GB MT
    guests this never happens, so the fleet keeps the plain 2 s stagger.

    File format (shell-style KEY=VALUE): MEM_STAGGER=1 enables it;
    RESERVE_MB (12288, same margin as the RAM guard), MIN_GAP_S (30) and
    MAX_WAIT_S (240: a VM is started anyway after that — a customer left down
    is worse than a minute of pressure) and TOTAL_MAX_S, a DEADLINE for the
    pacing measured from the start of the reconcile (starts included): past
    it, the remaining VMs start with the plain 2 s stagger. node-boot-sync-dnat.service has TimeoutStartSec=300 and systemd
    KILLS the reconcile when it is exceeded — the VMs not yet started then
    stay down and the watch later records them as stopped — so the default
    (120, leaving ~12 s x VM for the unpaced starts) stays under it; install-sqx-node-extras.sh raises the unit timeout
    with a drop-in and sets TOTAL_MAX_S=1200. Absent/unreadable/MEM_STAGGER!=1
    -> None (old behaviour)."""
    try:
        with open(BOOT_STAGGER_FILE) as fh:
            raw = dict(line.strip().split("=", 1) for line in fh
                       if "=" in line and not line.lstrip().startswith("#"))
    except (OSError, ValueError):
        return None
    if raw.get("MEM_STAGGER", "").strip() != "1":
        return None
    def num(key, default):
        try:
            return max(0, int(raw.get(key, default)))
        except (TypeError, ValueError):
            return default
    return {"reserve_mb": num("RESERVE_MB", 12288), "min_gap_s": num("MIN_GAP_S", 30),
            "max_wait_s": num("MAX_WAIT_S", 240), "total_max_s": num("TOTAL_MAX_S", 120)}


def _mem_available_mb():
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _vm_memory_mb(vmid):
    try:
        with open(f"/etc/pve/qemu-server/{vmid}.conf") as fh:
            for line in fh:
                if line.startswith("["):
                    break
                if line.startswith("memory:"):
                    return int(line.split(":", 1)[1].strip())
    except (OSError, ValueError):
        pass
    return None


def _wait_for_boot_memory(vmid, cfg, last_start, sleep=time.sleep,
                          now=time.monotonic, avail=_mem_available_mb,
                          memory=_vm_memory_mb):
    """Block until the host can absorb this guest's boot: MemAvailable >=
    its full RAM + reserve, and at least min_gap_s since the previous start.
    Never longer than max_wait_s. Returns the seconds waited."""
    start = now()
    need = (memory(vmid) or 0) + cfg["reserve_mb"]
    while True:
        waited = now() - start
        if waited >= cfg["max_wait_s"]:
            logger.warning(f"boot stagger: VM {vmid} started after {waited:.0f}s "
                           f"without {need} MB available (cap reached)")
            return waited
        gap_ok = last_start is None or now() - last_start >= cfg["min_gap_s"]
        free = avail()
        if gap_ok and (free is None or free >= need):
            if waited >= 1:
                logger.info(f"boot stagger: VM {vmid} waited {waited:.0f}s "
                            f"(MemAvailable {free} MB, need {need})")
            return waited
        sleep(5)


def _node_boot_reconcile_locked(read_desired=None, on_resolved=None, only=None, loop=None):
    """The arguments are for token mode (_api_boot_reconcile): a reader that may
    answer _UNRESOLVED, a callback once a VM is dealt with, the VMs left to do
    and the reboot-loop verdict already taken for this boot."""
    stagger = _boot_stagger_config()
    last_start = None
    reconcile_start = time.monotonic()
    budget_spent_logged = False
    if stagger:
        logger.info(f"boot stagger ON: {stagger}")
    all_vm_status = get_all_vms()
    logger.info(f"All VMs: {sorted(all_vm_status.keys())}")
    if loop is None:
        loop = _boot_history_crashloop()
    if loop:
        logger.warning(
            "REBOOT LOOP detected (many reboots in a short window) — NOT auto-starting "
            "VMs this boot. Desired state left intact for the operator to resolve."
        )
    for vmid, actual_status in all_vm_status.items():
        if only is not None and vmid not in only:
            continue
        desired = (read_desired or get_firestore_status)(vmid)
        if desired is _UNRESOLVED:
            continue
        if desired == "running" and actual_status != "running":
            if loop:
                logger.warning(f"VM {vmid}: desired=running but node in reboot loop — skipping auto-start (Firestore left 'running')")
                if on_resolved:
                    on_resolved(vmid)
                continue
            logger.info(f"VM {vmid}: desired=running, stopped after node reboot — auto-starting")
            if stagger is not None:
                left = stagger["total_max_s"] - (time.monotonic() - reconcile_start)
                if left > 0:
                    _wait_for_boot_memory(
                        vmid, dict(stagger, max_wait_s=min(stagger["max_wait_s"], left)),
                        last_start)
                elif not budget_spent_logged:
                    logger.warning("boot stagger: total wait budget spent — starting the "
                                   "remaining VMs without waiting")
                    budget_spent_logged = True
            try:
                run(["qm", "start", str(vmid)])
                last_start = time.monotonic()
                time.sleep(2)  # small stagger so N guests don't all boot at once
            except subprocess.CalledProcessError as e:
                logger.error(f"auto-start VM {vmid} failed ({e.stderr or e}); Firestore desired state ('running') left intact")
        elif desired is not None and desired != "running" and actual_status == "running":
            logger.info(f"VM {vmid}: running on host but Firestore={desired}; syncing up to running")
            update_status_in_firestore(vmid, "running")
        # else: already matches, or desired stopped/None & VM stopped -> no-op (never clobber)
        if on_resolved:
            on_resolved(vmid)


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
        if use_node_api():
            global _boot_budget
            _boot_budget = _ApiBudget(NODE_BOOT_API_BUDGET_S)
            _boot_pending_init()
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

    if len(sys.argv) >= 2 and sys.argv[1] == "node-boot-retry":
        if use_node_api():
            node_boot_retry()
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
            if use_node_api():
                _settle_boot_vm(triggered_vmid)
            update_status_in_firestore(triggered_vmid, "running")
            logger.info("Sync complete.")
            return

        if phase == "post-stop":
            logger.info(f"Hook: VM {triggered_vmid} post-stop")
            if use_node_api():
                _settle_boot_vm(triggered_vmid)
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

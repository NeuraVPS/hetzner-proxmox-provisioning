#!/usr/bin/env python3
"""
sync-dnat: Sync VM running/stopped status to Firestore and keep dnsmasq dhcp-host pins
fresh. IPv6 is now deterministic per VMID; this script no longer discovers or writes
IPs.

Commands:
  Hook (Proxmox):
    sync-dnat.py <VMID> post-start         — Set status running, regen dhcp pins.
    sync-dnat.py <VMID> post-stop          — Set status stopped, regen dhcp pins.
  Manual sync:
    sync-dnat.py                           — Sync all local VMs' status + regen pins.
  Node boot:
    sync-dnat.py node-boot                 — Set proxmox_nodes/<hostname>.lastNodeBootAt,
                                              sync all VM statuses, regen dhcp pins.
  Pins-only:
    sync-dnat.py regen-pins                — Regenerate /etc/dnsmasq.d/vm-pins.conf.
  Firestore IPv6 update (one VM):
    sync-dnat.py set-server-ipv6 <VMID> <IPV6>
                                            — Set servers/{id}.ipv6 for the local-node
                                              VM matching (proxmoxId, nodeId). Used by
                                              the deterministic-ipv6 migration tool.
"""
import logging
import os
import re
import subprocess
import sys
import tempfile
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
# DHCP pin regeneration
# ---------------------------------------------------------------
PINS_FILE = Path("/etc/dnsmasq.d/vm-pins.conf")
# Match any 6-octet MAC (XX:XX:XX:XX:XX:XX) in the net0 line. The Proxmox VM
# config format is `net0: <model>=<MAC>,bridge=<bridge>,...` (e.g. `virtio=BC:24:11:5E:71:6A,bridge=vmbr0`),
# i.e. the MAC follows the model key directly — there is no `mac=` / `macaddr=`
# field. We just grab the first MAC-shaped token on the line.
NET0_MAC_RE = re.compile(r"\b([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})\b")

def _vmbr0_prefix():
    """Derive the /64 prefix from vmbr0's first global IPv6 (e.g. '2a01:4f9:6a:44eb::')."""
    out = run(["ip", "-6", "-o", "addr", "show", "dev", "vmbr0", "scope", "global"])
    if not out:
        raise RuntimeError("vmbr0 has no global IPv6 address")
    # Each line looks like:  2: vmbr0    inet6 2a01:4f9:6a:44eb::1/64 scope global ...
    addr = out.splitlines()[0].split()[3].split("/")[0]
    if "::" not in addr:
        raise RuntimeError(f"vmbr0 IPv6 has unexpected shape: {addr}")
    parts = addr.split(":")
    # Take first 4 hextets to form the /64 prefix
    if len(parts) < 4:
        raise RuntimeError(f"vmbr0 IPv6 has fewer than 4 hextets: {addr}")
    return ":".join(parts[:4]) + "::"

def _extract_net0_mac(conf_text):
    """Extract MAC from the net0: line of a Proxmox VM config. Returns lowercased mac or None."""
    for line in conf_text.splitlines():
        line = line.strip()
        if not line.startswith("net0:"):
            continue
        m = NET0_MAC_RE.search(line)
        if m:
            return m.group(1).lower()
    return None

def regen_dhcp_pins():
    """Rewrite /etc/dnsmasq.d/vm-pins.conf from /etc/pve/qemu-server/*.conf and reload dnsmasq.

    Idempotent. Safe to call from hooks, manual sync, and on-demand. Errors are logged
    but never raised — pin regen is best-effort and must not block status updates.
    """
    try:
        prefix = _vmbr0_prefix()
    except Exception as e:
        logger.warning(f"regen_dhcp_pins: cannot derive vmbr0 prefix: {e}")
        return

    lines = []
    skipped_no_mac = []
    qemu_dir = Path("/etc/pve/qemu-server")
    if not qemu_dir.is_dir():
        logger.warning(f"regen_dhcp_pins: {qemu_dir} not found; skipping")
        return

    for cfg in sorted(qemu_dir.glob("*.conf")):
        try:
            vmid = int(cfg.stem)
        except ValueError:
            continue
        if not 100 <= vmid <= 9999:
            continue
        try:
            mac = _extract_net0_mac(cfg.read_text())
        except Exception as e:
            logger.warning(f"regen_dhcp_pins: failed to read {cfg}: {e}")
            continue
        if not mac:
            skipped_no_mac.append(vmid)
            continue
        lines.append(f"dhcp-host={mac},[{prefix}{vmid:x}],vm{vmid},infinite\n")

    if skipped_no_mac:
        logger.warning(
            f"regen_dhcp_pins: skipped {len(skipped_no_mac)} VM(s) with no extractable net0 MAC: {skipped_no_mac}"
        )

    new_content = "".join(lines)
    try:
        existing = PINS_FILE.read_text() if PINS_FILE.exists() else ""
    except Exception:
        existing = ""

    if existing == new_content:
        logger.info(f"regen_dhcp_pins: pins unchanged ({len(lines)} entries); no reload needed (prefix={prefix})")
        return

    try:
        # Atomic write via tempfile in the same directory
        with tempfile.NamedTemporaryFile(
            mode="w", dir=str(PINS_FILE.parent), prefix=".vm-pins.", suffix=".tmp", delete=False
        ) as tmp:
            tmp.write(new_content)
            tmp_path = Path(tmp.name)
        os.chmod(tmp_path, 0o644)
        tmp_path.replace(PINS_FILE)
    except Exception as e:
        logger.warning(f"regen_dhcp_pins: failed to write {PINS_FILE}: {e}")
        return

    try:
        # Must use restart, not reload: dnsmasq SIGHUP doesn't re-parse main config
        # files (it only reloads /etc/hosts, --dhcp-hostsfile, --dhcp-hostsdir, etc.).
        # Our vm-pins.conf lives in /etc/dnsmasq.d/ which is parsed at startup, so
        # `systemctl reload` would leave the old pins in memory.
        result = subprocess.run(
            ["systemctl", "restart", "dnsmasq"], check=False, capture_output=True, text=True
        )
        if result.returncode != 0:
            logger.warning(
                f"regen_dhcp_pins: dnsmasq restart failed (rc={result.returncode}): {result.stderr.strip()}"
            )
        else:
            logger.info(f"regen_dhcp_pins: wrote {len(lines)} pin(s), restarted dnsmasq (prefix={prefix})")
    except Exception as e:
        logger.warning(f"regen_dhcp_pins: dnsmasq restart failed: {e}")

def regen_dhcp_pins_safe():
    """Call regen_dhcp_pins() and swallow all exceptions (used from hot paths)."""
    try:
        regen_dhcp_pins()
    except Exception as e:
        logger.warning(f"regen_dhcp_pins_safe: {e}")

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


def update_status_in_firestore(vmid, status):
    if not ensure_firebase_initialized():
        return
    try:
        db = firestore.client()
        docs = _query_local_server_doc(db, vmid)
        if not docs:
            logger.debug(f"No Firestore document found for VM {vmid} (nodeId={NODE_NAME}) to update status")
            return
        if len(docs) > 1:
            logger.warning(f"Multiple Firestore documents for VM {vmid}; updating all")
        for doc_snapshot in docs:
            db.collection('servers').document(doc_snapshot.id).update({
                'status': status,
                'lastStatusUpdate': firestore.SERVER_TIMESTAMP,
            })
            logger.info(f"Updated Firestore server {doc_snapshot.id} (VM {vmid}): status={status}")
    except Exception as e:
        logger.warning(f"Failed to update status for VM {vmid} in Firestore: {e}")

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

def sync_all_statuses():
    """Reconcile every local VM's running/stopped state with Firestore."""
    all_vm_status = get_all_vms()
    logger.info(f"All VMs: {sorted(all_vm_status.keys())}")
    for vmid, actual_status in all_vm_status.items():
        firestore_status = get_firestore_status(vmid)
        if firestore_status != actual_status:
            logger.info(f"VM {vmid} status changed from {firestore_status} to {actual_status}, updating Firestore")
            update_status_in_firestore(vmid, actual_status)

# ---------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------
def main():
    if is_running_under_backup():
        return

    # regen-pins subcommand: regenerate dnsmasq pins and exit
    if len(sys.argv) >= 2 and sys.argv[1] == "regen-pins":
        regen_dhcp_pins()
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

    # node-boot: stamp lastNodeBootAt, then sync all statuses + regen pins
    if len(sys.argv) >= 2 and sys.argv[1] == "node-boot":
        logger.info("node-boot: updating lastNodeBootAt and syncing VM statuses")
        update_proxmox_node_last_boot_at()
        sync_all_statuses()
        regen_dhcp_pins_safe()
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
            update_status_in_firestore(triggered_vmid, "running")
            regen_dhcp_pins_safe()
            logger.info("Sync complete.")
            return

        if phase == "post-stop":
            logger.info(f"Hook: VM {triggered_vmid} post-stop")
            update_status_in_firestore(triggered_vmid, "stopped")
            regen_dhcp_pins_safe()
            logger.info("Sync complete.")
            return

        # Other phases (pre-start, pre-stop): nothing to do
        return

    # Manual mode (no args): sync all statuses, regen pins
    logger.info("Manual sync: reconciling all VM statuses")
    sync_all_statuses()
    regen_dhcp_pins_safe()
    logger.info("Sync complete.")

if __name__ == "__main__":
    main()

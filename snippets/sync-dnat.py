#!/usr/bin/env python3
"""
sync-dnat: Sync VM running/stopped status to Firestore + maintain DHCPv6 pins.

IPv6 model: stateful DHCPv6. Each VM is pinned to <prefix>::<vmid_hex> by MAC
in /etc/dnsmasq.d/vm-pins.conf. dnsmasq RA flags M=1, A=0 → Windows uses ONLY
DHCPv6 for addresses (no SLAAC), and the pinned IP is reissued on every boot
via DHCPv6 SOLICIT — self-healing against any in-guest config drift.

Commands:
  Hook (Proxmox):
    sync-dnat.py <VMID> post-start         — Status=running + regen pins.
    sync-dnat.py <VMID> post-stop          — Status=stopped + regen pins.
  Manual sync:
    sync-dnat.py                           — Sync all VM statuses + regen pins.
  Node boot:
    sync-dnat.py node-boot                 — Set lastNodeBootAt, sync all
                                              statuses, regen pins.
  DHCPv6 pin file regeneration:
    sync-dnat.py regen-pins                — Rewrite /etc/dnsmasq.d/vm-pins.conf
                                              from /etc/pve/qemu-server/*.conf
                                              and reload dnsmasq.
  Firestore IPv6 update (one VM):
    sync-dnat.py set-server-ipv6 <VMID> <IPV6>
                                            — Set servers/{id}.ipv6 for the
                                              local-node VM matching
                                              (proxmoxId, nodeId).
"""
import logging
import os
import re
import subprocess
import sys
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
# DHCPv6 pin file
# ---------------------------------------------------------------
_MAC_RE = re.compile(r'\b([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})\b')

def _vmbr0_prefix():
    """Return '<a:b:c:d>::' from vmbr0's first global v6 address, or None."""
    try:
        out = subprocess.run(
            ["ip", "-6", "-o", "addr", "show", "vmbr0", "scope", "global"],
            capture_output=True, text=True, check=True
        ).stdout
        first = (out.splitlines() or [""])[0].split()
        if len(first) < 4:
            return None
        addr = first[3].split("/")[0]
        parts = addr.split(":")
        if len(parts) < 4:
            return None
        return ":".join(parts[:4]) + "::"
    except (subprocess.CalledProcessError, FileNotFoundError, IndexError):
        return None

def _extract_net0_mac(conf_text):
    for line in conf_text.splitlines():
        if line.startswith("net0:"):
            m = _MAC_RE.search(line)
            return m.group(1).lower() if m else None
    return None

def regen_dhcp_pins():
    """Rewrite /etc/dnsmasq-vm-pins.hosts from /etc/pve/qemu-server/*.conf and
    SIGHUP dnsmasq. The file is referenced by `dhcp-hostsfile=` in the main
    dnsmasq config — SIGHUP re-reads it (whereas /etc/dnsmasq.d/*.conf is only
    re-read on full restart). Each VM 100..9999 with a parseable net0 MAC
    gets a pin `<mac>,[<prefix>::<vmid_hex>],vm<vmid>,infinite`. Idempotent."""
    prefix = _vmbr0_prefix()
    if not prefix:
        logger.error("regen-pins: cannot derive vmbr0 /64 prefix")
        return False

    lines = []
    for cfg in sorted(Path("/etc/pve/qemu-server").glob("*.conf")):
        try:
            vmid = int(cfg.stem)
        except ValueError:
            continue
        if not 100 <= vmid <= 9999:
            continue
        try:
            mac = _extract_net0_mac(cfg.read_text())
        except OSError:
            continue
        if not mac:
            continue
        ipv6 = f"{prefix}{vmid:x}"
        lines.append(f"{mac},[{ipv6}],vm{vmid},infinite\n")

    out_path = Path("/etc/dnsmasq-vm-pins.hosts")
    tmp = out_path.with_suffix(".tmp")
    try:
        tmp.write_text("".join(lines))
        tmp.replace(out_path)
    except OSError as e:
        logger.error(f"regen-pins: write failed: {e}")
        return False

    # Clean up legacy file from when pins lived in /etc/dnsmasq.d/ (where
    # SIGHUP didn't re-read them, the bug we're fixing).
    legacy = Path("/etc/dnsmasq.d/vm-pins.conf")
    if legacy.exists():
        try:
            legacy.unlink()
        except OSError:
            pass

    try:
        subprocess.run(["systemctl", "reload", "dnsmasq"], check=False,
                       capture_output=True, timeout=10)
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        logger.warning(f"regen-pins: dnsmasq reload failed: {e}")

    logger.info(f"regen-pins: wrote {len(lines)} pins to {out_path}")
    return True

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

    # regen-pins: rewrite vm-pins.conf and reload dnsmasq
    if len(sys.argv) >= 2 and sys.argv[1] == "regen-pins":
        sys.exit(0 if regen_dhcp_pins() else 1)

    # node-boot: stamp lastNodeBootAt, sync all statuses, regen pins
    if len(sys.argv) >= 2 and sys.argv[1] == "node-boot":
        logger.info("node-boot: updating lastNodeBootAt and syncing VM statuses")
        update_proxmox_node_last_boot_at()
        sync_all_statuses()
        regen_dhcp_pins()
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
            regen_dhcp_pins()
            logger.info("Sync complete.")
            return

        if phase == "post-stop":
            logger.info(f"Hook: VM {triggered_vmid} post-stop")
            update_status_in_firestore(triggered_vmid, "stopped")
            regen_dhcp_pins()
            logger.info("Sync complete.")
            return

        # Other phases (pre-start, pre-stop): nothing to do
        return

    # Manual mode (no args): sync all statuses + regen pins
    logger.info("Manual sync: reconciling all VM statuses")
    sync_all_statuses()
    regen_dhcp_pins()
    logger.info("Sync complete.")

if __name__ == "__main__":
    main()

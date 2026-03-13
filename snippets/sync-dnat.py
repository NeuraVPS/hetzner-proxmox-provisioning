#!/usr/bin/env python3
"""
sync-dnat: Sync VM status and IPv6 to Firestore.

Commands:
  Hook (Proxmox):
    sync-dnat.py <VMID> post-start  — On VM start: set status running, wait for IP, sync IPv6 to Firestore.
    sync-dnat.py <VMID> post-stop    — On VM stop: set status stopped in Firestore.
  Manual sync:
    sync-dnat.py                     — No args: sync all local VMs' status and IPv6 to Firestore.
  Node boot:
    sync-dnat.py node-boot          — At node boot: set proxmox_nodes/<hostname>.lastNodeBootAt, then sync all VM statuses (and IPv6 for running VMs).
"""
import subprocess
import json
import ipaddress
import re
import sys
import time
import logging
from pathlib import Path
import os

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

# Constants
BRIDGE_NET = "10.0.0.0/16"
BASE_PORT_RDP = 20000
BASE_PORT_SAMBA = 10000
# Get hostname reliably - use subprocess.run to avoid capturing stderr
try:
    result = subprocess.run(["hostname"], capture_output=True, text=True, check=True)
    NODE_NAME = result.stdout.strip()
except (subprocess.CalledProcessError, FileNotFoundError):
    # Fallback to os.uname if hostname command fails
    NODE_NAME = os.uname().nodename

# Set up logging
LOG_FILE = Path("/var/log/sync-dnat.log")
MAX_LOG_SIZE = 1024 * 1024  # 1 MB

def clean_log_if_needed():
    """Delete log file if it exceeds MAX_LOG_SIZE."""
    if LOG_FILE.exists():
        size = LOG_FILE.stat().st_size
        if size > MAX_LOG_SIZE:
            LOG_FILE.unlink()
            return True  # Indicates cleanup happened
    return False

# Clean log before setting up logging
was_cleaned = clean_log_if_needed()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, mode='a'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Log cleanup if it happened
if was_cleaned:
    logger.info("Log file deleted due to size limit (> 1 MB)")

# ---------------------------------------------------------------
# Firebase initialization
# ---------------------------------------------------------------
def initialize_firebase():
    """Initialize Firebase Admin SDK if credentials file is available."""
    if not FIREBASE_AVAILABLE:
        logger.debug("Firebase Admin SDK not available (package not installed)")
        return False
    
    # Check if already initialized
    if firebase_admin._apps:
        return True
    
    # Get credentials file path from environment or use default
    creds_file = os.environ.get("FIREBASE_CREDENTIALS_FILE", "/etc/firebase-credentials.json")
    creds_path = Path(creds_file)
    
    if not creds_path.exists():
        logger.debug(f"Firebase credentials file not found at {creds_file}, skipping Firestore sync")
        return False
    
    if not creds_path.is_file():
        logger.warning(f"Firebase credentials path {creds_file} is not a file, skipping Firestore sync")
        return False
    
    try:
        cred = credentials.Certificate(str(creds_path))
        firebase_admin.initialize_app(cred)
        logger.info(f"Firebase Admin SDK initialized with credentials from {creds_file}")
        return True
    except Exception as e:
        logger.warning(f"Failed to initialize Firebase Admin SDK: {e}, continuing without Firestore sync")
        return False

# Firebase initialization flag (lazy initialization - only when needed)
FIREBASE_INITIALIZED = False

def ensure_firebase_initialized():
    """Lazy initialization of Firebase - only when actually needed."""
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
        # Get parent process command
        cmdline = open(f"/proc/{ppid}/cmdline").read().replace("\x00", " ")
        # Get executable name
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


def run_ip6tables(cmd, check=True, max_retries=3):
    """Run an ip6tables command with retries on non-zero exit (e.g. exit 4 = resource/lock).
    Returns subprocess.CompletedProcess. On final failure when check=True, raises CalledProcessError.
    """
    last_result = None
    for attempt in range(max_retries):
        last_result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if last_result.returncode == 0:
            return last_result
        if check and attempt < max_retries - 1:
            time.sleep(0.3)
            continue
        if check:
            raise subprocess.CalledProcessError(
                last_result.returncode, cmd, last_result.stdout, last_result.stderr
            )
        return last_result
    return last_result

# ---------------------------------------------------------------
# Proxmox and VM info
# ---------------------------------------------------------------

def get_running_vms():
    result = run(["qm", "list"])
    vms = []
    for line in result.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 3 and fields[2] == "running":
            vms.append(int(fields[0]))
    return vms

def get_all_vms():
    """Return dict mapping vmid -> "running" or "stopped" for all VMs."""
    result = run(["qm", "list"])
    vm_status = {}
    for line in result.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 3:
            vmid = int(fields[0])
            status = fields[2]
            vm_status[vmid] = "running" if status == "running" else "stopped"
    return vm_status

def get_vm_info(vmid):
    try:
        config = run(["qm", "config", str(vmid)])
        ostype = "linux"
        for line in config.splitlines():
            if line.startswith("ostype:"):
                ostype = line.split(":")[1].strip()
        interfaces = json.loads(run(["qm", "guest", "cmd", str(vmid), "network-get-interfaces"]))
        
        ipv4 = None
        ipv6 = None
        
        for iface in interfaces:
            # Skip loopback interfaces
            if 'loopback' in iface.get('name', '').lower():
                continue
            
            ip_addresses = iface.get("ip-addresses", [])
            for addr in ip_addresses:
                ip = addr.get("ip-address")
                if not ip:
                    continue
                
                # Check IPv4 addresses
                try:
                    ip_obj = ipaddress.ip_address(ip)
                    if ip_obj.version == 4:
                        if ip_obj in ipaddress.ip_network(BRIDGE_NET):
                            ipv4 = ip
                    elif ip_obj.version == 6:
                        # Extract IPv6 address, filtering out non-public addresses
                        addr_str = ip
                        
                        # Skip link-local addresses (fe80::) and loopback (::1)
                        # Also skip addresses with % (zone identifiers for link-local)
                        if addr_str.startswith('fe80::') or addr_str.startswith('::1') or '%' in addr_str:
                            continue
                        
                        # Skip unique local addresses (fc00::/7)
                        if addr_str.startswith('fc') or addr_str.startswith('fd'):
                            continue
                        
                        # Found a global IPv6 address
                        if not ipv6:
                            ipv6 = addr_str
                except ValueError:
                    # Invalid IP address, skip
                    continue
            
            # If we found both IPv4 and IPv6, we can return early
            if ipv4 and ipv6:
                break
        
        # Return info if we found at least IPv4 (required for NAT rules)
        if ipv4:
            return {"ip": ipv4, "ipv6": ipv6, "ostype": ostype}
        
    except subprocess.CalledProcessError as e:
        if e.returncode == 2:
            # Exit code 2 means no guest agent, exit immediately
            raise Exception(f"VM {vmid} has no guest agent installed")
        elif e.returncode == 255:
            # Exit code 255 means guest agent not ready yet, keep waiting
            logger.debug(f"Guest agent not ready for VM {vmid} (exit code 255)")
            return None
        else:
            logger.warning(f"Failed to get info for VM {vmid}: {e}")
            return None
    except json.JSONDecodeError as e:
        # Handle JSON decode errors
        logger.warning(f"Failed to parse network interfaces for VM {vmid}: {e}")
        return None
    except Exception as e:
        # Only catch other exceptions if it's not our "no guest agent" exception
        if "no guest agent" not in str(e).lower():
            logger.warning(f"Failed to get info for VM {vmid}: {e}")
            return None
        raise  # Re-raise "no guest agent" exception
    return None

def wait_for_vm_ip(vmid, max_wait=600, initial_wait=1):
    """Wait for a VM to get an IP, with exponential backoff."""
    start = time.time()
    wait_time = initial_wait
    
    while time.time() - start < max_wait:
        try:
            info = get_vm_info(vmid)
            if info:
                return info
            logger.info(f"VM {vmid} has no IP yet, waiting {wait_time}s...")
        except Exception as e:
            # If VM has no guest agent, exit immediately
            logger.error(str(e))
            raise  # Re-raise to exit immediately
        
        time.sleep(wait_time)
        wait_time = min(wait_time * 2, 30)  # exponential backoff, max 30s
    
    logger.error(f"VM {vmid} did not get an IP within {max_wait}s")
    return None

# ---------------------------------------------------------------
# Firestore sync
# ---------------------------------------------------------------
def sync_ipv6_to_firestore(vm_infos):
    """Sync IPv6 addresses to Firestore servers collection.
    
    Args:
        vm_infos: Dict mapping vmid to VM info (ip, ipv6, ostype)
    """
    if not ensure_firebase_initialized():
        return
    
    if not vm_infos:
        logger.debug("No VM info to sync to Firestore")
        return
    
    try:
        db = firestore.client()
        
        for vmid, info in vm_infos.items():
            ipv6_address = info.get("ipv6")
            
            try:
                # Query for server document with matching proxmoxId and nodeId
                servers_ref = db.collection('servers')
                # Use new filter API if available to avoid deprecation warnings
                if FIELD_FILTER_AVAILABLE:
                    query = servers_ref.where(filter=FieldFilter('proxmoxId', '==', vmid)).where(filter=FieldFilter('nodeId', '==', NODE_NAME))
                else:
                    query = servers_ref.where('proxmoxId', '==', vmid).where('nodeId', '==', NODE_NAME)
                docs = list(query.stream())
                
                if not docs:
                    logger.debug(f"No Firestore document found for VM {vmid} (proxmoxId={vmid})")
                    continue
                
                if len(docs) > 1:
                    logger.warning(f"Multiple Firestore documents found for VM {vmid} (proxmoxId={vmid}), updating all")
                
                # Update each matching document only if IPv6 has changed
                for doc_snapshot in docs:
                    server_id = doc_snapshot.id
                    doc_data = doc_snapshot.to_dict()
                    current_ipv6 = doc_data.get('ipv6')
                    
                    # Normalize values for comparison: convert to string, strip whitespace, treat empty/None as None
                    if current_ipv6:
                        current_str = str(current_ipv6).strip()
                        current_normalized = current_str if current_str else None
                    else:
                        current_normalized = None
                    
                    new_ipv6_raw = ipv6_address if ipv6_address else None
                    if new_ipv6_raw:
                        new_str = str(new_ipv6_raw).strip()
                        new_normalized = new_str if new_str else None
                    else:
                        new_normalized = None
                    
                    # Check if IPv6 address has changed (both normalized to None or same string value)
                    if current_normalized == new_normalized:
                        continue
                    
                    # IPv6 has changed, update it
                    update_data = {'ipv6': new_ipv6_raw}
                    server_doc_ref = db.collection('servers').document(server_id)
                    server_doc_ref.update(update_data)
                    logger.info(f"Updated Firestore server {server_id} (VM {vmid}): ipv6 changed from {current_normalized} to {new_normalized}")
                    
            except Exception as e:
                logger.warning(f"Failed to sync IPv6 for VM {vmid} to Firestore: {e}")
                continue
    
    except Exception as e:
        logger.error(f"Failed to sync IPv6 to Firestore: {e}")

def get_firestore_status(vmid):
    """Get current status from Firestore for a VM.
    
    Args:
        vmid: VM ID (proxmoxId)
    
    Returns:
        Current status string ("running" or "stopped") or None if not found/not set
    """
    if not ensure_firebase_initialized():
        return None
    
    try:
        db = firestore.client()
        
        # Query for server document with matching proxmoxId and nodeId
        servers_ref = db.collection('servers')
        # Use new filter API if available to avoid deprecation warnings
        if FIELD_FILTER_AVAILABLE:
            query = servers_ref.where(filter=FieldFilter('proxmoxId', '==', vmid)).where(filter=FieldFilter('nodeId', '==', NODE_NAME))
        else:
            query = servers_ref.where('proxmoxId', '==', vmid).where('nodeId', '==', NODE_NAME)
        docs = list(query.stream())
        
        if not docs:
            logger.debug(f"No Firestore document found for VM {vmid} (proxmoxId={vmid}, nodeId={NODE_NAME})")
            return None
        
        if len(docs) > 1:
            logger.warning(f"Multiple Firestore documents found for VM {vmid} (proxmoxId={vmid}, nodeId={NODE_NAME}), using first")
        
        # Get status from first matching document
        doc_data = docs[0].to_dict()
        return doc_data.get('status')
        
    except Exception as e:
        logger.warning(f"Failed to get status for VM {vmid} from Firestore: {e}")
        return None

def update_status_in_firestore(vmid, status):
    """Update status and lastStatusUpdate timestamp in Firestore.
    
    Args:
        vmid: VM ID (proxmoxId)
        status: Status string ("running" or "stopped")
    """
    if not ensure_firebase_initialized():
        return
    
    try:
        db = firestore.client()
        
        # Query for server document with matching proxmoxId and nodeId
        servers_ref = db.collection('servers')
        # Use new filter API if available to avoid deprecation warnings
        if FIELD_FILTER_AVAILABLE:
            query = servers_ref.where(filter=FieldFilter('proxmoxId', '==', vmid)).where(filter=FieldFilter('nodeId', '==', NODE_NAME))
        else:
            query = servers_ref.where('proxmoxId', '==', vmid).where('nodeId', '==', NODE_NAME)
        docs = list(query.stream())
        
        if not docs:
            logger.debug(f"No Firestore document found for VM {vmid} (proxmoxId={vmid}, nodeId={NODE_NAME}) to update status")
            return
        
        if len(docs) > 1:
            logger.warning(f"Multiple Firestore documents found for VM {vmid} (proxmoxId={vmid}, nodeId={NODE_NAME}), updating all")
        
        # Update each matching document
        for doc_snapshot in docs:
            server_id = doc_snapshot.id
            update_data = {
                'status': status,
                'lastStatusUpdate': firestore.SERVER_TIMESTAMP
            }
            
            # Get document reference and update
            server_doc_ref = db.collection('servers').document(server_id)
            server_doc_ref.update(update_data)
            logger.info(f"Updated Firestore server {server_id} (VM {vmid}): status set to {status}")
            
    except Exception as e:
        logger.warning(f"Failed to update status for VM {vmid} in Firestore: {e}")


def update_proxmox_node_last_boot_at():
    """Update proxmox_nodes/<hostname>.lastNodeBootAt to SERVER_TIMESTAMP. Document id = NODE_NAME (hostname). If doc does not exist, log and return."""
    if not ensure_firebase_initialized():
        logger.debug("Firebase not initialized, skipping lastNodeBootAt update")
        return
    try:
        db = firestore.client()
        doc_ref = db.collection("proxmox_nodes").document(NODE_NAME)
        doc_ref.update({"lastNodeBootAt": firestore.SERVER_TIMESTAMP})
        logger.info(f"Updated proxmox_nodes/{NODE_NAME} lastNodeBootAt")
    except Exception as e:
        logger.warning(f"Failed to update lastNodeBootAt for proxmox_nodes/{NODE_NAME}: {e}")


# ---------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------
def main():
    # Handle arguments from Proxmox hook
    triggered_vmid = None
    phase = None
    hook_mode = False

    if is_running_under_backup():
        return

    # Node boot: update lastNodeBootAt then run default (manual) sync
    if len(sys.argv) >= 2 and sys.argv[1] == "node-boot":
        logger.info("node-boot: updating lastNodeBootAt and syncing VM statuses")
        update_proxmox_node_last_boot_at()
        # Fall through to manual mode below (hook_mode remains False)

    if len(sys.argv) >= 3:
        triggered_vmid = int(sys.argv[1])
        phase = sys.argv[2]
        hook_mode = True
        
        # Exit immediately for anything other than post-stop and post-start
        if phase not in ["post-stop", "post-start"]:
            #logger.info(f"Phase {phase} - exiting immediately")
            return
        
        logger.info(f"Hook triggered: VM {triggered_vmid}, phase {phase}")
    
    # Handle post-stop hook: update status only
    if hook_mode and phase == "post-stop":
        update_status_in_firestore(triggered_vmid, "stopped")
        logger.info("Sync complete.")
        return

    vm_infos = {}
    vms_for_ipv6_sync = {}  # Track VMs for IPv6 sync (manual mode only)
    
    # In hook mode, only process the triggered VM
    if hook_mode:
        if phase == "post-start":
            # Push status to running ASAP (VM is up from Proxmox's perspective)
            update_status_in_firestore(triggered_vmid, "running")
            # Then wait for the VM to get an IP via qemu-agent and sync when available
            logger.info(f"Waiting for triggered VM {triggered_vmid} to get IP...")
            try:
                info = wait_for_vm_ip(triggered_vmid)
                if info:
                    vm_infos[triggered_vmid] = info
                    sync_ipv6_to_firestore({triggered_vmid: info})
                else:
                    logger.warning(f"VM {triggered_vmid} did not get an IP yet, status already set to running")
            except Exception as e:
                if "no guest agent" in str(e).lower():
                    logger.error(f"VM {triggered_vmid} has no guest agent - status already set to running")
                    return
                raise
    else:
        # Manual mode: process all VMs (running and stopped)
        all_vm_status = get_all_vms()
        logger.info(f"All VMs: {list(all_vm_status.keys())}")
        
        for vmid, actual_status in all_vm_status.items():
            # Get current status from Firestore
            firestore_status = get_firestore_status(vmid)
            
            # Only update status if it has changed
            if firestore_status != actual_status:
                logger.info(f"VM {vmid} status changed from {firestore_status} to {actual_status}, updating Firestore")
                update_status_in_firestore(vmid, actual_status)
            
            # For running VMs: get VM info and prepare for IPv6 sync
            if actual_status == "running":
                try:
                    info = get_vm_info(vmid)
                    if info:
                        vm_infos[vmid] = info
                        # Track for IPv6 sync if IPv6 is available
                        if info.get("ipv6"):
                            vms_for_ipv6_sync[vmid] = info
                    else:
                        logger.warning(f"No internal IP for VM {vmid}")
                except Exception as e:
                    if "no guest agent" in str(e).lower():
                        logger.warning(f"VM {vmid} has no guest agent - skipping")
                    else:
                        raise

    # Sync IPv6 addresses to Firestore (only in manual mode, only for running VMs with IPv6)
    if not hook_mode and vms_for_ipv6_sync:
        sync_ipv6_to_firestore(vms_for_ipv6_sync)

    logger.info("Sync complete.")

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
import subprocess
import json
import ipaddress
import re
import shlex
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

def normalize_rule(rule: str) -> str:
    """Normalize rule text for reliable comparison (ignore order, masks, -m tcp)."""
    rule = re.sub(r"/32", "", rule)  # remove explicit /32 masks
    rule = rule.replace("-m tcp", "")  # remove redundant matcher
    rule = re.sub(r"\s+", " ", rule.strip())  # normalize spaces
    return rule

# ---------------------------------------------------------------
# Proxmox and VM info
# ---------------------------------------------------------------

def get_default_wan_if():
    route = run(["ip", "route", "show", "default"])
    for line in route.splitlines():
        parts = line.split()
        if "dev" in parts:
            return parts[parts.index("dev") + 1]

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
# iptables management
# ---------------------------------------------------------------

def parse_iptables_rules(vmid_filter=None):
    """Return dict of tables with only our managed rules.
    
    Args:
        vmid_filter: Optional VM ID to filter rules. If provided, only rules
                    with comments containing -vmid-{vmid_filter} will be included.
                    If None, all managed rules are included.
    """
    rules = {"nat": set(), "filter": set()}
    current_table = None
    for line in run(["iptables-save"]).splitlines():
        if line.startswith("*"):
            current_table = line[1:]
        elif line.startswith("-A") and current_table in rules:
            if "ssh-vmid-" in line or "rdp-vmid-" in line or "samba-vmid-" in line:
                # If filtering by vmid, check that the rule belongs to this VM
                if vmid_filter is not None:
                    if f"-vmid-{vmid_filter}" not in line:
                        continue
                rules[current_table].add(normalize_rule(line))
    return rules

def build_expected_rules(vm_infos, wan_if):
    """Build expected iptables rules.
    
    Args:
        vm_infos: Dict mapping vmid to VM info (ip, ostype)
        wan_if: WAN interface name
    
    Returns:
        Tuple of (expected_nat, expected_filter) where:
        - expected_nat: set of normalized NAT rule strings
        - expected_filter: empty dict (no FORWARD rules needed - group rules handle it)
    """
    expected_nat = set()
    expected_filter = {}  # Empty - Proxmox firewall group rules handle FORWARD filtering
    
    for vmid, info in vm_infos.items():
        vm_ip = info["ip"]
        
        # SSH/RDP rule (20000+vmid -> 22/3389)
        to_port = 3389 if info["ostype"].startswith("win") else 22
        comment = f"{'rdp' if to_port == 3389 else 'ssh'}-vmid-{vmid}"
        host_port = BASE_PORT_RDP + vmid

        nat_rule = normalize_rule(
            f"-A PREROUTING -i {wan_if} -p tcp --dport {host_port} "
            f"-m comment --comment {comment} -j DNAT --to-destination {vm_ip}:{to_port}"
        )
        expected_nat.add(nat_rule)
        
        # Samba rule (10000+vmid -> 445) - only for Windows VMs
        if info["ostype"].startswith("win"):
            samba_comment = f"samba-vmid-{vmid}"
            samba_host_port = BASE_PORT_SAMBA + vmid
            samba_nat_rule = normalize_rule(
                f"-A PREROUTING -i {wan_if} -p tcp --dport {samba_host_port} "
                f"-m comment --comment {samba_comment} -j DNAT --to-destination {vm_ip}:445"
            )
            expected_nat.add(samba_nat_rule)
    
    return expected_nat, expected_filter

def delete_rule_by_comment(comment, table):
    """Delete all rules in the given table that have this comment."""
    output = run(["iptables", "-t", table, "-S"])
    for line in output.splitlines():
        if f'--comment {comment}' in line:
            # example: -A PREROUTING -i eno1 ...
            args = shlex.split(line)
            args[0] = "-D"  # replace -A with -D
            logger.info(f"DEL: {' '.join(args)}")
            subprocess.run(["iptables", "-t", table] + args, check=False)

def sync_iptables_rules(expected, actual, table):
    """Sync iptables rules, adds missing ones, deletes obsolete ones.
    
    Args:
        expected: For NAT table, a set of normalized rule strings.
                 For FILTER table, an empty dict (no FORWARD rules needed).
        actual: Set of normalized rule strings for the table
        table: Table name ('nat' or 'filter')
    """
    if table == "filter" and isinstance(expected, dict):
        # FORWARD rules: expected is always empty (group rules handle it)
        # Delete any old FORWARD rules that shouldn't exist
        if actual:
            logger.info(f"Cleaning up {len(actual)} old FORWARD rules (no longer needed)")
            for rule in sorted(actual):
                m = re.search(r"--comment\s+(\S+)", rule)
                if m:
                    comment = m.group(1)
                    delete_rule_by_comment(comment, table)
    else:
        # NAT rules
        expected_set = expected if isinstance(expected, set) else set(expected.keys())
        to_add = expected_set - actual
        to_del = actual - expected_set

        # delete obsolete rules by comment
        for rule in sorted(to_del):
            m = re.search(r"--comment\s+(\S+)", rule)
            if m:
                comment = m.group(1)
                delete_rule_by_comment(comment, table)

        # add missing rules
        for rule in sorted(to_add):
            logger.info(f"ADD: {rule}")
            subprocess.run(["iptables", "-t", table] + shlex.split(rule), check=True)

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

# ---------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------

def main():
    # Handle arguments from Proxmox hook
    triggered_vmid = None
    phase = None
    hook_mode = False

    if is_running_under_backup():
        #logger.info(f"Detected vzdump/backup context (parent process). Skipping execution for VM {triggered_vmid}.")
        return
    
    if len(sys.argv) >= 3:
        triggered_vmid = int(sys.argv[1])
        phase = sys.argv[2]
        hook_mode = True
        
        # Exit immediately for anything other than post-stop and post-start
        if phase not in ["post-stop", "post-start"]:
            #logger.info(f"Phase {phase} - exiting immediately")
            return
        
        logger.info(f"Hook triggered: VM {triggered_vmid}, phase {phase}")
    
    # Handle post-stop hook: remove DNAT rules and update status
    if hook_mode and phase == "post-stop":
        logger.info(f"Removing DNAT rules for VM {triggered_vmid}")
        actual = parse_iptables_rules(vmid_filter=triggered_vmid)
        # Delete all rules for this VM
        for rule in sorted(actual["nat"]):
            m = re.search(r"--comment\s+(\S+)", rule)
            if m:
                comment = m.group(1)
                delete_rule_by_comment(comment, "nat")
        # Update status to stopped
        update_status_in_firestore(triggered_vmid, "stopped")
        logger.info("Sync complete.")
        return
    
    # Get WAN interface (needed for building rules)
    wan_if = get_default_wan_if()
    logger.info(f"WAN interface: {wan_if}")

    vm_infos = {}
    vms_for_ipv6_sync = {}  # Track VMs for IPv6 sync (manual mode only)
    
    # In hook mode, only process the triggered VM
    if hook_mode:
        if phase == "post-start":
            # For post-start, wait for the triggered VM to get an IP
            logger.info(f"Waiting for triggered VM {triggered_vmid} to get IP...")
            try:
                info = wait_for_vm_ip(triggered_vmid)
                if info:
                    vm_infos[triggered_vmid] = info
                    # Sync IPv6 first (before status update, as functions triggered by status change need the updated IP)
                    sync_ipv6_to_firestore({triggered_vmid: info})
                    # Update status to running when VM receives an IP
                    update_status_in_firestore(triggered_vmid, "running")
                else:
                    logger.warning(f"VM {triggered_vmid} did not get an IP, skipping")
            except Exception as e:
                if "no guest agent" in str(e).lower():
                    logger.error(f"VM {triggered_vmid} has no guest agent - exiting immediately")
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
            
            # For running VMs: get VM info and prepare for IPv6 sync and DNAT rules
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
            # For stopped VMs: DNAT rules will be automatically removed since they're not in vm_infos

    # Build expected rules (only NAT rules - group rules handle FORWARD filtering)
    expected_nat, expected_filter = build_expected_rules(vm_infos, wan_if)
    
    # Get actual rules (filtered for hook mode)
    if hook_mode:
        actual = parse_iptables_rules(vmid_filter=triggered_vmid)
    else:
        actual = parse_iptables_rules()

    # Sync iptables rules
    sync_iptables_rules(expected_nat, actual["nat"], "nat")
    sync_iptables_rules(expected_filter, actual["filter"], "filter")

    # Sync IPv6 addresses to Firestore (only in manual mode, only for running VMs with IPv6)
    if not hook_mode and vms_for_ipv6_sync:
        sync_ipv6_to_firestore(vms_for_ipv6_sync)

    logger.info("Sync complete.")

if __name__ == "__main__":
    main()

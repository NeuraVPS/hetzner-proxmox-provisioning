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
NODES_CONF = Path("/etc/sync-dnat/nodes.conf")
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
# BASE node detection and config
# ---------------------------------------------------------------

def is_base_node():
    """Return True when hostname contains 'BASE' (case-insensitive)."""
    return "BASE" in NODE_NAME.upper()

def get_nodes_config():
    """Read nodes.conf. Returns dict hostname -> public_ipv4, or None if missing/invalid."""
    if not NODES_CONF.exists() or not NODES_CONF.is_file():
        return None
    nodes = {}
    try:
        for line in NODES_CONF.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            if len(parts) >= 2:
                nodes[parts[0]] = parts[1]
    except Exception as e:
        logger.warning(f"Failed to read {NODES_CONF}: {e}")
        return None
    return nodes

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

def parse_iptables_rules(vmid_filter=None, base_mode=False):
    """Return dict of tables with only our managed rules.

    Args:
        vmid_filter: Optional VM ID to filter rules. If provided, only rules
                    with comments containing -vmid-{vmid_filter} will be included.
                    If None, all managed rules are included.
        base_mode: If True, only match rules with base-rdp-vmid- or base-samba-vmid-.
    """
    rules = {"nat": set(), "filter": set()}
    current_table = None
    for line in run(["iptables-save"]).splitlines():
        if line.startswith("*"):
            current_table = line[1:]
        elif line.startswith("-A") and current_table in rules:
            if base_mode:
                match = "base-rdp-vmid-" in line or "base-samba-vmid-" in line
            else:
                match = "ssh-vmid-" in line or "rdp-vmid-" in line or "samba-vmid-" in line
            if match:
                if vmid_filter is not None and not base_mode:
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

def get_base_public_ip():
    """Return BASE's public IPv4 (for Jool BIB). Must run on BASE."""
    out = run(["ip", "-4", "route", "get", "8.8.8.8"])
    m = re.search(r"src\s+(\S+)", out)
    return m.group(1) if m else None

def _jool_bib_add(vm_ipv6, vm_port, base_ip, base_port, protocol="--tcp"):
    """Add one Jool BIB entry. Returns True on success."""
    try:
        subprocess.run(
            ["jool", "bib", "add", f"{vm_ipv6}#{vm_port}", f"{base_ip}#{base_port}", protocol],
            capture_output=True, text=True, check=True, timeout=10
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        logger.warning(f"Jool BIB add failed: {e}")
        return False

def _jool_bib_remove(base_ip, base_port, protocol="--tcp"):
    """Remove one Jool BIB entry by BASE side port. Returns True on success."""
    try:
        subprocess.run(
            ["jool", "bib", "remove", f"{base_ip}#{base_port}", protocol],
            capture_output=True, text=True, check=True, timeout=10
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        logger.warning(f"Jool BIB remove failed: {e}")
        return False

def _delete_rule_by_comment_ip6(comment):
    """Delete ip6tables FORWARD rules that have this comment. Deletes by line number high-to-low."""
    try:
        output = run(["ip6tables", "-L", "FORWARD", "-n", "-v", "--line-numbers"])
        line_nums = []
        for line in output.splitlines():
            if comment in line:
                parts = line.strip().split()
                if parts and parts[0].isdigit():
                    line_nums.append(int(parts[0]))
        for num in sorted(line_nums, reverse=True):
            subprocess.run(["ip6tables", "-D", "FORWARD", str(num)], capture_output=True, check=False)
    except Exception as e:
        logger.warning(f"ip6tables delete by comment failed: {e}")

def apply_base_create_nat64(node_hostname, vmid, vm_ipv6, ostype):
    """Add BASE NAT64 BIB entries and ip6tables FORWARD for (node_hostname, vmid, vm_ipv6). Must run on BASE."""
    if not is_base_node():
        logger.error("apply_base_create_nat64 must run on BASE")
        return False
    base_ip = get_base_public_ip()
    if not base_ip:
        logger.error("Could not determine BASE public IPv4")
        return False
    rdp_port = BASE_PORT_RDP + vmid
    samba_port = BASE_PORT_SAMBA + vmid
    to_port_rdp = 3389 if ostype.startswith("win") else 22
    fwd_comment = f"nat64-fwd-vmid-{vmid}"
    ok = True
    if _jool_bib_add(vm_ipv6, to_port_rdp, base_ip, rdp_port):
        logger.info(f"ADD: jool bib {base_ip}#{rdp_port} -> {vm_ipv6}#{to_port_rdp} (vmid-{vmid})")
    else:
        ok = False
    try:
        subprocess.run(
            ["ip6tables", "-A", "FORWARD", "-p", "tcp", "-d", vm_ipv6,
             "-m", "comment", "--comment", fwd_comment, "-j", "ACCEPT"],
            capture_output=True, check=True
        )
        subprocess.run(
            ["ip6tables", "-A", "FORWARD", "-p", "tcp", "-s", vm_ipv6,
             "-m", "comment", "--comment", fwd_comment, "-j", "ACCEPT"],
            capture_output=True, check=True
        )
        logger.info(f"ADD: ip6tables FORWARD for {vm_ipv6} (vmid-{vmid})")
    except subprocess.CalledProcessError as e:
        logger.warning(f"ip6tables FORWARD add failed: {e}")
    if ostype.startswith("win"):
        if _jool_bib_add(vm_ipv6, 445, base_ip, samba_port):
            logger.info(f"ADD: jool bib {base_ip}#{samba_port} -> {vm_ipv6}#445 (vmid-{vmid})")
        else:
            ok = False
    return ok

def apply_base_delete_nat64(node_hostname, vmid):
    """Remove BASE NAT64 BIB entries and ip6tables FORWARD for (node_hostname, vmid). Must run on BASE."""
    if not is_base_node():
        logger.error("apply_base_delete_nat64 must run on BASE")
        return False
    base_ip = get_base_public_ip()
    if not base_ip:
        logger.error("Could not determine BASE public IPv4")
        return False
    rdp_port = BASE_PORT_RDP + vmid
    samba_port = BASE_PORT_SAMBA + vmid
    _jool_bib_remove(base_ip, rdp_port)
    _jool_bib_remove(base_ip, samba_port)
    _delete_rule_by_comment_ip6(f"nat64-fwd-vmid-{vmid}")
    logger.info(f"Removed BASE NAT64 rules for {node_hostname} vmid {vmid}")
    return True

def sync_base_for_node(node_hostname):
    """Sync BASE NAT64 BIB for one node. No-op: BIB is updated by node notifications on VM start/stop."""
    if not is_base_node():
        logger.error("sync_base_for_node must run on BASE")
        return False
    logger.info("NAT64: sync via node notification only (no-op)")
    return True

def sync_base_all():
    """Sync all BASE forwarding rules for all nodes. Must run on BASE."""
    if not is_base_node():
        logger.error("sync_base_all must run on BASE")
        return False
    nodes = get_nodes_config()
    if not nodes:
        logger.error("nodes.conf missing or empty")
        return False
    for node_hostname in nodes:
        sync_base_for_node(node_hostname)


def sync_base_restore_from_firestore():
    """Repopulate BASE BIB and ip6tables from Firestore (for use after BASE reboot).
    Queries all servers with ipv6 set and calls apply_base_create_nat64 for each.
    Must run on BASE. Requires Firebase credentials.
    """
    if not is_base_node():
        logger.error("sync_base_restore_from_firestore must run on BASE")
        return False
    if not ensure_firebase_initialized():
        logger.error("Firebase not initialized (credentials missing); cannot restore BIB from Firestore")
        return False
    try:
        db = firestore.client()
        servers_ref = db.collection("servers")
        count = 0
        for doc in servers_ref.stream():
            data = doc.to_dict() or {}
            ipv6 = (data.get("ipv6") or "").strip() or None
            if not ipv6:
                continue
            node_id = data.get("nodeId")
            vmid_raw = data.get("proxmoxId")
            if not node_id or vmid_raw is None:
                continue
            try:
                vmid = int(vmid_raw)
            except (TypeError, ValueError):
                continue
            ostype = "win"  # Assume Windows (RDP 3389, Samba 445); change when Linux servers are needed
            if apply_base_create_nat64(node_id, vmid, ipv6, ostype):
                count += 1
        logger.info(f"NAT64 restore from Firestore: applied BIB for {count} server(s)")
        return True
    except Exception as e:
        logger.warning(f"NAT64 restore from Firestore failed: {e}")
        return False


def delete_rule_by_comment(comment, table):
    """Delete all rules in the given table that have this comment."""
    output = run(["iptables", "-t", table, "-S"])
    for line in output.splitlines():
        if f'--comment {comment}' in line:
            args = shlex.split(line)
            args[0] = "-D"
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

def handle_update_base():
    """Handle update_base invocations. Returns True if we handled it and should exit."""
    if len(sys.argv) < 2 or sys.argv[1] != "update_base":
        return False
    if is_running_under_backup():
        return True
    if not is_base_node():
        logger.error("update_base must run on BASE node")
        return True
    # update_base create NODE_HOSTNAME vmid VM_IPV6 OSTYPE
    if len(sys.argv) >= 6 and sys.argv[2] == "create":
        node_hostname = sys.argv[3]
        try:
            vmid = int(sys.argv[4])
        except ValueError:
            logger.error(f"Invalid vmid: {sys.argv[4]}")
            return True
        vm_ipv6 = sys.argv[5]
        ostype = sys.argv[6] if len(sys.argv) > 6 else "linux"
        apply_base_create_nat64(node_hostname, vmid, vm_ipv6, ostype)
        return True
    # update_base delete NODE_HOSTNAME vmid
    if len(sys.argv) >= 5 and sys.argv[2] == "delete":
        node_hostname = sys.argv[3]
        try:
            vmid = int(sys.argv[4])
        except ValueError:
            logger.error(f"Invalid vmid: {sys.argv[4]}")
            return True
        apply_base_delete_nat64(node_hostname, vmid)
        return True
    # update_base restore — repopulate BIB from Firestore (after BASE reboot)
    if len(sys.argv) == 3 and sys.argv[2] == "restore":
        sync_base_restore_from_firestore()
        return True
    # update_base NODE_HOSTNAME (sync one node)
    if len(sys.argv) >= 3:
        node_hostname = sys.argv[2]
        sync_base_for_node(node_hostname)
        return True
    # update_base (sync all nodes)
    sync_base_all()
    return True

def main():
    # Handle arguments from Proxmox hook
    triggered_vmid = None
    phase = None
    hook_mode = False

    if is_running_under_backup():
        return

    # Handle update_base invocations first
    if len(sys.argv) >= 2 and sys.argv[1] == "update_base":
        handle_update_base()
        logger.info("Sync complete.")
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

    # Sync iptables rules (node-side NAT: NODE -> VM, kept for compatibility)
    sync_iptables_rules(expected_nat, actual["nat"], "nat")
    sync_iptables_rules(expected_filter, actual["filter"], "filter")

    # Sync IPv6 addresses to Firestore (only in manual mode, only for running VMs with IPv6)
    if not hook_mode and vms_for_ipv6_sync:
        sync_ipv6_to_firestore(vms_for_ipv6_sync)

    logger.info("Sync complete.")

if __name__ == "__main__":
    main()

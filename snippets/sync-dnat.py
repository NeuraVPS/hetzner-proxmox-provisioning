#!/usr/bin/env python3
"""
sync-dnat: Sync VM status and IPv6 to Firestore; on BASE, manage NAT64 (Jool BIB + ip6tables).

Commands:
  Hook (Proxmox):
    sync-dnat.py <VMID> post-start  — On VM start: set status running, wait for IP, sync IPv6 to Firestore.
    sync-dnat.py <VMID> post-stop    — On VM stop: set status stopped in Firestore.
  Manual sync:
    sync-dnat.py                     — No args: sync all local VMs' status and IPv6 to Firestore (no BASE NAT64).
  update_base (BASE only):
    update_base create NODE_HOSTNAME VMID VM_IPV6 [OSTYPE]  — Add NAT64 BIB + ip6tables for one VM. OSTYPE defaults to "win".
    update_base delete NODE_HOSTNAME VMID                   — Remove NAT64 rules for one VM.
    update_base restore                                     — Restore NAT64 to match Firestore: remove stale VMIDs, then add/update all.
    update_base restore VMID                                 — Restore NAT64 rules for a single VM from Firestore.
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
BASE_INTERNAL_IPV6 = "fd00:4000::1"  # SNAT source for IPv6 DNAT so VMs only see BASE, not client IPv6
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
# BASE node detection
# ---------------------------------------------------------------

def is_base_node():
    """Return True when hostname contains 'BASE' (case-insensitive)."""
    return "BASE" in NODE_NAME.upper()

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
# BASE: public IP, Jool BIB, ip6tables NAT64
# ---------------------------------------------------------------
def get_base_public_ip():
    """Return BASE's public IPv4 (for Jool BIB). Must run on BASE."""
    out = run(["ip", "-4", "route", "get", "8.8.8.8"])
    m = re.search(r"src\s+(\S+)", out)
    return m.group(1) if m else None

def get_base_public_ipv6():
    """Return BASE's public IPv6 (for IPv6 DNAT). Must run on BASE. Returns None if not found."""
    try:
        out = run(["ip", "-6", "route", "get", "2001:4860:4860::8888"])
        m = re.search(r"src\s+(\S+)", out)
        return m.group(1) if m else None
    except subprocess.CalledProcessError:
        return None

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
        result = run_ip6tables(["ip6tables", "-L", "FORWARD", "-n", "-v", "--line-numbers"])
        output = result.stdout or ""
        line_nums = []
        for line in output.splitlines():
            if comment in line:
                parts = line.strip().split()
                if parts and parts[0].isdigit():
                    line_nums.append(int(parts[0]))
        for num in sorted(line_nums, reverse=True):
            run_ip6tables(["ip6tables", "-D", "FORWARD", str(num)], check=False)
    except subprocess.CalledProcessError as e:
        logger.warning(f"ip6tables delete by comment failed: {e} (stderr: {e.stderr})")
    except Exception as e:
        logger.warning(f"ip6tables delete by comment failed: {e}")

def _delete_nat6_prerouting_by_comment(comment):
    """Delete ip6tables nat PREROUTING rules that have this comment. Deletes by line number high-to-low."""
    try:
        result = run_ip6tables(["ip6tables", "-t", "nat", "-L", "PREROUTING", "-n", "-v", "--line-numbers"])
        output = result.stdout or ""
        line_nums = []
        for line in output.splitlines():
            if comment in line:
                parts = line.strip().split()
                if parts and parts[0].isdigit():
                    line_nums.append(int(parts[0]))
        for num in sorted(line_nums, reverse=True):
            run_ip6tables(
                ["ip6tables", "-t", "nat", "-D", "PREROUTING", str(num)],
                check=False
            )
    except subprocess.CalledProcessError as e:
        logger.warning(f"ip6tables nat PREROUTING delete by comment failed: {e} (stderr: {e.stderr})")
    except Exception as e:
        logger.warning(f"ip6tables nat PREROUTING delete by comment failed: {e}")

def _delete_nat6_postrouting_by_comment(comment):
    """Delete ip6tables nat POSTROUTING rules that have this comment. Deletes by line number high-to-low."""
    try:
        result = run_ip6tables(["ip6tables", "-t", "nat", "-L", "POSTROUTING", "-n", "-v", "--line-numbers"])
        output = result.stdout or ""
        line_nums = []
        for line in output.splitlines():
            if comment in line:
                parts = line.strip().split()
                if parts and parts[0].isdigit():
                    line_nums.append(int(parts[0]))
        for num in sorted(line_nums, reverse=True):
            run_ip6tables(
                ["ip6tables", "-t", "nat", "-D", "POSTROUTING", str(num)],
                check=False
            )
    except subprocess.CalledProcessError as e:
        logger.warning(f"ip6tables nat POSTROUTING delete by comment failed: {e} (stderr: {e.stderr})")
    except Exception as e:
        logger.warning(f"ip6tables nat POSTROUTING delete by comment failed: {e}")

def apply_base_create_nat64(node_hostname, vmid, vm_ipv6, ostype):
    """Add BASE NAT64 BIB entries and ip6tables FORWARD for (node_hostname, vmid, vm_ipv6). Must run on BASE."""
    if not is_base_node():
        logger.error("apply_base_create_nat64 must run on BASE")
        return False
    base_ip = get_base_public_ip()
    if not base_ip:
        logger.error("Could not determine BASE public IPv4")
        return False
    # Remove any existing NAT64 rules for this VMID so create is idempotent (avoids duplicate rules after migration).
    apply_base_delete_nat64(node_hostname, vmid)
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
        # Insert at position 1 so our rules are evaluated before PVEFW-FORWARD (Proxmox firewall),
        # which would otherwise drop IPv6 forwarded traffic before we can ACCEPT it.
        # Insert -s first so -d ends up at 1 (incoming to VM is checked first).
        run_ip6tables(
            ["ip6tables", "-I", "FORWARD", "1", "-p", "tcp", "-s", vm_ipv6,
             "-m", "comment", "--comment", fwd_comment, "-j", "ACCEPT"]
        )
        run_ip6tables(
            ["ip6tables", "-I", "FORWARD", "1", "-p", "tcp", "-d", vm_ipv6,
             "-m", "comment", "--comment", fwd_comment, "-j", "ACCEPT"]
        )
        logger.info(f"ADD: ip6tables FORWARD for {vm_ipv6} (vmid-{vmid})")
    except subprocess.CalledProcessError as e:
        logger.warning(f"ip6tables FORWARD add failed: {e} (stderr: {e.stderr})")
    if ostype.startswith("win"):
        if _jool_bib_add(vm_ipv6, 445, base_ip, samba_port):
            logger.info(f"ADD: jool bib {base_ip}#{samba_port} -> {vm_ipv6}#445 (vmid-{vmid})")
        else:
            ok = False

    # IPv6 DNAT: BASE_IPv6:port -> VM_IPv6:3389/445 so IPv6 clients can connect without NAT64
    base_ipv6 = get_base_public_ipv6()
    dnat_comment = f"nat64-dnat-vmid-{vmid}"
    if base_ipv6:
        try:
            run_ip6tables(
                ["ip6tables", "-t", "nat", "-A", "PREROUTING", "-d", base_ipv6,
                 "-p", "tcp", "--dport", str(rdp_port), "-j", "DNAT",
                 "--to-destination", f"[{vm_ipv6}]:{to_port_rdp}",
                 "-m", "comment", "--comment", dnat_comment]
            )
            logger.info(f"ADD: ip6tables DNAT {base_ipv6}#{rdp_port} -> [{vm_ipv6}]:{to_port_rdp} (vmid-{vmid})")
        except subprocess.CalledProcessError as e:
            logger.warning(f"ip6tables DNAT RDP add failed: {e} (stderr: {e.stderr})")
        if ostype.startswith("win"):
            try:
                run_ip6tables(
                    ["ip6tables", "-t", "nat", "-A", "PREROUTING", "-d", base_ipv6,
                     "-p", "tcp", "--dport", str(samba_port), "-j", "DNAT",
                     "--to-destination", f"[{vm_ipv6}]:445",
                     "-m", "comment", "--comment", dnat_comment]
                )
                logger.info(f"ADD: ip6tables DNAT {base_ipv6}#{samba_port} -> [{vm_ipv6}]:445 (vmid-{vmid})")
            except subprocess.CalledProcessError as e:
                logger.warning(f"ip6tables DNAT Samba add failed: {e} (stderr: {e.stderr})")
        # SNAT so VMs see source BASE only; VM firewall allows fd00:4000::1, not arbitrary IPv6
        snat_comment = f"nat64-snat-vmid-{vmid}"
        try:
            run_ip6tables(
                ["ip6tables", "-t", "nat", "-A", "POSTROUTING", "-p", "tcp", "-d", vm_ipv6,
                 "--dport", str(to_port_rdp), "-j", "SNAT", "--to-source", BASE_INTERNAL_IPV6,
                 "-m", "comment", "--comment", snat_comment]
            )
            logger.info(f"ADD: ip6tables SNAT -> {BASE_INTERNAL_IPV6} for {vm_ipv6}:{to_port_rdp} (vmid-{vmid})")
        except subprocess.CalledProcessError as e:
            logger.warning(f"ip6tables SNAT RDP add failed: {e} (stderr: {e.stderr})")
        if ostype.startswith("win"):
            try:
                run_ip6tables(
                    ["ip6tables", "-t", "nat", "-A", "POSTROUTING", "-p", "tcp", "-d", vm_ipv6,
                     "--dport", "445", "-j", "SNAT", "--to-source", BASE_INTERNAL_IPV6,
                     "-m", "comment", "--comment", snat_comment]
                )
                logger.info(f"ADD: ip6tables SNAT -> {BASE_INTERNAL_IPV6} for {vm_ipv6}:445 (vmid-{vmid})")
            except subprocess.CalledProcessError as e:
                logger.warning(f"ip6tables SNAT Samba add failed: {e} (stderr: {e.stderr})")
    else:
        logger.debug("BASE has no public IPv6, skipping IPv6 DNAT")

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
    _delete_nat6_prerouting_by_comment(f"nat64-dnat-vmid-{vmid}")
    _delete_nat6_postrouting_by_comment(f"nat64-snat-vmid-{vmid}")
    logger.info(f"Removed BASE NAT64 rules for {node_hostname} vmid {vmid}")
    return True

def _get_base_nat64_vmids():
    """Return set of VMIDs that have NAT64 rules on BASE (from ip6tables PREROUTING comments)."""
    vmids = set()
    try:
        result = run_ip6tables(["ip6tables", "-t", "nat", "-L", "PREROUTING", "-n", "-v", "--line-numbers"])
        output = result.stdout or ""
        for line in output.splitlines():
            # Comment format: /* nat64-dnat-vmid-996 */
            m = re.search(r"nat64-dnat-vmid-(\d+)", line)
            if m:
                vmids.add(int(m.group(1)))
    except subprocess.CalledProcessError as e:
        logger.warning(f"Could not list BASE NAT64 VMIDs: {e} (stderr: {e.stderr})")
    except Exception as e:
        logger.warning(f"Could not list BASE NAT64 VMIDs: {e}")
    return vmids


def sync_base_restore_from_firestore():
    """Repopulate BASE BIB and ip6tables from Firestore (for use after BASE reboot).
    Removes NAT64 rules for VMIDs no longer in Firestore, then adds/updates rules for all
    servers with ipv6 set. Must run on BASE. Requires Firebase credentials.
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
        entries = []  # (node_id, vmid, ipv6, ostype)
        firestore_vmids = set()
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
            ostype_raw = (data.get("ostype") or "win").strip() or "win"
            ostype = ostype_raw if ostype_raw else "win"
            entries.append((node_id, vmid, ipv6, ostype))
            firestore_vmids.add(vmid)
        # Remove NAT64 rules for VMIDs no longer in Firestore
        current_vmids = _get_base_nat64_vmids()
        stale_vmids = current_vmids - firestore_vmids
        for vmid in sorted(stale_vmids):
            apply_base_delete_nat64("unknown", vmid)
        if stale_vmids:
            logger.info(f"NAT64 restore: removed stale rules for VMID(s) {sorted(stale_vmids)}")
        # Apply rules for each server in Firestore
        count = 0
        for node_id, vmid, ipv6, ostype in entries:
            if apply_base_create_nat64(node_id, vmid, ipv6, ostype):
                count += 1
            time.sleep(0.1)  # Reduce ip6tables lock contention between VMs
        logger.info(f"NAT64 restore from Firestore: applied BIB for {count} server(s)")
        return True
    except Exception as e:
        logger.warning(f"NAT64 restore from Firestore failed: {e}")
        return False


def sync_base_restore_vmid_from_firestore(vmid):
    """Restore NAT64 rules for a single VM from Firestore. Must run on BASE."""
    if not is_base_node():
        logger.error("sync_base_restore_vmid_from_firestore must run on BASE")
        return False
    if not ensure_firebase_initialized():
        logger.error("Firebase not initialized; cannot restore from Firestore")
        return False
    try:
        db = firestore.client()
        servers_ref = db.collection("servers")
        if FIELD_FILTER_AVAILABLE:
            query = servers_ref.where(filter=FieldFilter("proxmoxId", "==", vmid))
        else:
            query = servers_ref.where("proxmoxId", "==", vmid)
        docs = list(query.stream())
        if not docs:
            logger.warning(f"No Firestore server document found for VMID {vmid}")
            return False
        count = 0
        for doc in docs:
            data = doc.to_dict() or {}
            ipv6 = (data.get("ipv6") or "").strip() or None
            if not ipv6:
                continue
            node_id = data.get("nodeId") or "unknown"
            ostype_raw = (data.get("ostype") or "win").strip() or "win"
            ostype = ostype_raw if ostype_raw else "win"
            if apply_base_create_nat64(node_id, vmid, ipv6, ostype):
                count += 1
        logger.info(f"NAT64 restore from Firestore for VMID {vmid}: applied for {count} document(s)")
        return count > 0
    except Exception as e:
        logger.warning(f"NAT64 restore from Firestore for VMID {vmid} failed: {e}")
        return False

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
    # update_base create NODE_HOSTNAME vmid VM_IPV6 [OSTYPE]
    if len(sys.argv) >= 6 and sys.argv[2] == "create":
        node_hostname = sys.argv[3]
        try:
            vmid = int(sys.argv[4])
        except ValueError:
            logger.error(f"Invalid vmid: {sys.argv[4]}")
            return True
        vm_ipv6 = sys.argv[5]
        ostype = sys.argv[6] if len(sys.argv) > 6 else "win"
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
    # update_base restore VMID — restore one VM from Firestore
    if len(sys.argv) == 4 and sys.argv[2] == "restore":
        try:
            vmid = int(sys.argv[3])
        except ValueError:
            logger.error(f"Invalid vmid: {sys.argv[3]}")
            return True
        sync_base_restore_vmid_from_firestore(vmid)
        return True
    # update_base restore — repopulate BIB from Firestore (after BASE reboot)
    if len(sys.argv) == 3 and sys.argv[2] == "restore":
        sync_base_restore_from_firestore()
        return True
    logger.error("update_base requires a subcommand: create, delete, or restore")
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
    
    # Handle post-stop hook: update status only (node DNAT deprecated; NAT64 from BASE)
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

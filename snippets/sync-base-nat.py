#!/usr/bin/env python3
"""
sync-base-nat: BASE server NAT64 (Jool) + IPv6 DNAT for Samba/RDP via Firestore.

Reads /etc/default/base-nat (shell KEY=value). Env overrides.

Commands:
  sync-base-nat.py sync
      Full sync: Firestore servers (proxmoxId + ipv6); remove stale rules; apply all.
  sync-base-nat.py sync <proxmoxId>
      Single VM: one Firestore query + merge into STATE_FILE snapshot; full rebuild
      of Jool/DNAT from that map. Run "sync" (no args) to refresh from Firestore
      if servers were added only in the cloud.
  sync-base-nat.py sync <proxmoxId> <publicVMIPv6>
      Single VM with explicit IPv6 (no Firestore read for target).
  sync-base-nat.py sync <proxmoxId> del
      Remove rules for proxmoxId only (no Firestore).

Ports (TCP), VMID in [0, VMID_MAX] (default VMID_MAX=9999):
  Samba failover:(10000+VMID) -> vm:445   (ports 10000-19999)
  RDP    failover:(20000+VMID) -> vm:3389 (ports 20000-29999)
IPv4 path: Jool NAT64 static BIB + pool4 on FAILOVER_IPV4.
IPv6 path: ip6tables DNAT on FAILOVER_IPV6; optional POSTROUTING SNAT via MAIN_IPV6
(Hetzner egress often drops forwarded packets whose source is not your routed /64).
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

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

LOG_FILE = Path("/var/log/sync-base-nat.log")
MAX_LOG_SIZE = 1024 * 1024
STATE_DIR = Path("/var/lib/base-nat")
DEFAULT_STATE_FILE = STATE_DIR / "state.json"
DEFAULT_ENV_FILE = Path("/etc/default/base-nat")


def clean_log_if_needed():
    if LOG_FILE.exists() and LOG_FILE.stat().st_size > MAX_LOG_SIZE:
        LOG_FILE.unlink()
        return True
    return False


was_cleaned = clean_log_if_needed()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, mode="a"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)
if was_cleaned:
    logger.info("Log truncated (>1 MB)")


def load_default_env(path: Path = DEFAULT_ENV_FILE):
    """Parse KEY=value from /etc/default/base-nat (no export required)."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


load_default_env()

FAILOVER_IPV4 = os.environ.get("FAILOVER_IPV4", "").strip()
FAILOVER_IPV6 = os.environ.get("FAILOVER_IPV6", "").strip()
# Primary inet6 on BASE (iface static ::2). SNAT forwarded DNAT traffic so egress source is in your /64.
MAIN_IPV6 = os.environ.get("MAIN_IPV6", "").strip()
POOL6 = os.environ.get("POOL6", "").strip()
JOOL_INSTANCE = os.environ.get("JOOL_INSTANCE", "base").strip()
STATE_FILE = Path(os.environ.get("STATE_FILE", str(DEFAULT_STATE_FILE)))
FIREBASE_CREDENTIALS = os.environ.get(
    "FIREBASE_CREDENTIALS_FILE", "/etc/firebase-credentials.json"
)
SAMBA_PORT_BASE = int(os.environ.get("SAMBA_PORT_BASE", "10000"))
RDP_PORT_BASE = int(os.environ.get("RDP_PORT_BASE", "20000"))
# Max proxmoxId/VMID; Samba uses SAMBA_PORT_BASE..SAMBA_PORT_BASE+VMID_MAX, RDP uses RDP_PORT_BASE..RDP_PORT_BASE+VMID_MAX
VMID_MAX = int(os.environ.get("VMID_MAX", os.environ.get("PORT_MAX", "9999")))
SAMBA_PORT_END = SAMBA_PORT_BASE + VMID_MAX
RDP_PORT_END = RDP_PORT_BASE + VMID_MAX
DPORTS_SMB_RDP = f"{SAMBA_PORT_BASE}:{SAMBA_PORT_END},{RDP_PORT_BASE}:{RDP_PORT_END}"

FIREBASE_INITIALIZED = False


def initialize_firebase():
    global FIREBASE_INITIALIZED
    if not FIREBASE_AVAILABLE:
        return False
    if firebase_admin._apps:
        return True
    p = Path(FIREBASE_CREDENTIALS)
    if not p.is_file():
        logger.warning("Firebase credentials missing: %s", FIREBASE_CREDENTIALS)
        return False
    try:
        firebase_admin.initialize_app(credentials.Certificate(str(p)))
        logger.info("Firebase initialized from %s", FIREBASE_CREDENTIALS)
        FIREBASE_INITIALIZED = True
        return True
    except Exception as e:
        logger.warning("Firebase init failed: %s", e)
        return False


def ensure_firebase():
    if FIREBASE_INITIALIZED or firebase_admin._apps:
        return True
    return initialize_firebase()


def run(cmd: list[str], check: bool = True) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if check and r.returncode != 0:
        raise subprocess.CalledProcessError(
            r.returncode, cmd, r.stdout, r.stderr
        )
    return (r.stdout or "").strip()


def run_jool(args: list[str], check: bool = True) -> str:
    cmd = ["jool", "-i", JOOL_INSTANCE] + args
    return run(cmd, check=check)


def run_ip6tables(cmd: list[str], check: bool = True, max_retries: int = 3):
    last = None
    for _ in range(max_retries):
        last = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if last.returncode == 0:
            return last
        time.sleep(0.3)
    if check:
        raise subprocess.CalledProcessError(
            last.returncode, cmd, last.stdout, last.stderr
        )
    return last


def jool_bib_add_tcp(ipv4_host: str, ipv4_port: int, ipv6_host: str, ipv6_port: int):
    # Syntax: bib add --tcp <IPv4>#port <IPv6>#port
    run_jool(
        [
            "bib",
            "add",
            "--tcp",
            f"{ipv4_host}#{ipv4_port}",
            f"{ipv6_host}#{ipv6_port}",
        ]
    )


def jool_bib_remove_tcp(ipv4_host: str, ipv4_port: int):
    run_jool(["bib", "remove", "--tcp", f"{ipv4_host}#{ipv4_port}"], check=False)


def validate_config():
    if not FAILOVER_IPV4 or not FAILOVER_IPV6:
        logger.error("FAILOVER_IPV4 and FAILOVER_IPV6 required in /etc/default/base-nat")
        sys.exit(1)
    ipaddress.ip_address(FAILOVER_IPV4)
    ipaddress.ip_address(FAILOVER_IPV6)
    if MAIN_IPV6:
        ipaddress.ip_address(MAIN_IPV6)
    if POOL6:
        ipaddress.ip_network(POOL6, strict=False)


def vm_ports(vmid: int) -> tuple[int, int]:
    if vmid < 0 or vmid > VMID_MAX:
        raise ValueError(f"proxmoxId/VMID {vmid} out of range [0, {VMID_MAX}]")
    return SAMBA_PORT_BASE + vmid, RDP_PORT_BASE + vmid


def read_state() -> dict:
    if not STATE_FILE.is_file():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception as e:
        logger.warning("State read failed: %s", e)
        return {}


def write_state(state: dict):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def desired_from_state() -> dict[int, str]:
    """vmid -> ipv6 from STATE_FILE (on-box snapshot)."""
    out: dict[int, str] = {}
    for k, d in read_state().items():
        try:
            vmid = int(k)
        except (TypeError, ValueError):
            continue
        if not isinstance(d, dict):
            continue
        ipv6 = d.get("ipv6")
        if ipv6:
            try:
                ipaddress.ip_address(str(ipv6).strip())
            except ValueError:
                continue
            out[vmid] = str(ipv6).strip()
    return out


def ensure_ip6tables_chain():
    # SMB block 10000-19999 + RDP block 20000-29999 (when VMID_MAX=9999)
    run_ip6tables(
        ["ip6tables", "-t", "nat", "-N", "BASE_DNAT"], check=False
    )
    r = run_ip6tables(
        [
            "ip6tables",
            "-t",
            "nat",
            "-C",
            "PREROUTING",
            "-d",
            FAILOVER_IPV6,
            "-p",
            "tcp",
            "-m",
            "multiport",
            "--dports",
            DPORTS_SMB_RDP,
            "-j",
            "BASE_DNAT",
        ],
        check=False,
    )
    if r.returncode != 0:
        run_ip6tables(
            [
                "ip6tables",
                "-t",
                "nat",
                "-A",
                "PREROUTING",
                "-d",
                FAILOVER_IPV6,
                "-p",
                "tcp",
                "-m",
                "multiport",
                "--dports",
                DPORTS_SMB_RDP,
                "-j",
                "BASE_DNAT",
            ]
        )


def flush_base_dnat_chain():
    run_ip6tables(["ip6tables", "-t", "nat", "-F", "BASE_DNAT"], check=False)


def ensure_base_snat_chain():
    """POSTROUTING SNAT so forwarded SYNs to VMs use MAIN_IPV6 (provider egress filter)."""
    run_ip6tables(["ip6tables", "-t", "nat", "-N", "BASE_SNAT"], check=False)
    r = run_ip6tables(
        [
            "ip6tables",
            "-t",
            "nat",
            "-C",
            "POSTROUTING",
            "-j",
            "BASE_SNAT",
        ],
        check=False,
    )
    if r.returncode != 0:
        run_ip6tables(
            ["ip6tables", "-t", "nat", "-A", "POSTROUTING", "-j", "BASE_SNAT"]
        )


def flush_base_snat_chain():
    run_ip6tables(["ip6tables", "-t", "nat", "-F", "BASE_SNAT"], check=False)


def rebuild_ip6tables_snat(desired: dict[int, str]):
    """SNAT tcp to each VM:445/3389 to MAIN_IPV6 (only traffic BASE forwards, e.g. after DNAT)."""
    ensure_base_snat_chain()
    flush_base_snat_chain()
    if not MAIN_IPV6:
        logger.info(
            "MAIN_IPV6 unset: no IPv6 SNAT (set MAIN_IPV6=primary ::2 if failover IPv6 DNAT times out)"
        )
        return
    for _vmid, ipv6 in sorted(desired.items(), key=lambda x: x[0]):
        run_ip6tables(
            [
                "ip6tables",
                "-t",
                "nat",
                "-A",
                "BASE_SNAT",
                "-d",
                ipv6,
                "-p",
                "tcp",
                "-m",
                "multiport",
                "--dports",
                "445,3389",
                "-j",
                "SNAT",
                "--to-source",
                MAIN_IPV6,
            ]
        )
    logger.info("IPv6 SNAT via MAIN_IPV6 for %d VM target(s)", len(desired))


def rebuild_ip6tables_dnat(desired: dict[int, str]):
    """desired: vmid -> ipv6 string"""
    ensure_ip6tables_chain()
    flush_base_dnat_chain()
    for vmid in sorted(desired.keys()):
        ipv6 = desired[vmid]
        samba_p, rdp_p = vm_ports(vmid)
        run_ip6tables(
            [
                "ip6tables",
                "-t",
                "nat",
                "-A",
                "BASE_DNAT",
                "-p",
                "tcp",
                "--dport",
                str(samba_p),
                "-m",
                "comment",
                "--comment",
                f"base-nat-{vmid}-samba",
                "-j",
                "DNAT",
                "--to-destination",
                f"[{ipv6}]:445",
            ]
        )
        run_ip6tables(
            [
                "ip6tables",
                "-t",
                "nat",
                "-A",
                "BASE_DNAT",
                "-p",
                "tcp",
                "--dport",
                str(rdp_p),
                "-m",
                "comment",
                "--comment",
                f"base-nat-{vmid}-rdp",
                "-j",
                "DNAT",
                "--to-destination",
                f"[{ipv6}]:3389",
            ]
        )
    rebuild_ip6tables_snat(desired)


def remove_vm_rules(vmid: int):
    samba_p, rdp_p = vm_ports(vmid)
    jool_bib_remove_tcp(FAILOVER_IPV4, samba_p)
    jool_bib_remove_tcp(FAILOVER_IPV4, rdp_p)


def apply_vm_jool(vmid: int, ipv6: str):
    samba_p, rdp_p = vm_ports(vmid)
    jool_bib_remove_tcp(FAILOVER_IPV4, samba_p)
    jool_bib_remove_tcp(FAILOVER_IPV4, rdp_p)
    jool_bib_add_tcp(FAILOVER_IPV4, samba_p, ipv6, 445)
    jool_bib_add_tcp(FAILOVER_IPV4, rdp_p, ipv6, 3389)


def firestore_list_configured_servers() -> dict[int, str]:
    """proxmoxId -> ipv6"""
    if not ensure_firebase():
        logger.error("Firebase required for full sync")
        sys.exit(1)
    db = firestore.client()
    out: dict[int, str] = {}
    for doc in db.collection("servers").stream():
        d = doc.to_dict() or {}
        pid = d.get("proxmoxId")
        ipv6 = d.get("ipv6")
        if pid is None or not ipv6:
            continue
        try:
            vmid = int(pid)
        except (TypeError, ValueError):
            continue
        if vmid < 0 or vmid > VMID_MAX:
            logger.warning(
                "Skip server %s: proxmoxId %s outside SMB/RDP port range [0,%s]",
                doc.id,
                vmid,
                VMID_MAX,
            )
            continue
        s = str(ipv6).strip()
        if not s:
            continue
        try:
            ipaddress.ip_address(s)
        except ValueError:
            logger.warning("Skip server %s: bad ipv6 %s", doc.id, s)
            continue
        out[vmid] = s
    return out


def firestore_ipv6_for_vmid(vmid: int) -> str | None:
    if not ensure_firebase():
        return None
    db = firestore.client()
    if FIELD_FILTER_AVAILABLE:
        q = db.collection("servers").where(
            filter=FieldFilter("proxmoxId", "==", vmid)
        )
    else:
        q = db.collection("servers").where("proxmoxId", "==", vmid)
    for doc in q.limit(2).stream():
        d = doc.to_dict() or {}
        ipv6 = d.get("ipv6")
        if ipv6:
            return str(ipv6).strip()
    return None


def sync_full():
    desired = firestore_list_configured_servers()
    state = read_state()
    state_ids = {int(k) for k in state.keys()}
    for vmid in state_ids | set(desired.keys()):
        remove_vm_rules(vmid)
    for vmid, ipv6 in desired.items():
        logger.info("Apply proxmoxId=%s -> %s", vmid, ipv6)
    apply_all_jool(desired)
    rebuild_ip6tables_dnat(desired)  # includes rebuild_ip6tables_snat
    write_state(
        {
            str(k): {
                "ipv6": desired[k],
                "samba": SAMBA_PORT_BASE + k,
                "rdp": RDP_PORT_BASE + k,
            }
            for k in desired
        }
    )
    try:
        run_jool(["session", "sync"], check=False)
    except Exception:
        pass
    logger.info("Full sync done (%d servers)", len(desired))


def apply_all_jool(desired: dict[int, str]):
    """Reapply every BIB from desired (full sync of Jool static BIBs for our ports)."""
    for vmid in sorted(desired.keys()):
        apply_vm_jool(vmid, desired[vmid])


def sync_single_vmid(
    vmid: int,
    ipv6: str | None,
    delete_only: bool,
    ipv6_override: bool = False,
):
    """Patch one VMID into on-box desired map (state snapshot), then full Jool/DNAT rebuild.

    Does not scan all Firestore servers: desired starts from STATE_FILE, then
    delete_only / explicit ipv6 / ipv6 from caller (e.g. firestore_ipv6_for_vmid).
    Run full ``sync`` to pull every server from Firestore into state.
    """
    if not delete_only and not ipv6_override:
        if not ensure_firebase():
            logger.error("Firebase required for sync <proxmoxId> (lookup)")
            sys.exit(1)
    if ipv6_override and ipv6 is None:
        logger.error("Explicit IPv6 required for sync <proxmoxId> <ipv6>")
        sys.exit(2)
    desired = desired_from_state()
    if delete_only:
        desired.pop(vmid, None)
        logger.info("proxmoxId=%s removed from maps", vmid)
    elif ipv6_override:
        desired[vmid] = ipv6  # type: ignore[assignment]
    elif ipv6 is not None:
        desired[vmid] = ipv6
    else:
        desired.pop(vmid, None)
        logger.info("proxmoxId=%s not in Firestore; dropping from maps", vmid)

    # Only touch Jool for this VMID (other BIBs unchanged). Full sync + boot still
    # run apply_all_jool. Avoids ~6 Jool ops per server on every sync <id>.
    try:
        remove_vm_rules(vmid)
    except Exception:
        pass
    if vmid in desired:
        try:
            apply_vm_jool(vmid, desired[vmid])
        except Exception as e:
            logger.warning("apply_vm_jool proxmoxId=%s: %s", vmid, e)
    rebuild_ip6tables_dnat(desired)
    write_state(
        {
            str(k): {
                "ipv6": desired[k],
                "samba": SAMBA_PORT_BASE + k,
                "rdp": RDP_PORT_BASE + k,
            }
            for k in desired
        }
    )
    try:
        run_jool(["session", "sync"], check=False)
    except Exception:
        pass
    logger.info(
        "Sync proxmoxId=%s done (desired=%d VMs; Jool updated for this id only)",
        vmid,
        len(desired),
    )


def main():
    if len(sys.argv) < 2 or sys.argv[1] != "sync":
        print(
            "Usage: sync-base-nat.py sync\n"
            "       sync-base-nat.py sync <proxmoxId>\n"
            "       sync-base-nat.py sync <proxmoxId> <ipv6>\n"
            "       sync-base-nat.py sync <proxmoxId> del",
            file=sys.stderr,
        )
        sys.exit(2)
    validate_config()
    args = sys.argv[2:]
    if not args:
        sync_full()
        return
    try:
        vmid = int(args[0])
    except ValueError:
        logger.error("proxmoxId must be int")
        sys.exit(2)
    is_del = len(args) >= 2 and args[1].lower() == "del"
    if vmid < 0 or vmid > VMID_MAX:
        logger.error(
            "proxmoxId must be in [0, %s] (SMB %s-%s, RDP %s-%s)",
            VMID_MAX,
            SAMBA_PORT_BASE,
            SAMBA_PORT_END,
            RDP_PORT_BASE,
            RDP_PORT_END,
        )
        sys.exit(2)
    if is_del:
        sync_single_vmid(vmid, None, delete_only=True)
        return
    if len(args) >= 2:
        ipv6 = args[1].strip()
        try:
            ipaddress.ip_address(ipv6)
        except ValueError:
            logger.error("Invalid IPv6")
            sys.exit(2)
        sync_single_vmid(vmid, ipv6, delete_only=False, ipv6_override=True)
        return
    ipv6 = firestore_ipv6_for_vmid(vmid)
    sync_single_vmid(vmid, ipv6, delete_only=False, ipv6_override=False)


if __name__ == "__main__":
    main()

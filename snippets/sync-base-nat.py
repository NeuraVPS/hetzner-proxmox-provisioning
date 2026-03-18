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

  sync-base-nat.py sync nodes
      PVE proxy: full sync from Firestore collection proxmox_nodes (doc id = node
      slug, field ip = IPv6). Writes pve_nodes.json + nginx map; nginx reload.
  sync-base-nat.py sync nodes <nodeId>
      Merge one node from Firestore proxmox_nodes/<nodeId> (field ip).
  sync-base-nat.py sync nodes add <nodeId> <ipv6>
      Upsert node locally + nginx map (no Firestore read). For Cloud Function hooks.
  sync-base-nat.py sync nodes del <nodeId>
      Remove node from local map + nginx reload.
  sync-base-nat.py sync nodes sync-firewall
      Pull cluster.fw from Storage Box; replace [IPSET hosts-ipv6] with /64 per
      pve_nodes.json; push back; SSH each node to pull + pve-firewall restart.

Ports, VMID in [0, VMID_MAX] (default VMID_MAX=9999):
  Samba main:(10000+VMID) -> vm:445   TCP only (ports 10000-19999)
  RDP    main:(20000+VMID) -> vm:3389 TCP+UDP (ports 20000-29999)
IPv4 path: Jool NAT64 static BIB + pool4 on MAIN_IPV4. Failover IPv4 is DNAT'd to MAIN_IPV4.
IPv6 path: ip6tables DNAT on MAIN_IPV6; POSTROUTING SNAT via MAIN_IPV6. Failover IPv6 DNAT'd to MAIN_IPV6.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import subprocess
import sys
import tempfile
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
# Main/public IPs: NAT ingress (Jool pool4/BIB and ip6tables DNAT listen here).
MAIN_IPV4 = os.environ.get("MAIN_IPV4", "").strip()
MAIN_IPV6 = os.environ.get("MAIN_IPV6", "").strip()
POOL6 = os.environ.get("POOL6", "").strip()
JOOL_INSTANCE = os.environ.get("JOOL_INSTANCE", "base").strip()
STATE_FILE = Path(os.environ.get("STATE_FILE", str(DEFAULT_STATE_FILE)))
PVE_NODES_STATE_FILE = Path(
    os.environ.get("PVE_NODES_STATE_FILE", str(STATE_DIR / "pve_nodes.json"))
)
PVE_NGINX_MAP_FILE = Path(
    os.environ.get(
        "PVE_NGINX_MAP_FILE", "/etc/nginx/conf.d/pve-proxy-backends.map.conf"
    )
)
# Storage Box: cluster.fw template (scp port 23). Override via env on BASE.
FIREWALL_STORAGE_USER = os.environ.get("FIREWALL_STORAGE_USER", "u560363").strip()
FIREWALL_STORAGE_HOST = os.environ.get(
    "FIREWALL_STORAGE_HOST", "u560363.your-storagebox.de"
).strip()
FIREWALL_REMOTE_PATH = os.environ.get(
    "FIREWALL_REMOTE_PATH", "/home/firewall/cluster.fw"
).strip()
FIREWALL_SCP_PORT = os.environ.get("FIREWALL_SCP_PORT", "23").strip()
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
DPORTS_RDP = f"{RDP_PORT_BASE}:{RDP_PORT_END}"  # UDP RDP only

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


def jool_bib_add_udp(ipv4_host: str, ipv4_port: int, ipv6_host: str, ipv6_port: int):
    run_jool(
        [
            "bib",
            "add",
            "--udp",
            f"{ipv4_host}#{ipv4_port}",
            f"{ipv6_host}#{ipv6_port}",
        ]
    )


def jool_bib_remove_udp(ipv4_host: str, ipv4_port: int):
    run_jool(["bib", "remove", "--udp", f"{ipv4_host}#{ipv4_port}"], check=False)


def validate_config():
    if not MAIN_IPV4 or not MAIN_IPV6:
        logger.error("MAIN_IPV4 and MAIN_IPV6 required in /etc/default/base-nat")
        sys.exit(1)
    if not FAILOVER_IPV4 or not FAILOVER_IPV6:
        logger.error("FAILOVER_IPV4 and FAILOVER_IPV6 required in /etc/default/base-nat")
        sys.exit(1)
    ipaddress.ip_address(MAIN_IPV4)
    ipaddress.ip_address(MAIN_IPV6)
    ipaddress.ip_address(FAILOVER_IPV4)
    ipaddress.ip_address(FAILOVER_IPV6)
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
    # PREROUTING to BASE_DNAT: main IPv6, TCP SMB+RDP and UDP RDP only.
    run_ip6tables(
        ["ip6tables", "-t", "nat", "-N", "BASE_DNAT"], check=False
    )
    for proto, dports in [("tcp", DPORTS_SMB_RDP), ("udp", DPORTS_RDP)]:
        r = run_ip6tables(
            [
                "ip6tables",
                "-t",
                "nat",
                "-C",
                "PREROUTING",
                "-d",
                MAIN_IPV6,
                "-p",
                proto,
                "-m",
                "multiport",
                "--dports",
                dports,
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
                    MAIN_IPV6,
                    "-p",
                    proto,
                    "-m",
                    "multiport",
                    "--dports",
                    dports,
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
    """SNAT to MAIN_IPV6: TCP 445,3389 and UDP 3389 (reply path for DNAT traffic)."""
    ensure_base_snat_chain()
    flush_base_snat_chain()
    if not MAIN_IPV6:
        logger.info(
            "MAIN_IPV6 unset: no IPv6 SNAT (set MAIN_IPV6=primary ::2 if DNAT reply path fails)"
        )
        return
    for _vmid, ipv6 in sorted(desired.items(), key=lambda x: x[0]):
        for proto, dports in [("tcp", "445,3389"), ("udp", "3389")]:
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
                    proto,
                    "-m",
                    "multiport",
                    "--dports",
                    dports,
                    "-j",
                    "SNAT",
                    "--to-source",
                    MAIN_IPV6,
                ]
            )
    logger.info("IPv6 SNAT via MAIN_IPV6 for %d VM target(s) (TCP 445/3389, UDP 3389)", len(desired))


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
        run_ip6tables(
            [
                "ip6tables",
                "-t",
                "nat",
                "-A",
                "BASE_DNAT",
                "-p",
                "udp",
                "--dport",
                str(rdp_p),
                "-m",
                "comment",
                "--comment",
                f"base-nat-{vmid}-rdp-udp",
                "-j",
                "DNAT",
                "--to-destination",
                f"[{ipv6}]:3389",
            ]
        )
    rebuild_ip6tables_snat(desired)


def remove_vm_rules(vmid: int):
    samba_p, rdp_p = vm_ports(vmid)
    jool_bib_remove_tcp(MAIN_IPV4, samba_p)
    jool_bib_remove_tcp(MAIN_IPV4, rdp_p)
    jool_bib_remove_udp(MAIN_IPV4, rdp_p)


def apply_vm_jool(vmid: int, ipv6: str):
    samba_p, rdp_p = vm_ports(vmid)
    jool_bib_remove_tcp(MAIN_IPV4, samba_p)
    jool_bib_remove_tcp(MAIN_IPV4, rdp_p)
    jool_bib_remove_udp(MAIN_IPV4, rdp_p)
    jool_bib_add_tcp(MAIN_IPV4, samba_p, ipv6, 445)
    jool_bib_add_tcp(MAIN_IPV4, rdp_p, ipv6, 3389)
    jool_bib_add_udp(MAIN_IPV4, rdp_p, ipv6, 3389)


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


# --- PVE proxy: proxmox_nodes -> nginx map (no Jool) --------------------------------


def _nginx_map_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


IPSET_HOSTS_IPV6_HEADER = "[IPSET hosts-ipv6]"


def _firewall_scp_base_args() -> list[str]:
    return [
        "-P",
        FIREWALL_SCP_PORT,
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "BatchMode=yes",
    ]


def replace_hosts_ipv6_section(text: str, nodes: dict[str, str]) -> str:
    """
    Keep template intact except [IPSET hosts-ipv6]: one line per unique /64
    from nodes (node_id comment). Dedupe by /64; log duplicate subnets.
    """
    lines = text.splitlines(keepends=True)
    start = None
    for i, line in enumerate(lines):
        if line.strip() == IPSET_HOSTS_IPV6_HEADER:
            start = i
            break
    if start is None:
        raise ValueError("cluster.fw: missing [IPSET hosts-ipv6] section")
    end = len(lines)
    for j in range(start + 1, len(lines)):
        stripped = lines[j].strip()
        if stripped.startswith("[") and stripped != IPSET_HOSTS_IPV6_HEADER:
            end = j
            break
    seen_net: dict[str, str] = {}
    dupes: list[tuple[str, str]] = []
    for node_id in sorted(nodes.keys()):
        ip_s = nodes[node_id].strip()
        try:
            iface = ipaddress.IPv6Interface(f"{ip_s}/64")
            net = iface.network
        except ValueError as e:
            raise ValueError(f"node {node_id!r}: bad IPv6 {ip_s!r}: {e}") from e
        net_s = str(net)
        if net_s in seen_net:
            dupes.append((node_id, seen_net[net_s]))
            continue
        seen_net[net_s] = node_id
    if dupes:
        for node_id, first in dupes:
            logger.warning(
                "sync-firewall: duplicate /64 for node %s (same as %s); skipping duplicate line",
                node_id,
                first,
            )
    body_lines = [f"{net_s} # {seen_net[net_s]}\n" for net_s in sorted(seen_net.keys())]
    new_block = IPSET_HOSTS_IPV6_HEADER + "\n" + "".join(body_lines)
    if end < len(lines) and lines[end].strip().startswith("["):
        if not new_block.endswith("\n"):
            new_block += "\n"
    else:
        new_block += "\n"
    return "".join(lines[:start]) + new_block + "".join(lines[end:])


def read_pve_nodes_state() -> dict[str, str]:
    if not PVE_NODES_STATE_FILE.is_file():
        return {}
    try:
        raw = json.loads(PVE_NODES_STATE_FILE.read_text())
    except Exception as e:
        logger.warning("PVE state read failed: %s", e)
        return {}
    out: dict[str, str] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        if not k or not isinstance(v, str):
            continue
        try:
            ipaddress.ip_address(v.strip())
        except ValueError:
            continue
        out[str(k).strip()] = v.strip()
    return out


def write_pve_nodes_state(nodes: dict[str, str]):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PVE_NODES_STATE_FILE.write_text(
        json.dumps(dict(sorted(nodes.items())), indent=2, sort_keys=True) + "\n"
    )


def write_pve_nginx_map(nodes: dict[str, str]):
    lines = [
        "# generated by sync-base-nat.py — do not edit",
        "map $pve_node_from_host $pve_backend_from_host {",
        '    default "";',
    ]
    for node_id in sorted(nodes.keys()):
        ip = nodes[node_id]
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            continue
        backend = f"https://[{ip}]:8006"
        lines.append(
            f'    "{_nginx_map_escape(node_id)}" "{_nginx_map_escape(backend)}";'
        )
    lines.append("}")
    PVE_NGINX_MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
    PVE_NGINX_MAP_FILE.write_text("\n".join(lines) + "\n")
    logger.info("Wrote %s (%d nodes)", PVE_NGINX_MAP_FILE, len(nodes))


def nginx_test_and_reload():
    try:
        subprocess.run(["nginx", "-t"], capture_output=True, text=True, check=True)
    except FileNotFoundError:
        logger.warning("nginx not installed; map written, skip reload")
        return
    except subprocess.CalledProcessError as e:
        logger.error("nginx -t failed: %s", (e.stderr or e.stdout or "").strip())
        sys.exit(1)
    r = subprocess.run(["systemctl", "reload", "nginx"], capture_output=True, text=True)
    if r.returncode != 0:
        logger.warning(
            "systemctl reload nginx: %s",
            (r.stderr or r.stdout or "failed").strip(),
        )
    else:
        logger.info("nginx reloaded")


def firestore_list_proxmox_nodes() -> dict[str, str]:
    """Document id -> ipv6 from field ip."""
    if not ensure_firebase():
        logger.error("Firebase required for sync nodes")
        sys.exit(1)
    db = firestore.client()
    out: dict[str, str] = {}
    for doc in db.collection("proxmox_nodes").stream():
        node_id = doc.id.strip()
        if not node_id:
            continue
        d = doc.to_dict() or {}
        ip = d.get("ip")
        if not ip:
            logger.warning("Skip proxmox_nodes/%s: missing ip", node_id)
            continue
        s = str(ip).strip()
        try:
            ipaddress.ip_address(s)
        except ValueError:
            logger.warning("Skip proxmox_nodes/%s: bad ip %s", node_id, s)
            continue
        out[node_id] = s
    return out


def firestore_ip_for_proxmox_node(node_id: str) -> str | None:
    if not ensure_firebase():
        return None
    db = firestore.client()
    ref = db.collection("proxmox_nodes").document(node_id)
    snap = ref.get()
    if not snap.exists:
        return None
    d = snap.to_dict() or {}
    ip = d.get("ip")
    if not ip:
        return None
    s = str(ip).strip()
    try:
        ipaddress.ip_address(s)
    except ValueError:
        return None
    return s


def sync_nodes_apply_state(nodes: dict[str, str], reload_nginx: bool = True):
    write_pve_nodes_state(nodes)
    write_pve_nginx_map(nodes)
    if reload_nginx:
        nginx_test_and_reload()
    logger.info("PVE nodes map: %d entries", len(nodes))


def sync_nodes_full():
    nodes = firestore_list_proxmox_nodes()
    sync_nodes_apply_state(nodes)
    logger.info("sync nodes full done (%d from Firestore)", len(nodes))


def sync_nodes_single(node_id: str):
    node_id = node_id.strip()
    if not node_id:
        logger.error("nodeId required")
        sys.exit(2)
    if not ensure_firebase():
        logger.error("Firebase required for sync nodes <nodeId>")
        sys.exit(1)
    ip = firestore_ip_for_proxmox_node(node_id)
    state = read_pve_nodes_state()
    if ip:
        state[node_id] = ip
        logger.info("sync nodes %s -> %s", node_id, ip)
    else:
        state.pop(node_id, None)
        logger.info("sync nodes %s: not in Firestore or bad ip; removed from map", node_id)
    sync_nodes_apply_state(state)


def sync_nodes_add(node_id: str, ipv6: str):
    node_id = node_id.strip()
    ipv6 = ipv6.strip()
    try:
        ipaddress.ip_address(ipv6)
    except ValueError:
        logger.error("Invalid IPv6")
        sys.exit(2)
    state = read_pve_nodes_state()
    state[node_id] = ipv6
    sync_nodes_apply_state(state)
    logger.info("sync nodes add %s -> %s", node_id, ipv6)


def sync_nodes_del(node_id: str):
    node_id = node_id.strip()
    state = read_pve_nodes_state()
    state.pop(node_id, None)
    sync_nodes_apply_state(state)
    logger.info("sync nodes del %s", node_id)


def sync_nodes_sync_firewall():
    """
    Pull cluster.fw from Storage Box, rewrite [IPSET hosts-ipv6] from
    pve_nodes.json, push back, then on each node scp pull + pve-firewall restart.
    """
    nodes = read_pve_nodes_state()
    if not nodes:
        logger.error("sync-firewall: pve_nodes.json empty or missing; abort")
        sys.exit(1)
    remote = f"{FIREWALL_STORAGE_USER}@{FIREWALL_STORAGE_HOST}:{FIREWALL_REMOTE_PATH}"
    scp_args = _firewall_scp_base_args()
    fd, tmp_path = tempfile.mkstemp(prefix="cluster.fw.", suffix=".tmp")
    os.close(fd)
    tmp = Path(tmp_path)
    try:
        r = subprocess.run(
            ["scp"] + scp_args + [remote, str(tmp)],
            capture_output=True,
            text=True,
            check=False,
        )
        if r.returncode != 0:
            logger.error(
                "sync-firewall: scp pull failed: %s",
                (r.stderr or r.stdout or "").strip(),
            )
            sys.exit(1)
        text = tmp.read_text()
        try:
            new_text = replace_hosts_ipv6_section(text, nodes)
        except ValueError as e:
            logger.error("sync-firewall: %s", e)
            sys.exit(1)
        tmp.write_text(new_text)
        r2 = subprocess.run(
            ["scp"] + scp_args + [str(tmp), remote],
            capture_output=True,
            text=True,
            check=False,
        )
        if r2.returncode != 0:
            logger.error(
                "sync-firewall: scp push failed: %s",
                (r2.stderr or r2.stdout or "").strip(),
            )
            sys.exit(1)
        logger.info("sync-firewall: Storage Box cluster.fw updated (%d nodes)", len(nodes))
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass

    remote_scp_inner = (
        f"scp -P {FIREWALL_SCP_PORT} -o StrictHostKeyChecking=no "
        f"-o UserKnownHostsFile=/dev/null -o BatchMode=yes "
        f"{FIREWALL_STORAGE_USER}@{FIREWALL_STORAGE_HOST}:{FIREWALL_REMOTE_PATH} "
        f"/etc/pve/firewall/cluster.fw && pve-firewall restart"
    )
    failed: list[str] = []
    for node_id, ipv6 in sorted(nodes.items()):
        target = f"root@{ipv6}"
        ssh_cmd = [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "BatchMode=yes",
            target,
            remote_scp_inner,
        ]
        r3 = subprocess.run(ssh_cmd, capture_output=True, text=True, check=False)
        if r3.returncode != 0:
            err = (r3.stderr or r3.stdout or "").strip()
            logger.warning("sync-firewall: node %s (%s): %s", node_id, target, err)
            failed.append(node_id)
        else:
            logger.info("sync-firewall: node %s ok", node_id)
    if failed:
        logger.error("sync-firewall: failed on %d node(s): %s", len(failed), failed)
        sys.exit(1)
    logger.info("sync-firewall: done (%d nodes)", len(nodes))


def main_sync_nodes():
    args = sys.argv[3:]
    if not args:
        sync_nodes_full()
        return
    if args[0] == "add":
        if len(args) < 3:
            print(
                "Usage: sync-base-nat.py sync nodes add <nodeId> <ipv6>",
                file=sys.stderr,
            )
            sys.exit(2)
        sync_nodes_add(args[1], args[2])
        return
    if args[0] == "del":
        if len(args) < 2:
            print(
                "Usage: sync-base-nat.py sync nodes del <nodeId>",
                file=sys.stderr,
            )
            sys.exit(2)
        sync_nodes_del(args[1])
        return
    if args[0] == "sync-firewall":
        if len(args) != 1:
            print(
                "Usage: sync-base-nat.py sync nodes sync-firewall",
                file=sys.stderr,
            )
            sys.exit(2)
        sync_nodes_sync_firewall()
        return
    if len(args) != 1:
        print(
            "Usage: sync-base-nat.py sync nodes\n"
            "       sync-base-nat.py sync nodes <nodeId>\n"
            "       sync-base-nat.py sync nodes add <nodeId> <ipv6>\n"
            "       sync-base-nat.py sync nodes del <nodeId>\n"
            "       sync-base-nat.py sync nodes sync-firewall",
            file=sys.stderr,
        )
        sys.exit(2)
    sync_nodes_single(args[0])


def main():
    if len(sys.argv) < 2 or sys.argv[1] != "sync":
        print(
            "Usage: sync-base-nat.py sync\n"
            "       sync-base-nat.py sync <proxmoxId>\n"
            "       sync-base-nat.py sync <proxmoxId> <ipv6>\n"
            "       sync-base-nat.py sync <proxmoxId> del\n"
            "       sync-base-nat.py sync nodes ... (incl. sync-firewall)",
            file=sys.stderr,
        )
        sys.exit(2)
    if len(sys.argv) >= 3 and sys.argv[2] == "nodes":
        main_sync_nodes()
        return
    validate_config()
    args = sys.argv[2:]
    if not args:
        sync_full()
        return
    try:
        vmid = int(args[0])
    except ValueError:
        logger.error("proxmoxId must be int (or use: sync nodes <nodeId>)")
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

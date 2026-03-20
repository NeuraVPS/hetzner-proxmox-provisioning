#!/usr/bin/env python3
"""
sync-base-nat: BASE server ingress sync using nftables + nginx stream (no Jool).

Reads /etc/default/base-nat (shell KEY=value). Env overrides.

Commands:
  sync-base-nat.py sync
      Full sync: read Firestore "servers" (proxmoxId + ipv6), regenerate
      nftables + nginx stream config, validate, apply, reload nginx.

  sync-base-nat.py sync <proxmoxId>
      Single VM: read one server from Firestore and merge into local state,
      then rebuild/apply full generated config from that local desired map.

  sync-base-nat.py sync <proxmoxId> <publicVMIPv6>
      Single VM override without Firestore read for that VM.

  sync-base-nat.py sync <proxmoxId> del
      Remove one VM from local desired map, rebuild/apply config.

  sync-base-nat.py sync nodes
      PVE proxy: full sync from Firestore "proxmox_nodes" (doc id -> field ip),
      write nginx backend map, reload nginx.

  sync-base-nat.py sync nodes <nodeId>
  sync-base-nat.py sync nodes add <nodeId> <ipv6>
  sync-base-nat.py sync nodes del <nodeId>
  sync-base-nat.py sync nodes sync-firewall
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import resource
import subprocess
import sys
import tempfile
from pathlib import Path
from glob import glob

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


def clean_log_if_needed() -> bool:
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


def parse_bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def normalize_ipv4(addr: str) -> str:
    return str(ipaddress.IPv4Address(addr))


def normalize_ipv6(addr: str) -> str:
    return str(ipaddress.IPv6Address(addr))


def check_vmid(vmid: int) -> int:
    if not (0 <= vmid <= VMID_MAX):
        raise ValueError(f"proxmoxId/VMID {vmid} out of range [0, {VMID_MAX}]")
    return vmid


load_default_env()


MAIN_IPV4 = os.environ.get("MAIN_IPV4", "").strip()
MAIN_IPV6 = os.environ.get("MAIN_IPV6", "").strip()
FAILOVER_IPV4 = os.environ.get("FAILOVER_IPV4", "").strip()
FAILOVER_IPV6 = os.environ.get("FAILOVER_IPV6", "").strip()
SNAT_IPV6 = os.environ.get("SNAT_IPV6", MAIN_IPV6).strip()

FIREBASE_CREDENTIALS = os.environ.get(
    "FIREBASE_CREDENTIALS_FILE", "/etc/firebase-credentials.json"
)

SAMBA_PORT_BASE = int(os.environ.get("SAMBA_PORT_BASE", "10000"))
RDP_PORT_BASE = int(os.environ.get("RDP_PORT_BASE", "20000"))
VMID_MAX = int(os.environ.get("VMID_MAX", os.environ.get("PORT_MAX", "9999")))
SAMBA_PORT_END = SAMBA_PORT_BASE + VMID_MAX
RDP_PORT_END = RDP_PORT_BASE + VMID_MAX
SSH_PORT = int(os.environ.get("SSH_PORT", "22"))
INCLUDE_UDP_RDP = parse_bool_env("INCLUDE_UDP_RDP", True)
NGINX_PROXY_BUFFER_SIZE = (
    os.environ.get("NGINX_PROXY_BUFFER_SIZE", "128k").strip() or "128k"
)
NGINX_TCP_NODELAY = parse_bool_env("NGINX_TCP_NODELAY", True)
NGINX_MIN_WORKER_CONNECTIONS = int(
    os.environ.get("NGINX_MIN_WORKER_CONNECTIONS", "16384")
)
NGINX_WORKER_CONN_HEADROOM = int(os.environ.get("NGINX_WORKER_CONN_HEADROOM", "1024"))
NGINX_WORKER_RLIMIT_NOFILE = int(os.environ.get("NGINX_WORKER_RLIMIT_NOFILE", "262144"))
NGINX_TEST_NOFILE = int(os.environ.get("NGINX_TEST_NOFILE", "262144"))
NGINX_PVE_STUB_FILE = Path(
    os.environ.get(
        "NGINX_PVE_STUB_FILE",
        "/etc/nginx/conf.d/00-base-nat-pve-vars-stub.conf",
    )
)

STATE_FILE = Path(os.environ.get("STATE_FILE", str(DEFAULT_STATE_FILE)))
PVE_NODES_STATE_FILE = Path(
    os.environ.get("PVE_NODES_STATE_FILE", str(STATE_DIR / "pve_nodes.json"))
)
PVE_NGINX_MAP_FILE = Path(
    os.environ.get(
        "PVE_NGINX_MAP_FILE",
        "/etc/nginx/conf.d/pve-proxy-backends.map.conf",
    )
)

NFT_CONFIG_FILE = Path(
    os.environ.get("NFT_CONFIG_FILE", "/etc/nftables.base-nat.generated.conf")
)
NGINX_STREAM_FILE = Path(
    os.environ.get("NGINX_STREAM_FILE", "/etc/nginx/stream.d/base-nat.conf")
)
NFT_FILTER_TABLE_NAME = os.environ.get("NFT_FILTER_TABLE_NAME", "base_filter").strip()
NFT_NAT_TABLE_NAME = os.environ.get("NFT_NAT_TABLE_NAME", "base_nat6").strip()

# Storage Box: cluster.fw template (scp port 23). Override via env on BASE.
FIREWALL_STORAGE_USER = os.environ.get("FIREWALL_STORAGE_USER", "u560363").strip()
FIREWALL_STORAGE_HOST = os.environ.get(
    "FIREWALL_STORAGE_HOST", "u560363.your-storagebox.de"
).strip()
FIREWALL_REMOTE_PATH = os.environ.get(
    "FIREWALL_REMOTE_PATH", "/home/firewall/cluster.fw"
).strip()
FIREWALL_SCP_PORT = os.environ.get("FIREWALL_SCP_PORT", "23").strip()

FIREBASE_INITIALIZED = False


def validate_config():
    if not MAIN_IPV4 or not MAIN_IPV6:
        logger.error("MAIN_IPV4 and MAIN_IPV6 are required in /etc/default/base-nat")
        sys.exit(1)
    if not FAILOVER_IPV4 or not FAILOVER_IPV6:
        logger.error(
            "FAILOVER_IPV4 and FAILOVER_IPV6 are required in /etc/default/base-nat"
        )
        sys.exit(1)

    normalize_ipv4(MAIN_IPV4)
    normalize_ipv4(FAILOVER_IPV4)
    normalize_ipv6(MAIN_IPV6)
    normalize_ipv6(FAILOVER_IPV6)
    normalize_ipv6(SNAT_IPV6)

    if not (1 <= SSH_PORT <= 65535):
        logger.error("SSH_PORT must be in [1, 65535]")
        sys.exit(1)
    if SAMBA_PORT_BASE < 1 or RDP_PORT_BASE < 1:
        logger.error("SAMBA_PORT_BASE and RDP_PORT_BASE must be >= 1")
        sys.exit(1)
    if SAMBA_PORT_END > 65535 or RDP_PORT_END > 65535:
        logger.error(
            "Port range overflow: SMB=%s-%s RDP=%s-%s",
            SAMBA_PORT_BASE,
            SAMBA_PORT_END,
            RDP_PORT_BASE,
            RDP_PORT_END,
        )
        sys.exit(1)


def initialize_firebase() -> bool:
    global FIREBASE_INITIALIZED
    if not FIREBASE_AVAILABLE:
        return False
    if firebase_admin._apps:
        FIREBASE_INITIALIZED = True
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


def ensure_firebase() -> bool:
    if FIREBASE_INITIALIZED:
        return True
    if FIREBASE_AVAILABLE and firebase_admin._apps:
        return True
    return initialize_firebase()


def run(cmd: list[str], check: bool = True) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode,
            cmd,
            result.stdout,
            result.stderr,
        )
    return (result.stdout or "").strip()


def vm_ports(vmid: int) -> tuple[int, int]:
    vmid = check_vmid(vmid)
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
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def desired_from_state() -> dict[int, str]:
    """vmid -> ipv6 from STATE_FILE (on-box snapshot)."""
    out: dict[int, str] = {}
    raw = read_state()
    if not isinstance(raw, dict):
        return out
    for k, d in raw.items():
        try:
            vmid = int(k)
        except (TypeError, ValueError):
            continue
        if not isinstance(d, dict):
            continue
        ipv6 = d.get("ipv6")
        if not ipv6:
            continue
        s = str(ipv6).strip()
        try:
            normalize_ipv6(s)
        except ValueError:
            continue
        if 0 <= vmid <= VMID_MAX:
            out[vmid] = s
    return out


def ingress_ipv6_hosts() -> list[str]:
    hosts: list[str] = []
    for ip_s in (MAIN_IPV6, FAILOVER_IPV6):
        if not ip_s:
            continue
        norm = normalize_ipv6(ip_s)
        if norm not in hosts:
            hosts.append(norm)
    return hosts


def firestore_list_configured_servers() -> dict[int, str]:
    """proxmoxId -> ipv6 from Firestore servers collection."""
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
                "Skip server %s: proxmoxId %s outside range [0,%s]",
                doc.id,
                vmid,
                VMID_MAX,
            )
            continue
        s = str(ipv6).strip()
        try:
            out[vmid] = normalize_ipv6(s)
        except ValueError:
            logger.warning("Skip server %s: bad ipv6 %s", doc.id, s)
    return out


def firestore_ipv6_for_vmid(vmid: int) -> str | None:
    if not ensure_firebase():
        return None
    db = firestore.client()
    if FIELD_FILTER_AVAILABLE:
        query = db.collection("servers").where(
            filter=FieldFilter("proxmoxId", "==", vmid)
        )
    else:
        query = db.collection("servers").where("proxmoxId", "==", vmid)
    for doc in query.limit(2).stream():
        d = doc.to_dict() or {}
        ipv6 = d.get("ipv6")
        if not ipv6:
            continue
        s = str(ipv6).strip()
        try:
            return normalize_ipv6(s)
        except ValueError:
            return None
    return None


def tcp_server_block(
    *,
    listen_ip: str,
    listen_port: int,
    upstream_ipv6: str,
    upstream_port: int,
    connect_timeout: str = "1s",
    session_timeout: str = "1h",
    proxy_buffer_size: str = "128k",
    tcp_nodelay: bool = True,
) -> str:
    tcp_nodelay_value = "on" if tcp_nodelay else "off"
    return (
        "server {\n"
        f"    listen {listen_ip}:{listen_port} so_keepalive=on;\n"
        f"    proxy_pass [{upstream_ipv6}]:{upstream_port};\n\n"
        f"    proxy_connect_timeout {connect_timeout};\n"
        f"    proxy_timeout {session_timeout};\n\n"
        f"    proxy_buffer_size {proxy_buffer_size};\n"
        f"    tcp_nodelay {tcp_nodelay_value};\n"
        "    proxy_socket_keepalive on;\n"
        "}"
    )


def udp_server_block(
    *,
    listen_ip: str,
    listen_port: int,
    upstream_ipv6: str,
    upstream_port: int,
    session_timeout: str = "1h",
) -> str:
    return (
        "server {\n"
        f"    listen {listen_ip}:{listen_port} udp reuseport;\n"
        f"    proxy_pass [{upstream_ipv6}]:{upstream_port};\n\n"
        f"    proxy_timeout {session_timeout};\n"
        "}"
    )


def generate_nginx_stream_config(desired: dict[int, str]) -> str:
    main_ipv4 = normalize_ipv4(MAIN_IPV4)
    failover_ipv4 = normalize_ipv4(FAILOVER_IPV4)
    blocks: list[str] = []

    for vmid in sorted(desired.keys()):
        vm_ipv6 = normalize_ipv6(desired[vmid])
        samba_p, rdp_p = vm_ports(vmid)

        blocks.append(
            tcp_server_block(
                listen_ip=main_ipv4,
                listen_port=rdp_p,
                upstream_ipv6=vm_ipv6,
                upstream_port=3389,
                proxy_buffer_size=NGINX_PROXY_BUFFER_SIZE,
                tcp_nodelay=NGINX_TCP_NODELAY,
            )
        )
        blocks.append(
            tcp_server_block(
                listen_ip=failover_ipv4,
                listen_port=rdp_p,
                upstream_ipv6=vm_ipv6,
                upstream_port=3389,
                proxy_buffer_size=NGINX_PROXY_BUFFER_SIZE,
                tcp_nodelay=NGINX_TCP_NODELAY,
            )
        )
        if INCLUDE_UDP_RDP:
            blocks.append(
                udp_server_block(
                    listen_ip=main_ipv4,
                    listen_port=rdp_p,
                    upstream_ipv6=vm_ipv6,
                    upstream_port=3389,
                )
            )
            blocks.append(
                udp_server_block(
                    listen_ip=failover_ipv4,
                    listen_port=rdp_p,
                    upstream_ipv6=vm_ipv6,
                    upstream_port=3389,
                )
            )

        blocks.append(
            tcp_server_block(
                listen_ip=main_ipv4,
                listen_port=samba_p,
                upstream_ipv6=vm_ipv6,
                upstream_port=445,
                proxy_buffer_size=NGINX_PROXY_BUFFER_SIZE,
                tcp_nodelay=NGINX_TCP_NODELAY,
            )
        )
        blocks.append(
            tcp_server_block(
                listen_ip=failover_ipv4,
                listen_port=samba_p,
                upstream_ipv6=vm_ipv6,
                upstream_port=445,
                proxy_buffer_size=NGINX_PROXY_BUFFER_SIZE,
                tcp_nodelay=NGINX_TCP_NODELAY,
            )
        )

    header = (
        "# Generated automatically by sync-base-nat.py. Do not edit manually.\n"
        "# IPv4 ingress -> direct IPv6 VM upstreams.\n"
    )
    if not blocks:
        return header + "\n"
    return header + "\n" + "\n\n".join(blocks) + "\n"


def generate_nftables_config(desired: dict[int, str]) -> str:
    host_v6 = normalize_ipv6(MAIN_IPV6)
    failover_v6 = normalize_ipv6(FAILOVER_IPV6)
    snat_v6 = normalize_ipv6(SNAT_IPV6)

    published_tcp_ports = {SSH_PORT, 80, 443}
    published_udp_ports: set[int] = set()
    forward_rules: list[str] = []
    prerouting_rules: list[str] = []
    postrouting_rules: list[str] = []

    ingress_v6 = [host_v6]
    if failover_v6 != host_v6:
        ingress_v6.append(failover_v6)

    for vmid in sorted(desired.keys()):
        vm_ipv6 = normalize_ipv6(desired[vmid])
        samba_p, rdp_p = vm_ports(vmid)

        published_tcp_ports.update({samba_p, rdp_p})
        if INCLUDE_UDP_RDP:
            published_udp_ports.add(rdp_p)

        forward_rules.append(
            f"        ip6 daddr {vm_ipv6} tcp dport {{ 3389, 445 }} counter accept"
        )
        if INCLUDE_UDP_RDP:
            forward_rules.append(
                f"        ip6 daddr {vm_ipv6} udp dport 3389 counter accept"
            )

        for ingress in ingress_v6:
            prerouting_rules.append(
                f"        ip6 daddr {ingress} tcp dport {rdp_p} counter dnat to [{vm_ipv6}]:3389"
            )
            if INCLUDE_UDP_RDP:
                prerouting_rules.append(
                    f"        ip6 daddr {ingress} udp dport {rdp_p} counter dnat to [{vm_ipv6}]:3389"
                )
            prerouting_rules.append(
                f"        ip6 daddr {ingress} tcp dport {samba_p} counter dnat to [{vm_ipv6}]:445"
            )

        postrouting_rules.append(
            f"        ip6 daddr {vm_ipv6} tcp dport 3389 counter snat to {snat_v6}"
        )
        postrouting_rules.append(
            f"        ip6 daddr {vm_ipv6} tcp dport 445 counter snat to {snat_v6}"
        )
        if INCLUDE_UDP_RDP:
            postrouting_rules.append(
                f"        ip6 daddr {vm_ipv6} udp dport 3389 counter snat to {snat_v6}"
            )

    tcp_ports = ", ".join(str(p) for p in sorted(published_tcp_ports))
    if published_udp_ports:
        udp_ports = ", ".join(str(p) for p in sorted(published_udp_ports))
        udp_set_block = (
            "    set published_udp_ports {\n"
            "        type inet_service\n"
            f"        elements = {{ {udp_ports} }}\n"
            "    }\n"
        )
        udp_accept_rule = "        udp dport @published_udp_ports counter accept\n"
    else:
        udp_set_block = ""
        udp_accept_rule = ""

    return (
        "#!/usr/sbin/nft -f\n\n"
        "flush ruleset\n\n"
        f"table inet {NFT_FILTER_TABLE_NAME} {{\n"
        "    set published_tcp_ports {\n"
        "        type inet_service\n"
        f"        elements = {{ {tcp_ports} }}\n"
        "    }\n\n"
        f"{udp_set_block}"
        "    chain input {\n"
        "        type filter hook input priority filter; policy drop;\n\n"
        "        iifname \"lo\" accept\n"
        "        ct state established,related accept\n"
        "        ct state invalid drop\n\n"
        "        ip protocol icmp accept\n"
        "        ip6 nexthdr ipv6-icmp accept\n\n"
        "        tcp dport @published_tcp_ports counter accept\n"
        f"{udp_accept_rule}"
        "    }\n\n"
        "    chain forward {\n"
        "        type filter hook forward priority filter; policy drop;\n\n"
        "        ct state established,related accept\n"
        "        ct state invalid drop\n"
        f"{chr(10).join(forward_rules)}\n"
        "    }\n\n"
        "    chain output {\n"
        "        type filter hook output priority filter; policy accept;\n"
        "    }\n"
        "}\n\n"
        f"table ip6 {NFT_NAT_TABLE_NAME} {{\n"
        "    chain prerouting {\n"
        "        type nat hook prerouting priority dstnat; policy accept;\n\n"
        f"{chr(10).join(prerouting_rules)}\n"
        "    }\n\n"
        "    chain postrouting {\n"
        "        type nat hook postrouting priority srcnat; policy accept;\n\n"
        f"{chr(10).join(postrouting_rules)}\n"
        "    }\n"
        "}\n"
    )


def write_generated_configs(desired: dict[int, str]):
    normalized: dict[int, str] = {}
    for vmid, ipv6 in desired.items():
        normalized[check_vmid(vmid)] = normalize_ipv6(ipv6)

    nft_conf = generate_nftables_config(normalized)
    nginx_stream = generate_nginx_stream_config(normalized)

    NFT_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    NGINX_STREAM_FILE.parent.mkdir(parents=True, exist_ok=True)
    NFT_CONFIG_FILE.write_text(nft_conf)
    NGINX_STREAM_FILE.write_text(nginx_stream)
    logger.info("Wrote %s", NFT_CONFIG_FILE)
    logger.info("Wrote %s", NGINX_STREAM_FILE)


def ensure_nginx_capacity(required_listen_sockets: int):
    """
    Ensure nginx has enough capacity for many stream listeners.

    Nginx validates that worker_connections >= listening sockets.
    """
    nginx_conf = Path("/etc/nginx/nginx.conf")
    if not nginx_conf.is_file():
        logger.warning("nginx.conf not found at %s; skip capacity tuning", nginx_conf)
        return

    text = nginx_conf.read_text()

    required_connections = max(
        NGINX_MIN_WORKER_CONNECTIONS,
        required_listen_sockets + NGINX_WORKER_CONN_HEADROOM,
    )
    required_nofile = max(NGINX_WORKER_RLIMIT_NOFILE, required_connections * 2)

    changed = False

    wc_pattern = re.compile(r"(^\s*worker_connections\s+)(\d+)(\s*;)", re.MULTILINE)
    wc_match = wc_pattern.search(text)
    if wc_match:
        current_wc = int(wc_match.group(2))
        if current_wc < required_connections:
            text = wc_pattern.sub(
                rf"\g<1>{required_connections}\3",
                text,
                count=1,
            )
            changed = True
            logger.info(
                "Raised nginx worker_connections: %s -> %s",
                current_wc,
                required_connections,
            )
    else:
        logger.warning("worker_connections directive not found; skip tuning")

    wr_pattern = re.compile(r"(^\s*worker_rlimit_nofile\s+)(\d+)(\s*;)", re.MULTILINE)
    wr_match = wr_pattern.search(text)
    if wr_match:
        current_wr = int(wr_match.group(2))
        if current_wr < required_nofile:
            text = wr_pattern.sub(
                rf"\g<1>{required_nofile}\3",
                text,
                count=1,
            )
            changed = True
            logger.info(
                "Raised nginx worker_rlimit_nofile: %s -> %s",
                current_wr,
                required_nofile,
            )
    else:
        inject_after = re.compile(
            r"(^\s*worker_processes\s+\S+\s*;[^\n]*\n)",
            re.MULTILINE,
        )
        if inject_after.search(text):
            text = inject_after.sub(
                rf"\1worker_rlimit_nofile {required_nofile};\n",
                text,
                count=1,
            )
            changed = True
            logger.info("Inserted nginx worker_rlimit_nofile=%s", required_nofile)
        else:
            text = f"worker_rlimit_nofile {required_nofile};\n{text}"
            changed = True
            logger.info("Prepended nginx worker_rlimit_nofile=%s", required_nofile)

    if changed:
        nginx_conf.write_text(text)


def ensure_pve_node_var_stub():
    """
    Ensure $pve_node_from_host is defined for nginx -t on hosts that do not
    have the full PVE wildcard proxy snippets installed.
    """
    conf_paths = ["/etc/nginx/**/*.conf"]
    var_defined = False
    for pattern in conf_paths:
        for path in glob(pattern, recursive=True):
            p = Path(path)
            if not p.is_file():
                continue
            try:
                text = p.read_text()
            except Exception:
                continue
            if "map $host $pve_node_from_host" in text:
                var_defined = True
                break
        if var_defined:
            break

    if var_defined:
        if NGINX_PVE_STUB_FILE.exists():
            # Remove stale stub once real config exists to avoid duplicate map vars.
            try:
                NGINX_PVE_STUB_FILE.unlink()
            except OSError:
                pass
        return

    NGINX_PVE_STUB_FILE.parent.mkdir(parents=True, exist_ok=True)
    NGINX_PVE_STUB_FILE.write_text(
        "# generated by sync-base-nat.py to satisfy optional PVE vars\n"
        "map $host $pve_node_from_host {\n"
        '    default "";\n'
        "}\n"
    )


def ensure_process_nofile_limit(min_limit: int):
    """Raise current process RLIMIT_NOFILE so nginx -t can open many sockets."""
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (ValueError, OSError):
        return

    target = max(min_limit, NGINX_WORKER_RLIMIT_NOFILE)
    if soft >= target:
        return

    new_soft = min(target, hard)
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (new_soft, hard))
        logger.info("Raised process RLIMIT_NOFILE: %s -> %s (hard=%s)", soft, new_soft, hard)
    except (ValueError, OSError) as e:
        logger.warning("Could not raise RLIMIT_NOFILE to %s: %s", target, e)


def apply_generated_configs(desired: dict[int, str]):
    write_generated_configs(desired)
    stream_conf = NGINX_STREAM_FILE.read_text() if NGINX_STREAM_FILE.is_file() else ""
    required_listeners = sum(
        1 for line in stream_conf.splitlines() if line.strip().startswith("listen ")
    )
    ensure_nginx_capacity(required_listeners)
    ensure_pve_node_var_stub()
    ensure_process_nofile_limit(max(NGINX_TEST_NOFILE, required_listeners + 1024))

    try:
        run(["nginx", "-t"], check=True)
    except FileNotFoundError:
        logger.error("nginx binary not found")
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        logger.error("nginx -t failed: %s", (e.stderr or e.stdout or "").strip())
        sys.exit(1)

    try:
        run(["nft", "-f", str(NFT_CONFIG_FILE)], check=True)
    except FileNotFoundError:
        logger.error("nft binary not found")
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        logger.error("nft apply failed: %s", (e.stderr or e.stdout or "").strip())
        sys.exit(1)

    reload_result = subprocess.run(
        ["systemctl", "reload", "nginx"],
        capture_output=True,
        text=True,
        check=False,
    )
    if reload_result.returncode != 0:
        logger.warning(
            "systemctl reload nginx failed: %s",
            (reload_result.stderr or reload_result.stdout or "").strip(),
        )
    else:
        logger.info("nginx reloaded")

    logger.info("Applied generated nft + nginx stream config (%d VMs)", len(desired))


def sync_full():
    desired = firestore_list_configured_servers()
    apply_generated_configs(desired)
    write_state(
        {
            str(k): {
                "ipv6": desired[k],
                "samba": SAMBA_PORT_BASE + k,
                "rdp": RDP_PORT_BASE + k,
            }
            for k in sorted(desired.keys())
        }
    )
    logger.info("Full sync done (%d servers)", len(desired))


def sync_single_vmid(
    vmid: int,
    ipv6: str | None,
    delete_only: bool,
    ipv6_override: bool = False,
):
    if not delete_only and not ipv6_override and not ensure_firebase():
        logger.error("Firebase required for sync <proxmoxId> (lookup)")
        sys.exit(1)
    if ipv6_override and ipv6 is None:
        logger.error("Explicit IPv6 required for sync <proxmoxId> <ipv6>")
        sys.exit(2)

    desired = desired_from_state()
    if delete_only:
        desired.pop(vmid, None)
        logger.info("proxmoxId=%s removed from desired map", vmid)
    elif ipv6_override:
        desired[vmid] = normalize_ipv6(ipv6 or "")
    elif ipv6 is not None:
        desired[vmid] = normalize_ipv6(ipv6)
    else:
        desired.pop(vmid, None)
        logger.info("proxmoxId=%s not in Firestore; removed from desired map", vmid)

    apply_generated_configs(desired)
    write_state(
        {
            str(k): {
                "ipv6": desired[k],
                "samba": SAMBA_PORT_BASE + k,
                "rdp": RDP_PORT_BASE + k,
            }
            for k in sorted(desired.keys())
        }
    )
    logger.info("Sync proxmoxId=%s done (desired=%d)", vmid, len(desired))


# --- PVE proxy: proxmox_nodes -> nginx map --------------------------------------------


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
        "# generated by sync-base-nat.py - do not edit",
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

    reload_result = subprocess.run(
        ["systemctl", "reload", "nginx"],
        capture_output=True,
        text=True,
        check=False,
    )
    if reload_result.returncode != 0:
        logger.warning(
            "systemctl reload nginx: %s",
            (reload_result.stderr or reload_result.stdout or "failed").strip(),
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
        pull = subprocess.run(
            ["scp"] + scp_args + [remote, str(tmp)],
            capture_output=True,
            text=True,
            check=False,
        )
        if pull.returncode != 0:
            logger.error(
                "sync-firewall: scp pull failed: %s",
                (pull.stderr or pull.stdout or "").strip(),
            )
            sys.exit(1)

        text = tmp.read_text()
        try:
            new_text = replace_hosts_ipv6_section(text, nodes)
        except ValueError as e:
            logger.error("sync-firewall: %s", e)
            sys.exit(1)
        tmp.write_text(new_text)

        push = subprocess.run(
            ["scp"] + scp_args + [str(tmp), remote],
            capture_output=True,
            text=True,
            check=False,
        )
        if push.returncode != 0:
            logger.error(
                "sync-firewall: scp push failed: %s",
                (push.stderr or push.stdout or "").strip(),
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
        "/etc/pve/firewall/cluster.fw && pve-firewall restart"
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
        push_node = subprocess.run(ssh_cmd, capture_output=True, text=True, check=False)
        if push_node.returncode != 0:
            err = (push_node.stderr or push_node.stdout or "").strip()
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

    is_del = len(args) >= 2 and args[1].lower() == "del"
    if is_del:
        sync_single_vmid(vmid, None, delete_only=True)
        return

    if len(args) >= 2:
        ipv6 = args[1].strip()
        try:
            normalize_ipv6(ipv6)
        except ValueError:
            logger.error("Invalid IPv6")
            sys.exit(2)
        sync_single_vmid(vmid, ipv6, delete_only=False, ipv6_override=True)
        return

    ipv6 = firestore_ipv6_for_vmid(vmid)
    sync_single_vmid(vmid, ipv6, delete_only=False, ipv6_override=False)


if __name__ == "__main__":
    main()

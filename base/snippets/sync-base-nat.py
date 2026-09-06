#!/usr/bin/env python3
"""
sync-base-nat: BASE dynamic forwarding sync for Jool+nftables.

Reads /etc/default/base-nat (shell KEY=value). Env overrides.

Commands:
  sync-base-nat.py sync
      Full sync: read Firestore "servers" (proxmoxId + ipv6), then
      reconcile managed dynamic DNAT rules in nftables ip6 nat prerouting.

  sync-base-nat.py sync <proxmoxId>
      Single VM: read one server from Firestore and merge into local state,
      then reconcile managed dynamic DNAT rules.

  sync-base-nat.py sync <proxmoxId> <publicVMIPv6> [rdp=0|1] [samba=0|1] [ssh=0|1]
                                                  [internet=0|1]
      Single VM override without Firestore read. Optional rdp/samba/ssh/internet
      flags force the matching service on/off; flags not passed are
      preserved from on-disk state (or default enabled for new VMs).

  sync-base-nat.py sync <proxmoxId> del
      Remove one VM from local desired map, reconcile rules.

  sync-base-nat.py sync nodes
      PVE proxy: full sync from Firestore "proxmox_nodes" (doc id -> field ip),
      write nginx backend map, reload nginx.

  sync-base-nat.py sync nodes <nodeId>
  sync-base-nat.py sync nodes add <nodeId> <ipv6>
  sync-base-nat.py sync nodes del <nodeId>
  sync-base-nat.py sync nodes sync-firewall
"""
from __future__ import annotations

import contextlib
import fcntl
import ipaddress
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import functools
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
LOCK_FILE = STATE_DIR / ".sync.lock"


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


FIREBASE_CREDENTIALS = os.environ.get(
    "FIREBASE_CREDENTIALS_FILE", "/etc/firebase-credentials.json"
)

SAMBA_PORT_BASE = int(os.environ.get("SAMBA_PORT_BASE", "10000"))
RDP_PORT_BASE = int(os.environ.get("RDP_PORT_BASE", "20000"))
SSH_PORT_BASE = int(os.environ.get("SSH_PORT_BASE", "30000"))
VMID_MAX = int(os.environ.get("VMID_MAX", os.environ.get("PORT_MAX", "9999")))
SAMBA_PORT_END = SAMBA_PORT_BASE + VMID_MAX
RDP_PORT_END = RDP_PORT_BASE + VMID_MAX
SSH_PORT_END = SSH_PORT_BASE + VMID_MAX
INCLUDE_UDP_RDP = parse_bool_env("INCLUDE_UDP_RDP", True)

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
HOSTS_FILE = Path(os.environ.get("HOSTS_FILE", "/etc/hosts"))
HOSTS_BLOCK_BEGIN = "# BEGIN sync-base-nat (managed; do not edit)"
HOSTS_BLOCK_END = "# END sync-base-nat"
NFT_INCLUDE_FILE = Path(
    os.environ.get(
        "NFT_INCLUDE_FILE",
        "/etc/nftables.d/base-nat-elements.nft",
    )
)

NFT_DNAT_FAMILY = os.environ.get("NFT_DNAT_FAMILY", "ip6").strip()
NFT_DNAT_TABLE = os.environ.get("NFT_DNAT_TABLE", "nat").strip()
NFT_DNAT_CHAIN = os.environ.get("NFT_DNAT_CHAIN", "prerouting").strip()
MANAGED_RULE_COMMENT_PREFIX = os.environ.get(
    "NFT_MANAGED_RULE_COMMENT_PREFIX", "sync-base-nat"
).strip()

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
    if not NFT_DNAT_FAMILY or not NFT_DNAT_TABLE or not NFT_DNAT_CHAIN:
        logger.error("NFT_DNAT_FAMILY, NFT_DNAT_TABLE and NFT_DNAT_CHAIN are required")
        sys.exit(1)
    if not MANAGED_RULE_COMMENT_PREFIX:
        logger.error("NFT_MANAGED_RULE_COMMENT_PREFIX cannot be empty")
        sys.exit(1)
    if SAMBA_PORT_BASE < 1 or RDP_PORT_BASE < 1 or SSH_PORT_BASE < 1:
        logger.error("SAMBA_PORT_BASE, RDP_PORT_BASE and SSH_PORT_BASE must be >= 1")
        sys.exit(1)
    if SAMBA_PORT_END > 65535 or RDP_PORT_END > 65535 or SSH_PORT_END > 65535:
        logger.error(
            "Port range overflow: SMB=%s-%s RDP=%s-%s SSH=%s-%s",
            SAMBA_PORT_BASE,
            SAMBA_PORT_END,
            RDP_PORT_BASE,
            RDP_PORT_END,
            SSH_PORT_BASE,
            SSH_PORT_END,
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


def vm_ports(vmid: int) -> tuple[int, int, int]:
    vmid = check_vmid(vmid)
    return SAMBA_PORT_BASE + vmid, RDP_PORT_BASE + vmid, SSH_PORT_BASE + vmid


@contextlib.contextmanager
def state_lock():
    """Serialize the read-modify-write of STATE_FILE + nft maps across
    concurrent sync-base-nat invocations.

    Without this, two processes (e.g. a Firestore-trigger SSH from the
    Cloud Function plus a migrate_vm.sh sync, or two back-to-back
    nat64_sync_trigger_handler del+create calls) can interleave their
    read of state.json with the other's non-atomic truncate-and-write.
    The reader then parses an empty/partial file, falls back to an empty
    desired map, flushes every nft map, and rewrites state.json with
    *only the VM it was processing* — wiping every unrelated client's
    DNAT until the next full sync. The lock is held for the whole
    reconcile (read + nft transaction + write_state + include file)
    because all four operations share the same desired snapshot.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def read_state() -> dict:
    if not STATE_FILE.is_file():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception as e:
        # state.json exists but is unreadable/unparseable. Treating this
        # as "empty" is dangerous: the caller would flush every nft map
        # and write back a single-VM state, breaking every unrelated VM.
        # Bail out so the operator (or systemd Restart=) sees the failure
        # and the include file from §7 / a subsequent full sync recovers.
        logger.error("State read failed (refusing to proceed): %s", e)
        sys.exit(1)


def write_state(state: dict):
    """Atomic state file write.

    Non-atomic write_text leaves a window where a concurrent reader sees
    an empty file (open(..., 'w') truncates before the new contents land),
    which read_state used to silently treat as `{}`. Even with the lock
    above, atomicity protects any out-of-band reader that doesn't take
    the lock (`cat`, monitoring scripts, jq queries during incident
    response) from observing a torn file.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, indent=2, sort_keys=True) + "\n"
    tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    tmp.write_text(payload)
    os.replace(tmp, STATE_FILE)


# Per-VM desired entry: ipv6 + which services BASE should redirect.
# Defaults: rdp/samba/ssh enabled when the field is missing (back-compat
# with pre-firewall-flag state files and Firestore docs).
def _server_entry(
    ipv6: str,
    rdp: bool = True,
    samba: bool = True,
    ssh: bool = True,
    node_id: str = "",
    ipv4: str = "",
    internet: bool = True,
) -> dict:
    return {
        "ipv6": ipv6,
        "rdp": bool(rdp),
        "samba": bool(samba),
        "ssh": bool(ssh),
        # `internet` False = la VM no sale a Internet. Antes lo hacía el grupo
        # `vm-no-internet` del firewall POR VM del nodo; ahora lo hace la base,
        # que es la única pieza que sigue en pie con el nodo caído.
        "internet": bool(internet),
        # Modelo nuevo (IDENT): la dirección del invitado ya no se deriva del
        # nodo, así que hace falta saber DÓNDE está para enrutar el /128 al
        # túnel correcto. En el modelo viejo estos campos se ignoran.
        "nodeId": str(node_id or ""),
        "ipv4": str(ipv4 or ""),
    }


# ---------------------------------------------------------------------------
# Rutas por VM del modelo nuevo (IDENT)
# ---------------------------------------------------------------------------
# Con el direccionamiento independiente del nodo, la dirección del invitado NO
# dice dónde está; lo dice `nodeId`. Así que además del mapa de puertos, la base
# mantiene una ruta por VM hacia el túnel del nodo que la aloja. Eso es lo único
# que se mueve en una migración, y es lo que sustituye a entrar en Windows.
#
# IDENT_PREFIX vacío = función DESACTIVADA: nada cambia para el modelo viejo.
# Ver base/docs/egress-failover-e-ipv6-estable.md §4.3 / §13.
IDENT_PREFIX = (os.environ.get("IDENT_PREFIX") or "").strip()
TUNNEL_IFACE_PREFIX = os.environ.get("TUNNEL_IFACE_PREFIX", "tun-")
# Trozo del /64 de identidad reservado al tránsito de los túneles. NO son VMs y
# este script NUNCA debe tocar sus rutas (ver _managed_route_dst).
TRANSIT_PREFIX = os.environ.get("TRANSIT_PREFIX", "2a01:4f9:c01f:e:ffff::/112")
# Rango de las IPv4 privadas de las VMs. El de los nodos (10.65/16) es aparte y
# no se gestiona desde aquí.
VM_V4_PREFIX = os.environ.get("VM_V4_PREFIX", "10.64.0.0/16")

# --- tuneles base<->nodo: tabla de nodos generada -----------------------------
# El lado BASE de los tuneles (neuravps-base-tunnels.sh) lee esta tabla. Se
# genera aqui y NO se toca a mano: mantenerla en las dos bases era el ultimo
# paso manual para convertir un nodo, y un fichero copiado a mano en dos sitios
# diverge tarde o temprano.
TUNNEL_NODES_FILE = Path(
    os.environ.get("TUNNEL_NODES_FILE", "/etc/neuravps/tunnel-nodes.conf")
)
TUNNEL_UNIT = os.environ.get("TUNNEL_UNIT", "neuravps-base-tunnels.service")
# El /112 de transito da 4096 huecos de 16 direcciones; el hueco 0 esta
# RESERVADO para las direcciones canonicas de las bases.
TUNNEL_SLOT_MIN, TUNNEL_SLOT_MAX = 1, 4095




def _ident_network():
    """La red IDENT como objeto, o None si la función está desactivada."""
    if not IDENT_PREFIX:
        return None
    try:
        return ipaddress.IPv6Network(IDENT_PREFIX, strict=False)
    except ValueError:
        logger.error("IDENT_PREFIX inválido: %r — rutas por VM DESACTIVADAS", IDENT_PREFIX)
        return None


def _is_ident(ipv6: str) -> bool:
    net = _ident_network()
    if net is None:
        return False
    try:
        return ipaddress.IPv6Address(str(ipv6).strip()) in net
    except ValueError:
        return False


def tunnel_iface_for_node(node_id: str) -> str | None:
    """`0000228-AX162-2-LTD` -> `tun-p228`. None si no se puede derivar."""
    alias = node_short_alias(node_id or "")
    return f"{TUNNEL_IFACE_PREFIX}{alias}" if alias else None


def _iface_exists(iface: str) -> bool:
    return Path(f"/sys/class/net/{iface}").exists()


def reconcile_vm_routes(desired: dict[int, dict]) -> None:
    """Rutas /128 (y /32 de la IPv4 privada) hacia el túnel del nodo de cada VM.

    Sólo actúa sobre VMs cuya IPv6 cae dentro de IDENT_PREFIX, así que durante
    el despliegue las del modelo viejo pasan intactas: su dirección vive en el
    /64 del nodo y se enruta nativamente, sin ruta ni túnel.

    Reconcilia de verdad — instala lo que falta y RETIRA lo que sobra — para que
    una migración deje de apuntar al túnel del nodo anterior.
    """
    if _ident_network() is None:
        return

    quieren: dict[str, str] = {}   # destino -> interfaz
    sin_tunel: list[int] = []
    for vmid, ent in desired.items():
        ipv6 = (ent.get("ipv6") or "").strip()
        if not _is_ident(ipv6):
            continue
        iface = tunnel_iface_for_node(ent.get("nodeId") or "")
        if not iface or not _iface_exists(iface):
            sin_tunel.append(vmid)
            continue
        quieren[f"{ipv6}/128"] = iface
        ipv4 = (ent.get("ipv4") or "").strip()
        if ipv4:
            quieren[f"{ipv4}/32"] = iface

    if sin_tunel:
        # No es fatal: la VM sigue alcanzable por el camino viejo si lo tiene.
        # Pero es exactamente lo que hay que mirar si una VM del modelo nuevo
        # deja de responder tras una migración.
        logger.warning(
            "rutas: %d VM(s) del modelo nuevo sin túnel en esta base (nodo sin "
            "preparar o interfaz ausente): %s",
            len(sin_tunel), sorted(sin_tunel)[:20],
        )

    for familia, flag in (("-6", "inet6"), ("-4", "inet")):
        # Sólo se tocan rutas cuyo destino cae en los prefijos gestionados y
        # cuya salida es un túnel: cualquier otra cosa de la tabla es ajena.
        try:
            actuales_raw = run(["ip", familia, "route", "show"], check=False)
        except Exception:  # pylint: disable=broad-except
            logger.exception("rutas: no se pudo leer la tabla %s", flag)
            continue
        actuales: dict[str, str] = {}
        for line in actuales_raw.splitlines():
            parts = line.split()
            if len(parts) < 3 or "dev" not in parts:
                continue
            dst = parts[0]
            iface = parts[parts.index("dev") + 1]
            if not iface.startswith(TUNNEL_IFACE_PREFIX):
                continue
            if "/" not in dst:
                dst = f"{dst}/128" if familia == "-6" else f"{dst}/32"
            actuales[dst] = iface

        esperadas = {d: i for d, i in quieren.items()
                     if (":" in d) == (familia == "-6")}

        for dst, iface in sorted(esperadas.items()):
            if actuales.get(dst) != iface:
                run(["ip", familia, "route", "replace", dst, "dev", iface], check=False)
                logger.info("ruta %s -> %s", dst, iface)
        for dst, iface in sorted(actuales.items()):
            if dst in esperadas:
                continue
            # Sólo se retira lo que está DENTRO de los prefijos gestionados: el
            # tránsito de los túneles y las rutas del propio nodo no se tocan.
            if not _managed_route_dst(dst):
                continue
            run(["ip", familia, "route", "del", dst, "dev", iface], check=False)
            logger.info("ruta RETIRADA %s (estaba en %s)", dst, iface)


def _transit_network():
    """Rango de tránsito de los túneles. Vive DENTRO del /64 de identidad."""
    try:
        return ipaddress.IPv6Network(TRANSIT_PREFIX, strict=False)
    except ValueError:
        return None


def _managed_route_dst(dst: str) -> bool:
    """True si el destino es una VM del modelo nuevo (y por tanto retirable).

    ⚠️ El tránsito de los túneles vive DENTRO del /64 de identidad, así que sin
    excluirlo explícitamente este reconciliador BORRARÍA las rutas de los
    propios túneles y dejaría la base sin camino a ningún nodo. Misma trampa que
    la regla de política del nodo (§13): el prefijo de identidad no es
    homogéneo, tiene un trozo reservado para infraestructura.
    """
    addr = dst.split("/", 1)[0]
    if ":" in addr:
        if not _is_ident(addr):
            return False
        tnet = _transit_network()
        try:
            if tnet is not None and ipaddress.IPv6Address(addr) in tnet:
                return False        # tránsito: NUNCA se toca
        except ValueError:
            return False
        return True
    try:
        # Sólo el rango de VMs. El de los nodos (10.65/16) no se gestiona aquí.
        return ipaddress.IPv4Address(addr) in ipaddress.IPv4Network(VM_V4_PREFIX)
    except ValueError:
        return False


def _firewall_flag(firewall: object, key: str, default: bool = True) -> bool:
    if not isinstance(firewall, dict):
        return default
    val = firewall.get(key)
    if not isinstance(val, bool):
        return default
    return val


def desired_from_state() -> dict[int, dict]:
    """vmid -> {ipv6, rdp, samba} from STATE_FILE (on-box snapshot).

    Reads `rdpEnabled` / `sambaEnabled` flags. Older state files written
    before per-service flags existed only carry `ipv6` plus the `rdp` /
    `samba` port numbers — for those, we default both flags to True.
    """
    out: dict[int, dict] = {}
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
        if not (0 <= vmid <= VMID_MAX):
            continue
        rdp_flag = d.get("rdpEnabled", True)
        samba_flag = d.get("sambaEnabled", True)
        ssh_flag = d.get("sshEnabled", True)
        net_flag = d.get("internetEnabled", True)
        out[vmid] = _server_entry(
            s,
            rdp=bool(rdp_flag) if isinstance(rdp_flag, bool) else True,
            samba=bool(samba_flag) if isinstance(samba_flag, bool) else True,
            ssh=bool(ssh_flag) if isinstance(ssh_flag, bool) else True,
            node_id=d.get("nodeId") or "",
            ipv4=d.get("ipv4") or "",
            # Un fichero de estado viejo no trae el campo. Por defecto CON
            # Internet: equivocarse hacia "abierto" deja a un cliente con red
            # de más; hacia "cerrado" lo deja sin ella y sin saber por qué.
            internet=bool(net_flag) if isinstance(net_flag, bool) else True,
        )
    return out


def firestore_list_configured_servers() -> dict[int, dict]:
    """proxmoxId -> {ipv6, rdp, samba} from Firestore servers collection.

    `rdp`/`samba` come from the server's `firewall.rdpEnabled` /
    `firewall.sambaEnabled` boolean fields and default to True when
    missing or non-boolean.
    """
    if not ensure_firebase():
        logger.error("Firebase required for full sync")
        sys.exit(1)
    db = firestore.client()
    out: dict[int, dict] = {}
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
            normalized = normalize_ipv6(s)
        except ValueError:
            logger.warning("Skip server %s: bad ipv6 %s", doc.id, s)
            continue
        firewall = d.get("firewall")
        out[vmid] = _server_entry(
            normalized,
            rdp=_firewall_flag(firewall, "rdpEnabled", True),
            samba=_firewall_flag(firewall, "sambaEnabled", True),
            ssh=_firewall_flag(firewall, "sshEnabled", True),
            internet=_firewall_flag(firewall, "internetEnabled", True),
            node_id=d.get("nodeId") or "",
            ipv4=d.get("ipv4") or "",
        )
    return out


def firestore_server_for_vmid(vmid: int) -> dict | None:
    """Return {ipv6, rdp, samba} for a single Firestore server, or None."""
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
            normalized = normalize_ipv6(s)
        except ValueError:
            return None
        firewall = d.get("firewall")
        return _server_entry(
            normalized,
            rdp=_firewall_flag(firewall, "rdpEnabled", True),
            samba=_firewall_flag(firewall, "sambaEnabled", True),
            ssh=_firewall_flag(firewall, "sshEnabled", True),
            internet=_firewall_flag(firewall, "internetEnabled", True),
            # Sin estos dos, una VM del modelo nuevo sincronizada de una en una
            # se queda sin nodo conocido y su ruta /128 no se puede mover. El
            # cargador masivo si los pasaba; este se habia quedado atras.
            node_id=d.get("nodeId") or "",
            ipv4=d.get("ipv4") or "",
        )
    return None


# --- Dynamic DNAT via nftables maps -------------------------------------------
#
# The NAT chain contains four persistent rules declared in
# /etc/nftables.conf (see base/docs/netns-jool-nat46-nat66-guide.md §7):
#
#   tcp dport @rdp_tcp_map dnat ip6 to tcp dport map @rdp_tcp_map
#   udp dport @rdp_udp_map dnat ip6 to udp dport map @rdp_udp_map
#   tcp dport @smb_tcp_map dnat ip6 to tcp dport map @smb_tcp_map
#   tcp dport @ssh_tcp_map dnat ip6 to tcp dport map @ssh_tcp_map
#
# This script only populates the ELEMENTS of those maps; structure stays
# in nftables.conf so it survives reboots and full rule reloads. Each
# reconcile is applied as a single atomic `nft -f -` transaction.

DNAT_MAP_RDP_TCP = "rdp_tcp_map"
DNAT_MAP_RDP_UDP = "rdp_udp_map"
DNAT_MAP_SMB_TCP = "smb_tcp_map"
DNAT_MAP_SSH_TCP = "ssh_tcp_map"
DNAT_MAPS = (DNAT_MAP_RDP_TCP, DNAT_MAP_RDP_UDP, DNAT_MAP_SMB_TCP, DNAT_MAP_SSH_TCP)

RDP_TARGET_PORT = 3389
SMB_TARGET_PORT = 445
SSH_TARGET_PORT = 22


def _build_desired_map_elements(
    desired: dict[int, dict],
) -> dict[str, dict[int, tuple[str, int]]]:
    """Build {map_name: {external_port: (target_ipv6, target_port)}}.

    A VM only contributes RDP and/or SMB elements when the matching
    firewall flag in `desired[vmid]` is True. Disabled services are
    omitted entirely so BASE drops the connection (no DNAT match in
    prerouting => no entry in the forward chain's `ct status dnat`
    accept rule => packet hits the default drop policy).
    """
    out: dict[str, dict[int, tuple[str, int]]] = {m: {} for m in DNAT_MAPS}
    for vmid in sorted(desired.keys()):
        vmid = check_vmid(vmid)
        entry = desired[vmid]
        target_ipv6 = normalize_ipv6(entry["ipv6"])
        samba_p, rdp_p, ssh_p = vm_ports(vmid)
        if entry.get("rdp", True):
            out[DNAT_MAP_RDP_TCP][rdp_p] = (target_ipv6, RDP_TARGET_PORT)
            if INCLUDE_UDP_RDP:
                out[DNAT_MAP_RDP_UDP][rdp_p] = (target_ipv6, RDP_TARGET_PORT)
        if entry.get("samba", True):
            out[DNAT_MAP_SMB_TCP][samba_p] = (target_ipv6, SMB_TARGET_PORT)
        if entry.get("ssh", True):
            out[DNAT_MAP_SSH_TCP][ssh_p] = (target_ipv6, SSH_TARGET_PORT)
    return out


def _map_exists(map_name: str) -> bool:
    result = subprocess.run(
        ["nft", "list", "map", NFT_DNAT_FAMILY, NFT_DNAT_TABLE, map_name],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _ensure_maps_exist():
    try:
        missing = [m for m in DNAT_MAPS if not _map_exists(m)]
    except FileNotFoundError:
        logger.error("nft binary not found")
        sys.exit(1)
    if not missing:
        return
    logger.error(
        "Missing nftables map(s) in %s/%s: %s. Declare them in "
        "/etc/nftables.conf (see netns-jool-nat46-nat66-guide.md §7) and "
        "reload nftables before running sync.",
        NFT_DNAT_FAMILY,
        NFT_DNAT_TABLE,
        ", ".join(missing),
    )
    sys.exit(1)


def _apply_nft_transaction(lines: list[str]):
    if not lines:
        return
    payload = "\n".join(lines) + "\n"
    result = subprocess.run(
        ["nft", "-f", "-"],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        logger.error(
            "nft transaction failed: %s",
            (result.stderr or result.stdout or "").strip(),
        )
        logger.error("Transaction was:\n%s", payload)
        sys.exit(1)


def _cleanup_legacy_managed_rules() -> int:
    """Remove per-rule managed DNAT entries from the pre-map version.

    Earlier versions added one nft rule per VM/service/protocol in
    `ip6 nat prerouting` tagged with a comment starting with
    ``MANAGED_RULE_COMMENT_PREFIX``. The map-based design makes those
    obsolete; this function drops them on first run after upgrade.
    """
    result = subprocess.run(
        [
            "nft",
            "-a",
            "list",
            "chain",
            NFT_DNAT_FAMILY,
            NFT_DNAT_TABLE,
            NFT_DNAT_CHAIN,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return 0
    handles: list[int] = []
    for line in (result.stdout or "").splitlines():
        if MANAGED_RULE_COMMENT_PREFIX not in line:
            continue
        m = re.search(r"# handle (\d+)\s*$", line.strip())
        if m:
            handles.append(int(m.group(1)))
    if not handles:
        return 0
    logger.info("Removing %d legacy per-rule managed DNAT entries", len(handles))
    removed = 0
    for handle in sorted(handles, reverse=True):
        delete_result = subprocess.run(
            [
                "nft",
                "delete",
                "rule",
                NFT_DNAT_FAMILY,
                NFT_DNAT_TABLE,
                NFT_DNAT_CHAIN,
                "handle",
                str(handle),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if delete_result.returncode == 0:
            removed += 1
        else:
            logger.warning(
                "Failed deleting legacy handle %s: %s",
                handle,
                (delete_result.stderr or "").strip(),
            )
    return removed


def _write_include_file(lines: list[str]):
    """Persist the same elements to NFT_INCLUDE_FILE so any nftables reload
    (boot, manual, package) restores DNAT state without needing Firestore.
    The companion include directive lives inside `table ip6 nat` in
    /etc/nftables.conf (see netns-jool-nat46-nat66-guide.md §7)."""
    try:
        NFT_INCLUDE_FILE.parent.mkdir(parents=True, exist_ok=True)
        body = "# generated by sync-base-nat.py - do not edit\n"
        body += "\n".join(lines) + ("\n" if lines else "")
        tmp = NFT_INCLUDE_FILE.with_suffix(NFT_INCLUDE_FILE.suffix + ".tmp")
        tmp.write_text(body)
        os.replace(tmp, NFT_INCLUDE_FILE)
    except OSError as e:
        logger.warning("Failed writing nft include file %s: %s", NFT_INCLUDE_FILE, e)


def reconcile_dynamic_dnat_rules(desired: dict[int, str]):
    _ensure_maps_exist()
    _cleanup_legacy_managed_rules()

    desired_by_map = _build_desired_map_elements(desired)

    flush_lines: list[str] = []
    element_lines: list[str] = []
    for map_name in DNAT_MAPS:
        flush_lines.append(f"flush map {NFT_DNAT_FAMILY} {NFT_DNAT_TABLE} {map_name}")
        for ext_port in sorted(desired_by_map[map_name].keys()):
            ipv6, tgt_port = desired_by_map[map_name][ext_port]
            element_lines.append(
                f"add element {NFT_DNAT_FAMILY} {NFT_DNAT_TABLE} {map_name} "
                f"{{ {ext_port} : {ipv6} . {tgt_port} }}"
            )

    _apply_nft_transaction(flush_lines + element_lines)
    _write_include_file(element_lines)

    logger.info(
        "Dynamic DNAT reconcile (maps): rdp_tcp=%d rdp_udp=%d smb_tcp=%d ssh_tcp=%d",
        len(desired_by_map[DNAT_MAP_RDP_TCP]),
        len(desired_by_map[DNAT_MAP_RDP_UDP]),
        len(desired_by_map[DNAT_MAP_SMB_TCP]),
        len(desired_by_map[DNAT_MAP_SSH_TCP]),
    )


# --- vm-no-internet servido desde la BASE -------------------------------------
#
# Antes esto vivía en el firewall POR VM del nodo (grupo `vm-no-internet` de
# cluster.fw). Traerlo aquí no es orden: es que el interruptor sigue
# funcionando CON EL NODO CAÍDO. Antes, con el nodo apagado no había forma de
# cortarle Internet a una VM, y al volver arrancaba con Internet antes de que
# nadie pudiera reaccionar — justo el caso malo si dentro hay un robot.
#
# Las reglas que consumen estos sets están en /etc/nftables.conf, al principio
# de `chain forward`, y distinguen la salida de la VM (ct direction original,
# se corta) de la respuesta a un RDP entrante (reply, se deja pasar).
NFT_FILTER_FAMILY = os.environ.get("NFT_FILTER_FAMILY", "inet")
NFT_FILTER_TABLE = os.environ.get("NFT_FILTER_TABLE", "filter")
SET_SIN_INET6 = "sin_internet6"
SET_SIN_INET4 = "sin_internet4"
# Fichero aparte del de los mapas DNAT a propósito: aquél se incluye ANTES de
# que `table inet filter` esté declarada, y un `add element` sobre una tabla que
# aún no existe rompe la recarga entera.
NFT_SIN_INET_FILE = Path(
    os.environ.get("NFT_SIN_INET_FILE", "/etc/nftables.d/base-nat-sin-internet.nft")
)


def _nft_set_exists(nombre: str) -> bool:
    return subprocess.run(
        ["nft", "list", "set", NFT_FILTER_FAMILY, NFT_FILTER_TABLE, nombre],
        capture_output=True, text=True, check=False,
    ).returncode == 0


def _nft_set_members(nombre: str) -> set[str]:
    r = subprocess.run(
        ["nft", "-j", "list", "set", NFT_FILTER_FAMILY, NFT_FILTER_TABLE, nombre],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        return set()
    try:
        datos = json.loads(r.stdout)
    except ValueError:
        return set()
    fuera: set[str] = set()
    for obj in datos.get("nftables", []):
        for elem in (obj.get("set", {}) or {}).get("elem", []) or []:
            if isinstance(elem, str):
                fuera.add(elem)
    return fuera


def _corta_conexiones(direcciones: list[str]) -> int:
    """Mata las conexiones ya abiertas de esas direcciones.

    Sin esto la regla nueva no se nota: un flujo ya establecido está descargado
    en el flowtable y sus paquetes se saltan la cadena entera, así que el robot
    de dentro seguiría hablando con su bróker durante horas. Es el equivalente
    del `reset_conntrack` que hacía el disparador cuando esto vivía en el nodo.
    """
    if not direcciones:
        return 0
    n = 0
    for addr in direcciones:
        for sentido in ("--src", "--dst"):
            r = subprocess.run(
                ["conntrack", "-D", sentido, addr],
                capture_output=True, text=True, check=False,
            )
            # rc=1 = no había ninguna. Solo es un fallo si falta el binario.
            if r.returncode not in (0, 1):
                logger.warning("conntrack -D %s %s: %s", sentido, addr,
                               (r.stderr or "").strip()[:120])
            else:
                n += (r.stderr or "").count("deleted")
    return n


def reconcile_sin_internet(desired: dict[int, dict]) -> None:
    """Repuebla los sets de VMs sin Internet y corta lo que ya estaba abierto."""
    if not _nft_set_exists(SET_SIN_INET6):
        # Base sin la parte de nftables instalada todavía. No es un error: el
        # resto de la sincronización tiene que seguir funcionando igual.
        logger.info("sin_internet: sets no declarados en nftables; omito")
        return

    v6: list[str] = []
    v4: list[str] = []
    for vmid in sorted(desired):
        entrada = desired[vmid]
        if entrada.get("internet", True):
            continue
        try:
            v6.append(normalize_ipv6(entrada["ipv6"]))
        except (KeyError, ValueError):
            logger.warning("sin_internet: vm %s con ipv6 ilegible; omito", vmid)
            continue
        bruta4 = str(entrada.get("ipv4") or "").strip()
        if bruta4:
            try:
                v4.append(normalize_ipv4(bruta4))
            except ValueError:
                logger.warning("sin_internet: vm %s con ipv4 ilegible: %s", vmid, bruta4)

    antes6 = _nft_set_members(SET_SIN_INET6)
    antes4 = _nft_set_members(SET_SIN_INET4)

    lineas = [
        f"flush set {NFT_FILTER_FAMILY} {NFT_FILTER_TABLE} {SET_SIN_INET6}",
        f"flush set {NFT_FILTER_FAMILY} {NFT_FILTER_TABLE} {SET_SIN_INET4}",
    ]
    elementos = [
        f"add element {NFT_FILTER_FAMILY} {NFT_FILTER_TABLE} {SET_SIN_INET6} {{ {a} }}"
        for a in v6
    ] + [
        f"add element {NFT_FILTER_FAMILY} {NFT_FILTER_TABLE} {SET_SIN_INET4} {{ {a} }}"
        for a in v4
    ]
    _apply_nft_transaction(lineas + elementos)

    try:
        NFT_SIN_INET_FILE.parent.mkdir(parents=True, exist_ok=True)
        cuerpo = "# generado por sync-base-nat.py - no editar a mano\n"
        cuerpo += "\n".join(elementos) + ("\n" if elementos else "")
        tmp = NFT_SIN_INET_FILE.with_suffix(NFT_SIN_INET_FILE.suffix + ".tmp")
        tmp.write_text(cuerpo)
        os.replace(tmp, NFT_SIN_INET_FILE)
    except OSError as e:
        logger.warning("sin_internet: no pude escribir %s: %s", NFT_SIN_INET_FILE, e)

    # Solo las que ACABAN de perder Internet: cortar las de las demás sería
    # tirarle la sesión a quien no ha cambiado nada.
    nuevas = [a for a in v6 if a not in antes6] + [a for a in v4 if a not in antes4]
    matadas = _corta_conexiones(nuevas)

    logger.info(
        "sin_internet reconcile: %d VM(s) sin Internet (v6=%d v4=%d), "
        "%d dirección(es) recién cortada(s), %d conexión(es) matada(s)",
        len(v6), len(v6), len(v4), len(nuevas), matadas,
    )


def _state_payload(desired: dict[int, dict]) -> dict:
    """Serializable view of the desired map for STATE_FILE."""
    return {
        str(k): {
            "ipv6": desired[k]["ipv6"],
            "samba": SAMBA_PORT_BASE + k,
            "rdp": RDP_PORT_BASE + k,
            "ssh": SSH_PORT_BASE + k,
            "rdpEnabled": bool(desired[k].get("rdp", True)),
            "sambaEnabled": bool(desired[k].get("samba", True)),
            "sshEnabled": bool(desired[k].get("ssh", True)),
            "internetEnabled": bool(desired[k].get("internet", True)),
            "nodeId": desired[k].get("nodeId", ""),
            "ipv4": desired[k].get("ipv4", ""),
        }
        for k in sorted(desired.keys())
    }


def sync_full():
    # Firestore read happens outside the lock because it can take seconds
    # and we don't want to block per-VM syncs that whole time. The lock
    # only needs to cover the nft+state critical section.
    desired = firestore_list_configured_servers()
    with state_lock():
        reconcile_dynamic_dnat_rules(desired)
        reconcile_vm_routes(desired)
        reconcile_sin_internet(desired)
        write_state(_state_payload(desired))
    logger.info("Full sync done (%d servers)", len(desired))


def sync_single_vmid(
    vmid: int,
    server: dict | None,
    delete_only: bool,
    ipv6_override: bool = False,
    flags_override: dict | None = None,
    server_loader=None,
):
    """Reconcile a single VM in the desired map.

    `server` is a `_server_entry` dict (ipv6 + flags) or None.
    `ipv6_override=True` skips Firestore (use `server` as-is). Otherwise
    `server` is the Firestore snapshot (already includes firewall flags)
    or None when the VM is not configured anymore.

    `server_loader` (callable returning the Firestore snapshot) defers the
    read until AFTER the state lock is held. Concurrent per-VM syncs for
    the same vmid (e.g. a burst of panel firewall toggles firing several
    cloud triggers) serialize on the lock, and each one then reads the
    CURRENT doc — so the last to run converges to the latest truth even
    when the trigger executions arrive out of order (VM 231's recurring
    lost rdp element, 2026-07-04).

    `flags_override` (only meaningful with `ipv6_override`) lets the
    caller force specific service flags by key (`rdp` and/or `samba`).
    Keys not present fall back to the previous on-disk state, or to
    enabled when the VM is new.
    """
    if not delete_only and not ipv6_override and not ensure_firebase():
        logger.error("Firebase required for sync <proxmoxId> (lookup)")
        sys.exit(1)
    if ipv6_override and (server is None or not server.get("ipv6")):
        logger.error("Explicit IPv6 required for sync <proxmoxId> <ipv6>")
        sys.exit(2)

    # All read-modify-write of state.json + nft maps must run under the
    # lock — see state_lock() docstring for why a torn read here used to
    # nuke unrelated VMs' DNAT.
    with state_lock():
        if server_loader is not None:
            server = server_loader()
        desired = desired_from_state()
        if delete_only:
            desired.pop(vmid, None)
            logger.info("proxmoxId=%s removed from desired map", vmid)
        elif server is not None:
            if ipv6_override:
                # Override path: caller is authoritative when it supplied a
                # flag, otherwise preserve the previous state so an IPv6
                # change doesn't accidentally re-enable a service the user
                # previously toggled off.
                existing = desired.get(vmid)
                rdp_default = bool(existing.get("rdp", True)) if existing else True
                samba_default = bool(existing.get("samba", True)) if existing else True
                ssh_default = bool(existing.get("ssh", True)) if existing else True
                net_default = bool(existing.get("internet", True)) if existing else True
                override = flags_override or {}
                rdp_flag = bool(override["rdp"]) if "rdp" in override else rdp_default
                samba_flag = bool(override["samba"]) if "samba" in override else samba_default
                ssh_flag = bool(override["ssh"]) if "ssh" in override else ssh_default
                net_flag = bool(override["internet"]) if "internet" in override else net_default
                # La IPv4 privada NO viene por esta via (el que llama solo pasa
                # la IPv6), y sin ella el corte de Internet quedaria a medias:
                # se cerraria la salida v6 y la v4 seguiria abierta. Se recupera
                # del estado en disco, que lo dejo el ultimo sync completo.
                ipv4_flag = str((existing or {}).get("ipv4") or "")
            else:
                rdp_flag = bool(server.get("rdp", True))
                samba_flag = bool(server.get("samba", True))
                ssh_flag = bool(server.get("ssh", True))
                net_flag = bool(server.get("internet", True))
                ipv4_flag = str(server.get("ipv4") or "")
            desired[vmid] = _server_entry(
                normalize_ipv6(server["ipv6"]),
                rdp=rdp_flag,
                samba=samba_flag,
                ssh=ssh_flag,
                internet=net_flag,
                ipv4=ipv4_flag,
            )
        else:
            desired.pop(vmid, None)
            logger.info("proxmoxId=%s not in Firestore; removed from desired map", vmid)

        # Modelo nuevo: la ruta /128 (y la /32 privada) cuelgan del tunel del
        # NODO, asi que una migracion obliga a moverlas. La Cloud Function que
        # reacciona a un cambio de nodo entra POR AQUI, no por el sync completo,
        # y esta via nunca reconciliaba rutas: tras migrar, las dos bases seguian
        # apuntando al tunel del nodo anterior y la VM quedaba inalcanzable
        # aunque su DNAT estuviera perfecto. Medido con la vm 1096 el 2026-08-14.
        ent = desired.get(vmid)
        if ent and _is_ident(ent.get("ipv6") or ""):
            if ipv6_override or not ent.get("nodeId"):
                # El camino de override salta Firestore a proposito (es el
                # rapido), pero sin el nodo no hay ruta que calcular. Se lee
                # SOLO para las VMs del rango de identidad: las unicas con ruta.
                fresco = firestore_server_for_vmid(vmid)
                if fresco:
                    if fresco.get("nodeId"):
                        ent["nodeId"] = fresco["nodeId"]
                    if fresco.get("ipv4"):
                        ent["ipv4"] = fresco["ipv4"]
                    # `internet` tambien, y por el mismo motivo que `ipv4`: el
                    # estado en disco puede ser de antes del cambio. Firestore
                    # es la fuente de verdad porque el disparador escribe ALLI
                    # primero y por eso se ejecuta esto. Si quien llama lo paso
                    # explicitamente, manda quien llama.
                    if "internet" not in (flags_override or {}):
                        ent["internet"] = bool(fresco.get("internet", True))

        reconcile_dynamic_dnat_rules(desired)
        reconcile_vm_routes(desired)
        reconcile_sin_internet(desired)
        write_state(_state_payload(desired))
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


def serialize_node_state(fn):
    """Lock the whole node-map read/update/apply, independently of VM routing.

    Registering several nodes at once must not overwrite another invocation's
    additions. A separate lock keeps tunnel/nginx updates from delaying the
    short, time-sensitive per-VM NAT cutover lock.
    """
    @functools.wraps(fn)
    def locked(*args, **kwargs):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with (STATE_DIR / ".pve-nodes.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return fn(*args, **kwargs)
    return locked


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
    # migrate_vm.sh reads this without the writer lock. Atomic replacement
    # guarantees it sees either complete generation, never a truncated map.
    PVE_NODES_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".pve-nodes-", dir=PVE_NODES_STATE_FILE.parent)
    try:
        with os.fdopen(fd, "w") as output:
            output.write(json.dumps(dict(sorted(nodes.items())), indent=2, sort_keys=True) + "\n")
        os.chmod(temporary, 0o644)
        os.replace(temporary, PVE_NODES_STATE_FILE)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


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


def parse_base_hosts() -> dict[str, str]:
    """{alias: ipv6, ...} from BASE_HOSTS env (comma-separated).

    Two entry forms, mixable:
      - "name=ipv6"  -> explicit alias (e.g. "b00=2a01:...", matches the
        b00/b0/b1 DNS naming introduced with the dual-region bases, where
        positional numbering can no longer express the alias set).
        '=' is the separator on purpose: aliases like b0/b00/b1 are valid
        IPv6 hextets, so "b0:2a01:..." would parse as an ADDRESS and a
        colon separator would be ambiguous.
      - "ipv6"       -> positional alias bN by position (legacy behavior).

    Empty/missing -> {}. Keep the same entries on every BASE so the
    aliases map to the same hosts everywhere.
    """
    raw = (os.environ.get("BASE_HOSTS") or "").strip()
    if not raw:
        return {}
    out: dict[str, str] = {}
    idx = 0
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        name = None
        ip = entry
        if "=" in entry:
            name, ip = entry.split("=", 1)
            name = name.strip()
            ip = ip.strip()
            if not name:
                logger.warning("BASE_HOSTS: skip entry with empty name %r", entry)
                continue
        try:
            normalize_ipv6(ip)
        except ValueError:
            logger.warning("BASE_HOSTS: skip invalid IPv6 %r", ip)
            continue
        if name:
            out[name] = ip
        else:
            out[f"b{idx}"] = ip
            idx += 1
    return out


_NODE_NUM_RE = re.compile(r"^0*(\d+)")


def node_short_alias(node_id: str) -> str | None:
    """`p<N>` from the leading digits of a node id, or None when absent.

    "0000002-AX162-R" -> "p2", "0000047-EX44" -> "p47".
    """
    m = _NODE_NUM_RE.match(node_id)
    return f"p{m.group(1)}" if m else None


def _replace_marker_block(text: str, begin: str, end: str, replacement: str) -> str:
    """Replace the begin..end block in text. Append at EOF when missing."""
    lines = text.splitlines(keepends=True)
    start = end_idx = None
    for i, line in enumerate(lines):
        s = line.rstrip("\n")
        if start is None and s == begin:
            start = i
        elif start is not None and s == end:
            end_idx = i
            break
    if start is None or end_idx is None:
        prefix = text if (not text or text.endswith("\n")) else text + "\n"
        return prefix + replacement
    return "".join(lines[:start]) + replacement + "".join(lines[end_idx + 1:])


def write_hosts_block(nodes: dict[str, str], bases: dict[str, str]):
    """Replace the managed block in /etc/hosts with bases + node aliases.

    Each line: `<ipv6>\t<canonical>\t<short>` so `ssh p2` / `ssh b1` work
    on any BASE. The block is rewritten atomically; rest of /etc/hosts is
    untouched.
    """
    if not HOSTS_FILE.is_file():
        logger.warning("hosts: %s missing; skip", HOSTS_FILE)
        return

    out_lines: list[str] = [HOSTS_BLOCK_BEGIN]
    for alias in sorted(bases.keys()):
        ipv6 = bases[alias]
        canonical = f"base-{alias[1:]}" if alias.startswith("b") else alias
        out_lines.append(f"{ipv6}\t{canonical}\t{alias}")
    for node_id in sorted(nodes.keys()):
        ipv6 = nodes[node_id]
        short = node_short_alias(node_id)
        if short:
            out_lines.append(f"{ipv6}\t{node_id}\t{short}")
        else:
            out_lines.append(f"{ipv6}\t{node_id}")
    out_lines.append(HOSTS_BLOCK_END)
    new_block = "\n".join(out_lines) + "\n"

    text = HOSTS_FILE.read_text()
    new_text = _replace_marker_block(text, HOSTS_BLOCK_BEGIN, HOSTS_BLOCK_END, new_block)
    if new_text == text:
        return
    try:
        tmp = HOSTS_FILE.with_suffix(HOSTS_FILE.suffix + ".sync-base-nat.tmp")
        tmp.write_text(new_text)
        os.replace(tmp, HOSTS_FILE)
        logger.info("hosts: updated (%d nodes, %d bases)", len(nodes), len(bases))
    except OSError as e:
        logger.warning("hosts: write %s failed: %s", HOSTS_FILE, e)


TUNNEL_NODES_HEADER = """# generado por sync-base-nat.py - NO editar a mano
#
# Tabla de nodos del lado BASE de los tuneles ip6gre. Cada linea:
#
#   <id>  <ipv6 principal del nodo>  <slot>
#
# El slot reserva un hueco de 16 direcciones dentro del /112 de transito:
# +0/+1 = par FSN, +2/+3 = par HEL. slot = id, determinista y sin registro.
# El hueco 0 esta RESERVADO para las canonicas de las bases (::0 FSN, ::2 HEL).
#
# Estan TODOS los nodos, tambien los que aun no se han convertido: el lado base
# de un tunel cuyo nodo no lo tiene montado es inerte (nada enruta hacia el), y
# tenerlo ya creado convierte la conversion de un nodo en una operacion de UNA
# sola maquina y permite que una VM migre a cualquier nodo ya convertido.
"""


def render_tunnel_nodes(nodes: dict[str, str]) -> str | None:
    """Texto del fichero, o None si el estado no es de fiar.

    Devolver None (y no un fichero corto) es deliberado: quien llama deja el
    fichero anterior intacto. Un fichero vacio borraria de golpe el camino de
    TODA la flota.
    """
    if not nodes:
        logger.warning("tunnel-nodes: estado de nodos vacio; no toco el fichero")
        return None

    lines, vistos = [], {}
    for node_id, ipv6 in sorted(nodes.items()):
        m = _NODE_NUM_RE.match(node_id)
        if not m:
            logger.warning("tunnel-nodes: %s sin numero al principio; lo salto", node_id)
            continue
        num = int(m.group(1))
        if not (TUNNEL_SLOT_MIN <= num <= TUNNEL_SLOT_MAX):
            logger.warning(
                "tunnel-nodes: %s tiene id %d fuera de %d-%d; lo salto (el hueco 0 es "
                "de las canonicas de las bases y por arriba no cabe en el /112)",
                node_id, num, TUNNEL_SLOT_MIN, TUNNEL_SLOT_MAX,
            )
            continue
        if num in vistos:
            # Dos nodos con el mismo numero compartirian direcciones de
            # transito: el segundo se llevaria el trafico del primero.
            logger.error(
                "tunnel-nodes: %s y %s comparten el id %d; me quedo con el primero "
                "y SALTO el segundo — corrige el nombre en Firestore",
                vistos[num], node_id, num,
            )
            continue
        vistos[num] = node_id
        lines.append(f"{num:<5} {ipv6:<28} {num}")

    if not lines:
        logger.warning("tunnel-nodes: ninguna linea valida; no toco el fichero")
        return None
    return TUNNEL_NODES_HEADER + "\n".join(lines) + "\n"


def write_tunnel_nodes(nodes: dict[str, str]) -> None:
    """Escribe la tabla y, SOLO si ha cambiado, relanza la unidad de tuneles.

    Nunca puede tumbar el sync: cualquier fallo aqui es un aviso. Y el disparo
    va condicionado al cambio a proposito — recorrer los ~470 tuneles cuesta
    ~15 s, y el sync se ejecuta muchas veces al dia.
    """
    try:
        nuevo = render_tunnel_nodes(nodes)
        if nuevo is None:
            return
        viejo = TUNNEL_NODES_FILE.read_text() if TUNNEL_NODES_FILE.is_file() else ""
        if nuevo == viejo:
            return

        antes = len([l for l in viejo.splitlines() if l and not l.startswith("#")])
        ahora = len([l for l in nuevo.splitlines() if l and not l.startswith("#")])
        TUNNEL_NODES_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = TUNNEL_NODES_FILE.with_suffix(".tmp")
        tmp.write_text(nuevo)
        tmp.replace(TUNNEL_NODES_FILE)      # atomico: nunca un fichero a medias
        logger.info("tunnel-nodes: %s actualizado (%d -> %d nodos)",
                    TUNNEL_NODES_FILE, antes, ahora)

        # Un nodo que desaparece de Firestore deja su tunel en pie. Es inerte
        # (nada enruta hacia el), asi que se AVISA y no se borra: un borrado
        # automatico sobre un estado mal leido se llevaria por delante el camino
        # de nodos vivos.
        if ahora < antes:
            logger.warning(
                "tunnel-nodes: %d nodo(s) menos que antes — sus tuneles siguen en pie "
                "(inertes). Borralos a mano si la baja es definitiva.", antes - ahora,
            )

        # Comprobacion canonica de "systemd esta al mando": evita importar
        # shutil solo para esto y no depende del PATH.
        if not Path("/run/systemd/system").is_dir():
            return
        r = subprocess.run(["systemctl", "restart", TUNNEL_UNIT],
                           capture_output=True, text=True, check=False)
        if r.returncode == 0:
            logger.info("tunnel-nodes: %s relanzada", TUNNEL_UNIT)
        else:
            logger.warning("tunnel-nodes: no pude relanzar %s: %s",
                           TUNNEL_UNIT, (r.stderr or r.stdout or "").strip()[:200])
    except Exception as e:                        # nunca romper el sync
        logger.warning("tunnel-nodes: fallo generando la tabla: %s", e)


def sync_nodes_apply_state(nodes: dict[str, str], reload_nginx: bool = True):
    write_pve_nodes_state(nodes)
    write_pve_nginx_map(nodes)
    # Hereda gratis la guardia anti-lectura-parcial: sync_nodes_full aborta
    # ANTES de llegar aqui si Firestore devuelve de menos.
    write_tunnel_nodes(nodes)
    write_hosts_block(nodes, parse_base_hosts())
    if reload_nginx:
        nginx_test_and_reload()
    logger.info("PVE nodes map: %d entries", len(nodes))


# A full sync REPLACES the node map. If the Firestore read comes back short --
# a blip, a half-finished pagination, an exception swallowed upstream -- the
# replacement silently strips every missing node from the nginx PVE map, from
# /etc/hosts and from the firewall IPSET.
#
# That is not hypothetical: on 2026-07-29 a full sync on b1 wrote 28 nodes
# instead of 236. The 208 missing ones lost their PVE console (the map's
# `default ""` turns into a bare HTTP 404), and node 0000235 received a
# cluster.fw listing only 28 hosts. Nothing alerted; it surfaced because an
# operator clicked a console link and got a 404.
#
# A real fleet never loses a fifth of its nodes between two runs, so treat a
# large shrink as a bad read and refuse to write. Removing many nodes on
# purpose stays possible with --force.
SHRINK_GUARD_PCT = float(os.environ.get("PVE_MAP_SHRINK_GUARD_PCT", "80"))


@serialize_node_state
def sync_nodes_full(force: bool = False):
    nodes = firestore_list_proxmox_nodes()
    prev = read_pve_nodes_state()
    if prev and not force:
        floor = len(prev) * SHRINK_GUARD_PCT / 100.0
        if len(nodes) < floor:
            logger.error(
                "sync nodes full: REFUSING to write — Firestore returned %d node(s) "
                "but the current state has %d (below the %.0f%% floor of %.0f). "
                "This is almost always a partial read, and writing it would drop "
                "%d node(s) from the PVE map, /etc/hosts and the firewall IPSET. "
                "State left untouched. Re-run when Firestore answers fully, or "
                "pass --force if the removal is intentional.",
                len(nodes), len(prev), SHRINK_GUARD_PCT, floor, len(prev) - len(nodes),
            )
            sys.exit(1)
    sync_nodes_apply_state(nodes)
    logger.info("sync nodes full done (%d from Firestore)", len(nodes))


@serialize_node_state
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


@serialize_node_state
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


@serialize_node_state
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
    force = False
    if "--force" in args:
        force = True
        args = [a for a in args if a != "--force"]
    if not args:
        sync_nodes_full(force=force)
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
            "Usage: sync-base-nat.py sync nodes [--force]\n"
            "       sync-base-nat.py sync nodes <nodeId>\n"
            "       sync-base-nat.py sync nodes add <nodeId> <ipv6>\n"
            "       sync-base-nat.py sync nodes del <nodeId>\n"
            "       sync-base-nat.py sync nodes sync-firewall",
            file=sys.stderr,
        )
        sys.exit(2)
    sync_nodes_single(args[0])


_FLAG_TRUE = {"1", "true", "yes", "on"}
_FLAG_FALSE = {"0", "false", "no", "off"}


def _parse_flag_args(extra: list[str]) -> dict:
    """Parse `key=value` args into a flag override dict.

    Accepts `rdp`, `samba` and `ssh` keys with boolean-ish values. Unknown
    keys or unparseable values are a hard error so a typo never silently
    leaves DNAT in a wrong state.
    """
    out: dict = {}
    for arg in extra:
        if "=" not in arg:
            logger.error("Unexpected argument %r (expected key=value)", arg)
            sys.exit(2)
        k, _, v = arg.partition("=")
        k = k.strip().lower()
        v = v.strip().lower()
        if k not in ("rdp", "samba", "ssh", "internet"):
            logger.error("Unknown flag %r (allowed: rdp, samba, ssh, internet)", k)
            sys.exit(2)
        if v in _FLAG_TRUE:
            out[k] = True
        elif v in _FLAG_FALSE:
            out[k] = False
        else:
            logger.error("Invalid value for %s: %r", k, v)
            sys.exit(2)
    return out


def main():
    if len(sys.argv) < 2 or sys.argv[1] != "sync":
        print(
            "Usage: sync-base-nat.py sync\n"
            "       sync-base-nat.py sync <proxmoxId>\n"
            "       sync-base-nat.py sync <proxmoxId> <ipv6> [rdp=0|1] [samba=0|1] [ssh=0|1] [internet=0|1]\n"
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
        # Override path: caller wins on ipv6 (and on whichever flags it
        # supplies). Flags not passed are preserved from the on-disk
        # state so an IPv6-only refresh doesn't clobber a user toggle.
        flags_override = _parse_flag_args(args[2:])
        sync_single_vmid(
            vmid,
            _server_entry(ipv6, rdp=True, samba=True),
            delete_only=False,
            ipv6_override=True,
            flags_override=flags_override or None,
        )
        return

    # Read Firestore INSIDE the lock (server_loader) so concurrent syncs
    # for the same vmid each converge to the freshest doc state.
    sync_single_vmid(
        vmid,
        None,
        delete_only=False,
        ipv6_override=False,
        server_loader=lambda: firestore_server_for_vmid(vmid),
    )


if __name__ == "__main__":
    main()

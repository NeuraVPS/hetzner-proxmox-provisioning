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

  sync-base-nat.py sync <proxmoxId> <publicVMIPv6> [rdp=0|1] [samba=0|1]
      Single VM override without Firestore read. Optional rdp/samba
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
VMID_MAX = int(os.environ.get("VMID_MAX", os.environ.get("PORT_MAX", "9999")))
SAMBA_PORT_END = SAMBA_PORT_BASE + VMID_MAX
RDP_PORT_END = RDP_PORT_BASE + VMID_MAX
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
# Defaults: rdp/samba enabled when the field is missing (back-compat with
# pre-firewall-flag state files and Firestore docs).
def _server_entry(ipv6: str, rdp: bool = True, samba: bool = True) -> dict:
    return {"ipv6": ipv6, "rdp": bool(rdp), "samba": bool(samba)}


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
        out[vmid] = _server_entry(
            s,
            rdp=bool(rdp_flag) if isinstance(rdp_flag, bool) else True,
            samba=bool(samba_flag) if isinstance(samba_flag, bool) else True,
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
        )
    return None


# --- Dynamic DNAT via nftables maps -------------------------------------------
#
# The NAT chain contains three persistent rules declared in
# /etc/nftables.conf (see base/docs/netns-jool-nat46-nat66-guide.md §7):
#
#   tcp dport @rdp_tcp_map dnat ip6 to tcp dport map @rdp_tcp_map
#   udp dport @rdp_udp_map dnat ip6 to udp dport map @rdp_udp_map
#   tcp dport @smb_tcp_map dnat ip6 to tcp dport map @smb_tcp_map
#
# This script only populates the ELEMENTS of those maps; structure stays
# in nftables.conf so it survives reboots and full rule reloads. Each
# reconcile is applied as a single atomic `nft -f -` transaction.

DNAT_MAP_RDP_TCP = "rdp_tcp_map"
DNAT_MAP_RDP_UDP = "rdp_udp_map"
DNAT_MAP_SMB_TCP = "smb_tcp_map"
DNAT_MAPS = (DNAT_MAP_RDP_TCP, DNAT_MAP_RDP_UDP, DNAT_MAP_SMB_TCP)

RDP_TARGET_PORT = 3389
SMB_TARGET_PORT = 445


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
        samba_p, rdp_p = vm_ports(vmid)
        if entry.get("rdp", True):
            out[DNAT_MAP_RDP_TCP][rdp_p] = (target_ipv6, RDP_TARGET_PORT)
            if INCLUDE_UDP_RDP:
                out[DNAT_MAP_RDP_UDP][rdp_p] = (target_ipv6, RDP_TARGET_PORT)
        if entry.get("samba", True):
            out[DNAT_MAP_SMB_TCP][samba_p] = (target_ipv6, SMB_TARGET_PORT)
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
        "Dynamic DNAT reconcile (maps): rdp_tcp=%d rdp_udp=%d smb_tcp=%d",
        len(desired_by_map[DNAT_MAP_RDP_TCP]),
        len(desired_by_map[DNAT_MAP_RDP_UDP]),
        len(desired_by_map[DNAT_MAP_SMB_TCP]),
    )


def _state_payload(desired: dict[int, dict]) -> dict:
    """Serializable view of the desired map for STATE_FILE."""
    return {
        str(k): {
            "ipv6": desired[k]["ipv6"],
            "samba": SAMBA_PORT_BASE + k,
            "rdp": RDP_PORT_BASE + k,
            "rdpEnabled": bool(desired[k].get("rdp", True)),
            "sambaEnabled": bool(desired[k].get("samba", True)),
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
        write_state(_state_payload(desired))
    logger.info("Full sync done (%d servers)", len(desired))


def sync_single_vmid(
    vmid: int,
    server: dict | None,
    delete_only: bool,
    ipv6_override: bool = False,
    flags_override: dict | None = None,
):
    """Reconcile a single VM in the desired map.

    `server` is a `_server_entry` dict (ipv6 + flags) or None.
    `ipv6_override=True` skips Firestore (use `server` as-is). Otherwise
    `server` is the Firestore snapshot (already includes firewall flags)
    or None when the VM is not configured anymore.

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
                override = flags_override or {}
                rdp_flag = bool(override["rdp"]) if "rdp" in override else rdp_default
                samba_flag = bool(override["samba"]) if "samba" in override else samba_default
            else:
                rdp_flag = bool(server.get("rdp", True))
                samba_flag = bool(server.get("samba", True))
            desired[vmid] = _server_entry(
                normalize_ipv6(server["ipv6"]),
                rdp=rdp_flag,
                samba=samba_flag,
            )
        else:
            desired.pop(vmid, None)
            logger.info("proxmoxId=%s not in Firestore; removed from desired map", vmid)

        reconcile_dynamic_dnat_rules(desired)
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


def parse_base_hosts() -> dict[str, str]:
    """{b0: ipv6, b1: ipv6, ...} from BASE_HOSTS env (positional, comma-separated).

    Empty/missing -> {}. Order matters: first entry becomes b0, second b1,
    etc. Keep the same order on every BASE so the bN aliases map to the
    same hosts everywhere.
    """
    raw = (os.environ.get("BASE_HOSTS") or "").strip()
    if not raw:
        return {}
    out: dict[str, str] = {}
    idx = 0
    for ip in raw.split(","):
        ip = ip.strip()
        if not ip:
            continue
        try:
            normalize_ipv6(ip)
        except ValueError:
            logger.warning("BASE_HOSTS: skip invalid IPv6 %r", ip)
            continue
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


def sync_nodes_apply_state(nodes: dict[str, str], reload_nginx: bool = True):
    write_pve_nodes_state(nodes)
    write_pve_nginx_map(nodes)
    write_hosts_block(nodes, parse_base_hosts())
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


_FLAG_TRUE = {"1", "true", "yes", "on"}
_FLAG_FALSE = {"0", "false", "no", "off"}


def _parse_flag_args(extra: list[str]) -> dict:
    """Parse `key=value` args into a flag override dict.

    Accepts `rdp` and `samba` keys with boolean-ish values. Unknown keys
    or unparseable values are a hard error so a typo never silently
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
        if k not in ("rdp", "samba"):
            logger.error("Unknown flag %r (allowed: rdp, samba)", k)
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
            "       sync-base-nat.py sync <proxmoxId> <ipv6> [rdp=0|1] [samba=0|1]\n"
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

    server = firestore_server_for_vmid(vmid)
    sync_single_vmid(vmid, server, delete_only=False, ipv6_override=False)


if __name__ == "__main__":
    main()

"""NeuraVPS web file browser — thin SMB bridge (runs on each BASE).

The panel mints a short-lived HMAC token (Cloud Function, ownership-checked)
and redirects the customer to files-hel/-fsn.neuravps.com, served by nginx →
this FastAPI app. The bridge talks SMB (userspace, smbprotocol — NO kernel
cifs mounts) to the customer's VMs by their DIRECT vmbr0 IPv6 :445, using the
per-VM Windows credentials it looks up in Firestore. Both bases reach every
VM's IPv6 directly (operator-confirmed 2026-07-07), so ONE bridge serves all
of a user's servers, cross-region included — no bridge-to-bridge.

Security model:
  - The browser token carries only {uid, servers:[{id,vmid,name,type,ipv6}],
    exp} signed with FILE_BRIDGE_TOKEN_SECRET (shared with the Cloud Function).
    NO passwords ever leave the base.
  - Every request is scoped to the servers embedded in the token (the user's
    own, resolved at mint time). Passwords are fetched here from Firestore
    (/etc/firebase-credentials.json) and never returned to the client.
  - Same exposure as the existing mapped-SMB product; no new surface.

Data path stays on Hetzner (base ↔ VM ↔ customer) — GCP is never in it.
"""

import base64
import hashlib
import hmac
import io
import json
import logging
import os
import posixpath
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("file_bridge")

# --- Config (env, set by systemd unit) --------------------------------------
TOKEN_SECRET = os.environ.get("FILE_BRIDGE_TOKEN_SECRET", "")
FIREBASE_CREDS = os.environ.get("FIREBASE_CREDENTIALS_FILE",
                                "/etc/firebase-credentials.json")
# Stability limits (operator-approved 2026-07-07; latency is NOT critical —
# this is not RDP — so favor robustness over speed).
THROTTLE_BYTES_PER_S = int(os.environ.get("FILE_BRIDGE_THROTTLE_BPS",
                                          str(40 * 1024 * 1024)))  # 40 MB/s/session
MAX_UPLOAD_BYTES = int(os.environ.get("FILE_BRIDGE_MAX_UPLOAD",
                                      str(20 * 1024 * 1024 * 1024)))  # 20 GB
LIST_PAGE_SIZE = 500
SMB_ENCRYPT = os.environ.get("FILE_BRIDGE_SMB_ENCRYPT", "1") != "0"
SMB_TIMEOUT_S = 30

_PW_CACHE_TTL = 60  # seconds; per-vmid credential cache (bridge-local)


# --- Token ------------------------------------------------------------------

class TokenError(Exception):
    pass


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def verify_token(token: str) -> Dict[str, Any]:
    """Validate the HMAC token minted by the Cloud Function. Returns its
    payload {uid, servers:[...], exp}. Raises TokenError on any problem."""
    if not TOKEN_SECRET:
        raise TokenError("bridge misconfigured: no TOKEN_SECRET")
    try:
        body_b64, sig_b64 = token.split(".", 1)
        expected = hmac.new(TOKEN_SECRET.encode(), body_b64.encode(),
                            hashlib.sha256).digest()
        sig = _b64url_decode(sig_b64)
        body = _b64url_decode(body_b64)
    except (ValueError, TypeError) as exc:
        # split/base64 failures on attacker-controlled input -> clean 401
        raise TokenError("malformed token") from exc
    if not hmac.compare_digest(expected, sig):
        raise TokenError("bad signature")
    try:
        payload = json.loads(body)
    except (ValueError, TypeError) as exc:
        raise TokenError("malformed payload") from exc
    if int(payload.get("exp", 0)) < int(time.time()):
        raise TokenError("expired")
    if not payload.get("uid") or not isinstance(payload.get("servers"), list):
        raise TokenError("incomplete token")
    return payload


def sign_token(payload: Dict[str, Any]) -> str:
    """Sign a payload with the shared secret (mirror of the CF's minter).
    Used for the session-cookie tokens the bridge itself issues — verified
    by the same verify_token() above."""
    if not TOKEN_SECRET:
        raise TokenError("bridge misconfigured: no TOKEN_SECRET")
    body_b64 = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()
    sig = hmac.new(TOKEN_SECRET.encode(), body_b64.encode(),
                   hashlib.sha256).digest()
    return body_b64 + "." + base64.urlsafe_b64encode(sig).rstrip(b"=").decode()


# --- Firestore credential lookup (passwords stay on the base) ---------------

_fb_app = None
_pw_cache: Dict[int, Tuple[float, Dict[str, str]]] = {}


def _firestore():
    global _fb_app
    import firebase_admin
    from firebase_admin import credentials, firestore
    if _fb_app is None:
        _fb_app = firebase_admin.initialize_app(
            credentials.Certificate(FIREBASE_CREDS))
    return firestore.client()


def resolve_credentials(vmid: int) -> Dict[str, str]:
    """{ipv6,user,password} for a VM from Firestore, cached briefly. The token
    already proved ownership + carries ipv6; we still read the password (and
    re-confirm ipv6) here so it never travels through the browser."""
    now = time.time()
    hit = _pw_cache.get(vmid)
    if hit and now - hit[0] < _PW_CACHE_TTL:
        return hit[1]
    db = _firestore()
    docs = list(db.collection("servers").where("proxmoxId", "==", vmid)
                .limit(1).stream())
    if not docs:
        raise TokenError(f"server vmid={vmid} not found")
    d = docs[0].to_dict() or {}
    creds = {
        "ipv6": d.get("ipv6") or "",
        "user": d.get("serverUser") or d.get("username") or "Administrator",
        "password": d.get("serverPassword") or "",
    }
    if not creds["ipv6"] or not creds["password"]:
        raise TokenError(f"server vmid={vmid} missing ipv6/password")
    _pw_cache[vmid] = (now, creds)
    return creds


# --- SMB layer (smbprotocol / smbclient) ------------------------------------

@dataclass
class ServerRef:
    vmid: int
    name: str
    stype: str


def _smb_session(vmid: int):
    """Register (idempotent) an SMB session to the VM and return its bare-IPv6
    server key + the C$ UNC root. smbprotocol keys sessions by server string;
    the bare IPv6 works for getaddrinfo and the UNC parser splits on '\\'."""
    import smbclient
    creds = resolve_credentials(vmid)
    server = creds["ipv6"]
    smbclient.register_session(
        server, username=creds["user"], password=creds["password"],
        encrypt=SMB_ENCRYPT, connection_timeout=SMB_TIMEOUT_S)
    return server, fr"\\{server}\C$"


def _safe_rel(path: str) -> str:
    """Normalize a client path to a safe C:-relative Windows path, blocking
    traversal outside the share. '' -> root."""
    p = (path or "").replace("/", "\\").strip("\\")
    parts: List[str] = []
    for seg in p.split("\\"):
        if seg in ("", "."):
            continue
        if seg == "..":
            raise TokenError("path traversal blocked")
        if ":" in seg or any(c in seg for c in '<>"|?*'):
            raise TokenError("illegal path character")
        parts.append(seg)
    return "\\".join(parts)


def _unc(root: str, rel: str) -> str:
    return root + ("\\" + rel if rel else "")


def list_dir(vmid: int, path: str, offset: int = 0,
             limit: int = LIST_PAGE_SIZE) -> Dict[str, Any]:
    import smbclient
    from smbprotocol.exceptions import SMBLinkRedirectionError
    _server, root = _smb_session(vmid)
    rel = _safe_rel(path)
    target = _unc(root, rel)
    names = smbclient.listdir(target)
    names.sort(key=str.lower)
    page = names[offset:offset + limit]
    entries = []
    skipped = 0
    for name in page:
        full = target + "\\" + name
        try:
            st = smbclient.stat(full)
            is_dir = bool(st.st_mode & 0o040000)
            entries.append({
                "name": name,
                "isDir": is_dir,
                "size": 0 if is_dir else st.st_size,
                "mtime": int(st.st_mtime),
            })
        except SMBLinkRedirectionError:
            # Windows SYMLINK resolved client-side (e.g. the cross-server
            # links under C:\My Servers, or Users\All Users). Explorer hides
            # these; the web browser's server dropdown covers cross-server —
            # skip them instead of 500ing the whole listing (bug 2026-07-08).
            skipped += 1
        except OSError:
            entries.append({"name": name, "isDir": False, "size": 0, "mtime": 0})
    return {"path": rel, "total": len(names) - skipped, "offset": offset,
            "entries": entries}


def open_read(vmid: int, path: str):
    import smbclient
    _server, root = _smb_session(vmid)
    return smbclient.open_file(_unc(root, _safe_rel(path)), mode="rb")


def open_write(vmid: int, path: str):
    import smbclient
    _server, root = _smb_session(vmid)
    return smbclient.open_file(_unc(root, _safe_rel(path)), mode="wb")


def mkdir(vmid: int, path: str) -> None:
    import smbclient
    _server, root = _smb_session(vmid)
    smbclient.makedirs(_unc(root, _safe_rel(path)), exist_ok=True)


def remove(vmid: int, path: str) -> None:
    import smbclient
    _server, root = _smb_session(vmid)
    full = _unc(root, _safe_rel(path))
    st = smbclient.stat(full)
    if st.st_mode & 0o040000:
        _rmtree(smbclient, full)
    else:
        smbclient.remove(full)


def _rmtree(smbclient, unc_dir: str) -> None:
    from smbprotocol.exceptions import SMBLinkRedirectionError
    for name in smbclient.listdir(unc_dir):
        child = unc_dir + "\\" + name
        try:
            st = smbclient.stat(child)
        except SMBLinkRedirectionError:
            # Windows symlink: delete the LINK itself (never follow it —
            # its target may be another VM). Dir-link first, file fallback.
            try:
                smbclient.rmdir(child)
            except OSError:
                smbclient.remove(child)
            continue
        if st.st_mode & 0o040000:
            _rmtree(smbclient, child)
        else:
            smbclient.remove(child)
    smbclient.rmdir(unc_dir)


def rename(vmid: int, path: str, new_path: str) -> None:
    import smbclient
    _server, root = _smb_session(vmid)
    smbclient.rename(_unc(root, _safe_rel(path)),
                     _unc(root, _safe_rel(new_path)))


# --- Copy / move (intra-server SMB-native; cross-server streamed) -----------

def _copy_file_stream(src_vmid: int, src_path: str,
                      dst_vmid: int, dst_path: str) -> int:
    """Stream one file src -> dst. Same VM or different VM (the bridge is the
    intermediary; bytes never leave the base). Returns bytes copied."""
    total = 0
    with open_read(src_vmid, src_path) as sf, open_write(dst_vmid, dst_path) as df:
        while True:
            chunk = sf.read(1024 * 1024)
            if not chunk:
                break
            df.write(chunk)
            total += len(chunk)
    return total


def _copy_tree(smbclient, src_vmid, src_rel, dst_vmid, dst_rel) -> int:
    from smbprotocol.exceptions import SMBLinkRedirectionError
    mkdir(dst_vmid, dst_rel)
    _s_server, s_root = _smb_session(src_vmid)
    total = 0
    for name in smbclient.listdir(_unc(s_root, src_rel)):
        s_child = (src_rel + "\\" + name) if src_rel else name
        d_child = (dst_rel + "\\" + name) if dst_rel else name
        try:
            st = smbclient.stat(_unc(s_root, s_child))
        except SMBLinkRedirectionError:
            continue  # never copy through symlinks (cross-server links!)
        if st.st_mode & 0o040000:
            total += _copy_tree(smbclient, src_vmid, s_child, dst_vmid, d_child)
        else:
            total += _copy_file_stream(src_vmid, s_child, dst_vmid, d_child)
    return total


def paste(items: List[Dict[str, Any]], dst_vmid: int, dst_path: str,
          op: str) -> Dict[str, Any]:
    """Windows-clipboard paste. items = [{vmid, path}] (copied/cut earlier,
    possibly from different servers). op = 'copy' | 'move'. Destination is a
    folder on dst_vmid. Server-native rename is used for a same-VM move;
    everything else streams through the bridge."""
    import smbclient
    dst_rel_dir = _safe_rel(dst_path)
    # The paste target is a FOLDER. Normally it's the folder the user is
    # viewing (already exists); ensure it exists anyway so a paste into a
    # freshly-referenced path can't fail mid-way.
    if dst_rel_dir:
        mkdir(dst_vmid, dst_rel_dir)
    copied = 0
    moved_native = 0
    for it in items:
        s_vmid = int(it["vmid"])
        s_rel = _safe_rel(it["path"])
        base = s_rel.split("\\")[-1] if s_rel else ""
        d_rel = (dst_rel_dir + "\\" + base) if dst_rel_dir else base
        _s_server, s_root = _smb_session(s_vmid)
        st = smbclient.stat(_unc(s_root, s_rel))
        is_dir = bool(st.st_mode & 0o040000)
        if op == "move" and s_vmid == dst_vmid:
            # same VM -> atomic server-side rename (no data movement)
            rename(s_vmid, s_rel, d_rel)
            moved_native += 1
            continue
        if is_dir:
            copied += _copy_tree(smbclient, s_vmid, s_rel, dst_vmid, d_rel)
        else:
            copied += _copy_file_stream(s_vmid, s_rel, dst_vmid, d_rel)
        if op == "move":
            remove(s_vmid, s_rel)
    return {"op": op, "items": len(items), "bytesCopied": copied,
            "nativeMoves": moved_native}

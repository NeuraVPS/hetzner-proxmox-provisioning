"""FastAPI HTTP layer for the NeuraVPS web file browser bridge.

nginx (files-hel/-fsn.neuravps.com) terminates TLS and proxies here. The
CF-minted link token is redeemed ONCE at POST /api/session for an HttpOnly
session cookie (sliding 1h, capped 12h); every other endpoint authenticates
by that cookie. Ownership is the session's server list; passwords are
resolved base-side in bridge.py.

Run: uvicorn app:app --host 127.0.0.1 --port 8088  (see systemd unit).
"""

import hashlib
import io
import json
import os
import secrets
import threading
import time
import zipfile
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, \
    UploadFile
from fastapi.responses import FileResponse, StreamingResponse

import bridge

app = FastAPI(title="NeuraVPS file bridge", docs_url=None, redoc_url=None)

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@app.get("/")
def index():
    """The single-page file browser UI (reads ?t= itself, calls /api/*)."""
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"),
                        media_type="text/html")


# --- Auth: single-use link redemption -> sliding session cookie -------------
#
# v3 (2026-07-08). The CF-minted URL token is ONLY accepted at POST
# /api/session, exactly once: it's exchanged for a signed session token in an
# HttpOnly cookie. A copied/leaked URL is dead after first open (and the UI
# strips it from the address bar anyway). The session slides +1h on renewal
# while the tab is active, hard-capped at SESSION_CAP from the LINK's mint
# time. Stateless (the cookie IS the signed state) — survives restarts; only
# the redeemed-links set is in memory (worst case after a restart: a
# not-yet-expired link could redeem again, ≤1h window).

CF_TOKEN_TTL = 3600          # the CF mints links with exp = mint + 1h
SESSION_TTL = 3600           # sliding step
SESSION_CAP = 12 * 3600      # absolute max session age, from link mint time
SESSION_COOKIE = "fb_session"

# NOTE: in-process store -> the service MUST run with --workers 1 (see
# file-bridge.service); with N workers each has its own set and a link
# could redeem N times. Same constraint already applied to _buckets.
# Persisted to disk (v3.1) so a service restart can't re-open a link.
_redeemed: Dict[str, int] = {}  # sha256(link-token) -> link exp (pruned)
_redeem_lock = threading.Lock()
REDEEMED_PATH = os.environ.get(
    "FILE_BRIDGE_REDEEMED_FILE",
    os.path.join(os.path.dirname(__file__), "redeemed.json"))


def _prune_redeemed() -> None:
    now = int(time.time())
    for k in [k for k, exp in _redeemed.items() if exp < now]:
        _redeemed.pop(k, None)


def _load_redeemed() -> None:
    try:
        with open(REDEEMED_PATH) as f:
            data = json.load(f)
        now = int(time.time())
        _redeemed.update({str(k): int(v) for k, v in data.items()
                          if int(v) >= now})
    except FileNotFoundError:
        pass
    except Exception:  # corrupt file -> start empty (worst: one re-redeem)
        pass


def _save_redeemed() -> None:
    tmp = REDEEMED_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(_redeemed, f)
    os.replace(tmp, REDEEMED_PATH)


_load_redeemed()


def _bearer(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def _session_from_cookie(request: Request) -> Optional[Dict[str, Any]]:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        payload = bridge.verify_token(raw)
    except bridge.TokenError:
        return None
    # session tokens carry sid+cap; a raw LINK token pasted as a cookie
    # must not pass here (links only redeem at /api/session)
    if not payload.get("sid") or not payload.get("cap"):
        return None
    return payload


def _set_session_cookie(response: Response, sess: Dict[str, Any]) -> None:
    max_age = max(1, int(sess["cap"]) - int(time.time()))
    response.set_cookie(SESSION_COOKIE, bridge.sign_token(sess),
                        max_age=max_age, httponly=True, secure=True,
                        samesite="strict", path="/")


def require_session(request: Request) -> Dict[str, Any]:
    sess = _session_from_cookie(request)
    if not sess:
        raise HTTPException(401, "session expired")
    return sess


@app.post("/api/session")
def api_session(request: Request, response: Response,
                t: Optional[str] = Query(None)):
    """Redeem-or-resume. A presented LINK token (Bearer or ?t=) always mints
    a NEW session — a browser OPEN — replacing any existing cookie. Only when
    NO token is presented (the ~20-min renewal ping) do we slide the existing
    cookie session. The UI sends the link token at boot and nothing on renew.

    CRITICAL: the token must be checked BEFORE the cookie. Otherwise opening a
    second server's browser in the same browser keeps the FIRST server's
    session (the cookie is one per files-* origin) — a cross-customer leak
    where e.g. an admin who opened customer A's browser then opened customer
    B's kept seeing A's servers (and had SMB access to them). 2026-07-08."""
    now = int(time.time())
    raw = _bearer(request) or t
    if raw:
        try:
            payload = bridge.verify_token(raw)
        except bridge.TokenError as e:
            raise HTTPException(401, f"invalid token: {e}")
        if payload.get("sid"):
            raise HTTPException(401, "not a link token")
        # Host binding (v3.1): the CF stamps the hostname the link was minted
        # for; without this a link could redeem once per REGION (each base has
        # its own redeemed set, and both serve both files-* hostnames).
        req_host = (request.headers.get("host") or "").split(":")[0].lower()
        if str(payload.get("host") or "").lower() != req_host:
            raise HTTPException(401, "wrong host")
        digest = hashlib.sha256(raw.encode()).hexdigest()
        minted_at = int(payload["exp"]) - CF_TOKEN_TTL
        cap = minted_at + SESSION_CAP
        if now >= cap:  # can't happen while links live 1h, keep the invariant
            raise HTTPException(401, "link too old")
        with _redeem_lock:
            _prune_redeemed()
            if digest in _redeemed:
                raise HTTPException(401, "link already used")
            _redeemed[digest] = int(payload["exp"])
            _save_redeemed()
        sess = {"uid": payload["uid"], "servers": payload["servers"],
                "sid": secrets.token_hex(8), "cap": cap,
                "exp": min(now + SESSION_TTL, cap)}
        _set_session_cookie(response, sess)
        return {"ok": True, "exp": sess["exp"], "cap": cap}
    # No token → renewal ping: slide the existing cookie session.
    sess = _session_from_cookie(request)
    if sess:
        sess["exp"] = min(now + SESSION_TTL, int(sess["cap"]))
        _set_session_cookie(response, sess)
        return {"ok": True, "exp": sess["exp"], "cap": sess["cap"],
                "resumed": True}
    raise HTTPException(401, "missing token")


def _authz_vmid(payload: Dict[str, Any], vmid: int) -> bridge.ServerRef:
    for s in payload.get("servers", []):
        if int(s.get("vmid")) == int(vmid):
            return bridge.ServerRef(vmid=int(vmid), name=s.get("name") or str(vmid),
                                    stype=s.get("type") or "")
    raise HTTPException(403, f"server {vmid} not in your session")


# --- Per-session throttle (token-bucket, keyed by uid) ----------------------

_buckets: Dict[str, List[float]] = {}  # uid -> [tokens, last_ts]


def _throttle(uid: str, nbytes: int) -> None:
    rate = bridge.THROTTLE_BYTES_PER_S
    now = time.monotonic()
    tokens, last = _buckets.get(uid, [rate, now])
    tokens = min(rate, tokens + (now - last) * rate)
    if tokens < nbytes:
        time.sleep((nbytes - tokens) / rate)
        tokens = 0
    else:
        tokens -= nbytes
    _buckets[uid] = [tokens, time.monotonic()]


# --- Endpoints --------------------------------------------------------------

@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/servers")
def servers(payload: Dict[str, Any] = Depends(require_session)):
    """The dropdown: the user's servers embedded in the token."""
    return {"servers": [
        {"vmid": s["vmid"], "name": s.get("name"), "type": s.get("type")}
        for s in payload.get("servers", [])
    ]}


@app.get("/api/list")
def api_list(vmid: int, path: str = "", offset: int = 0,
             payload: Dict[str, Any] = Depends(require_session)):
    _authz_vmid(payload, vmid)
    try:
        return bridge.list_dir(vmid, path, offset=offset)
    except bridge.TokenError as e:
        raise HTTPException(400, str(e))
    except OSError as e:
        raise HTTPException(502, f"SMB error: {e}")


@app.get("/api/download")
def api_download(vmid: int, path: str,
                 payload: Dict[str, Any] = Depends(require_session)):
    _authz_vmid(payload, vmid)
    uid = payload["uid"]
    name = bridge._safe_rel(path).split("\\")[-1] or "download"

    def gen():
        with bridge.open_read(vmid, path) as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                _throttle(uid, len(chunk))
                yield chunk

    return StreamingResponse(
        gen(), media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/download-folder")
def api_download_folder(vmid: int, path: str,
                        payload: Dict[str, Any] = Depends(require_session)):
    """Zip-on-the-fly of a folder (capped). Streamed; not seekable, so the
    zip is stored (no compression) to keep memory flat."""
    _authz_vmid(payload, vmid)
    uid = payload["uid"]
    import smbclient
    from smbprotocol.exceptions import SMBLinkRedirectionError
    _srv, root = bridge._smb_session(vmid)
    base_rel = bridge._safe_rel(path)
    base_name = base_rel.split("\\")[-1] or f"vm{vmid}"

    def walk(rel: str):
        for nm in smbclient.listdir(bridge._unc(root, rel)):
            child = (rel + "\\" + nm) if rel else nm
            try:
                st = smbclient.stat(bridge._unc(root, child))
            except SMBLinkRedirectionError:
                continue  # don't zip through symlinks (cross-server links)
            if st.st_mode & 0o040000:
                yield from walk(child)
            else:
                yield child

    def gen():
        buf = io.BytesIO()
        zf = zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED)
        for filerel in walk(base_rel):
            arc = filerel[len(base_rel):].lstrip("\\").replace("\\", "/")
            with bridge.open_read(vmid, filerel) as f, \
                    zf.open(arc or filerel.split("\\")[-1], "w") as zdst:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    _throttle(uid, len(chunk))
                    zdst.write(chunk)
            data = buf.getvalue()
            if data:
                yield data
                buf.seek(0)
                buf.truncate(0)
        zf.close()
        yield buf.getvalue()

    return StreamingResponse(
        gen(), media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{base_name}.zip"'})


@app.get("/api/download-zip")
def api_download_zip(vmid: int, path: List[str] = Query(...),
                     payload: Dict[str, Any] = Depends(require_session)):
    """One ZIP of a multi-selection (files and/or folders; repeated ?path=).
    Streamed like download-folder; symlinks skipped; arcname collisions
    deduped with a numeric suffix."""
    _authz_vmid(payload, vmid)
    uid = payload["uid"]
    import smbclient
    from smbprotocol.exceptions import SMBLinkRedirectionError
    _srv, root = bridge._smb_session(vmid)
    rels = [bridge._safe_rel(p) for p in path if bridge._safe_rel(p)]
    if not rels:
        raise HTTPException(400, "no paths")

    def walk(rel: str):
        for nm in smbclient.listdir(bridge._unc(root, rel)):
            child = rel + "\\" + nm
            try:
                st = smbclient.stat(bridge._unc(root, child))
            except SMBLinkRedirectionError:
                continue
            if st.st_mode & 0o040000:
                yield from walk(child)
            else:
                yield child

    used: set = set()

    def arcname(name: str) -> str:
        cand, n = name, 1
        while cand.lower() in used:
            n += 1
            stem, dot, ext = name.rpartition(".")
            cand = f"{stem} ({n}).{ext}" if dot else f"{name} ({n})"
        used.add(cand.lower())
        return cand

    def items():  # (arcname, file-rel) pairs, folders recursed
        for rel in rels:
            base = rel.split("\\")[-1]
            try:
                st = smbclient.stat(bridge._unc(root, rel))
            except SMBLinkRedirectionError:
                continue
            if st.st_mode & 0o040000:
                prefix = arcname(base)
                for frel in walk(rel):
                    yield prefix + "/" + frel[len(rel):].lstrip("\\").replace("\\", "/"), frel
            else:
                yield arcname(base), rel

    first = rels[0].split("\\")[-1]
    zname = first if len(rels) == 1 else f"{first} (+{len(rels)-1})"

    def gen():
        buf = io.BytesIO()
        zf = zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED)
        for arc, frel in items():
            with bridge.open_read(vmid, frel) as f, zf.open(arc, "w") as zdst:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    _throttle(uid, len(chunk))
                    zdst.write(chunk)
            data = buf.getvalue()
            if data:
                yield data
                buf.seek(0)
                buf.truncate(0)
        zf.close()
        yield buf.getvalue()

    return StreamingResponse(
        gen(), media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zname}.zip"'})


@app.post("/api/upload")
async def api_upload(request: Request, vmid: int, path: str,
                     file: UploadFile,
                     payload: Dict[str, Any] = Depends(require_session)):
    _authz_vmid(payload, vmid)
    uid = payload["uid"]
    dest = bridge._safe_rel(path)
    written = 0
    with bridge.open_write(vmid, dest) as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > bridge.MAX_UPLOAD_BYTES:
                raise HTTPException(413, "file exceeds the upload limit")
            _throttle(uid, len(chunk))
            out.write(chunk)
    return {"ok": True, "bytes": written}


class _Op:  # simple request bodies (avoid pydantic import churn)
    pass


@app.post("/api/mkdir")
async def api_mkdir(request: Request, payload: Dict[str, Any] = Depends(require_session)):
    body = await request.json()
    vmid = int(body["vmid"]); _authz_vmid(payload, vmid)
    bridge.mkdir(vmid, body["path"])
    return {"ok": True}


@app.post("/api/rename")
async def api_rename(request: Request, payload: Dict[str, Any] = Depends(require_session)):
    body = await request.json()
    vmid = int(body["vmid"]); _authz_vmid(payload, vmid)
    bridge.rename(vmid, body["path"], body["newPath"])
    return {"ok": True}


@app.post("/api/delete")
async def api_delete(request: Request, payload: Dict[str, Any] = Depends(require_session)):
    body = await request.json()
    vmid = int(body["vmid"]); _authz_vmid(payload, vmid)
    bridge.remove(vmid, body["path"])
    return {"ok": True}


@app.post("/api/paste")
async def api_paste(request: Request, payload: Dict[str, Any] = Depends(require_session)):
    """Clipboard paste: {items:[{vmid,path}], destVmid, destPath, op}.
    Every source vmid AND the destination must be in the session."""
    body = await request.json()
    op = body.get("op")
    if op not in ("copy", "move"):
        raise HTTPException(400, "op must be copy or move")
    dst_vmid = int(body["destVmid"]); _authz_vmid(payload, dst_vmid)
    items = body.get("items") or []
    for it in items:
        _authz_vmid(payload, int(it["vmid"]))
    try:
        return bridge.paste(items, dst_vmid, body.get("destPath", ""), op)
    except bridge.TokenError as e:
        raise HTTPException(400, str(e))
    except OSError as e:
        raise HTTPException(502, f"SMB error: {e}")

"""FastAPI HTTP layer for the NeuraVPS web file browser bridge.

nginx (files-hel/-fsn.neuravps.com) terminates TLS and proxies here. Every
request carries the HMAC token (Authorization: Bearer, or ?t= for the
download/EventSource cases where a header can't be set). Ownership is the
token's server list; passwords are resolved base-side in bridge.py.

Run: uvicorn app:app --host 127.0.0.1 --port 8088  (see systemd unit).
"""

import io
import time
import zipfile
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

import bridge

app = FastAPI(title="NeuraVPS file bridge", docs_url=None, redoc_url=None)


# --- Auth dependency --------------------------------------------------------

def _token_from(request: Request, t: Optional[str]) -> Dict[str, Any]:
    raw = None
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        raw = auth[7:].strip()
    raw = raw or t
    if not raw:
        raise HTTPException(401, "missing token")
    try:
        return bridge.verify_token(raw)
    except bridge.TokenError as e:
        raise HTTPException(401, f"invalid token: {e}")


def require_token(request: Request, t: Optional[str] = Query(None)) -> Dict[str, Any]:
    return _token_from(request, t)


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
def servers(payload: Dict[str, Any] = Depends(require_token)):
    """The dropdown: the user's servers embedded in the token."""
    return {"servers": [
        {"vmid": s["vmid"], "name": s.get("name"), "type": s.get("type")}
        for s in payload.get("servers", [])
    ]}


@app.get("/api/list")
def api_list(vmid: int, path: str = "", offset: int = 0,
             payload: Dict[str, Any] = Depends(require_token)):
    _authz_vmid(payload, vmid)
    try:
        return bridge.list_dir(vmid, path, offset=offset)
    except bridge.TokenError as e:
        raise HTTPException(400, str(e))
    except OSError as e:
        raise HTTPException(502, f"SMB error: {e}")


@app.get("/api/download")
def api_download(request: Request, vmid: int, path: str,
                 t: Optional[str] = Query(None)):
    payload = _token_from(request, t)
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
def api_download_folder(request: Request, vmid: int, path: str,
                        t: Optional[str] = Query(None)):
    """Zip-on-the-fly of a folder (capped). Streamed; not seekable, so the
    zip is stored (no compression) to keep memory flat."""
    payload = _token_from(request, t)
    _authz_vmid(payload, vmid)
    uid = payload["uid"]
    import smbclient
    _srv, root = bridge._smb_session(vmid)
    base_rel = bridge._safe_rel(path)
    base_name = base_rel.split("\\")[-1] or f"vm{vmid}"

    def walk(rel: str):
        for nm in smbclient.listdir(bridge._unc(root, rel)):
            child = (rel + "\\" + nm) if rel else nm
            st = smbclient.stat(bridge._unc(root, child))
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


@app.post("/api/upload")
async def api_upload(request: Request, vmid: int, path: str,
                     file: UploadFile,
                     payload: Dict[str, Any] = Depends(require_token)):
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
async def api_mkdir(request: Request, payload: Dict[str, Any] = Depends(require_token)):
    body = await request.json()
    vmid = int(body["vmid"]); _authz_vmid(payload, vmid)
    bridge.mkdir(vmid, body["path"])
    return {"ok": True}


@app.post("/api/rename")
async def api_rename(request: Request, payload: Dict[str, Any] = Depends(require_token)):
    body = await request.json()
    vmid = int(body["vmid"]); _authz_vmid(payload, vmid)
    bridge.rename(vmid, body["path"], body["newPath"])
    return {"ok": True}


@app.post("/api/delete")
async def api_delete(request: Request, payload: Dict[str, Any] = Depends(require_token)):
    body = await request.json()
    vmid = int(body["vmid"]); _authz_vmid(payload, vmid)
    bridge.remove(vmid, body["path"])
    return {"ok": True}


@app.post("/api/paste")
async def api_paste(request: Request, payload: Dict[str, Any] = Depends(require_token)):
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

# NeuraVPS web file browser — SMB bridge

Thin userspace-SMB bridge that powers the panel's **"Navegador web"** file
browser. Runs on each BASE; talks SMB to the customer's VMs by their direct
vmbr0 IPv6 `:445` (both bases reach every VM directly — no bridge-to-bridge).
Cloud Functions stay OUT of the data path (base↔VM↔customer is free Hetzner
traffic); a Cloud Function only mints the access token.

## Flow (v3 auth, 2026-07-08)

```
panel "Abrir navegador de archivos" (on server X)
  → CF mints HMAC LINK token {uid, servers:[{vmid,name,type}], exp=+1h}
    (ownership-checked; NO passwords in the token)
  → panel opens files-hel/-fsn.neuravps.com/?lang=..&theme=..#t=<token>
    (token in the FRAGMENT: never sent to the server → never in logs)
    → UI redeems it ONCE at POST /api/session → HttpOnly Secure
      SameSite=Strict cookie (sliding 1h, hard cap 12h from link mint);
      the token is stripped from the address bar. A copied/leaked URL is
      dead after first open ("link already used").
    → every endpoint authenticates by the session cookie only; ops are
      scoped to the session's servers; ipv6+password resolved from
      Firestore (base-side); SMB by direct vmbr0 IPv6.
```

Session renewal: the UI re-POSTs /api/session every ~20 min while visible
(sliding). On 401 it shows a "session expired" modal. The redeemed-links
set is in-process → **`--workers 1` is mandatory** (see the unit file);
worst case after a service restart a not-yet-expired link could redeem
again (≤1h window, accepted).

## Pieces

| File | Role |
|---|---|
| `bridge.py` | SMB core: token verify/sign, Firestore credential lookup, list/read/write/mkdir/rename/delete, intra-server native move + cross-server streamed copy/move (`paste`). |
| `app.py` | FastAPI HTTP layer: single-use link redemption + session cookie (`/api/session`), per-uid throttle, endpoints (servers/list/download/download-folder/upload/mkdir/rename/delete/paste). |
| `file-bridge.service` | systemd unit (`/opt/file-bridge`, uvicorn 127.0.0.1:8088, hardened). |
| `requirements.txt` | venv deps. |
| `test_bridge_http.py` | HTTP-layer tests (mocked SMB). |

## Deploy (per base)

```bash
apt install -y python3.13-venv
mkdir -p /opt/file-bridge && cp bridge.py app.py /opt/file-bridge/
python3 -m venv /opt/file-bridge/venv
/opt/file-bridge/venv/bin/pip install -r requirements.txt
mkdir -p /etc/neuravps
cat > /etc/neuravps/file-bridge.env <<EOF
FILE_BRIDGE_TOKEN_SECRET=<= GCP secret FILE_BRIDGE_TOKEN_SECRET>
FIREBASE_CREDENTIALS_FILE=/etc/firebase-credentials.json
EOF
chmod 600 /etc/neuravps/file-bridge.env
cp file-bridge.service /etc/systemd/system/
systemctl enable --now file-bridge
# nginx: add files-hel/-fsn.neuravps.com server block proxying to 127.0.0.1:8088
# cert: add the SANs to the neuravps-dual DNS-01 cert
```

## Model & limits

- **VM must be ON** (SMB needs the guest live). NTFS host-mount is rejected.
- **Single-pane, Windows-explorer style + clipboard**: select → Copiar/Cortar →
  navigate (same server or switch server) → Pegar. Clipboard holds
  `{vmid,path}`, so cross-server paste is natural. Same-VM move = atomic
  server-side rename; everything else streams through the bridge.
- Latency is not critical (not RDP) → the bridge favors robustness.
- Throttle 40 MB/s/session (protect RDP QoS on the shared base NIC); upload
  cap 20 GB; paginated listings (SQX databank dirs have tens of thousands of
  files); folder download = zip-on-the-fly.
- Native mapped SMB stays the documented path for very large moves.

## Validated (2026-07-07, against test VM 201/444)

- SMB reachable on `:445` by direct IPv6 from BOTH bases, cross-region.
- Auth + `C$` listing, write, read-back, native move, recursive copy.
- Cross-server + cross-region copy (201 Falkenstein → 444 Helsinki, byte-exact).
- HTTP layer: 10/10 (auth 401s, ownership 403s, paste authz, download `?t=`).

## Validated v3 (2026-07-08, live on both bases)

- Redeem 200 + cookie → reuse of the same link 401 ("incognito copy" dead).
- Link token rejected on normal endpoints; session token can't redeem.
- Cookie-only renewal (`resumed:true`), real SMB list of VM 201 via cookie.
- HTTP suite 21/21 (single-use, cap=mint+12h, tampered cookie, downloads
  via cookie, foreign-vmid 403s).

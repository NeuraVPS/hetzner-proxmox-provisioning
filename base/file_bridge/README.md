# NeuraVPS web file browser — SMB bridge

Thin userspace-SMB bridge that powers the panel's **"Navegador web"** file
browser. Runs on each BASE; talks SMB to the customer's VMs by their direct
vmbr0 IPv6 `:445` (both bases reach every VM directly — no bridge-to-bridge).
Cloud Functions stay OUT of the data path (base↔VM↔customer is free Hetzner
traffic); a Cloud Function only mints the access token.

## Flow

```
panel "Abrir navegador de archivos" (on server X)
  → CF mints HMAC token {uid, servers:[{vmid,name,type,ipv6}], exp=+1h}
    (ownership-checked; NO passwords in the token)
  → redirect to files-hel/-fsn.neuravps.com  (base nginx, neuravps-dual cert)
    → this bridge (FastAPI): validates HMAC, scopes every op to the token's
      servers, resolves ipv6+password from Firestore (base-side), does SMB.
```

## Pieces

| File | Role |
|---|---|
| `bridge.py` | SMB core: token verify, Firestore credential lookup, list/read/write/mkdir/rename/delete, intra-server native move + cross-server streamed copy/move (`paste`). |
| `app.py` | FastAPI HTTP layer: auth dependency, per-uid throttle, endpoints (servers/list/download/download-folder/upload/mkdir/rename/delete/paste). |
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

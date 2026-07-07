# file-bridge — base deployment record

What was changed on the BASES (b0 + b1) for the web file browser. All done
2026-07-07 with zero customer impact (bridge is localhost-only until nginx;
nginx changes are additive + `nginx -t`-gated + reload-not-restart; the
customer-facing `neuravps-dual` cert was NOT touched — a separate cert is used;
DNS was flipped only after the serve path was verified via `curl --resolve`).

## Per base (b0 and b1, identical)

1. **Package**: `apt install python3.13-venv`.
2. **App**: `/opt/file-bridge/{bridge.py,app.py,requirements.txt}` (from
   `base/file_bridge/` in this repo).
3. **venv**: `python3 -m venv --system-site-packages /opt/file-bridge/venv`
   then `pip install fastapi uvicorn[standard] python-multipart smbprotocol`
   (firebase-admin reused from system site-packages).
4. **Env**: `/etc/neuravps/file-bridge.env` (mode 600):
   `FILE_BRIDGE_TOKEN_SECRET=` (= GCP secret of the same name) +
   `FIREBASE_CREDENTIALS_FILE=/etc/firebase-credentials.json`.
5. **Service**: `/etc/systemd/system/file-bridge.service` → `systemctl
   enable --now file-bridge`. uvicorn on **127.0.0.1:8088** (localhost only).
6. **Cert**: a DEDICATED `file-bridge` cert (NOT neuravps-dual), DNS-01 via
   the existing `/opt/letsencrypt/certbot-hetzner-dns-hooks.py`:
   `certbot certonly --cert-name file-bridge -d files-hel.neuravps.com -d
   files-fsn.neuravps.com` (auto-renews). Expires 2026-10-05.
7. **nginx**: `/etc/nginx/sites-available/file-bridge.conf` (from
   `nginx-file-bridge.conf` here) symlinked into `sites-enabled/`, serving
   `files-hel`/`files-fsn` on 443 → `127.0.0.1:8088`, using the file-bridge
   cert. `nginx -t && systemctl reload nginx`. Both names served on both
   bases (survives a base failover).

## DNS (Hetzner Cloud API, zone neuravps.com id=1395035)

- `files-hel` CNAME → `sqx-hel.neuravps.com.` (→ failover VIP 77.42.49.79, b1)
- `files-fsn` CNAME → `sqx-fsn.neuravps.com.` (→ failover VIP 94.130.3.118, b0)

Inherits the failover VIPs, so the browser rides base failover automatically.

## GCP secret

`FILE_BRIDGE_TOKEN_SECRET` (Firebase functions secret) — shared HMAC key
between the token-minting Cloud Function (phase 4, pending) and both bridges.

## Verified 2026-07-07

- Bridge health on 127.0.0.1:8088 (both bases).
- Authenticated list of a real VM's C: through the DEPLOYED bridge.
- Existing PVE console + sqx redirects intact after each nginx reload.
- files-hel/-fsn serve the bridge with a valid cert (curl --resolve, then
  publicly after DNS) — full path DNS→nginx→bridge→Firestore→SMB.

## Still pending (not on the bases)

- **Phase 4**: Cloud Function that mints the HMAC token (ownership-checked,
  clones the VNC set-ticket flow) using FILE_BRIDGE_TOKEN_SECRET.
- **Phase 5**: panel UI (single-pane + clipboard, Chonky) in the "Transferir
  archivos" section, new "Navegador web" tab.

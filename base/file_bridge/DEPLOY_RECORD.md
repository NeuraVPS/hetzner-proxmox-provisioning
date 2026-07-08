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

## Shipped after (not on the bases — NeuraVPS repo)

- **Phase 4** (2026-07-07, NeuraVPS PR #103/#104): Cloud Function
  `get_file_browser_url` mints the HMAC token (ownership-checked, linked
  accounts expanded) using FILE_BRIDGE_TOKEN_SECRET.
- **Phase 5** (2026-07-07, NeuraVPS PR #103): panel UI — "Navegador web" tab
  in "Transferir archivos"; the bridge serves its own single-pane UI at `/`
  (`static/index.html` here).

## Update 2026-07-08 (v3.4) — CROSS-TENANT LEAK FIX: redeem link over stale cookie

- **Bug (cross-customer leak):** `/api/session` was "resume-or-redeem" —
  it checked the `fb_session` cookie FIRST and, if valid, slid it and
  **ignored any presented link token**. Since the cookie is one per
  `files-*` origin, opening a SECOND customer's browser in the same browser
  kept the FIRST session: an admin who opened customer A's file browser and
  then customer B's kept seeing A's servers (VMs 1920/1903/1867/1868 of
  mj_bradford surfaced while opening admin@hobbiecode's) **and had SMB
  read/write access to them.** (Non-admins were never affected — they can
  only ever mint links for their own servers.)
- **Fix:** a presented link token (Bearer/`?t=`) is now redeemed FIRST into
  a NEW session, replacing any existing cookie (a browser OPEN); the cookie
  is only resumed when NO token is presented (the ~20-min renewal ping).
- Deployed b0+b1 (restart). Tests 31/31 (3 new cross-session cases) +
  live E2E on files-hel: link A→[201], then link B on the same cookie→[777]
  (not [201]).

## Update 2026-07-08 (v3.3) — pre-select the origin server (?vmid=)

- `static/index.html`: `loadServers()` reads `?vmid=` and pre-selects that
  server in the dropdown (falls back to the first if absent/not in the list).
  The token-minting CF (NeuraVPS `get_file_browser_url`) now appends
  `&vmid=<clicked server's proxmoxId>` to the redirect. Backward-compatible
  both ways (old CF → no vmid → first server; old bridge → ignores the param).
- Companion: NeuraVPS adds a `/files?serverId=` Firebase page (a stable,
  copyable entry point like `/console`) that mints + redirects here; the panel
  and admin/servers now link to it.
- UI-only on the base; copied to b0+b1 `/opt/file-bridge/static/` (served per
  request, no restart).

## Update 2026-07-08 (v3.2) — symlink 500s + UI path desync

- Operator repro on VM 203: opening `My Servers`/`Users` → 500. Cause:
  `smbclient.stat()` raises `SMBLinkRedirectionError` (NOT an OSError) on
  Windows SYMLINKS resolved client-side — `C:\My Servers` holds the
  cross-server SMB links (setup_smb_symlinks), `Users` has `All Users`.
  Junctions are fine (server-side). Fix: `list_dir` skips symlink entries
  (like Explorer hides them; the web UI's server dropdown IS the
  cross-server path), and the recursive walkers (`_rmtree`, `_copy_tree`,
  zip `walk`) skip/handle them instead of aborting mid-operation.
- UI: a failed navigation left `STATE.path` on the broken folder while the
  view stayed put → next double-clicks chained nonsense paths
  (`My Servers\MetaTrader\Program Files`). `list()` now reverts to the
  last successfully listed path on error (+ stale-response guard).
- Deployed b0+b1 (restart) — live verify on VM 203: `My Servers` and
  `Users` list 200 with links skipped. Suite 26/26.

## Update 2026-07-08 (v3.1) — redeemed-set persistence + host binding

- The two v3 residuals are closed (operator OK'd invalidating old links):
  - **Persistence**: the redeemed-links set is written to
    `/opt/file-bridge/redeemed.json` (atomic tmp+rename, pruned, loaded at
    boot) → a service restart can no longer re-open a used link.
  - **Host binding**: the CF stamps `host` (files-hel/-fsn.neuravps.com)
    into the link token and the bridge only redeems on that hostname —
    before, a link could redeem once per REGION (each base has its own
    set and both serve both hostnames).
- Deploy order: CF first (old bridge ignores the claim), then bridges
  (hostless links rejected — pre-deploy links ≤1h die, user re-clicks).
- Verified live: wrong-region 401 / right-region 200 / reuse 401 /
  **reuse after restart 401** / old session cookie after restart 200.
  Suite 25/25.

## Update 2026-07-08 (v3) — single-use links + session cookie + renewal

- `app.py`/`bridge.py`/`static/index.html` v3 on both bases + **unit file
  changed to `--workers 1`** (`daemon-reload` + restart): the in-process
  single-use set and throttle buckets require one worker (with 2 workers the
  reuse-block failed live — caught in E2E).
- Auth model: CF link token redeems ONCE at `POST /api/session` → HttpOnly
  Secure SameSite=Strict cookie, sliding 1h / capped 12h from link mint;
  normal endpoints are cookie-only now (link tokens rejected — hard cut, old
  open tabs die within their hour). Panel sends the token as `#t=` fragment
  (never in server logs); UI strips it from the address bar after redeeming.
- Restart residual (accepted): the redeemed set is in-memory, so after a
  service restart a not-yet-expired link (≤1h) could redeem once more.
- Verified live on files-hel AND files-fsn: redeem 200+cookie / same-link
  reuse 401 / link-on-endpoint 401 / cookie renewal `resumed:true` / real
  SMB list via cookie. HTTP suite 21/21 (run on b1 venv).

## Update 2026-07-08 — UI i18n + dark/light theme

- `static/index.html` v2: es/en translation + dark/light theme. Reads
  `?lang=es|en` and `?theme=dark|light` (appended by the panel, which knows
  the user's locale + next-themes theme — the bridge is cross-origin so it
  can't read the panel's localStorage); falls back to `navigator.language`
  (es→es, else en) and `prefers-color-scheme`. Copied to
  `b0:/opt/file-bridge/static/index.html` + b1 same (no service restart —
  FileResponse reads per request); md5-verified repo↔b0↔b1 through the
  public path.

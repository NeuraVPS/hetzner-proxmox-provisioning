# PVE proxy on BASE (deprecated runbook)

**Deprecated — use [base_setup.sh](../base_setup.sh) (PVE proxy section) as the source of truth.**

- **Backends:** Firestore collection **`proxmox_nodes`** (document ID = node slug, e.g. `0000009-EX44`; field **`ip`** = public IPv6). BASE runs **`sync-base-nat.py sync nodes`** on boot and can run **`sync nodes add|del|<nodeId>`** (see `base_setup.sh`).
- **Nginx:** Repo snippets **`snippets/pve-proxy-map.conf`**, **`snippets/pve-proxy-backends.map.conf.example`** (initial empty map), **`snippets/neuravps-redirects.conf`** (wildcard `*.pve`, set-ticket, sub_filter for noVNC). No fd00 / NJS backend math anymore.
- **Set-ticket:** [scripts/pve-set-ticket.py](../scripts/pve-set-ticket.py) + **`snippets/pve-set-ticket.service`**.

The rest of this file is kept for **Let’s Encrypt wildcard (DNS-01) + Namecheap** notes (section 7) and historical context. Ignore fd00 / NJS `backend_from_host` sections below.

---

## Historical (fd00 — no longer used)

Proxmox VE UI and console are exposed only via the **wildcard subdomain:** `https://NODEID.pve.neuravps.com/` (e.g. `https://000001-EX44.pve.neuravps.com/`). The hostname alone selects the node; all traffic is proxied with no path or cookie logic. Requires DNS A/AAAA for `*.pve.neuravps.com` and a wildcard TLS cert (DNS-01, see section 7).

`sqx.neuravps.com` and `trading.neuravps.com` redirect every request to `https://www.neuravps.com` (path preserved); `.well-known/acme-challenge/` is served for Let's Encrypt renewal.

~~Node IDs formerly mapped to fd00 backends~~ — replaced by **`proxmox_nodes`** + generated nginx map.

**Server:** BASE node (e.g. 0000001-BASE).  
**Packages:** `nginx`, `libnginx-mod-http-js` (NJS not required for backend URL if using map).  
**Resolver:** When using variable `proxy_pass`, a `resolver` is required (e.g. in `server` or `http`): `resolver 127.0.0.53 valid=10s;`

---

## 1. Map (http context)

Must be in the **http** block (not inside server). Create a file that is included from `http {}`, e.g. `/etc/nginx/conf.d/pve-proxy-map.conf`:

```nginx
# Node name from wildcard host (e.g. 000001-EX44.pve.neuravps.com → 000001-EX44)
map $host $pve_node_from_host {
    "~^([0-9]+-[^.]+)\\.pve\\.neuravps\\.com$" $1;
    default "";
}

# WebSocket: set Connection to "upgrade" only when client sends Upgrade (avoids breaking normal API)
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}
```

---

## 2. NJS script: `/etc/nginx/njs/pve_proxy.js`

Create the directory if needed: `sudo mkdir -p /etc/nginx/njs`

```javascript
// Backend from wildcard host (e.g. 000001-EX44.pve.neuravps.com) – reads pve_node_from_host
function backend_from_host(r) {
  var node = (r.variables.pve_node_from_host || "").toString().trim();
  var match = node.match(/^([0-9]+)-/);
  if (!match) return "";
  var dec = parseInt(match[1], 10);
  if (isNaN(dec)) return "";
  return "https://[fd00:4000::" + dec.toString(16) + "]:8006";
}

export default { backend_from_host };
```

---

## 3. Site config: `/etc/nginx/sites-enabled/neuravps-redirects.conf`

Use this as the full server block for the PVE proxy and redirects. Adjust `server_name`, SSL paths, and `return 301` target if needed.

```nginx
js_import /etc/nginx/njs/pve_proxy.js;
js_set $pve_backend_from_host pve_proxy.backend_from_host;

# Wildcard PVE: NODEID.pve.neuravps.com → set-ticket to local app, everything else to node
server {
    server_name *.pve.neuravps.com;

    resolver 127.0.0.53 valid=10s;

    if ($pve_backend_from_host = "") {
        return 404;
    }

    proxy_cookie_domain ~^(.+)$ $host;
    proxy_cookie_path / /;

    # Set-ticket: redeem token, set PVEAuthCookie, redirect to PVE/noVNC. Cookie uses SameSite=None; Partitioned so it works when the console is embedded in an iframe from another origin (e.g. localhost or www.neuravps.com).
    # Disable proxy_cookie_domain here so our expire/set headers are not rewritten; we need Domain=.pve.neuravps.com
    # in the expire to clear old domain-scoped cookies, and no Domain on the new cookie (host-only).
    location /set-ticket {
        proxy_cookie_domain off;
        proxy_pass http://127.0.0.1:5000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location / {
        proxy_pass $pve_backend_from_host$request_uri;
        proxy_ssl_verify off;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_read_timeout 86400;
        proxy_send_timeout 86400;
    }

    listen 443 ssl;
    listen [::]:443 ssl;
    ssl_certificate /etc/letsencrypt/live/pve.neuravps.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/pve.neuravps.com/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;
}

server {
    listen 80;
    listen [::]:80;
    server_name *.pve.neuravps.com;
    return 301 https://$host$request_uri;
}

server {
    server_name trading.neuravps.com sqx.neuravps.com;

    location ^~ /.well-known/acme-challenge/ {
        root /var/www/letsencrypt;
        default_type "text/plain";
        try_files $uri =404;
    }

    location / {
        return 301 https://www.neuravps.com$request_uri;
    }

    listen 443 ssl;
    listen [::]:443 ssl;
    ssl_certificate /etc/letsencrypt/live/trading.neuravps.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/trading.neuravps.com/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;
}

server {
    listen 80;
    listen [::]:80;
    server_name trading.neuravps.com sqx.neuravps.com;

    location ^~ /.well-known/acme-challenge/ {
        root /var/www/letsencrypt;
        default_type "text/plain";
        try_files $uri =404;
    }

    location / {
        return 301 https://www.neuravps.com$request_uri;
    }
}
```

---

## 4. Apply on the BASE server

1. **Map**  
   Create `/etc/nginx/conf.d/pve-proxy-map.conf` with the map block from section 1 (or add that map inside `http {}` in `nginx.conf`).

2. **NJS**  
   Create `/etc/nginx/njs/pve_proxy.js` with the full script from section 2.

3. **Site**  
   Create or replace `/etc/nginx/sites-enabled/neuravps-redirects.conf` with the config from section 3.  
   The wildcard PVE server block needs a `resolver` for variable `proxy_pass`; the sqx/trading blocks do not.

4. **Test and reload**
   ```bash
   sudo nginx -t && sudo systemctl reload nginx
   ```

---

## 5. Quick reference

| What                                               | Where                                                                       |
| -------------------------------------------------- | --------------------------------------------------------------------------- |
| Map (`$pve_node_from_host`, `$connection_upgrade`) | `http {}` – e.g. `/etc/nginx/conf.d/pve-proxy-map.conf`                     |
| NJS (`backend_from_host`)                          | `/etc/nginx/njs/pve_proxy.js`                                               |
| Site config                                        | `/etc/nginx/sites-enabled/neuravps-redirects.conf`                          |
| Wildcard cert (optional until you use \*.pve)      | `/etc/letsencrypt/live/pve.neuravps.com/` (see section 7)                   |
| Resolver (if needed)                               | In each server block that uses `proxy_pass` with a variable or in `http {}` |

**Node → backend:** Host like `0000011-BASE` → decimal `11` → hex `b` → `https://[fd00:4000::b]:8006`.

---

## 6. VNC / noVNC notes

VNC and console (noVNC, xtermjs) are used only via the wildcard host (`NODEID.pve.neuravps.com`). The wildcard server block forwards WebSocket with `Upgrade` / `Connection $connection_upgrade` and long timeouts so `wss://.../api2/json/nodes/.../vncwebsocket` works. If WebSocket fails (e.g. 1006), check the request has `Upgrade: websocket` and that the node/firewall allows the connection.

### 6.1 noVNC send-text injection (paste to host)

The NeuraVPS console page (www.neuravps.com) embeds the noVNC iframe. To support “Send text to host” (paste text into the VNC session from a modal), the parent page uses `postMessage`; the noVNC page must run a small script that listens for messages and injects keystrokes into `noVNC_keyboardinput`. Because the iframe is cross-origin, that script cannot be added by the parent—it must be injected into the HTML served by the proxy.

**Script source:** [../scripts/novnc-send-string-inject.js](../scripts/novnc-send-string-inject.js). The script only exposes a single-event dispatch: it listens for `{ type: "noVNC_dispatchEvent", event }` (one keyboard event descriptor per message) and dispatches it on `noVNC_keyboardinput`. The parent (console page) builds the event list (text or key combo), implements the delay between events, and sends one postMessage per event. No regex or `$` in the injected code, so nginx is safe.

**Nginx:** In the wildcard PVE server block, update `location /` to inject the script before `</body>` and to request an uncompressed response so `sub_filter` can run:

- Add `proxy_set_header Accept-Encoding "";` so the backend returns uncompressed HTML.
- Add `sub_filter '</body>' '<script>...minified script...</script></body>';` and `sub_filter_once on;`.

Use the minified one-liner from the script file (remove comments and newlines). Example `location /` block:

```nginx
    location / {
        proxy_pass $pve_backend_from_host$request_uri;
        proxy_ssl_verify off;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Accept-Encoding "";
        proxy_read_timeout 86400;
        proxy_send_timeout 86400;
        sub_filter '</body>' '<script>(function(){"use strict";window.addEventListener("message",function(event){var data=event.data;if(!data||data.type!=="noVNC_dispatchEvent"||!data.event||typeof data.event!=="object")return;var e=data.event;if(e.type!=="keydown"&&e.type!=="keyup")return;var el=document.getElementById("noVNC_keyboardinput");if(!el)return;var opts={key:e.key,keyCode:e.keyCode};if(e.code!=null)opts.code=e.code;if(e.ctrlKey!=null)opts.ctrlKey=!!e.ctrlKey;if(e.altKey!=null)opts.altKey=!!e.altKey;if(e.shiftKey!=null)opts.shiftKey=!!e.shiftKey;if(e.metaKey!=null)opts.metaKey=!!e.metaKey;el.dispatchEvent(new KeyboardEvent(e.type,opts));});})();</script></body>';
        sub_filter_once on;
    }
```

No extra headers (CORS, X-Frame-Options, CSP) are required for postMessage; the iframe already loads from the node domain.

---

## 7. Wildcard subdomain (\*.pve.neuravps.com) and Let’s Encrypt

The server block for `*.pve.neuravps.com` (section 3) proxies `NODEID.pve.neuravps.com` to the PVE node for that ID. It uses the map `$pve_node_from_host` (section 1) and the NJS function `backend_from_host` (section 2).

**TLS:** Wildcard certificates (`*.pve.neuravps.com`) require **DNS-01** validation; HTTP-01 cannot be used for wildcards. Existing certs for `trading.neuravps.com` and `sqx.neuravps.com` stay on HTTP-01 and are unchanged.

### If the wildcard cert does not exist yet

Nginx will fail to start if the wildcard server block references missing cert files. Until you have the cert:

- **Option A:** Comment out the entire wildcard PVE server block (the `server { server_name *.pve.neuravps.com; ... }` that uses `ssl_certificate ... pve.neuravps.com`) and the HTTP redirect server for `*.pve.neuravps.com`. Reload nginx; sqx/trading redirects will still work. After obtaining the cert (below), uncomment and reload.
- **Option B:** Obtain the wildcard cert first (manual or automated), then start or reload nginx.

### Obtain the wildcard cert (manual DNS-01)

1. On the BASE server, run:
   ```bash
   sudo certbot certonly --manual --preferred-challenges dns \
     -d "*.pve.neuravps.com" -d "pve.neuravps.com"
   ```
2. When Certbot shows the TXT record, add it in Namecheap (or your DNS provider):
   - **Host:** `_acme-challenge.pve` (Certbot may show the full name)
   - **Value:** the token Certbot prints
   - **TTL:** 1–5 minutes to speed up propagation
3. Wait 1–2 minutes, then continue in Certbot. The cert will be written under `/etc/letsencrypt/live/pve.neuravps.com/` (or the first `-d` name).
4. Point the nginx wildcard server block’s `ssl_certificate` and `ssl_certificate_key` at that path (already set in section 3). Reload nginx.

### Automatic issuance and renewal with Namecheap API

You can issue and renew the wildcard cert with no manual steps by using Certbot's **manual auth/cleanup hooks** and the **Namecheap API** to create and delete the `_acme-challenge.pve` TXT record.

**Important:** Namecheap's `setHosts` API **replaces all** DNS host records for the domain. So the hook must (1) call `getHosts` to fetch current records, (2) add (auth) or remove (cleanup) the ACME TXT record, (3) call `setHosts` with the full list. Any record omitted in `setHosts` is deleted.

#### 1. Prerequisites

- **Namecheap API:** Enable API access (Namecheap → Profile → Tools → Business & Dev Tools → API Access). You must meet [Namecheap's API eligibility](https://www.namecheap.com/support/knowledgebase/article.aspx/9739/63/) (e.g. $50+ spent in the last 2 years).
- **Whitelist IP:** In the same API section, add your **BASE server's public IPv4** to the whitelist. Certbot runs on the server, so the API must allow that IP.
- **Credentials:** Note your **API User** (often your Namecheap username), **API Key**, and **Username**. The **ClientIp** must be the same whitelisted IPv4 (Certbot runs on the server, so use the server's public IP).

#### 2. Hook script (included in repo)

The repo includes a Python 3 script that uses only the standard library (`urllib`, `xml.etree`). No pip install.

- **Path:** [scripts/certbot-namecheap-dns-hooks.py](../scripts/certbot-namecheap-dns-hooks.py)
- **Copy to server:** e.g. `/opt/letsencrypt/certbot-namecheap-dns-hooks.py` (or `/etc/letsencrypt/`).
- **Make executable:** `chmod +x /opt/letsencrypt/certbot-namecheap-dns-hooks.py`.

The script reads these **environment variables** (set them in a small env file or export before running certbot):

| Variable              | Description                                               |
| --------------------- | --------------------------------------------------------- |
| `NAMECHEAP_API_USER`  | API user (often your Namecheap username).                 |
| `NAMECHEAP_API_KEY`   | Your API key from the API Access page.                    |
| `NAMECHEAP_USERNAME`  | Namecheap account username.                               |
| `NAMECHEAP_CLIENT_IP` | Your BASE server's **public IPv4** (must be whitelisted). |

**Behavior:**

- **Auth hook** (`auth`): Fetches current hosts with `getHosts`, adds a TXT record `_acme-challenge.pve` = `CERTBOT_VALIDATION`, calls `setHosts`, then sleeps 60 seconds so DNS can propagate before Certbot validates.
- **Cleanup hook** (`cleanup`): Fetches current hosts, removes the `_acme-challenge.pve` TXT record, calls `setHosts`.

Certbot sets `CERTBOT_DOMAIN` (e.g. `pve.neuravps.com` for `*.pve.neuravps.com`) and `CERTBOT_VALIDATION` (the token). The script derives SLD/TLD and the challenge host from `CERTBOT_DOMAIN`.

#### 3. First issuance (one-time)

Create an env file (e.g. `/opt/letsencrypt/namecheap.env`) with:

```bash
export NAMECHEAP_API_USER="your_api_user"
export NAMECHEAP_API_KEY="your_api_key"
export NAMECHEAP_USERNAME="your_username"
export NAMECHEAP_CLIENT_IP="1.2.3.4"
```

Replace with your values; `NAMECHEAP_CLIENT_IP` must be the BASE server's public IPv4. Secure the file: `chmod 600 /opt/letsencrypt/namecheap.env`.

Run certbot once with the hooks:

```bash
source /opt/letsencrypt/namecheap.env
sudo -E certbot certonly --manual --preferred-challenges dns \
  --manual-auth-hook   "/opt/letsencrypt/certbot-namecheap-dns-hooks.py auth" \
  --manual-cleanup-hook "/opt/letsencrypt/certbot-namecheap-dns-hooks.py cleanup" \
  -d "*.pve.neuravps.com" -d "pve.neuravps.com"
```

`-E` preserves the environment so the hook script sees the variables. Certbot will run the auth hook (script adds the TXT record), wait for DNS propagation (script sleeps 60s), validate, issue the cert, then run the cleanup hook (script removes the TXT). No manual DNS steps. The cert is written to `/etc/letsencrypt/live/pve.neuravps.com/`.

#### 4. Automatic renewal (cron)

Renewal uses the same hooks. Use a cron job that sources the env file and runs certbot renew.

Example: create `/etc/cron.d/certbot-namecheap-pve`:

```cron
# Renew wildcard cert for *.pve.neuravps.com (runs twice daily; certbot skips if not due)
SHELL=/bin/bash
0 3,15 * * * root source /opt/letsencrypt/namecheap.env && certbot renew --quiet --deploy-hook "systemctl reload nginx" --manual-auth-hook "/opt/letsencrypt/certbot-namecheap-dns-hooks.py auth" --manual-cleanup-hook "/opt/letsencrypt/certbot-namecheap-dns-hooks.py cleanup"
```

Or add to root's crontab (`crontab -e`):

```cron
0 3,15 * * * source /opt/letsencrypt/namecheap.env && certbot renew --quiet --deploy-hook "systemctl reload nginx" --manual-auth-hook "/opt/letsencrypt/certbot-namecheap-dns-hooks.py auth" --manual-cleanup-hook "/opt/letsencrypt/certbot-namecheap-dns-hooks.py cleanup"
```

`certbot renew` only acts on certs that are close to expiry (e.g. within 30 days), so running twice daily is safe. When it renews the wildcard cert, it will run the auth and cleanup hooks for the DNS-01 challenge. `--deploy-hook "systemctl reload nginx"` reloads nginx after a successful renewal so the new cert is used.

**Security:** Keep `namecheap.env` readable only by root and the hook script only writable by root so the API key is not exposed.

#### 5. Troubleshooting

- **API error / 1016 / 1020:** Usually means the request IP is not whitelisted. Ensure `NAMECHEAP_CLIENT_IP` is the server's public IPv4 and that this IP is whitelisted in Namecheap.
- **Domain not found / not associated:** The domain must be on Namecheap and use Namecheap's DNS (or the API user must have access).
- **Validation timeout:** The script sleeps 60s; if your DNS is slow, increase the sleep in the script (auth branch) or run certbot with a longer propagation delay.
- **setHosts deletes other records:** The script is designed to get all hosts, add/remove only the ACME TXT, then set the full list. If you have many records and hit API limits, see Namecheap's docs on setHosts limits.

There is no official Certbot plugin for Namecheap; this hook approach is the standard way. Alternatives include **acme-dns** (CNAME `_acme-challenge.pve` to an acme-dns server that handles the challenge) or other DNS challenge services.

---

## 8. PVE SSO – set-ticket endpoint on node domain

Admins and customers can open the PVE panel / noVNC console authenticated from the NeuraVPS website. The Cloud Function returns a redirect to `https://NODEID.pve.neuravps.com/set-ticket?token=XXX`. Visiting that URL redeems the token (via a secret-authenticated call to the Cloud Function), sets the `PVEAuthCookie` cookie for `.pve.neuravps.com`, and redirects to `https://NODEID.pve.neuravps.com/` (or the noVNC console URL for customers).

Using the **node domain** (NODEID.pve.neuravps.com) for set-ticket ensures cookies are first-party when the VNC console loads in an iframe, avoiding third-party cookie blocking in modern browsers.

Nginx routes `*.pve.neuravps.com/set-ticket` to the set-ticket app (section 3). No separate `auth.pve.neuravps.com` host is required.

### 8.1 Set-ticket app (Python, stdlib only)

The repo includes a small HTTP server that handles `GET /set-ticket?token=XXX`:

- **Path:** [scripts/pve-set-ticket.py](../scripts/pve-set-ticket.py)
- **Copy to server:** e.g. `/opt/pve-set-ticket/pve-set-ticket.py`
- **Environment variables:**

| Variable              | Description                                                                                                       |
| --------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `PVE_REDEEM_SECRET`   | Shared secret (must match the secret configured in the Cloud Function `redeem_pve_ticket_token`).                 |
| `REDEEM_FUNCTION_URL` | Full URL of the redeem function, e.g. `https://europe-west1-neuravps.cloudfunctions.net/redeem_pve_ticket_token`. |
| `HOST`                | Optional; default `127.0.0.1`.                                                                                    |
| `PORT`                | Optional; default `5000`.                                                                                         |

Run the app with systemd so it listens on `127.0.0.1:5000`. Step-by-step:

#### 1. Create directory and copy the script

On the BASE server:

```bash
sudo mkdir -p /opt/pve-set-ticket
sudo cp /path/to/scripts/pve-set-ticket.py /opt/pve-set-ticket/pve-set-ticket.py
sudo chmod 755 /opt/pve-set-ticket/pve-set-ticket.py
```

(Replace `/path/to/` with the path to your clone of this repo, or copy the script contents manually.)

#### 2. Create the environment file

Create `/opt/pve-set-ticket/env` with the two required variables (one per line, no quotes unless the value contains spaces):

```bash
sudo tee /opt/pve-set-ticket/env << 'EOF'
PVE_REDEEM_SECRET=your_secret_here
REDEEM_FUNCTION_URL=https://europe-west1-neuravps.cloudfunctions.net/redeem_pve_ticket_token
EOF
```

Replace `your_secret_here` with the same value you set for the Firebase secret `PVE_REDEEM_SECRET`. Then restrict access:

```bash
sudo chmod 600 /opt/pve-set-ticket/env
```

#### 3. Create the systemd unit file

```bash
sudo tee /etc/systemd/system/pve-set-ticket.service << 'EOF'
[Unit]
Description=PVE set-ticket endpoint (auth.pve.neuravps.com)
After=network.target

[Service]
Type=simple
User=www-data
Group=www-data
EnvironmentFile=/opt/pve-set-ticket/env
ExecStart=/usr/bin/python3 /opt/pve-set-ticket/pve-set-ticket.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
```

If Python 3 is not at `/usr/bin/python3`, use `which python3` to get the path and update `ExecStart`.

#### 4. Reload systemd, enable and start the service

```bash
sudo systemctl daemon-reload
sudo systemctl enable pve-set-ticket
sudo systemctl start pve-set-ticket
sudo systemctl status pve-set-ticket
```

You should see `active (running)`. Check that it is listening on port 5000:

```bash
ss -tlnp | grep 5000
# or: curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:5000/set-ticket?token=invalid"
# (expect 302 redirect)
```

#### 5. Useful commands

| Command                                 | Purpose                                  |
| --------------------------------------- | ---------------------------------------- |
| `sudo systemctl status pve-set-ticket`  | Check if the service is running          |
| `sudo systemctl restart pve-set-ticket` | Restart after changing the script or env |
| `sudo journalctl -u pve-set-ticket -f`  | Follow logs                              |
| `sudo systemctl stop pve-set-ticket`    | Stop the service                         |
| `sudo systemctl disable pve-set-ticket` | Disable automatic start on boot          |

### 8.2 Nginx: location /set-ticket in wildcard block

The `location /set-ticket` block is included in the wildcard `*.pve.neuravps.com` server block (section 3). It proxies to the set-ticket app at `127.0.0.1:5000`. No separate server block for `auth.pve.neuravps.com` is needed.

### 8.3 Why the cookie is not HttpOnly

The PVE web UI’s client-side JavaScript checks for `PVEAuthCookie` (e.g. via `document.cookie`) and shows the login form if it is missing. If the cookie is set with `HttpOnly`, the browser still sends it to the server and API calls succeed, but the UI does not see it and keeps showing the login mask. So the set-ticket app sets the cookie **without** `HttpOnly` (see [Proxmox forum #89194](https://forum.proxmox.com/threads/pve-web-interface-not-recognizing-pveauthcookie.89194/)).

### 8.4 Console in iframe (third-party cookies)

The console is often embedded in an iframe (e.g. from `https://www.neuravps.com` or `http://localhost` during dev). The iframe is then **third-party** relative to the parent. Browsers may block or partition cookies set in that context. The set-ticket app sets `PVEAuthCookie` with **SameSite=None; Secure; Partitioned** so the cookie is stored and sent when set inside the iframe. If you still see **401 No ticket** on API calls from the noVNC page, try: testing from the production origin (`https://www.neuravps.com`), allowing third-party cookies for the dev site, or using a separate browser profile with relaxed cookie settings for local testing.

### 8.5 Summary

| What                | Where                                                                                                           |
| ------------------- | --------------------------------------------------------------------------------------------------------------- |
| Set-ticket script   | `scripts/pve-set-ticket.py` → deploy to e.g. `/opt/pve-set-ticket/`                                             |
| Redeem secret       | Firebase/Google Cloud secret `PVE_REDEEM_SECRET`; same value in Cloud Function and in `/opt/pve-set-ticket/env` |
| Redeem function URL | `REDEEM_FUNCTION_URL` in env (e.g. `https://europe-west1-neuravps.cloudfunctions.net/redeem_pve_ticket_token`)  |
| Nginx               | `location /set-ticket` in the wildcard `*.pve.neuravps.com` server block, proxying to `http://127.0.0.1:5000`   |

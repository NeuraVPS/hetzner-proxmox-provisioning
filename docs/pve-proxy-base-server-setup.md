# PVE proxy on BASE server (nginx + njs)

Proxmox VE UI and console are exposed only via the **wildcard subdomain:** `https://NODEID.pve.neuravps.com/` (e.g. `https://000001-EX44.pve.neuravps.com/`). The hostname alone selects the node; all traffic is proxied with no path or cookie logic. Requires DNS A/AAAA for `*.pve.neuravps.com` and a wildcard TLS cert (DNS-01, see section 7).

`sqx.neuravps.com` and `trading.neuravps.com` redirect every request to `https://www.neuravps.com` (path preserved); `.well-known/acme-challenge/` is served for Let's Encrypt renewal.

Node IDs like `0000001-BASE` or `0000002-AX162-R` map to backends `https://[fd00:4000::<hex>]:8006` (decimal part → hex, e.g. 1 → `fd00:4000::1`).

**Server:** BASE node (e.g. 0000001-BASE).  
**Packages:** `nginx`, `libnginx-mod-http-js`.  
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
    var node = (r.variables.pve_node_from_host || '').toString().trim();
    var match = node.match(/^([0-9]+)-/);
    if (!match) return '';
    var dec = parseInt(match[1], 10);
    if (isNaN(dec)) return '';
    return 'https://[fd00:4000::' + dec.toString(16) + ']:8006';
}

export default { backend_from_host };
```

---

## 3. Site config: `/etc/nginx/sites-enabled/neuravps-redirects.conf`

Use this as the full server block for the PVE proxy and redirects. Adjust `server_name`, SSL paths, and `return 301` target if needed.

```nginx
js_import /etc/nginx/njs/pve_proxy.js;
js_set $pve_backend_from_host pve_proxy.backend_from_host;

# Wildcard PVE: NODEID.pve.neuravps.com → proxy all traffic to that node (no path/cookie logic)
server {
    server_name *.pve.neuravps.com;

    resolver 127.0.0.53 valid=10s;

    if ($pve_backend_from_host = "") {
        return 404;
    }

    proxy_cookie_domain ~^(.+)$ $host;
    proxy_cookie_path / /;

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

| What | Where |
|------|--------|
| Map (`$pve_node_from_host`, `$connection_upgrade`) | `http {}` – e.g. `/etc/nginx/conf.d/pve-proxy-map.conf` |
| NJS (`backend_from_host`) | `/etc/nginx/njs/pve_proxy.js` |
| Site config | `/etc/nginx/sites-enabled/neuravps-redirects.conf` |
| Wildcard cert (optional until you use *.pve) | `/etc/letsencrypt/live/pve.neuravps.com/` (see section 7) |
| Resolver (if needed) | In each server block that uses `proxy_pass` with a variable or in `http {}` |

**Node → backend:** Host like `0000011-BASE` → decimal `11` → hex `b` → `https://[fd00:4000::b]:8006`.

---

## 6. VNC / noVNC notes

VNC and console (noVNC, xtermjs) are used only via the wildcard host (`NODEID.pve.neuravps.com`). The wildcard server block forwards WebSocket with `Upgrade` / `Connection $connection_upgrade` and long timeouts so `wss://.../api2/json/nodes/.../vncwebsocket` works. If WebSocket fails (e.g. 1006), check the request has `Upgrade: websocket` and that the node/firewall allows the connection.

---

## 7. Wildcard subdomain (*.pve.neuravps.com) and Let’s Encrypt

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

| Variable | Description |
|----------|-------------|
| `NAMECHEAP_API_USER` | API user (often your Namecheap username). |
| `NAMECHEAP_API_KEY` | Your API key from the API Access page. |
| `NAMECHEAP_USERNAME` | Namecheap account username. |
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

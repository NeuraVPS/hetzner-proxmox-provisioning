# PVE proxy on BASE server (nginx + njs)

Proxmox VE UI and console are exposed under `https://sqx.neuravps.com/pve/NODENAME/`. Node IDs like `0000001-BASE` or `0000002-AX162-R` map to backends `https://[fd00:4000::<hex>]:8006` (decimal part → hex, e.g. 1 → `fd00:4000::1`).

**Server:** BASE node (e.g. 0000001-BASE).  
**Packages:** `nginx`, `libnginx-mod-http-js`.  
**Resolver:** When using variable `proxy_pass`, a `resolver` is required (e.g. in `server` or `http`): `resolver 127.0.0.53 valid=10s;`

---

## 1. Map (http context)

Must be in the **http** block (not inside server). Create a file that is included from `http {}`, e.g. `/etc/nginx/conf.d/pve-proxy-map.conf`:

```nginx
# PVE proxy path prefix (used by body filter)
map $request_uri $pve_prefix {
    ~^(/pve/[0-9]+-[^/]+) $1;
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
function backend(r) {
    var raw = r.variables.request_uri || '';
    var path = raw.indexOf('?') >= 0 ? raw.substring(0, raw.indexOf('?')) : raw;
    var match = path.match(/^\/pve\/([0-9]+)-/);
    if (!match) return '';
    var dec = parseInt(match[1], 10);
    if (isNaN(dec)) return '';
    return 'https://[fd00:4000::' + dec.toString(16) + ']:8006';
}

function backend_from_referer(r) {
    var ref = (r.headersIn && (r.headersIn['Referer'] || r.headersIn['referer'])) || '';
    ref = String(ref).trim();
    var match = ref.match(/\/pve\/([0-9]+)-[^/]*/);
    if (!match) {
        match = ref.match(/[?&]node=([0-9]+-[^&]*)/);
    }
    if (!match) return '';
    var dec = parseInt(match[1], 10);
    if (isNaN(dec)) return '';
    return 'https://[fd00:4000::' + dec.toString(16) + ']:8006';
}

function backend_from_arg_node(r) {
    var node = (r.variables.arg_node || '').toString().trim();
    var match = node.match(/^([0-9]+)-/);
    if (!match) return '';
    var dec = parseInt(match[1], 10);
    if (isNaN(dec)) return '';
    return 'https://[fd00:4000::' + dec.toString(16) + ']:8006';
}

// Backend from path /api2/json/nodes/NODENAME/... (for WebSocket and API when Referer may be missing)
function backend_from_api2_path(r) {
    var path = (r.variables.request_uri || r.variables.uri || '');
    if (path.indexOf('?') >= 0) path = path.substring(0, path.indexOf('?'));
    var match = path.match(/^\/api2\/json\/nodes\/([0-9]+)-[^/]*/);
    if (!match) return '';
    var dec = parseInt(match[1], 10);
    if (isNaN(dec)) return '';
    return 'https://[fd00:4000::' + dec.toString(16) + ']:8006';
}

// Backend for /novnc/: try Referer first, then cookie (fonts/preload often send no Referer)
function backend_from_referer_or_cookie(r) {
    var ref = (r.headersIn && (r.headersIn['Referer'] || r.headersIn['referer'])) || '';
    ref = String(ref).trim();
    var match = ref.match(/\/pve\/([0-9]+)-[^/]*/);
    if (!match) match = ref.match(/[?&]node=([0-9]+-[^&]*)/);
    if (!match) {
        var cookie = (r.variables.cookie_PVENode || '').toString().trim();
        match = cookie.match(/^([0-9]+)-/);
    }
    if (!match) return '';
    var dec = parseInt(match[1], 10);
    if (isNaN(dec)) return '';
    return 'https://[fd00:4000::' + dec.toString(16) + ']:8006';
}

function rewrite_body(r, data, flags) {
    var uri = (r.variables.uri || r.variables.request_uri || '');
    if (/\/pve2\/(images|css|ext6|fa|font-logos|js|locale)\//.test(uri) ||
        /\/pwt\//.test(uri) ||
        /\/novnc\//.test(uri) ||
        /\/xtermjs\//.test(uri) ||
        /\/pve-docs\//.test(uri) ||
        /\.(woff2?|ttf|eot|otf|png|jpg|jpeg|gif|ico|webp|svg)(\?|$)/i.test(uri)) {
        r.sendBuffer(data, flags);
        return;
    }
    var prefix = r.variables.pve_prefix || '';
    var contentType = (r.headersOut && (r.headersOut['Content-Type'] || r.headersOut['content-type'])) || '';
    var isHtml = contentType.indexOf('text/html') !== -1;

    if (!prefix || !isHtml || !data || typeof data !== 'string') {
        r.sendBuffer(data, flags);
        return;
    }
    try {
        // Shell console (xtermjs): inject <base href="https://host/pve/NODE"> so relative script URLs resolve without the query string (avoids location.href + "/xtermjs/..." producing ...?cmd=/xtermjs/...)
        var reqUri = (r.variables.request_uri || '').toString();
        if (reqUri.indexOf('console=shell') !== -1 && reqUri.indexOf('xtermjs') !== -1) {
            var baseUrl = 'https://' + (r.variables.host || '') + prefix;
            data = data.replace(/<head(\s[^>]*)?>/i, '<head$1><base href="' + baseUrl + '">');
        }
        // Don't rewrite /xtermjs/ – console scripts must load from origin /xtermjs/ so requests hit location /xtermjs/ (Referer/cookie)
        data = data
            .replace(/href="\/(?!pve\/\d+-|xtermjs\/)/g, 'href="' + prefix + '/')
            .replace(/src="\/(?!pve\/\d+-|xtermjs\/)/g, 'src="' + prefix + '/')
            .replace(/url:\s*['"]\/(?!pve\/\d+-|xtermjs\/)/g, 'url: \'' + prefix + '/')
            .replace(/"\/api2\/(?!pve\/\d+-)/g, '"' + prefix + '/api2/')
            .replace(/'\/api2\/(?!pve\/\d+-)/g, "'" + prefix + '/api2/')
            .replace(/\/pve\/(?![0-9]+-)/g, prefix + '/');
    } catch (e) {}
    r.sendBuffer(data, flags);
}

export default { backend, backend_from_referer, backend_from_arg_node, backend_from_api2_path, backend_from_referer_or_cookie, rewrite_body };
```

---

## 3. Site config: `/etc/nginx/sites-enabled/neuravps-redirects.conf`

Use this as the full server block for the PVE proxy and redirects. Adjust `server_name`, SSL paths, and `return 301` target if needed.

```nginx
js_import /etc/nginx/njs/pve_proxy.js;
js_set $pve_backend pve_proxy.backend;
js_set $pve_backend_from_referer pve_proxy.backend_from_referer;
js_set $pve_backend_from_arg_node pve_proxy.backend_from_arg_node;
js_set $pve_backend_from_api2_path pve_proxy.backend_from_api2_path;
js_set $pve_backend_from_referer_or_cookie pve_proxy.backend_from_referer_or_cookie;

server {
    server_name trading.neuravps.com sqx.neuravps.com;

    resolver 127.0.0.53 valid=10s;

    location ^~ /.well-known/acme-challenge/ {
        root /var/www/letsencrypt;
        default_type "text/plain";
        try_files $uri =404;
    }

    # PVE proxy – index only (body filter rewrites HTML paths); keep query string (e.g. ?console=kvm&novnc=1)
    location ~ ^/pve/([0-9]+-[^/]+)/?$ {
        rewrite ^/pve/[0-9]+-[^/]+/?$ / break;
        proxy_pass $pve_backend/$is_args$args;
        proxy_ssl_verify off;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 86400;
        proxy_set_header Accept-Encoding "";
        js_body_filter pve_proxy.rewrite_body;
    }

    # PVE proxy – all other paths under /pve/NODENAME/... (no body filter; binary safe)
    location ~ ^/pve/([0-9]+-[^/]+)/(.+)$ {
        rewrite ^/pve/[0-9]+-[^/]+/(.*)$ /$1 break;
        proxy_pass $pve_backend$uri;
        proxy_ssl_verify off;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 86400;
    }

    # /pve/pwt/, /pve/pve2/, etc. (no node id – wrong base path from JS) – use Referer, proxy to backend
    location ~ ^/pve/(pwt|pve2|novnc|pve-docs|xtermjs)/(.*)$ {
        if ($pve_backend_from_referer = "") {
            return 404;
        }
        rewrite ^/pve/(pwt|pve2|novnc|pve-docs|xtermjs)/(.*)$ /$1/$2 break;
        proxy_pass $pve_backend_from_referer$uri;
        proxy_ssl_verify off;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # API with node in path: /api2/json/nodes/NODENAME/... – use path (no Referer needed; fixes VNC WebSocket)
    location ~ ^/api2/json/nodes/([0-9]+-[^/]+)/ {
        if ($pve_backend_from_api2_path = "") {
            return 404;
        }
        proxy_pass $pve_backend_from_api2_path$request_uri;
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

    # API: /api2/... (no node in path) – use Referer to pick node
    location /api2/ {
        if ($pve_backend_from_referer = "") {
            return 404;
        }
        proxy_pass $pve_backend_from_referer$request_uri;
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

    # noVNC (console iframe): /novnc/... – use Referer or cookie to pick node (fonts/preload often omit Referer)
    location /novnc/ {
        if ($pve_backend_from_referer_or_cookie = "") {
            return 404;
        }
        proxy_pass $pve_backend_from_referer_or_cookie$request_uri;
        proxy_ssl_verify off;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_read_timeout 86400;
    }

    # xtermjs (shell console): /xtermjs/... – use Referer or cookie (same as noVNC)
    location /xtermjs/ {
        if ($pve_backend_from_referer_or_cookie = "") {
            return 404;
        }
        proxy_pass $pve_backend_from_referer_or_cookie$request_uri;
        proxy_ssl_verify off;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_read_timeout 86400;
    }

    # Fallback: /pve/PVE/*.js only (ExtJS loader) – serve pvemanagerlib.js; need Referer
    location ~ ^/pve/PVE/[^?]*$ {
        if ($pve_backend_from_referer = "") {
            return 404;
        }
        rewrite ^/pve/PVE/[^?]* /pve2/js/pvemanagerlib.js break;
        proxy_pass $pve_backend_from_referer$uri;
        proxy_ssl_verify off;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Console iframe/new page: /?console=kvm&node=... or ?console=shell&xtermjs=1&node=... (query may include vmid, vmname, cmd, etc.). Proxy to node from ?node=; set cookie for /novnc/ and /xtermjs/
    location = / {
        if ($pve_backend_from_arg_node = "") {
            return 301 https://www.neuravps.com$request_uri;
        }
        if ($arg_node ~ ^[0-9]+-) {
            add_header Set-Cookie "PVENode=$arg_node; Path=/; Max-Age=3600; SameSite=Lax";
        }
        proxy_pass $pve_backend_from_arg_node$request_uri;
        proxy_ssl_verify off;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 86400;
    }

    location / {
        return 301 https://www.neuravps.com$request_uri;
    }

    listen 443 ssl;
    listen [::]:443 ssl ipv6only=on;
    ssl_certificate /etc/letsencrypt/live/trading.neuravps.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/trading.neuravps.com/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;
}

server {
    if ($host = sqx.neuravps.com) {
        return 301 https://$host$request_uri;
    }
    if ($host = trading.neuravps.com) {
        return 301 https://$host$request_uri;
    }
    listen 80;
    listen [::]:80;
    server_name trading.neuravps.com sqx.neuravps.com;
    return 404;
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
   If you already have a `resolver` in `http {}` or another server, you can remove the `resolver` line from this server block.

4. **Test and reload**  
   ```bash
   sudo nginx -t && sudo systemctl reload nginx
   ```

---

## 5. Quick reference

| What | Where |
|------|--------|
| Map `$pve_prefix` | `http {}` – e.g. `/etc/nginx/conf.d/pve-proxy-map.conf` |
| NJS script | `/etc/nginx/njs/pve_proxy.js` |
| Site config | `/etc/nginx/sites-enabled/neuravps-redirects.conf` |
| Resolver (if needed) | In this server block or in `http {}` |

**Node → backend:** Path or param like `0000011-BASE` → decimal `11` → hex `b` → `https://[fd00:4000::b]:8006`.

---

## 6. VNC / noVNC notes

- **WebSocket:** The `/api2/` location uses `Upgrade`, `Connection $connection_upgrade` (from the map in section 1), and long timeouts so `wss://.../api2/json/nodes/.../vncwebsocket` works. You must have the `map $http_upgrade $connection_upgrade` in the http block; otherwise use `Connection "upgrade"` in the location.
- **If WebSocket still fails (e.g. code 1006):** In the browser DevTools → Network, select the `vncwebsocket` request and check: (1) Request has `Upgrade: websocket` and `Connection: upgrade`; (2) Response is 101 Switching Protocols (if you get 404, Referer is missing so we don’t proxy). If the backend returns 101 but the connection then closes, the node may be rejecting the ticket or there may be a firewall between BASE and the node.
- **noVNC assets (fonts, SVGs) 404 only via proxy:** Browsers often omit the Referer for font/preload requests, so we couldn’t pick the node. The config sets a **cookie** when you load the console page (`/?console=...&node=NODENAME`): `PVENode=NODENAME` (1 hour). The `/novnc/` location uses **Referer or cookie** (`backend_from_referer_or_cookie`) so fonts and images load even without Referer. If you still see 404s, the node’s noVNC package may not ship those files; safe to ignore.

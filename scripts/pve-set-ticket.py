#!/usr/bin/env python3
"""
PVE set-ticket endpoint: redeem one-time token and set PVEAuthCookie, then redirect to PVE UI.

Run on BASE server; nginx proxies auth.pve.neuravps.com to this app (e.g. port 5000).
Environment variables:
  PVE_REDEEM_SECRET   - Shared secret for redeeming tokens (must match Cloud Function secret).
  REDEEM_FUNCTION_URL - Cloud Function URL, e.g. https://europe-west1-neuravps.cloudfunctions.net/redeem_pve_ticket_token

Usage:
  PVE_REDEEM_SECRET=... REDEEM_FUNCTION_URL=... python3 pve-set-ticket.py

Listens on 127.0.0.1:5000. Use a process manager (systemd, etc.) and bind to 127.0.0.1 only.
Requires: Python 3, stdlib only (urllib, json, http.server).
"""
import json
import os
import sys
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler


def env(name, default=None):
    v = os.environ.get(name)
    if v is not None:
        return v
    if default is not None:
        return default
    print(f"Missing env: {name}", file=sys.stderr)
    sys.exit(1)


REDEEM_SECRET = None
REDEEM_URL = None


def redeem_token(token):
    """Call Cloud Function to redeem token. Returns (ticket, node_id) or (None, error_msg)."""
    global REDEEM_SECRET, REDEEM_URL
    url = f"{REDEEM_URL}?token={urllib.parse.quote(token, safe='')}"
    req = urllib.request.Request(url, method="GET")
    req.add_header("X-PVE-Redeem-Secret", REDEEM_SECRET)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status != 200:
                return None, f"Redeem returned {resp.status}"
            data = json.loads(resp.read().decode())
            ticket = data.get("ticket")
            node_id = data.get("nodeId")
            if not ticket or not node_id:
                return None, "Invalid response"
            return (ticket, node_id), None
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode()
            err = json.loads(body).get("error", body)
        except Exception:
            err = str(e)
        return None, err
    except Exception as e:
        return None, str(e)


class SetTicketHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not self.path.startswith("/set-ticket"):
            self.send_error(404)
            return
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        token = (params.get("token") or [""])[0].strip()
        if not token:
            self.send_redirect_error("Missing token")
            return
        result, err = redeem_token(token)
        if err:
            self.send_redirect_error(err)
            return
        ticket, node_id = result
        # Cookie for *.pve.neuravps.com; 2h to match PVE ticket TTL.
        # Do NOT use HttpOnly: the PVE web UI client-side JS checks document.cookie for
        # PVEAuthCookie and shows the login form if it is missing (Proxmox forum #89194).
        cookie_val = (
            f"PVEAuthCookie={ticket}; Domain=.pve.neuravps.com; Path=/; "
            "Secure; SameSite=Lax; Max-Age=7200"
        )
        location = f"https://{node_id}.pve.neuravps.com/"
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Set-Cookie", cookie_val)
        self.end_headers()

    def send_redirect_error(self, message):
        """Redirect to admin with error (or a simple 400)."""
        self.send_response(302)
        self.send_header("Location", "https://www.neuravps.com/admin/nodos?pve_error=" + urllib.parse.quote(message, safe=""))
        self.end_headers()

    def log_message(self, format, *args):
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), format % args))


def main():
    global REDEEM_SECRET, REDEEM_URL
    REDEEM_SECRET = env("PVE_REDEEM_SECRET")
    REDEEM_URL = env("REDEEM_FUNCTION_URL").rstrip("/")
    port = int(env("PORT", "5000"))
    host = env("HOST", "127.0.0.1")
    server = HTTPServer((host, port), SetTicketHandler)
    print(f"PVE set-ticket listening on {host}:{port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()


if __name__ == "__main__":
    main()

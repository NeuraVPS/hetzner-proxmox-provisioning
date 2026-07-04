#!/usr/bin/env python3
"""certbot manual DNS-01 hooks for Hetzner DNS via the Cloud API.

Hetzner folded DNS into the Cloud Console/API (api.hetzner.cloud/v1/zones);
the legacy dns.hetzner.com API now 301s. A Hetzner CLOUD token with DNS
permissions manages the zone. Python stdlib only — no pip deps.

Usage (certbot calls these; see base_setup.sh / dual-region-cutover.md):
  certbot-hetzner-dns-hooks.py auth      # from --manual-auth-hook
  certbot-hetzner-dns-hooks.py cleanup   # from --manual-cleanup-hook

Env:
  HETZNER_API_TOKEN   Cloud API token (or set HETZNER_TOKEN_FILE to a path;
                      default /opt/letsencrypt/hetzner.env is sourced for
                      HETZNER_API_TOKEN=... lines when the var is unset).
  CERTBOT_DOMAIN      set by certbot (wildcards arrive without the "*.").
  CERTBOT_VALIDATION  set by certbot.

auth  : appends CERTBOT_VALIDATION to the TXT rrset
        _acme-challenge.<domain> (creating it if absent, TTL 60) and waits
        until the zone's authoritative NS serve it (fallback fixed sleep
        when dig is unavailable). Appending matters: a wildcard + apex pair
        validates TWO tokens on the SAME rrset name.
cleanup: deletes the whole TXT rrset (validation already happened).
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.hetzner.cloud/v1"
TOKEN_FILE_DEFAULT = "/opt/letsencrypt/hetzner.env"


def _token() -> str:
    tok = (os.environ.get("HETZNER_API_TOKEN") or "").strip()
    if tok:
        return tok
    path = os.environ.get("HETZNER_TOKEN_FILE", TOKEN_FILE_DEFAULT)
    try:
        for line in open(path):
            line = line.strip()
            if line.startswith("HETZNER_API_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    print(f"HETZNER_API_TOKEN not set and {path} unreadable", file=sys.stderr)
    sys.exit(1)


def _req(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(
        API + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {_token()}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode() or "{}"
            return r.status, json.loads(raw)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}


def _zone_for(domain: str) -> tuple[str, str]:
    """(zone_id, zone_name) for the longest zone suffix of `domain`."""
    status, data = _req("GET", "/zones?per_page=50")
    if status != 200:
        print(f"zones list failed: {status} {data}", file=sys.stderr)
        sys.exit(1)
    best = None
    for z in data.get("zones", []):
        name = z["name"].rstrip(".")
        if domain == name or domain.endswith("." + name):
            if best is None or len(name) > len(best[1]):
                best = (str(z["id"]), name)
    if not best:
        print(f"no Hetzner zone matches {domain}", file=sys.stderr)
        sys.exit(1)
    return best


def _rrset_name(domain: str, zone_name: str) -> str:
    fqdn = f"_acme-challenge.{domain}"
    rel = fqdn[: -len(zone_name)].rstrip(".") if fqdn.endswith(zone_name) else fqdn
    return rel or "@"


HETZNER_NS = (
    "hydrogen.ns.hetzner.com",
    "oxygen.ns.hetzner.com",
    "helium.ns.hetzner.de",
)


def _authoritative_ns_has(fqdn: str, value: str, timeout_s: int = 120) -> bool:
    """Poll ALL Hetzner authoritative NS with dig until every one serves the
    TXT. The secondaries lag the primary by a few seconds under bursts of
    changes; Let's Encrypt may query any of them, so waiting only on
    hydrogen produced transient NXDOMAINs mid-order."""
    pending = set(HETZNER_NS)
    deadline = time.time() + timeout_s
    while time.time() < deadline and pending:
        for ns in sorted(pending):
            try:
                out = subprocess.run(
                    ["dig", "+short", "TXT", fqdn, f"@{ns}"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                ).stdout
            except (OSError, subprocess.TimeoutExpired):
                return False  # no dig — caller falls back to a fixed sleep
            if value in out:
                pending.discard(ns)
        if pending:
            time.sleep(4)
    return not pending


def auth() -> None:
    domain = os.environ["CERTBOT_DOMAIN"]
    validation = os.environ["CERTBOT_VALIDATION"]
    zone_id, zone_name = _zone_for(domain)
    name = _rrset_name(domain, zone_name)
    quoted = f'"{validation}"'

    # The Cloud DNS API has no working record-update on an rrset (PUT
    # returns 200 but only touches metadata; there is no
    # actions/set-records) — so merge = GET existing values, DELETE the
    # rrset, POST it back with the merged list. The sub-second gap is
    # irrelevant for ACME: certbot runs every auth hook before any
    # validation is polled.
    path = f"/zones/{zone_id}/rrsets/{urllib.parse.quote(name)}/TXT"
    status, data = _req("GET", path)
    records = []
    if status == 200:
        records = data.get("rrset", {}).get("records", [])
        _req("DELETE", path)
    if not any(r.get("value") == quoted for r in records):
        records = records + [{"value": quoted}]
    _status, resp = _req(
        "POST",
        f"/zones/{zone_id}/rrsets",
        {"name": name, "type": "TXT", "ttl": 60, "records": records},
    )
    if _status not in (200, 201):
        print(f"rrset create failed: {_status} {resp}", file=sys.stderr)
        sys.exit(1)

    if not _authoritative_ns_has(f"_acme-challenge.{domain}", validation):
        time.sleep(45)  # fallback: give the authoritative NS time to serve it


def cleanup() -> None:
    domain = os.environ["CERTBOT_DOMAIN"]
    zone_id, zone_name = _zone_for(domain)
    name = _rrset_name(domain, zone_name)
    _req("DELETE", f"/zones/{zone_id}/rrsets/{urllib.parse.quote(name)}/TXT")


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "auth":
        auth()
    elif mode == "cleanup":
        cleanup()
    else:
        print(__doc__, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()

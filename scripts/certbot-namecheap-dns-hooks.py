#!/usr/bin/env python3
"""
Certbot manual-auth-hook and manual-cleanup-hook for Namecheap DNS-01.

Usage:
  NAMECHEAP_API_USER=... NAMECHEAP_API_KEY=... NAMECHEAP_USERNAME=... \\
  NAMECHEAP_CLIENT_IP=... certbot certonly --manual --preferred-challenges dns \\
  --manual-auth-hook   './certbot-namecheap-dns-hooks.py auth' \\
  --manual-cleanup-hook './certbot-namecheap-dns-hooks.py cleanup' \\
  -d '*.pve.neuravps.com' -d 'pve.neuravps.com'

Requires: Python 3, urllib, xml.etree. No extra pip packages.
Namecheap API: getHosts returns current records; setHosts REPLACES all records,
so we must fetch, add/remove the _acme-challenge TXT, then setHosts.
"""
import os
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

BASE = "https://api.namecheap.com/xml.response"

def env(name):
    v = os.environ.get(name)
    if not v:
        print("Missing env: " + name, file=sys.stderr)
        sys.exit(1)
    return v

def domain_parts(domain):
    """CERTBOT_DOMAIN e.g. pve.neuravps.com -> SLD=neuravps, TLD=com, host_prefix=pve."""
    parts = domain.split(".")
    if len(parts) < 2:
        raise SystemExit("Invalid CERTBOT_DOMAIN: " + domain)
    tld = parts[-1]
    sld = parts[-2]
    # Subdomain(s) before sld.tld: e.g. pve for pve.neuravps.com
    host_prefix = ".".join(parts[:-2]) if len(parts) > 2 else ""
    return sld, tld, host_prefix

def api_get(params):
    url = BASE + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read().decode()
    root = ET.fromstring(body)
    if root.get("Status") != "OK":
        err = root.find(".//{http://api.namecheap.com/xml.response}Error")
        msg = err.text if err is not None else body
        raise SystemExit("Namecheap API error: " + msg)
    return root

def get_hosts(api_user, api_key, username, client_ip, sld, tld):
    params = {
        "ApiUser": api_user,
        "ApiKey": api_key,
        "UserName": username,
        "ClientIp": client_ip,
        "Command": "namecheap.domains.dns.getHosts",
        "SLD": sld,
        "TLD": tld,
    }
    root = api_get(params)
    hosts = []
    for host in root.iter():
        if host.tag.endswith("host") or host.tag == "host":
            name = typ = addr = ttl = None
            for child in host:
                tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if tag == "Name":
                    name = child.text
                elif tag == "Type":
                    typ = child.text
                elif tag == "Address":
                    addr = child.text
                elif tag == "TTL":
                    ttl = child.text
            if name is not None and typ is not None and addr is not None:
                hosts.append({
                    "Name": name or "",
                    "Type": typ or "",
                    "Address": addr or "",
                    "TTL": int(ttl) if ttl and ttl.isdigit() else 1800,
                })
    return hosts

def set_hosts(api_user, api_key, username, client_ip, sld, tld, hosts):
    params = {
        "ApiUser": api_user,
        "ApiKey": api_key,
        "UserName": username,
        "ClientIp": client_ip,
        "Command": "namecheap.domains.dns.setHosts",
        "SLD": sld,
        "TLD": tld,
    }
    for i, h in enumerate(hosts, 1):
        params["HostName%d" % i] = h["Name"]
        params["RecordType%d" % i] = h["Type"]
        params["Address%d" % i] = h["Address"]
        params["TTL%d" % i] = str(h["TTL"])
    api_get(params)

def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("auth", "cleanup"):
        print("Usage: certbot-namecheap-dns-hooks.py auth|cleanup", file=sys.stderr)
        sys.exit(1)
    mode = sys.argv[1]

    domain = os.environ.get("CERTBOT_DOMAIN")
    validation = os.environ.get("CERTBOT_VALIDATION")
    if not domain or not validation:
        print("CERTBOT_DOMAIN and CERTBOT_VALIDATION must be set (Certbot sets these).", file=sys.stderr)
        sys.exit(1)

    api_user = env("NAMECHEAP_API_USER")
    api_key = env("NAMECHEAP_API_KEY")
    username = env("NAMECHEAP_USERNAME")
    client_ip = env("NAMECHEAP_CLIENT_IP")

    sld, tld, host_prefix = domain_parts(domain)
    # ACME challenge host: _acme-challenge.<subdomain> e.g. _acme-challenge.pve
    acme_host = "_acme-challenge." + host_prefix if host_prefix else "_acme-challenge"

    hosts = get_hosts(api_user, api_key, username, client_ip, sld, tld)

    if mode == "auth":
        # Remove any existing _acme-challenge TXT for this host, then add current validation
        hosts = [h for h in hosts if not (h["Name"] == acme_host and h["Type"] == "TXT")]
        hosts.append({"Name": acme_host, "Type": "TXT", "Address": validation, "TTL": 120})
        set_hosts(api_user, api_key, username, client_ip, sld, tld, hosts)
        # Allow DNS propagation before Certbot checks
        time.sleep(60)
    else:
        # cleanup: remove the TXT we added
        hosts = [h for h in hosts if not (h["Name"] == acme_host and h["Type"] == "TXT")]
        set_hosts(api_user, api_key, username, client_ip, sld, tld, hosts)

if __name__ == "__main__":
    main()

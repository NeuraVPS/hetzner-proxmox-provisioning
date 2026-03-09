#!/usr/bin/env python3
"""
Generate nginx backends from Firestore proxmox_nodes.

Writes /etc/nginx/pve-node-backends.conf on BASE (nginx map format:
node_id https://[ipv6]:8006;). Nginx uses it for NODEID.pve.neuravps.com.
Scripts that need a node list (e.g. RUN_UPDATE_FIREWALL_ALL=1 in first_boot.sh)
parse this file (e.g. sed to extract node_id and ipv6).

Run on BASE (or any host with /etc/firebase-credentials.json). New nodes
update only their line via first_boot (inline, no separate script). Use this
script for initial setup or manual full refresh from Firestore.
Optional env: PVE_NODE_BACKENDS_PATH.
"""
import os
import sys
from pathlib import Path

try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except ImportError:
    print("firebase_admin not installed", file=sys.stderr)
    sys.exit(1)

CREDS_FILE = os.environ.get("FIREBASE_CREDENTIALS_FILE", "/etc/firebase-credentials.json")
OUT_NGINX = os.environ.get("PVE_NODE_BACKENDS_PATH", "/etc/nginx/pve-node-backends.conf")


def main():
    creds_path = Path(CREDS_FILE)
    if not creds_path.is_file():
        print(f"Credentials not found: {CREDS_FILE}", file=sys.stderr)
        sys.exit(1)

    if not firebase_admin._apps:
        cred = credentials.Certificate(str(creds_path))
        firebase_admin.initialize_app(cred)

    db = firestore.client()
    coll = db.collection("proxmox_nodes")
    entries = []  # (node_id, ipv6)
    for doc in coll.stream():
        doc_id = doc.id
        data = doc.to_dict() or {}
        ip = (data.get("ip") or "").strip()
        if not ip:
            continue
        entries.append((doc_id, ip))
    entries.sort(key=lambda x: x[0])

    nginx_lines = [f"{node_id} https://[{ip}]:8006;" for node_id, ip in entries]
    out_nginx = Path(OUT_NGINX)
    out_nginx.parent.mkdir(parents=True, exist_ok=True)
    out_nginx.write_text("\n".join(nginx_lines) + "\n")
    print(f"Wrote {len(nginx_lines)} backends to {out_nginx}", file=sys.stderr)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Update Firestore server document with connectionUrl and nodeId after VM migration.
Used by migrate_vm.sh before starting the VM on destination.
Credentials: FIREBASE_CREDENTIALS_FILE env or /etc/firebase-credentials.json.
"""
import argparse
import os
import sys
from pathlib import Path

try:
    import firebase_admin
    from firebase_admin import credentials, firestore
    try:
        from google.cloud.firestore_v1 import FieldFilter
        FIELD_FILTER_AVAILABLE = True
    except ImportError:
        FIELD_FILTER_AVAILABLE = False
    FIREBASE_AVAILABLE = True
except ImportError:
    FIREBASE_AVAILABLE = False
    FIELD_FILTER_AVAILABLE = False


def initialize_firebase():
    """Initialize Firebase Admin SDK if credentials file is available."""
    if not FIREBASE_AVAILABLE:
        return False
    if firebase_admin._apps:
        return True
    creds_file = os.environ.get("FIREBASE_CREDENTIALS_FILE", "/etc/firebase-credentials.json")
    creds_path = Path(creds_file)
    if not creds_path.exists() or not creds_path.is_file():
        return False
    try:
        cred = credentials.Certificate(str(creds_path))
        firebase_admin.initialize_app(cred)
        return True
    except Exception:
        return False


def update_server_migration(vmid: int, connection_url: str, node_id: str) -> bool:
    """
    Update servers collection: find document by proxmoxId, set connectionUrl and nodeId.
    Returns True on success, False otherwise.
    """
    if not initialize_firebase():
        return False
    try:
        db = firestore.client()
        servers_ref = db.collection("servers")
        if FIELD_FILTER_AVAILABLE:
            query = servers_ref.where(filter=FieldFilter("proxmoxId", "==", vmid))
        else:
            query = servers_ref.where("proxmoxId", "==", vmid)
        docs = list(query.stream())
        if not docs:
            return False
        for doc_snapshot in docs:
            server_doc_ref = db.collection("servers").document(doc_snapshot.id)
            server_doc_ref.update({
                "connectionUrl": connection_url,
                "nodeId": node_id,
            })
        return True
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser(description="Update Firestore server connectionUrl and nodeId for migration")
    parser.add_argument("vmid", type=int, help="Proxmox VM ID (proxmoxId)")
    parser.add_argument("connection_url", help="connectionUrl (e.g. 157.180.15.173:20411)")
    parser.add_argument("node_id", help="Destination node hostname (nodeId)")
    args = parser.parse_args()

    if not update_server_migration(args.vmid, args.connection_url, args.node_id):
        sys.stderr.write("update_firestore_migration: failed (no creds, no doc, or Firestore error)\n")
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Escribe ipv6/ipv4 del modelo nuevo en Firestore. Se ejecuta EN UNA BASE.
Eso dispara la Cloud Function que mueve DNAT y rutas en las dos bases, y de paso
regenera los enlaces SMB del usuario (ipv6 esta entre sus campos vigilados).
Uso: fs_set.py <vmid>
"""
import sys

from google.cloud import firestore
from google.oauth2 import service_account

vmid = int(sys.argv[1])
nuevo6 = f"2a01:4f9:c01f:e::{vmid:x}"
nuevo4 = f"10.64.{vmid // 256}.{vmid % 256}"

c = service_account.Credentials.from_service_account_file("/etc/firebase-credentials.json")
db = firestore.Client(credentials=c, project=c.project_id)

for d in db.collection("servers").where(
        filter=firestore.FieldFilter("proxmoxId", "==", vmid)).limit(2).stream():
    a = d.to_dict() or {}
    print(f"  antes:   ipv6={a.get('ipv6')}  ipv4={a.get('ipv4')}")
    d.reference.update({"ipv6": nuevo6, "ipv4": nuevo4})
    b = d.reference.get().to_dict() or {}
    print(f"  despues: ipv6={b.get('ipv6')}  ipv4={b.get('ipv4')}")
    break
else:
    sys.exit(f"  vm {vmid}: no esta en Firestore")

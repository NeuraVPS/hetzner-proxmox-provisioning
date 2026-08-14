#!/usr/bin/env python3
"""Vuelve a disparar el resync de enlaces SMB de un usuario. Corre EN UNA BASE.

Por que hace falta: `setup_smb_symlinks_trigger` reconstruye los enlaces de TODO
el usuario leyendo Firestore, y se dispara con cada cambio de `ipv6`. Al
convertir VMs en serie, el resync que corre dentro de la VM N lee Firestore
cuando la VM N+1 todavia tiene su direccion vieja — asi que el enlace hacia la
ULTIMA VM convertida se queda obsoleto en todas las demas.

Se toca `lastStarted`, que esta entre los campos vigilados del trigger y es
inocuo. Hay que hacerlo UNA VEZ POR USUARIO cuando ya estan convertidas TODAS
sus VMs.

Uso: resync_smb.py <vmid-de-cualquier-VM-del-usuario>
"""
import sys
from datetime import datetime, timezone

from google.cloud import firestore
from google.oauth2 import service_account

vmid = int(sys.argv[1])
c = service_account.Credentials.from_service_account_file("/etc/firebase-credentials.json")
db = firestore.Client(credentials=c, project=c.project_id)

for d in db.collection("servers").where(
        filter=firestore.FieldFilter("proxmoxId", "==", vmid)).limit(2).stream():
    s = d.to_dict() or {}
    print(f"  vm {vmid} (usuario {s.get('userId')}): disparo resync tocando lastStarted")
    d.reference.update({"lastStarted": datetime.now(timezone.utc)})
    print("  hecho — el trigger reconstruye los enlaces de TODAS las VMs del usuario")
    break
else:
    sys.exit(f"  vm {vmid}: no esta en Firestore")

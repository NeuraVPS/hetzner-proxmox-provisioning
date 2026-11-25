#!/usr/bin/env python3
"""rtmig_alloc.py <VMID> — asignación FIABLE de una VM para el modo pull del kit coldmove.

Lee servers/<doc> (proxmoxId == VMID) en Firestore, con las credenciales de la BASE
(/etc/firebase-credentials.json o FIREBASE_CREDENTIALS_FILE), y escribe las variables que exige
`RTMIG_MODE=pull`:

    ALLOC_MEMORY_MB=<memoryGb*1024> ALLOC_CORES=<cores> ALLOC_DISK_GB=<diskGb>

En modo pull el origen (un nodo de otro proveedor) no es fiable: la RAM, los núcleos y el disco de la
VM en el destino salen de aquí, no de su config. Sale con 2 si no hay exactamente un server con esa
VMID o si algún campo falta o no es un número razonable. Solo lee.
"""
import os
import sys


def main(argv):
    if len(argv) != 2 or not argv[1].isdigit() or argv[1].startswith("0"):
        sys.stderr.write("uso: rtmig_alloc.py <VMID>\n")
        return 2
    vmid = int(argv[1])
    import firebase_admin
    from firebase_admin import credentials, firestore
    creds = os.environ.get("FIREBASE_CREDENTIALS_FILE", "/etc/firebase-credentials.json")
    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(creds))
    docs = list(firestore.client().collection("servers").where("proxmoxId", "==", vmid).stream())
    if len(docs) != 1:
        sys.stderr.write("hay %d servers con proxmoxId=%d (tiene que haber exactamente 1)\n" % (len(docs), vmid))
        return 2
    d = docs[0].to_dict() or {}
    try:
        mem_gb, cores, disk_gb = float(d["memoryGb"]), int(d["cores"]), int(d["diskGb"])
    except (KeyError, TypeError, ValueError):
        sys.stderr.write("servers/%s sin memoryGb/cores/diskGb numéricos\n" % docs[0].id)
        return 2
    mem_mb = int(round(mem_gb * 1024))
    if not (512 <= mem_mb <= 1048576 and 1 <= cores <= 256 and 1 <= disk_gb <= 65536):
        sys.stderr.write("servers/%s con valores fuera de rango: memoryGb=%s cores=%s diskGb=%s\n"
                         % (docs[0].id, d.get("memoryGb"), d.get("cores"), d.get("diskGb")))
        return 2
    print("ALLOC_MEMORY_MB=%d ALLOC_CORES=%d ALLOC_DISK_GB=%d" % (mem_mb, cores, disk_gb))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

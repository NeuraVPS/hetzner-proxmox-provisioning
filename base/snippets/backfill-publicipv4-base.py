#!/usr/bin/env python3
"""Pone `publicIpv4` = IP de la BASE en las VMs ya convertidas. Corre EN UNA BASE.

El barrido diario ya lo hara solo a partir de ahora, pero las 1.768 convertidas
hoy seguirian enseñando en el panel la IP de su NODO hasta mañana — y es la que
el cliente copia en la lista blanca de su broker.

Solo toca VMs del modelo nuevo (IPv6 dentro del /64 de identidad) y solo si el
valor cambia. `--aplicar` para escribir; sin el, solo enseña que haria.
"""
import sys

from google.cloud import firestore
from google.oauth2 import service_account

IDENT = "2a01:4f9:c01f:e:"
BASE = {"falkenstein": "188.40.153.120", "helsinki": "37.27.135.250"}
aplicar = "--aplicar" in sys.argv

c = service_account.Credentials.from_service_account_file("/etc/firebase-credentials.json")
db = firestore.Client(credentials=c, project=c.project_id)

# Region de cada nodo, de una sola lectura.
region = {}
for d in db.collection("proxmox_nodes").stream():
    region[d.id] = str((d.to_dict() or {}).get("location") or "").strip().lower()

cambian, ya_ok, sin_region = 0, 0, []
for d in db.collection("servers").stream():
    s = d.to_dict() or {}
    if not str(s.get("ipv6") or "").lower().startswith(IDENT):
        continue
    ip = BASE.get(region.get(s.get("nodeId") or "", ""))
    if not ip:
        sin_region.append(s.get("proxmoxId"))
        continue
    if s.get("publicIpv4") == ip:
        ya_ok += 1
        continue
    cambian += 1
    if aplicar:
        d.reference.update({"publicIpv4": ip})

print(f"  {'APLICADO' if aplicar else 'SIMULACION'}: {cambian} a cambiar, {ya_ok} ya correctas")
if sin_region:
    print(f"  ⚠️ {len(sin_region)} sin region conocida en su nodo: {sorted(x for x in sin_region if x)[:12]}")

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
def selected_base_ipv4(config, location):
    """Closed selector shared with the application watchdog; no network changes."""
    selection = config.get('activeBases', {'b0': 'legacy', 'b1': 'legacy'})
    if not isinstance(selection, dict) or set(selection) != {'b0', 'b1'}:
        raise ValueError('activeBases must contain exactly b0 and b1')
    if any(not isinstance(v, str) or v not in ('legacy', 'ecc') for v in selection.values()):
        raise ValueError('activeBases values must be legacy or ecc')
    role = {'falkenstein': 'b0', 'helsinki': 'b1'}.get(str(location or '').strip().lower())
    if role is None:
        return None
    addresses = {
        'legacy': {'b0': '188.40.153.120', 'b1': '37.27.135.250'},
        'ecc': {'b0': '116.202.118.221', 'b1': '95.216.102.179'},
    }
    return addresses[selection[role]][role]

aplicar = "--aplicar" in sys.argv

c = service_account.Credentials.from_service_account_file("/etc/firebase-credentials.json")
db = firestore.Client(credentials=c, project=c.project_id)
cfg_ref = db.collection("config").document("failover_watchdog")
cfg_snap = cfg_ref.get(retry=None, timeout=5)
cfg = (cfg_snap.to_dict() or {}) if cfg_snap.exists else {}
# Validate before previewing or writing even if the fleet happens to be empty.
selected_base_ipv4(cfg, "falkenstein")

@firestore.transactional
def apply_one(tx, ref, node_id, node_location, desired):
    current_cfg = cfg_ref.get(transaction=tx)
    current = ref.get(transaction=tx)
    node = db.collection("proxmox_nodes").document(node_id).get(transaction=tx)
    if current_cfg.update_time != cfg_snap.update_time:
        raise RuntimeError("Hardware selection changed; stop and preview again")
    if not current.exists:
        return False
    data = current.to_dict() or {}
    if data.get("nodeId") != node_id or str((node.to_dict() or {}).get("location") or "").strip().lower() != node_location:
        raise RuntimeError("VM placement or node region changed; preview again")
    if not str(data.get("ipv6") or "").lower().startswith(IDENT):
        return False
    if data.get("publicIpv4") == desired:
        return False
    tx.update(ref, {"publicIpv4": desired})
    return True

# Region de cada nodo, de una sola lectura.
region = {}
for d in db.collection("proxmox_nodes").stream():
    region[d.id] = str((d.to_dict() or {}).get("location") or "").strip().lower()

cambian, ya_ok, sin_region, escritos = 0, 0, [], 0
for d in db.collection("servers").stream():
    s = d.to_dict() or {}
    if not str(s.get("ipv6") or "").lower().startswith(IDENT):
        continue
    ip = selected_base_ipv4(cfg, region.get(s.get("nodeId") or "", ""))
    if not ip:
        sin_region.append(s.get("proxmoxId"))
        continue
    if s.get("publicIpv4") == ip:
        ya_ok += 1
        continue
    cambian += 1
    if aplicar:
        escritos += int(apply_one(db.transaction(), d.reference, s.get("nodeId"), region.get(s.get("nodeId"), ""), ip))

print(f"  {'APLICADO' if aplicar else 'SIMULACION'}: {cambian} candidatos, {escritos} escritos, {ya_ok} ya correctas")
if sin_region:
    print(f"  ⚠️ {len(sin_region)} sin region conocida en su nodo: {sorted(x for x in sin_region if x)[:12]}")

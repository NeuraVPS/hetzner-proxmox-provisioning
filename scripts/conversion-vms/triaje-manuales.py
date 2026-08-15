#!/usr/bin/env python3
"""Triaje de las VMs que quedan en lista manual. Corre EN UNA BASE.
Uso: triaje.py <vmid> [<vmid> ...]
"""
import collections
import json
import sys

sys.path.insert(0, "/root")
import vmtool  # noqa: E402

st = json.load(open("/var/lib/base-nat/state.json"))
ids = [x.strip() for x in sys.argv[1:] if x.strip()]

grupos = collections.defaultdict(list)
for v in ids:
    _i, s = vmtool.doc_de(int(v))
    s = s or {}
    estado = (s.get("status") or "?").lower()
    en_state = v in st
    nuevo = str(s.get("ipv6") or "").lower().startswith("2a01:4f9:c01f:e:")
    if nuevo:
        clave = "YA CONVERTIDA (revisar)"
    elif estado != "running":
        clave = f"apagada ({estado})"
    elif not en_state:
        clave = "encendida pero NO en el NAT de la base"
    else:
        clave = "encendida y en NAT — agente mudo"
    grupos[clave].append((v, s.get("nodeId")))

for k in sorted(grupos, key=lambda x: -len(grupos[x])):
    vms = grupos[k]
    print(f"\n  {len(vms):2d}  {k}")
    porn = collections.Counter(n for _, n in vms)
    for n, c in porn.most_common(6):
        print(f"        {c:2d} en {n}")
    print(f"        vmids: {' '.join(v for v, _ in vms)}")

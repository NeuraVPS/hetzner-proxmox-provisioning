#!/usr/bin/env python3
"""Convierte la flota nodo a nodo. Se ejecuta en el SANDBOX.

Por cada nodo: huella de sus clientes ANTES -> convierte -> huella DESPUES.
Si algún cliente que respondía deja de responder por LAS DOS VIPs, se PARA todo
el despliegue. Un cliente que ya estaba apagado antes no cuenta.
"""
import base64
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

DIR = Path(__file__).resolve().parent
TGZ = base64.b64encode((DIR / "nvconv.tgz").read_bytes()).decode()
VIPS = {"FSN": "94.130.3.118", "HEL": "77.42.49.79"}
LOG = DIR / "rollout.log"
SALTAR = {"0000228-AX162-2-LTD", "0000238-AX162-2-LTD", "0000054-AX162-R"}  # ya convertidos


def log(m):
    line = f"{time.strftime('%H:%M:%S')} {m}"
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def sh(*a, t=120):
    return subprocess.run(a, capture_output=True, text=True, timeout=t)


def probe(ip, port):
    try:
        with socket.create_connection((ip, port), timeout=4):
            return True
    except Exception:
        return False


def huella(vms):
    """{vmid: (fsn, hel)} — sólo de las VMs que responden por alguna VIP."""
    out = {}
    for v in vms:
        r = tuple(probe(ip, 20000 + v) for ip in VIPS.values())
        if any(r):
            out[v] = r
    return out


estado = json.loads(sh("ssh", "-n", "b1", "cat /var/lib/base-nat/state.json", t=90).stdout)
nodos = json.loads(sh("ssh", "-n", "b1", "cat /var/lib/base-nat/pve_nodes.json", t=90).stdout)
por_nodo = {}
for k, v in estado.items():
    por_nodo.setdefault(v.get("nodeId") or "", []).append(int(k))

pendientes = [n for n in sorted(nodos, key=lambda k: int(k.split("-")[0])) if n not in SALTAR]
if len(sys.argv) > 1:
    pendientes = pendientes[:int(sys.argv[1])]
log(f"=== {len(pendientes)} nodos por convertir ===")

ok = fallos = 0
for i, nodo in enumerate(pendientes, 1):
    ip = nodos[nodo]
    vms = sorted(por_nodo.get(nodo, []))
    antes = huella(vms)

    r = sh("ssh", "-n", "b1",
           f"ssh -n -o ConnectTimeout=10 -o StrictHostKeyChecking=no root@{ip} "
           f"'rm -rf /tmp/nvconv && mkdir -p /tmp/nvconv && cd /tmp/nvconv && "
           f"echo {TGZ} | base64 -d | tar xz && bash convertir_nodo.sh'", t=240)
    salida = " ".join((r.stdout + r.stderr).split())

    if "convertido" not in salida:
        fallos += 1
        log(f"[{i}/{len(pendientes)}] ❌ {nodo}: {salida[:200]}")
        if fallos >= 3:
            sys.exit("⛔ 3 fallos de conversión — PARO el despliegue")
        continue

    despues = huella(vms)
    caidos = [v for v in antes if v not in despues]
    if caidos:
        log(f"[{i}/{len(pendientes)}] ⛔⛔ {nodo}: SE CAYERON {caidos} — PARO TODO")
        sys.exit(1)

    ok += 1
    log(f"[{i}/{len(pendientes)}] ✅ {nodo}  {len(antes)} clientes intactos"
        + ("  (sin clientes)" if not vms else ""))

log(f"=== FIN: {ok} convertidos, {fallos} fallidos ===")

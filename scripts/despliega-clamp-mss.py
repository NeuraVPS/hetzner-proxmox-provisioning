#!/usr/bin/env python3
"""Despliega el clamp de MSS a toda la flota. Corre en el SANDBOX.

Instala nvx-mss.sh, lo ejecuta, y comprueba que las tres reglas quedan puestas.
El script del tunel ya lo invoca al arrancar (`[ -x nvx-mss.sh ] && ...`), asi
que con dejar el fichero queda persistente: eso era justo lo que faltaba — el
clamp estaba diseñado y probado en los dos nodos de prueba, pero el fichero
nunca entro en el paquete de conversion de la flota.
"""
import base64
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# El fichero vive en base/snippets/, no junto a este script. Apuntaba mal y
# reventaba con FileNotFoundError — descubierto el 16-08-2026 mientras se
# reponia el clamp de urgencia en la flota, que es justo cuando no quieres
# que la herramienta falle.
DIR = Path(__file__).resolve().parent.parent / "base" / "snippets"
B64 = base64.b64encode((DIR / "nvx-mss.sh").read_bytes()).decode()
CMD = (f"echo {B64} | base64 -d > /usr/local/sbin/nvx-mss.sh && "
       "chmod 755 /usr/local/sbin/nvx-mss.sh && /usr/local/sbin/nvx-mss.sh && "
       "echo REGLAS=$(nft list table inet nvxmss 2>/dev/null | grep -c maxseg)")

nodos = json.loads(subprocess.run(["ssh", "-n", "b1", "cat /var/lib/base-nat/pve_nodes.json"],
                                  capture_output=True, text=True, timeout=120).stdout)


def uno(kv):
    n, ip = kv
    r = subprocess.run(
        ["ssh", "-n", "b1", f"ssh -n -o ConnectTimeout=10 -o StrictHostKeyChecking=no root@{ip} '{CMD}'"],
        capture_output=True, text=True, timeout=150)
    s = " ".join((r.stdout + r.stderr).split())
    return n, ("REGLAS=3" in s), s


with ThreadPoolExecutor(max_workers=14) as ex:
    res = list(ex.map(uno, sorted(nodos.items(), key=lambda kv: int(kv[0].split("-")[0]))))

ok = [n for n, b, _ in res if b]
mal = [(n, s) for n, b, s in res if not b]
print(f"  ✅ clamp puesto en {len(ok)}/{len(res)} nodos")
if mal:
    print(f"  ❌ {len(mal)} sin poner:")
    for n, s in mal[:15]:
        print(f"     {n:26s} {s[:90]}")

#!/usr/bin/env python3
"""Despliega el aislamiento VM<->VM del puente a la flota. Corre en el SANDBOX.

Instala `nvx-aisla.sh` y su unidad, la habilita y COMPRUEBA el resultado en el
nodo: las 4 reglas puestas y la unidad habilitada. No se fia del codigo de
salida —esta API miente demasiadas veces— sino de leer el estado.

A diferencia del clamp de MSS, aqui hace falta unidad propia: no hay ningun
script existente que lo invoque al arrancar, y `/etc/nftables.conf` empieza por
`flush ruleset`, asi que tiene que ejecutarse DESPUES de nftables.

  despliega-aislamiento.py [--nodos a,b,c] [--hilos N] [--solo-mirar]
"""
import argparse
import base64
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DIR = Path(__file__).resolve().parent.parent / "base" / "snippets"
SH = base64.b64encode((DIR / "nvx-aisla.sh").read_bytes()).decode()
UNIDAD = base64.b64encode((DIR / "nvx-aisla.service").read_bytes()).decode()

INSTALA = (
    f"echo {SH} | base64 -d > /usr/local/sbin/nvx-aisla.sh && "
    "chmod 755 /usr/local/sbin/nvx-aisla.sh && "
    f"echo {UNIDAD} | base64 -d > /etc/systemd/system/nvx-aisla.service && "
    "systemctl daemon-reload && "
    "systemctl enable --now nvx-aisla.service >/dev/null 2>&1; "
    "echo REGLAS=$(nft list table bridge nvxaisla 2>/dev/null | grep -c drop) "
    "UNIDAD=$(systemctl is-enabled nvx-aisla.service 2>/dev/null) "
    "ACTIVA=$(systemctl is-active nvx-aisla.service 2>/dev/null)"
)
MIRAR = (
    "echo REGLAS=$(nft list table bridge nvxaisla 2>/dev/null | grep -c drop) "
    "UNIDAD=$(systemctl is-enabled nvx-aisla.service 2>/dev/null) "
    "ACTIVA=$(systemctl is-active nvx-aisla.service 2>/dev/null) "
    "TOCADOS=$(nft list table bridge nvxaisla 2>/dev/null | "
    "  grep -oE 'packets [0-9]+' | awk '{s+=$2} END {print s+0}')"
)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--nodos")
    p.add_argument("--hilos", type=int, default=12)
    p.add_argument("--solo-mirar", action="store_true")
    a = p.parse_args()
    orden = MIRAR if a.solo_mirar else INSTALA

    nodos = json.loads(subprocess.run(
        ["ssh", "-n", "b1", "cat /var/lib/base-nat/pve_nodes.json"],
        capture_output=True, text=True, timeout=120).stdout)
    if a.nodos:
        quiero = set(a.nodos.split(","))
        nodos = {k: v for k, v in nodos.items() if k in quiero}

    def uno(kv):
        n, ip = kv
        try:
            r = subprocess.run(
                ["ssh", "-n", "b1",
                 f"ssh -n -o ConnectTimeout=10 -o StrictHostKeyChecking=no "
                 f"-o BatchMode=yes root@{ip} '{orden}'"],
                capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            return n, {"ERROR": "timeout"}
        d = {}
        for trozo in (r.stdout or "").split():
            if "=" in trozo:
                k, _, v = trozo.partition("=")
                d[k] = v
        if not d:
            d = {"ERROR": " ".join(((r.stdout or "") + (r.stderr or "")).split())[:70]}
        return n, d

    print(f"  {len(nodos)} nodo(s), {a.hilos} hilos, "
          f"{'SOLO MIRAR' if a.solo_mirar else 'INSTALANDO'}\n")
    from collections import Counter
    c = Counter()
    malos, tocados = [], []
    with ThreadPoolExecutor(max_workers=a.hilos) as ex:
        for n, d in ex.map(uno, sorted(nodos.items())):
            bien = d.get("REGLAS") == "4" and d.get("UNIDAD") == "enabled"
            c["ok" if bien else "MAL"] += 1
            if not bien:
                malos.append((n, d))
            if int(d.get("TOCADOS", 0) or 0) > 0:
                tocados.append((n, d["TOCADOS"]))
    print(f"  ok={c['ok']}  MAL={c['MAL']}")
    for n, d in malos[:25]:
        print(f"    ⚠ {n:<26} {d}")
    if tocados:
        print(f"\n  ⚠ nodos donde la regla HA CASADO (alguien habla por capa 2):")
        for n, t in tocados:
            print(f"    {n:<26} {t} paquetes")


if __name__ == "__main__":
    main()

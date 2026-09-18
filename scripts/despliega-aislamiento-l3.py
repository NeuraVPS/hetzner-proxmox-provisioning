#!/usr/bin/env python3
"""Despliega el aislamiento L3 (vmbr0<->vmbr0) a la flota. Corre en el SANDBOX.

Companion de despliega-aislamiento.py, para la tabla `inet nvxaisla3` (capa 3).
Instala nvx-aisla-l3.sh + su unidad, fija el MODE por drop-in y COMPRUEBA el
resultado leyendo el estado del nodo (no el codigo de salida, que miente).

  despliega-aislamiento-l3.py [--nodos a,b,c] [--modo count|drop] [--hilos N] [--solo-mirar]

`--modo count` (defecto) solo cuenta, no descarta: es el canario de flota. Una
vez confirmado que TOCADOS=0 en todos, se repasa con `--modo drop`.
"""
import argparse
import base64
import json
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DIR = Path(__file__).resolve().parent.parent / "base" / "snippets"
SH = base64.b64encode((DIR / "nvx-aisla-l3.sh").read_bytes()).decode()
UNIDAD = base64.b64encode((DIR / "nvx-aisla-l3.service").read_bytes()).decode()


# ⚠️ Nada de comandos inline con comillas embebidas a traves del doble salto
# SSH: el anidado de comillas destroza el patron (contar por SSH en linea dio
# 0 con los mapas llenos, ver neuravps-base-maintenance-reboot-cycle). Se manda
# un script en base64 y se ejecuta dentro del nodo.
_MIRAR_SH = r"""
REGLAS=$(nft list chain inet nvxaisla3 aisla3 2>/dev/null | grep -c oifname)
if nft list chain inet nvxaisla3 aisla3 2>/dev/null | grep -q drop; then MODO=drop; else MODO=count; fi
UNIDAD=$(systemctl is-enabled nvx-aisla-l3.service 2>/dev/null)
ACTIVA=$(systemctl is-active nvx-aisla-l3.service 2>/dev/null)
TOCADOS=$(nft list chain inet nvxaisla3 aisla3 2>/dev/null | grep -oE 'packets [0-9]+' | awk '{s+=$2} END {print s+0}')
echo "REGLAS=$REGLAS MODO=$MODO UNIDAD=$UNIDAD ACTIVA=$ACTIVA TOCADOS=$TOCADOS"
"""


_INSTALA_SH = """\
echo {SH} | base64 -d > /usr/local/sbin/nvx-aisla-l3.sh
chmod 755 /usr/local/sbin/nvx-aisla-l3.sh
echo {UNIDAD} | base64 -d > /etc/systemd/system/nvx-aisla-l3.service
mkdir -p /etc/systemd/system/nvx-aisla-l3.service.d
printf '[Service]\\nEnvironment=MODE={modo}\\n' > /etc/systemd/system/nvx-aisla-l3.service.d/mode.conf
systemctl daemon-reload
systemctl enable nvx-aisla-l3.service >/dev/null 2>&1
systemctl restart nvx-aisla-l3.service >/dev/null 2>&1
"""


def _node_script(modo=None):
    body = "" if modo is None else _INSTALA_SH.format(SH=SH, UNIDAD=UNIDAD, modo=modo)
    return base64.b64encode((body + _MIRAR_SH).encode()).decode()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--nodos")
    p.add_argument("--modo", choices=["count", "drop"], default="count")
    p.add_argument("--hilos", type=int, default=12)
    p.add_argument("--solo-mirar", action="store_true")
    a = p.parse_args()
    b64 = _node_script(None if a.solo_mirar else a.modo)
    orden = f"echo {b64} | base64 -d | bash"

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
          f"{'SOLO MIRAR' if a.solo_mirar else 'INSTALANDO modo ' + a.modo}\n")
    c = Counter()
    malos, tocados = [], []
    with ThreadPoolExecutor(max_workers=a.hilos) as ex:
        for n, d in ex.map(uno, sorted(nodos.items())):
            bien = d.get("REGLAS") == "1" and d.get("UNIDAD") == "enabled"
            c["ok" if bien else "MAL"] += 1
            if not bien:
                malos.append((n, d))
            if int(d.get("TOCADOS", 0) or 0) > 0:
                tocados.append((n, d["TOCADOS"]))
    print(f"  ok={c['ok']}  MAL={c['MAL']}")
    for n, d in malos[:25]:
        print(f"    ⚠ {n:<26} {d}")
    if tocados:
        print("\n  ⚠ nodos donde la regla HA CASADO (reenvio L3 vmbr0<->vmbr0):")
        for n, t in tocados:
            print(f"    {n:<26} {t} paquetes")


if __name__ == "__main__":
    main()

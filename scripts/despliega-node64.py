#!/usr/bin/env python3
"""Despliega la guarda del /64 propio del nodo (`inet nvxnode64`). Corre en el SANDBOX.

Hermano de despliega-aislamiento-l3.py. Instala nvx-node64.py + su unidad y su
timer, la lista de vmids excluidos, fija el MODE por drop-in y COMPRUEBA el
resultado leyendo el estado del nodo (`nvx-node64.py --mirar`), no el codigo de
salida.

  despliega-node64.py [--nodos a,b,c] [--modo count|drop] [--excluir 520,1306,...]
                      [--hilos N] [--base b1|b0] [--solo-mirar | --quitar]

`--modo count` (defecto) solo cuenta y registra: canario. Con FINAL/SALIDA
revisados, se repasa con `--modo drop`.
`--quitar` = marcha atras: para y deshabilita timer y servicio y borra la tabla
(deja los ficheros, inertes). En un nodo a mano:
  systemctl disable --now nvx-node64.timer nvx-node64.service; python3 /usr/local/sbin/nvx-node64.py --quitar
"""
import argparse
import base64
import json
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DIR = Path(__file__).resolve().parent.parent / "base" / "snippets"
PY = base64.b64encode((DIR / "nvx-node64.py").read_bytes()).decode()
SERVICE = base64.b64encode((DIR / "nvx-node64.service").read_bytes()).decode()
TIMER = base64.b64encode((DIR / "nvx-node64.timer").read_bytes()).decode()
# ipv6Enabled=true el 25/11/2026 (Firestore servers.firewall.ipv6Enabled). Ninguna
# usa hoy una direccion del /64 de su nodo; se excluyen por prudencia.
EXCLUIR_DEFECTO = "520,1306,1712,1713"

# ⚠️ Nada de comandos inline con comillas embebidas a traves del doble salto SSH:
# se manda un script en base64 y se ejecuta dentro del nodo.
_MIRAR_SH = "python3 /usr/local/sbin/nvx-node64.py --mirar 2>&1 || echo TABLA=error\n"

_INSTALA_SH = """\
set -e
echo {PY} | base64 -d > /usr/local/sbin/nvx-node64.py
chmod 755 /usr/local/sbin/nvx-node64.py
echo {SERVICE} | base64 -d > /etc/systemd/system/nvx-node64.service
echo {TIMER} | base64 -d > /etc/systemd/system/nvx-node64.timer
mkdir -p /etc/neuravps /etc/systemd/system/nvx-node64.service.d
printf '%s\\n' {excluir} > /etc/neuravps/node64-guard-allow-vmids
printf '[Service]\\nEnvironment=MODE={modo}\\n' > /etc/systemd/system/nvx-node64.service.d/mode.conf
systemctl daemon-reload
systemctl enable nvx-node64.service nvx-node64.timer >/dev/null 2>&1
systemctl start nvx-node64.timer >/dev/null 2>&1
# --forzar: el cambio de MODE o de lista se aplica ya, aunque el estado de /run coincida.
MODE={modo} python3 /usr/local/sbin/nvx-node64.py --forzar >/dev/null
set +e
"""

_QUITAR_SH = """\
systemctl disable --now nvx-node64.timer nvx-node64.service >/dev/null 2>&1
[ -x /usr/local/sbin/nvx-node64.py ] && python3 /usr/local/sbin/nvx-node64.py --quitar >/dev/null
nft list table inet nvxnode64 >/dev/null 2>&1 && nft delete table inet nvxnode64
"""


def _node_script(accion, modo, excluir):
    if accion == "instalar":
        body = _INSTALA_SH.format(PY=PY, SERVICE=SERVICE, TIMER=TIMER, modo=modo,
                                  excluir=" ".join(excluir))
    elif accion == "quitar":
        body = _QUITAR_SH
    else:
        body = ""
    return base64.b64encode((body + _MIRAR_SH).encode()).decode()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--nodos")
    p.add_argument("--modo", choices=["count", "drop"], default="count")
    p.add_argument("--excluir", default=EXCLUIR_DEFECTO)
    p.add_argument("--hilos", type=int, default=12)
    p.add_argument("--base", default="b1")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--solo-mirar", action="store_true")
    g.add_argument("--quitar", action="store_true")
    a = p.parse_args()
    excluir = [x for x in a.excluir.split(",") if x.strip().isdigit()]
    accion = "mirar" if a.solo_mirar else "quitar" if a.quitar else "instalar"
    orden = f"echo {_node_script(accion, a.modo, excluir)} | base64 -d | bash"

    nodos = json.loads(subprocess.run(
        ["ssh", "-n", a.base, "cat /var/lib/base-nat/pve_nodes.json"],
        capture_output=True, text=True, timeout=120).stdout)
    if a.nodos:
        quiero = set(a.nodos.split(","))
        falta = quiero - set(nodos)
        if falta:
            raise SystemExit(f"nodos desconocidos: {sorted(falta)}")
        nodos = {k: v for k, v in nodos.items() if k in quiero}

    def uno(kv):
        n, ip = kv
        try:
            r = subprocess.run(
                ["ssh", "-n", a.base,
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
            d = {"ERROR": " ".join(((r.stdout or "") + (r.stderr or "")).split())[:90]}
        return n, d

    print(f"  {len(nodos)} nodo(s), {a.hilos} hilos via {a.base}, {accion}"
          f"{' modo ' + a.modo + ' excluir ' + ','.join(excluir) if accion == 'instalar' else ''}\n")
    c = Counter()
    malos, casados = [], []
    with ThreadPoolExecutor(max_workers=a.hilos) as ex:
        for n, d in ex.map(uno, sorted(nodos.items())):
            if accion == "quitar":
                bien = d.get("TABLA") == "no" and d.get("TIMER") in ("disabled", "?", "")
            elif accion == "instalar":
                sin64 = d.get("NODE64") == "-"
                bien = d.get("TIMER") == "enabled" and (
                    (sin64 and d.get("TABLA") == "no") or
                    (d.get("TABLA") == "si" and d.get("MODO") == a.modo))
            else:
                bien = "ERROR" not in d
            c["ok" if bien else "MAL"] += 1
            if not bien:
                malos.append((n, d))
            if int(d.get("FINAL", 0) or 0) or int(d.get("SALIDA", 0) or 0) or int(d.get("EXCL", 0) or 0):
                casados.append((n, d))
            if d.get("NODE64") == "-":
                c["sin_64"] += 1
    print(f"  ok={c['ok']}  MAL={c['MAL']}  sin /64 en vmbr0={c['sin_64']}")
    for n, d in malos[:40]:
        print(f"    ⚠ {n:<26} {d}")
    if casados:
        print("\n  Nodos con trafico por la via node64 (FINAL=de fuera sin permiso, SALIDA=invitado"
              " sale con origen node64, EXCL=a una VM excluida):")
        for n, d in casados:
            print(f"    {n:<26} FINAL={d.get('FINAL')} SALIDA={d.get('SALIDA')} EXCL={d.get('EXCL')}"
                  f" INFRA={d.get('INFRA')} EST={d.get('EST')}")


if __name__ == "__main__":
    main()

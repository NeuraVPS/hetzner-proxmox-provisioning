#!/usr/bin/env python3
"""Despliega la guarda del /64 propio del nodo (`inet nvxnode64` + `bridge nvxnode64b`).

Corre en el SANDBOX. Hermano de despliega-aislamiento-l3.py. Instala
nvx-node64.py + su unidad y su timer, la lista de vmids excluidos, fija el MODE
por drop-in, aplica a traves de la unidad (serializada con el timer) y COMPRUEBA
el resultado leyendo el estado del nodo (`nvx-node64.py --mirar`). Sale con
codigo 1 si algun nodo queda MAL.

  despliega-node64.py [--nodos a,b,c] [--modo count|drop] [--modo-salida count|drop]
                      [--excluir 520,1306,...]
                      [--sin64 0000266-KDM-9755] [--hilos N] [--base b1|b0]
                      [--solo-mirar | --quitar]

`--modo` (entrada) y `--modo-salida` (invitado -> fuera con origen en el /64 del
nodo) son OBLIGATORIOS al instalar y al mirar; los dos van al drop-in.
`--solo-mirar` verifica que cada nodo siga protegido en el `--modo` indicado.
`count` solo cuenta y registra (canario); `drop` arma. Marcha atras de un solo
sentido: repetir con ese sentido en `count`. Repetir el mismo modo no reinicia los
contadores (la pasada solo reconstruye si algo cambia).
`--quitar` = marcha atras: para y deshabilita timer y servicio y borra las tablas
(deja los ficheros, inertes). En un nodo a mano:
  systemctl disable --now nvx-node64.timer nvx-node64.service; python3 /usr/local/sbin/nvx-node64.py --quitar
"""
import argparse
import base64
import json
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DIR = Path(__file__).resolve().parent.parent / "base" / "snippets"
PY = base64.b64encode((DIR / "nvx-node64.py").read_bytes()).decode()
SERVICE = base64.b64encode((DIR / "nvx-node64.service").read_bytes()).decode()
TIMER = base64.b64encode((DIR / "nvx-node64.timer").read_bytes()).decode()
# ipv6Enabled=true el 25/11/2026 (Firestore servers.firewall.ipv6Enabled). Ninguna
# usa hoy una direccion del /64 de su nodo; se excluyen por prudencia, solo en el
# nodo donde residen y ligadas a su puerto del puente.
EXCLUIR_DEFECTO = "520,1306,1712,1713"
# Nodos donde vmbr0 NO tiene /64 propio (la guarda no pone nada y es correcto).
SIN64_DEFECTO = "0000266-KDM-9755"

# ⚠️ Nada de comandos inline con comillas embebidas a traves del doble salto SSH:
# se manda un script en base64 y se ejecuta dentro del nodo. La ultima linea es
# siempre la de --mirar, con un marcador para saber que el script llego al final.
_MIRAR_SH = ("if [ -x /usr/local/sbin/nvx-node64.py ]; then "
             "echo NVXFIN=1 $(python3 /usr/local/sbin/nvx-node64.py --mirar 2>&1); "
             "else echo NVXFIN=1 TABLA=no INSTALADO=no "
             "TIMER=$(systemctl is-enabled nvx-node64.timer 2>/dev/null || echo not-found) "
             "SERVICIO=$(systemctl is-enabled nvx-node64.service 2>/dev/null || echo not-found); fi\n")

_INSTALA_SH = """\
set -o pipefail
# 1) Primero el drop-in con los DOS modos y daemon-reload: si el timer vence en
#    medio, el ejecutable nuevo ya ve SALIDA_MODE (el viejo lo ignora).
mkdir -p /etc/neuravps /etc/systemd/system/nvx-node64.service.d
printf '[Service]\\nEnvironment=MODE={modo}\\nEnvironment=SALIDA_MODE={smodo}\\n' > /etc/systemd/system/nvx-node64.service.d/mode.conf.new \
  && mv /etc/systemd/system/nvx-node64.service.d/mode.conf.new /etc/systemd/system/nvx-node64.service.d/mode.conf || echo DROPIN=fallo
printf '%s\\n' {excluir} > /etc/neuravps/node64-guard-allow-vmids
case "$(hostname)" in {sin64pat}) : > /etc/neuravps/node64-guard-sin64 ;; *) rm -f /etc/neuravps/node64-guard-sin64 ;; esac
echo {SERVICE} | base64 -d > /etc/systemd/system/nvx-node64.service
echo {TIMER} | base64 -d > /etc/systemd/system/nvx-node64.timer
systemctl daemon-reload || echo RELOAD=fallo
ENVV=$(systemctl show -p Environment --value nvx-node64.service)
case " $ENVV " in *" MODE={modo} "*) ;; *) echo DROPIN=no_cargado ;; esac
case " $ENVV " in *" SALIDA_MODE={smodo} "*) ;; *) echo DROPIN=no_cargado ;; esac
# Si el drop-in no esta cargado con los dos valores exactos, NO se toca el
# ejecutable ni se aplica: el nodo queda como estaba y sale MAL.
if [ ! -s /etc/systemd/system/nvx-node64.service.d/mode.conf ] || ! systemctl show -p Environment --value nvx-node64.service | tr ' ' '\\n' | grep -qx "SALIDA_MODE={smodo}" \
   || ! systemctl show -p Environment --value nvx-node64.service | tr ' ' '\\n' | grep -qx "MODE={modo}"; then
  echo ABORTADO=dropin
else
# 2) Despues, el ejecutable (sustitucion atomica).
echo {PY} | base64 -d > /usr/local/sbin/nvx-node64.py.new && chmod 755 /usr/local/sbin/nvx-node64.py.new \
  && mv /usr/local/sbin/nvx-node64.py.new /usr/local/sbin/nvx-node64.py || echo INSTALAR=fallo
systemctl enable nvx-node64.service nvx-node64.timer >/dev/null 2>&1 || echo ENABLE=fallo
systemctl start nvx-node64.timer >/dev/null 2>&1 || echo TIMERSTART=fallo
# A traves de la unidad: serializado con el timer (y el script lleva cerrojo).
systemctl start nvx-node64.service >/dev/null 2>&1 || echo APLICAR=fallo
fi
"""

_QUITAR_SH = """\
systemctl disable --now nvx-node64.timer nvx-node64.service >/dev/null 2>&1
[ -x /usr/local/sbin/nvx-node64.py ] && python3 /usr/local/sbin/nvx-node64.py --quitar >/dev/null
nft list table inet nvxnode64 >/dev/null 2>&1 && nft delete table inet nvxnode64
nft list table bridge nvxnode64b >/dev/null 2>&1 && nft delete table bridge nvxnode64b
"""


def _node_script(accion, modo, excluir, sin64=(), smodo="count"):
    if accion == "instalar":
        body = _INSTALA_SH.format(PY=PY, SERVICE=SERVICE, TIMER=TIMER, modo=modo, smodo=smodo,
                                  excluir=" ".join(excluir),
                                  sin64pat="|".join(sorted(sin64)) or "__ninguno__")
    elif accion == "quitar":
        body = _QUITAR_SH
    else:
        body = ""
    return base64.b64encode((body + _MIRAR_SH).encode()).decode()


def veredicto(accion, modo, sin64, n, d, smodo="count"):
    if "ERROR" in d or d.get("NVXFIN") != "1":
        return False
    if any(k in d for k in ("INSTALAR", "ENABLE", "TIMERSTART", "APLICAR", "DROPIN", "RELOAD", "ABORTADO")):
        return False
    if accion == "quitar":
        return (d.get("TABLA") == "no" and d.get("BR", "no") == "no"
                and d.get("TIMER") in ("disabled", "not-found")
                and d.get("SERVICIO") in ("disabled", "not-found"))
    if accion == "instalar":
        unidades = (d.get("TIMER") == "enabled" and d.get("TIMERACT") == "active"
                    and d.get("SERVICIO") == "enabled" and d.get("ULTIMA") == "success")
        return unidades and protegido(modo, sin64, n, d, smodo)
    # mirar: acredita que el nodo sigue protegido en el modo pedido
    return protegido(modo, sin64, n, d, smodo)


def protegido(modo, sin64, n, d, smodo="count"):
    """Tabla completa, en el modo pedido, con el /64 vivo cargado (vacio solo en --sin64)."""
    p64 = d.get("NODE64")
    if n in sin64:
        if p64 != "-":
            return False
    elif p64 in ("error", "-", None):
        return False
    return (d.get("TABLA") == "si" and d.get("MODO") == modo and d.get("MODOSAL") == smodo
            and d.get("N64OK") == "si")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--nodos")
    # Sin valores por defecto a proposito: cada orden dice los DOS sentidos, para
    # que cambiar las excluidas o auditar no desarme (ni arme) un sentido sin querer.
    p.add_argument("--modo", choices=["count", "drop"])
    p.add_argument("--modo-salida", choices=["count", "drop"])
    p.add_argument("--excluir", default=EXCLUIR_DEFECTO)
    p.add_argument("--sin64", default=SIN64_DEFECTO)
    p.add_argument("--hilos", type=int, default=8)
    p.add_argument("--base", default="b1")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--solo-mirar", action="store_true")
    g.add_argument("--quitar", action="store_true")
    a = p.parse_args()
    excluir = [x.strip() for x in a.excluir.split(",") if x.strip().isdigit()]
    sin64 = set(x.strip() for x in a.sin64.split(",") if x.strip())
    accion = "mirar" if a.solo_mirar else "quitar" if a.quitar else "instalar"
    if accion != "quitar" and (not a.modo or not a.modo_salida):
        raise SystemExit("indica --modo y --modo-salida (count|drop) explicitamente")
    orden = f"echo {_node_script(accion, a.modo, excluir, sin64, a.modo_salida)} | base64 -d | bash"

    r = subprocess.run(["ssh", "-n", a.base, "cat /var/lib/base-nat/pve_nodes.json"],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise SystemExit(f"no se pudo leer pve_nodes.json de {a.base}: {r.stderr.strip()}")
    nodos = json.loads(r.stdout)
    if a.nodos:
        quiero = set(x.strip() for x in a.nodos.split(",") if x.strip())
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
        for linea in (r.stdout or "").splitlines():
            for trozo in linea.split():
                if "=" in trozo:
                    k, _, v = trozo.partition("=")
                    d[k] = v
        if r.returncode != 0 and d.get("NVXFIN") != "1":
            d["ERROR"] = f"ssh rc={r.returncode} " + " ".join((r.stderr or "").split())[-80:]
        return n, d

    print(f"  {len(nodos)} nodo(s), {a.hilos} hilos via {a.base}, {accion}"
          f"{' modo ' + a.modo + ' salida ' + a.modo_salida + ' excluir ' + ','.join(excluir) if accion == 'instalar' else ''}\n")
    c = Counter()
    malos, casados = [], []
    with ThreadPoolExecutor(max_workers=a.hilos) as ex:
        for n, d in ex.map(uno, sorted(nodos.items())):
            bien = veredicto(accion, a.modo, sin64, n, d, a.modo_salida)
            c["ok" if bien else "MAL"] += 1
            if not bien:
                malos.append((n, d))
            if any(int(d.get(k, 0) or 0) for k in ("FINAL", "SALIDA", "EXCL", "AJENA", "SPOOF")):
                casados.append((n, d))
    print(f"  ok={c['ok']}  MAL={c['MAL']}")
    for n, d in malos:
        print(f"    ⚠ {n:<26} {d}")
    if casados:
        print("\n  Trafico por la via node64 (FINAL=de fuera sin permiso, SALIDA=del invitado con origen"
              " node64: conexiones nuevas en count, paquetes rechazados en drop, EXCL=a una VM excluida, AJENA=a su direccion en otro puerto):")
        for n, d in casados:
            print(f"    {n:<26} FINAL={d.get('FINAL')} SALIDA={d.get('SALIDA')} EXCL={d.get('EXCL')}"
                  f" AJENA={d.get('AJENA')} SPOOF={d.get('SPOOF')} INFRA={d.get('INFRA')} EST={d.get('EST')}")
    return 1 if malos else 0


if __name__ == "__main__":
    sys.exit(main())

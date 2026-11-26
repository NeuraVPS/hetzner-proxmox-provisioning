#!/usr/bin/env python3
"""Despliega la guarda de red del nodo frente a recargas de ifupdown2 (nvx-netguard).

Corre en el SANDBOX. Hermano de despliega-node64.py. Extrae LITERALMENTE de
install.sh (fuente unica) los tres ficheros:

  /usr/local/sbin/nvx-netguard.sh                                  (la guarda)
  /etc/network/if-post-up.d/nvx-netguard                                (hook de ifupdown2)
  /etc/systemd/system/neuravps-tunnels.service.d/nvx-netguard.conf (ExecStartPost)

los instala con sustitucion atomica, hace daemon-reload, ejecuta UNA pasada de
la guarda (pone la /32 propia tambien en la dummy `nvx-src` y deja una sola
copia de la regla raw del post-up de vmbr0; si a un nodo ya le faltara algo,
relanza neuravps-tunnels.sh, que es la recuperacion de siempre) y COMPRUEBA con
`nvx-netguard.sh --check`. Sale con codigo 1 si algun nodo queda MAL.

⚠️ NUNCA ejecuta ifreload/ifup/ifdown ni toca /etc/network/interfaces: en nodos con
clientes solo se instala y se comprueba. La prueba de recarga se hace aparte, en
un nodo vacio (ver base/docs/ifreload-guard.md).

  despliega-netguard.py [--nodos a,b,c] [--hilos N] [--base b1|b0]
                        [--previo | --solo-mirar | --quitar]

`--previo` = SOLO LECTURA antes de instalar: copia la guarda a /tmp, ejecuta
`--check` y la borra. Acredita que las comprobaciones casan con lo que ya hay en
el nodo (si no, instalar relanzaria los tuneles en el acto). NVXSRC/HOOK/DROPIN
aun no existen y no cuentan. Pasarlo por toda la flota ANTES de instalar.

`--quitar` = marcha atras: borra hook y drop-in (daemon-reload) y la dummy
nvx-src SOLO si la /32 sigue en `lo` (asi nunca se queda el nodo sin ella).
Deja nvx-netguard.sh, inerte. En un nodo a mano:
  rm -f /etc/network/if-post-up.d/nvx-netguard /etc/systemd/system/neuravps-tunnels.service.d/nvx-netguard.conf
  systemctl daemon-reload; ip link del nvx-src   # solo si `ip -4 a show lo` tiene la 10.65.x.y/32
"""
import argparse
import base64
import json
import re
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

INSTALL_SH = Path(__file__).resolve().parent.parent / "install.sh"
GUARD = "/usr/local/sbin/nvx-netguard.sh"
HOOK = "/etc/network/if-post-up.d/nvx-netguard"
DROPIN = "/etc/systemd/system/neuravps-tunnels.service.d/nvx-netguard.conf"


def heredoc(dst, src=INSTALL_SH):
    """Mismo criterio que heredoc() de adoptar-nodo-externo.sh: exactamente un
    `cat > [/mnt]<dst> <<'TERM'`, hasta el terminador, no vacio."""
    lines = Path(src).read_text().split("\n")
    pat = re.compile(r"^\s*cat\s*>\s*(?:/mnt)?" + re.escape(dst) + r"\s+<<'([A-Za-z_]+)'\s*$")
    hits = [(i, m.group(1)) for i, l in enumerate(lines) for m in [pat.match(l)] if m]
    if len(hits) != 1:
        raise SystemExit(f"heredoc {dst}: {len(hits)} coincidencias en {src}")
    i, term = hits[0]
    out = []
    for l in lines[i + 1:]:
        if l == term:
            break
        out.append(l)
    else:
        raise SystemExit(f"heredoc {dst}: sin terminador {term}")
    if not "".join(out).strip():
        raise SystemExit(f"heredoc {dst}: vacio")
    return "\n".join(out) + "\n"


def _b64(s):
    return base64.b64encode(s.encode()).decode()


# La ultima linea es siempre la de --check con un marcador, para saber que el
# script llego al final. `--check` imprime CLAVE=si|no y CT/HOOK/DROPIN.
_MIRAR_SH = ("if [ -x " + GUARD + " ]; then echo NVXFIN=1 INSTALADO=si $(" + GUARD + " --check 2>&1 | tr '\\n' ' '); "
             "else echo NVXFIN=1 INSTALADO=no HOOK=$([ -x " + HOOK + " ] && echo si || echo no) "
             "DROPIN=$([ -f " + DROPIN + " ] && echo si || echo no) "
             "NVXSRCDEV=$(ip link show nvx-src >/dev/null 2>&1 && echo si || echo no); fi\n")

_INSTALA_SH = """\
set -o pipefail
grep -qE '^addon_scripts_support=1' /etc/network/ifupdown2/ifupdown2.conf || echo SCRIPTS=desactivados
[ -r /etc/default/neuravps-tunnels ] || echo CONF=falta
mkdir -p /etc/network/if-post-up.d /etc/systemd/system/neuravps-tunnels.service.d
put() {{ # put <b64> <destino> <modo>
  echo "$1" | base64 -d > "$2.nvxnew" && chmod "$3" "$2.nvxnew" && mv -f "$2.nvxnew" "$2" || echo INSTALAR=fallo
}}
put {GUARD} {guard_path} 755
bash -n {guard_path} || echo SINTAXIS=guarda
put {HOOK} {hook_path} 755
sh -n {hook_path} || echo SINTAXIS=hook
put {DROPIN} {dropin_path} 644
systemctl daemon-reload || echo RELOAD=fallo
systemctl show -p ExecStartPost --value neuravps-tunnels.service | grep -q nvx-netguard || echo DROPIN_CARGADO=no
# Una pasada: nvx-src + una sola regla raw (+ reparacion si ya faltaba algo).
# Sale 1 si tras reparar sigue faltando algo.
{guard_path} || echo PASADA=fallo
"""

_QUITAR_SH = """\
rm -f {hook_path} {dropin_path}
rmdir /etc/systemd/system/neuravps-tunnels.service.d 2>/dev/null
systemctl daemon-reload
. /etc/default/neuravps-tunnels 2>/dev/null
V4="10.65.$(( ${{NODE_ID:-0}} / 256 )).$(( ${{NODE_ID:-0}} % 256 ))"
if ip link show nvx-src >/dev/null 2>&1; then
  if ip -4 -o addr show dev lo | grep -q " inet $V4/32 "; then ip link del nvx-src; else echo NVXSRC_CONSERVADA=sin_copia_en_lo; fi
fi
"""


def node_script(accion):
    if accion == "instalar":
        body = _INSTALA_SH.format(GUARD=_b64(heredoc(GUARD)), HOOK=_b64(heredoc(HOOK)),
                                  DROPIN=_b64(heredoc(DROPIN)), guard_path=GUARD,
                                  hook_path=HOOK, dropin_path=DROPIN)
    elif accion == "quitar":
        body = _QUITAR_SH.format(hook_path=HOOK, dropin_path=DROPIN)
    elif accion == "previo":
        # Sin _MIRAR_SH: no se deja nada instalado.
        return base64.b64encode(_PREVIO_SH.format(GUARD=_b64(heredoc(GUARD))).encode()).decode()
    else:
        body = ""
    return base64.b64encode((body + _MIRAR_SH).encode()).decode()


ERRORES = ("INSTALAR", "SINTAXIS", "RELOAD", "DROPIN_CARGADO", "PASADA", "SCRIPTS", "CONF", "NVXG",
           "NVXSRC_CONSERVADA")
CHEQUEOS = ("NVXSRC", "LOCAL4", "TUNS", "RULES", "DEFS", "DEFMAIN", "IDENT6", "GW4", "GW6")
# Lo que ya tiene que estar bien ANTES de instalar (lo demas lo pone la guarda).
PREVIOS = tuple(k for k in CHEQUEOS if k != "NVXSRC")

_PREVIO_SH = """\
T=$(mktemp /tmp/nvx-netguard.XXXXXX)
echo {GUARD} | base64 -d > "$T" && chmod 700 "$T"
echo NVXFIN=1 PREVIO=si $(bash "$T" --check 2>&1 | tr '\\n' ' ')
rm -f "$T"
"""


def veredicto(accion, d):
    if "ERROR" in d or d.get("NVXFIN") != "1":
        return False
    if any(k in d for k in ERRORES):
        return False
    if accion == "quitar":
        return d.get("HOOK") == "no" and d.get("DROPIN") == "no"
    # Una sola regla raw (0 = falta la del post-up o iptables no responde) y la
    # unidad de tuneles activa (si no, el hook es inerte).
    base = d.get("CT") == "1" and d.get("TUNSVC") == "si"
    if accion == "previo":
        return d.get("PREVIO") == "si" and base and all(d.get(k) == "si" for k in PREVIOS)
    return (base and d.get("INSTALADO") == "si" and all(d.get(k) == "si" for k in CHEQUEOS)
            and d.get("HOOK") == "si" and d.get("DROPIN") == "si")


def parse(stdout):
    d = {}
    for linea in (stdout or "").splitlines():
        for trozo in linea.split():
            if "=" in trozo:
                k, _, v = trozo.partition("=")
                d[k] = v
    return d


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--nodos")
    p.add_argument("--hilos", type=int, default=8)
    p.add_argument("--base", default="b1")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--previo", action="store_true")
    g.add_argument("--solo-mirar", action="store_true")
    g.add_argument("--quitar", action="store_true")
    a = p.parse_args()
    accion = ("previo" if a.previo else "mirar" if a.solo_mirar
              else "quitar" if a.quitar else "instalar")
    orden = f"echo {node_script(accion)} | base64 -d | bash"

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
        d = {}
        for intento in range(3):  # MaxStartups de la base con muchos hilos
            try:
                r = subprocess.run(
                    ["ssh", "-n", a.base,
                     f"ssh -n -o ConnectTimeout=10 -o StrictHostKeyChecking=no "
                     f"-o BatchMode=yes root@{ip} '{orden}'"],
                    capture_output=True, text=True, timeout=240)
            except subprocess.TimeoutExpired:
                return n, {"ERROR": "timeout"}
            d = parse(r.stdout)
            if d.get("NVXFIN") == "1":
                return n, d
            if "exchange_identification" not in (r.stderr or "") or intento == 2:
                d["ERROR"] = f"ssh rc={r.returncode} " + " ".join((r.stderr or "").split())[-80:]
                return n, d
        return n, d

    print(f"  {len(nodos)} nodo(s), {a.hilos} hilos via {a.base}, {accion}\n")
    malos = []
    cuenta = Counter()
    with ThreadPoolExecutor(max_workers=a.hilos) as ex:
        for n, d in ex.map(uno, sorted(nodos.items())):
            ok = veredicto(accion, d)
            cuenta["OK" if ok else "MAL"] += 1
            resumen = " ".join(f"{k}={d[k]}" for k in
                               ("PREVIO", "INSTALADO", *CHEQUEOS, "CT", "TUNSVC", "HOOK", "DROPIN", *ERRORES, "ERROR")
                               if k in d)
            print(f"{'OK ' if ok else 'MAL'} {n:24s} {resumen}", flush=True)
            if not ok:
                malos.append(n)
    print(f"\n  {dict(cuenta)}")
    if malos:
        print("  MAL: " + ",".join(malos))
        sys.exit(1)


if __name__ == "__main__":
    main()

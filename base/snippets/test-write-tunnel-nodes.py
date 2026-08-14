#!/usr/bin/env python3
"""Prueba write_tunnel_nodes (deteccion de cambio, escritura atomica, aviso de
huerfanos) contra un fichero de /tmp — produccion no se toca."""
import importlib.util
import json
import os
import pathlib
import sys

TMP = "/tmp/tn-test.conf"
os.environ["TUNNEL_NODES_FILE"] = TMP
os.environ["TUNNEL_UNIT"] = "no-existe-a-proposito.service"
pathlib.Path(TMP).unlink(missing_ok=True)

spec = importlib.util.spec_from_file_location("sbn", "/tmp/sync_base_nat_new.py")
m = importlib.util.module_from_spec(spec); sys.modules["sbn"] = m; spec.loader.exec_module(m)

real = json.loads(pathlib.Path("/var/lib/base-nat/pve_nodes.json").read_text())
fallos = []
def check(n, c, extra=""):
    print(f"  {'✅' if c else '❌'} {n}{'  ' + extra if extra and not c else ''}")
    if not c: fallos.append(n)

def n_lineas():
    p = pathlib.Path(TMP)
    return len([l for l in p.read_text().splitlines() if l and not l.startswith("#")]) if p.is_file() else -1

# 1. Primera escritura
m.write_tunnel_nodes(real)
check(f"crea el fichero con {len(real)} nodos", n_lineas() == len(real), str(n_lineas()))

# 2. Sin cambios -> no reescribe (mtime intacto)
mt = pathlib.Path(TMP).stat().st_mtime_ns
m.write_tunnel_nodes(real)
check("sin cambios: NO reescribe", pathlib.Path(TMP).stat().st_mtime_ns == mt)

# 3. Nodo nuevo -> lo anade
mas = dict(real, **{"0000240-AX162-2-LTD": "2a01:4f9:aaaa:bbbb::2"})
m.write_tunnel_nodes(mas)
txt = pathlib.Path(TMP).read_text()
check("nodo nuevo: aparece", n_lineas() == len(real) + 1 and "2a01:4f9:aaaa:bbbb::2" in txt)
check("nodo nuevo: slot = id", any(l.split()[:1] == ["240"] and l.split()[2] == "240"
                                   for l in txt.splitlines() if l and not l.startswith("#")))

# 4. Nodo que desaparece -> lo quita y AVISA (no borra tuneles)
m.write_tunnel_nodes(real)
check("nodo retirado: desaparece de la tabla", n_lineas() == len(real))

# 5. Estado vacio -> deja el fichero ANTERIOR intacto (lo critico)
antes = pathlib.Path(TMP).read_text()
m.write_tunnel_nodes({})
check("estado vacio: NO toca el fichero", pathlib.Path(TMP).read_text() == antes)

# 6. Estado con basura -> tampoco
m.write_tunnel_nodes({"sin-numero": "2a01:4f9:1::2"})
check("estado invalido: NO toca el fichero", pathlib.Path(TMP).read_text() == antes)

# 7. Unidad inexistente -> avisa pero NO lanza excepcion
try:
    m.write_tunnel_nodes(mas); ok = True
except Exception as e:
    ok = False; print(f"    excepcion: {e}")
check("unidad inexistente: avisa pero no rompe el sync", ok)

# 8. Fichero sin permisos -> tampoco rompe
pathlib.Path(TMP).unlink(missing_ok=True)
os.environ["TUNNEL_NODES_FILE"] = "/proc/imposible/tn.conf"
m.TUNNEL_NODES_FILE = pathlib.Path("/proc/imposible/tn.conf")
try:
    m.write_tunnel_nodes(real); ok = True
except Exception as e:
    ok = False; print(f"    excepcion: {e}")
check("ruta imposible: avisa pero no rompe el sync", ok)

print()
sys.exit(f"❌ {len(fallos)} FALLO(S): {fallos}" if fallos else 0) if fallos else print("✅ todos los tests pasan")

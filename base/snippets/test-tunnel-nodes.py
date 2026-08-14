#!/usr/bin/env python3
"""Tests de render_tunnel_nodes antes de tocar produccion."""
import importlib.util
import os
import pathlib
import sys

SRC = pathlib.Path(os.environ.get("SBN", "/tmp/sync_base_nat_new.py"))
spec = importlib.util.spec_from_file_location("sbn", SRC)
m = importlib.util.module_from_spec(spec)
sys.modules["sbn"] = m
spec.loader.exec_module(m)

fallos = []


def check(nombre, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {nombre}{'  ' + extra if extra and not cond else ''}")
    if not cond:
        fallos.append(nombre)


# --- 1. Flota normal ---------------------------------------------------------
flota = {"0000002-AX162-R": "2a01:4f9:6a:44eb::2",
         "0000029-EX44": "2a01:4f9:60:1::2",
         "0000228-AX162-2-LTD": "2a01:4f8:2240:201f::2"}
out = m.render_tunnel_nodes(flota)
datos = [l.split() for l in out.splitlines() if l and not l.startswith("#")]
check("flota normal: 3 lineas", len(datos) == 3, str(datos))
check("id y slot coinciden", all(a == c for a, _, c in datos), str(datos))
check("ordenada por id", [int(d[0]) for d in datos] == [2, 29, 228], str(datos))
check("ipv6 correcta del 228",
      ["2a01:4f8:2240:201f::2"] == [d[1] for d in datos if d[0] == "228"])

# --- 2. Estado vacio: NUNCA escribir ----------------------------------------
check("estado vacio -> None", m.render_tunnel_nodes({}) is None)

# --- 3. Ids duplicados: se queda con el primero ------------------------------
dup = {"0000050-AX102": "2a01:4f9:1::2", "0000050-EX44": "2a01:4f9:2::2"}
out = m.render_tunnel_nodes(dup)
datos = [l.split() for l in out.splitlines() if l and not l.startswith("#")]
check("id duplicado -> 1 sola linea", len(datos) == 1, str(datos))
check("duplicado: gana el primero por orden", datos[0][1] == "2a01:4f9:1::2", str(datos))

# --- 4. Fuera de rango: el hueco 0 es de las bases ---------------------------
out = m.render_tunnel_nodes({"0000000-BASE": "2a01:4f9:9::2",
                             "0004096-RARO": "2a01:4f9:8::2",
                             "0000007-EX44": "2a01:4f9:7::2"})
datos = [l.split() for l in out.splitlines() if l and not l.startswith("#")]
check("id 0 y 4096 fuera; queda el 7", [d[0] for d in datos] == ["7"], str(datos))

# --- 5. Nombre sin numero ----------------------------------------------------
out = m.render_tunnel_nodes({"raro-sin-numero": "2a01:4f9:5::2",
                             "0000009-EX44": "2a01:4f9:9::2"})
datos = [l.split() for l in out.splitlines() if l and not l.startswith("#")]
check("nombre sin numero se salta", [d[0] for d in datos] == ["9"], str(datos))

# --- 6. Solo entradas invalidas -> None (no un fichero vacio) ----------------
check("todo invalido -> None", m.render_tunnel_nodes({"raro": "2a01:4f9:5::2"}) is None)

# --- 7. Idempotente ----------------------------------------------------------
check("idempotente", m.render_tunnel_nodes(flota) == m.render_tunnel_nodes(flota))

# --- 8. Lo que generaria HOY vs lo que hay en las bases ----------------------
import json
try:
    nodes = json.loads(pathlib.Path("/var/lib/base-nat/pve_nodes.json").read_text())
    out = m.render_tunnel_nodes(nodes)
    datos = [l.split() for l in out.splitlines() if l and not l.startswith("#")]
    check(f"flota real: {len(datos)} nodos, sin duplicados",
          len(datos) == len(nodes) and len({d[0] for d in datos}) == len(datos))
    vivo = pathlib.Path("/etc/neuravps/tunnel-nodes.conf")
    if vivo.is_file():
        v = [l.split() for l in vivo.read_text().splitlines() if l and not l.startswith("#")]
        check("MISMAS lineas de datos que el fichero vivo (solo cambia la cabecera)",
              datos == v, f"generado={len(datos)} vivo={len(v)}")
except FileNotFoundError:
    print("  (sin /var/lib/base-nat/pve_nodes.json: me salto la prueba contra la flota real)")

print()
if fallos:
    sys.exit(f"❌ {len(fallos)} FALLO(S): {fallos}")
print("✅ todos los tests pasan")

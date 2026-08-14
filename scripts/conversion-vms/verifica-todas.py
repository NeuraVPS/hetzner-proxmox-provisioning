#!/usr/bin/env python3
"""Verificacion final de VMs convertidas. Corre en el SANDBOX.
Exige respuesta NO VACIA: una salida en blanco NO cuenta como verificada.
"""
import socket
import subprocess
import sys

VIPS = ["94.130.3.118", "77.42.49.79"]


def puerto(ip, p):
    try:
        with socket.create_connection((ip, p), timeout=5):
            return True
    except Exception:
        return False


malas = []
for v in [int(x) for x in sys.argv[1:]]:
    r = subprocess.run(["ssh", "-n", "b1", f"cd /root && python3 vmtool.py salida {v}"],
                       capture_output=True, text=True, timeout=180)
    sal = next((l for l in r.stdout.splitlines() if "salida4=" in l), "")
    campos = dict(kv.split("=", 1) for kv in sal.split() if "=" in kv)
    s4, s6, dns = campos.get("salida4", ""), campos.get("salida6", ""), campos.get("dns", "")
    ent = {ip: puerto(ip, 20000 + v) for ip in VIPS}
    bien = bool(s4) and bool(s6) and dns not in ("", "FALLO") and all(ent.values())
    print(f"  vm {v:5d} {'✅' if bien else '❌'}  v4={s4 or '(vacio)':16s} "
          f"v6={s6 or '(vacio)':26s} dns={dns or '(vacio)':16s} "
          f"rdp={'/'.join('OK' if o else 'FALLO' for o in ent.values())}")
    if not bien:
        malas.append(v)

print(f"\n  {len(sys.argv)-1-len(malas)} verificadas, {len(malas)} con problema"
      + (f": {malas}" if malas else ""))

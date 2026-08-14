#!/usr/bin/env python3
"""Convierte VMs al modelo nuevo, una a una y midiendo. Corre en el SANDBOX.

Por cada VM:
  1. agente: v6 /128 + ruta fe80::1 + v4 estatica, y MEDIR dentro (BOUND:)
  2. Firestore ipv6+ipv4  -> la CF mueve DNAT y rutas en las dos bases
  3. verificar ENTRADA por las 2 VIPs y SALIDA real desde dentro

Para al primer fallo. Si el agente no puede ejecutar, la VM va a la lista
MANUAL y NO se toca: por SSH no se puede, se cortaria la sesion al cambiar la IP.
Nunca se reinicia nada.
"""
import socket
import subprocess
import sys
import time

VIPS = ["94.130.3.118", "77.42.49.79"]


def sh(*a, t=180):
    return subprocess.run(a, capture_output=True, text=True, timeout=t)


def en_b1(cmd, t=180):
    r = sh("ssh", "-n", "b1", cmd, t=t)
    return "\n".join(l for l in (r.stdout + r.stderr).splitlines()
                     if "UserWarning" not in l and "return query" not in l).strip()


def puerto(ip, p, t=5):
    try:
        with socket.create_connection((ip, p), timeout=t):
            return True
    except Exception:
        return False


def entrada(vmid):
    return {ip: (puerto(ip, 20000 + vmid), puerto(ip, 30000 + vmid)) for ip in VIPS}


def resumen(e):
    return "  ".join(f"{ip.split('.')[0]}:rdp{'OK' if r else '❌'}/ssh{'OK' if s else '❌'}"
                     for ip, (r, s) in e.items())


vms = [int(x) for x in sys.argv[1:]]
manual, hechas = [], []

for v in vms:
    print(f"\n─── vm {v} ────────────────────────────────────")
    antes = entrada(v)
    print(f"  antes:  {resumen(antes)}")

    out = en_b1(f"cd /root && python3 vmtool.py convert {v}")
    print(f"  {out.splitlines()[-1] if out else '(sin salida)'}")
    if "MANUAL" in out or "BOUND:" not in out:
        manual.append(v)
        print(f"  ⚠️  vm {v} -> MANUAL, no la toco")
        continue

    print(f"  {en_b1(f'python3 /root/fs_set.py {v}').splitlines()[-1]}")

    ok = False
    for i in range(8):
        time.sleep(8)
        e = entrada(v)
        if all(r for r, _ in e.values()):
            ok = True
            break
    e = entrada(v)
    sal = en_b1(f"cd /root && python3 vmtool.py salida {v}")
    print(f"  despues: {resumen(e)}")
    print(f"  {sal.splitlines()[-1] if sal else ''}")

    # Una respuesta VACIA no cuenta como verificada: con la comprobacion
    # anterior, un invitado que no contestara nada pasaba por bueno.
    campos = dict(kv.split("=", 1) for kv in sal.split() if "=" in kv)
    salida_ok = bool(campos.get("salida4")) and bool(campos.get("salida6")) \
        and campos.get("dns") not in (None, "", "FALLO")
    if not ok or not salida_ok:
        print(f"\n⛔ vm {v}: NO verificada — PARO. Revisar antes de seguir.")
        sys.exit(1)
    hechas.append(v)
    print(f"  ✅ vm {v} convertida y verificada")

print(f"\n=== {len(hechas)} convertidas: {hechas}")
if manual:
    print(f"=== {len(manual)} para aplicar A MANO (el agente no ejecuta): {manual}")

#!/usr/bin/env python3
"""Deja el [IPSET base] del nodo con las 2 VIPs de failover. Idempotente.

Los tuneles pasan a anclarse a las VIPs, asi que el GRE llega DESDE la VIP y no
desde la IP principal de la base. Sin esto el `policy_in: DROP` del nodo lo tira.
Aditivo: las entradas viejas se quedan, asi que un nodo del modelo antiguo no se
entera de nada.
"""
import re
import subprocess
import sys

P = "/etc/pve/firewall/cluster.fw"
VIPS = [("2a01:4f8:fff2:95::/64", "VIP FSN (failover) - tuneles anclados a VIP"),
        ("2a01:4f9:fff1:5f::/64", "VIP HEL (failover) - tuneles anclados a VIP")]

s = open(P).read()
m = re.search(r"\[IPSET base\]\n(.*?)(\n\[)", s, re.S)
if not m:
    sys.exit("ABORTO: no encuentro [IPSET base]")
block = m.group(1)

# Quitar cualquier version previa (incluida la de la prueba) y reponer limpio.
lines = [l for l in block.split("\n") if not any(v[0] in l for v in VIPS)]
while lines and not lines[-1].strip():
    lines.pop()
for cidr, note in VIPS:
    lines.append(f"{cidr} # {note}")
new = "\n".join(lines) + "\n"

if new == block:
    print("  ya estaba; sin cambios")
else:
    open(P, "w").write(s[:m.start(1)] + new + s[m.end(1):])
    print("  [IPSET base] actualizado")

subprocess.run(["pve-firewall", "compile"], capture_output=True)
subprocess.run(["pve-firewall", "restart"], capture_output=True)
out = subprocess.run(["ipset", "list", "PVEFW-0-base-v6"], capture_output=True, text=True).stdout
print(f"  VIPs vivas en el ipset: {sum(1 for v in VIPS if v[0].split('::')[0] in out)}/2")

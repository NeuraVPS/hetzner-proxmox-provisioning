#!/usr/bin/env python3
"""Quita del /etc/nftables.conf la regla SMB duplicada y deja el fichero
identico a lo que corre en vivo.

Aplicar la regla en dos pasadas (primero solo `dport`, luego `dport`+`sport`)
dejo la de `dport` DOS veces en el fichero mientras en vivo hay una. Es inocuo
--un accept repetido-- pero es deriva entre lo que corre y lo que se cargaria en
el proximo arranque, que es justo la clase de cosa que muerde meses despues.
"""
import re
import subprocess
import sys

CONF = "/etc/nftables.conf"
DPORT = 'th dport { 135, 137, 138, 139, 445 }'

src = open(CONF).read()
lineas = src.splitlines(keepends=True)
idx = [i for i, l in enumerate(lineas) if DPORT in l and 'oifname "tun-*"' in l]

if len(idx) < 2:
    print(f"  ya limpio ({len(idx)} regla dport)")
else:
    ult = idx[-1]
    # Arrastrar el bloque de comentarios huerfano que quedo justo encima.
    ini = ult
    while ini - 1 >= 0 and lineas[ini - 1].strip().startswith("#"):
        ini -= 1
    del lineas[ini:ult + 1]
    nuevo = "".join(lineas)
    open("/tmp/nft.dedup", "w").write(nuevo)
    r = subprocess.run(["nft", "-c", "-f", "/tmp/nft.dedup"], capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"  ABORTO: nft -c rechaza:\n{r.stderr}")
    subprocess.run(["cp", "/tmp/nft.dedup", CONF], check=True)
    print(f"  quitadas {ult - ini + 1} lineas (regla duplicada + su comentario)")

# Comprobacion final: fichero y vivo tienen que contar lo mismo.
f = len(re.findall(r'oifname "tun-\*" meta l4proto', open(CONF).read()))
v = subprocess.run(["nft", "list", "chain", "inet", "filter", "forward"],
                   capture_output=True, text=True).stdout.count('oifname "tun-*" meta l4proto')
print(f"  fichero={f}  vivo={v}  {'✅ coinciden' if f == v else '❌ SIGUEN DISTINTOS'}")
sys.exit(0 if f == v else 1)

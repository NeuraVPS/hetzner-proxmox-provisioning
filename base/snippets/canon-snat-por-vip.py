#!/usr/bin/env python3
"""La direccion canonica de SNAT pasa a ser DE LA VIP, no de la maquina.

Antes: una regla por base, con su propia direccion. Rompia al moverse una VIP,
porque el nodo enruta esa /128 por un tunel fijo y la base de detras cambiaba.

Ahora: dos reglas, elegidas por el tunel de SALIDA. Lo que sale por un tunel
anclado a la VIP FSN lleva la canonica FSN, y el nodo la devuelve por su
tun-fsn, que termina en quien posea esa VIP. Correcto por construccion, sin
importar donde este cada VIP.

Aplica en vivo (sin flush: hay clientes encima) y persiste en el fichero.
"""
import re
import subprocess
import sys

CONF = "/etc/nftables.conf"
IDENT = "2a01:4f9:c01f:e::/64"
CANON_FSN = "2a01:4f9:c01f:e:ffff::"
CANON_HEL = "2a01:4f9:c01f:e:ffff::2"
NEW = [(f'oifname "tun-fp*" ip6 daddr {IDENT} ct status dnat snat to {CANON_FSN}'),
       (f'oifname "tun-hp*" ip6 daddr {IDENT} ct status dnat snat to {CANON_HEL}')]


def sh(*a):
    return subprocess.run(a, capture_output=True, text=True)


# --- 1. en vivo -------------------------------------------------------------
live = sh("nft", "-a", "list", "chain", "ip6", "nat", "postrouting").stdout
if 'oifname "tun-fp*"' in live:
    print("  vivo: ya aplicado")
else:
    viejas = re.findall(r"^\s*(ip6 daddr .*?ct status dnat.*?snat to \S+) # handle (\d+)$",
                        live, re.M)
    for _, h in viejas:
        r = sh("nft", "delete", "rule", "ip6", "nat", "postrouting", "handle", h)
        if r.returncode:
            sys.exit(f"ABORTO borrando handle {h}: {r.stderr}")
    for rule in NEW:                      # insert -> van al principio, antes
        r = sh("nft", "insert", "rule", "ip6", "nat", "postrouting", *rule.split())
        if r.returncode:
            sys.exit(f"ABORTO insertando: {r.stderr}")
    print(f"  vivo: {len(viejas)} regla(s) vieja(s) fuera, 2 nuevas dentro")

# --- 2. persistido ----------------------------------------------------------
src = open(CONF).read()
if 'oifname "tun-fp*"' in src:
    print("  fichero: ya aplicado")
else:
    pat = re.compile(r"^ *ip6 daddr .*?ct status dnat snat to \S+\n", re.M)
    if not pat.search(src):
        sys.exit("ABORTO: no encuentro la regla canonica vieja en el fichero")
    src = pat.sub("".join(f"        {r}\n" for r in NEW), src, count=1)
    open("/tmp/nft.new", "w").write(src)
    r = sh("nft", "-c", "-f", "/tmp/nft.new")
    if r.returncode:
        sys.exit(f"ABORTO: nft -c rechaza el fichero:\n{r.stderr}")
    sh("cp", "-a", CONF, CONF + ".pre-vip")
    sh("cp", "/tmp/nft.new", CONF)
    print(f"  fichero: actualizado y validado (copia en {CONF}.pre-vip)")

print("  --- postrouting v6 ahora:")
for l in sh("nft", "list", "chain", "ip6", "nat", "postrouting").stdout.splitlines():
    if "snat" in l:
        print("   ", l.strip())

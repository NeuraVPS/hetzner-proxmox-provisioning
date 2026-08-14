#!/usr/bin/env python3
"""Permite SMB entre VMs a traves de la base (tunel -> tunel).

Antes de la conversion, dos VMs del mismo cliente se hablaban NODO a NODO
directamente: sus direcciones salian del /64 del nodo y se enrutaban de forma
nativa. Con el modelo nuevo ese trafico cruza la base, y la cadena `forward` de
la base tiene `policy drop` con reglas solo para tunel->uplink y
veth-host->tunel. No habia NINGUNA regla tunel->tunel, asi que el enlace SMB
moria en la base sin dejar rastro.

Se abre SOLO el juego de puertos de SMB, no de par en par: el nodo destino ya
limita a SMB con el ipset `vm-ident`, pero duplicar aqui la restriccion evita
que una VM creada en el futuro sin firewall quede expuesta a las VMs de otros
clientes. Hoy las 1877 lo llevan activo; manana puede no ser cierto.

Aplica en vivo (sin flush: hay clientes encima) y persiste en el fichero.
"""
import re
import subprocess
import sys

CONF = "/etc/nftables.conf"
# ⚠️ Hay que casar por puerto de ORIGEN ademas de por destino. Entre regiones
# el camino VM<->VM es ASIMETRICO: la ida sale por la base de una region y la
# vuelta por la de la otra, asi que NINGUNA base ve los dos sentidos y conntrack
# no casa nunca. El SYN-ACK llega con destino un puerto efimero; sin `sport` se
# cae y la conexion muere a medias.
REGLA = ('iifname "tun-*" oifname "tun-*" meta l4proto { tcp, udp } '
         'th dport { 135, 137, 138, 139, 445 } accept')
REGLA_VUELTA = ('iifname "tun-*" oifname "tun-*" meta l4proto { tcp, udp } '
                'th sport { 135, 137, 138, 139, 445 } accept')
ANCLA_VIVA = 'iifname "tun-*" oifname "enp6s0"'


def sh(*a):
    return subprocess.run(a, capture_output=True, text=True)


# --- en vivo -----------------------------------------------------------------
live = sh("nft", "list", "chain", "inet", "filter", "forward").stdout
if "th sport" in live:
    print("  vivo: ya aplicado")
else:
    for rl in (REGLA, REGLA_VUELTA):
        r = sh("nft", "add", "rule", "inet", "filter", "forward", *rl.split())
        if r.returncode:
            sys.exit(f"ABORTO en vivo: {r.stderr.strip()}")
    print("  vivo: reglas anadidas (ida y vuelta)")

# --- persistido --------------------------------------------------------------
src = open(CONF).read()
if "th sport { 135, 137, 138, 139, 445 }" in src:
    print("  fichero: ya aplicado")
else:
    m = re.search(r'^(\s*)iifname "tun-\*" oifname "enp6s0" accept\n', src, re.M)
    if not m:
        sys.exit("ABORTO: no encuentro el ancla en el fichero")
    ind = m.group(1)
    nuevo = (m.group(0)
             + f"{ind}# SMB entre VMs del mismo cliente: con el modelo nuevo ese trafico\n"
             + f"{ind}# cruza la base en vez de ir nodo a nodo. Solo los puertos de SMB —\n"
             + f"{ind}# el nodo destino ya limita con el ipset vm-ident, pero duplicarlo\n"
             + f"{ind}# aqui protege a una VM que manana se cree sin firewall.\n"
             + f"{ind}{REGLA}\n"
             + f"{ind}{REGLA_VUELTA}\n")
    src = src[:m.start()] + nuevo + src[m.end():]
    open("/tmp/nft.smb", "w").write(src)
    c = sh("nft", "-c", "-f", "/tmp/nft.smb")
    if c.returncode:
        sys.exit(f"ABORTO: nft -c rechaza:\n{c.stderr}")
    sh("cp", "-a", CONF, CONF + ".pre-smb")
    sh("cp", "/tmp/nft.smb", CONF)
    print(f"  fichero: actualizado y validado (copia en {CONF}.pre-smb)")

print("  --- forward ahora:")
for l in sh("nft", "list", "chain", "inet", "filter", "forward").stdout.splitlines():
    if "tun-" in l or "policy" in l:
        print("   ", l.strip())

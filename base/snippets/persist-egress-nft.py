#!/usr/bin/env python3
"""Persiste en /etc/nftables.conf las reglas del modelo nuevo que hoy solo
viven en memoria. Estricto: cada ancla debe aparecer EXACTAMENTE una vez o
aborta sin tocar nada."""
import re
import subprocess
import sys

CONF = "/etc/nftables.conf"
MARK = "# --- egress-failover (modelo nuevo)"

which = sys.argv[1]
if which == "b0":
    MAIN_V4, MAIN_V6, V6_PREFIX = "188.40.153.120", "2a01:4f8:2b03:18a9::2", "2a01:4f8:2b03:18a9::/64"
    CANON6 = "2a01:4f9:c01f:e:ffff::"
elif which == "b1":
    MAIN_V4, MAIN_V6, V6_PREFIX = "37.27.135.250", "2a01:4f9:3070:3984::2", "2a01:4f9:3070:3984::/64"
    CANON6 = "2a01:4f9:c01f:e:ffff::2"
else:
    sys.exit("uso: persist_nft.py b0|b1")

IDENT = "2a01:4f9:c01f:e::/64"
UP = "enp6s0"

src = open(CONF).read()
if MARK in src:
    sys.exit("YA PERSISTIDO (marca presente); no toco nada")

# (ancla, texto, antes/despues)
EDITS = [
    # 1. Salida IPv4 de invitados (10.64/16) y de los propios nodos (10.65/16).
    (f'        ip saddr 10.0.0.0/24 oifname "{UP}" snat to {MAIN_V4}\n',
     f'{MARK}\n'
     f'        # Salida IPv4 de los invitados del esquema nuevo y de los nodos que\n'
     f'        # ya no tienen IPv4 publica. Ambos llegan encapsulados por un tunel.\n'
     f'        ip saddr 10.64.0.0/16 iifname "tun-*" oifname "{UP}" snat to {MAIN_V4}\n'
     f'        ip saddr 10.65.0.0/16 iifname "tun-*" oifname "{UP}" snat to {MAIN_V4}\n',
     "after"),

    # 2a. Direccion canonica de SNAT de ESTA base hacia el /64 de identidad.
    #     ANTES de la generica: el nodo enruta esta /128 por el tunel correcto.
    (f'        ct status dnat snat to {MAIN_V6}\n',
     f'{MARK}\n'
     f'        # Direccion canonica de SNAT de ESTA base hacia los invitados del\n'
     f'        # esquema nuevo. Va ANTES de la regla generica: cada nodo enruta esta\n'
     f'        # /128 por el tunel que toca, y sin ella la entrada cruzada (RDP por\n'
     f'        # la VIP de la otra region) sale por el tunel equivocado y no vuelve.\n'
     f'        ip6 daddr {IDENT} ct status dnat snat to {CANON6}\n',
     "before"),

    # 2b. Netmap de salida: /64 de identidad -> /64 publico, sufijo intacto.
    (f'        ct status dnat snat to {MAIN_V6}\n',
     f'        # Salida IPv6: netmap del /64 de identidad al /64 publico de la base\n'
     f'        # preservando el sufijo (= vmid). Una regla para toda la flota.\n'
     f'        ip6 saddr {IDENT} oifname "{UP}" snat prefix to {V6_PREFIX}\n',
     "after"),

    # 3. Set de peers GRE, poblado por neuravps-base-tunnels.sh.
    ('table inet filter {\n',
     f'{MARK}\n'
     '    # Nodos con tunel ip6gre hacia esta base. Lo puebla\n'
     '    # neuravps-base-tunnels.sh desde /etc/neuravps/tunnel-nodes.conf, que es\n'
     '    # la unica fuente de verdad: set y tuneles no pueden divergir.\n'
     '    set gre_peers {\n'
     '        type ipv6_addr\n'
     '    }\n\n',
     "after"),

    # 4. Aceptar GRE de esos peers.
    ('        tcp dport 443 accept\n',
     f'{MARK}\n'
     '        # GRE de los nodos convertidos. nexthdr 47 (no 60) porque los tuneles\n'
     '        # se crean con `encaplimit none`; ver §4.5 de la doc.\n'
     '        ip6 nexthdr gre ip6 saddr @gre_peers accept\n',
     "after"),

    # 5. Reenvio entre el netns de Jool y los tuneles, y salida de los tuneles.
    (f'        iifname "veth-host" oifname "{UP}" accept\n',
     f'{MARK}\n'
     '        # Trafico ya traducido por Jool hacia un invitado del esquema nuevo,\n'
     '        # y salida a Internet de lo que llega por los tuneles.\n'
     '        iifname "veth-host" oifname "tun-*" accept\n'
     f'        iifname "tun-*" oifname "{UP}" accept\n',
     "after"),
]

for anchor, text, where in EDITS:
    n = src.count(anchor)
    if n != 1:
        sys.exit(f"ABORTO: el ancla aparece {n} veces (esperaba 1): {anchor.strip()!r}")
    src = src.replace(anchor, anchor + text if where == "after" else text + anchor)

open("/tmp/nftables.conf.new", "w").write(src)
r = subprocess.run(["nft", "-c", "-f", "/tmp/nftables.conf.new"], capture_output=True, text=True)
if r.returncode != 0:
    sys.exit(f"ABORTO: nft -c rechaza el fichero:\n{r.stderr}")

subprocess.run(["cp", "-a", CONF, CONF + ".pre-egress"], check=True)
subprocess.run(["cp", "/tmp/nftables.conf.new", CONF], check=True)
print(f"OK {which}: {len(EDITS)} bloques insertados, sintaxis validada, backup en {CONF}.pre-egress")

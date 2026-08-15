#!/usr/bin/env bash
# Saca la salida a Internet de los invitados del alcance del rdpguard.
#
# Por qué (2026-08-15)
# --------------------
# `table ip rdpguard chain pre` está en `hook prerouting`, así que ve TODO lo
# que entra a la base. Sus dos vetos —bf_static y bf_auto— no filtran por
# puerto: tiran cualquier paquete de esa IP de origen.
#
# Eso era correcto cuando por ahí solo pasaban atacantes de fuera. Con el
# direccionamiento nuevo la salida a Internet de CADA invitado atraviesa la
# base con origen 10.64.x.y, de modo que un invitado que caiga en `bf_auto` se
# queda sin Internet entero —ni DNS, ni ping, ni HTTPS— durante horas, con el
# RDP entrando con toda normalidad. No lo ve ninguna alarma.
#
# Pasó de verdad: las vm570 y vm581, de DOS clientes distintos, sin rastro de
# barrido en bf_seen. El castigo por un falso positivo del detector no puede
# ser dejar a un cliente sin servicio.
#
# El arreglo saca del guard SOLO el tráfico de invitado que no va a los puertos
# de forward. Lo que sí va (10000-39999) sigue cayendo en las mismas reglas de
# siempre: los vetos y los tres detectores se le aplican igual. O sea que un
# invitado comprometido que barra nuestros forwards se sigue bloqueando —lo que
# ya no se le puede quitar es su propia salida.
#
# Van DOS reglas porque `tcp dport` no casa en un paquete que no sea TCP: sin la
# primera, el ICMP y el UDP (DNS) de los invitados seguirían dentro del guard.
#
# Idempotente. Toca la sesión viva y /etc/nftables.conf, para que aguante el
# próximo arranque.
set -euo pipefail

RED_INVITADOS="10.64.0.0/16"
CONF=/etc/nftables.conf
ANCLA='ip saddr @bf_allow accept'

echo "=== eximir la salida de los invitados del rdpguard en $(hostname) ==="

# --- 1) sesión viva --------------------------------------------------------
if nft list chain ip rdpguard pre 2>/dev/null | grep -q "$RED_INVITADOS"; then
  echo "  [vivo] ya estaba"
else
  # `insert` mete al principio, así que se añaden en orden inverso para que
  # queden juntas y por debajo del accept de bf_allow.
  # ⚠️ nft recibe los tokens sin pasar por la shell, asi que el comentario ha
  # de llevar SUS PROPIAS comillas dentro del argumento.
  nft insert rule ip rdpguard pre \
    ip saddr "$RED_INVITADOS" tcp dport != 10000-39999 accept \
    comment '"salida del invitado: fuera del guard (solo se vigilan los forwards)"'
  nft insert rule ip rdpguard pre \
    ip saddr "$RED_INVITADOS" meta l4proto != tcp accept \
    comment '"salida del invitado no-TCP (ICMP/DNS): fuera del guard"' 
  echo "  [vivo] dos reglas insertadas"
fi

# --- 2) persistencia -------------------------------------------------------
# ⚠️ NO vale buscar la red a secas: 10.64.0.0/16 ya sale en la regla de SNAT
# de la salida, asi que la comprobacion daba positivo siempre y se saltaba la
# persistencia — reglas vivas que se perdian en el siguiente arranque.
if grep -q "fuera del guard" "$CONF"; then
  echo "  [conf] ya estaba"
else
  BK="$CONF.bak.guestegress.$(date +%Y%m%d-%H%M%S)"
  cp -a "$CONF" "$BK"
  echo "  [conf] copia en $BK"
  # Solo en la tabla ip (v4). La ip6 no necesita esto: los invitados salen con
  # su IDENT global, no con una 10.64.x.y, y esa no la mete nadie en bf_auto.
  python3 - "$CONF" "$RED_INVITADOS" <<'PY'
import re
import sys

conf, red = sys.argv[1], sys.argv[2]
s = open(conf).read()
i = s.index("table ip rdpguard {")
j = s.index("\ntable ", i + 10) if "\ntable " in s[i + 10:] else len(s)
bloque = s[i:j]
ancla = "        ip saddr @bf_allow accept comment \"trusted infra/ops — exempt from RDP guard\"\n"
assert bloque.count(ancla) == 1, "no encuentro el accept de bf_allow en table ip rdpguard"
nuevo = ancla + (
    f'        ip saddr {red} meta l4proto != tcp accept comment '
    '"salida del invitado no-TCP (ICMP/DNS): fuera del guard"\n'
    f'        ip saddr {red} tcp dport != 10000-39999 accept comment '
    '"salida del invitado: fuera del guard (solo se le vigilan los forwards)"\n')
open(conf, "w").write(s[:i] + bloque.replace(ancla, nuevo) + s[j:])
print("  [conf] reglas escritas en table ip rdpguard")
PY
fi

# --- 3) el fichero tiene que cargar, o no sirve de nada ---------------------
nft -c -f "$CONF" && echo "  [conf] sintaxis OK"

echo "--- como queda la cadena ---"
nft list chain ip rdpguard pre | sed 's/^/  /'

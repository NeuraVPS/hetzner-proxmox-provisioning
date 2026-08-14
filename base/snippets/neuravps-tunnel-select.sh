#!/usr/bin/env bash
# Apunta las rutas por defecto del nodo (las suyas y las de sus invitados) al
# tunel de una region. Idempotente y sin efecto si ya esta apuntando ahi.
# Uso: neuravps-tunnel-select.sh <fsn|hel>
set -u
. /etc/default/neuravps-tunnels
REGION="$1"
TUN="tun-$REGION"
NODE_V4="10.65.$(( NODE_ID / 256 )).$(( NODE_ID % 256 ))"

ip link show "$TUN" >/dev/null 2>&1 || { echo "$TUN no existe" >&2; exit 1; }

# Si ya estamos ahi, no tocar nada: reescribir la ruta por defecto tira las
# conexiones en curso de los invitados.
cur="$(ip -4 route show default | sed -n 's/.*dev \([^ ]*\).*/\1/p' | head -1)"
[ "$cur" = "$TUN" ] && exit 0

ip -6 route replace default dev "$TUN" table 100
ip route    replace default dev "$TUN" table 101
ip route    replace default dev "$TUN" src "$NODE_V4" table 102

# El nodo ya no tiene IPv4 publica: TODA su salida v4 va por el tunel y la base
# hace el SNAT. El `src` es obligatorio: sin direccion de origen el kernel no
# puede elegir una y la conexion ni sale.
if [ "${DEFAULT_V4_VIA_TUNNEL:-0}" = "1" ]; then
  ip route replace default dev "$TUN" src "$NODE_V4"
fi

logger -t neuravps-tunnels "salida por defecto -> $TUN"
echo "salida por defecto -> $TUN"

#!/usr/bin/env bash
# Lado BASE de los tuneles base<->nodo. Idempotente: se puede correr en caliente
# sin cortar trafico (solo recrea un tunel si sus parametros han cambiado).
# Ver base/docs/egress-failover-e-ipv6-estable.md §4.5.
#
# v2 (2026-08-14): DOS juegos de tuneles por base, uno por VIP, los dos siempre
# en pie. En cada momento solo lleva trafico el de la VIP que esta base posee de
# verdad; el otro esta de reserva. Cuando una VIP se mueve, el de reserva de la
# superviviente se activa SOLO, sin reconfigurar nada en ningun lado.
#
# ⚠️ UN SOLO juego NO vale, aunque se ancle a la VIP: al moverse la VIP los
# paquetes del nodo llegan a la superviviente dirigidos a una VIP para la que no
# tiene ningun tunel con ese par (local, remoto), y se caen. Medido 2026-08-14.
set -u

. /etc/default/neuravps-base-tunnels

NODES_FILE="${NODES_FILE:-/etc/neuravps/tunnel-nodes.conf}"
TRANSIT_BASE="${TRANSIT_BASE:-2a01:4f9:c01f:e:ffff::}"
VIP_FSN="${VIP_FSN:-2a01:4f8:fff2:95::2}"
VIP_HEL="${VIP_HEL:-2a01:4f9:fff1:5f::2}"
: "${HOME_REGION:?falta HOME_REGION (fsn|hel) en /etc/default/neuravps-base-tunnels}"

transit() { printf '%s%x' "$TRANSIT_BASE" $(( $1 * 16 + $2 )); }

CANON_FSN="$(transit 0 0)"
CANON_HEL="$(transit 0 2)"

# Las dos canonicas viven en lo, en las DOS bases: son direcciones de la VIP, no
# de la maquina, y la base debe poder usarlas como origen del SNAT sea cual sea
# el tunel por el que salga. Solo son alcanzables por tunel, asi que la ruta del
# nodo decide sin ambiguedad a quien llegan.
ip -6 addr replace "${CANON_FSN}/128" dev lo
ip -6 addr replace "${CANON_HEL}/128" dev lo

[ -r "$NODES_FILE" ] || { echo "sin $NODES_FILE: nada que hacer"; exit 0; }

rc=0
peers=""

# Un unico volcado de todos los tuneles en vez de un `ip` por tunel: a escala de
# flota son 2 tuneles x 234 nodos = 468, y preguntar uno a uno cuesta ~1400
# procesos en cada arranque, con base-nat-boot esperando detras (Before=).
TUNNELS_NOW="$(ip -6 tunnel show 2>/dev/null)"

mk() { # iface local_vip remoto direccion/127
  local linea cur
  linea="$(printf '%s\n' "$TUNNELS_NOW" | grep -m1 "^$1:")"
  cur="$(printf '%s' "$linea" | sed -n 's/.*remote \([0-9a-f:]*\) .*/\1/p')"
  if [ "$cur" != "$3" ] || ! printf '%s' "$linea" | grep -q 'encaplimit none'; then
    ip link del "$1" 2>/dev/null
    # `encaplimit none` OBLIGATORIO, ver §4.5: sin el, nexthdr 60 en vez de 47 y
    # el firewall deja de casar GRE. Muere en silencio.
    ip link add name "$1" type ip6gre local "$2" remote "$3" encaplimit none || return 1
  fi
  ip link set "$1" mtu 1456 up
  ip -6 addr replace "$4" dev "$1"
  # rp_filter=0: con anclaje a VIP el camino es asimetrico A PROPOSITO (entra
  # por el tunel de la VIP que poseemos, sale por el de casa). El filtro de ruta
  # inversa lo tira como "martian source", y el sintoma enganya: IPv6 pasa
  # (Linux no tiene rp_filter v6) y IPv4 no. El modo laxo (2) NO basta.
  # `all` tambien: el kernel usa el MAXIMO de all y de la interfaz, y PVE deja
  # all=2 desde /usr/lib/sysctl.d/pve-firewall.conf. Con all=2 no hay forma de
  # bajar de laxo, que ya medimos que NO basta.
  sysctl -qw "net.ipv4.conf.all.rp_filter=0" 2>/dev/null
  sysctl -qw "net.ipv4.conf.$1.rp_filter=0" 2>/dev/null
}

while read -r id node_v6 slot _rest; do
  case "${id:-}" in ''|\#*) continue ;; esac
  [ -n "${node_v6:-}" ] || continue
  slot="$(( 10#${slot:-$id} ))"
  id="$(( 10#$id ))"
  # El hueco 0 esta reservado a las canonicas de las bases.
  [ "$slot" -ge 1 ] && [ "$slot" -le 4095 ] || { echo "nodo $id: slot $slot fuera de 1-4095" >&2; rc=1; continue; }

  mk "tun-fp${id}" "$VIP_FSN" "$node_v6" "$(transit "$slot" 0)/127" || { rc=1; continue; }
  mk "tun-hp${id}" "$VIP_HEL" "$node_v6" "$(transit "$slot" 2)/127" || { rc=1; continue; }

  # Ruta al HOST del nodo por el tunel de NUESTRA region. Si esta base tambien
  # acaba con la otra VIP, sigue valiendo: seguimos poseyendo la nuestra. Las
  # rutas POR VM (/128 y /32) las reconcilia sync-base-nat.py desde Firestore.
  ip route replace "10.65.$(( id / 256 )).$(( id % 256 ))" dev "tun-${HOME_REGION:0:1}p${id}" scope link || rc=1

  peers="${peers:+$peers, }$node_v6"
done < "$NODES_FILE"

if [ -n "$peers" ]; then
  if nft list set inet filter gre_peers >/dev/null 2>&1; then
    nft add element inet filter gre_peers "{ $peers }" 2>/dev/null || true
  else
    echo "AVISO: inet filter gre_peers no existe todavia (nftables.conf sin recargar)" >&2
  fi
fi

exit "$rc"

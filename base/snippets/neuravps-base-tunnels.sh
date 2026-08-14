#!/usr/bin/env bash
# Lado BASE de los tuneles base<->nodo. Idempotente: se puede correr en caliente
# sin cortar trafico (solo recrea un tunel si sus parametros han cambiado).
# Ver base/docs/egress-failover-e-ipv6-estable.md §4.5.
set -u

. /etc/default/neuravps-base-tunnels

NODES_FILE="${NODES_FILE:-/etc/neuravps/tunnel-nodes.conf}"
TRANSIT_BASE="${TRANSIT_BASE:-2a01:4f9:c01f:e:ffff::}"
: "${BASE_V6:?falta BASE_V6 en /etc/default/neuravps-base-tunnels}"
: "${BASE_OFF:?falta BASE_OFF en /etc/default/neuravps-base-tunnels}"

# Direccion de transito. Cada nodo tiene un hueco de 16 direcciones dentro del
# /112; dentro del hueco, cada base ocupa un /127 (b0 -> +0/+1, b1 -> +2/+3).
transit() { printf '%s%x' "$TRANSIT_BASE" $(( $1 * 16 + $2 )); }

[ -r "$NODES_FILE" ] || { echo "sin $NODES_FILE: nada que hacer"; exit 0; }

rc=0
peers=""

while read -r id node_v6 slot _rest; do
  case "${id:-}" in ''|\#*) continue ;; esac
  [ -n "${node_v6:-}" ] || continue
  slot="${slot:-$id}"

  iface="tun-p${id}"
  local_addr="$(transit "$((10#$slot))" "$BASE_OFF")/127"
  node_v4="10.65.$(( 10#$id / 256 )).$(( 10#$id % 256 ))"

  # Recrear SOLO si cambian los parametros: un `ip link del` gratuito tira el
  # trafico de los clientes de ese nodo durante ~1 s en cada arranque del
  # servicio.
  show="$(ip -6 tunnel show "$iface" 2>/dev/null)"
  cur_remote="$(printf '%s' "$show" | sed -n 's/.*remote \([0-9a-f:]*\) .*/\1/p')"
  if [ "$cur_remote" != "$node_v6" ] || ! printf '%s' "$show" | grep -q 'encaplimit none'; then
    ip link del "$iface" 2>/dev/null
    # `encaplimit none` es OBLIGATORIO: por defecto ip6gre anade una cabecera de
    # extension Destination Options y el nexthdr pasa a 60 en vez de 47, asi que
    # las reglas de firewall que casan GRE no casan y el tunel muere EN SILENCIO
    # (UP en los dos extremos, TX subiendo, RX clavado a 0).
    if ! ip link add name "$iface" type ip6gre local "$BASE_V6" remote "$node_v6" encaplimit none; then
      echo "ERROR: no pude crear $iface -> $node_v6" >&2
      rc=1
      continue
    fi
  fi
  ip link set "$iface" mtu 1456 up || rc=1
  ip -6 addr replace "$local_addr" dev "$iface" || rc=1

  # Ruta al HOST del nodo. Las rutas por VM (/128 de identidad y /32 privada)
  # las reconcilia sync-base-nat.py desde Firestore; aqui va solo lo del nodo.
  ip route replace "$node_v4" dev "$iface" scope link || rc=1

  peers="${peers:+$peers, }$node_v6"
done < "$NODES_FILE"

# Peers GRE autorizados en el firewall, del mismo fichero que los tuneles para
# que set y tuneles no puedan divergir. NO es fatal: mientras /etc/nftables.conf
# no se haya recargado el set aun no existe y el ruleset vivo sigue trayendo las
# reglas GRE explicitas de antes.
if [ -n "$peers" ]; then
  if nft list set inet filter gre_peers >/dev/null 2>&1; then
    nft add element inet filter gre_peers "{ $peers }" 2>/dev/null || true
  else
    echo "AVISO: inet filter gre_peers no existe todavia (nftables.conf sin recargar)" >&2
  fi
fi

exit "$rc"

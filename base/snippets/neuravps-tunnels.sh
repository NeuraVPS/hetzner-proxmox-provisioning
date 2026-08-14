#!/usr/bin/env bash
# Lado NODO de los tuneles base<->nodo. Idempotente.
# Ver base/docs/egress-failover-e-ipv6-estable.md §4.5.
#
# v2 (2026-08-14): ANCLADO A LAS VIPs DE FAILOVER, no a las IPs principales de
# las bases. El tunel deja de apuntar a una MAQUINA y apunta a un SERVICIO: al
# moverse una VIP, el tunel la sigue sin tocar nada y termina en la
# superviviente. Es lo que hace invisible el mantenimiento de una base.
set -u

. /etc/default/neuravps-tunnels

: "${NODE_ID:?falta NODE_ID}"
SLOT="${SLOT:-$NODE_ID}"
TRANSIT_BASE="${TRANSIT_BASE:-2a01:4f9:c01f:e:ffff::}"
VIP_FSN="${VIP_FSN:-2a01:4f8:fff2:95::2}"
VIP_HEL="${VIP_HEL:-2a01:4f9:fff1:5f::2}"
IDENT6="${IDENT6:-2a01:4f9:c01f:e::/64}"
HOME_REGION="${HOME_REGION:-fsn}"

# El /112 da 4096 huecos de 16 direcciones. El hueco 0 esta RESERVADO para las
# direcciones canonicas de las bases, asi que un nodo nunca puede ocuparlo.
[ "$SLOT" -ge 1 ] && [ "$SLOT" -le 4095 ] || { echo "SLOT $SLOT fuera de rango 1-4095" >&2; exit 1; }

transit() { printf '%s%x' "$TRANSIT_BASE" $(( $1 * 16 + $2 )); }

NODE_V6="$(ip -6 addr show scope global | sed -n 's#.*inet6 \([0-9a-f:]*\)/128.*#\1#p' | head -1)"
[ -n "$NODE_V6" ] || NODE_V6="$(ip -6 addr show dev "$(ip -6 route show default | sed -n 's/.*dev \([^ ]*\).*/\1/p' | head -1)" scope global | sed -n 's#.*inet6 \([0-9a-f:]*\)/.*#\1#p' | head -1)"
NODE_V4="10.65.$(( NODE_ID / 256 )).$(( NODE_ID % 256 ))"

# Direcciones canonicas: son DE LA VIP, no de la maquina. Por eso el nodo puede
# enrutarlas siempre por el mismo tunel aunque la base de detras cambie.
CANON_FSN="$(transit 0 0)"
CANON_HEL="$(transit 0 2)"

mk() { # iface remoto direccion_local/127
  local show cur
  show="$(ip -6 tunnel show "$1" 2>/dev/null)"
  cur="$(printf '%s' "$show" | sed -n 's/.*remote \([0-9a-f:]*\) .*/\1/p')"
  if [ "$cur" != "$2" ] || ! printf '%s' "$show" | grep -q 'encaplimit none'; then
    ip link del "$1" 2>/dev/null
    # `encaplimit none` es OBLIGATORIO: por defecto ip6gre mete una cabecera
    # DSTOPT, el nexthdr pasa a 60 en vez de 47 y las reglas de firewall que
    # casan GRE dejan de casar. El tunel muere EN SILENCIO: UP en los dos
    # extremos, TX subiendo, RX clavado a 0.
    ip link add name "$1" type ip6gre local "$NODE_V6" remote "$2" encaplimit none || return 1
  fi
  ip link set "$1" mtu 1456 up
  ip -6 addr replace "$3" dev "$1"
  # rp_filter=0 en los tuneles. Con anclaje a VIP el camino es asimetrico A
  # PROPOSITO: el trafico entra por el tunel de la VIP que la base posee y sale
  # por el de casa. El filtro de ruta inversa lo tira como "martian source" y el
  # sintoma es desconcertante — IPv6 pasa (Linux no tiene rp_filter para v6) y
  # IPv4 no. El modo laxo (2) NO basta: tiene que ser 0.
  # Riesgo acotado: estos enlaces solo llevan GRE entre maquinas nuestras, ya
  # filtrado por [IPSET base]; la uplink conserva su rp_filter.
  sysctl -qw "net.ipv4.conf.$1.rp_filter=0" 2>/dev/null
}

[ -x /usr/local/sbin/nvx-mss.sh ] && /usr/local/sbin/nvx-mss.sh >/dev/null 2>&1

mk tun-fsn "$VIP_FSN" "$(transit "$SLOT" 1)/127"
mk tun-hel "$VIP_HEL" "$(transit "$SLOT" 3)/127"

# Direccion propia del nodo para su trafico saliente. NO 10.64.255.1: esa es la
# puerta de enlace de los invitados y es identica en todos los nodos, asi que la
# base no sabria a que tunel devolver el retorno.
ip addr replace "${NODE_V4}/32" dev lo

ip -6 route replace "$IDENT6" dev vmbr0

# Cada canonica por SU tunel. Esto es lo que hace que el retorno acabe siempre
# en la base que emitio, sea cual sea.
#
# Y los DOS /127 de transito de este nodo, cada uno por su tunel. Son
# imprescindibles en la tabla 100: el transito vive DENTRO del /64 de identidad,
# asi que una respuesta del invitado hacia la direccion de transito de una base
# casa la regla de politica y cae en la tabla 100. Sin estas rutas solo encuentra
# la ruta por defecto y sale por el tunel de CASA — de modo que todo lo que
# origine la base REMOTA (sondeo cruzado de conncheck, file-bridge) se pierde.
for T in main 100; do
  ip -6 route replace "${CANON_FSN}/128" dev tun-fsn table $T
  ip -6 route replace "${CANON_HEL}/128" dev tun-hel table $T
  ip -6 route replace "$(transit "$SLOT" 0)/127" dev tun-fsn table $T
  ip -6 route replace "$(transit "$SLOT" 2)/127" dev tun-hel table $T
done

# Tablas de politica. Identificadores NUMERICOS a proposito: /etc/iproute2/
# rt_tables no existe en todos los nodos y con nombre falla en silencio.
# `iif vmbr0` es OBLIGATORIO: las direcciones de transito viven DENTRO del /64
# de identidad, asi que sin acotar por interfaz el trafico que llega DE la base
# tambien casaria y el nodo lo devolveria por el tunel: bucle.
ip -6 rule del from "$IDENT6" iif vmbr0 lookup 100 2>/dev/null
ip -6 rule add from "$IDENT6" iif vmbr0 lookup 100 priority 100
ip rule del from 10.64.0.0/16 iif vmbr0 lookup 101 2>/dev/null
ip rule add from 10.64.0.0/16 iif vmbr0 lookup 101 priority 101
ip rule del from "$NODE_V4" lookup 102 2>/dev/null
ip rule add from "$NODE_V4" lookup 102 priority 102

# --- region de casa ----------------------------------------------------------
# `auto` = la mas cercana, medida. Es el criterio correcto (el trafico del
# cliente debe salir por la base LOCAL) y ademas evita que install.sh tenga que
# averiguar en que centro de datos esta el nodo: la separacion es de ~1 ms
# contra ~20 ms, asi que no hay ambiguedad posible.
if [ "$HOME_REGION" = auto ]; then
  best=""; bestms=999999
  for r in fsn hel; do
    c=$([ "$r" = fsn ] && echo "$CANON_FSN" || echo "$CANON_HEL")
    t0=$(date +%s%N)
    if timeout 3 bash -c "</dev/tcp/$c/443" 2>/dev/null; then
      ms=$(( ($(date +%s%N) - t0) / 1000000 ))
      [ "$ms" -lt "$bestms" ] && { bestms=$ms; best=$r; }
    fi
  done
  HOME_REGION="${best:-fsn}"
  logger -t neuravps-tunnels "region de casa detectada: $HOME_REGION (${bestms} ms)"
  # Cachearla: si en el proximo arranque la base local esta caida, la medicion
  # elegiria la remota y el nodo se quedaria ahi para siempre.
  sed -i "s/^HOME_REGION=.*/HOME_REGION=$HOME_REGION/" /etc/default/neuravps-tunnels
fi

# Rutas por defecto hacia el tunel de casa. El sondeo
# (neuravps-tunnel-probe.sh) las reapunta al otro si esta base deja de contestar.
/usr/local/sbin/neuravps-tunnel-select.sh "$HOME_REGION"

exit 0

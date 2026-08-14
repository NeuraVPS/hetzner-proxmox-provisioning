#!/usr/bin/env bash
# Sondeo del tunel activo. GRE es SIN ESTADO: el tunel NO se cae cuando la base
# muere, sigue UP para siempre apuntando a una caja muerta. Sin esto, la unica
# recuperacion posible es que alguien mueva la VIP (~5-6 min en una caida no
# planificada). Con esto, el nodo se pasa al otro tunel en ~30 s.
#
# El anclado a VIPs y el sondeo son ORTOGONALES y cubren casos distintos:
#   - mantenimiento PLANIFICADO: movemos la VIP antes -> 0 s, sin sondeo.
#   - muerte NO planificada     : el sondeo salta a la otra region -> ~30 s.
set -u
. /etc/default/neuravps-tunnels

TRANSIT_BASE="${TRANSIT_BASE:-2a01:4f9:c01f:e:ffff::}"
HOME_REGION="${HOME_REGION:-fsn}"
OTHER_REGION=$([ "$HOME_REGION" = fsn ] && echo hel || echo fsn)
STATE=/run/neuravps-tunnel-probe.state
FAILS_TO_SWITCH=2      # ~30 s con el timer a 15 s
OKS_TO_RETURN=4        # volver a casa cuesta mas: evita el ping-pong

canon() { [ "$1" = fsn ] && printf '%s0' "$TRANSIT_BASE" || printf '%s2' "$TRANSIT_BASE"; }

# La base contesta 443 (nginx) por su direccion canonica. Un TCP completo prueba
# ida Y vuelta; el ping NO sirve: PVE tira el eco ICMPv6 y da falso negativo.
alive() { timeout 4 bash -c "</dev/tcp/$(canon "$1")/443" 2>/dev/null; }

cur="$(ip -4 route show default | sed -n 's/.*dev tun-\([a-z]*\).*/\1/p' | head -1)"
[ -n "$cur" ] || exit 0

read -r st n < "$STATE" 2>/dev/null || { st=""; n=0; }
[ "$st" = "$cur" ] || n=0

if [ "$cur" = "$HOME_REGION" ]; then
  if alive "$HOME_REGION"; then n=0; else
    n=$((n+1))
    if [ "$n" -ge "$FAILS_TO_SWITCH" ] && alive "$OTHER_REGION"; then
      logger -t neuravps-tunnel-probe "casa ($HOME_REGION) no responde x$n -> me paso a $OTHER_REGION"
      /usr/local/sbin/neuravps-tunnel-select.sh "$OTHER_REGION" && { echo "$OTHER_REGION 0" > "$STATE"; exit 0; }
    fi
  fi
else
  # Estamos fuera de casa: volver solo cuando casa lleve un rato estable.
  if alive "$HOME_REGION"; then
    n=$((n+1))
    if [ "$n" -ge "$OKS_TO_RETURN" ]; then
      logger -t neuravps-tunnel-probe "casa ($HOME_REGION) estable x$n -> vuelvo"
      /usr/local/sbin/neuravps-tunnel-select.sh "$HOME_REGION" && { echo "$HOME_REGION 0" > "$STATE"; exit 0; }
    fi
  else n=0; fi
fi

echo "$cur $n" > "$STATE"

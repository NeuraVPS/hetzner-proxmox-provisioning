#!/usr/bin/env bash
# D) Aislamiento de CAPA 3 en el nodo: descarta el reenvío vmbr0 -> vmbr0.
#
# QUE RESUELVE
# `nvx-aisla` (tabla bridge) corta el trafico VM<->VM directo por CAPA 2. Pero
# el cliente es Administrador y puede ponerse una direccion de ORIGEN ajena
# (fuera de 10.64/16 o del /64 de identidad); entonces el nodo, que tiene
# `ip_forward=1` y `send_redirects=1`, lo ENRUTA por capa 3 a la vecina del
# mismo nodo por vmbr0, saltandose la puerta de enlace y la base. La cadena
# forward del nodo esta vacia con `policy accept` y `cluster.fw` (policy_forward
# DROP) no se aplica porque no corre el backend nftables de proxmox-firewall.
# Medido el 18/09/2026: con origen 10.0.99.99 / 192.168.1.10 / 2001:db8::5 el
# nodo decide `dev vmbr0` hacia la vecina.
#
# QUE HACE
# Una tabla propia `inet nvxaisla3` con un hook forward que descarta
# `iifname "vmbr0" oifname "vmbr0"`. En operacion normal ese reenvio NO existe:
# la salida del invitado es vmbr0->tun, la entrada tun->vmbr0, y el SMB entre
# VMs del mismo cliente viaja por la base (tun->vmbr0 en la vuelta). Solo el
# atajo L3 entre vecinas es vmbr0->vmbr0.
#
# MODE=count (defecto en el canario): solo cuenta, NO descarta — para medir en
#            nodos vivos que no hay trafico legitimo vmbr0->vmbr0 antes de armar.
# MODE=drop: descarta (y cuenta).
#
# ⚠️ `policy accept` a proposito: si algo se escapa, pasa en vez de cortarse.
# ⚠️ La unidad va con After= + PartOf= nftables.service, NUNCA Requires=/Wants=
#    (la trampa del flush ruleset; ver nvx-aisla.service).
#
# Idempotente: reconstruye su propia tabla. Rollback: `nft delete table inet nvxaisla3`.
set -u
MODE=${MODE:-drop}
case "$MODE" in
  count) VERDICT="counter" ;;
  drop)  VERDICT="counter drop" ;;
  *) echo "nvx-aisla-l3: MODE debe ser count o drop" >&2; exit 2 ;;
esac

nft delete table inet nvxaisla3 2>/dev/null
nft -f - <<EOF
table inet nvxaisla3 {
    chain aisla3 {
        type filter hook forward priority filter; policy accept;
        # vmbr0 -> vmbr0 = atajo L3 entre vecinas del mismo nodo. En condiciones
        # normales NUNCA casa (todo va por el tunel a la base). Un contador que
        # sube = alguien enrutando a su vecina con origen ajeno.
        iifname "vmbr0" oifname "vmbr0" $VERDICT
    }
}
EOF

N=$(nft list chain inet nvxaisla3 aisla3 2>/dev/null | grep -c 'iifname "vmbr0" oifname "vmbr0"')
[ "$N" = "1" ] || { echo "nvx-aisla-l3: esperaba 1 regla, hay $N" >&2; exit 1; }
echo "nvx-aisla-l3: aislamiento L3 vmbr0<->vmbr0 puesto (modo $MODE)"

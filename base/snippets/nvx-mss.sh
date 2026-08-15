#!/usr/bin/env bash
# Clamp de MSS para el trafico de los invitados que sale por el tunel.
#
# EL PROBLEMA QUE RESUELVE (medido en produccion el 2026-08-15)
# El invitado tiene la interfaz a MTU 1500 y anuncia MSS 1460, pero su camino
# real pasa por un ip6gre de MTU 1456 (1500 - 40 de cabecera IPv6 - 4 de GRE).
# Sin clamp, los paquetes grandes se pierden EN SILENCIO: el TCP conecta, y
# luego el handshake TLS se cuelga con retransmisiones (3,2 s y 6,5 s = backoff
# 1+2 y 1+2+4) o da timeout. StrategyQuant sale solo por IPv4
# (-Djava.net.preferIPv4Stack=true) asi que se lo come entero, mientras el
# navegador del mismo servidor va por IPv6 y funciona — lo que despista mucho.
# Prueba controlada: api.strategyquant.com daba 000 a los 25 s en nodos sin
# clamp y 200 en 0,24 s en nodos con clamp, con el MISMO MTU en el invitado.
#
# ⚠️ POR QUE LOS DOS SENTIDOS SON DISTINTOS
# La opcion MSS es POR SENTIDO: la que va en el SYN le dice al SERVIDOR cuanto
# puede mandar, y la del SYN-ACK se lo dice al INVITADO. Hay que tocar las dos.
#   - saliente (oifname tun-*): la decision de ruta ya esta tomada y es la de la
#     tabla de politica, que apunta al tunel -> `rt mtu` resuelve 1456. Correcto
#     y se autoajusta si cambia el MTU del tunel.
#   - entrante (iifname tun-*): la ruta de SALIDA es vmbr0, que es 1500, asi que
#     `rt mtu` daria MSS 1460 y seria un NO-OP. Hay que fijarlo al MTU real del
#     tunel. Se recalcula en cada ejecucion, asi que sigue atado a la realidad.
#
# Idempotente: reconstruye su propia tabla, no toca el resto del cortafuegos.
set -u

TUN=$(ip -br link show type ip6gre 2>/dev/null | grep -oE '^tun-[a-z]+' | head -1)
[ -n "$TUN" ] || { echo "nvx-mss: sin tuneles, nada que hacer"; exit 0; }
MTU=$(cat "/sys/class/net/$TUN/mtu" 2>/dev/null)
[ -n "$MTU" ] && [ "$MTU" -gt 100 ] 2>/dev/null || { echo "nvx-mss: MTU de $TUN ilegible" >&2; exit 1; }

MSS4=$(( MTU - 40 ))   # 20 IP + 20 TCP
MSS6=$(( MTU - 60 ))   # 40 IPv6 + 20 TCP

nft delete table inet nvxmss 2>/dev/null
nft -f - <<EOF
table inet nvxmss {
    chain clamp {
        type filter hook forward priority mangle; policy accept;

        # Saliente por el tunel: rt mtu resuelve la ruta REAL del invitado
        # (la de la tabla de politica), no la principal.
        oifname "tun-*" tcp flags syn tcp option maxseg size set rt mtu

        # Entrante del tunel hacia el invitado: aqui rt mtu daria el MTU de
        # vmbr0 (1500) y no serviria de nada. Valor explicito, recalculado.
        iifname "tun-*" meta nfproto ipv4 tcp flags syn tcp option maxseg size set $MSS4
        iifname "tun-*" meta nfproto ipv6 tcp flags syn tcp option maxseg size set $MSS6
    }
}
EOF
echo "nvx-mss: clamp puesto sobre $TUN (mtu $MTU -> mss v4 $MSS4 / v6 $MSS6)"

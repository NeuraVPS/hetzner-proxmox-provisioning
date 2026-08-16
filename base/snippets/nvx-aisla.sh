#!/usr/bin/env bash
# Aisla a las VMs entre si dentro del puente vmbr0.
#
# QUE RESUELVE
# Las VMs de un mismo nodo cuelgan todas del mismo puente. El encaminamiento
# manda su trafico por el tunel a la base —incluso el que va de una VM a su
# vecina— pero eso es una decision de CAPA 3, y el cliente es Administrador de
# su propio Windows: puede ponerse una direccion on-link y hablar con sus
# vecinas por capa 2, saltandose el nodo y la base enteros.
#
# Hasta 2026-08-16 eso lo tapaba el firewall POR VM (el `fwbr` que crea PVE con
# `firewall=1`). Esta regla lo sustituye por algo mucho mas barato: una tabla
# estatica por nodo en vez de tres interfaces de red por VM (5628 en la flota).
#
# ⚠️ NO basta con el /32 del invitado. El /32 quita la vecindad on-link, pero es
# configuracion DENTRO de la VM y el cliente puede deshacerla en diez segundos,
# a proposito o al reparar la red. El control tiene que vivir donde el cliente
# no llega: aqui.
#
# ⚠️ `bridge link set ... isolated on` NO sirve. Se probo el 16-08-2026 y dejo a
# la VM sin salida: el puerto aislado pierde tambien el camino hacia el propio
# puente, que es por donde sale TODO su trafico legitimo.
#
# POR QUE ESTAS CUATRO REGLAS Y NO MAS
# `vmbr0` se declara con `bridge-ports none` en los 227 nodos (comprobado en
# muestra): sus unicos puertos son los de las VMs. Asi que lo UNICO que ese
# puente reenvia es trafico VM<->VM. Todo lo legitimo —salida a Internet, y el
# SMB entre las VMs de un mismo cliente, que viaja por la base— es entrega
# LOCAL al puente y lo enruta el nodo, no pasa por el hook de forward.
#
# Los nombres dependen de si la VM lleva `firewall=1` (el puerto en vmbr0 es
# `fwpr<vmid>p0`) o `firewall=0` (es `tap<vmid>i0`). Se cubren las cuatro
# combinaciones para que la flota pueda estar mezclada durante la transicion.
# El puente interno `fwbr<vmid>i0` de cada VM reenvia tap<->fwln, que no casa
# con ninguna regla: no se toca.
#
# `policy accept` a proposito: si algun nombre se me escapa, el trafico pasa en
# vez de cortarse. Una regla de aislamiento que se equivoca hacia "abierto" es
# un fallo; hacia "cerrado" es una averia de cliente.
#
# Idempotente: reconstruye su propia tabla y no toca el resto del cortafuegos.
set -u

nft delete table bridge nvxaisla 2>/dev/null
nft -f - <<'EOF'
table bridge nvxaisla {
    chain aisla {
        type filter hook forward priority filter; policy accept;

        # `counter` a proposito: en condiciones normales estas reglas NUNCA
        # deben casar, porque el /32 del invitado ya evita que lo intente. Un
        # contador que sube significa que alguien esta hablando con sus vecinas
        # por capa 2 — o una VM mal configurada, o alguien probando. Convierte
        # la regla en detector ademas de control, y cuesta nada.
        iifname "tap*"  oifname "tap*"  counter drop
        iifname "tap*"  oifname "fwpr*" counter drop
        iifname "fwpr*" oifname "tap*"  counter drop
        iifname "fwpr*" oifname "fwpr*" counter drop
    }
}
EOF

N=$(nft list chain bridge nvxaisla aisla 2>/dev/null | grep -c drop)
[ "$N" = "4" ] || { echo "nvx-aisla: esperaba 4 reglas, hay $N" >&2; exit 1; }
echo "nvx-aisla: aislamiento VM<->VM puesto en el puente ($N reglas)"

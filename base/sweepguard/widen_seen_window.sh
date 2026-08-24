#!/usr/bin/env bash
# Ensancha la VENTANA de observación del detector de barridos: bf_seen 1h -> 6h.
#
# POR QUÉ. El detector 1 cuenta MÁQUINAS distintas por IP de origen y bloquea a
# partir de minPorts=20. Ese recuento vive en `bf_seen`, un set con timeout, así
# que la pregunta real no era "¿cuántas máquinas ha tocado esta IP?" sino
# "¿cuántas en la ÚLTIMA HORA?". Un barredor lento se queda siempre por debajo
# del umbral dentro de la ventana mientras recorre la flota entera en días:
# medido el 2026-08-20, `174.138.187.126` barría 14 VMs de clientes con 462
# logins fallidos en 6 h contra una sola, y NO estaba bloqueado (14 < 20).
#
# POR QUÉ LA VENTANA Y NO EL UMBRAL. Bajar minPorts acerca el listón a los 7
# servidores del cliente legítimo más grande, y el detector 3 ya bloqueó a un
# cliente real una vez. El recuento es de máquinas DISTINTAS, acotado por
# cuántas tiene esa fuente, así que alargar la ventana NO infla a un cliente de
# 7 servidores — sí acumula a quien va recorriendo la flota. Medido en b0 con
# la ventana de 1 h: de 182 fuentes IPv4, 157 tocan UNA máquina, 14 dos, 6 tres,
# y una sola pasa de 5 — `85.239.151.10` con 16 en una hora, justo debajo del
# umbral. Con 6 h esa cruza y las legítimas siguen donde estaban.
#
# RIESGO ACEPTADO: una IP compartida por MUCHOS clientes distintos (oficina,
# CGNAT, VPN, revendedor) acumula más en 6 h que en 1 h. Los bloqueos caducan
# solos en 24 h; quitar uno a mano es `nft delete element ip rdpguard bf_auto`.
#
# El timeout de un set NO se puede cambiar en caliente: hay que borrar la regla
# que lo referencia, borrar el set y recrearlo. Todo en UNA transacción `nft -f`
# para que la cadena nunca quede sin la regla de registro.
set -euo pipefail

NEW_TIMEOUT="${NEW_TIMEOUT:-6h}"
BACKUP="/root/nft-pre-seenwindow-$(date -u +%Y%m%dT%H%M%SZ).nft"
nft list ruleset > "$BACKUP"
echo "backup: $BACKUP"

# OJO: `nft list ruleset` NO lleva `flush ruleset`, así que `nft -f backup`
# AÑADE sobre lo que ya hay en vez de reemplazarlo — duplica la cadena entera.
# Pasó en b0 el 2026-08-24 durante la primera pasada de este script: los
# rate-limiters quedaron evaluándose dos veces hasta limpiarlos a mano. El
# backup se guarda como último recurso MANUAL; el deshacer automático es
# dirigido, y como cada familia se aplica en UNA transacción `nft -f`, un
# fallo de la transacción no deja nada a medias.
undo_family() {
  local fam=$1 atype=$2 saddr=$3 ports=$4 old_to=$5
  echo "  $fam: deshaciendo -> timeout $old_to"
  local nh
  nh=$(nft -a list chain "$fam" rdpguard pre | awk '/add @bf(_src)? \{/{print $NF; exit}')
  local rh
  rh=$(nft -a list chain "$fam" rdpguard pre | awk '/add @bf_seen /{print $NF; exit}')
  local U; U=$(mktemp)
  { [ -n "$rh" ] && echo "delete rule $fam rdpguard pre handle $rh"
    echo "delete set $fam rdpguard bf_seen"
    echo "add set $fam rdpguard bf_seen { type $atype . inet_service; flags dynamic,timeout; timeout $old_to; size 65536; }"
    echo "insert rule $fam rdpguard pre handle $nh tcp dport $ports tcp flags and (syn|ack) == syn add @bf_seen { $saddr . tcp dport } comment \"record (source,port) for the sweep detector\""
  } > "$U"
  nft -f "$U" && echo "  $fam: deshecho" || echo "  $fam: !! DESHACER FALLÓ — revisa a mano, backup en $BACKUP"
  rm -f "$U"
}

for FAM in ip ip6; do
  atype=ipv4_addr; saddr="ip saddr"
  [ "$FAM" = ip6 ] && { atype=ipv6_addr; saddr="ip6 saddr"; }

  if ! nft list set "$FAM" rdpguard bf_seen >/dev/null 2>&1; then
    echo "  $FAM: no hay bf_seen — salto"; continue
  fi
  cur=$(nft list set "$FAM" rdpguard bf_seen | awk '$1=="timeout"{print $2; exit}')
  if [ "$cur" = "$NEW_TIMEOUT" ]; then echo "  $FAM: ya está en $NEW_TIMEOUT"; continue; fi

  # handle de la regla que registra en bf_seen, y su rango de puertos
  line=$(nft -a list chain "$FAM" rdpguard pre | grep 'add @bf_seen ')
  h=$(sed -E 's/.*# handle ([0-9]+).*/\1/' <<<"$line")
  ports=$(grep -oE 'tcp dport [0-9]+-[0-9]+' <<<"$line" | head -1 | awk '{print $3}')
  # handle de la regla de rate-limit que va JUSTO DESPUÉS (ancla de reinserción)
  nexth=$(nft -a list chain "$FAM" rdpguard pre | awk '/add @bf(_src)? \{/{print $NF; exit}')
  [ -n "$h" ] && [ -n "$ports" ] && [ -n "$nexth" ] || { echo "  $FAM: no encuentro handles — no toco nada"; exit 1; }
  echo "  $FAM: regla handle=$h puertos=$ports ancla=$nexth timeout $cur -> $NEW_TIMEOUT"

  TX=$(mktemp)
  cat > "$TX" <<EOF
delete rule $FAM rdpguard pre handle $h
delete set $FAM rdpguard bf_seen
add set $FAM rdpguard bf_seen { type $atype . inet_service; flags dynamic,timeout; timeout $NEW_TIMEOUT; size 65536; }
insert rule $FAM rdpguard pre handle $nexth tcp dport $ports tcp flags and (syn|ack) == syn add @bf_seen { $saddr . tcp dport } comment "record (source,port) for the sweep detector"
EOF
  # si la transacción no valida o falla, NADA se ha aplicado: no hay que deshacer
  nft -c -f "$TX" || { echo "  $FAM: la transacción NO valida"; cat "$TX"; rm -f "$TX"; exit 1; }
  nft -f "$TX" || { echo "  $FAM: la transacción falló (sin efecto)"; rm -f "$TX"; exit 1; }
  rm -f "$TX"

  got=$(nft list set "$FAM" rdpguard bf_seen | awk '$1=="timeout"{print $2; exit}')
  n_rules=$(nft list chain "$FAM" rdpguard pre | grep -c 'add @bf_seen ')
  if [ "$n_rules" != "1" ] || [ "$got" != "$NEW_TIMEOUT" ]; then
    echo "  $FAM: verificación FALLA (reglas=$n_rules timeout=$got)"
    undo_family "$FAM" "$atype" "$saddr" "$ports" "$cur"; exit 1
  fi
  echo "  $FAM: OK ($got, regla reinsertada)"
done

# ---- persistir en /etc/nftables.conf (solo dentro de los bloques bf_seen) ----
python3 - "$NEW_TIMEOUT" <<'PY'
import re, sys, shutil
new=sys.argv[1]; p="/etc/nftables.conf"
s=open(p).read(); shutil.copy(p, p+".pre-seenwindow")
def fix(m):
    return re.sub(r'timeout \S+;?', f'timeout {new}', m.group(0), count=1)
out, n = re.subn(r'set bf_seen \{[^}]*\}', fix, s)
open(p,'w').write(out)
print(f"  /etc/nftables.conf: {n} bloque(s) bf_seen actualizados a {new}")
PY
nft -c -f /etc/nftables.conf && echo "  /etc/nftables.conf VALIDA" || { echo "  !! nftables.conf NO valida — restaurando"; cp /etc/nftables.conf.pre-seenwindow /etc/nftables.conf; exit 1; }

echo "=== orden final de la cadena ==="
for FAM in ip ip6; do
  echo "--- $FAM ---"
  nft list chain "$FAM" rdpguard pre 2>/dev/null | grep -E 'bf_allow|bf_static|bf_auto|bf_seen|bf_src|@bf ' | sed 's/^/    /'
done

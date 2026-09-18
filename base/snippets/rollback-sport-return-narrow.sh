#!/usr/bin/env bash
# Deshace deploy-sport-return-narrow.sh: vuelve a la regla ancha de sport.
# Atómico, sin flush ni recarga.
set -euo pipefail
CONF=${CONF:-/etc/nftables.conf}
T='iifname "tun-*" oifname "tun-*"'

echo "=== rollback: regla de vuelta SMB ancha en $(hostname) ==="

CH=$(nft -a list chain inet filter forward)
if printf '%s' "$CH" | grep -q 'tcp sport { 135, 139, 445 } tcp flags'; then
  DPORT_H=$(printf '%s' "$CH" | awk '/oifname "tun-\*" meta l4proto \{ tcp, udp \} th dport \{ 135, 137, 138, 139, 445 \} accept/{print $NF; exit}')
  H_TCP=$(printf '%s' "$CH" | awk '/tcp sport \{ 135, 139, 445 \} tcp flags/{print $NF; exit}')
  H_U137=$(printf '%s' "$CH" | awk '/udp sport 137 udp dport 137 accept/{print $NF; exit}')
  H_U138=$(printf '%s' "$CH" | awk '/udp sport 138 udp dport 138 accept/{print $NF; exit}')
  TX=$(mktemp --suffix=.nft /root/sport-narrow-rb.XXXXXX)
  {
    echo "add rule inet filter forward position $DPORT_H $T meta l4proto { tcp, udp } th sport { 135, 137, 138, 139, 445 } accept"
    for h in "$H_TCP" "$H_U137" "$H_U138"; do [ -n "$h" ] && echo "delete rule inet filter forward handle $h"; done
  } > "$TX"
  nft -c -f "$TX"; nft -f "$TX"
  echo "  [vivo] regla ancha restaurada ($TX)"
else
  echo "  [vivo] nada que deshacer"
fi

NEW='meta l4proto tcp tcp sport { 135, 139, 445 } tcp flags & (syn|ack) != syn accept'
if grep -qF "$NEW" "$CONF"; then
  BK="$CONF.bak.sportnarrow-rb.$(date +%Y%m%d-%H%M%S)"; cp -a "$CONF" "$BK"
  python3 - "$CONF" <<'PY'
import sys
conf = sys.argv[1]; s = open(conf).read()
T = '        iifname "tun-*" oifname "tun-*" '
new = (T + 'meta l4proto tcp tcp sport { 135, 139, 445 } tcp flags & (syn|ack) != syn accept comment "respuesta SMB asimetrica (no puede iniciar)"\n'
       + T + 'meta l4proto udp udp sport 137 udp dport 137 accept comment "NetBIOS name service"\n'
       + T + 'meta l4proto udp udp sport 138 udp dport 138 accept comment "NetBIOS datagram"\n')
old = '        iifname "tun-*" oifname "tun-*" meta l4proto { tcp, udp } th sport { 135, 137, 138, 139, 445 } accept\n'
assert s.count(new) == 1, "no encuentro el bloque estrecho"
open(conf, "w").write(s.replace(new, old))
print("  [conf] regla ancha restaurada")
PY
  if nft -c -f "$CONF"; then echo "  [conf] sintaxis OK (no cargada)"
  else echo "  !! conf no valida — restauro"; cp -a "$BK" "$CONF"; exit 1; fi
else
  echo "  [conf] nada que revertir"
fi
nft list chain inet filter forward | grep -E "sport" | sed 's/^/  /'
echo "SPORT_NARROW_ROLLBACK_OK $(hostname)"

#!/usr/bin/env bash
# A) Estrechar la regla de "vuelta" SMB entre túneles para que no pueda INICIAR
# conexiones. Hoy la base acepta tun-*->tun-* con `th sport {135,137,138,139,445}`
# (para las respuestas SMB por camino asimétrico entre regiones, que conntrack
# no casa). Como solo mira el puerto de ORIGEN, un invitado que ponga su puerto
# local en 137/138 abre CUALQUIER puerto (RDP 3389, SSH 22...) de otra VM.
#
# La respuesta legítima nunca es un SYN a secas y el NetBIOS UDP va 137->137 y
# 138->138. La regla nueva acepta solo eso:
#   TCP  sport {135,139,445} y flags != SYN-a-secas   (SYN-ACK y demás respuestas)
#   UDP  137->137 y 138->138                          (NetBIOS name/datagram)
# La ENTRADA de SMB nuevo (dport {...}) NO se toca: la sigue aceptando la regla
# de dport, así que "Mis Servidores" (UNC a la IPv6:445) y el SMB entre VMs del
# mismo cliente siguen igual.
#
# Atómico (una transacción), sin flush ni recarga. Idempotente. Persiste en
# /etc/nftables.conf. Rollback: rollback-sport-return-narrow.sh
set -euo pipefail
CONF=${CONF:-/etc/nftables.conf}
T='iifname "tun-*" oifname "tun-*"'

echo "=== estrechar la regla de vuelta SMB en $(hostname) ==="

CH=$(nft -a list chain inet filter forward)
if printf '%s' "$CH" | grep -q 'tcp sport { 135, 139, 445 } tcp flags'; then
  echo "  [vivo] ya estaba"
else
  SPORT_H=$(printf '%s' "$CH" | awk '/oifname "tun-\*" meta l4proto \{ tcp, udp \} th sport \{ 135, 137, 138, 139, 445 \} accept/{print $NF; exit}')
  DPORT_H=$(printf '%s' "$CH" | awk '/oifname "tun-\*" meta l4proto \{ tcp, udp \} th dport \{ 135, 137, 138, 139, 445 \} accept/{print $NF; exit}')
  [ -n "$SPORT_H" ] || { echo "  !! no encuentro la regla sport actual — aborto"; exit 1; }
  [ -n "$DPORT_H" ] || { echo "  !! no encuentro la regla dport — aborto"; exit 1; }
  TX=$(mktemp --suffix=.nft /root/sport-narrow.XXXXXX)
  {
    echo "add rule inet filter forward position $DPORT_H $T meta l4proto tcp tcp sport { 135, 139, 445 } tcp flags & (syn | ack) != syn accept comment \"respuesta SMB asimetrica (no puede iniciar)\""
    echo "add rule inet filter forward position $DPORT_H $T meta l4proto udp udp sport 137 udp dport 137 accept comment \"NetBIOS name service\""
    echo "add rule inet filter forward position $DPORT_H $T meta l4proto udp udp sport 138 udp dport 138 accept comment \"NetBIOS datagram\""
    echo "delete rule inet filter forward handle $SPORT_H"
  } > "$TX"
  nft -c -f "$TX"
  nft -f "$TX"
  echo "  [vivo] regla de vuelta estrechada ($TX)"
fi

# --- persistencia ------------------------------------------------------------
OLD='        iifname "tun-*" oifname "tun-*" meta l4proto { tcp, udp } th sport { 135, 137, 138, 139, 445 } accept'
if ! grep -qF "$OLD" "$CONF"; then
  echo "  [conf] ya estaba"
else
  BK="$CONF.bak.sportnarrow.$(date +%Y%m%d-%H%M%S)"; cp -a "$CONF" "$BK"; echo "  [conf] copia en $BK"
  python3 - "$CONF" <<'PY'
import sys
conf = sys.argv[1]; s = open(conf).read()
old = '        iifname "tun-*" oifname "tun-*" meta l4proto { tcp, udp } th sport { 135, 137, 138, 139, 445 } accept\n'
T = '        iifname "tun-*" oifname "tun-*" '
new = (T + 'meta l4proto tcp tcp sport { 135, 139, 445 } tcp flags & (syn|ack) != syn accept comment "respuesta SMB asimetrica (no puede iniciar)"\n'
       + T + 'meta l4proto udp udp sport 137 udp dport 137 accept comment "NetBIOS name service"\n'
       + T + 'meta l4proto udp udp sport 138 udp dport 138 accept comment "NetBIOS datagram"\n')
assert s.count(old) == 1, "ancla sport no única/ausente"
open(conf, "w").write(s.replace(old, new))
print("  [conf] regla de vuelta estrechada")
PY
  if nft -c -f "$CONF"; then echo "  [conf] sintaxis OK (no cargada)"
  else echo "  !! conf no valida — restauro"; cp -a "$BK" "$CONF"; exit 1; fi
fi

nft list chain inet filter forward | grep -E "sport|dport" | sed 's/^/  /'
echo "SPORT_NARROW_OK $(hostname)"

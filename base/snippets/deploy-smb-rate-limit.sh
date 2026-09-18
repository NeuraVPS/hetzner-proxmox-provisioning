#!/usr/bin/env bash
# B1) Limite de ritmo a las conexiones SMB NUEVAS entre VMs (tun-*->tun-*).
#
# El SMB VM<->VM entre inquilinos distintos esta abierto sin ningun freno
# (medido: 60 conexiones en 157 ms). Esto pone un techo por ORIGEN a las
# conexiones NUEVAS (SYN a secas) a los puertos 139/445, muy por encima del uso
# real y por debajo de un flood.
#
# POR QUE SOBRE EL SYN Y NO SOBRE `ct state new`
# Medido 18/09/2026: contando `ct state new` de SMB salian miles/min, pero el
# camino VM<->VM es ASIMETRICO (la vuelta va por la otra base) y conntrack no lo
# confirma nunca, asi que TODOS los paquetes de una transferencia en curso se
# cuentan como "new". El ritmo de CONEXIONES nuevas de verdad (SYN a secas) era
# ~0/min en las dos bases (m_syn_over120 = 0 en toda la ventana). Un burst de 40
# SMB desde una VM subio el contador de SYN en 40, un flood lo dispararia. Sobre
# el SYN, un mismo par que copia ficheros sin parar NO se ve afectado (sus
# paquetes de datos no son SYN); solo se frena quien ABRE conexiones a chorro.
#
# Umbral: RATE/min por origen, burst=RATE. Defecto 300 (>>0/min real, <<flood).
# Bajo el umbral: el SYN cae a la regla de dport y se acepta como siempre.
# Sobre el umbral: se descarta y se cuenta en `smb_rl_drops`, que sweepguard.py
# reporta en su journal en cada pasada. SIN `log` por paquete: en la prueba del
# 18/09 una rafaga de 400 dejo 280 lineas en el log del kernel; en un ataque lo
# inundaria. Y un `limit` en la misma regla cambiaria QUE se descarta.
#
# Atomico, sin flush ni recarga. Idempotente. Persiste en /etc/nftables.conf.
# Rollback: rollback-smb-rate-limit.sh
set -euo pipefail
CONF=${CONF:-/etc/nftables.conf}
RATE=${RATE:-300}
T='iifname "tun-*" oifname "tun-*"'

echo "=== limite de ritmo SMB VM<->VM ($RATE/min) en $(hostname) ==="

CH=$(nft -a list chain inet filter forward)
if printf '%s' "$CH" | grep -q '@smb_rl6'; then
  echo "  [vivo] ya estaba"
else
  DPORT_H=$(printf '%s' "$CH" | awk '/oifname "tun-\*" meta l4proto \{ tcp, udp \} th dport \{ 135, 137, 138, 139, 445 \} accept/{print $NF; exit}')
  [ -n "$DPORT_H" ] || { echo "  !! no encuentro la regla dport — aborto"; exit 1; }
  TX=$(mktemp --suffix=.nft /root/smb-rl.XXXXXX)
  {
    echo "add counter inet filter smb_rl_drops"
    echo "add set inet filter smb_rl4 { type ipv4_addr ; flags dynamic,timeout ; timeout 5m ; size 65536 ; }"
    echo "add set inet filter smb_rl6 { type ipv6_addr ; flags dynamic,timeout ; timeout 5m ; size 65536 ; }"
    echo "insert rule inet filter forward position $DPORT_H $T meta nfproto ipv6 tcp dport { 139, 445 } tcp flags & (syn | ack) == syn add @smb_rl6 { ip6 saddr limit rate over $RATE/minute burst $RATE packets } counter name smb_rl_drops drop"
    echo "insert rule inet filter forward position $DPORT_H $T meta nfproto ipv4 tcp dport { 139, 445 } tcp flags & (syn | ack) == syn add @smb_rl4 { ip saddr limit rate over $RATE/minute burst $RATE packets } counter name smb_rl_drops drop"
  } > "$TX"
  nft -c -f "$TX"
  nft -f "$TX"
  echo "  [vivo] limite SMB puesto ($TX)"
fi

# --- persistencia ------------------------------------------------------------
if grep -q 'smb_rl6' "$CONF"; then
  echo "  [conf] ya estaba"
else
  BK="$CONF.bak.smbrl.$(date +%Y%m%d-%H%M%S)"; cp -a "$CONF" "$BK"; echo "  [conf] copia en $BK"
  python3 - "$CONF" "$RATE" <<'PY'
import sys
conf, rate = sys.argv[1], sys.argv[2]
s = open(conf).read()
# sets + counter dentro de table inet filter (antes de flowtable ft, que ya existe)
anchor_set = "    flowtable ft {"
sets = ("    counter smb_rl_drops {\n    }\n\n"
        "    set smb_rl4 {\n        type ipv4_addr\n        flags dynamic,timeout\n        timeout 5m\n        size 65536\n    }\n\n"
        "    set smb_rl6 {\n        type ipv6_addr\n        flags dynamic,timeout\n        timeout 5m\n        size 65536\n    }\n\n")
assert s.count(anchor_set) == 1, "no encuentro la flowtable ft en table inet filter"
s = s.replace(anchor_set, sets + anchor_set, 1)
# reglas: justo ANTES de la regla dport
T = '        iifname "tun-*" oifname "tun-*" '
dport = T + 'meta l4proto { tcp, udp } th dport { 135, 137, 138, 139, 445 } accept\n'
rules = (T + f'meta nfproto ipv6 tcp dport {{ 139, 445 }} tcp flags & (syn|ack) == syn add @smb_rl6 {{ ip6 saddr limit rate over {rate}/minute burst {rate} packets }} counter name smb_rl_drops drop\n'
         + T + f'meta nfproto ipv4 tcp dport {{ 139, 445 }} tcp flags & (syn|ack) == syn add @smb_rl4 {{ ip saddr limit rate over {rate}/minute burst {rate} packets }} counter name smb_rl_drops drop\n')
assert s.count(dport) == 1, "no encuentro la regla dport en la conf"
s = s.replace(dport, rules + dport, 1)
open(conf, "w").write(s)
print("  [conf] sets, contador y 2 reglas escritos")
PY
  if nft -c -f "$CONF"; then echo "  [conf] sintaxis OK (no cargada)"
  else echo "  !! conf no valida — restauro"; cp -a "$BK" "$CONF"; exit 1; fi
fi

nft list chain inet filter forward | grep -E "smb_rl|dport \{ 135" | sed 's/^/  /'
echo "SMB_RL_OK $(hostname)"

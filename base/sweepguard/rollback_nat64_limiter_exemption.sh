#!/usr/bin/env bash
# Deshace deploy_nat64_limiter_exemption.sh. Solo toca la regla PROPIA (texto
# exacto: interfaz, prefijo, veredicto y comentario) y aborta si hay otra
# parecida o duplicada. Primero prepara y valida la conf nueva (fichero hermano
# + nft -c), después borra la regla viva por handle (una transacción) y al
# final publica la conf con mv.
set -euo pipefail
CONF=${CONF:-/etc/nftables.conf}
JOOL_IF=${JOOL_IF:-veth-host}
COM="IPv4 traducido (NAT64): ya juzgado en ip rdpguard con la IP real"
RULE="iifname \"$JOOL_IF\" ip6 saddr 64:ff9b:1::/96 accept"
OWN="$RULE comment \"$COM\""
HERE=$(cd "$(dirname "$0")" && pwd)
census() { python3 "$HERE/nat64_rule_census.py" "$1" "$2" "$OWN"; }
LIVE=$(mktemp); nft -a list chain ip6 rdpguard pre > "$LIVE"
read -r N_OWN N_ANY H _ < <(census live "$LIVE"); rm -f "$LIVE"
N_ANY=$(nft list table ip6 rdpguard | grep -v '^\s*#' | grep -c "64:ff9b:1::/96" || true)   # toda la tabla
[ "$N_ANY" = "$N_OWN" ] && [ "$N_OWN" -le 1 ] || { echo "  !! reglas 64:ff9b:1::/96 ajenas o duplicadas en vivo ($N_ANY/$N_OWN) — no toco nada"; exit 1; }
read -r C_OWN C_ANY _ _ < <(census disk "$CONF")
[ "$C_ANY" = "$C_OWN" ] && [ "$C_OWN" -le 1 ] || { echo "  !! reglas 64:ff9b:1::/96 ajenas o duplicadas en $CONF ($C_ANY/$C_OWN) — no toco nada"; exit 1; }
NEW=""
if [ "$C_OWN" = 1 ]; then
  NEW="$CONF.nat64lim-rollback.new"; cp -a "$CONF" "$NEW"
  python3 - "$NEW" "$OWN" <<'PY'
import re, sys
p, own = sys.argv[1:]
s = open(p).read()
i = s.index("table ip6 rdpguard {")
m = re.compile(r"\n(\s*)chain pre \{\n(.*?)\n\1\}", re.S).search(s, i)
body = m.group(2).split("\n")
keep = [l for l in body if l.strip() != own]
assert len(body) - len(keep) == 1, "la regla propia no aparece exactamente una vez en ip6 rdpguard pre"
s = s[:m.start(2)] + "\n".join(keep) + s[m.end(2):]
open(p, "w").write(s)
PY
  nft -c -f "$NEW" || { rm -f "$NEW"; echo "  !! conf nueva no valida — no toco nada"; exit 1; }
fi
if [ "$N_OWN" = 1 ]; then
  [ "$H" != "-" ] || { echo "  !! sin handle de la regla propia — aborto"; exit 1; }
  nft delete rule ip6 rdpguard pre handle "$H"; echo "  [vivo] regla $H borrada"
else
  echo "  [vivo] nada que borrar"
fi
if [ -n "$NEW" ]; then
  cp -a "$CONF" "$CONF.bak.nat64lim-rollback.$(date +%Y%m%d-%H%M%S)"; mv -f "$NEW" "$CONF"; echo "  [conf] quitada"
else
  echo "  [conf] nada que quitar"
fi
echo "ROLLBACK_NAT64LIM_OK $(hostname)"

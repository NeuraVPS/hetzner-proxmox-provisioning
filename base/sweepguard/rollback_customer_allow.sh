#!/usr/bin/env bash
# Deshace deploy_customer_allow.sh. DIRIGIDO: borra exactamente las dos reglas
# (por handle, localizadas por @cust_allow) y los dos sets, en UNA transacción;
# quita lo mismo de /etc/nftables.conf; y vuelve a poner sweepguard.py y
# neuravps-unblock-ip anteriores si hay copia.
#
# ⚠️ NUNCA se restaura un `nft list ruleset` ni se recarga /etc/nftables.conf
#    (el primero AÑADE en vez de reemplazar; el segundo empieza por
#    `flush ruleset` y tira los túneles).
#
# Al borrar el set, las IPs que tuvieran excepción vuelven a quedar bloqueadas
# del todo si siguen en bf_auto (lo de antes). El botón «Desbloquear mi IP»
# sigue funcionando: no depende de esto.
#
# Antes de deshacer en una base, desactiva el desbloqueo automático en el panel
# (functions/ip_unblock.py AUTO_ENABLED = False) o allow-vm dará «error» ahí.
set -euo pipefail
CONF=${CONF:-/etc/nftables.conf}
SKIP_INSTALL=${SKIP_INSTALL:-0}

echo "=== rollback de cust_allow en $(hostname) ==="

TX=$(mktemp --suffix=.nft /root/sg-cust-allow-rollback.XXXXXX 2>/dev/null || mktemp --suffix=.nft)
A4=$(nft -a list chain ip rdpguard pre)
A6=$(nft -a list chain ip6 rdpguard pre)
H4=$(awk '/@cust_allow/{print $NF}' <<<"$A4")
H6=$(awk '/@cust_allow/{print $NF}' <<<"$A6")
: > "$TX"
for h in $H4; do echo "delete rule ip rdpguard pre handle $h" >> "$TX"; done
for h in $H6; do echo "delete rule ip6 rdpguard pre handle $h" >> "$TX"; done
nft list set ip rdpguard cust_allow >/dev/null 2>&1 && echo "delete set ip rdpguard cust_allow" >> "$TX"
nft list set ip6 rdpguard cust_allow >/dev/null 2>&1 && echo "delete set ip6 rdpguard cust_allow" >> "$TX"
if [ -s "$TX" ]; then
  sed 's/^/  tx: /' "$TX"
  nft -c -f "$TX"
  nft -f "$TX"
  echo "  [vivo] deshecho en una transacción"
else
  echo "  [vivo] nada que deshacer"
fi

if grep -q "cust_allow" "$CONF"; then
  BK="$CONF.bak.custallow-rollback.$(date +%Y%m%d-%H%M%S)"
  cp -a "$CONF" "$BK"
  python3 - "$CONF" <<'PY'
import re, sys
conf = sys.argv[1]
s = open(conf).read()
s, n1 = re.subn(r"    # cust_allow: [^\n]*\n    #[^\n]*\n    set cust_allow \{\n(?:        [^\n]*\n)*?    \}\n", "", s)
s, n2 = re.subn(r" +meta l4proto \{ tcp, udp \} ip6? saddr \. th dport @cust_allow accept comment \"[^\"]*\"\n", "", s)
assert "cust_allow" not in s, "quedan restos de cust_allow"
open(conf, "w").write(s)
print(f"  [conf] quitados {n1} sets y {n2} reglas")
PY
  if nft -c -f "$CONF"; then echo "  [conf] sintaxis OK (no cargada)"
  else echo "  !! conf no valida — restauro"; cp -a "$BK" "$CONF"; exit 1; fi
else
  echo "  [conf] nada que quitar"
fi

if [ "$SKIP_INSTALL" != 1 ]; then
  for dst in /usr/local/sbin/sweepguard.py /usr/local/sbin/neuravps-unblock-ip; do
    # la MÁS ANTIGUA: la de antes de este cambio (sello del NOMBRE)
    PREV=$(ls -1 "$dst".bak.custallow.* 2>/dev/null | sort | head -1 || true)
    if [ -n "$PREV" ]; then
      install -m 0755 -o root -g root "$PREV" "$dst"
      echo "  [py] restaurado $PREV"
    else
      echo "  [py] sin copia previa de $dst (el nuevo sin el set se comporta como el viejo)"
    fi
  done
fi

nft list chain ip rdpguard pre | sed 's/^/  /'
nft list chain ip6 rdpguard pre | sed 's/^/  /'
echo "ROLLBACK_OK $(hostname)"

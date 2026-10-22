#!/usr/bin/env bash
# Deshace deploy_customer_allow.sh. DIRIGIDO: borra exactamente las dos reglas
# (por handle, localizadas por @cust_allow) y los dos sets, en UNA transacción;
# quita lo mismo de /etc/nftables.conf; vuelve a poner sweepguard.py y
# neuravps-unblock-ip que guardó ESE despliegue (rutas en $STATE) y borra los
# /root/sg-cust-allow*.nft que hayan quedado.
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
STATE=${STATE:-/var/lib/neuravps-sweepguard/cust_allow.deploy}
SBIN=${SBIN:-/usr/local/sbin}

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
rm -f "$TX"

if grep -q "cust_allow" "$CONF"; then
  BK="$CONF.bak.custallow-rollback.$(date +%Y%m%d-%H%M%S)"
  cp -a "$CONF" "$BK"
  python3 - "$CONF" <<'PY'
import re, sys
conf = sys.argv[1]
s = open(conf).read()
s, n1 = re.subn(r"    # cust_allow: [^\n]*\n    #[^\n]*\n    set cust_allow \{\n(?:        [^\n]*\n)*?    \}\n", "", s)
s, n2 = re.subn(r" +ip6? saddr @bf_auto meta l4proto \{ tcp, udp \} ip6? saddr \. th dport "
                r"@cust_allow accept comment \"[^\"]*\"\n", "", s)
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
  for dst in "$SBIN/sweepguard.py" "$SBIN/neuravps-unblock-ip"; do
    # la que guardó el despliegue (no la más antigua del directorio)
    PREV=$(awk -F= -v k="$dst" '$1 == k {print $2}' "$STATE" 2>/dev/null | tail -1 || true)
    if [ -n "$PREV" ] && [ -e "$PREV" ]; then
      install -m 0755 -o root -g root "$PREV" "$dst"
      echo "  [py] restaurado $PREV"
    else
      echo "  !! sin copia registrada de $dst en $STATE: se queda el actual (sin el set" \
           "se comporta como el anterior); revisa a mano los $dst.bak.custallow.*"
    fi
  done
  if [ -e "$STATE" ]; then mv "$STATE" "$STATE.rolledback.$(date +%Y%m%d-%H%M%S)"; fi
fi
rm -f /root/sg-cust-allow*.nft 2>/dev/null || true

nft list chain ip rdpguard pre | sed 's/^/  /'
nft list chain ip6 rdpguard pre | sed 's/^/  /'
echo "ROLLBACK_OK $(hostname)"

#!/usr/bin/env bash
# Deshace deploy_nat64_limiter_exemption.sh: borra la regla por handle (una
# transacción) y la quita de /etc/nftables.conf (fichero hermano + nft -c + mv).
set -euo pipefail
CONF=${CONF:-/etc/nftables.conf}
A=$(nft -a list chain ip6 rdpguard pre)
H=$(awk '/64:ff9b:1::\/96 accept/{print $NF}' <<<"$A")
for h in $H; do nft delete rule ip6 rdpguard pre handle "$h"; echo "  [vivo] regla $h borrada"; done
if grep -q "64:ff9b:1::/96 accept" "$CONF"; then
  NEW="$CONF.nat64lim-rollback.new"; cp -a "$CONF" "$CONF.bak.nat64lim-rollback.$(date +%Y%m%d-%H%M%S)"; cp -a "$CONF" "$NEW"
  python3 - "$NEW" <<'PY'
import re, sys
p = sys.argv[1]; s = open(p).read()
s, n = re.subn(r" +iifname \"[^\"]+\" ip6 saddr 64:ff9b:1::/96 accept comment \"[^\"]*\"\n", "", s)
assert n == 1 and "64:ff9b:1::/96 accept" not in s, n
open(p, "w").write(s)
PY
  if nft -c -f "$NEW"; then mv -f "$NEW" "$CONF"; echo "  [conf] quitada"; else rm -f "$NEW"; echo "  !! no valida"; exit 1; fi
fi
echo "ROLLBACK_NAT64LIM_OK $(hostname)"

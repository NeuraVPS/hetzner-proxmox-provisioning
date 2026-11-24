#!/usr/bin/env bash
# Deshace deploy_customer_closed.sh: borra las reglas (por handle) y los sets del
# modo cerrado en UNA transacción, los quita de /etc/nftables.conf (fichero
# hermano + nft -c + mv) y reinstala el neuravps-unblock-ip que guardó ese
# despliegue. Las VMs cerradas quedan ABIERTAS (reglas normales): su lista de
# confianza sigue vigente. Antes, apaga el modo cerrado en NeuraVPS
# (TRUSTED_IPS_CLOSED_ENABLED=0) o trust-set ... closed dará error aquí.
set -euo pipefail
CONF=${CONF:-/etc/nftables.conf}; SKIP_INSTALL=${SKIP_INSTALL:-0}
STATE=${STATE:-/var/lib/neuravps-sweepguard/cust_closed.deploy}; SBIN=${SBIN:-/usr/local/sbin}
TX=$(mktemp --suffix=.nft); : > "$TX"
for fam in ip ip6; do
  A=$(nft -a list chain $fam rdpguard pre)
  for h in $(awk '/@cust_closed|@cust_trust_pub6/{print $NF}' <<<"$A"); do echo "delete rule $fam rdpguard pre handle $h" >> "$TX"; done
done
for s in "ip cust_closed" "ip6 cust_closed" "ip6 cust_closed_pub6" "ip6 cust_trust_pub6"; do
  set -- $s; nft list set $1 rdpguard $2 >/dev/null 2>&1 && echo "delete set $1 rdpguard $2" >> "$TX"
done
if [ -s "$TX" ]; then nft -c -f "$TX"; nft -f "$TX"; echo "  [vivo] deshecho en una transacción"; else echo "  [vivo] nada"; fi
rm -f "$TX"
if grep -q "cust_closed" "$CONF"; then
  NEW="$CONF.custclosed-rollback.new"; cp -a "$CONF" "$CONF.bak.custclosed-rollback.$(date +%Y%m%d-%H%M%S)"; cp -a "$CONF" "$NEW"
  python3 - "$NEW" <<'PY'
import re, sys
p = sys.argv[1]; s = open(p).read()
s = re.sub(r"    # modo cerrado por VPS[^\n]*\n", "", s)
s = re.sub(r"    set (cust_closed|cust_closed_pub6|cust_trust_pub6) \{\n(?:        [^\n]*\n)*?    \}\n", "", s)
s = re.sub(r" +[^\n]*@(cust_closed|cust_closed_pub6|cust_trust_pub6) (drop|accept) comment \"[^\"]*\"\n", "", s)
assert "cust_closed" not in s and "cust_trust_pub6" not in s, "quedan restos"
open(p, "w").write(s)
PY
  if nft -c -f "$NEW"; then mv -f "$NEW" "$CONF"; echo "  [conf] quitado"; else rm -f "$NEW"; echo "  !! no valida"; exit 1; fi
fi
if [ "$SKIP_INSTALL" != 1 ]; then
  PREV=$(awk -F= -v k="$SBIN/neuravps-unblock-ip" '$1 == k {print $2}' "$STATE" 2>/dev/null | tail -1 || true)
  if [ -n "$PREV" ] && [ -e "$PREV" ]; then install -m 0755 "$PREV" "$SBIN/neuravps-unblock-ip"; echo "  [py] restaurado $PREV"
  else echo "  !! sin copia registrada: se queda el actual (sin los sets no cierra nada)"; fi
  [ -e "$STATE" ] && mv "$STATE" "$STATE.rolledback.$(date +%Y%m%d-%H%M%S)"
fi
echo "ROLLBACK_CUSTCLOSED_OK $(hostname)"

#!/usr/bin/env bash
# Deshace deploy_customer_trust.sh. DIRIGIDO: borra exactamente las dos reglas
# (por handle, localizadas por @cust_trust) y los dos sets en UNA transacción;
# quita lo mismo de /etc/nftables.conf; vuelve a poner sweepguard.py y
# neuravps-unblock-ip que guardó ESE despliegue (rutas en $STATE) y deshabilita
# neuravps-cust-trust-restore.service. El estado
# (/var/lib/neuravps-sweepguard/cust_trust.json) se APARTA (.rolledback.*): con el
# script anterior, revoke-vm ya no lo mantendría al día, y un redespliegue no debe
# devolver la confianza de un dueño anterior. Tras redesplegar, la reconciliación
# de NeuraVPS vuelve a llenarlo desde Firestore.
#
# ⚠️ NUNCA se restaura un `nft list ruleset` ni se recarga /etc/nftables.conf.
#
# Efecto: las IPs de confianza vuelven a las reglas normales en todas las VMs
# (lo de antes). Antes de deshacer en una base, apaga la función en NeuraVPS
# (TRUSTED_IPS_ENABLED=0 / config) o trust-set dará «error» ahí.
set -euo pipefail
CONF=${CONF:-/etc/nftables.conf}
SKIP_INSTALL=${SKIP_INSTALL:-0}
SKIP_SYSTEMD=${SKIP_SYSTEMD:-0}
STATE=${STATE:-/var/lib/neuravps-sweepguard/cust_trust.deploy}
SBIN=${SBIN:-/usr/local/sbin}
UNIT_DIR=${UNIT_DIR:-/etc/systemd/system}
UNIT=neuravps-cust-trust-restore.service
TRUST_STATE=${TRUST_STATE:-/var/lib/neuravps-sweepguard/cust_trust.json}

echo "=== rollback de cust_trust en $(hostname) ==="

TX=$(mktemp --suffix=.nft /root/sg-cust-trust-rollback.XXXXXX 2>/dev/null || mktemp --suffix=.nft)
A4=$(nft -a list chain ip rdpguard pre)
A6=$(nft -a list chain ip6 rdpguard pre)
H4=$(awk '/@cust_trust/{print $NF}' <<<"$A4")
H6=$(awk '/@cust_trust/{print $NF}' <<<"$A6")
: > "$TX"
for h in $H4; do echo "delete rule ip rdpguard pre handle $h" >> "$TX"; done
for h in $H6; do echo "delete rule ip6 rdpguard pre handle $h" >> "$TX"; done
nft list set ip rdpguard cust_trust >/dev/null 2>&1 && echo "delete set ip rdpguard cust_trust" >> "$TX"
nft list set ip6 rdpguard cust_trust >/dev/null 2>&1 && echo "delete set ip6 rdpguard cust_trust" >> "$TX"
if [ -s "$TX" ]; then
  sed 's/^/  tx: /' "$TX"
  nft -c -f "$TX"
  nft -f "$TX"
  echo "  [vivo] deshecho en una transacción"
else
  echo "  [vivo] nada que deshacer"
fi
rm -f "$TX"

if grep -q "cust_trust" "$CONF"; then
  BK="$CONF.bak.custtrust-rollback.$(date +%Y%m%d-%H%M%S)"
  NEW="$CONF.custtrust-rollback.new"
  cp -a "$CONF" "$BK"
  cp -a "$CONF" "$NEW"
  python3 - "$NEW" <<'PY'
import re, sys
conf = sys.argv[1]
s = open(conf).read()
s, n1 = re.subn(r"    # cust_trust: [^\n]*\n    #[^\n]*\n    set cust_trust \{\n(?:        [^\n]*\n)*?    \}\n", "", s)
s, n2 = re.subn(r" +fib daddr type local ip6? saddr \. meta l4proto \. th dport @cust_trust accept comment \"[^\"]*\"\n", "", s)
assert "cust_trust" not in s, "quedan restos de cust_trust"
open(conf, "w").write(s)
print(f"  [conf] quitados {n1} sets y {n2} reglas")
PY
  if nft -c -f "$NEW"; then mv -f "$NEW" "$CONF"; echo "  [conf] sintaxis OK (no cargada); publicada con mv"
  else rm -f "$NEW"; echo "  !! conf nueva no valida — $CONF sin tocar"; exit 1; fi
else
  echo "  [conf] nada que quitar"
fi

if [ "$SKIP_INSTALL" != 1 ]; then
  for dst in "$SBIN/sweepguard.py" "$SBIN/neuravps-unblock-ip"; do
    PREV=$(awk -F= -v k="$dst" '$1 == k {print $2}' "$STATE" 2>/dev/null | tail -1 || true)
    if [ -n "$PREV" ] && [ -e "$PREV" ]; then
      install -m 0755 -o "$(id -u)" -g "$(id -g)" "$PREV" "$dst"
      echo "  [py] restaurado $PREV"
    else
      echo "  !! sin copia registrada de $dst en $STATE: se queda el actual (sin el set" \
           "se comporta como el anterior); revisa a mano los $dst.bak.custtrust.*"
    fi
  done
  if [ -e "$STATE" ]; then mv "$STATE" "$STATE.rolledback.$(date +%Y%m%d-%H%M%S)"; fi
fi
if [ -e "$UNIT_DIR/$UNIT" ]; then
  if [ "$SKIP_SYSTEMD" != 1 ]; then systemctl disable "$UNIT" >/dev/null 2>&1 || true; fi
  rm -f "$UNIT_DIR/$UNIT"
  [ "$SKIP_SYSTEMD" = 1 ] || systemctl daemon-reload
  echo "  [unit] $UNIT quitado"
fi
if [ -e "$TRUST_STATE" ]; then
  mv "$TRUST_STATE" "$TRUST_STATE.rolledback.$(date +%Y%m%d-%H%M%S)"
  echo "  [estado] cust_trust.json apartado"
fi
rm -f /root/sg-cust-trust*.nft 2>/dev/null || true
echo "ROLLBACK_CUSTTRUST_OK $(hostname)"

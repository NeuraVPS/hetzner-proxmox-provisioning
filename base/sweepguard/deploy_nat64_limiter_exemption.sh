#!/usr/bin/env bash
# Los clientes IPv4 no deben pasar DOS veces por el limitador (24/11/2026).
#
# Por qué
# -------
# Un cliente IPv4 entra por `ip rdpguard pre` con su IP real: bf_static,
# cust_trust, bf_auto, bf_seen, bf_src (60/min por origen) y bf_port (12/min
# por origen y puerto). Después se le hace DNAT a Jool (10.0.0.3), SNAT a la VIP
# y vuelve traducido por `veth-host` como 64:ff9b:1::<VIP>, y entonces pasa
# OTRA VEZ por `ip6 rdpguard pre`, donde el limitador `@bf` (12/min, ráfaga 6
# por origen y puerto) ve a TODOS los clientes IPv4 de esa VM como UNA sola
# fuente. Efecto: un puerto de VM admite 12 conexiones nuevas por minuto entre
# todos sus clientes IPv4, y una IP de confianza (cust_trust) que abre 10 SSH a
# la vez sigue viendo SYN descartados (medido en b0: 9 de 12 en 3 s).
# sweepguard ya ignora 64:ff9b:1::/96 en v6 (la familia v4 es la que juzga).
#
# Este script inserta en `ip6 rdpguard pre`, justo ANTES del registro en
# bf_seen (o sea, después de bf_allow, invitado, cust_trust, cust_allow y
# bf_auto):
#     iifname "veth-host" ip6 saddr 64:ff9b:1::/96 accept
# Solo lo que llega por el enlace de Jool: una 64:ff9b:1:: falseada desde
# Internet entra por la interfaz pública y sigue las reglas de siempre.
# Lo persiste en /etc/nftables.conf (fichero hermano + nft -c + mv).
# ⚠️ Vivo = UNA transacción. NUNCA se recarga /etc/nftables.conf.
# Deshacer: rollback_nat64_limiter_exemption.sh.
#   CONF=/ruta/nftables.conf para probarlo fuera de producción.
set -euo pipefail
CONF=${CONF:-/etc/nftables.conf}
JOOL_IF=${JOOL_IF:-veth-host}
COM="IPv4 traducido (NAT64): ya juzgado en ip rdpguard con la IP real"
RULE="iifname \"$JOOL_IF\" ip6 saddr 64:ff9b:1::/96 accept"

echo "=== exención NAT64 del limitador v6 en $(hostname) ==="
ROUTE=$(ip -6 route show 64:ff9b:1::/96 2>/dev/null || true); [ "${SKIP_ROUTE_CHECK:-0}" = 1 ] && ROUTE="dev $JOOL_IF (sin comprobar)"
case "$ROUTE" in *"dev $JOOL_IF"*) echo "  [ruta] 64:ff9b:1::/96 por $JOOL_IF OK" ;;
  *) echo "  !! 64:ff9b:1::/96 no va por $JOOL_IF ($ROUTE) — aborto"; exit 1 ;; esac

CH=$(nft list chain ip6 rdpguard pre)
case "$CH" in
  *"64:ff9b:1::/96 accept"*) echo "  [vivo] ya estaba" ;;
  *)
    A=$(nft -a list chain ip6 rdpguard pre)
    H=$(awk '/add @bf_seen/{print $NF; exit}' <<<"$A")
    [ -n "$H" ] || { echo "  !! no encuentro el registro en bf_seen de ip6 rdpguard pre — aborto"; exit 1; }
    TX=$(mktemp --suffix=.nft)
    echo "insert rule ip6 rdpguard pre position $H $RULE comment \"$COM\"" > "$TX"
    nft -c -f "$TX"; nft -f "$TX"; rm -f "$TX"
    echo "  [vivo] regla insertada antes de bf_seen"
    ;;
esac

python3 - <<'PY'
import subprocess
r = [l.strip() for l in subprocess.run(["nft", "list", "chain", "ip6", "rdpguard", "pre"],
     capture_output=True, text=True, check=True).stdout.splitlines()]
i = lambda s: next(k for k, l in enumerate(r) if s in l)
x, a, seen, lim = i("64:ff9b:1::/96 accept"), i("saddr @bf_auto"), i("add @bf_seen"), i("add @bf {")
assert a < x < seen < lim, f"orden incorrecto auto={a} nat64={x} seen={seen} bf={lim}"
assert 'iifname "veth-host"' in r[x] or "iifname" in r[x], "la exención no exige la interfaz de Jool"
print("  [orden] bf_auto -> exención NAT64 (solo veth-host) -> bf_seen -> @bf OK")
PY

if grep -q "64:ff9b:1::/96 accept" "$CONF"; then
  echo "  [conf] ya estaba"
else
  BK="$CONF.bak.nat64lim.$(date +%Y%m%d-%H%M%S)"; NEW="$CONF.nat64lim.new"
  cp -a "$CONF" "$BK"; cp -a "$CONF" "$NEW"
  python3 - "$NEW" "$RULE" "$COM" <<'PY'
import re, sys
conf, rule, com = sys.argv[1:]
s = open(conf).read()
i = s.index("table ip6 rdpguard {")
rest = s[i:]
cands = [k for k in (rest.find("\ntable ", 1), rest.find("\ninclude ")) if k > 0]
j = i + (min(cands) if cands else len(rest))
b = s[i:j]
m = list(re.finditer(r"\n( +)[^\n]*add @bf_seen[^\n]*\n", b))
assert len(m) == 1, "registro en bf_seen no único/ausente en ip6 rdpguard"
m = m[0]
b = b[:m.start() + 1] + f"{m.group(1)}{rule} comment \"{com}\"\n" + b[m.start() + 1:]
open(conf, "w").write(s[:i] + b + s[j:])
print("  [conf] regla escrita (fichero hermano)")
PY
  if nft -c -f "$NEW"; then mv -f "$NEW" "$CONF"; echo "  [conf] sintaxis OK (no cargada); publicada con mv; copia en $BK"
  else rm -f "$NEW"; echo "  !! conf nueva no valida — sin tocar"; exit 1; fi
fi
nft list chain ip6 rdpguard pre | sed 's/^/  /'
echo "NAT64LIM_OK $(hostname)"

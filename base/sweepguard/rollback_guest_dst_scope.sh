#!/usr/bin/env bash
# Deshace deploy_guest_dst_scope.sh. Deshacer DIRIGIDO: borra exactamente las dos
# reglas (por handle, localizadas por su comentario) y los dos sets, en UNA
# transacción; quita lo mismo de /etc/nftables.conf; y vuelve a poner el
# sweepguard.py anterior si hay copia.
#
# ⚠️ NUNCA se restaura un `nft list ruleset` ni se recarga /etc/nftables.conf:
#    el primero AÑADE en vez de reemplazar (duplicó `rdpguard pre` en b0 el
#    24/08) y el segundo empieza por `flush ruleset` (se caen los túneles).
#
# Tras deshacer, la cadena vuelve a la de 2026-08-15: los invitados solo quedan
# exentos fuera de 10000-39999 (y vuelven los falsos positivos de sweepguard).
set -euo pipefail
CONF=${CONF:-/etc/nftables.conf}
SKIP_INSTALL=${SKIP_INSTALL:-0}

echo "=== rollback de nuestras4/nuestras6 en $(hostname) ==="

TX=$(mktemp --suffix=.nft /root/sg-dst-scope-rollback.XXXXXX)
H4=$(nft -a list chain ip rdpguard pre | awk '/@nuestras4/{print $NF}')
H6=$(nft -a list chain ip6 rdpguard pre | awk '/@nuestras6/{print $NF}')
: > "$TX"
for h in $H4; do echo "delete rule ip rdpguard pre handle $h" >> "$TX"; done
for h in $H6; do echo "delete rule ip6 rdpguard pre handle $h" >> "$TX"; done
nft list set ip rdpguard nuestras4 >/dev/null 2>&1 && echo "delete set ip rdpguard nuestras4" >> "$TX"
nft list set ip6 rdpguard nuestras6 >/dev/null 2>&1 && echo "delete set ip6 rdpguard nuestras6" >> "$TX"
if [ -s "$TX" ]; then
  sed 's/^/  tx: /' "$TX"
  nft -c -f "$TX"
  nft -f "$TX"
  echo "  [vivo] deshecho en una transacción"
else
  echo "  [vivo] nada que deshacer"
fi

if grep -q "nuestras4\|nuestras6" "$CONF"; then
  BK="$CONF.bak.dstscope-rollback.$(date +%Y%m%d-%H%M%S)"
  cp -a "$CONF" "$BK"
  python3 - "$CONF" <<'PY'
import re, sys
conf = sys.argv[1]
s = open(conf).read()
# bloque del set con sus dos líneas de comentario y la línea en blanco de detrás
s, n1 = re.subn(r"    # (nuestras[46]): NUESTRAS direcciones\.[^\n]*\n    #[^\n]*\n"
                r"    set nuestras[46] \{\n(?:        [^\n]*\n)*?    \}\n\n", "", s)
s, n2 = re.subn(r"        ip6? saddr \S+ ip6? daddr != @nuestras[46] accept comment \"[^\"]*\"\n", "", s)
assert "nuestras4" not in s and "nuestras6" not in s, "quedan restos de nuestras4/6"
open(conf, "w").write(s)
print(f"  [conf] quitados {n1} sets y {n2} reglas")
PY
  if nft -c -f "$CONF"; then echo "  [conf] sintaxis OK (no cargada)"
  else echo "  !! conf no valida — restauro"; cp -a "$BK" "$CONF"; exit 1; fi
else
  echo "  [conf] nada que quitar"
fi

if [ "$SKIP_INSTALL" != 1 ]; then
  # la MÁS ANTIGUA: la que se guardó al aplicar este cambio por primera vez
  # (por el sello del NOMBRE: cp -a conserva la mtime del original)
  PREV=$(ls -1 /usr/local/sbin/sweepguard.py.bak.dstscope.* 2>/dev/null | sort | head -1 || true)
  if [ -n "$PREV" ]; then
    install -m 0755 "$PREV" /usr/local/sbin/sweepguard.py
    echo "  [py] restaurado $PREV"
  else
    echo "  [py] sin copia previa (el .py nuevo sin el set ya se comporta como el viejo)"
  fi
fi

nft list chain ip rdpguard pre | sed 's/^/  /'
nft list chain ip6 rdpguard pre | sed 's/^/  /'
echo "ROLLBACK_OK $(hostname)"

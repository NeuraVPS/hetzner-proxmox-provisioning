#!/usr/bin/env bash
# Extiende las reglas rdpguard de 20000-29999 a 10000-29999.
#
# Por que (2026-07-25)
# --------------------
# La base reenvia DOS puertos por VM: RDP (3389) en 2xxxx y SMB (445) en 1xxxx,
# los mismos 1768 clientes en ambos rangos. Las reglas solo cubrian 2xxxx, asi
# que SMB — objetivo de fuerza bruta tan clasico como RDP, y la via por la que
# entra el ransomware — estaba SIN rate limit, SIN deteccion de barrido y SIN
# deteccion de flood. No habia ataque en curso cuando se detecto; el hueco
# estaba abierto igualmente.
#
# Idempotente: si ya estan en 10000-29999 no hace nada.
set -euo pipefail

echo "=== extender rango rdpguard en $(hostname) ==="

# El contador propio de portguard puede no existir (se creo a mano en b1).
nft list counter ip rdpguard portguard_drops >/dev/null 2>&1 || \
  nft add counter ip rdpguard portguard_drops
BK="/etc/nftables.conf.bak.smbrange.$(date +%Y%m%d-%H%M%S)"

# Idempotencia: NO basta con que no queden reglas del rango viejo — hay que
# comprobar que estan las TRES. Un fallo a medias dejo b0 sin la regla bf_port
# porque el contador propio no existia y la comprobacion antigua lo dio por bueno.
have_new="$(nft list chain ip rdpguard pre | grep -c 'dport 10000-29999' || true)"
old="$(nft list chain ip rdpguard pre | grep -c 'dport 20000-29999' || true)"
if [ "$old" = "0" ] && [ "$have_new" -ge 3 ]; then
  echo "  ya extendido en vivo (3 reglas)"
else
  echo "  reglas a migrar: $old (nuevas presentes: $have_new)"
  # se borran TAMBIEN las del rango nuevo para reconstruir el juego completo
  nft -a list chain ip rdpguard pre | grep -E 'dport (10000|20000)-29999' \
    | grep -oE 'handle [0-9]+' | grep -oE '[0-9]+' | sort -rn > /tmp/rg_handles_new
  while read -r h; do nft delete rule ip rdpguard pre handle "$h" 2>/dev/null || true; done < /tmp/rg_handles_new
  # Se reconstruyen con el rango nuevo. Se borran de mayor a menor handle para
  # que los handles restantes no se desplacen.
  nft add rule ip rdpguard pre tcp dport 10000-29999 \
    tcp flags '&' '(syn|ack)' == syn \
    add @bf_seen "{ ip saddr . tcp dport }" \
    comment '"record (source,port) for the sweep detector"'
  nft add rule ip rdpguard pre tcp dport 10000-29999 \
    tcp flags '&' '(syn|ack)' == syn \
    add @bf_src "{ ip saddr limit rate over 60/minute burst 30 packets }" \
    counter name bf_v4_drops drop \
    comment '"rate-limit excess new conns per source (anti port-spray)"'
  nft add rule ip rdpguard pre tcp dport 10000-29999 \
    tcp flags '&' '(syn|ack)' == syn \
    add @bf_port "{ ip saddr . tcp dport limit rate over 12/minute burst 15 packets }" \
    counter name portguard_drops drop \
    comment '"rate-limit repeated hits on the SAME customer port"'
  echo "  reglas recreadas con 10000-29999"
fi

# bf_allow DEBE seguir primero: se comprueba, no se asume.
first="$(nft list chain ip rdpguard pre | grep -E '^\s+(ip|tcp|meta)' | head -1)"
case "$first" in
  *bf_allow*accept*) echo "  orden OK (bf_allow primero)" ;;
  *) echo "  !! bf_allow NO es la primera regla — REVISAR"; exit 1 ;;
esac

echo "=== persistencia ==="
cp -a /etc/nftables.conf "$BK"
python3 - <<'PY'
import re, sys
p = '/etc/nftables.conf'
s = open(p).read()
start = s.find('table ip rdpguard {')
if start < 0:
    print('  no encuentro table ip rdpguard'); sys.exit(1)
depth, end = 0, None
for i in range(start, len(s)):
    if s[i] == '{':
        depth += 1
    elif s[i] == '}':
        depth -= 1
        if depth == 0:
            end = i + 1
            break
block = s[start:end]
new = block.replace('dport 20000-29999', 'dport 10000-29999')
if new == block:
    print('  ya persistido')
else:
    open(p, 'w').write(s[:start] + new + s[end:])
    print(f'  persistido ({block.count("dport 20000-29999")} reglas)')
PY

if nft -c -f /etc/nftables.conf; then
  echo "  conf valida OK"
else
  echo "  !! conf NO valida — restauro $BK"; cp -a "$BK" /etc/nftables.conf; exit 1
fi
echo "SMBRANGE_OK $(hostname)"

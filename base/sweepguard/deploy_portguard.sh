#!/usr/bin/env bash
# portguard — rate-limit repeated hits on the SAME customer port (v4).
#
# Why this exists (2026-07-25)
# ----------------------------
# The v4 rdpguard table had NO per-(source,port) limit, only 60/min per source.
# So one IP could hammer ONE customer VM at 60 attempts/min forever without
# tripping anything, and a handful of IPs coordinating on the same port stayed
# invisible to every other detector:
#
#   * port-diversity (sweepguard minPorts=20): they touch 1-2 ports
#   * connection flood (sweepguard maxConns=100): connect->fail->close leaves
#     ~1 live connection at any instant, however fast they go
#   * per-source rate (bf_src 60/min): they stay under it
#
# Measured while writing this, on b1: port 21845 was under attack from SIX
# distinct sources at once. The VM behind it had 17936 failed logons in 24h
# (16589 against ADMINISTRATOR) and ZERO successful RDP logins — a paying
# customer locked out of their own machine. Port 20389: 1189 failed, 0 ok.
#
# The ip6 table already had this rule (12/min per (saddr,dport)); only v4 —
# the family that judges essentially every real client — was missing it.
#
# Why a rate limit and not a block: it can only THROTTLE, never lock anyone
# out. A real client needs ONE successful connection; 12/min with burst 15
# leaves that untouched even during an RDP auto-reconnect storm. Measured
# legitimate behaviour: median 1, p95 4 concurrent connections to a given
# port. An attacker needs thousands, and gets 12.
set -euo pipefail

RATE="${PORTGUARD_RATE:-12/minute}"
BURST="${PORTGUARD_BURST:-15}"
COMMENT="rate-limit repeated hits on the SAME customer port (anti concentrated brute-force)"

echo "=== portguard en $(hostname) — ${RATE} burst ${BURST} ==="

if ! nft list table ip rdpguard >/dev/null 2>&1; then
  echo "  no existe table ip rdpguard — ejecuta deploy_sweepguard.sh antes"; exit 1
fi

# ---------- en vivo (idempotente) ----------
nft list set ip rdpguard bf_port >/dev/null 2>&1 || \
  nft add set ip rdpguard bf_port \
    "{ type ipv4_addr . inet_service; flags dynamic,timeout; timeout 10m; size 65536; }"

if nft list chain ip rdpguard pre | grep -q "@bf_port"; then
  echo "  regla ya activa en vivo"
else
  nft add rule ip rdpguard pre tcp dport 20000-29999 \
    tcp flags '&' '(syn|ack)' == syn \
    add @bf_port "{ ip saddr . tcp dport limit rate over ${RATE} burst ${BURST} packets }" \
    counter name bf_v4_drops drop comment "\"$COMMENT\""
  echo "  regla aplicada en vivo"
fi

# bf_allow DEBE seguir siendo la primera regla de la cadena, o una IP de la
# lista blanca podria ser limitada. Se comprueba, no se asume.
first="$(nft list chain ip rdpguard pre | grep -E '^\s+(ip|tcp|meta)' | head -1)"
case "$first" in
  *bf_allow*accept*) echo "  orden OK (bf_allow primero)" ;;
  *) echo "  !! bf_allow NO es la primera regla — revisar a mano"; exit 1 ;;
esac

# ---------- persistencia ----------
echo "=== persistencia en /etc/nftables.conf ==="
BK="/etc/nftables.conf.bak.portguard.$(date +%Y%m%d-%H%M%S)"
cp -a /etc/nftables.conf "$BK"

RATE="$RATE" BURST="$BURST" COMMENT="$COMMENT" python3 - <<'PY'
import os, re, sys
p = '/etc/nftables.conf'
s = open(p).read()
if 'bf_port' in s:
    print('  ya persistido'); sys.exit(0)

rate, burst, comment = os.environ['RATE'], os.environ['BURST'], os.environ['COMMENT']

# SOLO dentro de `table ip rdpguard` — ip6 no tiene las mismas anclas y un
# parche global le metio una vez reglas ip6 en la tabla ip.
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

# set: justo detras del bloque de bf_auto
m = re.search(r'\n(\s*)set bf_auto \{.*?\n\1\}\n', block, re.S)
if not m:
    print('  no encuentro el set bf_auto'); sys.exit(1)
ind = m.group(1)
setdef = (f"\n{ind}set bf_port {{\n{ind}    type ipv4_addr . inet_service\n"
          f"{ind}    size 65536\n{ind}    flags dynamic,timeout\n"
          f"{ind}    timeout 10m\n{ind}}}\n")
block = block[:m.end()] + setdef + block[m.end():]

# regla: al final de la cadena, detras de la de bf_src (la ultima)
m2 = re.search(r'\n([ \t]*)tcp dport 20000-29999[^\n]*@bf_src[^\n]*\n', block)
if not m2:
    print('  no encuentro la regla bf_src'); sys.exit(1)
rind = m2.group(1)
rule = (f"{rind}tcp dport 20000-29999 tcp flags & (syn|ack) == syn "
        f"add @bf_port {{ ip saddr . tcp dport limit rate over {rate} "
        f"burst {burst} packets }} counter name \"bf_v4_drops\" drop "
        f"comment \"{comment}\"\n")
block = block[:m2.end()] + rule + block[m2.end():]

open(p, 'w').write(s[:start] + block + s[end:])
print('  persistido en la conf')
PY

if nft -c -f /etc/nftables.conf; then
  echo "  conf valida OK"
else
  echo "  !! conf NO valida — restauro $BK"; cp -a "$BK" /etc/nftables.conf; exit 1
fi
echo "PORTGUARD_OK $(hostname)"

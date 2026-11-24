#!/usr/bin/env bash
# Prueba de deploy/rollback_nat64_limiter_exemption.sh con nft y paquetes de
# verdad, sin root (netns de usuario). Uso: test_nat64_limiter_netns.sh <copia de nftables.conf>
# (sin las líneas include). Comprueba que 30 SYN seguidos desde 64:ff9b:1::x por
# `veth-host` pasan todos (sin @bf) y que desde otra interfaz con la MISMA
# dirección falseada el limitador sigue cortando; y que el rollback deja la conf igual.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd); CONF_IN=${1:-}
W=$(mktemp -d); trap 'rm -rf "$W"' EXIT
if [ -n "$CONF_IN" ]; then
  # conf real: solo despliegue + rollback (sus tablas de filtrado no dejan pasar
  # los paquetes de esta topología de prueba)
  grep -v "^include " "$CONF_IN" > "$W/conf.nft"; cp "$W/conf.nft" "$W/orig.nft"
  cat > "$W/real.sh" <<'REAL'
set -euo pipefail
HERE=$1; cd "$2"
nft -f conf.nft
CONF=conf.nft SKIP_ROUTE_CHECK=1 bash "$HERE/deploy_nat64_limiter_exemption.sh" > d.out 2>&1 || { cat d.out; echo "FAIL deploy"; exit 1; }
grep -E "\[|OK" d.out | grep -v "^  \s*\(ip\|tcp\|meta\|fib\|type\|chain\)" | head -6
[ "$(nft list chain ip6 rdpguard pre | grep -c 'iifname "veth-host" ip6 saddr 64:ff9b:1::/96 accept')" = 1 ] || { echo "FAIL regla viva"; exit 1; }
[ "$(grep -c '64:ff9b:1::/96 accept' conf.nft)" = 1 ] || { echo "FAIL regla en conf"; exit 1; }
CONF=conf.nft bash "$HERE/rollback_nat64_limiter_exemption.sh" > r.out 2>&1 || { cat r.out; echo "FAIL rollback"; exit 1; }
nft list chain ip6 rdpguard pre | grep -q "64:ff9b:1::/96" && { echo "FAIL regla viva tras rollback"; exit 1; }
cmp orig.nft conf.nft || { echo "FAIL conf distinta tras rollback"; exit 1; }
echo REALCONF_OK
REAL
  unshare -rn bash "$W/real.sh" "$HERE" "$W"
  exit 0
fi
cat > "$W/conf.nft" <<'NFT'
flush ruleset
table ip6 rdpguard {
    set bf_allow { type ipv6_addr; flags interval; }
    set bf_auto { type ipv6_addr; flags dynamic,timeout; timeout 24h; size 65536; }
    set bf_seen { type ipv6_addr . inet_service; flags dynamic,timeout; timeout 6h; size 65536; }
    set bf { type ipv6_addr . inet_service; flags dynamic,timeout; timeout 10m; size 65536; }
    chain pre {
        type filter hook prerouting priority mangle; policy accept;
        ip6 saddr @bf_allow accept
        ip6 saddr @bf_auto counter drop comment "auto-blocked RDP port sweeper (expires)"
        tcp dport 20000-39999 tcp flags & (syn | ack) == syn add @bf_seen { ip6 saddr . tcp dport } comment "record (source,port) for the sweep detector"
        tcp dport 20000-39999 tcp flags & (syn | ack) == syn add @bf { ip6 saddr . tcp dport limit rate over 12/minute burst 6 packets } counter drop
    }
}
NFT
cp "$W/conf.nft" "$W/orig.nft"
cat > "$W/inner.sh" <<'INNER'
set -euo pipefail
HERE=$1; W=$2; cd "$W"
trap 'kill $(jobs -p) 2>/dev/null || true' EXIT
nft -f conf.nft
ip link set lo up
B6=2a01:4f9:aaaa::2
ip -6 addr add $B6/128 dev lo
mkns() { unshare -n sleep 600 >/dev/null 2>&1 & echo $!; }
J=$(mkns); X=$(mkns); sleep 0.3
ip link add veth-host type veth peer name vj netns /proc/$J/ns/net
ip link add vx type veth peer name vy netns /proc/$X/ns/net
ip link set veth-host up; ip link set vx up
ip -6 addr add fd00::1/64 dev veth-host nodad; ip -6 addr add fd01::1/64 dev vx nodad
for p in "$J vj fd00::2 fd00::1" "$X vy fd01::2 fd01::1"; do set -- $p
  nsenter --net=/proc/$1/ns/net sh -c "ip link set lo up; ip link set $2 up; ip -6 addr add $3/64 dev $2 nodad; \
    ip -6 addr add 64:ff9b:1::4d2a:314f/128 dev lo; ip -6 route add default via $4"; done
ip -6 route add 64:ff9b:1::4d2a:314f/128 via fd00::2 dev veth-host
python3 -c "
import socket
s=socket.socket(socket.AF_INET6); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind(('$B6',21517)); s.listen(256)
while True:
    c,_=s.accept(); c.close()" >/dev/null 2>&1 & SP=$!
sleep 0.5
burst() { nsenter --net=/proc/$1/ns/net python3 -c "
import socket
ok=0
for _ in range(30):
    s=socket.socket(socket.AF_INET6); s.settimeout(0.3); s.bind(('64:ff9b:1::4d2a:314f',0))
    try: s.connect(('$B6',21517)); ok+=1
    except OSError: pass
    s.close()
print(ok)"; }
n=$(burst $J); [ "$n" -lt 15 ] || { echo "FAIL antes: $n/30 sin limitador"; exit 1; }; echo "  ok  antes, por veth-host: $n/30 (limitador compartido)"
nft flush set ip6 rdpguard bf 2>/dev/null || true
CONF=conf.nft SKIP_ROUTE_CHECK=1 bash "$HERE/deploy_nat64_limiter_exemption.sh" > d.out 2>&1 || { cat d.out; exit 1; }
grep -E "\[|NAT64LIM_OK" d.out | grep -v "^  \s*\(ip\|tcp\|meta\|fib\|type\|chain\)" | head -8
n=$(burst $J); [ "$n" = 30 ] || { echo "FAIL veth-host $n/30"; exit 1; }; echo "  ok  por veth-host 30/30 (sin @bf)"
ip -6 route del 64:ff9b:1::4d2a:314f/128; ip -6 route add 64:ff9b:1::4d2a:314f/128 via fd01::2 dev vx
nft flush set ip6 rdpguard bf
n=$(burst $X); [ "$n" -lt 15 ] || { echo "FAIL falseada por otra interfaz $n/30"; exit 1; }; echo "  ok  misma dirección por otra interfaz: $n/30 (limitador)"
CONF=conf.nft bash "$HERE/rollback_nat64_limiter_exemption.sh" > r.out 2>&1 || { cat r.out; exit 1; }
cmp orig.nft conf.nft && echo "  ok  rollback: conf idéntica"
nft list chain ip6 rdpguard pre | grep -q "64:ff9b:1::/96 accept" && { echo "FAIL regla viva tras rollback"; exit 1; }
nft add rule ip6 rdpguard pre iifname "eth9" ip6 saddr 64:ff9b:1::/96 accept   # regla ajena parecida
if CONF=conf.nft SKIP_ROUTE_CHECK=1 bash "$HERE/deploy_nat64_limiter_exemption.sh" > d2.out 2>&1; then
  echo "FAIL despliegue con regla ajena"; exit 1; fi
grep -q "no son esta" d2.out || { echo "FAIL motivo del aborto"; cat d2.out; exit 1; }
nft list chain ip6 rdpguard pre | grep -q 'iifname "veth-host" ip6 saddr 64:ff9b:1::/96' && { echo "FAIL insertó la propia pese al conflicto"; exit 1; }
echo "  ok  despliegue aborta ante una regla 64:ff9b:1:: ajena, sin tocar nada"
if CONF=conf.nft bash "$HERE/rollback_nat64_limiter_exemption.sh" > r2.out 2>&1; then
  echo "FAIL rollback con regla ajena"; exit 1; fi
nft list chain ip6 rdpguard pre | grep -q 'iifname "eth9"' || { echo "FAIL rollback borró la regla ajena"; exit 1; }
echo "  ok  rollback aborta y no borra la regla ajena"
# regla ajena que CONTIENE el texto propio con una condición delante, y conflicto solo en disco
nft flush chain ip6 rdpguard pre 2>/dev/null || true; nft -f orig.nft; cp orig.nft conf.nft
OWN_TXT='iifname "veth-host" ip6 saddr 64:ff9b:1::/96 accept comment "IPv4 traducido (NAT64): ya juzgado en ip rdpguard con la IP real"'
nft add rule ip6 rdpguard pre tcp dport 1 $OWN_TXT
if CONF=conf.nft bash "$HERE/rollback_nat64_limiter_exemption.sh" > r3.out 2>&1; then echo "FAIL rollback con superconjunto"; exit 1; fi
nft list chain ip6 rdpguard pre | grep -q "tcp dport 1 " || { echo "FAIL borró la regla ajena que contiene el texto"; exit 1; }
echo "  ok  rollback no confunde una regla ajena que contiene el texto propio"
nft flush ruleset; nft -f orig.nft; cp orig.nft conf.nft
sed -i "s|    chain pre {|    # $OWN_TXT\n    chain pre {|" conf.nft     # solo como comentario en disco
sed -i "0,/add @bf_seen/s||add @bf_seen|" conf.nft
python3 - conf.nft <<'PY'
import sys
p = sys.argv[1]; s = open(p).read()
i = s.index("table ip6 rdpguard {"); j = s.index("chain pre {", i)
s = s[:j] + "chain pre {\n        iifname \"eth9\" ip6 saddr 64:ff9b:1::/96 accept" + s[j + len("chain pre {"):]
open(p, "w").write(s)
PY
if CONF=conf.nft SKIP_ROUTE_CHECK=1 bash "$HERE/deploy_nat64_limiter_exemption.sh" > d3.out 2>&1; then echo "FAIL deploy con conflicto en disco"; exit 1; fi
nft list chain ip6 rdpguard pre | grep -q '64:ff9b:1::/96' && { echo "FAIL tocó lo vivo con conflicto en disco"; exit 1; }
echo "  ok  conflicto solo en disco: aborta sin tocar lo vivo (y un comentario no cuenta como regla)"
# la regla exacta, pero en OTRA cadena de la tabla
nft flush ruleset; nft -f orig.nft; cp orig.nft conf.nft
nft add chain ip6 rdpguard otra
nft add rule ip6 rdpguard otra $OWN_TXT
if CONF=conf.nft SKIP_ROUTE_CHECK=1 bash "$HERE/deploy_nat64_limiter_exemption.sh" > d4.out 2>&1; then echo "FAIL deploy con la regla en otra cadena"; exit 1; fi
if CONF=conf.nft bash "$HERE/rollback_nat64_limiter_exemption.sh" > r4.out 2>&1; then echo "FAIL rollback con la regla en otra cadena"; exit 1; fi
nft list chain ip6 rdpguard otra | grep -q "64:ff9b:1::/96" || { echo "FAIL borró la regla de otra cadena"; exit 1; }
echo "  ok  la regla exacta en otra cadena: deploy y rollback abortan sin tocarla"
# en disco, en pre pero ANTES de bf_auto: posición mala → aborta
nft flush ruleset; nft -f orig.nft; cp orig.nft conf.nft
python3 - conf.nft "$OWN_TXT" <<'PY'
import sys
p, own = sys.argv[1:]; s = open(p).read()
i = s.index("table ip6 rdpguard {"); j = s.index("chain pre {", i); k = s.index("\n", s.index("policy accept", j))
s = s[:k + 1] + "        " + own + "\n" + s[k + 1:]
open(p, "w").write(s)
PY
if CONF=conf.nft SKIP_ROUTE_CHECK=1 bash "$HERE/deploy_nat64_limiter_exemption.sh" > d5.out 2>&1; then echo "FAIL deploy con posición mala en disco"; exit 1; fi
grep -q "(bad)" d5.out || { echo "FAIL motivo posición"; cat d5.out; exit 1; }
echo "  ok  en disco antes de bf_auto: aborta por posición"
kill $SP $J $X 2>/dev/null || true
echo "NETNS_NAT64_OK"
INNER
unshare -rn bash ${TRACE:+-x} "$W/inner.sh" "$HERE" "$W"

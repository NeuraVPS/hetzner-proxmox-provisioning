#!/usr/bin/env bash
# Prueba de extremo a extremo del modo cerrado (deploy_customer_closed.sh +
# neuravps-unblock-ip trust-set ... closed) con nft y paquetes de verdad, sin
# root: netns de usuario, v4, v6, IPv6 directa (public6) y NAT64 por veth-host.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
cat > "$WORK/conf.nft" <<'NFT'
flush ruleset
table ip6 rdpguard {
    set bf_allow {
        type ipv6_addr
        flags interval
    }
    set bf_seen {
        type ipv6_addr . inet_service
        flags dynamic,timeout
        timeout 6h
        size 65536
    }
    set bf_auto {
        type ipv6_addr
        flags dynamic,timeout
        timeout 24h
        size 65536
    }
    set bf {
        type ipv6_addr . inet_service
        flags dynamic,timeout
        timeout 10m
        size 65536
    }
    set nuestras6 {
        type ipv6_addr
        flags interval
        elements = { 2a01:4f9:aaaa::/64 }
    }
    chain pre {
        type filter hook prerouting priority -150; policy accept;
        ip6 saddr @bf_allow accept
        ip6 saddr @bf_auto counter drop comment "auto-blocked RDP port sweeper (expires)"
        tcp dport 20000-39999 tcp flags & (syn|ack) == syn add @bf_seen { ip6 saddr . tcp dport }
        tcp dport 20000-39999 tcp flags & (syn|ack) == syn add @bf { ip6 saddr . tcp dport limit rate over 12/minute burst 6 packets } counter drop
    }
}
table ip rdpguard {
    set bf_allow {
        type ipv4_addr
        flags interval
    }
    set bf_static {
        type ipv4_addr
        flags interval
        elements = { 45.155.205.0/24 }
    }
    set bf_seen {
        type ipv4_addr . inet_service
        flags dynamic,timeout
        timeout 6h
        size 65536
    }
    set bf_auto {
        type ipv4_addr
        flags dynamic,timeout
        timeout 24h
        size 65536
    }
    set bf_port {
        type ipv4_addr . inet_service
        flags dynamic,timeout
        timeout 10m
        size 65536
    }
    set nuestras4 {
        type ipv4_addr
        flags interval
        elements = { 77.42.49.0/24 }
    }
    chain pre {
        type filter hook prerouting priority raw; policy accept;
        ip saddr @bf_allow accept comment "trusted infra/ops — exempt from RDP guard"
        ip saddr @bf_static counter drop comment "confirmed RDP brute-force sources"
        ip saddr @bf_auto counter drop comment "auto-blocked RDP port sweeper (expires)"
        tcp dport 10000-39999 tcp flags & (syn|ack) == syn add @bf_seen { ip saddr . tcp dport } comment "record (source,port) for the sweep detector"
        tcp dport 10000-39999 tcp flags & (syn|ack) == syn add @bf_port { ip saddr . tcp dport limit rate over 12/minute burst 15 packets } counter drop
    }
}
NFT


cat > "$WORK/inner.sh" <<'INNER'
set -euo pipefail
HERE=$1; WORK=$2; cd "$WORK"
trap 'kill $(jobs -p) 2>/dev/null || true' EXIT
CLIENT=149.56.212.201; OTHER=149.56.212.202; BASE=77.42.49.79
C6=2001:41d0:1:2::5; X6=2001:41d0:9::5; B6=2a01:4f9:aaaa::2; PUB6=2a01:4f9:aaaa::5ed
nft -f conf.nft
ip link set lo up
mkns() { unshare -n sleep 900 >/dev/null 2>&1 & echo $!; }
CPID=$(mkns); JPID=$(mkns); sleep 0.3
ip link add vb type veth peer name vc netns /proc/$CPID/ns/net
ip addr add $BASE/24 dev vb; ip -6 addr add $B6/64 dev vb nodad; ip link set vb up
ip -6 addr add $PUB6/128 dev lo
nsenter --net=/proc/$CPID/ns/net sh -c "ip link set lo up; ip addr add $CLIENT/24 dev vc; ip addr add $OTHER/24 dev vc; \
  ip -6 addr add $C6/64 dev vc nodad; ip -6 addr add $X6/64 dev vc nodad; ip link set vc up; \
  ip route add default dev vc; ip -6 route add default dev vc; ip -6 route add $PUB6/128 via $B6 dev vc"
ip route add $CLIENT/32 dev vb; ip route add $OTHER/32 dev vb
ip -6 route add 2001:41d0:1:2::/64 dev vb; ip -6 route add 2001:41d0:9::/64 dev vb
# Jool: lo que vuelve por veth-host con 64:ff9b:1::<VIP>
ip link add veth-host type veth peer name vj netns /proc/$JPID/ns/net
ip link set veth-host up; ip -6 addr add fd00::1/64 dev veth-host nodad
nsenter --net=/proc/$JPID/ns/net sh -c "ip link set lo up; ip link set vj up; ip -6 addr add fd00::2/64 dev vj nodad; \
  ip -6 addr add 64:ff9b:1::4d2a:314f/128 dev lo; ip -6 route add default via fd00::1"
ip -6 route add 64:ff9b:1::4d2a:314f/128 via fd00::2 dev veth-host
python3 - >/dev/null 2>&1 <<'PY' &
import socket, threading
def tcp(p):
    s = socket.socket(socket.AF_INET6); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0); s.bind(("::", p)); s.listen(64)
    while True:
        c, _ = s.accept(); c.close()
for p in (21517, 31517, 21999, 3389):
    threading.Thread(target=tcp, args=(p,), daemon=True).start()
threading.Event().wait()
PY
sleep 0.7
probe() {   # probe <ns-pid> <src> <dst> <port> -> ok | blocked
  nsenter --net=/proc/$1/ns/net python3 - "$2" "$3" "$4" <<'PY'
import socket, sys
src, dst, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
s = socket.socket(socket.AF_INET6 if ":" in dst else socket.AF_INET); s.settimeout(1)
try:
    s.bind((src, 0)); s.connect((dst, port)); print("ok")
except OSError:
    print("blocked")
PY
}
expect() { got=$(probe "$1" "$2" "$3" "$4"); [ "$got" = "$5" ] || { echo "FAIL $2 -> $3:$4 = $got (esperaba $5)"; exit 1; }; echo "  ok  $2 -> $3:$4 $got"; }
cat > ub.py <<'PY'
import importlib.machinery, importlib.util, sys
path, work, *args = sys.argv[1:]
loader = importlib.machinery.SourceFileLoader("ub", path)
spec = importlib.util.spec_from_loader("ub", loader); ub = importlib.util.module_from_spec(spec)
loader.exec_module(ub); ub.LOCK = work + "/lock"; ub.TRUST_STATE = work + "/cust_trust.json"
ub.IPV6_POLICY_STATE = work + "/ipv6-policy.json"
sys.exit(ub.main(["neuravps-unblock-ip"] + args))
PY
echo '{"main": "2a01:4f9:aaaa::2", "entries": []}' > ipv6-policy.json
UB() { python3 ub.py "$HERE/neuravps-unblock-ip" "$WORK" "$@" || true; }
must() { local d=$1; shift; "$@" || { echo "FAIL $d"; exit 1; }; echo "  ok  $d"; }

# calienta vecinos v6 (ND) antes de medir con plazos de 1 s
for d in $B6 $PUB6; do for src in $C6 $X6; do probe $CPID $src $d 21999 >/dev/null; probe $CPID $src $d 3389 >/dev/null; done; done
echo "-- 0) despliegue: cust_allow, cust_trust y modo cerrado"
CONF=conf.nft SKIP_INSTALL=1 STATE=$WORK/s1 bash "$HERE/deploy_customer_allow.sh" > d1.out 2>&1 || { cat d1.out; exit 1; }
CONF=conf.nft SKIP_INSTALL=1 STATE=$WORK/s2 SKIP_SYSTEMD=1 UNIT_DIR=$WORK TRUST_STATE=$WORK/cust_trust.json \
  bash "$HERE/deploy_customer_trust.sh" > d2.out 2>&1 || { cat d2.out; exit 1; }
cp conf.nft conf.pre-closed
CONF=conf.nft SKIP_INSTALL=1 bash "$HERE/deploy_customer_closed.sh" > d3.out 2>&1 || { cat d3.out; exit 1; }
grep -E "\[(vivo|orden|conf)\]" d3.out
OUT=$(CONF=conf.nft SKIP_INSTALL=1 bash "$HERE/deploy_customer_closed.sh")
case "$OUT" in *"[vivo] ya estaba"*"[conf] ya estaba"*) echo "  ok  idempotente" ;; *) echo "FAIL idempotente"; exit 1 ;; esac

echo "-- 1) VM 1517 cerrada con lista $CLIENT y $C6/64"
must "trust-set ... closed" sh -c "python3 ub.py '$HERE/neuravps-unblock-ip' '$WORK' trust-set 1517 $CLIENT,2001:41d0:1:2::/64 closed | grep -q '\"closed\": true'"
expect $CPID $CLIENT $BASE 21517 ok
expect $CPID $CLIENT $BASE 31517 ok
expect $CPID $OTHER $BASE 21517 blocked
expect $CPID $OTHER $BASE 31517 blocked
expect $CPID $OTHER $BASE 21999 ok          # otra VM: reglas normales
expect $CPID $C6 $B6 21517 ok
expect $CPID $X6 $B6 21517 blocked
expect $CPID $X6 $B6 21999 ok
[ -n "${DEBUG:-}" ] && { nft list set ip6 rdpguard cust_closed_pub6; nft list set ip6 rdpguard cust_trust_pub6; nft list chain ip6 rdpguard pre; }
expect $CPID $C6 $PUB6 3389 ok              # IPv6 directa de la VM: su lista
expect $CPID $X6 $PUB6 3389 blocked
expect $JPID 64:ff9b:1::4d2a:314f $B6 21517 ok     # IPv4 ya decidido en la tabla ip (Jool)

echo "-- 2) infraestructura (bf_allow) entra; «Desbloquear mi IP» no abre una VM cerrada"
nft add element ip rdpguard bf_allow "{ $OTHER }"
expect $CPID $OTHER $BASE 21517 ok
nft delete element ip rdpguard bf_allow "{ $OTHER }"
nft add element ip rdpguard bf_auto "{ $OTHER }"
UB allow-vm $OTHER 1517 >/dev/null
expect $CPID $OTHER $BASE 21517 blocked
nft delete element ip rdpguard bf_auto "{ $OTHER }"

echo "-- 3) desmarcar: vuelve a las reglas normales"
UB trust-set 1517 $CLIENT,2001:41d0:1:2::/64 >/dev/null
expect $CPID $OTHER $BASE 21517 ok
expect $CPID $X6 $PUB6 3389 ok
echo "-- 4) cerrar sin lista: queda abierta"
must "closedRefused" sh -c "python3 ub.py '$HERE/neuravps-unblock-ip' '$WORK' trust-set 1517 - closed | grep -q closedRefused"
expect $CPID $OTHER $BASE 21517 ok
echo "-- 5) revoke-vm abre"
UB trust-set 1517 $CLIENT closed >/dev/null
expect $CPID $OTHER $BASE 21517 blocked
UB revoke-vm 1517 >/dev/null
expect $CPID $OTHER $BASE 21517 ok
echo "-- 6) trust-sync con closed"
echo "{\"gen\": $(( $(date +%s%6N) * 4 )), \"vms\": {\"1517\": [\"$CLIENT\"]}, \"closed\": [\"1517\"]}" | UB trust-sync >/dev/null
expect $CPID $OTHER $BASE 21517 blocked
expect $CPID $CLIENT $BASE 21517 ok
echo "-- 7) rollback del modo cerrado"
CONF=conf.nft SKIP_INSTALL=1 bash "$HERE/rollback_customer_closed.sh" > r.out 2>&1 || { cat r.out; exit 1; }
must "conf igual que antes del modo cerrado" cmp -s conf.pre-closed conf.nft
expect $CPID $OTHER $BASE 21517 ok
must "trust-set sigue funcionando sin los sets (closedApplied false)" sh -c "python3 ub.py '$HERE/neuravps-unblock-ip' '$WORK' trust-set 1517 $CLIENT closed | grep -q '\"closedApplied\": false'"
echo "NETNS_CLOSED_OK"
INNER
unshare -rn bash "$WORK/inner.sh" "$HERE" "$WORK"

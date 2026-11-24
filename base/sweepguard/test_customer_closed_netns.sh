#!/usr/bin/env bash
# Prueba de extremo a extremo del modo cerrado (deploy_customer_closed.sh +
# neuravps-unblock-ip trust-set ... closed) con nft y paquetes de verdad, sin
# root. Topología con reenvío real, como en las bases:
#   cliente(s) ──vb── BASE ──vv── VMs (1517 = 10.99.0.2 / ident ::5ed, 1999 = 10.99.0.3 / ::7cf)
#                        └─veth-host── «Jool» (64:ff9b:1::<VIP>)
# DNAT v4 por VIP+puerto, DNAT v6 por PUERTO (cualquier destino, como ip6 nat),
# IPv6 directa (public6 → ident) y SNAT de salida de la VM a su public6.
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

cat >> "$WORK/conf.nft" <<'NFT'
table ip nat {
    chain prerouting {
        type nat hook prerouting priority dstnat; policy accept;
        ip daddr 77.42.49.79 tcp dport 21517 dnat to 10.99.0.2:3389
        ip daddr 77.42.49.79 udp dport 21517 dnat to 10.99.0.2:3389
        ip daddr 77.42.49.79 tcp dport 31517 dnat to 10.99.0.2:22
        ip daddr 77.42.49.79 tcp dport 21999 dnat to 10.99.0.3:3389
        ip daddr 77.42.49.79 udp dport 21999 dnat to 10.99.0.3:3389
    }
    chain postrouting {
        type nat hook postrouting priority srcnat; policy accept;
        ct status dnat snat to ct original ip daddr
    }
}
table ip6 nat {
    chain direct_in {
        type nat hook prerouting priority -110; policy accept;
        ip6 daddr 2a01:4f9:aaaa::5ed dnat ip6 to 2a01:4f9:c01f:e::5ed
    }
    chain prerouting {
        type nat hook prerouting priority dstnat; policy accept;
        iifname { "vb", "veth-host" } tcp dport 21517 dnat ip6 to [2a01:4f9:c01f:e::5ed]:3389
        iifname { "vb", "veth-host" } tcp dport 21999 dnat ip6 to [2a01:4f9:c01f:e::7cf]:3389
    }
    chain postrouting {
        type nat hook postrouting priority srcnat; policy accept;
        oifname "vb" ip6 saddr 2a01:4f9:c01f:e::5ed snat ip6 to 2a01:4f9:aaaa::5ed
    }
}
NFT

cat > "$WORK/inner.sh" <<'INNER'
set -euo pipefail
HERE=$1; WORK=$2; cd "$WORK"
trap 'kill $(jobs -p) 2>/dev/null || true' EXIT
CLIENT=149.56.212.201; OTHER=149.56.212.202; BASE=77.42.49.79
C6=2001:41d0:1:2::5; X6=2001:41d0:9::5; B6=2a01:4f9:aaaa::2; PUB6=2a01:4f9:aaaa::5ed
ROUTED6=2a01:4f9:bbbb::99          # destino enrutado a la base que NO es local
ID1517=2a01:4f9:c01f:e::5ed; ID1999=2a01:4f9:c01f:e::7cf
nft -f conf.nft
ip link set lo up
sysctl -qw net.ipv4.ip_forward=1; sysctl -qw net.ipv6.conf.all.forwarding=1
mkns() { unshare -n sleep 900 >/dev/null 2>&1 & echo $!; }
CPID=$(mkns); VPID=$(mkns); JPID=$(mkns); sleep 0.3
ip link add vb type veth peer name vc netns /proc/$CPID/ns/net
ip link add vv type veth peer name vm netns /proc/$VPID/ns/net
ip link add veth-host type veth peer name vj netns /proc/$JPID/ns/net
ip addr add $BASE/24 dev vb; ip -6 addr add $B6/64 dev vb nodad; ip link set vb up
ip addr add 10.99.0.1/24 dev vv; ip -6 addr add 2a01:4f9:c01f:e::1/64 dev vv nodad; ip link set vv up
ip link set veth-host up; ip -6 addr add fd00::1/64 dev veth-host nodad
nsenter --net=/proc/$CPID/ns/net sh -c "ip link set lo up; ip addr add $CLIENT/24 dev vc; ip addr add $OTHER/24 dev vc; \
  ip -6 addr add $C6/64 dev vc nodad; ip -6 addr add $X6/64 dev vc nodad; ip link set vc up; \
  ip route add default dev vc; ip -6 route add default via $B6 dev vc onlink"
nsenter --net=/proc/$VPID/ns/net sh -c "ip link set lo up; ip addr add 10.99.0.2/24 dev vm; ip addr add 10.99.0.3/24 dev vm; \
  ip -6 addr add $ID1517/64 dev vm nodad; ip -6 addr add $ID1999/64 dev vm nodad; ip link set vm up; \
  ip route add default via 10.99.0.1; ip -6 route add default via 2a01:4f9:c01f:e::1"
nsenter --net=/proc/$JPID/ns/net sh -c "ip link set lo up; ip link set vj up; ip -6 addr add fd00::2/64 dev vj nodad; \
  ip -6 addr add 64:ff9b:1::4d2a:314f/128 dev lo; ip -6 route add default via fd00::1"
ip route add $CLIENT/32 dev vb; ip route add $OTHER/32 dev vb
ip -6 route add 2001:41d0:1:2::/64 dev vb; ip -6 route add 2001:41d0:9::/64 dev vb
ip -6 route add 64:ff9b:1::4d2a:314f/128 via fd00::2 dev veth-host
servers() {  # en la VM: TCP 3389/22 y UDP 3389 (eco)
  nsenter --net=/proc/$VPID/ns/net python3 - >/dev/null 2>&1 <<'PY' &
import socket, threading
def tcp(p):
    s = socket.socket(socket.AF_INET6); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0); s.bind(("::", p)); s.listen(64)
    while True:
        c, _ = s.accept(); c.close()
def udp(addr, p):    # una por dirección: así la respuesta sale de la misma dirección
    s = socket.socket(socket.AF_INET6 if ":" in addr else socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((addr, p))
    while True:
        d, a = s.recvfrom(100); s.sendto(b"pong", a)
for p in (3389, 22):
    threading.Thread(target=tcp, args=(p,), daemon=True).start()
for a in ("10.99.0.2", "10.99.0.3"):
    threading.Thread(target=udp, args=(a, 3389), daemon=True).start()
threading.Event().wait()
PY
  # en el «Internet» (cliente): un servicio al que la VM se conecta (salida)
  nsenter --net=/proc/$CPID/ns/net python3 - >/dev/null 2>&1 <<'PY' &
import socket
s = socket.socket(socket.AF_INET6); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("2001:41d0:9::5", 8443)); s.listen(16)
while True:
    c, _ = s.accept(); c.sendall(b"hola"); c.close()
PY
}
servers; sleep 0.8
probe() {   # probe <ns-pid> <proto> <src> <dst> <port> -> ok | blocked
  nsenter --net=/proc/$1/ns/net python3 - "$2" "$3" "$4" "$5" <<'PY'
import socket, sys
proto, src, dst, port = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
fam = socket.AF_INET6 if ":" in dst else socket.AF_INET
try:
    if proto == "tcp":
        s = socket.socket(fam); s.settimeout(1.5); s.bind((src, 0)); s.connect((dst, port))
        if port == 8443:
            assert s.recv(10) == b"hola"
    else:
        s = socket.socket(fam, socket.SOCK_DGRAM); s.settimeout(1.5); s.bind((src, 0))
        s.sendto(b"ping", (dst, port)); s.recvfrom(10)
    print("ok")
except (OSError, AssertionError):
    print("blocked")
PY
}
expect() { got=$(probe "$1" "$2" "$3" "$4" "$5"); [ "$got" = "$6" ] || { echo "FAIL $2 $3 -> $4:$5 = $got (esperaba $6)"; exit 1; }; echo "  ok  $2 $3 -> $4:$5 $got"; }
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
# calienta vecinos/ND
for d in $B6 $PUB6 $ROUTED6; do probe $CPID tcp $C6 $d 21999 >/dev/null; probe $CPID tcp $X6 $d 21999 >/dev/null; done
probe $CPID tcp $CLIENT $BASE 21999 >/dev/null; probe $CPID tcp $OTHER $BASE 21999 >/dev/null

echo "-- 0) sin modo cerrado todo llega (camino de reenvío real)"
expect $CPID tcp $OTHER $BASE 21517 ok
expect $CPID udp $OTHER $BASE 21517 ok
expect $CPID udp $OTHER $BASE 21999 ok
expect $CPID tcp $X6 $B6 21517 ok
expect $CPID tcp $X6 $PUB6 3389 ok
expect $VPID tcp $ID1517 2001:41d0:9::5 8443 ok          # salida v6 de la VM (SNAT a su public6)

echo "-- 1) despliegue: cust_allow, cust_trust y modo cerrado"
CONF=conf.nft SKIP_INSTALL=1 STATE=$WORK/s1 bash "$HERE/deploy_customer_allow.sh" > d1.out 2>&1 || { cat d1.out; exit 1; }
CONF=conf.nft SKIP_INSTALL=1 STATE=$WORK/s2 SKIP_SYSTEMD=1 UNIT_DIR=$WORK TRUST_STATE=$WORK/cust_trust.json \
  bash "$HERE/deploy_customer_trust.sh" > d2.out 2>&1 || { cat d2.out; exit 1; }
cp conf.nft conf.pre-closed
CONF=conf.nft SKIP_INSTALL=1 bash "$HERE/deploy_customer_closed.sh" > d3.out 2>&1 || { cat d3.out; exit 1; }
grep -E "\[(vivo|orden|conf)\]" d3.out
OUT=$(CONF=conf.nft SKIP_INSTALL=1 bash "$HERE/deploy_customer_closed.sh")
case "$OUT" in *"[vivo] ya estaba"*"[conf] ya estaba"*) echo "  ok  idempotente" ;; *) echo "FAIL idempotente"; exit 1 ;; esac

echo "-- 2) VM 1517 cerrada con lista $CLIENT y $C6/64"
must "trust-set ... closed" sh -c "python3 ub.py '$HERE/neuravps-unblock-ip' '$WORK' trust-set 1517 $CLIENT,2001:41d0:1:2::/64 closed | grep -q '\"closed\": true'"
expect $CPID tcp $CLIENT $BASE 21517 ok
expect $CPID udp $CLIENT $BASE 21517 ok
expect $CPID tcp $CLIENT $BASE 31517 ok
expect $CPID tcp $OTHER $BASE 21517 blocked
expect $CPID udp $OTHER $BASE 21517 blocked
expect $CPID tcp $OTHER $BASE 31517 blocked
expect $CPID tcp $OTHER $BASE 21999 ok                   # otra VM: reglas normales
expect $CPID udp $OTHER $BASE 21999 ok
expect $CPID tcp $C6 $B6 21517 ok
expect $CPID tcp $X6 $B6 21517 blocked
expect $CPID tcp $X6 $ROUTED6 21517 blocked              # destino enrutado no local: el DNAT v6 lo reenvía igual
expect $CPID tcp $X6 $B6 21999 ok
expect $CPID tcp $C6 $PUB6 3389 ok                       # IPv6 directa: su lista
expect $CPID tcp $X6 $PUB6 3389 blocked
expect $VPID tcp $ID1517 2001:41d0:9::5 8443 ok          # la salida v6 de la VM cerrada sigue funcionando
expect $VPID tcp 10.99.0.3 $BASE 21517 blocked           # VM 1999 -> VM 1517 por el VIP (TCP)
expect $VPID udp 10.99.0.3 $BASE 21517 blocked           # ... y UDP
expect $JPID tcp 64:ff9b:1::4d2a:314f $B6 21517 ok       # IPv4 ya decidido en la tabla ip (Jool)

echo "-- 3) infraestructura (bf_allow) entra; «Desbloquear mi IP» no abre una VM cerrada"
nft add element ip rdpguard bf_allow "{ $OTHER }"
expect $CPID tcp $OTHER $BASE 21517 ok
nft delete element ip rdpguard bf_allow "{ $OTHER }"
nft add element ip rdpguard bf_auto "{ $OTHER }"
UB allow-vm $OTHER 1517 >/dev/null
expect $CPID tcp $OTHER $BASE 21517 blocked
nft delete element ip rdpguard bf_auto "{ $OTHER }"

echo "-- 4) desmarcar: vuelve a las reglas normales"
UB trust-set 1517 $CLIENT,2001:41d0:1:2::/64 >/dev/null
expect $CPID tcp $OTHER $BASE 21517 ok
expect $CPID tcp $X6 $PUB6 3389 ok
echo "-- 5) cerrar sin lista: queda abierta"
must "closedRefused" sh -c "python3 ub.py '$HERE/neuravps-unblock-ip' '$WORK' trust-set 1517 - closed | grep -q closedRefused"
expect $CPID tcp $OTHER $BASE 21517 ok
echo "-- 6) revoke-vm abre"
UB trust-set 1517 $CLIENT closed >/dev/null
expect $CPID tcp $OTHER $BASE 21517 blocked
UB revoke-vm 1517 >/dev/null
expect $CPID tcp $OTHER $BASE 21517 ok
echo "-- 7) trust-sync con closed"
echo "{\"gen\": $(( $(date +%s%6N) * 4 )), \"vms\": {\"1517\": [\"$CLIENT\"]}, \"closed\": [\"1517\"]}" | UB trust-sync >/dev/null
expect $CPID tcp $OTHER $BASE 21517 blocked
expect $CPID tcp $CLIENT $BASE 21517 ok
echo "-- 8) rollback del modo cerrado"
CONF=conf.nft SKIP_INSTALL=1 bash "$HERE/rollback_customer_closed.sh" > r.out 2>&1 || { cat r.out; exit 1; }
must "conf igual que antes del modo cerrado" cmp -s conf.pre-closed conf.nft
expect $CPID tcp $OTHER $BASE 21517 ok
must "trust-set sigue funcionando sin los sets (closedApplied false)" sh -c "python3 ub.py '$HERE/neuravps-unblock-ip' '$WORK' trust-set 1517 $CLIENT closed | grep -q '\"closedApplied\": false'"
echo "NETNS_CLOSED_OK"
INNER
unshare -rn bash "$WORK/inner.sh" "$HERE" "$WORK"

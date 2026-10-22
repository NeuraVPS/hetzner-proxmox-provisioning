#!/usr/bin/env bash
# Prueba de extremo a extremo de cust_allow con nft y paquetes de verdad, sin
# root y sin tocar ninguna base: dos netns de usuario unidas por un veth.
#
#   base/sweepguard/test_customer_allow_netns.sh [nftables.conf]
#
# Sin argumento usa unas tablas rdpguard mínimas con la misma forma que las de
# b0/b1; con uno, carga ese fichero (p. ej. una copia de la conf de una base) y
# despliega encima con deploy_customer_allow.sh. Comprueba:
#   1. IP en bf_auto: no llega a nada;
#   2. allow-vm <ip> 1671 console: llega a 11671/21671/31671 (TCP), 21671 (UDP)
#      y 443, y NO a 21999 (otra VM) ni a 22;
#   3. status lo refleja y sweepguard.customer_allowed lee los pares;
#   4. rollback_customer_allow.sh lo deja como estaba (vuelve a no llegar).
# Necesita: unshare (util-linux), nft, ip (iproute2), python3.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
CONF_IN=${1:-}
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

if [ -z "$CONF_IN" ]; then
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
    chain pre {
        type filter hook prerouting priority -150; policy accept;
        ip6 saddr @bf_allow accept
        ip6 saddr @bf_auto counter drop comment "auto-blocked RDP port sweeper (expires)"
        tcp dport 20000-39999 tcp flags & (syn|ack) == syn add @bf_seen { ip6 saddr . tcp dport }
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
    chain pre {
        type filter hook prerouting priority raw; policy accept;
        ip saddr @bf_allow accept comment "trusted infra/ops — exempt from RDP guard"
        ip saddr @bf_static counter drop comment "confirmed RDP brute-force sources"
        ip saddr @bf_auto counter drop comment "auto-blocked RDP port sweeper (expires)"
        tcp dport 10000-39999 tcp flags & (syn|ack) == syn add @bf_seen { ip saddr . tcp dport } comment "record (source,port) for the sweep detector"
    }
}
NFT
else
  cp "$CONF_IN" "$WORK/conf.nft"
fi
cp "$WORK/conf.nft" "$WORK/conf.orig"

cat > "$WORK/inner.sh" <<'INNER'
set -euo pipefail
HERE=$1; WORK=$2
cd "$WORK"
CLIENT=47.194.157.202; BASE=77.42.49.79
nft -f conf.nft
ip link set lo up
# la otra punta (el cliente) en su propia netns, unida por un veth
unshare -n sleep 600 & CPID=$!
sleep 0.3
ip link add vb type veth peer name vc netns /proc/$CPID/ns/net
ip addr add $BASE/24 dev vb; ip link set vb up
nsenter --net=/proc/$CPID/ns/net sh -c "ip link set lo up; ip addr add $CLIENT/24 dev vc; ip link set vc up; ip route add default dev vc"
ip route add $CLIENT/32 dev vb
python3 - <<'PY' &
import socket, threading
def tcp(p):
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", p)); s.listen(16)
    while True:
        c, _ = s.accept(); c.close()
def udp(p):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(("0.0.0.0", p))
    while True:
        d, a = s.recvfrom(100); s.sendto(b"pong", a)
for p in (443, 22, 11671, 21671, 31671, 21999):
    threading.Thread(target=tcp, args=(p,), daemon=True).start()
threading.Thread(target=udp, args=(21671,), daemon=True).start()
threading.Event().wait()
PY
SPID=$!
sleep 0.5
probe() {   # probe <proto> <port> -> ok | blocked
  nsenter --net=/proc/$CPID/ns/net python3 - "$1" "$2" "$BASE" <<'PY'
import socket, sys
proto, port, base = sys.argv[1], int(sys.argv[2]), sys.argv[3]
try:
    if proto == "tcp":
        socket.create_connection((base, port), timeout=1).close()
    else:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(1)
        s.sendto(b"ping", (base, port)); s.recvfrom(10)
    print("ok")
except OSError:
    print("blocked")
PY
}
expect() { got=$(probe "$1" "$2"); [ "$got" = "$3" ] || { echo "FAIL $1/$2: $got (esperaba $3)"; exit 1; }; echo "  ok  $1/$2 $got"; }
UB() {  # el script restringido tal cual, con el cerrojo en el directorio de trabajo
  python3 - "$HERE/neuravps-unblock-ip" "$WORK/lock" "$@" <<'PY'
import importlib.machinery, importlib.util, sys
path, lock, *args = sys.argv[1:]
loader = importlib.machinery.SourceFileLoader("ub", path)
spec = importlib.util.spec_from_loader("ub", loader); ub = importlib.util.module_from_spec(spec)
loader.exec_module(ub); ub.LOCK = lock
sys.exit(ub.main(["neuravps-unblock-ip"] + args))
PY
}

echo "-- 0) sin bloqueo llega a todo"
expect tcp 21999 ok
CONF=conf.nft SKIP_INSTALL=1 bash "$HERE/deploy_customer_allow.sh" > deploy.out 2>&1 || { cat deploy.out; exit 1; }
grep -E "\[(vivo|orden|conf)\]" deploy.out
echo "-- 1) en bf_auto no llega a nada"
nft add element ip rdpguard bf_auto "{ $CLIENT }"
expect tcp 21671 blocked; expect tcp 443 blocked
UB status $CLIENT
echo "-- 2) allow-vm 1671 console"
UB allow-vm $CLIENT 1671 console
for p in 11671 21671 31671 443; do expect tcp $p ok; done
expect udp 21671 ok
expect tcp 21999 blocked; expect tcp 22 blocked
nft get element ip rdpguard bf_auto "{ $CLIENT }" >/dev/null && echo "  ok  sigue en bf_auto"
echo "-- 3) status y sweepguard"
UB status $CLIENT
python3 - "$HERE" <<'PY'
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("sg", os.path.join(sys.argv[1], "sweepguard.py"))
sg = importlib.util.module_from_spec(spec); spec.loader.exec_module(sg)
got = sg.customer_allowed("ip")
want = {("47.194.157.202", p) for p in (443, 11671, 21671, 31671)}
assert got == want, got
seen = {tuple(e["elem"]["val"]["concat"]) for e in sg.set_elements("ip", "bf_seen")
        if isinstance(e, dict) and "elem" in e}
assert not any(p in (11671, 21671, 31671) for _, p in seen), seen   # aceptado antes del registro
print("  ok  customer_allowed =", sorted(p for _, p in got), "; bf_seen sin sus puertos")
PY
echo "-- 4) rollback"
CONF=conf.nft SKIP_INSTALL=1 bash "$HERE/rollback_customer_allow.sh" > rb.out 2>&1 || { cat rb.out; exit 1; }
cmp conf.orig conf.nft && echo "  ok  conf identica"
expect tcp 21671 blocked
kill $SPID $CPID 2>/dev/null || true
echo "NETNS_OK"
INNER

unshare -rn bash "$WORK/inner.sh" "$HERE" "$WORK"

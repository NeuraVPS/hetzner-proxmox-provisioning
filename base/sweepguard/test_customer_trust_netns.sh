#!/usr/bin/env bash
# Prueba de extremo a extremo de cust_trust (IPs de confianza por VPS) con nft y
# paquetes de verdad, sin root y sin tocar ninguna base: dos netns de usuario
# unidas por un veth, v4 y v6.
#
#   base/sweepguard/test_customer_trust_netns.sh [nftables.conf]
#
# Sin argumento usa unas tablas rdpguard mínimas con la forma de las de b0/b1
# (bf_allow, bf_static, cust_allow, bf_auto, bf_seen, limitadores, nuestras4/6);
# con uno, carga ese fichero (p. ej. una copia de la conf de una base). Despliega
# encima deploy_customer_allow.sh y deploy_customer_trust.sh y comprueba:
#   1. trust-set 1302 <v4>,<v6>/64: con la IP en bf_auto llega a 11302/21302/
#      31302 (TCP) y 21302 (UDP), y NO a otra VM (21999);
#   2. fuera de bf_auto: 60 conexiones seguidas a 31302 pasan todas (sin
#      limitador) y no dejan rastro en bf_seen; a 21999 el limitador corta;
#   3. v6: otra dirección del MISMO /64 llega a 21302 estando en bf_auto, y no a 21999;
#   4. sweepguard lee los pares (Exempt, /64 incluido);
#   5. trust-check / trust-set rechazan bf_static y nuestras4/6; bf_static manda
#      sobre una IP ya de confianza;
#   6. revoke-vm 1302 quita la confianza; trust-sync (stdin) y trust-restore
#      vuelven a llenar el set desde el estado;
#   7. rollback_customer_trust.sh deja la conf como estaba.
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
else
  cp "$CONF_IN" "$WORK/conf.nft"
fi

cat > "$WORK/inner.sh" <<'INNER'
set -euo pipefail
HERE=$1; WORK=$2
cd "$WORK"
CLIENT=149.56.212.201; BASE=77.42.49.79
C6=2001:41d0:1:2::5; C6B=2001:41d0:1:2::6; B6=2a01:4f9:aaaa::2
nft -f conf.nft
ip link set lo up
unshare -n sleep 900 & CPID=$!
sleep 0.3
ip link add vb type veth peer name vc netns /proc/$CPID/ns/net
ip addr add $BASE/24 dev vb; ip -6 addr add $B6/64 dev vb nodad; ip link set vb up
nsenter --net=/proc/$CPID/ns/net sh -c "ip link set lo up; ip addr add $CLIENT/24 dev vc; \
  ip -6 addr add $C6/64 dev vc nodad; ip -6 addr add $C6B/64 dev vc nodad; ip link set vc up; \
  ip route add default dev vc; ip -6 route add default dev vc"
ip route add $CLIENT/32 dev vb; ip -6 route add 2001:41d0:1:2::/64 dev vb
python3 - <<'PY' &
import socket, threading
def tcp(p):
    s = socket.socket(socket.AF_INET6); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    s.bind(("::", p)); s.listen(128)
    while True:
        c, _ = s.accept(); c.close()
def udp(p):
    s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0); s.bind(("::", p))
    while True:
        d, a = s.recvfrom(100); s.sendto(b"pong", a)
for p in (11302, 21302, 31302, 21999, 31999):
    threading.Thread(target=tcp, args=(p,), daemon=True).start()
threading.Thread(target=udp, args=(21302,), daemon=True).start()
threading.Event().wait()
PY
SPID=$!
sleep 0.7
probe() {   # probe <proto> <port> [src] [dst] -> ok | blocked
  nsenter --net=/proc/$CPID/ns/net python3 - "$1" "$2" "${3:-$CLIENT}" "${4:-$BASE}" <<'PY'
import socket, sys
proto, port, src, dst = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
fam = socket.AF_INET6 if ":" in dst else socket.AF_INET
try:
    if proto == "tcp":
        s = socket.socket(fam); s.settimeout(1); s.bind((src, 0)); s.connect((dst, port)); s.close()
    else:
        s = socket.socket(fam, socket.SOCK_DGRAM); s.settimeout(1); s.bind((src, 0))
        s.sendto(b"ping", (dst, port)); s.recvfrom(10)
    print("ok")
except OSError:
    print("blocked")
PY
}
burst() {   # burst <port> <n> [src] [dst] -> conexiones que pasaron
  nsenter --net=/proc/$CPID/ns/net python3 - "$1" "$2" "${3:-$CLIENT}" "${4:-$BASE}" <<'PY'
import socket, sys
port, n, src, dst = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3], sys.argv[4]
fam = socket.AF_INET6 if ":" in dst else socket.AF_INET
ok = 0
for _ in range(n):
    s = socket.socket(fam); s.settimeout(0.3); s.bind((src, 0))
    try:
        s.connect((dst, port)); ok += 1
    except OSError:
        pass
    s.close()
print(ok)
PY
}
expect() { want=${!#}; got=$(probe "${@:1:$#-1}"); [ "$got" = "$want" ] || { echo "FAIL $*: $got"; exit 1; }; echo "  ok  $*"; }
cat > ub.py <<'PY'
import importlib.machinery, importlib.util, sys
path, work, *args = sys.argv[1:]
loader = importlib.machinery.SourceFileLoader("ub", path)
spec = importlib.util.spec_from_loader("ub", loader); ub = importlib.util.module_from_spec(spec)
loader.exec_module(ub); ub.LOCK = work + "/lock"; ub.TRUST_STATE = work + "/cust_trust.json"
sys.exit(ub.main(["neuravps-unblock-ip"] + args))
PY
# el script restringido tal cual (stdin pasa: trust-sync); nunca corta el test por rc != 0
UB() { python3 ub.py "$HERE/neuravps-unblock-ip" "$WORK" "$@" || true; }
jget() { python3 -c "import json,sys; d=json.loads(sys.stdin.read().splitlines()[-1]); print(eval(sys.argv[1], {}, {'d': d}))" "$1"; }

echo "-- 0) despliegue (cust_allow y cust_trust) en sbin de prueba"
mkdir -p sbin units
printf '# viejo sweepguard\n' > sbin/sweepguard.py
printf '# viejo unblock\n' > sbin/neuravps-unblock-ip
cp sbin/sweepguard.py sweepguard.viejo
CONF=conf.nft SKIP_INSTALL=1 STATE=$WORK/state.ca bash "$HERE/deploy_customer_allow.sh" > deploy0.out 2>&1 || { cat deploy0.out; exit 1; }
cp conf.nft conf.orig
CONF=conf.nft STATE=$WORK/state SBIN=$WORK/sbin UNIT_DIR=$WORK/units SKIP_SYSTEMD=1 SKIP_RESTORE=1 \
  bash "$HERE/deploy_customer_trust.sh" > deploy.out 2>&1 || { cat deploy.out; exit 1; }
grep -E "\[(vivo|orden|conf|py|unit)\]" deploy.out
grep -q "trust-restore" units/neuravps-cust-trust-restore.service && echo "  ok  unidad escrita"
OUT2=$(CONF=conf.nft STATE=$WORK/state SBIN=$WORK/sbin UNIT_DIR=$WORK/units SKIP_SYSTEMD=1 SKIP_RESTORE=1 \
  bash "$HERE/deploy_customer_trust.sh")
case "$OUT2" in *"[vivo] ya estaba"*"[conf] ya estaba"*) echo "  ok  idempotente" ;; *) echo "FAIL no idempotente"; exit 1 ;; esac

echo "-- 1) trust-set 1302 y la IP en bf_auto"
UB trust-set 1302 $CLIENT,2001:41d0:1:2::/64 | tee o1 | tail -1
[ "$(jget 'd["pairs"]' < o1)" = 6 ] && echo "  ok  6 pares"
nft add element ip rdpguard bf_auto "{ $CLIENT }"
for p in 11302 21302 31302; do expect tcp $p ok; done
expect udp 21302 ok
expect tcp 21999 blocked
echo "-- 2) fuera de bf_auto: sin limitador hacia SU VM, con limitador hacia otra"
nft delete element ip rdpguard bf_auto "{ $CLIENT }"
n=$(burst 31302 60); [ "$n" = 60 ] && echo "  ok  60/60 a 31302" || { echo "FAIL 31302 $n/60"; exit 1; }
n=$(burst 31999 60); [ "$n" -lt 30 ] && echo "  ok  $n/60 a 31999 (limitador)" || { echo "FAIL 31999 $n/60"; exit 1; }
S=$(nft list set ip rdpguard bf_seen)
case "$S" in *"$CLIENT . 31302"*) echo "FAIL 31302 en bf_seen"; exit 1 ;; esac
case "$S" in *"$CLIENT . 31999"*) echo "  ok  bf_seen: 31999 sí, 31302 no" ;; *) echo "FAIL sin 31999 en bf_seen"; exit 1 ;; esac
echo "-- 3) v6: otra dirección del mismo /64, en bf_auto"
expect tcp 21999 $C6B $B6 ok      # calienta el vecino v6 (ND) antes de medir
nft add element ip6 rdpguard bf_auto "{ $C6B }"
expect tcp 21302 $C6B $B6 ok
expect udp 21302 $C6B $B6 ok
expect tcp 21999 $C6B $B6 blocked
nft delete element ip6 rdpguard bf_auto "{ $C6B }"
n=$(burst 21302 40 $C6 $B6); [ "$n" = 40 ] && echo "  ok  v6 40/40 a 21302 (sin limitador @bf)" || { echo "FAIL v6 $n/40"; exit 1; }
echo "-- 4) sweepguard"
python3 - "$HERE" <<'PY'
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("sg", os.path.join(sys.argv[1], "sweepguard.py"))
sg = importlib.util.module_from_spec(spec); spec.loader.exec_module(sg)
ex = sg.Exempt(sg.customer_allowed("ip"), sg.customer_trusted("ip"))
ex6 = sg.Exempt(sg.customer_allowed("ip6"), sg.customer_trusted("ip6"))
assert ("149.56.212.201", 31302) in ex and ("149.56.212.201", 31999) not in ex
assert ("2001:41d0:1:2::abcd", 21302) in ex6 and ("2001:41d0:1:3::1", 21302) not in ex6
assert ("2001:41d0:1:2::abcd", 21999) not in ex6
print("  ok  Exempt v4 y v6 /64:", len(ex), len(ex6))
PY
echo "-- 5) rechazos: bf_static y nuestras"
UB trust-check 45.155.205.9 | tail -1 | grep -q '"reason": "static"' && echo "  ok  static"
UB trust-check 77.42.49.200 | tail -1 | grep -q '"reason": "ours"' && echo "  ok  ours v4"
UB trust-check 2a01:4f9:aaaa::/64 | tail -1 | grep -q '"reason": "ours"' && echo "  ok  ours v6"
UB trust-check 10.0.0.1 | tail -1 | grep -q bad_request && echo "  ok  privada"
UB trust-check 2001:41d0::/48 | tail -1 | grep -q bad_request && echo "  ok  /48"
UB trust-check 1.2.3.0/24 | tail -1 | grep -q bad_request && echo "  ok  rango v4"
UB trust-set 1302 $CLIENT,45.155.205.9,2001:41d0:1:2::/64,2001:41d0:1:2::7 > o5 || true
grep -q '"45.155.205.9": "static"' o5 && grep -q '"2001:41d0:1:2::7": "covered"' o5 && echo "  ok  trust-set filtra static y /128 cubierta"
nft add element ip rdpguard bf_static "{ $CLIENT }"
expect tcp 21302 blocked
nft delete element ip rdpguard bf_static "{ $CLIENT }"
echo "  ok  bf_static manda sobre la confianza"
echo "-- 6) revoke-vm, trust-sync, trust-restore, trust-status"
UB revoke-vm 1302 | tail -1 | grep -q '"trustRemoved": 1' && echo "  ok  revoke-vm quita la confianza"
nft add element ip rdpguard bf_auto "{ $CLIENT }"
expect tcp 21302 blocked
echo '{"vms": {"1302": ["149.56.212.201"], "1999": ["2001:41d0:1:2::/64"]}}' | UB trust-sync | tail -1 | tee o6
grep -q '"pairs": 6' o6 && echo "  ok  trust-sync 6 pares"
expect tcp 21302 ok
nft flush set ip rdpguard cust_trust; nft flush set ip6 rdpguard cust_trust
expect tcp 21302 blocked
UB trust-restore | tail -1 | grep -q '"pairs": 6' && echo "  ok  trust-restore"
expect tcp 21302 ok
UB trust-status | tail -1
echo '{"vms": {"1302": ["10.0.0.1"]}}' | UB trust-sync | tail -1 | grep -q '"rejectedCount": 1' && echo "  ok  sync descarta entradas malas"
echo 'basura' | UB trust-sync | tail -1 | grep -q bad_request && echo "  ok  sync rechaza carga no JSON"
echo "{\"vms\": {\"1302\": [\"$CLIENT\"]}}" | UB trust-sync >/dev/null
echo "garbage" > cust_trust.json
UB trust-set 1302 - | tail -1 | grep -q unreadable && echo "  ok  estado roto: no vacía el set"
expect tcp 21302 ok
nft delete element ip rdpguard bf_auto "{ $CLIENT }"
echo "-- 7) rollback"
CONF=conf.nft STATE=$WORK/state SBIN=$WORK/sbin UNIT_DIR=$WORK/units SKIP_SYSTEMD=1 \
  bash "$HERE/rollback_customer_trust.sh" > rb.out 2>&1 || { cat rb.out; exit 1; }
grep -E "\[(py|unit|vivo)\]|!!" rb.out
cmp conf.orig conf.nft && echo "  ok  conf idéntica"
cmp sbin/sweepguard.py sweepguard.viejo && echo "  ok  sweepguard.py restaurado"
nft list chain ip rdpguard pre | grep -q cust_trust && { echo "FAIL quedan reglas"; exit 1; }
[ ! -e units/neuravps-cust-trust-restore.service ] && echo "  ok  unidad quitada"
kill $SPID $CPID 2>/dev/null || true
echo "NETNS_TRUST_OK"
INNER

unshare -rn bash "$WORK/inner.sh" "$HERE" "$WORK"

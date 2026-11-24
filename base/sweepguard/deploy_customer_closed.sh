#!/usr/bin/env bash
# Modo cerrado por VPS (24/11/2026): si el cliente marca «Bloquear todas las
# conexiones desde IPs fuera de esta lista», a los puertos públicos de ESA VM
# solo llegan su lista de IPs de confianza y nuestra infraestructura.
#
# Dónde: una cadena `closed` en el hook FORWARD de cada tabla rdpguard, que solo
# mira las conexiones que la base ha DNATeado (`ct status dnat`) y solo en su
# sentido ORIGINAL (`ct direction original`), por el puerto que pidió el cliente
# (`ct original proto-dst`). Así:
#   * cubre exactamente los forwards, se llegue por la dirección que se llegue
#     (el DNAT v6 elige por puerto, no por destino), y la IPv6 directa (public6);
#   * no toca las respuestas de conexiones que inicia la propia VM (SNAT a su
#     public6 o a la base: van en sentido «reply») ni el ICMP relacionado;
#   * vale igual para TCP y UDP, y para el tráfico de VM a VM por el VIP.
#   ip  rdpguard closed:
#     ct direction original ct status dnat ct original ip saddr @bf_allow return
#     ct direction original ct status dnat ct original ip saddr . meta l4proto . ct original proto-dst @cust_trust return
#     ct direction original ct status dnat meta l4proto . ct original proto-dst @cust_closed drop
#   ip6 rdpguard closed: lo mismo con ip6, más
#     iifname "veth-host" ip6 saddr 64:ff9b:1::/96 return   (IPv4 de Jool: ya decidido en la tabla ip)
#     ... ct original ip6 daddr . ct reply ip6 saddr @cust_closed_pub6 ct original ip6 saddr . ct original ip6 daddr @cust_trust_pub6 return
#     ... ct original ip6 daddr . ct reply ip6 saddr @cust_closed_pub6 drop
#     (par «IPv6 directa . identidad de la VM»: solo si el DNAT llevó esa dirección a ESA VM)
# «Desbloquear mi IP» (cust_allow, en la cadena pre) no abre una VM cerrada: el
# drop de aquí va aparte. Requiere deploy_customer_trust.sh (usa @cust_trust).
# Los elementos los escribe neuravps-unblock-ip (trust-set ... closed / trust-sync).
# ⚠️ Vivo = UNA transacción. NUNCA se recarga /etc/nftables.conf.
# Deshacer: rollback_customer_closed.sh.  Pruebas: CONF=... SKIP_INSTALL=1
set -euo pipefail
CONF=${CONF:-/etc/nftables.conf}
SKIP_INSTALL=${SKIP_INSTALL:-0}
STATE=${STATE:-/var/lib/neuravps-sweepguard/cust_closed.deploy}
SBIN=${SBIN:-/usr/local/sbin}
HERE=$(cd "$(dirname "$0")" && pwd)
JOOL_IF=${JOOL_IF:-veth-host}
COM="modo cerrado del cliente: solo su lista e infraestructura (neuravps-unblock-ip)"
O="ct direction original ct status dnat"
RULES4=(
  "$O ct original ip saddr @bf_allow return"
  "$O ct original ip saddr . meta l4proto . ct original proto-dst @cust_trust return"
  "$O meta l4proto . ct original proto-dst @cust_closed drop"
)
# ip6: la IPv6 directa (public6, DNAT de inet neura_ipv6) se reconoce por su
# destino y solo se le aplica la política de SU VM. Los forwards por puerto se
# reconocen porque el DNAT de ip6 nat TRADUCE el puerto (2xxxx→3389, 1xxxx→445,
# 3xxxx→22: `ct reply proto-src` es el puerto traducido), mientras que el DNAT
# directo lo conserva. Así la lista o el cierre de la VM B (forward 21517) no
# alcanzan un servicio directo de la VM A en el 21517 de su public6, sin
# depender de qué direcciones haya asignadas.
FWD6="ct reply proto-src { 22, 445, 3389 }"
RULES6=(
  "iifname \"$JOOL_IF\" ip6 saddr 64:ff9b:1::/96 return"
  "$O ct original ip6 saddr @bf_allow return"
  "$O ct original ip6 daddr . ct reply ip6 saddr @cust_closed_pub6 ct original ip6 saddr . ct original ip6 daddr @cust_trust_pub6 return"
  "$O ct original ip6 daddr . ct reply ip6 saddr @cust_closed_pub6 drop"
  "$O $FWD6 ct original ip6 saddr . meta l4proto . ct original proto-dst @cust_trust return"
  "$O $FWD6 meta l4proto . ct original proto-dst @cust_closed drop"
)
CHAIN4="type filter hook forward priority -5; policy accept;"

echo "=== modo cerrado en $(hostname) ==="
for fam in ip ip6; do
  nft list set $fam rdpguard cust_trust >/dev/null 2>&1 || { echo "  !! falta $fam cust_trust (deploy_customer_trust.sh) — aborto"; exit 1; }
done
have() { nft list chain "$1" rdpguard closed >/dev/null 2>&1; }
if have ip && have ip6; then
  echo "  [vivo] ya estaba"
else
  if have ip || have ip6 || nft list set ip rdpguard cust_closed >/dev/null 2>&1 \
     || nft list set ip6 rdpguard cust_closed >/dev/null 2>&1; then
    echo "  !! estado a medias — revisa a mano, no toco nada"; exit 1
  fi
  TX=$(mktemp --suffix=.nft /root/sg-cust-closed.XXXXXX 2>/dev/null || mktemp --suffix=.nft)
  {
    echo "add set ip rdpguard cust_closed { type inet_proto . inet_service; size 65536; }"
    echo "add chain ip rdpguard closed { $CHAIN4 }"
    for r in "${RULES4[@]}"; do echo "add rule ip rdpguard closed $r comment \"$COM\""; done
    echo "add set ip6 rdpguard cust_closed { type inet_proto . inet_service; size 65536; }"
    echo "add set ip6 rdpguard cust_closed_pub6 { type ipv6_addr . ipv6_addr; size 65536; }"
    echo "add set ip6 rdpguard cust_trust_pub6 { type ipv6_addr . ipv6_addr; flags interval; size 262144; }"
    echo "add chain ip6 rdpguard closed { $CHAIN4 }"
    for r in "${RULES6[@]}"; do echo "add rule ip6 rdpguard closed $r comment \"$COM\""; done
  } > "$TX"
  nft -c -f "$TX"; nft -f "$TX"; rm -f "$TX"
  echo "  [vivo] sets + cadena closed (forward) en las dos familias, en una transacción"
fi

python3 - "${#RULES4[@]}" "${#RULES6[@]}" <<'PY'
import subprocess, sys
want = {"ip": int(sys.argv[1]), "ip6": int(sys.argv[2])}
for fam in ("ip", "ip6"):
    out = subprocess.run(["nft", "list", "chain", fam, "rdpguard", "closed"], capture_output=True, text=True, check=True).stdout
    assert "hook forward" in out, f"{fam}: closed no cuelga de forward"
    rules = [l.strip() for l in out.splitlines() if "modo cerrado del cliente" in l]
    assert len(rules) == want[fam], f"{fam}: {len(rules)} reglas, esperaba {want[fam]}"
    body = [l.split(" comment ")[0] for l in rules]
    def at(s):
        return next(k for k, l in enumerate(body) if s in l)
    pd = at("meta l4proto . ct original proto-dst @cust_closed drop")
    assert at("@bf_allow") < at("proto-dst @cust_trust return") < pd, f"{fam}: orden de excepciones/drop"
    drops = [k for k, l in enumerate(body) if l.endswith(" drop")]
    if fam == "ip6":
        assert at("@bf_allow") < at("@cust_trust_pub6 return") < at("@cust_closed_pub6 drop") \
            < at("proto-dst @cust_trust return"), "ip6: la IPv6 directa debe ir antes de los puertos"
        assert all("ct reply proto-src" in l for l in body if "proto-dst" in l), "ip6: forward sin el puerto traducido"
    assert all("ct direction original ct status dnat" in rules[k] for k in drops), f"{fam}: drop sin acotar a DNAT/original"
    print(f"  [orden] {fam}: closed (forward): bf_allow/lista -> drop solo en DNAT y sentido original OK")
PY

CONF_STATE=$(python3 - "$CONF" <<'PY'
import sys
s = open(sys.argv[1]).read()
def block(head):
    i = s.index(head); rest = s[i + len(head):]
    c = [k for k in (rest.find("\ntable "), rest.find("\ninclude ")) if k >= 0]
    return s[i:i + len(head) + (min(c) if c else len(rest))]
b4, b6 = block("table ip rdpguard {"), block("table ip6 rdpguard {")
got = [("set cust_closed {" in b4) + ("chain closed {" in b4),
       ("set cust_closed {" in b6) + ("set cust_closed_pub6 {" in b6) + ("set cust_trust_pub6 {" in b6)
       + ("chain closed {" in b6)]
print("complete" if got == [2, 4] else "absent" if got == [0, 0] else "partial")
PY
)
if [ "$CONF_STATE" = complete ]; then echo "  [conf] ya estaba"
elif [ "$CONF_STATE" = partial ]; then echo "  !! $CONF con el modo cerrado a medias — revisa a mano"; exit 1
else
  BK="$CONF.bak.custclosed.$(date +%Y%m%d-%H%M%S)"; NEW="$CONF.custclosed.new"
  cp -a "$CONF" "$BK"; cp -a "$CONF" "$NEW"
  R4=$(printf '%s\n' "${RULES4[@]}"); R6=$(printf '%s\n' "${RULES6[@]}")
  python3 - "$NEW" "$COM" "$R4" "$R6" <<'PY'
import sys
conf, com, r4, r6 = sys.argv[1:]
s = open(conf).read()
def block(s, head):
    i = s.index(head); rest = s[i + len(head):]
    c = [k for k in (rest.find("\ntable "), rest.find("\ninclude ")) if k >= 0]
    return i, i + len(head) + (min(c) if c else len(rest))
def patch(s, head, sets, rules):
    i, j = block(s, head); b = s[i:j]
    assert "cust_closed" not in b and "chain closed" not in b, head
    k = b.rindex("}")                       # cierre de la tabla
    chain = ("    # modo cerrado por VPS (deploy_customer_closed.sh); elementos de neuravps-unblock-ip\n"
             + "".join(sets) + "    chain closed {\n"
             + "        type filter hook forward priority -5; policy accept;\n"
             + "".join(f"        {r} comment \"{com}\"\n" for r in rules.splitlines() if r)
             + "    }\n")
    b = b[:k] + chain + b[k:]
    return s[:i] + b + s[j:]
S = "    set {} {{\n        type {}\n{}        size {}\n    }}\n"
s = patch(s, "table ip rdpguard {", [S.format("cust_closed", "inet_proto . inet_service", "", 65536)], r4)
s = patch(s, "table ip6 rdpguard {", [S.format("cust_closed", "inet_proto . inet_service", "", 65536),
                                     S.format("cust_closed_pub6", "ipv6_addr . ipv6_addr", "", 65536),
                                     S.format("cust_trust_pub6", "ipv6_addr . ipv6_addr", "        flags interval\n", 262144)], r6)
open(conf, "w").write(s)
print("  [conf] sets y cadena closed escritos (fichero hermano)")
PY
  if nft -c -f "$NEW"; then mv -f "$NEW" "$CONF"; echo "  [conf] sintaxis OK (no cargada); publicada con mv; copia en $BK"
  else rm -f "$NEW"; echo "  !! conf nueva no valida — sin tocar"; exit 1; fi
fi

if [ "$SKIP_INSTALL" = 1 ]; then echo "  [py] SKIP_INSTALL=1"
else
  src=$HERE/neuravps-unblock-ip; dst=$SBIN/neuravps-unblock-ip
  if cmp -s "$src" "$dst"; then echo "  [py] ya instalado"
  else
    if [ -e "$dst" ] && ! grep -q cust_closed "$dst"; then
      cp -a "$dst" "$dst.bak.custclosed.$(date +%Y%m%d-%H%M%S)"; mkdir -p "$(dirname "$STATE")"
      echo "$dst=$(ls -t "$dst".bak.custclosed.* | head -1)" > "$STATE"
    fi
    python3 - "$src" <<'PY' || { echo "  !! no compila"; exit 1; }
import sys; compile(open(sys.argv[1]).read(), sys.argv[1], "exec")
PY
    install -m 0755 -o "$(id -u)" -g "$(id -g)" "$src" "$dst"; echo "  [py] $dst instalado ($(md5sum < "$dst" | cut -c1-8))"
  fi
  "$SBIN/neuravps-unblock-ip" trust-restore | sed 's/^/  [estado] /'
fi
echo "CUSTCLOSED_OK $(hostname)"

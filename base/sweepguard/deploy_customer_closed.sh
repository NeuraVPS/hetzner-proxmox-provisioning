#!/usr/bin/env bash
# Modo cerrado por VPS (24/11/2026): si el cliente marca «Bloquear todas las
# conexiones desde IPs fuera de esta lista», a los puertos públicos de ESA VM
# solo llegan su lista de IPs de confianza y nuestra infraestructura.
#
# Qué crea (requiere deploy_customer_trust.sh; ancla en las reglas de cust_trust
# y cust_allow):
#   ip  rdpguard: set cust_closed (inet_proto . inet_service) y, justo DESPUÉS de
#       cust_trust (que ya aceptó la lista) y ANTES de cust_allow:
#         fib daddr type local meta l4proto . th dport @cust_closed drop
#   ip6 rdpguard: cust_closed, cust_closed_pub6 (IPv6 pública directa de cada VM
#       cerrada) y cust_trust_pub6 (origen de su lista . esa IPv6), y en el mismo
#       sitio:
#         ip6 daddr @cust_closed_pub6 ip6 saddr . ip6 daddr @cust_trust_pub6 accept
#         ip6 daddr @cust_closed_pub6 drop
#         iifname != "veth-host" fib daddr type local meta l4proto . th dport @cust_closed drop
#       (lo que vuelve de Jool por veth-host es IPv4 ya decidido en la tabla ip).
# Queda: bf_allow (bases, DEVEL) → [invitado] → bf_static → cust_trust →
# CERRADO → cust_allow → bf_auto → ...  «Desbloquear mi IP» (cust_allow) no abre
# una VM cerrada. La salida de VM a VM por el VIP (origen 10.64.x) también se
# descarta: no se puede poner en la lista.
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
R4="fib daddr type local meta l4proto . th dport @cust_closed drop"
R6A="ip6 daddr @cust_closed_pub6 ip6 saddr . ip6 daddr @cust_trust_pub6 accept"
R6B="ip6 daddr @cust_closed_pub6 drop"
R6C="iifname != \"$JOOL_IF\" fib daddr type local meta l4proto . th dport @cust_closed drop"

echo "=== modo cerrado en $(hostname) ==="
CH4=$(nft list chain ip rdpguard pre); CH6=$(nft list chain ip6 rdpguard pre)
has() { case "$1" in *"$2"*) return 0 ;; *) return 1 ;; esac; }
for c in "$CH4" "$CH6"; do has "$c" "@cust_trust accept" || { echo "  !! falta cust_trust (deploy_customer_trust.sh) — aborto"; exit 1; }; done
if has "$CH4" "@cust_closed drop" && has "$CH6" "@cust_closed drop" && has "$CH6" "@cust_closed_pub6 drop"; then
  echo "  [vivo] ya estaba"
else
  if has "$CH4" "@cust_closed" || has "$CH6" "@cust_closed" || nft list set ip rdpguard cust_closed >/dev/null 2>&1 \
     || nft list set ip6 rdpguard cust_closed >/dev/null 2>&1; then
    echo "  !! estado a medias — revisa a mano, no toco nada"; exit 1
  fi
  A4=$(nft -a list chain ip rdpguard pre); A6=$(nft -a list chain ip6 rdpguard pre)
  H4=$(awk '/@cust_allow accept/{print $NF; exit}' <<<"$A4"); H6=$(awk '/@cust_allow accept/{print $NF; exit}' <<<"$A6")
  [ -n "$H4" ] && [ -n "$H6" ] || { echo "  !! no encuentro la regla de cust_allow — aborto"; exit 1; }
  TX=$(mktemp --suffix=.nft /root/sg-cust-closed.XXXXXX 2>/dev/null || mktemp --suffix=.nft)
  cat > "$TX" <<NFT
add set ip rdpguard cust_closed { type inet_proto . inet_service; size 65536; }
insert rule ip rdpguard pre position $H4 $R4 comment "$COM"
add set ip6 rdpguard cust_closed { type inet_proto . inet_service; size 65536; }
add set ip6 rdpguard cust_closed_pub6 { type ipv6_addr; size 65536; }
add set ip6 rdpguard cust_trust_pub6 { type ipv6_addr . ipv6_addr; flags interval; size 262144; }
insert rule ip6 rdpguard pre position $H6 $R6A comment "$COM"
insert rule ip6 rdpguard pre position $H6 $R6B comment "$COM"
insert rule ip6 rdpguard pre position $H6 $R6C comment "$COM"
NFT
  nft -c -f "$TX"; nft -f "$TX"; rm -f "$TX"
  echo "  [vivo] sets + reglas en las dos familias, en una transacción"
fi

python3 - <<'PY'
import subprocess
for fam in ("ip", "ip6"):
    r = [l.strip() for l in subprocess.run(["nft", "list", "chain", fam, "rdpguard", "pre"],
         capture_output=True, text=True, check=True).stdout.splitlines()]
    i = lambda s: next(k for k, l in enumerate(r) if s in l)
    w, t, c, a, au = i("saddr @bf_allow accept"), i("@cust_trust accept"), i("@cust_closed drop"), \
        i("@cust_allow accept"), i("saddr @bf_auto counter")
    assert w < t < c < a < au, f"{fam}: orden incorrecto allow={w} trust={t} closed={c} cust_allow={a} auto={au}"
    assert "fib daddr type local" in r[c], f"{fam}: cerrado sin acotar el destino"
    if fam == "ip6":
        pa, pd = i("@cust_trust_pub6 accept"), i("@cust_closed_pub6 drop")
        assert t < pa < pd < a and 'iifname != "veth-host"' in r[c], "ip6: orden/NAT64 del cerrado"
    else:
        assert i("saddr @bf_static") < c, "ip: cerrado antes que bf_static"
    print(f"  [orden] {fam}: bf_allow -> ... -> cust_trust -> cerrado -> cust_allow -> bf_auto OK")
PY

CONF_STATE=$(python3 - "$CONF" <<'PY'
import sys
s = open(sys.argv[1]).read()
def block(head):
    i = s.index(head); rest = s[i + len(head):]
    c = [k for k in (rest.find("\ntable "), rest.find("\ninclude ")) if k >= 0]
    return s[i:i + len(head) + (min(c) if c else len(rest))]
b4, b6 = block("table ip rdpguard {"), block("table ip6 rdpguard {")
got = [("set cust_closed {" in b4) + ("@cust_closed drop" in b4),
       ("set cust_closed {" in b6) + ("set cust_closed_pub6 {" in b6) + ("set cust_trust_pub6 {" in b6)
       + ("@cust_closed drop" in b6) + ("@cust_closed_pub6 drop" in b6) + ("@cust_trust_pub6 accept" in b6)]
print("complete" if got == [2, 6] else "absent" if got == [0, 0] else "partial")
PY
)
if [ "$CONF_STATE" = complete ]; then echo "  [conf] ya estaba"
elif [ "$CONF_STATE" = partial ]; then echo "  !! $CONF con el modo cerrado a medias — revisa a mano"; exit 1
else
  BK="$CONF.bak.custclosed.$(date +%Y%m%d-%H%M%S)"; NEW="$CONF.custclosed.new"
  cp -a "$CONF" "$BK"; cp -a "$CONF" "$NEW"
  python3 - "$NEW" "$COM" "$R4" "$R6A" "$R6B" "$R6C" <<'PY'
import re, sys
conf, com, r4, r6a, r6b, r6c = sys.argv[1:]
s = open(conf).read()
def block(s, head):
    i = s.index(head); rest = s[i + len(head):]
    c = [k for k in (rest.find("\ntable "), rest.find("\ninclude ")) if k >= 0]
    return i, i + len(head) + (min(c) if c else len(rest))
def patch(s, head, sets, rules):
    i, j = block(s, head); b = s[i:j]
    assert "chain pre {" in b and "cust_closed" not in b, head
    b = b.replace("    chain pre {", "".join(sets) + "    chain pre {", 1)
    m = list(re.finditer(r"\n( +)[^\n]*@cust_allow accept[^\n]*\n", b))
    assert len(m) == 1, f"{head}: regla de cust_allow no única"
    m = m[0]
    ins = "".join(f"{m.group(1)}{r} comment \"{com}\"\n" for r in rules)
    b = b[:m.start() + 1] + ins + b[m.start() + 1:]
    return s[:i] + b + s[j:]
hdr = "    # modo cerrado por VPS (deploy_customer_closed.sh); elementos de neuravps-unblock-ip\n"
s = patch(s, "table ip rdpguard {",
          [hdr + "    set cust_closed {\n        type inet_proto . inet_service\n        size 65536\n    }\n"], [r4])
s = patch(s, "table ip6 rdpguard {",
          [hdr + "    set cust_closed {\n        type inet_proto . inet_service\n        size 65536\n    }\n",
           "    set cust_closed_pub6 {\n        type ipv6_addr\n        size 65536\n    }\n",
           "    set cust_trust_pub6 {\n        type ipv6_addr . ipv6_addr\n        flags interval\n        size 262144\n    }\n"],
          [r6a, r6b, r6c])
open(conf, "w").write(s)
print("  [conf] sets y reglas escritos (fichero hermano)")
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

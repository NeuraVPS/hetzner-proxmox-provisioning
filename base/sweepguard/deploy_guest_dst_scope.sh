#!/usr/bin/env bash
# Vigilar a los invitados SOLO cuando van a NUESTRAS direcciones.
#
# Por qué (2026-09-18)
# --------------------
# Desde el 15/08 la salida de cada invitado cruza la base con origen 10.64.x.y
# (v4) o con su identidad 2a01:4f9:c01f:e::/64 (v6). El rdpguard solo miraba el
# PUERTO de destino, así que cualquier servicio de Internet en 10000-39999 se
# contaba como «máquina nuestra atacada»: el bróker MT de vm1261 en AWS Global
# Accelerator (214xx/220xx), RustDesk 21116, Syncthing 22000, un servicio de
# DigitalOcean en 25060… Del 05/09 al 18/09 los 113 bloqueos de invitados del
# journal iban TODOS a Internet; vm570 y vm581 se re-bloqueaban cada día. Y
# mientras dura el bloqueo, la VM pierde las conexiones NUEVAS a esos puertos:
# la reconexión a su bróker.
#
# Un forward solo existe en nuestras direcciones. Este script:
#   1. crea `nuestras4` / `nuestras6` (sets de intervalos) en cada tabla rdpguard;
#   2. añade UNA regla por familia, justo después de las exenciones que ya había:
#        ip  saddr 10.64.0.0/16         ip  daddr != @nuestras4 accept
#        ip6 saddr 2a01:4f9:c01f:e::/64 ip6 daddr != @nuestras6 accept
#      Lo que un invitado mande a nuestras IPs sigue pasando por los vetos, el
#      registro de bf_seen y los límites de ritmo: un invitado que barra
#      nuestros forwards se sigue cazando.
#   3. lo persiste en /etc/nftables.conf (acotado a cada tabla);
#   4. instala el sweepguard.py que filtra conntrack por el mismo set;
#   5. borra de bf_seen las entradas de invitados que ya había (todas eran
#      salida a Internet: medido, 0 flujos de invitado hacia nuestras IPs).
#
# ⚠️ Vivo = UNA transacción `nft -f` (se crean set y regla a la vez o nada).
#    NUNCA se recarga /etc/nftables.conf: empieza por `flush ruleset` y tiraría
#    los túneles. El fichero solo se edita y se valida con `nft -c`.
#
# Deshacer: rollback_guest_dst_scope.sh (probado en b0/b1 el 2026-09-18).
#
# Variables para probarlo fuera de producción (netns):
#   CONF=/ruta/nftables.conf   SKIP_INSTALL=1
set -euo pipefail

CONF=${CONF:-/etc/nftables.conf}
SKIP_INSTALL=${SKIP_INSTALL:-0}
APPLIED=0
HERE=$(cd "$(dirname "$0")" && pwd)

# --- NUESTRAS direcciones ----------------------------------------------------
# Las mismas en las DOS bases: un invitado que sale por b1 hacia la IP principal
# de b0 llega a b0 con origen «IP principal de b1», que está en bf_allow — solo
# se le puede ver en la base por la que sale. Por eso van las dos principales.
#   IPs principales: b0 116.202.118.221 / 2a01:4f8:2b01:124::/64
#                    b1  95.216.102.179 / 2a01:4f9:2a:2d56::/64
#   VIPs:            FSN 94.130.3.118   / 2a01:4f8:fff2:95::/64
#                    HEL 77.42.49.79    / 2a01:4f9:fff1:5f::/64
#   Internos:        10.0.0.0/8 (10.0.0.0/24 jool, 10.64/16 invitados, 10.65/16 nodos)
#                    2a01:4f9:c01f:e::/64 (identidad de invitados), fd00::/8 (jool)
# ⚠️ Si se añade una IP con forwards (p.ej. los /26 de egress-ipv4-pools cuando
#    Hetzner los entregue), va AQUÍ y en vivo con `nft add element`.
NUESTRAS4="116.202.118.221, 95.216.102.179, 94.130.3.118, 77.42.49.79, 10.0.0.0/8, 95.217.93.0/26, 91.98.53.128/26"
NUESTRAS6="2a01:4f8:2b01:124::/64, 2a01:4f9:2a:2d56::/64, 2a01:4f8:fff2:95::/64, 2a01:4f9:fff1:5f::/64, 2a01:4f9:c01f:e::/64, fd00::/8"
RED4="10.64.0.0/16"
RED6="2a01:4f9:c01f:e::/64"
COM4="salida del invitado a Internet (destino no nuestro): fuera del guard"
COM6="salida v6 del invitado a Internet (destino no nuestro): fuera del guard"

echo "=== vigilar invitados solo hacia nuestras IPs en $(hostname) ==="

# --- 1) sesión viva: UNA transacción ------------------------------------------
# ⚠️ Nada de `nft ... | grep -q` aquí: con pipefail, grep sale al primer acierto,
# nft muere por SIGPIPE y la tubería da FALSO. Se lee entero a una variable.
CH4=$(nft list chain ip rdpguard pre)
CH6=$(nft list chain ip6 rdpguard pre)
has() { case "$1" in *"$2"*) return 0 ;; *) return 1 ;; esac; }
if has "$CH4" "@nuestras4" && has "$CH6" "@nuestras6"; then
  echo "  [vivo] ya estaba"
else
  # Anclas por handle. v4: la exención por puerto de 2026-08-15. v6: bf_allow.
  H4=$(nft -a list chain ip rdpguard pre | awk '/ip saddr 10\.64\.0\.0\/16 tcp dport != 10000-39999 accept/{print $NF; exit}')
  H6=$(nft -a list chain ip6 rdpguard pre | awk '/ip6 saddr @bf_allow accept/{print $NF; exit}')
  [ -n "$H4" ] || { echo "  !! no encuentro la exención v4 de 10.64 (deploy_guest_egress_exemption.sh) — aborto"; exit 1; }
  [ -n "$H6" ] || { echo "  !! no encuentro 'ip6 saddr @bf_allow accept' — aborto"; exit 1; }
  # ninguna de las dos puede estar a medias
  if has "$CH4" "@nuestras4" || has "$CH6" "@nuestras6" \
     || nft list set ip rdpguard nuestras4 >/dev/null 2>&1 || nft list set ip6 rdpguard nuestras6 >/dev/null 2>&1; then
    echo "  !! estado a medias (set o regla de una sola familia) — revisa a mano, no toco nada"; exit 1
  fi
  TX=$(mktemp --suffix=.nft /root/sg-dst-scope.XXXXXX)
  cat > "$TX" <<NFT
add set ip rdpguard nuestras4 { type ipv4_addr; flags interval; auto-merge; elements = { $NUESTRAS4 }; }
add rule ip rdpguard pre position $H4 ip saddr $RED4 ip daddr != @nuestras4 accept comment "$COM4"
add set ip6 rdpguard nuestras6 { type ipv6_addr; flags interval; auto-merge; elements = { $NUESTRAS6 }; }
add rule ip6 rdpguard pre position $H6 ip6 saddr $RED6 ip6 daddr != @nuestras6 accept comment "$COM6"
NFT
  nft -c -f "$TX"
  nft -f "$TX"
  APPLIED=1
  echo "  [vivo] sets + 2 reglas en una transacción ($TX)"
fi

# --- 2) persistencia (acotada a cada tabla) -------------------------------------
if grep -q "@nuestras4" "$CONF" && grep -q "@nuestras6" "$CONF"; then
  echo "  [conf] ya estaba"
else
  BK="$CONF.bak.dstscope.$(date +%Y%m%d-%H%M%S)"
  cp -a "$CONF" "$BK"
  echo "  [conf] copia en $BK"
  python3 - "$CONF" "$NUESTRAS4" "$NUESTRAS6" "$RED4" "$RED6" "$COM4" "$COM6" <<'PY'
import sys
conf, n4, n6, red4, red6, com4, com6 = sys.argv[1:]
s = open(conf).read()

def block(s, head):
    i = s.index(head)
    rest = s[i + len(head):]
    cands = [k for k in (rest.find("\ntable "), rest.find("\ninclude ")) if k >= 0]
    j = i + len(head) + (min(cands) if cands else len(rest))
    return i, j

def patch(s, head, setname, atype, elems, anchor, rule):
    i, j = block(s, head)
    b = s[i:j]
    assert b.count(anchor) == 1, f"{head}: ancla no única/ausente: {anchor.strip()}"
    assert "chain pre {" in b and setname not in b, f"{head}: sin chain pre o {setname} ya presente"
    setdef = (f"    # {setname}: NUESTRAS direcciones. Solo el tráfico de un invitado hacia\n"
              f"    # ellas puede ser un ataque a un forward (deploy_guest_dst_scope.sh).\n"
              f"    set {setname} {{\n        type {atype}\n        flags interval\n        auto-merge\n"
              f"        elements = {{ {elems} }}\n    }}\n\n")
    b = b.replace("    chain pre {", setdef + "    chain pre {", 1)
    b = b.replace(anchor, anchor + rule, 1)
    return s[:i] + b + s[j:]

a4 = ('        ip saddr 10.64.0.0/16 tcp dport != 10000-39999 accept comment '
      '"salida del invitado: fuera del guard (solo se le vigilan los forwards)"\n')
r4 = f'        ip saddr {red4} ip daddr != @nuestras4 accept comment "{com4}"\n'
a6 = '        ip6 saddr @bf_allow accept\n'
r6 = f'        ip6 saddr {red6} ip6 daddr != @nuestras6 accept comment "{com6}"\n'
s = patch(s, "table ip rdpguard {", "nuestras4", "ipv4_addr", n4, a4, r4)
s = patch(s, "table ip6 rdpguard {", "nuestras6", "ipv6_addr", n6, a6, r6)
open(conf, "w").write(s)
print("  [conf] sets y reglas escritos en las dos tablas rdpguard")
PY
  if nft -c -f "$CONF"; then
    echo "  [conf] sintaxis OK (solo comprobada, NO cargada)"
  else
    echo "  !! la conf no valida — restauro la copia"; cp -a "$BK" "$CONF"; exit 1
  fi
fi

# --- 3) sweepguard.py con filtro por destino -----------------------------------
if [ "$SKIP_INSTALL" = 1 ]; then
  echo "  [py] SKIP_INSTALL=1"
elif cmp -s "$HERE/sweepguard.py" /usr/local/sbin/sweepguard.py; then
  echo "  [py] ya instalado"
else
  # Copia SOLO del .py anterior a este cambio (sin GUARDED_SET): es al que
  # tiene que volver el rollback, no a una versión intermedia del mismo cambio.
  if ! grep -q GUARDED_SET /usr/local/sbin/sweepguard.py; then
    cp -a /usr/local/sbin/sweepguard.py "/usr/local/sbin/sweepguard.py.bak.dstscope.$(date +%Y%m%d-%H%M%S)"
  fi
  install -m 0755 "$HERE/sweepguard.py" /usr/local/sbin/sweepguard.py
  echo "  [py] instalado"
fi

# --- 4) olvidar lo que bf_seen ya había apuntado de invitados --------------------
# Todo era salida a Internet; si no, el detector 1 los seguiría contando hasta
# que caduquen (6 h). SOLO en la pasada que aplica el cambio: en una re-pasada
# borraría lo que la regla nueva sí registra (invitado -> nuestras IPs).
if [ "$APPLIED" = 1 ]; then
python3 - <<'PY'
import json, subprocess, ipaddress
nets = {"ip": ipaddress.ip_network("10.64.0.0/16"), "ip6": ipaddress.ip_network("2a01:4f9:c01f:e::/64")}
for fam, net in nets.items():
    out = subprocess.run(["nft", "-j", "list", "set", fam, "rdpguard", "bf_seen"], capture_output=True, text=True)
    if out.returncode:
        continue
    pairs = []
    for obj in json.loads(out.stdout).get("nftables", []):
        for e in (obj.get("set") or {}).get("elem", []) or []:
            v = e.get("elem", {}).get("val") if isinstance(e, dict) else None
            cat = v.get("concat") if isinstance(v, dict) else None
            if cat and ipaddress.ip_address(cat[0]) in net:
                pairs.append(f"{cat[0]} . {cat[1]}")
    for k in range(0, len(pairs), 200):
        subprocess.run(["nft", "delete", "element", fam, "rdpguard", "bf_seen",
                        "{ " + ", ".join(pairs[k:k + 200]) + " }"], check=False)
    print(f"  [bf_seen] {fam}: {len(pairs)} entradas de invitados borradas")
PY
else
  echo "  [bf_seen] no se toca (el cambio ya estaba)"
fi

echo "--- cadenas ---"
nft list chain ip rdpguard pre | sed 's/^/  /'
nft list chain ip6 rdpguard pre | sed 's/^/  /'
echo "DSTSCOPE_OK $(hostname)"

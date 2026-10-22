#!/usr/bin/env bash
# Excepciones por cliente: una IP bloqueada en `bf_auto` puede llegar a los
# puertos de SUS VMs, y solo a esos (22/10/2026).
#
# Por qué
# -------
# `bf_auto` corta TODO el tráfico de la IP hacia la base: sus VPS, los de los
# demás y la consola VNC. Un cliente bloqueado por error (22/10: SSH a sus dos
# VMs, rebloqueado cada 24 h) lo perdía todo. Con «Desbloquear mi IP» se quita
# la IP del set entero; el panel, al entrar, hace algo más acotado: mete en
# `cust_allow` los pares (IP . puerto) de las VMs del usuario y deja el resto
# de la base cerrado (neuravps-unblock-ip allow-vm, desde la Cloud Function).
#
# Este script:
#   1. crea `cust_allow` en las dos tablas rdpguard (addr . inet_service, con
#      timeout; los elementos llevan el suyo, 48 h);
#   2. inserta UNA regla por familia justo ANTES de la de `bf_auto`:
#        ip  saddr @bf_auto meta l4proto { tcp, udp } ip  saddr . th dport @cust_allow accept
#        ip6 saddr @bf_auto meta l4proto { tcp, udp } ip6 saddr . th dport @cust_allow accept
#      Solo actúa mientras la IP ESTÉ en `bf_auto`: fuera del bloqueo, su
#      tráfico sigue el camino normal (registro en `bf_seen` y límites de
#      ritmo). En v4 queda DESPUÉS de `bf_static` (un bloqueo manual manda: el
#      script restringido tampoco abre nada a una IP de bf_static). `th dport`
#      cubre TCP y UDP (el transporte UDP de RDP en 2xxxx);
#   3. lo persiste en /etc/nftables.conf, acotado a cada tabla;
#   4. instala sweepguard.py (no cuenta esos pares en ningún detector) y
#      neuravps-unblock-ip (status / allow-vm / revoke-vm), con copia de los
#      anteriores; las rutas de ESAS copias quedan en $STATE para que el
#      rollback vuelva exactamente a ellas.
#
# ⚠️ Vivo = UNA transacción `nft -f`. NUNCA se recarga /etc/nftables.conf:
#    empieza por `flush ruleset` y tiraría los túneles. Se edita y se valida
#    con `nft -c`. Los elementos de cust_allow NO se persisten (como bf_auto):
#    tras un reinicio el cliente los recupera al volver a entrar en el panel.
#
# Deshacer: rollback_customer_allow.sh.
#
# Variables para probarlo fuera de producción (netns):
#   CONF=/ruta/nftables.conf   SKIP_INSTALL=1   STATE=/ruta/estado   SBIN=/ruta/sbin
set -euo pipefail

CONF=${CONF:-/etc/nftables.conf}
SKIP_INSTALL=${SKIP_INSTALL:-0}
STATE=${STATE:-/var/lib/neuravps-sweepguard/cust_allow.deploy}
SBIN=${SBIN:-/usr/local/sbin}
HERE=$(cd "$(dirname "$0")" && pwd)
COM="cliente del panel: solo los puertos de SUS VMs (neuravps-unblock-ip allow-vm)"
SETDEF4="type ipv4_addr . inet_service; size 65536; flags timeout; timeout 2d;"
SETDEF6="type ipv6_addr . inet_service; size 65536; flags timeout; timeout 2d;"
RULE4="ip saddr @bf_auto meta l4proto { tcp, udp } ip saddr . th dport @cust_allow accept"
RULE6="ip6 saddr @bf_auto meta l4proto { tcp, udp } ip6 saddr . th dport @cust_allow accept"

echo "=== cust_allow en $(hostname) ==="

# --- 1) sesión viva: UNA transacción ------------------------------------------
# ⚠️ Nada de `nft ... | grep -q` con pipefail (SIGPIPE da falso): a variable.
CH4=$(nft list chain ip rdpguard pre)
CH6=$(nft list chain ip6 rdpguard pre)
has() { case "$1" in *"$2"*) return 0 ;; *) return 1 ;; esac; }
if has "$CH4" "@cust_allow" && has "$CH6" "@cust_allow"; then
  echo "  [vivo] ya estaba"
else
  if has "$CH4" "@cust_allow" || has "$CH6" "@cust_allow" \
     || nft list set ip rdpguard cust_allow >/dev/null 2>&1 || nft list set ip6 rdpguard cust_allow >/dev/null 2>&1; then
    echo "  !! estado a medias (set o regla de una sola familia) — revisa a mano, no toco nada"; exit 1
  fi
  # Ancla: la regla de bf_auto de cada familia (por handle; insert = ANTES de ella).
  # Se lee entera a una variable: `nft | awk '{...; exit}'` con pipefail mata a
  # nft por SIGPIPE de vez en cuando y set -e aborta a mitad (visto en netns).
  A4=$(nft -a list chain ip rdpguard pre)
  A6=$(nft -a list chain ip6 rdpguard pre)
  H4=$(awk '/ip saddr @bf_auto .* drop/{print $NF; exit}' <<<"$A4")
  H6=$(awk '/ip6 saddr @bf_auto .* drop/{print $NF; exit}' <<<"$A6")
  [ -n "$H4" ] || { echo "  !! no encuentro la regla de bf_auto en ip rdpguard pre — aborto"; exit 1; }
  [ -n "$H6" ] || { echo "  !! no encuentro la regla de bf_auto en ip6 rdpguard pre — aborto"; exit 1; }
  TX=$(mktemp --suffix=.nft /root/sg-cust-allow.XXXXXX 2>/dev/null || mktemp --suffix=.nft)
  cat > "$TX" <<NFT
add set ip rdpguard cust_allow { $SETDEF4 }
insert rule ip rdpguard pre position $H4 $RULE4 comment "$COM"
add set ip6 rdpguard cust_allow { $SETDEF6 }
insert rule ip6 rdpguard pre position $H6 $RULE6 comment "$COM"
NFT
  nft -c -f "$TX"
  nft -f "$TX"
  rm -f "$TX"
  echo "  [vivo] set + regla en las dos familias, en una transacción"
fi

# --- comprobación de orden: bf_allow/bf_static < cust_allow < bf_auto -----------
python3 - <<'PY'
import subprocess
for fam, need_static in (("ip", True), ("ip6", False)):
    rules = [l.strip() for l in subprocess.run(["nft", "list", "chain", fam, "rdpguard", "pre"],
             capture_output=True, text=True, check=True).stdout.splitlines()]
    idx = lambda s, t="": next(i for i, l in enumerate(rules) if s in l and t in l)
    c, a, w = idx("@cust_allow accept"), idx("saddr @bf_auto", "drop"), idx("saddr @bf_allow accept")
    assert "saddr @bf_auto" in rules[c], f"{fam}: la regla de cust_allow no exige bf_auto"
    assert w < c < a, f"{fam}: orden incorrecto bf_allow={w} cust_allow={c} bf_auto={a}"
    if need_static:
        assert idx("saddr @bf_static") < c, f"{fam}: cust_allow antes que bf_static"
    print(f"  [orden] {fam}: bf_allow -> {'bf_static -> ' if need_static else ''}cust_allow -> bf_auto OK")
PY

# --- 2) persistencia (acotada a cada tabla) -------------------------------------
if grep -q "@cust_allow" "$CONF"; then
  echo "  [conf] ya estaba"
else
  BK="$CONF.bak.custallow.$(date +%Y%m%d-%H%M%S)"
  cp -a "$CONF" "$BK"
  echo "  [conf] copia en $BK"
  python3 - "$CONF" "$COM" "$RULE4" "$RULE6" <<'PY'
import re, sys
conf, com, rule4, rule6 = sys.argv[1:]
s = open(conf).read()

def block(s, head):
    i = s.index(head)
    rest = s[i + len(head):]
    cands = [k for k in (rest.find("\ntable "), rest.find("\ninclude ")) if k >= 0]
    j = i + len(head) + (min(cands) if cands else len(rest))
    return i, j

def patch(s, head, atype, saddr, text):
    i, j = block(s, head)
    b = s[i:j]
    assert "chain pre {" in b and "cust_allow" not in b, f"{head}: sin chain pre o cust_allow ya presente"
    anchors = re.findall(rf"\n( +){saddr} @bf_auto [^\n]*drop[^\n]*\n", b)
    assert len(anchors) == 1, f"{head}: regla de bf_auto no única/ausente"
    setdef = (f"    # cust_allow: (IP . puerto) que el panel abre a un cliente bloqueado,\n"
              f"    # solo hacia SUS VMs (deploy_customer_allow.sh). Caducan solos.\n"
              f"    set cust_allow {{\n        type {atype} . inet_service\n        size 65536\n"
              f"        flags timeout\n        timeout 2d\n    }}\n")
    b = b.replace("    chain pre {", setdef + "    chain pre {", 1)
    m = re.search(rf"\n( +){saddr} @bf_auto [^\n]*drop[^\n]*\n", b)
    rule = f"{m.group(1)}{text} comment \"{com}\"\n"
    b = b[:m.start() + 1] + rule + b[m.start() + 1:]
    return s[:i] + b + s[j:]

s = patch(s, "table ip rdpguard {", "ipv4_addr", "ip saddr", rule4)
s = patch(s, "table ip6 rdpguard {", "ipv6_addr", "ip6 saddr", rule6)
open(conf, "w").write(s)
print("  [conf] set y regla escritos en las dos tablas rdpguard")
PY
  if nft -c -f "$CONF"; then
    echo "  [conf] sintaxis OK (solo comprobada, NO cargada)"
  else
    echo "  !! la conf no valida — restauro la copia"; cp -a "$BK" "$CONF"; exit 1
  fi
fi

# --- 3) scripts ------------------------------------------------------------------
if [ "$SKIP_INSTALL" = 1 ]; then
  echo "  [py] SKIP_INSTALL=1"
else
  STAMP=$(date +%Y%m%d-%H%M%S)
  for pair in "sweepguard.py:$SBIN/sweepguard.py" "neuravps-unblock-ip:$SBIN/neuravps-unblock-ip"; do
    src=$HERE/${pair%%:*}; dst=${pair#*:}
    if cmp -s "$src" "$dst"; then
      echo "  [py] $dst ya instalado"; continue
    fi
    # copia SOLO de la versión anterior a este cambio, y su ruta en $STATE: a
    # ella vuelve el rollback (no a la más antigua que haya por ahí)
    if [ -e "$dst" ] && ! grep -q cust_allow "$dst"; then
      cp -a "$dst" "$dst.bak.custallow.$STAMP"
      mkdir -p "$(dirname "$STATE")"
      grep -v "^$dst=" "$STATE" 2>/dev/null > "$STATE.new" || true
      echo "$dst=$dst.bak.custallow.$STAMP" >> "$STATE.new"
      mv "$STATE.new" "$STATE"
    fi
    python3 -m py_compile "$src" 2>/dev/null || { echo "  !! $src no compila"; exit 1; }
    install -m 0755 -o root -g root "$src" "$dst"
    echo "  [py] $dst instalado ($(md5sum < "$dst" | cut -c1-8))"
  done
fi

echo "--- cadenas ---"
nft list chain ip rdpguard pre | sed 's/^/  /'
nft list chain ip6 rdpguard pre | sed 's/^/  /'
echo "CUSTALLOW_OK $(hostname)"

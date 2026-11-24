#!/usr/bin/env bash
# IPs de confianza por VPS: una IP que el cliente fija en el panel para UNA VM
# no se filtra hacia los puertos de esa VM (24/11/2026).
#
# Por qué
# -------
# sweepguard bloquea por IP de origen y el bloqueo (`bf_auto`) corta TODO lo de
# esa IP hacia la base. Un cliente con un servidor de automatización que abre
# ~10 SSH a la vez a SU VPS (24/11, hot-port) perdía todos sus VPS. Ahora puede
# declarar esa IP «de confianza» para ese VPS (functions/trusted_ips.py →
# neuravps-unblock-ip trust-set) y la base la deja pasar SOLO hacia los puertos
# de esa VM (SMB 1xxxx, RDP 2xxxx tcp+udp, SSH 3xxxx). Hacia las demás VMs,
# reglas normales.
#
# Este script:
#   1. crea `cust_trust` en las dos tablas rdpguard: v4 `ipv4_addr .
#      inet_proto . inet_service`, v6 igual con `flags interval` (admite /64).
#      Sin caducidad. Por entrada: TCP a 1xxxx/2xxxx/3xxxx y UDP solo a 2xxxx;
#   2. inserta UNA regla por familia justo ANTES de la de `cust_allow`:
#        fib daddr type local ip  saddr . meta l4proto . th dport @cust_trust accept
#        fib daddr type local ip6 saddr . meta l4proto . th dport @cust_trust accept
#      (solo destinos locales de la base: main y VIPs, donde están los forwards)
#      Así queda DESPUÉS de bf_allow, de las reglas del invitado y de
#      `bf_static` (un bloqueo manual manda) y ANTES de cust_allow, bf_auto,
#      bf_seen y los limitadores (bf_src, bf_port, bf). Aunque la IP acabe en
#      bf_auto por lo que haga contra OTRAS VMs, sigue llegando a la suya;
#   3. lo persiste en /etc/nftables.conf, acotado a cada tabla;
#   4. instala sweepguard.py (no cuenta esos pares) y neuravps-unblock-ip
#      (trust-*), con copia de los anteriores (rutas en $STATE para el rollback);
#   5. instala neuravps-cust-trust-restore.service: al arrancar, tras
#      nftables.service, vuelve a llenar el set desde
#      /var/lib/neuravps-sweepguard/cust_trust.json (los elementos no van en la
#      conf). La reconciliación horaria de NeuraVPS hace lo mismo desde Firestore.
#
# Requiere deploy_customer_allow.sh (ancla en la regla de cust_allow).
# ⚠️ Vivo = UNA transacción `nft -f`. NUNCA se recarga /etc/nftables.conf
#    (empieza por `flush ruleset` y tiraría los túneles).
# Deshacer: rollback_customer_trust.sh.
#
# Variables para probarlo fuera de producción (netns):
#   CONF=/ruta/nftables.conf  SKIP_INSTALL=1  STATE=/ruta/estado  SBIN=/ruta/sbin
#   UNIT_DIR=/ruta  SKIP_SYSTEMD=1  SKIP_RESTORE=1
set -euo pipefail

CONF=${CONF:-/etc/nftables.conf}
SKIP_INSTALL=${SKIP_INSTALL:-0}
SKIP_SYSTEMD=${SKIP_SYSTEMD:-0}
STATE=${STATE:-/var/lib/neuravps-sweepguard/cust_trust.deploy}
SBIN=${SBIN:-/usr/local/sbin}
UNIT_DIR=${UNIT_DIR:-/etc/systemd/system}
UNIT=neuravps-cust-trust-restore.service
HERE=$(cd "$(dirname "$0")" && pwd)
COM="IPs de confianza del cliente: solo los puertos de ESA VM (neuravps-unblock-ip trust-set)"
TRUST_STATE=${TRUST_STATE:-/var/lib/neuravps-sweepguard/cust_trust.json}
SETDEF4="type ipv4_addr . inet_proto . inet_service; size 262144;"
SETDEF6="type ipv6_addr . inet_proto . inet_service; flags interval; size 262144;"
# Solo hacia direcciones DE LA BASE (main y VIPs: donde viven los forwards). Las
# IPv6 públicas directas de cada VM (inet neura_ipv6, public6) no son locales y
# quedan fuera: el mismo número de puerto hacia otra VM no se exime.
RULE4="fib daddr type local ip saddr . meta l4proto . th dport @cust_trust accept"
RULE6="fib daddr type local ip6 saddr . meta l4proto . th dport @cust_trust accept"

echo "=== cust_trust en $(hostname) ==="

# --- 1) sesión viva: UNA transacción ------------------------------------------
# ⚠️ Nada de `nft ... | grep -q` con pipefail (SIGPIPE da falso): a variable.
CH4=$(nft list chain ip rdpguard pre)
CH6=$(nft list chain ip6 rdpguard pre)
has() { case "$1" in *"$2"*) return 0 ;; *) return 1 ;; esac; }
FRESH=0
if has "$CH4" "@cust_trust" && has "$CH6" "@cust_trust" \
   && nft list set ip rdpguard cust_trust >/dev/null 2>&1 && nft list set ip6 rdpguard cust_trust >/dev/null 2>&1; then
  echo "  [vivo] ya estaba"
else
  FRESH=1
  if has "$CH4" "@cust_trust" || has "$CH6" "@cust_trust" \
     || nft list set ip rdpguard cust_trust >/dev/null 2>&1 || nft list set ip6 rdpguard cust_trust >/dev/null 2>&1; then
    echo "  !! estado a medias (set o regla de una sola familia) — revisa a mano, no toco nada"; exit 1
  fi
  A4=$(nft -a list chain ip rdpguard pre)
  A6=$(nft -a list chain ip6 rdpguard pre)
  H4=$(awk '/@cust_allow accept/{print $NF; exit}' <<<"$A4")
  H6=$(awk '/@cust_allow accept/{print $NF; exit}' <<<"$A6")
  [ -n "$H4" ] || { echo "  !! no encuentro la regla de cust_allow en ip rdpguard pre (deploy_customer_allow.sh) — aborto"; exit 1; }
  [ -n "$H6" ] || { echo "  !! no encuentro la regla de cust_allow en ip6 rdpguard pre (deploy_customer_allow.sh) — aborto"; exit 1; }
  TX=$(mktemp --suffix=.nft /root/sg-cust-trust.XXXXXX 2>/dev/null || mktemp --suffix=.nft)
  cat > "$TX" <<NFT
add set ip rdpguard cust_trust { $SETDEF4 }
insert rule ip rdpguard pre position $H4 $RULE4 comment "$COM"
add set ip6 rdpguard cust_trust { $SETDEF6 }
insert rule ip6 rdpguard pre position $H6 $RULE6 comment "$COM"
NFT
  nft -c -f "$TX"
  nft -f "$TX"
  rm -f "$TX"
  echo "  [vivo] set + regla en las dos familias, en una transacción"
fi

# --- comprobación de orden ----------------------------------------------------
python3 - <<'PY'
import subprocess
for fam, need_static in (("ip", True), ("ip6", False)):
    rules = [l.strip() for l in subprocess.run(["nft", "list", "chain", fam, "rdpguard", "pre"],
             capture_output=True, text=True, check=True).stdout.splitlines()]
    def idx(s, t=""):
        return next(i for i, l in enumerate(rules) if s in l and t in l)
    w = idx("saddr @bf_allow accept")
    tr = idx("@cust_trust accept")
    ca = idx("@cust_allow accept")
    au = idx("saddr @bf_auto", "drop")
    seen = idx("add @bf_seen")
    assert "@bf_auto" not in rules[tr], f"{fam}: la regla de cust_trust no debe exigir bf_auto"
    assert "fib daddr type local" in rules[tr], f"{fam}: la regla de cust_trust no acota el destino"
    assert w < tr < ca < au < seen, f"{fam}: orden incorrecto allow={w} trust={tr} cust_allow={ca} auto={au} seen={seen}"
    lim = [i for i, l in enumerate(rules) if "limit rate" in l]
    assert all(tr < i for i in lim), f"{fam}: un limitador va antes que cust_trust"
    if need_static:
        assert idx("saddr @bf_static") < tr, f"{fam}: cust_trust antes que bf_static"
    print(f"  [orden] {fam}: bf_allow -> {'bf_static -> ' if need_static else ''}cust_trust -> cust_allow -> bf_auto -> bf_seen/limitadores OK")
PY

# --- 2) persistencia (acotada a cada tabla; fichero hermano + nft -c + mv) -------
CONF_STATE=$(python3 - "$CONF" <<'PY'
import sys
s = open(sys.argv[1]).read()
def block(head):
    i = s.index(head); rest = s[i + len(head):]
    c = [k for k in (rest.find("\ntable "), rest.find("\ninclude ")) if k >= 0]
    return s[i:i + len(head) + (min(c) if c else len(rest))]
got = [("set cust_trust {" in b) + ("@cust_trust accept" in b)
       for b in (block("table ip rdpguard {"), block("table ip6 rdpguard {"))]
print("complete" if got == [2, 2] else "absent" if got == [0, 0] else "partial")
PY
)
if [ "$CONF_STATE" = complete ]; then
  echo "  [conf] ya estaba"
elif [ "$CONF_STATE" = partial ]; then
  echo "  !! $CONF tiene cust_trust a medias (alguna tabla sin set o sin regla) — revisa a mano"; exit 1
else
  BK="$CONF.bak.custtrust.$(date +%Y%m%d-%H%M%S)"
  NEW="$CONF.custtrust.new"
  cp -a "$CONF" "$BK"
  cp -a "$CONF" "$NEW"
  echo "  [conf] copia en $BK"
  python3 - "$NEW" "$COM" "$RULE4" "$RULE6" <<'PY'
import re, sys
conf, com, rule4, rule6 = sys.argv[1:]
s = open(conf).read()

def block(s, head):
    i = s.index(head)
    rest = s[i + len(head):]
    cands = [k for k in (rest.find("\ntable "), rest.find("\ninclude ")) if k >= 0]
    j = i + len(head) + (min(cands) if cands else len(rest))
    return i, j

def patch(s, head, atype, flags, text):
    i, j = block(s, head)
    b = s[i:j]
    assert "chain pre {" in b and "cust_trust" not in b, f"{head}: sin chain pre o cust_trust ya presente"
    anchors = re.findall(r"\n( +)[^\n]*@cust_allow accept[^\n]*\n", b)
    assert len(anchors) == 1, f"{head}: regla de cust_allow no única/ausente (deploy_customer_allow.sh)"
    setdef = (f"    # cust_trust: IPs de confianza del cliente (IP . proto . puerto de UNA VM),\n"
              f"    # sin caducidad; elementos en cust_trust.json (deploy_customer_trust.sh).\n"
              f"    set cust_trust {{\n        type {atype} . inet_proto . inet_service\n"
              + (f"        flags {flags}\n" if flags else "")
              + f"        size 262144\n    }}\n")
    b = b.replace("    chain pre {", setdef + "    chain pre {", 1)
    m = re.search(r"\n( +)[^\n]*@cust_allow accept[^\n]*\n", b)
    rule = f"{m.group(1)}{text} comment \"{com}\"\n"
    b = b[:m.start() + 1] + rule + b[m.start() + 1:]
    return s[:i] + b + s[j:]

s = patch(s, "table ip rdpguard {", "ipv4_addr", "", rule4)
s = patch(s, "table ip6 rdpguard {", "ipv6_addr", "interval", rule6)
with open(conf, "w") as fh:
    fh.write(s)
    fh.flush()
print("  [conf] set y regla escritos en las dos tablas rdpguard (fichero hermano)")
PY
  if nft -c -f "$NEW"; then
    sync "$NEW" 2>/dev/null || true
    mv -f "$NEW" "$CONF"
    echo "  [conf] sintaxis OK (solo comprobada, NO cargada); publicada con mv"
  else
    rm -f "$NEW"; echo "  !! la conf nueva no valida — $CONF sin tocar"; exit 1
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
    if [ -e "$dst" ] && ! grep -q cust_trust "$dst"; then
      cp -a "$dst" "$dst.bak.custtrust.$STAMP"
      mkdir -p "$(dirname "$STATE")"
      grep -v "^$dst=" "$STATE" 2>/dev/null > "$STATE.new" || true
      echo "$dst=$dst.bak.custtrust.$STAMP" >> "$STATE.new"
      mv "$STATE.new" "$STATE"
    fi
    python3 - "$src" <<'PY' || { echo "  !! $src no compila"; exit 1; }
import sys; compile(open(sys.argv[1]).read(), sys.argv[1], "exec")
PY
    install -m 0755 -o "$(id -u)" -g "$(id -g)" "$src" "$dst"
    echo "  [py] $dst instalado ($(md5sum < "$dst" | cut -c1-8))"
  done
fi

# --- 4) restauración al arrancar -----------------------------------------------
cat > "$UNIT_DIR/$UNIT.new" <<UNITEOF
[Unit]
Description=NeuraVPS: rellena el set cust_trust (IPs de confianza por VPS) tras cargar nftables
After=nftables.service
Wants=nftables.service

[Service]
Type=oneshot
ExecStart=$SBIN/neuravps-unblock-ip trust-restore

[Install]
WantedBy=multi-user.target
UNITEOF
if cmp -s "$UNIT_DIR/$UNIT.new" "$UNIT_DIR/$UNIT"; then
  rm -f "$UNIT_DIR/$UNIT.new"; echo "  [unit] ya estaba"
else
  mv "$UNIT_DIR/$UNIT.new" "$UNIT_DIR/$UNIT"; echo "  [unit] $UNIT escrito"
fi
if [ "$SKIP_SYSTEMD" != 1 ]; then
  systemctl daemon-reload
  systemctl enable "$UNIT" >/dev/null 2>&1
  echo "  [unit] habilitado (no se arranca ahora)"
fi

# --- 5) el set ---------------------------------------------------------------------
# En un despliegue NUEVO (no había set vivo) un cust_trust.json que quede de antes
# (rollback, base reutilizada) puede traer confianza de VMs que cambiaron de dueño
# mientras tanto: se aparta y el set empieza vacío hasta la reconciliación de
# NeuraVPS (trusted_ips_reconcile, cada hora; o lanzarla a mano).
if [ "$FRESH" = 1 ] && [ -e "$TRUST_STATE" ]; then
  mv "$TRUST_STATE" "$TRUST_STATE.stale.$(date +%Y%m%d-%H%M%S)"
  echo "  [estado] cust_trust.json anterior apartado: se llena con la reconciliación"
fi
if [ "$SKIP_INSTALL" != 1 ] && [ "${SKIP_RESTORE:-0}" != 1 ]; then
  "$SBIN/neuravps-unblock-ip" trust-restore | sed 's/^/  [estado] /'
  "$SBIN/neuravps-unblock-ip" trust-status | sed 's/^/  [estado] /'
fi

echo "--- cadenas ---"
nft list chain ip rdpguard pre | sed 's/^/  /'
nft list chain ip6 rdpguard pre | sed 's/^/  /'
echo "CUSTTRUST_OK $(hostname)"

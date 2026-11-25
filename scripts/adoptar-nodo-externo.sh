#!/usr/bin/env bash
# Adopta en la flota un nodo Proxmox que NO se instalo con install.sh desde el
# rescate de Hetzner (primer caso: Kademar 0000266, 2x EPYC 9755, 25/11/2026).
#
# QUE HACE
# Deja el nodo como uno de la flota con el modelo de red vigente: tuneles ip6gre
# anclados a las VIPs de las bases, invitados en 10.64/16 + /64 de identidad por
# el tunel, aislamiento VM<->VM, clamp de MSS, perfil de memoria SQX, hookscript
# sync-dnat.py, guardas de RAM y extras de nodo SQX. Las piezas se sacan de
# install.sh y first_boot.sh de ESTE repo (heredocs entre comillas, extraidos
# literalmente), no de una copia: si cambian alli, cambian aqui.
#
# QUE NO HACE (a proposito)
#   - No pone la clave de flota ni /etc/firebase-credentials.json: es decision
#     del operador para un proveedor que no es Hetzner (ver el runbook).
#   - No toca el BootOrder: en Kademar el PXE es SU live de reinstalacion
#     (first_boot.sh lo pone primero en Hetzner; aqui seria un desastre).
#   - No instala sistema, discos ni swap: el nodo ya existe.
#   - No toca ninguna VM y se niega si hay VMs (salvo la fase que no las afecta).
#
# USO (en el nodo, con el repo copiado en $REPO; por fases, en este orden):
#   export REPO=/root/nvadopt/repo NODE_ID=266 NODE_NAME=0000266-KDM-9755 \
#          NODE_V6=2a0c:4ac1:3:8::2 UPLINK=bond0 TUN_MTU=1432
#   bash adoptar-nodo-externo.sh check      # solo lectura
#   bash adoptar-nodo-externo.sh hostname   # renombra el nodo PVE (sin VMs)
#   bash adoptar-nodo-externo.sh net        # ENI con vuelta atras a 5 min
#   bash adoptar-nodo-externo.sh confirm    # desde una sesion SSH NUEVA: cancela la vuelta atras
#   bash adoptar-nodo-externo.sh tunnels    # tuneles + clamp + aislamiento
#   bash adoptar-nodo-externo.sh node       # perfil, hooks, guardas, cortafuegos, extras SQX
#
# Runbook: NeuraVPS docs/KADEMAR_NODE.md (repo NeuraVPS).
set -u
PHASE="${1:-}"
: "${REPO:?falta REPO (copia de hetzner-proxmox-provisioning en el nodo)}"
: "${NODE_ID:?falta NODE_ID}"
: "${NODE_NAME:?falta NODE_NAME}"
: "${NODE_V6:?falta NODE_V6}"
UPLINK="${UPLINK:-bond0}"
TUN_MTU="${TUN_MTU:-1456}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
STATE=/root/nv-adopt
if [ "$PHASE" = check ]; then
  log() { echo "[$(date -u +%FT%TZ)] $*"; }        # check: solo lectura, no escribe estado
else
  mkdir -p "$STATE"
  log() { echo "[$(date -u +%FT%TZ)] $*" | tee -a "$STATE/adopt.log"; }
fi
die() { log "ERROR: $*"; exit 1; }

INSTALL_SH="$REPO/install.sh"
FIRST_BOOT="$REPO/first_boot.sh"
[ -r "$INSTALL_SH" ] && [ -r "$FIRST_BOOT" ] || die "no encuentro install.sh/first_boot.sh en $REPO"
[[ "$NODE_NAME" =~ ^0*${NODE_ID}- ]] || die "NODE_NAME ($NODE_NAME) no empieza por el NODE_ID ($NODE_ID) con ceros"

# Extrae literalmente un heredoc ENTRE COMILLAS ('EOF') cuyo destino es $2 en el
# fichero $1 (acepta el prefijo /mnt de install.sh). Falla si no hay exactamente
# uno o si sale vacio: nunca instalar un fichero a medias.
heredoc() { # heredoc <fichero> <ruta destino>
  python3 - "$1" "$2" <<'PY'
import re, sys
src, dst = sys.argv[1], sys.argv[2]
lines = open(src).read().split("\n")
pat = re.compile(r"^\s*cat\s*>\s*(?:/mnt)?" + re.escape(dst) + r"\s+<<'([A-Za-z_]+)'\s*$")
hits = [(i, m.group(1)) for i, l in enumerate(lines) for m in [pat.match(l)] if m]
if len(hits) != 1:
    sys.exit(f"heredoc {dst}: {len(hits)} coincidencias en {src}")
i, term = hits[0]
out = []
for l in lines[i + 1:]:
    if l == term:
        break
    out.append(l)
else:
    sys.exit(f"heredoc {dst}: sin terminador {term}")
if not "".join(out).strip():
    sys.exit(f"heredoc {dst}: vacio")
sys.stdout.write("\n".join(out) + "\n")
PY
}

# install <fichero fuente> <destino> <modo>, con copia del anterior.
put() {
  local src=$1 dst=$2 mode=$3
  if [ -e "$dst" ] && cmp -s "$src" "$dst"; then return 0; fi
  if [ -e "$dst" ]; then cp -a "$dst" "$STATE/$(echo "$dst" | tr / _).bak-$TS" || die "no pude respaldar $dst"; fi
  install -D -m "$mode" "$src" "$dst" || die "no pude instalar $dst"
  log "instalado $dst"
}
put_heredoc() { # put_heredoc <fichero> <destino> <modo>
  local tmp; tmp="$(mktemp)"
  heredoc "$1" "$2" > "$tmp" || { rm -f "$tmp"; die "no pude extraer $2"; }
  case "$2" in *.sh) bash -n "$tmp" || { rm -f "$tmp"; die "$2: sintaxis"; } ;; esac
  put "$tmp" "$2" "$3"; rm -f "$tmp"
}

# Guarda de invitados: qm y pct tienen que responder y estar vacios. Un fallo de
# qm NO cuenta como «0 VMs». Ademas el nodo tiene que ser independiente (sin
# corosync): renombrar o cambiar la config de cluster en un cluster es otra cosa.
require_no_guests() {
  local q c
  q="$(qm list 2>&1)" || die "qm list fallo: $q"
  c="$(pct list 2>&1)" || die "pct list fallo: $c"
  [ -z "$(printf '%s\n' "$q" | awk 'NR>1 && NF')" ] || die "hay VMs en el nodo; esta fase no se ejecuta con invitados"
  [ -z "$(printf '%s\n' "$c" | awk 'NR>1 && NF')" ] || die "hay contenedores en el nodo; esta fase no se ejecuta con invitados"
  [ ! -e /etc/pve/corosync.conf ] || die "el nodo esta en un cluster PVE (corosync.conf); no es un nodo independiente"
}

phase_check() {
  local bad=0
  log "== check (solo lectura)"
  echo "hostname: $(hostname)  -> $NODE_NAME"
  echo "VMs: $(qm list 2>/dev/null | awk 'NR>1 && NF' | wc -l)  CTs: $(pct list 2>/dev/null | awk 'NR>1 && NF' | wc -l)"
  ip link show "$UPLINK" >/dev/null 2>&1 && echo "uplink $UPLINK: OK" || { echo "uplink $UPLINK: NO EXISTE"; bad=1; }
  ip -6 addr show | grep -q "inet6 ${NODE_V6}/" && echo "NODE_V6 ya configurada" || echo "NODE_V6 aun no configurada"
  echo "ruta v4: $(ip -4 route show default)"
  echo "MTU v6 hacia la VIP FSN: $(ip -6 route get 2a01:4f8:fff2:95::2 2>/dev/null | grep -o 'mtu [0-9]*' || echo 'sin PMTU cacheado')"
  for f in /usr/local/sbin/neuravps-tunnels.sh /usr/local/sbin/nvx-mss.sh /var/lib/svz/snippets/sync-dnat.py \
           /etc/firebase-credentials.json /root/.ssh/neuravps_id; do
    [ -e "$f" ] && echo "presente: $f" || echo "falta:    $f"
  done
  for f in /usr/local/sbin/neuravps-tunnels.sh /usr/local/sbin/neuravps-tunnel-select.sh \
           /usr/local/sbin/neuravps-tunnel-probe.sh /usr/local/sbin/nvx-mss.sh /usr/local/sbin/nvx-aisla.sh \
           /usr/local/sbin/nvx-aisla-l3.sh; do
    heredoc "$INSTALL_SH" "$f" >/dev/null && echo "extraible: $f" || { echo "NO EXTRAIBLE: $f"; bad=1; }
  done
  [ -e /etc/network/interfaces.new ] && { echo "HAY /etc/network/interfaces.new pendiente: revisalo antes de net"; bad=1; }
  pveversion
  return "$bad"
}

# ---------------------------------------------------------------------------
phase_hostname() {
  log "== hostname"
  local old; old="$(hostname)"
  [ "$old" = "$NODE_NAME" ] && { log "ya se llama $NODE_NAME"; return 0; }
  require_no_guests
  [ -d /etc/pve/nodes ] || die "/etc/pve sin montar"
  ls /etc/pve/nodes | grep -qx "$old" || die "no encuentro /etc/pve/nodes/$old"
  cp -a /etc/hosts "$STATE/hosts.bak-$TS"; cp -a /etc/hostname "$STATE/hostname.bak-$TS"
  local v4; v4="$(ip -4 addr show dev "$UPLINK" scope global | sed -n 's#.*inet \([0-9.]*\)/.*#\1#p' | head -1)"
  # /etc/hosts: la entrada del nodo con su IPv6 de flota (como los de Hetzner) y la IPv4 propia.
  grep -v -E "[[:space:]]${old}([[:space:]]|$)" /etc/hosts | grep -v -E "[[:space:]]${NODE_NAME}([[:space:]]|$)" > "$STATE/hosts.new"
  echo "${NODE_V6} ${NODE_NAME}.neuravps.com ${NODE_NAME}" >> "$STATE/hosts.new"
  [ -n "$v4" ] && echo "${v4} ${NODE_NAME}.neuravps.com ${NODE_NAME}" >> "$STATE/hosts.new"
  install -m 644 "$STATE/hosts.new" /etc/hosts
  hostnamectl set-hostname "$NODE_NAME"
  echo "$NODE_NAME" > /etc/hostname
  systemctl restart pve-cluster && sleep 3
  systemctl restart pvedaemon pveproxy pvestatd pve-firewall 2>/dev/null || true
  for i in $(seq 1 20); do [ -d "/etc/pve/nodes/$NODE_NAME" ] && break; sleep 1; done
  [ -d "/etc/pve/nodes/$NODE_NAME" ] || die "PVE no ha creado /etc/pve/nodes/$NODE_NAME"
  # Lo que haya en el directorio viejo (sin VMs: host.fw, lrm_status...) pasa al nuevo, sin pisar.
  if [ -d "/etc/pve/nodes/$old" ]; then
    for f in host.fw; do
      [ -e "/etc/pve/nodes/$old/$f" ] && [ ! -e "/etc/pve/nodes/$NODE_NAME/$f" ] && cp "/etc/pve/nodes/$old/$f" "/etc/pve/nodes/$NODE_NAME/$f"
    done
    [ -z "$(ls -A "/etc/pve/nodes/$old/qemu-server" 2>/dev/null)" ] && [ -z "$(ls -A "/etc/pve/nodes/$old/lxc" 2>/dev/null)" ] \
      && rm -rf "/etc/pve/nodes/$old" && log "borrado /etc/pve/nodes/$old (vacio)"
  fi
  pvecm updatecerts --force >/dev/null 2>&1 || true
  systemctl restart pveproxy
  log "renombrado $old -> $NODE_NAME"
}

# ---------------------------------------------------------------------------
# ENI: IPv6 de flota estatica en el uplink (sin SLAAC: una sola direccion, la
# del tunel), vmbr0 como en install.sh SIN la parte de Hetzner (el /64 del
# nodo no se pone en vmbr0: los invitados van por el /64 de identidad), sin el
# NAT ni el DNAT de DNS del piloto.
phase_net() {
  log "== net"
  require_no_guests
  # Una sola transaccion a la vez: si hay una vuelta atras pendiente, no se pisa su copia.
  if [ -e "$STATE/net-pending" ] || systemctl is-active --quiet nv-adopt-net-rollback.timer; then
    die "hay una transaccion de red pendiente ($(cat "$STATE/net-pending" 2>/dev/null)); ejecuta confirm o espera a la vuelta atras"
  fi
  [ ! -e /etc/network/interfaces.new ] || die "hay /etc/network/interfaces.new (cambios de red pendientes de PVE); revisalo antes"
  local eni=/etc/network/interfaces snap="$STATE/net-snap-$TS" new="$STATE/interfaces.net-$TS"
  mkdir -p "$snap/sysctl.d" || die "no pude crear $snap"
  cp -a "$eni" "$snap/interfaces" || die "no pude respaldar el ENI"
  local f
  for f in 99-neuravps-ra.conf 99-neuravps-proxyarp.conf 99-neuravps-forwarding.conf 99-neuravps-uplink-ra.conf; do
    [ -e "/etc/sysctl.d/$f" ] && { cp -a "/etc/sysctl.d/$f" "$snap/sysctl.d/" || die "no pude respaldar $f"; }
  done
  ls "$snap/sysctl.d" > "$snap/sysctl.list"
  local mac; mac="$(printf '%s' "$NODE_V6" | sha256sum)"
  mac="02:${mac:0:2}:${mac:2:2}:${mac:4:2}:${mac:6:2}:${mac:8:2}"
  python3 - "$eni" "$new" "$UPLINK" "$NODE_V6" "$mac" <<'PY' || die "no pude generar el ENI nuevo"
import re, sys
src, dst, up, v6, mac = sys.argv[1:6]
s = open(src).read()
# Quita las stanzas de vmbr0 (inet e inet6) y la inet6 del uplink si la hubiera.
blocks = re.split(r"\n(?=(?:auto|iface|allow-hotplug|source)\b)", s)
keep = []
for b in blocks:
    h = b.split("\n", 1)[0].split()
    if len(h) >= 2 and h[0] in ("auto", "iface") and h[1] == "vmbr0":
        continue
    if len(h) >= 3 and h[0] == "iface" and h[1] == up and h[2] == "inet6":
        continue
    keep.append(b.rstrip("\n"))
out = "\n".join(keep).rstrip("\n") + "\n"
if f"iface {up} inet" not in out:
    sys.exit(f"no hay stanza 'iface {up} inet ...' en el ENI")
out += f"""
# IPv6 de flota del nodo (endpoint de los tuneles ip6gre hacia las VIPs). Fija y
# sin SLAAC: una sola direccion global. La ruta por defecto llega por RA
# (accept_ra 2 porque el nodo reenvia). adoptar-nodo-externo.sh
iface {up} inet6 static
\taddress {v6}/128
\tpost-up sysctl -qw net.ipv6.conf.{up}.accept_ra=2 net.ipv6.conf.{up}.autoconf=0

auto vmbr0
iface vmbr0 inet static
    hwaddress {mac}
    address 10.0.0.1/16
    bridge-disable-mac-learning 1
    bridge-ports none
    bridge-stp off
    bridge-fd 0
    post-up   echo 1 > /proc/sys/net/ipv4/ip_forward
    post-up   iptables -t raw -I PREROUTING -i fwbr+ -j CT --zone 1
    post-down iptables -t raw -D PREROUTING -i fwbr+ -j CT --zone 1
    post-up   ip addr add 10.64.255.1/16 dev vmbr0
    post-up   echo 1 > /proc/sys/net/ipv6/conf/all/forwarding
    post-up   ip -6 addr add fe80::1/64 dev vmbr0 || true
"""
open(dst, "w").write(out)
PY
  [ -s "$new" ] || die "no se genero el ENI nuevo"
  ifquery -a -i "$new" >/dev/null 2>&1 || die "ifquery rechaza el ENI nuevo ($new)"
  # Direcciones globales del uplink que no son NODE_V6: solo se quitan las SLAAC
  # (dinamicas). Una estatica sin clasificar para la fase: puede ser del operador.
  local extra_static
  extra_static="$(ip -6 addr show dev "$UPLINK" scope global permanent | sed -n 's#.*inet6 \([0-9a-f:]*\)/.*#\1#p' | grep -vx "$NODE_V6" || true)"
  [ -z "$extra_static" ] || die "el uplink tiene IPv6 estaticas que no son NODE_V6: $extra_static (clasificalas antes)"
  diff -u "$snap/interfaces" "$new" | tee "$STATE/interfaces.diff-$TS"
  # Vuelta atras: restaura el ENI y los sysctl tal cual estaban (incluidos los que
  # se borran) y quita los que esta fase crea. Se ARMA y se COMPRUEBA antes de
  # tocar nada.
  cat > "$STATE/net-rollback.sh" <<EOF
#!/bin/bash
for f in 99-neuravps-ra.conf 99-neuravps-proxyarp.conf 99-neuravps-forwarding.conf 99-neuravps-uplink-ra.conf; do
  if grep -qx "\$f" "$snap/sysctl.list"; then cp -a "$snap/sysctl.d/\$f" /etc/sysctl.d/; else rm -f "/etc/sysctl.d/\$f"; fi
done
cp -a "$snap/interfaces" /etc/network/interfaces
sysctl -q --system
ifreload -a
rm -f "$STATE/net-pending"
logger -t nv-adopt "red restaurada desde $snap (sin confirmar)"
EOF
  chmod 755 "$STATE/net-rollback.sh"
  systemctl reset-failed nv-adopt-net-rollback.timer nv-adopt-net-rollback.service 2>/dev/null || true
  systemd-run --unit nv-adopt-net-rollback --on-active=300 /bin/bash "$STATE/net-rollback.sh" >/dev/null \
    || die "no pude armar la vuelta atras; no toco la red"
  systemctl is-active --quiet nv-adopt-net-rollback.timer || die "el temporizador de vuelta atras no esta activo; no toco la red"
  echo "$TS $snap" > "$STATE/net-pending"
  log "vuelta atras armada (5 min): $STATE/net-rollback.sh (copia en $snap)"
  # --- a partir de aqui, cambios ---
  rm -f /etc/sysctl.d/99-neuravps-ra.conf /etc/sysctl.d/99-neuravps-proxyarp.conf
  put_heredoc "$INSTALL_SH" /etc/sysctl.d/99-neuravps-forwarding.conf 644
  printf 'net.ipv6.conf.%s.accept_ra = 2\nnet.ipv6.conf.%s.autoconf = 0\n' "$UPLINK" "$UPLINK" > /etc/sysctl.d/99-neuravps-uplink-ra.conf \
    || die "no pude escribir 99-neuravps-uplink-ra.conf"
  # Reglas EXACTAS del piloto que el ENI viejo puso en post-up (NAT de 10.64/16 por
  # el uplink y DNAT de los DNS de Hetzner a 1.1.1.1). Nada mas.
  iptables -t nat -D POSTROUTING -s 10.64.0.0/16 -o "$UPLINK" -j MASQUERADE 2>/dev/null
  local p d
  for p in udp tcp; do for d in 185.12.64.1 185.12.64.2; do
    while iptables -t nat -D PREROUTING -i vmbr0 -d "$d" -p "$p" -m "$p" --dport 53 -j DNAT --to-destination 1.1.1.1 2>/dev/null; do :; done
  done; done
  install -m 644 "$new" "$eni" || die "no pude instalar el ENI (la vuelta atras sigue armada)"
  sysctl -q -p /etc/sysctl.d/99-neuravps-forwarding.conf -p /etc/sysctl.d/99-neuravps-uplink-ra.conf
  ifreload -a || die "ifreload -a fallo (la vuelta atras restaurara la red en 5 min)"
  # Las SLAAC no se van solas al poner autoconf=0: se quitan solo las dinamicas.
  ip -6 addr show dev "$UPLINK" scope global dynamic | sed -n 's#.*inet6 \([0-9a-f:]*/[0-9]*\).*#\1#p' | while read -r a; do
    [ "${a%/*}" = "$NODE_V6" ] || { ip -6 addr del "$a" dev "$UPLINK" && log "quitada $a (SLAAC)"; }
  done
  sleep 3
  ip -6 addr show dev "$UPLINK" scope global | grep -q "inet6 ${NODE_V6}/128" || die "NODE_V6 no quedo en $UPLINK (la vuelta atras restaurara la red)"
  ip -br addr show dev "$UPLINK"; ip -br addr show dev vmbr0
  ip -6 route show default
  log "ENI aplicado. Comprueba desde una sesion SSH NUEVA y ejecuta: $0 confirm"
}

phase_confirm() {
  [ -e "$STATE/net-pending" ] || systemctl is-active --quiet nv-adopt-net-rollback.timer \
    || die "no hay transaccion de red pendiente (¿ya vencio la vuelta atras?)"
  systemctl stop nv-adopt-net-rollback.timer nv-adopt-net-rollback.service 2>/dev/null || true
  systemctl is-active --quiet nv-adopt-net-rollback.timer && die "no pude parar la vuelta atras"
  rm -f "$STATE/net-pending"
  log "vuelta atras cancelada"
  # Red de respaldo del arranque (solo Kademar): si existe nv-netfallback, que su
  # ENI de un solo puerto lleve tambien la red nueva.
  if [ -x /usr/local/sbin/nv-netfallback.sh ]; then
    local p1=/root/nvinst/interfaces.prod-p1
    python3 - /etc/network/interfaces "$p1" "$UPLINK" <<'PY'
import re, sys
src, dst, up = sys.argv[1:4]
s = open(src).read()
m = re.search(r"^iface %s inet static\n((?:[ \t].*\n)+)" % re.escape(up), s, re.M)
if not m:
    sys.exit("sin stanza inet static del uplink")
body = m.group(1)
mac = re.search(r"hwaddress\s+(\S+)", body)
addr = re.search(r"address\s+(\S+)", body).group(1)
gw = re.search(r"gateway\s+(\S+)", body).group(1)
# Puerto 1 directo (nvup0) con las mismas direcciones; sin bond.
p1 = f"iface nvup0 inet static\n\taddress {addr}\n\tgateway {gw}\n"
s = s.replace(m.group(0), "")
s = re.sub(r"^auto %s\n" % re.escape(up), "", s, flags=re.M)
s = re.sub(r"^iface %s inet6 static" % re.escape(up), "iface nvup0 inet6 static", s, flags=re.M)
s = s.replace("net.ipv6.conf.%s." % up, "net.ipv6.conf.nvup0.")
s = re.sub(r"^iface nvup0 inet manual\n", p1, s, flags=re.M)
if "iface nvup0 inet static" not in s:
    sys.exit("no pude construir la stanza de nvup0")
open(dst, "w").write(s)
PY
    if [ -s "$p1" ]; then
      sed -i "s#^PRE=.*#PRE=$p1#" /usr/local/sbin/nv-netfallback.sh
      sed -i "s#; PRE=/root/nvinst/[^ ]*#; PRE=$p1#" /usr/local/sbin/nv-netfallback.sh
      grep -q "$p1" /usr/local/sbin/nv-netfallback.sh && log "nv-netfallback -> $p1" || log "AVISO: no pude apuntar nv-netfallback a $p1"
    fi
  fi
}

# ---------------------------------------------------------------------------
phase_tunnels() {
  log "== tunnels"
  require_no_guests
  ip -6 addr show dev "$UPLINK" scope global | grep -q "inet6 ${NODE_V6}/" || die "NODE_V6 no esta en $UPLINK: ejecuta antes net/confirm"
  local f
  for f in /usr/local/sbin/neuravps-tunnels.sh /usr/local/sbin/neuravps-tunnel-select.sh \
           /usr/local/sbin/neuravps-tunnel-probe.sh /usr/local/sbin/nvx-mss.sh \
           /usr/local/sbin/nvx-aisla.sh /usr/local/sbin/nvx-aisla-l3.sh; do
    put_heredoc "$INSTALL_SH" "$f" 755
  done
  for f in neuravps-tunnels.service nvx-mss.service nvx-aisla.service nvx-aisla-l3.service \
           neuravps-tunnel-probe.service neuravps-tunnel-probe.timer; do
    put_heredoc "$INSTALL_SH" "/etc/systemd/system/$f" 644
  done
  put_heredoc "$INSTALL_SH" /etc/sysctl.d/zz-neuravps-rpfilter.conf 644
  grep -q 'TUN_MTU' /usr/local/sbin/neuravps-tunnels.sh || die "neuravps-tunnels.sh sin TUN_MTU: el repo copiado es anterior al cambio"
  # DEFAULT_V4_VIA_TUNNEL=0: el nodo conserva su IPv4 publica para su propio
  # trafico; solo los invitados (tablas 100/101) salen por el tunel.
  cat > "$STATE/neuravps-tunnels.default" <<EOF
# Parametros de ESTE nodo. Ver docs/egress-failover-e-ipv6-estable.md §4.5.
# Nodo externo adoptado con scripts/adoptar-nodo-externo.sh ($TS).
NODE_ID=${NODE_ID}
SLOT=${NODE_ID}
HOME_REGION=auto
IDENT6=2a01:4f9:c01f:e::/64
TRANSIT_BASE=2a01:4f9:c01f:e:ffff::
VIP_FSN=2a01:4f8:fff2:95::2
VIP_HEL=2a01:4f9:fff1:5f::2
# 0 = el nodo tiene IPv4 publica propia (ruta por defecto normal).
DEFAULT_V4_VIA_TUNNEL=0
# Fuera de Hetzner: endpoint fijo y MTU del tunel = MTU IPv6 de salida (1476) - 44.
NODE_V6=${NODE_V6}
TUN_MTU=${TUN_MTU}
EOF
  put "$STATE/neuravps-tunnels.default" /etc/default/neuravps-tunnels 644
  sysctl -q -p /etc/sysctl.d/zz-neuravps-rpfilter.conf
  systemctl daemon-reload
  systemctl enable neuravps-tunnels.service neuravps-tunnel-probe.timer nvx-mss.service nvx-aisla.service nvx-aisla-l3.service >/dev/null 2>&1
  /usr/local/sbin/neuravps-tunnels.sh || log "AVISO: neuravps-tunnels.sh rc!=0 (normal si las bases aun no tienen el nodo en Firestore)"
  systemctl restart nvx-mss.service nvx-aisla.service nvx-aisla-l3.service
  systemctl start neuravps-tunnels.service neuravps-tunnel-probe.timer
  for f in tun-fsn tun-hel; do
    ip -6 tunnel show "$f" 2>/dev/null | grep -q "local ${NODE_V6} " || die "$f no tiene local $NODE_V6"
    [ "$(cat /sys/class/net/$f/mtu 2>/dev/null)" = "$TUN_MTU" ] || die "$f no tiene MTU $TUN_MTU"
  done
  for f in nvx-mss nvx-aisla nvx-aisla-l3; do systemctl is-active --quiet "$f" || die "$f no esta activo"; done
  ip -br link show type ip6gre | grep tun-
  ip -6 rule; ip rule
  nft list table inet nvxmss | grep maxseg
}

# ---------------------------------------------------------------------------
phase_node() {
  log "== node"
  require_no_guests
  # El cortafuegos se valida ANTES de tocar nada: sin uno valido no se sigue.
  [ -n "${CLUSTER_FW:-}" ] && [ -s "$CLUSTER_FW" ] || die "falta CLUSTER_FW (el cluster.fw de la flota); sin el no se retira el cortafuegos actual"
  grep -q '^\[IPSET base\]' "$CLUSTER_FW" && grep -q '^\[IPSET hosts-ipv6\]' "$CLUSTER_FW" && grep -q '^policy_in: DROP' "$CLUSTER_FW" \
    || die "CLUSTER_FW no parece el cluster.fw de la flota (IPSET base / hosts-ipv6 / policy_in DROP)"
  # Perfil de memoria SQX (el «ad» de first_boot.sh) salvo KSM: con 26-28 VPS E
  # la RAM sobra (1.092-1.176 GB de 1.511) y el limite es la CPU; ksmd la gasta.
  printf 'vm.swappiness=100\nvm.page-cluster=0\n' > /etc/sysctl.d/99-neuravps-memprofile.conf
  rm -f /etc/sysctl.d/99-proxmox-swap.conf
  put_heredoc "$FIRST_BOOT" /etc/sysctl.d/99-neuravps-stability.conf 644
  sysctl -q -p /etc/sysctl.d/99-neuravps-memprofile.conf -p /etc/sysctl.d/99-neuravps-stability.conf
  systemctl disable --now ksmtuned >/dev/null 2>&1 || true
  echo 0 > /sys/kernel/mm/ksm/run 2>/dev/null || true   # 0 = para sin deshacer fusiones
  local arc=$((16 * 1024 * 1024 * 1024))
  echo "$arc" > /sys/module/zfs/parameters/zfs_arc_max
  if [ "$(cat /etc/modprobe.d/zfs.conf 2>/dev/null)" != "options zfs zfs_arc_max=$arc" ]; then
    echo "options zfs zfs_arc_max=$arc" > /etc/modprobe.d/zfs.conf
    update-initramfs -u >/dev/null 2>&1 || log "AVISO: update-initramfs fallo"
  fi
  # zswap.
  put_heredoc "$FIRST_BOOT" /usr/local/sbin/neuravps-zswap.sh 755
  put_heredoc "$FIRST_BOOT" /etc/systemd/system/neuravps-zswap.service 644
  # Readahead de zvol 128 KB + watermark 100 (como los AX162, incidente del 11/11).
  put_heredoc "$FIRST_BOOT" /etc/udev/rules.d/61-neuravps-zvol-readahead.rules 644
  printf '# NeuraVPS AX162: wake kswapd earlier (default 10) — see run_remotes/install-ex44-readahead.sh\nvm.watermark_scale_factor = 100\n' \
    > /etc/sysctl.d/99-neuravps-ax162-watermark.conf
  udevadm control --reload; sysctl -q -p /etc/sysctl.d/99-neuravps-ax162-watermark.conf
  put_heredoc "$FIRST_BOOT" /etc/modprobe.d/blacklist-headless-gpu.conf 644
  # Watchdog de hardware AMD (sp5100_tco), como first_boot.sh, solo si el modulo enlaza.
  if grep -qi 'AuthenticAMD' /proc/cpuinfo && modprobe sp5100_tco 2>/dev/null && ls /dev/watchdog* >/dev/null 2>&1; then
    local pham=/etc/default/pve-ha-manager; touch "$pham"
    grep -q '^WATCHDOG_MODULE=sp5100_tco$' "$pham" || { grep -vE '^WATCHDOG_MODULE=' "$pham" > "$pham.tco"; echo 'WATCHDOG_MODULE=sp5100_tco' >> "$pham.tco"; mv "$pham.tco" "$pham"; log "watchdog: sp5100_tco (activo tras reiniciar)"; }
  else
    log "watchdog: sp5100_tco no enlaza aqui; se queda softdog"
  fi
  # smartd: filtro del aviso de desgaste de NVMe.
  put_heredoc "$FIRST_BOOT" /usr/local/sbin/smartd-filter.sh 755
  { for dev in /dev/nvme[0-9]n[0-9]; do [[ -b "$dev" ]] && echo "${dev} -d nvme -a -n standby -m root -M exec /usr/local/sbin/smartd-filter.sh"; done; } > "$STATE/smartd.conf"
  [ -s "$STATE/smartd.conf" ] && put "$STATE/smartd.conf" /etc/smartd.conf 644 && systemctl restart smartd 2>/dev/null
  # Almacenamiento como la flota: local = import/backup/iso/vztmpl; shared = snippets.
  pvesm set local --content import,backup,iso,vztmpl >/dev/null 2>&1 || log "AVISO: pvesm set local"
  mkdir -p /var/lib/svz/snippets
  pvesm status | awk '{print $1}' | grep -qx shared || pvesm add dir shared --path /var/lib/svz --content snippets --shared true
  # Hookscript + servicio de arranque + vigilante de estado + guarda de RAM.
  put "$REPO/snippets/sync-dnat.py" /var/lib/svz/snippets/sync-dnat.py 755
  put "$REPO/run_remotes/neuravps-ram-guard.py" /usr/local/sbin/neuravps-ram-guard.py 755
  put_heredoc "$FIRST_BOOT" /etc/systemd/system/node-boot-sync-dnat.service 644
  put "$REPO/run_remotes/neuravps-vm-status-watch.service" /etc/systemd/system/neuravps-vm-status-watch.service 644
  put "$REPO/run_remotes/neuravps-vm-status-watch.timer" /etc/systemd/system/neuravps-vm-status-watch.timer 644
  # netconsole hacia la base mas cercana (la de FSN desde Kademar: 5,5 ms).
  put_heredoc "$FIRST_BOOT" /usr/local/sbin/neuravps-netconsole.sh 755
  put_heredoc "$FIRST_BOOT" /etc/systemd/system/neuravps-netconsole.service 644
  [ -s /etc/neuravps-netconsole.conf ] || printf 'BASE_IP=%s\nPORT=6666\n' "${NC_BASE:-2a01:4f8:2b01:124::2}" > /etc/neuravps-netconsole.conf
  systemctl daemon-reload
  systemctl enable --now neuravps-zswap.service >/dev/null 2>&1
  systemctl enable node-boot-sync-dnat.service neuravps-vm-status-watch.timer neuravps-netconsole.service >/dev/null 2>&1
  systemctl start neuravps-vm-status-watch.timer neuravps-netconsole.service 2>/dev/null || true
  # Extras de nodo SQX: reconciliador de globo, guarda de RAM al 90 %, arranque escalonado.
  if [ -e /etc/systemd/system/neuravps-balloon-reconciler.timer ]; then
    log "extras SQX ya instalados"
  else
    SNIPPETS_SRC="$REPO/snippets" bash "$REPO/run_remotes/install-sqx-node-extras.sh" > "$STATE/sqx-extras-$TS.log" 2>&1
    local rc=$?; cat "$STATE/sqx-extras-$TS.log" >> "$STATE/adopt.log"
    [ "$rc" -eq 0 ] || die "install-sqx-node-extras.sh fallo (rc $rc), ver $STATE/sqx-extras-$TS.log"
  fi
  for f in neuravps-balloon-reconciler.timer neuravps-vm-status-watch.timer neuravps-zswap.service; do
    systemctl is-active --quiet "$f" || die "$f no esta activo"
  done
  [ -s /etc/default/neuravps-boot-stagger ] || die "falta /etc/default/neuravps-boot-stagger (arranque escalonado)"
  # Relevo del cortafuegos: se instala el de PVE, se valida y SOLO entonces se
  # retira el del piloto. Copias de lo que se pisa.
  local hostfw="/etc/pve/nodes/$(hostname)/host.fw"
  [ -e /etc/pve/firewall/cluster.fw ] && cp -a /etc/pve/firewall/cluster.fw "$STATE/cluster.fw.bak-$TS"
  [ -e "$hostfw" ] && cp -a "$hostfw" "$STATE/host.fw.bak-$TS"
  cp "$CLUSTER_FW" /etc/pve/firewall/cluster.fw || die "no pude escribir cluster.fw"
  if [ -n "${HOST_FW_V4:-}" ]; then
    {
      echo "[OPTIONS]"; echo; echo "enable: 1"; echo
      echo "[RULES]"; echo
      for ip in $HOST_FW_V4; do echo "IN SSH(ACCEPT) -source $ip -log nolog # base (IPv4, respaldo)"; done
    } > "$hostfw" || die "no pude escribir host.fw"
    log "host.fw: SSH por IPv4 desde $HOST_FW_V4"
  fi
  local diag; diag="$(pve-firewall compile 2>&1 >/dev/null | grep -i -E 'unable|error|parse' || true)"
  if [ -n "$diag" ]; then
    # El cluster.fw canonico de la flota arrastra hoy un error conocido (25/11,
    # linea 258, tarea aparte). Solo se acepta con ALLOW_FW_DIAG=<texto exacto>.
    if [ -n "${ALLOW_FW_DIAG:-}" ] && [ "$diag" = "$(printf '%b' "$ALLOW_FW_DIAG")" ]; then
      log "AVISO: pve-firewall con diagnostico conocido y aceptado: $diag"
    else
      log "pve-firewall compile: $diag"
      die "el cortafuegos de PVE no compila limpio; el del piloto sigue puesto (ALLOW_FW_DIAG para aceptar un diagnostico conocido)"
    fi
  fi
  pve-firewall restart || die "pve-firewall restart fallo; el del piloto sigue puesto"
  sleep 3
  pve-firewall status | grep -q 'enabled/running' || die "pve-firewall no esta enabled/running; el del piloto sigue puesto"
  iptables -S INPUT | grep -q -- '-j PVEFW-INPUT' || die "sin PVEFW-INPUT en INPUT; el del piloto sigue puesto"
  iptables -S PVEFW-HOST-IN | grep -v PVESIG | tail -1 | grep -q -E -- '-j (DROP|PVEFW-Drop)' || die "PVEFW-HOST-IN no acaba en DROP; el del piloto sigue puesto"
  ip6tables -S PVEFW-HOST-IN | grep -v PVESIG | tail -1 | grep -q -E -- '-j (DROP|PVEFW-Drop)' || die "PVEFW-HOST-IN (v6) no acaba en DROP; el del piloto sigue puesto"
  # Ahora si: fuera el piloto (cortafuegos propio y sonda).
  systemctl disable --now nvpilot-fw.service nvprobe.service >/dev/null 2>&1 || true
  nft delete table inet nvpilot 2>/dev/null || true
  log "node: hecho"
}

case "$PHASE" in
  check) phase_check ;;
  hostname) phase_hostname ;;
  net) phase_net ;;
  confirm) phase_confirm ;;
  tunnels) phase_tunnels ;;
  node) phase_node ;;
  *) echo "uso: $0 check|hostname|net|confirm|tunnels|node" >&2; exit 2 ;;
esac

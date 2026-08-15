#!/usr/bin/env bash
# Convierte un nodo al modelo nuevo SIN tocar a sus invitados actuales.
# Se ejecuta EN EL NODO. Todo es aditivo; la unica pieza capaz de cortar a un
# cliente es la ruta por defecto v4, y va detras de una guarda dura.
set -u
cd /tmp/nvconv || { echo "❌ falta /tmp/nvconv"; exit 1; }

NODE_NUM=$(hostname | cut -d- -f1 | sed 's/^0*//')
[ -n "$NODE_NUM" ] && [ "$NODE_NUM" -ge 1 ] 2>/dev/null || { echo "❌ no deduzco el numero de $(hostname)"; exit 1; }

ANTES_DEF=$(ip -4 route show default)
ANTES_RPF=$(sysctl -n net.ipv4.conf.vmbr0.rp_filter)

install -m755 neuravps-tunnels.sh        /usr/local/sbin/neuravps-tunnels.sh
install -m755 neuravps-tunnel-select.sh  /usr/local/sbin/neuravps-tunnel-select.sh
install -m755 neuravps-tunnel-probe.sh   /usr/local/sbin/neuravps-tunnel-probe.sh
# El clamp de MSS: sin el, el invitado anuncia 1460 sobre un camino de 1456.
install -m755 nvx-mss.sh                 /usr/local/sbin/nvx-mss.sh
install -m644 neuravps-tunnels.service   /etc/systemd/system/
install -m644 neuravps-tunnel-probe.service /etc/systemd/system/
install -m644 neuravps-tunnel-probe.timer   /etc/systemd/system/
install -m644 zz-neuravps-rpfilter.conf  /etc/sysctl.d/zz-neuravps-rpfilter.conf
rm -f /etc/sysctl.d/99-neuravps-rpfilter.conf

# ⚠️ DEFAULT_V4_VIA_TUNNEL=0 durante la transicion: el nodo CONSERVA su IPv4
# publica para su propio trafico y para sus invitados del esquema viejo. Solo
# los del esquema nuevo (10.64/16, tabla 101) salen por el tunel. Ponerlo a 1
# aqui cortaria de golpe a todos los clientes de la caja.
cat > /etc/default/neuravps-tunnels <<EOF
NODE_ID=${NODE_NUM}
SLOT=${NODE_NUM}
HOME_REGION=auto
IDENT6=2a01:4f9:c01f:e::/64
TRANSIT_BASE=2a01:4f9:c01f:e:ffff::
VIP_FSN=2a01:4f8:fff2:95::2
VIP_HEL=2a01:4f9:fff1:5f::2
DEFAULT_V4_VIA_TUNNEL=0
EOF
grep -q '^DEFAULT_V4_VIA_TUNNEL=0$' /etc/default/neuravps-tunnels || { echo "❌ ABORTO: DEFAULT_V4_VIA_TUNNEL no quedo a 0"; exit 1; }

sysctl -p /etc/sysctl.d/zz-neuravps-rpfilter.conf >/dev/null 2>&1

# --- red del invitado nuevo, ADITIVA -----------------------------------------
UPLINK=$(ip -4 route show default | sed -n 's/.*dev \([^ ]*\).*/\1/p' | head -1)
ip addr replace 10.64.255.1/16 dev vmbr0
ip -6 addr replace fe80::1/64 dev vmbr0 2>/dev/null || ip -6 addr add fe80::1/64 dev vmbr0 2>/dev/null
iptables -t nat -C POSTROUTING -s 10.64.0.0/16 -o "$UPLINK" -j MASQUERADE 2>/dev/null \
  || iptables -t nat -A POSTROUTING -s 10.64.0.0/16 -o "$UPLINK" -j MASQUERADE
sysctl -qw net.ipv4.conf.vmbr0.proxy_arp=1

# Persistir en /etc/network/interfaces si aun no esta (idempotente).
if ! grep -q '10\.64\.255\.1' /etc/network/interfaces; then
  python3 - "$UPLINK" <<'PY'
import re, sys
up = sys.argv[1]
p = "/etc/network/interfaces"
s = open(p).read()
m = re.search(r"(iface vmbr0 inet static\n(?:\t| ).*?\n)(?=\n|iface|auto|$)", s, re.S)
if m:
    blk = m.group(1)
    add = (f"    post-up   ip addr add 10.64.255.1/16 dev vmbr0\n"
           f"    post-up   iptables -t nat -A POSTROUTING -s '10.64.0.0/16' -o {up} -j MASQUERADE\n"
           f"    post-down iptables -t nat -D POSTROUTING -s '10.64.0.0/16' -o {up} -j MASQUERADE\n"
           f"    post-up   ip -6 addr add fe80::1/64 dev vmbr0 || true\n")
    open(p, "w").write(s.replace(blk, blk + add, 1))
    print("   interfaces: bloque anadido")
else:
    print("   ⚠️ interfaces: no encuentro la stanza de vmbr0 — hazlo a mano")
PY
fi
grep -q 'proxy_arp' /etc/sysctl.d/99-neuravps-forwarding.conf 2>/dev/null \
  || echo 'net.ipv4.conf.vmbr0.proxy_arp = 1' >> /etc/sysctl.d/99-neuravps-forwarding.conf

# --- tuneles + sondeo --------------------------------------------------------
systemctl daemon-reload
systemctl enable neuravps-tunnels.service neuravps-tunnel-probe.timer >/dev/null 2>&1
/usr/local/sbin/neuravps-tunnels.sh || { echo "❌ el script de tuneles fallo"; exit 1; }
systemctl start neuravps-tunnel-probe.timer >/dev/null 2>&1

# --- GUARDA DURA: la ruta por defecto NO puede haber cambiado ----------------
DESPUES_DEF=$(ip -4 route show default)
if [ "$ANTES_DEF" != "$DESPUES_DEF" ]; then
  echo "❌❌ LA RUTA POR DEFECTO CAMBIO — restaurando"
  echo "     antes:   $ANTES_DEF"
  echo "     despues: $DESPUES_DEF"
  ip -4 route replace $ANTES_DEF
  exit 1
fi
[ "$(sysctl -n net.ipv4.conf.vmbr0.rp_filter)" = "$ANTES_RPF" ] || echo "  ⚠️ rp_filter de vmbr0 cambio ($ANTES_RPF -> $(sysctl -n net.ipv4.conf.vmbr0.rp_filter))"

echo "  ✅ $(hostname) convertido (slot $NODE_NUM, region $(. /etc/default/neuravps-tunnels; echo $HOME_REGION))"
echo "     ruta por defecto INTACTA: $DESPUES_DEF"
echo "     tuneles: $(ip -br link show type ip6gre | grep -c 'tun-\(fsn\|hel\)')/2  10.0.0.1 sigue: $(ip -4 addr show dev vmbr0 | grep -c '10\.0\.0\.1')"

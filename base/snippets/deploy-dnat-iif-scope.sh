#!/usr/bin/env bash
# C) Acotar el DNAT IPv6 de la base a las interfaces por las que ENTRA tráfico
# legítimo: la pública (enp2s0) y jool (veth-host). Hoy las cuatro reglas de
# `ip6 nat prerouting` no filtran por interfaz, así que una conexión IPv6 de un
# invitado (iif tun-*) hacia CUALQUIER dirección en un puerto que sea clave de
# un mapa acaba DNATeada a la VM de ese puerto — secuestro entre inquilinos —
# y además rompe la salida v6 legítima del invitado en esos puertos.
#
# Caminos de ENTRADA que hay que conservar (medido en b0/b1, 18/09/2026):
#   - RDP/SMB/SSH v6 nativo desde Internet  -> iif enp2s0
#   - RDP/SMB/SSH v4 desde Internet vía jool (el paquete traducido vuelve con
#     dst = IP principal v6 de la base)     -> iif veth-host
# El invitado (iif tun-*) NO es un camino de entrada: es exactamente el abuso.
#
# Atómico (una transacción nft -f), sin flush ni recarga de nftables.conf.
# Idempotente. Persiste en /etc/nftables.conf. Rollback: rollback-dnat-iif-scope.sh
set -euo pipefail
CONF=${CONF:-/etc/nftables.conf}
IIF='{ "enp2s0", "veth-host" }'

echo "=== acotar DNAT ip6 por interfaz en $(hostname) ==="

CH=$(nft list chain ip6 nat prerouting)
if printf '%s' "$CH" | grep -q 'iifname'; then
  echo "  [vivo] ya estaba"
else
  # handle de cada regla DNAT, por su mapa (el orden puede variar entre bases)
  declare -A H
  for m in rdp_tcp_map rdp_udp_map smb_tcp_map ssh_tcp_map; do
    H[$m]=$(nft -a list chain ip6 nat prerouting | awk -v m="@$m " '$0 ~ m {print $NF; exit}')
    [ -n "${H[$m]}" ] || { echo "  !! no encuentro la regla de $m — aborto"; exit 1; }
  done
  TX=$(mktemp --suffix=.nft /root/dnat-iif.XXXXXX)
  {
    echo "delete rule ip6 nat prerouting handle ${H[rdp_tcp_map]}"
    echo "delete rule ip6 nat prerouting handle ${H[rdp_udp_map]}"
    echo "delete rule ip6 nat prerouting handle ${H[smb_tcp_map]}"
    echo "delete rule ip6 nat prerouting handle ${H[ssh_tcp_map]}"
    echo "add rule ip6 nat prerouting iifname $IIF tcp dport @rdp_tcp_map dnat ip6 to tcp dport map @rdp_tcp_map"
    echo "add rule ip6 nat prerouting iifname $IIF udp dport @rdp_udp_map dnat ip6 to udp dport map @rdp_udp_map"
    echo "add rule ip6 nat prerouting iifname $IIF tcp dport @smb_tcp_map dnat ip6 to tcp dport map @smb_tcp_map"
    echo "add rule ip6 nat prerouting iifname $IIF tcp dport @ssh_tcp_map dnat ip6 to tcp dport map @ssh_tcp_map"
  } > "$TX"
  nft -c -f "$TX"
  nft -f "$TX"
  echo "  [vivo] 4 reglas DNAT reescritas con iifname ($TX)"
fi

# --- persistencia ------------------------------------------------------------
if grep -q 'iifname .* dnat ip6 to' "$CONF"; then
  echo "  [conf] ya estaba"
else
  BK="$CONF.bak.dnatiif.$(date +%Y%m%d-%H%M%S)"
  cp -a "$CONF" "$BK"; echo "  [conf] copia en $BK"
  python3 - "$CONF" <<'PY'
import sys
conf = sys.argv[1]
s = open(conf).read()
iif = 'iifname { "enp2s0", "veth-host" } '
subs = [
    ("        tcp dport @rdp_tcp_map dnat ip6 to tcp dport map @rdp_tcp_map\n",
     "        " + iif + "tcp dport @rdp_tcp_map dnat ip6 to tcp dport map @rdp_tcp_map\n"),
    ("        udp dport @rdp_udp_map dnat ip6 to udp dport map @rdp_udp_map\n",
     "        " + iif + "udp dport @rdp_udp_map dnat ip6 to udp dport map @rdp_udp_map\n"),
    ("        tcp dport @smb_tcp_map dnat ip6 to tcp dport map @smb_tcp_map\n",
     "        " + iif + "tcp dport @smb_tcp_map dnat ip6 to tcp dport map @smb_tcp_map\n"),
    ("        tcp dport @ssh_tcp_map dnat ip6 to tcp dport map @ssh_tcp_map\n",
     "        " + iif + "tcp dport @ssh_tcp_map dnat ip6 to tcp dport map @ssh_tcp_map\n"),
]
for old, new in subs:
    assert s.count(old) == 1, f"ancla no única/ausente: {old.strip()}"
    s = s.replace(old, new)
open(conf, "w").write(s)
print("  [conf] 4 reglas DNAT acotadas por interfaz")
PY
  if nft -c -f "$CONF"; then echo "  [conf] sintaxis OK (no cargada)"
  else echo "  !! conf no valida — restauro"; cp -a "$BK" "$CONF"; exit 1; fi
fi

nft list chain ip6 nat prerouting | sed 's/^/  /'
echo "DNAT_IIF_OK $(hostname)"

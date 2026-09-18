#!/usr/bin/env bash
# Deshace deploy-dnat-iif-scope.sh: vuelve a las 4 reglas DNAT sin iifname.
# Atómico, sin flush ni recarga. Deshacer dirigido por handle.
set -euo pipefail
CONF=${CONF:-/etc/nftables.conf}

echo "=== rollback DNAT ip6 (quitar iifname) en $(hostname) ==="

CH=$(nft -a list chain ip6 nat prerouting)
if printf '%s' "$CH" | grep -q 'iifname'; then
  declare -A H
  for m in rdp_tcp_map rdp_udp_map smb_tcp_map ssh_tcp_map; do
    H[$m]=$(nft -a list chain ip6 nat prerouting | awk -v m="@$m " '$0 ~ m {print $NF; exit}')
  done
  TX=$(mktemp --suffix=.nft /root/dnat-iif-rb.XXXXXX)
  {
    echo "delete rule ip6 nat prerouting handle ${H[rdp_tcp_map]}"
    echo "delete rule ip6 nat prerouting handle ${H[rdp_udp_map]}"
    echo "delete rule ip6 nat prerouting handle ${H[smb_tcp_map]}"
    echo "delete rule ip6 nat prerouting handle ${H[ssh_tcp_map]}"
    echo "add rule ip6 nat prerouting tcp dport @rdp_tcp_map dnat ip6 to tcp dport map @rdp_tcp_map"
    echo "add rule ip6 nat prerouting udp dport @rdp_udp_map dnat ip6 to udp dport map @rdp_udp_map"
    echo "add rule ip6 nat prerouting tcp dport @smb_tcp_map dnat ip6 to tcp dport map @smb_tcp_map"
    echo "add rule ip6 nat prerouting tcp dport @ssh_tcp_map dnat ip6 to tcp dport map @ssh_tcp_map"
  } > "$TX"
  nft -c -f "$TX"; nft -f "$TX"
  echo "  [vivo] deshecho ($TX)"
else
  echo "  [vivo] nada que deshacer"
fi

if grep -q 'iifname .* dnat ip6 to' "$CONF"; then
  BK="$CONF.bak.dnatiif-rb.$(date +%Y%m%d-%H%M%S)"; cp -a "$CONF" "$BK"
  sed -i 's/        iifname { "enp2s0", "veth-host" } \(tcp dport @rdp_tcp_map\|udp dport @rdp_udp_map\|tcp dport @smb_tcp_map\|tcp dport @ssh_tcp_map\)/        \1/' "$CONF"
  if nft -c -f "$CONF"; then echo "  [conf] revertida (sintaxis OK)"
  else echo "  !! conf no valida — restauro"; cp -a "$BK" "$CONF"; exit 1; fi
else
  echo "  [conf] nada que revertir"
fi
nft list chain ip6 nat prerouting | sed 's/^/  /'
echo "DNAT_IIF_ROLLBACK_OK $(hostname)"

#!/usr/bin/env bash
# ¿Sobrevive todo a un reinicio? Se ejecuta DENTRO de la maquina.
# Uso: persist_audit.sh <nodo|base>
KIND="$1"
ok() { printf "    %-38s %s\n" "$1" "$2"; }

echo "  [$KIND] $(hostname)"
if [ "$KIND" = nodo ]; then
  ok "neuravps-tunnels.service" "$(systemctl is-enabled neuravps-tunnels.service 2>&1)/$(systemctl is-active neuravps-tunnels.service 2>&1)"
  ok "neuravps-tunnel-probe.timer" "$(systemctl is-enabled neuravps-tunnel-probe.timer 2>&1)/$(systemctl is-active neuravps-tunnel-probe.timer 2>&1)"
  for f in /usr/local/sbin/neuravps-tunnels.sh /usr/local/sbin/neuravps-tunnel-select.sh \
           /usr/local/sbin/neuravps-tunnel-probe.sh /etc/default/neuravps-tunnels \
           /etc/sysctl.d/zz-neuravps-rpfilter.conf; do
    ok "$(basename "$f")" "$([ -s "$f" ] && echo presente || echo "❌ FALTA")"
  done
  ok "HOME_REGION" "$(. /etc/default/neuravps-tunnels; echo "$HOME_REGION (slot $SLOT)")"
  ok "10.64.255.1 en interfaces" "$(grep -c '10\.64\.255\.1' /etc/network/interfaces)"
  ok "fe80::1 en interfaces" "$(grep -c 'fe80::1/64 dev vmbr0' /etc/network/interfaces)"
  ok "MASQUERADE 10.64 en interfaces" "$(grep -c '10\.64\.0\.0/16' /etc/network/interfaces)"
  ok "proxy_arp persistido" "$(grep -rc 'proxy_arp' /etc/sysctl.d/ 2>/dev/null | grep -v ':0' | wc -l)"
  ok "VIPs en cluster.fw" "$(grep -cE 'fff2:95::/64|fff1:5f::/64' /etc/pve/firewall/cluster.fw)/2"
  ok "tuneles arriba" "$(ip -br link show type ip6gre | grep -c 'tun-\(fsn\|hel\).*UP')/2"
else
  ok "neuravps-base-tunnels.service" "$(systemctl is-enabled neuravps-base-tunnels.service 2>&1)/$(systemctl is-active neuravps-base-tunnels.service 2>&1)"
  for f in /usr/local/sbin/neuravps-base-tunnels.sh /etc/default/neuravps-base-tunnels \
           /etc/neuravps/tunnel-nodes.conf /etc/sysctl.d/zz-neuravps-rpfilter.conf; do
    ok "$(basename "$f")" "$([ -s "$f" ] && echo presente || echo "❌ FALTA")"
  done
  ok "HOME_REGION" "$(. /etc/default/neuravps-base-tunnels; echo "$HOME_REGION")"
  ok "TUNNEL_IFACE_PREFIX" "$(grep '^TUNNEL_IFACE_PREFIX=' /etc/default/base-nat | cut -d= -f2)"
  ok "IDENT/TRANSIT/VM_V4 en base-nat" "$(grep -cE '^(IDENT_PREFIX|TRANSIT_PREFIX|VM_V4_PREFIX)=' /etc/default/base-nat)/3"
  ok "canonicas por VIP en nftables.conf" "$(grep -c 'oifname \"tun-[fh]p\*\"' /etc/nftables.conf)/2"
  ok "snat v4 10.64/10.65 en conf" "$(grep -cE 'ip saddr 10\.6[45]\.' /etc/nftables.conf)/2"
  ok "gre_peers en conf" "$(grep -c 'set gre_peers' /etc/nftables.conf)/1"
  ok "forward de tuneles en conf" "$(grep -c 'tun-\*' /etc/nftables.conf)/3"
  ok "tuneles arriba" "$(ip -br link show type ip6gre | grep -c 'tun-[fh]p.*UP')/4"
fi
ok "rp_filter all/default" "$(sysctl -n net.ipv4.conf.all.rp_filter)/$(sysctl -n net.ipv4.conf.default.rp_filter)"

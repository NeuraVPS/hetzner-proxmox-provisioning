#!/usr/bin/env bash
# ¿Sobrevive la conversión a un reinicio? Se ejecuta EN EL NODO. Solo lee.
p=0; t=0
chk() { t=$((t+1)); [ "$2" -ge 1 ] 2>/dev/null && { p=$((p+1)); printf "✅"; } || printf "❌"; printf " %s " "$1"; }
printf "  %-22s " "$(hostname | cut -d- -f1)"
chk unidades  "$([ "$(systemctl is-enabled neuravps-tunnels.service 2>/dev/null)" = enabled ] && [ "$(systemctl is-enabled neuravps-tunnel-probe.timer 2>/dev/null)" = enabled ] && echo 1 || echo 0)"
chk scripts   "$(ls /usr/local/sbin/neuravps-tunnel{s,-select,-probe}.sh 2>/dev/null | wc -l)"
chk default   "$([ -s /etc/default/neuravps-tunnels ] && echo 1 || echo 0)"
chk rpf-file  "$([ -s /etc/sysctl.d/zz-neuravps-rpfilter.conf ] && echo 1 || echo 0)"
chk if-gw4    "$(grep -c '10\.64\.255\.1' /etc/network/interfaces)"
chk if-masq   "$(grep -c '10\.64\.0\.0/16' /etc/network/interfaces)"
chk if-gw6    "$(grep -c 'fe80::1/64 dev vmbr0' /etc/network/interfaces)"
chk parp-file "$(grep -rc 'proxy_arp' /etc/sysctl.d/ 2>/dev/null | grep -v ':0' | wc -l)"
chk v4tunel0  "$(grep -c '^DEFAULT_V4_VIA_TUNNEL=0$' /etc/default/neuravps-tunnels)"
echo "=> $p/$t"

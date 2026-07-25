#!/usr/bin/env bash
# Despliega el auto-bloqueo de barridos RDP en una BASE. Idempotente.
#
#   bf_seen  (addr . port, 1h)  <- se llena con una regla SIN veredicto
#   bf_auto  (addr, timeout)    <- bloqueos automáticos que CADUCAN solos
#
# Orden final de la cadena `pre` (importa):
#   bf_allow accept -> bf_static drop -> bf_auto drop -> record bf_seen -> rate-limit
# Las dos reglas nuevas se INSERTAN antes de la del rate-limit usando su handle,
# nunca al final (appendear las dejaría detrás del drop y no harían nada).
set -euo pipefail

add_family() {
  local fam=$1 atype=$2 saddr=$3
  # 1) sets (idempotente)
  nft list set "$fam" rdpguard bf_seen >/dev/null 2>&1 || \
    nft add set "$fam" rdpguard bf_seen "{ type ${atype} . inet_service; flags dynamic,timeout; timeout 1h; size 65536; }"
  nft list set "$fam" rdpguard bf_auto >/dev/null 2>&1 || \
    nft add set "$fam" rdpguard bf_auto "{ type ${atype}; flags dynamic,timeout; timeout 24h; size 65536; }"

  # 2) reglas — solo si no están ya
  if nft list chain "$fam" rdpguard pre | grep -q "@bf_auto"; then
    echo "  $fam: reglas ya presentes"
    return 0
  fi
  # handle de la regla de rate-limit (la que lleva 'add @bf_src' o 'add @bf')
  local h
  h=$(nft -a list chain "$fam" rdpguard pre | awk '/add @bf(_src)? /{print $NF; exit}')
  if [ -z "$h" ]; then echo "  $fam: NO encuentro la regla de rate-limit; abortando"; return 1; fi

  local cname
  cname=$(nft list chain "$fam" rdpguard pre | grep -oE 'counter name "[a-z0-9_]+"' | head -1 | grep -oE '"[a-z0-9_]+"' | tr -d '"')
  cname=${cname:-bf_drops}

  # se insertan en orden inverso: cada `insert ... handle H` queda justo antes de H
  nft insert rule "$fam" rdpguard pre handle "$h" \
    tcp dport 20000-29999 tcp flags and \(syn\|ack\) == syn \
    add @bf_seen "{ ${saddr} . tcp dport }" \
    comment "\"record (source,port) for the sweep detector\""
  nft insert rule "$fam" rdpguard pre handle "$h" \
    "${saddr%% *}" saddr @bf_auto counter name "$cname" drop \
    comment "\"auto-blocked RDP port sweeper (expires)\""
  echo "  $fam: reglas insertadas"
}

echo "=== live ==="
add_family ip  ipv4_addr "ip saddr"
add_family ip6 ipv6_addr "ip6 saddr"

echo "=== config ==="
if [ ! -f /etc/neuravps-sweepguard.json ]; then
  cat > /etc/neuravps-sweepguard.json <<'JSON'
{
  "enabled": true,
  "dryRun": true,
  "minPorts": 20,
  "maxAddsPerRun": 50,
  "blockSeconds": 86400
}
JSON
  echo "  creada /etc/neuravps-sweepguard.json (dryRun=true)"
else
  echo "  config ya existe (no la toco)"
fi

echo "=== systemd ==="
cat > /etc/systemd/system/neuravps-sweepguard.service <<'UNIT'
[Unit]
Description=NeuraVPS RDP sweep guard (auto-block port sweepers)
After=nftables.service
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/sweepguard.py
UNIT
cat > /etc/systemd/system/neuravps-sweepguard.timer <<'UNIT'
[Unit]
Description=Run the RDP sweep guard every 5 minutes
[Timer]
OnBootSec=5min
OnUnitActiveSec=5min
AccuracySec=30s
[Install]
WantedBy=timers.target
UNIT
install -m 0755 /root/sweepguard.py /usr/local/sbin/sweepguard.py
systemctl daemon-reload
systemctl enable --now neuravps-sweepguard.timer >/dev/null 2>&1
echo "  timer activo: $(systemctl is-active neuravps-sweepguard.timer)"

echo "=== persistencia en /etc/nftables.conf ==="
BK="/etc/nftables.conf.bak.sweepguard.$(date +%Y%m%d-%H%M%S)"
cp -a /etc/nftables.conf "$BK"
python3 - <<'PY'
import re
p='/etc/nftables.conf'; s=open(p).read()
if 'bf_seen' in s:
    print('  ya persistido'); raise SystemExit(0)
specs=[('table ip rdpguard','ipv4_addr','ip saddr','bf_v4_drops'),
       ('table ip6 rdpguard','ipv6_addr','ip6 saddr','bf_drops')]
for tbl,atype,saddr,counter in specs:
    i=s.find(tbl)
    if i<0: print(f'  {tbl} no encontrada'); continue
    j=s.find('chain pre', i)
    k=s.find('{', j)
    sets=(f"\n    set bf_seen {{\n        type {atype} . inet_service\n"
          f"        flags dynamic,timeout\n        timeout 1h\n        size 65536\n    }}\n"
          f"\n    set bf_auto {{\n        type {atype}\n        flags dynamic,timeout\n"
          f"        timeout 24h\n        size 65536\n    }}\n")
    s = s[:j] + sets + "\n    " + s[j:]
    j=s.find('chain pre', i); k=s.find('\n', s.find('policy accept;', j))
    rules=(f"\n        {saddr} @bf_auto counter name {counter} drop comment \"auto-blocked RDP port sweeper (expires)\""
           f"\n        tcp dport 20000-29999 tcp flags & (syn|ack) == syn add @bf_seen {{ {saddr} . tcp dport }} comment \"record (source,port) for the sweep detector\"")
    s = s[:k] + rules + s[k:]
open(p,'w').write(s); print('  persistido en la conf')
PY
if nft -c -f /etc/nftables.conf; then echo "  conf valida OK"
else echo "  !! conf NO valida — restauro"; cp -a "$BK" /etc/nftables.conf; exit 1; fi

echo "=== estado final de la cadena ip ==="
nft list chain ip rdpguard pre
echo "SWEEPGUARD_OK $(hostname)"

#!/usr/bin/env bash
# Añade el tercer forward por VM (SSH: 30000+vmid -> guest:22) y extiende
# todas las protecciones al rango 3xxxx.
#
# Por qué (2026-08-04)
# --------------------
# Se ofrece a los clientes acceso a PowerShell vía OpenSSH in-guest
# (base:30000+vmid), pensado para conectar aplicaciones de IA. Piezas:
#   1. table ip nat     — el DNAT de entrada v4 solo cubría 10000-29999;
#                         sin ampliarlo, el puerto 3xxxx ni entra a jool.
#   2. table ip6 nat    — nuevo map ssh_tcp_map + regla dnat (elementos los
#                         pone sync-base-nat.py, igual que rdp/smb).
#   3. table ip rdpguard  — las 3 reglas (bf_seen/bf_src/bf_port) pasan a
#                           10000-39999: SSH es fuerza bruta clásica y ha de
#                           tener EXACTAMENTE la misma protección que RDP/SMB.
#   4. table ip6 rdpguard — sus 2 reglas pasan de 20000-29999 a 20000-39999.
# sweepguard.py (detectores) se actualiza aparte (PORT_HI/vm_slot).
#
# Idempotente: cada pieza se comprueba antes de tocarla.
set -euo pipefail

echo "=== extender rango SSH (30000+vmid) en $(hostname) ==="
BK="/etc/nftables.conf.bak.sshrange.$(date +%Y%m%d-%H%M%S)"

# --- 1) v4 ingress: tcp 10000-29999 -> 10000-39999 (todas las VIPs) ---------
old_v4="$(nft -a list chain ip nat prerouting | grep -c 'tcp dport 10000-29999' || true)"
if [ "$old_v4" = "0" ]; then
  echo "  [v4 ingress] ya extendido"
else
  # replace por handle conservando la daddr de cada VIP
  nft -a list chain ip nat prerouting | grep 'tcp dport 10000-29999' \
    | sed -E 's/.*ip daddr ([0-9.]+).*# handle ([0-9]+)/\1 \2/' \
    | while read -r daddr handle; do
        nft replace rule ip nat prerouting handle "$handle" \
          ip daddr "$daddr" tcp dport 10000-39999 dnat to 10.0.0.3
        echo "  [v4 ingress] VIP $daddr -> 10000-39999"
      done
fi

# --- 2) ip6 nat: map ssh_tcp_map + regla dnat -------------------------------
if ! nft list map ip6 nat ssh_tcp_map >/dev/null 2>&1; then
  nft add map ip6 nat ssh_tcp_map '{ type inet_service : ipv6_addr . inet_service ; }'
  echo "  [ip6 nat] map ssh_tcp_map creado"
else
  echo "  [ip6 nat] map ssh_tcp_map ya existe"
fi
if ! nft list chain ip6 nat prerouting | grep -q 'ssh_tcp_map'; then
  nft add rule ip6 nat prerouting tcp dport @ssh_tcp_map dnat ip6 to tcp dport map @ssh_tcp_map
  echo "  [ip6 nat] regla dnat ssh añadida"
else
  echo "  [ip6 nat] regla dnat ssh ya presente"
fi

# --- 3) ip rdpguard: 10000-29999 -> 10000-39999 (las 3 reglas) --------------
have_new="$(nft list chain ip rdpguard pre | grep -c 'dport 10000-39999' || true)"
old="$(nft list chain ip rdpguard pre | grep -c 'dport 10000-29999' || true)"
if [ "$old" = "0" ] && [ "$have_new" -ge 3 ]; then
  echo "  [ip guard] ya extendido en vivo (3 reglas)"
else
  echo "  [ip guard] reglas a migrar: $old (nuevas presentes: $have_new)"
  # se borran TAMBIEN las del rango nuevo para reconstruir el juego completo;
  # de mayor a menor handle para que los restantes no se desplacen
  nft -a list chain ip rdpguard pre | grep -E 'dport 10000-[23]9999' \
    | grep -oE 'handle [0-9]+' | grep -oE '[0-9]+' | sort -rn > /tmp/rg_ssh_handles
  while read -r h; do nft delete rule ip rdpguard pre handle "$h" 2>/dev/null || true; done < /tmp/rg_ssh_handles
  nft add rule ip rdpguard pre tcp dport 10000-39999 \
    tcp flags '&' '(syn|ack)' == syn \
    add @bf_seen "{ ip saddr . tcp dport }" \
    comment '"record (source,port) for the sweep detector"'
  nft add rule ip rdpguard pre tcp dport 10000-39999 \
    tcp flags '&' '(syn|ack)' == syn \
    add @bf_src "{ ip saddr limit rate over 60/minute burst 30 packets }" \
    counter name bf_v4_drops drop \
    comment '"rate-limit excess new conns per source (anti port-spray)"'
  nft add rule ip rdpguard pre tcp dport 10000-39999 \
    tcp flags '&' '(syn|ack)' == syn \
    add @bf_port "{ ip saddr . tcp dport limit rate over 12/minute burst 15 packets }" \
    counter name portguard_drops drop \
    comment '"rate-limit repeated hits on the SAME customer port"'
  echo "  [ip guard] reglas recreadas con 10000-39999"
fi

# bf_allow DEBE seguir primero: se comprueba, no se asume.
first="$(nft list chain ip rdpguard pre | grep -E '^\s+(ip|tcp|meta)' | head -1)"
case "$first" in
  *bf_allow*accept*) echo "  [ip guard] orden OK (bf_allow primero)" ;;
  *) echo "  !! bf_allow NO es la primera regla — REVISAR"; exit 1 ;;
esac

# --- 4) ip6 rdpguard: 20000-29999 -> 20000-39999 (las 2 reglas) -------------
have_new6="$(nft list chain ip6 rdpguard pre | grep -c 'dport 20000-39999' || true)"
old6="$(nft list chain ip6 rdpguard pre | grep -c 'dport 20000-29999' || true)"
if [ "$old6" = "0" ] && [ "$have_new6" -ge 2 ]; then
  echo "  [ip6 guard] ya extendido en vivo (2 reglas)"
else
  echo "  [ip6 guard] reglas a migrar: $old6 (nuevas presentes: $have_new6)"
  nft -a list chain ip6 rdpguard pre | grep -E 'dport 20000-[23]9999' \
    | grep -oE 'handle [0-9]+' | grep -oE '[0-9]+' | sort -rn > /tmp/rg6_ssh_handles
  while read -r h; do nft delete rule ip6 rdpguard pre handle "$h" 2>/dev/null || true; done < /tmp/rg6_ssh_handles
  nft add rule ip6 rdpguard pre tcp dport 20000-39999 \
    tcp flags '&' '(syn|ack)' == syn \
    add @bf_seen "{ ip6 saddr . tcp dport }" \
    comment '"record (source,port) for the sweep detector"'
  nft add rule ip6 rdpguard pre tcp dport 20000-39999 \
    tcp flags '&' '(syn|ack)' == syn \
    add @bf "{ ip6 saddr . tcp dport limit rate over 12/minute burst 6 packets }" \
    counter name bf_drops drop \
    comment '"drop excess new RDP conns per source+port"'
  echo "  [ip6 guard] reglas recreadas con 20000-39999"
fi
# el drop de bf_auto debe seguir ANTES de las reglas de registro
first6="$(nft list chain ip6 rdpguard pre | grep -E '^\s+(ip6|tcp|meta)' | head -1)"
case "$first6" in
  *bf_auto*drop*) echo "  [ip6 guard] orden OK (bf_auto primero)" ;;
  *) echo "  !! bf_auto NO es la primera regla ip6 — REVISAR"; exit 1 ;;
esac

# --- 5) persistencia en /etc/nftables.conf ----------------------------------
echo "=== persistencia ==="
cp -a /etc/nftables.conf "$BK"
python3 - <<'PY'
import sys

p = '/etc/nftables.conf'
s = open(p).read()
changed = []


def table_block(src, header):
    start = src.find(header)
    if start < 0:
        return None, None
    depth = 0
    for i in range(start, len(src)):
        if src[i] == '{':
            depth += 1
        elif src[i] == '}':
            depth -= 1
            if depth == 0:
                return start, i + 1
    return None, None


def patch(header, old, new, label):
    global s
    a, b = table_block(s, header)
    if a is None:
        print(f'  !! no encuentro {header}')
        sys.exit(1)
    block = s[a:b]
    if old in block:
        s = s[:a] + block.replace(old, new) + s[b:]
        changed.append(label)
    elif new not in block:
        print(f'  !! {label}: ni rango viejo ni nuevo — revisar a mano')
        sys.exit(1)


patch('table ip nat {', 'tcp dport 10000-29999', 'tcp dport 10000-39999', 'v4-ingress')
patch('table ip rdpguard {', 'dport 10000-29999', 'dport 10000-39999', 'ip-guard')
patch('table ip6 rdpguard {', 'dport 20000-29999', 'dport 20000-39999', 'ip6-guard')

a, b = table_block(s, 'table ip6 nat {')
if a is None:
    print('  !! no encuentro table ip6 nat')
    sys.exit(1)
block = s[a:b]
if 'ssh_tcp_map' not in block:
    anchor_map = ('    map smb_tcp_map {\n'
                  '        type inet_service : ipv6_addr . inet_service\n'
                  '    }\n')
    ssh_map = ('    map ssh_tcp_map {\n'
               '        type inet_service : ipv6_addr . inet_service\n'
               '    }\n')
    if anchor_map not in block:
        print('  !! no encuentro el bloque map smb_tcp_map — revisar a mano')
        sys.exit(1)
    block = block.replace(anchor_map, anchor_map + ssh_map, 1)
    anchor_rule = '        tcp dport @smb_tcp_map dnat ip6 to tcp dport map @smb_tcp_map\n'
    ssh_rule = '        tcp dport @ssh_tcp_map dnat ip6 to tcp dport map @ssh_tcp_map\n'
    if anchor_rule not in block:
        print('  !! no encuentro la regla smb_tcp_map — revisar a mano')
        sys.exit(1)
    block = block.replace(anchor_rule, anchor_rule + ssh_rule, 1)
    s = s[:a] + block + s[b:]
    changed.append('ip6-nat-ssh-map')

if changed:
    open(p, 'w').write(s)
    print(f'  persistido: {", ".join(changed)}')
else:
    print('  ya persistido todo')
PY

if nft -c -f /etc/nftables.conf; then
  echo "  conf valida OK"
else
  echo "  !! conf NO valida — restauro $BK"; cp -a "$BK" /etc/nftables.conf; exit 1
fi
echo "SSHRANGE_OK $(hostname)"

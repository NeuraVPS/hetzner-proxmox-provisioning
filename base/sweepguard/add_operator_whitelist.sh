#!/usr/bin/env bash
# Whitelist única del servidor de desarrollo del operador (IPv4 + IPv6) en el
# anti-fuerza-bruta de la BASE.
#
# Por qué (2026-08-07)
# --------------------
# El dev server del operador accede a MUCHAS VMs de la flota por sus forwards
# (RDP/SMB/SSH = 20000/10000/30000+vmid) — y también corre automatización que
# barre cientos de puertos. El detector de barridos del sweepguard lo lee como
# un port-scanner y mete su IP en bf_auto (bloqueo 24h que dropea TODO su
# tráfico a la base). Igual que las IPs main de las bases están en `bf_allow`,
# el dev server merece la misma exención.
#
# Diseño: `bf_allow` por familia. En IPv4 ya existe (table ip rdpguard) con
# regla `ip saddr @bf_allow accept` y el sweepguard lo respeta. En IPv6 NO
# existía; se crea `set bf_allow` en `table ip6 rdpguard` con **el mismo
# nombre** — así `sweepguard.py` (que ya hace `set_elements(family,"bf_allow")`
# para ambas familias) lo respeta AUTOMÁTICAMENTE, sin tocar el .py — más una
# regla `ip6 saddr @bf_allow accept` la PRIMERA de la cadena (antes del drop de
# bf_auto). Idempotente. Backup + nft -c + rollback.
set -euo pipefail

# --- IPs del dev server (única fuente; edita aquí para añadir/quitar) --------
DEV_V4=("188.40.215.246")
DEV_V6=("2a01:4f8:2240:1d48::2")

echo "=== whitelist operador en $(hostname) ==="
BK="/etc/nftables.conf.bak.opwl.$(date +%Y%m%d-%H%M%S)"

# --- 1) IPv4: añadir al bf_allow existente (runtime) ------------------------
for ip in "${DEV_V4[@]}"; do
  nft add element ip rdpguard bf_allow "{ $ip }" 2>/dev/null || true
  echo "  [ipv4] $ip -> bf_allow"
done

# --- 2) IPv6: crear set bf_allow + regla accept primera + elementos ---------
if ! nft list set ip6 rdpguard bf_allow >/dev/null 2>&1; then
  nft add set ip6 rdpguard bf_allow '{ type ipv6_addr ; flags interval ; auto-merge ; }'
  echo "  [ip6] set bf_allow creado"
fi
for ip in "${DEV_V6[@]}"; do
  nft add element ip6 rdpguard bf_allow "{ $ip }" 2>/dev/null || true
  echo "  [ip6] $ip -> bf_allow"
done
# la regla accept debe ir ANTES del drop de bf_auto -> insert (prepende)
if ! nft list chain ip6 rdpguard pre | grep -q '@bf_allow accept'; then
  nft insert rule ip6 rdpguard pre ip6 saddr @bf_allow accept
  echo "  [ip6] regla 'saddr @bf_allow accept' insertada (primera)"
else
  echo "  [ip6] regla accept ya presente"
fi
# comprobar que quedó la PRIMERA (antes de cualquier drop de bf_auto)
first6="$(nft list chain ip6 rdpguard pre | grep -E '^\s+(ip6|tcp|meta)' | head -1)"
case "$first6" in
  *bf_allow*accept*) echo "  [ip6] orden OK (bf_allow accept primero)" ;;
  *) echo "  !! bf_allow accept NO es la primera regla ip6 — REVISAR"; exit 1 ;;
esac

# --- 3) persistencia en /etc/nftables.conf ----------------------------------
echo "=== persistencia ==="
cp -a /etc/nftables.conf "$BK"
# Persistencia con python; las IPs se pasan por argv (única fuente arriba).
python3 - "${DEV_V4[@]}" "${DEV_V6[@]}" <<'PY'
import re, sys
p = '/etc/nftables.conf'
s = open(p).read()
v4 = [a for a in sys.argv[1:] if ':' not in a]
v6 = [a for a in sys.argv[1:] if ':' in a]
changed = []


def block(src, header):
    i = src.find(header)
    if i < 0:
        return None, None
    depth = 0
    for j in range(i, len(src)):
        if src[j] == '{':
            depth += 1
        elif src[j] == '}':
            depth -= 1
            if depth == 0:
                return i, j + 1
    return None, None


# --- IPv4: insertar la IP como PRIMER elemento del set bf_allow (con coma).
# Insertar al principio evita el problema del ultimo-elemento-sin-coma y de los
# comentarios en linea. Se ancla a `set bf_allow {` para no tocar bf_static.
a, b = block(s, 'table ip rdpguard {')
if a is None:
    print('  !! no encuentro table ip rdpguard'); sys.exit(1)
blk = s[a:b]
for ip in v4:
    if ip in blk:
        continue
    m = re.search(r'set bf_allow \{.*?elements = \{\n', blk, re.S)
    if not m:
        print('  !! no encuentro elements de ip bf_allow'); sys.exit(1)
    ins = m.end()
    blk = blk[:ins] + '            ' + ip + ',   # dev-operador\n' + blk[ins:]
    changed.append('ipv4:' + ip)
s = s[:a] + blk + s[b:]

# --- IPv6: set bf_allow + regla accept en table ip6 rdpguard
a, b = block(s, 'table ip6 rdpguard {')
if a is None:
    print('  !! no encuentro table ip6 rdpguard'); sys.exit(1)
blk = s[a:b]
if 'set bf_allow' not in blk:
    elems = ',\n            '.join(v6)
    setdef = ('    set bf_allow {\n'
              '        type ipv6_addr\n'
              '        flags interval\n'
              '        auto-merge\n'
              '        elements = {\n'
              '            ' + elems + '\n'
              '        }\n'
              '    }\n\n')
    # insertar el set justo despues de la apertura de la tabla
    nl = blk.find('\n') + 1
    blk = blk[:nl] + setdef + blk[nl:]
    changed.append('ip6:set bf_allow')
else:
    # set ya existe: asegurar los elementos
    for ip in v6:
        if ip not in blk:
            blk = re.sub(r'(set bf_allow \{.*?elements = \{)',
                         r'\1\n            ' + ip + ',', blk, count=1, flags=re.S)
            changed.append('ip6:' + ip)
# regla accept la primera de chain pre (tras la linea 'type filter hook...')
if '@bf_allow accept' not in blk:
    blk = re.sub(r'(type filter hook prerouting priority -150; policy accept;\n)',
                 r'\1        ip6 saddr @bf_allow accept\n', blk, count=1)
    changed.append('ip6:accept-rule')
s = s[:a] + blk + s[b:]

if changed:
    open(p, 'w').write(s)
    print('  persistido: ' + ', '.join(changed))
else:
    print('  ya persistido todo')
PY

if nft -c -f /etc/nftables.conf; then
  echo "  conf valida OK"
else
  echo "  !! conf NO valida — restauro $BK"; cp -a "$BK" /etc/nftables.conf; exit 1
fi
echo "OPWL_OK $(hostname)"

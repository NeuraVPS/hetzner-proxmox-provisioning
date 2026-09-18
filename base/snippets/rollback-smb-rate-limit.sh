#!/usr/bin/env bash
# Deshace deploy-smb-rate-limit.sh. Atomico, sin flush ni recarga.
set -euo pipefail
CONF=${CONF:-/etc/nftables.conf}

echo "=== rollback limite de ritmo SMB en $(hostname) ==="

CH=$(nft -a list chain inet filter forward)
if printf '%s' "$CH" | grep -q '@smb_rl6'; then
  TX=$(mktemp --suffix=.nft /root/smb-rl-rb.XXXXXX)
  for h in $(printf '%s' "$CH" | awk '/@smb_rl[46]/{print $NF}'); do
    echo "delete rule inet filter forward handle $h" >> "$TX"
  done
  echo "delete set inet filter smb_rl4" >> "$TX"
  echo "delete set inet filter smb_rl6" >> "$TX"
  echo "delete counter inet filter smb_rl_drops" >> "$TX"
  nft -c -f "$TX"; nft -f "$TX"
  echo "  [vivo] deshecho ($TX)"
else
  echo "  [vivo] nada que deshacer"
fi

if grep -q 'smb_rl6' "$CONF"; then
  BK="$CONF.bak.smbrl-rb.$(date +%Y%m%d-%H%M%S)"; cp -a "$CONF" "$BK"
  python3 - "$CONF" <<'PY'
import re, sys
conf = sys.argv[1]; s = open(conf).read()
s = re.sub(r"    counter smb_rl_drops \{\n    \}\n\n", "", s, count=1)
s = re.sub(r"    set smb_rl4 \{\n(?:        [^\n]*\n)*?    \}\n\n", "", s, count=1)
s = re.sub(r"    set smb_rl6 \{\n(?:        [^\n]*\n)*?    \}\n\n", "", s, count=1)
s = re.sub(r'        iifname "tun-\*" oifname "tun-\*" meta nfproto ip[v46]+ tcp dport \{ 139, 445 \}[^\n]*smb_rl[46][^\n]*drop\n', "", s)
assert "smb_rl" not in s, "quedan restos de smb_rl"
open(conf, "w").write(s)
print("  [conf] sets, contador y reglas quitados")
PY
  if nft -c -f "$CONF"; then echo "  [conf] sintaxis OK (no cargada)"
  else echo "  !! conf no valida — restauro"; cp -a "$BK" "$CONF"; exit 1; fi
else
  echo "  [conf] nada que revertir"
fi
echo "SMB_RL_ROLLBACK_OK $(hostname)"

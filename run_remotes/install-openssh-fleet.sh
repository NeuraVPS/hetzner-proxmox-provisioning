#!/usr/bin/env bash
# Sweep de flota: instala/configura OpenSSH Server (windows_vm/install_openssh.ps1)
# en todos los guests Windows RUNNING de todos los nodos. Corre EN UNA BASE.
#
# Paralelismo: NODE_PARALLEL nodos a la vez (una sesión SSH base->nodo cada
# uno; mantener <=12 entre ambas bases — base/README.md) y VM_PARALLEL VMs
# dentro de cada nodo vía qm guest exec. NUNCA reinicia nada.
#
# Preparación (desde la máquina del operador):
#   python3 -c "import base64;print(base64.b64encode(open('windows_vm/install_openssh.ps1','rb').read().decode().encode('utf-16-le')).decode())" > /tmp/payload.b64
#   scp /tmp/payload.b64 run_remotes/install-openssh-fleet.sh run_remotes/openssh-fleet-node-task.sh b0:/root/openssh-sweep/
#   ssh b0 'cd /root/openssh-sweep && nohup bash install-openssh-fleet.sh > sweep.out 2>&1 &'
#
# Salida: results.log con líneas "<node> VMRES <vmid> OK|FAIL <detalle>".
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
NODES_FILE="${NODES_FILE:-/var/lib/base-nat/pve_nodes.json}"
NODE_PARALLEL="${NODE_PARALLEL:-10}"
RESULTS="${RESULTS:-$DIR/results.log}"

python3 - "$NODES_FILE" > "$DIR/nodes.txt" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
for name in sorted(d):
    ip = d[name] if isinstance(d[name], str) else d[name].get("ip", "")
    if ip:
        print(name, ip)
PY
total=$(wc -l < "$DIR/nodes.txt")
echo "sweep: $total nodos, NODE_PARALLEL=$NODE_PARALLEL ($(date -u +%FT%TZ))"

xargs -a "$DIR/nodes.txt" -P "$NODE_PARALLEL" -L1 bash "$DIR/openssh-fleet-node-task.sh" >> "$RESULTS" 2>"$DIR/errors.log"

echo "sweep: fin ($(date -u +%FT%TZ))"
echo "== resumen =="
awk '$2=="VMRES" {print $4}' "$RESULTS" | sort | uniq -c
awk '$2=="VMRES" && $4=="FAIL"' "$RESULTS" > "$DIR/failed.log" || true
echo "fallos en $DIR/failed.log: $(wc -l < "$DIR/failed.log")"

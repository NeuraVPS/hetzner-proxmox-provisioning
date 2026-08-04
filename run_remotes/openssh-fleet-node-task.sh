#!/usr/bin/env bash
# Tarea por nodo del sweep OpenSSH (la invoca install-openssh-fleet.sh vía
# xargs). Abre UNA sesión SSH al nodo y dentro paraleliza VM_PARALLEL
# guest-execs. Prefija cada línea con el nombre del nodo.
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
name="$1"; ip="$2"
VM_PARALLEL="${VM_PARALLEL:-4}"
GUEST_TIMEOUT="${GUEST_TIMEOUT:-240}"
PSB64=$(cat "$DIR/payload.b64")

ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o ConnectTimeout=10 -o ForwardAgent=yes "root@$ip" \
    "bash -s -- '$PSB64' '$VM_PARALLEL' '$GUEST_TIMEOUT'" <<'REMOTE' | sed "s/^/$name /"
PSB64="$1"; VMPAR="$2"; GTMO="$3"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
# Solo VMs running; 100/101 son plantillas en mantenimiento, fuera.
qm list 2>/dev/null | tail -n +2 | awk '$3=="running" {print $1}' \
  | grep -vE '^(100|101)$' > "$tmp/vms" || true

run_one() {
  vmid="$1"
  out=$(qm guest exec "$vmid" --timeout "$GTMO" -- powershell -NoProfile -EncodedCommand "$PSB64" 2>&1)
  res=$(printf '%s\n' "$out" | python3 -c '
import json, sys
raw = sys.stdin.read()
try:
    start = raw.index("{")
    d = json.loads(raw[start:raw.rindex("}") + 1], strict=False)
except Exception:
    print("FAIL exec-error " + raw.strip().replace("\n", " ")[:120])
    raise SystemExit
if "exitcode" not in d and "exited" not in d:
    # qm devolvio solo el pid: timeout del guest exec (estado desconocido)
    print("FAIL guest-timeout pid=" + str(d.get("pid", "")))
    raise SystemExit
o = d.get("out-data") or ""
for l in o.splitlines():
    if l.startswith("RESULT:OK"):
        print("OK")
        break
    if l.startswith("RESULT:FAIL"):
        print("FAIL " + l[12:132].replace("\n", " "))
        break
else:
    print("FAIL no-result exitcode=" + str(d.get("exitcode")))
')
  echo "VMRES $vmid $res"
}

i=0
while read -r vmid; do
  run_one "$vmid" &
  i=$((i + 1))
  if [ $((i % VMPAR)) -eq 0 ]; then wait; fi
done < "$tmp/vms"
wait
echo "NODE_DONE $(wc -l < "$tmp/vms") vms"
REMOTE
rc=$?
if [ $rc -ne 0 ]; then echo "$name NODE_SSH_FAIL rc=$rc"; fi

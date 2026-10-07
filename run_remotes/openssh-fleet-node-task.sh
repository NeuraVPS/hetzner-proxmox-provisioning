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

# qemu-ga guarda el resultado de cada proceso terminado hasta que alguien lo
# pide por PID, y Windows reutiliza PID: `exec-status <pid>` puede devolver
# primero la entrada vieja de OTRO proceso (vm749, 04/10/2026: exit 0 y la
# salida del script SMB). Y `qm guest exec` sincrono se queda con el primer
# `exited` que le den. Por eso: --synchronous 0, el script imprime
# NV-RUN-<nonce> como primera linea, y se sondea ESE PID hasta verla. Lo
# ajeno se descarta y se vuelve a preguntar; a la tercera, FAIL. NUNCA se
# relanza: el instalador escribe. Mismo patron que functions/guest_run_nonce.py
# y el bloque nv_* de neuravps-egresscheck.py (PR #261).
NV_PY=$(cat <<'PY'
import base64, json, os, subprocess, sys, time
vmid, psb64, gtmo = sys.argv[1], sys.argv[2], int(sys.argv[3])
nonce = os.urandom(16).hex()

def una_linea(raw, n=120):
    return " ".join((raw or "").split())[:n]

def qm(*args):
    r = subprocess.run(["qm", "guest", *args], stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=60)
    return (r.stdout or "") + (r.stderr or "")

def js(raw):
    try:
        return json.loads(raw[raw.index("{"):raw.rindex("}") + 1], strict=False)
    except Exception:
        return None

# La cola repite lo que hace powershell.exe con la ultima sentencia: el codigo
# de salida no cambia; un `exit N` explicito se la salta (sin marca final).
ps = base64.b64decode(psb64).decode("utf-16-le")
ps = f"'NV-RUN-{nonce}';{ps}\n;$nvok=$?;'NV-END-{nonce}';if(-not $nvok){{exit 1}}"
b64 = base64.b64encode(ps.encode("utf-16-le")).decode()
try:
    raw = qm("exec", vmid, "--synchronous", "0", "--",
             "powershell", "-NoProfile", "-EncodedCommand", b64)
except Exception as e:
    print("FAIL exec-error " + una_linea(str(e))); sys.exit()
d = js(raw)
pid = d.get("pid") if isinstance(d, dict) else None
if not pid:
    print("FAIL exec-error " + una_linea(raw)); sys.exit()

fin = time.monotonic() + gtmo
ajenos = 0
while True:
    if time.monotonic() > fin:
        # estado desconocido: puede seguir corriendo en el invitado
        print(f"FAIL guest-timeout pid={pid}"); sys.exit()
    try:
        raw = qm("exec-status", vmid, str(pid))
    except subprocess.TimeoutExpired:
        continue
    d = js(raw)
    if not isinstance(d, dict):
        print(f"FAIL exec-status-error pid={pid} " + una_linea(raw)); sys.exit()
    if not d.get("exited"):
        time.sleep(2); continue
    cuerpo = (d.get("out-data") or "").lstrip("﻿ \t\r\n")
    primera, _, resto = cuerpo.partition("\n")
    if primera.strip() != "NV-RUN-" + nonce:
        ajenos += 1
        if ajenos >= 3:
            print(f"FAIL foreign-result pid={pid} (qemu-ga devolvio el de otro proceso con el mismo PID)")
            sys.exit()
        continue
    break

for l in resto.splitlines():
    if l.startswith("RESULT:OK"):
        print("OK")
        break
    if l.startswith("RESULT:FAIL"):
        print("FAIL " + l[12:132].replace("\n", " "))
        break
else:
    print("FAIL no-result exitcode=" + str(d.get("exitcode")))
PY
)

run_one() {
  vmid="$1"
  res=$(python3 -c "$NV_PY" "$vmid" "$PSB64" "$GTMO" 2>&1 | tail -n 1)
  case "$res" in
    OK|FAIL\ *) ;;
    *) res="FAIL node-error ${res:0:120}" ;;
  esac
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

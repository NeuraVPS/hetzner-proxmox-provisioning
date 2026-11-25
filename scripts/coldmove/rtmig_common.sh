# rtmig_common.sh — lo que comparten rtmig_send.sh y rtmig_cutover.sh. Se carga con `source`.
#
# DOS MODOS (RTMIG_MODE), según qué nodo puede abrir SSH hacia el otro:
#   push (por defecto)  el kit corre en el ORIGEN y empuja al destino. Lo normal entre nodos de la
#                       flota (todos tienen la clave de flota) y hacia un nodo de otro proveedor que
#                       autoriza la clave de flota (p. ej. Kademar como DESTINO).
#   pull                el kit corre en el DESTINO (un nodo de Hetzner) y tira del origen. Para un
#                       ORIGEN sin clave de flota (proxmox_nodes.provider distinto de hetzner, p. ej.
#                       0000266-KDM-9755, decisión de Iván «B+» del 25/11/2026): ese nodo acepta SSH
#                       de la flota pero NO puede entrar en ningún nodo nuestro. En este modo TODAS las
#                       conexiones las abre el destino; al origen solo le llegan órdenes sueltas
#                       (zfs, qm, cat, sha256sum, rm, mv…) y ninguna de ellas hace ssh.
# En los dos modos el estado, la copia aparcada y el cerrojo viven en $WORK del nodo que corre el kit.
#
# En modo pull el ORIGEN es NO FIABLE (puede estar comprometido): todo lo que devuelve (contadores, guid,
# configs, .fw, nombres, firmas de CPU) se valida aquí como entrada hostil, y nada suyo se ejecuta ni se
# evalúa. Su config y su .fw no se publican tal cual en el destino: pasan por un perfil con lista
# blanca (rtmig_cutover.sh, sanitize_conf / sanitize_fw). No se le reenvía nunca el agente SSH.
#
# Variables comunes (todas por entorno; nada de valores de un traslado concreto en el código):
#   VMID      (obligatoria) VMID de la VM; es la misma en origen y destino.
#   push:  DST       IP del nodo DESTINO, tal cual en proxmox_nodes/<nodo>.ip (canónica, sin zona).
#          DST_NODE  id del nodo destino (= hostname = nodo PVE); se comprueba con `hostname` allí.
#   pull:  SRC       IP del nodo ORIGEN, tal cual en proxmox_nodes/<nodo>.ip (canónica, sin zona).
#          SRC_NODE  id del nodo origen; se comprueba con `hostname` allí.
#          DST_NODE  OBLIGATORIO también: este nodo; tiene que ser igual a `hostname` (el corte queda
#                    ligado al par origen/destino explícito).
#   WORK      (por defecto /root/rtmig) carpeta de trabajo del nodo que corre el kit.
#   DISKS     (opcional) sufijos N de rpool/data/vm-$VMID-disk-N. Si se pone, tiene que coincidir
#             EXACTAMENTE con el inventario de la config; si no, se usa el inventario.
#   SSH_PORT  (por defecto 22) puerto SSH del otro nodo.
#
# Rutas que se pueden desviar (solo para pruebas; en el otro nodo las expande SU shell, así que por
# SSH real valen siempre los valores por defecto): PVE_ETC, CPUINFO, NV_RAM_GUARD, NV_RECONCILER,
# QEMU_PIDDIR, PROC_DIR, NV_FLEET_KEY.
#
# Regla de las comprobaciones: nunca se toma «el SSH falló» por «no hay nada». Cada consulta devuelve
# una respuesta explícita terminada en un marcador; sin él, el estado es INDETERMINADO y se aborta sin
# tocar nada.
set -euo pipefail
shopt -s inherit_errexit   # un die dentro de $(...) también corta la orden que lo llama

RTMIG_MODE="${RTMIG_MODE:-push}"
VMID="${VMID:?VMID}"
WORK="${WORK:-/root/rtmig}"
POOLDS="${POOLDS:-rpool/data}"
PVE_ETC="${PVE_ETC:-/etc/pve}"
CPUINFO="${CPUINFO:-/proc/cpuinfo}"
NV_RAM_GUARD="${NV_RAM_GUARD:-/usr/local/sbin/neuravps-ram-guard.py}"
NV_RECONCILER="${NV_RECONCILER:-/usr/local/sbin/neuravps-balloon-reconciler.sh}"
QEMU_PIDDIR="${QEMU_PIDDIR:-/var/run/qemu-server}"
PROC_DIR="${PROC_DIR:-/proc}"
NV_FLEET_KEY="${NV_FLEET_KEY:-/root/.ssh/neuravps_id}"

# ---- validación de todo lo que acaba dentro de una orden remota -----------------------------------
[[ "$VMID" =~ ^[1-9][0-9]*$ ]] || { echo "VMID no válida: $VMID" >&2; exit 2; }
# IP literal, canónica (como la escribe ipaddress) y sin zona (%eth0): es lo que se mete en ssh.
canon_ip() {
  python3 -c '
import ipaddress, sys
s = sys.argv[1]
try:
    ip = ipaddress.ip_address(s)
except ValueError:
    sys.exit(1)
sys.exit(0 if "%" not in s and str(ip) == s else 1)' "$1" 2>/dev/null
}
valid_node() { [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]]; }
LOCAL_NODE="$(hostname)"
case "$RTMIG_MODE" in
  push)
    DST="${DST:?DST (IP del nodo destino, de proxmox_nodes.ip)}"
    DST_NODE="${DST_NODE:?DST_NODE (id del nodo destino; se comprueba con hostname)}"
    canon_ip "$DST" || { echo "DST tiene que ser una IP literal canónica y sin zona (proxmox_nodes.ip), no '$DST'" >&2; exit 2; }
    valid_node "$DST_NODE" || { echo "DST_NODE no válido: $DST_NODE" >&2; exit 2; }
    SRC_NODE="$LOCAL_NODE"; PEER="$DST"; PEER_NODE="$DST_NODE"; PEER_ROLE=dst ;;
  pull)
    SRC="${SRC:?SRC (IP del nodo origen, de proxmox_nodes.ip)}"
    SRC_NODE="${SRC_NODE:?SRC_NODE (id del nodo origen; se comprueba con hostname)}"
    canon_ip "$SRC" || { echo "SRC tiene que ser una IP literal canónica y sin zona (proxmox_nodes.ip), no '$SRC'" >&2; exit 2; }
    valid_node "$SRC_NODE" || { echo "SRC_NODE no válido: $SRC_NODE" >&2; exit 2; }
    [[ -n "${DST_NODE:-}" ]] || { echo "en modo pull hace falta DST_NODE explícito (este nodo, ${LOCAL_NODE})" >&2; exit 2; }
    [[ "$DST_NODE" == "$LOCAL_NODE" ]] \
      || { echo "en modo pull el kit corre en el DESTINO: DST_NODE=${DST_NODE} pero este nodo es ${LOCAL_NODE}" >&2; exit 2; }
    DST_NODE="$LOCAL_NODE"; PEER="$SRC"; PEER_NODE="$SRC_NODE"; PEER_ROLE=src ;;
  *) echo "RTMIG_MODE solo admite push o pull (vale '${RTMIG_MODE}')" >&2; exit 2 ;;
esac
[[ "$WORK" =~ ^/[A-Za-z0-9._/-]+$ && "$WORK" != *..* ]] || { echo "WORK no válido (ruta absoluta, sin espacios ni ..): $WORK" >&2; exit 2; }
[[ "$POOLDS" =~ ^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*$ ]] || { echo "POOLDS no válido: $POOLDS" >&2; exit 2; }
[[ "${SSH_PORT:-22}" =~ ^[0-9]+$ ]] || { echo "SSH_PORT no válido: ${SSH_PORT}" >&2; exit 2; }

# Rutas en la forma que expande el shell de QUIEN ejecuta la orden (local o remoto).
R_CONF="\${PVE_ETC:-/etc/pve}/qemu-server/${VMID}.conf"
R_FW="\${PVE_ETC:-/etc/pve}/firewall/${VMID}.fw"
STATE="$WORK/${VMID}.state"

# -a / ForwardAgent=no / sin reenvíos: el otro nodo (en pull, uno externo) nunca recibe el agente.
SSHO=(-a -x -o ForwardAgent=no -o ClearAllForwardings=yes
      -p "${SSH_PORT:-22}" -c aes128-gcm@openssh.com -o BatchMode=yes -o ConnectTimeout=15
      -o ServerAliveInterval=30 -o ServerAliveCountMax=6
      -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"; }
warn() { log "AVISO: $*"; }
die() { log "ERROR: $*" >&2; exit 1; }   # a stderr: también se ve desde dentro de $(...)

# ---- ejecución por ROL ------------------------------------------------------------------------------
# peer: SSH al otro nodo, SIEMPRE iniciado por el nodo que corre el kit. local_sh: el mismo texto de
# orden ejecutado aquí, con las mismas variables que vería el otro lado.
peer()    { ssh -n "${SSHO[@]}" "root@${PEER}" "$@"; }    # sin stdin (no se come el del llamador)
peer_in() { ssh "${SSHO[@]}" "root@${PEER}" "$@"; }       # con stdin (flujo zfs, ficheros)
local_sh() {
  PVE_ETC="$PVE_ETC" CPUINFO="$CPUINFO" NV_RAM_GUARD="$NV_RAM_GUARD" NV_RECONCILER="$NV_RECONCILER" \
    QEMU_PIDDIR="$QEMU_PIDDIR" PROC_DIR="$PROC_DIR" bash -c "$1"
}
# on <src|dst> "<orden>" (sin stdin) / on_in <src|dst> "<orden>" (con stdin)
on() {
  if [[ "$1" == "$PEER_ROLE" ]]; then peer "$2"; else local_sh "$2" </dev/null; fi
}
on_in() {
  if [[ "$1" == "$PEER_ROLE" ]]; then peer_in "$2"; else local_sh "$2"; fi
}
role_name() { [[ "$1" == src ]] && echo "origen ${SRC_NODE}" || echo "destino ${DST_NODE}"; }

check_snap_name() {
  [[ "$1" =~ ^rtmig-[A-Za-z0-9_.:-]+$ ]] || die "el snapshot '$1' debe empezar por rtmig- (solo letras, números y _.:-)"
}

# El nodo que corre el kit tiene que tener la clave de flota (es quien abre las conexiones). Si no la
# tiene, es justo el caso del modo pull: el kit se corre en el otro nodo.
check_fleet_key() {
  [[ -s "$NV_FLEET_KEY" ]] && return 0
  if [[ "$RTMIG_MODE" == push ]]; then
    die "este nodo (${LOCAL_NODE}) no tiene la clave de flota (${NV_FLEET_KEY}): no puede abrir SSH hacia ${DST_NODE}. Si es un nodo de otro proveedor (proxmox_nodes.provider ≠ hetzner) y es el ORIGEN, corre el kit en el DESTINO con RTMIG_MODE=pull SRC=<IP de este nodo> SRC_NODE=${LOCAL_NODE}"
  fi
  die "este nodo (${LOCAL_NODE}) no tiene la clave de flota (${NV_FLEET_KEY}): en modo pull el kit tiene que correr en un nodo de Hetzner (el destino)"
}

check_peer_identity() {
  local h
  h=$(peer hostname 2>/dev/null | tr -d '\r') || true
  [[ -n "$h" ]] || die "no llego a ${PEER} por SSH (puerto ${SSH_PORT:-22}); ¿está en el IPSET hosts-ipv6 y autoriza la clave de la flota?"
  [[ "$h" == "$PEER_NODE" ]] || die "${PEER} dice llamarse '${h}', no '${PEER_NODE}': no sigo"
  [[ "$h" != "$LOCAL_NODE" ]] || die "origen y destino son el mismo nodo (${h})"
}

# Un solo proceso del kit por VM en este nodo (cutover y envíos que escriben).
take_lock() {
  mkdir -p "$WORK"
  exec 8>"$WORK/${VMID}.lock"
  flock -n 8 || die "otro proceso del kit tiene el cerrojo de la VM ${VMID} ($WORK/${VMID}.lock); espera a que acabe"
}

# ---- sondas con respuesta positiva ------------------------------------------------------------------
probe_cmd() {
  cat <<EOF
P="\${PVE_ETC:-/etc/pve}"; C="\$P/qemu-server/${VMID}.conf"
if [ -f "\$P/.version" ] && [ -d "\$P/qemu-server" ]; then echo PVE=ok; else echo PVE=bad; fi
if [ -e "\$C" ]; then echo CONF=present; echo "SHA=\$(sha256sum < "\$C" | cut -d' ' -f1)"; else echo CONF=absent; fi
if [ -e "\$C" ]; then echo "UUID=\$(sed -n '/^\[/q; s/^smbios1:.*uuid=\([0-9A-Fa-f-]*\).*/\1/p' "\$C" | head -1)"; fi
F="\$P/firewall/${VMID}.fw"
if [ -e "\$F" ]; then echo FW=present; echo "FWSHA=\$(sha256sum < "\$F" | cut -d' ' -f1)"; else echo FW=absent; fi
st=\$(qm status ${VMID} 2>/dev/null | awk '{print \$2}'); echo "STATUS=\${st:-none}"
echo PROBE-END
EOF
}
# probe <src|dst> → P_CONF (present|absent), P_SHA, P_UUID, P_FW (present|absent), P_FWSHA, P_STATUS;
# muere si la respuesta no es completa.
probe() {
  local where="$1" out line
  P_CONF=""; P_SHA=""; P_UUID=""; P_FW=""; P_FWSHA=""; P_STATUS=""; local pve="" endok=0
  out=$(on "$where" "$(probe_cmd)" 2>/dev/null) || true
  while IFS= read -r line; do
    case "$line" in
      PVE=*) pve="${line#PVE=}" ;; CONF=*) P_CONF="${line#CONF=}" ;; SHA=*) P_SHA="${line#SHA=}" ;;
      UUID=*) P_UUID="${line#UUID=}" ;; FWSHA=*) P_FWSHA="${line#FWSHA=}" ;;
      FW=*) P_FW="${line#FW=}" ;; STATUS=*) P_STATUS="${line#STATUS=}" ;; PROBE-END) endok=1 ;;
    esac
  done <<< "$out"
  local name; name=$(role_name "$where")
  (( endok )) || die "estado INDETERMINADO en ${name}: la consulta no terminó (¿SSH cortado?). No se ha tocado nada; repite cuando responda"
  [[ "$pve" == ok ]] || die "estado INDETERMINADO en ${name}: /etc/pve no está disponible (pmxcfs). No se ha tocado nada"
  [[ ( "$P_CONF" == present || "$P_CONF" == absent ) && ( "$P_FW" == present || "$P_FW" == absent ) ]] \
    || die "estado INDETERMINADO en ${name}: respuesta rara (${out//$'\n'/ })"
  # Formato estricto de cada campo (el otro lado puede no ser fiable).
  [[ "$P_STATUS" =~ ^(running|stopped|paused|prelaunch|suspended|none)$ ]] \
    || die "estado INDETERMINADO en ${name}: estado de la VM con formato raro"
  if [[ "$P_CONF" == present ]]; then
    [[ "$P_SHA" =~ ^[0-9a-f]{64}$ && ( -z "$P_UUID" || "$P_UUID" =~ ^[0-9A-Fa-f-]{36}$ ) ]] \
      || die "estado INDETERMINADO en ${name}: sha o UUID de la config con formato raro"
  fi
  if [[ "$P_FW" == present ]]; then
    [[ "$P_FWSHA" =~ ^[0-9a-f]{64}$ ]] || die "estado INDETERMINADO en ${name}: sha del .fw con formato raro"
  fi
  if [[ "$P_CONF" == present && "$P_STATUS" == none ]]; then
    die "estado INDETERMINADO en ${name}: hay config de la VM ${VMID} pero qm status no responde"
  fi
}

# Propiedad ZFS con respuesta explícita, en cualquiera de los dos lados: imprime el valor, o ABSENT SOLO
# si ZFS dice que el objeto no existe («dataset does not exist»). Cualquier otro error de zfs, o el SSH
# caído, es estado INDETERMINADO y se muere.
zget() {   # <src|dst> <prop> <objeto> [-p]
  local side="$1" out val name
  [[ "$2" =~ ^[a-z_@:.A-Za-z0-9-]+$ && "$3" =~ ^[A-Za-z0-9_./@:-]+$ && "${4:-}" =~ ^(-p)?$ ]] \
    || die "consulta ZFS con parámetros raros: $*"
  name=$(role_name "$side")
  out=$(on "$side" "if o=\$(zfs get -H ${4:-} -o value '$2' '$3' 2>&1); then echo \"VAL=\$o\"; elif printf '%s' \"\$o\" | grep -q 'dataset does not exist'; then echo VAL=ABSENT; else echo \"ERR=\$(printf '%s' \"\$o\" | head -1)\"; fi; echo Q-END" 2>/dev/null) || true
  [[ "$out" == *Q-END* ]] || die "no pude consultar $2 de $3 en ${name} (SSH): estado INDETERMINADO"
  if grep -q '^ERR=' <<< "$out"; then
    die "zfs falló en ${name} al leer $2 de $3 ($(sed -n 's/^ERR=//p' <<< "$out")): estado INDETERMINADO"
  fi
  val=$(sed -n 's/^VAL=//p' <<< "$out" | head -1)
  [[ -n "$val" ]] || die "respuesta vacía de zfs en ${name} para $2 de $3: estado INDETERMINADO"
  # Entrada posiblemente hostil: guid y contadores, entero decimal estricto y acotado (cabe en la
  # aritmética de 64 bits del shell); el token, solo su alfabeto; el resto, sin caracteres de control.
  if [[ "$val" != ABSENT ]]; then
    case "$2" in
      guid) [[ "$val" =~ ^[0-9]{1,20}$ ]] || die "guid con formato raro en ${name} para $3: estado INDETERMINADO" ;;
      written@*|used|referenced|logicalreferenced)
        if [[ "${4:-}" == -p && ! "$val" =~ ^[0-9]{1,18}$ ]]; then
          die "contador $2 con formato raro en ${name} para $3: estado INDETERMINADO"
        fi ;;
      receive_resume_token) [[ "$val" == - || "$val" =~ ^[A-Za-z0-9-]{1,4096}$ ]] || die "token de reanudación raro en ${name} para $3" ;;
      *) val=$(printf '%s' "$val" | tr -cd '[:print:]' | cut -c1-200) ;;
    esac
  fi
  printf '%s\n' "$val"
}
dst_zget() { zget dst "$@"; }
src_zget_guid() { zget src guid "$1"; }

# Orden con resultado explícito en un lado: imprime su salida, o muere si falló (rc ≠ 0) o si la
# respuesta no llegó entera (SSH cortado: INDETERMINADO).
on_q() {   # <src|dst> "<orden>"
  local out rc
  out=$(on "$1" "{ $2 ; } 2>&1; echo \"RTMIG-Q-RC=\$?\"" 2>/dev/null) || true
  [[ "$out" == *RTMIG-Q-RC=* ]] || die "la orden en $(role_name "$1") no terminó (¿SSH cortado?): estado INDETERMINADO"
  rc=$(sed -n 's/^RTMIG-Q-RC=//p' <<< "$out" | tail -1)
  out=$(sed '/^RTMIG-Q-RC=/d' <<< "$out")
  [[ "$rc" == 0 ]] || die "falló en $(role_name "$1") (rc ${rc}): ${out:0:300}"
  [[ -z "$out" ]] || printf '%s\n' "$out"
}

# Publicación segura de un fichero en /etc/pve de un lado: se escribe a <final>.rtmig-tmp, se
# comprueba allí su sha256 y solo entonces se renombra (mv -n: nunca pisa) a la ruta final. pmxcfs
# hace atómico el rename (es lo que usa el propio PVE). Imprime PUBLISHED, BADSHA, EXISTS, WRITEFAIL,
# MVFAIL o NOANSWER (sin respuesta completa). <src|dst> <fichero local> <ruta final expandible>
publish() {
  local side="$1" f="$2" final="$3" sha out
  sha=$(sha256sum < "$f" | cut -d' ' -f1)
  out=$(on_in "$side" "T=\"${final}.rtmig-tmp\"; rm -f \"\$T\"; if ! cat > \"\$T\"; then rm -f \"\$T\"; echo PUB=WRITEFAIL; elif [ \"\$(sha256sum < \"\$T\" | cut -d' ' -f1)\" != '${sha}' ]; then rm -f \"\$T\"; echo PUB=BADSHA; elif [ -e \"${final}\" ]; then rm -f \"\$T\"; echo PUB=EXISTS; elif mv -n \"\$T\" \"${final}\" && [ ! -e \"\$T\" ]; then echo PUB=PUBLISHED; else rm -f \"\$T\"; echo PUB=MVFAIL; fi; echo PUB-END" < "$f" 2>/dev/null) || true
  if [[ "$out" == *PUB-END* ]]; then sed -n 's/^PUB=//p' <<< "$out" | head -1; else echo NOANSWER; fi
}
dst_publish() { publish dst "$@"; }

# Copia un fichero de un lado a un fichero local y comprueba su sha256. <src|dst> <ruta expandible>
# <fichero local> <sha esperado>
fetch() {
  local side="$1" path="$2" out="$3" want="$4"
  on "$side" "cat \"${path}\"" > "${out}.tmp" 2>/dev/null || { rm -f "${out}.tmp"; die "no pude leer ${path} en $(role_name "$side")"; }
  [[ "$(sha256sum < "${out}.tmp" | cut -d' ' -f1)" == "$want" ]] \
    || { rm -f "${out}.tmp"; die "la copia de ${path} desde $(role_name "$side") no coincide con su sha256: no sigo"; }
  mv "${out}.tmp" "$out"
}

# ---- fichero de estado (fases ligadas a modo, VM, UUID, origen, destino, snapshot) ------------------
state_get() { [[ -f "$STATE" ]] && sed -n "s/^$1=//p" "$STATE" | head -1 || true; }
state_set() {   # state_set KEY=VAL ... (fusiona y escribe de forma atómica)
  local tmp="$STATE.tmp.$$" kv k
  { if [[ -f "$STATE" ]]; then cat "$STATE"; fi; } > "$tmp"
  for kv in "$@"; do
    k="${kv%%=*}"
    grep -v "^${k}=" "$tmp" > "$tmp.2" || true; mv "$tmp.2" "$tmp"
    printf '%s\n' "$kv" >> "$tmp"
  done
  mv "$tmp" "$STATE"
}
conf_uuid() { awk '/^\[/{exit} /^smbios1:/{if (match($0,/uuid=[0-9A-Fa-f-]+/)) print substr($0,RSTART+5,RLENGTH-5); exit}' "$1"; }

# Estado de la versión anterior del kit (solo push, sin MODE ni PEER, con DST): se migra a mano de
# lo comprobable. Solo si ahora se corre en push, en el mismo nodo origen y hacia la MISMA IP y nodo
# destino que guardó; si no, se rechaza (no se adivina).
migrate_old_state() {
  [[ -z "$(state_get MODE)" ]] || return 0
  local old_dst; old_dst=$(state_get DST)
  [[ -n "$old_dst" && "$RTMIG_MODE" == push && "$old_dst" == "$DST" && "$(state_get DST_NODE)" == "$DST_NODE" \
     && "$(state_get SRC_NODE)" == "$SRC_NODE" ]] \
    || die "estado de una versión anterior del kit (sin MODE) que no corresponde a este traslado en push hacia ${DST_NODE:-?} (${DST:-?}): termínalo con la versión con la que se empezó"
  state_set "MODE=push" "PEER=${old_dst}"
  log "estado de la versión anterior del kit migrado (MODE=push, PEER=${old_dst})"
}

# Comprueba que el estado guardado es de ESTE traslado. Args: fases admitidas.
require_state() {
  local ph; ph=$(state_get PHASE)
  [[ -n "$ph" ]] || die "no hay estado de traslado en $STATE (¿falta park?)"
  local ok=0 p; for p in "$@"; do [[ "$ph" == "$p" ]] && ok=1; done
  (( ok )) || die "fase actual '${ph}', y este paso necesita: $*"
  [[ "$(state_get VMID)" == "$VMID" ]] || die "el estado es de la VM $(state_get VMID), no de ${VMID}"
  migrate_old_state
  [[ "$(state_get MODE)" == "$RTMIG_MODE" ]] || die "el estado es de un traslado en modo $(state_get MODE), no ${RTMIG_MODE}"
  [[ "$(state_get SRC_NODE)" == "$SRC_NODE" ]] || die "el estado es de un traslado desde $(state_get SRC_NODE), no desde ${SRC_NODE}"
  [[ "$(state_get POOLDS)" == "$POOLDS" ]] || die "el estado es del pool $(state_get POOLDS), no de ${POOLDS}"
  [[ "$(state_get DST_NODE)" == "$DST_NODE" ]] || die "el estado es de un traslado hacia $(state_get DST_NODE), no hacia ${DST_NODE}"
  [[ "$(state_get PEER)" == "$PEER" ]] || die "el estado es de un traslado con el otro nodo en $(state_get PEER), no en ${PEER}"
  [[ -f "$WORK/${VMID}.conf.src" ]] || die "falta la config aparcada $WORK/${VMID}.conf.src"
  [[ "$(conf_uuid "$WORK/${VMID}.conf.src")" == "$(state_get UUID)" ]] \
    || die "la config aparcada no es la de este traslado (UUID $(conf_uuid "$WORK/${VMID}.conf.src") ≠ $(state_get UUID))"
}

# ---- config e inventario de discos --------------------------------------------------------------
# Config de la VM en el ORIGEN, en un fichero local: la copia aparcada si ya existe (salvo con «live»);
# si no, la viva del origen (leída por el lado que toque) en un temporal. Imprime la ruta.
conf_file() {   # [live]
  if [[ "${1:-}" != live && -f "$WORK/${VMID}.conf.src" ]]; then echo "$WORK/${VMID}.conf.src"; return 0; fi
  mkdir -p "$WORK"
  local t="$WORK/${VMID}.conf.live"
  on src "cat \"${R_CONF}\"" > "$t" 2>/dev/null || { rm -f "$t"; return 1; }
  [[ -s "$t" ]] || { rm -f "$t"; return 1; }
  echo "$t"
}

# Inventario COMPLETO de volúmenes de una config (todas las secciones). Solo se admiten zvols
# local-zfs:vm-$VMID-disk-N; cualquier otro volumen (otro almacenamiento, otra VM, base-, vmstate,
# ruta cruda) hace abortar. CD-ROM y «none» no cuentan. Imprime los N ordenados.
disk_inventory() {
  local f="$1" line key val vol out=() bad=()
  while IFS= read -r line; do
    [[ "$line" =~ ^(scsi|sata|ide|virtio|efidisk|tpmstate|unused)[0-9]+:[[:space:]]*(.*)$ ]] || {
      [[ "$line" =~ ^vmstate: ]] && bad+=("$line"); continue; }
    key="${line%%:*}"; val="${BASH_REMATCH[2]}"; vol="${val%%,*}"
    [[ "$val" == *media=cdrom* || "$vol" == none ]] && continue
    if [[ "$vol" =~ ^local-zfs:vm-${VMID}-disk-([0-9]+)$ ]]; then out+=("${BASH_REMATCH[1]}")
    else bad+=("${key}: ${vol}"); fi
  done < "$f"
  (( ${#bad[@]} == 0 )) || die "volúmenes no soportados por el kit en la config de la VM ${VMID}: ${bad[*]}"
  (( ${#out[@]} > 0 )) || die "la config de la VM ${VMID} no tiene discos local-zfs:vm-${VMID}-disk-N"
  printf '%s\n' "${out[@]}" | sort -n -u | tr '\n' ' ' | sed 's/ $//'
}

detect_disks() {   # [live]
  local f inv want
  f=$(conf_file "${1:-}") || die "no hay config de la VM ${VMID} en el origen (ni viva ni aparcada)"
  inv=$(disk_inventory "$f")
  if [[ -n "${DISKS:-}" ]]; then
    want=$(tr -s ' \t' '\n' <<< "$DISKS" | sed '/^$/d' | sort -n -u | tr '\n' ' ' | sed 's/ $//')
    [[ "$want" =~ ^[0-9]+( [0-9]+)*$ ]] || die "DISKS no válido: '${DISKS}'"
    [[ "$want" == "$inv" ]] || die "DISKS='${DISKS}' no coincide con el inventario de la config (${inv}): no se envía un subconjunto"
  fi
  echo "$inv"
}

# "vendor|family|model" desde un /proc/cpuinfo por stdin
cpu_sig_from() {
  awk -F: '/^vendor_id/ && !sv {gsub(/[ \t]/,"",$2); v=$2; sv=1}
           /^cpu family/ && !sf {gsub(/[ \t]/,"",$2); f=$2; sf=1}
           /^model[ \t]*:/ && !sm {gsub(/[ \t]/,"",$2); m=$2; sm=1}
           END {printf "%s|%s|%s", v, f, m}'
}

# Último @rtmig-* presente en TODOS los discos de los dos lados con el mismo guid. Los nombres que
# devuelve el origen se filtran por el patrón de check_snap_name (los que no cuadran se ignoran).
last_common() {
  local cand g1 g2 ok first list
  first="${DISKS%% *}"
  list=$(on_q src "zfs list -H -t snapshot -o name -s createtxg '${POOLDS}/vm-${VMID}-disk-${first}'") || exit 1
  for cand in $(sed -n "s#^${POOLDS}/vm-${VMID}-disk-${first}@\(rtmig-[A-Za-z0-9_.:-]*\)\$#\1#p" <<< "$list" | tac); do
    ok=1
    for dd in $DISKS; do
      g1=$(src_zget_guid "${POOLDS}/vm-${VMID}-disk-${dd}@${cand}") || exit 1
      g2=$(dst_zget guid "${POOLDS}/vm-${VMID}-disk-${dd}@${cand}") || exit 1
      [[ "$g1" != ABSENT && "$g1" == "$g2" ]] || { ok=0; break; }
    done
    (( ok )) && { echo "$cand"; return 0; }
  done
  return 1
}

# Flujo ZFS de origen a destino, con el limitador en el nodo que corre el kit. Siempre lo abre el nodo
# que corre el kit: en push, `zfs send` aquí | ssh destino `zfs recv`; en pull, ssh origen `zfs send` |
# `zfs recv` aquí. Deja en STREAM_RC "envío pv recepción".  <orden de envío> <orden de recepción>
stream_src_to_dst() {
  local sendcmd="$1" recvcmd="$2" rc
  set +e
  if [[ "$RTMIG_MODE" == push ]]; then
    local_sh "$sendcmd" </dev/null | ratepipe | peer_in "$recvcmd"
  else
    peer "$sendcmd" | ratepipe | local_sh "$recvcmd"
  fi
  rc=("${PIPESTATUS[@]}")
  set -e
  STREAM_RC="${rc[0]} ${rc[1]} ${rc[2]}"
}

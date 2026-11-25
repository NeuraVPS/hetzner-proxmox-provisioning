# rtmig_common.sh — lo que comparten rtmig_send.sh y rtmig_cutover.sh. Se carga con `source`.
#
# Variables comunes (todas por entorno; nada de valores de un traslado concreto en el código):
#   VMID      (obligatoria) VMID de la VM; es la misma en origen y destino.
#   DST       (obligatoria) IP del nodo DESTINO tal cual está en proxmox_nodes/<nodo>.ip (en la flota,
#             IPv6; en una BASE: jq -r '."<nodo>"' /var/lib/base-nat/pve_nodes.json).
#   DST_NODE  (obligatoria) id del nodo destino (= hostname = nodo PVE, p. ej. el de proxmox_nodes).
#             Se comprueba con `hostname` en el destino antes de hacer nada: una IP mal copiada
#             no puede mandar discos a otro nodo.
#   WORK      (por defecto /root/rtmig) carpeta de trabajo en cada nodo (config aparcada, estado).
#   DISKS     (opcional) sufijos N de rpool/data/vm-$VMID-disk-N. Si se pone, tiene que coincidir
#             EXACTAMENTE con el inventario de la config; si no, se usa el inventario.
#   SSH_PORT  (por defecto 22) puerto SSH del destino.
#
# Rutas que se pueden desviar (solo para pruebas; en el destino las expande SU shell, así que por SSH
# real valen siempre los valores por defecto): PVE_ETC, CPUINFO, NV_RAM_GUARD, NV_RECONCILER.
#
# Regla de las comprobaciones remotas: nunca se toma «el SSH falló» por «no hay nada». Cada consulta
# al destino devuelve una respuesta explícita terminada en un marcador; sin él, el estado es
# INDETERMINADO y se aborta sin tocar nada.
set -euo pipefail
shopt -s inherit_errexit   # un die dentro de $(...) también corta la orden que lo llama

VMID="${VMID:?VMID}"
DST="${DST:?DST (IP del nodo destino, de proxmox_nodes.ip)}"
DST_NODE="${DST_NODE:?DST_NODE (id del nodo destino; se comprueba con hostname)}"
WORK="${WORK:-/root/rtmig}"
POOLDS="${POOLDS:-rpool/data}"
PVE_ETC="${PVE_ETC:-/etc/pve}"
CPUINFO="${CPUINFO:-/proc/cpuinfo}"
NV_RAM_GUARD="${NV_RAM_GUARD:-/usr/local/sbin/neuravps-ram-guard.py}"
NV_RECONCILER="${NV_RECONCILER:-/usr/local/sbin/neuravps-balloon-reconciler.sh}"

# ---- validación de todo lo que acaba dentro de una orden remota -----------------------------------
[[ "$VMID" =~ ^[1-9][0-9]*$ ]] || { echo "VMID no válida: $VMID" >&2; exit 2; }
python3 -c 'import ipaddress,sys; ipaddress.ip_address(sys.argv[1])' "$DST" 2>/dev/null \
  || { echo "DST tiene que ser una IP literal (proxmox_nodes.ip), no '$DST'" >&2; exit 2; }
[[ "$DST_NODE" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]] || { echo "DST_NODE no válido: $DST_NODE" >&2; exit 2; }
[[ "$WORK" =~ ^/[A-Za-z0-9._/-]+$ && "$WORK" != *..* ]] || { echo "WORK no válido (ruta absoluta, sin espacios ni ..): $WORK" >&2; exit 2; }
[[ "$POOLDS" =~ ^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*$ ]] || { echo "POOLDS no válido: $POOLDS" >&2; exit 2; }
[[ "${SSH_PORT:-22}" =~ ^[0-9]+$ ]] || { echo "SSH_PORT no válido: ${SSH_PORT}" >&2; exit 2; }

CONF="${PVE_ETC}/qemu-server/${VMID}.conf"; FW="${PVE_ETC}/firewall/${VMID}.fw"
# Las mismas rutas, pero escritas para que las expanda el shell del DESTINO.
R_CONF="\${PVE_ETC:-/etc/pve}/qemu-server/${VMID}.conf"
R_FW="\${PVE_ETC:-/etc/pve}/firewall/${VMID}.fw"
STATE="$WORK/${VMID}.state"

SSHO=(-p "${SSH_PORT:-22}" -c aes128-gcm@openssh.com -o BatchMode=yes -o ConnectTimeout=15
      -o ServerAliveInterval=30 -o ServerAliveCountMax=6
      -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
dst()    { ssh -n "${SSHO[@]}" "root@${DST}" "$@"; }    # sin stdin (no se come el del llamador)
dst_in() { ssh "${SSHO[@]}" "root@${DST}" "$@"; }       # con stdin (flujo zfs, ficheros)
log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"; }
warn() { log "AVISO: $*"; }
die() { log "ERROR: $*" >&2; exit 1; }   # a stderr: también se ve desde dentro de $(...)

src_node() { hostname; }

check_snap_name() {
  [[ "$1" =~ ^rtmig-[A-Za-z0-9_.:-]+$ ]] || die "el snapshot '$1' debe empezar por rtmig- (solo letras, números y _.:-)"
}

check_dst_identity() {
  local h
  h=$(dst hostname 2>/dev/null | tr -d '\r') || true
  [[ -n "$h" ]] || die "no llego al destino ${DST} por SSH (puerto ${SSH_PORT:-22}); ¿está en el IPSET hosts-ipv6 y tiene la clave de la flota?"
  [[ "$h" == "$DST_NODE" ]] || die "el destino ${DST} dice llamarse '${h}', no '${DST_NODE}': no sigo"
  [[ "$h" != "$(src_node)" ]] || die "origen y destino son el mismo nodo (${h})"
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
if [ -e "\$P/firewall/${VMID}.fw" ]; then echo FW=present; else echo FW=absent; fi
st=\$(qm status ${VMID} 2>/dev/null | awk '{print \$2}'); echo "STATUS=\${st:-none}"
echo PROBE-END
EOF
}
# probe <src|dst> → P_CONF (present|absent), P_SHA, P_FW, P_STATUS; muere si la respuesta no es completa.
probe() {
  local where="$1" out line
  P_CONF=""; P_SHA=""; P_FW=""; P_STATUS=""; local pve="" endok=0
  if [[ "$where" == dst ]]; then out=$(dst "$(probe_cmd)" 2>/dev/null) || true
  else out=$(PVE_ETC="$PVE_ETC" bash -c "$(probe_cmd)" 2>/dev/null) || true; fi
  while IFS= read -r line; do
    case "$line" in
      PVE=*) pve="${line#PVE=}" ;; CONF=*) P_CONF="${line#CONF=}" ;; SHA=*) P_SHA="${line#SHA=}" ;;
      FW=*) P_FW="${line#FW=}" ;; STATUS=*) P_STATUS="${line#STATUS=}" ;; PROBE-END) endok=1 ;;
    esac
  done <<< "$out"
  local name; [[ "$where" == dst ]] && name="destino ${DST_NODE}" || name="origen $(src_node)"
  (( endok )) || die "estado INDETERMINADO en ${name}: la consulta no terminó (¿SSH cortado?). No se ha tocado nada; repite cuando responda"
  [[ "$pve" == ok ]] || die "estado INDETERMINADO en ${name}: /etc/pve no está disponible (pmxcfs). No se ha tocado nada"
  [[ "$P_CONF" == present || "$P_CONF" == absent ]] || die "estado INDETERMINADO en ${name}: respuesta rara (${out//$'\n'/ })"
  if [[ "$P_CONF" == present && "$P_STATUS" == none ]]; then
    die "estado INDETERMINADO en ${name}: hay config de la VM ${VMID} pero qm status no responde"
  fi
}

# Consulta remota de una propiedad ZFS con respuesta explícita: imprime el valor, o ABSENT si el
# objeto no existe; muere si el SSH falla.
dst_zget() {   # <prop> <objeto> [-p]
  local out
  out=$(dst "if v=\$(zfs get -H ${3:-} -o value '$1' '$2' 2>/dev/null); then echo \"VAL=\$v\"; else echo VAL=ABSENT; fi; echo Q-END" 2>/dev/null) || true
  [[ "$out" == *Q-END* ]] || die "no pude consultar $1 de $2 en el destino (SSH): estado indeterminado"
  sed -n 's/^VAL=//p' <<< "$out" | head -1
}

# ---- fichero de estado (fases ligadas a VM, UUID, origen, destino, snapshot) ---------------------
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

# Comprueba que el estado guardado es de ESTE traslado. Args: fases admitidas.
require_state() {
  local ph; ph=$(state_get PHASE)
  [[ -n "$ph" ]] || die "no hay estado de traslado en $STATE (¿falta park?)"
  local ok=0 p; for p in "$@"; do [[ "$ph" == "$p" ]] && ok=1; done
  (( ok )) || die "fase actual '${ph}', y este paso necesita: $*"
  [[ "$(state_get VMID)" == "$VMID" ]] || die "el estado es de la VM $(state_get VMID), no de ${VMID}"
  [[ "$(state_get SRC_NODE)" == "$(src_node)" ]] || die "el estado es de un traslado desde $(state_get SRC_NODE), no desde $(src_node)"
  [[ "$(state_get DST_NODE)" == "$DST_NODE" && "$(state_get DST)" == "$DST" ]] \
    || die "el estado es de un traslado hacia $(state_get DST_NODE) ($(state_get DST)), no hacia ${DST_NODE} (${DST})"
  [[ -f "$WORK/${VMID}.conf.src" ]] || die "falta la config aparcada $WORK/${VMID}.conf.src"
  [[ "$(conf_uuid "$WORK/${VMID}.conf.src")" == "$(state_get UUID)" ]] \
    || die "la config aparcada no es la de este traslado (UUID $(conf_uuid "$WORK/${VMID}.conf.src") ≠ $(state_get UUID))"
}

# ---- config e inventario de discos --------------------------------------------------------------
# Config de la VM en este nodo: la viva o, si ya se aparcó, la copia de $WORK.
conf_file() {
  if [[ -f "$CONF" ]]; then echo "$CONF"
  elif [[ -f "$WORK/${VMID}.conf.src" ]]; then echo "$WORK/${VMID}.conf.src"
  else return 1; fi
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
  (( ${#bad[@]} == 0 )) || die "volúmenes no soportados por el kit en ${f}: ${bad[*]}"
  (( ${#out[@]} > 0 )) || die "la config ${f} no tiene discos local-zfs:vm-${VMID}-disk-N"
  printf '%s\n' "${out[@]}" | sort -n -u | tr '\n' ' ' | sed 's/ $//'
}

detect_disks() {
  local f inv want
  f=$(conf_file) || die "no hay config de la VM ${VMID} en este nodo (ni viva ni aparcada)"
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

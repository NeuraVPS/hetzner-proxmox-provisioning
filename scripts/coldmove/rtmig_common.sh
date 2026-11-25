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
#   DISKS     (opcional) sufijos N de rpool/data/vm-$VMID-disk-N. Por defecto salen de la config
#             (/etc/pve/qemu-server/$VMID.conf o, si ya está aparcada, $WORK/$VMID.conf.src).
#   SSH_PORT  (por defecto 22) puerto SSH del destino.
#
# Rutas que se pueden desviar (solo para pruebas; en el destino las expande SU shell, así que por SSH
# real valen siempre los valores por defecto): PVE_ETC, CPUINFO, NV_RAM_GUARD, NV_RECONCILER.
set -euo pipefail

VMID="${VMID:?VMID}"
DST="${DST:?DST (IP del nodo destino, de proxmox_nodes.ip)}"
DST_NODE="${DST_NODE:?DST_NODE (id del nodo destino; se comprueba con hostname)}"
WORK="${WORK:-/root/rtmig}"
POOLDS="${POOLDS:-rpool/data}"
PVE_ETC="${PVE_ETC:-/etc/pve}"
CPUINFO="${CPUINFO:-/proc/cpuinfo}"
NV_RAM_GUARD="${NV_RAM_GUARD:-/usr/local/sbin/neuravps-ram-guard.py}"
NV_RECONCILER="${NV_RECONCILER:-/usr/local/sbin/neuravps-balloon-reconciler.sh}"
CONF="${PVE_ETC}/qemu-server/${VMID}.conf"; FW="${PVE_ETC}/firewall/${VMID}.fw"
# Las mismas rutas, pero escritas para que las expanda el shell del DESTINO.
R_CONF="\${PVE_ETC:-/etc/pve}/qemu-server/${VMID}.conf"
R_FW="\${PVE_ETC:-/etc/pve}/firewall/${VMID}.fw"

[[ "$VMID" =~ ^[0-9]+$ ]] || { echo "VMID no numérica: $VMID" >&2; exit 2; }
python3 -c 'import ipaddress,sys; ipaddress.ip_address(sys.argv[1])' "$DST" 2>/dev/null \
  || { echo "DST tiene que ser una IP literal (proxmox_nodes.ip), no '$DST'" >&2; exit 2; }
[[ "$DST_NODE" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]] || { echo "DST_NODE no válido: $DST_NODE" >&2; exit 2; }

SSHO=(-p "${SSH_PORT:-22}" -c aes128-gcm@openssh.com -o BatchMode=yes -o ConnectTimeout=15
      -o ServerAliveInterval=30 -o ServerAliveCountMax=6
      -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
dst()    { ssh -n "${SSHO[@]}" "root@${DST}" "$@"; }    # sin stdin (no se come el del llamador)
dst_in() { ssh "${SSHO[@]}" "root@${DST}" "$@"; }       # con stdin (flujo zfs, ficheros)
log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"; }
warn() { log "AVISO: $*"; }
die() { log "ERROR: $*"; exit 1; }

src_node() { hostname; }

check_dst_identity() {
  local h
  h=$(dst hostname 2>/dev/null | tr -d '\r') || true
  [[ -n "$h" ]] || die "no llego al destino ${DST} por SSH (puerto ${SSH_PORT:-22}); ¿está en el IPSET hosts-ipv6 y tiene la clave de la flota?"
  [[ "$h" == "$DST_NODE" ]] || die "el destino ${DST} dice llamarse '${h}', no '${DST_NODE}': no sigo"
  [[ "$h" != "$(src_node)" ]] || die "origen y destino son el mismo nodo (${h})"
}

# Config de la VM en este nodo: la viva o, si ya se aparcó, la copia de $WORK.
conf_file() {
  if [[ -f "$CONF" ]]; then echo "$CONF"
  elif [[ -f "$WORK/${VMID}.conf.src" ]]; then echo "$WORK/${VMID}.conf.src"
  else return 1; fi
}

detect_disks() {
  if [[ -n "${DISKS:-}" ]]; then echo "$DISKS"; return 0; fi
  local f; f=$(conf_file) || die "no hay config de la VM ${VMID} en este nodo (ni viva ni aparcada) y DISKS no está puesto"
  local d
  d=$(grep -oE "local-zfs:vm-${VMID}-disk-[0-9]+" "$f" | sed 's/.*-disk-//' | sort -n -u | tr '\n' ' ' | sed 's/ $//')
  [[ -n "$d" ]] || die "la config ${f} no tiene discos local-zfs:vm-${VMID}-disk-N"
  echo "$d"
}

# "vendor|family|model" desde un /proc/cpuinfo por stdin
cpu_sig_from() {
  awk -F: '/^vendor_id/ && !sv {gsub(/[ \t]/,"",$2); v=$2; sv=1}
           /^cpu family/ && !sf {gsub(/[ \t]/,"",$2); f=$2; sf=1}
           /^model[ \t]*:/ && !sm {gsub(/[ \t]/,"",$2); m=$2; sm=1}
           END {printf "%s|%s|%s", v, f, m}'
}

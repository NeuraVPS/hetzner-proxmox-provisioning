#!/usr/bin/env bash
# rtmig_deploy.sh — copia el kit (rtmig_common.sh, rtmig_send.sh, rtmig_cutover.sh) a $WORK de los dos
# nodos y comprueba los md5. Se ejecuta en el equipo del operador; a los nodos se llega por un salto
# (una BASE con la clave de la flota), porque los nodos solo aceptan SSH desde las bases y entre sí.
# Escribe solo en $WORK de los dos nodos; no toca la VM ni ZFS.
#
#   JUMP=<alias ssh de la BASE> SRC=<IP origen> DST=<IP destino> scripts/coldmove/rtmig_deploy.sh
#
# SRC y DST: proxmox_nodes/<nodo>.ip (o, en la BASE, /var/lib/base-nat/pve_nodes.json).
# WORK por defecto /root/rtmig.
set -euo pipefail
cd "$(dirname "$0")"
JUMP="${JUMP:?JUMP (alias ssh de la BASE por la que se llega a los nodos)}"
SRC="${SRC:?SRC (IP del nodo origen)}"; DST="${DST:?DST (IP del nodo destino)}"
WORK="${WORK:-/root/rtmig}"
FILES="rtmig_common.sh rtmig_send.sh rtmig_cutover.sh"
for N in "$SRC" "$DST"; do
  python3 -c 'import ipaddress,sys; ipaddress.ip_address(sys.argv[1])' "$N" || { echo "no es una IP: $N" >&2; exit 2; }
  ssh "$JUMP" "ssh -o ConnectTimeout=15 root@$N 'mkdir -p $WORK'"
  for f in $FILES; do ssh "$JUMP" "ssh -o ConnectTimeout=15 root@$N 'cat > $WORK/$f'" < "$f"; done
  echo "== $N ($(ssh "$JUMP" "ssh root@$N hostname"))"
  ssh "$JUMP" "ssh root@$N 'chmod +x $WORK/*.sh; cd $WORK && md5sum $FILES'"
done
echo "== local"; md5sum $FILES

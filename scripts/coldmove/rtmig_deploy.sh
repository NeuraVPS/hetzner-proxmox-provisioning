#!/usr/bin/env bash
# rtmig_deploy.sh — copia el kit (rtmig_common.sh, rtmig_send.sh, rtmig_cutover.sh) a $WORK de los dos
# nodos y COMPRUEBA que los md5 remotos son los locales (sale con error si no). Se ejecuta en el
# equipo del operador; a los nodos se llega por un salto (una BASE con la clave de la flota), porque
# los nodos solo aceptan SSH desde las bases y entre sí. Escribe solo en $WORK de los dos nodos; no
# toca la VM ni ZFS.
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
[[ "$JUMP" =~ ^[A-Za-z0-9][A-Za-z0-9._@-]*$ ]] || { echo "JUMP no válido: $JUMP" >&2; exit 2; }
[[ "$WORK" =~ ^/[A-Za-z0-9._/-]+$ && "$WORK" != *..* ]] || { echo "WORK no válido: $WORK" >&2; exit 2; }
FILES="rtmig_common.sh rtmig_send.sh rtmig_cutover.sh"
LOCAL=$(md5sum $FILES)
rc=0
for N in "$SRC" "$DST"; do
  python3 -c 'import ipaddress,sys; ipaddress.ip_address(sys.argv[1])' "$N" || { echo "no es una IP: $N" >&2; exit 2; }
  ssh "$JUMP" "ssh -o ConnectTimeout=15 root@$N 'mkdir -p $WORK'"
  for f in $FILES; do ssh "$JUMP" "ssh -o ConnectTimeout=15 root@$N 'cat > $WORK/$f'" < "$f"; done
  ssh "$JUMP" "ssh root@$N 'chmod 755 $WORK/rtmig_send.sh $WORK/rtmig_cutover.sh'"
  REMOTE=$(ssh "$JUMP" "ssh root@$N 'cd $WORK && md5sum $FILES'")
  if [[ "$REMOTE" == "$LOCAL" ]]; then
    echo "OK  $N ($(ssh "$JUMP" "ssh root@$N hostname")): md5 iguales"
  else
    echo "MAL $N: md5 distintos" >&2; diff <(echo "$LOCAL") <(echo "$REMOTE") >&2 || true; rc=1
  fi
done
exit "$rc"

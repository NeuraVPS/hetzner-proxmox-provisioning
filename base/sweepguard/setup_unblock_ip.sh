#!/usr/bin/env bash
# Instala en una o varias BASE el script del boton «Desbloquear mi IP»
# (18/10/2026) y autoriza la clave de la Cloud Function `unblock_my_ip`, que
# SOLO puede ejecutar ese script. Idempotente. Desde el puesto de operador:
#
#   base/sweepguard/setup_unblock_ip.sh <clave.pub> b0 b1
#
# <clave.pub> es la parte publica del secreto BASE_UNBLOCK_SSH_KEY de Firebase
# (la privada no hace falta aqui). En cada base:
#   * /usr/local/sbin/neuravps-unblock-ip (0755, root) desde este repo;
#   * en /root/.ssh/authorized_keys, una linea
#       restrict,command="/usr/local/sbin/neuravps-unblock-ip" <clave.pub>
#     restrict = sin TTY, sin reenvios, sin agente; command= ignora lo que pida
#     el cliente SSH y ejecuta el script, que lee la peticion de
#     SSH_ORIGINAL_COMMAND y solo acepta `unblock <ip>`, `status <ip>` y
#     `allow-vm <ip> <vmids> [console]` (este ultimo necesita el set de
#     deploy_customer_allow.sh; sin el, contesta error). Sin from=: las
#     funciones salen por direcciones de Google que no son fijas. Una linea
#     anterior con el mismo comentario (otra clave) se sustituye; queda copia
#     .bak.unblock-ip.*;
#   * prueba: el script responde a una direccion de documentacion (rechazo) y
#     a una de verdad que no esta bloqueada, sin tocar nada.
#
# Al final imprime la clave de host ed25519 de cada base: la funcion la lleva
# fijada en functions/ip_unblock.py (BASE_HOST_KEYS). AL SUSTITUIR UNA BASE hay
# que relanzar este script Y actualizar esa clave de host en el repo de
# funciones; hasta entonces el boton da «no se pudo comprobar» en esa base.
#
# Deshacer: borra la linea "neuravps-unblock-ip@" de /root/.ssh/authorized_keys
# en cada base. El script por si solo no hace nada si nadie lo llama.
set -euo pipefail
[ $# -ge 2 ] || { echo "uso: $0 <clave.pub> <base> [<base>...]"; exit 2; }
PUBFILE=$1; shift
HERE=$(cd "$(dirname "$0")" && pwd)
SRC=$HERE/neuravps-unblock-ip
DST=/usr/local/sbin/neuravps-unblock-ip
TAG=neuravps-unblock-ip@functions

pub=$(awk 'NF>=2 {print $1" "$2; exit}' "$PUBFILE")
case "$pub" in ssh-ed25519\ AAAA*) ;; *) echo "$PUBFILE no es una clave ssh-ed25519"; exit 2;; esac
line="restrict,command=\"$DST\" $pub $TAG"
python3 -m py_compile "$SRC"

for b in "$@"; do
  echo "== $b"
  scp -q "$SRC" "$b:/root/neuravps-unblock-ip.new"
  ssh -n "$b" "install -m 0755 -o root -g root /root/neuravps-unblock-ip.new $DST && rm -f /root/neuravps-unblock-ip.new && md5sum $DST"
  ssh "$b" "set -e; f=/root/.ssh/authorized_keys
    if grep -qxF '$line' \$f; then echo '  clave ya autorizada'; exit 0; fi
    cp -a \$f \$f.bak.unblock-ip.\$(date +%Y%m%d-%H%M%S)
    grep -vF ' $TAG' \$f > \$f.new || true
    printf '%s\n' '$line' >> \$f.new
    chmod 600 \$f.new && mv \$f.new \$f
    echo '  clave autorizada (solo $DST)'" </dev/null
  ssh -n "$b" "$DST unblock 192.0.2.1 >/dev/null; test \$? -eq 2 && echo '  prueba rechazo OK'; $DST unblock 9.9.9.9"
  echo "  clave de host: $(ssh -n "$b" "awk '{print \$1\" \"\$2}' /etc/ssh/ssh_host_ed25519_key.pub")"
done
echo "UNBLOCK_IP_OK"

#!/usr/bin/env bash
# Canal de SOLO LECTURA entre las dos BASE para compartir la memoria de
# sesiones RDP de sweepguard (17/10/2026). Idempotente. Se lanza desde el
# puesto de operador, con los alias SSH de las dos bases:
#
#   base/sweepguard/setup_session_peer.sh b0 b1
#
# Para cada sentido X -> Y:
#   * X tiene una clave propia /root/.ssh/sweepguard_peer (se crea si falta);
#   * en Y esa clave solo puede ejecutar `cat sessions.json`:
#       from="<IP de X>",restrict,command="cat /var/lib/neuravps-sweepguard/sessions.json"
#     (restrict = sin TTY, sin reenvios, sin agente);
#   * X fija la clave de host de Y en /var/lib/neuravps-sweepguard/peer_known_hosts,
#     comprobada contra la que ve el operador al entrar en Y directamente;
#   * prueba final: X lee el fichero de Y con la misma orden que sweepguard.py.
#
# La IP de Y sale de PEER_V4 en /etc/neuravps/failover-watchdog.env de X, la
# misma que usa sweepguard.py. Al sustituir una base, vuelve a lanzarlo: la
# clave de host nueva no casa con la fijada y, hasta entonces, cada base usa
# solo su memoria local (lo dice el journal: "peer ... unreadable").
#
# Deshacer: borra en cada base la linea "sweepguard-peer@" de
# /root/.ssh/authorized_keys (queda copia .bak.sweepguard-peer.*).
set -euo pipefail
[ $# -eq 2 ] || { echo "uso: $0 <base-a> <base-b>"; exit 2; }

KEY=/root/.ssh/sweepguard_peer
KH=/var/lib/neuravps-sweepguard/peer_known_hosts
SESS=/var/lib/neuravps-sweepguard/sessions.json

one_way() {   # $1 lee de $2
  local x=$1 y=$2 peer src pub fp_seen fp_real
  peer=$(ssh -n "$x" "sed -n 's/^PEER_V4=//p' /etc/neuravps/failover-watchdog.env" | tr -d '"')
  [ -n "$peer" ] || { echo "$x: sin PEER_V4 en failover-watchdog.env"; return 1; }
  src=$(ssh -n "$x" "ip -4 route get $peer | sed -n 's/.* src \([0-9.]*\).*/\1/p'")
  [ -n "$src" ] || { echo "$x: no sé con qué IP sale hacia $peer"; return 1; }
  echo "== $x lee de $y ($peer), sale con $src"

  ssh -n "$x" "test -f $KEY || ssh-keygen -q -t ed25519 -N '' -C sweepguard-peer@\$(hostname) -f $KEY"
  pub=$(ssh -n "$x" "cat $KEY.pub")
  ssh "$y" "set -e; f=/root/.ssh/authorized_keys; k='$(echo "$pub" | awk '{print $2}')'
    if grep -qF \"\$k\" \$f; then echo '  clave ya autorizada en $y'; exit 0; fi
    cp -a \$f \$f.bak.sweepguard-peer.\$(date +%Y%m%d-%H%M%S)
    printf '%s\n' 'from=\"$src\",restrict,command=\"cat $SESS\" $pub' >> \$f
    echo '  clave autorizada en $y (solo lectura de sessions.json, desde $src)'" </dev/null

  fp_real=$(ssh -n "$y" "ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub | awk '{print \$2}'")
  ssh -n "$x" "mkdir -p $(dirname "$KH"); ssh-keyscan -T 10 -t ed25519 $peer 2>/dev/null > $KH.new"
  fp_seen=$(ssh -n "$x" "ssh-keygen -lf $KH.new | awk '{print \$2}'")
  if [ -z "$fp_seen" ] || [ "$fp_seen" != "$fp_real" ]; then
    echo "  !! la clave de host de $peer vista desde $x ($fp_seen) no es la de $y ($fp_real)"
    ssh -n "$x" "rm -f $KH.new"; return 1
  fi
  ssh -n "$x" "mv $KH.new $KH"
  echo "  clave de host de $y fijada en $x ($fp_real)"

  ssh -n "$x" "ssh -i $KEY -o BatchMode=yes -o IdentitiesOnly=yes -o ConnectTimeout=5 \
      -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$KH -o LogLevel=ERROR root@$peer sessions \
      | python3 -c 'import json,sys; print(\"  prueba OK:\", len(json.load(sys.stdin)), \"fuentes en $y\")'"
}

one_way "$1" "$2"
one_way "$2" "$1"
echo "SESSION_PEER_OK"

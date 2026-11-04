#!/bin/bash
# Prepara como nodo SQX un nodo que NO es AX162 (hoy: los AX102-3-LTD 0000260 y
# 0000261, asignados a SQX por Iván el 04/11/2026 con allowedServerTypes
# ["vps-a","vps-b"]). first_boot.sh instala el reconciliador solo en *AX162* y el
# guard de MT en *AX102*, por nombre; este script deja el nodo como un SQX:
#
#   1. neuravps-balloon-reconciler (el de los 86 SQX, misma versión y la misma
#      /etc/default: FLOOR_BUDGET_PCT=90, LOWER_ENABLED=1). Por qué, y no el
#      autoballoon de pvestatd: welcome-boost y el boot RAM guard suben el suelo
#      de una VM a su RAM entera, y en un AX102 NADIE lo baja para VMs de más de
#      8 GB (mt-freemem-guard: MAX_VM_MB=8192). Sin reconciliador, los suelos de
#      las vps-a/b (16/19 GB) solo suben y el nodo acaba sin globo que reclamar.
#   2. mt-freemem-guard en ENABLED=0: en un nodo SQX no hay MT y así no hay dos
#      escritores de /var/lib/neuravps-balloon/floors.json.
#   3. neuravps-ram-guard.py nuevo: presupuesto del 90 % si hay reconciliador
#      (antes: si el nombre llevaba -AX162).
#   4. sync-dnat.py nuevo + /etc/default/neuravps-boot-stagger (MEM_STAGGER=1):
#      tras reiniciar el nodo, arranca las VMs de una en una cuando cabe la RAM
#      de la siguiente (piloto 03/11: 8 vps-b a la vez -> 4,5 GB libres y 8,4 GB
#      de swap).
#
# Copias: cada fichero que se pisa queda como <fichero>.bak-<UTC> y la lista en
# /root/nv-sqx-extras/manifest-<UTC>. Deshacer: este mismo script con --rollback
# (restaura la última instalación). No toca ninguna VM.
#
# Uso (en el nodo, con este directorio run_remotes/ + snippets/ copiados):
#   bash install-sqx-node-extras.sh            # instala
#   bash install-sqx-node-extras.sh --rollback # deshace la última instalación
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SNIPPETS_SRC="${SNIPPETS_SRC:-${HERE}/../snippets}"
STATE=/root/nv-sqx-extras
TS="$(date -u +%Y%m%dT%H%M%SZ)"
HOOK=/var/lib/svz/snippets/sync-dnat.py
RAMG=/usr/local/sbin/neuravps-ram-guard.py
REC=/usr/local/sbin/neuravps-balloon-reconciler.sh
REC_DEF=/etc/default/neuravps-balloon-reconciler
REC_SVC=/etc/systemd/system/neuravps-balloon-reconciler.service
REC_TMR=/etc/systemd/system/neuravps-balloon-reconciler.timer
MTG_DEF=/etc/default/neuravps-mt-freemem-guard
STAGGER=/etc/default/neuravps-boot-stagger

mkdir -p "$STATE"

rollback() {
  local m
  m="$(ls -1 "$STATE"/manifest-* 2>/dev/null | sort | tail -1 || true)"
  [ -n "$m" ] || { echo "no hay instalación que deshacer"; exit 1; }
  echo "deshaciendo $m"
  systemctl disable --now neuravps-balloon-reconciler.timer 2>/dev/null || true
  while IFS='|' read -r kind path bak; do
    case "$kind" in
      replaced) cp -a "$bak" "$path"; echo "restaurado $path <- $bak" ;;
      created)  rm -f "$path"; echo "borrado $path (lo creó la instalación)" ;;
    esac
  done < "$m"
  systemctl daemon-reload
  mv "$m" "$m.rolledback"
  echo "hecho. Estado: reconciler=$(systemctl is-enabled neuravps-balloon-reconciler.timer 2>/dev/null || echo ausente)," \
       "mt-guard=$(grep -E '^ENABLED=' "$MTG_DEF" 2>/dev/null || echo '?')"
}

[ "${1:-}" = "--rollback" ] && { rollback; exit 0; }

MAN="$STATE/manifest-$TS"
: > "$MAN"
put() {  # put <src> <dst> <mode>: replace with backup, or create
  local src=$1 dst=$2 mode=$3
  if [ -e "$dst" ]; then
    if cmp -s "$src" "$dst"; then echo "igual: $dst"; return 0; fi
    cp -a "$dst" "$dst.bak-$TS"
    echo "replaced|$dst|$dst.bak-$TS" >> "$MAN"
  else
    echo "created|$dst|" >> "$MAN"
  fi
  install -m "$mode" "$src" "$dst"
  echo "instalado: $dst"
}

for f in "$HERE/neuravps-ram-guard.py" "$SNIPPETS_SRC/sync-dnat.py"; do
  python3 -c 'import pathlib,sys; p=pathlib.Path(sys.argv[1]); compile(p.read_text(),str(p),"exec")' "$f"
done
grep -q '#NBRVER=' "$HERE/neuravps-balloon-reconciler.sh"
bash -n "$HERE/neuravps-balloon-reconciler.sh"
[ -f "$HOOK" ] || { echo "no encuentro el hookscript en $HOOK" >&2; exit 2; }

put "$HERE/neuravps-ram-guard.py" "$RAMG" 755
put "$SNIPPETS_SRC/sync-dnat.py" "$HOOK" 755

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
cat > "$TMP/rec.default" <<'CONF'
# Gestionado por NeuraVPS (install-sqx-node-extras.sh). Misma configuración que
# los 86 nodos SQX AX162. Revertir el decay = LOWER_ENABLED=0 (<1 min).
FLOOR_BUDGET_PCT=90
LOWER_ENABLED=1
CONF
cat > "$TMP/rec.service" <<'UNIT'
[Unit]
Description=NeuraVPS balloon reconciler (RAM follows activity on over-committed SQX nodes)
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/neuravps-balloon-reconciler.sh
UNIT
cat > "$TMP/rec.timer" <<'UNIT'
[Unit]
Description=Run neuravps-balloon-reconciler every minute
[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
AccuracySec=10s
[Install]
WantedBy=timers.target
UNIT
cat > "$TMP/stagger" <<'CONF'
# Gestionado por NeuraVPS (install-sqx-node-extras.sh, 2026-11-04). Tras reiniciar
# el nodo, sync-dnat.py node-boot arranca cada VM cuando MemAvailable >= su RAM +
# RESERVE_MB, con MIN_GAP_S entre arranques y nunca más de MAX_WAIT_S de espera.
# Quitar = borrar este fichero o MEM_STAGGER=0.
MEM_STAGGER=1
RESERVE_MB=12288
MIN_GAP_S=30
MAX_WAIT_S=240
CONF

put "$HERE/neuravps-balloon-reconciler.sh" "$REC" 755
put "$TMP/rec.default" "$REC_DEF" 644
put "$TMP/rec.service" "$REC_SVC" 644
put "$TMP/rec.timer" "$REC_TMR" 644
put "$TMP/stagger" "$STAGGER" 644

if [ -f "$MTG_DEF" ]; then
  sed 's/^ENABLED=.*/ENABLED=0/' "$MTG_DEF" > "$TMP/mtg"
  grep -q '^ENABLED=0' "$TMP/mtg" || echo 'ENABLED=0' >> "$TMP/mtg"
  put "$TMP/mtg" "$MTG_DEF" 644
fi

systemctl daemon-reload
systemctl enable --now neuravps-balloon-reconciler.timer >/dev/null
echo "manifest: $MAN"
cat "$MAN"
echo "reconciler: $(systemctl is-enabled neuravps-balloon-reconciler.timer) / $(systemctl is-active neuravps-balloon-reconciler.timer)"
echo "mt-guard: $(grep -E '^ENABLED=' "$MTG_DEF" 2>/dev/null || echo 'sin fichero')"

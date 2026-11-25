#!/usr/bin/env bash
# rtmig_send.sh — precopia ZFS nodo→nodo para el traslado en frío de una VM.
# Se ejecuta EN EL NODO ORIGEN (root). Viaja directo nodo→nodo por SSH: los nodos se aceptan entre sí
# por el IPSET hosts-ipv6 de cluster.fw (sale de proxmox_nodes.ip) y la clave ~/.ssh/neuravps_id.
# NO pasa por ninguna BASE.
#
#   rtmig_send.sh base <SNAP>            snapshot atómico @SNAP de todos los discos + envío completo
#   rtmig_send.sh incr <PREV> <NEW>      snapshot atómico @NEW + incremental PREV→NEW
#                                           (PREV=auto: último @rtmig-* común con el mismo guid)
#   rtmig_send.sh last                   imprime ese último snapshot común
#   rtmig_send.sh pending                base común + bytes escritos desde ella por disco + pool
#   rtmig_send.sh verify <SNAP>          compara guid/referenced de @SNAP en origen y destino
#   rtmig_send.sh resume                 reanuda envíos cortados (receive_resume_token en destino)
#
# Los nombres de snapshot deben empezar por «rtmig-» (p. ej. rtmig-$(date -u +%Y%m%dT%H%MZ)).
#
# Variables: las de rtmig_common.sh (VMID, DST, DST_NODE, WORK, DISKS, SSH_PORT) y además:
#   RATE  (por defecto 30m)   límite de pv (-L), por flujo. 30m ≈ 250 Mbit/s: cabe en un enlace
#                             no-Hetzner de ~300 Mbit/s por flujo (Kademar, medido 08/11) sin ahogar al
#                             nodo. Dentro de Hetzner se puede subir (60m–110m). "0" = sin límite
#                             (solo con la VM APAGADA: la incremental final).
#   ZSTD  (opcional, nivel)   flujo SIN -c recomprimido con zstd multihilo en el cable (ZSTD_T hilos,
#                             12 por defecto); útil si el cuello es la red. El receptor recomprime.
#   REUSE_SNAP=1 (solo base)  @SNAP ya creada a mano (p. ej. pool lleno: no hay sitio para crearla aquí).
#   KEEP  (opcional)          valor de neuravps:keep que se pone en los snapshots de origen.
#   DROP_PREV=1 (solo incr)   tras verificar @NEW en ambos lados, destruye @PREV en ambos lados.
#
# Nunca destruye un zvol, nunca usa -F en el envío completo, nunca toca la VM.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=rtmig_common.sh
source "$HERE/rtmig_common.sh"

RATE="${RATE:-30m}"
KEEP="${KEEP:-}"

[[ "$RATE" =~ ^[0-9]+[kKmMgG]?$ ]] || die "RATE no válido: '$RATE' (p. ej. 30m, 110m o 0)"

ratepipe() {
  # -n -b: una línea por intervalo con los bytes transferidos (legible en journal)
  if command -v pv >/dev/null; then
    if [[ "$RATE" == "0" ]]; then pv -f -n -b -i 30; else pv -f -n -b -i 30 -L "$RATE"; fi
  else   # respaldo sin pv: mismo formato de salida (bytes cada 30 s por stderr)
    RATE="$RATE" python3 -c '
import os, sys, time
r = os.environ["RATE"].lower(); m = {"k": 1 << 10, "m": 1 << 20, "g": 1 << 30}
lim = 0 if r == "0" else (int(r[:-1]) * m[r[-1]] if r[-1] in m else int(r))
i, o = sys.stdin.buffer, sys.stdout.buffer
t0 = time.monotonic(); n = 0; last = t0
while True:
    b = i.read1(1 << 20)
    if not b: break
    o.write(b); n += len(b)
    now = time.monotonic()
    if lim:
        ahead = n / lim - (now - t0)
        if ahead > 0: time.sleep(ahead)
    if now - last >= 30: sys.stderr.write(f"{n}\n"); sys.stderr.flush(); last = now
o.flush(); sys.stderr.write(f"{n}\n")'
  fi
}

rate_bps() {   # RATE → bytes/s (0 = sin límite)
  local r="${RATE,,}" n
  n="${r%[kmg]}"
  case "$r" in *k) echo $(( n << 10 )) ;; *m) echo $(( n << 20 )) ;; *g) echo $(( n << 30 )) ;; *) echo "$n" ;; esac
}

snap_all() {   # snapshot atómico de todos los discos en UNA orden zfs
  local snap="$1" args=()
  for d in $DISKS; do args+=("${POOLDS}/vm-${VMID}-disk-${d}@${snap}"); done
  zfs snapshot "${args[@]}"
  if [[ -n "$KEEP" ]]; then
    for a in "${args[@]}"; do zfs set "neuravps:keep=${KEEP}" "$a"; done
  fi
  log "snapshot atómico @${snap} creado en: ${args[*]}"
}

verify() {   # guid igual en origen y destino = mismo snapshot (lref/ref solo informativos:
             # la contabilidad de huecos/bloques embebidos difiere unos KB sin diferir el contenido)
  local snap="$1" bad=0 s d si di
  for dd in $DISKS; do
    local ds="${POOLDS}/vm-${VMID}-disk-${dd}@${snap}"
    s=$(zfs get -H -o value guid "$ds" 2>/dev/null || true)
    d=$(dst "zfs get -H -o value guid '$ds' 2>/dev/null" || true)
    si=$(zfs get -H -o value referenced,logicalreferenced "$ds" 2>/dev/null | paste -sd/)
    di=$(dst "zfs get -H -o value referenced,logicalreferenced '$ds' 2>/dev/null | paste -sd/" || true)
    if [[ -n "$s" && "$s" == "$d" ]]; then log "VERIFY OK   $ds  guid=$s  ref/lref origen=$si destino=$di"
    else log "VERIFY FAIL $ds  guid origen=[${s:-ausente}] destino=[${d:-ausente}]"; bad=1; fi
  done
  return $bad
}

last_common() {   # último @rtmig-* presente en TODOS los discos de ambos lados con el mismo guid
  local cand g1 ok first
  first="${DISKS%% *}"
  for cand in $(zfs list -H -t snapshot -o name -s createtxg "${POOLDS}/vm-${VMID}-disk-${first}" | sed -n 's/.*@\(rtmig-.*\)/\1/p' | tac); do
    ok=1
    for dd in $DISKS; do
      g1=$(zfs get -H -o value guid "${POOLDS}/vm-${VMID}-disk-${dd}@${cand}" 2>/dev/null || true)
      [[ -n "$g1" && "$g1" == "$(dst "zfs get -H -o value guid '${POOLDS}/vm-${VMID}-disk-${dd}@${cand}' 2>/dev/null" || true)" ]] || { ok=0; break; }
    done
    (( ok )) && { echo "$cand"; return 0; }
  done
  return 1
}

send_one() {   # send_one <disk> <snap> [prev]
  local snap="$2" prev="${3:-}" ds="${POOLDS}/vm-${VMID}-disk-${1}"
  local recvflags="-s"; local sendargs=(-c -e)
  [[ -n "${ZSTD:-}" ]] && sendargs=(-e)
  if [[ -n "$prev" ]]; then sendargs+=(-i "@${prev}"); recvflags="-s -F"; fi
  local est; est=$(zfs send -nP "${sendargs[@]}" "${ds}@${snap}" 2>/dev/null | awk '/^size/{print $2}')
  log "send ${ds}@${snap} ${prev:+(incremental desde @$prev) }estimado=$(numfmt --to=iec "${est:-0}")B rate=${RATE} zstd=${ZSTD:-no}"
  local t0=$SECONDS rc
  set +e
  if [[ -n "${ZSTD:-}" ]]; then
    nice -n 19 zfs send "${sendargs[@]}" "${ds}@${snap}" | zstd -q -c "-${ZSTD}" -T"${ZSTD_T:-12}" | ratepipe \
      | dst_in "set -o pipefail; zstd -q -d -c | zfs recv ${recvflags} '${ds}'"
    rc=("${PIPESTATUS[0]}" "${PIPESTATUS[2]}" "${PIPESTATUS[3]}" "${PIPESTATUS[1]}")
  else
    nice -n 19 zfs send "${sendargs[@]}" "${ds}@${snap}" | ratepipe | dst_in "zfs recv ${recvflags} '${ds}'"
    rc=("${PIPESTATUS[@]}" 0)
  fi
  set -e
  [[ "${rc[0]}" == 0 && "${rc[1]}" == 0 && "${rc[2]}" == 0 && "${rc[3]}" == 0 ]] \
    || die "envío de ${ds} falló (send=${rc[0]} pv=${rc[1]} recv=${rc[2]} zstd=${rc[3]}); reanudar con: $0 resume"
  log "hecho ${ds}@${snap} en $((SECONDS - t0)) s"
}

check_snap_name() { [[ "$1" == rtmig-* && "$1" =~ ^[A-Za-z0-9_.:-]+$ ]] || die "el snapshot '$1' debe empezar por rtmig- (y sin espacios ni @)"; }

CMD="${1:-}"
case "$CMD" in base|incr|last|pending|verify|resume) ;; *) sed -n '2,32p' "$0"; exit 2 ;; esac
DISKS=$(detect_disks)
check_dst_identity
log "VM ${VMID}: $(src_node) → ${DST_NODE} (${DST}); discos: ${DISKS}"

case "$CMD" in
  base)
    SNAP="${2:?SNAP}"; check_snap_name "$SNAP"
    for d in $DISKS; do
      dst "zfs list -H '${POOLDS}/vm-${VMID}-disk-${d}' >/dev/null 2>&1" \
        && die "el destino ya tiene ${POOLDS}/vm-${VMID}-disk-${d}; no piso nada"
    done
    dst "qm config ${VMID} >/dev/null 2>&1" && die "el destino ya tiene config de la VM ${VMID}"
    if [[ "${REUSE_SNAP:-0}" == 1 ]]; then   # @SNAP creada a mano antes (p. ej. pool lleno)
      for d in $DISKS; do zfs list -H "${POOLDS}/vm-${VMID}-disk-${d}@${SNAP}" >/dev/null || die "falta @${SNAP} en disk-${d}"; done
      log "reutilizo @${SNAP} ya existente"
    else
      snap_all "$SNAP"
    fi
    # discos pequeños primero (efi, tpm), el grande al final
    for d in $(printf '%s\n' $DISKS | sort -n -r); do send_one "$d" "$SNAP"; done
    verify "$SNAP"
    ;;
  incr)
    PREV="${2:?PREV}"; NEW="${3:?NEW}"; check_snap_name "$NEW"
    if [[ "$PREV" == "auto" ]]; then PREV=$(last_common) || die "no hay snapshot rtmig-* común"; log "PREV=auto -> @${PREV}"; fi
    dst "qm config ${VMID} >/dev/null 2>&1" && die "el destino ya tiene config de la VM ${VMID}: no recibo encima"
    snap_all "$NEW"
    for d in $(printf '%s\n' $DISKS | sort -n -r); do send_one "$d" "$NEW" "$PREV"; done
    verify "$NEW"
    if [[ "${DROP_PREV:-0}" == "1" ]]; then
      for d in $DISKS; do
        zfs destroy "${POOLDS}/vm-${VMID}-disk-${d}@${PREV}"
        dst "zfs destroy '${POOLDS}/vm-${VMID}-disk-${d}@${PREV}'"
      done
      log "@${PREV} destruido en origen y destino (queda @${NEW} como base común)"
    fi
    ;;
  verify)
    verify "${2:?SNAP}"
    ;;
  last)
    last_common || die "no hay snapshot rtmig-* común"
    ;;
  pending)
    P=$(last_common) || die "no hay snapshot rtmig-* común"
    log "base común: @${P}"
    BPS=$(rate_bps)
    for d in $DISKS; do
      w=$(zfs get -Hp -o value "written@${P}" "${POOLDS}/vm-${VMID}-disk-${d}")
      if (( BPS > 0 )); then eta="~$(( w / BPS ))s a RATE=${RATE}"; else eta="RATE=0 (sin límite)"; fi
      log "  disk-${d}: $(numfmt --to=iec "$w")B escritos desde @${P}  (${eta})"
    done
    log "pool origen: $(zpool list -H -o cap,free rpool | tr '\t' ' ')"
    ;;
  resume)
    for d in $DISKS; do
      ds="${POOLDS}/vm-${VMID}-disk-${d}"
      tok=$(dst "zfs get -H -o value receive_resume_token '${ds}' 2>/dev/null" || true)
      if [[ -n "$tok" && "$tok" != "-" ]]; then
        log "reanudando ${ds}"
        set +e
        zfs send -t "$tok" | ratepipe | dst_in "zfs recv -s '${ds}'"
        rc=("${PIPESTATUS[@]}"); set -e
        [[ "${rc[0]}" == 0 && "${rc[2]}" == 0 ]] || die "reanudación de ${ds} falló (${rc[*]})"
      fi
    done
    log "sin tokens pendientes (o reanudados). Relanza base/incr si faltaba algún disco."
    ;;
esac

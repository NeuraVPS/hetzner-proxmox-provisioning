#!/usr/bin/env bash
# rtmig_cutover.sh — pasos del corte de un traslado en frío con precopia ZFS (ver README.md).
# Se ejecuta EN EL NODO ORIGEN (root). Habla con el destino por SSH directo nodo→nodo.
#
#   shutdown [timeout_s]     qm shutdown ORDENADO (nunca stop/forceStop). Si vence el tiempo,
#                            sale con error y la VM SIGUE encendida: decide una persona.
#   park                     con la VM parada: guarda el historial del reconciliador de globo
#                            (export-state, si este nodo lo tiene) y mueve <VMID>.conf (+ .fw) de
#                            /etc/pve a $WORK para que nadie pueda arrancarla en origen.
#                            Los zvols NO se tocan.
#   push <SNAP> [balloon_mb] escribe la config aparcada en el destino (+ .fw), fija balloon si se pasa
#                            e importa el historial del globo (import-state) si el destino tiene
#                            reconciliador. Exige, por disco: @SNAP con el mismo guid en ambos lados
#                            y 0 bytes escritos después de @SNAP en ambos lados.
#                            CPU: ver «Guarda de CPU» abajo.
#   start                    qm start en destino y espera 'running'.
#   rollback                 vuelve al origen: exige VM parada en destino; aparca la config del
#                            destino en $WORK del destino, restaura la del origen y arranca en origen.
#   status                   dónde está la config y el estado en cada lado.
#
# Guarda de CPU (push; coherente con migrate_vm.sh _live_silicon_guard y el defrag):
#   - En frío cualquier CPU arranca, pero el invitado verá la del destino. Con un modelo genérico
#     (x86-64-v2/v3/v4) el Hardware ID de SQX no cambia (medido: Genoa↔Turin e Intel↔AMD con v4).
#   - Con cpu=host/max, otro modelo con nombre o sin línea cpu (kvm64), si el silicio
#     (vendor|family|model de /proc/cpuinfo) cambia, el Hardware ID cambia: se exige UNA de
#       CHANGE_CPU=1                pone en el destino la CPU de las altas (x86-64-v4, o v3 sin AVX-512);
#       ACK_HWID_CHANGE=<VMID>      deja la CPU tal cual (el invitado verá el silicio nuevo);
#     y en los dos casos CPU_NOTICE='<cuándo y por dónde se avisó al cliente de reactivar licencias>'.
#   - x86-64-v4 hacia un destino sin AVX-512 no arrancaría (PVE pone enforce): solo con CHANGE_CPU=1.
#   Se registra en $WORK/cpu-change.txt. El rollback recupera la CPU antigua (la config del origen no
#   se toca).
#
# Variables: las de rtmig_common.sh (VMID, DST, DST_NODE, WORK, SSH_PORT) y CHANGE_CPU,
# ACK_HWID_CHANGE, CPU_NOTICE (solo push).
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=rtmig_common.sh
source "$HERE/rtmig_common.sh"

st_src() { qm status "$VMID" 2>/dev/null | awk '{print $2}'; }
st_dst() { dst "qm status $VMID 2>/dev/null" | awk '{print $2}'; }
has_reconciler_src() { [[ -x "$NV_RECONCILER" && -f "$NV_RAM_GUARD" ]]; }
has_reconciler_dst() { dst "[ -x \"\${NV_RECONCILER:-/usr/local/sbin/neuravps-balloon-reconciler.sh}\" ] && [ -f \"\${NV_RAM_GUARD:-/usr/local/sbin/neuravps-ram-guard.py}\" ]"; }

# CPU efectiva de una config (sección principal, sin [snapshots]): la que verá el invitado al arrancar.
conf_cpu() { awk '/^\[/{exit} /^cpu:/{sub(/^cpu:[ \t]*/,""); sub(/,.*/,""); sub(/^cputype=/,""); print; exit}' "$1"; }
is_generic_cpu() { [[ "$1" =~ ^x86-64-v[0-9]+(-[A-Za-z0-9]+)?$ ]]; }

# Decide la CPU del destino: deja en CPU_NEW la CPU a poner ("" = no tocar) o muere. Sin subshell,
# para que los mensajes de die se vean.
cpu_decision() {
  local conf="$1" cpu lc src_sig dst_sig flags need_v3=0 new=""
  cpu=$(conf_cpu "$conf"); cpu="${cpu:-kvm64}"; lc="${cpu,,}"
  src_sig=$(cpu_sig_from < "$CPUINFO")
  dst_sig=$(dst "cat \"\${CPUINFO:-/proc/cpuinfo}\"" | cpu_sig_from) || true
  flags=$(dst "grep -m1 '^flags' \"\${CPUINFO:-/proc/cpuinfo}\"" || true)
  [[ "$src_sig" =~ ^[^|]+\|[0-9]+\|[0-9]+$ && "$dst_sig" =~ ^[^|]+\|[0-9]+\|[0-9]+$ && -n "$flags" ]] \
    || die "no pude leer la CPU de origen [${src_sig}] o destino [${dst_sig}]; no se ha escrito nada"
  for f in avx512f avx512bw avx512cd avx512dq avx512vl; do grep -qw "$f" <<<"$flags" || need_v3=1; done
  local tgt=x86-64-v4; (( need_v3 )) && tgt=x86-64-v3

  case "${CHANGE_CPU:-0}" in 0|1) ;; *) die "CHANGE_CPU solo admite 0 o 1 (vale '${CHANGE_CPU}')" ;; esac
  if [[ -n "${ACK_HWID_CHANGE:-}" && "${ACK_HWID_CHANGE}" != "$VMID" ]]; then
    die "ACK_HWID_CHANGE=${ACK_HWID_CHANGE} no es esta VM (${VMID})"
  fi
  if [[ "${CHANGE_CPU:-0}" == 1 || -n "${ACK_HWID_CHANGE:-}" ]]; then
    [[ -n "${CPU_NOTICE:-}" ]] || die "CHANGE_CPU=1 / ACK_HWID_CHANGE exigen CPU_NOTICE='<cuándo y por dónde se avisó al cliente de reactivar licencias>'"
  fi
  [[ "${CHANGE_CPU:-0}" == 1 && -n "${ACK_HWID_CHANGE:-}" ]] && die "CHANGE_CPU=1 y ACK_HWID_CHANGE son excluyentes: elige una"

  if [[ "${CHANGE_CPU:-0}" == 1 ]]; then
    [[ "$lc" == "$tgt" ]] || new="$tgt"
  elif is_generic_cpu "$lc"; then
    if [[ "$lc" == x86-64-v4* && $need_v3 == 1 ]]; then
      die "la VM usa ${cpu} y el destino ${DST_NODE} no tiene AVX-512: no arrancaría. Solo con CHANGE_CPU=1 (→ x86-64-v3) y el cliente avisado"
    fi
  elif [[ "$src_sig" != "$dst_sig" ]]; then
    [[ -n "${ACK_HWID_CHANGE:-}" ]] || die "cpu=${cpu} enseña la CPU real y el silicio cambia (origen [${src_sig//|/ }] → destino [${dst_sig//|/ }]): el Hardware ID de SQX y las licencias del Market cambiarán. Hace falta dejar constancia del aviso al cliente: CHANGE_CPU=1 (pone ${tgt}) o ACK_HWID_CHANGE=${VMID} (deja ${cpu}), con CPU_NOTICE='…'. No se ha escrito nada"
    printf '%s VM%s cpu %s sin cambiar, silicio %s -> %s ACK_HWID_CHANGE aviso=%s\n' "$(date -u +%FT%TZ)" "$VMID" "$cpu" "$src_sig" "$dst_sig" "$CPU_NOTICE" >> "$WORK/cpu-change.txt"
    log "ACK_HWID_CHANGE=${VMID}: cpu=${cpu} se queda; el invitado verá [${dst_sig//|/ }] (aviso: ${CPU_NOTICE})"
  fi
  CPU_NEW="$new"
}

mkdir -p "$WORK"
CMD="${1:-}"
case "$CMD" in shutdown|park|push|start|rollback|status) ;; *) sed -n '2,36p' "$0"; exit 2 ;; esac
check_dst_identity

case "$CMD" in
  shutdown)
    T="${2:-600}"
    [[ "$(st_src)" == "running" ]] || die "la VM ${VMID} no está 'running' en origen (estado: $(st_src))"
    log "qm shutdown ${VMID} --timeout ${T} (ordenado; sin forceStop)"
    t0=$SECONDS
    if qm shutdown "$VMID" --timeout "$T"; then
      log "apagada en $((SECONDS - t0)) s; estado: $(st_src)"
    else
      die "Windows no se apagó en ${T} s. NO forzar. La VM sigue en estado '$(st_src)'. Escalar (README §apagado)."
    fi
    [[ "$(st_src)" == "stopped" ]] || die "tras el shutdown el estado es '$(st_src)'"
    ;;
  park)
    [[ "$(st_src)" == "stopped" ]] || die "solo con la VM parada (estado: $(st_src))"
    [[ -f "$CONF" ]] || die "no hay $CONF (¿ya aparcada? mira $WORK)"
    # Historial del reconciliador de globo: export-state necesita la config viva, así que va ANTES de
    # aparcar. Solo si este nodo tiene reconciliador (en un EX44 no hay historial que llevar).
    rm -f "$WORK/${VMID}.balloon-state"
    if has_reconciler_src; then
      if B=$(python3 "$NV_RAM_GUARD" export-state "$VMID" "$(src_node)" 2>/dev/null) && [[ "$B" =~ ^[A-Za-z0-9+/=]+$ ]]; then
        printf '%s\n' "$B" > "$WORK/${VMID}.balloon-state"
        log "historial del globo exportado a $WORK/${VMID}.balloon-state"
      else
        warn "no pude exportar el historial del globo (el destino lo reaprenderá de forma conservadora)"
      fi
    else
      log "este nodo no tiene reconciliador de globo: no hay historial que exportar"
    fi
    cp -a "$CONF" "$WORK/${VMID}.conf.src"
    [[ -f "$FW" ]] && cp -a "$FW" "$WORK/${VMID}.fw.src"
    cmp -s "$CONF" "$WORK/${VMID}.conf.src" || die "la copia de la config no coincide"
    rm -f "$CONF"; [[ -f "$WORK/${VMID}.fw.src" ]] && rm -f "$FW"
    qm config "$VMID" >/dev/null 2>&1 && die "qm aún ve la VM en origen"
    log "config aparcada en $WORK/${VMID}.conf.src (fw: $( [[ -f $WORK/${VMID}.fw.src ]] && echo sí || echo no )); zvols intactos:"
    zfs list -H -o name,refer -r "$POOLDS" | grep "vm-${VMID}-" || true
    ;;
  push)
    SNAP="${2:?SNAP final}"; B="${3:-}"
    [[ -z "$B" || "$B" =~ ^[0-9]+$ ]] || die "balloon_mb no numérico: $B"
    [[ -f "$WORK/${VMID}.conf.src" ]] || die "no hay config aparcada en $WORK"
    dst "qm config $VMID >/dev/null 2>&1" && die "el destino ya tiene config de ${VMID}"
    cpu_decision "$WORK/${VMID}.conf.src"
    DISKS=$(detect_disks)
    for d in $DISKS; do
      ds="${POOLDS}/vm-${VMID}-disk-${d}"
      gs=$(zfs get -H -o value guid "${ds}@${SNAP}" 2>/dev/null || true)
      gd=$(dst "zfs get -H -o value guid '${ds}@${SNAP}' 2>/dev/null" || true)
      [[ -n "$gs" && "$gs" == "$gd" ]] || die "$ds@$SNAP: guid origen=[$gs] destino=[$gd] — falta la sincronización final"
      ws=$(zfs get -Hp -o value "written@${SNAP}" "$ds"); wd=$(dst "zfs get -Hp -o value 'written@${SNAP}' '$ds'")
      [[ "$ws" == 0 && "$wd" == 0 ]] || die "$ds: escrito después de @$SNAP origen=${ws} B destino=${wd} B — la final no es la última"
      log "disco OK: $ds@$SNAP guid=$gs, 0 B posteriores en ambos lados"
    done
    dst_in "cat > \"${R_CONF}\"" < "$WORK/${VMID}.conf.src"
    [[ -f "$WORK/${VMID}.fw.src" ]] && dst_in "cat > \"${R_FW}\"" < "$WORK/${VMID}.fw.src"
    if [[ -n "$B" ]]; then dst "qm set $VMID --balloon $B" >/dev/null; log "balloon=${B} en destino"; fi
    if [[ -n "$CPU_NEW" ]]; then
      CPU_OLD=$(conf_cpu "$WORK/${VMID}.conf.src")
      dst "qm set $VMID --cpu $CPU_NEW" >/dev/null
      log "CHANGE_CPU=1: cpu ${CPU_OLD:-<sin línea>} -> ${CPU_NEW} en destino (aviso: ${CPU_NOTICE}); la config del origen conserva la antigua (rollback)"
      printf '%s VM%s cpu %s -> %s aviso=%s\n' "$(date -u +%FT%TZ)" "$VMID" "${CPU_OLD:-<sin línea>}" "$CPU_NEW" "$CPU_NOTICE" >> "$WORK/cpu-change.txt"
    elif [[ "${CHANGE_CPU:-0}" == 1 ]]; then
      log "CHANGE_CPU=1: la VM ya tiene la CPU de las altas; no se cambia"
    fi
    # Historial del globo: import-state necesita la config ya escrita en el destino.
    if [[ -s "$WORK/${VMID}.balloon-state" ]]; then
      if has_reconciler_dst; then
        if dst "python3 \"\${NV_RAM_GUARD:-/usr/local/sbin/neuravps-ram-guard.py}\" import-state ${VMID} '${DST_NODE}' '$(cat "$WORK/${VMID}.balloon-state")'" >/dev/null; then
          log "historial del globo importado en ${DST_NODE} (retención de 24 h)"
        else
          warn "no pude importar el historial del globo en ${DST_NODE}: lo reaprenderá de forma conservadora"
        fi
      else
        log "${DST_NODE} no tiene reconciliador de globo: el historial no se importa"
      fi
    fi
    dst "qm config $VMID" | grep -E "^(balloon|memory|cores|cpu|scsi0|efidisk0|tpmstate0|net0|hookscript|name):"
    ;;
  start)
    [[ "$(st_src)" == "" ]] || die "la VM aún existe en origen (estado $(st_src)); aparca primero"
    log "qm start ${VMID} en destino"
    dst "qm start $VMID"
    for _ in $(seq 1 20); do [[ "$(st_dst)" == "running" ]] && break; sleep 3; done
    log "estado en destino: $(st_dst)"
    [[ "$(st_dst)" == "running" ]] || die "no llegó a running"
    log "siguiente paso, en una BASE: migrate_vm.sh ${VMID} <número de ${DST_NODE}> (rama «already on dest»)"
    ;;
  rollback)
    [[ -f "$WORK/${VMID}.conf.src" ]] || die "no hay config aparcada en origen"
    if dst "qm config $VMID >/dev/null 2>&1"; then
      [[ "$(st_dst)" == "stopped" ]] || die "la VM está '$(st_dst)' en destino: apágala ORDENADAMENTE allí primero (qm shutdown), con decisión de una persona"
      dst "mkdir -p '$WORK' && cp -a \"${R_CONF}\" '$WORK/${VMID}.conf.dst' && rm -f \"${R_CONF}\" \"${R_FW}\""
      log "config del destino aparcada en ${DST}:$WORK/${VMID}.conf.dst (discos de destino intactos)"
    fi
    [[ -f "$CONF" ]] && die "ya hay config en origen"
    cp -a "$WORK/${VMID}.conf.src" "$CONF"
    [[ -f "$WORK/${VMID}.fw.src" ]] && cp -a "$WORK/${VMID}.fw.src" "$FW"
    qm start "$VMID"
    log "VM ${VMID} arrancada de nuevo en ORIGEN; estado: $(st_src). Firestore no se ha tocado si aún no corriste migrate_vm.sh (si ya lo corriste: vuelve a correrlo hacia el origen)."
    ;;
  status)
    log "origen $(src_node): config=$( [[ -f $CONF ]] && echo sí || echo no ) aparcada=$( [[ -f $WORK/${VMID}.conf.src ]] && echo sí || echo no ) estado=$(st_src)"
    log "destino ${DST_NODE}: config=$(dst "[ -f \"${R_CONF}\" ] && echo sí || echo no") estado=$(st_dst)"
    ;;
esac

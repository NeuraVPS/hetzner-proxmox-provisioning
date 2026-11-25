#!/usr/bin/env bash
# rtmig_cutover.sh — pasos del corte de un traslado en frío con precopia ZFS (ver README.md).
# Se ejecuta EN EL NODO ORIGEN (root). Habla con el destino por SSH directo nodo→nodo.
#
#   shutdown [timeout_s]     comprobaciones previas (discos, CPU/licencias, destino libre) y
#                            qm shutdown ORDENADO (nunca stop/forceStop). Si vence el tiempo, sale
#                            con error y la VM SIGUE encendida: decide una persona.
#   park                     con la VM parada: guarda el historial del reconciliador de globo
#                            (export-state, si este nodo lo tiene), copia <VMID>.conf (+ .fw) a $WORK,
#                            abre el estado del traslado (fase «parked») y quita la config de /etc/pve
#                            para que nadie pueda arrancarla en origen. Los zvols NO se tocan.
#   push <SNAP> [balloon_mb] comprueba disco a disco (inventario completo de la config) que @SNAP
#                            tiene el mismo guid en ambos lados y 0 bytes escritos después; escribe el
#                            .fw y PUBLICA la config al final, de una vez y ya con CPU/balloon
#                            aplicados; la relee y compara su sha256. Luego importa el historial del
#                            globo (import-state) si el destino tiene reconciliador. Fase «pushed».
#   start                    solo tras un push completo: comprueba que la config del destino es la
#                            publicada (sha256) y que el origen no tiene config; qm start. Fase «started».
#   rollback                 vuelve al origen (ver README §Rollback). Exige respuesta POSITIVA del
#                            destino (config ausente, o presente y parada); si la VM llegó a arrancar
#                            en destino o sus discos tienen escrituras posteriores, exige además
#                            ACK_DISCARD_DEST_WRITES=<VMID>. Fase «rolledback»; la copia aparcada se
#                            archiva (no se reutiliza).
#   close                    tras verificar el traslado (y correr migrate_vm.sh): cierra el estado
#                            (fase «done») y archiva la copia aparcada. Los zvols del origen se quedan
#                            hasta borrarlos a mano tras el keep.
#   status                   estado del traslado y de la VM en cada lado.
#
# Fases y cerrojo: $WORK/<VMID>.state liga el traslado a VMID, UUID (smbios1), nodo origen, nodo e IP
# destino y snapshot final; cada paso exige la fase anterior y los mismos datos. Un solo proceso del
# kit por VM a la vez ($WORK/<VMID>.lock). Ninguna comprobación del destino toma un fallo de SSH por
# «no hay nada»: sin respuesta completa, se aborta sin tocar nada.
#
# Guarda de CPU (shutdown la comprueba antes de apagar; push la aplica; coherente con migrate_vm.sh,
# _live_silicon_guard / _cold_hwid_guard, y con el defrag):
#   - En frío cualquier CPU arranca, pero el invitado verá la del destino. Con un modelo genérico
#     (x86-64-v2/v3/v4) el Hardware ID de SQX no cambia (medido: Genoa↔Turin e Intel↔AMD con v4).
#   - Con cpu=host/max, otro modelo con nombre o sin línea cpu (kvm64), si el silicio
#     (vendor|family|model de /proc/cpuinfo) cambia, el Hardware ID cambia: se exige UNA de
#       CHANGE_CPU=1                pone en el destino la CPU de las altas (x86-64-v4, o v3 sin AVX-512);
#       ACK_HWID_CHANGE=<VMID>      deja la CPU tal cual (el invitado verá el silicio nuevo);
#     y en los dos casos CPU_NOTICE='<cuándo y por dónde se avisó al cliente de reactivar licencias>'.
#   - x86-64-v4 hacia un destino sin AVX-512 no arrancaría (PVE pone enforce): solo con CHANGE_CPU=1.
#   Se registra en $WORK/cpu-change.txt al publicar. El rollback recupera la CPU antigua (la config del
#   origen no se toca).
#
# Variables: las de rtmig_common.sh (VMID, DST, DST_NODE, WORK, SSH_PORT) y CHANGE_CPU,
# ACK_HWID_CHANGE, CPU_NOTICE (shutdown y push), ACK_DISCARD_DEST_WRITES (rollback).
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=rtmig_common.sh
source "$HERE/rtmig_common.sh"

has_reconciler_src() { [[ -x "$NV_RECONCILER" && -f "$NV_RAM_GUARD" ]]; }
# imprime yes | no | unknown (SSH sin respuesta)
reconciler_dst() {
  local out
  out=$(dst "if [ -x \"\${NV_RECONCILER:-/usr/local/sbin/neuravps-balloon-reconciler.sh}\" ] && [ -f \"\${NV_RAM_GUARD:-/usr/local/sbin/neuravps-ram-guard.py}\" ]; then echo REC=yes; else echo REC=no; fi" 2>/dev/null) || true
  case "$out" in *REC=yes*) echo yes ;; *REC=no*) echo no ;; *) echo unknown ;; esac
}

# CPU efectiva de una config (sección principal, sin [snapshots]): la que verá el invitado al arrancar.
conf_cpu() { awk '/^\[/{exit} /^cpu:/{sub(/^cpu:[ \t]*/,""); sub(/,.*/,""); sub(/^cputype=/,""); print; exit}' "$1"; }
is_generic_cpu() { [[ "$1" =~ ^x86-64-v[0-9]+(-[A-Za-z0-9]+)?$ ]]; }

# Pone «key: value» en la sección principal de una config (sustituye o añade al principio).
conf_set_key() {   # <fichero> <key> <value>
  local f="$1" k="$2" v="$3" tmp="$1.tmp.$$"
  awk -v k="$k" -v v="$v" 'BEGIN{done=0; top=1}
       /^\[/ {top=0}
       top && index($0, k ":")==1 {print k ": " v; done=1; next}
       {print}
       END{}' "$f" > "$tmp"
  if ! grep -q "^${k}: ${v}\$" "$tmp"; then { printf '%s: %s\n' "$k" "$v"; cat "$tmp"; } > "$tmp.2"; mv "$tmp.2" "$tmp"; fi
  mv "$tmp" "$f"
}

# Decide la CPU del destino: deja en CPU_NEW la CPU a poner ("" = no tocar) o muere. Sin subshell,
# para que los mensajes se vean. <conf> <record 0|1>: con 1 deja constancia del ACK en cpu-change.txt.
cpu_decision() {
  local conf="$1" record="${2:-0}" cpu lc src_sig dst_sig flags need_v3=0 new="" cpuinfo_dst
  cpu=$(conf_cpu "$conf"); cpu="${cpu:-kvm64}"; lc="${cpu,,}"
  src_sig=$(cpu_sig_from < "$CPUINFO")
  cpuinfo_dst=$(dst "cat \"\${CPUINFO:-/proc/cpuinfo}\"" 2>/dev/null) || true
  dst_sig=$(cpu_sig_from <<< "$cpuinfo_dst")
  flags=$(grep -m1 '^flags' <<< "$cpuinfo_dst" || true)
  [[ "$src_sig" =~ ^[^|]+\|[0-9]+\|[0-9]+$ && "$dst_sig" =~ ^[^|]+\|[0-9]+\|[0-9]+$ && -n "$flags" ]] \
    || die "no pude leer la CPU de origen [${src_sig}] o destino [${dst_sig}]; no se ha tocado nada"
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
    [[ -n "${ACK_HWID_CHANGE:-}" ]] || die "cpu=${cpu} enseña la CPU real y el silicio cambia (origen [${src_sig//|/ }] → destino [${dst_sig//|/ }]): el Hardware ID de SQX y las licencias del Market cambiarán. Hace falta dejar constancia del aviso al cliente: CHANGE_CPU=1 (pone ${tgt}) o ACK_HWID_CHANGE=${VMID} (deja ${cpu}), con CPU_NOTICE='…'. No se ha tocado nada"
    if [[ "$record" == 1 ]]; then
      printf '%s VM%s cpu %s sin cambiar, silicio %s -> %s ACK_HWID_CHANGE aviso=%s\n' "$(date -u +%FT%TZ)" "$VMID" "$cpu" "$src_sig" "$dst_sig" "$CPU_NOTICE" >> "$WORK/cpu-change.txt"
      log "ACK_HWID_CHANGE=${VMID}: cpu=${cpu} se queda; el invitado verá [${dst_sig//|/ }] (aviso: ${CPU_NOTICE})"
    fi
  fi
  CPU_NEW="$new"
  CPU_OLD="$cpu"
}

no_active_transfer() {
  local ph; ph=$(state_get PHASE)
  [[ -z "$ph" || "$ph" == rolledback || "$ph" == done ]] \
    || die "ya hay un traslado de la VM ${VMID} en fase '${ph}' ($STATE); termínalo o haz rollback"
}

archive_parked() {   # <sufijo>: la copia aparcada no se reutiliza nunca
  local ts f; ts=$(date -u +%Y%m%dT%H%M%SZ)
  for f in conf.src fw.src balloon-state conf.push; do
    [[ -e "$WORK/${VMID}.${f}" ]] && mv "$WORK/${VMID}.${f}" "$WORK/${VMID}.${f}.${1}-${ts}"
  done
  return 0
}

take_lock
CMD="${1:-}"
case "$CMD" in shutdown|park|push|start|rollback|close|status) ;; *) awk 'NR>1 \&\& /^#/ {print; next} NR>1 {exit}' "$0"; exit 2 ;; esac
check_dst_identity

case "$CMD" in
  shutdown)
    T="${2:-600}"; [[ "$T" =~ ^[0-9]+$ ]] || die "timeout no numérico: $T"
    no_active_transfer
    probe src
    [[ "$P_CONF" == present && "$P_STATUS" == running ]] || die "la VM ${VMID} no está 'running' en origen (config ${P_CONF}, estado ${P_STATUS})"
    # Comprobaciones previas, ANTES de apagar: inventario de discos, CPU/licencias y destino libre.
    DISKS=$(detect_disks)
    cpu_decision "$CONF" 0
    probe dst
    [[ "$P_CONF" == absent ]] || die "el destino ${DST_NODE} ya tiene config de la VM ${VMID}: no apago"
    [[ -n "$(conf_uuid "$CONF")" ]] || die "la config no tiene smbios1 uuid: el kit no puede ligar el traslado a la VM"
    log "previas OK: discos ${DISKS}; cpu ${CPU_OLD}${CPU_NEW:+ → ${CPU_NEW} en push}; destino libre"
    log "qm shutdown ${VMID} --timeout ${T} (ordenado; sin forceStop)"
    t0=$SECONDS
    if qm shutdown "$VMID" --timeout "$T"; then
      probe src
      log "apagada en $((SECONDS - t0)) s; estado: ${P_STATUS}"
    else
      probe src
      die "Windows no se apagó en ${T} s. NO forzar. La VM sigue en estado '${P_STATUS}'. Escalar (README §Apagado)."
    fi
    [[ "$P_STATUS" == "stopped" ]] || die "tras el shutdown el estado es '${P_STATUS}'"
    ;;
  park)
    no_active_transfer
    probe src
    [[ "$P_CONF" == present && "$P_STATUS" == stopped ]] || die "solo con la VM parada y su config en /etc/pve (config ${P_CONF}, estado ${P_STATUS})"
    for f in conf.src fw.src balloon-state conf.push; do
      [[ ! -e "$WORK/${VMID}.${f}" ]] || die "queda una copia anterior $WORK/${VMID}.${f} (¿traslado a medias o rollback antiguo?); revísala y archívala a mano"
    done
    UUID=$(conf_uuid "$CONF"); [[ -n "$UUID" ]] || die "la config no tiene smbios1 uuid: el kit no puede ligar el traslado a la VM"
    DISKS=$(detect_disks)
    probe dst
    [[ "$P_CONF" == absent ]] || die "el destino ${DST_NODE} ya tiene config de la VM ${VMID}"
    # Historial del reconciliador de globo: export-state necesita la config viva, así que va ANTES de
    # aparcar. Solo si este nodo tiene reconciliador (en un EX44 no hay historial que llevar).
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
    if [[ -f "$FW" ]]; then cmp -s "$FW" "$WORK/${VMID}.fw.src" || die "la copia del .fw no coincide"; fi
    # El estado se abre ANTES de quitar la config: si algo cae aquí, push verá la config en origen y parará.
    rm -f "$STATE"
    state_set "VMID=${VMID}" "UUID=${UUID}" "SRC_NODE=$(src_node)" "DST_NODE=${DST_NODE}" "DST=${DST}" \
              "DISKS=${DISKS}" "PHASE=parked" "PARKED_AT=$(date -u +%FT%TZ)"
    rm -f "$CONF"; [[ -f "$WORK/${VMID}.fw.src" ]] && rm -f "$FW"
    probe src
    [[ "$P_CONF" == absent ]] || die "qm aún ve la config de la VM en origen"
    log "config aparcada en $WORK/${VMID}.conf.src (fw: $( [[ -f $WORK/${VMID}.fw.src ]] && echo sí || echo no )); fase parked; zvols intactos:"
    zfs list -H -o name,refer -r "$POOLDS" | grep "vm-${VMID}-" || true
    ;;
  push)
    SNAP="${2:?SNAP final}"; B="${3:-}"; check_snap_name "$SNAP"
    [[ -z "$B" || "$B" =~ ^[0-9]+$ ]] || die "balloon_mb no numérico: $B"
    require_state parked
    probe src
    [[ "$P_CONF" == absent ]] || die "el origen vuelve a tener config de la VM ${VMID}: no publico en destino (dos configs arrancables)"
    probe dst
    [[ "$P_CONF" == absent ]] || die "el destino ya tiene config de ${VMID}"
    cpu_decision "$WORK/${VMID}.conf.src" 0
    DISKS=$(DISKS="" detect_disks)   # inventario COMPLETO de la config aparcada
    [[ "$DISKS" == "$(state_get DISKS)" ]] || die "el inventario de discos (${DISKS}) no es el de park ($(state_get DISKS))"
    for d in $DISKS; do
      ds="${POOLDS}/vm-${VMID}-disk-${d}"
      gs=$(zfs get -H -o value guid "${ds}@${SNAP}" 2>/dev/null || true)
      gd=$(dst_zget guid "${ds}@${SNAP}")
      [[ -n "$gs" && "$gs" == "$gd" ]] || die "$ds@$SNAP: guid origen=[$gs] destino=[$gd] — falta la sincronización final"
      ws=$(zfs get -Hp -o value "written@${SNAP}" "$ds")
      wd=$(dst_zget "written@${SNAP}" "$ds" -p)
      [[ "$ws" == 0 && "$wd" == 0 ]] || die "$ds: escrito después de @$SNAP origen=${ws} B destino=${wd} B — la final no es la última"
      log "disco OK: $ds@$SNAP guid=$gs, 0 B posteriores en ambos lados"
    done
    # Config final, preparada en local: CPU y balloon ya aplicados. Se publica de una sola vez.
    cp -a "$WORK/${VMID}.conf.src" "$WORK/${VMID}.conf.push"
    [[ -n "$B" ]] && conf_set_key "$WORK/${VMID}.conf.push" balloon "$B"
    [[ -n "$CPU_NEW" ]] && conf_set_key "$WORK/${VMID}.conf.push" cpu "$CPU_NEW"
    PUSH_SHA=$(sha256sum < "$WORK/${VMID}.conf.push" | cut -d' ' -f1)
    if [[ -f "$WORK/${VMID}.fw.src" ]]; then   # el .fw solo no hace arrancable la VM: va primero
      dst_in "cat > \"${R_FW}\"" < "$WORK/${VMID}.fw.src" || die "no pude escribir el .fw en el destino; la config NO se ha publicado"
    fi
    dst_in "cat > \"${R_CONF}\"" < "$WORK/${VMID}.conf.push" || true
    probe dst
    if [[ "$P_CONF" != present || "$P_SHA" != "$PUSH_SHA" ]]; then
      dst "rm -f \"${R_CONF}\"" 2>/dev/null || true
      probe dst
      [[ "$P_CONF" == absent ]] || die "la config publicada en destino no es la esperada y no pude retirarla: REVISAR A MANO ${DST_NODE}:/etc/pve/qemu-server/${VMID}.conf antes de nada"
      die "la config no llegó entera al destino (sha distinto); retirada. Repite push"
    fi
    state_set "PHASE=pushed" "SNAP=${SNAP}" "CONF_SHA=${PUSH_SHA}" "PUSHED_AT=$(date -u +%FT%TZ)"
    [[ -n "$B" ]] && log "balloon=${B} en destino"
    if [[ -n "$CPU_NEW" ]]; then
      log "CHANGE_CPU=1: cpu ${CPU_OLD} -> ${CPU_NEW} en destino (aviso: ${CPU_NOTICE}); la config del origen conserva la antigua (rollback)"
      printf '%s VM%s cpu %s -> %s aviso=%s\n' "$(date -u +%FT%TZ)" "$VMID" "$CPU_OLD" "$CPU_NEW" "$CPU_NOTICE" >> "$WORK/cpu-change.txt"
    elif [[ "${CHANGE_CPU:-0}" == 1 ]]; then
      log "CHANGE_CPU=1: la VM ya tiene la CPU de las altas; no se cambia"
    elif [[ -n "${ACK_HWID_CHANGE:-}" ]]; then
      cpu_decision "$WORK/${VMID}.conf.src" 1   # deja la constancia, ya publicada
    fi
    log "config publicada en ${DST_NODE} (sha256 ${PUSH_SHA:0:12}…); fase pushed"
    # Historial del globo: import-state necesita la config ya escrita en el destino.
    if [[ -s "$WORK/${VMID}.balloon-state" ]]; then
      case "$(reconciler_dst)" in
        yes)
          if dst "python3 \"\${NV_RAM_GUARD:-/usr/local/sbin/neuravps-ram-guard.py}\" import-state ${VMID} '${DST_NODE}' '$(cat "$WORK/${VMID}.balloon-state")'" >/dev/null 2>&1; then
            log "historial del globo importado en ${DST_NODE} (retención de 24 h)"
          else
            warn "no pude importar el historial del globo en ${DST_NODE}: lo reaprenderá de forma conservadora"
          fi ;;
        no) log "${DST_NODE} no tiene reconciliador de globo: el historial no se importa" ;;
        *)  warn "no sé si ${DST_NODE} tiene reconciliador (SSH sin respuesta): el historial no se importa" ;;
      esac
    fi
    dst "qm config $VMID" | grep -E "^(balloon|memory|cores|cpu|scsi0|efidisk0|tpmstate0|net0|hookscript|name):" || true
    ;;
  start)
    require_state pushed
    probe src
    [[ "$P_CONF" == absent ]] || die "la VM tiene config en origen: no arranco en destino"
    probe dst
    [[ "$P_CONF" == present ]] || die "el destino no tiene la config de ${VMID}"
    [[ "$P_SHA" == "$(state_get CONF_SHA)" ]] || die "la config del destino no es la publicada por push (sha distinto): revisar a mano"
    [[ "$P_STATUS" == stopped ]] || die "la VM está '${P_STATUS}' en destino"
    log "qm start ${VMID} en destino"
    dst "qm start $VMID" || die "qm start falló en destino"
    for _ in $(seq 1 20); do probe dst; [[ "$P_STATUS" == "running" ]] && break; sleep 3; done
    log "estado en destino: ${P_STATUS}"
    [[ "$P_STATUS" == "running" ]] || die "no llegó a running"
    state_set "PHASE=started" "STARTED_AT=$(date -u +%FT%TZ)"
    log "siguiente paso, en una BASE: migrate_vm.sh ${VMID} <número de ${DST_NODE}> (rama «already on dest»)"
    ;;
  rollback)
    require_state parked pushed started
    PH=$(state_get PHASE); SNAP=$(state_get SNAP)
    probe src
    [[ "$P_CONF" == absent ]] || die "ya hay config de la VM en origen: nada que restaurar"
    probe dst
    if [[ "$P_CONF" == present ]]; then
      [[ "$P_STATUS" == "stopped" ]] || die "la VM está '${P_STATUS}' en destino: apágala ORDENADAMENTE allí primero (qm shutdown), con decisión de una persona"
    fi
    # ¿Hubo actividad en destino? Si arrancó allí, o sus discos tienen escrituras posteriores a la
    # final, volver al origen las PIERDE: hace falta consentimiento expreso.
    ACTIVITY=""
    [[ "$PH" == started ]] && ACTIVITY="la VM llegó a arrancar en destino"
    if [[ -n "$SNAP" ]]; then
      for d in $(state_get DISKS); do
        w=$(dst_zget "written@${SNAP}" "${POOLDS}/vm-${VMID}-disk-${d}" -p)
        [[ "$w" == ABSENT || "$w" == 0 ]] || ACTIVITY="${ACTIVITY:+$ACTIVITY; }disk-${d} tiene ${w} B escritos en destino tras @${SNAP}"
      done
    fi
    if [[ -n "$ACTIVITY" && "${ACK_DISCARD_DEST_WRITES:-}" != "$VMID" ]]; then
      die "volver al origen PIERDE lo escrito en destino (${ACTIVITY}). Si es lo decidido: ACK_DISCARD_DEST_WRITES=${VMID}. No se ha tocado nada"
    fi
    if [[ "$P_CONF" == present || "$P_FW" == present ]]; then
      dst "mkdir -p '$WORK' && ts=\$(date -u +%Y%m%dT%H%M%SZ) && { [ ! -e \"${R_CONF}\" ] || cp -a \"${R_CONF}\" '$WORK/${VMID}.conf.dst-'\$ts; } && rm -f \"${R_CONF}\" \"${R_FW}\"" \
        || die "no pude aparcar la config del destino (SSH); no se ha restaurado nada en origen"
      probe dst
      [[ "$P_CONF" == absent && "$P_FW" == absent ]] || die "la config del destino sigue ahí tras aparcarla: no restauro el origen"
      log "config del destino aparcada en ${DST_NODE}:$WORK/${VMID}.conf.dst-* (discos de destino intactos)"
    fi
    cp -a "$WORK/${VMID}.conf.src" "$CONF"
    [[ -f "$WORK/${VMID}.fw.src" ]] && cp -a "$WORK/${VMID}.fw.src" "$FW"
    archive_parked rolledback
    state_set "PHASE=rolledback" "ROLLEDBACK_AT=$(date -u +%FT%TZ)" ${ACTIVITY:+"DISCARDED=${ACTIVITY}"}
    qm start "$VMID"
    probe src
    log "VM ${VMID} arrancada de nuevo en ORIGEN; estado: ${P_STATUS}. Si ya corriste migrate_vm.sh hacia el destino, vuelve a correrlo hacia el origen."
    ;;
  close)
    require_state started
    probe src
    [[ "$P_CONF" == absent ]] || die "el origen tiene config de la VM: no cierro"
    probe dst
    [[ "$P_CONF" == present ]] || die "el destino no tiene la config de la VM: no cierro"
    archive_parked done
    state_set "PHASE=done" "CLOSED_AT=$(date -u +%FT%TZ)"
    log "traslado cerrado (fase done); copia aparcada archivada en $WORK/${VMID}.*.done-*. Los zvols y snapshots @rtmig-* del origen se borran a mano tras el keep"
    ;;
  status)
    log "traslado: $( [[ -f $STATE ]] && tr '\n' ' ' < "$STATE" || echo 'sin estado')"
    probe src; log "origen $(src_node): config=${P_CONF} estado=${P_STATUS} aparcada=$( [[ -f $WORK/${VMID}.conf.src ]] && echo sí || echo no )"
    probe dst; log "destino ${DST_NODE}: config=${P_CONF} estado=${P_STATUS} sha=${P_SHA:0:12}"
    ;;
esac

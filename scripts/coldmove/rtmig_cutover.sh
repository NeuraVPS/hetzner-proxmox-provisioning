#!/usr/bin/env bash
# rtmig_cutover.sh — pasos del corte de un traslado en frío con precopia ZFS (ver README.md).
# Se ejecuta EN EL NODO ORIGEN (root). Habla con el destino por SSH directo nodo→nodo.
#
#   shutdown [timeout_s]     comprobaciones previas (sin cambios [PENDING], discos, CPU/licencias,
#                            destino libre) y
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

# CPU con la que ARRANCARÁ la VM: la de [PENDING] si hay un cambio pendiente (qm start lo aplica),
# ninguna (kvm64) si [PENDING] la borra, y si no, la de la sección principal. Nunca la de un snapshot.
conf_cpu() {
  awk 'BEGIN{sec="main"}
       /^\[/ {sec=$0; next}
       sec=="main" && /^cpu:/ {m=$0}
       sec=="[PENDING]" && /^cpu:/ {p=$0}
       sec=="[PENDING]" && /^delete:/ {s=$0; sub(/^delete:[ \t]*/,"",s); n=split(s,a,/[ ,]+/); for(i=1;i<=n;i++) if(a[i]=="cpu") d=1}
       END{v = (p!="") ? p : (d ? "" : m); sub(/^cpu:[ \t]*/,"",v); sub(/,.*/,"",v); sub(/^cputype=/,"",v); print v}' "$1"
}
# rc 0 si [PENDING] cambia o borra <key>
pending_touches() {
  awk -v k="$2" 'BEGIN{sec="main"; t=0}
       /^\[/ {sec=$0; next}
       sec=="[PENDING]" && index($0, k ":")==1 {t=1}
       sec=="[PENDING]" && /^delete:/ {s=$0; sub(/^delete:[ \t]*/,"",s); n=split(s,a,/[ ,]+/); for(i=1;i<=n;i++) if(a[i]==k) t=1}
       END{exit t ? 0 : 1}' "$1"
}
is_generic_cpu() { [[ "$1" =~ ^x86-64-v[0-9]+(-[A-Za-z0-9]+)?$ ]]; }

# Pone «key: value» en la sección PRINCIPAL de una config: sustituye la línea si está, o la añade al
# final de esa sección. Las secciones [PENDING]/[snapshot] no cuentan ni se tocan.
conf_set_key() {   # <fichero> <key> <value>
  local f="$1" k="$2" v="$3" tmp="$1.tmp.$$"
  awk -v k="$k" -v v="$v" 'BEGIN{done=0; top=1}
       /^\[/ { if (top && !done) {print k ": " v; done=1}; top=0 }
       top && index($0, k ":")==1 { if (!done) {print k ": " v; done=1}; next }
       {print}
       END{ if (top && !done) print k ": " v }' "$f" > "$tmp"
  mv "$tmp" "$f"
}

# rc 0 si la config del destino (última probe dst) es la de ESTE traslado: presente y con su UUID.
dst_conf_is_ours() { [[ "$P_CONF" == present && -n "$P_UUID" && "$P_UUID" == "$(state_get UUID)" ]]; }

# CPU con la que CORRE ahora la VM en origen (primer campo de -cpu en la línea de QEMU), o vacío.
QEMU_PIDDIR="${QEMU_PIDDIR:-/var/run/qemu-server}"; PROC_DIR="${PROC_DIR:-/proc}"
running_cpu() {
  local pid
  pid=$(cat "$QEMU_PIDDIR/${VMID}.pid" 2>/dev/null) || return 0
  [[ "$pid" =~ ^[0-9]+$ ]] || return 0
  tr '\0' '\n' < "$PROC_DIR/$pid/cmdline" 2>/dev/null | grep -A1 -x -- '-cpu' | tail -1 | cut -d, -f1 || true
}
# Clase de CPU a efectos de Hardware ID: host (host/max), generic (x86-64-v*; PVE los arranca como
# qemu64,+flags), o el propio modelo (kvm64, EPYC-Genoa…).
cpu_class() {
  local lc="${1,,}"
  case "$lc" in
    host|max) echo host ;;
    qemu64|x86-64-v*) echo generic ;;
    "") echo kvm64 ;;
    *) echo "$lc" ;;
  esac
}

# Decide la CPU del destino (sin escribir nada): deja CPU_NEW (CPU a poner, "" = no tocar), CPU_OLD (la
# del próximo arranque según la config) y CPU_ACK_REASON (por qué hizo falta ACK_HWID_CHANGE, o vacío),
# o muere. <conf> [cpu con la que corría la VM en origen]: si el modelo con el que corría y el del
# próximo arranque son de clase distinta (p. ej. host en marcha con [PENDING] cpu: x86-64-v4), el
# Hardware ID cambia aunque el silicio no cambie: también exige constancia del aviso.
cpu_decision() {
  local conf="$1" run_cpu="${2:-}" cpu lc src_sig dst_sig flags need_v3=0 new="" cpuinfo_dst reason=""
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

  if [[ -n "$run_cpu" && "$(cpu_class "$run_cpu")" != "$(cpu_class "$cpu")" ]]; then
    reason="cambio de modelo pendiente: corría con ${run_cpu} y arrancará con ${cpu}"
  fi
  if [[ "${CHANGE_CPU:-0}" == 1 ]]; then
    [[ "$lc" == "$tgt" ]] || new="$tgt"
  else
    if is_generic_cpu "$lc"; then
      if [[ "$lc" == x86-64-v4* && $need_v3 == 1 ]]; then
        die "la VM usa ${cpu} y el destino ${DST_NODE} no tiene AVX-512: no arrancaría. Solo con CHANGE_CPU=1 (→ x86-64-v3) y el cliente avisado"
      fi
    elif [[ "$src_sig" != "$dst_sig" ]]; then
      reason="${reason:+$reason; }cpu=${cpu} con silicio distinto ${src_sig} -> ${dst_sig}"
    fi
    if [[ -n "$reason" && -z "${ACK_HWID_CHANGE:-}" ]]; then
      die "el Hardware ID de SQX y las licencias del Market cambiarán (${reason}). Hace falta dejar constancia del aviso al cliente: CHANGE_CPU=1 (pone ${tgt}) o ACK_HWID_CHANGE=${VMID} (deja ${cpu}), con CPU_NOTICE='…'. No se ha tocado nada"
    fi
  fi
  CPU_NEW="$new"; CPU_OLD="$cpu"; CPU_ACK_REASON=""
  [[ -n "${ACK_HWID_CHANGE:-}" ]] && CPU_ACK_REASON="${reason:-sin cambio de Hardware ID detectado}"
  return 0
}

# Archivado de la copia aparcada, recuperable: la fase terminal (rolledback/done) se escribe antes con
# ARCHIVE_PENDING=<sufijo>; esto mueve lo que quede y limpia la marca. Idempotente.
finish_archive() {
  local suf; suf=$(state_get ARCHIVE_PENDING)
  [[ -n "$suf" ]] || return 0
  archive_parked "$suf"
  state_set "ARCHIVE_PENDING="
}

# Copia local segura a /etc/pve del origen: <destino>.rtmig-tmp, sha256, mv -n.
local_publish() {   # <fichero> <ruta final>
  local f="$1" final="$2" sha
  sha=$(sha256sum < "$f" | cut -d' ' -f1)
  rm -f "${final}.rtmig-tmp"
  cp "$f" "${final}.rtmig-tmp" || { rm -f "${final}.rtmig-tmp"; return 1; }
  [[ "$(sha256sum < "${final}.rtmig-tmp" | cut -d' ' -f1)" == "$sha" ]] || { rm -f "${final}.rtmig-tmp"; return 1; }
  [[ ! -e "$final" ]] || { rm -f "${final}.rtmig-tmp"; return 1; }
  mv -n "${final}.rtmig-tmp" "$final" && [[ ! -e "${final}.rtmig-tmp" ]]
}

no_active_transfer() {
  finish_archive   # un archivado que se cortó tras la fase terminal
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

# Tras la última «probe dst»: config publicada (UUID y sha256 del estado) y .fw publicado (sha256) o
# ausente si el origen no tenía. Muere si algo no cuadra.
verify_dst_published() {
  dst_conf_is_ours || die "el destino no tiene la config publicada por este traslado (config ${P_CONF}, UUID ${P_UUID:-?})"
  [[ "$P_SHA" == "$(state_get CONF_SHA)" ]] || die "la config del destino no es la publicada por push (sha ${P_SHA:0:12} ≠ $(state_get CONF_SHA | cut -c1-12)): revisar a mano"
  if [[ "$(state_get FW_SHA)" == none ]]; then
    [[ "$P_FW" == absent ]] || die "el destino tiene un .fw y este traslado no publicó ninguno: revisar a mano"
  else
    [[ "$P_FW" == present && "$P_FWSHA" == "$(state_get FW_SHA)" ]] || die "el .fw del destino no es el publicado (sha ${P_FWSHA:0:12}): revisar a mano"
  fi
}

# Cierre del push, ÚNICO para el camino normal y para la reanudación (fase publishing con la config
# ya en destino): verifica los dos ficheros, deja la constancia de CPU una vez, importa el historial
# del globo una vez y marca «pushed». Todo sale del estado, no del entorno.
finish_push() {
  local cn co ca note
  probe dst
  verify_dst_published
  if [[ "$(state_get CPU_RECORDED)" != 1 ]]; then
    cn=$(state_get CPU_NEW); co=$(state_get CPU_OLD); ca=$(state_get CPU_ACK_REASON); note=$(state_get CPU_NOTICE)
    if [[ -n "$cn" ]]; then
      printf '%s VM%s cpu %s -> %s CHANGE_CPU aviso=%s\n' "$(date -u +%FT%TZ)" "$VMID" "$co" "$cn" "$note" >> "$WORK/cpu-change.txt"
      log "CHANGE_CPU=1: cpu ${co} -> ${cn} en destino (aviso: ${note}); la config del origen conserva la antigua (rollback)"
    elif [[ -n "$ca" ]]; then
      printf '%s VM%s cpu %s sin cambiar, ACK_HWID_CHANGE (%s) aviso=%s\n' "$(date -u +%FT%TZ)" "$VMID" "$co" "$ca" "$note" >> "$WORK/cpu-change.txt"
      log "ACK_HWID_CHANGE=${VMID}: cpu=${co} se queda (${ca}; aviso: ${note})"
    fi
    state_set "CPU_RECORDED=1"
  fi
  if [[ "$(state_get BALLOON_IMPORTED)" != 1 && -s "$WORK/${VMID}.balloon-state" ]]; then
    # import-state necesita la config ya escrita en el destino.
    case "$(reconciler_dst)" in
      yes)
        if dst "python3 \"\${NV_RAM_GUARD:-/usr/local/sbin/neuravps-ram-guard.py}\" import-state ${VMID} '${DST_NODE}' '$(cat "$WORK/${VMID}.balloon-state")'" >/dev/null 2>&1; then
          state_set "BALLOON_IMPORTED=1"
          log "historial del globo importado en ${DST_NODE} (retención de 24 h)"
        else
          warn "no pude importar el historial del globo en ${DST_NODE}: lo reaprenderá de forma conservadora"
        fi ;;
      no) log "${DST_NODE} no tiene reconciliador de globo: el historial no se importa" ;;
      *)  warn "no sé si ${DST_NODE} tiene reconciliador (SSH sin respuesta): el historial no se importa" ;;
    esac
  fi
  state_set "PHASE=pushed" "PUSHED_AT=$(date -u +%FT%TZ)"
  [[ -n "$(state_get BALLOON_MB)" ]] && log "balloon=$(state_get BALLOON_MB) en destino"
  log "config publicada en ${DST_NODE} (sha256 $(state_get CONF_SHA | cut -c1-12)…, fw $(state_get FW_SHA | cut -c1-12)); fase pushed"
  dst "qm config $VMID" 2>/dev/null | grep -E "^(balloon|memory|cores|cpu|scsi0|efidisk0|tpmstate0|net0|hookscript|name):" || true
}

# Config del origen sin cambios pendientes: imprime su sha256, o muere. La lectura tiene que ser
# correcta (awk con rc 0 y una respuesta CLEAN|PENDING explícita): cualquier otra cosa aborta. La
# cabecera se reconoce como PVE (QemuServer: /^\[PENDING\]\s*$/i, sin distinguir mayúsculas y con
# espacios al final); los «delete:» pendientes solo existen dentro de esa sección.
conf_clean_sha() {
  local out sha
  [[ -s "$CONF" ]] || die "no pude leer la config de la VM ${VMID} ($CONF)"
  out=$(awk 'BEGIN{p=0} { l=tolower($0); sub(/[ \t\r]+$/, "", l); if (l == "[pending]") p=1 } END{print (p ? "PENDING" : "CLEAN")}' "$CONF") \
    || die "no pude leer la config de la VM ${VMID} para buscar cambios pendientes: no apago"
  case "$out" in
    CLEAN) ;;
    PENDING) die "la config de la VM ${VMID} tiene cambios pendientes ([PENDING]) que PVE aplicaría al apagarla: la config validada no sería la que se traslada. Aplícalos (reiniciando la VM) o revíertelos (qm set ${VMID} --revert <claves>) y vuelve a lanzar shutdown. No se ha tocado nada" ;;
    *) die "respuesta rara al buscar cambios pendientes en la config de la VM ${VMID}: no apago" ;;
  esac
  sha=$(sha256sum < "$CONF" | cut -d' ' -f1) || die "no pude leer la config de la VM ${VMID}"
  [[ "$sha" =~ ^[0-9a-f]{64}$ ]] || die "no pude leer la config de la VM ${VMID}"
  echo "$sha"
}

take_lock
finish_archive   # archivado pendiente de una fase terminal anterior (si se cortó justo después)
CMD="${1:-}"
case "$CMD" in shutdown|park|push|start|rollback|close|status) ;; *) awk 'NR>1 \&\& /^#/ {print; next} NR>1 {exit}' "$0"; exit 2 ;; esac
check_dst_identity

case "$CMD" in
  shutdown)
    T="${2:-600}"; [[ "$T" =~ ^[0-9]+$ ]] || die "timeout no numérico: $T"
    no_active_transfer
    probe src
    [[ "$P_CONF" == present && "$P_STATUS" == running ]] || die "la VM ${VMID} no está 'running' en origen (config ${P_CONF}, estado ${P_STATUS})"
    # Cambios pendientes: PVE los aplica al parar la VM (QemuServer vm_stop_cleanup / _do_vm_stop), así
    # que la config que se valida aquí no sería la que se aparca y se publica. Se exige una config sin
    # sección [PENDING] ANTES de apagar (se vuelve a comprobar justo antes de `qm shutdown`).
    CONF_SHA0=$(conf_clean_sha)
    # Comprobaciones previas, ANTES de apagar: inventario de discos, CPU/licencias y destino libre.
    DISKS=$(detect_disks)
    RUN_CPU=$(running_cpu)
    [[ -n "$RUN_CPU" ]] || die "no pude leer con qué CPU corre la VM (${QEMU_PIDDIR}/${VMID}.pid, /proc/<pid>/cmdline): no se puede saber si cambia el Hardware ID; no apago"
    cpu_decision "$CONF" "$RUN_CPU"
    probe dst
    [[ "$P_CONF" == absent ]] || die "el destino ${DST_NODE} ya tiene config de la VM ${VMID}: no apago"
    [[ "$P_FW" == absent ]] || die "el destino ${DST_NODE} tiene un .fw de la VM ${VMID}: no es de este traslado; revísalo antes de apagar"
    [[ -n "$(conf_uuid "$CONF")" ]] || die "la config no tiene smbios1 uuid: el kit no puede ligar el traslado a la VM"
    printf '%s\n' "$RUN_CPU" > "$WORK/${VMID}.run-cpu"
    log "previas OK: discos ${DISKS}; corre con ${RUN_CPU}, arrancará con ${CPU_OLD}${CPU_NEW:+ → ${CPU_NEW} en push}; destino libre"
    # Justo antes de apagar: la config sigue sin [PENDING] y es EXACTAMENTE la que se ha comprobado (una
    # edición concurrente durante las comprobaciones remotas hace abortar con la VM en marcha).
    [[ "$(conf_clean_sha)" == "$CONF_SHA0" ]] \
      || die "la config de la VM ${VMID} cambió mientras se hacían las comprobaciones: vuelve a lanzar shutdown. No se ha tocado nada"
    printf '%s\n' "$CONF_SHA0" > "$WORK/${VMID}.shutdown-conf-sha"
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
    # Si alguien metió un cambio pendiente DURANTE el apagado, PVE lo ha aplicado al parar: la config ya
    # no es la comprobada. No se impide, pero se detecta, y park tampoco seguirá con ella.
    [[ "$(sha256sum < "$CONF" | cut -d' ' -f1)" == "$CONF_SHA0" ]] \
      || die "la config de la VM ${VMID} cambió durante el apagado (¿un cambio pendiente aplicado al parar?): la VM está parada en el origen con una config distinta de la comprobada. Revísala; para seguir, arráncala (qm start ${VMID}) y repite el corte desde shutdown"
    ;;
  park)
    no_active_transfer
    probe src
    [[ "$P_CONF" == present && "$P_STATUS" == stopped ]] || die "solo con la VM parada y su config en /etc/pve (config ${P_CONF}, estado ${P_STATUS})"
    # La config a aparcar tiene que ser la que comprobó shutdown (si el apagado lo hizo el kit).
    if [[ -f "$WORK/${VMID}.shutdown-conf-sha" ]]; then
      [[ "$(sha256sum < "$CONF" | cut -d' ' -f1)" == "$(head -1 "$WORK/${VMID}.shutdown-conf-sha")" ]] \
        || die "la config de la VM ${VMID} no es la que comprobó shutdown (¿cambio pendiente aplicado al parar?): no la aparco. Revísala; para seguir, arráncala (qm start ${VMID}) y repite el corte desde shutdown"
    fi
    for f in conf.src fw.src balloon-state conf.push; do
      [[ ! -e "$WORK/${VMID}.${f}" ]] || die "queda una copia anterior $WORK/${VMID}.${f} (¿traslado a medias o rollback antiguo?); revísala y archívala a mano"
    done
    UUID=$(conf_uuid "$CONF"); [[ -n "$UUID" ]] || die "la config no tiene smbios1 uuid: el kit no puede ligar el traslado a la VM"
    DISKS=$(detect_disks)
    probe dst
    [[ "$P_CONF" == absent ]] || die "el destino ${DST_NODE} ya tiene config de la VM ${VMID}"
    [[ "$P_FW" == absent ]] || die "el destino ${DST_NODE} tiene un .fw de la VM ${VMID} sin config: no es de este traslado; revísalo"
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
    # Copia PROVISIONAL, y solo si es exactamente la config que comprobó shutdown (y la del origen en
    # este momento) se promueve a la copia aparcada. Si algo cambió durante la sonda del destino o el
    # export, la config se queda en el origen y la provisional se descarta.
    STG_CONF="$WORK/${VMID}.conf.staging"; STG_FW="$WORK/${VMID}.fw.staging"
    rm -f "$STG_CONF" "$STG_FW"
    discard_staging() { rm -f "$STG_CONF" "$STG_FW"; die "$1. La config se queda en el origen; no se ha aparcado nada. Revísala; para seguir, arranca la VM (qm start ${VMID}) y repite el corte desde shutdown"; }
    cp -a "$CONF" "$STG_CONF" || discard_staging "no pude copiar la config"
    [[ -f "$FW" ]] && { cp -a "$FW" "$STG_FW" || discard_staging "no pude copiar el .fw"; }
    STG_SHA=$(sha256sum < "$STG_CONF" | cut -d' ' -f1)
    if [[ -f "$WORK/${VMID}.shutdown-conf-sha" && "$STG_SHA" != "$(head -1 "$WORK/${VMID}.shutdown-conf-sha")" ]]; then
      discard_staging "la config de la VM ${VMID} cambió desde que la comprobó shutdown"
    fi
    [[ "$(conf_uuid "$STG_CONF")" == "$UUID" && "$(disk_inventory "$STG_CONF")" == "$DISKS" ]] \
      || discard_staging "la config de la VM ${VMID} cambió durante el park (UUID o discos)"
    # Y el origen sigue siendo exactamente esa copia justo antes de aparcarla.
    cmp -s "$CONF" "$STG_CONF" || discard_staging "la config de la VM ${VMID} cambió durante el park"
    if [[ -f "$FW" || -f "$STG_FW" ]]; then
      [[ -f "$FW" && -f "$STG_FW" ]] && cmp -s "$FW" "$STG_FW" || discard_staging "el .fw de la VM ${VMID} cambió durante el park"
    fi
    [[ -f "$STG_FW" ]] && mv "$STG_FW" "$WORK/${VMID}.fw.src"
    mv "$STG_CONF" "$WORK/${VMID}.conf.src"
    # El estado se abre ANTES de quitar la config: si algo cae aquí, push verá la config en origen y parará.
    rm -f "$STATE"
    state_set "VMID=${VMID}" "UUID=${UUID}" "SRC_NODE=$(src_node)" "DST_NODE=${DST_NODE}" "DST=${DST}" \
              "DISKS=${DISKS}" "POOLDS=${POOLDS}" "PHASE=parked" "PARKED_AT=$(date -u +%FT%TZ)" \
              "RUN_CPU=$(head -1 "$WORK/${VMID}.run-cpu" 2>/dev/null | tr -cd 'A-Za-z0-9._+-')"
    rm -f "$WORK/${VMID}.run-cpu" "$WORK/${VMID}.shutdown-conf-sha"
    rm -f "$CONF"; [[ -f "$WORK/${VMID}.fw.src" ]] && rm -f "$FW"
    probe src
    [[ "$P_CONF" == absent ]] || die "qm aún ve la config de la VM en origen"
    log "config aparcada en $WORK/${VMID}.conf.src (fw: $( [[ -f $WORK/${VMID}.fw.src ]] && echo sí || echo no )); fase parked; zvols intactos:"
    zfs list -H -o name,refer -r "$POOLDS" | grep "vm-${VMID}-" || true
    ;;
  push)
    SNAP="${2:?SNAP final}"; B="${3:-}"; check_snap_name "$SNAP"
    [[ -z "$B" || "$B" =~ ^[0-9]+$ ]] || die "balloon_mb no numérico: $B"
    require_state parked publishing   # publishing = un push anterior se cortó a mitad: se retoma
    PH=$(state_get PHASE)
    probe src
    [[ "$P_CONF" == absent ]] || die "el origen vuelve a tener config de la VM ${VMID}: no publico en destino (dos configs arrancables)"
    probe dst
    if [[ "$PH" == publishing && "$P_CONF" == present ]] && dst_conf_is_ours && [[ "$P_SHA" == "$(state_get CONF_SHA)" ]]; then
      log "la config ya estaba publicada por el push anterior (sha ${P_SHA:0:12}…): completo el cierre del push"
      finish_push
      exit 0
    fi
    [[ "$P_CONF" == absent ]] || die "el destino ya tiene config de ${VMID} y no es la publicada por este traslado"
    DST_FW_BEFORE="$P_FW"; DST_FWSHA_BEFORE="$P_FWSHA"
    if [[ "$P_FW" == present ]]; then
      [[ "$PH" == publishing && "$P_FWSHA" == "$(state_get FW_SHA)" ]] \
        || die "el destino tiene un .fw de la VM ${VMID} que no es de este traslado: revísalo a mano; no se ha publicado nada"
    fi
    cpu_decision "$WORK/${VMID}.conf.src" "$(state_get RUN_CPU)"
    if [[ -n "$CPU_NEW" ]] && pending_touches "$WORK/${VMID}.conf.src" cpu; then
      die "la config tiene un cambio PENDIENTE de cpu: aplícalo o descártalo en origen antes (rollback) y repite"
    fi
    if [[ -n "$B" ]] && pending_touches "$WORK/${VMID}.conf.src" balloon; then
      die "la config tiene un cambio PENDIENTE de balloon: no se puede fijar ${B} sin pisarlo; resuélvelo antes"
    fi
    DISKS=$(DISKS="" detect_disks)   # inventario COMPLETO de la config aparcada
    [[ "$DISKS" == "$(state_get DISKS)" ]] || die "el inventario de discos (${DISKS}) no es el de park ($(state_get DISKS))"
    for d in $DISKS; do
      ds="${POOLDS}/vm-${VMID}-disk-${d}"
      gs=$(zfs get -H -o value guid "${ds}@${SNAP}" 2>/dev/null || true)
      gd=$(dst_zget guid "${ds}@${SNAP}") || exit 1
      [[ -n "$gs" && "$gs" == "$gd" ]] || die "$ds@$SNAP: guid origen=[$gs] destino=[$gd] — falta la sincronización final"
      ws=$(zfs get -Hp -o value "written@${SNAP}" "$ds" 2>/dev/null || true)
      wd=$(dst_zget "written@${SNAP}" "$ds" -p) || exit 1
      [[ "$ws" == 0 && "$wd" == 0 ]] || die "$ds: escrito después de @$SNAP origen=[${ws}] destino=[${wd}] — la final no es la última"
      log "disco OK: $ds@$SNAP guid=$gs, 0 B posteriores en ambos lados"
    done
    # Config final, preparada en local: CPU y balloon ya aplicados. Se publica de una sola vez.
    cp -a "$WORK/${VMID}.conf.src" "$WORK/${VMID}.conf.push"
    [[ -n "$B" ]] && conf_set_key "$WORK/${VMID}.conf.push" balloon "$B"
    [[ -n "$CPU_NEW" ]] && conf_set_key "$WORK/${VMID}.conf.push" cpu "$CPU_NEW"
    PUSH_SHA=$(sha256sum < "$WORK/${VMID}.conf.push" | cut -d' ' -f1)
    if [[ -f "$WORK/${VMID}.fw.src" ]]; then FW_SHA=$(sha256sum < "$WORK/${VMID}.fw.src" | cut -d' ' -f1); else FW_SHA=none; fi
    # Intención registrada ANTES de escribir nada en el destino: lo que haya allí con estos hashes es nuestro.
    # La decisión de CPU (y el aviso que la justifica) va en la intención: el cierre del push la deja
    # en cpu-change.txt una sola vez, aunque se repita push sin las variables.
    state_set "PHASE=publishing" "SNAP=${SNAP}" "CONF_SHA=${PUSH_SHA}" "FW_SHA=${FW_SHA}" \
              "CPU_OLD=${CPU_OLD}" "CPU_NEW=${CPU_NEW}" "CPU_ACK_REASON=${CPU_ACK_REASON}" \
              "CPU_NOTICE=$(printf '%s' "${CPU_NOTICE:-}" | tr '\n\r' '  ')" "CPU_RECORDED=0" "BALLOON_IMPORTED=0" \
              "BALLOON_MB=${B}"
    if [[ "$FW_SHA" == none ]]; then
      [[ "$DST_FW_BEFORE" == absent ]] || die "el origen no tiene .fw pero el destino sí (de un push anterior con otro .fw): revísalo; no se ha publicado nada"
    elif [[ "$DST_FW_BEFORE" == present && "$DST_FWSHA_BEFORE" == "$FW_SHA" ]]; then
      log ".fw ya publicado por el push anterior (sha ${FW_SHA:0:12}…)"
    else   # el .fw solo no hace arrancable la VM: va primero
      r=$(dst_publish "$WORK/${VMID}.fw.src" "$R_FW")
      probe dst   # manda lo que hay en el destino, no la respuesta (pudo perderse tras el rename)
      [[ "$P_FW" == present && "$P_FWSHA" == "$FW_SHA" ]] \
        || die "el .fw no llegó bien al destino (${r}); la config NO se ha publicado. Repite push o haz rollback"
      [[ "$r" == PUBLISHED ]] || warn "la respuesta de la publicación del .fw se perdió (${r}), pero está en el destino con su sha"
    fi
    r=$(dst_publish "$WORK/${VMID}.conf.push" "$R_CONF")
    probe dst   # manda lo que hay en el destino, no la respuesta (pudo perderse tras el rename)
    if [[ "$P_CONF" == absent ]]; then
      die "la config no se publicó (${r}); el destino sigue sin ella. Repite push o haz rollback"
    fi
    dst_conf_is_ours && [[ "$P_SHA" == "$PUSH_SHA" ]] \
      || die "la config del destino no es la esperada tras publicar (${r}): REVISAR A MANO ${DST_NODE}:/etc/pve/qemu-server/${VMID}.conf"
    [[ "$r" == PUBLISHED ]] || warn "la respuesta de la publicación se perdió (${r}), pero la config está en el destino con su sha y su UUID"
    finish_push
    ;;
  start)
    # Reentrante: con la fase «starting» (se perdió la confirmación) o «pushed» y la VM ya en marcha
    # en destino con la config publicada, solo se reconcilia el estado.
    require_state pushed starting
    probe src
    [[ "$P_CONF" == absent ]] || die "la VM tiene config en origen: no arranco en destino"
    probe dst
    verify_dst_published
    if [[ "$P_STATUS" == running ]]; then
      log "la VM ya está running en destino: reconcilio el estado"
    else
      [[ "$P_STATUS" == stopped ]] || die "la VM está '${P_STATUS}' en destino"
      state_set "PHASE=starting" "START_REQUESTED_AT=$(date -u +%FT%TZ)"
      log "qm start ${VMID} en destino"
      dst "qm start $VMID" || warn "qm start devolvió error (o se cortó el SSH): compruebo el estado"
      for _ in $(seq 1 20); do probe dst; [[ "$P_STATUS" == "running" ]] && break; sleep 3; done
      log "estado en destino: ${P_STATUS}"
      [[ "$P_STATUS" == "running" ]] || die "no llegó a running (fase starting; repite start cuando se aclare)"
    fi
    state_set "PHASE=started" "STARTED_AT=$(date -u +%FT%TZ)"
    log "siguiente paso, en una BASE: migrate_vm.sh ${VMID} <número de ${DST_NODE}> (rama «already on dest»)"
    ;;
  rollback)
    require_state parked publishing pushed starting started rollingback
    PH=$(state_get PHASE); SNAP=$(state_get SNAP)
    if [[ "$PH" != rollingback ]]; then
      probe src
      [[ "$P_CONF" == absent ]] || die "ya hay config de la VM en origen: nada que restaurar"
      probe dst
      OURS_CONF=0; OURS_FW=0
      if [[ "$P_CONF" == present ]]; then
        [[ "$PH" != parked ]] || die "el destino tiene una config de la VM ${VMID} y este traslado aún no publicó nada: no es nuestra, no la toco"
        dst_conf_is_ours || die "la config de la VM ${VMID} en destino no es la de este traslado (UUID ${P_UUID:-?} ≠ $(state_get UUID)): no la toco"
        [[ "$P_STATUS" == "stopped" ]] || die "la VM está '${P_STATUS}' en destino: apágala ORDENADAMENTE allí primero (qm shutdown), con decisión de una persona"
        OURS_CONF=1
      fi
      if [[ "$P_FW" == present ]]; then
        if (( OURS_CONF )) || [[ "$PH" != parked && "$P_FWSHA" == "$(state_get FW_SHA)" ]]; then OURS_FW=1
        else die "hay un .fw de la VM ${VMID} en destino que no es de este traslado: no lo toco; revísalo a mano"; fi
      fi
      # ¿Hubo actividad en destino? Si arrancó allí, o sus discos tienen escrituras posteriores a la
      # final, volver al origen las PIERDE: hace falta consentimiento expreso. Cada disco tiene que
      # dar un entero (verificado en push): ABSENT o un error es estado indeterminado.
      ACTIVITY=""
      [[ "$PH" == started || "$PH" == starting ]] && ACTIVITY="la VM llegó (o pudo llegar) a arrancar en destino"
      if [[ -n "$SNAP" && "$PH" != parked ]]; then
        for d in $(state_get DISKS); do
          w=$(dst_zget "written@${SNAP}" "${POOLDS}/vm-${VMID}-disk-${d}" -p) || exit 1
          [[ "$w" =~ ^[0-9]+$ ]] || die "disk-${d}: no hay un número de bytes escritos en destino tras @${SNAP} (${w}): estado INDETERMINADO, no restauro"
          [[ "$w" == 0 ]] || ACTIVITY="${ACTIVITY:+$ACTIVITY; }disk-${d} tiene ${w} B escritos en destino tras @${SNAP}"
        done
      fi
      if [[ -n "$ACTIVITY" && "${ACK_DISCARD_DEST_WRITES:-}" != "$VMID" ]]; then
        die "volver al origen PIERDE lo escrito en destino (${ACTIVITY}). Si es lo decidido: ACK_DISCARD_DEST_WRITES=${VMID}. No se ha tocado nada"
      fi
      if (( OURS_CONF || OURS_FW )); then
        rmlist=""; (( OURS_CONF )) && rmlist="\"${R_CONF}\""; (( OURS_FW )) && rmlist="${rmlist} \"${R_FW}\""
        dst "mkdir -p '$WORK' && ts=\$(date -u +%Y%m%dT%H%M%SZ) && { [ ! -e \"${R_CONF}\" ] || cp -a \"${R_CONF}\" '$WORK/${VMID}.conf.dst-'\$ts; } && { [ ! -e \"${R_FW}\" ] || cp -a \"${R_FW}\" '$WORK/${VMID}.fw.dst-'\$ts; } && rm -f ${rmlist}" \
          || die "no pude aparcar la config del destino (SSH); no se ha restaurado nada en origen"
        probe dst
        [[ "$P_CONF" == absent && "$P_FW" == absent ]] || die "la config o el .fw del destino siguen ahí tras aparcarlos: no restauro el origen"
        log "config del destino aparcada en ${DST_NODE}:$WORK/${VMID}.conf.dst-* (discos de destino intactos)"
      fi
      state_set "PHASE=rollingback" "ROLLBACK_FROM=${PH}" ${ACTIVITY:+"DISCARDED=${ACTIVITY}"}
    else
      log "rollback a medias (fase rollingback): lo continúo"
      probe dst
      [[ "$P_CONF" == absent ]] || die "en fase rollingback el destino vuelve a tener config de la VM: revisar a mano"
    fi
    # Restauración en origen (reentrante): .fw y config se reconcilian POR SEPARADO contra la copia
    # aparcada (sha256, o ausencia si no había .fw), cada uno con tmp + mv -n (nunca a medias), el .fw
    # primero; solo se arranca con los dos verificados.
    CONF_SRC_SHA=$(sha256sum < "$WORK/${VMID}.conf.src" | cut -d' ' -f1)
    if [[ -f "$WORK/${VMID}.fw.src" ]]; then FW_SRC_SHA=$(sha256sum < "$WORK/${VMID}.fw.src" | cut -d' ' -f1); else FW_SRC_SHA=none; fi
    probe src
    if [[ "$FW_SRC_SHA" == none ]]; then
      [[ "$P_FW" == absent ]] || die "el origen tiene un .fw de la VM pero la copia aparcada no tenía: revisar a mano antes de arrancar"
    elif [[ "$P_FW" == absent ]]; then
      local_publish "$WORK/${VMID}.fw.src" "$FW" || die "no pude restaurar el .fw en origen (fase rollingback; repite rollback)"
    fi
    if [[ "$P_CONF" == absent ]]; then
      local_publish "$WORK/${VMID}.conf.src" "$CONF" || die "no pude restaurar la config en origen (fase rollingback; repite rollback)"
    fi
    probe src
    [[ "$P_CONF" == present && "$P_UUID" == "$(state_get UUID)" && "$P_SHA" == "$CONF_SRC_SHA" ]] \
      || die "la config del origen no es la aparcada (UUID ${P_UUID:-?}, sha ${P_SHA:0:12}): revisar a mano; no arranco"
    if [[ "$FW_SRC_SHA" == none ]]; then
      [[ "$P_FW" == absent ]] || die "el origen tiene un .fw que no estaba en la copia aparcada: no arranco"
    else
      [[ "$P_FW" == present && "$P_FWSHA" == "$FW_SRC_SHA" ]] || die "el .fw del origen no es el aparcado (sha ${P_FWSHA:0:12}): no arranco sin su firewall"
    fi
    if [[ "$P_STATUS" != running ]]; then qm start "$VMID" || warn "qm start devolvió error: compruebo el estado"; probe src; fi
    [[ "$P_STATUS" == running ]] || die "la VM no llegó a running en origen (fase rollingback; repite rollback)"
    # Fase terminal PRIMERO (con el archivado pendiente) y luego el archivado: si se corta en medio,
    # el siguiente paso del kit termina el archivado (ver finish_archive).
    state_set "PHASE=rolledback" "ROLLEDBACK_AT=$(date -u +%FT%TZ)" "ARCHIVE_PENDING=rolledback"
    finish_archive
    log "VM ${VMID} arrancada de nuevo en ORIGEN; estado: ${P_STATUS}. Si ya corriste migrate_vm.sh hacia el destino, vuelve a correrlo hacia el origen."
    ;;
  close)
    require_state started starting
    probe src
    [[ "$P_CONF" == absent ]] || die "el origen tiene config de la VM: no cierro"
    probe dst
    dst_conf_is_ours || die "la config de la VM en destino no es la de este traslado (UUID ${P_UUID:-?}): no cierro"
    [[ "$P_STATUS" == running ]] || die "la VM no está running en destino (${P_STATUS}): no cierro"
    state_set "PHASE=done" "CLOSED_AT=$(date -u +%FT%TZ)" "ARCHIVE_PENDING=done"
    finish_archive
    log "traslado cerrado (fase done); copia aparcada archivada en $WORK/${VMID}.*.done-*. Los zvols y snapshots @rtmig-* del origen se borran a mano tras el keep"
    ;;
  status)
    log "traslado: $( [[ -f $STATE ]] && tr '\n' ' ' < "$STATE" || echo 'sin estado')"
    probe src; log "origen $(src_node): config=${P_CONF} estado=${P_STATUS} aparcada=$( [[ -f $WORK/${VMID}.conf.src ]] && echo sí || echo no )"
    probe dst; log "destino ${DST_NODE}: config=${P_CONF} estado=${P_STATUS} sha=${P_SHA:0:12}"
    ;;
esac

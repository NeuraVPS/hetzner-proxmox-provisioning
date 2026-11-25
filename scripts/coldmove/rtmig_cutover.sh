#!/usr/bin/env bash
# rtmig_cutover.sh — pasos del corte de un traslado en frío con precopia ZFS (ver README.md).
# Se ejecuta EN EL NODO ORIGEN en modo push (por defecto) o EN EL DESTINO en modo pull (RTMIG_MODE=pull,
# para un origen sin clave de flota). Las conexiones nodo→nodo las abre siempre el nodo que corre el
# kit; al otro solo le llegan órdenes sueltas, ninguna de las cuales hace ssh.
#
#   shutdown [timeout_s]     comprobaciones previas sobre copias verificadas de la config y el .fw del
#                            origen (discos, perfil y MAC en pull, CPU/licencias, precopia común,
#                            destino libre), abre el estado (fase «stopping») y qm shutdown ORDENADO
#                            (nunca stop/forceStop). Si vence el tiempo, sale con error y la VM SIGUE
#                            encendida: decide una persona (repetir shutdown o rollback).
#   park                     con la VM parada y su config igual a la validada: fase «preparing», copia
#                            verificada a $WORK/<VMID>.staging/ (y perfil en pull), promoción atómica
#                            del directorio a $WORK/<VMID>.parked/, fase «parked» y quita la config de /etc/pve
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
# Variables: las de rtmig_common.sh (RTMIG_MODE, VMID, DST/DST_NODE o SRC/SRC_NODE, WORK, SSH_PORT) y CHANGE_CPU,
# ACK_HWID_CHANGE, CPU_NOTICE (shutdown y push), ACK_DISCARD_DEST_WRITES (rollback).
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=rtmig_common.sh
source "$HERE/rtmig_common.sh"

# ¿Tiene ese lado reconciliador de globo? Imprime yes | no | unknown (sin respuesta). <src|dst>
reconciler() {
  local out
  out=$(on "$1" "if [ -x \"\${NV_RECONCILER:-/usr/local/sbin/neuravps-balloon-reconciler.sh}\" ] && [ -f \"\${NV_RAM_GUARD:-/usr/local/sbin/neuravps-ram-guard.py}\" ]; then echo REC=yes; else echo REC=no; fi" 2>/dev/null) || true
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

# ---- perfil local de VM (modo pull: la config y el .fw del origen son entrada HOSTIL) ------------------
# Se construye en ESTE nodo una config nueva solo con claves permitidas y valores con formato
# estricto; el hookscript es siempre el nuestro. Cualquier clave fuera de la lista (args, hostpci, usb,
# serial, parallel, audio, rng, virtiofs, cicustom, lock, startup, hookscript ajeno…), un [PENDING] o un
# valor con formato raro hacen abortar. Las secciones de snapshot y `parent` se descartan (sus
# snapshots no viajan). Imprime en stderr qué se descartó.
NV_HOOKSCRIPT="${NV_HOOKSCRIPT:-shared:snippets/sync-dnat.py}"
sanitize_conf() {   # <entrada> <salida> <uuid esperado>
  VMID="$VMID" HOOK="$NV_HOOKSCRIPT" UUID_WANT="$3" A_MEM="${ALLOC_MEMORY_MB:-}" A_CORES="${ALLOC_CORES:-}" \
    A_DISK="${ALLOC_DISK_GB:-}" python3 - "$1" "$2" <<'PYSAN'
import os, re, sys
vmid, hook, uuid_want = os.environ["VMID"], os.environ["HOOK"], os.environ["UUID_WANT"]
a_mem, a_cores, a_disk = int(os.environ["A_MEM"]), int(os.environ["A_CORES"]), int(os.environ["A_DISK"])
src, dst = sys.argv[1], sys.argv[2]
DISK = r"local-zfs:vm-%s-disk-\d{1,3}" % re.escape(vmid)
DOPT = r"(,(cache|discard|iothread|size|ssd|efitype|pre-enrolled-keys|ms-cert|version|aio|backup|replicate|format)=[A-Za-z0-9.+_-]{1,32})*"
RULES = {
    "name": r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}",
    "memory": r"\d{1,7}", "balloon": r"\d{1,7}", "cores": r"\d{1,3}", "sockets": r"\d{1,2}", "vcpus": r"\d{1,3}",
    "cpu": r"(x86-64-v[234](-AES)?|host|max|kvm64|qemu64|EPYC[A-Za-z0-9-]{0,24})",
    "ostype": r"(win11|win10|win8|win7|w2k8|wvista|wxp|w2k|l26|l24|other|solaris)",
    "machine": r"(pc|q35|pc-q35-[0-9.]+(\+pve\d+)?|pc-i440fx-[0-9.]+(\+pve\d+)?)",
    "bios": r"(ovmf|seabios)", "boot": r"order=[a-z0-9;]{1,64}", "scsihw": r"[a-z0-9-]{1,32}",
    "agent": r"[01](,(fstrim_cloned_disks|freeze-fs-on-backup|type)=[a-z0-9-]{1,16})*",
    "smbios1": r"uuid=[0-9A-Fa-f-]{36}(,base64=1)?(,(family|manufacturer|product|serial|sku|version)=[A-Za-z0-9+/=]{0,128})*",
    "vmgenid": r"[0-9A-Fa-f-]{36}",
    "numa": r"[01]", "tablet": r"[01]", "onboot": r"[01]", "localtime": r"[01]", "kvm": r"[01]",
    "vga": r"(std|virtio|qxl|none|serial0)(,memory=\d{1,4})?",
    "net0": r"virtio=[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5},bridge=vmbr0(,(firewall|queues|mtu|link_down)=\d{1,5}|,rate=[0-9.]{1,8})*",
    "efidisk0": DISK + DOPT, "tpmstate0": DISK + DOPT,
}
DISK_KEY = re.compile(r"(scsi\d{1,2}|virtio\d{1,2}|sata\d|unused\d{1,2})$")
DROP = {"digest", "meta", "parent", "hookscript"}   # hookscript: se fija aquí, no se copia
CDROM = re.compile(r"(ide|sata)\d")
out, bad, dropped, top, seen = [], [], [], True, set()
for raw in open(src, encoding="utf-8", errors="strict").read().splitlines():
    line = raw.rstrip("\r")
    if line.startswith("["):
        if line.strip() == "[PENDING]":
            bad.append("[PENDING]")
        top = False
        dropped.append(line.strip()); continue
    if not top:
        continue
    if not line.strip():
        continue
    if line.startswith("#"):
        if re.fullmatch(r"#neuravps-stream-template-key%3A [A-Za-z0-9._-]{1,80}", line):
            out.append(line)
        else:
            dropped.append("#descripción")
        continue
    m = re.fullmatch(r"([a-z0-9_-]{1,32}):\s?(.*)", line)
    if not m:
        bad.append(repr(line[:40])); continue
    k, v = m.group(1), m.group(2).strip()
    if k in seen:
        bad.append(k + " (repetida)"); continue
    seen.add(k)
    if k in DROP:
        dropped.append(k); continue
    if k in RULES:
        ok = re.fullmatch(RULES[k], v)
    elif DISK_KEY.fullmatch(k):
        ok = re.fullmatch(DISK + (DOPT if not k.startswith("unused") else ""), v)
    elif CDROM.fullmatch(k):
        ok = re.fullmatch(r"none,media=cdrom", v)
    else:
        bad.append(k); continue
    if not ok:
        bad.append(k + "=" + v[:40]); continue
    out.append("%s: %s" % (k, v))
if bad:
    sys.stderr.write("NO-PERMITIDO " + "; ".join(bad) + "\n"); sys.exit(3)
sm = [l for l in out if l.startswith("smbios1: ")]
if not sm or ("uuid=" + uuid_want) not in sm[0]:
    sys.stderr.write("NO-PERMITIDO smbios1 sin el UUID del traslado\n"); sys.exit(3)
for need in ("memory", "cores", "net0", "name"):
    if not any(l.startswith(need + ": ") for l in out):
        sys.stderr.write("NO-PERMITIDO falta %s\n" % need); sys.exit(3)
# Recursos: los de la asignación FIABLE (servers/<doc>), nunca los que pida el origen.
def val(k):
    for l in out:
        if l.startswith(k + ": "):
            return l.split(": ", 1)[1]
    return None
notes = []
old = (val("memory"), val("cores"), val("sockets"), val("vcpus"), val("balloon"))
bal = val("balloon")
bal = min(int(bal), a_mem) if bal is not None else None
out = [l for l in out if not l.startswith(("memory: ", "cores: ", "sockets: ", "vcpus: ", "balloon: "))]
out += ["memory: %d" % a_mem, "cores: %d" % a_cores, "sockets: 1"]
if bal is not None:
    out.append("balloon: %d" % bal)
new = (str(a_mem), str(a_cores), "1", None, None if bal is None else str(bal))
if old != new:
    notes.append("recursos de la asignación: memory %s→%d, cores %s→%d, sockets %s→1%s" % (
        old[0], a_mem, old[1], a_cores, old[2], "" if old[4] == new[4] else ", balloon %s→%s" % (old[4], new[4])))
# Disco: la suma de los tamaños declarados no puede pasar de la asignación (+64 MiB de efi/tpm).
UNIT = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}
total = 0
for l in out:
    k, v = l.split(": ", 1)
    if (DISK_KEY.fullmatch(k) and not k.startswith("unused")) or k in ("efidisk0", "tpmstate0"):
        m = re.search(r",size=(\d{1,7})([KMGT])", v)
        if not m:
            sys.stderr.write("NO-PERMITIDO %s sin size\n" % k); sys.exit(3)
        total += int(m.group(1)) * UNIT[m.group(2)]
if total > a_disk * (1 << 30) + (64 << 20):
    sys.stderr.write("NO-PERMITIDO discos de %d GiB > asignación de %d GiB\n" % (total >> 30, a_disk)); sys.exit(3)
out.append("hookscript: " + hook)
open(dst, "w").write("\n".join(out) + "\n")
if dropped:
    sys.stderr.write("DESCARTADO " + ", ".join(dropped) + "\n")
for n in notes:
    sys.stderr.write("AJUSTADO " + n + "\n")
PYSAN
}

# El .fw por VM de un origen no fiable: solo [OPTIONS] con enable (y policy_in DROP|REJECT, que solo
# restringe), y [RULES] con los grupos de la
# flota (activos o desactivados con «|»). Nada más (ni reglas propias, ni IPSET, ni ALIASES, ni
# políticas). Se reescribe en limpio.
sanitize_fw() {   # <entrada> <salida>
  python3 - "$1" "$2" <<'PYFW'
import re, sys
src, dst = sys.argv[1], sys.argv[2]
sec, opts, rules, bad = None, [], [], []
for raw in open(src, encoding="utf-8", errors="strict").read().splitlines():
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    if line in ("[OPTIONS]", "[RULES]"):
        sec = line; continue
    if line.startswith("["):
        bad.append(line[:40]); sec = "x"; continue
    if sec == "[OPTIONS]" and re.fullmatch(r"enable:\s?[01]", line):
        opts.append("enable: " + line[-1]); continue
    if sec == "[OPTIONS]" and re.fullmatch(r"policy_in:\s?(DROP|REJECT)", line):   # solo las restrictivas
        opts.append("policy_in: " + line.split(":", 1)[1].strip()); continue
    if sec == "[RULES]":
        m = re.fullmatch(r"(\|?)GROUP (vm-default|vm-no-internet|vm-public-ipv6)(\s+#.*)?", line)
        if m:
            rules.append("%sGROUP %s" % (m.group(1), m.group(2))); continue
    bad.append(line[:40])
if bad:
    sys.stderr.write("NO-PERMITIDO " + "; ".join(bad) + "\n"); sys.exit(3)
if len([o for o in opts if o.startswith("enable")]) > 1 or len([o for o in opts if o.startswith("policy_in")]) > 1:
    sys.stderr.write("NO-PERMITIDO opción repetida en [OPTIONS]\n"); sys.exit(3)
txt = "[OPTIONS]\n\n" + "\n".join(opts) + ("\n" if opts else "") + "\n[RULES]\n\n" + "\n".join(rules) + ("\n" if rules else "")
open(dst, "w").write(txt)
PYFW
}

# Aplica el perfil (solo en pull) a <conf> [<fw>] → $WORK/<VMID>.conf.prof [.fw.prof]; muere si no pasa.
profile_check() {   # <conf> <uuid> [fw]
  local err
  if ! err=$(sanitize_conf "$1" "$WORK/${VMID}.conf.prof" "$2" 2>&1 >/dev/null); then
    die "la config del origen no cumple el perfil permitido para traerla a ${DST_NODE}: ${err#NO-PERMITIDO } (no se toca nada; resolverlo en el origen)"
  fi
  [[ -n "$err" ]] && log "perfil: ${err}"
  if [[ -n "${3:-}" ]]; then
    if ! err=$(sanitize_fw "$3" "$WORK/${VMID}.fw.prof" 2>&1 >/dev/null); then
      die "el .fw del origen no cumple el perfil permitido: ${err#NO-PERMITIDO } (no se toca nada)"
    fi
  fi
  return 0
}

# MAC de net0 (del perfil): prefijo esperado y que ninguna otra VM del DESTINO la use (antes de apagar
# y al publicar). La MAC no se cambia: cambiarla rompe la red de Windows.
# Prefijos admitidos por defecto: 52:54:00, el que pone create_vm (NeuraVPS functions/proxmox_handlers.py,
# _randomize_net0_mac: '52:54:00:%02x:%02x:%02x' con secrets.randbelow(256)), y BC:24:11, el que genera
# PVE por su cuenta. Medido el 25/11/2026: la mayoría de la flota es 52:54:00 (0000231: 8 de 11; la 1517
# del 238 y KDMLAB-E1 del 266 también). EXPECTED_MAC_PREFIXES (lista separada por espacios o comas)
# lo cambia; EXPECTED_MAC_PREFIX (uno solo) se sigue aceptando.
EXPECTED_MAC_PREFIXES="${EXPECTED_MAC_PREFIXES:-${EXPECTED_MAC_PREFIX:-52:54:00 BC:24:11}}"
mac_check() {   # <config del perfil>
  local mac others p ok=0 list
  list=$(tr ',' ' ' <<< "$EXPECTED_MAC_PREFIXES" | tr 'a-f' 'A-F')
  for p in $list; do
    [[ "$p" =~ ^[0-9A-F]{2}(:[0-9A-F]{2}){0,5}$ ]] || die "prefijo de MAC no válido en EXPECTED_MAC_PREFIXES: ${p}"
  done
  [[ -n "${list// /}" ]] || die "EXPECTED_MAC_PREFIXES vacío"
  mac=$(sed -n 's/^net0: virtio=\([0-9A-Fa-f:]\{17\}\),.*/\1/p' "$1" | head -1 | tr 'a-f' 'A-F')
  [[ -n "$mac" ]] || die "la config no tiene una MAC de net0 reconocible"
  for p in $list; do [[ "$mac" == "$p"* ]] && ok=1; done
  (( ok )) || die "la MAC ${mac} no tiene ninguno de los prefijos esperados (${list}); si es legítima, añade el suyo a EXPECTED_MAC_PREFIXES"
  others=$(on_q dst "cd \"\${PVE_ETC:-/etc/pve}/qemu-server\" && for f in *.conf; do [ -e \"\$f\" ] || continue; [ \"\$f\" = ${VMID}.conf ] && continue; grep -il '${mac}' \"\$f\" || true; done") \
    || die "no pude comprobar las MAC del destino"
  [[ -z "$others" ]] || die "la MAC ${mac} ya la usa otra VM en ${DST_NODE} (${others//$'\n'/ }): no sigo"
}

# rc 0 si la config del destino (última probe dst) es la de ESTE traslado: presente y con su UUID.
dst_conf_is_ours() { [[ "$P_CONF" == present && -n "$P_UUID" && "$P_UUID" == "$(state_get UUID)" ]]; }

# CPU con la que CORRE ahora la VM en el origen (primer campo de -cpu en la línea de QEMU), o vacío.
running_cpu() {
  on src "p=\$(cat \"\${QEMU_PIDDIR:-/var/run/qemu-server}/${VMID}.pid\" 2>/dev/null) && case \"\$p\" in ''|*[!0-9]*) ;; *) tr '\\0' '\\n' < \"\${PROC_DIR:-/proc}/\$p/cmdline\" | grep -A1 -x -- '-cpu' | tail -1 | cut -d, -f1 ;; esac" 2>/dev/null | tr -cd 'A-Za-z0-9._+-' || true
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
  src_sig=$(on src "cat \"\${CPUINFO:-/proc/cpuinfo}\"" 2>/dev/null | cpu_sig_from) || true
  cpuinfo_dst=$(on dst "cat \"\${CPUINFO:-/proc/cpuinfo}\"" 2>/dev/null) || true
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

no_active_transfer() {
  finish_archive   # un archivado que se cortó tras la fase terminal
  migrate_old_layout
  local ph; ph=$(state_get PHASE)
  [[ -z "$ph" || "$ph" == rolledback || "$ph" == done ]] \
    || die "ya hay un traslado de la VM ${VMID} en fase '${ph}' ($STATE); termínalo o haz rollback"
}

archive_parked() {   # <sufijo>: la copia aparcada (y el staging) no se reutilizan nunca
  local ts f; ts=$(date -u +%Y%m%dT%H%M%SZ)
  [[ -e "$PARKED_DIR" ]] && mv -T "$PARKED_DIR" "${PARKED_DIR}.${1}-${ts}"
  [[ -e "$STAGE_DIR" ]] && mv -T "$STAGE_DIR" "${STAGE_DIR}.${1}-${ts}"
  for f in conf.push conf.prof fw.prof conf.live fw.live; do
    [[ -e "$WORK/${VMID}.${f}" ]] && mv "$WORK/${VMID}.${f}" "$WORK/${VMID}.${f}.${1}-${ts}"
  done
  return 0
}

# El manifiesto del directorio aparcado cuadra con el estado (y sus ficheros con el manifiesto).
parked_manifest_ok() {
  [[ -f "$PARKED_DIR/MANIFEST" && -f "$CONF_SRC" ]] || return 1
  [[ "$(sed -n 's/^CONF_SHA=//p' "$PARKED_DIR/MANIFEST")" == "$(state_get SRC_CONF_SHA)" ]] || return 1
  [[ "$(sed -n 's/^FW_SHA=//p' "$PARKED_DIR/MANIFEST")" == "$(state_get SRC_FW_SHA)" ]] || return 1
  [[ "$(sha256sum < "$CONF_SRC" | cut -d' ' -f1)" == "$(state_get SRC_CONF_SHA)" ]] || return 1
  if [[ "$(state_get SRC_FW_SHA)" == none ]]; then [[ ! -e "$FW_SRC" ]]
  else [[ -f "$FW_SRC" && "$(sha256sum < "$FW_SRC" | cut -d' ' -f1)" == "$(state_get SRC_FW_SHA)" ]]; fi
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
  if [[ "$(state_get BALLOON_IMPORTED)" != 1 && -s "$PARKED_DIR/balloon-state" ]]; then
    # import-state necesita la config ya escrita en el destino.
    case "$(reconciler dst)" in
      yes)
        if on_q dst "python3 \"\${NV_RAM_GUARD:-/usr/local/sbin/neuravps-ram-guard.py}\" import-state ${VMID} '${DST_NODE}' '$(cat "$PARKED_DIR/balloon-state")'" >/dev/null 2>&1; then
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
  on dst "qm config $VMID" 2>/dev/null | grep -E "^(balloon|memory|cores|cpu|scsi0|efidisk0|tpmstate0|net0|hookscript|name):" || true
}

take_lock
finish_archive   # archivado pendiente de una fase terminal anterior (si se cortó justo después)
CMD="${1:-}"
case "$CMD" in shutdown|park|push|start|rollback|close|status) ;; *) awk 'NR>1 \&\& /^#/ {print; next} NR>1 {exit}' "$0"; exit 2 ;; esac
check_fleet_key
check_peer_identity

case "$CMD" in
  shutdown)
    T="${2:-600}"; [[ "$T" =~ ^[0-9]+$ ]] || die "timeout no numérico: $T"
    PH=$(state_get PHASE)
    if [[ "$PH" == stopping ]]; then
      require_state stopping      # un shutdown anterior que no terminó (Windows no se apagó, SSH…): se repite
    else
      no_active_transfer
    fi
    probe src
    [[ "$P_CONF" == present ]] || die "la VM ${VMID} no tiene config en el origen"
    if [[ "$PH" == stopping && "$P_STATUS" == stopped ]]; then
      [[ "$P_SHA" == "$(state_get SRC_CONF_SHA)" ]] || die "la config del origen cambió desde el shutdown anterior: haz rollback"
      log "la VM ya está parada (shutdown anterior); siguiente paso: park"; exit 0
    fi
    [[ "$P_STATUS" == running ]] || die "la VM ${VMID} no está 'running' en origen (estado ${P_STATUS})"
    SRC_CONF_SHA="$P_SHA"; SRC_FW="$P_FW"; SRC_FWSHA="$P_FWSHA"
    # Comprobaciones previas, ANTES de apagar, sobre copias VERIFICADAS (sha de la sonda) de la config
    # y del .fw del origen: inventario de discos, perfil (pull), CPU/licencias, precopia común, destino
    # libre y MAC libre en el destino.
    mkdir -p "$WORK"
    fetch src "$R_CONF" "$WORK/${VMID}.conf.live" "$SRC_CONF_SHA"
    rm -f "$WORK/${VMID}.fw.live"
    [[ "$SRC_FW" == present ]] && fetch src "$R_FW" "$WORK/${VMID}.fw.live" "$SRC_FWSHA"
    LIVE_CONF="$WORK/${VMID}.conf.live"
    DISKS=$(disks_of_file "$LIVE_CONF")
    UUID=$(conf_uuid "$LIVE_CONF"); [[ "$UUID" =~ ^[0-9A-Fa-f-]{36}$ ]] || die "la config no tiene smbios1 uuid válido: el kit no puede ligar el traslado a la VM"
    RUN_CPU=$(running_cpu)
    [[ -n "$RUN_CPU" ]] || die "no pude leer con qué CPU corre la VM en el origen (pid de QEMU y /proc/<pid>/cmdline): no se puede saber si cambia el Hardware ID; no apago"
    if [[ "$RTMIG_MODE" == pull ]]; then
      # pull: la config y el .fw del origen (no fiable) tienen que pasar el perfil, y antes que nada más.
      profile_check "$LIVE_CONF" "$UUID" "$( [[ -f $WORK/${VMID}.fw.live ]] && echo "$WORK/${VMID}.fw.live" )"
      mac_check "$WORK/${VMID}.conf.prof"
      rm -f "$WORK/${VMID}.conf.prof" "$WORK/${VMID}.fw.prof"
    fi
    cpu_decision "$LIVE_CONF" "$RUN_CPU"
    # Precopia común verificada con ESTE destino antes de apagar: sin ella el corte duraría la copia
    # entera (y un nodo que no es el de la precopia no puede apagar la VM).
    BASE=$(last_common) || die "no hay ningún @rtmig-* común (mismo guid en todos los discos) entre ${SRC_NODE} y ${DST_NODE}: haz antes la precopia (rtmig_send.sh base). No apago"
    log "precopia común con ${DST_NODE}: @${BASE}"
    probe dst
    [[ "$P_CONF" == absent ]] || die "el destino ${DST_NODE} ya tiene config de la VM ${VMID}: no apago"
    [[ "$P_FW" == absent ]] || die "el destino ${DST_NODE} tiene un .fw de la VM ${VMID}: no es de este traslado; revísalo antes de apagar"
    # Intención registrada ANTES de apagar: si algo cae después, rollback sabe qué arrancar y park
    # comprueba que la config del origen sigue siendo la que se validó aquí.
    rm -f "$STATE"
    state_set "VMID=${VMID}" "UUID=${UUID}" "MODE=${RTMIG_MODE}" "SRC_NODE=${SRC_NODE}" "DST_NODE=${DST_NODE}" "PEER=${PEER}" \
              "DISKS=${DISKS}" "POOLDS=${POOLDS}" "RUN_CPU=${RUN_CPU}" "SRC_CONF_SHA=${SRC_CONF_SHA}" \
              "SRC_FW_SHA=$( [[ "$SRC_FW" == present ]] && echo "$SRC_FWSHA" || echo none )" \
              "PHASE=stopping" "STOP_REQUESTED_AT=$(date -u +%FT%TZ)"
    rm -f "$WORK/${VMID}.conf.live" "$WORK/${VMID}.fw.live"
    log "previas OK: discos ${DISKS}; corre con ${RUN_CPU}, arrancará con ${CPU_OLD}${CPU_NEW:+ → ${CPU_NEW} en push}; destino libre; fase stopping"
    log "qm shutdown ${VMID} --timeout ${T} (ordenado; sin forceStop)"
    t0=$SECONDS
    if on src "qm shutdown ${VMID} --timeout ${T}"; then
      probe src
      log "apagada en $((SECONDS - t0)) s; estado: ${P_STATUS}"
    else
      probe src
      die "Windows no se apagó en ${T} s. NO forzar. La VM sigue en estado '${P_STATUS}' (fase stopping: repite shutdown o haz rollback). Escalar (README §Apagado)."
    fi
    [[ "$P_STATUS" == "stopped" ]] || die "tras el shutdown el estado es '${P_STATUS}'"
    ;;
  park)
    PH=$(state_get PHASE)
    migrate_old_layout
    # Reentrante: si un park anterior abrió la fase parked pero no llegó a quitar la config del origen
    # (p. ej. en pull se cortó el SSH en el rm), se retoma solo si el origen sigue teniendo EXACTAMENTE
    # la config (y el .fw) aparcados.
    if [[ "$PH" == parked && -f "$CONF_SRC" ]]; then
      require_state parked
      probe src
      [[ "$P_CONF" == present ]] || die "fase parked y el origen ya no tiene la config: el park está completo"
      [[ "$P_STATUS" == stopped ]] || die "la VM está '${P_STATUS}' en origen: no retomo el park"
      [[ "$P_SHA" == "$(sha256sum < "$CONF_SRC" | cut -d' ' -f1)" ]] || die "la config del origen ya no es la aparcada: revisar a mano"
      if [[ -f "$FW_SRC" ]]; then
        [[ "$P_FW" == absent || "$P_FWSHA" == "$(sha256sum < "$FW_SRC" | cut -d' ' -f1)" ]] || die "el .fw del origen ya no es el aparcado: revisar a mano"
        on_q src "rm -f \"${R_CONF}\" \"${R_FW}\"" >/dev/null
      else
        [[ "$P_FW" == absent ]] || die "el origen tiene un .fw que no estaba al aparcar: revisar a mano"
        on_q src "rm -f \"${R_CONF}\"" >/dev/null
      fi
      probe src
      [[ "$P_CONF" == absent && "$P_FW" == absent ]] || die "la config o el .fw siguen en el origen tras aparcarlos"
      log "park retomado: config retirada del origen; fase parked"
      exit 0
    fi
    [[ "$PH" == stopping || "$PH" == preparing ]] || die "park necesita un shutdown del kit antes (fase actual '${PH:-ninguna}')"
    require_state stopping preparing
    probe src
    [[ "$P_CONF" == present && "$P_STATUS" == stopped ]] || die "solo con la VM parada y su config en /etc/pve (config ${P_CONF}, estado ${P_STATUS})"
    [[ "$P_SHA" == "$(state_get SRC_CONF_SHA)" ]] || die "la config del origen cambió desde el shutdown: no la aparco (rollback, o repite shutdown)"
    if [[ "$(state_get SRC_FW_SHA)" == none ]]; then
      [[ "$P_FW" == absent ]] || die "el origen tiene ahora un .fw que no tenía al hacer shutdown: no aparco"
    else
      [[ "$P_FW" == present && "$P_FWSHA" == "$(state_get SRC_FW_SHA)" ]] || die "el .fw del origen cambió desde el shutdown: no aparco"
    fi
    probe dst
    [[ "$P_CONF" == absent ]] || die "el destino ${DST_NODE} ya tiene config de la VM ${VMID}"
    [[ "$P_FW" == absent ]] || die "el destino ${DST_NODE} tiene un .fw de la VM ${VMID} sin config: no es de este traslado; revísalo"
    if [[ -e "$PARKED_DIR" ]]; then
      # Solo vale si es la promoción de ESTE park (fase preparing) y su manifiesto cuadra con el estado.
      [[ "$PH" == preparing ]] && parked_manifest_ok \
        || die "queda una copia aparcada anterior ($PARKED_DIR): revísala y archívala a mano"
      log "la copia aparcada ya estaba promovida por el park anterior"
    else
      state_set "PHASE=preparing" "PREPARING_AT=$(date -u +%FT%TZ)"
      rm -rf "$STAGE_DIR"; mkdir -p "$STAGE_DIR"
      # Historial del reconciliador de globo: export-state necesita la config viva. Solo en push y si
      # el origen tiene reconciliador: lo que diga un origen no fiable (pull) no entra en el destino.
      if [[ "$RTMIG_MODE" == pull ]]; then
        log "modo pull: el historial del globo del origen no se importa (origen no fiable)"
      elif [[ "$(reconciler src)" == yes ]]; then
        if B=$(on_q src "python3 \"\${NV_RAM_GUARD:-/usr/local/sbin/neuravps-ram-guard.py}\" export-state ${VMID} '${SRC_NODE}'" 2>/dev/null) && [[ "$B" =~ ^[A-Za-z0-9+/=]+$ ]]; then
          printf '%s\n' "$B" > "$STAGE_DIR/balloon-state"
          log "historial del globo exportado"
        else
          warn "no pude exportar el historial del globo (el destino lo reaprenderá de forma conservadora)"
        fi
      else
        log "el origen no tiene reconciliador de globo: no hay historial que exportar"
      fi
      # Copias verificadas por sha256 al staging; solo cuando están completas (y pasan el perfil en
      # pull) el directorio entero se promueve con UN rename atómico a la copia aparcada.
      fetch src "$R_CONF" "$STAGE_DIR/conf" "$(state_get SRC_CONF_SHA)"
      [[ "$(state_get SRC_FW_SHA)" == none ]] || fetch src "$R_FW" "$STAGE_DIR/fw" "$(state_get SRC_FW_SHA)"
      [[ "$(conf_uuid "$STAGE_DIR/conf")" == "$(state_get UUID)" ]] || die "la config leída del origen no tiene el UUID del traslado: no sigo"
      if [[ "$RTMIG_MODE" == pull ]]; then
        profile_check "$STAGE_DIR/conf" "$(state_get UUID)" "$( [[ -f $STAGE_DIR/fw ]] && echo "$STAGE_DIR/fw" )"
        rm -f "$WORK/${VMID}.conf.prof" "$WORK/${VMID}.fw.prof"
      fi
      { echo "CONF_SHA=$(state_get SRC_CONF_SHA)"; echo "FW_SHA=$(state_get SRC_FW_SHA)"; } > "$STAGE_DIR/MANIFEST"
      mv -T "$STAGE_DIR" "$PARKED_DIR"
    fi
    # Fase parked ANTES de quitar la config del origen: si algo cae aquí, push verá la config en origen
    # y parará, y park se retoma.
    state_set "PHASE=parked" "PARKED_AT=$(date -u +%FT%TZ)"
    if [[ -f "$FW_SRC" ]]; then on_q src "rm -f \"${R_CONF}\" \"${R_FW}\"" >/dev/null
    else on_q src "rm -f \"${R_CONF}\"" >/dev/null; fi
    probe src
    [[ "$P_CONF" == absent && "$P_FW" == absent ]] || die "la config o el .fw siguen en el origen tras aparcarlos"
    log "config aparcada en $PARKED_DIR (fw: $( [[ -f $FW_SRC ]] && echo sí || echo no )); fase parked; zvols intactos:"
    on src "zfs list -H -o name,refer -r '${POOLDS}'" 2>/dev/null | grep "vm-${VMID}-" || true
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
    cpu_decision "$CONF_SRC" "$(state_get RUN_CPU)"
    if [[ -n "$CPU_NEW" ]] && pending_touches "$CONF_SRC" cpu; then
      die "la config tiene un cambio PENDIENTE de cpu: aplícalo o descártalo en origen antes (rollback) y repite"
    fi
    if [[ -n "$B" ]] && pending_touches "$CONF_SRC" balloon; then
      die "la config tiene un cambio PENDIENTE de balloon: no se puede fijar ${B} sin pisarlo; resuélvelo antes"
    fi
    DISKS=$(DISKS="" detect_disks)   # inventario COMPLETO de la config aparcada
    [[ "$DISKS" == "$(state_get DISKS)" ]] || die "el inventario de discos (${DISKS}) no es el de park ($(state_get DISKS))"
    for d in $DISKS; do
      ds=$(src_ds "$d"); dds=$(dst_ds "$d")
      gs=$(src_zget_guid "${ds}@${SNAP}") || exit 1
      gd=$(dst_zget guid "${dds}@${SNAP}") || exit 1
      [[ "$gs" != ABSENT && "$gs" == "$gd" ]] || die "$ds@$SNAP: guid origen=[$gs] destino=[$gd] — falta la sincronización final"
      ws=$(zget src "written@${SNAP}" "$ds" -p) || exit 1
      wd=$(dst_zget "written@${SNAP}" "$dds" -p) || exit 1
      [[ "$ws" == 0 && "$wd" == 0 ]] || die "$ds: escrito después de @$SNAP origen=[${ws}] destino=[${wd}] — la final no es la última"
      log "disco OK: $ds@$SNAP guid=$gs, 0 B posteriores en ambos lados"
    done
    # pull: lo recibido se comprueba entero (exactamente un volumen por disco, tamaños) y solo entonces
    # se promueve del contenedor a su sitio definitivo, antes de publicar ninguna config.
    promote_recv
    # Config final, preparada en local: CPU y balloon ya aplicados. Se publica de una sola vez.
    if [[ "$RTMIG_MODE" == pull ]]; then
      # Origen no fiable: lo que se publica en el destino es el PERFIL (lista blanca y recursos de la
      # asignación fiable), no su config.
      [[ -z "$B" || "$B" -le "$ALLOC_MEMORY_MB" ]] || die "balloon ${B} > memoria asignada ${ALLOC_MEMORY_MB}"
      profile_check "$CONF_SRC" "$(state_get UUID)" "$( [[ -f $FW_SRC ]] && echo "$FW_SRC" )"
      mac_check "$WORK/${VMID}.conf.prof"
      mv "$WORK/${VMID}.conf.prof" "$WORK/${VMID}.conf.push"
    else
      cp -a "$CONF_SRC" "$WORK/${VMID}.conf.push"
    fi
    [[ -n "$B" ]] && conf_set_key "$WORK/${VMID}.conf.push" balloon "$B"
    [[ -n "$CPU_NEW" ]] && conf_set_key "$WORK/${VMID}.conf.push" cpu "$CPU_NEW"
    PUSH_SHA=$(sha256sum < "$WORK/${VMID}.conf.push" | cut -d' ' -f1)
    FW_PUB=""
    if [[ -f "$FW_SRC" ]]; then
      FW_PUB="$FW_SRC"; [[ "$RTMIG_MODE" == pull ]] && FW_PUB="$WORK/${VMID}.fw.prof"
      FW_SHA=$(sha256sum < "$FW_PUB" | cut -d' ' -f1)
    else
      FW_SHA=none
    fi
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
      r=$(dst_publish "$FW_PUB" "$R_FW")
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
      on dst "qm start $VMID" || warn "qm start devolvió error (o se cortó el SSH): compruebo el estado"
      for _ in $(seq 1 20); do probe dst; [[ "$P_STATUS" == "running" ]] && break; sleep 3; done
      log "estado en destino: ${P_STATUS}"
      [[ "$P_STATUS" == "running" ]] || die "no llegó a running (fase starting; repite start cuando se aclare)"
    fi
    state_set "PHASE=started" "STARTED_AT=$(date -u +%FT%TZ)"
    log "siguiente paso, en una BASE: migrate_vm.sh ${VMID} <número de ${DST_NODE}> (rama «already on dest»)"
    ;;
  rollback)
    require_state stopping preparing parked publishing pushed starting started rollingback
    PH=$(state_get PHASE); SNAP=$(state_get SNAP)
    if [[ "$PH" == stopping || "$PH" == preparing ]]; then
      # Antes de aparcar: la config del origen no se ha tocado. Solo hay que comprobar que sigue siendo
      # la validada en shutdown y arrancar la VM si está parada. El destino no se toca.
      probe src
      [[ "$P_CONF" == present ]] || die "fase ${PH} y el origen no tiene la config: revisar a mano"
      [[ "$P_SHA" == "$(state_get SRC_CONF_SHA)" && "$P_UUID" == "$(state_get UUID)" ]] \
        || die "la config del origen no es la validada en shutdown: revisar a mano; no arranco"
      if [[ "$(state_get SRC_FW_SHA)" == none ]]; then
        [[ "$P_FW" == absent ]] || die "el origen tiene un .fw que no tenía en shutdown: no arranco"
      else
        [[ "$P_FW" == present && "$P_FWSHA" == "$(state_get SRC_FW_SHA)" ]] || die "el .fw del origen no es el validado en shutdown: no arranco"
      fi
      if [[ "$P_STATUS" != running ]]; then on src "qm start ${VMID}" || warn "qm start devolvió error: compruebo el estado"; probe src; fi
      [[ "$P_STATUS" == running ]] || die "la VM no llegó a running en origen (fase ${PH}; repite rollback)"
      state_set "PHASE=rolledback" "ROLLEDBACK_AT=$(date -u +%FT%TZ)" "ROLLBACK_FROM=${PH}" "ARCHIVE_PENDING=rolledback"
      finish_archive
      log "VM ${VMID} en marcha de nuevo en ORIGEN (rollback desde ${PH}: no se había aparcado nada)"
      exit 0
    fi
    if [[ "$PH" == parked ]]; then
      probe src
      if [[ "$P_CONF" == present ]]; then
        # park a medias: la config no llegó a salir del origen. Solo vale si es exactamente la aparcada.
        [[ "$P_SHA" == "$(sha256sum < "$CONF_SRC" | cut -d' ' -f1)" ]] \
          || die "ya hay config de la VM en origen y no es la aparcada: revisar a mano"
        log "park a medias (la config sigue en el origen): el rollback solo tiene que reconciliar y arrancar"
      fi
    fi
    if [[ "$PH" != rollingback ]]; then
      probe src
      [[ "$P_CONF" == absent || "$PH" == parked ]] || die "ya hay config de la VM en origen: nada que restaurar"
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
          w=$(dst_zget "written@${SNAP}" "$(dst_ds "$d")" -p) || exit 1
          [[ "$w" =~ ^[0-9]+$ ]] || die "disk-${d}: no hay un número de bytes escritos en destino tras @${SNAP} (${w}): estado INDETERMINADO, no restauro"
          [[ "$w" == 0 ]] || ACTIVITY="${ACTIVITY:+$ACTIVITY; }disk-${d} tiene ${w} B escritos en destino tras @${SNAP}"
        done
      fi
      if [[ -n "$ACTIVITY" && "${ACK_DISCARD_DEST_WRITES:-}" != "$VMID" ]]; then
        die "volver al origen PIERDE lo escrito en destino (${ACTIVITY}). Si es lo decidido: ACK_DISCARD_DEST_WRITES=${VMID}. No se ha tocado nada"
      fi
      if (( OURS_CONF || OURS_FW )); then
        rmlist=""; (( OURS_CONF )) && rmlist="\"${R_CONF}\""; (( OURS_FW )) && rmlist="${rmlist} \"${R_FW}\""
        on dst "mkdir -p '$WORK' && ts=\$(date -u +%Y%m%dT%H%M%SZ) && { [ ! -e \"${R_CONF}\" ] || cp -a \"${R_CONF}\" '$WORK/${VMID}.conf.dst-'\$ts; } && { [ ! -e \"${R_FW}\" ] || cp -a \"${R_FW}\" '$WORK/${VMID}.fw.dst-'\$ts; } && rm -f ${rmlist}" \
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
    CONF_SRC_SHA=$(sha256sum < "$CONF_SRC" | cut -d' ' -f1)
    if [[ -f "$FW_SRC" ]]; then FW_SRC_SHA=$(sha256sum < "$FW_SRC" | cut -d' ' -f1); else FW_SRC_SHA=none; fi
    probe src
    if [[ "$FW_SRC_SHA" == none ]]; then
      [[ "$P_FW" == absent ]] || die "el origen tiene un .fw de la VM pero la copia aparcada no tenía: revisar a mano antes de arrancar"
    elif [[ "$P_FW" == absent ]]; then
      r=$(publish src "$FW_SRC" "$R_FW")
      [[ "$r" == PUBLISHED || "$r" == NOANSWER ]] || die "no pude restaurar el .fw en origen (${r}; fase rollingback; repite rollback)"
    fi
    if [[ "$P_CONF" == absent ]]; then
      r=$(publish src "$CONF_SRC" "$R_CONF")
      [[ "$r" == PUBLISHED || "$r" == NOANSWER ]] || die "no pude restaurar la config en origen (${r}; fase rollingback; repite rollback)"
    fi
    probe src
    [[ "$P_CONF" == present && "$P_UUID" == "$(state_get UUID)" && "$P_SHA" == "$CONF_SRC_SHA" ]] \
      || die "la config del origen no es la aparcada (UUID ${P_UUID:-?}, sha ${P_SHA:0:12}): revisar a mano; no arranco"
    if [[ "$FW_SRC_SHA" == none ]]; then
      [[ "$P_FW" == absent ]] || die "el origen tiene un .fw que no estaba en la copia aparcada: no arranco"
    else
      [[ "$P_FW" == present && "$P_FWSHA" == "$FW_SRC_SHA" ]] || die "el .fw del origen no es el aparcado (sha ${P_FWSHA:0:12}): no arranco sin su firewall"
    fi
    if [[ "$P_STATUS" != running ]]; then on src "qm start ${VMID}" || warn "qm start devolvió error: compruebo el estado"; probe src; fi
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
    log "modo ${RTMIG_MODE}: el kit corre en ${LOCAL_NODE}; el otro nodo es ${PEER_NODE} (${PEER})"
    probe src; log "origen ${SRC_NODE}: config=${P_CONF} estado=${P_STATUS} aparcada=$( [[ -f $CONF_SRC ]] && echo sí || echo no )"
    probe dst; log "destino ${DST_NODE}: config=${P_CONF} estado=${P_STATUS} sha=${P_SHA:0:12}"
    ;;
esac

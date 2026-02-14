pve_zfs_migrate_vm() {
  local USAGE="Usage: pve_zfs_migrate_vm <vmid> <dest_ssh> [options]. Options: --dest-node, --snapname, --shutdown-timeout, --src-cleanup, --dest-cleanup, --delete-src-vm, --update-firestore, --keep-dest-stopped. Or pass positional args 3-10 for legacy. Use --help for details."
  [[ $# -ge 2 ]] || { echo "$USAGE" >&2; exit 1; }

  local VMID="$1"
  local DEST_SSH="$2"
  shift 2

  local DEST_NODE=""
  local SNAPNAME="migration"
  local SHUTDOWN_TIMEOUT="180"
  local SRC_CLEANUP="yes"
  local DEST_CLEANUP="yes"
  local DELETE_SRC_VM="true"
  local UPDATE_FIRESTORE="true"
  local KEEP_DEST_STOPPED="false"

  if [[ $# -gt 0 && "$1" == --* ]]; then
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --help|-h)
          echo "pve_zfs_migrate_vm <vmid> <dest_ssh> [options]"
          echo "  Required: vmid, dest_ssh"
          echo "  Options (defaults in parentheses):"
          echo "    --dest-node=<node>       Destination node name (auto-detect)"
          echo "    --snapname=<name>        Snapshot name (migration)"
          echo "    --shutdown-timeout=<sec> Shutdown wait seconds (180)"
          echo "    --src-cleanup=<yes|no>   Remove source snapshots after (yes)"
          echo "    --dest-cleanup=<yes|no>  Remove dest snapshots after (yes)"
          echo "    --delete-src-vm=<true|false>  Destroy source VM after migration (true)"
          echo "    --update-firestore=<true|false>  Update Firestore after migration (true)"
          echo "    --keep-dest-stopped=<true|false>  Do not start VM on destination (false)"
          echo "  Boolean options can be given as --opt or --opt=true/false."
          echo "  With set -e, keep session open and preserve exit code: r=0; curl ... | bash -s -- <vmid> <dest> ... || r=\$?"
          exit 0
          ;;
        --dest-node=*)  DEST_NODE="${1#*=}"; shift ;;
        --dest-node)    [[ $# -ge 2 ]] || { echo "Missing value for $1" >&2; exit 1; }; DEST_NODE="$2"; shift 2 ;;
        --snapname=*)   SNAPNAME="${1#*=}"; shift ;;
        --snapname)     [[ $# -ge 2 ]] || { echo "Missing value for $1" >&2; exit 1; }; SNAPNAME="$2"; shift 2 ;;
        --shutdown-timeout=*)  SHUTDOWN_TIMEOUT="${1#*=}"; shift ;;
        --shutdown-timeout)    [[ $# -ge 2 ]] || { echo "Missing value for $1" >&2; exit 1; }; SHUTDOWN_TIMEOUT="$2"; shift 2 ;;
        --src-cleanup=*)       SRC_CLEANUP="${1#*=}"; shift ;;
        --src-cleanup)         [[ $# -ge 2 ]] || { echo "Missing value for $1" >&2; exit 1; }; SRC_CLEANUP="$2"; shift 2 ;;
        --dest-cleanup=*)      DEST_CLEANUP="${1#*=}"; shift ;;
        --dest-cleanup)        [[ $# -ge 2 ]] || { echo "Missing value for $1" >&2; exit 1; }; DEST_CLEANUP="$2"; shift 2 ;;
        --delete-src-vm=*)     DELETE_SRC_VM="${1#*=}"; shift ;;
        --delete-src-vm)       if [[ "${2:-}" == "true" || "${2:-}" == "false" ]]; then DELETE_SRC_VM="${2}"; shift 2; else DELETE_SRC_VM="true"; shift; fi ;;
        --update-firestore=*)  UPDATE_FIRESTORE="${1#*=}"; shift ;;
        --update-firestore)    if [[ "${2:-}" == "true" || "${2:-}" == "false" ]]; then UPDATE_FIRESTORE="${2}"; shift 2; else UPDATE_FIRESTORE="true"; shift; fi ;;
        --keep-dest-stopped=*) KEEP_DEST_STOPPED="${1#*=}"; shift ;;
        --keep-dest-stopped)   if [[ "${2:-}" == "true" || "${2:-}" == "false" ]]; then KEEP_DEST_STOPPED="${2}"; shift 2; else KEEP_DEST_STOPPED="true"; shift; fi ;;
        *)
          echo "Unknown option: $1" >&2
          exit 1
          ;;
      esac
    done
  elif [[ $# -ge 1 ]]; then
    DEST_NODE="${1:-}"; [[ $# -ge 1 ]] && shift
    SNAPNAME="${1:-migration}"; [[ $# -ge 1 ]] && shift
    SHUTDOWN_TIMEOUT="${1:-180}"; [[ $# -ge 1 ]] && shift
    SRC_CLEANUP="${1:-yes}"; [[ $# -ge 1 ]] && shift
    DEST_CLEANUP="${1:-yes}"; [[ $# -ge 1 ]] && shift
    DELETE_SRC_VM="${1:-true}"; [[ $# -ge 1 ]] && shift
    UPDATE_FIRESTORE="${1:-true}"; [[ $# -ge 1 ]] && shift
    KEEP_DEST_STOPPED="${1:-false}"; [[ $# -ge 1 ]] && shift
  fi

  local SSH_OPTS=(-o LogLevel=ERROR -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ForwardAgent=yes)

  local MBUFFER_BLOCK="128k"
  local MBUFFER_MEM="1G"

  local MIGRATION_SUCCESS=0
  local PHASE=0

  _die()  { echo "❌ $*" >&2; exit 1; }
  _info() { echo "ℹ️  $*" >&2; }
  _ok()   { echo "✅ $*" >&2; }

  _cleanup_on_failure() {
    set +e
    _info "Migration failed or interrupted; cleaning up and restoring state..."
    if [[ "$UPDATE_FIRESTORE" == "true" ]]; then
      _info "Setting server maintenance=false in Firestore..."
      local HELPER_CLEANUP
      HELPER_CLEANUP="$(mktemp -t update_firestore_migration_XXXXXX.py)"
      if command -v python3 >/dev/null 2>&1 && curl -sSL "https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/snippets/update_firestore_migration.py" -o "$HELPER_CLEANUP" 2>/dev/null; then
        python3 "$HELPER_CLEANUP" --set-maintenance false "$VMID" 2>/dev/null || true
      fi
      rm -f "$HELPER_CLEANUP"
    fi
    if [[ "$PHASE" -ge 1 ]]; then
      _info "Destroying source snapshots @${SNAPNAME}..."
      for ds in "${DATASETS[@]}"; do
        zfs destroy "${ds}@${SNAPNAME}" 2>/dev/null || true
      done
      _info "Starting VM ${VMID} on source again..."
      qm start "$VMID" 2>/dev/null || true
    fi
    if [[ "$PHASE" -ge 2 ]]; then
      _info "Cleaning up destination (snapshots, datasets, config)..."
      if [[ "$PHASE" -ge 4 ]]; then
        ssh "${SSH_OPTS[@]}" "$DEST_SSH" "qm stop '${VMID}'" 2>/dev/null || true
      fi
      for ds in "${DATASETS[@]}"; do
        ssh "${SSH_OPTS[@]}" "$DEST_SSH" "zfs destroy '${ds}@${SNAPNAME}'" 2>/dev/null || true
        ssh "${SSH_OPTS[@]}" "$DEST_SSH" "zfs destroy -R '${ds}'" 2>/dev/null || true
      done
      ssh "${SSH_OPTS[@]}" "$DEST_SSH" "rm -f '${CONF_DEST}' '${CONF_DEST}.tmp' '/etc/pve/firewall/${VMID}.fw'" 2>/dev/null || true
    fi
  }

  _exit_handler() {
    local code=$?
    if [[ "${MIGRATION_SUCCESS:-0}" -ne 1 ]]; then
      _cleanup_on_failure
    fi
    exit "$code"
  }
  trap _exit_handler EXIT

  set -E -e -o pipefail

  command -v qm >/dev/null || _die "qm not found; run on a Proxmox node."
  command -v zfs >/dev/null || _die "zfs not found."

  # Tools on source (pv needed for %)
  if ! command -v mbuffer >/dev/null 2>&1 || ! command -v pv >/dev/null 2>&1; then
    _info "Installing required tools on source host (mbuffer + pv)..."
    apt-get update -qq
    apt-get -y install mbuffer pv
  fi

  local CONF_LOCAL="/etc/pve/qemu-server/${VMID}.conf"
  [[ -f "$CONF_LOCAL" ]] || _die "VM config not found: $CONF_LOCAL"

  if [[ -z "$DEST_NODE" ]]; then
    DEST_NODE="$(ssh "${SSH_OPTS[@]}" "$DEST_SSH" 'hostname' | tr -d "\r")" \
      || _die "Cannot detect destination hostname via SSH."
    _info "Detected destination node name: $DEST_NODE"
  fi
  local CONF_DEST="/etc/pve/nodes/${DEST_NODE}/qemu-server/${VMID}.conf"

  # Tool on destination (only mbuffer)
  ssh "${SSH_OPTS[@]}" "$DEST_SSH" '
    if ! command -v mbuffer >/dev/null 2>&1; then
      apt-get update -qq
      apt-get -y install mbuffer
    fi
  '

  _info "Collecting ZFS volumes referenced by VM ${VMID}..."
  mapfile -t DISK_TOKENS < <(
    awk -F': ' '
      /^[a-z0-9]+[0-9]*: / {print $1" "$2}
    ' "$CONF_LOCAL" \
    | awk '$1 ~ /^(scsi|virtio|sata|ide)[0-9]+$|^efidisk0$|^tpmstate0$/ {print $2}' \
    | sed 's/,.*$//' \
    | sort -u
  )
  [[ "${#DISK_TOKENS[@]}" -gt 0 ]] || _die "No disks found in config."

  local -a DATASETS=()
  local tok base ds
  for tok in "${DISK_TOKENS[@]}"; do
    base="${tok#*:}"
    if [[ "$base" == */* ]]; then
      ds="$base"
    else
      ds="$(zfs list -H -o name | awk -v leaf="$base" -F/ '$NF==leaf{print $0; exit}')"
      [[ -n "$ds" ]] || _die "Could not map disk token '$tok' -> dataset."
    fi
    DATASETS+=("$ds")
  done

  local -a UNIQUE=()
  declare -A seen=()
  for ds in "${DATASETS[@]}"; do
    if [[ -z "${seen[$ds]:-}" ]]; then UNIQUE+=("$ds"); seen["$ds"]=1; fi
  done
  DATASETS=("${UNIQUE[@]}")

  _info "Datasets to migrate:"
  for ds in "${DATASETS[@]}"; do _info "  - $ds"; done

  if [[ "$UPDATE_FIRESTORE" == "true" ]]; then
    _info "Setting server maintenance=true in Firestore before migration..."
    local HELPER_MAINT
    HELPER_MAINT="$(mktemp -t update_firestore_migration_XXXXXX.py)"
    if command -v python3 >/dev/null 2>&1 && curl -sSL "https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/snippets/update_firestore_migration.py" -o "$HELPER_MAINT" 2>/dev/null; then
      if python3 "$HELPER_MAINT" --set-maintenance true "$VMID" 2>/dev/null; then
        _ok "Server maintenance=true set."
      else
        _info "Could not set maintenance (no creds or doc); continuing."
      fi
      rm -f "$HELPER_MAINT"
    else
      _info "Could not download Firestore helper or python3 not found; skipping maintenance flag."
    fi
  fi

  _info "Shutting down VM ${VMID}..."
  qm shutdown "$VMID" || true

  local elapsed=0
  while qm status "$VMID" 2>/dev/null | grep -q "status: running"; do
    if (( elapsed >= SHUTDOWN_TIMEOUT )); then
      _info "Shutdown timeout reached; forcing stop..."
      qm stop "$VMID" || true
      break
    fi
    sleep 2; (( elapsed += 2 ))
  done
  _ok "VM ${VMID} stopped."

  _info "Ensuring snapshot name @${SNAPNAME} is free (source + destination)..."
  for ds in "${DATASETS[@]}"; do
    if zfs list -H -t snapshot -o name "${ds}@${SNAPNAME}" >/dev/null 2>&1; then
      _info "  Source snapshot exists, destroying: ${ds}@${SNAPNAME}"
      zfs destroy "${ds}@${SNAPNAME}"
    fi
    ssh "${SSH_OPTS[@]}" "$DEST_SSH" \
      "zfs list -H -t snapshot -o name '${ds}@${SNAPNAME}' >/dev/null 2>&1 && zfs destroy '${ds}@${SNAPNAME}' || true"
  done

  _info "Creating source snapshots @${SNAPNAME}..."
  for ds in "${DATASETS[@]}"; do
    zfs snapshot "${ds}@${SNAPNAME}"
  done
  _ok "Source snapshots created."
  PHASE=1

  _info "Sending datasets to ${DEST_SSH} (pv % progress)..."
  local i=0 total="${#DATASETS[@]}"
  for ds in "${DATASETS[@]}"; do
    ((i+=1))
    _info "[$i/$total] Estimating stream size for ${ds}@${SNAPNAME}..."
    local SIZE
    SIZE="$(zfs send -nP -R "${ds}@${SNAPNAME}" | awk '$1=="size"{print $2; exit}')"
    [[ -n "${SIZE:-}" ]] || _die "Could not estimate stream size for ${ds}@${SNAPNAME}"

    _info "[$i/$total] Transferring ${ds}@${SNAPNAME} (estimated ${SIZE} bytes)..."

    zfs send -R "${ds}@${SNAPNAME}" \
      | pv -s "$SIZE" -ptebar \
      | mbuffer -q -s "${MBUFFER_BLOCK}" -m "${MBUFFER_MEM}" \
      | ssh "${SSH_OPTS[@]}" "$DEST_SSH" \
          "mbuffer -q -s '${MBUFFER_BLOCK}' -m '${MBUFFER_MEM}' | zfs receive -F '${ds}'"

    _ok "[$i/$total] ${ds}@${SNAPNAME} transferred (100%)"
    PHASE=2
  done
  _ok "All datasets transferred."

  _info "Verifying received datasets and snapshots on destination..."
  for ds in "${DATASETS[@]}"; do
    if ! ssh "${SSH_OPTS[@]}" "$DEST_SSH" "zfs list -H -o name '${ds}@${SNAPNAME}' >/dev/null 2>&1"; then
      _die "Destination missing dataset or snapshot: ${ds}@${SNAPNAME}; aborting before copying config."
    fi
  done
  _ok "All required datasets and snapshots present on destination."

  _info "Copying VM config -> ${CONF_DEST}"
  ssh "${SSH_OPTS[@]}" "$DEST_SSH" "mkdir -p '$(dirname "$CONF_DEST")'"
  ssh "${SSH_OPTS[@]}" "$DEST_SSH" \
    "cat > '${CONF_DEST}.tmp' && mv -f '${CONF_DEST}.tmp' '${CONF_DEST}'" \
    < "$CONF_LOCAL"
  _ok "VM config copied."

  local FW_LOCAL="/etc/pve/firewall/${VMID}.fw"
  if [[ -f "$FW_LOCAL" ]]; then
    _info "Copying firewall config -> /etc/pve/firewall/${VMID}.fw"
    ssh "${SSH_OPTS[@]}" "$DEST_SSH" "mkdir -p /etc/pve/firewall"
    ssh "${SSH_OPTS[@]}" "$DEST_SSH" \
      "cat > '/etc/pve/firewall/${VMID}.fw.tmp' && mv -f '/etc/pve/firewall/${VMID}.fw.tmp' '/etc/pve/firewall/${VMID}.fw'" \
      < "$FW_LOCAL"
    _ok "Firewall config copied."
  else
    _info "No firewall config found at ${FW_LOCAL}; skipping."
  fi
  PHASE=3

  if [[ "$KEEP_DEST_STOPPED" == "true" ]]; then
    _info "Keeping destination VM stopped (keep_dest_stopped=true)."
    PHASE=4
  else
    _info "Starting VM ${VMID} on destination..."
    ssh "${SSH_OPTS[@]}" "$DEST_SSH" "qm start '${VMID}'"
    _ok "VM ${VMID} started on destination."
    PHASE=4
  fi

  if [[ "$UPDATE_FIRESTORE" == "true" ]]; then
    _info "Updating Firestore (connectionUrl, nodeId)..."
    local DEST_PUBLIC_IP
    DEST_PUBLIC_IP="$(ssh "${SSH_OPTS[@]}" "$DEST_SSH" 'curl -4 -s --connect-timeout 5 ifconfig.me 2>/dev/null || ip route get 8.8.8.8 2>/dev/null | awk "{print \$7; exit}"' | tr -d '\r')"
    if [[ -z "$DEST_PUBLIC_IP" ]]; then
      _die "Could not get destination public IPv4; cannot update Firestore."
    fi
    local BASE_PORT=20000
    local CONNECTION_URL="${DEST_PUBLIC_IP}:$((BASE_PORT + VMID))"
    local HELPER
    HELPER="$(mktemp -t update_firestore_migration_XXXXXX.py)"
    if ! command -v python3 >/dev/null 2>&1; then
      _die "python3 not found; cannot update Firestore."
    fi
    if ! curl -sSL "https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/snippets/update_firestore_migration.py" -o "$HELPER"; then
      _die "Could not download Firestore helper."
    fi
    if ! python3 "$HELPER" "$VMID" "$CONNECTION_URL" "$DEST_NODE"; then
      rm -f "$HELPER"
      _die "Firestore update failed."
    fi
    rm -f "$HELPER"
    _ok "Firestore updated: connectionUrl=${CONNECTION_URL}, nodeId=${DEST_NODE}"
  fi

  if [[ "$DEST_CLEANUP" == "yes" ]]; then
    _info "Destroying destination snapshots @${SNAPNAME}..."
    for ds in "${DATASETS[@]}"; do
      ssh "${SSH_OPTS[@]}" "$DEST_SSH" "zfs destroy '${ds}@${SNAPNAME}'" || true
    done
    _ok "Destination snapshots removed."
  else
    _info "Keeping destination snapshots."
  fi

  if [[ "$SRC_CLEANUP" == "yes" ]]; then
    _info "Destroying source snapshots @${SNAPNAME}..."
    for ds in "${DATASETS[@]}"; do
      zfs destroy "${ds}@${SNAPNAME}" || true
    done
    _ok "Source snapshots removed."
  else
    _info "Keeping source snapshots."
  fi

  if [[ "$DELETE_SRC_VM" == "true" && "$KEEP_DEST_STOPPED" != "true" ]]; then
    _info "Verifying VM ${VMID} is running on destination (first check)..."
    sleep 5
    local DEST_STATUS
    DEST_STATUS="$(ssh "${SSH_OPTS[@]}" "$DEST_SSH" "qm status '${VMID}' 2>/dev/null" | awk -F': ' '/status:/{print $2; exit}' | tr -d '\r' || true)"
    if [[ "$DEST_STATUS" != "running" ]]; then
      _die "VM ${VMID} is not running on destination (status: ${DEST_STATUS:-unknown}); refusing to destroy source."
    fi
    _ok "VM ${VMID} is running on destination (first check)."
    _info "Verifying VM ${VMID} is still running (second check after 8s)..."
    sleep 8
    DEST_STATUS="$(ssh "${SSH_OPTS[@]}" "$DEST_SSH" "qm status '${VMID}' 2>/dev/null" | awk -F': ' '/status:/{print $2; exit}' | tr -d '\r' || true)"
    if [[ "$DEST_STATUS" != "running" ]]; then
      _die "VM ${VMID} is not running on destination after second check (status: ${DEST_STATUS:-unknown}); refusing to destroy source."
    fi
    _info "Verifying disks exist on destination..."
    local DEST_CONFIG
    DEST_CONFIG="$(ssh "${SSH_OPTS[@]}" "$DEST_SSH" "qm config '${VMID}' 2>/dev/null" || true)"
    if [[ -z "$DEST_CONFIG" ]]; then
      _die "Could not read VM config on destination; refusing to destroy source."
    fi
    if ! echo "$DEST_CONFIG" | grep -qE '^(scsi|virtio|sata|ide)[0-9]+:'; then
      _die "VM config on destination has no disk entries; refusing to destroy source."
    fi
    _ok "VM ${VMID} is running on destination with disks attached."
    _info "Destroying VM ${VMID} on source (and associated disks)..."
    qm destroy "$VMID" --purge --destroy-unreferenced-disks 2>/dev/null || qm destroy "$VMID" --purge 2>/dev/null || true
    for ds in "${DATASETS[@]}"; do
      if zfs list -H -o name "$ds" >/dev/null 2>&1; then
        _info "Destroying source dataset: $ds"
        zfs destroy -R "${ds}@${SNAPNAME}" 2>/dev/null || true
        zfs destroy -R "$ds" 2>/dev/null || true
      fi
    done
    _ok "Source VM ${VMID} and associated disks destroyed."
  elif [[ "$DELETE_SRC_VM" == "true" && "$KEEP_DEST_STOPPED" == "true" ]]; then
    _info "Skipping source VM destruction (keep_dest_stopped=true; start and verify destination VM first)."
  fi

  MIGRATION_SUCCESS=1
  _ok "Migration completed: VM ${VMID} -> ${DEST_SSH} (node ${DEST_NODE})"
  trap - EXIT
}

# Examples:
#   Minimal (only required args):
#     bash migrate_vm.sh 620 fd00:4000::6
#   With named options:
#     bash migrate_vm.sh 620 fd00:4000::6 --keep-dest-stopped
#     bash migrate_vm.sh 620 fd00:4000::6 --keep-dest-stopped --delete-src-vm=false
#   Legacy positional (args 3-10: dest_node snapname shutdown_timeout src_cleanup dest_cleanup delete_src_vm update_firestore keep_dest_stopped):
#     bash migrate_vm.sh 619 fd00:4000::2d '' migration 180 yes yes false true true
#   One-liner (curl):
#     curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/migrate_vm.sh | bash -s -- 620 fd00:4000::6 --keep-dest-stopped
#   Keep session open on failure but preserve exit code (e.g. with set -e):
#     r=0; curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/migrate_vm.sh | bash -s -- 620 fd00:4000::6 || r=$? ; echo "Exit code: $r"
#   Multiple migrations in sequence: parent shell never closes; next runs only if previous succeeded.
#     Use { } not ( ) so r is updated in the same shell. Use || true so a failed [[ ]] does not exit the shell with set -e.
#     r=0; curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/migrate_vm.sh | bash -s -- 620 fd00:4000::6 || r=$? ; echo "Exit code: $r"
#     [[ $r -eq 0 ]] && { curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/migrate_vm.sh | bash -s -- 621 fd00:4000::6 || r=$? ; echo "Exit code: $r"; } || true
#     [[ $r -eq 0 ]] && { curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/migrate_vm.sh | bash -s -- 622 fd00:4000::6 || r=$? ; echo "Exit code: $r"; } || true
#
pve_zfs_migrate_vm "$@"

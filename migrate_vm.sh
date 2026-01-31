pve_zfs_migrate_vm() {
  #set -euo pipefail

  local VMID="${1:?Usage: pve_zfs_migrate_vm <vmid> <dest_ssh> [dest_node] [snapname] [shutdown_timeout] [src_cleanup] [dest_cleanup] [delete_src_vm] [update_firestore]}"
  local DEST_SSH="${2:?Usage: pve_zfs_migrate_vm <vmid> <dest_ssh> [dest_node] [snapname] [shutdown_timeout] [src_cleanup] [dest_cleanup] [delete_src_vm] [update_firestore]}"

  local DEST_NODE="${3:-}"
  local SNAPNAME="${4:-migration}"
  local SHUTDOWN_TIMEOUT="${5:-180}"
  local SRC_CLEANUP="${6:-yes}"
  local DEST_CLEANUP="${7:-yes}"
  local DELETE_SRC_VM="${8:-false}"
  local UPDATE_FIRESTORE="${9:-true}"

  local SSH_OPTS=(-o LogLevel=ERROR -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ForwardAgent=yes)

  local MBUFFER_BLOCK="128k"
  local MBUFFER_MEM="1G"

  _die()  { echo "❌ $*" >&2; return 1; }
  _info() { echo "ℹ️  $*" >&2; }
  _ok()   { echo "✅ $*" >&2; }

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
  done
  _ok "All datasets transferred."

  _info "Copying VM config -> ${CONF_DEST}"
  ssh "${SSH_OPTS[@]}" "$DEST_SSH" "mkdir -p '$(dirname "$CONF_DEST")'"
  ssh "${SSH_OPTS[@]}" "$DEST_SSH" \
    "cat > '${CONF_DEST}.tmp' && mv -f '${CONF_DEST}.tmp' '${CONF_DEST}'" \
    < "$CONF_LOCAL"
  _ok "VM config copied."

  if [[ "$UPDATE_FIRESTORE" == "true" ]]; then
    _info "Updating Firestore (connectionUrl, nodeId) before starting VM on destination..."
    local DEST_PUBLIC_IP
    DEST_PUBLIC_IP="$(ssh "${SSH_OPTS[@]}" "$DEST_SSH" 'curl -4 -s --connect-timeout 5 ifconfig.me 2>/dev/null || ip route get 8.8.8.8 2>/dev/null | awk "{print \$7; exit}"' | tr -d '\r')"
    if [[ -n "$DEST_PUBLIC_IP" ]]; then
      local BASE_PORT=20000
      local CONNECTION_URL="${DEST_PUBLIC_IP}:$((BASE_PORT + VMID))"
      local HELPER
      HELPER="$(mktemp -t update_firestore_migration_XXXXXX.py)"
      if command -v python3 >/dev/null 2>&1 && curl -sSL "https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/snippets/update_firestore_migration.py" -o "$HELPER" 2>/dev/null; then
        if python3 "$HELPER" "$VMID" "$CONNECTION_URL" "$DEST_NODE"; then
          _ok "Firestore updated: connectionUrl=${CONNECTION_URL}, nodeId=${DEST_NODE}"
        else
          _info "Firestore update skipped or failed (no creds, no doc, or error); continuing."
        fi
      else
        _info "Could not download Firestore helper or python3 not found; skipping Firestore update."
      fi
      rm -f "$HELPER"
    else
      _info "Could not get destination public IPv4; skipping Firestore update."
    fi
  fi

  _info "Starting VM ${VMID} on destination..."
  ssh "${SSH_OPTS[@]}" "$DEST_SSH" "qm start '${VMID}'"
  _ok "VM ${VMID} started on destination."

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

  if [[ "$DELETE_SRC_VM" == "true" ]]; then
    _info "Verifying VM ${VMID} is running on destination..."
    sleep 5
    local DEST_STATUS
    DEST_STATUS="$(ssh "${SSH_OPTS[@]}" "$DEST_SSH" "qm status '${VMID}' 2>/dev/null" | awk -F': ' '/status:/{print $2; exit}' | tr -d '\r' || true)"
    if [[ "$DEST_STATUS" != "running" ]]; then
      _die "VM ${VMID} is not running on destination (status: ${DEST_STATUS:-unknown}); refusing to destroy source."
    fi
    _ok "VM ${VMID} is running on destination."
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
  fi

  _ok "Migration completed: VM ${VMID} -> ${DEST_SSH} (node ${DEST_NODE})"
}

# Examples:
#   Run with local script (pass args):
#     bash migrate_vm.sh 619 fd00:4000::2d
#
#   One-liner: download from GitHub and run (no manual download):
#     curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/migrate_vm.sh | bash -s -- 619 fd00:4000::2d
#
pve_zfs_migrate_vm "$@"

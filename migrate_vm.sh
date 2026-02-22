#!/usr/bin/env bash
# Migrate a Proxmox VM to another node using native remote_migrate API.
# Uses: apt upgrade, Firestore prep, hookscript detach, token creation, pvesh remote_migrate, sync-dnat, hookscript reattach, token cleanup.

pve_zfs_migrate_vm() {
  local USAGE="Usage: pve_zfs_migrate_vm <vmid> <dest_ssh> [options]. Options: --dest-node, --update-firestore, --target-storage. Use --help for details."
  [[ $# -ge 2 ]] || { echo "$USAGE" >&2; exit 1; }

  local VMID="$1"
  local DEST_SSH="$2"
  shift 2

  local DEST_NODE=""
  local UPDATE_FIRESTORE="true"
  local TARGET_STORAGE="local-zfs"

  if [[ $# -gt 0 && "$1" == --* ]]; then
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --help|-h)
          echo "pve_zfs_migrate_vm <vmid> <dest_ssh> [options]"
          echo "  Required: vmid, dest_ssh (e.g. root@10.64.0.7 or root@[fd00:4000::7])"
          echo "  Options (defaults in parentheses):"
          echo "    --dest-node=<node>       Destination node name (auto-detect)"
          echo "    --update-firestore=<true|false>  Update Firestore (true)"
          echo "    --target-storage=<id>    Target storage for migration (local-zfs)"
          echo "  Uses Proxmox native remote_migrate with --online --delete."
          exit 0
          ;;
        --dest-node=*)  DEST_NODE="${1#*=}"; shift ;;
        --dest-node)    [[ $# -ge 2 ]] || { echo "Missing value for $1" >&2; exit 1; }; DEST_NODE="$2"; shift 2 ;;
        --update-firestore=*)  UPDATE_FIRESTORE="${1#*=}"; shift ;;
        --update-firestore)    if [[ "${2:-}" == "true" || "${2:-}" == "false" ]]; then UPDATE_FIRESTORE="${2}"; shift 2; else UPDATE_FIRESTORE="true"; shift; fi ;;
        --target-storage=*)    TARGET_STORAGE="${1#*=}"; shift ;;
        --target-storage)      [[ $# -ge 2 ]] || { echo "Missing value for $1" >&2; exit 1; }; TARGET_STORAGE="$2"; shift 2 ;;
        *)
          echo "Unknown option: $1" >&2
          exit 1
          ;;
      esac
    done
  fi

  local SSH_OPTS=(-o LogLevel=ERROR -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ForwardAgent=yes)
  local MIGRATION_SUCCESS=0
  local TOKEN_CREATED=0
  local SOURCE_NODE=""
  local SOURCE_CONNECTION_URL=""
  local DEST_CONNECTION_URL=""

  _die()  { echo "❌ $*" >&2; exit 1; }
  _info() { echo "ℹ️  $*" >&2; }
  _ok()   { echo "✅ $*" >&2; }

  # Extract host from DEST_SSH (root@10.64.0.7 -> 10.64.0.7; root@fd00:4000::7 -> fd00:4000::7)
  _extract_dest_host() {
    local ssh_spec="$1"
    local host
    if [[ "$ssh_spec" == *"@"* ]]; then
      host="${ssh_spec#*@}"
    else
      host="$ssh_spec"
    fi
    # Strip brackets if present (root@[fd00:4000::7] -> fd00:4000::7 for parsing)
    host="${host#\[}"
    host="${host%\]}"
    echo "$host"
  }

  # Host for openssl connect: IPv6 needs [addr]:8006
  _openssl_connect_host() {
    local h="$1"
    if [[ "$h" == *:* ]]; then
      echo "[${h}]:8006"
    else
      echo "${h}:8006"
    fi
  }

  # Host for target-endpoint: use as-is for IPv4; use [addr] for IPv6
  _target_endpoint_host() {
    local h="$1"
    if [[ "$h" == *:* ]]; then
      echo "[${h}]"
    else
      echo "$h"
    fi
  }

  _cleanup_on_failure() {
    set +e
    _info "Migration failed; cleaning up and restoring state..."
    if [[ "$UPDATE_FIRESTORE" == "true" ]] && [[ -n "${SOURCE_CONNECTION_URL:-}" ]] && [[ -n "${SOURCE_NODE:-}" ]]; then
      _info "Reverting Firestore to source (connectionUrl, nodeId)..."
      local HELPER
      HELPER="$(mktemp -t update_firestore_migration_XXXXXX.py)"
      if command -v python3 >/dev/null 2>&1 && curl -sSL "https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/snippets/update_firestore_migration.py" -o "$HELPER" 2>/dev/null; then
        python3 "$HELPER" "$VMID" "$SOURCE_CONNECTION_URL" "$SOURCE_NODE" 2>/dev/null || true
      fi
      rm -f "$HELPER"
    fi
    if [[ "$UPDATE_FIRESTORE" == "true" ]]; then
      _info "Setting server maintenance=false in Firestore..."
      local HELPER_MAINT
      HELPER_MAINT="$(mktemp -t update_firestore_migration_XXXXXX.py)"
      if command -v python3 >/dev/null 2>&1 && curl -sSL "https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/snippets/update_firestore_migration.py" -o "$HELPER_MAINT" 2>/dev/null; then
        python3 "$HELPER_MAINT" --set-maintenance false "$VMID" 2>/dev/null || true
      fi
      rm -f "$HELPER_MAINT"
    fi
    _info "Reattaching hookscript on source..."
    qm set "$VMID" --hookscript shared:snippets/sync-dnat.py 2>/dev/null || true
    if [[ "$TOKEN_CREATED" -eq 1 ]]; then
      _info "Removing migration token on destination..."
      ssh "${SSH_OPTS[@]}" "$DEST_SSH" "pveum user token remove root@pam migrate-full 2>/dev/null" || true
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
  command -v pvesh >/dev/null || _die "pvesh not found."

  local CONF_LOCAL="/etc/pve/qemu-server/${VMID}.conf"
  [[ -f "$CONF_LOCAL" ]] || _die "VM config not found: $CONF_LOCAL"

  local DEST_HOST
  DEST_HOST="$(_extract_dest_host "$DEST_SSH")"
  [[ -n "$DEST_HOST" ]] || _die "Could not extract destination host from: $DEST_SSH"

  if [[ -z "$DEST_NODE" ]]; then
    DEST_NODE="$(ssh "${SSH_OPTS[@]}" "$DEST_SSH" 'hostname' | tr -d "\r")" \
      || _die "Cannot detect destination hostname via SSH."
    _info "Detected destination node name: $DEST_NODE"
  fi

  SOURCE_NODE="$(hostname 2>/dev/null)" || SOURCE_NODE=""
  local BASE_PORT=20000
  local SOURCE_PUBLIC_IP
  SOURCE_PUBLIC_IP="$(curl -4 -s --connect-timeout 5 ifconfig.me 2>/dev/null || ip route get 8.8.8.8 2>/dev/null | awk '{print $7; exit}')" || true
  SOURCE_PUBLIC_IP="${SOURCE_PUBLIC_IP%%[[:space:]]*}"
  SOURCE_PUBLIC_IP="${SOURCE_PUBLIC_IP//$'\r'/}"
  if [[ -n "${SOURCE_PUBLIC_IP:-}" ]]; then
    SOURCE_CONNECTION_URL="${SOURCE_PUBLIC_IP}:$((BASE_PORT + VMID))"
  fi

  # 1) apt-get update && apt-get -y upgrade on source and destination
  _info "Running apt-get update && apt-get -y upgrade on source..."
  apt-get update -qq && apt-get -y upgrade -qq || _info "apt upgrade on source had issues; continuing."
  _info "Running apt-get update && apt-get -y upgrade on destination..."
  ssh "${SSH_OPTS[@]}" "$DEST_SSH" "apt-get update -qq && apt-get -y upgrade -qq" || _info "apt upgrade on dest had issues; continuing."

  # 2) Firestore: maintenance=true, nodeId=DEST_NODE
  if [[ "$UPDATE_FIRESTORE" == "true" ]]; then
    _info "Setting Firestore: maintenance=true, nodeId=${DEST_NODE}..."
    local HELPER_PREP
    HELPER_PREP="$(mktemp -t update_firestore_migration_XXXXXX.py)"
    if command -v python3 >/dev/null 2>&1 && curl -sSL "https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/snippets/update_firestore_migration.py" -o "$HELPER_PREP" 2>/dev/null; then
      if python3 "$HELPER_PREP" --set-migration-prep "$DEST_NODE" "$VMID" 2>/dev/null; then
        _ok "Firestore prep set."
      else
        _info "Could not set Firestore prep (no creds or doc); continuing."
      fi
      rm -f "$HELPER_PREP"
    else
      _info "Could not download Firestore helper; skipping."
    fi
  fi

  # 3) Detach hookscript on source
  _info "Detaching hookscript on source..."
  qm set "$VMID" --delete hookscript 2>/dev/null || true
  _ok "Hookscript detached."

  # 4) Remove any existing token on destination (e.g. from a previous failed run), then create it
  _info "Ensuring clean migration token on destination..."
  ssh "${SSH_OPTS[@]}" "$DEST_SSH" "pveum user token remove root@pam migrate-full 2>/dev/null" || true
  _info "Creating migration token on destination..."
  local TOKEN_OUTPUT
  TOKEN_OUTPUT="$(ssh "${SSH_OPTS[@]}" "$DEST_SSH" "pveum user token add root@pam migrate-full --privsep=0 2>&1")" || \
    _die "Failed to create token on destination: $TOKEN_OUTPUT"
  local TOKEN_SECRET
  # pveum outputs the secret as UUID; may appear in table or as part of PVEAPIToken=...=UUID
  TOKEN_SECRET="$(echo "$TOKEN_OUTPUT" | grep -oE '[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}' | head -1)"
  [[ -n "$TOKEN_SECRET" ]] || _die "Could not parse token secret from pveum output."
  TOKEN_CREATED=1
  _ok "Token created."

  # Get fingerprint
  local CONNECT_SPEC
  CONNECT_SPEC="$(_openssl_connect_host "$DEST_HOST")"
  local FINGERPRINT
  FINGERPRINT="$(openssl s_client -connect "$CONNECT_SPEC" -servername "$DEST_HOST" </dev/null 2>/dev/null | openssl x509 -noout -fingerprint -sha256 2>/dev/null | cut -d'=' -f2)"
  [[ -n "$FINGERPRINT" ]] || _die "Could not get SSL fingerprint for $DEST_HOST:8006"

  local TARGET_HOST_PARAM
  TARGET_HOST_PARAM="$(_target_endpoint_host "$DEST_HOST")"

  # 5) Run remote_migrate
  _info "Starting remote_migrate..."
  local MIGRATE_RC=0
  pvesh create "/nodes/$(hostname)/qemu/${VMID}/remote_migrate" \
    --target-bridge=1 \
    --target-endpoint="apitoken=PVEAPIToken=root@pam!migrate-full=${TOKEN_SECRET},host=${TARGET_HOST_PARAM},fingerprint=${FINGERPRINT}" \
    --target-storage="$TARGET_STORAGE" \
    --online \
    --delete \
    || MIGRATE_RC=$?

  if [[ "$MIGRATE_RC" -ne 0 ]]; then
    _die "remote_migrate failed with exit code $MIGRATE_RC"
  fi

  # Migration succeeded - clear trap and run success steps
  trap - EXIT
  MIGRATION_SUCCESS=1

  # 6 success) Run sync-dnat.py post-start on destination if VM is running
  local DEST_STATUS
  DEST_STATUS="$(ssh "${SSH_OPTS[@]}" "$DEST_SSH" "qm status '${VMID}' 2>/dev/null" | awk -F': ' '/status:/{print $2; exit}' | tr -d '\r' || true)"
  if [[ "$DEST_STATUS" == "running" ]]; then
    local SYNC_DNAT="/var/lib/svz/snippets/sync-dnat.py"
    if ssh "${SSH_OPTS[@]}" "$DEST_SSH" "test -x '${SYNC_DNAT}'" 2>/dev/null; then
      _info "Running sync-dnat.py post-start on destination..."
      ssh "${SSH_OPTS[@]}" "$DEST_SSH" "'${SYNC_DNAT}' '${VMID}' post-start" 2>/dev/null || _info "sync-dnat.py post-start had issues; continuing."
      _ok "sync-dnat post-start done."
    else
      _info "sync-dnat.py not found on destination; skipping post-start."
    fi
  else
    _info "VM ${VMID} is not running on destination (status: ${DEST_STATUS:-unknown}); skipping sync-dnat post-start."
  fi

  # Add hookscript on destination
  _info "Adding hookscript on destination..."
  ssh "${SSH_OPTS[@]}" "$DEST_SSH" "qm set '${VMID}' --hookscript shared:snippets/sync-dnat.py" || _die "Failed to add hookscript on destination."
  _ok "Hookscript added on destination."

  # Update Firestore: connectionUrl, nodeId, maintenance=false
  if [[ "$UPDATE_FIRESTORE" == "true" ]]; then
    _info "Updating Firestore (connectionUrl, nodeId, maintenance=false)..."
    local DEST_PUBLIC_IP
    DEST_PUBLIC_IP="$(ssh "${SSH_OPTS[@]}" "$DEST_SSH" 'curl -4 -s --connect-timeout 5 ifconfig.me 2>/dev/null || ip route get 8.8.8.8 2>/dev/null | awk "{print \$7; exit}"' | tr -d '\r')"
    if [[ -z "$DEST_PUBLIC_IP" ]]; then
      _info "Could not get destination public IP; Firestore connectionUrl may be stale."
    else
      DEST_CONNECTION_URL="${DEST_PUBLIC_IP}:$((BASE_PORT + VMID))"
      local HELPER
      HELPER="$(mktemp -t update_firestore_migration_XXXXXX.py)"
      if command -v python3 >/dev/null 2>&1 && curl -sSL "https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/snippets/update_firestore_migration.py" -o "$HELPER" 2>/dev/null; then
        if python3 "$HELPER" "$VMID" "$DEST_CONNECTION_URL" "$DEST_NODE"; then
          _ok "Firestore updated: connectionUrl=${DEST_CONNECTION_URL}, nodeId=${DEST_NODE}"
        else
          _info "Firestore update failed; check credentials."
        fi
        rm -f "$HELPER"
      fi
    fi
  fi

  # Remove token on destination
  _info "Removing migration token on destination..."
  ssh "${SSH_OPTS[@]}" "$DEST_SSH" "pveum user token remove root@pam migrate-full" || _info "Token remove had issues (may already be removed)."
  TOKEN_CREATED=0
  _ok "Token removed."

  _ok "Migration completed: VM ${VMID} -> ${DEST_SSH} (node ${DEST_NODE})"
}

# Examples:
#   Minimal:
#     bash migrate_vm.sh 1008 root@10.64.0.7
#   With options:
#     bash migrate_vm.sh 1008 root@10.64.0.7 --dest-node=0000007-AX162-R --target-storage=local-zfs
#   One-liner (curl):
#     curl -sSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/migrate_vm.sh | bash -s -- 1008 root@10.64.0.7
#
pve_zfs_migrate_vm "$@"

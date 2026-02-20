pve_zfs_migrate_vm() {
  local USAGE="Usage: pve_zfs_migrate_vm <vmid> <dest_ssh> [options]. Options: --dest-node, --snapname, --shutdown-timeout, --suspend-timeout, --hot, --src-cleanup, --dest-cleanup, --delete-src-vm, --update-firestore, --keep-dest-stopped. Or pass positional args 3-11 for legacy. Use --help for details."
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
  local HOT_MIGRATION="false"
  local SUSPEND_TIMEOUT="120"
  local HOT_REFRESH_MAX_ATTEMPTS=3
  local HOT_REFRESH_WAIT_SECONDS=10
  local HOT_TRIGGER_WAIT_SECONDS=10

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
          echo "    --hot=<true|false>                Hot migration: suspend to disk, migrate, resume on dest (false)"
          echo "    --suspend-timeout=<sec>           Wait up to this many seconds for suspend to complete (120)"
          echo "  Hot migration uses suspend-to-disk and qm resume on destination. VM state must be on storage"
          echo "  that is migrated (e.g. same ZFS as VM disks). With --hot and --keep-dest-stopped the VM is"
          echo "  left suspended on destination."
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
        --hot=*)               HOT_MIGRATION="${1#*=}"; shift ;;
        --hot)                 if [[ "${2:-}" == "true" || "${2:-}" == "false" ]]; then HOT_MIGRATION="${2}"; shift 2; else HOT_MIGRATION="true"; shift; fi ;;
        --suspend-timeout=*)   SUSPEND_TIMEOUT="${1#*=}"; shift ;;
        --suspend-timeout)     [[ $# -ge 2 ]] || { echo "Missing value for $1" >&2; exit 1; }; SUSPEND_TIMEOUT="$2"; shift 2 ;;
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
    HOT_MIGRATION="${1:-false}"; [[ $# -ge 1 ]] && shift
    SUSPEND_TIMEOUT="${1:-120}"; [[ $# -ge 1 ]] && shift
  fi

  local SSH_OPTS=(-o LogLevel=ERROR -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ForwardAgent=yes)

  local MBUFFER_BLOCK="128k"
  local MBUFFER_MEM="1G"

  local MIGRATION_SUCCESS=0
  local PHASE=0
  local WAS_SUSPENDED=0
  local FIRESTORE_UPDATED_TO_DEST=0
  local SOURCE_NODE=""
  local SOURCE_CONNECTION_URL=""

  _die()  { echo "❌ $*" >&2; exit 1; }
  _info() { echo "ℹ️  $*" >&2; }
  _ok()   { echo "✅ $*" >&2; }
  _bytes_human() {
    local b="${1:-0}"
    if command -v numfmt >/dev/null 2>&1; then
      numfmt --to=iec-i --suffix=B "$b" 2>/dev/null || echo "${b} bytes"
    else
      if [[ "$b" -ge 1073741824 ]]; then
        echo "$(( b / 1073741824 )) GiB"
      elif [[ "$b" -ge 1048576 ]]; then
        echo "$(( b / 1048576 )) MiB"
      else
        echo "${b} bytes"
      fi
    fi
  }

  _extract_global_ipv6_from_interfaces_json() {
    local interfaces_json="${1:-}"
    if [[ -z "$interfaces_json" ]]; then
      echo ""
      return 0
    fi
    if ! command -v python3 >/dev/null 2>&1; then
      echo ""
      return 0
    fi
    JSON_INPUT="$interfaces_json" python3 - <<'PY'
import json
import os
import ipaddress

raw = os.environ.get("JSON_INPUT", "")
if not raw:
    raise SystemExit(0)

try:
    interfaces = json.loads(raw)
except Exception:
    raise SystemExit(0)

if not isinstance(interfaces, list):
    raise SystemExit(0)

for iface in interfaces:
    if not isinstance(iface, dict):
        continue
    name = str(iface.get("name", "")).lower()
    if "loopback" in name:
        continue
    for addr in iface.get("ip-addresses", []):
        if not isinstance(addr, dict):
            continue
        ip = str(addr.get("ip-address", "")).strip()
        if not ip:
            continue
        if "%" in ip or ip.startswith("fe80::") or ip == "::1":
            continue
        try:
            ip_obj = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if ip_obj.version != 6:
            continue
        if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local:
            continue
        print(ip)
        raise SystemExit(0)
PY
  }

  _get_vm_ipv6_local() {
    local vmid="$1"
    local interfaces_json
    set +e
    interfaces_json="$(qm guest cmd "$vmid" network-get-interfaces 2>/dev/null)"
    local rc=$?
    set -e
    if (( rc != 0 )) || [[ -z "$interfaces_json" ]]; then
      echo ""
      return 0
    fi
    _extract_global_ipv6_from_interfaces_json "$interfaces_json"
  }

  _get_vm_ipv6_dest() {
    local interfaces_json
    set +e
    interfaces_json="$(ssh "${SSH_OPTS[@]}" "$DEST_SSH" "qm guest cmd '${VMID}' network-get-interfaces 2>/dev/null" | tr -d '\r')"
    local rc=$?
    set -e
    if (( rc != 0 )) || [[ -z "$interfaces_json" ]]; then
      echo ""
      return 0
    fi
    _extract_global_ipv6_from_interfaces_json "$interfaces_json"
  }

  GA_LAST_EXITCODE=""
  GA_LAST_STDOUT=""
  GA_LAST_STDERR=""
  _ga_exec_and_wait_dest() {
    local description="$1"
    local timeout_seconds="${2:-180}"
    shift 2
    local -a command_parts=("$@")
    local -a create_cmd=(pvesh create "/nodes/${DEST_NODE}/qemu/${VMID}/agent/exec" --output-format json)
    local create_cmd_str exec_out pid status_out
    local elapsed=0 poll=2

    GA_LAST_EXITCODE=""
    GA_LAST_STDOUT=""
    GA_LAST_STDERR=""

    for arg in "${command_parts[@]}"; do
      create_cmd+=(--command "$arg")
    done
    printf -v create_cmd_str "%q " "${create_cmd[@]}"

    set +e
    exec_out="$(ssh "${SSH_OPTS[@]}" "$DEST_SSH" "$create_cmd_str" 2>&1)"
    local exec_rc=$?
    set -e
    if (( exec_rc != 0 )); then
      GA_LAST_STDERR="Failed to start ${description}: ${exec_out}"
      return 1
    fi

    if ! command -v python3 >/dev/null 2>&1; then
      GA_LAST_STDERR="python3 is required to parse guest-agent responses."
      return 1
    fi
    pid="$(EXEC_JSON="$exec_out" python3 - <<'PY'
import json
import os
raw = os.environ.get("EXEC_JSON", "")
try:
    payload = json.loads(raw)
except Exception:
    print("")
    raise SystemExit(0)
if isinstance(payload, dict):
    data = payload.get("data")
    if isinstance(data, dict) and data.get("pid") is not None:
        pid = data.get("pid")
    else:
        pid = payload.get("pid")
else:
    pid = None
print(pid if isinstance(pid, int) else "")
PY
)"
    if [[ -z "$pid" || "$pid" -le 0 ]]; then
      GA_LAST_STDERR="Missing PID for ${description}. Raw response: ${exec_out}"
      return 1
    fi

    while (( elapsed < timeout_seconds )); do
      set +e
      status_out="$(ssh "${SSH_OPTS[@]}" "$DEST_SSH" "pvesh get /nodes/${DEST_NODE}/qemu/${VMID}/agent/exec-status --output-format json --pid '${pid}'" 2>&1)"
      local status_rc=$?
      set -e
      if (( status_rc != 0 )); then
        sleep "$poll"
        (( elapsed += poll )) || true
        continue
      fi

      local parsed exited exitcode out_b64 err_b64
      parsed="$(STATUS_JSON="$status_out" python3 - <<'PY'
import base64
import json
import os
raw = os.environ.get("STATUS_JSON", "")
try:
    payload = json.loads(raw)
except Exception:
    print("0 -1   ")
    raise SystemExit(0)
if isinstance(payload, dict):
    data = payload.get("data")
    if not isinstance(data, dict):
        data = payload
else:
    data = {}
exited = int(data.get("exited", 0) or 0)
exitcode_raw = data.get("exitcode")
if exitcode_raw is None:
    exitcode = -1
else:
    try:
        exitcode = int(exitcode_raw)
    except Exception:
        exitcode = -1
out_data = data.get("out-data") or ""
err_data = data.get("err-data") or ""
print(f"{exited} {exitcode} {base64.b64encode(out_data.encode()).decode()} {base64.b64encode(err_data.encode()).decode()}")
PY
)"
      read -r exited exitcode out_b64 err_b64 <<<"$parsed"
      GA_LAST_EXITCODE="$exitcode"
      GA_LAST_STDOUT="$(printf '%s' "${out_b64:-}" | base64 -d 2>/dev/null || true)"
      GA_LAST_STDERR="$(printf '%s' "${err_b64:-}" | base64 -d 2>/dev/null || true)"

      if [[ "${exited:-0}" == "1" ]]; then
        if [[ "${GA_LAST_EXITCODE:-1}" == "0" ]]; then
          return 0
        fi
        return 1
      fi
      sleep "$poll"
      (( elapsed += poll )) || true
    done

    GA_LAST_STDERR="Timed out after ${timeout_seconds}s waiting for ${description} (PID ${pid}). Last status: ${status_out}"
    return 1
  }

  _get_firestore_ipv6_by_vmid() {
    local vmid="$1"
    if ! command -v python3 >/dev/null 2>&1; then
      echo ""
      return 0
    fi
    VMID_QUERY="$vmid" python3 - <<'PY'
import os
import sys
from pathlib import Path

vmid_raw = os.environ.get("VMID_QUERY", "")
try:
    vmid = int(vmid_raw)
except Exception:
    print("")
    raise SystemExit(0)

try:
    import firebase_admin
    from firebase_admin import credentials, firestore
    try:
        from google.cloud.firestore_v1 import FieldFilter
        has_filter = True
    except Exception:
        has_filter = False
except Exception:
    print("")
    raise SystemExit(0)

try:
    if not firebase_admin._apps:
        creds_file = os.environ.get("FIREBASE_CREDENTIALS_FILE", "/etc/firebase-credentials.json")
        creds_path = Path(creds_file)
        if not creds_path.exists() or not creds_path.is_file():
            print("")
            raise SystemExit(0)
        firebase_admin.initialize_app(credentials.Certificate(str(creds_path)))

    db = firestore.client()
    servers_ref = db.collection("servers")
    if has_filter:
        query = servers_ref.where(filter=FieldFilter("proxmoxId", "==", vmid))
    else:
        query = servers_ref.where("proxmoxId", "==", vmid)
    docs = list(query.stream())
    if not docs:
        print("")
        raise SystemExit(0)

    ipv6 = docs[0].to_dict().get("ipv6")
    if ipv6 is None:
        print("")
    else:
        print(str(ipv6).strip())
except Exception:
    print("")
PY
  }

  _run_connectivity_checks() {
    local out6 out4 rc6 rc4
    set +e
    out6="$(nc -w 5 -6 -zv sqx.neuravps.com 20996 2>&1)"
    rc6=$?
    out4="$(nc -w 5 -4 -zv sqx.neuravps.com 20996 2>&1)"
    rc4=$?
    set -e
    if (( rc6 == 0 && rc4 == 0 )); then
      _ok "Connectivity checks succeeded (IPv6 and IPv4): sqx.neuravps.com:20996"
      return 0
    fi
    _info "Connectivity check IPv6 output: ${out6}"
    _info "Connectivity check IPv4 output: ${out4}"
    return 1
  }

  _cleanup_on_failure() {
    set +e
    _info "Migration failed or interrupted; cleaning up and restoring state..."
    if [[ "$UPDATE_FIRESTORE" == "true" ]]; then
      if [[ "${FIRESTORE_UPDATED_TO_DEST:-0}" == "1" ]] && [[ -n "${SOURCE_CONNECTION_URL:-}" ]] && [[ -n "${SOURCE_NODE:-}" ]]; then
        _info "Reverting Firestore to source (connectionUrl, nodeId)..."
        local HELPER_REVERT
        HELPER_REVERT="$(mktemp -t update_firestore_migration_XXXXXX.py)"
        if command -v python3 >/dev/null 2>&1 && curl -sSL "https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/snippets/update_firestore_migration.py" -o "$HELPER_REVERT" 2>/dev/null; then
          python3 "$HELPER_REVERT" "$VMID" "$SOURCE_CONNECTION_URL" "$SOURCE_NODE" 2>/dev/null || true
        fi
        rm -f "$HELPER_REVERT"
      fi
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
      if [[ "$WAS_SUSPENDED" -eq 1 ]]; then
        _info "Resuming VM ${VMID} on source again..."
        qm resume "$VMID" 2>/dev/null || true
      else
        _info "Starting VM ${VMID} on source again..."
        qm start "$VMID" 2>/dev/null || true
      fi
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

  local PRE_MIGRATION_IPV6=""
  if [[ "$HOT_MIGRATION" == "true" ]]; then
    _info "Detecting VM ${VMID} IPv6 before migration..."
    local pre_attempt=1 pre_max_attempts=6
    while (( pre_attempt <= pre_max_attempts )); do
      PRE_MIGRATION_IPV6="$(_get_vm_ipv6_local "$VMID")"
      if [[ -n "$PRE_MIGRATION_IPV6" ]]; then
        _ok "Detected pre-migration IPv6 for VM ${VMID}: ${PRE_MIGRATION_IPV6}"
        break
      fi
      if (( pre_attempt == pre_max_attempts )); then
        _die "Could not detect previous IPv6 for VM ${VMID} before migration."
      fi
      _info "Previous IPv6 not available yet (attempt ${pre_attempt}/${pre_max_attempts}); retrying in 5s..."
      sleep 5
      (( pre_attempt++ )) || true
    done
  fi

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

  if [[ "$HOT_MIGRATION" == "true" ]]; then
    local memory_mb
    memory_mb="$(qm config "$VMID" 2>/dev/null | awk -F': ' '/^memory:/{print $2; exit}' | tr -d ' ')"
    local memory_bytes=$(( (memory_mb + 0) * 1024 * 1024 ))
    [[ "$memory_bytes" -le 0 ]] && memory_bytes=1
    local node_name
    node_name="$(hostname 2>/dev/null)" || node_name=""
    if [[ -n "$node_name" ]]; then
      local api_json api_val config_bytes
      config_bytes=$(( (memory_mb + 0) * 1024 * 1024 ))
      api_json="$(pvesh get "/nodes/${node_name}/qemu/${VMID}/status/current" --output-format=json 2>/dev/null)" || true
      if [[ -n "${api_json:-}" ]]; then
        # Proxmox API returns mem/maxmem in bytes; match "mem" key only (not "maxmem")
        api_val="$(echo "$api_json" | grep -oE '(^|,)"mem":[0-9]+' | head -1 | sed 's/.*"mem"://')"
        [[ -z "${api_val:-}" ]] && api_val="$(echo "$api_json" | grep -oE '(^|,)"balloon":[0-9]+' | head -1 | sed 's/.*"balloon"://')"
        if [[ -n "${api_val:-}" && "$api_val" -gt 0 ]]; then
          # Sanity cap: denominator at most 2x configured memory so progress bar stays meaningful
          if [[ "$config_bytes" -gt 0 && "$api_val" -le $(( config_bytes * 2 )) ]]; then
            memory_bytes=$api_val
          fi
        fi
      fi
    fi
    [[ "$memory_bytes" -le 0 ]] && memory_bytes=1

    local MEM_HUMAN
    MEM_HUMAN="$(_bytes_human "$memory_bytes")"
    _info "Suspending VM ${VMID} to disk (hot migration)..."
    _info "  Amount of RAM to suspend (denominator): ${memory_bytes} bytes (${MEM_HUMAN})"
    qm suspend "$VMID" --todisk 1 &
    local SUSPEND_PID=$!
    local elapsed=0
    _is_suspended() {
      local s
      s="$(qm status "$VMID" 2>/dev/null | awk -F': ' '/^status:/{gsub(/^[ \t]+|[ \t\r]+$/,"",$2); print $2; exit}')"
      [[ "$s" == "suspended" ]]
    }
    _get_state_used() {
      local u
      u="$(zfs list -H -p -o name,used 2>/dev/null | awk -v vmid="$VMID" 'index($1, "vm-" vmid) && index($1, "state-suspend") {print $2; exit}')"
      [[ -n "$u" ]] && echo "$u" && return
      zfs list -H -p -o name,used -t volume 2>/dev/null | awk -v vmid="$VMID" 'index($1, "vm-" vmid) && index($1, "state-suspend") {print $2; exit}'
    }
    while true; do
      if _is_suspended || ! kill -0 "$SUSPEND_PID" 2>/dev/null; then
        break
      fi
      if (( elapsed >= SUSPEND_TIMEOUT )); then
        kill "$SUSPEND_PID" 2>/dev/null || true
        _die "Suspend timeout (${SUSPEND_TIMEOUT}s) reached; VM may be in an inconsistent state. Check and resume manually if needed."
      fi
      local state_used
      state_used="$(_get_state_used)"
      # Debug: every 6 seconds print written/total so we can see if numerator is detected
      if (( elapsed > 0 && elapsed % 6 == 0 )); then
        if [[ -n "${state_used:-}" && "$state_used" -ge 0 ]]; then
          local sh
          sh="$(_bytes_human "$state_used")"
          _info "  Suspend progress: written ${state_used} bytes (${sh}) / ${memory_bytes} bytes (${MEM_HUMAN})"
        else
          _info "  Suspend progress: written (no ZFS state dataset found yet) / ${memory_bytes} bytes (${MEM_HUMAN})"
        fi
      fi
      if [[ -n "${state_used:-}" && "$memory_bytes" -gt 0 ]]; then
        local pct=$(( state_used * 100 / memory_bytes ))
        [[ "$pct" -gt 99 ]] && pct=99
        local bar_width=30 filled=$(( pct * bar_width / 100 )) bar="" i
        for (( i=0; i<filled; i++ )); do bar+="#"; done
        for (( i=filled; i<bar_width; i++ )); do bar+=" "; done
        printf '\r[%s] %3d%%   ' "$bar" "$pct" >&2
      else
        printf '\rSuspending to disk... ( %3ds)   ' "$elapsed" >&2
      fi
      sleep 2; (( elapsed += 2 ))
    done
    printf '\n' >&2
    # Reap suspend process; cap wait at 10s so we never block forever if it lingers
    wait "$SUSPEND_PID" 2>/dev/null &
    local WAIT_PID=$!
    local wait_elapsed=0
    while kill -0 "$WAIT_PID" 2>/dev/null && (( wait_elapsed < 10 )); do
      sleep 1; (( wait_elapsed += 1 ))
    done
    kill "$WAIT_PID" 2>/dev/null || true
    wait "$WAIT_PID" 2>/dev/null || true
    WAS_SUSPENDED=1
    _ok "VM ${VMID} suspended."
  else
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
  fi

  # Hot migration: include suspend state zvol so resume works on destination
  if [[ "$HOT_MIGRATION" == "true" ]]; then
    local state_ds
    state_ds="$(zfs list -H -o name 2>/dev/null | grep -E "vm-${VMID}-state-suspend" | head -1)"
    if [[ -n "${state_ds:-}" ]]; then
      local -a ALL_DS=("${DATASETS[@]}" "$state_ds")
      local -a UNIQUE2=()
      declare -A seen2=()
      local d
      for d in "${ALL_DS[@]}"; do
        if [[ -z "${seen2[$d]:-}" ]]; then UNIQUE2+=("$d"); seen2["$d"]=1; fi
      done
      DATASETS=("${UNIQUE2[@]}")
      _info "Including suspend state zvol in migration: ${state_ds}"
    else
      _info "No ZFS state zvol found for VM ${VMID} (state may be on other storage); resume on destination may fail."
    fi
  fi

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

    if command -v stdbuf >/dev/null 2>&1; then
      PV_CMD=(stdbuf -e 0 pv -s "$SIZE" -ptebar)
    else
      PV_CMD=(pv -s "$SIZE" -ptebar)
    fi
    zfs send -R "${ds}@${SNAPNAME}" \
      | "${PV_CMD[@]}" \
      | mbuffer -q -s "${MBUFFER_BLOCK}" -m "${MBUFFER_MEM}" \
      | ssh "${SSH_OPTS[@]}" "$DEST_SSH" \
          "mbuffer -q -s '${MBUFFER_BLOCK}' -m '${MBUFFER_MEM}' | zfs receive -F '${ds}'" \
      || _die "Transfer failed for ${ds}@${SNAPNAME}"

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

  # Update Firestore before start/resume so sync-dnat.py can update IPs on start
  if [[ "$UPDATE_FIRESTORE" == "true" ]]; then
    _info "Updating Firestore (connectionUrl, nodeId) before starting VM..."
    SOURCE_NODE="$(hostname 2>/dev/null)" || SOURCE_NODE=""
    local SOURCE_PUBLIC_IP
    SOURCE_PUBLIC_IP="$(curl -4 -s --connect-timeout 5 ifconfig.me 2>/dev/null || ip route get 8.8.8.8 2>/dev/null | awk '{print $7; exit}')" || true
    SOURCE_PUBLIC_IP="${SOURCE_PUBLIC_IP%%[[:space:]]*}"
    SOURCE_PUBLIC_IP="${SOURCE_PUBLIC_IP//$'\r'/}"
    local BASE_PORT=20000
    if [[ -n "${SOURCE_PUBLIC_IP:-}" ]]; then
      SOURCE_CONNECTION_URL="${SOURCE_PUBLIC_IP}:$((BASE_PORT + VMID))"
    fi
    local DEST_PUBLIC_IP
    DEST_PUBLIC_IP="$(ssh "${SSH_OPTS[@]}" "$DEST_SSH" 'curl -4 -s --connect-timeout 5 ifconfig.me 2>/dev/null || ip route get 8.8.8.8 2>/dev/null | awk "{print \$7; exit}"' | tr -d '\r')"
    if [[ -z "$DEST_PUBLIC_IP" ]]; then
      _die "Could not get destination public IPv4; cannot update Firestore."
    fi
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
    FIRESTORE_UPDATED_TO_DEST=1
    _ok "Firestore updated: connectionUrl=${CONNECTION_URL}, nodeId=${DEST_NODE}"
  fi

  if [[ "$KEEP_DEST_STOPPED" == "true" ]]; then
    _info "Keeping destination VM stopped (keep_dest_stopped=true)."
    PHASE=4
  else
    if [[ "$HOT_MIGRATION" == "true" ]]; then
      _info "Resuming VM ${VMID} on destination..."
      ssh "${SSH_OPTS[@]}" "$DEST_SSH" "qm resume '${VMID}'"
      _ok "VM ${VMID} resumed on destination."
    else
      _info "Starting VM ${VMID} on destination..."
      ssh "${SSH_OPTS[@]}" "$DEST_SSH" "qm start '${VMID}'"
      _ok "VM ${VMID} started on destination."
    fi
    PHASE=4

    # Hot migration only: wait for VM running, force Windows network reset, validate IPv6 rollover, run sync-dnat post-start
    if [[ "$HOT_MIGRATION" == "true" ]]; then
      local WAIT_RUNNING_TIMEOUT=180 wait_elapsed=0
      _info "Waiting for VM ${VMID} to be running on destination (timeout ${WAIT_RUNNING_TIMEOUT}s)..."
      while true; do
        local DEST_STATUS
        DEST_STATUS="$(ssh "${SSH_OPTS[@]}" "$DEST_SSH" "qm status '${VMID}' 2>/dev/null" | awk -F': ' '/status:/{print $2; exit}' | tr -d '\r' || true)"
        if [[ "$DEST_STATUS" == "running" ]]; then
          _ok "VM ${VMID} is running on destination."
          break
        fi
        if (( wait_elapsed >= WAIT_RUNNING_TIMEOUT )); then
          _die "VM ${VMID} did not reach running state within ${WAIT_RUNNING_TIMEOUT}s (status: ${DEST_STATUS:-unknown})."
        fi
        sleep 5
        (( wait_elapsed += 5 )) || true
      done

      # Force Windows to reset network via guest agent and ensure IPv6 changes from pre-migration value
      _info "Waiting for guest agent to become available on destination..."
      sleep 20
      local ostype
      ostype="$(ssh "${SSH_OPTS[@]}" "$DEST_SSH" "qm config '${VMID}' 2>/dev/null" | awk -F': ' '/^ostype:/{print $2; exit}' | tr -d '\r' || true)"
      local old_ipv6="$PRE_MIGRATION_IPV6"
      local new_ipv6=""
      local ipv6_changed=0

      if [[ "$ostype" == win* ]]; then
        local refresh_attempt=1
        while (( refresh_attempt <= HOT_REFRESH_MAX_ATTEMPTS )); do
          local is_strong_attempt=0
          local ps_description="Windows network refresh (PowerShell basic)"
          local ps_command='$ErrorActionPreference="Stop"; ipconfig /release; Start-Sleep -Seconds 3; ipconfig /renew; Start-Sleep -Seconds 2; Get-NetIPAddress -AddressFamily IPv6 | Where-Object { $_.IPAddress -notlike "fe80*" -and $_.IPAddress -ne "::1" -and $_.IPAddress -notlike "fd*" -and $_.IPAddress -notlike "fc*" } | Select-Object -ExpandProperty IPAddress'
          if (( refresh_attempt == HOT_REFRESH_MAX_ATTEMPTS )); then
            is_strong_attempt=1
            ps_description="Windows network refresh (PowerShell strong no-reboot)"
            ps_command='$ErrorActionPreference="Stop"; ipconfig /release; Start-Sleep -Seconds 2; ipconfig /renew; Start-Sleep -Seconds 2; $adapter = Get-NetAdapter | Where-Object { $_.Status -eq "Up" -and $_.Name -notmatch "Loopback" } | Select-Object -First 1; if (-not $adapter) { throw "No active network adapter found" }; Disable-NetAdapter -Name $adapter.Name -Confirm:$false; Start-Sleep -Seconds 4; Enable-NetAdapter -Name $adapter.Name -Confirm:$false; Start-Sleep -Seconds 6; netsh interface ipv6 reset; Start-Sleep -Seconds 4; ipconfig /renew; Start-Sleep -Seconds 2; Get-NetIPAddress -AddressFamily IPv6 | Where-Object { $_.IPAddress -notlike "fe80*" -and $_.IPAddress -ne "::1" -and $_.IPAddress -notlike "fd*" -and $_.IPAddress -notlike "fc*" } | Select-Object -ExpandProperty IPAddress'
            _info "Windows network refresh attempt ${refresh_attempt}/${HOT_REFRESH_MAX_ATTEMPTS} (strong reset: adapter bounce + IPv6 reset)..."
          else
            _info "Windows network refresh attempt ${refresh_attempt}/${HOT_REFRESH_MAX_ATTEMPTS}..."
          fi

          if ! _ga_exec_and_wait_dest "${ps_description}" 240 powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "${ps_command}"; then
            _info "${ps_description} failed (exit ${GA_LAST_EXITCODE:-unknown}). stdout: ${GA_LAST_STDOUT:-<empty>}"
            _info "${ps_description} stderr: ${GA_LAST_STDERR:-<empty>}"
            if (( is_strong_attempt == 0 )); then
              _info "Trying cmd fallback (ipconfig release/renew)..."
              if ! _ga_exec_and_wait_dest "Windows network refresh (cmd fallback)" 180 cmd.exe /c "ipconfig /release & ipconfig /renew"; then
                _die "Windows network refresh failed on attempt ${refresh_attempt}. PowerShell and fallback both failed. Last stdout: ${GA_LAST_STDOUT:-<empty>} | Last stderr: ${GA_LAST_STDERR:-<empty>}"
              fi
              _ok "Fallback network refresh succeeded. stdout: ${GA_LAST_STDOUT:-<empty>}"
            else
              _die "Strong no-reboot Windows network reset failed on final attempt. Last stdout: ${GA_LAST_STDOUT:-<empty>} | Last stderr: ${GA_LAST_STDERR:-<empty>}"
            fi
          else
            _ok "${ps_description} succeeded. stdout: ${GA_LAST_STDOUT:-<empty>}"
          fi

          _info "Waiting ${HOT_REFRESH_WAIT_SECONDS}s for Windows to acquire a new IPv6..."
          sleep "${HOT_REFRESH_WAIT_SECONDS}"
          new_ipv6="$(_get_vm_ipv6_dest)"
          if [[ -n "$new_ipv6" && "$new_ipv6" != "$old_ipv6" ]]; then
            _ok "IPv6 rollover detected for VM ${VMID}: ${old_ipv6} -> ${new_ipv6}"
            ipv6_changed=1
            break
          fi

          if (( refresh_attempt == HOT_REFRESH_MAX_ATTEMPTS )); then
            _info "IPv6 did not change after ${HOT_REFRESH_MAX_ATTEMPTS} refresh attempts (including strong reset). old=${old_ipv6} new=${new_ipv6:-<empty>}. Proceeding to sync/connectivity validation."
            break
          fi
          _info "IPv6 unchanged after attempt ${refresh_attempt}. old=${old_ipv6} new=${new_ipv6:-<empty>}; retrying refresh..."
          (( refresh_attempt++ )) || true
        done
      else
        _info "VM ostype is '${ostype}' (not Windows); skipping Windows network refresh."
        sleep "${HOT_REFRESH_WAIT_SECONDS}"
        new_ipv6="$(_get_vm_ipv6_dest)"
        if [[ -n "$new_ipv6" && "$new_ipv6" != "$old_ipv6" ]]; then
          _ok "IPv6 changed for non-Windows VM: ${old_ipv6} -> ${new_ipv6}"
          ipv6_changed=1
        elif [[ -n "$new_ipv6" ]]; then
          _info "IPv6 did not change for non-Windows VM (old=${old_ipv6}, new=${new_ipv6}); continuing."
        else
          _info "Could not detect destination IPv6 for non-Windows VM after migration."
        fi
      fi

      # Reconfigure assigned IPv6 on destination and make failure explicit
      local SYNC_DNAT="/var/lib/svz/snippets/sync-dnat.py"
      if ! ssh "${SSH_OPTS[@]}" "$DEST_SSH" "test -x '${SYNC_DNAT}'" 2>/dev/null; then
        _die "sync-dnat.py not found or not executable on destination (${SYNC_DNAT})."
      fi
      _info "Running sync-dnat.py post-start on destination..."
      if ! ssh "${SSH_OPTS[@]}" "$DEST_SSH" "'${SYNC_DNAT}' '${VMID}' post-start" 2>&1; then
        _die "sync-dnat.py post-start failed on destination."
      fi
      _ok "sync-dnat.py post-start completed."

      _info "Waiting ${HOT_TRIGGER_WAIT_SECONDS}s for Firestore trigger/NAT64 propagation..."
      sleep "${HOT_TRIGGER_WAIT_SECONDS}"

      # Validate connectivity to published endpoint over both IPv6 and IPv4.
      if ! _run_connectivity_checks; then
        local firestore_ipv6
        firestore_ipv6="$(_get_firestore_ipv6_by_vmid "$VMID")"
        _info "Connectivity failed. Firestore IPv6 for VM ${VMID}: ${firestore_ipv6:-<empty>}; detected destination IPv6: ${new_ipv6:-<empty>}"

        if [[ -n "${new_ipv6:-}" && "${firestore_ipv6:-}" != "${new_ipv6:-}" ]]; then
          _info "Firestore IPv6 mismatch detected; rerunning sync-dnat.py post-start..."
          if ! ssh "${SSH_OPTS[@]}" "$DEST_SSH" "'${SYNC_DNAT}' '${VMID}' post-start" 2>&1; then
            _die "Connectivity failed and sync-dnat remediation failed for VM ${VMID}."
          fi
          sleep "${HOT_TRIGGER_WAIT_SECONDS}"
          firestore_ipv6="$(_get_firestore_ipv6_by_vmid "$VMID")"
          _info "Firestore IPv6 after remediation: ${firestore_ipv6:-<empty>}"
        fi

        if ! _run_connectivity_checks; then
          _die "Post-migration connectivity validation failed for VM ${VMID}. old_ipv6=${old_ipv6} new_ipv6=${new_ipv6:-<empty>} firestore_ipv6=${firestore_ipv6:-<empty>}"
        fi
      fi

      if [[ "$ipv6_changed" == "0" ]]; then
        _ok "IPv6 stayed unchanged after non-reboot reset attempts, but migration is accepted because sync-dnat and connectivity checks passed. old_ipv6=${old_ipv6} new_ipv6=${new_ipv6:-<empty>}"
      fi
    fi
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
#   Hot migration (suspend then resume on dest):
#     bash migrate_vm.sh 620 fd00:4000::6 --hot
#   Legacy positional (args 3-11: dest_node snapname shutdown_timeout src_cleanup dest_cleanup delete_src_vm update_firestore keep_dest_stopped hot suspend_timeout):
#     bash migrate_vm.sh 619 fd00:4000::2d '' migration 180 yes yes false true true false 120
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

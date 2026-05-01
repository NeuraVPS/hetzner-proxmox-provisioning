#!/usr/bin/env bash
# Migrate a Proxmox VM to another node using native remote_migrate API.
# Uses: apt upgrade, Firestore prep, hookscript detach, token creation, pvesh remote_migrate, sync-dnat, hookscript reattach, token cleanup.

MIGRATE_SCRIPT_URL="https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/scripts/migrate_vm.sh"

pve_zfs_migrate_vm() {
  local USAGE="Usage: pve_zfs_migrate_vm <vmid> <dest_ssh> [options]  (run on source node). Or: <vmid> <source_ssh> <dest_ssh> [options]  (SSH to source, then migrate). Use --help for details."
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
          echo "  When run on the source node: vmid, dest_ssh (e.g. root@10.64.0.7 or root@[fd00:4000::7])."
          echo "  When run from an orchestrator: <vmid> <source_ssh> <dest_ssh> [options] — SSHs to source_ssh and runs migration from there."
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
  _warn() { echo "⚠️  $*" >&2; }

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

  # Verify the dest VM currently has the given IPv6 (canonical IPv6 comparison
  # against `qm guest cmd network-get-interfaces` over SSH). Returns 0 if seen
  # within $2 seconds, 1 otherwise.
  _verify_dest_ipv6() {
    local expected="$1" timeout="${2:-90}" interval=5 elapsed=0 ifaces_json
    [[ -z "$expected" ]] && return 1
    while (( elapsed < timeout )); do
      ifaces_json="$(ssh "${SSH_OPTS[@]}" "$DEST_SSH" "pvesh get /nodes/${DEST_NODE}/qemu/${VMID}/agent/network-get-interfaces --output-format json 2>/dev/null" | tr -d '\r')"
      if [[ -n "$ifaces_json" ]] && EXPECTED="$expected" JSON="$ifaces_json" python3 -c '
import ipaddress, json, os, sys
try:
    target = ipaddress.IPv6Address(os.environ["EXPECTED"])
except ValueError: sys.exit(2)
try:
    data = json.loads(os.environ["JSON"])
except Exception: sys.exit(1)
ifaces = data.get("result", data) if isinstance(data, dict) else data
if not isinstance(ifaces, list): sys.exit(1)
for iface in ifaces:
    if not isinstance(iface, dict): continue
    for addr in iface.get("ip-addresses", []):
        if not isinstance(addr, dict): continue
        ip = str(addr.get("ip-address", "") or "").strip().split("%")[0]
        if not ip: continue
        try:
            if ipaddress.IPv6Address(ip) == target: sys.exit(0)
        except ValueError: continue
sys.exit(1)
' 2>/dev/null; then
        return 0
      fi
      sleep "$interval"
      (( elapsed += interval )) || true
    done
    return 1
  }

  # Run a PowerShell snippet on the dest VM via guest agent using -EncodedCommand
  # (UTF-16LE base64). Sidesteps bash/ssh/PowerShell quoting. Returns 0 if exec
  # exited within $2 seconds (default 120), 1 otherwise.
  _run_dest_ps() {
    local ps_script="$1" exec_timeout="${2:-120}"
    local ps_b64
    if ! command -v iconv >/dev/null 2>&1; then
      _info "iconv not available; cannot encode PS command"
      return 1
    fi
    ps_b64="$(printf '%s' "$ps_script" | iconv -t UTF-16LE | base64 -w0)"
    [[ -n "$ps_b64" ]] || return 1

    local exec_out agent_pid agent_attempt=0 agent_poll_max=90 agent_poll_interval=5
    while (( agent_attempt * agent_poll_interval < agent_poll_max )); do
      exec_out="$(ssh "${SSH_OPTS[@]}" "$DEST_SSH" \
        "pvesh create /nodes/${DEST_NODE}/qemu/${VMID}/agent/exec --output-format json --command powershell.exe --command -NoProfile --command -NonInteractive --command -EncodedCommand --command '${ps_b64}'" \
        2>&1)" || true
      agent_pid="$(echo "$exec_out" | sed -n 's/.*"pid"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' | head -1)"
      if [[ -n "$agent_pid" && "$agent_pid" -gt 0 ]]; then
        local exit_elapsed=0 status_out
        while (( exit_elapsed < exec_timeout )); do
          status_out="$(ssh "${SSH_OPTS[@]}" "$DEST_SSH" "pvesh get /nodes/${DEST_NODE}/qemu/${VMID}/agent/exec-status --output-format json --pid '${agent_pid}' 2>/dev/null" || true)"
          if echo "$status_out" | grep -qE '"exited"\s*:\s*1'; then return 0; fi
          sleep 5; (( exit_elapsed += 5 )) || true
        done
        return 1
      fi
      if echo "$exec_out" | grep -qi "guest agent is not running"; then
        (( agent_attempt++ )) || true
        sleep "$agent_poll_interval"
        continue
      fi
      return 1
    done
    return 1
  }

  _run_connectivity_checks() {
    local port=$((BASE_PORT + VMID)) out6 out4 rc6 rc4
    set +e
    out6="$(nc -w 5 -6 -zv sqx.neuravps.com "$port" 2>&1)"; rc6=$?
    out4="$(nc -w 5 -4 -zv sqx.neuravps.com "$port" 2>&1)"; rc4=$?
    set -e
    if (( rc6 == 0 && rc4 == 0 )); then
      _ok "Connectivity checks succeeded (IPv6 and IPv4): sqx.neuravps.com:${port}"
      return 0
    fi
    _info "Connectivity check IPv6 output: ${out6}"
    _info "Connectivity check IPv4 output: ${out4}"
    return 1
  }

  _firestore_update() {
    # Wrapper around inline python helper to avoid external downloads.
    command -v python3 >/dev/null 2>&1 || return 1
    python3 - "$@" <<'PY'
import argparse
import os
import sys
from pathlib import Path

try:
    import firebase_admin
    from firebase_admin import credentials, firestore
    try:
        from google.cloud.firestore_v1 import FieldFilter
        FIELD_FILTER_AVAILABLE = True
    except ImportError:
        FIELD_FILTER_AVAILABLE = False
    FIREBASE_AVAILABLE = True
except ImportError:
    FIREBASE_AVAILABLE = False
    FIELD_FILTER_AVAILABLE = False


def initialize_firebase():
    if not FIREBASE_AVAILABLE:
        return False
    if firebase_admin._apps:
        return True
    creds_file = os.environ.get("FIREBASE_CREDENTIALS_FILE", "/etc/firebase-credentials.json")
    creds_path = Path(creds_file)
    if not creds_path.exists() or not creds_path.is_file():
        return False
    try:
        cred = credentials.Certificate(str(creds_path))
        firebase_admin.initialize_app(cred)
        return True
    except Exception:
        return False


def _query_servers_by_vmid(db, vmid):
    servers_ref = db.collection("servers")
    if FIELD_FILTER_AVAILABLE:
        query = servers_ref.where(filter=FieldFilter("proxmoxId", "==", vmid))
    else:
        query = servers_ref.where("proxmoxId", "==", vmid)
    return list(query.stream())


def update_server_maintenance(vmid: int, maintenance: bool) -> bool:
    if not initialize_firebase():
        return False
    try:
        db = firestore.client()
        docs = _query_servers_by_vmid(db, vmid)
        if not docs:
            return False
        for doc_snapshot in docs:
            db.collection("servers").document(doc_snapshot.id).update({"maintenance": maintenance})
        return True
    except Exception:
        return False


def update_server_migration_prep(vmid: int, node_id: str) -> bool:
    if not initialize_firebase():
        return False
    try:
        db = firestore.client()
        docs = _query_servers_by_vmid(db, vmid)
        if not docs:
            return False
        for doc_snapshot in docs:
            db.collection("servers").document(doc_snapshot.id).update({
                "maintenance": True,
                "nodeId": node_id,
            })
        return True
    except Exception:
        return False


def _compute_deterministic_ipv6(db, node_id: str, vmid: int):
    """Read proxmox_nodes/{node_id}.ip → derive /64 prefix → return prefix + vmid_hex.

    Returns the deterministic IPv6 string or None on any failure.
    """
    try:
        node_doc = db.collection("proxmox_nodes").document(node_id).get()
        if not node_doc.exists:
            return None
        node_ip = str((node_doc.to_dict() or {}).get("ip", "") or "").strip()
        if not node_ip or "::" not in node_ip:
            return None
        parts = node_ip.split(":")
        if len(parts) < 4:
            return None
        prefix = ":".join(parts[:4]) + "::"
        return f"{prefix}{int(vmid):x}"
    except Exception:
        return None


def update_server_migration(vmid: int, connection_url: str, node_id: str,
                             explicit_ipv6: str = "") -> bool:
    """Update servers/{id} after a successful migration.

    Always sets nodeId and maintenance=False. ipv6 is set if either:
      (a) explicit_ipv6 is non-empty (passed in by caller, e.g. derived from
          dest vmbr0 over SSH), or
      (b) we can derive it from proxmox_nodes/{node_id}.ip in Firestore.

    connection_url is set only if non-empty — if the dest's public IP couldn't
    be determined, the critical fields (nodeId, ipv6) still flip so NAT46
    retargets correctly; connectionUrl can be patched later.
    """
    if not initialize_firebase():
        return False
    try:
        db = firestore.client()
        docs = _query_servers_by_vmid(db, vmid)
        if not docs:
            return False
        update_data = {
            "nodeId": node_id,
            "maintenance": False,
        }
        if connection_url:
            update_data["connectionUrl"] = connection_url
        # Prefer explicit ipv6 (caller already validated/derived it); fall back
        # to deriving from proxmox_nodes/{node_id}.ip in Firestore.
        ipv6 = explicit_ipv6 or _compute_deterministic_ipv6(db, node_id, vmid)
        if ipv6:
            update_data["ipv6"] = ipv6
        for doc_snapshot in docs:
            db.collection("servers").document(doc_snapshot.id).update(update_data)
        return True
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser(description="Update Firestore server for migration (connectionUrl, nodeId, maintenance)")
    parser.add_argument("--set-maintenance", choices=("true", "false"), metavar="BOOL",
                        help="Only set maintenance field (requires only vmid)")
    parser.add_argument("--set-migration-prep", metavar="NODE_ID",
                        help="Set maintenance=True and nodeId (requires only vmid). Used before migration.")
    parser.add_argument("--print-ipv6", action="store_true",
                        help="Print deterministic ipv6 for (vmid, node_id) and exit. No Firestore write.")
    parser.add_argument("--ipv6", default="",
                        help="Explicit ipv6 to write (overrides Firestore-derived). Used when "
                             "proxmox_nodes/{node_id}.ip is missing and the bash side derived "
                             "the prefix from the dest's vmbr0 directly.")
    parser.add_argument("vmid", type=int, help="Proxmox VM ID (proxmoxId)")
    parser.add_argument("connection_url", nargs="?", default="", help="connectionUrl")
    parser.add_argument("node_id", nargs="?", default="", help="Destination node hostname (nodeId)")
    args = parser.parse_args()

    if args.print_ipv6:
        # Backward-compat: callers may invoke `--print-ipv6 <vmid> <node_id>` (skipping
        # the optional connection_url positional). In that case argparse fills
        # `connection_url` with what was meant to be `node_id`. If `node_id` is
        # empty AND `connection_url` looks like a hostname (not an IP:PORT), treat it
        # as the node_id.
        node_id_for_ipv6 = args.node_id
        if not node_id_for_ipv6 and args.connection_url and ":" not in args.connection_url:
            node_id_for_ipv6 = args.connection_url
        if not node_id_for_ipv6:
            parser.error("--print-ipv6 requires both vmid and node_id")
        if not initialize_firebase():
            sys.stderr.write("firebase init failed\n"); sys.exit(1)
        ipv6 = _compute_deterministic_ipv6(firestore.client(), node_id_for_ipv6, args.vmid)
        if not ipv6:
            sys.stderr.write(
                f"could not compute deterministic ipv6 for node={node_id_for_ipv6!r} vmid={args.vmid} "
                f"(check proxmox_nodes/<node>.ip exists and is well-formed)\n"
            )
            sys.exit(1)
        print(ipv6)
        sys.exit(0)

    if args.set_migration_prep is not None:
        if not update_server_migration_prep(args.vmid, args.set_migration_prep):
            sys.stderr.write("update_firestore_migration: set migration prep failed\n")
            sys.exit(1)
        sys.exit(0)

    if args.set_maintenance is not None:
        maintenance = args.set_maintenance == "true"
        if not update_server_maintenance(args.vmid, maintenance):
            sys.stderr.write("update_firestore_migration: set maintenance failed\n")
            sys.exit(1)
        sys.exit(0)

    # node_id is required for migration update; connection_url is optional
    # (will be skipped in the Firestore write if empty — see update_server_migration).
    if not args.node_id:
        parser.error("node_id required when not using --set-maintenance/--set-migration-prep/--print-ipv6")
    if not update_server_migration(args.vmid, args.connection_url, args.node_id, args.ipv6):
        sys.stderr.write("update_firestore_migration: failed (no creds, no doc, or Firestore error)\n")
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
PY
  }

  _cleanup_on_failure() {
    set +e
    _info "Migration failed; cleaning up and restoring state..."
    if [[ "$UPDATE_FIRESTORE" == "true" ]] && [[ -n "${SOURCE_CONNECTION_URL:-}" ]] && [[ -n "${SOURCE_NODE:-}" ]]; then
      _info "Reverting Firestore to source (connectionUrl, nodeId)..."
      _firestore_update "$VMID" "$SOURCE_CONNECTION_URL" "$SOURCE_NODE" 2>/dev/null || true
    fi
    if [[ "$UPDATE_FIRESTORE" == "true" ]]; then
      _info "Setting server maintenance=false in Firestore..."
      _firestore_update --set-maintenance false "$VMID" 2>/dev/null || true
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

  # 1) apt-get update && apt-get -y upgrade, and install netcat-openbsd on source and destination
  _info "Running apt-get update && apt-get -y upgrade on source..."
  apt-get update -qq && apt-get -y upgrade -qq || _info "apt upgrade on source had issues; continuing."
  apt-get install -y -qq netcat-openbsd 2>/dev/null || _info "netcat-openbsd install on source had issues; continuing."
  _info "Running apt-get update && apt-get -y upgrade on destination..."
  ssh "${SSH_OPTS[@]}" "$DEST_SSH" "apt-get update -qq && apt-get -y upgrade -qq" || _info "apt upgrade on dest had issues; continuing."
  ssh "${SSH_OPTS[@]}" "$DEST_SSH" "apt-get install -y -qq netcat-openbsd" || _info "netcat-openbsd install on dest had issues; continuing."

  # 2) Firestore: maintenance=true (do not change nodeId before migration completes)
  if [[ "$UPDATE_FIRESTORE" == "true" ]]; then
    _info "Setting Firestore: maintenance=true..."
    if _firestore_update --set-maintenance true "$VMID" 2>/dev/null; then
      _ok "Firestore maintenance=true set."
    else
      _info "Could not set Firestore maintenance=true (no creds or doc); continuing."
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

  # Get fingerprint (with timeout so we never hang on unreachable hosts, e.g. public IP with port 8006 blocked)
  local CONNECT_SPEC
  CONNECT_SPEC="$(_openssl_connect_host "$DEST_HOST")"
  _info "Fetching SSL fingerprint from ${CONNECT_SPEC}..."
  local FINGERPRINT
  FINGERPRINT="$(timeout 15 openssl s_client -connect "$CONNECT_SPEC" -servername "$DEST_HOST" </dev/null 2>/dev/null | openssl x509 -noout -fingerprint -sha256 2>/dev/null | cut -d'=' -f2)"
  [[ -n "$FINGERPRINT" ]] || _die "Could not get SSL fingerprint for $DEST_HOST:8006 (connection timed out or refused). When using a public IP, ensure port 8006 is open and reachable from this host (firewall/security group)."

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

  local SYNC_DNAT="/var/lib/svz/snippets/sync-dnat.py"

  # 6 success) Compute the deterministic ipv6 the VM should have on dest.
  # Try Firestore first (proxmox_nodes/{node_id}.ip → derive prefix). If missing,
  # fall back to deriving the prefix from dest's vmbr0 over SSH. Done now (not
  # later) so it's available for both the Windows reset verification AND the
  # Firestore update at step 8 — regardless of OS type or running state.
  local EXPECTED_IPV6=""
  EXPECTED_IPV6="$(_firestore_update --print-ipv6 "$VMID" "$DEST_NODE" 2>/dev/null | tr -d '[:space:]\r')" || EXPECTED_IPV6=""
  if [[ -z "$EXPECTED_IPV6" ]]; then
    _info "proxmox_nodes/${DEST_NODE}.ip missing in Firestore; deriving prefix from dest vmbr0 directly..."
    local DEST_PREFIX
    DEST_PREFIX="$(ssh "${SSH_OPTS[@]}" "$DEST_SSH" \
      "ip -6 -o addr show dev vmbr0 scope global 2>/dev/null | awk '{print \$4}' | head -1 | cut -d/ -f1 | awk -F: '{printf \"%s:%s:%s:%s::\", \$1, \$2, \$3, \$4}'" \
      2>/dev/null | tr -d '\r')"
    if [[ -n "$DEST_PREFIX" && "$DEST_PREFIX" != "::"* ]]; then
      local VMID_HEX
      VMID_HEX="$(printf '%x' "$VMID")"
      EXPECTED_IPV6="${DEST_PREFIX}${VMID_HEX}"
      _info "Derived deterministic ipv6 from dest vmbr0: ${EXPECTED_IPV6}"
    else
      _warn "Could not derive prefix from dest vmbr0 either; ipv6 won't be updated in Firestore."
    fi
  fi

  # 7 success) Assign deterministic static IPv6 + renew DHCPv4 on dest VM.
  # Uses _run_dest_ps (-EncodedCommand base64) + Get-NetAdapter -Physical so we
  # don't depend on an adapter literally named "Ethernet" (renames are common)
  # and so we don't have to fight nested bash/ssh/PowerShell quoting.
  #
  # PS:
  #   1. Find the active physical adapter
  #   2. Remove any prior Manual IPv6 in PersistentStore/ActiveStore that doesn't
  #      match expected (clears the OLD node's static IPv6 left over after the
  #      hot-migration; otherwise both old + new addresses would coexist).
  #   3. Add the expected IPv6 to BOTH stores (PersistentStore = survive reboots,
  #      ActiveStore = bound now without restart). Idempotent.
  #   4. Release/renew DHCPv4 so the VM gets a fresh lease from the dest node's
  #      dnsmasq (the source node's lease is no longer valid).
  local DEST_STATUS
  DEST_STATUS="$(ssh "${SSH_OPTS[@]}" "$DEST_SSH" "qm status '${VMID}' 2>/dev/null" | awk -F': ' '/status:/{print $2; exit}' | tr -d '\r' || true)"
  if [[ "$DEST_STATUS" == "running" ]]; then
    local ostype
    ostype="$(ssh "${SSH_OPTS[@]}" "$DEST_SSH" "qm config '${VMID}' 2>/dev/null" | awk -F': ' '/^ostype:/{print $2; exit}' | tr -d '\r' || true)"
    if [[ "$ostype" == win* && -n "$EXPECTED_IPV6" ]]; then
      _info "Assigning static IPv6 ${EXPECTED_IPV6} + renewing DHCPv4 lease on dest VM..."
      local PS_ASSIGN
      PS_ASSIGN=$(cat <<PSEOF
\$ErrorActionPreference = 'SilentlyContinue'
\$exp = '${EXPECTED_IPV6}'
\$a = Get-NetAdapter -Physical | Where-Object Status -eq 'Up' | Select-Object -First 1
if (-not \$a) { Write-Error 'no active physical adapter'; exit 1 }
\$idx = \$a.ifIndex
foreach (\$st in 'PersistentStore', 'ActiveStore') {
  Get-NetIPAddress -InterfaceIndex \$idx -AddressFamily IPv6 -PolicyStore \$st -ErrorAction SilentlyContinue |
    Where-Object { \$_.PrefixOrigin -eq 'Manual' -and \$_.IPAddress -ne \$exp } |
    ForEach-Object {
      Remove-NetIPAddress -InterfaceIndex \$idx -IPAddress \$_.IPAddress -PolicyStore \$st -Confirm:\$false -ErrorAction SilentlyContinue
    }
}
foreach (\$st in 'PersistentStore', 'ActiveStore') {
  Remove-NetIPAddress -InterfaceIndex \$idx -IPAddress \$exp -PolicyStore \$st -Confirm:\$false -ErrorAction SilentlyContinue
  New-NetIPAddress -InterfaceIndex \$idx -IPAddress \$exp -PrefixLength 64 -AddressFamily IPv6 -PolicyStore \$st -ErrorAction SilentlyContinue | Out-Null
}
ipconfig /release | Out-Null
ipconfig /renew | Out-Null
exit 0
PSEOF
)
      if _run_dest_ps "$PS_ASSIGN" 90; then
        _ok "Static IPv6 + DHCPv4 renewal completed."
      else
        _warn "Static IPv6 assignment did not complete cleanly (guest agent unavailable or exec timed out)."
      fi

      _info "Verifying dest VM has deterministic ipv6 ${EXPECTED_IPV6}..."
      if _verify_dest_ipv6 "$EXPECTED_IPV6" 30; then
        _ok "ipv6 ${EXPECTED_IPV6} verified on dest VM."
      else
        _warn "ipv6 ${EXPECTED_IPV6} not visible after assignment (within 30s). Proceeding with Firestore update anyway."
      fi
    elif [[ "$ostype" == win* ]]; then
      _warn "Windows VM but EXPECTED_IPV6 could not be derived; skipping in-guest static assignment."
    else
      _info "VM ostype is '${ostype}' (not Windows); skipping in-guest IPv6 assignment."
    fi
  else
    _info "VM ${VMID} is not running on destination (status: ${DEST_STATUS:-unknown}); skipping in-guest IPv6 assignment."
  fi

  # 8 success) Update Firestore: nodeId + maintenance=false + deterministic ipv6 ALWAYS;
  # connectionUrl best-effort (skipped if dest public IP can't be determined, but the
  # critical fields still flip so NAT46 retargets correctly).
  if [[ "$UPDATE_FIRESTORE" == "true" ]]; then
    _info "Resolving destination public IP for connectionUrl..."
    local DEST_PUBLIC_IP=""
    # Try multiple services; fall back to nothing if all fail.
    DEST_PUBLIC_IP="$(ssh "${SSH_OPTS[@]}" "$DEST_SSH" '
      for svc in https://ifconfig.me https://api.ipify.org https://icanhazip.com; do
        ip=$(curl -4 -s --connect-timeout 5 "$svc" 2>/dev/null)
        if [[ -n "$ip" ]] && [[ "$ip" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
          echo "$ip"
          exit 0
        fi
      done
      exit 1
    ' 2>/dev/null | tr -d '\r' || true)"

    if [[ -n "$DEST_PUBLIC_IP" ]]; then
      DEST_CONNECTION_URL="${DEST_PUBLIC_IP}:$((BASE_PORT + VMID))"
      _info "Updating Firestore (connectionUrl=${DEST_CONNECTION_URL}, nodeId=${DEST_NODE}, maintenance=false, ipv6)..."
    else
      DEST_CONNECTION_URL=""
      _warn "Could not resolve destination public IP; updating Firestore WITHOUT connectionUrl (nodeId+ipv6+maintenance still updated)."
    fi

    # Pass the explicit ipv6 we already derived (either from Firestore or from
    # dest vmbr0). This way the update succeeds even if proxmox_nodes/{node}.ip
    # is missing in Firestore — without it, the helper would silently skip ipv6.
    local FIRESTORE_ARGS=()
    if [[ -n "${EXPECTED_IPV6:-}" ]]; then
      FIRESTORE_ARGS+=(--ipv6 "$EXPECTED_IPV6")
    fi
    if _firestore_update "${FIRESTORE_ARGS[@]}" "$VMID" "$DEST_CONNECTION_URL" "$DEST_NODE"; then
      _ok "Firestore updated: nodeId=${DEST_NODE}${EXPECTED_IPV6:+, ipv6=${EXPECTED_IPV6}}${DEST_CONNECTION_URL:+, connectionUrl=${DEST_CONNECTION_URL}}"
    else
      _warn "Firestore update FAILED. Migration succeeded but server doc may be stale (check credentials, or run /var/lib/svz/snippets/sync-dnat.py manually)."
    fi
  fi

  # 9 success) Add hookscript on destination
  _info "Adding hookscript on destination..."
  ssh "${SSH_OPTS[@]}" "$DEST_SSH" "qm set '${VMID}' --hookscript shared:snippets/sync-dnat.py" || _die "Failed to add hookscript on destination."
  _ok "Hookscript added on destination."

  # 10 success) Connectivity check (NAT46 should be reconciled by nat64_sync_trigger after Firestore update)
  if [[ "$DEST_STATUS" == "running" ]]; then
    _info "Waiting 5s for NAT/routing to propagate before connectivity check..."
    sleep 5
    _info "Verifying connectivity with netcat (sqx.neuravps.com:$((BASE_PORT + VMID)))..."
    if _run_connectivity_checks; then
      _ok "Netcat connectivity verification passed."
    else
      _info "Netcat connectivity check failed; migration may still be OK (Firestore/NAT may need time to propagate)."
    fi
  fi

  # Remove token on destination
  _info "Removing migration token on destination..."
  ssh "${SSH_OPTS[@]}" "$DEST_SSH" "pveum user token remove root@pam migrate-full" || _info "Token remove had issues (may already be removed)."
  TOKEN_CREATED=0
  _ok "Token removed."

  _ok "Migration completed: VM ${VMID} -> ${DEST_SSH} (node ${DEST_NODE})"
}

# Wrapper: 3-arg form (vmid source_ssh dest_ssh) → SSH to source host and run migration there
if [[ $# -ge 3 && "$3" != --* ]]; then
  VMID="$1"
  SOURCE_SSH="$2"
  DEST_SSH="$3"
  shift 3
  SSH_OPTS=(-o LogLevel=ERROR -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ForwardAgent=yes)
  curl -sSL "$MIGRATE_SCRIPT_URL" | ssh "${SSH_OPTS[@]}" "$SOURCE_SSH" bash -s -- "$VMID" "$DEST_SSH" "$@"
  exit $?
fi

# Examples:
#   Run on current host (must be source node):
#     bash migrate_vm.sh 1008 root@10.64.0.7
#   Run from orchestrator: SSH to source, then migrate to dest (enables sequential multi-host migrations):
#     bash migrate_vm.sh 1008 root@hostA root@hostB
#     bash migrate_vm.sh 1009 root@hostB root@hostC
#   With options:
#     bash migrate_vm.sh 1008 root@10.64.0.7 --dest-node=0000007-AX162-R --target-storage=local-zfs
#   One-liner (curl; add ?t=$(date +%s) to avoid CDN cache):
#     curl -sSL "https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/scripts/migrate_vm.sh?t=$(date +%s)" | bash -s -- 830 2a01:4f9:3070:2ccf::2
#
pve_zfs_migrate_vm "$@"

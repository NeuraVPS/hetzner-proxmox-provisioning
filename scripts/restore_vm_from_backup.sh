#!/usr/bin/env bash
# curl | bash -s feeds the script on stdin (fd 0). Re-exec from a temp file so ssh never reads the script stream.
if [[ ! -t 0 ]] && [[ -z "${BASH_SOURCE[0]:-}" ]]; then
  _restore_tmp="$(mktemp)"
  if ! cat >"$_restore_tmp"; then
    echo "ERROR: failed to read script from stdin" >&2
    exit 1
  fi
  if [[ ! -s "$_restore_tmp" ]]; then
    echo "ERROR: empty script from stdin" >&2
    exit 1
  fi
  exec bash "$_restore_tmp" "$@" </dev/null
fi
set -euo pipefail

# --- Run from curl (on the Proxmox node, as root) ---
# The block at the top re-executes the script from a temp file when stdin is a pipe
# (e.g. curl), so ssh/zstd/zfs still see a normal script file.
#
# You MUST pipe curl into bash: curl ... | bash -s -- args
#
# Examples (cache-bust the raw URL so you always get the latest revision):
#   # List available backups on the storage box:
#   curl -fsSL "https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/scripts/restore_vm_from_backup.sh?t=$(date +%s)" | bash -s -- --list
#
#   # In-place restore (overwrite the same VM, keep VMID/MAC/UUIDs); VM must be stopped:
#   curl -fsSL "https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/scripts/restore_vm_from_backup.sh?t=$(date +%s)" | bash -s -- 20260518_103000_node1_100_windows-es
#
#   # Restore a backup's disk DATA onto an EXISTING (stopped) different VM, keeping
#   # that VM's own config/MAC/UUIDs/firewall (e.g. backup of 611 onto VM 1312):
#   curl -fsSL "https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/scripts/restore_vm_from_backup.sh?t=$(date +%s)" | bash -s -- 20260518_072521_node_611_E-dev 1312
#
#   # Restore the backup as a NEW VM (new datasets, regenerated vmgenid/smbios uuid):
#   curl -fsSL "https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/scripts/restore_vm_from_backup.sh?t=$(date +%s)" | bash -s -- 20260518_103000_node1_100_windows-es 250
#
#   # Relocate to a different VMID but keep the SAME identity (MAC/vmgenid/smbios uuid):
#   curl -fsSL "https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/scripts/restore_vm_from_backup.sh?t=$(date +%s)" | KEEP_UUIDS=1 bash -s -- 20260518_103000_node1_100_windows-es 250
#
# Backup folders are written by NeuraVPS functions/proxmox_client.py::backup_vm_to_storagebox:
#   <STORAGE_BOX_BACKUP_PATH>/<YYYYmmdd_HHMMSS>_<node>_<vmid>_<name>/
#     config.conf
#     firewall.fw           (optional)
#     disk0.stream.zst ... disk{n-1}.stream.zst   (one zfs-send|zstd per disk, in sorted-config-key order)

die() {
  echo "ERROR: $*" >&2
  exit 1
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"
}

# [node] must match a directory under /etc/pve/nodes/ (often hostname -s, not FQDN).
resolve_pve_node_name() {
  local want="${1:-}"
  if [[ -n "$want" ]]; then
    if [[ -d "/etc/pve/nodes/${want}/qemu-server" ]] || [[ -d "/etc/pve/nodes/${want}" ]]; then
      echo "$want"
      return
    fi
    die "No /etc/pve/nodes/${want}/ on this host — use the cluster node name (ls /etc/pve/nodes/)"
  fi
  local c
  for c in "$(hostname -s 2>/dev/null)" "$(hostname 2>/dev/null)"; do
    [[ -z "$c" ]] && continue
    if [[ -d "/etc/pve/nodes/${c}/qemu-server" ]]; then
      echo "$c"
      return
    fi
  done
  die "Could not find local node under /etc/pve/nodes/*/qemu-server (try: $0 ... <node>). hostname -s=$(hostname -s 2>/dev/null) hostname=$(hostname 2>/dev/null)"
}

# pmxcfs: write via tmp + mv; mode/group like other *.conf on PVE (root:www-data 0640).
install_pve_cfg_file() {
  local src="$1" dst="$2"
  local dir tmp
  dir="$(dirname "$dst")"
  mkdir -p "$dir" || die "mkdir -p $dir failed"
  tmp="${dst}.tmp.${PPID}.$$"
  cp -- "$src" "$tmp" || die "cp to $tmp failed"
  chmod 0640 "$tmp" || true
  if getent group www-data >/dev/null 2>&1; then
    chgrp www-data "$tmp" 2>/dev/null || true
  else
    chmod 0644 "$tmp" || true
  fi
  mv -f -- "$tmp" "$dst" || die "mv to $dst failed (pmxcfs full?)"
  [[ -f "$dst" ]] || die "Config file missing after write: $dst"
}

# Same as proxmox_client._parse_vm_config_disk_tokens: one volume leaf per disk key,
# sorted by config key. NOTE: backup_vm_to_storagebox streams one disk{i}.stream.zst
# per token in this exact order WITHOUT deduping, so we must NOT dedupe here either.
# Emit "diskkey<TAB>volumeleaf" lines, sorted by config key (the order
# backup_vm_to_storagebox numbers disk{i}.stream.zst in).
_pve_sorted_disk_kv() {
  local f="$1"
  LC_ALL=C awk '
    function is_disk_key(k) {
      if (k == "efidisk0" || k == "tpmstate0") return 1
      return (match(k, /^(scsi|virtio|sata|ide)[0-9]+$/))
    }
    {
      line = $0
      sub(/\r$/, "", line)
      if (line ~ /^[[:space:]]*$/ || line ~ /^#/) next
      p = index(line, ":")
      if (p == 0) next
      key = substr(line, 1, p - 1)
      if (!is_disk_key(key)) next
      val = substr(line, p + 1)
      gsub(/^[[:space:]]+/, "", val)
      sub(/,.*/, "", val)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", val)
      if (index(val, ":") > 0) {
        q = index(val, ":")
        val = substr(val, q + 1)
      }
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", val)
      if (val != "") print key "\t" val
    }
  ' "$f" | LC_ALL=C sort -t $'\t' -k1,1
}

pve_sorted_disk_tokens() { _pve_sorted_disk_kv "$1" | cut -f2; }
pve_sorted_disk_keys() { _pve_sorted_disk_kv "$1" | cut -f1; }

# Map a sorted-order volume leaf (e.g. vm-1312-disk-1) to its ZFS dataset, like
# proxmox_client._map_tokens_to_datasets: full path used as-is, else match leaf.
map_token_to_dataset() {
  local token="$1" zfs_list="$2"
  if [[ "$token" == */* ]]; then
    printf '%s\n' "$token"
    return 0
  fi
  local ds
  while IFS= read -r ds; do
    [[ -z "$ds" ]] && continue
    if [[ "${ds##*/}" == "$token" ]]; then
      printf '%s\n' "$ds"
      return 0
    fi
  done <<<"$zfs_list"
  return 1
}

# Rewrite the backed-up VM config for the target VMID.
# regenerate_uuids=1 -> new vmgenid + smbios1 uuid (NEW-VM restore, like a clone).
# regenerate_uuids=0 -> keep uuids/mac (IN-PLACE restore: it is the same VM).
# Tokens passed as a file: a heredoc would occupy stdin (cannot pipe tokens into python -).
rewrite_backup_vm_config() {
  local config_path="$1"
  local next_vmid="$2"
  local vm_name="$3"
  local tokens_file="$4"
  local regenerate_uuids="$5"
  [[ -s "$tokens_file" ]] || die "tokens file missing or empty: $tokens_file"
  python3 - "$config_path" "$next_vmid" "$vm_name" "$tokens_file" "$regenerate_uuids" <<'PY'
import re, sys, uuid


def parse_conf(text: str) -> dict:
    result = {}
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        idx = line.index(":")
        key = line[:idx].strip()
        value = line[idx + 1:].strip()
        if key:
            result[key] = value
    return result


def rewrite(config, remote_vmid, next_vmid, name, token_to_local_leaf, regen):
    rewritten = {}
    for k, v in sorted(config.items()):
        if k in ("parent", "snaptime"):
            continue
        if token_to_local_leaf:
            for token, local_leaf in token_to_local_leaf.items():
                v = v.replace(token, local_leaf)
        else:
            v = v.replace(f"vm-{remote_vmid}-disk-", f"vm-{next_vmid}-disk-")
        if k == "name" and name:
            v = name
        rewritten[k] = v
    if regen:
        if "vmgenid" in rewritten:
            rewritten["vmgenid"] = str(uuid.uuid4())
        if "smbios1" in rewritten:
            old = rewritten["smbios1"]
            nu = str(uuid.uuid4())
            if "uuid=" in old:
                rewritten["smbios1"] = re.sub(r"uuid=[^,\s]+", f"uuid={nu}", old)
            else:
                rewritten["smbios1"] = f"{old},uuid={nu}".lstrip(",")
    lines = [f"{k}: {v}" for k, v in sorted(rewritten.items())]
    return "\n".join(lines) + "\n"


def main() -> None:
    config_path = sys.argv[1]
    next_vmid = int(sys.argv[2])
    name = sys.argv[3]
    tokens_path = sys.argv[4]
    regen = sys.argv[5] == "1"
    with open(tokens_path, encoding="utf-8") as tf:
        tokens = [ln.strip() for ln in tf if ln.strip()]
    if not tokens:
        print("no disk tokens in file", tokens_path, file=sys.stderr)
        sys.exit(1)
    text = open(config_path, encoding="utf-8").read()
    config = parse_conf(text)
    first = tokens[0]
    m = re.match(r"vm-(\d+)-disk-\d+", first)
    remote_vmid = int(m.group(1)) if m else 100
    token_to_local = {t: f"vm-{next_vmid}-disk-{i}" for i, t in enumerate(tokens)}
    out = rewrite(config, remote_vmid, next_vmid, name, token_to_local, regen)
    open(config_path, "w", encoding="utf-8").write(out)


if __name__ == "__main__":
    main()
PY
}

usage() {
  cat <<'EOF'
Restore a VM from a storage-box backup (folders written by
proxmox_client.py::backup_vm_to_storagebox).

Usage:
  restore_vm_from_backup.sh --list [node]
  restore_vm_from_backup.sh <backup_folder> [target_vmid] [node]

  <backup_folder>  Folder name under $STORAGE_BOX_BACKUP_PATH (default /home/backups),
                   e.g. 20260518_103000_node1_100_windows-es. An absolute path on the
                   storage box is also accepted.
  [target_vmid]    Optional. Picks the mode:
                     omitted / equal to backed-up VMID -> IN-PLACE:
                       overwrite that VM's datasets, restore the backed-up config
                       (keeps VMID/MAC/UUIDs). VM must be STOPPED.
                     different VMID, and it ALREADY EXISTS -> DATA-RESTORE:
                       overwrite only that VM's disk DATA with the backup, keep its
                       existing config & identity (MAC/UUIDs/hookscript/firewall).
                       Disk layout must match the backup. VM must be STOPPED.
                     different VMID, and it is FREE -> NEW VM:
                       receive into fresh rpool/data/vm-<target>-disk-* datasets,
                       regenerate vmgenid + smbios1 uuid.
                       With KEEP_UUIDS=1 -> RELOCATE: same but keep the original
                       MAC + vmgenid + smbios1 uuid (no reactivation).
  [node]           Must match a name under /etc/pve/nodes/ (often `hostname -s`).
                   If omitted, picks the first of hostname -s / hostname with qemu-server/.

Environment:
  STORAGE_BOX_HOST, STORAGE_BOX_USER, STORAGE_BOX_PORT
  STORAGE_BOX_BACKUP_PATH  (default /home/backups)
  ZFS_PARENT               (default rpool/data)
  VM_NAME                  override guest name (default: keep backed-up name)
  KEEP_UUIDS=1             when restoring to a DIFFERENT VMID, keep the VM's
                           identity (MAC, vmgenid, smbios1 uuid) instead of
                           regenerating. Only run one of the original / restored
                           VM at a time or they will conflict.
EOF
}

[[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && {
  usage
  exit 0
}

DEFAULT_STORAGE_BOX_HOST="u560363.your-storagebox.de"
DEFAULT_STORAGE_BOX_USER="u560363"
DEFAULT_STORAGE_BOX_PORT="23"
DEFAULT_STORAGE_BOX_BACKUP_PATH="/home/backups"

STORAGE_BOX_HOST="${STORAGE_BOX_HOST:-$DEFAULT_STORAGE_BOX_HOST}"
STORAGE_BOX_USER="${STORAGE_BOX_USER:-$DEFAULT_STORAGE_BOX_USER}"
STORAGE_BOX_PORT="${STORAGE_BOX_PORT:-$DEFAULT_STORAGE_BOX_PORT}"
STORAGE_BOX_BACKUP_PATH="${STORAGE_BOX_BACKUP_PATH:-$DEFAULT_STORAGE_BOX_BACKUP_PATH}"
ZFS_PARENT="${ZFS_PARENT:-rpool/data}"

[[ -n "$STORAGE_BOX_HOST" && -n "$STORAGE_BOX_USER" ]] || die "STORAGE_BOX_HOST and STORAGE_BOX_USER are required"

need_cmd ssh
need_cmd zstd
need_cmd zfs
need_cmd awk
need_cmd sort
need_cmd cut
need_cmd mktemp
need_cmd python3
need_cmd sed
need_cmd head
need_cmd wc

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  die "Run as root on a Proxmox node"
fi

SSH_BASE=(
  ssh
  -n
  -p "$STORAGE_BOX_PORT"
  -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null
  "${STORAGE_BOX_USER}@${STORAGE_BOX_HOST}"
)

BACKUP_ROOT="${STORAGE_BOX_BACKUP_PATH%/}"

# --list: show backup folders on the storage box and exit.
if [[ "${1:-}" == "--list" ]]; then
  echo "Backups under ${STORAGE_BOX_USER}@${STORAGE_BOX_HOST}:${BACKUP_ROOT}"
  "${SSH_BASE[@]}" "ls -1 '${BACKUP_ROOT}' 2>/dev/null" \
    || die "Could not list ${BACKUP_ROOT} on the storage box"
  exit 0
fi

ARG_FOLDER="${1:-}"
ARG_TARGET_VMID="${2:-}"
ARG_NODE="${3:-}"

[[ -n "$ARG_FOLDER" ]] || {
  usage
  die "Missing <backup_folder> (use --list to see available backups)"
}

# Accept an absolute path or a bare folder name under the backup root.
if [[ "$ARG_FOLDER" == /* ]]; then
  REMOTE_BASE="${ARG_FOLDER%/}"
else
  REMOTE_BASE="${BACKUP_ROOT}/${ARG_FOLDER%/}"
fi
REMOTE_CONFIG="${REMOTE_BASE}/config.conf"

NODE="$(resolve_pve_node_name "$ARG_NODE")"
echo "Proxmox node directory: /etc/pve/nodes/${NODE}/ (pass [node] as 3rd arg if this is wrong)"

echo "Fetching backup config: ${REMOTE_CONFIG}"
CONFIG_TMP="$(mktemp)"
TOKENS_TMP=""
FW_TMP=""
trap 'rm -f "$CONFIG_TMP" "${TOKENS_TMP:-}" "${FW_TMP:-}"' EXIT

if ! "${SSH_BASE[@]}" "cat '$REMOTE_CONFIG'" >"$CONFIG_TMP"; then
  die "Failed to fetch backup config (is '${ARG_FOLDER}' a valid backup folder? try --list)"
fi
[[ -s "$CONFIG_TMP" ]] || die "Empty backup config: ${REMOTE_CONFIG}"

mapfile -t REMOTE_TOKENS < <(pve_sorted_disk_tokens "$CONFIG_TMP")
[[ "${#REMOTE_TOKENS[@]}" -gt 0 ]] || die "No disk tokens in backup config (scsi|virtio|sata|ide|efidisk0|tpmstate0)"

# Stream count on the box must match the number of disk tokens (no dedupe — matches the writer).
REMOTE_LIST="$("${SSH_BASE[@]}" "ls -1 '${REMOTE_BASE}'/disk*.stream.zst 2>/dev/null" || true)"
REMOTE_STREAM_COUNT="$(printf '%s\n' "$REMOTE_LIST" | grep -c '\.stream\.zst$' || true)"
[[ "$REMOTE_STREAM_COUNT" =~ ^[0-9]+$ ]] || REMOTE_STREAM_COUNT=0
[[ "$REMOTE_STREAM_COUNT" -gt 0 ]] || die "No disk*.stream.zst under ${REMOTE_BASE} on the storage box"
if [[ "${#REMOTE_TOKENS[@]}" -ne "$REMOTE_STREAM_COUNT" ]]; then
  die "Disk tokens in config (${#REMOTE_TOKENS[@]}) != stream files on storage (${REMOTE_STREAM_COUNT}). Backup is incomplete or mismatched."
fi

# VMID the backup was taken from (from the first disk volume token).
ORIG_VMID="$(sed -nE 's/.*vm-([0-9]+)-disk-[0-9]+.*/\1/p' <<<"${REMOTE_TOKENS[0]}" | head -n1)"
[[ -n "$ORIG_VMID" ]] || die "Could not determine original VMID from token '${REMOTE_TOKENS[0]}'"

# Decide target VMID.
if [[ -n "$ARG_TARGET_VMID" ]]; then
  [[ "$ARG_TARGET_VMID" =~ ^[0-9]+$ ]] || die "<target_vmid> must be digits, got: ${ARG_TARGET_VMID}"
  TARGET_VMID=$((10#$ARG_TARGET_VMID))
else
  TARGET_VMID=$((10#$ORIG_VMID))
fi
if [[ "$TARGET_VMID" -lt 100 || "$TARGET_VMID" -gt 999999999 ]]; then
  die "target VMID must be between 100 and 999999999, got: ${TARGET_VMID}"
fi

# KEEP_UUIDS=1 keeps the VM's identity (MAC, vmgenid, smbios1 uuid) even when
# restoring to a different VMID — a "relocate", not a clone. Only affects the
# new-vm path (no effect for in-place or data-restore).
KEEP_UUIDS="${KEEP_UUIDS:-0}"
VM_NAME="${VM_NAME:-}"

shopt -s nullglob
EXISTING_CONFS=(/etc/pve/nodes/*/qemu-server/${TARGET_VMID}.conf)
shopt -u nullglob

# Mode:
#   in-place     target == backup VMID            -> overwrite that VM, restore backup config
#   data-restore target != backup VMID, EXISTS    -> overwrite that VM's disk DATA only,
#                                                    keep its existing config & identity
#   relocate     target != backup VMID, free, KEEP_UUIDS=1 -> new VM, keep backup identity
#   new-vm       target != backup VMID, free      -> new VM, regenerated vmgenid/smbios uuid
if [[ "$TARGET_VMID" -eq "$ORIG_VMID" ]]; then
  MODE="in-place"
  REGEN_UUIDS=0
elif [[ "${#EXISTING_CONFS[@]}" -gt 0 ]]; then
  MODE="data-restore"
  REGEN_UUIDS=0
elif [[ "$KEEP_UUIDS" == "1" ]]; then
  MODE="relocate"
  REGEN_UUIDS=0
else
  MODE="new-vm"
  REGEN_UUIDS=1
fi

# Resolve the node + config path. data-restore uses the target VM's existing
# node/config; the other modes write into the resolved local node.
if [[ "$MODE" == "data-restore" ]]; then
  CONF_PATH="${EXISTING_CONFS[0]}"
  TARGET_NODE="$(sed -nE 's#^/etc/pve/nodes/([^/]+)/qemu-server/[0-9]+\.conf$#\1#p' <<<"$CONF_PATH")"
  [[ -n "$TARGET_NODE" ]] || die "Could not parse node from existing config path: ${CONF_PATH}"
else
  TARGET_NODE="$NODE"
  CONF_PATH="/etc/pve/nodes/${TARGET_NODE}/qemu-server/${TARGET_VMID}.conf"
fi

# Per-mode existence guard.
if [[ "$MODE" == "new-vm" || "$MODE" == "relocate" ]]; then
  [[ "${#EXISTING_CONFS[@]}" -eq 0 ]] \
    || die "Refusing ${MODE^^} restore: VMID ${TARGET_VMID} already exists: ${EXISTING_CONFS[0]}"
  if [[ "$MODE" == "relocate" ]]; then
    shopt -s nullglob
    ORIG_CONFS=(/etc/pve/nodes/*/qemu-server/${ORIG_VMID}.conf)
    shopt -u nullglob
    if [[ "${#ORIG_CONFS[@]}" -gt 0 ]]; then
      echo "WARNING: KEEP_UUIDS relocate keeps the original MAC + vmgenid + smbios uuid," >&2
      echo "         but VMID ${ORIG_VMID} still exists (${ORIG_CONFS[0]}). Running BOTH will" >&2
      echo "         cause MAC/identity conflicts — only run one. Retire VMID ${ORIG_VMID} first." >&2
    fi
  fi
fi

# Modes that overwrite an existing VM's zvols require it to be STOPPED.
if [[ "$MODE" == "in-place" || "$MODE" == "data-restore" ]]; then
  if command -v qm >/dev/null 2>&1; then
    QM_STATUS="$(qm status "$TARGET_VMID" 2>/dev/null || true)"
    if grep -qi 'status: running' <<<"$QM_STATUS"; then
      die "VM ${TARGET_VMID} is RUNNING. Stop it first (qm stop ${TARGET_VMID}) — restore overwrites its disks."
    fi
  fi
  if [[ -f "/run/qemu-server/${TARGET_VMID}.pid" ]]; then
    die "VM ${TARGET_VMID} appears to be running (/run/qemu-server/${TARGET_VMID}.pid). Stop it first."
  fi
fi

n="${#REMOTE_TOKENS[@]}"

# Resolve the n local datasets that stream i (disk{i}.stream.zst) is received into.
LOCAL_DATASETS=()
if [[ "$MODE" == "data-restore" ]]; then
  # Keep the target VM's config; only overwrite its disk DATA. Map streams to the
  # target's *current* datasets by the same sorted-disk-key order the backup used,
  # and require the disk-key layout to line up so each stream lands in its role.
  mapfile -t LOCAL_KEYS < <(pve_sorted_disk_keys "$CONF_PATH")
  mapfile -t LOCAL_TOKENS < <(pve_sorted_disk_tokens "$CONF_PATH")
  mapfile -t BACKUP_KEYS < <(pve_sorted_disk_keys "$CONFIG_TMP")
  if [[ "${#LOCAL_TOKENS[@]}" -ne "$n" ]]; then
    die "data-restore: target VM ${TARGET_VMID} has ${#LOCAL_TOKENS[@]} disk(s) but backup has ${n}. Disk layout differs — refusing (use new-vm/relocate to restore as a separate VM)."
  fi
  for ((i = 0; i < n; i++)); do
    if [[ "${LOCAL_KEYS[i]}" != "${BACKUP_KEYS[i]}" ]]; then
      die "data-restore: disk-key mismatch at position ${i} (target '${LOCAL_KEYS[i]}' vs backup '${BACKUP_KEYS[i]}'). Layouts differ — refusing."
    fi
  done
  ZFS_LIST="$(zfs list -H -o name 2>/dev/null || true)"
  [[ -n "$ZFS_LIST" ]] || die "data-restore: 'zfs list' returned nothing"
  for ((i = 0; i < n; i++)); do
    ds="$(map_token_to_dataset "${LOCAL_TOKENS[i]}" "$ZFS_LIST")" \
      || die "data-restore: cannot map disk '${LOCAL_KEYS[i]}' volume '${LOCAL_TOKENS[i]}' to a ZFS dataset"
    LOCAL_DATASETS+=("$ds")
  done
else
  for ((i = 0; i < n; i++)); do
    LOCAL_DATASETS+=("${ZFS_PARENT}/vm-${TARGET_VMID}-disk-${i}")
  done
fi

echo "Mode: ${MODE} | backup VMID=${ORIG_VMID} -> target VMID=${TARGET_VMID} | node=${TARGET_NODE} | ${n} disk stream(s)"
case "$MODE" in
  in-place)
    echo "IN-PLACE restore will OVERWRITE ${ZFS_PARENT}/vm-${TARGET_VMID}-disk-0..$((n - 1)) and replace ${CONF_PATH}"
    ;;
  data-restore)
    echo "DATA-RESTORE will OVERWRITE the disk DATA of VM ${TARGET_VMID} (keeping its config/MAC/UUIDs):"
    for ((i = 0; i < n; i++)); do
      echo "  ${BACKUP_KEYS[i]} <- backup disk${i}  ->  ${LOCAL_DATASETS[i]}"
    done
    echo "  (config ${CONF_PATH} is NOT modified; backup's firewall is ignored)"
    ;;
esac

# Stream each disk: ssh storagebox "dd if=... bs=4M" | zstd -d | zfs receive -F <local_ds>
for ((i = 0; i < n; i++)); do
  local_ds="${LOCAL_DATASETS[i]}"
  remote_file="${REMOTE_BASE}/disk${i}.stream.zst"
  echo "[${i}/$((n - 1))] ${remote_file} -> ${local_ds}"

  # Destroy snapshots on the target so zfs receive -F can replace the dataset.
  while IFS= read -r snap; do
    snap="${snap//[$'\r']}"
    [[ -z "$snap" || "$snap" != *"@"* ]] && continue
    zfs destroy "$snap" || true
  done < <(zfs list -H -o name -t snapshot -S creation -r "$local_ds" 2>/dev/null || true)

  "${SSH_BASE[@]}" "dd if='${remote_file}' bs=4M" \
    | zstd -d \
    | zfs receive -F "$local_ds"

  zfs set refreservation=none "$local_ds" || true
done

# Destroy the @backup snapshot carried in the received stream, if present.
for ((i = 0; i < n; i++)); do
  local_ds="${LOCAL_DATASETS[i]}"
  while IFS= read -r snap; do
    snap="${snap//[$'\r']}"
    [[ -z "$snap" || "$snap" != *"@"* ]] && continue
    zfs destroy "$snap" || true
  done < <(zfs list -H -o name -t snapshot -S creation -r "$local_ds" 2>/dev/null || true)
done

if [[ "$MODE" == "data-restore" ]]; then
  echo "Disk data restored into VM ${TARGET_VMID}. Config left untouched: ${CONF_PATH}"
  echo "NOTE: zvol sizes now match the backup (VMID ${ORIG_VMID}); the target's config"
  echo "      disk 'size=' is only metadata. Resize later with 'qm disk resize' if needed."
  echo "Restore finished (${MODE}). VMID=${TARGET_VMID}"
  echo "VM left STOPPED. Start it when ready: qm start ${TARGET_VMID}"
  exit 0
fi

# Rewrite + install VM config from the backup.
TOKENS_TMP="$(mktemp)"
printf '%s\n' "${REMOTE_TOKENS[@]}" >"$TOKENS_TMP"
if ! rewrite_backup_vm_config "$CONFIG_TMP" "$TARGET_VMID" "$VM_NAME" "$TOKENS_TMP" "$REGEN_UUIDS"; then
  die "rewrite_backup_vm_config (python3) failed — VM config not updated"
fi
rm -f "$TOKENS_TMP"
TOKENS_TMP=""
[[ -s "$CONFIG_TMP" ]] || die "Rewrite left empty config at $CONFIG_TMP"

install_pve_cfg_file "$CONFIG_TMP" "$CONF_PATH"
echo "Wrote VM config: ${CONF_PATH}"

# Optional firewall (rewrite original VMID -> target VMID in the text).
REMOTE_FW="${REMOTE_BASE}/firewall.fw"
FW_TMP="$(mktemp)"
if "${SSH_BASE[@]}" "cat '$REMOTE_FW'" >"$FW_TMP" 2>/dev/null && [[ -s "$FW_TMP" ]]; then
  if [[ "$ORIG_VMID" != "$TARGET_VMID" ]]; then
    sed -i "s/${ORIG_VMID}/${TARGET_VMID}/g" "$FW_TMP"
  fi
  mkdir -p /etc/pve/firewall
  install_pve_cfg_file "$FW_TMP" "/etc/pve/firewall/${TARGET_VMID}.fw"
  echo "Wrote firewall: /etc/pve/firewall/${TARGET_VMID}.fw"
else
  echo "No firewall file (optional): ${REMOTE_FW}"
fi

echo "Restore finished (${MODE}). VMID=${TARGET_VMID} config=${CONF_PATH}"
echo "VM left STOPPED. Start it when ready: qm start ${TARGET_VMID}"

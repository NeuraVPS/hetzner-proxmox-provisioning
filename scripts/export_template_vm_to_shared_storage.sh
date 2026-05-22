#!/usr/bin/env bash
# curl | bash -s feeds the script on stdin (fd 0). Re-exec from a temp file so ssh never reads the script stream.
if [[ ! -t 0 ]] && [[ -z "${BASH_SOURCE[0]:-}" ]]; then
  _export_tmp="$(mktemp)"
  if ! cat >"$_export_tmp"; then
    echo "ERROR: failed to read script from stdin" >&2
    exit 1
  fi
  if [[ ! -s "$_export_tmp" ]]; then
    echo "ERROR: empty script from stdin" >&2
    exit 1
  fi
  exec bash "$_export_tmp" "$@" </dev/null
fi
set -euo pipefail

# --- Run from curl (on the Proxmox node, as root) ---
# Inverse of restore_template_vm_from_shared_storage.sh.
# Takes a VMID on this node, snapshots its disks, and streams them as
# disk*.stream.zst (one file per unique volume, in sorted-disk-key order)
# to the storage box under <STORAGE_BOX_BASE_PATH>/<template_key>/,
# along with the VM's config.conf and (if present) firewall.fw.
#
# Layout produced on the storage box matches what create_vm_from_storagebox
# and restore_template_vm_from_shared_storage.sh expect:
#   <STORAGE_BOX_BASE_PATH>/<template_key>/
#     config.conf
#     firewall.fw            (optional)
#     disk0.stream.zst       (efidisk0 / first sorted-by-key)
#     disk1.stream.zst       (scsi0 / next)
#     disk2.stream.zst       (tpmstate0 / next)
#     ...                    (one per unique volume)
#
# You MUST pipe curl into bash: curl ... | bash -s -- args
# If you omit the "|", curl treats "bash", "--", "100", "windows-en" as extra URLs to fetch
# and you get errors like: curl: (6) Could not resolve host: bash
#
# Example (cache-bust the raw URL so you always get the latest revision):
#   curl -fsSL "https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/scripts/export_template_vm_to_shared_storage.sh?t=$(date +%s)" \
#     | bash -s -- <vmid> <template_key> [node]
#
# Concrete examples:
#   curl -fsSL "https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/scripts/export_template_vm_to_shared_storage.sh?t=$(date +%s)" | bash -s -- 100 windows-en
#   curl -fsSL "https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/scripts/export_template_vm_to_shared_storage.sh?t=$(date +%s)" | OVERWRITE=1 bash -s -- 100 windows-es
#   curl -fsSL "https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/scripts/export_template_vm_to_shared_storage.sh?t=$(date +%s)" | OVERWRITE=1 bash -s -- 101 windows-en
#
# IMPORTANT: env vars (OVERWRITE, ALLOW_RUNNING_VM, KEEP_SNAPSHOT, STORAGE_BOX_*) must go
# AFTER the pipe, prefixing `bash`. `VAR=x curl ... | bash` attaches VAR to curl only —
# bash sees no value. Use `curl ... | VAR=x bash` instead.
#
# Pre-requisite: the VM must be SHUT DOWN (post-sysprep state is ideal). Running VMs are
# refused by default to avoid capturing a torn filesystem state; set ALLOW_RUNNING_VM=1
# to override (NOT recommended for templates).

die() {
  echo "ERROR: $*" >&2
  exit 1
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"
}

# Third arg [node] must match a directory under /etc/pve/nodes/ (often hostname -s, not FQDN).
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
  die "Could not find local node under /etc/pve/nodes/*/qemu-server (try: $0 ... <vmid> <template_key> <node>)."
}

# Same as restore_template_vm_from_shared_storage.sh / proxmox_client._parse_vm_config_disk_tokens:
# one volume leaf per disk key, sorted by key. Both scripts MUST agree on order so the
# disk<i>.stream.zst indexing is consistent between export and restore.
pve_sorted_disk_tokens() {
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
  ' "$f" | LC_ALL=C sort -t $'\t' -k1,1 | cut -f2
}

# Same volume can appear on multiple config lines; we want one stream per unique volume.
dedupe_tokens_first_seen() {
  awk '!seen[$0]++'
}

usage() {
  cat <<'EOF'
Export a Proxmox VM as a template to the shared storage box (inverse of
restore_template_vm_from_shared_storage.sh).

Usage:
  export_template_vm_to_shared_storage.sh <vmid> <template_key> [node]

  <vmid>          VMID on this node (e.g. 100). Must be SHUT DOWN.
  <template_key>  Subdirectory under STORAGE_BOX_BASE_PATH (e.g. windows-en, windows-es).
                  Only [A-Za-z0-9_-] allowed.
  [node]          Must match a name under /etc/pve/nodes/ (often `hostname -s`).
                  If omitted, picks the first of hostname -s / hostname that has qemu-server/.

Run from GitHub via curl|bash (no checkout needed; the ?t=... cache-busts so you
always pull the latest master revision):

  curl -fsSL "https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/scripts/export_template_vm_to_shared_storage.sh?t=$(date +%s)" \
    | bash -s -- <vmid> <template_key> [node]

  Concrete examples:
    curl -fsSL "https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/scripts/export_template_vm_to_shared_storage.sh?t=$(date +%s)" | bash -s -- 100 windows-en
    curl -fsSL "https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/scripts/export_template_vm_to_shared_storage.sh?t=$(date +%s)" | OVERWRITE=1 bash -s -- 100 windows-en

  IMPORTANT 1: you MUST pipe curl into bash with `| bash -s -- args`. If you omit
  the "|", curl treats "bash", "--", "100", "windows-en" as extra URLs to fetch
  and fails with: curl: (6) Could not resolve host: bash

  IMPORTANT 2: env vars (OVERWRITE, ALLOW_RUNNING_VM, KEEP_SNAPSHOT, STORAGE_BOX_*)
  must go AFTER the pipe, prefixing `bash`. `VAR=x curl ... | bash` attaches VAR
  to curl only — bash sees no value. Use `curl ... | VAR=x bash` instead.

Environment:
  STORAGE_BOX_HOST, STORAGE_BOX_USER, STORAGE_BOX_PORT, STORAGE_BOX_BASE_PATH (default /home/templates)
  ZFS_PARENT          default: rpool/data
  SNAPSHOT_NAME       default: stable   (the snapshot tag created on each volume before send)
  ALLOW_RUNNING_VM    default: 0        (set 1 to allow exporting a running VM — NOT recommended)
  OVERWRITE           default: 0        (set 1 to replace an existing template under template_key)
  KEEP_SNAPSHOT       default: 1        (set 0 to destroy the local @<SNAPSHOT_NAME> snapshots after upload)
EOF
}

[[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && {
  usage
  exit 0
}

REQUEST_VMID_RAW="${1:-}"
TEMPLATE_KEY="${2:-}"
ARG_NODE="${3:-}"

[[ -n "$REQUEST_VMID_RAW" ]] || {
  usage
  die "Missing <vmid>"
}
[[ -n "$TEMPLATE_KEY" ]] || {
  usage
  die "Missing <template_key>"
}

if [[ ! "$REQUEST_VMID_RAW" =~ ^[0-9]+$ ]]; then
  die "<vmid> must be digits, got: ${REQUEST_VMID_RAW}"
fi
VMID=$((10#$REQUEST_VMID_RAW))
if [[ "$VMID" -lt 100 || "$VMID" -gt 999999999 ]]; then
  die "<vmid> must be between 100 and 999999999, got: ${VMID}"
fi

# Sanitize template_key — it becomes a remote directory name, so no slashes / shell metas
if ! [[ "$TEMPLATE_KEY" =~ ^[A-Za-z0-9_-]+$ ]]; then
  die "<template_key> may only contain [A-Za-z0-9_-], got: ${TEMPLATE_KEY}"
fi

DEFAULT_STORAGE_BOX_HOST="u560363.your-storagebox.de"
DEFAULT_STORAGE_BOX_USER="u560363"
DEFAULT_STORAGE_BOX_PORT="23"
DEFAULT_STORAGE_BOX_BASE_PATH="/home/templates"

STORAGE_BOX_HOST="${STORAGE_BOX_HOST:-$DEFAULT_STORAGE_BOX_HOST}"
STORAGE_BOX_USER="${STORAGE_BOX_USER:-$DEFAULT_STORAGE_BOX_USER}"
STORAGE_BOX_PORT="${STORAGE_BOX_PORT:-$DEFAULT_STORAGE_BOX_PORT}"
STORAGE_BOX_BASE_PATH="${STORAGE_BOX_BASE_PATH:-$DEFAULT_STORAGE_BOX_BASE_PATH}"
ZFS_PARENT="${ZFS_PARENT:-rpool/data}"
SNAPSHOT_NAME="${SNAPSHOT_NAME:-stable}"
ALLOW_RUNNING_VM="${ALLOW_RUNNING_VM:-0}"
OVERWRITE="${OVERWRITE:-0}"
KEEP_SNAPSHOT="${KEEP_SNAPSHOT:-1}"

[[ -n "$STORAGE_BOX_HOST" && -n "$STORAGE_BOX_USER" ]] || die "STORAGE_BOX_HOST and STORAGE_BOX_USER are required"

need_cmd ssh
need_cmd scp
need_cmd zstd
need_cmd zfs
need_cmd qm
need_cmd awk
need_cmd sort
need_cmd cut
need_cmd mktemp

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  die "Run as root on a Proxmox node"
fi

NODE="$(resolve_pve_node_name "$ARG_NODE")"
echo "Proxmox node directory: /etc/pve/nodes/${NODE}/ (pass [node] as 3rd arg if this is wrong)"

CONF_PATH="/etc/pve/nodes/${NODE}/qemu-server/${VMID}.conf"
FW_PATH="/etc/pve/firewall/${VMID}.fw"

[[ -f "$CONF_PATH" ]] || die "VM config not found: ${CONF_PATH}"

# Refuse to export a running VM — block-level snapshots of a running guest's
# disk can capture mid-flight filesystem state. Sysprep'd templates are expected
# to be in the post-shutdown state.
VM_STATUS=""
if qm_status_out="$(qm status "$VMID" 2>&1)"; then
  VM_STATUS="$(awk -F': ' '/^status:/ {print $2}' <<<"$qm_status_out" | tr -d '[:space:]' | head -c 32)"
fi
if [[ "$VM_STATUS" == "running" ]]; then
  if [[ "$ALLOW_RUNNING_VM" != "1" ]]; then
    die "VM ${VMID} is running. Stop it first (qm shutdown ${VMID}) or set ALLOW_RUNNING_VM=1 (NOT recommended — guest disks may be in inconsistent state)."
  fi
  echo "WARN: VM ${VMID} is running but ALLOW_RUNNING_VM=1 — proceeding"
fi
echo "VM ${VMID} status: ${VM_STATUS:-unknown}"

# Parse disk tokens (same order as restore expects)
mapfile -t TOKENS < <(pve_sorted_disk_tokens "$CONF_PATH" | dedupe_tokens_first_seen)
[[ "${#TOKENS[@]}" -gt 0 ]] || die "No disk tokens in VM config: ${CONF_PATH}"

n="${#TOKENS[@]}"
echo "VM ${VMID} has ${n} unique volume(s):"
for ((i = 0; i < n; i++)); do
  echo "  disk${i}.stream.zst <- ${TOKENS[$i]}"
done

# Validate each volume exists as a ZFS dataset under ZFS_PARENT
for tok in "${TOKENS[@]}"; do
  ds="${ZFS_PARENT}/${tok}"
  zfs list -H -o name "$ds" >/dev/null 2>&1 || die "ZFS dataset not found: ${ds} (is ZFS_PARENT=${ZFS_PARENT} correct?)"
done

# SSH-with-stdin-blocked (for commands that don't read stdin: mkdir, ls, rm, cat-of-remote)
SSH_BASE=(
  ssh
  -n
  -p "$STORAGE_BOX_PORT"
  -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null
  "${STORAGE_BOX_USER}@${STORAGE_BOX_HOST}"
)

# SSH-with-stdin-passthrough (for the zfs-send pipeline)
SSH_PIPE=(
  ssh
  -p "$STORAGE_BOX_PORT"
  -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null
  "${STORAGE_BOX_USER}@${STORAGE_BOX_HOST}"
)

# SCP for small file uploads (config, firewall) — more reliable than
# `ssh ... "cat > file" <local` which can race with stdin-from-file under curl|bash.
SCP_BASE=(
  scp
  -P "$STORAGE_BOX_PORT"
  -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null
)

base="${STORAGE_BOX_BASE_PATH%/}"
REMOTE_BASE="${base}/${TEMPLATE_KEY}"

# Refuse to overwrite an existing template unless OVERWRITE=1
if "${SSH_BASE[@]}" "ls -1 '${REMOTE_BASE}'/disk*.stream.zst 2>/dev/null | head -n1" 2>/dev/null | grep -q .; then
  if [[ "$OVERWRITE" != "1" ]]; then
    die "Template '${TEMPLATE_KEY}' already exists at ${REMOTE_BASE} on the storage box. Set OVERWRITE=1 to replace it."
  fi
  echo "WARN: Overwriting existing template at ${REMOTE_BASE}"
fi

# Create the remote directory; clean previous streams if overwriting (avoids stale disk<N> files
# from a template that previously had more volumes)
echo "Preparing remote dir: ${REMOTE_BASE}"
"${SSH_BASE[@]}" "mkdir -p '${REMOTE_BASE}'" || die "Could not create remote dir ${REMOTE_BASE}"
if [[ "$OVERWRITE" == "1" ]]; then
  "${SSH_BASE[@]}" "rm -f '${REMOTE_BASE}'/disk*.stream.zst '${REMOTE_BASE}/config.conf' '${REMOTE_BASE}/firewall.fw'" \
    || die "Failed to clean previous template files at ${REMOTE_BASE}"
fi

# Take snapshots atomically across all the VM's datasets. A single `zfs snapshot`
# invocation with multiple targets is atomic. We refresh any pre-existing snapshot
# with the same name first (so re-running export produces a fresh baseline).
SNAP_LIST=()
DESTROY_ON_EXIT=()
for tok in "${TOKENS[@]}"; do
  ds="${ZFS_PARENT}/${tok}"
  snap="${ds}@${SNAPSHOT_NAME}"
  if zfs list -H -o name "$snap" >/dev/null 2>&1; then
    echo "  Destroying pre-existing ${snap}"
    zfs destroy "$snap" || die "Could not destroy old snapshot ${snap}"
  fi
  SNAP_LIST+=("$snap")
done

echo "Creating snapshots: ${SNAP_LIST[*]}"
zfs snapshot "${SNAP_LIST[@]}" || die "zfs snapshot failed"

# On any error after this point, destroy the snapshots we created so re-runs are clean.
# (If everything succeeds and KEEP_SNAPSHOT=1, we skip the destroy at the end.)
cleanup_partial() {
  local s
  for s in "${SNAP_LIST[@]}"; do
    zfs destroy "$s" 2>/dev/null || true
  done
}
trap cleanup_partial ERR

# Stream each volume in canonical order
for ((i = 0; i < n; i++)); do
  tok="${TOKENS[$i]}"
  ds="${ZFS_PARENT}/${tok}"
  snap="${ds}@${SNAPSHOT_NAME}"
  remote_file="${REMOTE_BASE}/disk${i}.stream.zst"
  echo "[${i}/$((n - 1))] ${snap} -> ${remote_file}"
  zfs send "$snap" \
    | zstd -T0 \
    | "${SSH_PIPE[@]}" "dd of='${remote_file}' bs=4M conv=fsync status=none"
done

# Upload config.conf — keep the source VM's config as-is; restore_template_vm_from_shared_storage.sh
# rewrites disk tokens and identity fields (vmgenid/smbios1 uuid) at restore time.
echo "Uploading config: ${CONF_PATH} -> ${REMOTE_BASE}/config.conf"
"${SCP_BASE[@]}" "$CONF_PATH" "${STORAGE_BOX_USER}@${STORAGE_BOX_HOST}:${REMOTE_BASE}/config.conf" \
  || die "scp of config.conf failed"

# Upload firewall.fw if the VM has one. If OVERWRITE=1 and the source has no firewall,
# we already removed any stale remote firewall.fw above.
if [[ -f "$FW_PATH" ]]; then
  echo "Uploading firewall: ${FW_PATH} -> ${REMOTE_BASE}/firewall.fw"
  "${SCP_BASE[@]}" "$FW_PATH" "${STORAGE_BOX_USER}@${STORAGE_BOX_HOST}:${REMOTE_BASE}/firewall.fw" \
    || die "scp of firewall.fw failed"
else
  echo "No local firewall file (${FW_PATH}); skipping"
fi

# Verify the uploads actually landed on the box (catches the case where the original
# `cat > file <localfile` path silently created empty files under curl|bash).
echo "Verifying remote uploads..."
REMOTE_LISTING="$("${SSH_BASE[@]}" "ls -la '${REMOTE_BASE}/'" 2>&1)" || die "Could not list remote dir for verification"
echo "${REMOTE_LISTING}"
"${SSH_BASE[@]}" "[ -s '${REMOTE_BASE}/config.conf' ]" \
  || die "Remote config.conf is missing or empty after upload"
if [[ -f "$FW_PATH" ]]; then
  "${SSH_BASE[@]}" "[ -s '${REMOTE_BASE}/firewall.fw' ]" \
    || die "Remote firewall.fw is missing or empty after upload"
fi

# Clear the ERR trap — we're past the streaming phase, partial-state cleanup no longer applies
trap - ERR

# Optionally destroy the local snapshots we created
if [[ "$KEEP_SNAPSHOT" != "1" ]]; then
  echo "Destroying local snapshots (KEEP_SNAPSHOT=0)..."
  for s in "${SNAP_LIST[@]}"; do
    zfs destroy "$s" || echo "WARN: failed to destroy ${s}"
  done
else
  echo "Retaining local snapshots: ${SNAP_LIST[*]}"
fi

echo
echo "Export finished:"
echo "  template_key: ${TEMPLATE_KEY}"
echo "  remote path:  ${REMOTE_BASE}/"
echo "  streams:      ${n}"
echo "  config:       ${REMOTE_BASE}/config.conf"
[[ -f "$FW_PATH" ]] && echo "  firewall:     ${REMOTE_BASE}/firewall.fw"
echo
echo "Restore this template on any node with:"
echo "  curl -fsSL \"https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/scripts/restore_template_vm_from_shared_storage.sh?t=\$(date +%s)\" | bash -s -- ${TEMPLATE_KEY} <new_vmid>"

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
# The block at the top of this file re-executes the script from a temp file when
# stdin is a pipe (e.g. curl), so tools like ssh/zstd/zfs still see a normal script file.
#
# You MUST pipe curl into bash: curl ... | bash -s -- args
# If you omit the "|", curl treats "bash", "--", "windows-es", "100" as extra URLs to fetch
# and you get errors like: curl: (6) Could not resolve host: bash
#
# Example (cache-bust the raw URL so you always get the latest revision):
#   curl -fsSL "https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/scripts/restore_template_vm_from_shared_storage.sh?t=$(date +%s)" \
#     | bash -s -- <template_key> <vmid> [node]
#
# Concrete example:
#   curl -fsSL "https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/scripts/restore_template_vm_from_shared_storage.sh?t=$(date +%s)" | bash -s -- windows-es 100
#   curl -fsSL "https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/scripts/restore_template_vm_from_shared_storage.sh?t=$(date +%s)" | bash -s -- windows-en 101
#
# With storage-box overrides (optional; script has defaults):
#   curl -fsSL "https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/scripts/restore_template_vm_from_shared_storage.sh?t=$(date +%s)" \
#     | STORAGE_BOX_HOST=... STORAGE_BOX_USER=... bash -s -- windows-es 100
#
# IMPORTANT: env vars must go AFTER the pipe, prefixing `bash`. Putting them before
# `curl` (e.g. `STORAGE_BOX_HOST=... curl ... | bash`) attaches them to curl only —
# bash never sees them. Use `curl ... | VAR=x bash` instead.
#
# Optional third argument [node] defaults to $(hostname); set ZFS_PARENT / VM_NAME in the environment if needed.

# Restore a template VM from storage (same flow as NeuraVPS functions/proxmox_client.py::create_vm_from_storagebox).
# - Start from _parse_vm_config_disk_tokens order (sorted PVE disk keys), then drop duplicate volume
#   tokens (efidisk0 + scsi0 often share the same vm-*-disk-* leaf). Streams on the box are one file
#   per unique volume: disk0.stream.zst … disk{n-1}.stream.zst — n must match that count.
# - local_ds = <ZFS_PARENT>/vm-<vmid>-disk-<i> for i in 0..n-1 (NOT the template’s old leaf names)
# - token_to_local + _rewrite_vm_config_for_clone (embedded python3)

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
  die "Could not find local node under /etc/pve/nodes/*/qemu-server (try: $0 ... <vmid> <node>). hostname -s=$(hostname -s 2>/dev/null) hostname=$(hostname 2>/dev/null)"
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

# Same as proxmox_client._parse_vm_config_disk_tokens: one volume leaf per disk key, sorted by key.
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

# Same volume can appear on multiple config lines; template backup has one stream per unique volume.
dedupe_tokens_first_seen() {
  awk '!seen[$0]++'
}

# proxmox_client._rewrite_vm_config_for_clone (subset: needs python3 + uuid)
# Tokens must be passed as a file: a heredoc would occupy stdin (cannot pipe tokens into python -).
rewrite_vm_config_like_create_vm() {
  local config_path="$1"
  local next_vmid="$2"
  local vm_name="$3"
  local tokens_file="$4"
  [[ -s "$tokens_file" ]] || die "tokens file missing or empty: $tokens_file"
  python3 - "$config_path" "$next_vmid" "$vm_name" "$tokens_file" <<'PY'
import re, sys, uuid

def parse_conf(text: str) -> dict:
    result = {}
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        idx = line.index(":")
        key = line[:idx].strip()
        value = line[idx + 1 :].strip()
        if key:
            result[key] = value
    return result


def rewrite(
    config: dict,
    remote_vmid: int,
    next_vmid: int,
    name: str,
    token_to_local_leaf: dict,
) -> str:
    rewritten = {}
    for k, v in sorted(config.items()):
        if k in ("parent", "snaptime"):
            continue
        if token_to_local_leaf:
            for token, local_leaf in token_to_local_leaf.items():
                v = v.replace(token, local_leaf)
        else:
            v = v.replace(f"vm-{remote_vmid}-disk-", f"vm-{next_vmid}-disk-")
        if k == "name":
            v = name
        rewritten[k] = v
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
    out = rewrite(config, remote_vmid, next_vmid, name, token_to_local)
    open(config_path, "w", encoding="utf-8").write(out)


if __name__ == "__main__":
    main()
PY
}

usage() {
  cat <<'EOF'
Restore a template VM from storage (same logic as proxmox_client.create_vm_from_storagebox).

Usage:
  restore_template_vm_from_shared_storage.sh <template_key> <vmid> [node]

  [node] must match a name under /etc/pve/nodes/ (often `hostname -s`, not the FQDN).
  If omitted, the script picks the first of hostname -s / hostname that has qemu-server/.

Run from GitHub via curl|bash (no checkout needed; the ?t=... cache-busts so you
always pull the latest master revision):

  curl -fsSL "https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/scripts/restore_template_vm_from_shared_storage.sh?t=$(date +%s)" \
    | bash -s -- <template_key> <vmid> [node]

  Concrete examples:
    curl -fsSL "https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/scripts/restore_template_vm_from_shared_storage.sh?t=$(date +%s)" | bash -s -- windows-es 100
    curl -fsSL "https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/scripts/restore_template_vm_from_shared_storage.sh?t=$(date +%s)" | bash -s -- windows-en 101

  IMPORTANT: you MUST pipe curl into bash with `| bash -s -- args`. If you omit
  the "|", curl treats "bash", "--", "windows-en", "101" as extra URLs to fetch
  and fails with: curl: (6) Could not resolve host: bash

Environment:
  STORAGE_BOX_HOST, STORAGE_BOX_USER, STORAGE_BOX_PORT, STORAGE_BOX_BASE_PATH (default /home/templates)
  ZFS_PARENT (default rpool/data)
  VM_NAME    optional guest name (default: vm-<vmid>)
EOF
}

[[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && {
  usage
  exit 0
}

TEMPLATE_KEY="${1:-}"
REQUEST_VMID_RAW="${2:-}"
ARG_NODE="${3:-}"

[[ -n "$TEMPLATE_KEY" ]] || {
  usage
  die "Missing <template_key>"
}
[[ -n "$REQUEST_VMID_RAW" ]] || {
  usage
  die "Missing <vmid>"
}

if [[ ! "$REQUEST_VMID_RAW" =~ ^[0-9]+$ ]]; then
  die "<vmid> must be digits, got: ${REQUEST_VMID_RAW}"
fi
REQUEST_VMID=$((10#$REQUEST_VMID_RAW))
if [[ "$REQUEST_VMID" -lt 100 || "$REQUEST_VMID" -gt 999999999 ]]; then
  die "<vmid> must be between 100 and 999999999, got: ${REQUEST_VMID}"
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
VM_NAME="${VM_NAME:-vm-${REQUEST_VMID}}"

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

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  die "Run as root on a Proxmox node"
fi

NODE="$(resolve_pve_node_name "$ARG_NODE")"
echo "Proxmox node directory: /etc/pve/nodes/${NODE}/ (pass [node] as 3rd arg if this is wrong)"

SSH_BASE=(
  ssh
  -n
  -p "$STORAGE_BOX_PORT"
  -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null
  "${STORAGE_BOX_USER}@${STORAGE_BOX_HOST}"
)

base="${STORAGE_BOX_BASE_PATH%/}"
REMOTE_BASE="${base}/${TEMPLATE_KEY}"
REMOTE_CONFIG="${REMOTE_BASE}/config.conf"

echo "Fetching template config: ${REMOTE_CONFIG}"
CONFIG_TMP="$(mktemp)"
FW_TMP=""
trap 'rm -f "$CONFIG_TMP" "${FW_TMP:-}"' EXIT

if ! "${SSH_BASE[@]}" "cat '$REMOTE_CONFIG'" >"$CONFIG_TMP"; then
  die "Failed to fetch template config"
fi
[[ -s "$CONFIG_TMP" ]] || die "Empty template config: ${REMOTE_CONFIG}"

mapfile -t REMOTE_TOKENS < <(pve_sorted_disk_tokens "$CONFIG_TMP" | dedupe_tokens_first_seen)
[[ "${#REMOTE_TOKENS[@]}" -gt 0 ]] || die "No disk tokens in template (scsi|virtio|sata|ide|efidisk0|tpmstate0)"

REMOTE_LIST="$("${SSH_BASE[@]}" "ls -1 '${REMOTE_BASE}'/disk*.stream.zst 2>/dev/null")"
REMOTE_STREAM_COUNT="$(printf '%s\n' "$REMOTE_LIST" | wc -l | tr -d '[:space:]')"
[[ "$REMOTE_STREAM_COUNT" =~ ^[0-9]+$ ]] || REMOTE_STREAM_COUNT=0
if [[ "$REMOTE_STREAM_COUNT" -eq 0 ]]; then
  die "No disk*.stream.zst under ${REMOTE_BASE} on the storage box"
fi
if [[ "${#REMOTE_TOKENS[@]}" -ne "$REMOTE_STREAM_COUNT" ]]; then
  die "Unique disk volumes in config (${#REMOTE_TOKENS[@]}) != stream files on storage (${REMOTE_STREAM_COUNT}). Fix template or paths."
fi

VMID="$REQUEST_VMID"

shopt -s nullglob
for existing in /etc/pve/nodes/*/qemu-server/${VMID}.conf; do
  [[ -e "$existing" ]] && die "VM ${VMID} already exists: ${existing}"
done
shopt -u nullglob

n="${#REMOTE_TOKENS[@]}"
echo "Restoring VMID ${VMID} on node ${NODE} (${n} unique volume(s) / stream(s), create_vm_from_storagebox layout)"

# 3) Stream each disk: ssh storagebox "dd if=... bs=4M" | zstd -d | zfs receive <local_ds>
for ((i = 0; i < n; i++)); do
  local_ds="${ZFS_PARENT}/vm-${VMID}-disk-${i}"
  remote_file="${REMOTE_BASE}/disk${i}.stream.zst"
  echo "[${i}/$((n - 1))] ${remote_file} -> ${local_ds}"

  while IFS= read -r snap; do
    [[ -z "$snap" || "$snap" != *"@"* ]] && continue
    zfs destroy "$snap" || true
  done < <(zfs list -H -o name -t snapshot -S creation -r "$local_ds" 2>/dev/null || true)

  "${SSH_BASE[@]}" "dd if='${remote_file}' bs=4M" \
    | zstd -d \
    | zfs receive -F "$local_ds"

  zfs set refreservation=none "$local_ds" || true
done

# 5) Destroy template snapshot on received datasets if present (e.g. @stable) — same as create_vm_from_storagebox
for ((i = 0; i < n; i++)); do
  local_ds="${ZFS_PARENT}/vm-${VMID}-disk-${i}"
  while IFS= read -r snap; do
    snap="${snap//[$'\r']}"
    [[ -z "$snap" || "$snap" != *"@"* ]] && continue
    zfs destroy "$snap" || true
  done < <(zfs list -H -o name -t snapshot -S creation -r "$local_ds" 2>/dev/null || true)
done

# 6) Write VM config — _rewrite_vm_config_for_clone + token_to_local like create_vm_from_storagebox
TOKENS_TMP="$(mktemp)"
printf '%s\n' "${REMOTE_TOKENS[@]}" >"$TOKENS_TMP"
trap 'rm -f "$CONFIG_TMP" "${FW_TMP:-}" "$TOKENS_TMP"' EXIT
if ! rewrite_vm_config_like_create_vm "$CONFIG_TMP" "$VMID" "$VM_NAME" "$TOKENS_TMP"; then
  die "rewrite_vm_config_like_create_vm (python3) failed — VM config not updated"
fi
rm -f "$TOKENS_TMP"
trap 'rm -f "$CONFIG_TMP" "${FW_TMP:-}"' EXIT
[[ -s "$CONFIG_TMP" ]] || die "Rewrite left empty config at $CONFIG_TMP"

CONF_DIR="/etc/pve/nodes/${NODE}/qemu-server"
CONF_PATH="${CONF_DIR}/${VMID}.conf"
install_pve_cfg_file "$CONFIG_TMP" "$CONF_PATH"
echo "Wrote VM config: ${CONF_PATH}"

# 7) Optional firewall (replace remote vmid in text like create_vm_from_storagebox)
REMOTE_FW="${REMOTE_BASE}/firewall.fw"
FW_TMP="$(mktemp)"
OLD_VMID_FOR_FW="$(sed -nE 's/.*vm-([0-9]+)-disk-[0-9]+.*/\1/p' <<<"${REMOTE_TOKENS[0]}" | head -n1)"
[[ -z "$OLD_VMID_FOR_FW" ]] && OLD_VMID_FOR_FW="100"
if "${SSH_BASE[@]}" "cat '$REMOTE_FW'" >"$FW_TMP" 2>/dev/null && [[ -s "$FW_TMP" ]]; then
  if [[ "$OLD_VMID_FOR_FW" != "$VMID" ]]; then
    # \b: a blind numeric replace mangles values that merely CONTAIN the vmid
    # (vmid=100 turns "dport 3100" into "dport 3<target>") and IPv6 suffixes.
    sed -i -E "s/\b${OLD_VMID_FOR_FW}\b/${VMID}/g" "$FW_TMP"
  fi
  mkdir -p /etc/pve/firewall
  install_pve_cfg_file "$FW_TMP" "/etc/pve/firewall/${VMID}.fw"
  echo "Wrote firewall: /etc/pve/firewall/${VMID}.fw"
else
  echo "No firewall file (optional): ${REMOTE_FW}"
fi

echo "Restore finished. VMID=${VMID} config=${CONF_PATH}"

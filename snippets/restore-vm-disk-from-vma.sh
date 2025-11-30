#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   restore-vm-disk-from-vma.sh <vmid> [path_to_vma(.zst)]
#
# Defaults:
#   VMA path: /var/lib/svz/dump/vzdump-qemu-100.vma.zst
#   Disk: scsi0 on ZFS rpool/data (local-zfs:vm-<vmid>-disk-1)

# Logging configuration (similar to sync-dnat.py)
LOG_FILE="/var/log/restore-vm-disk.log"
MAX_LOG_SIZE=$((1024 * 1024))  # 1 MB

# Clean log if it exceeds MAX_LOG_SIZE
clean_log_if_needed() {
  if [[ -f "$LOG_FILE" ]]; then
    local size
    size=$(stat -f%z "$LOG_FILE" 2>/dev/null || stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)
    if [[ $size -gt $MAX_LOG_SIZE ]]; then
      rm -f "$LOG_FILE"
      return 0  # Indicates cleanup happened
    fi
  fi
  return 1
}

# Logging function that writes to both file and stdout
log() {
  local level="$1"
  shift
  local message="$*"
  local timestamp
  timestamp=$(date '+%Y-%m-%d %H:%M:%S')
  local log_entry="[${timestamp}] [${level}] ${message}"
  
  # Write to log file (ignore errors to prevent script failure on disk full)
  echo "$log_entry" >> "$LOG_FILE" 2>/dev/null || true
  
  # Also write to stdout (for real-time monitoring)
  echo "$log_entry" 2>/dev/null || true
}

# Wrapper for getting VM status using pvesh API
get_vm_status() {
  local vmid="$1"
  local node="${2:-$(hostname)}"
  local output
  local exit_code
  local status
  
  {
    log "DEBUG" "Executing: pvesh get /nodes/${node}/qemu/${vmid}/status/current"
  } >&2
  set +e  # Temporarily disable exit on error
  output=$(pvesh get "/nodes/${node}/qemu/${vmid}/status/current" --output-format json 2>&1)
  exit_code=$?
  set -e  # Re-enable exit on error
  
  if [[ $exit_code -eq 0 && -n "$output" ]]; then
    # Extract status from JSON output (pvesh returns JSON like {"status":"running",...})
    # Try multiple methods to parse JSON
    if command -v jq >/dev/null 2>&1; then
      status=$(echo "$output" | jq -r '.status // empty' 2>/dev/null || echo "")
    else
      # Fallback: use grep/sed to extract status
      status=$(echo "$output" | grep -o '"status"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/.*"status"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/' || echo "")
    fi
    
    if [[ -n "$status" ]]; then
      echo "$status"
      return 0
    else
      {
        log "ERROR" "Could not parse status from pvesh output: $output"
      } >&2
      return 1
    fi
  else
    {
      log "ERROR" "pvesh get status for VM $vmid failed with exit code $exit_code: $output"
    } >&2
    return $exit_code
  fi
}

# Wrapper for getting VM config using pvesh API
get_vm_config_value() {
  local vmid="$1"
  local config_key="$2"  # e.g., "scsi0"
  local node="${3:-$(hostname)}"
  local output
  local exit_code
  local value
  
  {
    log "DEBUG" "Executing: pvesh get /nodes/${node}/qemu/${vmid}/config"
  } >&2
  set +e  # Temporarily disable exit on error
  output=$(pvesh get "/nodes/${node}/qemu/${vmid}/config" --output-format json 2>&1)
  exit_code=$?
  set -e  # Re-enable exit on error
  
  if [[ $exit_code -eq 0 && -n "$output" ]]; then
    # Extract config value from JSON output (pvesh returns JSON like {"scsi0":"local-zfs:vm-201-disk-1,...",...})
    # Try multiple methods to parse JSON
    if command -v jq >/dev/null 2>&1; then
      value=$(echo "$output" | jq -r ".[\"${config_key}\"] // empty" 2>/dev/null || echo "")
    else
      # Fallback: use grep/sed to extract config value
      value=$(echo "$output" | grep -o "\"${config_key}\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" | sed "s/.*\"${config_key}\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/" || echo "")
    fi
    
    if [[ -n "$value" ]]; then
      echo "$value"
      return 0
    else
      {
        log "ERROR" "Could not find config key '${config_key}' in pvesh output: $output"
      } >&2
      return 1
    fi
  else
    {
      log "ERROR" "pvesh get config for VM $vmid failed with exit code $exit_code: $output"
    } >&2
    return $exit_code
  fi
}

# Initialize logging
if clean_log_if_needed; then
  log "INFO" "Log file deleted due to size limit (> 1 MB)"
fi

VMID="${1:-}"
BACKUP="${2:-/var/lib/svz/dump/vzdump-qemu-100.vma.zst}"

if [[ -z "$VMID" ]]; then
  log "ERROR" "Usage: $0 <vmid> [path_to_vma(.zst)]"
  exit 1
fi

if [[ ! -f "$BACKUP" ]]; then
  log "ERROR" "Backup file not found: $BACKUP"
  exit 1
fi

NODE="$(hostname)"

log "INFO" "Running on node: $NODE"
log "INFO" "VMID: $VMID"
log "INFO" "Backup: $BACKUP"

# --- sanity checks ---
log "INFO" "Checking if VM $VMID exists..."
if ! STATUS_CHECK=$(get_vm_status "$VMID" "$NODE" 2>/dev/null); then
  log "ERROR" "VM $VMID does not exist on this node or status check failed"
  exit 1
fi
log "INFO" "VM $VMID exists (status: ${STATUS_CHECK:-unknown})"

log "INFO" "Reading VM $VMID disk configuration..."
DISK_ENTRY="$(get_vm_config_value "$VMID" "scsi0" "$NODE")"
if [[ $? -ne 0 || -z "${DISK_ENTRY}" ]]; then
  log "ERROR" "VM $VMID has no scsi0 disk configured or failed to read config"
  exit 1
fi
log "INFO" "Found disk entry: $DISK_ENTRY"

VOLID="${DISK_ENTRY%%,*}"            # local-zfs:vm-201-disk-1
VOLNAME="${VOLID#*:}"                # vm-201-disk-1
ZVOL_PATH="/dev/zvol/rpool/data/${VOLNAME}"

log "INFO" "Checking ZFS volume: $ZVOL_PATH"
if [[ ! -b "$ZVOL_PATH" ]]; then
  log "ERROR" "ZFS volume not found: $ZVOL_PATH"
  exit 1
fi
log "INFO" "ZFS volume exists: $ZVOL_PATH"

# --- stop VM if running ---
log "INFO" "Checking VM $VMID status..."
STATUS="$(get_vm_status "$VMID" "$NODE")"
STATUS_EXIT=$?
if [[ $STATUS_EXIT -ne 0 ]]; then
  log "ERROR" "Failed to get VM status. VM may be in a bad state. Proceeding anyway..."
  STATUS="unknown"
elif [[ -z "$STATUS" ]]; then
  log "WARN" "VM status returned empty. Proceeding anyway..."
  STATUS="unknown"
fi
log "INFO" "VM $VMID current status: $STATUS"

if [[ "$STATUS" == "running" ]]; then
  log "INFO" "VM $VMID is running, stopping..."
  set +e  # Temporarily disable exit on error for pipe
  pvesh create "/nodes/${NODE}/qemu/${VMID}/status/stop" 2>&1 | tee -a "$LOG_FILE" 2>/dev/null || true
  stop_exit_code=${PIPESTATUS[0]}  # Get exit code of pvesh
  set -e  # Re-enable exit on error
  if [[ $stop_exit_code -ne 0 ]]; then
    log "ERROR" "Failed to stop VM $VMID (exit code: $stop_exit_code)"
    exit 1
  fi
  log "INFO" "Waiting for VM $VMID to stop..."
  # Wait for VM to actually stop
  wait_count=0
  while [[ $wait_count -lt 60 ]]; do
    sleep 2
    CURRENT_STATUS="$(get_vm_status "$VMID" "$NODE")"
    CURRENT_STATUS_EXIT=$?
    if [[ $CURRENT_STATUS_EXIT -ne 0 ]]; then
      log "WARN" "VM status check failed while waiting for stop (attempt $wait_count/60), continuing..."
      # If we can't check status, assume it's stopped and continue
      break
    fi
    if [[ -z "$CURRENT_STATUS" ]]; then
      log "WARN" "VM status returned empty (attempt $wait_count/60), continuing..."
      break
    fi
    if [[ "$CURRENT_STATUS" != "running" ]]; then
      log "INFO" "VM $VMID stopped (status: $CURRENT_STATUS)"
      break
    fi
    wait_count=$((wait_count + 1))
  done
  if [[ $wait_count -ge 60 ]]; then
    log "WARN" "VM $VMID did not stop within 120 seconds, but continuing with restore..."
  fi
else
  log "INFO" "VM $VMID is not running (status: $STATUS)"
fi

# --- temp dir & cleanup ---
TMPROOT="$(mktemp -d /var/tmp/restore-${VMID}-XXXXXX)"
EXTRACT_DIR="${TMPROOT}/vma"
rm -rf "$EXTRACT_DIR"
mkdir -p "$(dirname "$EXTRACT_DIR")"
log "INFO" "Using temp dir: $TMPROOT"

cleanup() {
  log "INFO" "Cleaning up $TMPROOT"
  rm -rf "$TMPROOT"
}
trap cleanup EXIT

# --- extract .vma or .vma.zst ---
log "INFO" "Starting backup extraction..."
log "INFO" "Extract directory: $EXTRACT_DIR"

if [[ "$BACKUP" == *.zst ]]; then
  log "INFO" "Detected compressed backup (.zst)"
  if ! command -v zstd >/dev/null 2>&1; then
    log "ERROR" "zstd is not installed"
    exit 1
  fi
  # Stream decompress directly into vma extract
  log "INFO" "Decompressing and extracting backup (this may take a while)..."
  log "INFO" "Progress output will be shown on stdout only to avoid filling log file"
  set +e  # Temporarily disable exit on error for pipe
  # Only show progress on stdout, don't write to log file (to avoid disk full issues)
  zstd -dc "$BACKUP" 2>&1 | vma extract -v - "$EXTRACT_DIR" 2>&1
  extract_exit_code=${PIPESTATUS[0]}  # Get exit code of zstd
  set -e  # Re-enable exit on error
  if [[ $extract_exit_code -ne 0 ]]; then
    log "ERROR" "Backup extraction failed (zstd exit code: $extract_exit_code)"
    exit 1
  fi
  log "INFO" "Backup extraction completed"
else
  log "INFO" "Detected uncompressed .vma"
  log "INFO" "Extracting backup (this may take a while)..."
  log "INFO" "Progress output will be shown on stdout only to avoid filling log file"
  set +e  # Temporarily disable exit on error for pipe
  # Only show progress on stdout, don't write to log file (to avoid disk full issues)
  vma extract -v "$BACKUP" "$EXTRACT_DIR" 2>&1
  extract_exit_code=${PIPESTATUS[0]}  # Get exit code of vma extract
  set -e  # Re-enable exit on error
  if [[ $extract_exit_code -ne 0 ]]; then
    log "ERROR" "Backup extraction failed (vma extract exit code: $extract_exit_code)"
    exit 1
  fi
  log "INFO" "Backup extraction completed"
fi

# Try to match disk type (scsi0, virtio0, sata0, ide0, etc.)
log "INFO" "Looking for extracted disk file..."
DISK_NAME="$(echo "$DISK_ENTRY" | awk -F: '{print $1}')"
RAW_DISK="${EXTRACT_DIR}/disk-drive-${DISK_NAME}.raw"
log "INFO" "Expected disk file: $RAW_DISK"

if [[ ! -f "$RAW_DISK" ]]; then
  log "WARN" "Could not find disk for $DISK_NAME, searching fallback..."
  RAW_DISK="$(find "$EXTRACT_DIR" -type f -name '*scsi0*.raw' | head -n1 || true)"
  if [[ -n "$RAW_DISK" ]]; then
    log "INFO" "Found fallback disk: $RAW_DISK"
  fi
fi

if [[ -z "$RAW_DISK" || ! -f "$RAW_DISK" ]]; then
  log "ERROR" "Could not find the expected .raw disk for $DISK_NAME in $EXTRACT_DIR"
  log "DEBUG" "Available files in extract directory:"
  ls -la "$EXTRACT_DIR" 2>/dev/null | while IFS= read -r line; do
    log "DEBUG" "$line"
  done || true
  log "DEBUG" "Available .raw files:"
  ls -1 "$EXTRACT_DIR"/*.raw 2>/dev/null | while IFS= read -r file; do
    log "DEBUG" "  $file"
  done || true
  exit 1
fi
log "INFO" "Found disk file: $RAW_DISK"

# Clean up unnecessary files from extracted backup to save space before restore
log "INFO" "Cleaning up unnecessary files from extracted backup to save space..."
RAW_DISK_BASENAME="$(basename "$RAW_DISK")"
FILES_TO_DELETE=0

# Helper function to format bytes
format_bytes() {
  local bytes="$1"
  if command -v numfmt >/dev/null 2>&1; then
    numfmt --to=iec-i --suffix=B "$bytes" 2>/dev/null || echo "${bytes} bytes"
  else
    # Simple formatting without numfmt (integer division)
    if [[ $bytes -gt 1073741824 ]]; then
      echo "$((bytes / 1073741824)) GB"
    elif [[ $bytes -gt 1048576 ]]; then
      echo "$((bytes / 1048576)) MB"
    elif [[ $bytes -gt 1024 ]]; then
      echo "$((bytes / 1024)) KB"
    else
      echo "${bytes} bytes"
    fi
  fi
}

# Calculate space before cleanup
SPACE_BEFORE="0"
if command -v du >/dev/null 2>&1; then
  SPACE_BEFORE=$(du -sb "$EXTRACT_DIR" 2>/dev/null | awk '{print $1}' || echo "0")
fi

# Delete all files except the one we need
for file in "$EXTRACT_DIR"/*; do
  if [[ -f "$file" && "$(basename "$file")" != "$RAW_DISK_BASENAME" ]]; then
    FILE_SIZE="0"
    if command -v stat >/dev/null 2>&1; then
      FILE_SIZE=$(stat -f%z "$file" 2>/dev/null || stat -c%s "$file" 2>/dev/null || echo "0")
    fi
    if rm -f "$file" 2>/dev/null; then
      FILES_TO_DELETE=$((FILES_TO_DELETE + 1))
      if [[ "$FILE_SIZE" != "0" ]]; then
        log "DEBUG" "Deleted: $(basename "$file") ($(format_bytes "$FILE_SIZE"))"
      fi
    fi
  fi
done

# Calculate and report space freed
if [[ "$SPACE_BEFORE" != "0" ]] && command -v du >/dev/null 2>&1; then
  SPACE_AFTER=$(du -sb "$EXTRACT_DIR" 2>/dev/null | awk '{print $1}' || echo "0")
  if [[ "$SPACE_AFTER" != "0" ]]; then
    ACTUAL_SPACE_FREED=$((SPACE_BEFORE - SPACE_AFTER))
    log "INFO" "Cleanup complete: freed $(format_bytes "$ACTUAL_SPACE_FREED") ($FILES_TO_DELETE files deleted)"
  else
    log "INFO" "Cleanup complete: deleted $FILES_TO_DELETE unnecessary files"
  fi
else
  log "INFO" "Cleanup complete: deleted $FILES_TO_DELETE unnecessary files"
fi

log "INFO" "Starting disk restore: $RAW_DISK → $ZVOL_PATH"
DISK_SIZE="$(stat -f%z "$RAW_DISK" 2>/dev/null || stat -c%s "$RAW_DISK" 2>/dev/null || echo "unknown")"
log "INFO" "Disk size: $DISK_SIZE bytes"
if command -v pv >/dev/null 2>&1; then
  log "INFO" "Using pv for progress display..."
  # Use pv for progress display, show on stdout only (to avoid disk full issues)
  set +e  # Temporarily disable exit on error for pipe
  pv "$RAW_DISK" 2>&1 | dd of="$ZVOL_PATH" bs=4M conv=fsync 2>&1
  restore_exit_code=${PIPESTATUS[0]}  # Get exit code of pv
  set -e  # Re-enable exit on error
  if [[ $restore_exit_code -ne 0 ]]; then
    log "ERROR" "Disk restore failed (pv exit code: $restore_exit_code)"
    exit 1
  fi
else
  log "INFO" "Using dd with status=progress..."
  # Use dd with status=progress, show on stdout only (to avoid disk full issues)
  set +e  # Temporarily disable exit on error for pipe
  dd if="$RAW_DISK" of="$ZVOL_PATH" bs=4M conv=fsync status=progress 2>&1
  restore_exit_code=${PIPESTATUS[0]}  # Get exit code of dd
  set -e  # Re-enable exit on error
  if [[ $restore_exit_code -ne 0 ]]; then
    log "ERROR" "Disk restore failed (dd exit code: $restore_exit_code)"
    exit 1
  fi
fi

log "INFO" "Disk restore completed, syncing filesystem..."
sync
log "INFO" "Filesystem sync completed"

# --- restart VM ---
log "INFO" "Starting VM $VMID..."
set +e  # Temporarily disable exit on error for pipe
pvesh create "/nodes/${NODE}/qemu/${VMID}/status/start" 2>&1 | tee -a "$LOG_FILE" 2>/dev/null || true
start_exit_code=${PIPESTATUS[0]}  # Get exit code of pvesh
set -e  # Re-enable exit on error
if [[ $start_exit_code -ne 0 ]]; then
  log "ERROR" "Failed to start VM $VMID (exit code: $start_exit_code)"
  exit 1
fi
log "INFO" "VM $VMID start command completed"

log "INFO" "Done."

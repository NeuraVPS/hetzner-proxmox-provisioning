#!/usr/bin/env bash
#
# check_duplicate_net0_mac.sh
#
# 1) Finds VMs that share the same MAC address on net0.
# 2) For each running VM, reports when config file differs from live QEMU
#    (net0 MAC, CPU cores, RAM, disk size) — restart required to apply.
#
# Run on a Proxmox node (reads /etc/pve/qemu-server/*.conf and
# /var/run/qemu-server/<vmid>.pid + /proc/<pid>/cmdline for live state).
#
# Usage:
#   ./check_duplicate_net0_mac.sh [CONFIG_DIR]
#
# Default CONFIG_DIR: /etc/pve/qemu-server
# Set PVE_CONF_DIR to override from environment.
#
# Safe to copy-paste into an interactive bash: no exit/return, no set -e.
#

CONFIG_DIR="${1:-${PVE_CONF_DIR:-/etc/pve/qemu-server}}"

if [[ ! -d "$CONFIG_DIR" ]]; then
  echo "Error: Config directory not found: $CONFIG_DIR" >&2
  echo "  CONFIG_DIR defaults to /etc/pve/qemu-server (or set PVE_CONF_DIR)" >&2
fi

# Normalize MAC for comparison: lowercase, strip optional separators for grouping
normalize_mac() {
  local mac="$1"
  # Allow XX:XX:XX:XX:XX:XX or XX-XX-XX-XX-XX-XX
  echo "$mac" | tr '[:upper:]' '[:lower:]' | tr -d '-' | sed 's/://g'
}

# Extract MAC from net0 line: net0: virtio=MAC,bridge=... or net0: e1000=MAC,...
get_net0_mac() {
  local line="$1"
  local rest="${line#*: }"   # strip "net0: "
  local value="${rest#*=}"   # strip "virtio=" or "e1000=" etc.
  echo "${value%%,*}"        # strip everything after first comma
}

declare -A mac_to_vms   # normalized_mac -> "vmid1 vmid2 ..."
declare -A mac_raw      # normalized_mac -> one raw MAC string for display
has_duplicates=0

for conf in "$CONFIG_DIR"/[0-9]*.conf; do
  [[ -f "$conf" ]] || continue
  vmid=$(basename "$conf" .conf)

  while IFS= read -r line; do
    if [[ "$line" =~ ^net0:\ .+=.+ ]]; then
      raw_mac=$(get_net0_mac "$line")
      # Basic MAC format check (6 hex bytes)
      if [[ ! "$raw_mac" =~ ^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$ ]] && \
         [[ ! "$raw_mac" =~ ^([0-9a-fA-F]{2}-){5}[0-9a-fA-F]{2}$ ]]; then
        echo "Warning: Skipping invalid net0 MAC for VM $vmid: $raw_mac" >&2
        continue
      fi
      norm=$(normalize_mac "$raw_mac")
      if [[ -z "${mac_to_vms[$norm]+x}" ]]; then
        mac_to_vms[$norm]="$vmid"
        mac_raw[$norm]="$raw_mac"
      else
        # Only add and flag duplicate if this is a different VM (same VM can appear twice if config is read twice)
        if [[ " ${mac_to_vms[$norm]} " != *" $vmid "* ]]; then
          mac_to_vms[$norm]="${mac_to_vms[$norm]} $vmid"
          has_duplicates=1
        fi
      fi
      break  # one net0 per VM
    fi
  done < "$conf"
done

# Report
echo "=== net0 MAC address report ==="
echo "Config dir: $CONFIG_DIR"
echo ""

if [[ ${#mac_to_vms[@]} -eq 0 ]]; then
  echo "No VMs with net0 found."
fi

# Uniquify VMIDs (same VM can appear twice if config is read twice) and only report real duplicates
has_duplicates=0
for norm in $(echo "${!mac_to_vms[@]}" | tr ' ' '\n' | sort); do
  vms="${mac_to_vms[$norm]}"
  raw="${mac_raw[$norm]}"
  unique_vms=$(echo "$vms" | tr ' ' '\n' | sort -u)
  unique_count=$(echo "$unique_vms" | wc -w)
  if [[ $unique_count -ge 2 ]]; then
    has_duplicates=1
    echo "DUPLICATE MAC (${unique_count} VMs): $raw"
    echo "  VMIDs: $(echo "$unique_vms" | tr '\n' ' ')"
    echo ""
  fi
done

if [[ $has_duplicates -eq 0 ]]; then
  echo "No duplicate net0 MAC addresses found."
else
  echo "--- All net0 MACs with 2+ VMs (for reference) ---"
  for norm in $(echo "${!mac_to_vms[@]}" | tr ' ' '\n' | sort); do
    vms="${mac_to_vms[$norm]}"
    raw="${mac_raw[$norm]}"
    unique_vms=$(echo "$vms" | tr ' ' '\n' | sort -u)
    unique_count=$(echo "$unique_vms" | wc -w)
    if [[ $unique_count -ge 2 ]]; then
      echo "  $raw -> VM(s) $(echo "$unique_vms" | tr '\n' ' ')"
    fi
  done
fi

echo ""
echo "=== Config vs live (restart required when different) ==="
echo "Comparing config file vs running QEMU process for each running VM."
echo ""

# Parse config file for one VM: sets config_cores, config_memory_mb, config_net0_mac, config_disk_gb (space-separated, in device order)
read_config() {
  local vmid="$1"
  local conf="$CONFIG_DIR/$vmid.conf"
  config_cores="" config_memory_mb="" config_net0_mac="" config_disk_gb=""
  [[ -f "$conf" ]] || return 1
  local sockets=1 cores=1 threads=1 memory=""
  while IFS= read -r line; do
    if [[ "$line" =~ ^sockets:\ (.+)$ ]]; then sockets="${BASH_REMATCH[1]}"; fi
    if [[ "$line" =~ ^cores:\ (.+)$ ]]; then cores="${BASH_REMATCH[1]}"; fi
    if [[ "$line" =~ ^threads:\ (.+)$ ]]; then threads="${BASH_REMATCH[1]}"; fi
    if [[ "$line" =~ ^memory:\ (.+)$ ]]; then memory="${BASH_REMATCH[1]}"; fi
    if [[ "$line" =~ ^net0:\ .+=.+ ]]; then config_net0_mac=$(get_net0_mac "$line"); fi
    if [[ "$line" =~ ^(virtio|scsi)[0-9]+:\ .*size=([0-9]+)([GgMm])? ]]; then
      local sz="${BASH_REMATCH[2]}" u="${BASH_REMATCH[3],,}"
      if [[ "$u" == "m" ]]; then config_disk_gb="$config_disk_gb $(awk "BEGIN{printf \"%.2f\", $sz/1024}" 2>/dev/null || echo "0")"; else config_disk_gb="$config_disk_gb $sz"; fi
    fi
  done < "$conf"
  config_cores=$((sockets * cores * threads))
  config_memory_mb="$memory"
  config_disk_gb="${config_disk_gb# }"
  return 0
}

# Convert memory spec to MB (e.g. 4096, 16M, 8G)
mem_to_mb() {
  local v="$1"
  if [[ "$v" =~ ^([0-9]+)[Gg]$ ]]; then echo $((${BASH_REMATCH[1]} * 1024)); return; fi
  if [[ "$v" =~ ^([0-9]+)[Mm]$ ]]; then echo "${BASH_REMATCH[1]}"; return; fi
  if [[ "$v" =~ ^[0-9]+$ ]]; then echo "$v"; return; fi
  echo "0"
}

# Get live state from running QEMU process; sets live_cores, live_memory_mb, live_net0_mac, live_disk_gb (space-separated)
get_live_state() {
  local vmid="$1"
  local pidfile="/var/run/qemu-server/${vmid}.pid"
  live_cores="" live_memory_mb="" live_net0_mac="" live_disk_gb=""
  [[ -f "$pidfile" ]] || return 1
  local pid
  pid=$(cat "$pidfile" 2>/dev/null) || return 1
  [[ -d "/proc/$pid" ]] || return 1
  local cmdline
  cmdline=$(cat "/proc/$pid/cmdline" 2>/dev/null | tr '\0' '\n') || return 1
  local prev=""
  local first_net_mac=""
  while IFS= read -r arg; do
    [[ -z "$arg" ]] && continue
    if [[ "$prev" == "-m" ]]; then live_memory_mb=$(mem_to_mb "$arg"); fi
    if [[ "$prev" == "-smp" ]] && [[ "$arg" =~ ^([0-9]+) ]]; then live_cores="${BASH_REMATCH[1]}"; fi
    if [[ "$arg" =~ ^-device.*,mac=([0-9a-fA-F:]+) ]]; then
      [[ -z "$first_net_mac" ]] && first_net_mac="${BASH_REMATCH[1]}"
    fi
    if [[ "$prev" == "-drive" ]] && [[ "$arg" =~ ^file=([^,]+) ]]; then
      local path="${BASH_REMATCH[1]}"
      local bytes="0"
      if [[ -b "$path" ]]; then bytes=$(blockdev --getsize64 "$path" 2>/dev/null || echo "0"); else bytes=$(stat -c %s "$path" 2>/dev/null || echo "0"); fi
      local gb=$(awk "BEGIN{printf \"%.2f\", $bytes/1024/1024/1024}" 2>/dev/null || echo "0")
      live_disk_gb="$live_disk_gb $gb"
    fi
    prev="$arg"
  done <<< "$cmdline"
  live_net0_mac="$first_net_mac"
  live_disk_gb="${live_disk_gb# }"
  return 0
}

config_restart_any=0
for conf in "$CONFIG_DIR"/[0-9]*.conf; do
  [[ -f "$conf" ]] || continue
  vmid=$(basename "$conf" .conf)
  pidfile="/var/run/qemu-server/${vmid}.pid"
  [[ -f "$pidfile" ]] || continue
  read_config "$vmid" || continue
  get_live_state "$vmid" || continue
  issues=""
  if [[ -n "$config_net0_mac" ]] && [[ -n "$live_net0_mac" ]] && [[ $(normalize_mac "$config_net0_mac") != $(normalize_mac "$live_net0_mac") ]]; then
    issues="$issues net0 MAC (config: $config_net0_mac, live: $live_net0_mac)"
  fi
  if [[ -n "$config_cores" ]] && [[ -n "$live_cores" ]] && [[ "$config_cores" != "$live_cores" ]]; then
    issues="$issues CPU cores (config: $config_cores, live: $live_cores)"
  fi
  if [[ -n "$config_memory_mb" ]] && [[ -n "$live_memory_mb" ]] && [[ "$config_memory_mb" != "$live_memory_mb" ]]; then
    issues="$issues RAM (config: $((config_memory_mb/1024)) GB, live: $((live_memory_mb/1024)) GB)"
  fi
  if [[ -n "$config_disk_gb" ]] && [[ -n "$live_disk_gb" ]]; then
    local cdisk live_disk_gb_arr=($live_disk_gb) i=0 cgi lgi
    for cdisk in $config_disk_gb; do
      if [[ -n "${live_disk_gb_arr[i]:-}" ]]; then
        local lg="${live_disk_gb_arr[i]}" cg="$cdisk"
        cgi=$(printf "%.0f" "$cg" 2>/dev/null || echo "$cg")
        lgi=$(printf "%.0f" "$lg" 2>/dev/null || echo "$lg")
        if [[ "$cgi" != "$lgi" ]]; then
          issues="$issues disk${i} (config: ${cg} GB, live: ${lg} GB)"
        fi
      fi
      i=$((i+1))
    done
  fi
  if [[ -n "$issues" ]]; then
    config_restart_any=1
    echo "VM $vmid — restart required:$issues"
    echo ""
  fi
done
if [[ $config_restart_any -eq 0 ]]; then
  echo "No config vs live mismatches (no restart needed)."
fi

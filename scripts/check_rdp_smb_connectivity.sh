#!/usr/bin/env bash
# Check RDP (20000+VMID) and SMB (10000+VMID) connectivity over IPv4 and IPv6 via nc.
# Paste this script on the target machine; set VMIDS and HOST_IPS below.
# Ctrl+C is ignored so the script keeps running; script does not exit so your shell stays open.

trap '' INT

VMIDS=( 201 )

# Ingress IPs to probe (IPv4 and/or IPv6). Add as many entries as needed.
HOST_IPS=(
  "46.62.188.207"         # BASE 0
  "2a01:4f9:3090:2488::2" # BASE 0
  #"37.27.135.250"         # BASE 1 <- PRODUCTION (domain DNS)
  #"2a01:4f9:3070:3984::2" # BASE 1 <- PRODUCTION (domain DNS)
  "77.42.49.79"         # FAILOVER <- Pointing to BASE 0
  "2a01:4f9:fff1:5f::2" # FAILOVER <- Pointing to BASE 0
)
NC_TIMEOUT=3

# nc -zv -w 3 77.42.49.79 20201

failures=()
for vmid in "${VMIDS[@]}"; do
  port_rdp=$((20000 + vmid))
  port_smb=$((10000 + vmid))
  vmid_fails=()

  for host in "${HOST_IPS[@]}"; do
    if ! nc -z -w "$NC_TIMEOUT" "$host" "$port_rdp" 2>/dev/null; then
      vmid_fails+=( "$host RDP ($port_rdp)" )
      failures+=( "VMID $vmid: $host RDP ($port_rdp) FAIL" )
    fi
    if ! nc -z -w "$NC_TIMEOUT" "$host" "$port_smb" 2>/dev/null; then
      vmid_fails+=( "$host SMB ($port_smb)" )
      failures+=( "VMID $vmid: $host SMB ($port_smb) FAIL" )
    fi
  done

  if [[ ${#vmid_fails[@]} -eq 0 ]]; then
    echo "VMID $vmid: OK"
  else
    echo "VMID $vmid: FAIL ($(IFS=,; echo "${vmid_fails[*]}"))"
  fi
done

echo ""
echo "--- Summary ---"
if [[ ${#failures[@]} -eq 0 ]]; then
  echo "All checks passed."
else
  echo "Failures:"
  printf '%s\n' "${failures[@]}"
fi

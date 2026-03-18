#!/usr/bin/env bash
# Check RDP (20000+VMID) and SMB (10000+VMID) connectivity over IPv4 and IPv6 via nc.
# Paste this script on the target machine; set VMIDS, HOST_IPV4, and HOST_IPV6 below.
# Ctrl+C is ignored so the script keeps running; script does not exit so your shell stays open.

trap '' INT

VMIDS=( 201 202 203 )
# Main IPs (NAT ingress). Use these for probes; failover IPs forward to main.
HOST_IPV4="77.42.49.79"
HOST_IPV6="2a01:4f9:fff1:5f::2"
#HOST_IPV4="37.27.135.250"
#HOST_IPV6="2a01:4f9:3070:3984::2"
NC_TIMEOUT=3

failures=()
for vmid in "${VMIDS[@]}"; do
  port_rdp=$((20000 + vmid))
  port_smb=$((10000 + vmid))
  vmid_fails=()

  if ! nc -z -w "$NC_TIMEOUT" "$HOST_IPV4" "$port_rdp" 2>/dev/null; then
    vmid_fails+=( "IPv4 RDP ($port_rdp)" )
    failures+=( "VMID $vmid: IPv4 RDP ($port_rdp) FAIL" )
  fi
  if ! nc -z -w "$NC_TIMEOUT" "$HOST_IPV4" "$port_smb" 2>/dev/null; then
    vmid_fails+=( "IPv4 SMB ($port_smb)" )
    failures+=( "VMID $vmid: IPv4 SMB ($port_smb) FAIL" )
  fi
  if ! nc -z -w "$NC_TIMEOUT" "$HOST_IPV6" "$port_rdp" 2>/dev/null; then
    vmid_fails+=( "IPv6 RDP ($port_rdp)" )
    failures+=( "VMID $vmid: IPv6 RDP ($port_rdp) FAIL" )
  fi
  if ! nc -z -w "$NC_TIMEOUT" "$HOST_IPV6" "$port_smb" 2>/dev/null; then
    vmid_fails+=( "IPv6 SMB ($port_smb)" )
    failures+=( "VMID $vmid: IPv6 SMB ($port_smb) FAIL" )
  fi

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

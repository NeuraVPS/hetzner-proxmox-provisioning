#!/usr/bin/env bash
# Check RDP (20000+VMID) and SMB (10000+VMID) connectivity over IPv4 and IPv6 via nc.
# Paste this script on the target machine; set VMID / VMID_SMB / VMID_RDP and HOST_IPS below.
# Arrays match NeuraVPS scripts/get_on_server_vmids.py (running + firewall flags).
# That script prints VMID, VMID_RDP, VMID_SMB — reorder the two lines here if you paste verbatim.
#   VMID      — both rdpEnabled and sambaEnabled
#   VMID_SMB  — sambaEnabled only
#   VMID_RDP  — rdpEnabled only
# Ctrl+C is ignored during checks (INT is discarded for this script only).

trap '' INT

VMID=( 572 1057 )
VMID_RDP=( )
VMID_SMB=( )

# Ingress IPs to probe (IPv4 and/or IPv6). Add as many entries as needed.
HOST_IPS=(
  "46.62.188.207"         # BASE 0
  "2a01:4f9:3090:2488::2" # BASE 0
  "37.27.135.250"         # BASE 1
  "2a01:4f9:3070:3984::2" # BASE 1
  "77.42.49.79"         # FAILOVER <- Pointing to BASE 1 <- PRODUCTION (domain DNS)
  "2a01:4f9:fff1:5f::2" # FAILOVER <- Pointing to BASE 1 <- PRODUCTION (domain DNS)
)
NC_TIMEOUT=3

# nc -zv -w 3 77.42.49.79 20201

failures=()

# bucket: label in logs; want_rdp / want_smb: 1 or 0
check_one_vmid() {
  local bucket=$1 vmid=$2 want_rdp=$3 want_smb=$4
  local port_rdp=$((20000 + vmid))
  local port_smb=$((10000 + vmid))
  local vmid_fails=()

  for host in "${HOST_IPS[@]}"; do
    if [[ "$want_rdp" -eq 1 ]]; then
      if ! nc -z -w "$NC_TIMEOUT" "$host" "$port_rdp" 2>/dev/null; then
        vmid_fails+=( "$host RDP ($port_rdp)" )
        failures+=( "$bucket $vmid: $host RDP ($port_rdp) FAIL" )
      fi
    fi
    if [[ "$want_smb" -eq 1 ]]; then
      if ! nc -z -w "$NC_TIMEOUT" "$host" "$port_smb" 2>/dev/null; then
        vmid_fails+=( "$host SMB ($port_smb)" )
        failures+=( "$bucket $vmid: $host SMB ($port_smb) FAIL" )
      fi
    fi
  done

  if [[ ${#vmid_fails[@]} -eq 0 ]]; then
    echo "$bucket $vmid: OK"
  else
    echo "$bucket $vmid: FAIL ($(IFS=,; echo "${vmid_fails[*]}"))"
  fi
}

for vmid in "${VMID[@]}"; do
  check_one_vmid VMID "$vmid" 1 1
done
for vmid in "${VMID_SMB[@]}"; do
  check_one_vmid VMID_SMB "$vmid" 0 1
done
for vmid in "${VMID_RDP[@]}"; do
  check_one_vmid VMID_RDP "$vmid" 1 0
done

echo ""
echo "--- Summary ---"
if [[ ${#failures[@]} -eq 0 ]]; then
  echo "All checks passed."
else
  echo "Failures:"
  printf '%s\n' "${failures[@]}"
fi

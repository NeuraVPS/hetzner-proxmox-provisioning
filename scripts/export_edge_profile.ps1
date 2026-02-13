# Run as Administrator BEFORE sysprep. Exports Edge profile to C:\Provisioning\EdgeDefault
# so it can be restored at first logon on deployed VMs (see restore_edge_profile.ps1).

$ErrorActionPreference = 'Stop'
$Source = "$env:LOCALAPPDATA\Microsoft\Edge"
$Dest   = 'C:\Provisioning\EdgeDefault'

if (-not (Test-Path $Source)) {
    Write-Warning "Edge profile not found at $Source. Configure Edge and run again."
    exit 0
}

New-Item -ItemType Directory -Path $Dest -Force | Out-Null
Write-Host "Copying Edge profile to $Dest ..."
robocopy $Source $Dest /E /COPY:DAT /R:2 /W:5 /NFL /NDL /NJH /NJS
if ($LASTEXITCODE -ge 8) { exit $LASTEXITCODE }
Write-Host "Done. You can run sysprep; first logon will restore Edge from $Dest."
exit 0

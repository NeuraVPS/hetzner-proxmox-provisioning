# Run at first logon (e.g. via unattend FirstLogonCommands). Restores Edge profile
# from C:\Provisioning\EdgeDefault into the current user's profile.

$ErrorActionPreference = 'Stop'
$Source = 'C:\Provisioning\EdgeDefault'
$Dest   = "$env:LOCALAPPDATA\Microsoft\Edge"

if (-not (Test-Path $Source)) { exit 0 }

# Avoid overwriting an already-used profile
if (Test-Path "$Dest\User Data") {
    $existing = Get-ChildItem "$Dest\User Data" -ErrorAction SilentlyContinue
    if ($existing) { exit 0 }
}

New-Item -ItemType Directory -Path (Split-Path $Dest) -Force | Out-Null
robocopy $Source $Dest /E /COPY:DAT /R:2 /W:5 /NFL /NDL /NJH /NJS
if ($LASTEXITCODE -ge 8) { exit $LASTEXITCODE }

# Optional: remove source so we don't restore again for other users
# Remove-Item $Source -Recurse -Force -ErrorAction SilentlyContinue
exit 0

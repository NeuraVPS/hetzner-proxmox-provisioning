#Requires -RunAsAdministrator
<#
.SYNOPSIS
  Maps the Hetzner Storage Box SMB share, extracts SQX_<Version>.zip to C:\SQX_<Version>, and adds symlinks on the desktop and Start Menu.

.DESCRIPTION
  Remote run (no local .ps1): iex (irm URL) does not pass arguments into this script's param block.
  Fetch the body and invoke it as a scriptblock so parameters bind correctly (PowerShell 7+ may use irm instead of Invoke-RestMethod):

  & ([scriptblock]::Create((Invoke-RestMethod -Uri 'https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/windows_vm/installers/install_sqx_from_storagebox.ps1' -UseBasicParsing))) -SmbPassword $env:STORAGEBOX_SMB_PASSWORD

  & ([scriptblock]::Create((Invoke-RestMethod -Uri 'https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/windows_vm/installers/install_sqx_from_storagebox.ps1' -UseBasicParsing))) -SmbPassword $env:STORAGEBOX_SMB_PASSWORD -Version 142

  Only use URLs and revisions you trust; this executes code from the network. #Requires Administrator.

.PARAMETER SmbPassword
  Password for the Storage Box SMB user (required).

.PARAMETER Version
  SQX build number: 142 or 143 only. Used for the zip name (SQX_<Version>.zip), install folder (C:\SQX_<Version>), and shortcut labels. Defaults to 143.

.EXAMPLE
  .\install_sqx_from_storagebox.ps1 -SmbPassword $env:STORAGEBOX_SMB_PASSWORD

.EXAMPLE
  .\install_sqx_from_storagebox.ps1 -SmbPassword $env:STORAGEBOX_SMB_PASSWORD -Version 142

.NOTES
  Run elevated. SmbPassword must be supplied every time (no default).
#>

param(
    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string]$SmbPassword,

    [ValidateSet('142', '143')]
    [string]$Version = '143'
)

$ErrorActionPreference = 'Stop'

$UncRoot = '\\u560363-sub1.your-storagebox.de\u560363-sub1'
$ZipName = "SQX_$Version.zip"
$ZipUnc  = Join-Path -Path $UncRoot -ChildPath $ZipName
$SmbUser = 'u560363-sub1'

function Connect-StorageBoxUnc {
    <#
    .NOTES
      Error 1312 (no logon session) hits New-SmbMapping and plain net use under QEMU guest agent / SYSTEM.
      New-SmbGlobalMapping is intended for machine-wide SMB access (services, SYSTEM).
      Fallback: cmdkey stores creds, then net use without inline password; then explicit net use variants.
    #>
    param(
        [Parameter(Mandatory)][string]$RemotePath,
        [Parameter(Mandatory)][string]$User,
        [Parameter(Mandatory)][string]$Password
    )
    if (Get-Command Remove-SmbGlobalMapping -ErrorAction SilentlyContinue) {
        Remove-SmbGlobalMapping -RemotePath $RemotePath -Force -ErrorAction SilentlyContinue
    }
    Remove-SmbMapping -RemotePath $RemotePath -Force -ErrorAction SilentlyContinue
    try { net use $RemotePath /delete /y 2>$null | Out-Null } catch {}

    $server = $null
    if ($RemotePath -match '^\\\\([^\\]+)\\') {
        $server = $Matches[1]
    }

    $errs = @()
    Import-Module SmbShare -ErrorAction SilentlyContinue | Out-Null

    if (Get-Command New-SmbGlobalMapping -ErrorAction SilentlyContinue) {
        try {
            New-SmbGlobalMapping -RemotePath $RemotePath -UserName $User -Password $Password -Persistent:$false -ErrorAction Stop | Out-Null
            return
        } catch {
            $errs += "New-SmbGlobalMapping: $($_.Exception.Message)"
        }
    } else {
        $errs += 'New-SmbGlobalMapping: cmdlet not available'
    }

    try {
        New-SmbMapping -RemotePath $RemotePath -UserName $User -Password $Password -Persistent:$false -ErrorAction Stop | Out-Null
        return
    } catch {
        $errs += "New-SmbMapping: $($_.Exception.Message)"
    }

    $cmdkey = Join-Path $env:SystemRoot 'System32\cmdkey.exe'
    if ($server -and (Test-Path -LiteralPath $cmdkey)) {
        $ck = Start-Process -FilePath $cmdkey -ArgumentList @("/add:$server", "/user:$User", "/pass:$Password") -Wait -NoNewWindow -PassThru
        try {
            if ($ck.ExitCode -eq 0) {
                $p = Start-Process -FilePath 'net.exe' -ArgumentList @('use', $RemotePath, '/persistent:no') -Wait -NoNewWindow -PassThru
                if ($p.ExitCode -eq 0) { return }
                $errs += "net use after cmdkey exit $($p.ExitCode)"
            } else {
                $errs += "cmdkey add exit $($ck.ExitCode)"
            }
        } finally {
            try {
                Start-Process -FilePath $cmdkey -ArgumentList @("/delete:$server") -Wait -NoNewWindow | Out-Null
            } catch { }
        }
    }

    $netVariants = @(
        @('use', $RemotePath, "/user:$User", $Password),
        @('use', $RemotePath, "/user:WORKGROUP\$User", $Password),
        @('use', $RemotePath, "/user:.\$User", $Password)
    )
    foreach ($na in $netVariants) {
        $p = Start-Process -FilePath 'net.exe' -ArgumentList $na -Wait -NoNewWindow -PassThru
        if ($p.ExitCode -eq 0) { return }
    }
    $errs += 'net use explicit variants failed'

    throw "SMB connect failed for $RemotePath. $($errs -join ' | ')"
}

$ExtractRoot = "C:\SQX_$Version"
$ExeTarget   = Join-Path -Path $ExtractRoot -ChildPath 'StrategyQuantX.exe'
$DesktopLink = "C:\Users\Public\Desktop\StrategyQuantX v$Version"
$StartMenuLink = "C:\ProgramData\Microsoft\Windows\Start Menu\Programs\StrategyQuantX v$Version"

try {
    Connect-StorageBoxUnc -RemotePath $UncRoot -User $SmbUser -Password $SmbPassword

    if (-not (Test-Path -LiteralPath $ZipUnc)) {
        throw "Zip not found at: $ZipUnc"
    }

    if (-not (Test-Path -LiteralPath $ExtractRoot)) {
        New-Item -ItemType Directory -Path $ExtractRoot | Out-Null
    }

    Expand-Archive -LiteralPath $ZipUnc -DestinationPath $ExtractRoot -Force

    if (-not (Test-Path -LiteralPath $ExeTarget)) {
        throw "Expected executable missing after extract: $ExeTarget (check zip layout)."
    }

    foreach ($linkPath in @($DesktopLink, $StartMenuLink)) {
        if (Test-Path -LiteralPath $linkPath) {
            Remove-Item -LiteralPath $linkPath -Force
        }
        New-Item -ItemType SymbolicLink -Path $linkPath -Target $ExeTarget | Out-Null
    }
}
finally {
    if (Get-Command Remove-SmbGlobalMapping -ErrorAction SilentlyContinue) {
        Remove-SmbGlobalMapping -RemotePath $UncRoot -Force -ErrorAction SilentlyContinue
    }
    Remove-SmbMapping -RemotePath $UncRoot -Force -ErrorAction SilentlyContinue
}

Write-Host 'Done: extracted to' $ExtractRoot 'and symlinks created.'

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

$ExtractRoot = "C:\SQX_$Version"
$ExeTarget   = Join-Path -Path $ExtractRoot -ChildPath 'StrategyQuantX.exe'
$DesktopLink = "C:\Users\Public\Desktop\StrategyQuantX v$Version"
$StartMenuLink = "C:\ProgramData\Microsoft\Windows\Start Menu\Programs\StrategyQuantX v$Version"

$securePass = ConvertTo-SecureString -String $SmbPassword -AsPlainText -Force
$credential = New-Object System.Management.Automation.PSCredential ($SmbUser, $securePass)

try {
    New-SmbMapping -RemotePath $UncRoot -Credential $credential -Persistent:$false | Out-Null

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
    Remove-SmbMapping -RemotePath $UncRoot -Force -ErrorAction SilentlyContinue
}

Write-Host 'Done: extracted to' $ExtractRoot 'and symlinks created.'

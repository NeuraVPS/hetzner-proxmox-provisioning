#Requires -RunAsAdministrator
<#
.SYNOPSIS
  Maps the Hetzner Storage Box SMB share, extracts QuantAnalyzer4.zip to C:\QuantAnalyzer4, and adds symlinks on the desktop and Start Menu.

.DESCRIPTION
  Remote run (no local .ps1): iex (irm URL) does not pass arguments into this script's param block.
  Fetch the body and invoke it as a scriptblock so parameters bind correctly (PowerShell 7+ may use irm instead of Invoke-RestMethod):

  & ([scriptblock]::Create((Invoke-RestMethod -Uri 'https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/windows_vm/installers/install_qa_from_storagebox.ps1' -UseBasicParsing))) -SmbPassword $env:STORAGEBOX_SMB_PASSWORD

  Only use URLs and revisions you trust; this executes code from the network. #Requires Administrator.

.PARAMETER SmbPassword
  Password for the Storage Box SMB user (required).

.EXAMPLE
  .\install_qa_from_storagebox.ps1 -SmbPassword $env:STORAGEBOX_SMB_PASSWORD

.NOTES
  Run elevated. SmbPassword must be supplied every time (no default).
#>

param(
    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string]$SmbPassword
)

$ErrorActionPreference = 'Stop'

$UncRoot = '\\u560363-sub1.your-storagebox.de\u560363-sub1'
$ZipName = "QuantAnalyzer4.zip"
$ZipUnc  = Join-Path -Path $UncRoot -ChildPath $ZipName
$SmbUser = 'u560363-sub1'

$ExtractRoot = "C:\QuantAnalyzer4"
$ExeTarget   = Join-Path -Path $ExtractRoot -ChildPath "QuantAnalyzer4.exe"
$DesktopLink = "C:\Users\Public\Desktop\QuantAnalyzer4"
$StartMenuLink = "C:\ProgramData\Microsoft\Windows\Start Menu\Programs\QuantAnalyzer4"

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

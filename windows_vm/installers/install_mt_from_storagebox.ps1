#Requires -RunAsAdministrator
<#
.SYNOPSIS
  Maps the Storage Box SMB share, installs MetaTrader 4 or 5 into numbered folders, adds desktop and Start Menu symlinks, and registers file associations for instance 001 (MT5 only).

.DESCRIPTION
  Remote run (no local .ps1): iex (irm URL) does not pass arguments into this script's param block.
  Fetch the body and invoke it as a scriptblock so parameters bind correctly (PowerShell 7+ may use irm instead of Invoke-RestMethod):

  & ([scriptblock]::Create((Invoke-RestMethod -Uri 'https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/windows_vm/installers/install_mt_from_storagebox.ps1' -UseBasicParsing))) -SmbPassword $env:STORAGEBOX_SMB_PASSWORD

  & ([scriptblock]::Create((Invoke-RestMethod -Uri 'https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/windows_vm/installers/install_mt_from_storagebox.ps1' -UseBasicParsing))) -SmbPassword $env:STORAGEBOX_SMB_PASSWORD -InstanceCount 5 -MetaTraderVersion 5

  & ([scriptblock]::Create((Invoke-RestMethod -Uri 'https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/windows_vm/installers/install_mt_from_storagebox.ps1' -UseBasicParsing))) -SmbPassword $env:STORAGEBOX_SMB_PASSWORD -MetaTraderVersion 4

  Only use URLs and revisions you trust; this executes code from the network. #Requires Administrator.

.PARAMETER InstanceCount
  Number of instances to create (1 through 999; folders use 001..00N). The zip is extracted to 001; 002..N are full folder copies. Defaults to 1.

.PARAMETER SmbPassword
  Password for the Storage Box SMB user (required).

.PARAMETER MetaTraderVersion
  Major version: 5 (default) or 4. Chooses zip name, folder labels, and terminal/editor executables. File associations are applied only for version 5.

.EXAMPLE
  .\install_mt_from_storagebox.ps1 -SmbPassword $env:STORAGEBOX_SMB_PASSWORD

.EXAMPLE
  .\install_mt_from_storagebox.ps1 -SmbPassword $env:STORAGEBOX_SMB_PASSWORD -InstanceCount 5

.EXAMPLE
  .\install_mt_from_storagebox.ps1 -SmbPassword $env:STORAGEBOX_SMB_PASSWORD -MetaTraderVersion 4

.NOTES
  Run elevated. SmbPassword must be supplied every time (no default).
#>

param(
    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string]$SmbPassword,

    [ValidateSet(4, 5)]
    [int]$MetaTraderVersion = 5,

    [ValidateRange(1, 999)]
    [int]$InstanceCount = 1
)

$ErrorActionPreference = 'Stop'

$UncRoot = '\\u560363-sub1.your-storagebox.de\u560363-sub1'
$ZipName = "MetaTrader$MetaTraderVersion.zip"
$ZipUnc  = Join-Path -Path $UncRoot -ChildPath $ZipName
$SmbUser = 'u560363-sub1'

$MtRoot        = 'C:\MetaTrader'
$StartMenuMt   = Join-Path -Path 'C:\ProgramData\Microsoft\Windows\Start Menu\Programs' -ChildPath "MetaTrader $MetaTraderVersion"
$PublicDesktop = 'C:\Users\Public\Desktop'

if ($MetaTraderVersion -eq 5) {
    $TerminalExeName = 'terminal64.exe'
    $EditorExeName = 'metaeditor64.exe'
} else {
    $TerminalExeName = 'terminal.exe'
    $EditorExeName = 'metaeditor.exe'
}

function Get-MtInstanceFolderName {
    param([int]$Index)
    return ('MetaTrader {0} - {1:000}' -f $MetaTraderVersion, $Index)
}

function Get-MtInstancePath {
    param([int]$Index)
    return Join-Path -Path $MtRoot -ChildPath (Get-MtInstanceFolderName -Index $Index)
}

$securePass = ConvertTo-SecureString -String $SmbPassword -AsPlainText -Force
$credential = New-Object System.Management.Automation.PSCredential ($SmbUser, $securePass)

try {
    New-SmbMapping -RemotePath $UncRoot -Credential $credential -Persistent:$false | Out-Null

    if (-not (Test-Path -LiteralPath $ZipUnc)) {
        throw "Zip not found at: $ZipUnc"
    }

    if (-not (Test-Path -LiteralPath $MtRoot)) {
        New-Item -ItemType Directory -Path $MtRoot | Out-Null
    }
    if (-not (Test-Path -LiteralPath $StartMenuMt)) {
        New-Item -ItemType Directory -Path $StartMenuMt | Out-Null
    }

    $firstPath = Get-MtInstancePath -Index 1
    if (-not (Test-Path -LiteralPath $firstPath)) {
        New-Item -ItemType Directory -Path $firstPath | Out-Null
    }

    Expand-Archive -LiteralPath $ZipUnc -DestinationPath $firstPath -Force

    $terminalExe001 = Join-Path -Path $firstPath -ChildPath $TerminalExeName
    if (-not (Test-Path -LiteralPath $terminalExe001)) {
        throw "Expected executable missing after extract: $terminalExe001 (check zip layout)."
    }

    for ($i = 2; $i -le $InstanceCount; $i++) {
        $dest = Get-MtInstancePath -Index $i
        if (Test-Path -LiteralPath $dest) {
            Remove-Item -LiteralPath $dest -Recurse -Force
        }
        Copy-Item -LiteralPath $firstPath -Destination $dest -Recurse -Force
    }

    for ($i = 1; $i -le $InstanceCount; $i++) {
        $folder = Get-MtInstancePath -Index $i
        $terminalExe = Join-Path -Path $folder -ChildPath $TerminalExeName
        if (-not (Test-Path -LiteralPath $terminalExe)) {
            throw "Missing $TerminalExeName for instance $i : $terminalExe"
        }

        $label = Get-MtInstanceFolderName -Index $i
        $desktopLink = Join-Path -Path $PublicDesktop -ChildPath $label
        $startMenuLink = Join-Path -Path $StartMenuMt -ChildPath $label

        foreach ($linkPath in @($desktopLink, $startMenuLink)) {
            if (Test-Path -LiteralPath $linkPath) {
                Remove-Item -LiteralPath $linkPath -Force
            }
            New-Item -ItemType SymbolicLink -Path $linkPath -Target $terminalExe | Out-Null
        }
    }

    if ($MetaTraderVersion -eq 4) {
        # MT4 file associations (instance 001): add $extMap / $openCommands and registry loop when extensions are defined.
    } elseif ($MetaTraderVersion -eq 5) {
        # Default file associations for MQL/MT5 types -> instance 001 (see WINDOWS_PREPARATION.md).
        $classes = 'HKLM:\SOFTWARE\Classes'
        $terminalExe = Join-Path -Path $firstPath -ChildPath $TerminalExeName
        $editorExe = Join-Path -Path $firstPath -ChildPath $EditorExeName
        if (-not (Test-Path -LiteralPath $editorExe)) {
            throw "Expected editor missing for associations: $editorExe"
        }

        $extMap = @{
            '.ex5' = 'EX5.File'
            '.mq5' = 'MQL5.File'
            '.mqh' = 'MQL5.Header'
            '.mt5' = 'MetaTrader 5 Export File'
        }

        foreach ($ext in $extMap.Keys) {
            $extKey = Join-Path $classes $ext
            New-Item -Path $extKey -Force | Out-Null
            Set-ItemProperty -Path $extKey -Name '(Default)' -Value $extMap[$ext] -Type String
        }

        $openCommands = @{
            'EX5.File'                   = "`"$terminalExe`" `"%1`""
            'MQL5.File'                  = "`"$editorExe`" `"%1`""
            'MQL5.Header'                = "`"$editorExe`" `"%1`""
            'MetaTrader 5 Export File' = "`"$terminalExe`" `"%1`""
            'mql5buy'                    = "`"$terminalExe`" `"%1`""
            'metaeditor5'                = "`"$editorExe`" `"%1`""
        }

        foreach ($progId in $openCommands.Keys) {
            $progPath = Join-Path $classes $progId
            New-Item -Path $progPath -Force | Out-Null

            if ($progId -in @('mql5buy', 'metaeditor5')) {
                Set-ItemProperty -Path $progPath -Name 'URL Protocol' -Value '' -Type String
            }

            $cmdPath = Join-Path $progPath 'shell\open\command'
            New-Item -Path $cmdPath -Force | Out-Null
            Set-ItemProperty -Path $cmdPath -Name '(Default)' -Value $openCommands[$progId] -Type String
        }
    }
}
finally {
    Remove-SmbMapping -RemotePath $UncRoot -Force -ErrorAction SilentlyContinue
}

$summary = "Done: $InstanceCount MetaTrader $MetaTraderVersion instance(s) under $MtRoot; symlinks applied."
if ($MetaTraderVersion -eq 5) {
    $summary += ' File associations (001) applied.'
}
Write-Host $summary

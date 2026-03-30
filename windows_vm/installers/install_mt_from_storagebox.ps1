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

try {
    Connect-StorageBoxUnc -RemotePath $UncRoot -User $SmbUser -Password $SmbPassword

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
        # Default file associations for MQL4 types -> instance 001.
        $classes = 'HKLM:\SOFTWARE\Classes'
        $terminalExe = Join-Path -Path $firstPath -ChildPath $TerminalExeName
        $editorExe = Join-Path -Path $firstPath -ChildPath $EditorExeName
        if (-not (Test-Path -LiteralPath $editorExe)) {
            throw "Expected editor missing for associations: $editorExe"
        }

        # .mq4 -> MQL4.File and basic ShellNew.
        $mq4ExtKey = Join-Path $classes '.mq4'
        New-Item -Path $mq4ExtKey -Force | Out-Null
        Set-ItemProperty -Path $mq4ExtKey -Name '(Default)' -Value 'MQL4.File' -Type String
        $mq4ShellNewKey = Join-Path $mq4ExtKey 'ShellNew'
        New-Item -Path $mq4ShellNewKey -Force | Out-Null
        New-ItemProperty -Path $mq4ShellNewKey -Name 'NullFile' -Value '' -PropertyType String -Force | Out-Null

        # MQL4.File ProgID.
        $mql4ProgKey = Join-Path $classes 'MQL4.File'
        New-Item -Path $mql4ProgKey -Force | Out-Null
        Set-ItemProperty -Path $mql4ProgKey -Name '(Default)' -Value 'MQL4 Source File' -Type String

        $mql4DefaultIconKey = Join-Path $mql4ProgKey 'DefaultIcon'
        New-Item -Path $mql4DefaultIconKey -Force | Out-Null
        Set-ItemProperty -Path $mql4DefaultIconKey -Name '(Default)' -Value "$editorExe,1" -Type String

        $mql4CmdKey = Join-Path $mql4ProgKey 'shell\open\command'
        New-Item -Path $mql4CmdKey -Force | Out-Null
        Set-ItemProperty -Path $mql4CmdKey -Name '(Default)' -Value "`"$editorExe`" `"%1`"" -Type String

        $mql4ShellNewKey = Join-Path $mql4ProgKey 'ShellNew'
        New-Item -Path $mql4ShellNewKey -Force | Out-Null
        New-ItemProperty -Path $mql4ShellNewKey -Name 'NullFile' -Value '' -PropertyType String -Force | Out-Null

        # mql4buy URL protocol.
        $mql4BuyKey = Join-Path $classes 'mql4buy'
        New-Item -Path $mql4BuyKey -Force | Out-Null
        Set-ItemProperty -Path $mql4BuyKey -Name '(Default)' -Value 'URL:MQL4 Buy Protocol' -Type String
        Set-ItemProperty -Path $mql4BuyKey -Name 'URL Protocol' -Value '' -Type String

        $mql4BuyDefaultIconKey = Join-Path $mql4BuyKey 'DefaultIcon'
        New-Item -Path $mql4BuyDefaultIconKey -Force | Out-Null
        Set-ItemProperty -Path $mql4BuyDefaultIconKey -Name '(Default)' -Value "$terminalExe,1" -Type String

        $mql4BuyCmdKey = Join-Path $mql4BuyKey 'shell\open\command'
        New-Item -Path $mql4BuyCmdKey -Force | Out-Null
        Set-ItemProperty -Path $mql4BuyCmdKey -Name '(Default)' -Value "`"$terminalExe`" `"%1`"" -Type String
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

        $ex5DefaultIconKey = Join-Path (Join-Path $classes 'EX5.File') 'DefaultIcon'
        New-Item -Path $ex5DefaultIconKey -Force | Out-Null
        Set-ItemProperty -Path $ex5DefaultIconKey -Name '(Default)' -Value "$terminalExe,2" -Type String

        $mql5DefaultIconKey = Join-Path (Join-Path $classes 'MQL5.File') 'DefaultIcon'
        New-Item -Path $mql5DefaultIconKey -Force | Out-Null
        Set-ItemProperty -Path $mql5DefaultIconKey -Name '(Default)' -Value "$editorExe,1" -Type String

        $mql5HeaderDefaultIconKey = Join-Path (Join-Path $classes 'MQL5.Header') 'DefaultIcon'
        New-Item -Path $mql5HeaderDefaultIconKey -Force | Out-Null
        Set-ItemProperty -Path $mql5HeaderDefaultIconKey -Name '(Default)' -Value "$editorExe,2" -Type String

        $mt5ExportDefaultIconKey = Join-Path (Join-Path $classes 'MetaTrader 5 Export File') 'DefaultIcon'
        New-Item -Path $mt5ExportDefaultIconKey -Force | Out-Null
        Set-ItemProperty -Path $mt5ExportDefaultIconKey -Name '(Default)' -Value "$terminalExe,15" -Type String

        $mql5BuyDefaultIconKey = Join-Path (Join-Path $classes 'mql5buy') 'DefaultIcon'
        New-Item -Path $mql5BuyDefaultIconKey -Force | Out-Null
        Set-ItemProperty -Path $mql5BuyDefaultIconKey -Name '(Default)' -Value "$terminalExe,1" -Type String

        $metaeditor5DefaultIconKey = Join-Path (Join-Path $classes 'metaeditor5') 'DefaultIcon'
        New-Item -Path $metaeditor5DefaultIconKey -Force | Out-Null
        Set-ItemProperty -Path $metaeditor5DefaultIconKey -Name '(Default)' -Value "$editorExe,1" -Type String

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
    if (Get-Command Remove-SmbGlobalMapping -ErrorAction SilentlyContinue) {
        Remove-SmbGlobalMapping -RemotePath $UncRoot -Force -ErrorAction SilentlyContinue
    }
    Remove-SmbMapping -RemotePath $UncRoot -Force -ErrorAction SilentlyContinue
}

$summary = "Done: $InstanceCount MetaTrader $MetaTraderVersion instance(s) under $MtRoot; symlinks applied."
if ($MetaTraderVersion -eq 5) {
    $summary += ' File associations (001) applied.'
}
Write-Host $summary

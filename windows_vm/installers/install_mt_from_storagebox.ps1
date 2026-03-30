#Requires -RunAsAdministrator
<#
.SYNOPSIS
  Maps the Storage Box SMB share, installs MetaTrader 4 or 5 into numbered folders, adds desktop and Start Menu shortcuts (.lnk) under Programs\MetaTrader\, and registers file associations for instance 001 (MT5 only).

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
  If file association icons do not update immediately, sign out or restart Explorer.
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
# Start Menu folder name is always "MetaTrader"; version (4/5) appears only in each shortcut label (Get-MtInstanceFolderName).
$StartMenuMt   = Join-Path -Path 'C:\ProgramData\Microsoft\Windows\Start Menu\Programs' -ChildPath 'MetaTrader'
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

function New-MtShellShortcut {
    <#
    .NOTES
      Start Menu "All apps" lists .lnk shell links; symlinks (SymbolicLink) are often not shown.
    #>
    param(
        [Parameter(Mandatory)][string]$ShortcutPath,
        [Parameter(Mandatory)][string]$TargetPath,
        [Parameter(Mandatory)][string]$WorkingDirectory
    )
    if (-not $ShortcutPath.EndsWith('.lnk', [System.StringComparison]::OrdinalIgnoreCase)) {
        $ShortcutPath = "$ShortcutPath.lnk"
    }
    $legacyNoExt = $ShortcutPath -replace '\.lnk$', ''
    $dir = Split-Path -Parent $ShortcutPath
    if (-not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    if (Test-Path -LiteralPath $ShortcutPath) {
        Remove-Item -LiteralPath $ShortcutPath -Force
    }
    if (-not [string]::Equals($legacyNoExt, $ShortcutPath, [System.StringComparison]::OrdinalIgnoreCase) -and (Test-Path -LiteralPath $legacyNoExt)) {
        Remove-Item -LiteralPath $legacyNoExt -Force -ErrorAction SilentlyContinue
    }
    $shell = New-Object -ComObject WScript.Shell
    $sc = $shell.CreateShortcut($ShortcutPath)
    $sc.TargetPath = $TargetPath
    $sc.WorkingDirectory = $WorkingDirectory
    if (Test-Path -LiteralPath $TargetPath) {
        $sc.IconLocation = "$TargetPath,0"
    }
    $sc.Save()
}

function Remove-MtRegistryKeyIfExists {
    param([Parameter(Mandatory)][string]$LiteralPath)
    if (Test-Path -LiteralPath $LiteralPath) {
        Remove-Item -LiteralPath $LiteralPath -Recurse -Force
    }
}

function Format-MtDefaultIconRegValue {
    <#
    .NOTES
      HKCR\...\DefaultIcon uses "C:\path with spaces\app.exe",index — quotes required when the path has spaces.
    #>
    param(
        [Parameter(Mandatory)][string]$ExePath,
        [int]$IconIndex = 0
    )
    if (-not (Test-Path -LiteralPath $ExePath)) {
        throw "DefaultIcon source missing: $ExePath"
    }
    $full = [System.IO.Path]::GetFullPath((Resolve-Path -LiteralPath $ExePath).Path)
    return '"' + $full + '",' + $IconIndex
}

function Set-MtDefaultIconRegValueRegExe {
    <#
    .NOTES
      Writes DefaultIcon via reg.exe so the stored string matches Explorer (quoted exe path, comma, icon index).
    #>
    param(
        [Parameter(Mandatory)][string]$KeyPathUnderHKLM,
        [Parameter(Mandatory)][string]$ExePath,
        [int]$IconIndex = 0
    )
    $data = Format-MtDefaultIconRegValue -ExePath $ExePath -IconIndex $IconIndex
    $regFull = 'HKLM\' + ($KeyPathUnderHKLM -replace '^HKLM\\', '' -replace '^\\', '')
    & reg.exe @('add', $regFull, '/ve', '/t', 'REG_SZ', '/d', $data, '/f') | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "reg.exe add DefaultIcon failed ($LASTEXITCODE): $regFull"
    }
}

function Update-MtShellIconCache {
    $ie4 = Join-Path $env:SystemRoot 'System32\ie4uinit.exe'
    if (Test-Path -LiteralPath $ie4) {
        Start-Process -FilePath $ie4 -ArgumentList @('-show') -WindowStyle Hidden -ErrorAction SilentlyContinue | Out-Null
    }
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

        New-MtShellShortcut -ShortcutPath $desktopLink -TargetPath $terminalExe -WorkingDirectory $folder
        New-MtShellShortcut -ShortcutPath $startMenuLink -TargetPath $terminalExe -WorkingDirectory $folder
    }

    if ($MetaTraderVersion -eq 4) {
        # Default file associations for MQL4 types -> instance 001.
        $classes = 'HKLM:\SOFTWARE\Classes'
        $terminalExe = Join-Path -Path $firstPath -ChildPath $TerminalExeName
        $editorExe = Join-Path -Path $firstPath -ChildPath $EditorExeName
        if (-not (Test-Path -LiteralPath $editorExe)) {
            throw "Expected editor missing for associations: $editorExe"
        }

        # Drop stale ProgIDs / extensions so DefaultIcon and commands are recreated cleanly.
        Remove-MtRegistryKeyIfExists -LiteralPath (Join-Path $classes 'MQL4.File')
        Remove-MtRegistryKeyIfExists -LiteralPath (Join-Path $classes 'mql4buy')
        Remove-MtRegistryKeyIfExists -LiteralPath (Join-Path $classes '.mq4')

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

        $mql4BuyCmdKey = Join-Path $mql4BuyKey 'shell\open\command'
        New-Item -Path $mql4BuyCmdKey -Force | Out-Null
        Set-ItemProperty -Path $mql4BuyCmdKey -Name '(Default)' -Value "`"$terminalExe`" `"%1`"" -Type String

        Set-MtDefaultIconRegValueRegExe -KeyPathUnderHKLM 'SOFTWARE\Classes\MQL4.File\DefaultIcon' -ExePath $editorExe -IconIndex 0
        Set-MtDefaultIconRegValueRegExe -KeyPathUnderHKLM 'SOFTWARE\Classes\mql4buy\DefaultIcon' -ExePath $terminalExe -IconIndex 0
        Update-MtShellIconCache
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

        $mt5ProgIds = @(
            'EX5.File'
            'MQL5.File'
            'MQL5.Header'
            'MetaTrader 5 Export File'
            'mql5buy'
            'metaeditor5'
        )
        foreach ($id in $mt5ProgIds) {
            Remove-MtRegistryKeyIfExists -LiteralPath (Join-Path $classes $id)
        }
        foreach ($ext in $extMap.Keys) {
            Remove-MtRegistryKeyIfExists -LiteralPath (Join-Path $classes $ext)
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

        # DefaultIcon resource indices in terminal64.exe / metaeditor64.exe (MT5 shell expectations).
        Set-MtDefaultIconRegValueRegExe -KeyPathUnderHKLM 'SOFTWARE\Classes\EX5.File\DefaultIcon' -ExePath $terminalExe -IconIndex 2
        Set-MtDefaultIconRegValueRegExe -KeyPathUnderHKLM 'SOFTWARE\Classes\MQL5.File\DefaultIcon' -ExePath $editorExe -IconIndex 1
        Set-MtDefaultIconRegValueRegExe -KeyPathUnderHKLM 'SOFTWARE\Classes\MQL5.Header\DefaultIcon' -ExePath $editorExe -IconIndex 2
        Set-MtDefaultIconRegValueRegExe -KeyPathUnderHKLM 'SOFTWARE\Classes\MetaTrader 5 Export File\DefaultIcon' -ExePath $terminalExe -IconIndex 15
        Set-MtDefaultIconRegValueRegExe -KeyPathUnderHKLM 'SOFTWARE\Classes\mql5buy\DefaultIcon' -ExePath $terminalExe -IconIndex 1
        Set-MtDefaultIconRegValueRegExe -KeyPathUnderHKLM 'SOFTWARE\Classes\metaeditor5\DefaultIcon' -ExePath $editorExe -IconIndex 1
        Update-MtShellIconCache
    }
}
finally {
    if (Get-Command Remove-SmbGlobalMapping -ErrorAction SilentlyContinue) {
        Remove-SmbGlobalMapping -RemotePath $UncRoot -Force -ErrorAction SilentlyContinue
    }
    Remove-SmbMapping -RemotePath $UncRoot -Force -ErrorAction SilentlyContinue
}

$summary = "Done: $InstanceCount MetaTrader $MetaTraderVersion instance(s) under $MtRoot; desktop and Start Menu shortcuts applied."
if ($MetaTraderVersion -eq 5) {
    $summary += ' File associations (001) applied.'
}
Write-Host $summary

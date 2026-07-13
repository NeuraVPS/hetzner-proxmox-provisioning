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

.PARAMETER ZipVariant
  Optional payload variant: extracts MetaTrader<ver>-<Variant>.zip instead of MetaTrader<ver>.zip
  (e.g. -ZipVariant PDFY99 -> MetaTrader5-PDFY99.zip, the Portfolios.io DFY preconfigured build).

.PARAMETER PinToTaskbar
  Pin all instances to the taskbar (LayoutModification.xml into Default + every real profile,
  clearing each profile's Taskband blob offline so the pins apply at next logon).

.PARAMETER NoDesktopShortcuts
  Skip desktop shortcuts and delete pre-existing ones for this MT version.

.PARAMETER AddToStartup
  Create an all-users Startup shortcut per instance (auto-launch at logon, minimised hint).

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
    [int]$InstanceCount = 1,

    # Optional zip variant: MetaTrader<ver>-<Variant>.zip instead of MetaTrader<ver>.zip
    # (e.g. -ZipVariant PDFY99 -> MetaTrader5-PDFY99.zip, the Portfolios.io DFY build).
    [ValidatePattern('^[A-Za-z0-9._-]*$')]
    [string]$ZipVariant = '',

    # Pin every installed instance to the taskbar via LayoutModification.xml
    # (Default profile + every real user profile; offline-safe, applies at next logon).
    [switch]$PinToTaskbar,

    # Do not create desktop shortcuts, and remove any pre-existing ones for this MT version.
    [switch]$NoDesktopShortcuts,

    # Create an all-users Startup shortcut per instance (auto-launch at logon, minimised).
    [switch]$AddToStartup
)

$ErrorActionPreference = 'Stop'

$UncRoot = '\\u560363-sub1.your-storagebox.de\u560363-sub1'
if ($ZipVariant) {
    $ZipName = "MetaTrader$MetaTraderVersion-$ZipVariant.zip"
} else {
    $ZipName = "MetaTrader$MetaTraderVersion.zip"
}
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
$AllUsersStartup = 'C:\ProgramData\Microsoft\Windows\Start Menu\Programs\StartUp'

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
      WScript.Shell CreateShortcut must run in an STA thread; PowerShell 7+ or non-interactive
      hosts may use MTA and produce shortcuts that do not behave like Explorer-created .lnk files.
      We delegate to Windows PowerShell 5.1 with -STA when available.
    #>
    param(
        [Parameter(Mandatory)][string]$ShortcutPath,
        [Parameter(Mandatory)][string]$TargetPath,
        [Parameter(Mandatory)][string]$WorkingDirectory,
        [int]$WindowStyle = 1                       # 1=Normal, 3=Maximized, 7=Minimized
    )
    if (-not $ShortcutPath.EndsWith('.lnk', [System.StringComparison]::OrdinalIgnoreCase)) {
        $ShortcutPath = "$ShortcutPath.lnk"
    }
    $lnkFull = [System.IO.Path]::GetFullPath($ShortcutPath)
    $targetFull = [System.IO.Path]::GetFullPath((Resolve-Path -LiteralPath $TargetPath).Path)
    $workFull = [System.IO.Path]::GetFullPath((Resolve-Path -LiteralPath $WorkingDirectory).Path)

    $legacyNoExt = $lnkFull -replace '\.lnk$', ''
    $dir = Split-Path -Parent $lnkFull
    if (-not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    if (Test-Path -LiteralPath $lnkFull) {
        Remove-Item -LiteralPath $lnkFull -Force
    }
    if (-not [string]::Equals($legacyNoExt, $lnkFull, [System.StringComparison]::OrdinalIgnoreCase) -and (Test-Path -LiteralPath $legacyNoExt)) {
        Remove-Item -LiteralPath $legacyNoExt -Force -ErrorAction SilentlyContinue
    }

    $winPs = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if (Test-Path -LiteralPath $winPs) {
        # Embed paths as UTF-8 Base64 in the helper file only: no extra argv (spaces break Start-Process),
        # and no reliance on env inheritance into the child (RDP / some hosts drop inherited vars).
        $encLnk = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($lnkFull))
        $encTgt = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($targetFull))
        $encWrk = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($workFull))
        $helper = @"
`$ErrorActionPreference = 'Stop'
`$Lnk = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$encLnk'))
`$Tgt = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$encTgt'))
`$Wrk = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$encWrk'))
`$w = New-Object -ComObject WScript.Shell
`$s = `$w.CreateShortcut(`$Lnk)
`$s.TargetPath = `$Tgt
`$s.WorkingDirectory = `$Wrk
`$s.WindowStyle = $WindowStyle
if (Test-Path -LiteralPath `$Tgt) { `$s.IconLocation = (`$Tgt + ',0') }
`$s.Save()
"@
        $tmp = Join-Path $env:TEMP ("neuravps-lnk-{0}.ps1" -f [guid]::NewGuid().ToString('N'))
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($tmp, $helper, $utf8NoBom)
        try {
            $p = Start-Process -FilePath $winPs -ArgumentList @(
                '-NoProfile', '-ExecutionPolicy', 'Bypass', '-STA', '-File', $tmp
            ) -Wait -NoNewWindow -PassThru
            if ($p.ExitCode -ne 0) {
                throw "Shell shortcut helper exited with code $($p.ExitCode)"
            }
        } finally {
            Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
        }
    } else {
        $shell = New-Object -ComObject WScript.Shell
        $sc = $shell.CreateShortcut($lnkFull)
        $sc.TargetPath = $targetFull
        $sc.WorkingDirectory = $workFull
        $sc.WindowStyle = $WindowStyle
        if (Test-Path -LiteralPath $targetFull) {
            $sc.IconLocation = "$targetFull,0"
        }
        $sc.Save()
    }

    if (-not (Test-Path -LiteralPath $lnkFull)) {
        throw "Shortcut was not created: $lnkFull"
    }
    $item = Get-Item -LiteralPath $lnkFull
    if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        throw "Shortcut path is a reparse point, not a shell .lnk: $lnkFull"
    }
    $bytes = [System.IO.File]::ReadAllBytes($lnkFull)
    if ($bytes.Length -lt 4) {
        throw "Shortcut file is empty or unreadable: $lnkFull"
    }
    $headerSize = [BitConverter]::ToUInt32($bytes, 0)
    if ($headerSize -ne 76) {
        throw "File is not a valid shell link (.lnk header size 76 expected): $lnkFull"
    }
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

    if ($NoDesktopShortcuts) {
        # Remove any pre-existing desktop shortcuts for this MT version (Public + user desktops).
        $desktopDirs = @($PublicDesktop) + @(Get-ChildItem 'C:\Users' -Directory -ErrorAction SilentlyContinue |
            ForEach-Object { Join-Path $_.FullName 'Desktop' })
        foreach ($dk in ($desktopDirs | Select-Object -Unique)) {
            Get-ChildItem -LiteralPath $dk -Filter ('MetaTrader {0} - *.lnk' -f $MetaTraderVersion) -ErrorAction SilentlyContinue |
                Remove-Item -Force -ErrorAction SilentlyContinue
        }
    }
    if ($AddToStartup) {
        # Drop stale Startup entries for this MT version before recreating (idempotent re-runs).
        Get-ChildItem -LiteralPath $AllUsersStartup -Filter ('MetaTrader {0} - *.lnk' -f $MetaTraderVersion) -ErrorAction SilentlyContinue |
            Remove-Item -Force -ErrorAction SilentlyContinue
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

        if (-not $NoDesktopShortcuts) {
            New-MtShellShortcut -ShortcutPath $desktopLink -TargetPath $terminalExe -WorkingDirectory $folder
        }
        New-MtShellShortcut -ShortcutPath $startMenuLink -TargetPath $terminalExe -WorkingDirectory $folder

        if ($AddToStartup) {
            # MetaTrader restores its own saved window placement; WindowStyle 7 is a best-effort
            # hint - the durable minimised state comes from the config baked into the zip variant.
            $startupLink = Join-Path -Path $AllUsersStartup -ChildPath $label
            New-MtShellShortcut -ShortcutPath $startupLink -TargetPath $terminalExe -WorkingDirectory $folder -WindowStyle 7
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

        # DefaultIcon: MQL4.File = metaeditor (001), index 3; mql4buy = terminal from instance 002, index 1 (fallback 001 if single instance).
        $mql4BuyIconTerminalExe = Join-Path -Path (Get-MtInstancePath -Index 2) -ChildPath $TerminalExeName
        if (-not (Test-Path -LiteralPath $mql4BuyIconTerminalExe)) {
            $mql4BuyIconTerminalExe = $terminalExe
        }

        Set-MtDefaultIconRegValueRegExe -KeyPathUnderHKLM 'SOFTWARE\Classes\MQL4.File\DefaultIcon' -ExePath $editorExe -IconIndex 3
        Set-MtDefaultIconRegValueRegExe -KeyPathUnderHKLM 'SOFTWARE\Classes\mql4buy\DefaultIcon' -ExePath $mql4BuyIconTerminalExe -IconIndex 1
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

    if ($PinToTaskbar) {
        # Pin every instance to the taskbar for current AND future users. LayoutModification.xml
        # is the only supported pin mechanism on Server 2025 (no "Pin to taskbar" verb). Explorer
        # applies it while building the taskbar at logon whenever the profile has no
        # Taskband\Favorites blob, so for existing profiles we clear that blob offline and the
        # next logon rebuilds the pins. Runs fine as SYSTEM with nobody logged in (guest agent).
        $pinLinks = 1..$InstanceCount | ForEach-Object {
            Join-Path -Path $StartMenuMt -ChildPath ((Get-MtInstanceFolderName -Index $_) + '.lnk')
        }
        $pinApps = ($pinLinks | ForEach-Object {
            '        <taskbar:DesktopApp DesktopApplicationLinkPath="' + $_ + '" />'
        }) -join "`r`n"
        $layoutXml = @"
<?xml version="1.0" encoding="utf-8"?>
<LayoutModificationTemplate
    xmlns="http://schemas.microsoft.com/Start/2014/LayoutModification"
    xmlns:defaultlayout="http://schemas.microsoft.com/Start/2014/FullDefaultLayout"
    xmlns:start="http://schemas.microsoft.com/Start/2014/StartLayout"
    xmlns:taskbar="http://schemas.microsoft.com/Start/2014/TaskbarLayout"
    Version="1">
  <CustomTaskbarLayoutCollection PinListPlacement="Replace">
    <defaultlayout:TaskbarLayout>
      <taskbar:TaskbarPinList>
$pinApps
      </taskbar:TaskbarPinList>
    </defaultlayout:TaskbarLayout>
  </CustomTaskbarLayoutCollection>
</LayoutModificationTemplate>
"@
        function Write-MtLayoutXml {
            param([Parameter(Mandatory)][string]$ProfileRoot)
            $shellDir = Join-Path -Path $ProfileRoot -ChildPath 'AppData\Local\Microsoft\Windows\Shell'
            New-Item -ItemType Directory -Path $shellDir -Force | Out-Null
            Set-Content -LiteralPath (Join-Path -Path $shellDir -ChildPath 'LayoutModification.xml') -Value $layoutXml -Encoding UTF8
        }
        function Invoke-MtRegExe {
            # reg.exe via Start-Process: native stderr must not reach PS streams
            # ($ErrorActionPreference='Stop' turns it into a terminating NativeCommandError).
            param([Parameter(Mandatory)][string[]]$ArgumentList)
            $p = Start-Process -FilePath 'reg.exe' -ArgumentList $ArgumentList -Wait -NoNewWindow -PassThru
            return $p.ExitCode
        }

        Write-MtLayoutXml -ProfileRoot 'C:\Users\Default'

        $taskbandSub = 'Software\Microsoft\Windows\CurrentVersion\Explorer\Taskband'
        $profiles = Get-CimInstance Win32_UserProfile -ErrorAction SilentlyContinue |
            Where-Object { $_.SID -like 'S-1-5-21-*' -and $_.LocalPath -and (Test-Path -LiteralPath $_.LocalPath) }
        foreach ($prof in $profiles) {
            try {
                Write-MtLayoutXml -ProfileRoot $prof.LocalPath

                # Stale pinned shortcuts would dedup-collide as "... (2)" labels on rebuild.
                $pinDir = Join-Path -Path $prof.LocalPath -ChildPath 'AppData\Roaming\Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar'
                if (Test-Path -LiteralPath $pinDir) {
                    Get-ChildItem -LiteralPath $pinDir -Filter '*.lnk' -ErrorAction SilentlyContinue |
                        Remove-Item -Force -ErrorAction SilentlyContinue
                }

                if (Test-Path ("Registry::HKEY_USERS\{0}" -f $prof.SID)) {
                    # Hive loaded (session active): edit in place. Note a live Explorer will
                    # write its in-memory pins back at logoff; during provisioning no session
                    # exists, so this branch only matters for operator-attended runs.
                    $tk = "Registry::HKEY_USERS\$($prof.SID)\$taskbandSub"
                    if (Test-Path $tk) {
                        Remove-ItemProperty -Path $tk -Name Favorites        -ErrorAction SilentlyContinue
                        Remove-ItemProperty -Path $tk -Name FavoritesResolve -ErrorAction SilentlyContinue
                    }
                } else {
                    $ntuser = Join-Path -Path $prof.LocalPath -ChildPath 'NTUSER.DAT'
                    if (Test-Path -LiteralPath $ntuser) {
                        $mount = 'HKU\NeuraVpsPinTmp'
                        if ((Invoke-MtRegExe -ArgumentList @('load', $mount, $ntuser)) -eq 0) {
                            try {
                                Invoke-MtRegExe -ArgumentList @('delete', "$mount\$taskbandSub", '/v', 'Favorites', '/f') | Out-Null
                                Invoke-MtRegExe -ArgumentList @('delete', "$mount\$taskbandSub", '/v', 'FavoritesResolve', '/f') | Out-Null
                            } finally {
                                if ((Invoke-MtRegExe -ArgumentList @('unload', $mount)) -ne 0) {
                                    Start-Sleep -Milliseconds 500
                                    Invoke-MtRegExe -ArgumentList @('unload', $mount) | Out-Null
                                }
                            }
                        } else {
                            Write-Warning ("PinToTaskbar: could not load hive for {0}; pins apply only after Taskband reset" -f $prof.LocalPath)
                        }
                    }
                }
            } catch {
                Write-Warning ("PinToTaskbar: profile {0} skipped: {1}" -f $prof.LocalPath, $_.Exception.Message)
            }
        }

        # Provisioned VMs run with AutoAdminLogon, so Explorer is normally already live when this
        # script executes. A live Explorer holds the pin list in memory and would write it back
        # over the cleared Taskband blob at logoff, so restart it: the fresh instance finds no
        # blob, reads LayoutModification.xml, and persists the pins. AutoRestartShell (default 1)
        # brings it back in the user's session. No-op when nobody is logged on.
        if (Get-Process -Name explorer -ErrorAction SilentlyContinue) {
            Stop-Process -Name explorer -Force -ErrorAction SilentlyContinue
            $deadline = (Get-Date).AddSeconds(30)
            do {
                Start-Sleep -Seconds 2
                $back = Get-Process -Name explorer -ErrorAction SilentlyContinue
            } while (-not $back -and (Get-Date) -lt $deadline)
            if (-not $back) {
                Write-Warning 'PinToTaskbar: Explorer did not auto-restart; pins apply at next logon.'
            }
        }
    }
}
finally {
    if (Get-Command Remove-SmbGlobalMapping -ErrorAction SilentlyContinue) {
        Remove-SmbGlobalMapping -RemotePath $UncRoot -Force -ErrorAction SilentlyContinue
    }
    Remove-SmbMapping -RemotePath $UncRoot -Force -ErrorAction SilentlyContinue
}

$summary = "Done: $InstanceCount MetaTrader $MetaTraderVersion instance(s) under $MtRoot (zip: $ZipName); Start Menu shortcuts applied."
if (-not $NoDesktopShortcuts) { $summary += ' Desktop shortcuts applied.' }
if ($AddToStartup)            { $summary += ' Startup shortcuts applied.' }
if ($PinToTaskbar)            { $summary += ' Taskbar pin layout applied.' }
if ($MetaTraderVersion -eq 5) {
    $summary += ' File associations (001) applied.'
}
Write-Host $summary

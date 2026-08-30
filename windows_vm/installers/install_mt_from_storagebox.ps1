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

.PARAMETER StartMinimized
  Install an all-users Startup helper that minimises the auto-started terminals in the
  logon session. MT5 restores its own saved window placement and ignores shortcut /
  STARTUPINFO window-style hints, so this per-logon ShowWindow sweep is the only reliable
  way to have the terminals come up minimised on every reboot. Meant to accompany
  -AddToStartup (the Portfolios.io DFY build passes both).

.PARAMETER ApplyLiveUpdate
  At logon, press "Restart" on MetaTrader's "Welcome to LiveUpdate" dialog so a downloaded
  update is actually installed. Left alone the dialog sits on the desktop indefinitely (the
  terminals never restart on their own), and MetaTrader centres it on the saved desktop size,
  so on a small console it lands half off-screen with the Restart button unreachable - which
  is what the Portfolios.io partner reported as a window that cannot be removed. Safe here:
  the helper only runs at logon, when the terminals have just started, and the IFEO hook
  re-adds /portable to the relaunch. Meant to accompany -AddToStartup.

.PARAMETER RemoveStockExperts
  Delete MetaQuotes' bundled sample Expert Advisors (MQL5\Experts\Advisors, \Examples and
  \Free Robots) from every instance. The Portfolios.io DFY desktop ships the partner's own
  EAs and the sample robots are noise in the Navigator. Applied to the extracted instance
  before it is cloned, and re-applied on every logon by the Startup helper, because a
  MetaTrader LiveUpdate reinstalls the standard MQL5 tree ("updating ... MQL5 folder,
  N files updated") and brings the samples back.

.PARAMETER WebRequestUrls
  URLs to add to each instance's "Allow WebRequest for listed URL" allow-list on first
  logon. MT5 stores this list in config\common.ini encrypted with a MACHINE-BOUND key, so
  a list baked into the golden zip cannot be decrypted on a customer clone (the box shows
  an empty list). There is no CLI/MQL5 API for it, so the same Startup helper drives the
  Options GUI once per instance; MT5 then rewrites the blob under the local machine key and
  it survives restarts. Idempotent: an instance that already has URLs is left alone.

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
    [switch]$AddToStartup,

    # Install an all-users Startup helper that minimises the auto-started terminals in the
    # logon session (MT5 ignores shortcut/STARTUPINFO minimise hints and restores its own
    # saved window placement, so a per-logon ShowWindow sweep is the only reliable way).
    [switch]$StartMinimized,

    # Delete MetaQuotes' sample Expert Advisors from every instance (the DFY build ships the
    # partner's own EAs; a LiveUpdate puts the samples back, so the logon helper re-applies it).
    [switch]$RemoveStockExperts,

    # Press "Restart" on MetaTrader's LiveUpdate dialog at logon, so a downloaded update is
    # installed instead of the prompt sitting on the customer's desktop for weeks.
    [switch]$ApplyLiveUpdate,

    # WebRequest allow-list URLs to add to every instance on first logon. MT5 encrypts this
    # list with a machine-bound key, so a list baked into the zip cannot be decrypted on a
    # customer clone - it has to be entered once per machine through the Options GUI.
    [string[]]$WebRequestUrls = @()
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
# MetaQuotes' own sample Expert Advisors, shipped inside every MetaTrader 5 build and
# reinstalled by every LiveUpdate. Names are the same in every language.
$StockExpertDirs = @('Advisors', 'Examples', 'Free Robots')
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

function Remove-MtStockExperts {
    <#
    .SYNOPSIS
      Drop MetaQuotes' sample EAs from one instance's MQL5\Experts folder.
    .NOTES
      experts.dat is the Navigator's cache of compiled programs; deleting it makes MetaTrader
      rebuild the tree instead of listing entries whose files are gone.
    #>
    param([Parameter(Mandatory)][string]$InstancePath)
    $experts = Join-Path -Path $InstancePath -ChildPath 'MQL5\Experts'
    if (-not (Test-Path -LiteralPath $experts)) { return 0 }
    $removed = 0
    foreach ($dir in $StockExpertDirs) {
        $target = Join-Path -Path $experts -ChildPath $dir
        if (Test-Path -LiteralPath $target) {
            Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction SilentlyContinue
            if (-not (Test-Path -LiteralPath $target)) { $removed++ }
        }
    }
    $dat = Join-Path -Path $InstancePath -ChildPath 'MQL5\experts.dat'
    if (Test-Path -LiteralPath $dat) {
        Remove-Item -LiteralPath $dat -Force -ErrorAction SilentlyContinue
    }
    return $removed
}

function Disable-MtAssistantMcp {
    <#
    .SYNOPSIS
      Switch off the AI-assistant MCP server in one instance's config\assistant.ini.
    .DESCRIPTION
      MetaTrader 5 build 6090 added an MCP server for its AI assistant and pins it to FIXED
      loopback ports (22345 MetaEditor / 22346 MetaTrader). With several terminals on one box
      only the first to start can bind; every other instance logs, at severity 3 (a red line
      in the Journal on every start):

        MCP  bind error on 127.0.0.1:22346 [Only one usage of each socket address ... (10048)]

      MetaQuotes ships the assistant OFF - a stock 10-instance box on the same 6140 build has
      no assistant.ini and no MCP lines at all. It is on here only because it happened to be
      enabled when the golden image was captured, and the file was then cloned into every
      instance.

      Deleting the file does NOT work: the terminal recreates it on the next start, with the
      fixed port back. It does honour a file that is already there, so the switch has to be
      written, not removed. Verified on a test box: Enable=0 -> no MCP line and the file is
      left alone; distinct Endpoint ports -> each terminal binds its own and none error, which
      is the one-line alternative if the assistant is ever actually wanted here.

      The ApiKey and Endpoint are preserved so a customer can turn it back on from the GUI.
    #>
    param([Parameter(Mandatory)][string]$InstancePath)
    $ini = Join-Path -Path $InstancePath -ChildPath 'config\assistant.ini'
    if (-not (Test-Path -LiteralPath $ini)) { return $false }
    $text = Get-Content -LiteralPath $ini -Raw
    if ($text -notmatch '(?m)^Enable=1') { return $false }
    $text = [regex]::Replace($text, '(?m)^Enable=1', 'Enable=0')
    # BOM-less UTF-8: how MetaTrader writes this file itself.
    [System.IO.File]::WriteAllText($ini, $text, (New-Object System.Text.UTF8Encoding($false)))
    return $true
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
        [string]$Arguments = '',                    # command-line arguments for the target
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
        $encArg = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($Arguments))
        $helper = @"
`$ErrorActionPreference = 'Stop'
`$Lnk = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$encLnk'))
`$Tgt = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$encTgt'))
`$Wrk = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$encWrk'))
`$Arg = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$encArg'))
`$w = New-Object -ComObject WScript.Shell
`$s = `$w.CreateShortcut(`$Lnk)
`$s.TargetPath = `$Tgt
`$s.WorkingDirectory = `$Wrk
if (`$Arg) { `$s.Arguments = `$Arg }
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
        if ($Arguments) { $sc.Arguments = $Arguments }
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

    if ($RemoveStockExperts) {
        # Before the clone loop, so 002..N are copied already clean instead of N deletions.
        $stockGone = Remove-MtStockExperts -InstancePath $firstPath
        Write-Output "Removed $stockGone stock Expert folder(s) from the extracted instance."
    }

    if ($InstanceCount -gt 1) {
        # Also before the clone loop. Not gated behind a switch: a fixed loopback port simply
        # cannot be shared by N terminals, whatever image ships the file, and it is a no-op
        # when there is no assistant.ini (the stock zip has none). See Disable-MtAssistantMcp.
        if (Disable-MtAssistantMcp -InstancePath $firstPath) {
            Write-Output "Disabled the AI-assistant MCP server (its fixed port cannot be shared by $InstanceCount terminals)."
        }
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

        # These installs are portable: their data lives next to the executable, not in
        # %APPDATA%\MetaQuotes\Terminal\<hash>. Put the flag ON THE SHORTCUT as well as
        # relying on the IFEO hook, because the two fail in opposite directions and
        # neither is sufficient alone:
        #   - an MT self-update recreates the shortcut WITHOUT /portable  -> the hook saves it
        #   - the hook loses its IFEO Debugger value                      -> the shortcut saves it
        # Until 2026-08-09 only the hook carried it, so one lost registry value silently
        # flipped a box to non-portable and the customer's terminals opened blank
        # (canfret78 vm1455; 74 boxes fleet-wide, one of them 3 days old).
        # mt_hook_launcher.vbs already de-duplicates the flag when the shortcut has it.
        $portableArg = '/portable'

        if (-not $NoDesktopShortcuts) {
            New-MtShellShortcut -ShortcutPath $desktopLink -TargetPath $terminalExe -WorkingDirectory $folder -Arguments $portableArg
        }
        New-MtShellShortcut -ShortcutPath $startMenuLink -TargetPath $terminalExe -WorkingDirectory $folder -Arguments $portableArg

        if ($AddToStartup) {
            # MetaTrader restores its own saved window placement; WindowStyle 7 is a best-effort
            # hint - the durable minimised state comes from the config baked into the zip variant.
            $startupLink = Join-Path -Path $AllUsersStartup -ChildPath $label
            New-MtShellShortcut -ShortcutPath $startupLink -TargetPath $terminalExe -WorkingDirectory $folder -Arguments $portableArg -WindowStyle 7
        }
    }

    if ($StartMinimized -or $RemoveStockExperts -or $ApplyLiveUpdate -or $WebRequestUrls.Count -gt 0) {
        # Per-logon setup helper. Two jobs, and they MUST run in this order in one script:
        #   1. WebRequest allow-list. MT5 stores it in config\common.ini as a blob encrypted
        #      with a MACHINE-BOUND key, so the list baked into the golden zip cannot be
        #      decrypted on a customer clone - every new box shows an empty list. There is no
        #      CLI/MQL5 API for it, so the only way is to drive the Options GUI once; MT5 then
        #      re-writes the blob under THIS machine's key and it persists across restarts.
        #   2. Minimise. MT5 ignores shortcut / STARTUPINFO minimise hints and restores its own
        #      saved window placement, so the only reliable "start minimised" is a window-placement
        #      sweep. It has to come AFTER the injection, or it fights it for window state.
        # A third job runs FIRST and needs no window at all: deleting MetaQuotes' sample EAs,
        # which a LiveUpdate reinstalls behind our back every time it refreshes the MQL5 tree.
        # Runs in the INTERACTIVE logon session (a terminal's MainWindowHandle reads 0 from
        # SYSTEM/session 0, and the Options dialog can only be driven on a real desktop).
        # Pure user32 P/Invoke via Add-Type - no compiled binary, so it is not subject to the
        # Defender content-signature deletion that kills a standalone injector .exe.
        $neuraDir = 'C:\ProgramData\neuravps'
        if (-not (Test-Path -LiteralPath $neuraDir)) {
            New-Item -ItemType Directory -Path $neuraDir -Force | Out-Null
        }
        $procName = [System.IO.Path]::GetFileNameWithoutExtension($TerminalExeName)   # terminal64 / terminal
        $urlLiteral = (($WebRequestUrls | ForEach-Object { "'" + ($_ -replace "'", "''") + "'" }) -join ',')
        $setupScript = @'
# NeuraVPS DFY image: per-logon MetaTrader setup (WebRequest allow-list, then minimise).
# Generated by install_mt_from_storagebox.ps1. Runs hidden from an all-users Startup shortcut.
$ErrorActionPreference = 'SilentlyContinue'
$PROC     = '__PROCNAME__'
$URLS     = @(__URLS__)
$MINIMIZE = __MINIMIZE__
$EXPECT   = __EXPECT__
$MTROOT   = '__MTROOT__'
$CLEANEXP = __CLEANEXPERTS__
$LIVEUPD  = __LIVEUPDATE__
$STOCKEXP = @('Advisors','Examples','Free Robots')
$MarkDir  = 'C:\ProgramData\neuravps'
$LogFile  = Join-Path $MarkDir 'mt_logon_setup.log'
function L($m){ try { Add-Content -LiteralPath $LogFile -Value ((Get-Date -Format o) + '  ' + $m) } catch {} }
if (-not (Test-Path $MarkDir)) { New-Item -ItemType Directory -Path $MarkDir -Force | Out-Null }
# MetaQuotes' sample EAs. The installer already deleted them, but a LiveUpdate reinstalls the
# whole standard MQL5 tree ("updating ... MQL5 folder, N files updated") and puts them back, so
# this cannot be a one-shot: it runs at logon and then again from the watch loop, because the
# update we apply below IS one of those reinstalls, and because a terminal that is starting or
# shutting down holds the folder open and the delete just fails (observed on the test box:
# identical code removed nothing at T+2s and all 8 a minute later).
function NvCleanExperts(){
  $gone=0
  foreach($d in (Get-ChildItem $MTROOT -Directory -Filter 'MetaTrader *' -ErrorAction SilentlyContinue)){
    $hit=0
    foreach($sd in $STOCKEXP){
      $t=Join-Path $d.FullName ('MQL5\Experts\' + $sd)
      if(Test-Path -LiteralPath $t){
        Remove-Item -LiteralPath $t -Recurse -Force -ErrorAction SilentlyContinue
        if(-not (Test-Path -LiteralPath $t)){ $hit++ }
      }
    }
    if($hit -gt 0){
      # experts.dat caches the Navigator tree; drop it so MT5 rebuilds it without the samples
      $dat=Join-Path $d.FullName 'MQL5\experts.dat'
      if(Test-Path -LiteralPath $dat){ Remove-Item -LiteralPath $dat -Force -ErrorAction SilentlyContinue }
      $gone+=$hit
    }
  }
  if($gone -gt 0){ L "stock experts: removed $gone folder(s)" }
}
if($CLEANEXP){ NvCleanExperts }
Add-Type @"
using System;using System.Text;using System.Runtime.InteropServices;
public delegate bool NvEnumCb(IntPtr h, IntPtr l);
[StructLayout(LayoutKind.Sequential)] public struct NvRECT { public int L,T,R,B; }
[StructLayout(LayoutKind.Sequential)] public struct NvPOINT { public int X,Y; }
[StructLayout(LayoutKind.Sequential)] public struct NvWP { public int len,flags,show; public NvPOINT ptMin,ptMax; public NvRECT rcNormal; }
public class NvU {
 [DllImport("user32.dll")] public static extern bool EnumWindows(NvEnumCb cb,IntPtr l);
 [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr p,NvEnumCb cb,IntPtr l);
 [DllImport("user32.dll",CharSet=CharSet.Auto)] public static extern int GetClassName(IntPtr h,StringBuilder s,int m);
 [DllImport("user32.dll",CharSet=CharSet.Auto)] public static extern int GetWindowText(IntPtr h,StringBuilder s,int m);
 [DllImport("user32.dll")] public static extern IntPtr GetDlgItem(IntPtr h,int id);
 [DllImport("user32.dll")] public static extern int GetDlgCtrlID(IntPtr h);
 [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
 [DllImport("user32.dll",SetLastError=true)] public static extern uint GetWindowThreadProcessId(IntPtr h,out uint pid);
 [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr h,int m,IntPtr w,IntPtr l);
 [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h,out NvRECT r);
 [DllImport("user32.dll")] public static extern bool ClientToScreen(IntPtr h,ref NvPOINT p);
 [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
 [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h,IntPtr after,int x,int y,int cx,int cy,uint flags);
 [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
 [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint a,uint b,bool f);
 [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr h);
 [DllImport("user32.dll")] public static extern IntPtr SetFocus(IntPtr h);
 [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h,int c);
 [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr h,int c);
 [DllImport("user32.dll")] public static extern void keybd_event(byte k,byte s,uint f,IntPtr e);
 [DllImport("user32.dll")] public static extern bool SetCursorPos(int x,int y);
 [DllImport("user32.dll")] public static extern void mouse_event(uint f,uint x,uint y,uint d,IntPtr e);
 [DllImport("user32.dll")] public static extern bool SystemParametersInfo(int a,int b,IntPtr c,int d);
 [DllImport("user32.dll")] public static extern bool SystemParametersInfo(int a,int b,ref NvRECT r,int d);
 [DllImport("user32.dll")] public static extern bool GetWindowPlacement(IntPtr h,ref NvWP p);
 [DllImport("user32.dll")] public static extern bool SetWindowPlacement(IntPtr h,ref NvWP p);
 [DllImport("kernel32.dll",SetLastError=true)] public static extern IntPtr OpenProcess(int a,bool i,uint pid);
 [DllImport("kernel32.dll")] public static extern IntPtr VirtualAllocEx(IntPtr h,IntPtr addr,uint size,uint typ,uint prot);
 [DllImport("kernel32.dll")] public static extern bool VirtualFreeEx(IntPtr h,IntPtr addr,uint size,uint typ);
 [DllImport("kernel32.dll")] public static extern bool WriteProcessMemory(IntPtr h,IntPtr addr,byte[] buf,uint n,ref uint w);
 [DllImport("kernel32.dll")] public static extern bool ReadProcessMemory(IntPtr h,IntPtr addr,byte[] buf,uint n,ref uint r);
 [DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr h);
}
"@
# Dialogs are identified STRUCTURALLY, by control id - never by window title. MT5's control
# ids are compile-time constants and identical in every language, but the titles are
# localised ("Options"/"Opciones", "Expert Advisors"/"Asesores Expertos"), and NeuraVPS
# provisions Windows in en and es. Title matching silently found nothing on an es box.
#   10322 = "Allow WebRequest for listed URL" checkbox (only on the Expert Advisors page)
#   12324 = the account wizard's "Next >" button (present ONLY on that wizard)
$script:q1=0;$script:q3=$null
$script:nvWant=0;$script:nvFound=$false
function NvHasDesc($top,$id){
  $script:nvWant=$id;$script:nvFound=$false
  $cb=[NvEnumCb]{ param($h,$l) if([NvU]::GetDlgCtrlID($h) -eq $script:nvWant){ $script:nvFound=$true }; return $true }
  [void][NvU]::EnumChildWindows($top,$cb,[IntPtr]::Zero)
  return $script:nvFound
}
# every top-level #32770 owned by this pid
function NvDlgsFor($fpid){
  $r=New-Object System.Collections.ArrayList;$script:q1=$fpid;$script:q3=$r
  $cb=[NvEnumCb]{ param($h,$l)
    $sb=New-Object Text.StringBuilder 64;[void][NvU]::GetClassName($h,$sb,64)
    if($sb.ToString() -eq '#32770'){ $pp=0;[void][NvU]::GetWindowThreadProcessId($h,[ref]$pp)
      if($pp -eq $script:q1){ [void]$script:q3.Add($h) } } ; return $true }
  [void][NvU]::EnumWindows($cb,[IntPtr]::Zero); return $r
}
# the Options dialog: carries the WebRequest checkbox somewhere below it (all tab pages
# exist as child #32770s at once), and is NOT the account wizard.
function NvFindOptions($fpid){
  foreach($d in (NvDlgsFor $fpid)){ if((NvHasDesc $d 10322) -and -not (NvHasDesc $d 12324)){ return $d } }
  return [IntPtr]::Zero
}
# MetaTrader's "Welcome to LiveUpdate" prompt ("Updates have been downloaded ... Press
# "Restart""). Its statics are LiveUpdate's own control ids, so it cannot be confused with the
# account wizard (12324) or Options (10322), and unlike the title they are the same in every
# language. Buttons are the standard IDOK/IDCANCEL: 1 = Restart, 2 = Later.
function NvFindLiveUpdate($fpid){
  foreach($d in (NvDlgsFor $fpid)){ if(NvHasDesc $d 10426){ return $d } }
  return [IntPtr]::Zero
}
# the Expert Advisors page: the child #32770 that directly owns the checkbox
function NvFindEaPage($dlg){
  $r=New-Object System.Collections.ArrayList;$script:pr=$r
  $cb=[NvEnumCb]{ param($h,$l)
    $sb=New-Object Text.StringBuilder 64;[void][NvU]::GetClassName($h,$sb,64)
    if($sb.ToString() -eq '#32770' -and ([NvU]::GetDlgItem($h,10322) -ne [IntPtr]::Zero)){ [void]$script:pr.Add($h) }
    return $true }
  [void][NvU]::EnumChildWindows($dlg,$cb,[IntPtr]::Zero)
  if($r.Count -gt 0){ return $r[0] } else { return [IntPtr]::Zero }
}
# cross-process LVM_GETITEMRECT / TCM_GETITEMRECT -> screen centre of a row / tab
function NvItemCentre($ctrl,$msg,$idx){
  $pp=0;[void][NvU]::GetWindowThreadProcessId($ctrl,[ref]$pp)
  $hP=[NvU]::OpenProcess(0x38,$false,$pp); if($hP -eq [IntPtr]::Zero){ return $null }
  $rem=[NvU]::VirtualAllocEx($hP,[IntPtr]::Zero,16,0x3000,0x04)
  $z=New-Object byte[] 16;$w=0;[void][NvU]::WriteProcessMemory($hP,$rem,$z,16,[ref]$w)
  [void][NvU]::SendMessage($ctrl,$msg,[IntPtr]$idx,$rem)
  $o=New-Object byte[] 16;$rd=0;[void][NvU]::ReadProcessMemory($hP,$rem,$o,16,[ref]$rd)
  [void][NvU]::VirtualFreeEx($hP,$rem,0,0x8000);[void][NvU]::CloseHandle($hP)
  $cl=[BitConverter]::ToInt32($o,0);$ct=[BitConverter]::ToInt32($o,4);$cr=[BitConverter]::ToInt32($o,8);$cb2=[BitConverter]::ToInt32($o,12)
  if(($cr-$cl) -le 0 -or ($cb2-$ct) -le 0){ return $null }
  $pt=New-Object NvPOINT;$pt.X=[int](($cl+$cr)/2);$pt.Y=[int](($ct+$cb2)/2)
  [void][NvU]::ClientToScreen($ctrl,[ref]$pt); return $pt
}
function NvClick($pt){ [void][NvU]::SetCursorPos($pt.X,$pt.Y);Start-Sleep -Milliseconds 90;[NvU]::mouse_event(0x02,0,0,0,[IntPtr]::Zero);[NvU]::mouse_event(0x04,0,0,0,[IntPtr]::Zero) }
function NvDblClick($pt){ NvClick $pt; Start-Sleep -Milliseconds 70;[NvU]::mouse_event(0x02,0,0,0,[IntPtr]::Zero);[NvU]::mouse_event(0x04,0,0,0,[IntPtr]::Zero) }
function NvInject($proc,$inst){
  $hwnd=$proc.MainWindowHandle
  if($hwnd -eq [IntPtr]::Zero){ L "  [$inst] no main window"; return $false }
  $me=[NvU]::GetCurrentThreadId()
  # the first-run "Open an Account" wizard is MODAL and blocks Options -> cancel it
  foreach($w in (NvDlgsFor $proc.Id)){ if(NvHasDesc $w 12324){ $c=[NvU]::GetDlgItem($w,2); if($c -ne [IntPtr]::Zero){ [void][NvU]::SendMessage($c,0x00F5,[IntPtr]::Zero,[IntPtr]::Zero) } } }
  Start-Sleep -Milliseconds 500
  # Ctrl+O only lands while our input queue is attached to the terminal's thread
  $tt=0;[void][NvU]::GetWindowThreadProcessId($hwnd,[ref]$tt)
  [void][NvU]::AttachThreadInput($me,$tt,$true)
  # 3 = SW_SHOWMAXIMIZED, not 9 = SW_RESTORE: restoring a maximised terminal drops it back to
  # its saved "normal" rect and that is the state the minimise sweep would then preserve.
  [void][NvU]::ShowWindow($hwnd,3);[void][NvU]::BringWindowToTop($hwnd);[void][NvU]::SetForegroundWindow($hwnd);[void][NvU]::SetFocus($hwnd);Start-Sleep -Milliseconds 300
  [NvU]::keybd_event(0x11,0,0,[IntPtr]::Zero);Start-Sleep -Milliseconds 50
  [NvU]::keybd_event(0x4F,0,0,[IntPtr]::Zero);Start-Sleep -Milliseconds 70
  [NvU]::keybd_event(0x4F,0,2,[IntPtr]::Zero);Start-Sleep -Milliseconds 50
  [NvU]::keybd_event(0x11,0,2,[IntPtr]::Zero)
  [void][NvU]::AttachThreadInput($me,$tt,$false)
  $dlg=[IntPtr]::Zero
  for($i=0;$i -lt 14;$i++){ Start-Sleep -Milliseconds 300; $o=NvFindOptions $proc.Id; if($o -ne [IntPtr]::Zero){ $dlg=$o; break } }
  if($dlg -eq [IntPtr]::Zero){ L "  [$inst] Options did not open"; return $false }
  $dtt=0;[void][NvU]::GetWindowThreadProcessId($dlg,[ref]$dtt)
  [void][NvU]::AttachThreadInput($me,$dtt,$true)
  [void][NvU]::SetForegroundWindow($dlg);[void][NvU]::BringWindowToTop($dlg);Start-Sleep -Milliseconds 150
  # the console desktop can be small; MT5 centres Options partly off-screen -> move it on-screen
  [void][NvU]::SetWindowPos($dlg,[IntPtr]::Zero,20,20,0,0,0x0005);Start-Sleep -Milliseconds 250
  # every tab page exists as a child #32770; the WebRequest controls live on "Expert Advisors"
  $ea=NvFindEaPage $dlg
  if($ea -eq [IntPtr]::Zero){ L "  [$inst] no Expert Advisors page"; [void][NvU]::AttachThreadInput($me,$dtt,$false); return $false }
  $tab=[NvU]::GetDlgItem($dlg,12320);$tcnt=[int][NvU]::SendMessage($tab,0x1304,[IntPtr]::Zero,[IntPtr]::Zero)
  for($ti=0; ($ti -lt $tcnt) -and (-not [NvU]::IsWindowVisible($ea)); $ti++){ $tp=NvItemCentre $tab 0x130A $ti; if($tp -ne $null){ NvClick $tp; Start-Sleep -Milliseconds 320 } }
  if(-not [NvU]::IsWindowVisible($ea)){ L "  [$inst] could not show Expert Advisors tab"; [void][NvU]::AttachThreadInput($me,$dtt,$false); return $false }
  $cbx=[NvU]::GetDlgItem($ea,10322);$lv=[NvU]::GetDlgItem($ea,10191)
  if([int][NvU]::SendMessage($cbx,0x00F0,[IntPtr]::Zero,[IntPtr]::Zero) -ne 1){ [void][NvU]::SendMessage($cbx,0x00F5,[IntPtr]::Zero,[IntPtr]::Zero);Start-Sleep -Milliseconds 250 }
  # last row is the "add new URL" placeholder, so real entries = count - 1.
  # Item TEXT is unreadable (owner-drawn) but the COUNT is not -> use it to stay idempotent.
  $existing=([int][NvU]::SendMessage($lv,0x1004,[IntPtr]::Zero,[IntPtr]::Zero))-1
  if($existing -ge 1){
    L "  [$inst] already has $existing url(s) - leaving alone"
  } else {
    foreach($url in $URLS){
      $c2=[int][NvU]::SendMessage($lv,0x1004,[IntPtr]::Zero,[IntPtr]::Zero)
      $idx=$c2-1; if($idx -lt 0){ $idx=0 }
      $pt=$null; if($c2 -ge 1){ $pt=NvItemCentre $lv 0x100E $idx }
      if($pt -eq $null){ $lr=New-Object NvRECT;[void][NvU]::GetWindowRect($lv,[ref]$lr);$pt=New-Object NvPOINT;$pt.X=[int](($lr.L+$lr.R)/2);$pt.Y=$lr.T+35 }
      NvDblClick $pt; Start-Sleep -Milliseconds 420
      # paste as REAL input: WM_SETTEXT sets the edit but MT5 never commits it
      try { Set-Clipboard -Value $url } catch {}
      Start-Sleep -Milliseconds 120
      [NvU]::keybd_event(0x11,0,0,[IntPtr]::Zero);Start-Sleep -Milliseconds 30
      [NvU]::keybd_event(0x56,0,0,[IntPtr]::Zero);Start-Sleep -Milliseconds 40;[NvU]::keybd_event(0x56,0,2,[IntPtr]::Zero);Start-Sleep -Milliseconds 30
      [NvU]::keybd_event(0x11,0,2,[IntPtr]::Zero);Start-Sleep -Milliseconds 250
      [NvU]::keybd_event(0x0D,0,0,[IntPtr]::Zero);Start-Sleep -Milliseconds 40;[NvU]::keybd_event(0x0D,0,2,[IntPtr]::Zero)
      Start-Sleep -Milliseconds 450
    }
    L ("  [$inst] urls now=" + (([int][NvU]::SendMessage($lv,0x1004,[IntPtr]::Zero,[IntPtr]::Zero))-1))
  }
  # OK -> MT5 writes common.ini with the list re-encrypted under THIS machine's key
  [void][NvU]::SendMessage(([NvU]::GetDlgItem($dlg,1)),0x00F5,[IntPtr]::Zero,[IntPtr]::Zero)
  [void][NvU]::AttachThreadInput($me,$dtt,$false)
  Start-Sleep -Milliseconds 900
  return $true
}

# wait for the terminals (they start from their own Startup shortcuts at this same logon)
$deadline=(Get-Date).AddSeconds(240)
while((Get-Date) -lt $deadline){
  $ready=@(Get-Process $PROC -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne [IntPtr]::Zero })
  if($ready.Count -ge $EXPECT){ break }
  Start-Sleep -Seconds 2
}
if($URLS.Count -gt 0){
  [void][NvU]::SystemParametersInfo(0x2001,0,[IntPtr]::Zero,0)   # drop the foreground lock
  $terms=Get-Process $PROC -ErrorAction SilentlyContinue | Where-Object { $_.Path -match 'MetaTrader \d+ - (\d+)' } |
         Sort-Object { [regex]::Match($_.Path,'MetaTrader \d+ - (\d+)').Groups[1].Value }
  foreach($p in $terms){
    $inst=[regex]::Match($p.Path,'MetaTrader \d+ - (\d+)').Groups[1].Value
    $mark=Join-Path $MarkDir ('webreq_' + $inst + '.done')
    if(Test-Path $mark){ continue }
    L "webrequest: instance $inst (pid $($p.Id))"
    $ok=$false
    try { $ok=NvInject $p $inst } catch { L ("  [$inst] " + $_.Exception.Message) }
    if($ok){ Set-Content -LiteralPath $mark -Value (Get-Date -Format o) }
  }
}
# On the taskbar, but full-screen when the customer clicks the icon. Two independent things:
#   showCmd = SW_SHOWMINNOACTIVE  -> minimised, and without stealing focus 8 times in a row
#   WPF_RESTORETOMAXIMIZED        -> the restore is maximised
# Without the flag the terminal comes back at its saved "normal" rect, and the golden zip baked
# that rect on a 2560x1392 desktop: on a customer console the window is larger than the screen
# and its edges run off it - the "partial windows" the partner reported. rcNormalPosition is
# clamped to the work area for the same reason, so a manual un-maximise also lands on-screen.
function NvMinToTaskbarRestoreMax($h){
  $wp=New-Object NvWP
  $wp.len=[System.Runtime.InteropServices.Marshal]::SizeOf($wp)
  if(-not [NvU]::GetWindowPlacement($h,[ref]$wp)){
    [void][NvU]::ShowWindow($h,3);[void][NvU]::ShowWindowAsync($h,6);return
  }
  $wa=New-Object NvRECT
  if([NvU]::SystemParametersInfo(0x0030,0,[ref]$wa,0)){
    if(($wp.rcNormal.R-$wp.rcNormal.L) -gt ($wa.R-$wa.L) -or
       ($wp.rcNormal.B-$wp.rcNormal.T) -gt ($wa.B-$wa.T) -or
       $wp.rcNormal.L -lt $wa.L -or $wp.rcNormal.T -lt $wa.T){ $wp.rcNormal=$wa }
  }
  $wp.flags=$wp.flags -bor 2   # WPF_RESTORETOMAXIMIZED
  $wp.show=7                   # SW_SHOWMINNOACTIVE
  if(-not [NvU]::SetWindowPlacement($h,[ref]$wp)){
    [void][NvU]::ShowWindow($h,3);[void][NvU]::ShowWindowAsync($h,6)
  }
}
# One watch loop for both jobs, because they interact: pressing Restart makes the terminal
# relaunch, and the new window has to be minimised again. Keyed by window handle / pid, so
# each window is touched once and a customer who restores a terminal is never fought.
# The loop is long because LiveUpdate downloads ~180 MB in the background before it prompts;
# a 2-minute sweep would end before the dialog appears.
if($MINIMIZE -or $LIVEUPD){
  $seen=@{}
  $luDone=@{}
  $md=(Get-Date).AddSeconds($(if($LIVEUPD){900}else{120}))
  $tick=0
  while((Get-Date) -lt $md){
    $procs=@(Get-Process $PROC -ErrorAction SilentlyContinue)
    if($LIVEUPD -and ($tick % 5) -eq 0){
      foreach($p in $procs){
        # Re-press after 2 min: pressing Restart on all 8 at once made only 3 of them actually
        # come back updated on the test box, so this both staggers (one terminal per pass, 10 s
        # apart) and retries the ones that stayed on the old build.
        $last=$luDone[$p.Id]
        if($last -ne $null -and ((Get-Date)-$last).TotalSeconds -lt 120){ continue }
        $d=NvFindLiveUpdate $p.Id
        if($d -ne [IntPtr]::Zero){
          # MetaTrader centres this dialog on the desktop size it remembers, which on a small
          # console puts the buttons off-screen; move it on before clicking anything.
          [void][NvU]::SetWindowPos($d,[IntPtr]::Zero,20,20,0,0,0x0005)
          $btn=[NvU]::GetDlgItem($d,1)
          if($btn -ne [IntPtr]::Zero){
            [void][NvU]::SendMessage($btn,0x00F5,[IntPtr]::Zero,[IntPtr]::Zero)
            L "liveupdate: pressed Restart (pid $($p.Id))"
          }
          $luDone[$p.Id]=Get-Date
          break
        }
      }
    }
    if($MINIMIZE){
      foreach($p in $procs){
        $h=$p.MainWindowHandle
        if($h -ne [IntPtr]::Zero -and -not $seen.ContainsKey([int64]$h)){
          NvMinToTaskbarRestoreMax $h        # once only, never fight the user
          $seen[[int64]$h]=$true
        }
      }
    }
    # every ~30 s: catches the samples the update we just applied reinstalled, and retries the
    # instances whose folders were locked by a terminal that was starting or shutting down
    if($CLEANEXP -and ($tick % 15) -eq 14){ NvCleanExperts }
    $tick++
    Start-Sleep -Milliseconds 2000
  }
  if($CLEANEXP){ NvCleanExperts }
}
'@
        $setupScript = $setupScript.Replace('__PROCNAME__', $procName).
                                    Replace('__URLS__', $urlLiteral).
                                    Replace('__MINIMIZE__', $(if ($StartMinimized) { '$true' } else { '$false' })).
                                    Replace('__EXPECT__', [string]$InstanceCount).
                                    Replace('__MTROOT__', $MtRoot).
                                    Replace('__CLEANEXPERTS__', $(if ($RemoveStockExperts) { '$true' } else { '$false' })).
                                    Replace('__LIVEUPDATE__', $(if ($ApplyLiveUpdate) { '$true' } else { '$false' }))
        $setupPath = Join-Path -Path $neuraDir -ChildPath 'mt_logon_setup.ps1'
        $utf8Bom = New-Object System.Text.UTF8Encoding($true)   # BOM so Windows PowerShell 5.1 reads it as UTF-8
        [System.IO.File]::WriteAllText($setupPath, $setupScript, $utf8Bom)

        # supersede the older minimise-only helper if this box was provisioned before
        Remove-Item -LiteralPath (Join-Path $AllUsersStartup 'NeuraVPS MT Minimize.lnk') -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath (Join-Path $neuraDir 'mt_minimize.ps1') -Force -ErrorAction SilentlyContinue

        $winPs = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        $setupLnk = Join-Path -Path $AllUsersStartup -ChildPath 'NeuraVPS MT Setup.lnk'
        $setupArgs = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $setupPath
        New-MtShellShortcut -ShortcutPath $setupLnk -TargetPath $winPs -WorkingDirectory $neuraDir -Arguments $setupArgs -WindowStyle 7
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
if ($StartMinimized -or $RemoveStockExperts -or $ApplyLiveUpdate -or $WebRequestUrls.Count -gt 0) {
    $bits = @()
    if ($WebRequestUrls.Count -gt 0) { $bits += ("WebRequest allow-list ({0} url(s))" -f $WebRequestUrls.Count) }
    if ($StartMinimized)             { $bits += 'start-minimised (restores maximised)' }
    if ($RemoveStockExperts)         { $bits += 'stock-EA cleanup' }
    if ($ApplyLiveUpdate)            { $bits += 'apply-LiveUpdate' }
    $summary += (' Logon helper applied: {0}.' -f ($bits -join ' + '))
}
if ($PinToTaskbar)            { $summary += ' Taskbar pin layout applied.' }
if ($MetaTraderVersion -eq 5) {
    $summary += ' File associations (001) applied.'
}
Write-Host $summary

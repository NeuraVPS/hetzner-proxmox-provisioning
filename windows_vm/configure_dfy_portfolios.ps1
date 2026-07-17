#Requires -RunAsAdministrator
<#
.SYNOPSIS
  DFY (Portfolios.io) desktop configuration - the extra steps applied on top of the
  default mt (mt-plus) image to produce the DFY "MetaTrader+" template.

  Idempotent. Intended to be run ELEVATED, inside the golden VM's interactive session
  (i.e. RDP in as Administrator and run it), before the template is captured. Several
  steps (pin preview, starting the terminals) act on the interactive desktop, so running
  it from a non-interactive/SYSTEM context (e.g. bare guest-agent exec) will not behave
  the same - use the operator's logged-on session.

.DESCRIPTION
  Steps (each can be skipped with the matching -No* switch):
    1. Taskbar pins  - pin the N MetaTrader terminals to the taskbar via LayoutModification.xml
                       (written to the current user AND the Default profile, so every future
                       customer logon gets it). Verb/COM pinning is blocked on Server 2025;
                       this is the supported route.
    2. Desktop clean - delete the MetaTrader desktop shortcuts the mt image dropped on the
                       Public desktop (they are replaced by the taskbar pins).
    3. Startup       - create a shortcut per terminal in the all-users Startup folder so all
                       N terminals auto-launch (minimised) on every logon.
    4. Start now     - launch the N terminals immediately, minimised.

  The mt image lays the terminals out as C:\MetaTrader\MetaTrader 5 - 00N\terminal64.exe
  with Start Menu + Public Desktop shortcuts labelled "MetaTrader 5 - 00N"
  (see install_mt_from_storagebox.ps1). This script only reorganises those.

.PARAMETER InstanceCount
  How many terminals to pin / add to Startup / start (default 8 for DFY).

.PARAMETER NoPreviewPins
  Write LayoutModification.xml but do NOT apply it to the current profile now
  (skip the Taskband clear + Explorer restart). New logons still get the pins.
#>
[CmdletBinding()]
param(
  [ValidateRange(1,999)][int]$InstanceCount = 8,
  [ValidateSet(4,5)][int]$MetaTraderVersion = 5,
  [switch]$NoTaskbarPins,
  [switch]$NoRemoveDesktop,
  [switch]$NoStartupFolder,
  [switch]$NoStartNow,
  [switch]$NoPreviewPins
)

$ErrorActionPreference = 'Stop'
$ProgressPreference     = 'SilentlyContinue'

$MtRoot          = 'C:\MetaTrader'
$StartMenuMt     = 'C:\ProgramData\Microsoft\Windows\Start Menu\Programs\MetaTrader'
$PublicDesktop   = 'C:\Users\Public\Desktop'
$AllUsersStartup = 'C:\ProgramData\Microsoft\Windows\Start Menu\Programs\StartUp'
$TerminalExe     = if ($MetaTraderVersion -eq 5) { 'terminal64.exe' } else { 'terminal.exe' }

function Get-Label   { param([int]$i) ('MetaTrader {0} - {1:000}' -f $MetaTraderVersion, $i) }
function Get-Folder  { param([int]$i) (Join-Path $MtRoot (Get-Label $i)) }
function Get-Terminal{ param([int]$i) (Join-Path (Get-Folder $i) $TerminalExe) }

function New-Lnk {
  param(
    [Parameter(Mandatory)][string]$Path,
    [Parameter(Mandatory)][string]$Target,
    [Parameter(Mandatory)][string]$WorkDir,
    [int]$Style = 1,                    # 1=Normal, 3=Maximized, 7=Minimized
    [string]$Description
  )
  $dir = Split-Path -Parent $Path
  if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
  $ws = New-Object -ComObject WScript.Shell
  $sc = $ws.CreateShortcut($Path)
  $sc.TargetPath       = $Target
  $sc.WorkingDirectory = $WorkDir
  $sc.WindowStyle      = $Style
  if (Test-Path -LiteralPath $Target) { $sc.IconLocation = "$Target,0" }
  if ($Description) { $sc.Description = $Description }
  $sc.Save()
}

# Instances that actually exist on disk.
$instances = 1..$InstanceCount | Where-Object { Test-Path -LiteralPath (Get-Terminal $_) }
if ($instances.Count -lt $InstanceCount) {
  Write-Warning ("Only {0}/{1} instances have {2} under {3}" -f $instances.Count, $InstanceCount, $TerminalExe, $MtRoot)
}
$log = New-Object System.Collections.Generic.List[string]

# ============ 1. Taskbar pins ============
if (-not $NoTaskbarPins) {
  $links = $instances | ForEach-Object { Join-Path $StartMenuMt ((Get-Label $_) + '.lnk') }
  $apps  = ($links | ForEach-Object { '        <taskbar:DesktopApp DesktopApplicationLinkPath="' + $_ + '" />' }) -join "`r`n"
  $xml = @"
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
$apps
      </taskbar:TaskbarPinList>
    </defaultlayout:TaskbarLayout>
  </CustomTaskbarLayoutCollection>
</LayoutModificationTemplate>
"@
  $shellDirs = @(
    (Join-Path $env:LOCALAPPDATA 'Microsoft\Windows\Shell'),
    'C:\Users\Default\AppData\Local\Microsoft\Windows\Shell'
  )
  foreach ($d in $shellDirs) {
    New-Item -ItemType Directory -Path $d -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $d 'LayoutModification.xml') -Value $xml -Encoding UTF8
  }
  $log.Add("pins: wrote LayoutModification.xml ($($links.Count) apps) to current-user + Default profile")

  if (-not $NoPreviewPins) {
    $sid = ([Security.Principal.WindowsIdentity]::GetCurrent()).User.Value
    $tk  = "Registry::HKEY_USERS\$sid\Software\Microsoft\Windows\CurrentVersion\Explorer\Taskband"
    if (Test-Path $tk) {
      Remove-ItemProperty -Path $tk -Name Favorites        -ErrorAction SilentlyContinue
      Remove-ItemProperty -Path $tk -Name FavoritesResolve -ErrorAction SilentlyContinue
    }
    # Delete any stale pinned .lnk (incl. Windows' " (2)" dedup copies) so Explorer recreates
    # clean, single-named shortcuts. Without this, re-applying accumulates "MetaTrader 5 - 00N (2)".
    $pinDir = Join-Path $env:APPDATA 'Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar'
    Get-ChildItem -LiteralPath $pinDir -Filter ('MetaTrader {0} - *.lnk' -f $MetaTraderVersion) -ErrorAction SilentlyContinue |
      Remove-Item -Force -ErrorAction SilentlyContinue
    Stop-Process -Name explorer -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 6
    $log.Add("pins: applied to current profile ($sid) - cleared Taskband + stale pin lnks + restarted Explorer")
  }
}

# ============ 2. Remove MetaTrader desktop shortcuts ============
if (-not $NoRemoveDesktop) {
  $removed = 0
  $desktops = @($PublicDesktop) + (Get-ChildItem 'C:\Users' -Directory -ErrorAction SilentlyContinue |
                                    ForEach-Object { Join-Path $_.FullName 'Desktop' })
  foreach ($dk in ($desktops | Select-Object -Unique)) {
    Get-ChildItem -LiteralPath $dk -Filter ('MetaTrader {0} - *.lnk' -f $MetaTraderVersion) -ErrorAction SilentlyContinue |
      ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue; $removed++ }
  }
  $log.Add("desktop: removed $removed MetaTrader $MetaTraderVersion shortcut(s)")
}

# ============ 3. Startup folder (auto-launch minimised on every logon) ============
if (-not $NoStartupFolder) {
  foreach ($i in $instances) {
    $lnk = Join-Path $AllUsersStartup ((Get-Label $i) + '.lnk')
    New-Lnk -Path $lnk -Target (Get-Terminal $i) -WorkDir (Get-Folder $i) -Style 7 -Description (Get-Label $i)
  }
  $log.Add("startup: created $($instances.Count) minimised shortcut(s) in all-users Startup")
}

# ============ 4. Start terminals now, minimised ============
# NOTE: MetaTrader restores its OWN saved window placement and ignores the shortcut /
# Start-Process window-style hint, so we also force-minimise each window after it appears.
# The DURABLE minimised state for customers comes from the per-terminal config baked into
# the image (configure each terminal minimised, then capture) - this is a best-effort helper.
if (-not $NoStartNow) {
  $canMinimize = $false
  try {
    Add-Type -Namespace NV -Name Win -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool ShowWindowAsync(System.IntPtr hWnd, int nCmdShow);
'@ -ErrorAction Stop
    $canMinimize = $true
  } catch { $log.Add("start: force-minimise unavailable ($($_.Exception.Message)); relying on saved config") }

  $procs = @()
  foreach ($i in $instances) {
    $procs += Start-Process -FilePath (Get-Terminal $i) -WorkingDirectory (Get-Folder $i) -WindowStyle Minimized -PassThru
    Start-Sleep -Milliseconds 400
  }
  if ($canMinimize) {
    # MetaTrader restores its state a beat after the window first appears, so sweep for a
    # few seconds, minimising each terminal repeatedly to win the race. 6 = SW_MINIMIZE.
    for ($sweep = 0; $sweep -lt 12; $sweep++) {
      foreach ($p in $procs) {
        try { $p.Refresh(); if ($p.MainWindowHandle -ne [IntPtr]::Zero) { [NV.Win]::ShowWindowAsync($p.MainWindowHandle, 6) | Out-Null } } catch {}
      }
      Start-Sleep -Milliseconds 700
    }
  }
  $log.Add("start: launched $($procs.Count) terminal(s) minimised (forceMinimize=$canMinimize)")
}

"DFY configure complete (MetaTrader $MetaTraderVersion, InstanceCount=$InstanceCount, present=$($instances.Count)):"
$log | ForEach-Object { " - $_" }

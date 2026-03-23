#Requires -RunAsAdministrator
# Copy MetaTrader 5 - 001 to instances 002-010, hide real exes, create launcher shortcuts.
# Prerequisites: C:\MetaTrader\MetaTrader 5 - 001\ with terminal64.exe and metaeditor64.exe;
#                C:\MetaTrader\mt5_open.vbs

param(
    [string]$SourceFolder = "C:\MetaTrader\MetaTrader 5 - 001",
    [string]$BasePath = "C:\MetaTrader",
    [string]$LauncherPath = "C:\MetaTrader\mt5_open.vbs",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

# Pre-flight checks
if (-not (Test-Path -LiteralPath $SourceFolder)) {
    Write-Error "Source folder not found: $SourceFolder"
    exit 1
}

if (-not (Test-Path -LiteralPath $LauncherPath)) {
    Write-Error "Launcher not found: $LauncherPath. Copy mt5_open.vbs to C:\MetaTrader\ first."
    exit 1
}

$terminalExe = Join-Path $SourceFolder "terminal64.exe"
$metaEditorExe = Join-Path $SourceFolder "metaeditor64.exe"
if (-not (Test-Path -LiteralPath $terminalExe)) {
    Write-Error "terminal64.exe not found in source: $terminalExe"
    exit 1
}
if (-not (Test-Path -LiteralPath $metaEditorExe)) {
    Write-Error "metaeditor64.exe not found in source: $metaEditorExe"
    exit 1
}

$startMenuPath = "C:\ProgramData\Microsoft\Windows\Start Menu\Programs\MetaTrader"
$publicDesktop = "C:\Users\Public\Desktop"

# Ensure Start Menu\MetaTrader exists
if (-not (Test-Path -LiteralPath $startMenuPath)) {
    New-Item -Path $startMenuPath -ItemType Directory -Force | Out-Null
}

# Copy source to 002-010
foreach ($i in 2..10) {
    $suffix = $i.ToString("000")
    $destFolder = Join-Path $BasePath "MetaTrader 5 - $suffix"
    if (Test-Path -LiteralPath $destFolder) {
        if (-not $Force) {
            Write-Host "Skipping copy (already exists): $destFolder (use -Force to overwrite)"
        } else {
            Remove-Item -LiteralPath $destFolder -Recurse -Force
            Copy-Item -LiteralPath $SourceFolder -Destination $destFolder -Recurse
            Write-Host "Copied to: $destFolder"
        }
    } else {
        Copy-Item -LiteralPath $SourceFolder -Destination $destFolder -Recurse
        Write-Host "Copied to: $destFolder"
    }
}

function Set-FileHidden {
    param([string]$Path)
    $parent = Split-Path -Parent $Path
    $name = Split-Path -Leaf $Path
    $item = Get-ChildItem -LiteralPath $parent -Filter $name -Force -File -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $item) { return }
    $item.Attributes = $item.Attributes -bor [System.IO.FileAttributes]::Hidden
}

function New-MT5Shortcut {
    param(
        [string]$LnkPath,
        [string]$Arguments,
        [string]$WorkingDirectory,
        [string]$IconLocation
    )
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($LnkPath)
    $shortcut.TargetPath = "C:\Windows\System32\wscript.exe"
    $shortcut.Arguments = $Arguments
    $shortcut.WorkingDirectory = $WorkingDirectory
    $shortcut.IconLocation = $IconLocation
    $shortcut.Save()
    [System.Runtime.Interopservices.Marshal]::ReleaseComObject($shell) | Out-Null
}

# Process each folder 001-010
foreach ($i in 1..10) {
    $suffix = $i.ToString("000")
    $folderPath = Join-Path $BasePath "MetaTrader 5 - $suffix"
    if (-not (Test-Path -LiteralPath $folderPath)) { continue }

    $terminalExePath = Join-Path $folderPath "terminal64.exe"
    $metaEditorExePath = Join-Path $folderPath "metaeditor64.exe"

    # Hide real executables
    Set-FileHidden -Path $terminalExePath
    Set-FileHidden -Path $metaEditorExePath

    $termArgs = "`"$LauncherPath`" `"terminal`" `"$folderPath\`""
    $editorArgs = "`"$LauncherPath`" `"editor`" `"$folderPath\`""
    $termIcon = "$terminalExePath,0"
    $editorIcon = "$metaEditorExePath,0"

    # Create terminal64.exe.lnk
    $termLnk = Join-Path $folderPath "terminal64.exe.lnk"
    New-MT5Shortcut -LnkPath $termLnk -Arguments $termArgs -WorkingDirectory $folderPath -IconLocation $termIcon

    # Copy as MetaTrader 5 - XXX.lnk
    $namedLnk = Join-Path $folderPath "MetaTrader 5 - $suffix.lnk"
    Copy-Item -LiteralPath $termLnk -Destination $namedLnk -Force

    # Create MetaEditor64.exe.lnk
    $editorLnk = Join-Path $folderPath "MetaEditor64.exe.lnk"
    New-MT5Shortcut -LnkPath $editorLnk -Arguments $editorArgs -WorkingDirectory $folderPath -IconLocation $editorIcon

    # Deploy MetaTrader 5 - XXX.lnk to Public Desktop and Start Menu
    $deployDest1 = Join-Path $publicDesktop "MetaTrader 5 - $suffix.lnk"
    $deployDest2 = Join-Path $startMenuPath "MetaTrader 5 - $suffix.lnk"
    Copy-Item -LiteralPath $namedLnk -Destination $deployDest1 -Force
    Copy-Item -LiteralPath $namedLnk -Destination $deployDest2 -Force

    Write-Host "Configured: MetaTrader 5 - $suffix"
}

Write-Host "Done. Instances 001-010 configured. Shortcuts on Public Desktop and Start Menu\MetaTrader."

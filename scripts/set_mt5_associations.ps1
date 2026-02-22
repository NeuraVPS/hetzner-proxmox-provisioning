#Requires -RunAsAdministrator
# Set all MetaTrader 5 file and URL associations to use the launcher (mt5_open.vbs)
# and default icons from instance 001. Run once on the Windows template.
# The launcher re-applies HKCU associations 5s after opening a file so MetaTrader cannot keep overrides.

param(
    [string]$LauncherPath = "C:\MetaTrader\mt5_open.vbs",
    [string]$IconBase = "C:\MetaTrader\MetaTrader 5 - 001"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $LauncherPath)) {
    Write-Error "Launcher not found: $LauncherPath. Copy scripts/mt5_open.vbs to C:\MetaTrader\mt5_open.vbs first."
    exit 1
}

$launcherCmdTemplate = "wscript.exe `"$LauncherPath`" `"{0}`" `"%1`""
$classes = "HKLM:\SOFTWARE\Classes"

# Extension keys: (Default) = ProgId
$extensions = @(
    @{ Key = ".ex5"; ProgId = "EX5.File" },
    @{ Key = ".mq5"; ProgId = "MQL5.File" },
    @{ Key = ".mqh"; ProgId = "MQL5.Header" },
    @{ Key = ".mt5"; ProgId = "MetaTrader 5 Export File" }
)

# ProgIds: (Default) description, DefaultIcon, shell\open\command mode; URL Protocol for protocols
$progIds = @(
    @{
        ProgId = "EX5.File"
        Description = "MQL5 Program"
        Icon = "$IconBase\terminal64.exe,2"
        Mode = "ex5"
        UrlProtocol = $false
    },
    @{
        ProgId = "MQL5.File"
        Description = "MQL5 Source File"
        Icon = "$IconBase\metaeditor64.exe,1"
        Mode = "editor"
        UrlProtocol = $false
    },
    @{
        ProgId = "MQL5.Header"
        Description = "MQL5 Header File"
        Icon = "$IconBase\metaeditor64.exe,2"
        Mode = "editor"
        UrlProtocol = $false
    },
    @{
        ProgId = "mql5buy"
        Description = "URL:MQL5 Buy Protocol"
        Icon = "$IconBase\terminal64.exe,1"
        Mode = "terminal"
        UrlProtocol = $true
    },
    @{
        ProgId = "metaeditor5"
        Description = "URL:MetaEditor5 Protocol"
        Icon = "$IconBase\metaeditor64.exe,1"
        Mode = "editor"
        UrlProtocol = $true
    },
    @{
        ProgId = "MetaTrader 5 Export File"
        Description = "MetaTrader 5 Export"
        Icon = "$IconBase\terminal64.exe,15"
        Mode = "import"
        UrlProtocol = $false
    }
)

foreach ($ext in $extensions) {
    $path = Join-Path $classes $ext.Key
    New-Item -Path $path -Force | Out-Null
    Set-ItemProperty -Path $path -Name "(Default)" -Value $ext.ProgId -Type String
}

# .mq5 ShellNew for "New -> MQL5 File"
$mq5ShellNew = Join-Path $classes ".mq5\ShellNew"
New-Item -Path $mq5ShellNew -Force | Out-Null
Set-ItemProperty -Path $mq5ShellNew -Name "NullFile" -Value "" -Type String

foreach ($prog in $progIds) {
    $basePath = Join-Path $classes $prog.ProgId
    New-Item -Path $basePath -Force | Out-Null
    Set-ItemProperty -Path $basePath -Name "(Default)" -Value $prog.Description -Type String

    if ($prog.UrlProtocol) {
        Set-ItemProperty -Path $basePath -Name "URL Protocol" -Value "" -Type String
    }

    $iconPath = Join-Path $basePath "DefaultIcon"
    New-Item -Path $iconPath -Force | Out-Null
    Set-ItemProperty -Path $iconPath -Name "(Default)" -Value $prog.Icon -Type String

    $shellOpenPath = Join-Path $basePath "shell\open\command"
    New-Item -Path $shellOpenPath -Force | Out-Null
    $cmd = $launcherCmdTemplate -f $prog.Mode
    Set-ItemProperty -Path $shellOpenPath -Name "(Default)" -Value $cmd -Type String
}

Write-Host "MetaTrader 5 associations updated. Launcher: $LauncherPath"
Write-Host "Extensions: .ex5, .mq5, .mqh, .mt5. ProgIds: EX5.File, MQL5.File, MQL5.Header, mql5buy, metaeditor5, MetaTrader 5 Export File."

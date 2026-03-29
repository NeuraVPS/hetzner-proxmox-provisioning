# Windows VM Hook Instructions

## Overview

This setup makes Windows automatically inject the Java flag `-Djava.awt.headless=true` whenever an executable named `StrategyQuantX_nocheck.exe` is launched.

It uses:

- `windows_vm/sqx_hook_launcher.vbs` as the hook implementation
- IFEO (Image File Execution Options) `Debugger` registry key under `HKLM`

The hook applies by executable name, so it works even if users unzip the app in different folders.

## Prerequisites

- Run setup steps with **Administrator** privileges.
- `sqx_hook_launcher.vbs` must be copied to `C:\ProgramData\SQXHook\sqx_hook_launcher.vbs`.
- `-Djava.awt.headless=true` may prevent GUI behavior for some apps.

## Enable The Hook

### 1) Create hook folder and copy script

Open PowerShell as Administrator and run:

```powershell
New-Item -Path "C:\ProgramData\SQXHook" -ItemType Directory -Force | Out-Null
Copy-Item -Path ".\sqx_hook_launcher.vbs" -Destination "C:\ProgramData\NeuraVPS\sqx_hook_launcher.vbs" -Force
```

If your source file is in this repository, run the copy from:

`/workspaces/rootfs/hetzner-proxmox-provisioning/windows_vm`

### 2) Register IFEO Debugger entry

Open **Command Prompt as Administrator** and run:

```bat
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\StrategyQuantX_nocheck.exe" ^
 /v Debugger /t REG_SZ ^
 /d "\"C:\Windows\System32\wscript.exe\" \"C:\ProgramData\NeuraVPS\sqx_hook_launcher.vbs\"" /f
```

## Verify

### 1) Check registry value

```bat
reg query "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\StrategyQuantX_nocheck.exe" /v Debugger
```

Expected result: `Debugger` points to `wscript.exe` with `C:\ProgramData\SQXHook\sqx_hook_launcher.vbs`.

### 2) Launch app normally

Start any `StrategyQuantX_nocheck.exe` (double-click or command line).  
The hook should run automatically and inject:

- `JAVA_TOOL_OPTIONS=-Djava.awt.headless=true`

## Disable / Rollback

Open Command Prompt as Administrator and run:

```bat
reg delete "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\StrategyQuantX_nocheck.exe" /v Debugger /f
```

Optional cleanup:

- Delete `C:\ProgramData\SQXHook\sqx_hook_launcher.vbs`
- Delete `C:\ProgramData\SQXHook\` if no longer needed

## Troubleshooting

- **No launch and no error**: verify the IFEO key value and script path are correct with `reg query`.
- **Access denied / registry write fails**: run shell as Administrator.
- **App launches but no GUI**: expected for some apps with `-Djava.awt.headless=true`; remove the flag if GUI is required.
- **Quoting issues in `reg add`**: copy the command exactly, including escaped quotes (`\"`).
- **Recursion concerns**: `sqx_hook_launcher.vbs` uses an internal environment bypass flag to prevent IFEO recursion during the relaunch step.

## MT5 Portable Hook Deployment

This is the new MT5 model for Windows VMs:

- Keep standard executable-based associations (no `mt5_open.vbs` dependency).
- Set first-time default associations to instance `001` executables.
- Enforce `/portable` at runtime using IFEO hooks for `terminal64.exe` and `metaeditor64.exe`.

### 1) Install hook scripts on the VM

Place the unified script on the VM (example location):

- `C:\MetaTrader\hooks\mt_hook_launcher.vbs`

Source script in this repository:

- `windows_vm/mt_hook_launcher.vbs`

The unified VBS hook does:

- Accept IFEO arguments (`originalExePath` first, then original args).
- Add `/portable` if missing.
- If a passed file is under `C:\MetaTrader\<any-subfolder>\...`, always reroute to `C:\MetaTrader\<any-subfolder>\{terminal64.exe|metaeditor64.exe}`.
- If a passed file is outside `C:\MetaTrader\...`, use the original executable path from IFEO.
- Temporarily remove and restore its own IFEO `Debugger` entry before/after launch (recursion-safe).

### 2) Register IFEO hooks

Run in **Command Prompt as Administrator**:

```bat
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\terminal64.exe" ^
 /v Debugger /t REG_SZ ^
 /d "\"C:\Windows\System32\wscript.exe\" \"C:\ProgramData\NeuraVPS\mt_hook_launcher.vbs\"" /f

reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\metaeditor64.exe" ^
 /v Debugger /t REG_SZ ^
 /d "\"C:\Windows\System32\wscript.exe\" \"C:\ProgramData\NeuraVPS\mt_hook_launcher.vbs\"" /f
```

### 3) Set first-time default associations to instance 001

Run in **PowerShell as Administrator**:

```powershell
$classes = "HKLM:\SOFTWARE\Classes"
$terminalExe = "C:\MetaTrader\MetaTrader 5 - 001\terminal64.exe"
$editorExe = "C:\MetaTrader\MetaTrader 5 - 001\metaeditor64.exe"

$extMap = @{
    ".ex5" = "EX5.File"
    ".mq5" = "MQL5.File"
    ".mqh" = "MQL5.Header"
    ".mt5" = "MetaTrader 5 Export File"
}

foreach ($ext in $extMap.Keys) {
    $extKey = Join-Path $classes $ext
    New-Item -Path $extKey -Force | Out-Null
    Set-ItemProperty -Path $extKey -Name "(Default)" -Value $extMap[$ext] -Type String
}

$openCommands = @{
    "EX5.File" = "`"$terminalExe`" `"%1`""
    "MQL5.File" = "`"$editorExe`" `"%1`""
    "MQL5.Header" = "`"$editorExe`" `"%1`""
    "MetaTrader 5 Export File" = "`"$terminalExe`" `"%1`""
    "mql5buy" = "`"$terminalExe`" `"%1`""
    "metaeditor5" = "`"$editorExe`" `"%1`""
}

foreach ($progId in $openCommands.Keys) {
    $progPath = Join-Path $classes $progId
    New-Item -Path $progPath -Force | Out-Null

    if ($progId -in @("mql5buy", "metaeditor5")) {
        Set-ItemProperty -Path $progPath -Name "URL Protocol" -Value "" -Type String
    }

    $cmdPath = Join-Path $progPath "shell\open\command"
    New-Item -Path $cmdPath -Force | Out-Null
    Set-ItemProperty -Path $cmdPath -Name "(Default)" -Value $openCommands[$progId] -Type String
}
```

### 4) Verify

Run in **Command Prompt as Administrator**:

```bat
reg query "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\terminal64.exe" /v Debugger
reg query "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\metaeditor64.exe" /v Debugger

reg query "HKLM\SOFTWARE\Classes\EX5.File\shell\open\command" /ve
reg query "HKLM\SOFTWARE\Classes\MQL5.File\shell\open\command" /ve
reg query "HKLM\SOFTWARE\Classes\mql5buy\shell\open\command" /ve
reg query "HKLM\SOFTWARE\Classes\metaeditor5\shell\open\command" /ve
```

Then manually test:

- Launch `terminal64.exe` and `metaeditor64.exe` directly.
- Open `.mq5`/`.ex5` files and protocol links.
- Confirm `/portable` is always enforced by the hook.

### 5) Rollback IFEO hooks

Run in **Command Prompt as Administrator**:

```bat
reg delete "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\terminal64.exe" /v Debugger /f
reg delete "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\metaeditor64.exe" /v Debugger /f
```

This removes launch-time `/portable` injection, but keeps default associations in place.

## MT4 Portable Hook Deployment

MT4 support uses IFEO hooks only to enforce `/portable` at launch time:

- Hook `terminal.exe` and `metaeditor.exe`.
- Do not set MT4 file associations in this phase.

### 1) Install hook scripts on the VM

Place the unified script on the VM (example location):

- `C:\MetaTrader\hooks\mt_hook_launcher.vbs`

Source script in this repository:

- `windows_vm/mt_hook_launcher.vbs`

### 2) Register IFEO hooks

Run in **Command Prompt as Administrator**:

```bat
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\terminal.exe" ^
 /v Debugger /t REG_SZ ^
 /d "\"C:\Windows\System32\wscript.exe\" \"C:\ProgramData\NeuraVPS\mt_hook_launcher.vbs\"" /f

reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\metaeditor.exe" ^
 /v Debugger /t REG_SZ ^
 /d "\"C:\Windows\System32\wscript.exe\" \"C:\ProgramData\NeuraVPS\mt_hook_launcher.vbs\"" /f
```

### 3) Verify

Run in **Command Prompt as Administrator**:

```bat
reg query "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\terminal.exe" /v Debugger
reg query "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\metaeditor.exe" /v Debugger
```

Then manually test:

- Launch `terminal.exe` and `metaeditor.exe` directly.
- Confirm `/portable` is always enforced by the hook.

### 4) Rollback IFEO hooks

Run in **Command Prompt as Administrator**:

```bat
reg delete "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\terminal.exe" /v Debugger /f
reg delete "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\metaeditor.exe" /v Debugger /f
```

# Prepare Windows Template

## Checklist

- Apply Windows and Winget updates
- Disable Password lock Policy
- Apply Java patch for SQX
- Install desired software (including 10× MetaTrader 5 portable in `C:\MetaTrader\MetaTrader 5 - 001` … `010`)
- Set MetaTrader file/URL associations: copy `scripts/mt5_open.vbs` to `C:\MetaTrader\mt5_open.vbs`, then run as Administrator: `powershell -ExecutionPolicy Bypass -File scripts\set_mt5_associations.ps1` (or from `C:\Provisioning\` if you copied the script there). This configures EX5, MQL5, MQL5.Header, mql5buy, metaeditor5, and MetaTrader 5 Export to use the launcher and default icons from instance 001. The launcher re-applies HKCU associations 5 seconds after opening a file so MetaTrader cannot keep overrides.
- NTP servers for Hetzner
- Permitir Samba en el firewall de Windows
- Crear carpeta C:\Mis Servidores y enlace en el escritorio
- Disk cleanup
- Sysprep with unattend.xml
- From Linux, remove recovery partition

## Java Issue with sqx

```powershell
setx _JAVA_OPTIONS "-Djava.awt.headless=true" /M
setx JAVA_TOOL_OPTIONS "-Djava.awt.headless=true" /M
```

## Other useful configurations

```powershell
# Don't lock accounts on failed login attempts
net accounts /lockoutthreshold:0

# Don't require password changes
net accounts /maxpwage:UNLIMITED

# Hide Telemetry configuration on first login
New-Item -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows\DataCollection" -Force | Out-Null
Set-ItemProperty -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows\DataCollection" -Name "AllowTelemetry" -Value 1 -Type DWord

New-Item -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows\OOBE" -Force | Out-Null
Set-ItemProperty -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows\OOBE" -Name "DisablePrivacyExperience" -Value 1 -Type DWord

# Hide Server Manager on login
Set-ItemProperty -Path "HKLM:\Software\Microsoft\ServerManager" -Name "DoNotOpenServerManagerAtLogon" -Value 1 -Type DWord

# Disable WindowsFeedbackHub installation for new users
Get-AppxProvisionedPackage -Online | Where-Object DisplayName -like "Microsoft.WindowsFeedbackHub" | Remove-AppxProvisionedPackage -Online

# Disable Edge start wizard and make it clean
$k = "HKLM:\SOFTWARE\Policies\Microsoft\Edge"
New-Item -Path $k -Force | Out-Null

# Quitar first-run / wizard
New-ItemProperty $k -Name HideFirstRunExperience -PropertyType DWord -Value 1 -Force | Out-Null

# Sin procesos background (server friendly)
New-ItemProperty $k -Name BackgroundModeEnabled -PropertyType DWord -Value 0 -Force | Out-Null
New-ItemProperty $k -Name StartupBoostEnabled -PropertyType DWord -Value 0 -Force | Out-Null

# NTP: eliminar TODO el contenido
New-ItemProperty $k -Name NewTabPageContentEnabled -PropertyType DWord -Value 0 -Force | Out-Null
New-ItemProperty $k -Name NewTabPageQuickLinksEnabled -PropertyType DWord -Value 0 -Force | Out-Null
New-ItemProperty $k -Name NewTabPageBackgroundImageEnabled -PropertyType DWord -Value 0 -Force | Out-Null
New-ItemProperty $k -Name NewTabPageCustomizeEnabled -PropertyType DWord -Value 0 -Force | Out-Null
New-ItemProperty $k -Name NewTabPageAppsEnabled -PropertyType DWord -Value 0 -Force | Out-Null
New-ItemProperty $k -Name NewTabPageHideWeather -PropertyType DWord -Value 1 -Force | Out-Null

# Show all file extensions in explorer
Start-Process powershell -ArgumentList @"
New-PSDrive -Name HKU -PSProvider Registry -Root HKEY_USERS | Out-Null
reg load HKU\DefaultUser 'C:\Users\Default\NTUSER.DAT'
New-Item -Path 'HKU:\DefaultUser\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced' -Force | Out-Null
New-ItemProperty -Path 'HKU:\DefaultUser\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced' -Name 'HideFileExt' -PropertyType DWord -Value 0 -Force | Out-Null
reg unload HKU\DefaultUser
"@

# SQX in High priority
$basePath = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\StrategyQuantX_nocheck.exe\PerfOptions'
New-Item -Path $basePath -Force | Out-Null
New-ItemProperty -Path $basePath -Name 'CpuPriorityClass' -PropertyType DWord -Value 6 -Force | Out-Null

# Autologin
$RegPath = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"
Set-ItemProperty -Path $RegPath -Name "AutoAdminLogon" -Value "1" -Type String
Set-ItemProperty -Path $RegPath -Name "DefaultUserName" -Value "Administrador" -Type String
#Set-ItemProperty -Path $RegPath -Name "DefaultPassword" -Value "<new password>" -Type String

# Allow SAMBA through Windows Firewall
Set-NetFirewallRule -DisplayName 'Uso compartido de archivos e impresoras (restrictivo) (SMB de entrada)' -Enabled True

# Create Mis Servidores folder and Desktop symlink
$targetFolder = 'C:\My Servers';
$publicDesktop = 'C:\Users\Public\Desktop';
$linkPath = "$publicDesktop\My Servers";

if (!(Test-Path $targetFolder)) { New-Item -ItemType Directory -Path $targetFolder -Force | Out-Null }

if (Test-Path $linkPath) {
    $item = Get-Item $linkPath -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { exit }
    Remove-Item $linkPath -Recurse -Force
}

New-Item -ItemType SymbolicLink -Path $linkPath -Target $targetFolder -Force | Out-Null

# desktop.ini
[.ShellClassInfo]
LocalizedResourceName=Mis Servidores

attrib +s 'C:\My Servers'
attrib +h 'C:\My Servers\desktop.ini'

# Optimized UI
$base = "Registry::HKEY_USERS\.DEFAULT\Control Panel\Desktop"

Set-ItemProperty $base -Name DragFullWindows -Value "0"
Set-ItemProperty $base -Name MenuAnimation -Value "0"
Set-ItemProperty $base -Name ToolTipAnimation -Value "0"
Set-ItemProperty $base -Name ComboBoxAnimation -Value "0"
Set-ItemProperty $base -Name MinAnimate -Value "0"
Set-ItemProperty $base -Name FontSmoothing -Value "2"
Set-ItemProperty $base -Name FontSmoothingType -Value 2
Set-ItemProperty $base -Name CursorShadow -Value 0
Set-ItemProperty $base -Name DropShadow -Value 0
Set-ItemProperty $base -Name UIEffects -Value 0
Set-ItemProperty $base -Name UserPreferencesMask -Value ([byte[]](0x90,0x12,0x03,0x80,0x10,0x00,0x00,0x00))

New-Item "Registry::HKEY_USERS\.DEFAULT\Software\Microsoft\Windows\CurrentVersion\Explorer\VisualEffects" -Force | Out-Null
Set-ItemProperty "Registry::HKEY_USERS\.DEFAULT\Software\Microsoft\Windows\CurrentVersion\Explorer\VisualEffects" `
  -Name VisualFXSetting -Value 3

```

## Winget update fix for Sysprep

```powershell
Get-AppxPackage *winget* | Remove-AppxPackage
```

## Sysprep

The answer file sets `CopyProfile` in the **specialize** pass (it does not run in oobeSystem). That copies the built-in Administrator profile to the Default User template. Even when correct, **CopyProfile often does not preserve Edge** (and other modern app data): many settings are user-SID–bound or encrypted and get reset on first logon.

### Reliable way to keep Edge (and other profile data): export/restore scripts

1. **On the reference VM (before sysprep):**
   - Copy `scripts/export_edge_profile.ps1` and `scripts/restore_edge_profile.ps1` to `C:\Provisioning\`.
   - Configure Edge (and anything else in the Administrator profile) as desired.
   - Run as Administrator:
     ```powershell
     C:\Provisioning\export_edge_profile.ps1
     ```
   - This copies the Edge profile to `C:\Provisioning\EdgeDefault` (survives generalize).

2. **Sysprep** as below. The unattend `FirstLogonCommands` will run `restore_edge_profile.ps1` at first logon if it exists; that script restores `EdgeDefault` into the new user’s profile.

3. **If you don’t use the Edge scripts:** leave `C:\Provisioning\` empty or omit the scripts; the first-logon command only runs the restore script if the file exists.

**CopyProfile (specialize) requirements:** use only the built-in Administrator account and run sysprep as Administrator. Taskbar pins, Start layout, and some encrypted settings are still not preserved; use GPO or scripts for those.

```powershell
cd C:\Windows\System32\Sysprep
.\sysprep.exe /generalize /oobe /shutdown /unattend:C:\Windows\unattend.xml
```

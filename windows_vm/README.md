# Windows VM Hook Instructions

## Overview

This setup makes Windows automatically inject the Java flag `-Djava.awt.headless=true` whenever an executable named `StrategyQuantX_nocheck.exe` is launched.

It uses:

- `windows_vm/sqx-hook.ps1` as the launcher hook
- IFEO (Image File Execution Options) `Debugger` registry key under `HKLM`

The hook applies by executable name, so it works even if users unzip the app in different folders.

## Prerequisites

- Run setup steps with **Administrator** privileges.
- PowerShell must be available (standard on Windows Server/Desktop).
- `sqx-hook.ps1` must be copied to `C:\ProgramData\SQXHook\sqx-hook.ps1`.
- `-Djava.awt.headless=true` may prevent GUI behavior for some apps.

## Enable The Hook

### 1) Create hook folder and copy script

Open PowerShell as Administrator and run:

```powershell
New-Item -Path "C:\ProgramData\SQXHook" -ItemType Directory -Force | Out-Null
Copy-Item -Path ".\sqx-hook.ps1" -Destination "C:\ProgramData\SQXHook\sqx-hook.ps1" -Force
```

If your source file is in this repository, run the copy from:

`/workspaces/rootfs/hetzner-proxmox-provisioning/windows_vm`

### 2) Register IFEO Debugger entry

Open **Command Prompt as Administrator** and run:

```bat
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\StrategyQuantX_nocheck.exe" ^
 /v Debugger /t REG_SZ ^
 /d "\"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe\" -NoProfile -ExecutionPolicy Bypass -File \"C:\ProgramData\SQXHook\sqx-hook.ps1\"" /f
```

## Verify

### 1) Check registry value

```bat
reg query "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\StrategyQuantX_nocheck.exe" /v Debugger
```

Expected result: `Debugger` points to PowerShell with `C:\ProgramData\SQXHook\sqx-hook.ps1`.

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

- Delete `C:\ProgramData\SQXHook\sqx-hook.ps1`
- Delete `C:\ProgramData\SQXHook\` if no longer needed

## Troubleshooting

- **No launch and no error**: verify the IFEO key value and script path are correct with `reg query`.
- **Access denied / registry write fails**: run shell as Administrator.
- **App launches but no GUI**: expected for some apps with `-Djava.awt.headless=true`; remove the flag if GUI is required.
- **Quoting issues in `reg add`**: copy the command exactly, including escaped quotes (`\"`).
- **Recursion concerns**: `sqx-hook.ps1` already prevents IFEO recursion by temporarily removing and restoring the `Debugger` value around process launch.

## MT5 Portable Hook Deployment

This is the new MT5 model for Windows VMs:

- Keep standard executable-based associations (no `mt5_open.vbs` dependency).
- Set first-time default associations to instance `001` executables.
- Enforce `/portable` at runtime using IFEO hooks for `terminal64.exe` and `metaeditor64.exe`.

### 1) Install hook scripts on the VM

Place both scripts on the VM (example location):

- `C:\MetaTrader\hooks\mt5_terminal_hook.ps1`
- `C:\MetaTrader\hooks\mt5_metaeditor_hook.ps1`

Each hook should:

- Accept IFEO arguments (`originalExePath` first, then original args).
- Add `/portable` if missing.
- If a passed file is under `C:\MetaTrader\MetaTrader 5 - 001..010\...`, reroute to the matching instance executable.
- Temporarily remove and restore its own IFEO `Debugger` entry before/after launch (recursion-safe).

### 2) Register IFEO hooks

Run in **Command Prompt as Administrator**:

```bat
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\terminal64.exe" ^
 /v Debugger /t REG_SZ ^
 /d "\"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe\" -NoProfile -ExecutionPolicy Bypass -File \"C:\MetaTrader\hooks\mt5_terminal_hook.ps1\"" /f

reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\metaeditor64.exe" ^
 /v Debugger /t REG_SZ ^
 /d "\"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe\" -NoProfile -ExecutionPolicy Bypass -File \"C:\MetaTrader\hooks\mt5_metaeditor_hook.ps1\"" /f
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

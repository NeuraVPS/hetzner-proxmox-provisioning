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

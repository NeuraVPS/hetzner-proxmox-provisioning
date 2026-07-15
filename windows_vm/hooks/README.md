Copy both hooks in `C:\ProgramData\NeuraVPS\`, then:

```powershell
$k = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\StrategyQuantX_nocheck.exe'
New-Item -Path $k -Force | Out-Null
Set-ItemProperty -Path $k -Name Debugger -Type String -Value '"C:\Windows\System32\wscript.exe" "C:\ProgramData\NeuraVPS\sqx_hook_launcher.vbs"'
```

```powershell
$k = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\terminal64.exe'
New-Item -Path $k -Force | Out-Null
Set-ItemProperty -Path $k -Name Debugger -Type String -Value '"C:\Windows\System32\wscript.exe" "C:\ProgramData\NeuraVPS\mt_hook_launcher.vbs"'

$k = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\metaeditor64.exe'
New-Item -Path $k -Force | Out-Null
Set-ItemProperty -Path $k -Name Debugger -Type String -Value '"C:\Windows\System32\wscript.exe" "C:\ProgramData\NeuraVPS\mt_hook_launcher.vbs"'
```

```powershell
$k = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\terminal.exe'
New-Item -Path $k -Force | Out-Null
Set-ItemProperty -Path $k -Name Debugger -Type String -Value '"C:\Windows\System32\wscript.exe" "C:\ProgramData\NeuraVPS\mt_hook_launcher.vbs"'

$k = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\metaeditor.exe'
New-Item -Path $k -Force | Out-Null
Set-ItemProperty -Path $k -Name Debugger -Type String -Value '"C:\Windows\System32\wscript.exe" "C:\ProgramData\NeuraVPS\mt_hook_launcher.vbs"'
```

## SQX v144 — use `sqx144_hook_launcher.vbs` (different recursion-guard key)

v144's engine is **`StrategyQuantX.exe`** (there is no `StrategyQuantX_nocheck.exe`). `sqx_hook_launcher.vbs` hardcodes its recursion-guard IFEO key to `StrategyQuantX_nocheck.exe`, so it **must not** be pointed at `StrategyQuantX.exe` — the guard would clear the wrong key on relaunch and the hook would **fork-bomb**. Use `sqx144_hook_launcher.vbs` (byte-identical except the guard key is `StrategyQuantX.exe`):

```powershell
$k = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\StrategyQuantX.exe'
New-Item -Path $k -Force | Out-Null
Set-ItemProperty -Path $k -Name Debugger -Type String -Value '"C:\Windows\System32\wscript.exe" "C:\ProgramData\NeuraVPS\sqx144_hook_launcher.vbs"'
```

⚠️ **NOT a default/provisioning step.** Headless disables opening external URLs from inside SQX, so the v144 hook is applied **case-by-case** only for v144 boxes hit by the `awt.dll` / `displayChanged` RDP-blip crash (agent doc §9.9.15b, "Mode A"). Default installs stay v143, which uses the `_nocheck.exe` hook above.

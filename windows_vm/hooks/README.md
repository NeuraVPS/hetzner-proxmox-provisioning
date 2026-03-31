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

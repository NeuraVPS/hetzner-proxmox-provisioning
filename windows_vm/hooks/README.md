Copy both hooks in `C:\ProgramData\NeuraVPS\`, then:

```bat
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\StrategyQuantX_nocheck.exe" ^
 /v Debugger /t REG_SZ ^
 /d "\"C:\Windows\System32\wscript.exe\" \"C:\ProgramData\NeuraVPS\sqx_hook_launcher.vbs\"" /f
```

```bat
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\terminal64.exe" ^
 /v Debugger /t REG_SZ ^
 /d "\"C:\Windows\System32\wscript.exe\" \"C:\ProgramData\NeuraVPS\mt_hook_launcher.vbs\"" /f

reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\metaeditor64.exe" ^
 /v Debugger /t REG_SZ ^
 /d "\"C:\Windows\System32\wscript.exe\" \"C:\ProgramData\NeuraVPS\mt_hook_launcher.vbs\"" /f
```

```bat
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\terminal.exe" ^
 /v Debugger /t REG_SZ ^
 /d "\"C:\Windows\System32\wscript.exe\" \"C:\ProgramData\NeuraVPS\mt_hook_launcher.vbs\"" /f

reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\metaeditor.exe" ^
 /v Debugger /t REG_SZ ^
 /d "\"C:\Windows\System32\wscript.exe\" \"C:\ProgramData\NeuraVPS\mt_hook_launcher.vbs\"" /f
```

SQX preconfigure RAM, add:

```
option -Xmx55g
```

C:\SQX_XXX\StrategyQuantX_nocheck.config

MT5 Set first-time default associations to instance 001

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

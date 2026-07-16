# NeuraVPS launch hooks (IFEO)

These VBS launchers are wired via the **Image File Execution Options** `Debugger`
key so they intercept each target `.exe` and relaunch it with the right options:

- `sqx_hook_launcher.vbs` — SQX **v142/v143** engine `StrategyQuantX_nocheck.exe` → injects `JAVA_TOOL_OPTIONS=-Djava.awt.headless=true` for the SQX process only, so SQX never binds to the volatile Remote-Desktop display and survives RDP/network blips (the `awt.dll`/`displayChanged` crash — agent doc §9.9.15b "Mode A").
- `sqx144_hook_launcher.vbs` — SQX **v144** engine `StrategyQuantX.exe`. Byte-identical to the v143 launcher **except its recursion-guard IFEO key is `StrategyQuantX.exe`** (see the fork-bomb warning below).
- `mt_hook_launcher.vbs` — MetaTrader `terminal64/metaeditor64/terminal/metaeditor.exe` → ensures `/portable` on non-shortcut launches (e.g. MT's self-update relaunch) so the terminal keeps using its portable data dir.

## Install (standard — apply all of it)

1. Copy the three `.vbs` files into `C:\ProgramData\NeuraVPS\`.

2. **Remove any legacy machine-scope headless variables.** Older images set `JAVA_TOOL_OPTIONS` / `_JAVA_OPTIONS = -Djava.awt.headless=true` at **global (machine) scope**, which forces headless on *every* Java GUI app (QuantAnalyzer etc. won't open). The per-app hooks above replace that, so delete the globals:

   ```powershell
   $env = 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment'
   foreach ($v in 'JAVA_TOOL_OPTIONS','_JAVA_OPTIONS') {
     if ($null -ne (Get-ItemProperty -Path $env -Name $v -ErrorAction SilentlyContinue).$v) {
       Remove-ItemProperty -Path $env -Name $v -Force
     }
   }
   ```

3. **SQX — wire both engines** (v143 `_nocheck` and v144 `StrategyQuantX.exe`):

   ```powershell
   $k = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\StrategyQuantX_nocheck.exe'
   New-Item -Path $k -Force | Out-Null
   Set-ItemProperty -Path $k -Name Debugger -Type String -Value '"C:\Windows\System32\wscript.exe" "C:\ProgramData\NeuraVPS\sqx_hook_launcher.vbs"'

   $k = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\StrategyQuantX.exe'
   New-Item -Path $k -Force | Out-Null
   Set-ItemProperty -Path $k -Name Debugger -Type String -Value '"C:\Windows\System32\wscript.exe" "C:\ProgramData\NeuraVPS\sqx144_hook_launcher.vbs"'
   ```

4. **MetaTrader — wire all four exes:**

   ```powershell
   foreach ($exe in 'terminal64.exe','metaeditor64.exe','terminal.exe','metaeditor.exe') {
     $k = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\$exe"
     New-Item -Path $k -Force | Out-Null
     Set-ItemProperty -Path $k -Name Debugger -Type String -Value '"C:\Windows\System32\wscript.exe" "C:\ProgramData\NeuraVPS\mt_hook_launcher.vbs"'
   }
   ```

The hooks take effect on the **next launch** of each app — no reinstall, no reboot, no data loss. Existing running SQX/MT keep their current process; they pick up the hook when next opened.

## ⚠️ Critical: v144 needs `sqx144_hook_launcher.vbs`, never `sqx_hook_launcher.vbs`

`sqx_hook_launcher.vbs` hardcodes its recursion-guard IFEO key to `StrategyQuantX_nocheck.exe`. If you point `StrategyQuantX.exe`'s Debugger at *that* VBS, the guard clears the wrong key on relaunch and the hook **fork-bombs**. `sqx144_hook_launcher.vbs` is identical except the guard key is `StrategyQuantX.exe`. (Derive it from the v143 file by replacing `StrategyQuantX_nocheck.exe` → `StrategyQuantX.exe`, which is its only occurrence — this also carries over any local customization such as an added `_JAVA_OPTIONS` line.)

## Note — v144 tradeoff (accepted, applied fleet-wide 2026-07-16)

Headless mode disables opening external browser URLs from **inside** SQX for the hooked engine. This was previously kept case-by-case for that reason; as of 2026-07-16 the decision is to apply the SQX v144 hook fleet-wide (crash-immunity across RDP blips outweighs the in-app-link convenience). The durable long-term fix remains a `java.desktop` build without the 25.0.x display bug, once available.

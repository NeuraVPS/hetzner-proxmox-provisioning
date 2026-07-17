# NeuraVPS launch hooks (IFEO)

These VBS launchers are wired via the **Image File Execution Options** `Debugger`
key so they intercept each target `.exe` and relaunch it with the right options:

- `sqx_hook_launcher.vbs` — SQX **v142/v143** engine `StrategyQuantX_nocheck.exe` → injects `JAVA_TOOL_OPTIONS=-Djava.awt.headless=true` for the SQX process only, so SQX never binds to the volatile Remote-Desktop display and survives RDP/network blips (the `awt.dll`/`displayChanged` crash — agent doc §9.9.15b "Mode A").
- `sqx144_hook_launcher.vbs` — SQX **v144** engine `StrategyQuantX.exe`. Byte-identical to the v143 launcher **except its recursion-guard IFEO key is `StrategyQuantX.exe`** (see the fork-bomb warning below). **v144-ONLY — see the gate below.**
- `mt_hook_launcher.vbs` — MetaTrader `terminal64/metaeditor64/terminal/metaeditor.exe` → ensures `/portable` on non-shortcut launches (e.g. MT's self-update relaunch) so the terminal keeps using its portable data dir. **Portable-data boxes ONLY — see the gate below.**

## ⚠️ Hard gates learned the hard way

The 2026-07-16 fleet sweep applied steps 3+4 below unconditionally and caused
two regressions (gates 1–2). A third gate (gate 3, 2026-07-17) is a scripting
hazard in the install steps themselves. All three are now mandatory:

1. **`StrategyQuantX.exe` → `sqx144_hook_launcher.vbs` ONLY on boxes with a real
   v144 install** (a `C:\SQX*144*` / `StrategyQuant*144*` dir). On **v143**
   boxes `StrategyQuantX.exe` is the interactive launcher/checker, and hooking
   it makes every double-click **die silently** (cursor spins, no window, no
   process — customers report "SQX no abre"; a VPS reboot does not help). The
   v143 engine hook is `StrategyQuantX_nocheck.exe` alone. Remediation on a
   mis-hooked v143 box: delete that one Debugger value — agent doc §9.9.28(b).

2. **The MT hook only on boxes whose real data is portable** (lives inside
   `C:\MetaTrader\...`). The hook forces `/portable`; on a box where the
   customer's real terminals run **non-portable**
   (`C:\Users\<u>\AppData\Roaming\MetaQuotes\Terminal\<hash>`), forcing
   portable makes MT open the other (near-empty/template) data set — the
   customer sees ALL their EAs/charts/accounts "gone" (hidden, not deleted).
   Decide by comparing EA counts / `accounts.dat` / newest `logs\YYYYMMDD.log`
   date on each side; when the non-portable side is as new or newer, do NOT
   hook. Remediation on a wrongly-hooked box: delete the 4 MT Debugger values —
   agent doc §9.9.28(a).

3. **Never `New-Item -Path <IFEO key> -Force` on a key that already exists** — in
   PowerShell that *recreates* the key and silently deletes its subkeys, which on
   `StrategyQuantX_nocheck.exe` includes the `PerfOptions` subkey holding
   `CpuPriorityClass=6` (SQX high-priority). Re-running the install steps on an
   already-configured box would drop high-priority. Step 3/4 below guard every
   create with `if (-not (Test-Path $k)) { New-Item … }` so a re-run only ensures
   the key exists and sets `Debugger`, never clobbering `PerfOptions`. (Seen
   2026-07-17 during the template hook refresh: an unguarded re-assert wiped
   high-priority; it had to be restored with a fresh `PerfOptions` write.)

## Install (gated — apply per the gates above)

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

3. **SQX — always wire the v143 engine; wire `StrategyQuantX.exe` ONLY if a
   v144 install exists** (gate 1):

   ```powershell
   # NOTE: `New-Item -Path <IFEO key> -Force` on a key that ALREADY EXISTS wipes its
   # subkeys — including the PerfOptions subkey that carries StrategyQuantX_nocheck.exe's
   # CpuPriorityClass=6 (high-priority) setting. Guard every create with Test-Path so a
   # re-run only ensures the key exists and never clobbers PerfOptions (see gate 3 below).
   $k = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\StrategyQuantX_nocheck.exe'
   if (-not (Test-Path $k)) { New-Item -Path $k -Force | Out-Null }
   Set-ItemProperty -Path $k -Name Debugger -Type String -Value '"C:\Windows\System32\wscript.exe" "C:\ProgramData\NeuraVPS\sqx_hook_launcher.vbs"'

   $has144 = @(Get-ChildItem 'C:\' -Directory -EA SilentlyContinue |
     Where-Object { $_.Name -match 'SQX.*144|StrategyQuant.*144' }).Count -gt 0
   $k = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\StrategyQuantX.exe'
   if ($has144) {
     if (-not (Test-Path $k)) { New-Item -Path $k -Force | Out-Null }
     Set-ItemProperty -Path $k -Name Debugger -Type String -Value '"C:\Windows\System32\wscript.exe" "C:\ProgramData\NeuraVPS\sqx144_hook_launcher.vbs"'
   } else {
     # v143-only box: this IFEO must NOT exist (silent launcher death otherwise)
     Remove-ItemProperty -Path $k -Name Debugger -Force -EA SilentlyContinue
   }
   ```

4. **MetaTrader — wire all four exes ONLY on portable-data boxes** (gate 2; a
   freshly provisioned box from our images is portable — an aged box the
   customer has used non-portable is NOT):

   ```powershell
   foreach ($exe in 'terminal64.exe','metaeditor64.exe','terminal.exe','metaeditor.exe') {
     $k = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\$exe"
     if (-not (Test-Path $k)) { New-Item -Path $k -Force | Out-Null }  # Test-Path guard: never -Force an existing key (wipes subkeys)
     Set-ItemProperty -Path $k -Name Debugger -Type String -Value '"C:\Windows\System32\wscript.exe" "C:\ProgramData\NeuraVPS\mt_hook_launcher.vbs"'
   }
   ```

The hooks take effect on the **next launch** of each app — no reinstall, no reboot, no data loss. Existing running SQX/MT keep their current process; they pick up the hook when next opened.

## ⚠️ Critical: v144 needs `sqx144_hook_launcher.vbs`, never `sqx_hook_launcher.vbs`

`sqx_hook_launcher.vbs` hardcodes its recursion-guard IFEO key to `StrategyQuantX_nocheck.exe`. If you point `StrategyQuantX.exe`'s Debugger at *that* VBS, the guard clears the wrong key on relaunch and the hook **fork-bombs**. `sqx144_hook_launcher.vbs` is identical except the guard key is `StrategyQuantX.exe`. (Derive it from the v143 file by replacing `StrategyQuantX_nocheck.exe` → `StrategyQuantX.exe`, which is its only occurrence — this also carries over any local customization such as an added `_JAVA_OPTIONS` line.)

## ⚠️ Critical: the MT hook guard must stay concurrency-safe

`mt_hook_launcher.vbs` guards recursion by delete→launch→re-add of a **shared**
IFEO key. When several MT terminals auto-start at logon, interleaved re-adds
re-intercept a relaunched terminal and the box **fork-bombs** (hundreds of
`wscript`/`reg` per minute — the 2026-07-16 incident). The current VBS
serializes that critical section with an atomic lock dir
(`C:\ProgramData\NeuraVPS\mt_hook.lock.d`, stale-steal 12 s, fail-open). Any
future edit must preserve the lock, and any deployed copy must contain it
(check for `mt_hook.lock.d` in the file).

## Note — v144 tradeoff (accepted, applied fleet-wide 2026-07-16)

Headless mode disables opening external browser URLs from **inside** SQX for the hooked engine. This was previously kept case-by-case for that reason; as of 2026-07-16 the decision is to apply the SQX v144 hook fleet-wide **on v144 boxes** (crash-immunity across RDP blips outweighs the in-app-link convenience). The durable long-term fix remains a `java.desktop` build without the 25.0.x display bug, once available.

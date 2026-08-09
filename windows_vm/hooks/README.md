# NeuraVPS launch hooks (IFEO)

> # 🛑 `sqx144_hook_launcher.vbs` IS WITHDRAWN — DO NOT WIRE IT (2026-08-03)
>
> **Wiring `StrategyQuantX.exe` to this launcher fork-bombs.** Not "can" —
> does, reproducibly. It is removed from the fleet and must not be re-applied,
> baked into a template, or recommended to anyone until the cause below is
> found, fixed, and proven by a test that *actually launches SQX*.
>
> **What was seen.** Applied fleet-wide on 2026-08-02 to 122 boxes with a real
> v144 engine (gate 4 detection, which is correct). Then:
>
> * **Customer box (vm 998, dual v143+v144).** The customer reported v144
>   "will not open". Evidence from that machine: **90 `wscript.exe` copies
>   spawned in ~33 s and StrategyQuant never started once.** Support removed
>   the IFEO entry to unblock them.
> * **Controlled reproduction (vm 1350, dual v143+v144, SQX idle,
>   2026-08-03).** Launching the v144 engine took the guest's QEMU process to
>   **764 % CPU**, node load 18, and the guest agent stopped answering — from a
>   standing start with nothing else running. Confirmed, cleaned up, hook
>   removed.
> * On the same customer box a **`reg.exe` was found blocked for 24 minutes at
>   0 s CPU**, mid-`reg add`, with the `Debugger` value left **empty**. That is
>   the prime suspect: the guard's `reg delete` / `reg add` run through
>   `shell.Run`, and if the *delete* has not landed before the relaunch, the
>   IFEO re-triggers on itself — which is precisely a fork bomb.
>
> **A single-install box is NOT proof of safety.** A controlled launch test on
> a single-install v144 box (vm 1985, 2026-08-02) passed cleanly — hook fired,
> `Runtime args: -Djava.awt.headless=true` in SQX's own log, no recursion. Both
> reproductions of the bomb were on **dual** v143+v144 installs, but the
> mechanism is not established, so single-install is *unverified*, not safe.
>
> **`sqx_hook_launcher.vbs` (v143 `_nocheck`) is NOT implicated.** It has run
> in production since July, it was left in place on all 119 boxes during the
> rollback, and no fork bomb has ever been traced to it.
>
> **Before this can be reconsidered**, the acceptance test is behavioural, not
> registry-deep: on a **dual-install** box, double-click launch must open SQX,
> `wscript` must stay in single digits, and the `Debugger` value must be back
> in place afterwards. Checking the registry alone would have passed every one
> of these broken boxes.


These VBS launchers are wired via the **Image File Execution Options** `Debugger`
key so they intercept each target `.exe` and relaunch it with the right options:

- `sqx_hook_launcher.vbs` — SQX **v142/v143** engine `StrategyQuantX_nocheck.exe` → injects `JAVA_TOOL_OPTIONS=-Djava.awt.headless=true` for the SQX process only, so SQX never binds to the volatile Remote-Desktop display and survives RDP/network blips (the `awt.dll`/`displayChanged` crash — agent doc §9.9.15b "Mode A").
- `sqx144_hook_launcher.vbs` — SQX **v144** engine `StrategyQuantX.exe`. **🛑 WITHDRAWN 2026-08-03 — fork-bombs, see the banner at the top. Do not wire.** Kept in the repo only so the withdrawal is traceable.
- `mt_hook_launcher.vbs` — MetaTrader `terminal64/metaeditor64/terminal/metaeditor.exe` → ensures `/portable` on non-shortcut launches (e.g. MT's self-update relaunch) so the terminal keeps using its portable data dir. **Portable-data boxes ONLY — see the gate below.**

## Before any sweep: check the per-machine opt-out

A sweep must read `C:\ProgramData\NeuraVPS\hook_optout.txt` and skip the hooks
listed there **before** applying gates 1–4. The gates answer "does this
machine's software shape call for the hook?"; they cannot answer "has a human
already decided this machine must not have it?".

That is not hypothetical: on 2026-08-02 a sweep re-applied the v144 hook to a
box where it had been deliberately removed on 28 July, and the customer wrote
in about the same fault for the second time. See [`optout.md`](optout.md) for
the format and the snippet every sweep should paste.

## ⚠️ Hard gates learned the hard way

The 2026-07-16 fleet sweep applied steps 3+4 below unconditionally and caused
two regressions (gates 1–2). A third gate (gate 3, 2026-07-17) is a scripting
hazard in the install steps themselves. All three are now mandatory:

1. **SUPERSEDED 2026-08-03 — do not wire `StrategyQuantX.exe` at all** (see the
   withdrawal banner at the top: it fork-bombs). Gate 1 as written below was
   about *where* to wire it; the answer is now *nowhere*. Kept for context.
   **`StrategyQuantX.exe` → `sqx144_hook_launcher.vbs` ONLY on boxes with a real
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

2bis. **The MT hook is a backstop for `/portable`, never the only carrier of it**
   (2026-08-09). The shortcut and the hook fail in opposite directions — an MT
   self-update recreates the shortcut without the flag, and the hook can lose
   its own `Debugger` value — so a portable install must have `/portable` in
   **both** places. `install_mt_from_storagebox.ps1` now writes it onto the
   desktop / Start Menu / Startup `.lnk`s, and `BuildForwardArgs` already
   de-duplicates it. It had been hook-only, so one lost registry value flipped
   a box to non-portable and its terminals opened blank.

   The loss is not hypothetical and needs no crash: the launcher used to read
   `Debugger` *before* taking the lock and re-add it only if that read
   succeeded, so a concurrent launcher that had already deleted the value made
   the second one skip the restore. Fixed in `mt_hook_launcher.vbs` — the read
   moved inside the lock and the re-add is unconditional, falling back to
   `CanonicalDebugger()`. Because `terminal64.exe` launches far more often than
   `metaeditor64.exe`, the loss lands **asymmetrically**: the terminal goes
   non-portable while MetaEditor stays forced portable, the two read different
   data folders, MT5 logs `MetaEditor not found`, and EAs pasted in MetaEditor
   never appear in the terminal. Read-only sweep 2026-08-09: **74 of 987**
   reachable MT boxes split that way (63 terminal-loose / 11 editor-loose),
   one of them provisioned three days earlier.

   Fixing a split box: make MetaEditor agree with the terminal, never the
   reverse. Where `terminal64` lost the hook, remove `metaeditor64`'s Debugger
   (safe — the editor holds no accounts, and it takes effect on its next
   launch). Where `metaeditor64` lost it, the terminal is genuinely portable,
   so removing `terminal64`'s Debugger would be gate 2 all over again.

3bis. **Detect a v144+ engine BY CONTENT, never by folder name** (gate 4,
   2026-08-02). The original gate-1 snippet tested
   `$_.Name -match 'SQX.*144|StrategyQuant.*144'`. A fleet sweep of 826 SQX
   boxes found **15** running v143 with an **empty** folder called
   `StrategyQuantX144` / `SQX 144` (0 files, 0 bytes) sitting in `C:\` — left
   behind by an abandoned download. The name matched, so the gate hooked
   `StrategyQuantX.exe`, and those customers' SQX **died silently on
   double-click**: precisely the regression gate 1 exists to prevent, caused
   by the gate itself. The same sweep found the mirror error: real v144
   installs unzipped into `C:\Users\<u>\Downloads\SQX_144_.../`, which the
   `C:\`-only scan never saw, so those boxes silently kept **no** protection.
   The correct test is: a directory containing `StrategyQuantX.exe` and NOT
   containing `StrategyQuantX_nocheck.exe` (>=144 dropped `_nocheck`), searched
   across `C:\` **and** per-user `Downloads\*` / `Desktop\*`. Folder names are
   arbitrary — real installs were found under `SQX_144`, `SQX 144`,
   `StrategyQuantX145`, `SQ_Installer_144`, `SrtategyQuanrX144` (customer
   typo) and even `C:\PerfLogs`.

4. **Never `New-Item -Path <IFEO key> -Force` on a key that already exists** — in
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

   # Detect a real modern engine BY CONTENT, never by folder name (gate 4).
   # >=144 dropped StrategyQuantX_nocheck.exe, so a modern install is a folder
   # holding StrategyQuantX.exe and NOT holding StrategyQuantX_nocheck.exe.
   # Search C:\ plus per-user Downloads/Desktop SUBfolders — customers unzip
   # SQX there and run it in place (seen on live boxes 2026-08-02).
   $roots = New-Object System.Collections.ArrayList
   foreach ($p in @('C:\', 'C:\Users\*\Downloads\*', 'C:\Users\*\Desktop\*')) {
     foreach ($d in (Get-ChildItem $p -Directory -EA SilentlyContinue)) { [void]$roots.Add($d) }
   }
   $has144 = $false
   foreach ($f in $roots) {
     if (Test-Path (Join-Path $f.FullName 'StrategyQuantX.exe')) {
       if (-not (Test-Path (Join-Path $f.FullName 'StrategyQuantX_nocheck.exe'))) { $has144 = $true }
     }
   }
   # 🛑 WITHDRAWN 2026-08-03: the $has144 branch below fork-bombs. Run ONLY the
   # `else` action — remove the Debugger — until the launcher is fixed.
   $k = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\StrategyQuantX.exe'
   if ($false -and $has144) {
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

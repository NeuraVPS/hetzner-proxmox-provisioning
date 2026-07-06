# Pre-Sysprep Cleanup (Windows Server 2025)

Recommended cleanup steps **before** running sysprep on a Windows template VM, to:

- Shrink the final image (less disk, faster clone, faster migration).
- Reclaim space on thin-provisioned storage (ZFS / qcow2 / LVM-thin on the Proxmox host).
- Reduce first-boot work on cloned VMs.

Run all commands in **PowerShell as Administrator**. Order matters — do cleanup first, then defrag, then zero free space, **then** sysprep.

> **Canonical script:** [`presysprep_cleanup.ps1`](presysprep_cleanup.ps1) in this directory runs steps 0–16 unattended and logs to `C:\ProgramData\NeuraVPS\presysprep.log`. The sections below explain each step; the script is the executable source of truth. See [Running it remotely via the guest agent](#running-it-remotely-via-the-guest-agent-qga) for the hands-off procedure used on 2026-07-06.

---

## 0. Pre-flight: no pending reboot

If Windows Update left a reboot pending, `DISM /ResetBase` can fail (`0x800f0806` and friends) or — worse — capture a half-applied servicing state into the template. Check, and reboot first if needed:

```powershell
Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending'
Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired'
# both must be False before proceeding
```

The canonical script aborts (exit 2) if either key exists.

---

## 1. Stop services that hold open files

Lets DISM / Disk Cleanup operate on staged update files without fighting a running scan.

```powershell
Stop-Service -Name wuauserv, bits, cryptsvc, msiserver -Force -ErrorAction SilentlyContinue
```

---

## 2. Clean the Component Store (biggest win)

The component store (`C:\Windows\WinSxS`) keeps superseded payloads from every Windows Update ever applied. On a freshly-patched Server 2025 template this can be **5–15 GB**. `/ResetBase` is the irreversible variant — after it, the currently-installed updates can no longer be uninstalled, which is exactly what you want for a template.

```powershell
Dism.exe /Online /Cleanup-Image /StartComponentCleanup /ResetBase
```

> Takes 5–20 minutes. Expect 100% CPU on one core and heavy disk I/O. Don't interrupt.

Optional follow-up (compresses remaining files in-place, modest extra savings):

```powershell
Dism.exe /Online /Cleanup-Image /SPSuperseded
```

---

## 3. Pre-compile queued .NET assemblies (NGEN)

Windows/winget updates re-queue .NET Framework assemblies for native-image regeneration. If you sysprep with the queue non-empty, **every cloned VM** burns several minutes of background CPU+IO on `mscorsvw.exe` at first boot — multiplied by the whole fleet. Drain the queue once, in the template (both CLR versions are installed here — v2 for the legacy .NET 3.5 that myfxbook needs, v4 for everything else):

```powershell
foreach ($fw in 'Framework64','Framework') {
    foreach ($ver in 'v4.0.30319','v2.0.50727') {
        $ngen = "$env:WINDIR\Microsoft.NET\$fw\$ver\ngen.exe"
        if (Test-Path $ngen) { & $ngen executeQueuedItems /nologo /silent | Out-Null }
    }
}
```

> A few minutes per CLR. The generated native images are *kept* (they're the point) — this step trades template bytes for clone first-boot speed.

---

## 4. Clear staged Windows Update payloads + BITS job database

`SoftwareDistribution\Download` holds downloaded-but-not-yet-cleaned update installers. `catroot2` holds signature catalogs that get regenerated on next WU scan. The BITS `qmgr.db` holds transfer-job state that is meaningless on a clone.

```powershell
Remove-Item -Path 'C:\Windows\SoftwareDistribution\Download\*'      -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -Path 'C:\Windows\SoftwareDistribution\DataStore\*'     -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -Path 'C:\Windows\SoftwareDistribution\ReportingEvents.log' -Force -ErrorAction SilentlyContinue
Remove-Item -Path 'C:\Windows\System32\catroot2\*'                  -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -Path 'C:\ProgramData\Microsoft\Network\Downloader\qmgr*' -Force -ErrorAction SilentlyContinue
```

---

## 5. Clear the Delivery Optimization cache

P2P update cache. Usually 1–3 GB on a recently-updated server.

```powershell
Delete-DeliveryOptimizationCache -Force -ErrorAction SilentlyContinue
# Fallback if cmdlet not available:
Remove-Item 'C:\Windows\ServiceProfiles\NetworkService\AppData\Local\Microsoft\Windows\DeliveryOptimization\Cache\*' -Recurse -Force -ErrorAction SilentlyContinue
```

---

## 6. Delete VSS shadow copies

Update sessions sometimes leave shadow copies behind; each one pins the pre-update blocks (GBs) and is useless on a template.

```powershell
vssadmin.exe delete shadows /all /quiet
```

---

## 7. Remove `Windows.old` (if present)

Only exists if this template was ever upgraded in-place (e.g. 2022 → 2025). Can be 10+ GB.

```powershell
if (Test-Path 'C:\Windows.old') {
    takeown /F C:\Windows.old /R /D Y | Out-Null
    icacls C:\Windows.old /grant administrators:F /T | Out-Null
    Remove-Item -Path 'C:\Windows.old' -Recurse -Force -ErrorAction SilentlyContinue
}
```

---

## 8. Temp folders, error reports, per-user caches

Covers Administrator/Administrador + Default profile. With the autologon session active some files are locked — best-effort (`SilentlyContinue`) is fine.

```powershell
# System temp + prefetch (prefetch rebuilds — fine for a template)
Remove-Item -Path "$env:WINDIR\Temp\*"                       -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -Path "$env:WINDIR\Prefetch\*"                   -Recurse -Force -ErrorAction SilentlyContinue

# System-wide Windows Error Reporting
Remove-Item -Path 'C:\ProgramData\Microsoft\Windows\WER\*'   -Recurse -Force -ErrorAction SilentlyContinue

# Per-user: temp, browser cache, WER, thumbnails, PowerShell traces, crash dumps
Get-ChildItem 'C:\Users' -Directory | ForEach-Object {
    $u = $_.FullName
    Remove-Item "$u\AppData\Local\Temp\*"                                     -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item "$u\AppData\Local\Microsoft\Windows\INetCache\*"              -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item "$u\AppData\Local\Microsoft\Windows\WER\*"                    -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item "$u\AppData\Local\Microsoft\Windows\Explorer\thumbcache_*.db" -Force -ErrorAction SilentlyContinue
    Remove-Item "$u\AppData\Local\Microsoft\Windows\PowerShell\ModuleAnalysisCache" -Force -ErrorAction SilentlyContinue
    Remove-Item "$u\AppData\Roaming\Microsoft\Windows\PowerShell\PSReadLine\ConsoleHost_history.txt" -Force -ErrorAction SilentlyContinue
    Remove-Item "$u\AppData\Local\CrashDumps\*"                               -Recurse -Force -ErrorAction SilentlyContinue
}
```

> `ConsoleHost_history.txt` is the PSReadLine command history — without this, every command typed during template prep ships to every customer clone.

---

## 9. Servicing/update logs + memory dumps

After a big update session `C:\Windows\Logs\CBS` alone can reach several hundred MB.

```powershell
foreach ($d in 'CBS','DISM','WindowsUpdate','MoSetup','NetSetup') {
    Remove-Item "C:\Windows\Logs\$d\*" -Recurse -Force -ErrorAction SilentlyContinue
}
Remove-Item 'C:\Windows\MEMORY.DMP'          -Force          -ErrorAction SilentlyContinue
Remove-Item 'C:\Windows\Minidump\*'          -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item 'C:\Windows\LiveKernelReports\*' -Recurse -Force -ErrorAction SilentlyContinue
```

> Leave `C:\Windows\Panther` alone — sysprep reads/writes there.

---

## 10. winget caches

Now that template app updates go through winget, its downloaded-installer cache is dead weight (winget itself gets removed before sysprep anyway — see [README](README.md)).

```powershell
Get-ChildItem 'C:\Users\*\AppData\Local\Packages\Microsoft.DesktopAppInstaller_*' -Directory | ForEach-Object {
    Remove-Item "$($_.FullName)\LocalCache\*"               -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item "$($_.FullName)\LocalState\DiagOutputDir\*" -Recurse -Force -ErrorAction SilentlyContinue
}
Remove-Item 'C:\Windows\Temp\WinGet\*' -Recurse -Force -ErrorAction SilentlyContinue
```

---

## 11. Empty the Recycle Bin

```powershell
Clear-RecycleBin -Force -ErrorAction SilentlyContinue
```

---

## 12. Disable + delete hibernation file (`hiberfil.sys`)

Server SKUs usually don't have it, but if it exists it's `RAM-size` GB of dead weight.

```powershell
powercfg.exe /hibernate off
```

---

## 13. Clear Event Logs (late on purpose)

Removes traces from template prep that would otherwise appear in every cloned VM's log history. Run this **after** all the steps above so the cleanup's own noise (service stops, DISM, VSS…) is wiped too.

```powershell
Get-WinEvent -ListLog * -ErrorAction SilentlyContinue |
    Where-Object { $_.RecordCount -gt 0 -and $_.IsEnabled } |
    ForEach-Object { wevtutil.exe cl $_.LogName 2>$null }
```

---

> **Pagefile shrink — intentionally omitted.** Earlier revisions of this doc had a "shrink/disable pagefile" step here. Measured on lz4-compressed ZFS (`local-zfs` / Proxmox default) with a fresh, never-stressed template, it produced **0 GB savings** because an unused `pagefile.sys` contains only zeros and lz4 already compresses it to ~nothing. If your template was heavily used during build (real data paged out) or your storage backend doesn't compress, the trade-off may be worth re-evaluating — but for the default case, leaving the OS-default automatic pagefile is correct, and `create_vm` / `reset_vm` need no special pagefile handling on clones.

> **`compact.exe /CompactOS` — considered and rejected.** NTFS-compressing the OS binaries saves 2–4 GB in-guest, but our zvols already sit on ZFS lz4 (host-side compression overlaps most of the gain) and it adds a small permanent CPU tax on every binary read in every customer VM. Not worth it for this fleet.

## 14. Built-in Disk Cleanup (`cleanmgr`) — interactive sessions only

Catches a few leftovers the manual steps miss (font cache, icon cache, setup log files). **Do not run it in unattended/QGA runs**: it is a GUI app — under the guest agent it runs as SYSTEM in session 0, where it can hang forever waiting on a window nobody can see. The heavyweight handler ("Update Cleanup") is already covered by step 2, and steps 8–9 cover most of the rest, so skipping it costs a few hundred MB at most.

If you *are* at an interactive console, either use the classic saved profile:

```powershell
cleanmgr.exe /sageset:1   # one-time: tick every box, OK
cleanmgr.exe /sagerun:1   # every future refresh
```

…or skip the interactive `/sageset` entirely by arming the handlers in the registry:

```powershell
# Enable every handler for profile 64, EXCEPT DownloadsFolder (would delete the admin's Downloads!)
Get-ChildItem 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\VolumeCaches' |
    Where-Object { $_.PSChildName -ne 'DownloadsFolder' } |
    ForEach-Object { Set-ItemProperty -Path $_.PSPath -Name 'StateFlags0064' -Value 2 -Type DWord }
cleanmgr.exe /sagerun:64
```

---

## 15. Defragment and TRIM the C: volume

On SSD-backed storage this issues TRIM, which lets the underlying storage actually free the blocks you just deleted. Our template VMs have `discard=on` + `virtio-scsi-single`, so in-guest TRIM propagates straight to the ZFS zvol.

```powershell
Optimize-Volume -DriveLetter C -Defrag -Verbose
Optimize-Volume -DriveLetter C -ReTrim -Verbose
```

---

## 16. Zero out free space (critical for thin provisioning)

This is the step that makes the previous cleanup **actually shrink the image on the Proxmox host**. Without it, the deleted files still occupy blocks from the host's perspective — ZFS/qcow2 only reclaim space that's been explicitly zeroed (or TRIM'd, but in-guest TRIM doesn't always propagate to the host depending on the SCSI controller / discard setting). With ZFS compression enabled, zero blocks are detected at write time and stored as holes, so this is cheap on the host even though the guest writes gigabytes.

Install Sysinternals **SDelete** once on the template (or copy `sdelete64.exe` into `C:\Windows\System32` — **already installed on windows-es/windows-en** since the 2026-06 refresh):

```powershell
# Download SDelete — use C:\Windows\Temp (always present) instead of $env:TEMP,
# which on Server 2025 points to a per-session subfolder (...\Temp\2) that the
# Step 8 user-temp cleanup wipes out.
$tmp = "$env:WINDIR\Temp\SDelete.zip"
Invoke-WebRequest -Uri 'https://download.sysinternals.com/files/SDelete.zip' -OutFile $tmp
Expand-Archive -Path $tmp -DestinationPath 'C:\Windows\System32' -Force
Remove-Item $tmp

# Override TEMP/TMP before running sdelete — it calls GetTempPath() internally
# to create its scratch file and will fail with "system cannot find the path
# specified" if the session's per-session subfolder was wiped in step 8.
$env:TEMP = 'C:\Windows\Temp'
$env:TMP  = 'C:\Windows\Temp'

# Accept EULA non-interactively (first run)
sdelete64.exe -accepteula -nobanner -z C:

# Final ReTrim: hand the zeroed range back to the host in one pass
Optimize-Volume -DriveLetter C -ReTrim
```

> `-z` writes zeros to free space (good for thin provisioning).
> `-c` ("clean") writes random data instead — only needed if you care about cryptographic erasure of deleted files, which a template generally doesn't.
>
> Expect this to take a while and to **temporarily fill C: to 100%** as it writes the zero file, then delete it. That's normal.

After this completes, on the Proxmox host the disk image will compact dramatically on the next ZFS snapshot / qcow2 conversion / migration.

---

## 17. Final pre-sysprep check

```powershell
# Confirm WU service is set to Manual (per README — manual updates still work on clones)
Get-Service wuauserv | Select-Object Name, Status, StartType

# Confirm policy values
Get-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU' |
    Select-Object NoAutoUpdate, AUOptions

# Free space sanity check
Get-Volume -DriveLetter C | Select-Object DriveLetter, FileSystemLabel, SizeRemaining, Size

# No appx packages installed-for-a-user-but-not-provisioned (classic sysprep blocker).
# EXEMPT Microsoft.DesktopAppInstaller: after the operator's user-level winget uninstall
# the OS-serviced stub stays registered (NonRemovable) — that state syspreps fine and
# must be left alone (see README, 2026-07-06 note).
Get-AppxPackage -AllUsers |
    Where-Object { $_.PackageUserInformation.InstallState -contains 'Installed' -and -not $_.IsFramework -and
                   $_.Name -ne 'Microsoft.DesktopAppInstaller' } |
    Select-Object Name, PackageFullName
```

Then proceed with sysprep as documented in [README.md](README.md):

```powershell
cd C:\Windows\System32\Sysprep
.\sysprep.exe /generalize /oobe /shutdown /unattend:C:\ProgramData\NeuraVPS\unattend.xml
```

---

## Running it remotely via the guest agent (QGA)

Used on 2026-07-06 to clean both templates hands-off from the BASE (no console needed). Preconditions: `agent: 1` in the VM config and the templates running on some node (`p55` at the time).

```bash
# 0) Safety snapshot on the node (raw zfs — invisible to the Proxmox config, so it
#    does NOT leak into the config.conf that export_template_vm_to_shared_storage.sh captures)
for d in 0 1 2; do zfs snapshot rpool/data/vm-100-disk-$d@precleanup-$(date +%Y%m%d); done
# destroy after the new template is validated/exported — while it exists, the freed
# space stays referenced (REFER drops, USED doesn't)

# 1) Push the script (base64 -> WriteAllBytes; verify with Get-FileHash afterwards)
PAYLOAD=$(base64 -w0 < presysprep_cleanup.ps1)
WRITER="[IO.File]::WriteAllBytes('C:\\ProgramData\\NeuraVPS\\presysprep_cleanup.ps1',[Convert]::FromBase64String('$PAYLOAD'))"
B64=$(printf '%s' "$WRITER" | iconv -f utf-8 -t utf-16le | base64 -w0)
qm guest exec 100 --timeout 60 -- powershell -NoProfile -EncodedCommand "$B64"

# 2) Launch detached (qm guest exec must NOT wait 30+ min on the whole run)
LAUNCH="Start-Process powershell.exe -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','C:\\ProgramData\\NeuraVPS\\presysprep_cleanup.ps1' -WindowStyle Hidden"
B64=$(printf '%s' "$LAUNCH" | iconv -f utf-8 -t utf-16le | base64 -w0)
qm guest exec 100 --timeout 45 -- powershell -NoProfile -EncodedCommand "$B64"

# 3) Poll the log until it shows "CLEANUP COMPLETE rc=0" (or "ABORT")
TAIL="Get-Content 'C:\\ProgramData\\NeuraVPS\\presysprep.log' -Tail 6"
B64=$(printf '%s' "$TAIL" | iconv -f utf-8 -t utf-16le | base64 -w0)
qm guest exec 100 --timeout 40 -- powershell -NoProfile -EncodedCommand "$B64"
```

Notes:

- QGA exec runs as SYSTEM in session 0 — everything in the script works there **except GUI apps** (hence the `cleanmgr` rule in step 14).
- Both templates can run in parallel; DISM pegs one core each.
- The `out-data` JSON from `qm guest exec` can contain raw control characters (CRLF inside strings) — parse with `json.loads(s, strict=False)` in Python.
- Transient `guest-exec` errors ("PID … does not exist") happen — just re-poll (see provisioning PR #45/#46 background).

---

## Expected savings

Measured 2026-07-06 on the windows-es / windows-en templates (Server 2025, 40 G volsize, ZFS lz4, `discard=on`), right after a Windows Update + winget app refresh. Full unattended run took **~7 min per VM** (both in parallel on p55); DISM `/ResetBase` was 4.5 min of that, sdelete ~1.5 min.

| Metric | windows-es (100) | windows-en (101) |
|--------|------------------|------------------|
| In-guest free before → after | 20.0 → 23.0 GB | 20.3 → 23.7 GB |
| Host zvol `REFER` before → after | 14.4 → **11.4 G** | 14.2 → **11.2 G** |
| Host shrink | **−3.0 G (−21%)** | **−3.0 G (−21%)** |

The in-guest free space change is misleading — what actually matters is the post-zero shrink of the zvol's `REFER` on the Proxmox host (steps 15–16), which is what the storagebox export stream and every future clone/migration pay for. A template that skipped several update cycles will see bigger DISM wins (5–15 GB); this one was refreshed monthly.

> While a pre-cleanup safety snapshot exists, `REFER` drops but `USED` doesn't — the snapshot pins the old blocks. Destroy it once the new template generation is validated/exported.

# Pre-Sysprep Cleanup (Windows Server 2025)

Recommended cleanup steps **before** running sysprep on a Windows template VM, to:

- Shrink the final image (less disk, faster clone, faster migration).
- Reclaim space on thin-provisioned storage (ZFS / qcow2 / LVM-thin on the Proxmox host).
- Reduce first-boot work on cloned VMs.

Run all commands in **PowerShell as Administrator**. Order matters — do cleanup first, then defrag, then zero free space, **then** sysprep.

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

## 3. Clear staged Windows Update payloads

`SoftwareDistribution\Download` holds downloaded-but-not-yet-cleaned update installers. `catroot2` holds signature catalogs that get regenerated on next WU scan.

```powershell
Remove-Item -Path 'C:\Windows\SoftwareDistribution\Download\*' -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -Path 'C:\Windows\SoftwareDistribution\DataStore\*' -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -Path 'C:\Windows\System32\catroot2\*'              -Recurse -Force -ErrorAction SilentlyContinue
```

---

## 4. Clear the Delivery Optimization cache

P2P update cache. Usually 1–3 GB on a recently-updated server.

```powershell
Delete-DeliveryOptimizationCache -Force -ErrorAction SilentlyContinue
# Fallback if cmdlet not available:
Remove-Item 'C:\Windows\ServiceProfiles\NetworkService\AppData\Local\Microsoft\Windows\DeliveryOptimization\Cache\*' -Recurse -Force -ErrorAction SilentlyContinue
```

---

## 5. Remove `Windows.old` (if present)

Only exists if this template was ever upgraded in-place (e.g. 2022 → 2025). Can be 10+ GB.

```powershell
if (Test-Path 'C:\Windows.old') {
    takeown /F C:\Windows.old /R /D Y | Out-Null
    icacls C:\Windows.old /grant administrators:F /T | Out-Null
    Remove-Item -Path 'C:\Windows.old' -Recurse -Force -ErrorAction SilentlyContinue
}
```

---

## 6. Temp folders, error reports, thumbnails

```powershell
# System temp
Remove-Item -Path "$env:WINDIR\Temp\*"                       -Recurse -Force -ErrorAction SilentlyContinue

# All user temps (covers Administrator + Default profile)
Get-ChildItem 'C:\Users' -Directory | ForEach-Object {
    Remove-Item -Path "$($_.FullName)\AppData\Local\Temp\*"  -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -Path "$($_.FullName)\AppData\Local\Microsoft\Windows\INetCache\*" -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -Path "$($_.FullName)\AppData\Local\Microsoft\Windows\WER\*"       -Recurse -Force -ErrorAction SilentlyContinue
}

# System-wide Windows Error Reporting
Remove-Item -Path 'C:\ProgramData\Microsoft\Windows\WER\*'   -Recurse -Force -ErrorAction SilentlyContinue

# Prefetch (will rebuild — fine for a template)
Remove-Item -Path "$env:WINDIR\Prefetch\*"                   -Recurse -Force -ErrorAction SilentlyContinue
```

---

## 7. Empty the Recycle Bin

```powershell
Clear-RecycleBin -Force -ErrorAction SilentlyContinue
```

---

## 8. Clear Event Logs

Cosmetic but removes traces from template prep that would otherwise appear in every cloned VM's log history.

```powershell
Get-WinEvent -ListLog * -ErrorAction SilentlyContinue |
    Where-Object { $_.RecordCount -gt 0 -and $_.IsEnabled } |
    ForEach-Object { wevtutil.exe cl $_.LogName 2>$null }
```

---

## 9. Disable + delete hibernation file (`hiberfil.sys`)

Server SKUs usually don't have it, but if it exists it's `RAM-size` GB of dead weight.

```powershell
powercfg.exe /hibernate off
```

---

> **Pagefile shrink — intentionally omitted.** Earlier revisions of this doc had a "shrink/disable pagefile" step here. Measured on lz4-compressed ZFS (`local-zfs` / Proxmox default) with a fresh, never-stressed template, it produced **0 GB savings** because an unused `pagefile.sys` contains only zeros and lz4 already compresses it to ~nothing. If your template was heavily used during build (real data paged out) or your storage backend doesn't compress, the trade-off may be worth re-evaluating — but for the default case, leaving the OS-default automatic pagefile is correct, and `create_vm` / `reset_vm` need no special pagefile handling on clones.

## 10. Run the built-in Disk Cleanup with a saved profile

Catches things the manual steps above miss (icon cache, font cache, old service pack files, etc.). On Server 2025 Standard with Desktop Experience, `cleanmgr.exe` is available by default.

One-time setup of the cleanup profile (interactive — tick every box, then OK):

```powershell
cleanmgr.exe /sageset:1
```

Then run it (and on every future template refresh):

```powershell
cleanmgr.exe /sagerun:1
```

> Add `/VERYLOWDISK` to suppress the progress UI if running unattended.

---

## 11. Defragment and TRIM the C: volume

On SSD-backed storage this issues TRIM, which lets the underlying storage actually free the blocks you just deleted.

```powershell
Optimize-Volume -DriveLetter C -ReTrim -Defrag -Verbose
```

---

## 12. Zero out free space (critical for thin provisioning)

This is the step that makes the previous cleanup **actually shrink the image on the Proxmox host**. Without it, the deleted files still occupy blocks from the host's perspective — ZFS/qcow2 only reclaim space that's been explicitly zeroed (or TRIM'd, but in-guest TRIM doesn't always propagate to the host depending on the SCSI controller / discard setting).

Install Sysinternals **SDelete** once on the template (or copy `sdelete64.exe` into `C:\Windows\System32`):

```powershell
# Download SDelete — use C:\Windows\Temp (always present) instead of $env:TEMP,
# which on Server 2025 points to a per-session subfolder (...\Temp\2) that the
# Step 6 user-temp cleanup wipes out.
$tmp = "$env:WINDIR\Temp\SDelete.zip"
Invoke-WebRequest -Uri 'https://download.sysinternals.com/files/SDelete.zip' -OutFile $tmp
Expand-Archive -Path $tmp -DestinationPath 'C:\Windows\System32' -Force
Remove-Item $tmp

# Override TEMP/TMP before running sdelete — it calls GetTempPath() internally
# to create its scratch file and will fail with "system cannot find the path
# specified" if the session's per-session subfolder was wiped in step 6.
$env:TEMP = 'C:\Windows\Temp'
$env:TMP  = 'C:\Windows\Temp'

# Accept EULA non-interactively (first run)
sdelete64.exe -accepteula -nobanner -z C:
```

> `-z` writes zeros to free space (good for thin provisioning).
> `-c` ("clean") writes random data instead — only needed if you care about cryptographic erasure of deleted files, which a template generally doesn't.
>
> Expect this to take a while and to **temporarily fill C: to 100%** as it writes the zero file, then delete it. That's normal.

After this completes, on the Proxmox host the disk image will compact dramatically on the next ZFS snapshot / qcow2 conversion / migration.

---

## 13. Final pre-sysprep check

```powershell
# Confirm WU service is set to Manual (per README — manual updates still work on clones)
Get-Service wuauserv | Select-Object Name, Status, StartType

# Confirm policy values
Get-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU' |
    Select-Object NoAutoUpdate, AUOptions

# Free space sanity check
Get-Volume -DriveLetter C | Select-Object DriveLetter, FileSystemLabel, SizeRemaining, Size
```

Then proceed with sysprep as documented in [README.md](README.md):

```powershell
cd C:\Windows\System32\Sysprep
.\sysprep.exe /generalize /oobe /shutdown /unattend:C:\ProgramData\NeuraVPS\unattend.xml
```

---

## All-in-one script

For convenience — runs steps 1–9 + 11 + 12 + 13 in sequence. **Read it first**, then save as `C:\ProgramData\NeuraVPS\presysprep_cleanup.ps1` and run as Administrator.

```powershell
Write-Host '=== 1/10: Stopping update services ===' -ForegroundColor Cyan
Stop-Service -Name wuauserv, bits, cryptsvc, msiserver -Force -ErrorAction SilentlyContinue

Write-Host '=== 2/10: DISM component store cleanup (slow) ===' -ForegroundColor Cyan
Dism.exe /Online /Cleanup-Image /StartComponentCleanup /ResetBase

Write-Host '=== 3/10: Clearing SoftwareDistribution + catroot2 ===' -ForegroundColor Cyan
Remove-Item 'C:\Windows\SoftwareDistribution\Download\*'  -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item 'C:\Windows\SoftwareDistribution\DataStore\*' -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item 'C:\Windows\System32\catroot2\*'              -Recurse -Force -ErrorAction SilentlyContinue

Write-Host '=== 4/10: Delivery Optimization cache ===' -ForegroundColor Cyan
try { Delete-DeliveryOptimizationCache -Force -ErrorAction Stop } catch {
    Remove-Item 'C:\Windows\ServiceProfiles\NetworkService\AppData\Local\Microsoft\Windows\DeliveryOptimization\Cache\*' -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host '=== 5/10: Temp + WER + Prefetch ===' -ForegroundColor Cyan
Remove-Item "$env:WINDIR\Temp\*"     -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item "$env:WINDIR\Prefetch\*" -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item 'C:\ProgramData\Microsoft\Windows\WER\*' -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem 'C:\Users' -Directory | ForEach-Object {
    Remove-Item "$($_.FullName)\AppData\Local\Temp\*"                                  -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item "$($_.FullName)\AppData\Local\Microsoft\Windows\INetCache\*"           -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item "$($_.FullName)\AppData\Local\Microsoft\Windows\WER\*"                 -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host '=== 6/10: Recycle Bin ===' -ForegroundColor Cyan
Clear-RecycleBin -Force -ErrorAction SilentlyContinue

Write-Host '=== 7/10: Event logs ===' -ForegroundColor Cyan
Get-WinEvent -ListLog * -ErrorAction SilentlyContinue |
    Where-Object { $_.RecordCount -gt 0 -and $_.IsEnabled } |
    ForEach-Object { wevtutil.exe cl $_.LogName 2>$null }

Write-Host '=== 8/10: Disable hibernation ===' -ForegroundColor Cyan
powercfg.exe /hibernate off

Write-Host '=== 9/10: cleanmgr /sagerun:1 (requires /sageset:1 done once) ===' -ForegroundColor Cyan
Start-Process -FilePath 'cleanmgr.exe' -ArgumentList '/sagerun:1' -Wait

Write-Host '=== 10a/10: Defrag + TRIM ===' -ForegroundColor Cyan
Optimize-Volume -DriveLetter C -ReTrim -Defrag

Write-Host '=== 10b/10: Zero free space (sdelete) — will take a while ===' -ForegroundColor Cyan
if (-not (Get-Command sdelete64.exe -ErrorAction SilentlyContinue)) {
    Write-Host '   sdelete64.exe not found — downloading from Sysinternals' -ForegroundColor Yellow
    # Use C:\Windows\Temp — $env:TEMP on Server 2025 is a per-session subfolder
    # that the step 5 user-temp cleanup just wiped out.
    $tmp = "$env:WINDIR\Temp\SDelete.zip"
    Invoke-WebRequest -Uri 'https://download.sysinternals.com/files/SDelete.zip' -OutFile $tmp
    Expand-Archive -Path $tmp -DestinationPath 'C:\Windows\System32' -Force
    Remove-Item $tmp
}
# sdelete calls GetTempPath() internally for its scratch file; redirect TEMP/TMP
# to C:\Windows\Temp before invoking so it doesn't try to write to the wiped
# per-session folder (step 5 cleanup removed ...\AppData\Local\Temp\<sessionId>).
$env:TEMP = 'C:\Windows\Temp'
$env:TMP  = 'C:\Windows\Temp'
sdelete64.exe -accepteula -nobanner -z C:

Write-Host '=== Done. Ready for sysprep. ===' -ForegroundColor Green
Get-Volume -DriveLetter C | Format-Table DriveLetter, FileSystemLabel, SizeRemaining, Size
```

---

## Expected savings

On a freshly-patched Server 2025 Standard template (~20 GB used pre-cleanup), typical results:

| Step | Reclaimed (in-guest) | Reclaimed on host (post-zero) |
|------|----------------------|-------------------------------|
| DISM `/ResetBase` | 3–8 GB | included below |
| SoftwareDistribution + DO cache | 1–4 GB | included below |
| Temp + WER + Prefetch | 0.1–1 GB | included below |
| `cleanmgr /sagerun:1` | 0.5–2 GB | included below |
| **SDelete `-z` (compaction trigger)** | 0 GB | **5–15 GB** off the qcow2/ZFS volume |

The in-guest free space change is misleading — what actually matters is the post-zero shrink on the Proxmox host, which is the whole point of step 12.

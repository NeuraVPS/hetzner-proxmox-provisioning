# Pre-Sysprep cleanup for Windows Server 2025

Use [presysprep_cleanup.ps1](presysprep_cleanup.ps1) as the executable procedure.
It runs elevated, including through QGA, but does not launch Sysprep or change
`C:\ProgramData\NeuraVPS\unattend.xml`. Run it only on template VMs. Preserve the
operator's snapshots and record free space, package state and host ZFS `REFER`
before and after. Snapshot retention keeps old blocks allocated: a smaller
`REFER` does not mean the pool's total `USED` must immediately fall.

## Step-by-step decision table

The numbering matches the current script. Optional operations require evidence,
not just the fact that a previous checklist mentioned them.

| Step | Default and reason |
|---|---|
| Preflight | Abort on pending CBS/WU reboot; record free space and XML hash. |
| 1. Services | Stop Windows Update and BITS while cleaning closed files; restore BITS if it was running. Leave cryptsvc/msiserver alone. |
| 2. Component store | Skip DISM `/ResetBase`; the September templates already ran it. `-RunResetBase` is irreversible for update uninstallation and needs a fresh reason. |
| 3. NGEN | Skip; use `-RunNgen` only after measuring a queued assembly workload. Generated assemblies can make the image larger. |
| 4. Update downloads | Remove closed download-cache contents; preserve DataStore, catroot2 and BITS job state. |
| 5. Delivery Optimization | Clear the cache if present; no benefit when empty. |
| 6. VSS | Remove guest shadow copies only if present on a template; this does not delete Proxmox/ZFS snapshots. |
| 7. Temporary data | Remove closed temp files, WER/crash caches, thumbnails and PowerShell history from user profiles. Locked files are left alone. |
| 8. Logs/dumps | Remove closed servicing logs and dumps; preserve Panther and Sysprep evidence. Save any needed logs outside the image first. |
| 9. Winget cache | Remove download/diagnostic caches only. Do not uninstall AppX packages here. |
| 10. SSH identity | Stop sshd, delete all `ssh_host_*` files and template `administrators_authorized_keys`, then verify absence. Keep sshd Automatic but stopped until Sysprep. |
| 11. Recycle Bin | Empty it; measure rather than assume a saving. |
| 12. Hibernation | Disable only if `hiberfil.sys` exists. |
| 13. Event Logs | Preserve by default. `-ClearEventLogs` is only for an explicit decision after saving evidence. |
| 14. Storage reclaim | ReTrim C:. `-RunDefrag` requires a measured reason and is not routine SSD cleanup. |
| 15. Zero-fill | Skip SDelete. `-RunSDelete` is a fallback only if TRIM fails to reclaim space; verify the binary first and allow for temporary full-disk use. |
| 16. Winlogon secret | Remove only `HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon\DefaultPassword` when present, verify it is absent, and fail closed if removal does not stick. `DefaultUserName`, `DefaultDomainName`, the local SID500 account/password, XML, policies and LSA are untouched. |
| Final check | ReTrim again, record free space/policy, verify the XML hash stayed identical and require `CLEANUP COMPLETE rc=0`. |

Do not repeat recovery-partition removal, install hooks, remove Feedback Hub,
remove system AppX packages, reset servicing databases, shrink the pagefile,
or enable CompactOS as part of this cleanup. The app installers own MT/SQX
hooks, JVM settings and application priority. Provisioning owns per-plan
pagefile policy and per-VM credentials/network settings.

`cleanmgr` is not needed for this automated path. It can wait forever for a GUI
under SYSTEM/session 0. Old examples using `/SPSuperseded`, arbitrary AppX
lists or unconditional SDelete are not the current procedure.

## User packages and Sysprep

Keep the OS-serviced DesktopAppInstaller stub and Feedback Hub. A fully
installed user winget/source package can block Sysprep; identify the exact
package from Sysprep's logs and use the supported uninstall operation in that
user's context. Do not force-remove the non-removable stub. Listing AppX
packages alone is not a valid blocker detector: bundle and payload names can
differ, and a package present for a user is not by itself a failure.

Keep the operator's existing answer file unchanged. `CopyProfile` belongs only
to the earlier explicit profile-preparation phase; the final September sealing
phase uses the already supplied answer file without it.

Launch this command in the elevated interactive built-in Administrator session:

```powershell
cd C:\Windows\System32\Sysprep
.\sysprep.exe /generalize /oobe /shutdown /unattend:C:\ProgramData\NeuraVPS\unattend.xml
```

For a history-free remote launch, QGA may register a one-off scheduled task with
`Interactive` logon and `Highest` privileges for the logged-in SID ending in
`-500`. The task must verify its identity/session/elevation, unregister itself
before starting Sysprep, and invoke the exact command above. QGA must never
start Sysprep directly as SYSTEM. Check the resulting shutdown and do not boot
the sealed source again before export. Remove any operator helper files before
sealing; retain receipts outside the image.

## Acceptance and publication

Export to a unique staging key, then boot isolated disposable clones. Require
real RDP desktop login and reconnect, working Explorer/Start/UI, correct AppX
CBS/Core/XAML and Feedback Hub, new SSH fingerprints, authenticated SSH/SMB,
IPv4/IPv6 connectivity, power-plan checks, app installs, and a reboot. A healthy
QGA or open TCP port does not prove a working desktop.

Test reinstall with resources that differ from the plan and a separate data
disk. RAM, balloon, CPU count/topology and disk sizes/data must survive while
virtual hardware versions and CPU model update. Follow the publication order
and rollback procedure in [README.md](README.md); preserve source snapshots.

## September 20 measurements

The operator had already cleaned the component store. Download/DO caches, VSS
and hibernation were absent or empty. Safe cleanup reclaimed about 72–79 MiB
of ZFS referenced data; repeating DISM or zero-filling was not justified.
The sealed staging streams occupied about 10.6 GB (Spanish) and 10.2 GB
(English), with 32 GiB source disks. Report these separately from in-guest free
space. They are measurements for this release, not a promised future saving.

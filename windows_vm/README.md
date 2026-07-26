# Prepare Windows Template

## VM config (Proxmox side, not the guest)

- **`cpu: x86-64-v3` — never `host`.** The template's `config.conf` is cloned verbatim into every new customer VM, so a host-passthrough template pins each guest to the exact silicon of the node it was born on: live migration then only works onto a same-or-newer CPU of the *same vendor* (the VMs 708/1670 failure, see the pre-check in [`scripts/migrate_vm.sh`](../scripts/migrate_vm.sh)). A baseline model lets any new VM hot-migrate anywhere in the fleet, including across CPU types and vendors.
  - `x86-64-v3` is the highest generic model the whole fleet can run: PVE expands it to `qemu64,+aes,+avx,+avx2,+bmi1,+bmi2,+f16c,+fma,+abm,+movbe,+xsave,…,enforce`, and every current CPU (EPYC Genoa, Ryzen 7950X3D/5950X, Intel i5-13500) supports all of it. `x86-64-v4` additionally needs AVX-512, which the Intel EX44 nodes do **not** have — and because PVE appends `enforce`, a v4 VM would simply fail to start there.
  - Trade-off accepted 2026-07-26: the guest's Task Manager shows a generic *"QEMU Virtual CPU version 2.5+"* instead of the real model name, and AVX-512 is hidden on Genoa.
  - `export_template_vm_to_shared_storage.sh` refuses to upload a template with `cpu: host`/`max` (override: `ALLOW_HOST_CPU=1`), so a future refresh cannot silently revert this.
  - Existing customer VMs keep `cpu: host` — this only applies to VMs created from the templates from now on.

## Checklist

- Apply Windows and Winget updates
- Uninstall unneeded apps and software

```powershell
# Disable WindowsFeedbackHub installation for new users
Get-AppxProvisionedPackage -Online | Where-Object DisplayName -like "Microsoft.WindowsFeedbackHub" | Remove-AppxProvisionedPackage -Online
```

- Install .NET framework legacy for myfxbook installed to work
- Remove winget for sysprep to work — **user-level uninstall, done interactively by the operator**:

```powershell
Get-AppxPackage *winget* | Remove-AppxPackage
```

  > **Server 2025 (OS-serviced winget, clarified 2026-07-06):** the command above (run interactively as the admin user) removes the *full* app but Windows keeps the **stub** (`…_neutral_~_…` bundle, `NonRemovable=True`) registered for the user. That stub state is the **desired final state**: it syspreps fine (stubs are exempt from the appx installed-but-not-provisioned validation) and it keeps the on-demand mechanism — on a clone, just typing `winget` in PowerShell re-downloads the full app.
  >
  > **Do NOT try to remove the stub itself.** `Remove-AppxPackage` on it (any variant: `-AllUsers`, `-User <sid>`, from SYSTEM/QGA) fails `0x80073CFA` / DeStage `0x80070032`, and neither `Dism /Set-NonRemovableAppPolicy` nor `Set-NonRemovableAppsPolicy` nor a reboot changes that — it is not policy, it is the stub design. Forcing it (e.g. `EndOfLife` registry keys per SID make the engine accept the removal) *works* but destroys the winget-on-demand UX for every clone and the only clean way back is a snapshot restore — this was done by mistake on 2026-07-06 and both templates had to be rolled back to their `@precleanup` ZFS snapshots. Also note: a false positive in a naive appx sysprep-blocker check (installed-for-user + not-provisioned) — whitelist `Microsoft.DesktopAppInstaller` when the only registration left is the stub.

- Disable Password lock Policy

```powershell
# Don't lock accounts on failed login attempts
net accounts /lockoutthreshold:0

# Don't require password changes
net accounts /maxpwage:UNLIMITED
```

- Apply SQX and MT5 hooks from `hooks`
- SQX in high priority mode

```powershell
# SQX in High priority
$basePath = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\StrategyQuantX_nocheck.exe\PerfOptions'
New-Item -Path $basePath -Force | Out-Null
New-ItemProperty -Path $basePath -Name 'CpuPriorityClass' -PropertyType DWord -Value 6 -Force | Out-Null
```

- NTP servers for Hetzner
- Permitir Samba en el firewall de Windows

```powershell
# Allow SAMBA through Windows Firewall
Set-NetFirewallRule -DisplayName 'Uso compartido de archivos e impresoras (restrictivo) (SMB de entrada)' -Enabled True
```

- Crear carpeta C:\Mis Servidores y enlace en el escritorio

```powershell
# Create Mis Servidores folder and Desktop symlink
$targetFolder = 'C:\My Servers';
$publicDesktop = 'C:\Users\Public\Desktop';
$linkPath = "$publicDesktop\My Servers";

if (!(Test-Path $targetFolder)) { New-Item -ItemType Directory -Path $targetFolder -Force | Out-Null }

if (Test-Path $linkPath) {
    $item = Get-Item $linkPath -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { exit }
    Remove-Item $linkPath -Recurse -Force
}

New-Item -ItemType SymbolicLink -Path $linkPath -Target $targetFolder -Force | Out-Null

# desktop.ini
[.ShellClassInfo]
LocalizedResourceName=Mis Servidores

attrib +s 'C:\My Servers'
attrib +h 'C:\My Servers\desktop.ini'
```

- Crear C:\NeuraData

- Prepare for Autologin

```powershell
# Autologin
$RegPath = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"
Set-ItemProperty -Path $RegPath -Name "AutoAdminLogon" -Value "1" -Type String
Set-ItemProperty -Path $RegPath -Name "DefaultUserName" -Value "Administrador" -Type String
#Set-ItemProperty -Path $RegPath -Name "DefaultPassword" -Value "<new password>" -Type String
```

- Disk cleanup — run [presysprep_cleanup.ps1](presysprep_cleanup.ps1) (unattended, can be pushed+launched via `qm guest exec`; logs to `C:\ProgramData\NeuraVPS\presysprep.log`). [prepare.md](prepare.md) documents every step (DISM `/ResetBase`, NGEN, SoftwareDistribution, Delivery Optimization, winget caches, defrag/TRIM, SDelete zero-fill) and the remote-run procedure
- From Linux, remove recovery partition
- Sysprep with unattend_cleanup.xml

```powershell
cd C:\Windows\System32\Sysprep
.\sysprep.exe /generalize /oobe /shutdown /unattend:C:\ProgramData\NeuraVPS\unattend_cleanup.xml
```

- UI and Edge tweaks
  -- Wizard de Envio de datos, elegir opción Mínima
  -- Sistema / Rendimiento, quitar efectos avanzados de UI
  -- Opciones de carpeta: Mostrar extensiones para archivos conocidos
  -- Edge, preconfigurar pantalla de inicio
  -- Edge, desactivar en Sistema opcion de seguir ejecutando aplicaciones en segundo plano
- Sysprep with unattend.xml

```powershell
cd C:\Windows\System32\Sysprep
.\sysprep.exe /generalize /oobe /shutdown /unattend:C:\ProgramData\NeuraVPS\unattend.xml
```

Disable *automatic* Windows Update (manual updates from Settings still work)

```powershell
# Policy: "Never check for updates (not recommended)" — blocks auto scan/download/install
$auPath = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU'
if (!(Test-Path $auPath)) { New-Item -Path $auPath -Force | Out-Null }
Set-ItemProperty -Path $auPath -Name 'NoAutoUpdate' -Value 1 -Type DWord
Set-ItemProperty -Path $auPath -Name 'AUOptions'    -Value 1 -Type DWord

# Keep wuauserv on-demand so the user can still click "Check for updates" in Settings
Set-Service -Name wuauserv -StartupType Manual
```
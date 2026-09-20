# Prepare Windows Template

## Guest config (inside Windows)

- **Power plan must be High performance** — the Windows default (*Balanced*)
  parks most of the vCPUs on a multi-core guest and costs ~34% of SQX
  benchmark throughput. Measured on a customer VPS E: 76k → 102k
  strategies/hour from this one change, and a freshly provisioned box was
  confirmed shipping with 16 of 22 cores parked. See
  [`POWER_PLAN.md`](POWER_PLAN.md) for the exact commands and the verification
  that must pass on a new box.
- **App launch hooks (SQX / MetaTrader)** — see [`hooks/README.md`](hooks/README.md).
  Note gate 4 there: a v144+ engine must be detected **by content**, never by
  folder name. The app installers fetch the reviewed VBS launchers from the
  versioned `/pkg/hooks/<revision>/` cache on either BASE (`files-hel` or
  `files-fsn`) and verify SHA-256 before installing; they do not fetch hooks
  directly from GitHub.

## VM config (Proxmox side, not the guest)

- **`cpu: x86-64-v4` — never `host`.** The template's `config.conf` is cloned verbatim into every new customer VM, so a host-passthrough template pins each guest to the exact silicon of the node it was born on: live migration then only works onto a same-or-newer CPU of the *same vendor* (the VMs 708/1670 failure, see the pre-check in [`scripts/migrate_vm.sh`](../scripts/migrate_vm.sh)). A baseline model lets any new VM hot-migrate anywhere in the fleet, including across CPU types and vendors.
  - **`x86-64-v4` since 2026-07-27** (was v3). It adds AVX-512, and every AMD node in the fleet runs it — verified by booting a v4 probe VM on one of each class: EPYC 9454P (36 nodes), EPYC 9454 (20), Ryzen 9 7950X3D (23), Ryzen 7 PRO 8700GE (1). A fleet-wide flag sweep of all 208 nodes found the **EX44s (i5-13500) are the only boxes without AVX-512** — 128 of them, all on the retiring vps-e tier.
  - Because PVE appends `enforce`, a v4 config on an EX44 does not degrade quietly: the VM refuses to start. New orders can never land there (`auto_provision` skips VPS-E nodes), but a **reinstall rebuilds the VM on the customer's existing node** — so `proxmox_client.create_vm_from_storagebox` probes `/proc/cpuinfo` on the destination and steps the clone down to `x86-64-v3` when the AVX-512 flags are missing (and also when the probe itself fails: a slower guest beats one that will not boot). The step-down only ever goes *down*, never up.
  - **Measured on the metric we sell** (SQX's own global menu → Benchmark, 8 vCPU VM = the vps-c spec, same VM on the same idle AX162, only the `cpu:` line changed between runs):

    | | strategies/hour | median | vs advertised 43,000 |
    |---|---|---|---|
    | `x86-64-v3` (6 runs) | 40,000 – 43,831 | 41,636 | −3.2% |
    | `x86-64-v4` (3 runs) | 40,739 – 45,957 | 45,028 | +4.7% |

    **v4 is worth roughly +5%** — every warm v4 run beat every v3 run — so it clears the advertised figure with margin where v3 sits just under it. Run-to-run noise within v3 alone is ±4.6%, so treat +5% as the direction rather than a precise number.
  - What v4 actually changes for the JVM: `UseAVX` 2 → 3. It does **not** restore `host`'s vector width — `MaxVectorSize` stays at 16 on both baseline models (only `cpu: host` reports 64), because the JVM cannot identify the microarchitecture behind a generic "QEMU Virtual CPU" and keeps its conservative cap. The gain therefore comes from AVX-512's extra registers and masking, not from wider vectors. A possible free win still unexplored: forcing `-XX:MaxVectorSize=32`.
  - Trade-off accepted 2026-07-26: the guest's Task Manager shows a generic *"QEMU Virtual CPU version 2.5+"* instead of the real model name.
  - `export_template_vm_to_shared_storage.sh` reads only `qm config --current`, requires the VM to be positively stopped, refuses `cpu: host`/`max`, and defaults to generic `x86-64-v4`. It exports to an immutable dated staging key; publishing that key is a separate operator step. The old direct-key/overwrite workflow requires explicit `LEGACY_DIRECT_EXPORT=1`.
  - Existing customer VMs keep `cpu: host` — this only applies to VMs created from the templates from now on.

## Checklist

- Apply Windows and Winget updates
- Uninstall unneeded apps and software
- Install + configure OpenSSH Server (customer SSH access, 2026-08-04): run
  [install_openssh.ps1](install_openssh.ps1) inside the template. It installs
  the Win32-OpenSSH MSI served from the BASES, sets `DefaultShell` to
  PowerShell 7 when present (else 5.1), service to Automatic, forces the
  firewall rule to `-Profile Any` (the in-box capability's rule is
  Private-only, which leaves port 22 filtered on Public networks) and sets
  `MaxAuthTries 20`.
  - ⚠️ **Host keys must be deleted before sysprep** — they are NOT
    machine-specific data to sysprep, so without this **every clone ships the
    same SSH host fingerprint**. Verified 2026-08-04: sshd regenerates all
    three key pairs by itself on the next service start, so the clone gets
    its own on first boot and nothing else is needed.
    **No longer a manual step** — this was a manual reminder that got missed
    on 2026-09-14 (both templates were exported with their host keys still in
    place; caught and fixed after the fact). Step 10 of
    [`presysprep_cleanup.ps1`](presysprep_cleanup.ps1) now stops `sshd`,
    removes `C:\ProgramData\ssh\ssh_host_*` and
    `administrators_authorized_keys`, and logs how many key files it
    deleted — it runs as part of the unattended pre-sysprep cleanup, so
    there's nothing left to remember by hand.

FeedbackHub remains installed and provisioned. Do not remove it from the image;
the operator explicitly wants it available for template users.

- Install .NET framework legacy for myfxbook installed to work
- Remove winget for sysprep to work — **NO LONGER APPLICABLE on Server 2025 (verified 2026-08-04); leave it alone**:

```powershell
Get-AppxPackage *winget* | Remove-AppxPackage   # ← NO-OP, see below
```

  > **2026-08-04 — this step does nothing and does not need to be done.** Two
  > separate findings from the template refresh:
  >
  > 1. **The command above matches no package.** `Get-AppxPackage` filters on
  >    `Name`, and the package is `Microsoft.DesktopAppInstaller` — the string
  >    "winget" appears nowhere in it. Every previous run of this line was a
  >    silent no-op.
  > 2. **Targeting it by its real name fails by design.** `Get-AppxPackage
  >    Microsoft.DesktopAppInstaller | Remove-AppxPackage` returns
  >    `0x80073CFA` / `0x80070032`: *"This app is part of Windows and cannot be
  >    uninstalled on a per-user basis."* On Server 2025 winget is OS-serviced
  >    and `NonRemovable=True`. Forcing it is the mistake that destroyed both
  >    templates on 2026-07-06 (see the note below) — **do not**.
  >
  > **The image already syspreps in this state** (both templates sysprepped
  > cleanly on 2026-08-04 with winget present). The reason the old
  > "installed-for-user but not provisioned" check *looks* alarming is that it
  > is wrong for **bundles**: a bundle is provisioned under its
  > `…_neutral_~_…` name while what gets registered for the user is the
  > architecture payload, so the two names never match. On a clean Server 2025
  > image that check flags **41 packages**, all of them false positives. The
  > only two that are not obviously system components — `DesktopAppInstaller`
  > and `WindowsTerminal` — are both payloads of provisioned bundles:
  >
  > | registered for user | provisioned bundle |
  > |---|---|
  > | `Microsoft.DesktopAppInstaller_1.29.280.0_x64__8wekyb3d8bbwe` | `Microsoft.DesktopAppInstaller_2026.623.1704.0_neutral_~_8wekyb3d8bbwe` |
  > | `Microsoft.WindowsTerminal_1.24.11911.0_x64__8wekyb3d8bbwe` | `Microsoft.WindowsTerminal_3001.24.11911.0_neutral_~_8wekyb3d8bbwe` |
  >
  > A correct blocker check must exclude `IsFramework`, `NonRemovable`, and
  > anything whose family name matches a provisioned bundle. In practice: if
  > sysprep succeeds, there was no blocker.

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

  > The shipped templates leave `AutoAdminLogon` / `DefaultUserName` **unset** —
  > `set_auto_login` (functions) writes all three per VM at provisioning time,
  > together with `set_user_password`. If you need an interactive session inside
  > the template (winget, `cleanmgr`, anything that refuses to run under QGA's
  > session 0), set the three values, reboot, do the work, then **remove
  > `AutoAdminLogon` and `DefaultUserName` again** before sysprep.
  >
  > ⚠️ **The `DefaultPassword` left in the template registry does not match the
  > account** (found 2026-08-04: autologon silently did nothing until the
  > account password was set to it). Validate before trusting it:
  > `Add-Type -AssemblyName System.DirectoryServices.AccountManagement;` +
  > `(New-Object …PrincipalContext('Machine')).ValidateCredentials($user,$pw)`.
  > Setting the template's account password is safe — provisioning overwrites
  > it per VM via `set_user_password`. Note the account name differs per
  > template: **`Administrador` in windows-es, `Administrator` in windows-en**.

- Disk cleanup — run [presysprep_cleanup.ps1](presysprep_cleanup.ps1) (unattended, can be pushed+launched via `qm guest exec`; logs to `C:\ProgramData\NeuraVPS\presysprep.log`). The current script is safe-by-default: it does not repeat DISM `/ResetBase`, reset `DataStore`/`catroot2`/BITS state, defragment, clear Event Logs, or run SDelete unless explicit switches are supplied. It preserves Panther and AppX packages, removes SSH host keys, and prioritizes TRIM. See [prepare.md](prepare.md).
- Recovery partition removal is a separate, already-completed Linux-side image task; it is not part of the guest cleanup script.
- **Publish a staged export only after review** — the exporter writes a unique
  dated staging key and never pre-deletes the current production key. Its
  `config.conf` carries a `# neuravps-stream-template-key:` marker so consumers
  pin disks and firewall to the same immutable key. The old
  direct-key overwrite workflow is an explicit legacy escape hatch; if it is
  ever required, first rename the current key on the Storage Box (the box also
  keeps daily backups for 30 days):
  `printf "rename /home/templates/windows-es /home/templates/windows-es.bak-<date>\n" | sftp -P 23 u560363@u560363.your-storagebox.de`
> **Sysprep execution context:** run from the elevated,
> interactive built-in Administrator desktop. Never launch `sysprep.exe` under
> Local System, including QGA configured as SYSTEM. Microsoft documents that
> context as unsupported on Windows Server 2025 and links it to missing
> AppX/XAML registration and black screens: [Fix Black Screen After
> Running Sysprep as System](https://learn.microsoft.com/en-us/troubleshoot/windows-client/setup-upgrade-and-drivers/sysprep-as-system-windows-11).
> The QGA example is for `presysprep_cleanup.ps1`; run Sysprep separately in
> the Administrator desktop. Keep the existing `unattend.xml` unchanged.

- UI and Edge tweaks
  -- Wizard de Envio de datos, elegir opción Mínima
  -- Sistema / Rendimiento, quitar efectos avanzados de UI
  -- Opciones de carpeta: Mostrar extensiones para archivos conocidos
  -- Edge, preconfigurar pantalla de inicio
  -- Edge, desactivar en Sistema opcion de seguir ejecutando aplicaciones en segundo plano
- Sysprep with the existing `unattend.xml` (do not create or substitute a
  cleanup answer file)

```powershell
cd C:\Windows\System32\Sysprep
.\sysprep.exe /generalize /oobe /shutdown /unattend:C:\ProgramData\NeuraVPS\unattend.xml
```

> Sysprep must be started manually from the elevated interactive
> Administrator/Administrador desktop. Do not invoke it through QGA/SYSTEM,
> do not modify the existing `unattend.xml`, and do not add `CopyProfile`.

## Export handoff marker

Before exporting, the operator records the immutable template key in the
canonical config as a comment in the form
`# neuravps-stream-template-key: windows-es-YYYYMMDD` (or `windows-en-YYYYMMDD`).
The export/publication consumer must validate this marker and use that exact
immutable key for all disk and firewall reads. A missing, duplicated, or
mismatched marker is a publication failure; do not fall back to a mutable
`windows-es`/`windows-en` alias. Keep the marker normalization lowercase with
single hyphens and no surrounding quotes.

Publication is a two-part handoff. First validate the staged streams with the
canary VMs and deploy the verified versioned installer hooks to **both** BASE
package endpoints (`files-hel` and `files-fsn`). Only after both BASE checks
pass, update the canonical `windows-es`/`windows-en` `config.conf` with the
single immutable marker; the canonical directory receives config metadata only
and never replaces the staged stream files. New restores then read the marker
and consume the already-verified immutable streams and hook revision together.

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

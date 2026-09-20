# Windows Server 2025 templates

This is the operational checklist for `windows-es` and `windows-en`. Keep the
template state measured and reproducible; record deviations in the release
notes before export.

## Guest preparation

- Apply Windows updates and the required .NET Framework 3.5 feature.
- Check [Windows Server release health](https://learn.microsoft.com/en-us/windows/release-health/status-windows-server-2025)
  for applicable out-of-band fixes as well. Windows Update can report that the
  machine is current while a relevant fix is available only from the Microsoft
  Update Catalog. For example, [KB5129235](https://support.microsoft.com/en-gb/servicing/os/windows-server/2026/09/kb5129235-windows-server-2025-update)
  fixes a September 2026 RDS issue and advances Server 2025 to 26100.33451, but
  is not offered through Windows Update. Follow the package's checkpoint order,
  verify its official download hashes, and check the installed KB and full build
  after reboot. Record the measured build before sealing; do not infer it from
  an installer exit code or promise that a patch fixes every desktop problem.
- Keep Feedback Hub installed and provisioned. Keep the OS-serviced
  `Microsoft.DesktopAppInstaller` stub. A user-installed winget/source package
  can block Sysprep: use the actual Sysprep error to identify and uninstall
  that package through the supported user context. The September images have
  already had those user packages removed. Never force removal of the
  non-removable stub or system AppX packages.
- Configure OpenSSH, Samba firewall rules, NTP, password policy, the NeuraData
  and My Servers directories, UI preferences, and the intended profile state.
  Delete OpenSSH host keys and any template `administrators_authorized_keys`
  during the cleanup step; verify no host key remains before export.
- Set High performance and verify the live parked-core count is zero. Use
  [`POWER_PLAN.md`](POWER_PLAN.md).
- Do not enable template autologon credentials. Provisioning writes the per-VM
  password and autologon settings later.

## Application installers

Install SQX and MetaTrader through the versioned installer scripts in
`windows_vm/installers/`. They fetch hooks only from the verified `/pkg/hooks/<revision>/`
cache on either BASE (`files-hel` or `files-fsn`) and verify SHA-256. Hooks are
application-install behavior; they are not part of the template cleanup or
export stream.

- The SQX installer applies the same per-app setting as `set_java_headless.ps1`:
  `-Djava.awt.headless=true` to the application `.config`. Do not use a machine
  wide Java environment variable.
- The SQX installer may set IFEO priority `CpuPriorityClass=6` (AboveNormal)
  for the intended executable. It must never set an IFEO `Debugger` on `StrategyQuantX.exe`; that
  executable is unsafe on dual v143/v144 installations. The withdrawn
  `sqx144_hook_launcher.vbs` must not be restored.
- MetaTrader's installer applies `/portable` only after its documented data
  portability gate. Do not force it on non-portable customer data.
- Do not edit the existing VBS launchers unless a behavioural defect is
  demonstrated. Registry checks alone do not prove an application launch is
  safe.

See [`hooks/README.md`](hooks/README.md) for hook history and gates, and the
installer scripts for the current verified revision and hashes.

## Proxmox template configuration

- Use `cpu: x86-64-v4`; never introduce `cpu: host` into a template export.
  A reinstall applies the template CPU policy to the existing VM and may step
  down to `cpu: x86-64-v3` only when the destination node lacks AVX-512.
  Existing customer resource values (RAM, balloon, cores, sockets, NUMA) and
  all customer disk lines remain preserved during reinstall.
- Keep the template's EFI/TPM options while retaining each VM's local firmware
  volume identity. Preserve network/MAC, VM identity, and customer data disks.
- Export only a positively stopped VM using
  [`scripts/export_template_vm_to_shared_storage.sh`](../scripts/export_template_vm_to_shared_storage.sh).
  The default destination is an immutable dated staging key; do not use the
  legacy direct-key overwrite path unless explicitly approved.

## Pre-Sysprep cleanup and Sysprep

Run [`presysprep_cleanup.ps1`](presysprep_cleanup.ps1) elevated with its safe
defaults. It does not run Sysprep or edit `unattend.xml`; it preserves Panther,
AppX, Feedback Hub, `DataStore`, `catroot2`, and BITS state. It clears only
measured closed caches, removes SSH template identities, handles VSS/hibernation
only when present, and prioritizes TRIM. DISM `/ResetBase`, NGEN, defrag,
SDelete, and Event Log clearing require an explicit switch and measurement.

Start Sysprep in the elevated interactive Administrator/Administrador session.
A self-deleting scheduled task with `Interactive` logon and `Highest` privileges
is also validated; QGA may register that task, but must never run Sysprep as
SYSTEM. Verify the task SID ends in `-500`, its session is not 0, and it removes
itself before launching Sysprep. This avoids PowerShell command history:

```powershell
cd C:\Windows\System32\Sysprep
.\sysprep.exe /generalize /oobe /shutdown /unattend:C:\ProgramData\NeuraVPS\unattend.xml
```

Do not modify the existing `unattend.xml`, add `CopyProfile`, or substitute a
cleanup answer file. Validate after cloning: RDP, Explorer/UI, network, SSH
with a new fingerprint, Administrator SID `-500`, AppX CBS/Core/XAML state,
Feedback Hub, and the intended power plan.

## Export and publication handoff

Each new canonical config contains exactly one marker comment, for example:

```text
# neuravps-stream-template-key: windows-es-20260920
```

The English config points to its own `windows-en-20260920` release. The value
contains only letters, digits, `_` or `-` and names an immutable staging key,
never the mutable `windows-es` / `windows-en` aliases. Consumers read the config
once and use its key for every disk and firewall read. Legacy configs without
a marker remain supported only to permit deployment and rollback.

Publication order: validate the staging streams and installers with canaries;
deploy every Google create/reinstall/queue function that consumes templates;
deploy the updated `nvx-installers.sh` to both BASEs and run it, verifying hooks
and installer hashes at both endpoints; then atomically replace each canonical
`config.conf` with the complete corresponding staging config. Keep a copy of
the old config for rollback and leave the old canonical disk streams intact.
Existing jobs can finish on the old streams while new jobs use the pinned
release. The hourly cache timer refreshes payloads, not its own script: when
changing hook revisions, deploy the refresher script to both BASEs too.

## Historical notes

The old blanket claim that winget could never block Sysprep was based on an
invalid SYSTEM-context test. Check the actual user-package error instead.
Forced system AppX removal and SYSTEM Sysprep are retired procedures. The
September release preserves the OS stub and Feedback Hub and passed interactive
Sysprep plus real desktop logins in both languages.

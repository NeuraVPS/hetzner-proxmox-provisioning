# Windows Server 2025 templates

This is the operational checklist for `windows-es` and `windows-en`. Keep the
template state measured and reproducible; record deviations in the release
notes before export.

## Guest preparation

- Apply Windows updates and the required .NET Framework 3.5 feature.
- Keep Feedback Hub installed and provisioned. Keep the OS-serviced
  `Microsoft.DesktopAppInstaller`/winget stub and its source packages. Do not
  remove AppX packages or force removal of non-removable packages.
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

- SQX headless behavior comes from `set_java_headless.ps1`, which writes
  `-Djava.awt.headless=true` to the application `.config`. Do not use a machine
  wide Java environment variable.
- The SQX installer may set IFEO priority `CpuPriorityClass=6` (AboveNormal)
  for the intended executable. It must never wire `StrategyQuantX.exe`; that
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

Sysprep must be started manually from the elevated interactive
Administrator/Administrador desktop, never through QGA/SYSTEM:

```powershell
cd C:\Windows\System32\Sysprep
.\sysprep.exe /generalize /oobe /shutdown /unattend:C:\ProgramData\NeuraVPS\unattend.xml
```

Do not modify the existing `unattend.xml`, add `CopyProfile`, or substitute a
cleanup answer file. Validate after cloning: RDP, Explorer/UI, network, SSH
with a new fingerprint, Administrator SID `-500`, AppX CBS/Core/XAML state,
Feedback Hub, and the intended power plan.

## Export and publication handoff

The canonical config contains exactly one marker comment:

```text
# neuravps-stream-template-key: windows-es-YYYYMMDD
# neuravps-stream-template-key: windows-en-YYYYMMDD
```

The marker must be lowercase, single-hyphen, and point to the immutable staged
key. Missing, duplicated, malformed, or mutable-alias markers are publication
failures. Consumers must use that exact key for every disk and firewall read.

Publication order is strict: validate staged streams with the canaries, deploy
the verified installer hook cache to both BASE endpoints, then update only the
canonical `config.conf` marker. The canonical directory receives configuration
metadata; it does not replace immutable stream files.

## Historical notes

The old checklist asked operators to remove winget, Feedback Hub, or arbitrary
AppX packages and sometimes ran Sysprep under SYSTEM. Those procedures are
retired. Server 2025 keeps the DesktopAppInstaller stub by design, and the
current images have already passed the measured interactive Sysprep path.

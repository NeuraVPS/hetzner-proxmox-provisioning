Execute this on a new server to prepare it for Proxmox:

```bash
screen -d -m bash -c "curl -fsSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/install.sh | bash -s -- 1000 AX162-R; exec bash"
screen -r
```

```bash
screen -d -m bash -c "curl -fsSL https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/install.sh | bash -s -- 1000 EX44; exec bash"
screen -r
```

This will automatically generate:

- Hostname: `pve0000001-AX162-R`
- Private IPv4: `10.64.0.1`
- Private IPv6: `fd00:4000::1`

The server ID can be any number between 1 and 1,048,574.

# Prepare new host

## Checklist

- /etc/firebase-credentials.json
- /var/lib/svz/dump/vzdump-qemu-100-es.vma.zst
- Add server IPv6 CDIR to Proxmox firewall
- Add to firestore
- Re add snippets to shared storage

# Prepare Windows Template

## Checklist

- Apply Windows and Winget updates
- Disable Password lock Policy
- Apply Java patch for SQX
- Install desired software (including 10× MetaTrader 5 portable in `C:\MetaTrader\MetaTrader 5 - 001` … `010`)
- Set MetaTrader file/URL associations: copy `scripts/mt5_open.vbs` to `C:\MetaTrader\mt5_open.vbs`, then run as Administrator: `powershell -ExecutionPolicy Bypass -File scripts\set_mt5_associations.ps1` (or from `C:\Provisioning\` if you copied the script there). This configures EX5, MQL5, MQL5.Header, mql5buy, metaeditor5, and MetaTrader 5 Export to use the launcher and default icons from instance 001. The launcher re-applies HKCU associations 5 seconds after opening a file so MetaTrader cannot keep overrides.
- NTP servers for Hetzner
- Permitir Samba en el firewall de Windows
- Crear carpeta C:\Mis Servidores y enlace en el escritorio
- Disk cleanup
- Sysprep with unattend.xml
- From Linux, remove recovery partition

## Java Issue with sqx

```powershell
setx _JAVA_OPTIONS "-Djava.awt.headless=true" /M
setx JAVA_TOOL_OPTIONS "-Djava.awt.headless=true" /M
```

## Other useful configurations

```powershell
# Don't lock accounts on failed login attempts
net accounts /lockoutthreshold:0

# Don't require password changes
net accounts /maxpwage:UNLIMITED

# Hide Telemetry configuration on first login
New-Item -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows\DataCollection" -Force | Out-Null
Set-ItemProperty -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows\DataCollection" -Name "AllowTelemetry" -Value 1 -Type DWord

New-Item -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows\OOBE" -Force | Out-Null
Set-ItemProperty -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows\OOBE" -Name "DisablePrivacyExperience" -Value 1 -Type DWord

# Hide Server Manager on login
Set-ItemProperty -Path "HKLM:\Software\Microsoft\ServerManager" -Name "DoNotOpenServerManagerAtLogon" -Value 1 -Type DWord

# Disable WindowsFeedbackHub installation for new users
Get-AppxProvisionedPackage -Online | Where-Object DisplayName -like "Microsoft.WindowsFeedbackHub" | Remove-AppxProvisionedPackage -Online

# Disable Edge start wizard and make it clean
$k = "HKLM:\SOFTWARE\Policies\Microsoft\Edge"
New-Item -Path $k -Force | Out-Null

# Quitar first-run / wizard
New-ItemProperty $k -Name HideFirstRunExperience -PropertyType DWord -Value 1 -Force | Out-Null

# Sin procesos background (server friendly)
New-ItemProperty $k -Name BackgroundModeEnabled -PropertyType DWord -Value 0 -Force | Out-Null
New-ItemProperty $k -Name StartupBoostEnabled -PropertyType DWord -Value 0 -Force | Out-Null

# NTP: eliminar TODO el contenido
New-ItemProperty $k -Name NewTabPageContentEnabled -PropertyType DWord -Value 0 -Force | Out-Null
New-ItemProperty $k -Name NewTabPageQuickLinksEnabled -PropertyType DWord -Value 0 -Force | Out-Null
New-ItemProperty $k -Name NewTabPageBackgroundImageEnabled -PropertyType DWord -Value 0 -Force | Out-Null
New-ItemProperty $k -Name NewTabPageCustomizeEnabled -PropertyType DWord -Value 0 -Force | Out-Null
New-ItemProperty $k -Name NewTabPageAppsEnabled -PropertyType DWord -Value 0 -Force | Out-Null
New-ItemProperty $k -Name NewTabPageHideWeather -PropertyType DWord -Value 1 -Force | Out-Null

# Show all file extensions in explorer
Start-Process powershell -ArgumentList @"
New-PSDrive -Name HKU -PSProvider Registry -Root HKEY_USERS | Out-Null
reg load HKU\DefaultUser 'C:\Users\Default\NTUSER.DAT'
New-Item -Path 'HKU:\DefaultUser\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced' -Force | Out-Null
New-ItemProperty -Path 'HKU:\DefaultUser\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced' -Name 'HideFileExt' -PropertyType DWord -Value 0 -Force | Out-Null
reg unload HKU\DefaultUser
"@

# SQX in High priority
$basePath = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\StrategyQuantX_nocheck.exe\PerfOptions'
New-Item -Path $basePath -Force | Out-Null
New-ItemProperty -Path $basePath -Name 'CpuPriorityClass' -PropertyType DWord -Value 6 -Force | Out-Null

# Autologin
$RegPath = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"
Set-ItemProperty -Path $RegPath -Name "AutoAdminLogon" -Value "1" -Type String
Set-ItemProperty -Path $RegPath -Name "DefaultUserName" -Value "Administrador" -Type String
#Set-ItemProperty -Path $RegPath -Name "DefaultPassword" -Value "<new password>" -Type String

# Allow SAMBA through Windows Firewall
Set-NetFirewallRule -DisplayName 'Uso compartido de archivos e impresoras (restrictivo) (SMB de entrada)' -Enabled True

# Create Mis Servidores folder and Desktop symlink
$targetFolder = 'C:\Mis Servidores';
$publicDesktop = 'C:\Users\Public\Desktop';
$linkPath = "$publicDesktop\Mis Servidores";

if (!(Test-Path $targetFolder)) { New-Item -ItemType Directory -Path $targetFolder -Force | Out-Null }

if (Test-Path $linkPath) {
    $item = Get-Item $linkPath -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { exit }
    Remove-Item $linkPath -Recurse -Force
}

New-Item -ItemType SymbolicLink -Path $linkPath -Target $targetFolder -Force | Out-Null

# Optimized UI
$base = "Registry::HKEY_USERS\.DEFAULT\Control Panel\Desktop"

Set-ItemProperty $base -Name DragFullWindows -Value "0"
Set-ItemProperty $base -Name MenuAnimation -Value "0"
Set-ItemProperty $base -Name ToolTipAnimation -Value "0"
Set-ItemProperty $base -Name ComboBoxAnimation -Value "0"
Set-ItemProperty $base -Name MinAnimate -Value "0"
Set-ItemProperty $base -Name FontSmoothing -Value "2"
Set-ItemProperty $base -Name FontSmoothingType -Value 2
Set-ItemProperty $base -Name CursorShadow -Value 0
Set-ItemProperty $base -Name DropShadow -Value 0
Set-ItemProperty $base -Name UIEffects -Value 0
Set-ItemProperty $base -Name UserPreferencesMask -Value ([byte[]](0x90,0x12,0x03,0x80,0x10,0x00,0x00,0x00))

New-Item "Registry::HKEY_USERS\.DEFAULT\Software\Microsoft\Windows\CurrentVersion\Explorer\VisualEffects" -Force | Out-Null
Set-ItemProperty "Registry::HKEY_USERS\.DEFAULT\Software\Microsoft\Windows\CurrentVersion\Explorer\VisualEffects" `
  -Name VisualFXSetting -Value 3

```

## Winget update fix for Sysprep

```powershell
Get-AppxPackage *winget* | Remove-AppxPackage
```

## Sysprep

The answer file sets `CopyProfile` in the **specialize** pass (it does not run in oobeSystem). That copies the built-in Administrator profile to the Default User template. Even when correct, **CopyProfile often does not preserve Edge** (and other modern app data): many settings are user-SID–bound or encrypted and get reset on first logon.

### Reliable way to keep Edge (and other profile data): export/restore scripts

1. **On the reference VM (before sysprep):**
   - Copy `scripts/export_edge_profile.ps1` and `scripts/restore_edge_profile.ps1` to `C:\Provisioning\`.
   - Configure Edge (and anything else in the Administrator profile) as desired.
   - Run as Administrator:
     ```powershell
     C:\Provisioning\export_edge_profile.ps1
     ```
   - This copies the Edge profile to `C:\Provisioning\EdgeDefault` (survives generalize).

2. **Sysprep** as below. The unattend `FirstLogonCommands` will run `restore_edge_profile.ps1` at first logon if it exists; that script restores `EdgeDefault` into the new user’s profile.

3. **If you don’t use the Edge scripts:** leave `C:\Provisioning\` empty or omit the scripts; the first-logon command only runs the restore script if the file exists.

**CopyProfile (specialize) requirements:** use only the built-in Administrator account and run sysprep as Administrator. Taskbar pins, Start layout, and some encrypted settings are still not preserved; use GPO or scripts for those.

```powershell
cd C:\Windows\System32\Sysprep
.\sysprep.exe /generalize /oobe /shutdown /unattend:C:\Windows\unattend.xml
```

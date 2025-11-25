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
- Install desired software
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

# Hide Telemetry configuration on first login
New-Item -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows\DataCollection" -Force | Out-Null
Set-ItemProperty -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows\DataCollection" -Name "AllowTelemetry" -Value 1 -Type DWord

New-Item -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows\OOBE" -Force | Out-Null
Set-ItemProperty -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows\OOBE" -Name "DisablePrivacyExperience" -Value 1 -Type DWord

# Hide Server Manager on login
Set-ItemProperty -Path "HKLM:\Software\Microsoft\ServerManager" -Name "DoNotOpenServerManagerAtLogon" -Value 1 -Type DWord

# Disable WindowsFeedbackHub installation for new users
Get-AppxProvisionedPackage -Online | Where-Object DisplayName -like "Microsoft.WindowsFeedbackHub" | Remove-AppxProvisionedPackage -Online

# Disable Edge start wizard
New-Item -Path "HKLM:\SOFTWARE\Policies\Microsoft\Edge" -Force | Out-Null
Set-ItemProperty -Path "HKLM:\SOFTWARE\Policies\Microsoft\Edge" -Name "HideFirstRunExperience" -Value 1 -Type DWord
Set-ItemProperty -Path "HKLM:\SOFTWARE\Policies\Microsoft\Edge" -Name "BackgroundModeEnabled" -Value 0 -Type DWord

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
```

## Winget update fix for Sysprep
```powershell
Get-AppxPackage *winget* | Remove-AppxPackage
```

## Sysprep

```powershell
cd C:\Windows\System32\Sysprep
.\sysprep.exe /generalize /oobe /shutdown /unattend:C:\Windows\unattend.xml
```

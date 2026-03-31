# Prepare Windows Template

## Checklist

- Apply Windows and Winget updates
- Uninstall unneeded apps and software

```powershell
# Disable WindowsFeedbackHub installation for new users
Get-AppxProvisionedPackage -Online | Where-Object DisplayName -like "Microsoft.WindowsFeedbackHub" | Remove-AppxProvisionedPackage -Online
```

- Install .NET framework legacy for myfxbook installed to work
- Remove winget for sysprep to work

```powershell
Get-AppxPackage *winget* | Remove-AppxPackage
```

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

- Disk cleanup
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

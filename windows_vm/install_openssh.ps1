# NeuraVPS — instala y configura OpenSSH Server en un guest Windows.
#
# Idempotente y agnóstico del origen: si sshd ya existe (capability in-box o
# MSI), solo aplica/refuerza la config. Si no existe, instala el MSI oficial
# de Win32-OpenSSH servido desde las BASES (files-*.neuravps.com/pkg/) — la
# vía Add-WindowsCapability tarda 15-20 min por VM (Windows Update + TiWorker)
# y castiga cajas MT en horario de mercado; el MSI tarda ~15 s.
#
# Config aplicada SIEMPRE:
#   * DefaultShell = powershell.exe (caso de uso: apps de IA contra PowerShell)
#   * sshd StartupType=Automatic + Running
#   * Regla de firewall OpenSSH-Server-In-TCP con -Profile Any. OJO: la regla
#     que crea la capability nace SOLO con perfil Private y la red del guest
#     clasifica como Public => puerto filtrado (medido en la vm 1985,
#     2026-08-04). Forzar Any es obligatorio, no cosmético.
#
# NUNCA reinicia la VM (msiexec /norestart; nada de Restart-Computer).
# Emite una única línea RESULT:OK|FAIL para el sweep.
$ErrorActionPreference = 'Stop'
$MSI_SHA256 = 'ddec9c53864280759cf9f74791cefd387100e3946aa849a1c138a4ed1b96b7d9'
$MSI_URLS = @(
  'https://files-fsn.neuravps.com/pkg/OpenSSH-Win64-v10.0.0.0.msi',
  'https://files-hel.neuravps.com/pkg/OpenSSH-Win64-v10.0.0.0.msi'
)
try {
  $svc = Get-Service sshd -ErrorAction SilentlyContinue
  if (-not $svc) {
    $msi = 'C:\ProgramData\NeuraVPS\OpenSSH-Win64.msi'
    New-Item -ItemType Directory -Path 'C:\ProgramData\NeuraVPS' -Force | Out-Null
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $got = $false
    foreach ($url in $MSI_URLS) {
      try {
        Invoke-WebRequest -Uri $url -OutFile $msi -UseBasicParsing -TimeoutSec 60
        $got = $true; break
      } catch { }
    }
    if (-not $got) { throw 'MSI download failed from both bases' }
    $hash = (Get-FileHash $msi -Algorithm SHA256).Hash.ToLower()
    if ($hash -ne $MSI_SHA256) { throw "MSI hash mismatch: $hash" }
    $p = Start-Process msiexec.exe -ArgumentList '/i', $msi, '/qn', '/norestart' -Wait -PassThru
    if ($p.ExitCode -ne 0) { throw "msiexec exit $($p.ExitCode)" }
    Remove-Item $msi -Force -ErrorAction SilentlyContinue
  }
  # DefaultShell: PowerShell 7 (pwsh) si esta instalado; si no, Windows
  # PowerShell 5.1. Re-ejecutar este script tras instalar PS7 actualiza el
  # shell por defecto (peticion del operador 2026-08-04).
  $pwsh = 'C:\Program Files\PowerShell\7\pwsh.exe'
  $shell = if (Test-Path $pwsh) { $pwsh }
           else { 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' }
  New-Item -Path 'HKLM:\SOFTWARE\OpenSSH' -Force | Out-Null
  New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell `
    -Value $shell -PropertyType String -Force | Out-Null
  Set-Service sshd -StartupType Automatic
  if ((Get-Service sshd).Status -ne 'Running') { Start-Service sshd }
  $r = Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue
  if ($r) {
    Set-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -Profile Any -Enabled True
  } else {
    New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' `
      -DisplayName 'OpenSSH Server (sshd)' -Enabled True -Profile Any `
      -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
  }
  # MaxAuthTries 20: un cliente macOS/Linux con varias claves en el agent las
  # ofrece TODAS antes del password y el default (6) corta la conexion con
  # "Too many authentication failures" sin llegar a pedir contraseña
  # (reproducido por el operador 2026-08-04). El anti-brute-force real vive
  # en la BASE (rate-limits por origen y por puerto), no en este limite.
  $cfg = 'C:\ProgramData\ssh\sshd_config'
  if (Test-Path $cfg) {
    $s = Get-Content $cfg -Raw
    if ($s -notmatch '(?m)^MaxAuthTries 20\s*$') {
      if ($s -match '(?m)^\s*#?\s*MaxAuthTries\b.*$') {
        $s = $s -replace '(?m)^\s*#?\s*MaxAuthTries\b.*$', 'MaxAuthTries 20'
      } else {
        $s = "MaxAuthTries 20`r`n" + $s
      }
      Set-Content -Path $cfg -Value $s -Encoding ascii
      Restart-Service sshd
    }
  }
  # Prioridad alta para sshd (fleet-wide, 2026-08-07). Bajo saturacion de CPU
  # (cajas SQX/sqcli a tope, 20 cores pegados) el cliente SSH hace timeout
  # mientras RDP no sufre (TermService es un servicio residente y ya calido; no
  # crea proceso). CLAVE: OpenSSH 10 en Windows partio la arquitectura — el
  # maestro es `sshd.exe` (solo escucha) pero CADA conexion la atiende un
  # proceso NUEVO **`sshd-session.exe`**, y ES ESE el que manda el banner y hace
  # el handshake. Bajo carga, crear/planificar ese sshd-session.exe a prioridad
  # Normal se retrasa y el banner no llega antes del timeout del cliente.
  # Verificado en vivo (vm 444, 2026-08-07): el hijo era sshd-session.exe en
  # Normal; poner el IFEO solo en sshd.exe NO lo tocaba. Por eso el IFEO va en
  # AMBOS, y lo que de verdad importa es sshd-session.exe.
  # IFEO PerfOptions aplica al crear el proceso por NOMBRE de imagen y sobrevive
  # reinicios. NO afecta al shell (pwsh.exe es otro ejecutable, no hereda) =>
  # la carga del cliente sigue en Normal; priorizamos solo el MONTAJE.
  # CpuPriorityClass: 3=High (1=Idle 2=Normal 4=RealTime 5=BelowNormal
  # 6=AboveNormal). RealTime jamas. Guard Test-Path: New-Item -Force sobre una
  # clave IFEO existente borra sus subclaves (leccion SQX 2026-07-06).
  $ifeoBase = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options'
  foreach ($img in @('sshd.exe', 'sshd-session.exe')) {
    $k = "$ifeoBase\$img"
    if (-not (Test-Path $k)) { New-Item -Path $k -Force | Out-Null }
    if (-not (Test-Path "$k\PerfOptions")) { New-Item -Path "$k\PerfOptions" -Force | Out-Null }
    New-ItemProperty -Path "$k\PerfOptions" -Name 'CpuPriorityClass' -PropertyType DWord -Value 3 -Force | Out-Null
  }
  # Sube tambien los procesos sshd/sshd-session YA vivos en caliente (sin
  # reiniciar el servicio => no corta sesiones). Los nuevos ya nacen High por el
  # IFEO. -1 = no cambiar si ya son High.
  Get-Process sshd, sshd-session -ErrorAction SilentlyContinue | ForEach-Object {
    try { $_.PriorityClass = [Diagnostics.ProcessPriorityClass]::High } catch { }
  }
  $sock = New-Object Net.Sockets.TcpClient
  $ok = $sock.ConnectAsync('127.0.0.1', 22).Wait(5000)
  $sock.Close()
  if (-not $ok) { throw 'port 22 not answering after config' }
  Write-Output 'RESULT:OK'
} catch {
  Write-Output ('RESULT:FAIL ' + ($_.Exception.Message -replace "[`r`n]+", ' '))
}

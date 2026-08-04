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
  New-Item -Path 'HKLM:\SOFTWARE\OpenSSH' -Force | Out-Null
  New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell `
    -Value 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' `
    -PropertyType String -Force | Out-Null
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
  $sock = New-Object Net.Sockets.TcpClient
  $ok = $sock.ConnectAsync('127.0.0.1', 22).Wait(5000)
  $sock.Close()
  if (-not $ok) { throw 'port 22 not answering after config' }
  Write-Output 'RESULT:OK'
} catch {
  Write-Output ('RESULT:FAIL ' + ($_.Exception.Message -replace "[`r`n]+", ' '))
}

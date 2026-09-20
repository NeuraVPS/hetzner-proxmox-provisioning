[CmdletBinding()]
param(
  [switch]$RunResetBase,
  [switch]$RunNgen,
  [switch]$RunDefrag,
  [switch]$RunSDelete,
  [switch]$ClearEventLogs
)

# Safe-by-default pre-Sysprep cleanup for Server 2025 templates.
# Run elevated. This script does not run Sysprep and never removes AppX packages.
$ErrorActionPreference = 'Stop'
$unattendPath = 'C:\ProgramData\NeuraVPS\unattend.xml'
$unattendHash = (Get-FileHash -LiteralPath $unattendPath -Algorithm SHA256).Hash
$bitsWasRunning = (Get-Service bits).Status -eq 'Running'
$log = 'C:\ProgramData\NeuraVPS\presysprep.log'
New-Item -ItemType Directory -Path (Split-Path $log) -Force | Out-Null
Start-Transcript -Path $log -Force | Out-Null
function Step([string]$Name) { Write-Output ("[{0}] === {1} ===" -f (Get-Date -Format 'HH:mm:ss'), $Name) }
function RemoveContents([string]$Path) {
  if (Test-Path -LiteralPath $Path) { Get-ChildItem -LiteralPath $Path -Force -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue }
}

try {
  if ((Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending') -or
      (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired')) {
    Write-Output 'ABORT: reboot pending; reboot and rerun before cleanup'; exit 2
  }
  $before = (Get-Volume -DriveLetter C).SizeRemaining
  Step ("Free before: {0:N1} GB" -f ($before / 1GB))
  Step '1. Stop update services for closed-file cleanup'
  Stop-Service -Name wuauserv,bits -Force -ErrorAction Stop
  # No catalog reset is performed, so cryptsvc and msiserver need no changes.

  if ($RunResetBase) {
    Step '2. DISM ResetBase (explicit switch; irreversible update uninstall loss)'
    Dism.exe /Online /Cleanup-Image /StartComponentCleanup /ResetBase | Out-Host
    Write-Output ("DISM rc={0}" -f $LASTEXITCODE)
    if ($LASTEXITCODE -ne 0) { throw "DISM failed: $LASTEXITCODE" }
  } else { Step '2. DISM ResetBase skipped (use -RunResetBase explicitly)' }
  if ($RunNgen) {
    Step '3. NGEN queued items (explicit switch)'
    foreach ($fw in 'Framework64','Framework') { foreach ($ver in 'v4.0.30319','v2.0.50727') {
      $ng = "$env:WINDIR\Microsoft.NET\$fw\$ver\ngen.exe"
      if (Test-Path $ng) { & $ng executeQueuedItems /nologo /silent | Out-Null; if ($LASTEXITCODE -ne 0) { throw "NGEN failed: $ng rc=$LASTEXITCODE" } }
    }}
  } else { Step '3. NGEN skipped (use -RunNgen after measuring queue)' }

  Step '4. Closed Windows Update download cache only'
  RemoveContents 'C:\Windows\SoftwareDistribution\Download'
  # DataStore, catroot2 and BITS qmgr state are intentionally preserved.
  Step '5. Delivery Optimization cache'
  if (Get-Command Delete-DeliveryOptimizationCache -ErrorAction SilentlyContinue) { Delete-DeliveryOptimizationCache -Force -ErrorAction SilentlyContinue }
  else { RemoveContents 'C:\Windows\ServiceProfiles\NetworkService\AppData\Local\Microsoft\Windows\DeliveryOptimization\Cache' }
  Step '6. VSS shadows, only when present'
  $vss = @(Get-CimInstance Win32_ShadowCopy)
  if ($vss.Count -gt 0) { vssadmin.exe delete shadows /all /quiet | Out-Host; if ($LASTEXITCODE -ne 0) { throw "VSS cleanup failed: $LASTEXITCODE" } } else { Write-Output 'No guest VSS shadows found' }

  Step '7. Closed temp, WER, crash dumps and user history/cache files'
  RemoveContents "$env:WINDIR\Temp"; RemoveContents "$env:WINDIR\Prefetch"; RemoveContents 'C:\ProgramData\Microsoft\Windows\WER'
  Get-ChildItem 'C:\Users' -Directory -ErrorAction SilentlyContinue | ForEach-Object {
    $u = $_.FullName; RemoveContents "$u\AppData\Local\Temp"; RemoveContents "$u\AppData\Local\Microsoft\Windows\INetCache"; RemoveContents "$u\AppData\Local\Microsoft\Windows\WER"
    Remove-Item "$u\AppData\Local\Microsoft\Windows\Explorer\thumbcache_*.db" -Force -ErrorAction SilentlyContinue
    Remove-Item "$u\AppData\Local\Microsoft\Windows\PowerShell\ModuleAnalysisCache" -Force -ErrorAction SilentlyContinue
    Remove-Item "$u\AppData\Roaming\Microsoft\Windows\PowerShell\PSReadLine\ConsoleHost_history.txt" -Force -ErrorAction SilentlyContinue
    Remove-Item "$u\AppData\Roaming\Microsoft\PowerShell\PSReadLine\ConsoleHost_history.txt" -Force -ErrorAction SilentlyContinue
    RemoveContents "$u\AppData\Local\CrashDumps"
  }
  Step '8. Old servicing logs/dumps; preserve Panther'
  foreach ($d in 'CBS','DISM','WindowsUpdate','MoSetup','NetSetup') { RemoveContents "C:\Windows\Logs\$d" }
  Remove-Item 'C:\Windows\MEMORY.DMP' -Force -ErrorAction SilentlyContinue; RemoveContents 'C:\Windows\Minidump'; RemoveContents 'C:\Windows\LiveKernelReports'
  Step '9. Winget cache only; no AppX removal'
  Get-ChildItem 'C:\Users' -Directory -ErrorAction SilentlyContinue | ForEach-Object { Get-ChildItem -Path "$($_.FullName)\AppData\Local\Packages\Microsoft.DesktopAppInstaller_*" -Directory -ErrorAction SilentlyContinue | ForEach-Object { RemoveContents "$($_.FullName)\LocalCache"; RemoveContents "$($_.FullName)\LocalState\DiagOutputDir" } }
  RemoveContents 'C:\Windows\Temp\WinGet'
  Step '10. OpenSSH host keys and template authorization'
  $sshd = Get-Service sshd -ErrorAction SilentlyContinue
  if ($sshd) { Stop-Service sshd -Force -ErrorAction Stop }
  foreach ($key in @(Get-ChildItem 'C:\ProgramData\ssh\ssh_host_*' -File -ErrorAction SilentlyContinue)) { Remove-Item -LiteralPath $key.FullName -Force -ErrorAction Stop }
  $authorizedKeys = 'C:\ProgramData\ssh\administrators_authorized_keys'
  if (Test-Path -LiteralPath $authorizedKeys) { Remove-Item -LiteralPath $authorizedKeys -Force -ErrorAction Stop }
  if (@(Get-ChildItem 'C:\ProgramData\ssh\ssh_host_*' -File -ErrorAction SilentlyContinue).Count -ne 0 -or (Test-Path -LiteralPath $authorizedKeys)) { throw 'OpenSSH template identities remain; do not export' }
  # Leave sshd stopped so it cannot generate another shared identity before
  # Sysprep. Its unchanged Automatic startup regenerates keys on each clone.
  Step '11. Recycle Bin'; Clear-RecycleBin -Force -ErrorAction SilentlyContinue
  Step '12. Hibernation, only when hiberfil.sys exists'; if (Test-Path 'C:\hiberfil.sys') { powercfg.exe /hibernate off; if ($LASTEXITCODE -ne 0) { throw 'Could not disable hibernation' } }
  Step '13. Event logs preserved by default'
  if ($ClearEventLogs) { Write-Output 'Clearing event logs only because -ClearEventLogs was supplied'; Get-WinEvent -ListLog * -ErrorAction SilentlyContinue | Where-Object { $_.RecordCount -gt 0 -and $_.IsEnabled } | ForEach-Object { wevtutil.exe cl $_.LogName 2>$null } }
  else { Write-Output 'Event logs left intact; copy evidence before any explicit clear' }
  Step '14. ReTrim (default; defrag requires explicit switch)'; if ($RunDefrag) { Optimize-Volume -DriveLetter C -Defrag -Verbose }; Optimize-Volume -DriveLetter C -ReTrim -Verbose
  Step '15. SDelete zero-fill only with explicit switch'
  if ($RunSDelete) { $sdelete = "$env:WINDIR\System32\sdelete64.exe"; if (-not (Test-Path $sdelete)) { throw 'SDelete is absent; stage and verify it before using -RunSDelete' }; & $sdelete -accepteula -nobanner -z C: 2>&1 | Select-Object -Last 3; if ($LASTEXITCODE -ne 0) { throw "SDelete failed: $LASTEXITCODE" } }
  else { Write-Output 'SDelete skipped (use -RunSDelete only after measuring TRIM reclaim)' }
  Step '16. Remove Winlogon DefaultPassword before sealing'
  $winlogonPath = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
  $winlogonKey = Get-Item -LiteralPath $winlogonPath -ErrorAction Stop
  if ($winlogonKey.GetValueNames() -contains 'DefaultPassword') {
    Remove-ItemProperty -LiteralPath $winlogonPath -Name DefaultPassword -Force -ErrorAction Stop
  }
  $winlogonKey = Get-Item -LiteralPath $winlogonPath -ErrorAction Stop
  if ($winlogonKey.GetValueNames() -contains 'DefaultPassword') {
    throw 'Winlogon DefaultPassword remains; refusing to seal template'
  }
  Write-Output 'Winlogon DefaultPassword absent; existing username/domain and SID500 credentials were not changed'
  Step 'Final ReTrim'; Optimize-Volume -DriveLetter C -ReTrim -Verbose
  $after = (Get-Volume -DriveLetter C).SizeRemaining; Step ("Free after: {0:N1} GB (in-guest delta {1:+0.0;-0.0} GB)" -f ($after / 1GB), (($after-$before)/1GB))
  Step 'Final policy sanity check'; Get-Service wuauserv | Format-Table Name,Status,StartType | Out-String -Width 100 | Write-Output; Get-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU' -ErrorAction SilentlyContinue | Select-Object NoAutoUpdate,AUOptions | Out-String | Write-Output
  if ((Get-FileHash -LiteralPath $unattendPath -Algorithm SHA256).Hash -ne $unattendHash) { throw 'unattend.xml changed during cleanup' }
  Write-Output 'CLEANUP COMPLETE rc=0'
} finally {
  if ($bitsWasRunning) { Start-Service bits -ErrorAction Continue }
  Stop-Transcript | Out-Null
}

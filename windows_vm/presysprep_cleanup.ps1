# NeuraVPS pre-sysprep cleanup (autonomous run via QGA, SYSTEM, session 0)
# Log: C:\ProgramData\NeuraVPS\presysprep.log
$ErrorActionPreference='SilentlyContinue'
$log='C:\ProgramData\NeuraVPS\presysprep.log'
New-Item -ItemType Directory -Path 'C:\ProgramData\NeuraVPS' -Force | Out-Null
Start-Transcript -Path $log -Force | Out-Null
function Step($n){ Write-Output ("[{0}] === {1} ===" -f (Get-Date -Format 'HH:mm:ss'), $n) }

# 0. Guard: no pending reboot
if( (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending') -or
    (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired') ){
  Write-Output 'ABORT: reboot pending - reboot first, then rerun'; Stop-Transcript|Out-Null; exit 2
}
$v0=(Get-Volume -DriveLetter C).SizeRemaining
Step ("Free before: {0:N1} GB" -f ($v0/1GB))

Step '1. Stop update services'
Stop-Service -Name wuauserv,bits,cryptsvc,msiserver -Force

Step '2. DISM StartComponentCleanup /ResetBase (slow, 5-20 min)'
Dism.exe /Online /Cleanup-Image /StartComponentCleanup /ResetBase | Select-String -Pattern 'error|Error|complet|correctamente' | ForEach-Object { $_.Line }
Step ("DISM rc={0}" -f $LASTEXITCODE)

Step '3. NGEN executeQueuedItems (precompile .NET for clones)'
foreach($fw in 'Framework64','Framework'){ foreach($ver in 'v4.0.30319','v2.0.50727'){
  $ng="$env:WINDIR\Microsoft.NET\$fw\$ver\ngen.exe"
  if(Test-Path $ng){ Step ("  ngen $fw $ver"); & $ng executeQueuedItems /nologo /silent | Out-Null }
}}

Step '4. SoftwareDistribution + catroot2 + BITS db'
Remove-Item 'C:\Windows\SoftwareDistribution\Download\*'  -Recurse -Force
Remove-Item 'C:\Windows\SoftwareDistribution\DataStore\*' -Recurse -Force
Remove-Item 'C:\Windows\SoftwareDistribution\ReportingEvents.log' -Force
Remove-Item 'C:\Windows\System32\catroot2\*' -Recurse -Force
Remove-Item 'C:\ProgramData\Microsoft\Network\Downloader\qmgr*' -Force

Step '5. Delivery Optimization cache'
try { Delete-DeliveryOptimizationCache -Force -ErrorAction Stop } catch {
  Remove-Item 'C:\Windows\ServiceProfiles\NetworkService\AppData\Local\Microsoft\Windows\DeliveryOptimization\Cache\*' -Recurse -Force }

Step '6. VSS shadow copies'
vssadmin.exe delete shadows /all /quiet | Out-Null

Step '7. Temp / WER / Prefetch / INetCache / thumbs / PS caches (all users)'
Remove-Item "$env:WINDIR\Temp\*"     -Recurse -Force
Remove-Item "$env:WINDIR\Prefetch\*" -Recurse -Force
Remove-Item 'C:\ProgramData\Microsoft\Windows\WER\*' -Recurse -Force
Get-ChildItem 'C:\Users' -Directory | ForEach-Object {
  $u=$_.FullName
  Remove-Item "$u\AppData\Local\Temp\*"                                    -Recurse -Force
  Remove-Item "$u\AppData\Local\Microsoft\Windows\INetCache\*"             -Recurse -Force
  Remove-Item "$u\AppData\Local\Microsoft\Windows\WER\*"                   -Recurse -Force
  Remove-Item "$u\AppData\Local\Microsoft\Windows\Explorer\thumbcache_*.db" -Force
  Remove-Item "$u\AppData\Local\Microsoft\Windows\PowerShell\ModuleAnalysisCache" -Force
  Remove-Item "$u\AppData\Roaming\Microsoft\Windows\PowerShell\PSReadLine\ConsoleHost_history.txt" -Force
  Remove-Item "$u\AppData\Local\CrashDumps\*" -Recurse -Force
}

Step '8. Servicing/update logs + memory dumps'
foreach($d in 'CBS','DISM','WindowsUpdate','MoSetup','NetSetup'){ Remove-Item "C:\Windows\Logs\$d\*" -Recurse -Force }
Remove-Item 'C:\Windows\MEMORY.DMP' -Force
Remove-Item 'C:\Windows\Minidump\*' -Recurse -Force
Remove-Item 'C:\Windows\LiveKernelReports\*' -Recurse -Force

Step '9. winget caches'
Get-ChildItem 'C:\Users\*\AppData\Local\Packages\Microsoft.DesktopAppInstaller_*' -Directory | ForEach-Object {
  Remove-Item "$($_.FullName)\LocalCache\*"              -Recurse -Force
  Remove-Item "$($_.FullName)\LocalState\DiagOutputDir\*" -Recurse -Force
}
Remove-Item 'C:\Windows\Temp\WinGet\*' -Recurse -Force

Step '10. Recycle Bin'
Clear-RecycleBin -Force

Step '11. Hibernation off'
powercfg.exe /hibernate off

Step '12. Clear event logs'
Get-WinEvent -ListLog * | Where-Object { $_.RecordCount -gt 0 -and $_.IsEnabled } |
  ForEach-Object { wevtutil.exe cl $_.LogName 2>$null }

Step '13. Optimize-Volume (defrag + retrim -> host reclaims via discard=on)'
Optimize-Volume -DriveLetter C -Defrag
Optimize-Volume -DriveLetter C -ReTrim

Step '14. sdelete -z (zero free space, fills C: temporarily - normal)'
if(-not (Test-Path "$env:WINDIR\System32\sdelete64.exe")){
  $tmp="$env:WINDIR\Temp\SDelete.zip"
  Invoke-WebRequest -Uri 'https://download.sysinternals.com/files/SDelete.zip' -OutFile $tmp
  Expand-Archive -Path $tmp -DestinationPath "$env:WINDIR\System32" -Force; Remove-Item $tmp
}
$env:TEMP="$env:WINDIR\Temp"; $env:TMP="$env:WINDIR\Temp"
& "$env:WINDIR\System32\sdelete64.exe" -accepteula -nobanner -z C: 2>&1 | Select-Object -Last 3

Step '15. Final retrim (release the zeroed blocks to the host)'
Optimize-Volume -DriveLetter C -ReTrim

$v1=(Get-Volume -DriveLetter C).SizeRemaining
Step ("Free after: {0:N1} GB (delta in-guest {1:+0.0;-0.0} GB)" -f ($v1/1GB), (($v1-$v0)/1GB))
Step 'Sanity: WU service + policy'
Get-Service wuauserv | Format-Table Name,Status,StartType -AutoSize | Out-String -Width 100
Get-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU' | Select-Object NoAutoUpdate,AUOptions | Out-String
Step 'CLEANUP COMPLETE rc=0'
Stop-Transcript | Out-Null

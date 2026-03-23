param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$AllArgs
)

$ifeoKey = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\StrategyQuantX_nocheck.exe"

if ($AllArgs.Count -lt 1) { exit 2 }

$target = $AllArgs[0]
$forwardArgs = @()
if ($AllArgs.Count -gt 1) { $forwardArgs = $AllArgs[1..($AllArgs.Count - 1)] }

if (-not (Test-Path $target)) { exit 3 }

$mutex = New-Object System.Threading.Mutex($false, "Global\SQX_IFEO_MUTEX")
$null = $mutex.WaitOne()

$debuggerValue = $null
try {
    $debuggerValue = (Get-ItemProperty -Path $ifeoKey -Name Debugger -ErrorAction Stop).Debugger
    Remove-ItemProperty -Path $ifeoKey -Name Debugger -ErrorAction Stop

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $target
    $psi.WorkingDirectory = Split-Path -Parent $target
    $psi.UseShellExecute = $false
    foreach ($a in $forwardArgs) { [void]$psi.ArgumentList.Add($a) }

    $psi.Environment["JAVA_TOOL_OPTIONS"] = "-Djava.awt.headless=true"

    $p = [System.Diagnostics.Process]::Start($psi)
    if (-not $p) { throw "Process.Start returned null" }

    exit 0
}
catch {
    exit 111
}
finally {
    try {
        if ($debuggerValue) {
            Set-ItemProperty -Path $ifeoKey -Name Debugger -Value $debuggerValue -ErrorAction Stop
        }
    } catch {}
    try { $mutex.ReleaseMutex() | Out-Null } catch {}
    $mutex.Dispose()
}
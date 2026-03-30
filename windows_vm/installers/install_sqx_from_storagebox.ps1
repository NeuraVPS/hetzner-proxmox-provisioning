#Requires -RunAsAdministrator
<#
.SYNOPSIS
  Maps the Hetzner Storage Box SMB share, extracts SQX_<Version>.zip to C:\SQX_<Version>, and adds desktop and Start Menu shortcuts (.lnk).

.DESCRIPTION
  Remote run (no local .ps1): iex (irm URL) does not pass arguments into this script's param block.
  Fetch the body and invoke it as a scriptblock so parameters bind correctly (PowerShell 7+ may use irm instead of Invoke-RestMethod):

  & ([scriptblock]::Create((Invoke-RestMethod -Uri 'https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/windows_vm/installers/install_sqx_from_storagebox.ps1' -UseBasicParsing))) -SmbPassword $env:STORAGEBOX_SMB_PASSWORD

  & ([scriptblock]::Create((Invoke-RestMethod -Uri 'https://raw.githubusercontent.com/NeuraVPS/hetzner-proxmox-provisioning/refs/heads/master/windows_vm/installers/install_sqx_from_storagebox.ps1' -UseBasicParsing))) -SmbPassword $env:STORAGEBOX_SMB_PASSWORD -Version 142

  Only use URLs and revisions you trust; this executes code from the network. #Requires Administrator.

.PARAMETER SmbPassword
  Password for the Storage Box SMB user (required).

.PARAMETER Version
  SQX build number: 142 or 143 only. Used for the zip name (SQX_<Version>.zip), install folder (C:\SQX_<Version>), and shortcut labels. Defaults to 143.

.EXAMPLE
  .\install_sqx_from_storagebox.ps1 -SmbPassword $env:STORAGEBOX_SMB_PASSWORD

.EXAMPLE
  .\install_sqx_from_storagebox.ps1 -SmbPassword $env:STORAGEBOX_SMB_PASSWORD -Version 142

.NOTES
  Run elevated. SmbPassword must be supplied every time (no default).
#>

param(
    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string]$SmbPassword,

    [ValidateSet('142', '143')]
    [string]$Version = '143'
)

$ErrorActionPreference = 'Stop'

$UncRoot = '\\u560363-sub1.your-storagebox.de\u560363-sub1'
$ZipName = "SQX_$Version.zip"
$ZipUnc  = Join-Path -Path $UncRoot -ChildPath $ZipName
$SmbUser = 'u560363-sub1'

function Connect-StorageBoxUnc {
    <#
    .NOTES
      Error 1312 (no logon session) hits New-SmbMapping and plain net use under QEMU guest agent / SYSTEM.
      New-SmbGlobalMapping is intended for machine-wide SMB access (services, SYSTEM).
      Fallback: cmdkey stores creds, then net use without inline password; then explicit net use variants.
    #>
    param(
        [Parameter(Mandatory)][string]$RemotePath,
        [Parameter(Mandatory)][string]$User,
        [Parameter(Mandatory)][string]$Password
    )
    if (Get-Command Remove-SmbGlobalMapping -ErrorAction SilentlyContinue) {
        Remove-SmbGlobalMapping -RemotePath $RemotePath -Force -ErrorAction SilentlyContinue
    }
    Remove-SmbMapping -RemotePath $RemotePath -Force -ErrorAction SilentlyContinue
    try { net use $RemotePath /delete /y 2>$null | Out-Null } catch {}

    $server = $null
    if ($RemotePath -match '^\\\\([^\\]+)\\') {
        $server = $Matches[1]
    }

    $errs = @()
    Import-Module SmbShare -ErrorAction SilentlyContinue | Out-Null

    if (Get-Command New-SmbGlobalMapping -ErrorAction SilentlyContinue) {
        try {
            New-SmbGlobalMapping -RemotePath $RemotePath -UserName $User -Password $Password -Persistent:$false -ErrorAction Stop | Out-Null
            return
        } catch {
            $errs += "New-SmbGlobalMapping: $($_.Exception.Message)"
        }
    } else {
        $errs += 'New-SmbGlobalMapping: cmdlet not available'
    }

    try {
        New-SmbMapping -RemotePath $RemotePath -UserName $User -Password $Password -Persistent:$false -ErrorAction Stop | Out-Null
        return
    } catch {
        $errs += "New-SmbMapping: $($_.Exception.Message)"
    }

    $cmdkey = Join-Path $env:SystemRoot 'System32\cmdkey.exe'
    if ($server -and (Test-Path -LiteralPath $cmdkey)) {
        $ck = Start-Process -FilePath $cmdkey -ArgumentList @("/add:$server", "/user:$User", "/pass:$Password") -Wait -NoNewWindow -PassThru
        try {
            if ($ck.ExitCode -eq 0) {
                $p = Start-Process -FilePath 'net.exe' -ArgumentList @('use', $RemotePath, '/persistent:no') -Wait -NoNewWindow -PassThru
                if ($p.ExitCode -eq 0) { return }
                $errs += "net use after cmdkey exit $($p.ExitCode)"
            } else {
                $errs += "cmdkey add exit $($ck.ExitCode)"
            }
        } finally {
            try {
                Start-Process -FilePath $cmdkey -ArgumentList @("/delete:$server") -Wait -NoNewWindow | Out-Null
            } catch { }
        }
    }

    $netVariants = @(
        @('use', $RemotePath, "/user:$User", $Password),
        @('use', $RemotePath, "/user:WORKGROUP\$User", $Password),
        @('use', $RemotePath, "/user:.\$User", $Password)
    )
    foreach ($na in $netVariants) {
        $p = Start-Process -FilePath 'net.exe' -ArgumentList $na -Wait -NoNewWindow -PassThru
        if ($p.ExitCode -eq 0) { return }
    }
    $errs += 'net use explicit variants failed'

    throw "SMB connect failed for $RemotePath. $($errs -join ' | ')"
}

function New-ShellShortcutLnk {
    <#
    .NOTES
      Start Menu "All apps" expects .lnk shell links; symlinks are often not listed.
      WScript.Shell CreateShortcut must run in an STA thread; PowerShell 7+ or non-interactive
      hosts may use MTA and produce shortcuts that do not behave like Explorer-created .lnk files.
      We delegate to Windows PowerShell 5.1 with -STA when available.
    #>
    param(
        [Parameter(Mandatory)][string]$ShortcutPath,
        [Parameter(Mandatory)][string]$TargetPath,
        [Parameter(Mandatory)][string]$WorkingDirectory
    )
    if (-not $ShortcutPath.EndsWith('.lnk', [System.StringComparison]::OrdinalIgnoreCase)) {
        $ShortcutPath = "$ShortcutPath.lnk"
    }
    $lnkFull = [System.IO.Path]::GetFullPath($ShortcutPath)
    $targetFull = [System.IO.Path]::GetFullPath((Resolve-Path -LiteralPath $TargetPath).Path)
    $workFull = [System.IO.Path]::GetFullPath((Resolve-Path -LiteralPath $WorkingDirectory).Path)

    $legacyNoExt = $lnkFull -replace '\.lnk$', ''
    $dir = Split-Path -Parent $lnkFull
    if (-not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    if (Test-Path -LiteralPath $lnkFull) {
        Remove-Item -LiteralPath $lnkFull -Force
    }
    if (-not [string]::Equals($legacyNoExt, $lnkFull, [System.StringComparison]::OrdinalIgnoreCase) -and (Test-Path -LiteralPath $legacyNoExt)) {
        Remove-Item -LiteralPath $legacyNoExt -Force -ErrorAction SilentlyContinue
    }

    $winPs = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if (Test-Path -LiteralPath $winPs) {
        # Embed paths as UTF-8 Base64 in the helper file only: no extra argv (spaces break Start-Process),
        # and no reliance on env inheritance into the child (RDP / some hosts drop inherited vars).
        $encLnk = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($lnkFull))
        $encTgt = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($targetFull))
        $encWrk = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($workFull))
        $helper = @"
`$ErrorActionPreference = 'Stop'
`$Lnk = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$encLnk'))
`$Tgt = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$encTgt'))
`$Wrk = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$encWrk'))
`$w = New-Object -ComObject WScript.Shell
`$s = `$w.CreateShortcut(`$Lnk)
`$s.TargetPath = `$Tgt
`$s.WorkingDirectory = `$Wrk
if (Test-Path -LiteralPath `$Tgt) { `$s.IconLocation = (`$Tgt + ',0') }
`$s.Save()
"@
        $tmp = Join-Path $env:TEMP ("neuravps-lnk-{0}.ps1" -f [guid]::NewGuid().ToString('N'))
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($tmp, $helper, $utf8NoBom)
        try {
            $p = Start-Process -FilePath $winPs -ArgumentList @(
                '-NoProfile', '-ExecutionPolicy', 'Bypass', '-STA', '-File', $tmp
            ) -Wait -NoNewWindow -PassThru
            if ($p.ExitCode -ne 0) {
                throw "Shell shortcut helper exited with code $($p.ExitCode)"
            }
        } finally {
            Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
        }
    } else {
        $shell = New-Object -ComObject WScript.Shell
        $sc = $shell.CreateShortcut($lnkFull)
        $sc.TargetPath = $targetFull
        $sc.WorkingDirectory = $workFull
        if (Test-Path -LiteralPath $targetFull) {
            $sc.IconLocation = "$targetFull,0"
        }
        $sc.Save()
    }

    if (-not (Test-Path -LiteralPath $lnkFull)) {
        throw "Shortcut was not created: $lnkFull"
    }
    $item = Get-Item -LiteralPath $lnkFull
    if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        throw "Shortcut path is a reparse point, not a shell .lnk: $lnkFull"
    }
    $bytes = [System.IO.File]::ReadAllBytes($lnkFull)
    if ($bytes.Length -lt 4) {
        throw "Shortcut file is empty or unreadable: $lnkFull"
    }
    $headerSize = [BitConverter]::ToUInt32($bytes, 0)
    if ($headerSize -ne 76) {
        throw "File is not a valid shell link (.lnk header size 76 expected): $lnkFull"
    }
}

$ExtractRoot = "C:\SQX_$Version"
$ExeTarget   = Join-Path -Path $ExtractRoot -ChildPath 'StrategyQuantX.exe'
$DesktopLink = "C:\Users\Public\Desktop\StrategyQuantX v$Version"
$StartMenuLink = "C:\ProgramData\Microsoft\Windows\Start Menu\Programs\StrategyQuantX v$Version"

try {
    Connect-StorageBoxUnc -RemotePath $UncRoot -User $SmbUser -Password $SmbPassword

    if (-not (Test-Path -LiteralPath $ZipUnc)) {
        throw "Zip not found at: $ZipUnc"
    }

    if (-not (Test-Path -LiteralPath $ExtractRoot)) {
        New-Item -ItemType Directory -Path $ExtractRoot | Out-Null
    }

    Expand-Archive -LiteralPath $ZipUnc -DestinationPath $ExtractRoot -Force

    if (-not (Test-Path -LiteralPath $ExeTarget)) {
        throw "Expected executable missing after extract: $ExeTarget (check zip layout)."
    }

    New-ShellShortcutLnk -ShortcutPath $DesktopLink -TargetPath $ExeTarget -WorkingDirectory $ExtractRoot
    New-ShellShortcutLnk -ShortcutPath $StartMenuLink -TargetPath $ExeTarget -WorkingDirectory $ExtractRoot
}
finally {
    if (Get-Command Remove-SmbGlobalMapping -ErrorAction SilentlyContinue) {
        Remove-SmbGlobalMapping -RemotePath $UncRoot -Force -ErrorAction SilentlyContinue
    }
    Remove-SmbMapping -RemotePath $UncRoot -Force -ErrorAction SilentlyContinue
}

Write-Host 'Done: extracted to' $ExtractRoot 'and shortcuts (.lnk) created.'

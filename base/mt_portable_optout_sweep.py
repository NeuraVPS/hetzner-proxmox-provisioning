#!/usr/bin/env python3
"""Find the MetaTrader boxes that violate hook gate 2, and unhook them.

Background
----------
`windows_vm/hooks/README.md` gate 2 already states the rule: **the MT hook
belongs only on boxes whose real data is portable.** It re-adds `/portable` to
every launch, so that an MT self-update wiping the flag off a shortcut, or a
`.ex5`/`.mq5` opened through its file association, cannot start a terminal
non-portable. Right and wanted where the data lives next to the executable.

On a box whose terminals really run from `%APPDATA%\\MetaQuotes\\Terminal\\
<hash>`, forcing portable points MetaTrader at its install directory instead,
so it opens with no account, no EAs and no charts. Nothing is deleted — the
customer simply sees a freshly-installed terminal and concludes they lost
everything (real case 2026-07-27, four live MT4 instances invisible).

The 2026-07-16 sweep applied the hook without that gate. This finds the boxes
where it should never have gone, and takes it back off them.

Removing the hook is per-terminal correct on its own, because the SHORTCUT
already encodes the right choice: one carrying `/portable` still launches
portable, one without it goes back to AppData. The hook only ever existed to
survive the flag being wiped — and on a non-portable box there is no flag to
survive, so nothing is lost by removing it.

Why the judgement can't live inside the hook
--------------------------------------------
The moment such a terminal is opened once under the forced flag, MetaTrader
writes a stub `config\\accounts.ini` into the install directory. From then on
that directory *looks* populated, so any launch-time heuristic that inspects
only the portable side keeps hiding the customer's data. The call has to be
made with the AppData side in view — and it only has to be made once.

Each AppData profile carries `origin.txt` naming the installation it belongs
to, so the pairing is read, not guessed. That is the same comparison gate 2
describes, done at fleet scale instead of by hand.

Decision (deliberately conservative — a wrong opt-out would *create* the
problem on a healthy machine, so every condition must hold):

    opt out install D  <=>  some AppData profile P with origin == D where
                            P has an accounts file
                            AND P has strictly more EAs than D
                            AND P has at least one terminal log

Usage
-----
    python3 mt_portable_optout_sweep.py vms.txt              # dry run, reports
    python3 mt_portable_optout_sweep.py vms.txt --unhook     # removes the hook
    python3 mt_portable_optout_sweep.py vms.txt --unhook --only 933,327

`vms.txt`: one `vmid node_ipv6 [email]` per line. Read-only unless --unhook.

`--unhook` deletes ONLY the IFEO `Debugger` value for the four MetaTrader
executables, and only where it points at our own launcher — a Debugger set by
anything else is left alone. It never stops or restarts a terminal: running
ones keep running, and the change takes effect at their next launch.
"""

import argparse
import base64
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor



# Emits one JSON object: the AppData profiles (with the install each belongs
# to) and the state of every installation directory they point at.
COLLECT_PS = r'''
$ErrorActionPreference='SilentlyContinue'
function Probe($dir) {
  $acc = (Test-Path (Join-Path $dir 'config\accounts.ini')) -or
         (Test-Path (Join-Path $dir 'config\accounts.dat'))
  $eas = (Get-ChildItem (Join-Path $dir 'MQL4\Experts'),(Join-Path $dir 'MQL5\Experts') `
          -Recurse -Include *.ex4,*.ex5 -EA SilentlyContinue).Count
  $log = Get-ChildItem (Join-Path $dir 'logs\*.log') -EA SilentlyContinue |
         Sort-Object LastWriteTime -Descending | Select-Object -First 1
  [pscustomobject]@{
    exists   = (Test-Path $dir)
    accounts = [bool]$acc
    eas      = [int]$eas
    lastLog  = $(if ($log) { $log.LastWriteTime.ToString('yyyy-MM-dd') } else { $null })
  }
}

$profiles = @()
$installs = @{}
foreach ($u in Get-ChildItem C:\Users -Directory -EA SilentlyContinue) {
  $root = Join-Path $u.FullName 'AppData\Roaming\MetaQuotes\Terminal'
  if (-not (Test-Path $root)) { continue }
  foreach ($t in Get-ChildItem $root -Directory -EA SilentlyContinue) {
    $origin = (Get-Content (Join-Path $t.FullName 'origin.txt') -EA SilentlyContinue | Select-Object -First 1)
    if (-not $origin) { continue }
    $origin = $origin.Trim()
    $p = Probe $t.FullName
    $profiles += [pscustomobject]@{
      hash = $t.Name; origin = $origin
      accounts = $p.accounts; eas = $p.eas; lastLog = $p.lastLog
    }
    if (-not $installs.ContainsKey($origin)) { $installs[$origin] = (Probe $origin) }
  }
}
$hook = [bool]((Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\terminal.exe' -EA SilentlyContinue).Debugger) -or
        [bool]((Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\terminal64.exe' -EA SilentlyContinue).Debugger)
[pscustomobject]@{
  hook = $hook
  running = (Get-Process terminal,terminal64 -EA SilentlyContinue).Count
  profiles = $profiles
  installs = $installs
  complete = $true
} | ConvertTo-Json -Depth 6 -Compress
'''


def _encoded(ps: str) -> str:
    return base64.b64encode(ps.encode("utf-16-le")).decode()


def guest_exec(node_ip: str, vmid: str, ps: str, timeout: int = 150):
    """Run PowerShell in the guest through the BASE we are already on."""
    cmd = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=8",
           "-o", "BatchMode=yes", f"root@{node_ip}",
           f"qm guest exec {vmid} --timeout {timeout} -- "
           f"powershell -EncodedCommand {_encoded(ps)} 2>/dev/null"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 60)
    return json.loads(out.stdout)


def norm(p) -> str:
    return (p or "").strip().strip('"').rstrip("\\").lower()


def decide(data: dict):
    """Installs whose data really lives in AppData. See the module docstring."""
    installs = {norm(k): v for k, v in (data.get("installs") or {}).items()}
    out = {}
    for prof in data.get("profiles") or []:
        origin = norm(prof.get("origin"))
        inst = installs.get(origin)
        if not origin or not inst or not inst.get("exists"):
            continue
        if not prof.get("accounts"):
            continue
        if not prof.get("lastLog"):
            continue
        if int(prof.get("eas") or 0) <= int(inst.get("eas") or 0):
            continue
        out[origin] = {
            "profileEas": prof.get("eas"), "installEas": inst.get("eas"),
            "profileLastLog": prof.get("lastLog"), "installLastLog": inst.get("lastLog"),
            "hash": prof.get("hash"),
        }
    return out


UNHOOK_PS = r'''
$ErrorActionPreference='SilentlyContinue'
$base = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options'
$done = @()
foreach ($exe in 'terminal.exe','terminal64.exe','metaeditor.exe','metaeditor64.exe') {
  $p = Join-Path $base $exe
  if (-not (Test-Path $p)) { continue }
  $dbg = (Get-ItemProperty $p).Debugger
  if ([string]::IsNullOrEmpty($dbg)) {
    # Inert leftover: the key with no Debugger intercepts nothing, but it is a
    # dead interception point in the launch path. Drop it if it holds nothing
    # else, so the box is genuinely clean.
    $props = (Get-Item $p).Property | Where-Object { $_ -ne 'Debugger' }
    if (-not $props -and -not (Get-Item $p).SubKeyCount) { Remove-Item $p -Recurse -Force; $done += "$exe=removed-empty-key" }
    else { $done += "$exe=left-other-values" }
    continue
  }
  if ($dbg -match 'mt_hook_launcher\.vbs') {
    Remove-ItemProperty -Path $p -Name Debugger -Force
    $props = (Get-Item $p).Property
    if (-not $props -and -not (Get-Item $p).SubKeyCount) { Remove-Item $p -Recurse -Force }
    $done += "$exe=unhooked"
  } else {
    # Someone else's debugger — not ours to touch.
    $done += "$exe=FOREIGN:$dbg"
  }
}
if ($done.Count -eq 0) { 'no-mt-hook-present' } else { $done -join ' ' }
'''


def unhook(node_ip: str, vmid: str) -> str:
    """Remove our MT hook from this VM's launch path. Never touches terminals."""
    return (guest_exec(node_ip, vmid, UNHOOK_PS).get("out-data") or "").strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("vmlist")
    ap.add_argument("--unhook", action="store_true",
                    help="remove our MT hook from the affected VMs "
                         "(default: report only)")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--only", help="restrict to these comma-separated vmids")
    args = ap.parse_args()

    rows = [l.split() for l in open(args.vmlist) if l.strip()]
    if args.only:
        keep = {v.strip() for v in args.only.split(",")}
        rows = [r for r in rows if r[0] in keep]

    def run(row):
        vmid, ip = row[0], row[1]
        email = row[2] if len(row) > 2 else "?"
        try:
            data = guest_exec(ip, vmid, COLLECT_PS)
            payload = json.loads(data.get("out-data") or "{}")
        except Exception as exc:
            return vmid, ip, email, None, f"{type(exc).__name__}"
        # `complete` is emitted last, so its absence means the probe died
        # part-way and the profile list is short — which would silently
        # classify an affected VM as clean and skip a customer. Seen for real:
        # vm327's PowerShell hit an OutOfMemoryException on a busy MT box.
        # A partial read must be reported as a failure, never as "nothing here".
        if not payload.get("complete"):
            return vmid, ip, email, None, "incomplete probe (guest-side failure)"
        if isinstance(payload.get("installs"), list):   # PS emits [] when empty
            payload["installs"] = {}
        return vmid, ip, email, payload, None

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(run, rows))

    failed = [r for r in results if r[3] is None]
    affected = []
    for vmid, ip, email, payload, _ in results:
        if payload is None:
            continue
        opts = decide(payload)
        if opts:
            affected.append((vmid, ip, email, payload, opts))

    print(f"VMs probed        : {len(rows)}   unreachable/failed: {len(failed)}")
    print(f"VMs needing the hook removed: {len(affected)}")
    if failed:
        # Never let these blend into "clean": a VM we could not read is a VM we
        # have not cleared, and it has to be chased rather than assumed fine.
        print("\n⚠ NOT ASSESSED — re-run these, do not treat them as clean:")
        for vmid, _ip, email, _p, why in failed:
            print(f"   vm{vmid:<6} {email:<38} {why}")
    print()
    for vmid, _ip, email, payload, opts in sorted(
            affected, key=lambda t: -sum(o["profileEas"] for o in t[4].values())):
        tot = sum(o["profileEas"] for o in opts.values())
        print(f"  vm{vmid:<6} {email:<38} installs={len(opts):<2} EAs hidden={tot:<5} "
              f"terminals running={payload.get('running')}")
        for d, o in sorted(opts.items()):
            print(f"       {d:<34} appdata EAs={o['profileEas']:<4} "
                  f"(install has {o['installEas']}), last log {o['profileLastLog']}")

    if not args.unhook:
        print("\n(dry run — nothing changed; pass --unhook to remove the hook)")
        return 0

    print("\nunhooking…")
    failures = 0
    for vmid, ip, email, _payload, _opts in affected:
        res = unhook(ip, vmid)
        if "FOREIGN" in res or not res:
            failures += 1
        print(f"  vm{vmid:<6} {email:<38} {res or '(no output)'}")
    print(f"\ndone: {len(affected) - failures} clean, {failures} needing a look")
    return 0


if __name__ == "__main__":
    sys.exit(main())

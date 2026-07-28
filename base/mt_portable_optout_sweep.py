#!/usr/bin/env python3
"""Decide, once per VM, which MetaTrader installs must NOT be forced portable.

Background
----------
`windows_vm/hooks/mt_hook_launcher.vbs` re-adds `/portable` to every MT launch
so that an MT self-update wiping the flag off a shortcut, or a `.ex5`/`.mq5`
opened through its file association, can never start a terminal in
non-portable mode. That invariant is correct and wanted for every installation
whose data has always lived next to the executable.

It is wrong for installations that predate the hook and ran non-portable at
some point: their real data sits in `%APPDATA%\\MetaQuotes\\Terminal\\<hash>`,
and forcing portable points the terminal at its install directory instead, so
it opens with no account, no EAs and no charts. Nothing is lost, but the
customer sees a freshly-installed terminal (real case 2026-07-27: 238 EAs
across four live MT4 instances, invisible).

Why a list instead of a check inside the hook
---------------------------------------------
The moment such a terminal is opened once under the forced flag, MetaTrader
writes a stub `config\\accounts.ini` into the install directory. From then on
that directory *looks* populated, so any launch-time heuristic that inspects
only the portable side keeps hiding the customer's data. The call has to be
made with the AppData side in view — and it only has to be made once.

Each AppData profile carries `origin.txt` naming the installation it belongs
to, so the pairing is read, not guessed.

Decision (deliberately conservative — a wrong opt-out would *create* the
problem on a healthy machine, so every condition must hold):

    opt out install D  <=>  some AppData profile P with origin == D where
                            P has an accounts file
                            AND P has strictly more EAs than D
                            AND P has at least one terminal log

Usage
-----
    python3 mt_portable_optout_sweep.py vms.txt            # dry run, reports
    python3 mt_portable_optout_sweep.py vms.txt --apply    # writes the lists

`vms.txt`: one `vmid node_ipv6 [email]` per line. Read-only unless --apply.
"""

import argparse
import base64
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

OPTOUT_PATH = r"C:\ProgramData\NeuraVPS\mt_portable_optout.txt"

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


def write_optout(node_ip: str, vmid: str, dirs):
    body = "\n".join([
        "# NeuraVPS - installations that must NOT be forced into portable mode.",
        "# Their real MetaTrader data lives in %APPDATA%\\MetaQuotes\\Terminal.",
        "# Generated by base/mt_portable_optout_sweep.py. One directory per line.",
        *sorted(dirs),
        "",
    ])
    b64 = base64.b64encode(body.encode("utf-8")).decode()
    ps = (
        "$ErrorActionPreference='Stop'\n"
        "New-Item -ItemType Directory -Force -Path 'C:\\ProgramData\\NeuraVPS' | Out-Null\n"
        f"$b='{b64}'\n"
        f"[IO.File]::WriteAllBytes('{OPTOUT_PATH}',[Convert]::FromBase64String($b))\n"
        f"'WROTE {{0}} bytes' -f (Get-Item '{OPTOUT_PATH}').Length\n"
    )
    return guest_exec(node_ip, vmid, ps).get("out-data", "").strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("vmlist")
    ap.add_argument("--apply", action="store_true",
                    help="write the list on the affected VMs (default: report only)")
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
    print(f"VMs needing an opt-out list: {len(affected)}")
    print()
    for vmid, _ip, email, payload, opts in sorted(
            affected, key=lambda t: -sum(o["profileEas"] for o in t[4].values())):
        tot = sum(o["profileEas"] for o in opts.values())
        print(f"  vm{vmid:<6} {email:<38} installs={len(opts):<2} EAs hidden={tot:<5} "
              f"terminals running={payload.get('running')}")
        for d, o in sorted(opts.items()):
            print(f"       {d:<34} appdata EAs={o['profileEas']:<4} "
                  f"(install has {o['installEas']}), last log {o['profileLastLog']}")

    if not args.apply:
        print("\n(dry run — nothing written; pass --apply to write the lists)")
        return 0

    print("\napplying…")
    for vmid, ip, email, _payload, opts in affected:
        print(f"  vm{vmid}: {write_optout(ip, vmid, opts.keys())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

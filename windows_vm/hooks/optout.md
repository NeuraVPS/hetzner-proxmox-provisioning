# Hook opt-out — machines a sweep must not touch

## Why this exists

On 2026-08-02 a fleet sweep re-applied the SQX v144 hook to **vm 998**. That
machine had had the hook **deliberately removed on 28 July** because it broke
StrategyQuant there. The sweep had no way to know, so it "fixed" a machine that
was already correct, and the customer wrote in for the second time about the
same fault. He was right to be annoyed: we had told him it was resolved.

Gates 1–4 in [`README.md`](README.md) answer *"does this machine's software
shape call for the hook?"*. They cannot answer *"has a human already decided
this machine must not have it?"* — that is a different question, and no amount
of probing the disk will answer it.

## The file

    C:\ProgramData\NeuraVPS\hook_optout.txt

One hook id per line. `#` starts a comment. Blank lines ignored. Ids:

| id | means |
|---|---|
| `sqx143` | never wire `StrategyQuantX_nocheck.exe` |
| `sqx144` | never wire `StrategyQuantX.exe` |
| `mt`     | never wire the four MetaTrader executables |
| `*`      | never wire any hook on this machine |

**A missing file means no opt-outs** — exactly today's behaviour — so a freshly
provisioned VM needs no extra state and the template does not have to ship it.
That is the same property the MT `/portable` opt-out was built on
(`mt_portable_optout.txt`), deliberately: one convention, not two.

Always write a comment saying **who decided and why**. A bare id is
indistinguishable from a mistake six weeks later, and the next person will be
tempted to "clean it up".

    # 2026-08-03 — SQX v144 hook fork-bombs on this box: 90 wscript in ~33 s
    # and SQX never started (customer reported twice). Removed 28 Jul, and a
    # sweep put it back on 2 Aug. Do not re-apply until the cause is found.
    sqx144

## Every sweep MUST read it first

This is the half that actually matters. A marker nobody consults is decoration —
vm 998 was broken by the *sweep*, not by a hook misreading anything.

Paste this into any sweep before it decides to wire a hook:

```powershell
# Returns $true when this machine has opted out of $hookId.
function Test-HookOptOut([string]$hookId) {
    $f = 'C:\ProgramData\NeuraVPS\hook_optout.txt'
    if (-not (Test-Path $f)) { return $false }          # no file = no opt-outs
    foreach ($line in (Get-Content $f -ErrorAction SilentlyContinue)) {
        $t = ($line -split '#')[0].Trim()
        if ($t -eq '*' -or $t -ieq $hookId) { return $true }
    }
    return $false
}

if (Test-HookOptOut 'sqx144') { 'SKIP sqx144 (opt-out)'; return }
```

Fail **closed on ambiguity, open on absence**: an unreadable file is not an
opt-out (a fresh VM would otherwise silently lose its protection), but any line
that parses to the id — or to `*` — stops the sweep.

## Reading the fleet's opt-outs

Nothing central to keep in sync: the decision travels with the machine, on the
machine. To see who has opted out of what, sweep for the file:

```powershell
if (Test-Path 'C:\ProgramData\NeuraVPS\hook_optout.txt') {
    (Get-Content 'C:\ProgramData\NeuraVPS\hook_optout.txt') -join ' | '
} else { '(none)' }
```

A central list was considered and rejected: it drifts the moment a VM is
rebuilt, migrated or restored from backup, and the failure mode of a stale
central list is the exact bug this is meant to prevent.

## A different question: `mt_portable_optout.txt`

`hook_optout.txt` above answers *"should this machine have the hook wired at
all?"* — a per-**machine** decision, checked by a sweep before it touches the
registry. `mt_portable_optout.txt` answers a narrower question that only the
MT hook needs: *given the hook IS wired on this machine, should THIS ONE
installation be forced into `/portable`?*

The reason it has to be separate: a single box can carry several MT
installations (our own `C:\MetaTrader\...` plus one or more broker installs
under `C:\Program Files\...`), and MetaTrader installed outside our tree
redirects its data to `%APPDATA%\MetaQuotes\Terminal\<hash>` **by design**.
Forcing `/portable` on that install hides the customer's account and EAs even
though the box, as a whole, correctly has the hook. A per-machine opt-out
would be too blunt: it would either force portable on the whole box (hiding
that one broker install) or drop the hook everywhere (losing the
self-update/file-association protection for the installs that *are*
portable).

        C:\ProgramData\NeuraVPS\mt_portable_optout.txt

One **installation directory** per line (the folder holding `terminal64.exe`
etc., e.g. `C:\Program Files\Darwinex MetaTrader 5`), `#` comments and blank
lines ignored, matched case-insensitively with trailing backslashes stripped.
Read by `mt_hook_launcher.vbs` itself — no PowerShell snippet to paste, this
one lives inside the VBS because the decision depends on which installation
the launch resolved to (`ResolveMT5Target`), which only the launcher knows.

**A missing file means no opt-outs — exactly the behaviour before this
existed — so a freshly provisioned VM needs no extra state.** Same convention
as `hook_optout.txt`, deliberately: one pattern, not two.

Written by a fleet sweep from the AppData side (matching each roaming profile
to its installation via `origin.txt`, then comparing EA counts / accounts
file / terminal logs against the install directory — see
`base/mt_portable_optout_sweep.py` for that comparison). **As of 2026-09-14
no sweep writes this file yet**; the VBS reads it unconditionally, so the
file's absence is not a gap, it is the documented default.

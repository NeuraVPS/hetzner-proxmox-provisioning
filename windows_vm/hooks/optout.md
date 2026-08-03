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

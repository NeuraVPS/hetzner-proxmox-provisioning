# Daily explorer.exe /factory orphan sweep (runs on a BASE, currently b0)

Closes the `explorer.exe /factory,{75dff2b7-6936-4c06-a8bb-676a7b00b24b}
-Embedding` hosts that Windows leaves behind when a customer opens a folder
from MetaTrader (or any non-Explorer program) — see
`memory/neuravps-explorer-factory-fuga-ram.md` for the full investigation
(40% of VMs affected, ~9,200 processes, ~227 GB resident fleet-wide) and the
module docstring in `run_remotes/neuravps-explorer-sweep.py` for the design
rationale (why a sibling daemon and not inside welcome-boost, why every
result must carry our own marker, why the cap exists).

Install / reinstall after a base rebuild:
```
install -m755 run_remotes/neuravps-explorer-sweep.py /usr/local/sbin/neuravps-explorer-sweep.py
install -m644 base/systemd/neuravps-explorer-sweep.{service,timer} /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now neuravps-explorer-sweep.timer
```
Needs: `/etc/firebase-credentials.json`, `/var/lib/base-nat/pve_nodes.json`
(both already present on b0/b1 for the other daemons), SSH from this BASE to
every node (already required by every other `run_remotes/*` script).

**Kill-switch: Firestore `config/explorerSweep`
`{enabled, dryRun, maxVmsPerRun, workers}`. Doc absent or `enabled != true` =
OFF — this is a brand-new mechanism that kills customer-session processes,
so it ships disabled by default and stays that way until explicitly turned
on.**

Rollout sequence (do not skip steps):
1. `enabled: true, dryRun: true` — first real runs only count and journal,
   never call `Stop-Process`. Watch `explorer_sweep_runs/` for a few days:
   `totalFound` should land near the ~9,200-process fleet estimate the first
   time, then trail off fast (only new leaks in the loop). `unreachableByReason`
   should be small and boringly explained (a handful of `guest-timeout`/
   `no-agent`, not thousands).
2. Once those counts look sane, flip `dryRun: false`. `maxVmsPerRun` (default
   250 if unset) caps how many VMs get the *kill*-enabled payload per run —
   counting still covers every candidate regardless, so you keep full
   visibility on the backlog while ramping. Raise it once a few real runs
   have gone clean.
3. `workers` (default 24) overrides the SSH fan-out concurrency if a run is
   taking too long or too short.

Journal: `journalctl -t neuravps-explorer-sweep` on the base; per-run summary
(counts + a `perVm` map of only the VMs with something to report) in
Firestore `explorer_sweep_runs/<YYYYMMDD-HHMM>`.

Runs once daily at 03:40 UTC via `neuravps-explorer-sweep.timer`, single
instance (same model as `neuravps-defrag` — install only on b0, not both
bases). `fcntl` lock (`/run/neuravps-explorer-sweep.lock`) prevents overlap
if a run ever takes past the next scheduled tick.

Tests: `python3 run_remotes/test_explorer_sweep.py` — pure parsing logic
only (no network), including a regression fixture for a real qemu-ga
exec-status cross-talk case reproduced live on 2026-09-12.

## Commit sample (2026-09-13, piggybacked on the same round-trip)

The same PowerShell payload above also reads
`Win32_OperatingSystem.TotalVirtualMemorySize`/`FreeVirtualMemory` (the
commit limit and free commit) for every measured VM — no extra process, no
extra schedule. `commitPct`/`commitChargeMb`/`commitLimitMb` land in the
run's `perVm` entries whenever a VM is included there; two rollups feed an
`/admin/salud` card via the fixed doc `explorer_sweep_runs/_latest`:
- **`highCommitVms`** — VMs at or above 90% commit (`HIGH_COMMIT_PCT`),
  worst first.
- **`couldNotStartProcess`** — VMs where `qm guest exec` could not even
  start a process (`guest-timeout` / `no-marker` / `ps-exitcode-*`). On a
  healthy guest that round-trip always gets some response, so this failure
  signature is itself the commit-exhaustion signal (see
  `memory/neuravps-guest-commit-limit-diagnosis.md`).

Neither list can affect who gets killed — the explorer-orphan filter and the
kill cap are untouched.

## Testing without touching the live install, the journal, or any VM's kill

```
--only-vmids 215,701 --force-dry-run --no-journal
```
- `--only-vmids` restricts the run to the given vmids (still resolved
  through the normal Firestore candidate list, so node lookup/exclusions
  are unchanged).
- `--force-dry-run` guarantees every candidate gets the count-only payload
  — `Stop-Process` is never sent, regardless of `config/explorerSweep.dryRun`.
- `--no-journal` skips both Firestore writes (`explorer_sweep_runs/<runId>`
  and `/_latest`).

Because the script only needs `/etc/firebase-credentials.json` and
`/var/lib/base-nat/pve_nodes.json` (both already on b0) plus SSH to the
nodes, this can be run from a plain checkout on b0 without installing
anything over `/usr/local/sbin/neuravps-explorer-sweep.py`:
```
scp run_remotes/neuravps-explorer-sweep.py root@b0:/root/neuravps-explorer-sweep-test.py
ssh root@b0 'python3 /root/neuravps-explorer-sweep-test.py --only-vmids 215,701 --force-dry-run --no-journal'
```

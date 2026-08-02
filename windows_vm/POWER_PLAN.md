# Windows power plan — must be **High performance** on every image

## The problem

Windows Server ships on the **Balanced** power scheme
(`381b4222-f694-41f0-9685-ff5bb260df2e`). On a multi-vCPU guest that scheme
enables **core parking**: idle logical processors are put to sleep and woken
only gradually as load arrives, and the minimum processor state is capped
(`PROCTHROTTLEMIN = 5%`).

That is close to worst-case for our workload. StrategyQuantX's builder is a
**short, bursty, all-cores-at-once** job. A large part of each burst is spent
waiting for Windows to unpark cores, and Windows re-parks them between phases.
The guest is not throttled by the host, by RAM, or by the plan — it simply
never gets its own cores awake in time.

## Evidence (2026-08-01 / 2026-08-02)

Customer Brent Harding, VPS E (20 vCPU / 48 GB), one day into the service,
opened with a refund + cancellation request: SQX Benchmark reported
**69,500 then 93,500** strategies/hour against the published VPS E figure of
100,000. Full diagnostics were clean — node at 12% of capacity, zero CPU/IO
pressure, guest seeing all 48 GB, heap correctly at 41 GB, no competing app.

The actual fault:

```
powercfg /getactivescheme  -> 381b4222… (Balanced)
'\Processor Information(*)\Parking Status'  -> 18 of 20 vCPUs PARKED at idle
PROCTHROTTLEMIN AC = 5%
```

After switching to **High performance** (`SCHEME_MIN`, `8c5e7fda-…`) — no
reboot, nothing interrupted — the same benchmark on the same machine returned
**102,175 and 102,031**. From ~76,000 to ~102,000: a **~34% recovery**, and
from below the published figure to above it. The customer withdrew the refund
and cancellation.

This was **not** a one-off. A VPS E provisioned from the current image on
2026-08-02 was checked minutes after first boot:

```
SCHEME  = 381b4222… (Balanced)
PARKED  = 16 of 22
```

So every multi-core VPS we ship is losing multi-core throughput out of the
box, on exactly the workload the plan is sold for, and the published per-plan
benchmark figures were measured on machines in that state or on different
hardware. Small `mt` boxes (2 vCPU) do not park — too few cores — so this
affects **VPS A–E**.

## The fix — bake it into the template

```powershell
# High performance: no core parking, minimum processor state 100%.
powercfg /setactive SCHEME_MIN

# Belt and braces: some images have the scheme present but throttled.
powercfg /setacvalueindex SCHEME_CURRENT SUB_PROCESSOR PROCTHROTTLEMIN 100
powercfg /setacvalueindex SCHEME_CURRENT SUB_PROCESSOR CPMINCORES 100   # 100% of cores unparked
powercfg /setactive SCHEME_CURRENT

# Never sleep / never turn the disk off on a server that must stay reachable.
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change disk-timeout-ac 0
powercfg /change monitor-timeout-ac 0
```

`SCHEME_MIN` is the well-known GUID alias for High performance and is present
on Windows Server 2025 by default, so no scheme has to be created.

## Verify (must pass on a freshly provisioned box)

```powershell
# 1. Active scheme must be High performance
(powercfg /getactivescheme) -match '8c5e7fda'          # -> True

# 2. No parked cores
@((Get-Counter '\Processor Information(*)\Parking Status').CounterSamples |
   Where-Object { $_.CookedValue -eq 1 }).Count          # -> 0

# 3. Minimum processor state at 100%
(powercfg /query SCHEME_CURRENT SUB_PROCESSOR PROCTHROTTLEMIN) -match '0x00000064'   # -> True
```

Check 2 is the one that matters — it is the number that moved the benchmark.
A scheme can read "High performance" while a stale per-scheme value still
parks cores, which is why the verify reads the live counter rather than the
configuration.

## Notes

- The change takes effect **immediately**; no reboot, and running workloads
  are not interrupted (verified live on a customer machine mid-session).
- It is safe to re-apply — `powercfg /setactive` is idempotent.
- Sysprep preserves the active power scheme, so setting it before the
  pre-sysprep cleanup (`prepare.md`) is enough; it does not need to be part of
  first-boot.
- Existing fleet: remediating already-provisioned boxes is a separate sweep
  (`powercfg /setactive SCHEME_MIN` via `qm guest exec`), not covered here.

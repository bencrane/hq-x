# USASpending MV Benchmark Baseline

Companion to `scripts/benchmark_usaspending_mvs.py`. Numbers below are the
reference baseline for human regression review. The benchmark script
asserts against absolute sanity ceilings (not baseline-relative), because
the prod DB shows 1.5-2x warm-2 variance on the monthly MV under normal
load, which would make a strict 25% band flap in CI. A >25% drift from
these baselines is still the policy — investigate by re-running a few
times, capturing `EXPLAIN ANALYZE`, and updating the baseline only if the
drift is an intentional trade-off (index change, MV reshape).

## Run metadata

- **Date (UTC):** 2026-04-18
- **Git SHA:** 428e41684414d15e93b254a1000dc2ec83012b44 (branch base `origin/main`)
- **Target:** prod (`DATABASE_URL` via Doppler `data-engine-x-api / prd`)
- **Slim MV row count:** ~95K UEIs (from build report)
- **Monthly MV row count:** ~15.1M rows (from DealBridge Track B refresh, 2026-04-16)
- **Command:**
  ```
  doppler run --project data-engine-x-api --config prd -- \
    python3 scripts/benchmark_usaspending_mvs.py
  ```

## Results (ms wall-clock; asserted on warm-2)

| # | Query                                      | Target  | Rows | Cold   | Warm-1 | Warm-2 | Ceiling | Pass |
|---|--------------------------------------------|---------|------|--------|--------|--------|---------|------|
| 1 | slim / top-500 12mo, sector 54             | slim    | 500  |  475.0 |   42.2 |   24.5 |   150   | ✅    |
| 2 | slim / top-500 12mo, sector 23             | slim    | 500  |  808.4 |   46.7 |   49.4 |   150   | ✅    |
| 3 | slim / top-500 12mo, no filter             | slim    | 500  |   27.2 |   19.8 |   21.9 |   150   | ✅    |
| 4 | slim / top-500 all-time, sector 54         | slim    | 500  |   35.4 |   22.0 |   29.7 |   150   | ✅    |
| 5 | monthly / 12mo rollup, sector 54           | monthly | 500  | 5586.4 |  268.9 |  257.4 |  2000   | ✅    |
| 6 | monthly / straddle, sector 54              | monthly | 500  |  252.5 |  223.4 |  221.7 |  2000   | ✅    |
| 7 | monthly / 36mo, sector 54                  | monthly | 500  |  802.3 |  531.1 |  484.6 |  3000   | ✅    |
| 8 | monthly / 3mo, sector 54                   | monthly | 500  |   95.5 |   88.1 |   87.7 |  1000   | ✅    |

Across repeat runs the monthly queries (Q5-Q8) show 1.5-2x warm-2 variance
under normal prod DB load; the slim queries are more stable (Q2 hovers at
45-55ms, other slim queries stay under 30ms). Ceilings above are ~3-6x
baseline to catch catastrophic regressions without flapping.

## Regression policy

A >25% regression in warm-2 vs. baseline for any query merits
investigation. Re-run 2-3 times to separate real regressions from shared-DB
noise. Capture `EXPLAIN ANALYZE` before concluding a plan change; update
this file only when a drift is an intentional trade-off (e.g., an index
change or MV reshape). The benchmark script's ceilings are the CI gate.

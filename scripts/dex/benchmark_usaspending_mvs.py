"""Benchmark suite for the paired USASpending entity-grain MVs.

Usage:
    doppler run --project data-engine-x-api --config prd -- \\
        python3 scripts/benchmark_usaspending_mvs.py

Runs 8 representative queries three times each (cold + 2 warm) and asserts
the second warm pass against an absolute sanity ceiling chosen to catch
catastrophic regressions (index loss, MV shape change, seq-scan fallback)
without flapping on normal prod DB load variance. Exits 1 on any failure.

Observed baselines and the 25%-regression policy are documented in
``scripts/BASELINE_USASPENDING_MVS.md``. Human inspection of warm-2 vs.
baseline is the authoritative regression check; this script's job is to
catch clearly-broken numbers in CI.

Targets ``entities.mv_usaspending_entity_grain_slim`` (hot path, tens of ms)
and ``entities.mv_usaspending_entity_naics_monthly`` (custom-window, <1s
typical). Uses psycopg directly with ``statement_timeout = 0`` so cold runs
don't trip the service's 2-minute timeout.
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import psycopg


@dataclass
class Query:
    idx: int
    name: str
    target: str
    sql: str
    params: tuple[Any, ...]
    baseline_ms: float
    threshold_ms: float
    cold_ms: float = 0.0
    warm1_ms: float = 0.0
    warm2_ms: float = 0.0
    rows: int = 0


# --- Slim hot-path queries (threshold 50ms warm-2) -----------------------------

SLIM_BY_12MO_SECTOR = """
    SELECT recipient_uei, recipient_name, total_obligations_12mo
    FROM entities.mv_usaspending_entity_grain_slim
    WHERE naics_sectors @> ARRAY[%s]
    ORDER BY total_obligations_12mo DESC NULLS LAST
    LIMIT 500
"""

SLIM_BY_12MO_NO_FILTER = """
    SELECT recipient_uei, recipient_name, total_obligations_12mo
    FROM entities.mv_usaspending_entity_grain_slim
    ORDER BY total_obligations_12mo DESC NULLS LAST
    LIMIT 500
"""

SLIM_BY_ALL_TIME_SECTOR = """
    SELECT recipient_uei, recipient_name, total_obligations_all_time
    FROM entities.mv_usaspending_entity_grain_slim
    WHERE naics_sectors @> ARRAY[%s]
    ORDER BY total_obligations_all_time DESC NULLS LAST
    LIMIT 500
"""

# --- Monthly MV queries --------------------------------------------------------

MONTHLY_12MO_WINDOW = """
    SELECT recipient_uei,
           SUM(month_obligations)::double precision AS total_obligated,
           SUM(month_txn_count) AS contract_count
    FROM entities.mv_usaspending_entity_naics_monthly
    WHERE naics_sector = %s
      AND year_month >= date_trunc('month', (CURRENT_DATE - INTERVAL '12 months'))
      AND year_month <= date_trunc('month', CURRENT_DATE)
    GROUP BY recipient_uei
    ORDER BY total_obligated DESC NULLS LAST
    LIMIT 500
"""

MONTHLY_STRADDLE = """
    -- Won 3-9 months ago but not last 3 months (timing-arbitrage shape)
    WITH recent AS (
        SELECT DISTINCT recipient_uei
        FROM entities.mv_usaspending_entity_naics_monthly
        WHERE naics_sector = %s
          AND year_month >= date_trunc('month', (CURRENT_DATE - INTERVAL '3 months'))
    ),
    mid AS (
        SELECT recipient_uei,
               SUM(month_obligations)::double precision AS total_obligated,
               SUM(month_txn_count) AS contract_count
        FROM entities.mv_usaspending_entity_naics_monthly
        WHERE naics_sector = %s
          AND year_month >= date_trunc('month', (CURRENT_DATE - INTERVAL '9 months'))
          AND year_month <  date_trunc('month', (CURRENT_DATE - INTERVAL '3 months'))
        GROUP BY recipient_uei
    )
    SELECT mid.*
    FROM mid
    LEFT JOIN recent USING (recipient_uei)
    WHERE recent.recipient_uei IS NULL
    ORDER BY total_obligated DESC NULLS LAST
    LIMIT 500
"""

MONTHLY_36MO_WINDOW = """
    SELECT recipient_uei,
           SUM(month_obligations)::double precision AS total_obligated,
           SUM(month_txn_count) AS contract_count
    FROM entities.mv_usaspending_entity_naics_monthly
    WHERE naics_sector = %s
      AND year_month >= date_trunc('month', (CURRENT_DATE - INTERVAL '36 months'))
      AND year_month <= date_trunc('month', CURRENT_DATE)
    GROUP BY recipient_uei
    ORDER BY total_obligated DESC NULLS LAST
    LIMIT 500
"""

MONTHLY_3MO_WINDOW = """
    SELECT recipient_uei,
           SUM(month_obligations)::double precision AS total_obligated,
           SUM(month_txn_count) AS contract_count
    FROM entities.mv_usaspending_entity_naics_monthly
    WHERE naics_sector = %s
      AND year_month >= date_trunc('month', (CURRENT_DATE - INTERVAL '3 months'))
      AND year_month <= date_trunc('month', CURRENT_DATE)
    GROUP BY recipient_uei
    ORDER BY total_obligated DESC NULLS LAST
    LIMIT 500
"""


def build_queries() -> list[Query]:
    # Baselines (warm-2) captured 2026-04-18; see scripts/BASELINE_USASPENDING_MVS.md.
    # thresholds are catastrophic-regression ceilings (~3x baseline) — they exist
    # to catch index loss or seq-scan fallback, not to microbenchmark. Investigate
    # warm-2 drift >25% from baseline by inspection; that band is too tight for
    # a shared prod DB but remains the documented policy.
    return [
        Query(1, "slim / top-500 12mo, sector 54",    "slim",    SLIM_BY_12MO_SECTOR,     ("54",),       baseline_ms=24.5,  threshold_ms=150),
        Query(2, "slim / top-500 12mo, sector 23",    "slim",    SLIM_BY_12MO_SECTOR,     ("23",),       baseline_ms=49.4,  threshold_ms=150),
        Query(3, "slim / top-500 12mo, no filter",    "slim",    SLIM_BY_12MO_NO_FILTER,  (),            baseline_ms=21.9,  threshold_ms=150),
        Query(4, "slim / top-500 all-time, sector 54","slim",    SLIM_BY_ALL_TIME_SECTOR, ("54",),       baseline_ms=29.7,  threshold_ms=150),
        Query(5, "monthly / 12mo rollup, sector 54",  "monthly", MONTHLY_12MO_WINDOW,     ("54",),       baseline_ms=257.4, threshold_ms=2000),
        Query(6, "monthly / straddle, sector 54",     "monthly", MONTHLY_STRADDLE,        ("54", "54"),  baseline_ms=221.7, threshold_ms=2000),
        Query(7, "monthly / 36mo, sector 54",         "monthly", MONTHLY_36MO_WINDOW,     ("54",),       baseline_ms=484.6, threshold_ms=3000),
        Query(8, "monthly / 3mo, sector 54",          "monthly", MONTHLY_3MO_WINDOW,      ("54",),       baseline_ms=87.7,  threshold_ms=1000),
    ]


def run_once(conn: psycopg.Connection, q: Query) -> tuple[float, int]:
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = 0")
        start = time.perf_counter()
        cur.execute(q.sql, q.params)
        rows = cur.fetchall()
        elapsed_ms = (time.perf_counter() - start) * 1000
    return elapsed_ms, len(rows)


def run_all(queries: list[Query]) -> int:
    dsn = os.environ["DEX_DB_URL_POOLED"]
    failures: list[str] = []
    with psycopg.connect(dsn) as conn:
        for q in queries:
            q.cold_ms, q.rows = run_once(conn, q)
            q.warm1_ms, _ = run_once(conn, q)
            q.warm2_ms, _ = run_once(conn, q)
            if q.warm2_ms > q.threshold_ms:
                failures.append(
                    f"Q{q.idx} ({q.name}): warm-2 {q.warm2_ms:.1f} ms > "
                    f"{q.threshold_ms:.0f} ms ceiling (baseline {q.baseline_ms:.1f} ms)"
                )
    print_report(queries, failures)
    return 1 if failures else 0


def print_report(queries: list[Query], failures: list[str]) -> None:
    print()
    print(f"{'#':>2}  {'Query':<45} {'Target':<8} {'Rows':>5} {'Cold':>8} {'Warm1':>8} {'Warm2':>8} {'Base':>7} {'Thr':>7}  Result")
    print("-" * 120)
    for q in queries:
        ok = q.warm2_ms <= q.threshold_ms
        status = "PASS" if ok else "FAIL"
        print(
            f"{q.idx:>2}  {q.name:<45} {q.target:<8} {q.rows:>5} "
            f"{q.cold_ms:>7.1f}ms {q.warm1_ms:>7.1f}ms {q.warm2_ms:>7.1f}ms "
            f"{q.baseline_ms:>6.1f}ms {q.threshold_ms:>6.1f}ms  {status}"
        )
    print()
    if failures:
        print("FAILURES:")
        for f in failures:
            print(f"  - {f}")
    else:
        print("All 8 queries PASSED their warm-2 thresholds.")


def main() -> int:
    queries = build_queries()
    return run_all(queries)


if __name__ == "__main__":
    sys.exit(main())

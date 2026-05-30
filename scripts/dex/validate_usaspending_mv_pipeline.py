"""End-to-end validation for the USASpending MV pipeline.

Usage:
    doppler run --project data-engine-x-api --config prd -- \\
        python3 scripts/validate_usaspending_mv_pipeline.py

Compares a narrow slice (NAICS sector 54, June 2025) across four layers:

    1. Raw     — entities.usaspending_contracts          (text columns, cast on read)
    2. Typed   — entities.mv_usaspending_contracts_typed
    3. Monthly — entities.mv_usaspending_entity_naics_monthly
    4. Slim    — entities.mv_usaspending_entity_grain_slim

Closes the loop the consumer-port equivalence test (validate_contract_signals_mv_port.py)
can't close: that test compares two paths that both derive from typed, so a
typed-layer bug would pass silently. This script anchors the pipeline against
the raw table.

Assertions:
    - Layers 1/2/3 SUM(obligations) within 0.5% (wider than the consumer-port
      0.1% to accommodate the known raw->typed multi-extract dedup noise).
    - Layers 1/2/3 COUNT within 1%.
    - Slim UEI set is a superset of the raw UEI set for the slice.

Exits 1 on any failure; prints per-layer numbers.
"""
from __future__ import annotations

import os
import sys
from typing import Any

import psycopg
from psycopg.rows import dict_row

SLICE_SECTOR = "54"
SLICE_FROM = "2025-06-01"
SLICE_TO = "2025-06-30"

SUM_TOLERANCE = 0.005   # 0.5%
COUNT_TOLERANCE = 0.01  # 1%


RAW_SQL = """
    -- Match the typed MV's dedup semantic:
    --   SELECT DISTINCT ON (contract_transaction_unique_key) ... ORDER BY ..., extract_date DESC
    --
    -- Optimization: a contract_transaction_unique_key points at an immutable
    -- historical transaction; its action_date and naics_code are stable across
    -- extracts. So we can push date + sector + UEI filters INSIDE the dedup
    -- CTE without changing the result — and avoid a DISTINCT ON over ~23M rows.
    WITH scoped AS (
        -- action_date is TEXT (ISO-8601 sorts lexicographically, so the B-tree
        -- on action_date is usable with text comparison).
        SELECT
            contract_transaction_unique_key,
            extract_date,
            recipient_uei,
            NULLIF(federal_action_obligation, '')::numeric AS federal_action_obligation,
            naics_code
        FROM entities.usaspending_contracts
        WHERE contract_transaction_unique_key IS NOT NULL
          AND contract_transaction_unique_key <> ''
          AND action_date >= %(date_from)s
          AND action_date <= %(date_to)s
          AND LEFT(naics_code, 2) = %(sector)s
          AND recipient_uei IS NOT NULL
          AND recipient_uei <> ''
    ),
    dedup AS (
        SELECT DISTINCT ON (contract_transaction_unique_key)
            recipient_uei,
            federal_action_obligation
        FROM scoped
        ORDER BY contract_transaction_unique_key, extract_date DESC
    )
    SELECT
        recipient_uei,
        SUM(federal_action_obligation) AS total_obligations,
        COUNT(*) AS txn_count
    FROM dedup
    GROUP BY recipient_uei
"""

TYPED_SQL = """
    SELECT
        recipient_uei,
        SUM(federal_action_obligation) AS total_obligations,
        COUNT(*) AS txn_count
    FROM entities.mv_usaspending_contracts_typed
    WHERE action_date >= %(date_from)s::date
      AND action_date <= %(date_to)s::date
      AND LEFT(naics_code, 2) = %(sector)s
      AND recipient_uei IS NOT NULL
      AND recipient_uei <> ''
    GROUP BY recipient_uei
"""

MONTHLY_SQL = """
    SELECT
        recipient_uei,
        SUM(month_obligations) AS total_obligations,
        SUM(month_txn_count)   AS txn_count
    FROM entities.mv_usaspending_entity_naics_monthly
    WHERE year_month = date_trunc('month', %(date_from)s::date)
      AND naics_sector = %(sector)s
    GROUP BY recipient_uei
"""

SLIM_SQL = """
    SELECT recipient_uei
    FROM entities.mv_usaspending_entity_grain_slim
    WHERE first_contract_date <= %(date_to)s::date
      AND last_contract_date  >= %(date_from)s::date
      AND naics_sectors @> ARRAY[%(sector)s]
"""


def fetch_layer(conn: psycopg.Connection, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SET statement_timeout = 0")
        cur.execute(sql, params)
        return cur.fetchall()


def summarize(rows: list[dict[str, Any]], has_numerics: bool = True) -> tuple[float, int, set[str]]:
    total = 0.0
    count = 0
    ueis: set[str] = set()
    for r in rows:
        if r.get("recipient_uei"):
            ueis.add(r["recipient_uei"])
        if has_numerics:
            total += float(r.get("total_obligations") or 0)
            count += int(r.get("txn_count") or 0)
    return total, count, ueis


def within(a: float, b: float, tol: float) -> bool:
    denom = max(abs(a), abs(b), 1e-9)
    return abs(a - b) / denom <= tol


def main() -> int:
    params = {"date_from": SLICE_FROM, "date_to": SLICE_TO, "sector": SLICE_SECTOR}
    dsn = os.environ["DEX_DB_URL_POOLED"]
    with psycopg.connect(dsn) as conn:
        raw_rows = fetch_layer(conn, RAW_SQL, params)
        typed_rows = fetch_layer(conn, TYPED_SQL, params)
        monthly_rows = fetch_layer(conn, MONTHLY_SQL, params)
        slim_rows = fetch_layer(conn, SLIM_SQL, params)

    raw_sum, raw_count, raw_ueis = summarize(raw_rows)
    typed_sum, typed_count, typed_ueis = summarize(typed_rows)
    monthly_sum, monthly_count, monthly_ueis = summarize(monthly_rows)
    _, _, slim_ueis = summarize(slim_rows, has_numerics=False)

    print()
    print(f"Slice: sector {SLICE_SECTOR}, {SLICE_FROM} .. {SLICE_TO}")
    print()
    print(f"{'Layer':<10}  {'SUM(obligations)':>22}  {'COUNT(txns)':>14}  {'UEIs':>8}")
    print("-" * 64)
    print(f"{'raw':<10}  {raw_sum:>22,.2f}  {raw_count:>14,}  {len(raw_ueis):>8,}")
    print(f"{'typed':<10}  {typed_sum:>22,.2f}  {typed_count:>14,}  {len(typed_ueis):>8,}")
    print(f"{'monthly':<10}  {monthly_sum:>22,.2f}  {monthly_count:>14,}  {len(monthly_ueis):>8,}")
    print(f"{'slim':<10}  {'(membership only)':>22}  {'':>14}  {len(slim_ueis):>8,}")
    print()

    failures: list[str] = []

    def check_sum(label: str, a: float, b: float) -> None:
        if not within(a, b, SUM_TOLERANCE):
            delta_pct = 100.0 * abs(a - b) / max(abs(a), abs(b), 1e-9)
            failures.append(f"SUM delta {label}: {a:,.2f} vs {b:,.2f} ({delta_pct:.3f}% > {SUM_TOLERANCE * 100}%)")

    def check_count(label: str, a: int, b: int) -> None:
        denom = max(abs(a), abs(b), 1)
        if abs(a - b) / denom > COUNT_TOLERANCE:
            delta_pct = 100.0 * abs(a - b) / denom
            failures.append(f"COUNT delta {label}: {a:,} vs {b:,} ({delta_pct:.3f}% > {COUNT_TOLERANCE * 100}%)")

    check_sum("raw vs typed",    raw_sum, typed_sum)
    check_sum("raw vs monthly",  raw_sum, monthly_sum)
    check_sum("typed vs monthly", typed_sum, monthly_sum)
    check_count("raw vs typed",    raw_count, typed_count)
    check_count("raw vs monthly",  raw_count, monthly_count)
    check_count("typed vs monthly", typed_count, monthly_count)

    missing_from_slim = raw_ueis - slim_ueis
    if missing_from_slim:
        failures.append(
            f"{len(missing_from_slim)} UEIs present in raw slice but missing from slim MV"
        )
        sample_missing = list(missing_from_slim)[:10]
        raw_lookup = {r["recipient_uei"]: r for r in raw_rows}
        print("Sample missing UEIs (raw rollup):")
        for uei in sample_missing:
            row = raw_lookup.get(uei, {})
            print(f"  {uei}: sum={float(row.get('total_obligations') or 0):,.2f} count={row.get('txn_count')}")
        print()

    if failures:
        print("VALIDATION FAILURES:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("All assertions passed: raw/typed/monthly numerics agree within tolerance, slim is a superset of raw UEIs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

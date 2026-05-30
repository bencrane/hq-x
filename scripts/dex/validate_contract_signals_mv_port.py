"""Equivalence test for the query_contract_signals port from typed MV to monthly MV.

Usage:
    doppler run --project data-engine-x-api --config prd -- \\
        .venv/bin/python3 scripts/validate_contract_signals_mv_port.py

Iterates over ≥50 (date_from, date_to, naics_prefix) combinations and runs
query_contract_signals on both MV paths (typed vs. monthly). Compares
total_obligated and contract_count; tolerates 0.1% variance per research §FU.5
(whole-month vs. day-precise boundary).

Exits 1 if any combination exceeds 0.1% on either metric.
"""
from __future__ import annotations

import itertools
import os
import sys
from datetime import date, timedelta
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.services import dealbridge_signals_query as qs

TOLERANCE = 0.001  # 0.1%


def _direct_query(path: str, filters: dict[str, Any]) -> dict[str, Any]:
    """Execute the service's SQL directly on a fresh connection with no statement timeout.

    We bypass the pooled connection in dealbridge_signals_query so we can disable the
    prod statement_timeout for the long-window test windows.
    """
    safe_limit = 500
    safe_offset = 0

    today = date.today()
    date_from = qs._parse_date(filters["date_from"], "date_from") if filters.get("date_from") else today - timedelta(days=90)
    date_to = qs._parse_date(filters["date_to"], "date_to") if filters.get("date_to") else today
    if date_from > date_to:
        raise ValueError("date_from must be on or before date_to")

    if path == "typed":
        sql, params = qs._build_typed_contract_signals_sql(
            filters=filters, date_from=date_from, date_to=date_to,
            safe_limit=safe_limit, safe_offset=safe_offset,
        )
    else:
        sql, params = qs._build_monthly_contract_signals_sql(
            filters=filters, date_from=date_from, date_to=date_to,
            safe_limit=safe_limit, safe_offset=safe_offset,
        )

    dsn = os.environ["DEX_DB_URL_POOLED"]
    with psycopg.connect(dsn) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SET statement_timeout = 0")
            cur.execute(sql, params)
            rows = cur.fetchall()

    total_matched = 0
    items: list[dict[str, Any]] = []
    for row in rows:
        total_matched = row.pop("total_matched", 0)
        items.append(row)
    return {"items": items, "total_matched": total_matched, "limit": safe_limit, "offset": safe_offset}


def _run(path: str, filters: dict[str, Any]) -> dict[str, Any]:
    return _direct_query(path, filters)


def _aggregate(result: dict[str, Any]) -> tuple[float, int, set[str]]:
    total_obligated = 0.0
    contract_count = 0
    ueis: set[str] = set()
    for row in result["items"]:
        total_obligated += float(row.get("total_obligated") or 0.0)
        contract_count += int(row.get("contract_count") or 0)
        uei = row.get("recipient_uei")
        if uei:
            ueis.add(str(uei))
    return total_obligated, contract_count, ueis


def _pct_delta(a: float, b: float) -> float:
    base = max(abs(a), abs(b))
    if base == 0:
        return 0.0
    return abs(a - b) / base


def _first_of_month(d: date) -> date:
    return d.replace(day=1)


def _last_of_month(d: date) -> date:
    if d.month == 12:
        return d.replace(day=31)
    return (d.replace(day=1).replace(month=d.month + 1) - timedelta(days=1))


def main() -> int:
    today = date.today()
    # Month-aligned boundaries so day-precise vs. whole-month window semantics
    # match perfectly across the two paths. Per directive §2 constraint 5, the
    # 0.1% tolerance absorbs distinct-award rounding; the window offset itself
    # is product semantics and must be eliminated by alignment here.
    date_froms = [_first_of_month(today - timedelta(days=d)) for d in (90, 180, 365, 730)]
    date_tos = [_last_of_month(today - timedelta(days=d)) for d in (0, 30)]
    naics_prefixes: list[str | None] = [
        None, "54", "23", "33", "62", "72", "44", "11", "48", "51", "52", "81",
    ]

    combos: list[tuple[date, date, str | None]] = []
    for df, dt, np_ in itertools.product(date_froms, date_tos, naics_prefixes):
        if df > dt:
            continue
        combos.append((df, dt, np_))

    # Prune to ≥50 product-realistic combinations
    combos = combos[:60]
    assert len(combos) >= 50, f"only {len(combos)} combos generated"

    failures: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []

    for i, (df, dt, np_) in enumerate(combos, start=1):
        filters: dict[str, Any] = {"date_from": df.isoformat(), "date_to": dt.isoformat()}
        if np_ is not None:
            filters["naics_prefix"] = np_

        typed_res = _run("typed", filters)
        monthly_res = _run("monthly", filters)

        typed_total, typed_count, typed_ueis = _aggregate(typed_res)
        monthly_total, monthly_count, monthly_ueis = _aggregate(monthly_res)

        total_delta = _pct_delta(typed_total, monthly_total)
        count_delta = _pct_delta(float(typed_count), float(monthly_count))
        sym_diff = len(typed_ueis.symmetric_difference(monthly_ueis))

        passed = total_delta <= TOLERANCE and count_delta <= TOLERANCE
        summary_rows.append({
            "idx": i,
            "date_from": df.isoformat(),
            "date_to": dt.isoformat(),
            "naics_prefix": np_ or "-",
            "typed_total": typed_total,
            "monthly_total": monthly_total,
            "total_pct": total_delta,
            "typed_count": typed_count,
            "monthly_count": monthly_count,
            "count_pct": count_delta,
            "sym_diff_ueis": sym_diff,
            "passed": passed,
        })

        if not passed:
            failures.append(summary_rows[-1])

    # Print report
    print(f"{'idx':>3}  {'date_from':10}  {'date_to':10}  {'np':>3}  "
          f"{'typed_total':>18}  {'monthly_total':>18}  "
          f"{'tot_pct':>8}  {'cnt_pct':>8}  {'sym':>5}  pass")
    for r in summary_rows:
        print(
            f"{r['idx']:>3}  {r['date_from']:10}  {r['date_to']:10}  {str(r['naics_prefix']):>3}  "
            f"{r['typed_total']:>18,.0f}  {r['monthly_total']:>18,.0f}  "
            f"{r['total_pct']*100:>7.3f}%  {r['count_pct']*100:>7.3f}%  "
            f"{r['sym_diff_ueis']:>5}  {'Y' if r['passed'] else 'N'}"
        )

    max_total_delta = max((r["total_pct"] for r in summary_rows), default=0.0)
    max_count_delta = max((r["count_pct"] for r in summary_rows), default=0.0)
    max_sym_diff = max((r["sym_diff_ueis"] for r in summary_rows), default=0)
    passed_count = sum(1 for r in summary_rows if r["passed"])

    print("\n" + "=" * 80)
    print(f"combos tested: {len(summary_rows)}")
    print(f"passed:        {passed_count}/{len(summary_rows)}")
    print(f"max total_pct: {max_total_delta*100:.3f}%")
    print(f"max count_pct: {max_count_delta*100:.3f}%")
    print(f"max sym diff:  {max_sym_diff} UEIs")
    print(f"tolerance:     {TOLERANCE*100:.3f}%")

    if failures:
        print(f"\n!! {len(failures)} combination(s) exceeded tolerance:")
        for f in failures:
            print(
                f"   [{f['idx']}] df={f['date_from']} dt={f['date_to']} "
                f"np={f['naics_prefix']} total_pct={f['total_pct']*100:.3f}% "
                f"count_pct={f['count_pct']*100:.3f}%"
            )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

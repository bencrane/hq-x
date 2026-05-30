#!/usr/bin/env python3
"""s14 — Sanity gates on the smoke CSV (ucc-ca-v1-lender-pool-2026-05-12.csv).

Three gates (all must pass):
  1. ≥50 distinct non-bank lenders in top-100 (bank_classification column not
     present in smoke CSV — all rows are non_bank by construction; gate becomes
     "≥50 rows" == "at least 50 non-bank lenders exist in this file")
  2. ≥30 lenders with last_filing_date >= 2025-01-01 (recency + activity filter)
  3. Top-row lender's total_filings >= 100 (sanity on active-lender scale)

Usage (matches the harness s14 invocation):
    doppler run --project hq-all --config prd -- \\
        uv run --quiet python3 apps/data-engine-x/scripts/sanity_check_lender_pool.py \\
            --csv ~/Desktop/hq/inventory/ucc-ca-v1-lender-pool-2026-05-12.csv \\
            --min-non-bank 50 --min-recent 30 --min-top-total-filings 100 \\
            --recent-cutoff 2025-01-01
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
LOG = logging.getLogger(__name__)


def main() -> int:
    ap = argparse.ArgumentParser(description="Sanity-check lender pool CSV (s14)")
    ap.add_argument("--csv", required=True)
    ap.add_argument("--min-non-bank", type=int, default=50)
    ap.add_argument("--min-recent", type=int, default=30)
    ap.add_argument("--min-top-total-filings", type=int, default=100)
    ap.add_argument("--recent-cutoff", default="2025-01-01")
    args = ap.parse_args()

    csv_path = Path(args.csv).expanduser()
    if not csv_path.exists():
        LOG.error("FAIL: CSV not found at %s", csv_path)
        return 1

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if not rows:
        LOG.error("FAIL: CSV is empty")
        return 1

    LOG.info("CSV rows: %d", len(rows))

    cutoff = datetime.fromisoformat(args.recent_cutoff)
    failures = []

    # Gate 1: row count >= min_non_bank (all rows in CSV are non_bank by construction)
    if len(rows) < args.min_non_bank:
        failures.append(
            f"Gate 1 FAIL: only {len(rows)} rows (need >= {args.min_non_bank})"
        )
    else:
        LOG.info("Gate 1 PASS: %d rows >= %d", len(rows), args.min_non_bank)

    # Gate 2: recent lenders
    recent_count = 0
    for row in rows:
        lfd = row.get("last_filing_date", "").strip()
        if not lfd:
            continue
        try:
            d = datetime.fromisoformat(lfd.split(".")[0].split("T")[0])
            if d >= cutoff:
                recent_count += 1
        except Exception:
            pass
    if recent_count < args.min_recent:
        failures.append(
            f"Gate 2 FAIL: only {recent_count} lenders with last_filing_date >= {args.recent_cutoff} "
            f"(need >= {args.min_recent})"
        )
    else:
        LOG.info(
            "Gate 2 PASS: %d lenders with last_filing_date >= %s",
            recent_count, args.recent_cutoff,
        )

    # Gate 3: top-row total_filings
    top_row = rows[0]
    try:
        top_total = int(top_row.get("total_filings", 0))
    except ValueError:
        top_total = 0
    if top_total < args.min_top_total_filings:
        failures.append(
            f"Gate 3 FAIL: top lender '{top_row.get('lender_name_normalized', '?')}' "
            f"has total_filings={top_total} (need >= {args.min_top_total_filings})"
        )
    else:
        LOG.info(
            "Gate 3 PASS: top lender '%s' has total_filings=%d",
            top_row.get("lender_name_normalized", "?"), top_total,
        )

    if failures:
        for f in failures:
            LOG.error(f)
        return 1

    LOG.info("All sanity gates PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""s13 — Export top-100 active non-bank lenders as CSV smoke output.

Reads lenders_lance (s7 output) and writes:
  ~/Desktop/hq/inventory/ucc-ca-v1-lender-pool-2026-05-12.csv

Columns (DoD #13):
  lender_name_normalized, total_filings, active_filings, last_filing_date,
  top_debtor_states, category_inferred_from_name, address_sample

Filters:
  - bank_classification = 'non_bank'
  - last_filing_date >= 2023-01-01
  - Ranked by total_filings DESC

Usage:
    doppler run --project hq-all --config prd -- \\
        uv run --quiet --with pylance --with pyarrow \\
        python3 apps/data-engine-x/scripts/build_ucc_ca_lender_pool_csv.py
"""
from __future__ import annotations

import csv
import json
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
LOG = logging.getLogger(__name__)

LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/ucc_ca/lenders_lance/"
OUTPUT_CSV = Path.home() / "Desktop" / "hq" / "inventory" / "ucc-ca-v1-lender-pool-2026-05-12.csv"
CUTOFF_DATE = datetime(2023, 1, 1)
TOP_N = 100

COLUMNS = [
    "lender_name_normalized",
    "total_filings",
    "active_filings",
    "last_filing_date",
    "top_debtor_states",
    "category_inferred_from_name",
    "address_sample",
]


def main() -> int:
    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            LOG.error("FAIL: %s not set", var)
            return 64

    import lance
    import pyarrow.compute as pc

    storage_options = {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
    }

    LOG.info("Loading lenders_lance ...")
    ds = lance.dataset(LANCE_URI, storage_options=storage_options)
    tbl = ds.to_table(
        columns=[
            "lender_name_normalized",
            "total_filings",
            "active_filings",
            "last_filing_date",
            "top_debtor_states",
            "bank_classification",
            "category_inferred_from_name",
            "address_sample",
        ]
    )
    LOG.info("Loaded %d total lenders", len(tbl))

    # Filter: non_bank only
    mask_nonbank = pc.equal(tbl.column("bank_classification"), "non_bank")
    tbl_nb = tbl.filter(mask_nonbank)
    LOG.info("Non-bank lenders: %d", len(tbl_nb))

    # Filter: last_filing_date >= 2023-01-01
    import pyarrow as pa

    lfd_col = tbl_nb.column("last_filing_date")
    # Convert to Python timestamps for comparison
    rows = tbl_nb.to_pylist()
    filtered = []
    for row in rows:
        lfd = row.get("last_filing_date")
        if lfd is None:
            continue
        if isinstance(lfd, datetime):
            if lfd >= CUTOFF_DATE:
                filtered.append(row)
        elif isinstance(lfd, date):
            if datetime(lfd.year, lfd.month, lfd.day) >= CUTOFF_DATE:
                filtered.append(row)
        else:
            # Try to parse string
            try:
                d = datetime.fromisoformat(str(lfd).split(".")[0])
                if d >= CUTOFF_DATE:
                    filtered.append(row)
            except Exception:
                pass

    LOG.info("After cutoff filter: %d lenders", len(filtered))

    # Sort by total_filings DESC, take top 100
    filtered.sort(key=lambda r: r.get("total_filings", 0), reverse=True)
    top100 = filtered[:TOP_N]

    if len(top100) < TOP_N:
        LOG.warning(
            "Only %d non-bank lenders with recent filings found (expected %d). "
            "Sanity gate #2 may fail.",
            len(top100), TOP_N,
        )

    # Write CSV
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    LOG.info("Writing %d rows to %s ...", len(top100), OUTPUT_CSV)
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        for row in top100:
            writer.writerow({
                "lender_name_normalized": row.get("lender_name_normalized", ""),
                "total_filings": row.get("total_filings", 0),
                "active_filings": row.get("active_filings", 0),
                "last_filing_date": str(row.get("last_filing_date", "")).split(".")[0],
                "top_debtor_states": row.get("top_debtor_states", "[]"),
                "category_inferred_from_name": row.get("category_inferred_from_name", "unknown"),
                "address_sample": row.get("address_sample", ""),
            })

    LOG.info("s13 complete: %s (%d rows)", OUTPUT_CSV, len(top100))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

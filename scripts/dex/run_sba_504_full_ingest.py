#!/usr/bin/env python3
"""Full ingest of SBA 504 CSV into entities.sba_504_loans.

Usage:
doppler run --project data-engine-x-api --config prd -- python scripts/run_sba_504_full_ingest.py
"""

from __future__ import annotations

import csv
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)

from app.services.sba_504_ingest import ingest_sba_504_csv  # noqa: E402

CSV_FILE_PATH = (
    "/Users/benjamincrane/data-engine-x-api/SBA Data Downloads/as-of-251231/"
    "FOIA - 504 (FY2010-Present)/foia-504-fy2010-present-asof-251231.csv"
)
SOURCE_FILENAME = "foia-504-fy2010-present-asof-251231.csv"
SOURCE_URL = ""
CHUNK_SIZE = 50_000


def derive_extract_date(csv_path: str) -> str:
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        first_row = next(reader)
    raw = first_row["asofdate"].strip()
    dt = datetime.strptime(raw, "%m/%d/%Y")
    return dt.strftime("%Y-%m-%d")


def main() -> None:
    print("Starting SBA 504 full ingest (idempotent upsert by composite key).\n")

    extract_date = derive_extract_date(CSV_FILE_PATH)
    print(f"Derived extract_date from first row asofdate: {extract_date}\n")

    wall_start = time.monotonic()
    try:
        result = ingest_sba_504_csv(
            csv_file_path=CSV_FILE_PATH,
            extract_date=extract_date,
            source_filename=SOURCE_FILENAME,
            source_url=SOURCE_URL,
            chunk_size=CHUNK_SIZE,
        )
    except RuntimeError as exc:
        elapsed = time.monotonic() - wall_start
        print(f"\nINGEST FAILED after {elapsed:.1f}s")
        print(f"Error: {exc}")
        sys.exit(1)

    elapsed = time.monotonic() - wall_start
    minutes = int(elapsed // 60)
    seconds = elapsed % 60

    total_parsed = result["total_rows_parsed"]
    total_accepted = result["total_rows_accepted"]
    total_rejected = result["total_rows_rejected"]
    total_written = result["total_rows_written"]
    chunks = result["chunks_processed"]
    deduped = total_accepted - total_written

    print(
        f"""
=== SBA 504 FULL INGEST COMPLETE ===
File: {SOURCE_FILENAME}
Extract date: {extract_date}
Total rows parsed: {total_parsed}
Rows accepted: {total_accepted}
Rows rejected: {total_rejected}
Rows written: {total_written}
Duplicates deduplicated: {deduped}
Chunks processed: {chunks}
Total elapsed: {minutes}m {seconds:.1f}s
"""
    )


if __name__ == "__main__":
    main()

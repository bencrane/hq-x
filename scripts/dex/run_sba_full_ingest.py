#!/usr/bin/env python3
"""Full ingest of SBA 7(a) loan CSV into entities.sba_7a_loans.

Usage: doppler run -- python scripts/run_sba_full_ingest.py
"""

import csv
import logging
import sys
import time
from datetime import datetime

# Configure logging before any app imports
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)

from app.services.sba_ingest import ingest_sba_csv  # noqa: E402

CSV_FILE_PATH = "/Users/benjamincrane/Downloads/sba_7a_fy2020_present.csv"
SOURCE_FILENAME = "foia-7a-fy2020-present-asof-250930.csv"
SOURCE_URL = "https://data.sba.gov/dataset/0ff8e8e9-b967-4f4e-987c-6ac78c575087/resource/d67d3ccb-2002-4134-a288-481b51cd3479/download/foia-7a-fy2020-present-asof-250930.csv"
CHUNK_SIZE = 50_000


def derive_extract_date(csv_path: str) -> str:
    """Read the first data row's asofdate and convert MM/DD/YYYY -> YYYY-MM-DD."""
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        first_row = next(reader)
    raw = first_row["asofdate"].strip()
    dt = datetime.strptime(raw, "%m/%d/%Y")
    return dt.strftime("%Y-%m-%d")


def main() -> None:
    print("NOTE: This ingest is idempotent. If interrupted, re-run safely — committed chunks are preserved.\n")

    # Step 1: Derive extract_date
    extract_date = derive_extract_date(CSV_FILE_PATH)
    print(f"Derived extract_date from asofdate: {extract_date}\n")

    # Step 2: Run ingest
    wall_start = time.monotonic()
    try:
        result = ingest_sba_csv(
            csv_file_path=CSV_FILE_PATH,
            extract_date=extract_date,
            source_filename=SOURCE_FILENAME,
            source_url=SOURCE_URL,
            chunk_size=CHUNK_SIZE,
        )
    except RuntimeError as exc:
        wall_elapsed = time.monotonic() - wall_start
        print(f"\n!!! INGEST FAILED after {wall_elapsed:.1f}s !!!")
        print(f"Error: {exc}")
        sys.exit(1)

    wall_elapsed = time.monotonic() - wall_start
    minutes = int(wall_elapsed // 60)
    seconds = wall_elapsed % 60

    # Step 3: Final summary
    total_parsed = result["total_rows_parsed"]
    total_accepted = result["total_rows_accepted"]
    total_rejected = result["total_rows_rejected"]
    total_written = result["total_rows_written"]
    chunks = result["chunks_processed"]
    dupes = total_accepted - total_written

    print(f"""
=== SBA 7(a) FULL INGEST COMPLETE ===
File: {SOURCE_FILENAME}
Extract date: {extract_date} (derived from asofdate)
Total rows parsed: {total_parsed}
Rows accepted: {total_accepted}
Rows rejected: {total_rejected}
Rows written: {total_written}
Duplicates deduplicated: {dupes}
Chunks: {chunks}
Total elapsed: {minutes} minutes {seconds:.1f} seconds
""")


if __name__ == "__main__":
    main()

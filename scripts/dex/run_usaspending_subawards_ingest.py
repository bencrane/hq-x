#!/usr/bin/env python3
"""Sequentially ingest USASpending contract and assistance subaward CSV files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.usaspending_assistance_subawards_extract_ingest import (
    ingest_assistance_subawards_csv,
)
from app.services.usaspending_assistance_subawards_common import (
    close_assistance_subawards_connection_pool,
)
from app.services.usaspending_contract_subawards_extract_ingest import (
    ingest_contract_subawards_csv,
)
from app.services.usaspending_contract_subawards_common import (
    close_contract_subawards_connection_pool,
)

DEFAULT_EXTRACT_DATE = "2026-04-13"
DEFAULT_CHUNK_SIZE = 50_000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--extract-date",
        default=DEFAULT_EXTRACT_DATE,
        help="Extract date in YYYY-MM-DD format (default: 2026-04-13).",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_CHUNK_SIZE,
        help="Chunk size for COPY upsert writes.",
    )
    parser.add_argument(
        "--base-dir",
        default="USA SPENDING/Subawards and Assistance",
        help="Directory containing the USASpending subaward CSV files.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    base_dir = Path(args.base_dir)

    contract_file = base_dir / "All_Contracts_Subawards_2026-04-13_H16M24S47_1.csv"
    assistance_file = base_dir / "All_Assistance_Subawards_2026-04-13_H16M32S45_1.csv"

    for source_file in (contract_file, assistance_file):
        if not source_file.exists():
            raise FileNotFoundError(f"Subaward CSV not found: {source_file}")

    try:
        contract_summary = ingest_contract_subawards_csv(
            csv_file_path=str(contract_file),
            extract_date=args.extract_date,
            source_filename=contract_file.name,
            chunk_size=args.chunk_size,
        )

        assistance_summary = ingest_assistance_subawards_csv(
            csv_file_path=str(assistance_file),
            extract_date=args.extract_date,
            source_filename=assistance_file.name,
            chunk_size=args.chunk_size,
        )
    finally:
        close_contract_subawards_connection_pool()
        close_assistance_subawards_connection_pool()

    summaries = [contract_summary, assistance_summary]
    combined = {
        "extract_date": args.extract_date,
        "files_processed": len(summaries),
        "total_rows_parsed": sum(item["total_rows_parsed"] for item in summaries),
        "total_rows_accepted": sum(item["total_rows_accepted"] for item in summaries),
        "total_rows_rejected": sum(item["total_rows_rejected"] for item in summaries),
        "total_rows_written": sum(item["total_rows_written"] for item in summaries),
        "total_chunks_processed": sum(item["chunks_processed"] for item in summaries),
        "file_results": {
            "contract_subawards": contract_summary,
            "assistance_subawards": assistance_summary,
        },
    }
    print(json.dumps(combined, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

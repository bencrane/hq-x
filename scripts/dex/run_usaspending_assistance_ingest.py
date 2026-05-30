#!/usr/bin/env python3
"""Sequentially ingest USASpending assistance prime transaction CSV files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.services.usaspending_assistance_extract_ingest import ingest_usaspending_assistance_csv


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
        help="Directory containing the assistance prime transaction CSV files.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    base_dir = Path(args.base_dir)

    files = [
        base_dir / "All_Assistance_PrimeTransactions_2026-04-13_H17M36S13_1.csv",
        base_dir / "All_Assistance_PrimeTransactions_2026-04-13_H17M36S13_2.csv",
    ]

    summaries: list[dict] = []
    for file_path in files:
        if not file_path.exists():
            raise FileNotFoundError(f"Assistance CSV not found: {file_path}")

        summary = ingest_usaspending_assistance_csv(
            csv_file_path=str(file_path),
            extract_date=args.extract_date,
            source_filename=file_path.name,
            chunk_size=args.chunk_size,
        )
        summaries.append(summary)

    combined = {
        "extract_date": args.extract_date,
        "files_processed": len(summaries),
        "total_rows_parsed": sum(item["total_rows_parsed"] for item in summaries),
        "total_rows_accepted": sum(item["total_rows_accepted"] for item in summaries),
        "total_rows_rejected": sum(item["total_rows_rejected"] for item in summaries),
        "total_rows_written": sum(item["total_rows_written"] for item in summaries),
        "total_chunks_processed": sum(item["chunks_processed"] for item in summaries),
        "file_results": summaries,
    }
    print(json.dumps(combined, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

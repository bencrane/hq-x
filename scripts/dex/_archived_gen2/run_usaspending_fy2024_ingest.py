#!/usr/bin/env python3
"""Run FY2024 full USASpending contract ingest.

Usage:  doppler run -- python scripts/run_usaspending_fy2024_ingest.py
"""

from __future__ import annotations

import logging
import sys
import time

ZIP_FILE_PATH = "/Users/benjamincrane/Downloads/FY2024_All_Contracts_Full_20260306.zip"
EXTRACT_DATE = "2026-03-06"
EXTRACT_TYPE = "FULL"
CHUNK_SIZE = 50_000


class IngestProgressHandler(logging.Handler):
    """Intercepts structured log events from the ingest service and prints
    human-readable chunk-level progress to stdout."""

    def __init__(self) -> None:
        super().__init__()
        self.csv_files: list[str] = []
        self.csv_count = 0
        self.file_index = 0
        self.file_chunk_count = 0
        self.start_time = time.monotonic()
        self.file_header_printed = False

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()

        if msg == "usaspending_ingest_zip_start":
            self.csv_files = getattr(record, "csv_files", [])
            self.csv_count = getattr(record, "csv_count", 0)

        elif msg == "usaspending_batch_persist_phases":
            if getattr(record, "error", False):
                return
            rows_written = getattr(record, "rows_written", 0)
            if rows_written == 0:
                return

            if not self.file_header_printed:
                filename = (
                    self.csv_files[self.file_index]
                    if self.file_index < len(self.csv_files)
                    else "?"
                )
                print(f"\nFile {self.file_index + 1}/{self.csv_count}: {filename}")
                self.file_header_printed = True

            self.file_chunk_count += 1
            elapsed = time.monotonic() - self.start_time
            row_start = (self.file_chunk_count - 1) * CHUNK_SIZE + 1
            row_end = row_start + rows_written - 1
            print(
                f"  Chunk {self.file_chunk_count}: "
                f"rows {row_start:,}-{row_end:,} written "
                f"(elapsed: {elapsed:.1f}s)"
            )

        elif msg == "usaspending_ingest_csv_summary":
            self.file_index += 1
            self.file_chunk_count = 0
            self.file_header_printed = False


def main() -> int:
    print("=" * 70)
    print("USASpending.gov FY2024 Full Contract Ingest")
    print("=" * 70)
    print(f"ZIP:          {ZIP_FILE_PATH}")
    print(f"Extract date: {EXTRACT_DATE}")
    print(f"Extract type: {EXTRACT_TYPE}")
    print(f"Chunk size:   {CHUNK_SIZE:,}")
    print()
    print(
        "NOTE: This ingest is idempotent. If interrupted, re-run safely —\n"
        "      committed chunks are preserved."
    )
    print()

    # Attach progress handler to the two service loggers
    progress_handler = IngestProgressHandler()
    progress_handler.setLevel(logging.INFO)
    for logger_name in (
        "app.services.usaspending_common",
        "app.services.usaspending_extract_ingest",
    ):
        lgr = logging.getLogger(logger_name)
        lgr.setLevel(logging.INFO)
        lgr.addHandler(progress_handler)

    start = time.monotonic()

    try:
        from app.services.usaspending_extract_ingest import ingest_usaspending_zip

        result = ingest_usaspending_zip(
            zip_file_path=ZIP_FILE_PATH,
            extract_date=EXTRACT_DATE,
            extract_type=EXTRACT_TYPE,
            chunk_size=CHUNK_SIZE,
        )
    except Exception as exc:
        elapsed = time.monotonic() - start
        minutes = int(elapsed // 60)
        seconds = elapsed % 60
        print()
        print("=" * 70)
        print("INGEST FAILED")
        print("=" * 70)
        print(f"Error: {exc}")
        print(f"Elapsed before failure: {minutes} minutes {seconds:.1f} seconds")
        print()
        print("Committed chunks before failure are preserved (idempotent upsert).")
        print("Fix the issue and re-run.")
        return 1

    elapsed = time.monotonic() - start
    minutes = int(elapsed // 60)
    seconds = elapsed % 60

    print()
    print("=" * 70)
    print("=== USASPENDING FY2024 FULL INGEST COMPLETE ===")
    print("=" * 70)
    print(f"ZIP: {ZIP_FILE_PATH.rsplit('/', 1)[-1]}")
    print(f"Extract date: {EXTRACT_DATE}")
    print(f"Files processed: {result['csv_files_processed']}")
    print(f"Total rows parsed: {result['total_rows_parsed']:,}")
    print(f"Rows accepted: {result['total_rows_accepted']:,}")
    print(f"Rows rejected: {result['total_rows_rejected']:,}")
    print(f"Rows written: {result['total_rows_written']:,}")
    print(f"Chunks: {result['total_chunks_processed']}")
    print(f"Total elapsed: {minutes} minutes {seconds:.1f} seconds")

    if "file_results" in result:
        print()
        print("Per-file breakdown:")
        for i, fr in enumerate(result["file_results"], 1):
            print(f"  File {i}: {fr['source_filename']}")
            print(
                f"    Parsed: {fr['total_rows_parsed']:,}  "
                f"Accepted: {fr['total_rows_accepted']:,}  "
                f"Rejected: {fr['total_rows_rejected']:,}  "
                f"Written: {fr['total_rows_written']:,}  "
                f"Chunks: {fr['chunks_processed']}"
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())

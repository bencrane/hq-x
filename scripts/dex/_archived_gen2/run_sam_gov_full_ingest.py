#!/usr/bin/env python3
"""SAM.gov March 2026 full monthly ingest.

Extracts the .dat from the downloaded ZIP, calls ingest_sam_gov_extract(),
and prints progress per chunk. Zero API calls — file is already on disk.

Usage: PYTHONPATH=. doppler run -- python scripts/run_sam_gov_full_ingest.py
"""

from __future__ import annotations

import logging
import sys
import tempfile
import time
import zipfile

ZIP_PATH = "/Users/benjamincrane/Downloads/SAM_PUBLIC_MONTHLY_V2_20260301.ZIP"
EXTRACT_DATE = "2026-03-01"
EXTRACT_TYPE = "MONTHLY"
SOURCE_FILENAME = "SAM_PUBLIC_MONTHLY_V2_20260301.dat"
CHUNK_SIZE = 50_000
EXPECTED_TOTAL = 874_709
ESTIMATED_CHUNKS = (EXPECTED_TOTAL + CHUNK_SIZE - 1) // CHUNK_SIZE  # ~18


class ChunkProgressHandler(logging.Handler):
    """Intercept structured log messages to print human-readable chunk progress."""

    def __init__(self):
        super().__init__()
        self.start_time = time.monotonic()

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        extra = getattr(record, "__dict__", {})

        if msg == "sam_gov_ingest_bof_header":
            bof_line = extra.get("bof_line", "?")
            expected = extra.get("expected_record_count", "?")
            print(f"BOF header: {bof_line}")
            print(f"Expected records: {expected}")
            print()

        elif msg == "sam_gov_batch_persist_phases":
            rows_written = extra.get("rows_written", 0)
            if rows_written > 0:
                elapsed = time.monotonic() - self.start_time
                # Infer chunk number from cumulative rows written
                # (this handler sees each chunk individually)
                total_ms = extra.get("total_ms", 0)
                print(
                    f"  Chunk persisted: {rows_written:,} rows "
                    f"(chunk time: {total_ms/1000:.1f}s, "
                    f"wall elapsed: {elapsed:.0f}s)"
                )

        elif msg == "sam_gov_ingest_chunk_failed":
            chunk_num = extra.get("chunk_number", "?")
            written_so_far = extra.get("rows_written_so_far", 0)
            error = extra.get("error", "unknown")
            print(f"\n  CHUNK {chunk_num} FAILED")
            print(f"  Rows successfully written before failure: {written_so_far:,}")
            print(f"  Error: {error}")


def main() -> int:
    print("=" * 60)
    print("SAM.GOV FULL MONTHLY INGEST")
    print("=" * 60)
    print(f"File: {ZIP_PATH}")
    print(f"Extract date: {EXTRACT_DATE}")
    print(f"Chunk size: {CHUNK_SIZE:,}")
    print(f"Estimated chunks: ~{ESTIMATED_CHUNKS}")
    print()
    print(
        "NOTE: This ingest is idempotent. If interrupted, re-run "
        "safely — committed chunks are preserved."
    )
    print()

    # Configure logging so service log messages are visible
    progress_handler = ChunkProgressHandler()
    progress_handler.setLevel(logging.INFO)

    # Attach to the relevant loggers
    for logger_name in [
        "app.services.sam_gov_extract_ingest",
        "app.services.sam_gov_common",
    ]:
        lg = logging.getLogger(logger_name)
        lg.setLevel(logging.INFO)
        lg.addHandler(progress_handler)

    # Extract .dat from ZIP to temp dir
    print("Extracting .dat from ZIP...")
    with zipfile.ZipFile(ZIP_PATH, "r") as zf:
        dat_names = [n for n in zf.namelist() if n.lower().endswith(".dat")]
        if not dat_names:
            print(f"ERROR: No .dat file found in {ZIP_PATH}")
            return 1
        dat_name = dat_names[0]
        print(f"Found: {dat_name}")

        with tempfile.TemporaryDirectory() as tmp_dir:
            zf.extract(dat_name, tmp_dir)
            dat_path = f"{tmp_dir}/{dat_name}"
            print(f"Extracted to: {dat_path}")
            print()

            # Import here so logging is configured first
            from app.services.sam_gov_extract_ingest import ingest_sam_gov_extract

            print("Starting ingest...")
            print("-" * 40)
            wall_start = time.monotonic()

            try:
                result = ingest_sam_gov_extract(
                    extract_file_path=dat_path,
                    extract_date=EXTRACT_DATE,
                    extract_type=EXTRACT_TYPE,
                    source_filename=SOURCE_FILENAME,
                    source_download_url=None,
                    chunk_size=CHUNK_SIZE,
                )
            except RuntimeError as exc:
                print(f"\nINGEST FAILED: {exc}")
                return 1

            wall_elapsed = time.monotonic() - wall_start
            minutes = int(wall_elapsed // 60)
            seconds = wall_elapsed % 60

    # Final summary
    print()
    print("=" * 60)
    print("SAM.GOV FULL INGEST COMPLETE")
    print("=" * 60)
    print(f"File: {SOURCE_FILENAME}")
    print(f"Extract date: {EXTRACT_DATE}")
    print(f"Total rows parsed: {result['total_rows_parsed']:,}")
    print(f"Rows accepted: {result['total_rows_accepted']:,}")
    print(f"Rows rejected: {result['total_rows_rejected']:,}")
    print(f"Rows written: {result['total_rows_written']:,}")
    print(f"Chunks: {result['chunks_processed']}")
    print(f"Total elapsed: {minutes} minutes {seconds:.1f} seconds")

    return 0


if __name__ == "__main__":
    sys.exit(main())

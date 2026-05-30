#!/usr/bin/env python3
"""Validate SAM.gov .dat extract field count against the column map.

Downloads a DAILY extract (small), reads the first 10 lines, and compares
the pipe-delimited field count against SAM_GOV_COLUMN_COUNT (368).

Usage: doppler run -- python scripts/validate_sam_gov_field_count.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile


# Ensure project root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.sam_gov_column_map import SAM_GOV_COLUMN_COUNT
from app.services.sam_gov_extract_download import download_sam_gov_extract


def main() -> None:
    output_dir = tempfile.mkdtemp(prefix="sam_gov_validate_")
    api_calls_made = 0

    try:
        # Today (Monday 03/16/2026) first, then last Friday (03/13/2026)
        dates_to_try = ["03/16/2026", "03/13/2026"]

        download_result = None
        for attempt_date in dates_to_try:
            api_calls_made += 1
            print(f"[API call #{api_calls_made}] Requesting DAILY extract for {attempt_date}...")
            try:
                download_result = download_sam_gov_extract(
                    extract_type="DAILY",
                    date=attempt_date,
                    output_dir=output_dir,
                )
                print(f"  -> Downloaded: {download_result['source_filename']} "
                      f"({download_result['file_size_bytes']:,} bytes)")
                break
            except RuntimeError as exc:
                error_msg = str(exc)
                print(f"  -> Failed: {error_msg[:200]}")
                if api_calls_made >= 2:
                    print("\nMax API calls (2) reached. Stopping.")
                    sys.exit(1)

        if download_result is None:
            print("\nCould not download any extract. Stopping.")
            sys.exit(1)

        # Read first 10 lines
        dat_path = download_result["dat_file_path"]
        print(f"\nReading first 10 lines of: {dat_path}")
        print("=" * 80)

        field_counts: list[int] = []
        raw_first_line: str | None = None

        with open(dat_path, "r", encoding="utf-8") as f:
            for line_num in range(1, 11):
                line = f.readline()
                if not line:
                    print(f"  (file has fewer than 10 lines, stopped at line {line_num - 1})")
                    break

                stripped = line.rstrip("\n").rstrip("\r")
                if not stripped:
                    continue

                if raw_first_line is None:
                    raw_first_line = stripped

                fields = stripped.split("|")
                count = len(fields)
                last_field = fields[-1].strip() if fields else "(empty)"
                field_counts.append(count)

                print(f"  Line {line_num}: {count} fields, last_field='{last_field}'")

        print("=" * 80)

        if not field_counts:
            print("\nNo data lines found in file. Cannot validate.")
            sys.exit(1)

        # All lines should have the same count
        observed = field_counts[0]
        consistent = all(c == observed for c in field_counts)

        if not consistent:
            print(f"\nWARNING: Inconsistent field counts across lines: {field_counts}")
            print("Using first line's count for comparison.")

        # Verdict
        print(f"\nExpected (SAM_GOV_COLUMN_COUNT): {SAM_GOV_COLUMN_COUNT}")
        print(f"Observed (first line):            {observed}")

        if observed == SAM_GOV_COLUMN_COUNT:
            print(f"\n✓ MATCH: .dat file has {observed} fields, column map expects "
                  f"{SAM_GOV_COLUMN_COUNT}. Parser will work.")
        else:
            delta = observed - SAM_GOV_COLUMN_COUNT
            print(f"\n✗ MISMATCH: .dat file has {observed} fields, column map expects "
                  f"{SAM_GOV_COLUMN_COUNT}. Parser needs adjustment before ingest.")
            print(f"  Δ = {delta:+d} fields")
            if raw_first_line:
                print(f"\n  Raw first line (truncated to 500 chars):")
                print(f"  {raw_first_line[:500]}")

        print(f"\nAPI calls made: {api_calls_made}")

    finally:
        # Cleanup
        if os.path.exists(output_dir):
            shutil.rmtree(output_dir, ignore_errors=True)
            print(f"Cleaned up temp dir: {output_dir}")


if __name__ == "__main__":
    main()

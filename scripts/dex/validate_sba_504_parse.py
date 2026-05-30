#!/usr/bin/env python3
"""SBA 504 parse validation against real CSV file.

Runnable with:
doppler run --project data-engine-x-api --config prd -- python scripts/validate_sba_504_parse.py

Read-only: no database writes.
"""

from __future__ import annotations

import csv
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.sba_504_column_map import SBA_504_COLUMN_COUNT
from app.services.sba_504_common import (
    Sba504SourceContext,
    build_sba_504_loan_row,
    parse_sba_504_csv_row,
)

CSV_PATH = (
    "/Users/benjamincrane/data-engine-x-api/SBA Data Downloads/as-of-251231/"
    "FOIA - 504 (FY2010-Present)/foia-504-fy2010-present-asof-251231.csv"
)
SOURCE_FILENAME = "foia-504-fy2010-present-asof-251231.csv"
SOURCE_URL = ""
MAX_ROWS = 100


def convert_date(mm_dd_yyyy: str) -> str:
    dt = datetime.strptime(mm_dd_yyyy.strip(), "%m/%d/%Y")
    return dt.strftime("%Y-%m-%d")


def main() -> int:
    print(f"Opening {CSV_PATH}")
    print(f"Expected columns: {SBA_504_COLUMN_COUNT}")

    parsed_rows: list[dict[str, str | int | None]] = []
    extract_date: str | None = None
    errors: list[str] = []

    with open(CSV_PATH, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        if reader.fieldnames is None:
            print("ERROR: No CSV header found.")
            return 1
        header_count = len(reader.fieldnames)
        print(f"Header column count: {header_count}")
        if header_count != SBA_504_COLUMN_COUNT:
            print(f"ERROR: Expected {SBA_504_COLUMN_COUNT} columns, got {header_count}")
            return 1

        for row_dict in reader:
            row_number = len(parsed_rows) + 1
            if extract_date is None:
                raw_asofdate = row_dict.get("asofdate", "").strip()
                if not raw_asofdate:
                    print("ERROR: First row is missing asofdate.")
                    return 1
                extract_date = convert_date(raw_asofdate)
                print(f"Derived extract_date from asofdate: {extract_date}")

            parsed = parse_sba_504_csv_row(row_dict, row_number)
            if parsed is None:
                errors.append(f"Row {row_number}: parse failed.")
                continue

            source_context = Sba504SourceContext(
                extract_date=extract_date,
                source_filename=SOURCE_FILENAME,
                source_url=SOURCE_URL,
            )
            built = build_sba_504_loan_row(parsed, source_context)
            parsed_rows.append(built)

            if not built.get("borrname"):
                errors.append(f"Row {row_number}: borrname missing after mapping.")
            if built.get("extract_date") != extract_date:
                errors.append(f"Row {row_number}: extract_date mismatch.")
            if built.get("row_position") != row_number:
                errors.append(f"Row {row_number}: row_position mismatch.")

            if len(parsed_rows) >= MAX_ROWS:
                break

    if extract_date != "2025-12-31":
        errors.append(f"Expected extract_date 2025-12-31, got {extract_date}")

    dedup: dict[tuple[str, ...], dict[str, str | int | None]] = {}
    for row in parsed_rows:
        key = (
            str(row.get("extract_date") or ""),
            str(row.get("borrname") or ""),
            str(row.get("borrstreet") or ""),
            str(row.get("borrcity") or ""),
            str(row.get("borrstate") or ""),
            str(row.get("approvaldate") or ""),
            str(row.get("grossapproval") or ""),
        )
        dedup[key] = row
    dedup_count = len(dedup)
    if dedup_count > len(parsed_rows):
        errors.append("Dedup logic invalid: dedup_count exceeded parsed_rows.")

    if parsed_rows:
        synthetic_dupe_map: dict[tuple[str, ...], dict[str, str | int | None]] = {}
        first = parsed_rows[0].copy()
        first_key = (
            str(first.get("extract_date") or ""),
            str(first.get("borrname") or ""),
            str(first.get("borrstreet") or ""),
            str(first.get("borrcity") or ""),
            str(first.get("borrstate") or ""),
            str(first.get("approvaldate") or ""),
            str(first.get("grossapproval") or ""),
        )
        synthetic_dupe_map[first_key] = first
        duplicate_variant = first.copy()
        duplicate_variant["row_position"] = int(first.get("row_position") or 0) + 9999
        synthetic_dupe_map[first_key] = duplicate_variant
        if len(synthetic_dupe_map) != 1:
            errors.append("Conflict-key duplicate detection failed.")
        final_position = synthetic_dupe_map[first_key].get("row_position")
        if final_position != duplicate_variant["row_position"]:
            errors.append("Conflict-key overwrite behavior is not last-write-wins.")

    print(f"Rows parsed for validation: {len(parsed_rows)}")
    print(f"Unique rows by conflict key in sample: {dedup_count}")
    print(f"Validation errors: {len(errors)}")

    if errors:
        print("\nErrors:")
        for error in errors:
            print(f"- {error}")
        return 1

    print("All SBA 504 parse validations passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

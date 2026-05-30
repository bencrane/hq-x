#!/usr/bin/env python3
"""SBA 7(a) parse validation against real CSV file.

Runnable with: doppler run -- python scripts/validate_sba_parse.py
No database writes — read-only validation.
"""

from __future__ import annotations

import csv
import sys
from datetime import datetime

from app.services.sba_column_map import SBA_COLUMN_COUNT
from app.services.sba_common import (
    SbaSourceContext,
    build_sba_loan_row,
    parse_sba_csv_row,
)

CSV_PATH = "/Users/benjamincrane/Downloads/sba_7a_fy2020_present.csv"
SOURCE_URL = (
    "https://data.sba.gov/dataset/0ff8e8e9-b967-4f4e-987c-6ac78c575087/"
    "resource/d67d3ccb-2002-4134-a288-481b51cd3479/download/"
    "foia-7a-fy2020-present-asof-250930.csv"
)
SOURCE_FILENAME = "foia-7a-fy2020-present-asof-250930.csv"
MAX_ROWS = 100
PRINT_ROWS = 10


def convert_date(mm_dd_yyyy: str) -> str:
    """Convert MM/DD/YYYY to YYYY-MM-DD."""
    dt = datetime.strptime(mm_dd_yyyy.strip(), "%m/%d/%Y")
    return dt.strftime("%Y-%m-%d")


def main() -> int:
    print(f"Opening {CSV_PATH}")
    print(f"Expected columns: {SBA_COLUMN_COUNT}")
    print()

    errors: list[str] = []
    parsed_rows: list[dict] = []
    extract_date: str | None = None

    with open(CSV_PATH, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        if reader.fieldnames is not None:
            header_count = len(reader.fieldnames)
            print(f"Header column count: {header_count}")
            if header_count != SBA_COLUMN_COUNT:
                print(f"ERROR: Expected {SBA_COLUMN_COUNT} columns, got {header_count}")
                return 1
        else:
            print("ERROR: No header found")
            return 1

        for row_dict in reader:
            row_number = reader.line_num - 1  # DictReader counts from header

            # Derive extract_date from first row's asofdate
            if extract_date is None:
                raw_asofdate = row_dict.get("asofdate", "").strip()
                if not raw_asofdate:
                    print(f"ERROR: First row has no asofdate value")
                    return 1
                extract_date = convert_date(raw_asofdate)
                print(f"asofdate found: {raw_asofdate}")
                print(f"Derived extract_date: {extract_date}")
                print()

            parsed = parse_sba_csv_row(row_dict, row_number)
            if parsed is None:
                errors.append(f"Row {row_number}: parse failed (missing borrname or wrong column count)")
                continue

            source_context = SbaSourceContext(
                extract_date=extract_date,
                source_filename=SOURCE_FILENAME,
                source_url=SOURCE_URL,
            )
            built = build_sba_loan_row(parsed, source_context)
            parsed_rows.append(built)

            # Validate built row
            if not built.get("borrname"):
                errors.append(f"Row {row_number}: borrname is empty after build")
            if built.get("extract_date") != extract_date:
                errors.append(f"Row {row_number}: extract_date mismatch")
            if not built.get("source_filename"):
                errors.append(f"Row {row_number}: source_filename is empty")
            if built.get("row_position") != row_number:
                errors.append(f"Row {row_number}: row_position mismatch (got {built.get('row_position')})")

            naics = built.get("naicscode")
            if not naics or len(naics) != 6:
                errors.append(f"Row {row_number}: naicscode not 6-digit (got '{naics}')")

            state = built.get("borrstate")
            if not state or len(state) != 2:
                errors.append(f"Row {row_number}: borrstate not 2-letter (got '{state}')")

            if len(parsed_rows) >= MAX_ROWS:
                break

    # Print first N rows
    print(f"--- First {PRINT_ROWS} rows ---")
    for i, row in enumerate(parsed_rows[:PRINT_ROWS], start=1):
        name = row.get("borrname", "?")
        city = row.get("borrcity", "?")
        state = row.get("borrstate", "?")
        naics = row.get("naicscode", "?")
        amount = row.get("grossapproval", "?")
        date = row.get("approvaldate", "?")
        status = row.get("loanstatus", "?")
        ok = "OK" if not any(f"Row {i}" in e for e in errors) else "FAIL"
        print(f"Row {i}: name={name} | city={city} | state={state} | naics={naics} | amount={amount} | date={date} | status={status} | {ok}")

    # Field mapping verification for row 1
    if parsed_rows:
        print()
        print("--- Field mapping verification (Row 1) ---")
        row1 = parsed_rows[0]
        for key in ["borrname", "borrstreet", "borrcity", "borrstate", "borrzip",
                     "naicscode", "grossapproval", "approvaldate", "bankname",
                     "businesstype", "businessage", "loanstatus", "jobssupported",
                     "extract_date", "source_filename", "source_provider", "row_position"]:
            print(f"  {key}: {row1.get(key)}")

    # Check for composite key duplicates in first 100 rows
    seen_keys: dict[tuple, int] = {}
    duplicates: list[str] = []
    for i, row in enumerate(parsed_rows, start=1):
        key = (
            row.get("extract_date"),
            row.get("borrname"),
            row.get("borrstreet"),
            row.get("borrcity"),
            row.get("borrstate"),
            row.get("approvaldate"),
            row.get("grossapproval"),
        )
        if key in seen_keys:
            duplicates.append(f"Row {i} duplicates Row {seen_keys[key]} on composite key: {key}")
        else:
            seen_keys[key] = i

    # Summary
    print()
    print(f"--- Validation Summary ---")
    print(f"Rows parsed: {len(parsed_rows)}")
    print(f"Errors: {len(errors)}")
    print(f"Composite key duplicates in first {MAX_ROWS}: {len(duplicates)}")

    if errors:
        print()
        print("Errors:")
        for e in errors:
            print(f"  {e}")

    if duplicates:
        print()
        print("Duplicates:")
        for d in duplicates:
            print(f"  {d}")

    if errors or duplicates:
        return 1

    print()
    print("All validations passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

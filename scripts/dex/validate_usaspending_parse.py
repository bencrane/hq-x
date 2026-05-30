#!/usr/bin/env python3
"""Validate USASpending CSV parsing against real FY2026 full + delta files.

Usage:  doppler run -- python scripts/validate_usaspending_parse.py

No database writes. Read-only validation.
"""

from __future__ import annotations

import csv
import io
import sys
import zipfile

from app.services.usaspending_column_map import (
    USASPENDING_COLUMN_COUNT,
    USASPENDING_COLUMNS,
    USASPENDING_DELTA_COLUMN_COUNT,
)
from app.services.usaspending_common import (
    UsaspendingSourceContext,
    build_usaspending_contract_row,
    parse_usaspending_csv_row,
)

FULL_ZIP_PATH = "/Users/benjamincrane/Downloads/FY2026_All_Contracts_Full_20260306.zip"
DELTA_ZIP_PATH = "/Users/benjamincrane/Downloads/FY(All)_All_Contracts_Delta_20260306.zip"

ROWS_TO_PARSE_FULL = 100
ROWS_TO_PARSE_DELTA = 10
ROWS_TO_PRINT = 10


def validate_full_file() -> bool:
    """Parse and validate first 100 rows from full FY2026 CSV."""
    print("=" * 80)
    print("FULL FILE VALIDATION")
    print("=" * 80)

    zf = zipfile.ZipFile(FULL_ZIP_PATH)
    csv_names = sorted(n for n in zf.namelist() if n.endswith(".csv"))
    first_csv = csv_names[0]
    print(f"ZIP: {FULL_ZIP_PATH}")
    print(f"CSV: {first_csv}")
    print()

    source_context = UsaspendingSourceContext(
        extract_date="2026-03-07",
        extract_type="FULL",
        source_filename=first_csv,
    )

    with zf.open(first_csv) as raw_f:
        text_f = io.TextIOWrapper(raw_f, encoding="utf-8")
        reader = csv.DictReader(text_f)

        # Verify header
        assert reader.fieldnames is not None
        header_count = len(reader.fieldnames)
        print(f"Header column count: {header_count}")
        assert header_count == USASPENDING_COLUMN_COUNT, (
            f"Expected {USASPENDING_COLUMN_COUNT}, got {header_count}"
        )
        print(f"Header count OK: {header_count} == {USASPENDING_COLUMN_COUNT}")
        print()

        all_ok = True
        for i, row_dict in enumerate(reader):
            if i >= ROWS_TO_PARSE_FULL:
                break

            row_num = i + 1
            parsed = parse_usaspending_csv_row(row_dict, row_num, is_delta=False)
            if parsed is None:
                print(f"Row {row_num}: PARSE FAILED")
                all_ok = False
                continue

            built = build_usaspending_contract_row(parsed, source_context)

            # Validate
            errors = []
            txn_key = built.get("contract_transaction_unique_key")
            if not txn_key:
                errors.append("missing contract_transaction_unique_key")

            uei = built.get("recipient_uei")
            if uei and len(uei) != 12:
                errors.append(f"recipient_uei length={len(uei)}, expected 12")

            if built.get("extract_date") != "2026-03-07":
                errors.append(f"extract_date={built.get('extract_date')}")

            if built.get("source_filename") != first_csv:
                errors.append(f"source_filename mismatch")

            status = "OK" if not errors else f"FAIL: {'; '.join(errors)}"
            if errors:
                all_ok = False

            if row_num <= ROWS_TO_PRINT:
                name = built.get("recipient_name") or "(empty)"
                naics = built.get("naics_code") or "(empty)"
                agency = built.get("awarding_agency_name") or "(empty)"
                uei_display = uei or "(empty)"
                txn_short = txn_key[:50] + "..." if txn_key and len(txn_key) > 50 else txn_key
                print(
                    f"Row {row_num}: txn_key={txn_short} | "
                    f"UEI={uei_display} | name={name} | "
                    f"naics={naics} | agency={agency} | {status}"
                )

    print()
    print(f"Full file: parsed {min(ROWS_TO_PARSE_FULL, i + 1)} rows")

    # Print field mapping verification for row 1
    print()
    print("=== FIELD MAPPING VERIFICATION (Row 1) ===")
    with zf.open(first_csv) as raw_f:
        text_f = io.TextIOWrapper(raw_f, encoding="utf-8")
        reader = csv.DictReader(text_f)
        row_dict = next(reader)
        parsed = parse_usaspending_csv_row(row_dict, 1, is_delta=False)
        assert parsed is not None
        built = build_usaspending_contract_row(parsed, source_context)

        # Check renamed columns
        renamed = [
            ("outlayed_amount_from_COVID-19_supplementals_for_overall_award",
             "outlayed_amount_from_covid_19_supplementals_for_overall_award"),
            ("obligated_amount_from_COVID-19_supplementals_for_overall_award",
             "obligated_amount_from_covid_19_supplementals_for_overall_award"),
            ("outlayed_amount_from_IIJA_supplemental_for_overall_award",
             "outlayed_amount_from_iija_supplemental_for_overall_award"),
            ("obligated_amount_from_IIJA_supplemental_for_overall_award",
             "obligated_amount_from_iija_supplemental_for_overall_award"),
            ("1862_land_grant_college", "col_1862_land_grant_college"),
            ("1890_land_grant_college", "col_1890_land_grant_college"),
            ("1994_land_grant_college", "col_1994_land_grant_college"),
        ]
        print("Renamed column mappings:")
        for csv_name, db_name in renamed:
            csv_val = row_dict.get(csv_name, "(missing)")
            db_val = built.get(db_name, "(missing)")
            match = "OK" if (csv_val.strip() or None) == db_val or (not csv_val.strip() and db_val is None) else "MISMATCH"
            print(f"  {csv_name} -> {db_name}: csv='{csv_val}' db='{db_val}' [{match}]")

    return all_ok


def validate_delta_file() -> bool:
    """Parse and validate first 10 rows from delta CSV."""
    print()
    print("=" * 80)
    print("DELTA FILE VALIDATION")
    print("=" * 80)

    zf = zipfile.ZipFile(DELTA_ZIP_PATH)
    csv_names = sorted(n for n in zf.namelist() if n.endswith(".csv"))
    first_csv = csv_names[0]
    print(f"ZIP: {DELTA_ZIP_PATH}")
    print(f"CSV: {first_csv}")
    print()

    source_context = UsaspendingSourceContext(
        extract_date="2026-03-08",
        extract_type="DELTA",
        source_filename=first_csv,
    )

    with zf.open(first_csv) as raw_f:
        text_f = io.TextIOWrapper(raw_f, encoding="utf-8")
        reader = csv.DictReader(text_f)

        assert reader.fieldnames is not None
        header_count = len(reader.fieldnames)
        print(f"Header column count: {header_count}")
        assert header_count == USASPENDING_DELTA_COLUMN_COUNT, (
            f"Expected {USASPENDING_DELTA_COLUMN_COUNT}, got {header_count}"
        )
        print(f"Header count OK: {header_count} == {USASPENDING_DELTA_COLUMN_COUNT}")
        print()

        all_ok = True
        for i, row_dict in enumerate(reader):
            if i >= ROWS_TO_PARSE_DELTA:
                break

            row_num = i + 1
            parsed = parse_usaspending_csv_row(row_dict, row_num, is_delta=True)
            if parsed is None:
                print(f"Row {row_num}: PARSE FAILED")
                all_ok = False
                continue

            built = build_usaspending_contract_row(parsed, source_context, is_delta=True)

            txn_key = built.get("contract_transaction_unique_key") or "(empty)"
            corr_del = built.get("correction_delete_ind") or "(empty)"
            agency = built.get("agency_id") or "(empty)"
            uei = built.get("recipient_uei") or "(empty)"
            name = built.get("recipient_name") or "(empty)"
            txn_short = txn_key[:50] + "..." if len(txn_key) > 50 else txn_key

            print(
                f"Row {row_num}: txn_key={txn_short} | "
                f"corr_del={corr_del} | agency_id={agency} | "
                f"UEI={uei} | name={name} | OK"
            )

    print()
    print(f"Delta file: parsed {min(ROWS_TO_PARSE_DELTA, i + 1)} rows")
    return all_ok


def main() -> int:
    full_ok = validate_full_file()
    delta_ok = validate_delta_file()

    print()
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"Full file:  {'PASS' if full_ok else 'FAIL'}")
    print(f"Delta file: {'PASS' if delta_ok else 'FAIL'}")
    print(f"Overall:    {'PASS' if (full_ok and delta_ok) else 'FAIL'}")

    return 0 if (full_ok and delta_ok) else 1


if __name__ == "__main__":
    sys.exit(main())

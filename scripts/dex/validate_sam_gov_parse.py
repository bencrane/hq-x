#!/usr/bin/env python3
"""SAM.gov extract parse validation against real downloaded file.

Validates the 142-field Public V2 parser against a real extract without
writing to any database.

Usage: doppler run -- python scripts/validate_sam_gov_parse.py
"""

from __future__ import annotations

import sys
import zipfile

from app.services.sam_gov_common import (
    SamGovSourceContext,
    build_sam_gov_entity_row,
    parse_sam_gov_dat_line,
)

ZIP_PATH = "/Users/benjamincrane/Downloads/SAM_PUBLIC_MONTHLY_V2_20260301.ZIP"
MAX_ROWS = 100

SOURCE_CONTEXT = SamGovSourceContext(
    extract_date="2026-03-01",
    extract_type="MONTHLY",
    source_filename="SAM_PUBLIC_MONTHLY_V2_20260301.dat",
    source_download_url="",
)


def main() -> int:
    # Open ZIP and find .dat file
    with zipfile.ZipFile(ZIP_PATH, "r") as zf:
        dat_names = [n for n in zf.namelist() if n.lower().endswith(".dat")]
        if not dat_names:
            print(f"ERROR: No .dat file found in {ZIP_PATH}")
            return 1
        dat_name = dat_names[0]
        print(f"Reading: {dat_name} from {ZIP_PATH}\n")

        with zf.open(dat_name) as dat_file:
            # Read first line — expect BOF
            first_line = dat_file.readline().decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")
            if not first_line.startswith("BOF"):
                print(f"ERROR: First line does not start with BOF: {first_line[:200]}")
                return 1
            bof_line = first_line
            print(f"BOF line: {bof_line}\n")

            parsed_ok = 0
            parse_failures = 0
            build_ok = 0
            build_failures = 0
            validation_passed = 0
            validation_failed = 0
            first_row: dict | None = None
            errors: list[str] = []

            for i in range(MAX_ROWS):
                raw = dat_file.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace")
                row_number = i + 1

                # Parse
                parsed = parse_sam_gov_dat_line(line, row_number=row_number)
                if parsed is None:
                    parse_failures += 1
                    errors.append(f"Row {row_number}: PARSE FAILURE | {line.rstrip()[:200]}")
                    continue
                parsed_ok += 1

                # Build row
                try:
                    row = build_sam_gov_entity_row(parsed, SOURCE_CONTEXT)
                except Exception as e:
                    build_failures += 1
                    errors.append(f"Row {row_number}: BUILD FAILURE | {e}")
                    continue
                build_ok += 1

                if first_row is None:
                    first_row = row

                # Validate
                checks_ok = True
                uei = row.get("unique_entity_id") or ""
                if not uei:
                    errors.append(f"Row {row_number}: unique_entity_id is empty")
                    checks_ok = False

                if row.get("end_of_record_indicator") != "!end":
                    errors.append(f"Row {row_number}: end_of_record_indicator = {row.get('end_of_record_indicator')!r}")
                    checks_ok = False

                extract_code = row.get("sam_extract_code") or ""
                if extract_code not in ("A", "E"):
                    errors.append(f"Row {row_number}: sam_extract_code = {extract_code!r} (expected A or E)")
                    checks_ok = False

                if row.get("extract_date") != "2026-03-01":
                    errors.append(f"Row {row_number}: extract_date = {row.get('extract_date')!r}")
                    checks_ok = False

                if not row.get("source_filename"):
                    errors.append(f"Row {row_number}: source_filename is empty")
                    checks_ok = False

                if row.get("row_position") != row_number:
                    errors.append(f"Row {row_number}: row_position = {row.get('row_position')} (expected {row_number})")
                    checks_ok = False

                if checks_ok:
                    validation_passed += 1
                else:
                    validation_failed += 1

                # Print first 10 rows
                if row_number <= 10:
                    name = row.get("legal_business_name") or "?"
                    state = row.get("physical_address_province_or_state") or "?"
                    naics = row.get("primary_naics") or "?"
                    ec = row.get("extract_code") or "?"
                    status = "OK" if checks_ok else "FAIL"
                    print(f"Row {row_number}: UEI={uei} | name={name} | state={state} | naics={naics} | extract_code={ec} | {status}")

    # Summary
    print(f"\n{'=' * 50}")
    print("VALIDATION SUMMARY")
    print(f"{'=' * 50}")
    print(f"BOF line: {bof_line}")
    print(f"Lines read: {parsed_ok + parse_failures}")
    print(f"Parsed OK: {parsed_ok}")
    print(f"Parse failures: {parse_failures}")
    print(f"Row build OK: {build_ok}")
    print(f"Row build failures: {build_failures}")
    print(f"Validation checks passed: {validation_passed}")
    print(f"Validation checks failed: {validation_failed}")

    if first_row:
        print(f"\nSample field mapping verification (row 1):")
        print(f'  unique_entity_id (V2 pos 1) = "{first_row.get("unique_entity_id")}"')
        print(f'  legal_business_name (V2 pos 12) = "{first_row.get("legal_business_name")}"')
        print(f'  entity_url (V2 pos 27) = "{first_row.get("entity_url")}"')
        print(f'  primary_naics (V2 pos 33) = "{first_row.get("primary_naics")}"')
        print(f'  physical_address_province_or_state (V2 pos 19) = "{first_row.get("physical_address_province_or_state")}"')
        print(f'  end_of_record_indicator (V2 pos 142) = "{first_row.get("end_of_record_indicator")}"')

    if errors:
        print(f"\nERRORS ({len(errors)}):")
        for err in errors:
            print(f"  {err}")

    if validation_failed > 0 or parse_failures > 0 or build_failures > 0:
        print("\nRESULT: FAIL")
        return 1
    else:
        print("\nRESULT: PASS")
        return 0


if __name__ == "__main__":
    sys.exit(main())

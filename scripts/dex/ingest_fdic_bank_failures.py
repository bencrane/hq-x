#!/usr/bin/env python3
"""Ingest FDIC failed bank list into entities.fdic_bank_failures.

Usage:
SUPER_ADMIN_JWT_SECRET=unused-for-ingest doppler run --project data-engine-x-api --config prd -- .venv/bin/python3 scripts/ingest_fdic_bank_failures.py
"""

from __future__ import annotations

import csv
from pathlib import Path

from psycopg import connect

from app.config import get_settings

EXPECTED_HEADERS = [
    "Bank Name",
    "City",
    "State",
    "Cert",
    "Acquiring Institution",
    "Closing Date",
    "Fund",
]
EXPECTED_ROW_COUNT = 573
SOURCE_FILENAME = "fdic_bank_failures.csv"
SOURCE_PROVIDER = "fdic"


def _clean_header(value: str) -> str:
    return value.strip().lstrip("\ufeff")


def load_fdic_rows(csv_path: Path) -> list[tuple[str, str | None, str | None, str, str | None, str, str | None]]:
    rows: list[tuple[str, str | None, str | None, str, str | None, str, str | None]] = []

    parse_error: Exception | None = None
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            rows.clear()
            with csv_path.open("r", encoding=encoding, newline="") as f:
                reader = csv.reader(f)
                header = next(reader, None)
                if header is None:
                    raise RuntimeError("CSV is empty.")

                normalized_header = [_clean_header(col) for col in header]
                if normalized_header != EXPECTED_HEADERS:
                    raise RuntimeError(
                        f"Unexpected headers. Expected {EXPECTED_HEADERS}, got {normalized_header}"
                    )

                for line_number, row in enumerate(reader, start=2):
                    if len(row) != 7:
                        raise RuntimeError(
                            f"Row {line_number} has {len(row)} columns; expected 7."
                        )

                    bank_name = row[0].strip()
                    city = row[1].strip() or None
                    state = row[2].strip() or None
                    cert = row[3].strip()
                    acquiring_institution = row[4].strip() or None
                    closing_date = row[5].strip()
                    fund = row[6].strip() or None

                    if not bank_name:
                        raise RuntimeError(f"Row {line_number} missing required field: Bank Name")
                    if not cert:
                        raise RuntimeError(f"Row {line_number} missing required field: Cert")
                    if not closing_date:
                        raise RuntimeError(f"Row {line_number} missing required field: Closing Date")

                    rows.append(
                        (
                            bank_name,
                            city,
                            state,
                            cert,
                            acquiring_institution,
                            closing_date,
                            fund,
                        )
                    )
            break
        except UnicodeDecodeError as exc:
            parse_error = exc
    else:
        raise RuntimeError(f"Unable to decode CSV with supported encodings: {parse_error}")

    if len(rows) != EXPECTED_ROW_COUNT:
        raise RuntimeError(
            f"Expected {EXPECTED_ROW_COUNT} data rows, got {len(rows)}."
        )

    return rows


def ingest_fdic_rows(
    rows: list[tuple[str, str | None, str | None, str, str | None, str, str | None]]
) -> int:
    settings = get_settings()

    insert_sql = """
    INSERT INTO entities.fdic_bank_failures (
        bank_name,
        city,
        state,
        cert,
        acquiring_institution,
        closing_date,
        fund,
        source_filename,
        source_provider
    )
    VALUES (
        %s, %s, %s, %s, %s, %s, %s, %s, %s
    )
    ON CONFLICT (cert, closing_date, bank_name)
    DO UPDATE SET
        city = EXCLUDED.city,
        state = EXCLUDED.state,
        acquiring_institution = EXCLUDED.acquiring_institution,
        fund = EXCLUDED.fund,
        source_filename = EXCLUDED.source_filename,
        source_provider = EXCLUDED.source_provider,
        ingested_at = NOW()
    """

    with connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.executemany(
                insert_sql,
                [
                    (
                        bank_name,
                        city,
                        state,
                        cert,
                        acquiring_institution,
                        closing_date,
                        fund,
                        SOURCE_FILENAME,
                        SOURCE_PROVIDER,
                    )
                    for (
                        bank_name,
                        city,
                        state,
                        cert,
                        acquiring_institution,
                        closing_date,
                        fund,
                    ) in rows
                ],
            )
            conn.commit()

            cur.execute("SELECT COUNT(*) FROM entities.fdic_bank_failures")
            total_count = cur.fetchone()[0]

    return int(total_count)


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    csv_path = repo_root / SOURCE_FILENAME
    if not csv_path.exists():
        raise RuntimeError(f"CSV not found: {csv_path}")

    rows = load_fdic_rows(csv_path)
    total_count = ingest_fdic_rows(rows)

    print(
        f"Ingested {len(rows)} FDIC bank failure rows from {SOURCE_FILENAME}. "
        f"Table row count is now {total_count}."
    )


if __name__ == "__main__":
    main()

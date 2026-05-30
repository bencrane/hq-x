#!/usr/bin/env python3
"""Ingest US ZIP code crosswalk into `lookup.us_zip_codes`.

Source CSV is committed at scripts/data/us_zip_codes.csv.
Original upstream:
  https://github.com/growthenginenowoslawski/coldoutboundskills
    Common Outbound Lists/us-zip-codes.csv

Usage:
  doppler run -- python3 scripts/ingest_us_zip_codes.py
"""

from __future__ import annotations

import csv
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config import get_settings

try:
    from psycopg import connect
except ModuleNotFoundError:
    venv_python = REPO_ROOT / ".venv/bin/python3"
    if venv_python.exists() and Path(sys.executable) != venv_python:
        os.execv(str(venv_python), [str(venv_python), *sys.argv])
    raise

CSV_PATH = REPO_ROOT / "scripts/data/us_zip_codes.csv"
SOURCE_URL = (
    "https://github.com/growthenginenowoslawski/coldoutboundskills/raw/refs/heads/main/"
    "Common%20Outbound%20Lists/us-zip-codes.csv"
)
EXPECTED_ROW_COUNT = 42735


def _empty_to_none(value: str) -> str | None:
    value = value.strip()
    return value if value else None


def load_rows() -> list[tuple[Any, ...]]:
    if not CSV_PATH.exists():
        raise RuntimeError(f"Source CSV not found: {CSV_PATH}")

    rows: list[tuple[Any, ...]] = []
    seen_zips: set[str] = set()

    with CSV_PATH.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            zip_raw = row["zip"].strip()
            if not zip_raw:
                continue
            zip_padded = zip_raw.zfill(5)
            if len(zip_padded) != 5 or not zip_padded.isdigit():
                raise RuntimeError(f"Invalid ZIP after padding: {zip_raw!r} -> {zip_padded!r}")
            if zip_padded in seen_zips:
                raise RuntimeError(f"Duplicate ZIP in source CSV: {zip_padded}")
            seen_zips.add(zip_padded)

            rows.append(
                (
                    zip_padded,
                    row["primary_city"].strip(),
                    row["state"].strip().upper(),
                    _empty_to_none(row["timezone"]),
                    row["area_codes"].strip(),
                    _empty_to_none(row["world_region"]),
                    _empty_to_none(row["country"]),
                    float(row["latitude"]),
                    float(row["longitude"]),
                    int(row["irs_estimated_population"]),
                    SOURCE_URL,
                )
            )

    if len(rows) != EXPECTED_ROW_COUNT:
        raise RuntimeError(
            f"Expected {EXPECTED_ROW_COUNT} rows, got {len(rows)}."
        )

    return rows


def upsert_rows(rows: list[tuple[Any, ...]]) -> tuple[int, dict[str, int]]:
    settings = get_settings()

    sql = """
    INSERT INTO lookup.us_zip_codes (
        zip,
        primary_city,
        state,
        timezone,
        area_codes,
        world_region,
        country,
        latitude,
        longitude,
        irs_estimated_population,
        source_url,
        updated_at
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
    ON CONFLICT (zip)
    DO UPDATE SET
        primary_city = EXCLUDED.primary_city,
        state = EXCLUDED.state,
        timezone = EXCLUDED.timezone,
        area_codes = EXCLUDED.area_codes,
        world_region = EXCLUDED.world_region,
        country = EXCLUDED.country,
        latitude = EXCLUDED.latitude,
        longitude = EXCLUDED.longitude,
        irs_estimated_population = EXCLUDED.irs_estimated_population,
        source_url = EXCLUDED.source_url,
        updated_at = NOW()
    WHERE
        lookup.us_zip_codes.primary_city IS DISTINCT FROM EXCLUDED.primary_city
        OR lookup.us_zip_codes.state IS DISTINCT FROM EXCLUDED.state
        OR lookup.us_zip_codes.timezone IS DISTINCT FROM EXCLUDED.timezone
        OR lookup.us_zip_codes.area_codes IS DISTINCT FROM EXCLUDED.area_codes
        OR lookup.us_zip_codes.world_region IS DISTINCT FROM EXCLUDED.world_region
        OR lookup.us_zip_codes.country IS DISTINCT FROM EXCLUDED.country
        OR lookup.us_zip_codes.latitude IS DISTINCT FROM EXCLUDED.latitude
        OR lookup.us_zip_codes.longitude IS DISTINCT FROM EXCLUDED.longitude
        OR lookup.us_zip_codes.irs_estimated_population IS DISTINCT FROM EXCLUDED.irs_estimated_population
        OR lookup.us_zip_codes.source_url IS DISTINCT FROM EXCLUDED.source_url
    """

    with connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, rows)
            conn.commit()

            cur.execute("SELECT COUNT(*) FROM lookup.us_zip_codes")
            total = int(cur.fetchone()[0])

            cur.execute(
                "SELECT state, COUNT(*) AS n FROM lookup.us_zip_codes "
                "GROUP BY state ORDER BY n DESC LIMIT 5"
            )
            top_states = {row[0]: int(row[1]) for row in cur.fetchall()}

    return total, top_states


def main() -> None:
    rows = load_rows()
    total, top_states = upsert_rows(rows)
    print(
        json.dumps(
            {
                "rows_in_csv": len(rows),
                "table_count": total,
                "top_5_states_by_zip_count": top_states,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

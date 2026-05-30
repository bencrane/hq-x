#!/usr/bin/env python3
"""Ingest Entrepreneur.com franchise directory CSV into
`entities.source_franchisors_entrepreneur`.

Source: https://www.entrepreneur.com/franchises (manually scraped CSV export).
Per row: name + official_domain + description + parent_category +
primary_category + min_investment_usd + growth_percent + units_latest_year +
units_latest_total + type + slug.

Per apps/data-engine-x/CLAUDE.md §"Source ingest invariant":
  - 1:1 typed column mirror of the CSV columns
  - raw_source_row jsonb NOT NULL preserves the verbatim CSV row as JSON
  - 9-column canonical provenance set
  - PK: slug
  - Idempotency: ON CONFLICT (slug) DO UPDATE WHERE
    raw_source_row IS DISTINCT FROM EXCLUDED.raw_source_row
  - Sibling: ops.franchisors_entrepreneur_ingest_runs

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/ingest_franchisors_entrepreneur.py \\
    --csv-path /path/to/entrepreneur-franchises-export.csv \\
    [--source-download-url https://www.entrepreneur.com/franchises]
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("ingest_franchisors_entrepreneur")

SOURCE_PROVIDER = "entrepreneur_franchises"

# CSV column → table column mapping. The CSV has one column with a space
# (`Official Domain domain`) and `type` which collides with SQL keyword;
# we renamed the latter to `franchise_type` in the table.
_CSV_HEADERS = (
    "name",
    "Official Domain domain",
    "description",
    "parent_category",
    "primary_category",
    "min_investment_usd",
    "growth_percent",
    "units_latest_year",
    "units_latest_total",
    "type",
    "slug",
)


def _parse_int(value: str | None) -> int | None:
    if value is None or value.strip() == "":
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def _parse_float(value: str | None) -> float | None:
    if value is None or value.strip() == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _empty_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    s = value.strip()
    return s if s else None


def _load_csv(csv_path: Path) -> list[tuple]:
    """Read the Entrepreneur.com franchise CSV; return list of upsert tuples.

    Each tuple maps to the INSERT placeholder order:
      (slug, name, official_domain, description, parent_category, primary_category,
       min_investment_usd, growth_percent, units_latest_year, units_latest_total,
       franchise_type, raw_source_row).
    """
    rows: list[tuple] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)

        actual_headers = tuple(reader.fieldnames or ())
        if actual_headers != _CSV_HEADERS:
            raise RuntimeError(
                f"Unexpected CSV headers.\n  expected: {_CSV_HEADERS}\n  got:      {actual_headers}"
            )

        for line_no, row in enumerate(reader, start=2):
            slug = _empty_to_none(row.get("slug"))
            name = _empty_to_none(row.get("name"))
            if slug is None:
                raise RuntimeError(f"row {line_no}: missing slug")
            if name is None:
                raise RuntimeError(f"row {line_no}: missing name (slug={slug})")

            raw_json = json.dumps(row, ensure_ascii=False)

            rows.append(
                (
                    slug,
                    name,
                    _empty_to_none(row.get("Official Domain domain")),
                    _empty_to_none(row.get("description")),
                    _empty_to_none(row.get("parent_category")),
                    _empty_to_none(row.get("primary_category")),
                    _parse_float(row.get("min_investment_usd")),
                    _empty_to_none(row.get("growth_percent")),
                    _parse_int(row.get("units_latest_year")),
                    _parse_int(row.get("units_latest_total")),
                    _empty_to_none(row.get("type")),
                    raw_json,
                )
            )

    return rows


def _start_run(
    *,
    source_observed_at: datetime,
    source_filename: str,
    source_download_url: str | None,
) -> uuid.UUID:
    run_id = uuid.uuid4()
    with psycopg.connect(os.environ["DEX_DB_URL_DIRECT"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ops.franchisors_entrepreneur_ingest_runs (
                  run_id, status, source_observed_at, source_filename, source_download_url
                ) VALUES (%s, 'running', %s, %s, %s)
                """,
                (str(run_id), source_observed_at, source_filename, source_download_url),
            )
        conn.commit()
    return run_id


def _complete_run(
    run_id: uuid.UUID,
    *,
    rows_seen: int,
    rows_upserted: int,
    rows_unchanged: int,
) -> None:
    with psycopg.connect(os.environ["DEX_DB_URL_DIRECT"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE ops.franchisors_entrepreneur_ingest_runs
                   SET status = 'succeeded',
                       completed_at = now(),
                       rows_seen = %s,
                       rows_upserted = %s,
                       rows_unchanged = %s
                 WHERE run_id = %s
                """,
                (rows_seen, rows_upserted, rows_unchanged, str(run_id)),
            )
        conn.commit()


def _fail_run(run_id: uuid.UUID, error_text: str) -> None:
    try:
        with psycopg.connect(os.environ["DEX_DB_URL_DIRECT"]) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE ops.franchisors_entrepreneur_ingest_runs
                       SET status = 'failed',
                           completed_at = now(),
                           error_text = %s
                     WHERE run_id = %s
                    """,
                    (error_text[:8000], str(run_id)),
                )
            conn.commit()
    except Exception:
        logger.exception("also failed to mark run as failed")


def _upsert_rows(
    rows: list[tuple],
    *,
    run_id: uuid.UUID,
    source_observed_at: datetime,
    source_filename: str,
    source_download_url: str | None,
) -> tuple[int, int]:
    """Returns (rows_upserted, rows_unchanged)."""
    insert_sql = """
    INSERT INTO entities.source_franchisors_entrepreneur (
      slug, name, official_domain, description, parent_category, primary_category,
      min_investment_usd, growth_percent, units_latest_year, units_latest_total,
      franchise_type, raw_source_row,
      source_provider, source_filename, source_download_url, source_observed_at,
      source_run_metadata, source_task_id, source_schedule_id, ingested_at
    ) VALUES (
      %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb,
      %s, %s, %s, %s, %s::jsonb, %s, %s, now()
    )
    ON CONFLICT (slug) DO UPDATE SET
      name = EXCLUDED.name,
      official_domain = EXCLUDED.official_domain,
      description = EXCLUDED.description,
      parent_category = EXCLUDED.parent_category,
      primary_category = EXCLUDED.primary_category,
      min_investment_usd = EXCLUDED.min_investment_usd,
      growth_percent = EXCLUDED.growth_percent,
      units_latest_year = EXCLUDED.units_latest_year,
      units_latest_total = EXCLUDED.units_latest_total,
      franchise_type = EXCLUDED.franchise_type,
      raw_source_row = EXCLUDED.raw_source_row,
      source_filename = EXCLUDED.source_filename,
      source_download_url = EXCLUDED.source_download_url,
      source_observed_at = EXCLUDED.source_observed_at,
      source_run_metadata = EXCLUDED.source_run_metadata,
      ingested_at = now()
    WHERE entities.source_franchisors_entrepreneur.raw_source_row IS DISTINCT FROM EXCLUDED.raw_source_row
    """

    run_metadata = json.dumps({"run_id": str(run_id)}, ensure_ascii=False)

    rows_upserted = 0
    with psycopg.connect(os.environ["DEX_DB_URL_DIRECT"]) as conn:
        with conn.cursor() as cur:
            for r in rows:
                cur.execute(
                    insert_sql,
                    (
                        *r,
                        SOURCE_PROVIDER,
                        source_filename,
                        source_download_url,
                        source_observed_at,
                        run_metadata,
                        None,  # source_task_id
                        None,  # source_schedule_id
                    ),
                )
                rows_upserted += cur.rowcount
        conn.commit()

    rows_unchanged = len(rows) - rows_upserted
    return rows_upserted, rows_unchanged


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--csv-path",
        required=True,
        help="Absolute path to the Entrepreneur.com franchise-directory CSV export",
    )
    parser.add_argument(
        "--source-download-url",
        default="https://www.entrepreneur.com/franchises",
        help="URL of the upstream source (recorded as source_download_url)",
    )
    args = parser.parse_args()

    if not os.environ.get("DEX_DB_URL_DIRECT"):
        raise SystemExit("FAIL: DEX_DB_URL_DIRECT not set")

    csv_path = Path(args.csv_path).resolve()
    if not csv_path.is_file():
        raise SystemExit(f"FAIL: CSV not found at {csv_path}")

    source_observed_at = datetime.fromtimestamp(csv_path.stat().st_mtime, tz=timezone.utc)
    source_filename = csv_path.name

    logger.info(f"csv_path={csv_path}")
    logger.info(f"source_observed_at={source_observed_at.isoformat()}")
    logger.info(f"source_filename={source_filename}")
    logger.info(f"source_download_url={args.source_download_url}")

    logger.info("loading CSV …")
    rows = _load_csv(csv_path)
    logger.info(f"  loaded {len(rows):,} rows")

    run_id = _start_run(
        source_observed_at=source_observed_at,
        source_filename=source_filename,
        source_download_url=args.source_download_url,
    )
    logger.info(f"run_id={run_id}")

    try:
        upserted, unchanged = _upsert_rows(
            rows,
            run_id=run_id,
            source_observed_at=source_observed_at,
            source_filename=source_filename,
            source_download_url=args.source_download_url,
        )
        logger.info(f"  rows_upserted={upserted:,}  rows_unchanged={unchanged:,}")

        _complete_run(
            run_id,
            rows_seen=len(rows),
            rows_upserted=upserted,
            rows_unchanged=unchanged,
        )
        logger.info(f"OK — run_id={run_id}")
        return 0
    except Exception as exc:
        logger.exception("ingest failed")
        _fail_run(run_id, str(exc))
        raise


if __name__ == "__main__":
    raise SystemExit(main())

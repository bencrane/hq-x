#!/usr/bin/env python3
"""IRS Exempt Organizations Business Master File (EO BMF) — bulk-CSV ingest.

4 regional CSVs published monthly (cumulative snapshot). Source-first per
the source-first invariant: every IRS-published field lands in
entities.source_irs_bmf. 1:1 column mirror, raw_source_row jsonb preserved.

  eo1.csv  (Region 1: Northeast)
  eo2.csv  (Region 2: Mid-Atlantic + Great Lakes)
  eo3.csv  (Region 3: Gulf Coast + Pacific)
  eo4.csv  (Region 4: International + all others)

Source URL pattern:
  https://www.irs.gov/pub/irs-soi/eo{1,2,3,4}.csv

Idempotency: PK=ein, ON CONFLICT (ein) DO UPDATE SET ...
Audit: ops.irs_bmf_ingest_runs.

Usage:
  PYTHONPATH=. doppler run -- python3 scripts/run_irs_bmf_ingest.py --region all
  PYTHONPATH=. doppler run -- python3 scripts/run_irs_bmf_ingest.py --region 1
  PYTHONPATH=. doppler run -- python3 scripts/run_irs_bmf_ingest.py --fixture tests/fixtures/irs_bmf_smoke.csv
  PYTHONPATH=. doppler run -- python3 scripts/run_irs_bmf_ingest.py --region all --dry-run
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import psycopg
from psycopg.types.json import Jsonb


BMF_BASE_URL = "https://www.irs.gov/pub/irs-soi/"
BMF_REGIONS = {1: "eo1.csv", 2: "eo2.csv", 3: "eo3.csv", 4: "eo4.csv"}

DEFAULT_BATCH_SIZE = 25_000
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 5

# The IRS CSV header uses uppercase names; note GROUP is reserved in Postgres —
# we remap it to group_exemption_number in both the table and below.
BMF_CSV_HEADERS = [
    "EIN",
    "NAME",
    "ICO",
    "STREET",
    "CITY",
    "STATE",
    "ZIP",
    "GROUP",           # → group_exemption_number
    "SUBSECTION",
    "AFFILIATION",
    "CLASSIFICATION",
    "RULING",
    "DEDUCTIBILITY",
    "FOUNDATION",
    "ACTIVITY",
    "ORGANIZATION",
    "STATUS",
    "TAX_PERIOD",
    "ASSET_CD",
    "INCOME_CD",
    "FILING_REQ_CD",
    "PF_FILING_REQ_CD",
    "ACCT_PD",
    "ASSET_AMT",
    "INCOME_AMT",
    "REVENUE_AMT",
    "NTEE_CD",
    "SORT_NAME",
]

# Postgres column names corresponding to BMF_CSV_HEADERS (EIN → ein, GROUP →
# group_exemption_number, rest lowercased).
BMF_COLS = [
    "ein",
    "name",
    "ico",
    "street",
    "city",
    "state",
    "zip",
    "group_exemption_number",
    "subsection",
    "affiliation",
    "classification",
    "ruling",
    "deductibility",
    "foundation",
    "activity",
    "organization",
    "status",
    "tax_period",
    "asset_cd",
    "income_cd",
    "filing_req_cd",
    "pf_filing_req_cd",
    "acct_pd",
    "asset_amt",
    "income_amt",
    "revenue_amt",
    "ntee_cd",
    "sort_name",
]

NUMERIC_COLS = {"asset_amt", "income_amt", "revenue_amt"}


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("irs-bmf-ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# DB helpers
# --------------------------------------------------------------------------- #


def _database_url() -> str:
    url = os.environ.get("DEX_DB_URL_POOLED")
    if not url:
        raise RuntimeError("DEX_DB_URL_POOLED not set — run via doppler run --")
    return url


def _coerce_numeric(val: str) -> Any:
    """Return None for empty string, else pass through for Postgres numeric cast."""
    v = val.strip()
    return None if v == "" else v


def _build_row(
    csv_row: dict[str, str],
    *,
    source_filename: str,
    source_download_url: str,
    source_observed_at: datetime,
    source_run_metadata: dict[str, Any],
) -> dict[str, Any]:
    """Build a DB row dict from a CSV DictReader row."""
    row: dict[str, Any] = {}

    # Map CSV headers → Postgres columns
    for pg_col, csv_hdr in zip(BMF_COLS, BMF_CSV_HEADERS):
        raw = csv_row.get(csv_hdr, "")
        if pg_col in NUMERIC_COLS:
            row[pg_col] = _coerce_numeric(raw)
        else:
            v = raw.strip()
            row[pg_col] = v if v else None

    # Provenance
    row["raw_source_row"] = Jsonb(dict(csv_row))
    row["source_provider"] = "irs_bmf"
    row["source_filename"] = source_filename
    row["source_download_url"] = source_download_url
    row["source_observed_at"] = source_observed_at
    row["source_run_metadata"] = Jsonb(source_run_metadata)
    row["source_task_id"] = os.environ.get("TRIGGER_TASK_ID")
    row["source_schedule_id"] = os.environ.get("TRIGGER_SCHEDULE_ID")

    return row


# --------------------------------------------------------------------------- #
# Upsert logic
# --------------------------------------------------------------------------- #

_UPSERT_SQL = """
INSERT INTO entities.source_irs_bmf (
    ein, name, ico, street, city, state, zip, group_exemption_number,
    subsection, affiliation, classification, ruling, deductibility,
    foundation, activity, organization, status, tax_period,
    asset_cd, income_cd, filing_req_cd, pf_filing_req_cd, acct_pd,
    asset_amt, income_amt, revenue_amt, ntee_cd, sort_name,
    raw_source_row, source_provider, source_filename, source_download_url,
    source_observed_at, source_run_metadata, source_task_id,
    source_schedule_id
)
SELECT
    ein, name, ico, street, city, state, zip, group_exemption_number,
    subsection, affiliation, classification, ruling, deductibility,
    foundation, activity, organization, status, tax_period,
    asset_cd, income_cd, filing_req_cd, pf_filing_req_cd, acct_pd,
    asset_amt::numeric, income_amt::numeric, revenue_amt::numeric,
    ntee_cd, sort_name,
    raw_source_row, source_provider, source_filename, source_download_url,
    source_observed_at, source_run_metadata, source_task_id,
    source_schedule_id
FROM _stage_source_irs_bmf
ON CONFLICT (ein) DO UPDATE SET
    name                   = EXCLUDED.name,
    ico                    = EXCLUDED.ico,
    street                 = EXCLUDED.street,
    city                   = EXCLUDED.city,
    state                  = EXCLUDED.state,
    zip                    = EXCLUDED.zip,
    group_exemption_number = EXCLUDED.group_exemption_number,
    subsection             = EXCLUDED.subsection,
    affiliation            = EXCLUDED.affiliation,
    classification         = EXCLUDED.classification,
    ruling                 = EXCLUDED.ruling,
    deductibility          = EXCLUDED.deductibility,
    foundation             = EXCLUDED.foundation,
    activity               = EXCLUDED.activity,
    organization           = EXCLUDED.organization,
    status                 = EXCLUDED.status,
    tax_period             = EXCLUDED.tax_period,
    asset_cd               = EXCLUDED.asset_cd,
    income_cd              = EXCLUDED.income_cd,
    filing_req_cd          = EXCLUDED.filing_req_cd,
    pf_filing_req_cd       = EXCLUDED.pf_filing_req_cd,
    acct_pd                = EXCLUDED.acct_pd,
    asset_amt              = EXCLUDED.asset_amt,
    income_amt             = EXCLUDED.income_amt,
    revenue_amt            = EXCLUDED.revenue_amt,
    ntee_cd                = EXCLUDED.ntee_cd,
    sort_name              = EXCLUDED.sort_name,
    raw_source_row         = EXCLUDED.raw_source_row,
    source_provider        = EXCLUDED.source_provider,
    source_filename        = EXCLUDED.source_filename,
    source_download_url    = EXCLUDED.source_download_url,
    source_observed_at     = EXCLUDED.source_observed_at,
    source_run_metadata    = EXCLUDED.source_run_metadata,
    source_task_id         = EXCLUDED.source_task_id,
    source_schedule_id     = EXCLUDED.source_schedule_id,
    updated_at             = now()
"""


def _flush_batch(
    conn: psycopg.Connection,
    batch: list[dict[str, Any]],
    dry_run: bool,
) -> tuple[int, int]:
    """Copy batch into staging table, upsert, return (inserted, updated) approx."""
    if not batch:
        return 0, 0

    if dry_run:
        log.info("dry-run: would upsert %d rows", len(batch))
        return 0, 0

    with conn.cursor() as cur:
        # Temp stage table
        cur.execute("""
            CREATE TEMP TABLE IF NOT EXISTS _stage_source_irs_bmf (
                LIKE entities.source_irs_bmf INCLUDING DEFAULTS
            ) ON COMMIT DELETE ROWS
        """)
        cur.execute("TRUNCATE _stage_source_irs_bmf")

        cols = list(batch[0].keys())
        with cur.copy(
            f"COPY _stage_source_irs_bmf ({', '.join(cols)}) FROM STDIN (FORMAT BINARY)"
        ) as copy:
            copy.set_types([
                # Map psycopg binary types — let psycopg infer from python types
            ])
            for row in batch:
                copy.write_row([row[c] for c in cols])

        cur.execute(_UPSERT_SQL)
        affected = cur.rowcount

    conn.commit()
    return affected, 0


def _flush_batch_text(
    conn: psycopg.Connection,
    batch: list[dict[str, Any]],
    dry_run: bool,
) -> int:
    """Bulk-load via TEXT-format COPY (one round trip per batch), then UPSERT.

    Text COPY avoids the type-negotiation pitfalls of FORMAT BINARY while still
    sending the whole batch in a single round trip — orders of magnitude faster
    than per-row INSERT (~25k round trips per batch).
    """
    if not batch:
        return 0
    if dry_run:
        log.info("dry-run: would upsert %d rows", len(batch))
        return 0

    with conn.cursor() as cur:
        cur.execute("""
            CREATE TEMP TABLE IF NOT EXISTS _stage_source_irs_bmf (
                LIKE entities.source_irs_bmf INCLUDING DEFAULTS
            ) ON COMMIT DELETE ROWS
        """)
        cur.execute("TRUNCATE _stage_source_irs_bmf")

        cols = list(batch[0].keys())
        with cur.copy(
            f"COPY _stage_source_irs_bmf ({', '.join(cols)}) FROM STDIN"
        ) as copy:
            for row in batch:
                copy.write_row([row[c] for c in cols])

        cur.execute(_UPSERT_SQL)
        affected = cur.rowcount

    conn.commit()
    return affected


# --------------------------------------------------------------------------- #
# Run record helpers
# --------------------------------------------------------------------------- #


def _open_run(conn: psycopg.Connection, source_url: str, region: int | None) -> str:
    dataset_form = f"EO_BMF_REGION_{region}" if region else "EO_BMF"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.irs_bmf_ingest_runs
                (dataset_form, status, source_url)
            VALUES (%s, 'running', %s)
            RETURNING id::text
            """,
            (dataset_form, source_url),
        )
        run_id: str = cur.fetchone()[0]  # type: ignore[index]
    conn.commit()
    return run_id


def _close_run(
    conn: psycopg.Connection,
    run_id: str,
    *,
    status: str,
    rows_in_csv: int,
    rows_inserted: int,
    rows_updated: int,
    started_at: datetime,
    error_message: str | None = None,
) -> None:
    finished_at = datetime.now(timezone.utc)
    duration = (finished_at - started_at).total_seconds()
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ops.irs_bmf_ingest_runs SET
                status            = %s,
                rows_in_csv       = %s,
                rows_inserted     = %s,
                rows_updated      = %s,
                finished_at       = %s,
                duration_seconds  = %s,
                error_message     = %s
            WHERE id = %s::uuid
            """,
            (
                status,
                rows_in_csv,
                rows_inserted,
                rows_updated,
                finished_at,
                duration,
                error_message,
                run_id,
            ),
        )
    conn.commit()


# --------------------------------------------------------------------------- #
# Per-region ingest
# --------------------------------------------------------------------------- #


def _ingest_region(
    conn: psycopg.Connection,
    *,
    region: int,
    fixture: Path | None,
    dry_run: bool,
    batch_size: int,
) -> None:
    filename = BMF_REGIONS[region]
    url = BMF_BASE_URL + filename
    source_observed_at = datetime.now(timezone.utc)
    source_meta: dict[str, Any] = {"region": region}

    run_id = _open_run(conn, url if not fixture else str(fixture), region)
    started_at = datetime.now(timezone.utc)

    try:
        rows_in_csv = 0
        rows_inserted = 0

        if fixture:
            log.info("region %d: loading fixture %s", region, fixture)
            reader_src = fixture.open(newline="", encoding="utf-8-sig")
        else:
            log.info("region %d: streaming %s", region, url)
            # Streaming via httpx; handle retries
            reader_src = None  # set below

        if fixture:
            reader = csv.DictReader(reader_src)
            batch: list[dict[str, Any]] = []
            for csv_row in reader:
                rows_in_csv += 1
                row = _build_row(
                    csv_row,
                    source_filename=fixture.name,
                    source_download_url=f"fixture://{fixture}",
                    source_observed_at=source_observed_at,
                    source_run_metadata=source_meta,
                )
                batch.append(row)
                if len(batch) >= batch_size:
                    rows_inserted += _flush_batch_text(conn, batch, dry_run)
                    batch.clear()
            if batch:
                rows_inserted += _flush_batch_text(conn, batch, dry_run)
            reader_src.close()
        else:
            with httpx.Client(timeout=120) as client:
                retries = 0
                while True:
                    resp = client.get(url, follow_redirects=True)
                    if resp.status_code in RETRY_STATUSES and retries < MAX_RETRIES:
                        retries += 1
                        time.sleep(2**retries)
                        continue
                    resp.raise_for_status()
                    break

                text = resp.content.decode("utf-8-sig", errors="replace")
                reader = csv.DictReader(io.StringIO(text))
                batch = []
                for csv_row in reader:
                    rows_in_csv += 1
                    row = _build_row(
                        csv_row,
                        source_filename=filename,
                        source_download_url=url,
                        source_observed_at=source_observed_at,
                        source_run_metadata=source_meta,
                    )
                    batch.append(row)
                    if len(batch) >= batch_size:
                        rows_inserted += _flush_batch_text(conn, batch, dry_run)
                        batch.clear()
                if batch:
                    rows_inserted += _flush_batch_text(conn, batch, dry_run)

        log.info(
            "region %d: %d rows processed, ~%d upserted",
            region,
            rows_in_csv,
            rows_inserted,
        )
        _close_run(
            conn,
            run_id,
            status="completed",
            rows_in_csv=rows_in_csv,
            rows_inserted=rows_inserted,
            rows_updated=0,
            started_at=started_at,
        )

    except Exception as exc:
        log.exception("region %d: failed: %s", region, exc)
        _close_run(
            conn,
            run_id,
            status="failed",
            rows_in_csv=0,
            rows_inserted=0,
            rows_updated=0,
            started_at=started_at,
            error_message=str(exc),
        )
        raise


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def run(
    *,
    regions: list[int],
    fixture: Path | None,
    dry_run: bool,
    batch_size: int,
) -> int:
    db_url = _database_url()
    with psycopg.connect(db_url, autocommit=False) as conn:
        for region in regions:
            _ingest_region(
                conn,
                region=region,
                fixture=fixture,
                dry_run=dry_run,
                batch_size=batch_size,
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="IRS EO BMF bulk-CSV ingest → entities.source_irs_bmf"
    )
    parser.add_argument(
        "--region",
        default="all",
        help="Region number (1-4) or 'all' (default)",
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        default=None,
        help="Path to local CSV fixture (skips HTTP download)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse + validate but do not write to DB",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
    )
    args = parser.parse_args(argv)

    if args.fixture:
        # Fixture mode: run as single region 1 (region is meaningless for fixture)
        regions = [1]
    elif args.region == "all":
        regions = list(BMF_REGIONS.keys())
    else:
        try:
            r = int(args.region)
        except ValueError:
            parser.error(f"--region must be 1-4 or 'all', got {args.region!r}")
        if r not in BMF_REGIONS:
            parser.error(f"--region must be 1-4, got {r}")
        regions = [r]

    return run(
        regions=regions,
        fixture=args.fixture,
        dry_run=args.dry_run,
        batch_size=args.batch_size,
    )


if __name__ == "__main__":
    sys.exit(main())

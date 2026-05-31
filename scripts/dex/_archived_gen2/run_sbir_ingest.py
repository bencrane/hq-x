#!/usr/bin/env python3
"""SBIR + STTR awards — bulk-CSV ingest from sbir.gov.

One physical table holds BOTH SBIR and STTR award datasets — sbir.gov
publishes both under the same award schema, distinguished by the `program`
column ('SBIR' / 'STTR').

Source URL (no-abstract bulk CSV, ~91 MB, 219,501 rows as of 2026-05-03):
  https://data.www.sbir.gov/mod_awarddatapublic_no_abstract/award_data_no_abstract.csv

Idempotency: ON CONFLICT (program, agency, branch, contract,
agency_tracking_number, phase, award_year) DO UPDATE SET all cols.

Audit: ops.sbir_ingest_runs.

Usage:
  PYTHONPATH=. doppler run -- python3 scripts/run_sbir_ingest.py
  PYTHONPATH=. doppler run -- python3 scripts/run_sbir_ingest.py --skip-if-unchanged
  PYTHONPATH=. doppler run -- python3 scripts/run_sbir_ingest.py --max-rows 100
  PYTHONPATH=. doppler run -- python3 scripts/run_sbir_ingest.py --dry-run
  PYTHONPATH=. doppler run -- python3 scripts/run_sbir_ingest.py --batch-size 2000
  PYTHONPATH=. doppler run -- python3 scripts/run_sbir_ingest.py \\
      --source-url https://data.www.sbir.gov/mod_awarddatapublic_no_abstract/award_data_no_abstract.csv
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
from typing import Any

import httpx
import psycopg
from psycopg.types.json import Jsonb


SOURCE_URL = "https://data.www.sbir.gov/mod_awarddatapublic_no_abstract/award_data_no_abstract.csv"
SOURCE_PROVIDER = "sbir.gov"
SOURCE_FILENAME = "award_data_no_abstract.csv"
DEFAULT_BATCH_SIZE = 5_000
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 5

# 41-column header → snake_cased table column. Order matches forward migration exactly.
CSV_HEADER_TO_COL: dict[str, str] = {
    "Company": "company",
    "Award Title": "award_title",
    "Agency": "agency",
    "Branch": "branch",
    "Phase": "phase",
    "Program": "program",
    "Agency Tracking Number": "agency_tracking_number",
    "Contract": "contract",
    "Proposal Award Date": "proposal_award_date",
    "Contract End Date": "contract_end_date",
    "Solicitation Number": "solicitation_number",
    "Solicitation Year": "solicitation_year",
    "Solicitation Close Date": "solicitation_close_date",
    "Proposal Receipt Date": "proposal_receipt_date",
    "Date of Notification": "date_of_notification",
    "Topic Code": "topic_code",
    "Award Year": "award_year",
    "Award Amount": "award_amount",
    "UEI": "uei",
    "Duns": "duns",
    "HUBZone Owned": "hubzone_owned",
    "Socially and Economically Disadvantaged": "socially_and_economically_disadvantaged",
    "Woman Owned": "woman_owned",
    "Number Employees": "number_employees",
    "Company Website": "company_website",
    "Address1": "address1",
    "Address2": "address2",
    "City": "city",
    "State": "state",
    "Zip": "zip",
    "Contact Name": "contact_name",
    "Contact Title": "contact_title",
    "Contact Phone": "contact_phone",
    "Contact Email": "contact_email",
    "PI Name": "pi_name",
    "PI Title": "pi_title",
    "PI Phone": "pi_phone",
    "PI Email": "pi_email",
    "RI Name": "ri_name",
    "RI POC Name": "ri_poc_name",
    "RI POC Phone": "ri_poc_phone",
}

COLS: list[str] = list(CSV_HEADER_TO_COL.values())  # 41-element list

PK_COLS: tuple[str, ...] = (
    "program", "agency", "branch", "contract",
    "agency_tracking_number", "phase", "award_year",
)


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("sbir-ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# DB helpers
# --------------------------------------------------------------------------- #


def _database_url() -> str:
    url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ.get("DEX_DB_URL_POOLED")
    if not url:
        raise RuntimeError("DEX_DB_URL_DIRECT (or DEX_DB_URL_POOLED) not set in environment.")
    return url


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #


def head_url(client: httpx.Client, url: str) -> tuple[int | None, datetime | None]:
    """HEAD with retry/backoff. Returns (content_length, last_modified)."""
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = client.head(url, follow_redirects=True, timeout=30.0)
            if r.status_code in RETRY_STATUSES:
                wait = min(2 ** attempt, 30)
                log.warning("HEAD %s HTTP %s; retry in %ss", url, r.status_code, wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            cl = int(r.headers.get("content-length", 0)) or None
            lm_raw = r.headers.get("last-modified")
            lm: datetime | None = None
            if lm_raw:
                try:
                    lm = datetime.strptime(lm_raw, "%a, %d %b %Y %H:%M:%S %Z").replace(
                        tzinfo=timezone.utc
                    )
                except ValueError:
                    lm = None
            return cl, lm
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            wait = min(2 ** attempt, 30)
            log.warning("HEAD %s error (%s); retry in %ss", url, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"HEAD failed after {MAX_RETRIES} retries: {last_exc}")


# --------------------------------------------------------------------------- #
# Audit-row helpers
# --------------------------------------------------------------------------- #


def insert_run_row(
    conn: psycopg.Connection,
    *,
    source_url: str,
    source_filename: str,
    source_last_modified: datetime | None,
    prior_source_last_modified: datetime | None,
) -> str:
    sql = """
    INSERT INTO ops.sbir_ingest_runs (
        dataset_form, status, source_url, source_filename,
        source_last_modified, prior_source_last_modified
    ) VALUES ('AWARDS', 'running', %s, %s, %s, %s)
    RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            source_url, source_filename,
            source_last_modified, prior_source_last_modified,
        ))
        row_id = cur.fetchone()[0]
    conn.commit()
    return str(row_id)


def get_prior_source_last_modified(conn: psycopg.Connection) -> datetime | None:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT source_last_modified
              FROM ops.sbir_ingest_runs
             WHERE status = 'completed'
             ORDER BY started_at DESC LIMIT 1
            """)
        row = cur.fetchone()
    return row[0] if row else None


def write_no_change_run(
    conn: psycopg.Connection,
    *,
    source_url: str,
    source_filename: str,
    source_last_modified: datetime | None,
    prior_source_last_modified: datetime | None,
) -> None:
    started = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ops.sbir_ingest_runs (
                dataset_form, status, source_url, source_filename,
                source_last_modified, prior_source_last_modified,
                started_at, finished_at, duration_seconds, notes
            ) VALUES ('AWARDS', 'no_change', %s, %s, %s, %s, %s, %s, 0, %s);
            """,
            (
                source_url, source_filename, source_last_modified,
                prior_source_last_modified, started, started,
                Jsonb({"reason": "source_last_modified unchanged"}),
            ),
        )
    conn.commit()


def finalize_run_row(
    conn: psycopg.Connection,
    run_id: str,
    *,
    status: str,
    csv_bytes_downloaded: int | None,
    rows_in_csv: int,
    rows_inserted: int,
    rows_updated: int,
    rows_unchanged: int,
    rows_skipped: int,
    started_wall: float,
    error_message: str | None,
    notes: dict[str, Any] | None,
) -> None:
    duration = round(time.monotonic() - started_wall, 3)
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE ops.sbir_ingest_runs
               SET status = %s,
                   csv_bytes_downloaded = %s,
                   rows_in_csv = %s,
                   rows_inserted = %s,
                   rows_updated = %s,
                   rows_unchanged = %s,
                   rows_skipped = %s,
                   finished_at = now(),
                   duration_seconds = %s,
                   error_message = %s,
                   notes = %s
             WHERE id = %s;
            """, (
            status, csv_bytes_downloaded, rows_in_csv,
            rows_inserted, rows_updated, rows_unchanged, rows_skipped,
            duration, error_message,
            Jsonb(notes) if notes else None, run_id,
        ))
    conn.commit()


# --------------------------------------------------------------------------- #
# Upsert helper
# --------------------------------------------------------------------------- #


def upsert_batch(
    conn: psycopg.Connection,
    batch: list[tuple[dict[str, str | None], dict]],
    run_id: str,
    *,
    source_filename: str,
    source_url: str,
    source_observed_at: datetime | None,
) -> tuple[int, int]:
    """Upsert a batch of (row_cols, raw_row) pairs. Returns (inserted, updated)."""
    if not batch:
        return 0, 0

    # Build the INSERT column list: 41 source cols + raw_source_row + provenance + run_id
    # ingested_at is omitted — it has DEFAULT now() and must not be passed NULL explicitly
    insert_cols = (
        COLS
        + ["raw_source_row"]
        + ["source_filename", "source_download_url", "source_provider", "source_observed_at"]
        + ["run_id"]
    )

    # ON CONFLICT DO UPDATE: all source cols + raw + provenance (not ingested_at — use now())
    update_assigns = (
        ", ".join(f"{c} = EXCLUDED.{c}" for c in COLS)
        + ", raw_source_row = EXCLUDED.raw_source_row"
        + ", source_filename = EXCLUDED.source_filename"
        + ", source_download_url = EXCLUDED.source_download_url"
        + ", source_observed_at = EXCLUDED.source_observed_at"
        + ", run_id = EXCLUDED.run_id"
        + ", ingested_at = now()"
    )

    sql = f"""
    WITH upserted AS (
      INSERT INTO entities.source_sbir_awards ({', '.join(insert_cols)})
      VALUES %s
      ON CONFLICT (program, agency, branch, contract,
                   agency_tracking_number, phase, award_year)
      DO UPDATE SET
        {update_assigns}
      RETURNING (xmax = 0) AS inserted
    )
    SELECT
      count(*) FILTER (WHERE inserted)     AS rows_inserted,
      count(*) FILTER (WHERE NOT inserted) AS rows_updated
    FROM upserted;
    """

    # Deduplicate batch by composite key (last occurrence wins) to avoid
    # "ON CONFLICT DO UPDATE command cannot affect row a second time" when the
    # source CSV has duplicate rows within the same batch window.
    seen_keys: dict[tuple, tuple] = {}
    for row_cols, raw_row in batch:
        pk = tuple(row_cols.get(c) for c in PK_COLS)
        seen_keys[pk] = (row_cols, raw_row)
    deduped_batch = list(seen_keys.values())

    # Build parameter tuples
    params_list = []
    for row_cols, raw_row in deduped_batch:
        row_vals = [row_cols.get(c) for c in COLS]
        row_vals.append(Jsonb(raw_row))                    # raw_source_row
        row_vals.append(source_filename)                   # source_filename
        row_vals.append(source_url)                        # source_download_url
        row_vals.append(SOURCE_PROVIDER)                   # source_provider
        row_vals.append(source_observed_at)                # source_observed_at
        row_vals.append(run_id)                            # run_id
        # ingested_at omitted — DEFAULT now() fires automatically; passing NULL would violate NOT NULL
        params_list.append(tuple(row_vals))

    # Use psycopg executemany via a single VALUES (...) expansion
    # Build placeholders
    placeholders = ", ".join(
        f"({', '.join(['%s'] * len(params_list[0]))})"
        for _ in params_list
    )
    flat_params = [v for row in params_list for v in row]

    upsert_sql = sql.replace("%s", placeholders, 1)

    with conn.cursor() as cur:
        cur.execute(upsert_sql, flat_params)
        ins, upd = cur.fetchone()
    conn.commit()
    return int(ins), int(upd)


# --------------------------------------------------------------------------- #
# CSV → Postgres streaming pipeline
# --------------------------------------------------------------------------- #


def stream_csv_to_db(
    conn: psycopg.Connection,
    csv_fh: io.TextIOWrapper,
    run_id: str,
    *,
    source_filename: str,
    source_url: str,
    source_observed_at: datetime | None,
    batch_size: int,
    max_rows: int | None,
) -> tuple[int, int, int, int]:
    """Stream CSV rows into DB. Returns (inserted, updated, skipped, rows_seen)."""
    reader = csv.DictReader(csv_fh)
    header = reader.fieldnames or []

    missing = [h for h in CSV_HEADER_TO_COL if h not in header]
    extra = [h for h in header if h not in CSV_HEADER_TO_COL]
    if missing:
        log.warning("CSV header missing %d expected col(s): %s", len(missing), missing)
    if extra:
        log.warning("CSV header has %d unexpected col(s) (ignored): %s", len(extra), extra[:10])

    total_inserted = total_updated = total_skipped = rows_seen = 0
    batch: list[tuple[dict[str, str | None], dict]] = []
    page_started = time.monotonic()

    for raw in reader:
        rows_seen += 1
        if max_rows is not None and rows_seen > max_rows:
            log.info("--max-rows %d reached, stopping read", max_rows)
            break

        # Map CSV header → snake_cased column values
        row_cols: dict[str, str | None] = {
            col: (raw.get(hdr) or "").strip() or None
            for hdr, col in CSV_HEADER_TO_COL.items()
        }

        # Validate composite key — skip rows with any empty PK column
        if not all(row_cols.get(c) for c in PK_COLS):
            total_skipped += 1
            continue

        # raw_row preserves all fields from the CSV as-is
        raw_row = dict(raw)

        batch.append((row_cols, raw_row))

        if len(batch) >= batch_size:
            ins, upd = upsert_batch(
                conn, batch, run_id,
                source_filename=source_filename,
                source_url=source_url,
                source_observed_at=source_observed_at,
            )
            total_inserted += ins
            total_updated += upd
            log.info(
                "chunk: rows_seen=%d ins=%d upd=%d (cum ins=%d upd=%d skip=%d) elapsed=%.1fs",
                rows_seen, ins, upd,
                total_inserted, total_updated, total_skipped,
                time.monotonic() - page_started,
            )
            batch.clear()
            page_started = time.monotonic()

    if batch:
        ins, upd = upsert_batch(
            conn, batch, run_id,
            source_filename=source_filename,
            source_url=source_url,
            source_observed_at=source_observed_at,
        )
        total_inserted += ins
        total_updated += upd
        log.info(
            "final chunk: rows_seen=%d ins=%d upd=%d (cum ins=%d upd=%d skip=%d) elapsed=%.1fs",
            rows_seen, ins, upd,
            total_inserted, total_updated, total_skipped,
            time.monotonic() - page_started,
        )

    return total_inserted, total_updated, total_skipped, rows_seen


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--skip-if-unchanged", action="store_true",
        help="No-op if source Last-Modified has not advanced since the prior successful run.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="HEAD + read CSV header only; no DB writes.",
    )
    p.add_argument(
        "--max-rows", type=int, default=None,
        help="Cap rows read from CSV (smoke testing only).",
    )
    p.add_argument(
        "--source-url", default=None,
        help=f"Override the CSV source URL (default: {SOURCE_URL}).",
    )
    p.add_argument(
        "--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
        help=f"Rows per upsert batch (default: {DEFAULT_BATCH_SIZE}).",
    )
    return p.parse_args()


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def main() -> int:
    args = parse_args()
    url = args.source_url or SOURCE_URL
    started_wall = time.monotonic()

    log.info("start url=%s", url)

    with httpx.Client(headers={"User-Agent": "data-engine-x/sbir-ingest"}) as client:
        try:
            content_length, source_last_modified = head_url(client, url)
        except Exception:
            log.exception("HEAD failed")
            return 1

        log.info("HEAD content_length=%s last_modified=%s", content_length, source_last_modified)

        if args.dry_run:
            log.info("DRY RUN — reading CSV header only; no DB writes")
            with client.stream("GET", url, follow_redirects=True, timeout=600.0) as r:
                r.raise_for_status()
                text_io = io.TextIOWrapper(
                    r.raw, encoding="utf-8", errors="replace", newline=""
                )
                reader = csv.DictReader(text_io)
                header = reader.fieldnames or []
                log.info("CSV header (%d cols): %s", len(header), header)
            return 0

        with psycopg.connect(_database_url()) as conn:
            prior = get_prior_source_last_modified(conn)
            log.info("prior source_last_modified: %s", prior)

            if (
                args.skip_if_unchanged
                and prior is not None
                and source_last_modified is not None
                and source_last_modified <= prior
            ):
                log.info("source_last_modified unchanged — recording no_change run")
                write_no_change_run(
                    conn,
                    source_url=url,
                    source_filename=SOURCE_FILENAME,
                    source_last_modified=source_last_modified,
                    prior_source_last_modified=prior,
                )
                return 0

            run_id = insert_run_row(
                conn,
                source_url=url,
                source_filename=SOURCE_FILENAME,
                source_last_modified=source_last_modified,
                prior_source_last_modified=prior,
            )
            log.info("run id: %s", run_id)

            try:
                with client.stream("GET", url, follow_redirects=True, timeout=600.0) as r:
                    r.raise_for_status()
                    raw_bytes = b"".join(r.iter_bytes())
                    text_io = io.TextIOWrapper(
                        io.BytesIO(raw_bytes), encoding="utf-8", errors="replace", newline=""
                    )
                    ins, upd, skipped, rows_seen = stream_csv_to_db(
                        conn, text_io, run_id,
                        source_filename=SOURCE_FILENAME,
                        source_url=url,
                        source_observed_at=source_last_modified,
                        batch_size=args.batch_size,
                        max_rows=args.max_rows,
                    )

                finalize_run_row(
                    conn, run_id,
                    status="completed",
                    csv_bytes_downloaded=content_length,
                    rows_in_csv=rows_seen,
                    rows_inserted=ins,
                    rows_updated=upd,
                    rows_unchanged=max(0, rows_seen - ins - upd - skipped),
                    rows_skipped=skipped,
                    started_wall=started_wall,
                    error_message=None,
                    notes=None,
                )
                log.info(
                    "DONE rows_in_csv=%d ins=%d upd=%d unch=%d skip=%d wall=%.1fs",
                    rows_seen, ins, upd,
                    max(0, rows_seen - ins - upd - skipped),
                    skipped,
                    time.monotonic() - started_wall,
                )
                return 0

            except Exception as exc:
                log.exception("ingest failed")
                finalize_run_row(
                    conn, run_id,
                    status="failed",
                    csv_bytes_downloaded=None,
                    rows_in_csv=0,
                    rows_inserted=0,
                    rows_updated=0,
                    rows_unchanged=0,
                    rows_skipped=0,
                    started_wall=started_wall,
                    error_message=str(exc),
                    notes=None,
                )
                return 1


if __name__ == "__main__":
    sys.exit(main())

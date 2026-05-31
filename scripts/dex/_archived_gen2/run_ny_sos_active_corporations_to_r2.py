"""NY State Active Corporations (DoS Beginning 1800) → R2 ZSTD Parquet ingest.

Cadence — **Operator-Only Bulk Run (Quarterly Batch).** State SoS pipelines
(CA / FL / NY / CO) are retired from automated schedules per the 2026-05-25
operational policy shift. The companion Modal app
``modal/ny_sos_active_corporations_app.py`` previously fired this script
daily; that schedule is now removed and the deployment is permanently
stopped. Trigger manually, point-in-time. See
``apps/data-engine-x/modal/INDEX.md`` §"State SoS pipelines".

Downloads NY DoS Active Corporations CSV from Socrata (n9v6-gdp6, data.ny.gov),
transforms via DuckDB (all_varchar=TRUE — 4.2M rows; pandas would OOM Modal's
8 GB worker), writes ZSTD Parquet snapshot to Cloudflare R2, and logs each run
to ops.ny_sos_active_corporations_ingest_runs.

Source: https://data.ny.gov/api/views/n9v6-gdp6

R2 layout:
  s3://dex-raw-landing-zone/sos-ny/active-corporations/snapshot={YYYY-MM-DD}/data.parquet

Idempotency:
  Checks the X-SODA2-Truth-Last-Modified Socrata response header (NOT ETag —
  ETag changes per request even when data is unchanged). If the header value
  matches the most-recent completed run's error_message field (used as a
  metadata slot for this value when skipping), writes a completed row with
  rows_ingested=0 and error_message='skipped-no-change' and returns early.

Usage:
    cd ~/hq-all/apps/data-engine-x
    doppler run --project hq-all --config prd -- \\
        uv run python scripts/run_ny_sos_active_corporations_to_r2.py \\
        [--snapshot-date YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import datetime
import logging
import os
import sys
import tempfile
import uuid
from pathlib import Path

import boto3
import psycopg2
import requests

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ── load-bearing constants (verify harness greps for these) ─────────────────

SOURCE_CSV_URL = (
    "https://data.ny.gov/api/views/n9v6-gdp6/rows.csv?accessType=DOWNLOAD"
)

R2_BUCKET = "dex-raw-landing-zone"
R2_PREFIX = "sos-ny/active-corporations"

LEDGER_TABLE = "ops.ny_sos_active_corporations_ingest_runs"


# ── helpers ──────────────────────────────────────────────────────────────────


def _pg_conn():
    return psycopg2.connect(os.environ["DEX_DB_URL_DIRECT"])


def _last_completed_truth_modified(conn) -> str | None:
    """Return the X-SODA2-Truth-Last-Modified value from the most recent
    completed run, stored as a JSON-encoded string in the source_run_metadata
    slot.  We encode it in error_message prefixed with 'truth-last-modified:'
    when we write a skipped-no-change row, so this field acts as a metadata
    slot rather than a true error.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT error_message
            FROM {LEDGER_TABLE}
            WHERE status = 'completed'
              AND error_message LIKE 'truth-last-modified:%'
            ORDER BY started_at DESC NULLS LAST
            LIMIT 1
            """
        )
        row = cur.fetchone()
    if row and row[0]:
        # strip the prefix to recover the raw header value
        val = row[0]
        if val.startswith("truth-last-modified:"):
            return val[len("truth-last-modified:"):]
    return None


def _record_run_start(conn, snapshot_date: datetime.date) -> str:
    run_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {LEDGER_TABLE}
                (ingest_run_id, snapshot_date, started_at, status)
            VALUES (%s, %s, now(), 'running')
            """,
            (run_id, snapshot_date),
        )
    conn.commit()
    logger.info("started run %s snapshot=%s", run_id, snapshot_date)
    return run_id


def _record_run_complete(
    conn,
    run_id: str,
    rows_ingested: int,
    truth_last_modified: str | None = None,
) -> None:
    # Store the truth-last-modified header value in error_message when skipping;
    # for real completions also store it so the next run can compare.
    note = (
        f"truth-last-modified:{truth_last_modified}"
        if truth_last_modified
        else None
    )
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {LEDGER_TABLE}
               SET status = 'completed',
                   completed_at = now(),
                   rows_ingested = %s,
                   error_message = %s
             WHERE ingest_run_id = %s
            """,
            (rows_ingested, note, run_id),
        )
    conn.commit()
    logger.info("completed run %s rows=%d", run_id, rows_ingested)


def _record_run_skipped(
    conn, run_id: str, truth_last_modified: str | None
) -> None:
    note = (
        f"truth-last-modified:{truth_last_modified}"
        if truth_last_modified
        else "skipped-no-change"
    )
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {LEDGER_TABLE}
               SET status = 'completed',
                   completed_at = now(),
                   rows_ingested = 0,
                   error_message = %s
             WHERE ingest_run_id = %s
            """,
            (note, run_id),
        )
    conn.commit()
    logger.info("skipped run %s (no change: %s)", run_id, note)


def _record_run_failed(conn, run_id: str, error_message: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {LEDGER_TABLE}
               SET status = 'failed',
                   completed_at = now(),
                   error_message = %s
             WHERE ingest_run_id = %s
            """,
            (error_message[:2000], run_id),
        )
    conn.commit()
    logger.error("failed run %s: %s", run_id, error_message[:200])


def _r2_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="us-east-1",
    )


def _head_source_truth_modified() -> str | None:
    """HEAD the source URL and return X-SODA2-Truth-Last-Modified header."""
    try:
        resp = requests.head(SOURCE_CSV_URL, timeout=30, allow_redirects=True)
        return resp.headers.get("X-SODA2-Truth-Last-Modified")
    except Exception as exc:
        logger.warning("HEAD request for idempotency check failed: %s", exc)
        return None


def ingest(snapshot_date: datetime.date) -> None:
    """Fetch NY DoS Active Corps CSV, write ZSTD Parquet to R2, ledger row.

    Uses DuckDB read_csv (all_varchar=TRUE, null_padding=TRUE, strict_mode=FALSE)
    per L9 + L56 for 4.2M-row memory headroom.
    """
    conn = _pg_conn()

    # ── idempotency: X-SODA2-Truth-Last-Modified check ──────────────────────
    current_tlm = _head_source_truth_modified()
    logger.info("X-SODA2-Truth-Last-Modified: %s", current_tlm)

    if current_tlm:
        last_tlm = _last_completed_truth_modified(conn)
        logger.info("last completed truth-last-modified: %s", last_tlm)
        if last_tlm and last_tlm == current_tlm:
            run_id = _record_run_start(conn, snapshot_date)
            _record_run_skipped(conn, run_id, current_tlm)
            conn.close()
            logger.info(
                "skipped: X-SODA2-Truth-Last-Modified unchanged (%s)", current_tlm
            )
            return

    run_id = _record_run_start(conn, snapshot_date)

    tmp_csv_path = None
    local_parquet = None
    try:
        logger.info("downloading NY DoS Active Corps CSV from %s", SOURCE_CSV_URL)
        resp = requests.get(SOURCE_CSV_URL, timeout=600, stream=True)
        resp.raise_for_status()

        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp_csv:
            tmp_csv_path = tmp_csv.name
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                tmp_csv.write(chunk)
        logger.info(
            "downloaded CSV → %s (%d bytes)",
            tmp_csv_path,
            Path(tmp_csv_path).stat().st_size,
        )

        local_parquet = tmp_csv_path.replace(".csv", ".parquet")

        # ── DuckDB transform: all_varchar=TRUE per L9 + L56 ─────────────────
        import duckdb

        con = duckdb.connect()
        # Force-enable spilling to disk so the 4.2M-row transform doesn't OOM.
        con.execute("SET memory_limit='6GB'")
        con.execute("SET temp_directory='/tmp'")
        # Bulk-CSV endpoint preserves publisher display names ("DOS ID",
        # "Current Entity Name", ...) rather than the snake_case field names
        # the /resource API returns. Rename Title Case → snake_case at write
        # time so the Parquet (and downstream Lance dataset) uses canonical
        # snake_case identifiers. Mirrors all 30 published columns 1:1.
        con.execute(
            f"""
            COPY (
                SELECT
                    "DOS ID"                     AS dos_id,
                    "Current Entity Name"        AS current_entity_name,
                    "Initial DOS Filing Date"    AS initial_dos_filing_date,
                    "County"                     AS county,
                    "Jurisdiction"               AS jurisdiction,
                    "Entity Type"                AS entity_type,
                    "DOS Process Name"           AS dos_process_name,
                    "DOS Process Address 1"      AS dos_process_address_1,
                    "DOS Process Address 2"      AS dos_process_address_2,
                    "DOS Process City"           AS dos_process_city,
                    "DOS Process State"          AS dos_process_state,
                    "DOS Process Zip"            AS dos_process_zip,
                    "CEO Name"                   AS ceo_name,
                    "CEO Address 1"              AS ceo_address_1,
                    "CEO Address 2"              AS ceo_address_2,
                    "CEO City"                   AS ceo_city,
                    "CEO State"                  AS ceo_state,
                    "CEO Zip"                    AS ceo_zip,
                    "Registered Agent Name"      AS registered_agent_name,
                    "Registered Agent Address 1" AS registered_agent_address_1,
                    "Registered Agent Address 2" AS registered_agent_address_2,
                    "Registered Agent City"      AS registered_agent_city,
                    "Registered Agent State"     AS registered_agent_state,
                    "Registered Agent Zip"       AS registered_agent_zip,
                    "Location Name"              AS location_name,
                    "Location Address 1"         AS location_address_1,
                    "Location Address 2"         AS location_address_2,
                    "Location City"              AS location_city,
                    "Location State"             AS location_state,
                    "Location Zip"               AS location_zip
                FROM read_csv(
                    '{tmp_csv_path}',
                    all_varchar=TRUE,
                    null_padding=TRUE,
                    strict_mode=FALSE,
                    header=TRUE
                )
            ) TO '{local_parquet}'
            (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)
            """
        )
        row_count = con.execute(
            f"SELECT count(*) FROM '{local_parquet}'"
        ).fetchone()[0]
        con.close()
        logger.info("DuckDB transform complete: %d rows → %s", row_count, local_parquet)

        r2_key = f"{R2_PREFIX}/snapshot={snapshot_date}/data.parquet"
        s3 = _r2_client()
        s3.upload_file(
            local_parquet,
            R2_BUCKET,
            r2_key,
            ExtraArgs={"ContentType": "application/x-parquet"},
        )
        logger.info("uploaded → s3://%s/%s", R2_BUCKET, r2_key)

        _record_run_complete(conn, run_id, row_count, current_tlm)
    except Exception as exc:
        _record_run_failed(conn, run_id, str(exc))
        raise
    finally:
        conn.close()
        for p in (tmp_csv_path, local_parquet):
            if p and Path(p).exists():
                Path(p).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="NY DoS Active Corps → R2 ZSTD Parquet ingest"
    )
    parser.add_argument(
        "--snapshot-date",
        default=None,
        help="Snapshot date YYYY-MM-DD (default: today UTC)",
    )
    args = parser.parse_args()

    if args.snapshot_date:
        snapshot_date = datetime.date.fromisoformat(args.snapshot_date)
    else:
        snapshot_date = datetime.datetime.now(datetime.timezone.utc).date()

    logger.info("snapshot_date=%s", snapshot_date)
    ingest(snapshot_date)
    return 0


if __name__ == "__main__":
    sys.exit(main())

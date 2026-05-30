"""OPSC School Facility Program Funding → R2 ZSTD Parquet ingest.

Downloads OPSC School Facility Program Funding CSV (published by CA Department of
General Services / Office of Public School Construction) from data.ca.gov CKAN,
writes ZSTD Parquet snapshot to Cloudflare R2, and logs each run to
ops.opsc_school_facility_funding_ingest_runs.

CKAN resource id: 8080bb19-a63b-47e3-82d3-7451d119e27f
CKAN package id:  dd1eabf1-0b66-49d6-857d-8cef6ed93d45

R2 layout (kebab-case per CLAUDE.md):
  s3://dex-raw-landing-zone/opsc/school-facility-funding/snapshot={YYYY-MM-DD}/data.parquet

CSV shape (per 2026-05-19 probe):
  UTF-8 with BOM, 30 snake_case columns, 14,202 rows.
  Natural PK: Application_Number (district/sequence format, e.g. 50/10033-00-001) — unique.
  utf-8-sig read strips the BOM at the column-name level.

All-VARCHAR write per L9 (pandas dtype=str). Idempotent on snapshot partition.

Usage:
    cd ~/hq-all/apps/data-engine-x
    doppler run --project hq-all --config prd -- \\
        uv run python scripts/run_opsc_school_facility_funding_to_r2.py \\
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
import pandas as pd
import psycopg2
import pyarrow as pa
import pyarrow.parquet as pq
import requests

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ── load-bearing constants (verify harness greps for these) ─────────────────

CKAN_API_BASE = "https://data.ca.gov/api/3/action"
CKAN_RESOURCE_ID = "8080bb19-a63b-47e3-82d3-7451d119e27f"

R2_BUCKET = "dex-raw-landing-zone"
R2_PREFIX = "opsc/school-facility-funding"


# ── helpers ──────────────────────────────────────────────────────────────────

def _resolve_ckan_url(resource_id: str) -> str:
    """GET resource_show and return the direct download URL."""
    resp = requests.get(
        f"{CKAN_API_BASE}/resource_show",
        params={"id": resource_id},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"CKAN resource_show failed for {resource_id}: {data}")
    url = data["result"]["url"]
    logger.info("resolved CKAN resource %s → %s", resource_id, url)
    return url


def _pg_conn():
    return psycopg2.connect(os.environ["DEX_DB_URL_DIRECT"])


def _record_run_start(conn, snapshot_date: datetime.date) -> str:
    run_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.opsc_school_facility_funding_ingest_runs
                (ingest_run_id, snapshot_date, started_at, status)
            VALUES (%s, %s, now(), 'running')
            """,
            (run_id, snapshot_date),
        )
    conn.commit()
    logger.info("started run %s snapshot=%s", run_id, snapshot_date)
    return run_id


def _record_run_complete(conn, run_id: str, rows_ingested: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ops.opsc_school_facility_funding_ingest_runs
               SET status = 'completed', completed_at = now(), rows_ingested = %s
             WHERE ingest_run_id = %s
            """,
            (rows_ingested, run_id),
        )
    conn.commit()
    logger.info("completed run %s rows=%d", run_id, rows_ingested)


def _record_run_failed(conn, run_id: str, error_message: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ops.opsc_school_facility_funding_ingest_runs
               SET status = 'failed', completed_at = now(), error_message = %s
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


def ingest(snapshot_date: datetime.date) -> None:
    """Fetch OPSC SFP CSV, write ZSTD Parquet to R2, ledger row."""
    conn = _pg_conn()
    run_id = _record_run_start(conn, snapshot_date)
    tmp_csv_path = None
    local_parquet = None
    try:
        url = _resolve_ckan_url(CKAN_RESOURCE_ID)
        logger.info("downloading OPSC SFP CSV from %s", url)
        resp = requests.get(url, timeout=300, stream=True)
        resp.raise_for_status()

        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp_csv:
            tmp_csv_path = tmp_csv.name
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                tmp_csv.write(chunk)
        logger.info("downloaded OPSC SFP CSV → %s (%d bytes)", tmp_csv_path, Path(tmp_csv_path).stat().st_size)

        # All-VARCHAR read per L9. utf-8-sig strips the BOM from the first column name.
        df = pd.read_csv(tmp_csv_path, dtype=str, encoding="utf-8-sig", low_memory=False)
        logger.info("parsed OPSC SFP CSV: %d rows × %d cols", len(df), len(df.columns))

        # Lowercase column names for downstream Lance / SQL ergonomics.
        df.columns = [c.strip().lower() for c in df.columns]

        # Normalize NaN → None for proper NULL on the Parquet side.
        df = df.where(pd.notnull(df), other=None)

        local_parquet = tmp_csv_path.replace(".csv", ".parquet")
        table = pa.Table.from_pandas(df, preserve_index=False)
        pq.write_table(
            table, local_parquet,
            compression="ZSTD",
            compression_level=9,
        )

        r2_key = f"{R2_PREFIX}/snapshot={snapshot_date}/data.parquet"
        s3 = _r2_client()
        s3.upload_file(
            local_parquet, R2_BUCKET, r2_key,
            ExtraArgs={"ContentType": "application/x-parquet"},
        )
        logger.info("uploaded OPSC SFP → s3://%s/%s", R2_BUCKET, r2_key)

        _record_run_complete(conn, run_id, len(df))
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
        description="OPSC School Facility Program Funding → R2 ZSTD Parquet ingest"
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

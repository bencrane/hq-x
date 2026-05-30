"""WARN Act layoff notices (Big Local News) → R2 ZSTD Parquet ingest.

Downloads the Big Local News warn-transformer consolidated `integrated.csv`
(40 states + DC, ~85K rows, refreshed daily ~23:50 UTC) and writes a ZSTD
Parquet snapshot to Cloudflare R2, logging each run to
ops.warn_notices_r2_ingest_runs.

Source (Apache-2.0):
  https://raw.githubusercontent.com/biglocalnews/warn-github-flow/transformer/data/warn-transformer/processed/integrated.csv

Big Local News (Stanford) maintains the per-state scrapers + the daily
consolidation cron (warn-scraper / warn-transformer / warn-github-flow). We
ingest the finished consolidated artifact rather than re-running 40 fragile
per-state scrapers.

R2 layout:
  s3://dex-raw-landing-zone/warn/notices/snapshot={YYYY-MM-DD}/data.parquet

15 columns, all written as VARCHAR (pandas dtype=str) per L9. The integrated.csv
header is already snake_case — no rename needed. Natural PK: hash_id.

Idempotent: re-running with the same --snapshot-date overwrites the same R2 key.

Usage:
    cd ~/hq-all/apps/data-engine-x
    doppler run --project hq-all --config prd -- \\
        uv run python scripts/run_warn_notices_to_r2.py [--snapshot-date YYYY-MM-DD]
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

URL = (
    "https://raw.githubusercontent.com/biglocalnews/warn-github-flow/"
    "transformer/data/warn-transformer/processed/integrated.csv"
)

R2_BUCKET = "dex-raw-landing-zone"
R2_PREFIX = "warn/notices"

# integrated.csv is a cumulative integrated dataset (~85K rows, monotonically
# growing). A fetch returning far fewer rows means the upstream URL moved or
# returned an error page — fail loudly rather than write a garbage snapshot.
MIN_ROWS = 50_000

_USER_AGENT = "data-engine-x-warn-notices/1.0 (+https://substrate.build)"


def _pg_conn():
    return psycopg2.connect(os.environ["DEX_DB_URL_DIRECT"])


def _record_run_start(conn, snapshot_date: datetime.date) -> str:
    run_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.warn_notices_r2_ingest_runs
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
            UPDATE ops.warn_notices_r2_ingest_runs
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
            UPDATE ops.warn_notices_r2_ingest_runs
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
    """Download BLN integrated.csv, write a ZSTD Parquet snapshot to R2."""
    conn = _pg_conn()
    run_id = _record_run_start(conn, snapshot_date)
    local_csv = None
    local_parquet = None
    try:
        logger.info("downloading WARN integrated.csv from %s", URL)
        resp = requests.get(
            URL, headers={"User-Agent": _USER_AGENT}, timeout=120, stream=True
        )
        resp.raise_for_status()

        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp_csv:
            local_csv = tmp_csv.name
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                tmp_csv.write(chunk)
        logger.info("downloaded → %s", local_csv)

        # All-VARCHAR per L9. integrated.csv header is already snake_case.
        df = pd.read_csv(local_csv, dtype=str)
        df = df.where(pd.notnull(df), None)
        logger.info("parsed: %d rows × %d cols", len(df), len(df.columns))

        if len(df) < MIN_ROWS:
            raise RuntimeError(
                f"row count {len(df)} below floor {MIN_ROWS} — "
                "upstream URL likely moved or returned an error page"
            )

        local_parquet = local_csv.replace(".csv", ".parquet")
        table = pa.Table.from_pandas(df, preserve_index=False)
        pq.write_table(
            table, local_parquet, compression="ZSTD", compression_level=9
        )

        r2_key = f"{R2_PREFIX}/snapshot={snapshot_date}/data.parquet"
        s3 = _r2_client()
        s3.upload_file(
            local_parquet, R2_BUCKET, r2_key,
            ExtraArgs={"ContentType": "application/x-parquet"},
        )
        logger.info("uploaded → s3://%s/%s", R2_BUCKET, r2_key)

        _record_run_complete(conn, run_id, len(df))
    except Exception as exc:
        _record_run_failed(conn, run_id, str(exc))
        raise
    finally:
        conn.close()
        for p in (local_csv, local_parquet):
            if p and Path(p).exists():
                Path(p).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="WARN Act notices (Big Local News) → R2 ZSTD Parquet ingest"
    )
    parser.add_argument(
        "--snapshot-date", default=None,
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

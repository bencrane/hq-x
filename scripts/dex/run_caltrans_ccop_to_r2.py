"""Caltrans CCOP active bid solicitations → R2 ZSTD Parquet ingest.

Downloads the active projects table from the CA Caltrans Contract Opportunities
Portal (CCOP) at https://ccop.dot.ca.gov/allProjects, parses the server-rendered
HTML table, writes a ZSTD Parquet snapshot to Cloudflare R2, and logs each run
to ops.caltrans_ccop_ingest_runs.

R2 layout (kebab-case per CLAUDE.md §"R2 prefix convention"):
  s3://dex-raw-landing-zone/caltrans/ccop-active/snapshot={YYYY-MM-DD}/data.parquet

All columns written as VARCHAR (pandas dtype=str) per CLAUDE.md §"Source ingest invariant"
sub-section "bulk-historical Volume-King sources → R2 ZSTD Parquet → Lance emit" (L9).

Idempotent: re-running with the same --snapshot-date overwrites the same R2 key.

Source: server-rendered HTML, no auth, no WAF. ~96-97 rows per probe (2026-05-19).
Columns: Project ID, Project Title, County, License, Advertise Date, Bid Date, Status.

Usage:
    cd ~/hq-all/apps/data-engine-x
    doppler run --project hq-all --config prd -- \\
        uv run python scripts/run_caltrans_ccop_to_r2.py \\
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
from io import StringIO
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

URL = "https://ccop.dot.ca.gov/allProjects"

R2_BUCKET = "dex-raw-landing-zone"
R2_PREFIX = "caltrans/ccop-active"

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Target column names (snake_case) after rename
_COLUMNS = [
    "project_id",
    "project_title",
    "county",
    "license_class",
    "advertise_date",
    "bid_date",
    "status",
]


# ── helpers ──────────────────────────────────────────────────────────────────

def _pg_conn():
    return psycopg2.connect(os.environ["DEX_DB_URL_DIRECT"])


def _record_run_start(conn, snapshot_date: datetime.date) -> str:
    run_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.caltrans_ccop_ingest_runs
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
            UPDATE ops.caltrans_ccop_ingest_runs
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
            UPDATE ops.caltrans_ccop_ingest_runs
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
    """Fetch CCOP active projects, parse HTML table, upload ZSTD Parquet to R2."""
    conn = _pg_conn()
    run_id = _record_run_start(conn, snapshot_date)
    local_parquet = None
    try:
        logger.info("fetching CCOP active projects from %s", URL)
        resp = requests.get(
            URL,
            headers={"User-Agent": _USER_AGENT},
            timeout=30,
        )
        resp.raise_for_status()
        logger.info("fetched %d bytes", len(resp.content))

        # Primary parser: pandas.read_html (fast, handles colspan/rowspan gracefully).
        # Falls back to BeautifulSoup only if read_html returns unexpected shape.
        try:
            tables = pd.read_html(StringIO(resp.text))
            if not tables:
                raise ValueError("pd.read_html returned no tables")
            df = tables[0]
            if df.shape[1] != 7:
                raise ValueError(
                    f"pd.read_html returned {df.shape[1]} columns, expected 7; "
                    "falling back to BeautifulSoup"
                )
            logger.info("pd.read_html OK: %d rows × %d cols", len(df), df.shape[1])
        except Exception as parse_exc:
            logger.warning("pd.read_html fallback: %s", parse_exc)
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(resp.text, "lxml")
            rows = []
            for tr in soup.select("tbody tr"):
                cells = [td.get_text(strip=True) for td in tr.select("td")]
                if cells:
                    rows.append(cells)
            if not rows:
                raise RuntimeError("BeautifulSoup found no <tbody> rows") from parse_exc
            df = pd.DataFrame(rows)
            logger.info("BeautifulSoup fallback OK: %d rows × %d cols", len(df), df.shape[1])

        # Rename columns to snake_case regardless of source header text
        if len(df.columns) != 7:
            raise RuntimeError(
                f"Unexpected column count {len(df.columns)} (expected 7); "
                f"columns found: {list(df.columns)}"
            )
        df.columns = _COLUMNS

        # All-VARCHAR per CLAUDE.md §"Source ingest invariant" (L9)
        df = df.astype(str).where(df.notna(), None)
        logger.info("schema after rename: %s", list(df.columns))
        logger.info("row count: %d", len(df))

        # Write ZSTD Parquet to /tmp
        with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
            local_parquet = tmp.name

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
        logger.info("uploaded → s3://%s/%s", R2_BUCKET, r2_key)

        _record_run_complete(conn, run_id, len(df))
    except Exception as exc:
        _record_run_failed(conn, run_id, str(exc))
        raise
    finally:
        conn.close()
        if local_parquet and Path(local_parquet).exists():
            Path(local_parquet).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Caltrans CCOP active bid solicitations → R2 ZSTD Parquet ingest"
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

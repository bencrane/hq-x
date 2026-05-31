"""CA Cal eProcure archived opportunities → R2 ZSTD Parquet ingest.

Downloads DGS Purchase Order Data (PO, one-shot FY 2012-2015) and
DGS-Approved Non-Competitive Bids (NCB, monthly ≥$1M) from data.ca.gov CKAN,
writes ZSTD Parquet snapshots to Cloudflare R2, and logs each run to
ops.cal_eprocure_ingest_runs.

CKAN resource ids (corrected by validator from package-ids):
  PO:  bb82edc5-9c78-44e2-8947-68ece26197c5  (344,504 rows, FY 2012-2015, static since 2019)
  NCB: 14932789-485b-481b-910a-dafb40d3471c  (469 rows, monthly, Special Category ≥$1M)

R2 layout (kebab-case per CLAUDE.md §"R2 prefix convention"):
  s3://dex-raw-landing-zone/cal-eprocure-archived/po/snapshot={YYYY-MM-DD}/data.parquet
  s3://dex-raw-landing-zone/cal-eprocure-archived/ncb/snapshot={YYYY-MM-DD}/data.parquet

All columns written as VARCHAR (pandas dtype=str) per CLAUDE.md §"Source ingest invariant"
sub-section "bulk-historical Volume-King sources → R2 ZSTD Parquet → Lance emit" (L9).

Idempotent: re-running with the same --snapshot-date overwrites the same R2 key.

NCB format note: source is XLSX; we read sheet_name=0 (first sheet — confirmed as the
main data sheet; any additional sheets are metadata/instructions and are intentionally
skipped per validator research).

Usage:
    cd ~/hq-all/apps/data-engine-x
    doppler run --project hq-all --config prd -- \\
        uv run python scripts/run_cal_eprocure_archived_to_r2.py \\
        [--variant {po,ncb,both}] [--snapshot-date YYYY-MM-DD]
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
CKAN_PO_RESOURCE_ID  = "bb82edc5-9c78-44e2-8947-68ece26197c5"
CKAN_NCB_RESOURCE_ID = "14932789-485b-481b-910a-dafb40d3471c"

R2_BUCKET     = "dex-raw-landing-zone"
R2_PREFIX_PO  = "cal-eprocure-archived/po"
R2_PREFIX_NCB = "cal-eprocure-archived/ncb"


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


def _record_run_start(conn, source_variant: str, snapshot_date: datetime.date) -> str:
    run_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.cal_eprocure_ingest_runs
                (ingest_run_id, source_variant, snapshot_date, started_at, status)
            VALUES (%s, %s, %s, now(), 'running')
            """,
            (run_id, source_variant, snapshot_date),
        )
    conn.commit()
    logger.info("started run %s variant=%s snapshot=%s", run_id, source_variant, snapshot_date)
    return run_id


def _record_run_complete(conn, run_id: str, rows_ingested: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ops.cal_eprocure_ingest_runs
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
            UPDATE ops.cal_eprocure_ingest_runs
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


def ingest_po(snapshot_date: datetime.date) -> None:
    """Ingest DGS Purchase Order Data (CSV) → R2 ZSTD Parquet."""
    conn = _pg_conn()
    run_id = _record_run_start(conn, "archived_po", snapshot_date)
    try:
        url = _resolve_ckan_url(CKAN_PO_RESOURCE_ID)
        logger.info("downloading PO CSV from %s", url)
        resp = requests.get(url, timeout=300, stream=True)
        resp.raise_for_status()

        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp_csv:
            tmp_csv_path = tmp_csv.name
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                tmp_csv.write(chunk)
        logger.info("downloaded PO CSV → %s", tmp_csv_path)

        # All-VARCHAR read per L9 (CLAUDE.md §"Source ingest invariant")
        df = pd.read_csv(tmp_csv_path, dtype=str, low_memory=False)
        logger.info("parsed PO CSV: %d rows × %d cols", len(df), len(df.columns))

        local_parquet = tmp_csv_path.replace(".csv", ".parquet")
        table = pa.Table.from_pandas(df, preserve_index=False)
        pq.write_table(
            table, local_parquet,
            compression="ZSTD",
            compression_level=9,
        )

        r2_key = f"{R2_PREFIX_PO}/snapshot={snapshot_date}/data.parquet"
        s3 = _r2_client()
        s3.upload_file(
            local_parquet, R2_BUCKET, r2_key,
            ExtraArgs={"ContentType": "application/x-parquet"},
        )
        logger.info("uploaded PO → s3://%s/%s", R2_BUCKET, r2_key)

        _record_run_complete(conn, run_id, len(df))
    except Exception as exc:
        _record_run_failed(conn, run_id, str(exc))
        raise
    finally:
        conn.close()
        for p in (locals().get("tmp_csv_path"), locals().get("local_parquet")):
            if p and Path(p).exists():
                Path(p).unlink(missing_ok=True)


def ingest_ncb(snapshot_date: datetime.date) -> None:
    """Ingest DGS Non-Competitive Bids (XLSX) → R2 ZSTD Parquet.

    Reads sheet_name=0 (first sheet = main NCB data). Any additional
    sheets are instructions/metadata and are intentionally skipped.
    """
    conn = _pg_conn()
    run_id = _record_run_start(conn, "archived_ncb", snapshot_date)
    try:
        url = _resolve_ckan_url(CKAN_NCB_RESOURCE_ID)
        logger.info("downloading NCB XLSX from %s", url)
        resp = requests.get(url, timeout=120)
        resp.raise_for_status()

        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp_xlsx:
            tmp_xlsx_path = tmp_xlsx.name
            tmp_xlsx.write(resp.content)
        logger.info("downloaded NCB XLSX → %s (%d bytes)", tmp_xlsx_path, len(resp.content))

        # All-VARCHAR read; sheet_name=0 = first sheet (main NCB data).
        # header=2: row 0 is a preamble text line, row 1 is blank, row 2 is the real header.
        df = pd.read_excel(tmp_xlsx_path, dtype=str, sheet_name=0, header=2)
        logger.info("parsed NCB XLSX: %d rows × %d cols", len(df), len(df.columns))

        # Ensure all values are strings (Excel may smuggle floats/NaN in all-str mode)
        df = df.where(pd.notnull(df), other=None)
        df = df.astype(object).where(df.notna(), other=None)

        local_parquet = tmp_xlsx_path.replace(".xlsx", ".parquet")
        table = pa.Table.from_pandas(df, preserve_index=False)
        pq.write_table(
            table, local_parquet,
            compression="ZSTD",
            compression_level=9,
        )

        r2_key = f"{R2_PREFIX_NCB}/snapshot={snapshot_date}/data.parquet"
        s3 = _r2_client()
        s3.upload_file(
            local_parquet, R2_BUCKET, r2_key,
            ExtraArgs={"ContentType": "application/x-parquet"},
        )
        logger.info("uploaded NCB → s3://%s/%s", R2_BUCKET, r2_key)

        _record_run_complete(conn, run_id, len(df))
    except Exception as exc:
        _record_run_failed(conn, run_id, str(exc))
        raise
    finally:
        conn.close()
        for p in (locals().get("tmp_xlsx_path"), locals().get("local_parquet")):
            if p and Path(p).exists():
                Path(p).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="CA Cal eProcure archived → R2 ZSTD Parquet ingest"
    )
    parser.add_argument(
        "--variant",
        choices=["po", "ncb", "both"],
        default="both",
        help="Which variant to ingest (default: both)",
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

    logger.info("variant=%s snapshot_date=%s", args.variant, snapshot_date)

    if args.variant in ("po", "both"):
        ingest_po(snapshot_date)
    if args.variant in ("ncb", "both"):
        ingest_ncb(snapshot_date)

    return 0


if __name__ == "__main__":
    sys.exit(main())

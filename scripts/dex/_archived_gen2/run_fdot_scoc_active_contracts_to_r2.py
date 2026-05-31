"""FDOT SCOC active + historical contracts → R2 ZSTD Parquet ingest (one-shot manual).

Reads the operator-downloaded XLSX from the FL DOT State Construction Office Active
Contracts list page (https://scoc.fdot.gov/ → "Export" button → ActiveContractExport_*.xlsx).
The SPA is Cloudflare bot-protected — the "Export" button is the only operator-driven
download path until the underlying JSON/XLSX endpoint is reverse-engineered.

R2 layout (kebab-case per CLAUDE.md):
  s3://dex-raw-landing-zone/fdot/scoc-active-contracts/snapshot={YYYY-MM-DD}/data.parquet

XLSX shape (per 2026-05-19 operator export):
  Single sheet `data`. 1,590 rows × 30 columns. Header row 0.
  Natural PK: Contract ID (e.g. E1R18, E1R76, …). Per row carries Vendor Name +
  Vendor ID (winning contractor) AND Project Engineer/Manager Name (FDOT-side
  contact) — both load-bearing for the EquipmentWork newsletter audience.

All-VARCHAR pandas read (dtype=str). ZSTD Parquet write. Re-running with the same
--snapshot-date overwrites the same R2 key. Each run logs to
ops.fdot_scoc_active_contracts_ingest_runs.

Usage:
    cd ~/hq-all/apps/data-engine-x
    doppler run --project hq-all --config prd -- \\
        uv run python scripts/run_fdot_scoc_active_contracts_to_r2.py \\
        --input-path ~/Downloads/ActiveContractExport_13528.xlsx \\
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

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ── load-bearing constants (verify harness greps for these) ─────────────────

SOURCE_URL = "https://scoc.fdot.gov/"

R2_BUCKET = "dex-raw-landing-zone"
R2_PREFIX = "fdot/scoc-active-contracts"

# Canonical column rename: xlsx header → snake_case schema written to Parquet.
# Order matters — preserves upstream column order so downstream Parquet reads
# stay deterministic.
_COLUMN_RENAME: dict[str, str] = {
    "Active": "active",
    "District": "district",
    "Contract ID": "contract_id",
    "Status": "status",
    "Work Begin": "work_begin",
    "Project Id": "project_id",
    "Description": "description",
    "Work Mix": "work_mix",
    "County": "county",
    "Vendor Name": "vendor_name",
    "Vendor ID": "vendor_id",
    "Project Engineer/Manager Name": "project_engineer_manager_name",
    "Original Contract Days": "original_contract_days",
    "Adjusted Contract Days": "adjusted_contract_days",
    "Current Contract Days": "current_contract_days",
    "Project Type": "project_type",
    "Days Used": "days_used",
    "Original Contract Amount": "original_contract_amount",
    "Current Contract Amount": "current_contract_amount",
    "Estimate Paid to Date": "estimate_paid_to_date",
    "Project Work Type": "project_work_type",
    "Contract Type": "contract_type",
    "Letting Date": "letting_date",
    "Award Date": "award_date",
    "Executed Date": "executed_date",
    "Time Began Date": "time_began_date",
    "Final Acceptance Date": "final_acceptance_date",
    "Start Date": "start_date",
    "Estimated Completion Date": "estimated_completion_date",
    "Map Url True/False": "has_map_url",
}


def _pg_conn():
    return psycopg2.connect(os.environ["DEX_DB_URL_DIRECT"])


def _record_run_start(conn, snapshot_date: datetime.date) -> str:
    run_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.fdot_scoc_active_contracts_ingest_runs
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
            UPDATE ops.fdot_scoc_active_contracts_ingest_runs
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
            UPDATE ops.fdot_scoc_active_contracts_ingest_runs
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


def ingest(input_path: Path, snapshot_date: datetime.date) -> int:
    """Read operator-downloaded FDOT SCOC XLSX → ZSTD Parquet → R2.

    Returns number of data rows written.
    """
    conn = _pg_conn()
    run_id = _record_run_start(conn, snapshot_date)
    local_parquet: str | None = None
    try:
        logger.info("reading FDOT SCOC export from %s", input_path)
        # All-VARCHAR read per L9 (xlsx → pandas → Parquet). Sheet `data` is the
        # single sheet; XLSX has header row 0 with the 30 columns named in
        # _COLUMN_RENAME.
        df = pd.read_excel(input_path, sheet_name="data", dtype=str)
        logger.info("parsed FDOT SCOC xlsx: %d rows × %d cols", len(df), len(df.columns))

        # Verify upstream columns match our expected schema; fail fast on drift.
        missing = [c for c in _COLUMN_RENAME if c not in df.columns]
        if missing:
            raise RuntimeError(
                f"FDOT SCOC: upstream xlsx missing expected columns: {missing!r} "
                f"(have: {df.columns.tolist()!r})"
            )

        # Project + rename in canonical order.
        df = df[list(_COLUMN_RENAME.keys())].rename(columns=_COLUMN_RENAME)

        # Pyarrow Table: every column explicitly string-typed. Per-cell coercion
        # handles pd.read_excel's quirk of leaving NaN as float even with dtype=str;
        # OPSC's pd.read_csv path didn't hit this.
        def _to_str_or_none(v):
            return None if pd.isna(v) else str(v)

        arrays = {
            col: pa.array([_to_str_or_none(v) for v in df[col].tolist()], type=pa.string())
            for col in df.columns
        }
        table = pa.table(arrays)

        with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
            local_parquet = tmp.name
        pq.write_table(
            table, local_parquet,
            compression="ZSTD",
            compression_level=9,
        )
        size = Path(local_parquet).stat().st_size
        logger.info("wrote local parquet: %d bytes", size)

        r2_key = f"{R2_PREFIX}/snapshot={snapshot_date}/data.parquet"
        s3 = _r2_client()
        s3.upload_file(
            local_parquet, R2_BUCKET, r2_key,
            ExtraArgs={"ContentType": "application/x-parquet"},
        )
        logger.info("uploaded FDOT SCOC → s3://%s/%s", R2_BUCKET, r2_key)

        _record_run_complete(conn, run_id, len(df))
        return len(df)
    except Exception as exc:
        _record_run_failed(conn, run_id, str(exc))
        raise
    finally:
        conn.close()
        if local_parquet and Path(local_parquet).exists():
            Path(local_parquet).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="FDOT SCOC active + historical contracts → R2 ZSTD Parquet ingest"
    )
    parser.add_argument(
        "--input-path",
        required=True,
        type=Path,
        help="Local path to the FDOT SCOC ActiveContractExport_*.xlsx file",
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

    if not args.input_path.exists():
        logger.error("input file not found: %s", args.input_path)
        return 2

    logger.info("snapshot_date=%s source_url=%s", snapshot_date, SOURCE_URL)
    n = ingest(args.input_path, snapshot_date)
    logger.info("FDOT SCOC ingest OK: %d rows landed", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())

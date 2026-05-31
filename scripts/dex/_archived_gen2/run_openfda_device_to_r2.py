"""openFDA Medical Device → R2 ZSTD Parquet ingest.

Downloads 510(k) clearances, PMA approvals, and device classification from
the openFDA bulk manifest at https://api.fda.gov/download.json, writes
ZSTD Parquet snapshots to Cloudflare R2, and logs each run to
ops.openfda_device_ingest_runs.

R2 layout:
  s3://dex-raw-landing-zone/openfda/device/510k/snapshot={YYYY-MM-DD}/data.parquet
  s3://dex-raw-landing-zone/openfda/device/pma/snapshot={YYYY-MM-DD}/data.parquet
  s3://dex-raw-landing-zone/openfda/device/classification/snapshot={YYYY-MM-DD}/data.parquet

All columns written as VARCHAR (pandas astype(str)) per CLAUDE.md §"Source ingest invariant"
sub-case "bulk-historical Volume-King sources → R2 ZSTD Parquet → Lance emit" (L9).

Skip-if-unchanged: compares openFDA manifest export_date per variant against the last
completed run's export_date in ops.openfda_device_ingest_runs. No download if unchanged.

Column reference from legacy app/services/openfda_device_ingest.py DATASETS dict:
  510k:           23 scalar cols + openfda jsonb
  pma:            22 scalar cols + openfda jsonb
  classification: 16 scalar cols + openfda jsonb

Usage:
    cd ~/hq-all/apps/data-engine-x
    doppler run --project hq-all --config prd -- \\
        uv run python scripts/run_openfda_device_to_r2.py \\
        [--variant {510k,pma,classification,all}] [--snapshot-date YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import datetime
import io
import json
import logging
import os
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Any

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

MANIFEST_URL = "https://api.fda.gov/download.json"
R2_BUCKET = "dex-raw-landing-zone"

# Per-variant R2 prefix stems (verify harness greps for "openfda/device")
R2_PREFIX_510K           = "openfda/device/510k"
R2_PREFIX_PMA            = "openfda/device/pma"
R2_PREFIX_CLASSIFICATION = "openfda/device/classification"

VARIANT_TO_PREFIX = {
    "510k":           R2_PREFIX_510K,
    "pma":            R2_PREFIX_PMA,
    "classification": R2_PREFIX_CLASSIFICATION,
}

# Scalar columns per variant (from legacy DATASETS dict in openfda_device_ingest.py)
SCALAR_COLS: dict[str, list[str]] = {
    "510k": [
        "k_number", "applicant", "address_1", "address_2", "city", "state",
        "zip_code", "postal_code", "country_code", "contact", "device_name",
        "product_code", "clearance_type", "decision_code", "decision_description",
        "decision_date", "date_received", "advisory_committee",
        "advisory_committee_description", "review_advisory_committee",
        "statement_or_summary", "third_party_flag", "expedited_review_flag",
    ],
    "pma": [
        "pma_number", "supplement_number", "applicant", "street_1", "street_2",
        "city", "state", "zip", "zip_ext", "generic_name", "trade_name",
        "product_code", "advisory_committee", "advisory_committee_description",
        "supplement_type", "supplement_reason", "decision_code", "decision_date",
        "date_received", "docket_number", "expedited_review_flag", "ao_statement",
    ],
    "classification": [
        "product_code", "device_name", "device_class", "regulation_number",
        "review_panel", "review_code", "medical_specialty",
        "medical_specialty_description", "definition", "submission_type_id",
        "gmp_exempt_flag", "implant_flag", "life_sustain_support_flag",
        "third_party_flag", "summary_malfunction_reporting", "unclassified_reason",
    ],
}

JSONB_COLS: dict[str, list[str]] = {
    "510k":           ["openfda"],
    "pma":            ["openfda"],
    "classification": ["openfda"],
}


# ── helpers ──────────────────────────────────────────────────────────────────

def _pg_conn():
    return psycopg2.connect(os.environ["DEX_DB_URL_DIRECT"])


def _r2_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="us-east-1",
    )


def _fetch_manifest() -> dict:
    """Fetch the openFDA bulk download manifest."""
    logger.info("fetching openFDA manifest from %s", MANIFEST_URL)
    resp = requests.get(MANIFEST_URL, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _last_completed_export_date(conn, source_variant: str) -> str | None:
    """Return export_date of the last completed run for this variant, or None."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT export_date::TEXT
              FROM ops.openfda_device_ingest_runs
             WHERE source_variant = %s
               AND status = 'completed'
             ORDER BY started_at DESC
             LIMIT 1
            """,
            (source_variant,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _record_run_start(
    conn, source_variant: str, snapshot_date: datetime.date, export_date: datetime.date
) -> str:
    run_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.openfda_device_ingest_runs
                (ingest_run_id, source_variant, snapshot_date, export_date,
                 started_at, status)
            VALUES (%s, %s, %s, %s, now(), 'running')
            """,
            (run_id, source_variant, snapshot_date, export_date),
        )
    conn.commit()
    logger.info(
        "started run %s variant=%s snapshot=%s export_date=%s",
        run_id, source_variant, snapshot_date, export_date,
    )
    return run_id


def _record_run_complete(conn, run_id: str, rows_ingested: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ops.openfda_device_ingest_runs
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
            UPDATE ops.openfda_device_ingest_runs
               SET status = 'failed', completed_at = now(), error_message = %s
             WHERE ingest_run_id = %s
            """,
            (error_message[:2000], run_id),
        )
    conn.commit()
    logger.error("failed run %s: %s", run_id, error_message[:200])


def _flatten_record(record: dict[str, Any], variant: str) -> dict[str, str | None]:
    """Flatten one JSON result record to an all-VARCHAR dict."""
    scalar_cols = SCALAR_COLS[variant]
    jsonb_cols  = JSONB_COLS[variant]
    row: dict[str, str | None] = {}

    for col in scalar_cols:
        val = record.get(col)
        if val is None:
            row[col] = None
        elif isinstance(val, (dict, list)):
            row[col] = json.dumps(val)
        else:
            row[col] = str(val)

    for col in jsonb_cols:
        val = record.get(col)
        row[col] = json.dumps(val) if val is not None else None

    # Full record as raw_json catch-all
    row["raw_json"] = json.dumps(record)
    return row


def ingest_variant(variant: str, snapshot_date: datetime.date) -> None:
    """Download, flatten, and upload one openFDA device variant to R2."""
    conn = _pg_conn()
    run_id: str | None = None
    tmp_zip_path: str | None = None
    tmp_parquet_path: str | None = None

    try:
        manifest = _fetch_manifest()
        variant_meta = manifest["results"]["device"][variant]
        export_date_str: str = variant_meta["export_date"]      # e.g. "2026-05-19"
        total_records: int   = variant_meta["total_records"]
        partitions: list     = variant_meta["partitions"]

        logger.info(
            "variant=%s export_date=%s total_records=%d partitions=%d",
            variant, export_date_str, total_records, len(partitions),
        )
        export_date = datetime.date.fromisoformat(export_date_str)

        # Skip-if-unchanged: compare manifest export_date to last completed run
        last_export = _last_completed_export_date(conn, variant)
        if last_export and last_export == export_date_str:
            logger.info(
                "variant=%s export_date=%s unchanged — skipping (last completed=%s)",
                variant, export_date_str, last_export,
            )
            return

        run_id = _record_run_start(conn, variant, snapshot_date, export_date)

        # Download all partitions (defensively loop — openFDA splits past a size threshold)
        all_rows: list[dict[str, str | None]] = []
        for partition in partitions:
            zip_url: str = partition["file"]
            logger.info("downloading partition %s (%s MB)", zip_url, partition.get("size_mb", "?"))
            resp = requests.get(zip_url, timeout=600, stream=True)
            resp.raise_for_status()

            with tempfile.NamedTemporaryFile(suffix=".json.zip", delete=False) as tmp_zip:
                tmp_zip_path = tmp_zip.name
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    tmp_zip.write(chunk)
            logger.info("downloaded → %s (%d bytes)", tmp_zip_path, Path(tmp_zip_path).stat().st_size)

            with zipfile.ZipFile(tmp_zip_path) as zf:
                json_name = zf.namelist()[0]
                with zf.open(json_name) as f:
                    data = json.load(f)
            results: list[dict] = data["results"]
            logger.info("parsed %d records from %s", len(results), json_name)

            for record in results:
                all_rows.append(_flatten_record(record, variant))

            Path(tmp_zip_path).unlink(missing_ok=True)
            tmp_zip_path = None

        logger.info("total rows flattened: %d", len(all_rows))

        # Build all-VARCHAR DataFrame and write ZSTD Parquet (L9)
        df = pd.DataFrame(all_rows)
        # Ensure all-VARCHAR: astype(str) on non-null values (preserve None)
        df = df.astype(object)
        for col in df.columns:
            df[col] = df[col].apply(lambda v: str(v) if v is not None else None)

        table = pa.Table.from_pandas(df, preserve_index=False)
        with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp_pq:
            tmp_parquet_path = tmp_pq.name
        pq.write_table(
            table, tmp_parquet_path,
            compression="ZSTD",
            compression_level=9,
        )
        logger.info("wrote ZSTD Parquet → %s (%d bytes)", tmp_parquet_path, Path(tmp_parquet_path).stat().st_size)

        # Upload to R2 — no ContentEncoding (L42)
        r2_prefix = VARIANT_TO_PREFIX[variant]
        r2_key = f"{r2_prefix}/snapshot={snapshot_date}/data.parquet"
        s3 = _r2_client()
        s3.upload_file(
            tmp_parquet_path, R2_BUCKET, r2_key,
            ExtraArgs={"ContentType": "application/x-parquet"},
        )
        logger.info("uploaded → s3://%s/%s", R2_BUCKET, r2_key)

        _record_run_complete(conn, run_id, len(df))

    except Exception as exc:
        if run_id and conn:
            try:
                _record_run_failed(conn, run_id, str(exc))
            except Exception:
                pass
        raise
    finally:
        conn.close()
        for p in (tmp_zip_path, tmp_parquet_path):
            if p and Path(p).exists():
                Path(p).unlink(missing_ok=True)


def ingest(variants: list[str] | None = None, snapshot_date: datetime.date | None = None) -> None:
    """Ingest one or more openFDA device variants.

    Called by the Modal app. Defaults to all 3 variants if not specified.
    """
    if variants is None:
        variants = ["510k", "pma", "classification"]
    if snapshot_date is None:
        snapshot_date = datetime.datetime.now(datetime.timezone.utc).date()
    for v in variants:
        ingest_variant(v, snapshot_date)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="openFDA Medical Device → R2 ZSTD Parquet ingest"
    )
    parser.add_argument(
        "--variant",
        choices=["510k", "pma", "classification", "all"],
        default="all",
        help="Which variant to ingest (default: all)",
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

    variants: list[str]
    if args.variant == "all":
        variants = ["510k", "pma", "classification"]
    else:
        variants = [args.variant]

    logger.info("variants=%s snapshot_date=%s", variants, snapshot_date)
    ingest(variants, snapshot_date)
    return 0


if __name__ == "__main__":
    sys.exit(main())

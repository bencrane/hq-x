#!/usr/bin/env python3
"""s8 — Fetch FDIC institutions list and emit to R2 + Lance.

Fetches the FDIC API (paginated offset paging, limit=10000) and writes:
  R2: fdic/institutions/snapshot=YYYY-MM-DD/institutions.parquet
  Lance: s3://dex-raw-landing-zone/polaris-warehouse/fdic/institutions_lance/

Constraint P4 (R2-cache once + reuse): once the snapshot parquet lands in R2,
subsequent runs with --skip-if-cached skip the HTTP fetch and re-emit Lance from
the cached parquet. The snapshot date is today's date.

Used by the UCC CA lender classifier (s10) to identify banks in the secured-
party roster. FDIC publishes ~4,500-5,500 active institutions.

Usage:
    doppler run --project hq-all --config prd -- \\
        uv run --quiet python3 apps/data-engine-x/scripts/run_fdic_seed.py \\
            --apply [--skip-if-cached]
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import sys
import time
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
LOG = logging.getLogger(__name__)

FDIC_API = "https://api.fdic.gov/banks/institutions"
FDIC_FIELDS = "NAME,CERT,CITY,STNAME,STALP,RSSDID,ACTIVE,REPDTE,INSTCAT"
FDIC_LIMIT = 10_000
R2_BUCKET = "dex-raw-landing-zone"
SNAPSHOT_DATE = date.today().isoformat()  # 2026-05-12
R2_PARQUET_KEY = f"fdic/institutions/snapshot={SNAPSHOT_DATE}/institutions.parquet"
LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/fdic/institutions_lance/"
DATASET_SLUG = "fdic_institutions_lance"


def _r2_client():
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="us-east-1",
        config=Config(retries={"max_attempts": 3, "mode": "standard"}),
    )


def _r2_key_exists(s3, key: str) -> bool:
    """Return True only when a non-empty object exists (ContentLength > 0).

    A 0-byte object is treated as nonexistent to prevent poison-file residue
    from blocking reruns. See modal/landing/r2.py and scripts/_lib/r2_keys.py.
    """
    from scripts._lib.r2_keys import r2_object_is_landed

    return r2_object_is_landed(s3, bucket=R2_BUCKET, key=key)


def _fetch_fdic_institutions() -> list[dict]:
    """Paginate FDIC API and return all institution records."""
    import httpx

    records: list[dict] = []
    offset = 0
    while True:
        params = {
            "fields": FDIC_FIELDS,
            "limit": FDIC_LIMIT,
            "offset": offset,
            "output": "json",
        }
        LOG.info("FDIC fetch: offset=%d ...", offset)
        resp = httpx.get(FDIC_API, params=params, timeout=60.0)
        resp.raise_for_status()
        data = resp.json()
        batch = data.get("data", [])
        if not batch:
            break
        for item in batch:
            records.append(item.get("data", item))
        total_reported = data.get("meta", {}).get("total", None)
        LOG.info("  fetched %d (cumulative %d; reported total=%s)", len(batch), len(records), total_reported)
        if len(batch) < FDIC_LIMIT:
            break
        offset += FDIC_LIMIT
    return records


def _records_to_parquet_bytes(records: list[dict]) -> bytes:
    """Convert list of dicts to ZSTD-Parquet bytes via pyarrow."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    if not records:
        raise ValueError("No FDIC records to write")

    tbl = pa.Table.from_pylist(records)
    buf = io.BytesIO()
    pq.write_table(tbl, buf, compression="zstd")
    return buf.getvalue()


def _parquet_bytes_to_arrow(data: bytes):
    import pyarrow.parquet as pq
    import io
    return pq.read_table(io.BytesIO(data))


def _emit_lance_from_arrow(tbl) -> int:
    """Write FDIC institutions table to Lance. Returns row count."""
    import lance

    storage_options = {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }

    with lance_commit_lock(DATASET_SLUG):
        ds = lance.write_dataset(tbl, LANCE_URI, mode="overwrite", storage_options=storage_options)
        lance_count = ds.count_rows()
        try:
            ds.create_scalar_index("NAME", index_type="BTREE", replace=True)
        except Exception as e:
            LOG.warning("BTREE index failed (non-fatal): %s", e)
        try:
            ds.optimize.compact_files()
            ds.cleanup_old_versions()
        except Exception as e:
            LOG.warning("Optimize failed (non-fatal): %s", e)
        return lance_count


def main() -> int:
    ap = argparse.ArgumentParser(description="FDIC institutions seed (s8)")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true")
    grp.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-if-cached", action="store_true",
                    help="Skip HTTP fetch if snapshot parquet already in R2 (constraint P4)")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            LOG.error("FAIL: %s not set", var)
            return 64

    if args.dry_run:
        LOG.info("DRY RUN — exiting")
        return 0

    s3 = _r2_client()

    parquet_bytes: bytes | None = None

    if args.skip_if_cached and _r2_key_exists(s3, R2_PARQUET_KEY):
        LOG.info("R2 cache hit at %s — skipping HTTP fetch", R2_PARQUET_KEY)
        buf = io.BytesIO()
        s3.download_fileobj(R2_BUCKET, R2_PARQUET_KEY, buf)
        parquet_bytes = buf.getvalue()
        LOG.info("Loaded %d bytes from R2 cache", len(parquet_bytes))
    else:
        LOG.info("Fetching FDIC institutions from %s ...", FDIC_API)
        try:
            records = _fetch_fdic_institutions()
        except Exception as e:
            LOG.warning("FDIC fetch failed (%s); falling back to R2 cache if available", e)
            if _r2_key_exists(s3, R2_PARQUET_KEY):
                LOG.info("Fallback: loading R2 cache at %s", R2_PARQUET_KEY)
                buf = io.BytesIO()
                s3.download_fileobj(R2_BUCKET, R2_PARQUET_KEY, buf)
                parquet_bytes = buf.getvalue()
            else:
                LOG.error("FAIL: FDIC fetch failed and no R2 cache exists")
                return 1
        else:
            LOG.info("Fetched %d FDIC institutions", len(records))
            parquet_bytes = _records_to_parquet_bytes(records)

            # Upload to R2 (snapshot-partitioned path per reviewer fix)
            LOG.info("Uploading to s3://%s/%s ...", R2_BUCKET, R2_PARQUET_KEY)
            s3.put_object(Bucket=R2_BUCKET, Key=R2_PARQUET_KEY, Body=parquet_bytes)
            LOG.info("Uploaded %d bytes", len(parquet_bytes))

    # Emit Lance
    import pyarrow.parquet as pq
    tbl = pq.read_table(io.BytesIO(parquet_bytes))
    LOG.info("Emitting Lance from %d rows ...", len(tbl))
    lance_count = _emit_lance_from_arrow(tbl)
    LOG.info("s8 complete: %d rows in Lance at %s", lance_count, LANCE_URI)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

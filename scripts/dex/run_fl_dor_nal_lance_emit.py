#!/usr/bin/env python3
"""FL DOR NAL parquet (R2) -> Lance source dataset (Pattern A).

Reads  r2://dex-raw-landing-zone/fl-dor-nal/snapshot=*/*.parquet
       (latest_snapshot mode globs all 67 county parquets into one dataset)
Writes s3://dex-raw-landing-zone/polaris-warehouse/fl_dor/parcels_nal_lance

Primary BTREE on parcel_uid (CO_NO + PARCEL_ID = globally unique). Secondary
BTREE indexes on the high-traffic filter keys for property targeting:
county, DOR use code, situs zip, owner mailing zip, normalized owner name.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_fl_dor_nal_lance_emit.py --dry-run
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_fl_dor_nal_lance_emit.py --apply
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts._lib.lance_emit import (  # noqa: E402
    LanceEmitConfig,
    _ensure_tmpdir,
    _lance_storage_options,
)
from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
LOG = logging.getLogger("fl-dor-nal-lance")

R2_BUCKET = "dex-raw-landing-zone"
SNAPSHOT_PREFIX = "fl-dor-nal/snapshot=2025-10-02/"
LOCAL_SYNC_DIR = "/tmp/fldor/lance_in"

CONFIG = LanceEmitConfig(
    dataset_slug="fl_dor_parcels_nal",
    r2_bucket="dex-raw-landing-zone",
    parquet_input_prefix="fl-dor-nal",
    parquet_file_pattern="*.parquet",
    partition_mode="latest_snapshot",
    lance_uri="s3://dex-raw-landing-zone/polaris-warehouse/fl_dor/parcels_nal_lance",
    btree_column="parcel_uid",
)

# Secondary BTREE scalar indexes (primary parcel_uid handled by emit_lance).
SECONDARY_BTREE = [
    "co_no",
    "dor_uc",
    "phy_zipcd",
    "own_zipcd",
    "owner_name_normalized",
]


def _r2_client():
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 6, "mode": "adaptive"},
            read_timeout=120,
        ),
    )


def _sync_parquets_local() -> str:
    """Download all county parquets from R2 to local disk (robust per-file
    boto3 retries) so the Lance read never streams over flaky httpfs. Returns
    a local glob. Idempotent: skips files already present with matching size.
    """
    from pathlib import Path

    s3 = _r2_client()
    Path(LOCAL_SYNC_DIR).mkdir(parents=True, exist_ok=True)
    paginator = s3.get_paginator("list_objects_v2")
    keys = [
        (o["Key"], o["Size"])
        for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=SNAPSHOT_PREFIX)
        for o in page.get("Contents", [])
        if o["Key"].endswith(".parquet")
    ]
    LOG.info("syncing %d county parquets -> %s", len(keys), LOCAL_SYNC_DIR)
    for key, size in keys:
        dest = Path(LOCAL_SYNC_DIR) / key.split("/")[-1]
        if dest.exists() and dest.stat().st_size == size:
            continue
        # Explicit retry-with-backoff: R2 throttles sustained reads; boto3's
        # internal retries fire too fast to clear it. Backoff gives R2 room.
        last_err = None
        for attempt in range(8):
            try:
                s3.download_file(R2_BUCKET, key, str(dest))
                last_err = None
                break
            except Exception as e:
                last_err = e
                wait = min(2 ** attempt, 30)
                LOG.warning(
                    "  %s attempt %d failed (%s); retrying in %ds",
                    key.split("/")[-1], attempt + 1, type(e).__name__, wait,
                )
                time.sleep(wait)
        if last_err is not None:
            raise last_err
    return f"{LOCAL_SYNC_DIR}/*.parquet"


def _add_secondary_indexes(ds) -> None:
    os.environ["LANCE_BYPASS_SPILLING"] = "true"
    for col in SECONDARY_BTREE:
        t0 = time.time()
        LOG.info("creating BTREE scalar index on %s ...", col)
        try:
            ds.create_scalar_index(col, index_type="BTREE", replace=True)
            LOG.info("  %s index built in %.1fs", col, time.time() - t0)
        except Exception as e:  # non-fatal: a missing col shouldn't kill the run
            LOG.warning("  %s index FAILED (non-fatal): %s", col, e)


def emit_from_local() -> tuple[int, int]:
    """Read locally-synced parquet -> Lance (R2). Returns (parquet, lance) rows."""
    import duckdb
    import lance

    local_glob = _sync_parquets_local()
    con = duckdb.connect()
    pq_rows = con.execute(
        "SELECT count(*) FROM read_parquet('%s')" % local_glob
    ).fetchone()[0]
    LOG.info("local parquet rows: %d", pq_rows)

    storage_options = _lance_storage_options()
    t0 = time.time()
    with lance_commit_lock(CONFIG.dataset_slug):
        reader = con.from_query(
            "SELECT * FROM read_parquet('%s')" % local_glob
        ).to_arrow_reader(batch_size=100_000)
        LOG.info("writing Lance dataset (overwrite) -> %s", CONFIG.lance_uri)
        ds = lance.write_dataset(
            reader,
            CONFIG.lance_uri,
            mode="overwrite",
            storage_options=storage_options,
        )
        lance_rows = ds.count_rows()
        LOG.info(
            "wrote %d rows in %.1fs (version=%s)",
            lance_rows, time.time() - t0, ds.version,
        )
        os.environ["LANCE_BYPASS_SPILLING"] = "true"
        t1 = time.time()
        LOG.info("creating primary BTREE on %s ...", CONFIG.btree_column)
        ds.create_scalar_index(
            CONFIG.btree_column, index_type="BTREE", replace=True
        )
        LOG.info("  primary index built in %.1fs", time.time() - t1)
        _add_secondary_indexes(ds)
        try:
            LOG.info("optimize: compact_files ...")
            ds.optimize.compact_files()
        except Exception as e:
            LOG.warning("  compact_files failed (non-fatal): %s", e)
    return pq_rows, lance_rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="FL DOR NAL Lance emit")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--apply", action="store_true")
    g.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    for v in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(v):
            LOG.error("FAIL: %s not set", v)
            return 64
    _ensure_tmpdir()

    if args.dry_run:
        import duckdb

        local_glob = _sync_parquets_local()
        con = duckdb.connect()
        n = con.execute(
            "SELECT count(*) FROM read_parquet('%s')" % local_glob
        ).fetchone()[0]
        cc = con.execute(
            "SELECT count(DISTINCT co_no) FROM read_parquet('%s')" % local_glob
        ).fetchone()[0]
        LOG.info("DRY RUN: %d parcels across %d counties", n, cc)
        return 0

    pq_rows, lance_rows = emit_from_local()
    if pq_rows != lance_rows:
        LOG.error("FAIL: row mismatch parquet=%d lance=%d", pq_rows, lance_rows)
        return 1
    LOG.info(
        "OK — Lance dataset emitted: %d rows, primary + %d secondary indexes",
        lance_rows, len(SECONDARY_BTREE),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

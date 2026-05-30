#!/usr/bin/env python3
"""s6 — Emit UCC CA filing amendments as a Lance dataset on R2.

Reads: s3://dex-raw-landing-zone/ucc-ca/master/snapshot=2026-05-01/parsed/filing_amendments.parquet
Writes: s3://dex-raw-landing-zone/polaris-warehouse/ucc_ca/filing_amendments_lance/

PK: (UCC1_NUM, UCC3_NUM) — drives the "is this filing currently in force" lifecycle.
BTREE index on UCC1_NUM.

Usage:
    doppler run --project hq-all --config prd -- \\
        uv run --quiet python3 apps/data-engine-x/scripts/emit_ucc_ca_filing_amendments_lance.py \\
            --apply
"""
from __future__ import annotations

import logging
import os
import sys
import time
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

PARQUET_URI = (
    "r2://dex-raw-landing-zone/ucc-ca/master/snapshot=2026-05-01/parsed/filing_amendments.parquet"
)
LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/ucc_ca/filing_amendments_lance/"
DATASET_SLUG = "ucc_ca_filing_amendments_lance"


def _r2_account_id() -> str:
    ep = os.environ["R2_ENDPOINT"]
    return ep.split("//")[-1].split(".")[0]


def _connect_duckdb():
    import duckdb

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(
        f"""
        CREATE SECRET (
            TYPE r2,
            KEY_ID '{os.environ["R2_ACCESS_KEY_ID"]}',
            SECRET '{os.environ["R2_SECRET_ACCESS_KEY"]}',
            ACCOUNT_ID '{_r2_account_id()}'
        )
        """
    )
    return con


def _storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Emit UCC CA filing amendments Lance (s6)")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true")
    grp.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            LOG.error("FAIL: %s not set", var)
            return 64

    con = _connect_duckdb()

    total = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('{PARQUET_URI}')"
    ).fetchone()[0]
    LOG.info("Source rows: %d", total)

    if args.dry_run:
        LOG.info("DRY RUN — exiting")
        return 0

    import lance

    storage_options = _storage_options()

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        reader = con.execute(
            f"""
            SELECT
                UCC1_NUM,
                UCC3_NUM,
                ACTION_TYPE
            FROM read_parquet('{PARQUET_URI}')
            """
        ).to_arrow_reader(batch_size=100_000)

        LOG.info("Writing Lance dataset (mode=overwrite) to %s ...", LANCE_URI)
        ds = lance.write_dataset(
            reader,
            LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t0
        lance_count = ds.count_rows()
        LOG.info("Wrote %d rows in %.1fs (version=%s)", lance_count, write_dur, ds.version)

        try:
            ds.create_scalar_index("UCC1_NUM", index_type="BTREE", replace=True)
        except Exception as e:
            LOG.warning("BTREE index failed (non-fatal): %s", e)

        try:
            ds.optimize.compact_files()
            ds.cleanup_old_versions()
        except Exception as e:
            LOG.warning("Optimize failed (non-fatal): %s", e)

    LOG.info("s6 complete: %d rows, %.1fs", lance_count, time.time() - t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

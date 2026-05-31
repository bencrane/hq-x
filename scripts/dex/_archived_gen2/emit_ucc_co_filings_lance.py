#!/usr/bin/env python3
"""s1 — Emit UCC CO filings as a Lance dataset on R2.

Reads:  s3://dex-raw-landing-zone/ucc/state=CO/stream=filings/snapshot=2026-05-08/data.parquet
Writes: s3://dex-raw-landing-zone/polaris-warehouse/ucc_co/filings_lance/

Column rename map (CO source → CA parity schema):
  fileid            → UCC1_NUM               (promoted; 2,547,786 distinct values — effectively unique)
  (none)            → UCC3_NUM               (NULL — CO has no UCC-3 amendment num in source)
  filingdate_date   → FILING_DATE            (TRY_CAST to TIMESTAMP)
  (none)            → PROCESSED_DATE         (NULL — CO source lacks this field)
  transactiontype   → ACTION_TYPE
  (none)            → ALT_DESIGNATION_TYPE_ID (NULL — CO source lacks this field)
  filingtype        → FILING_TYPE_ID
  lapsedate_date    → LAPSE_DATE             (TRY_CAST to TIMESTAMP)
  (none)            → PAGE_COUNT             (NULL — CO source lacks this field)
  filingdate_date   → filing_year            (YEAR(...) derived column)

BTREE index on UCC1_NUM (primary join key for debtors + secured parties + collateral).
Commit-lock via Postgres advisory lock (lance_commit_lock constraint).

MIN_ROW_FLOOR = 2_400_000 — HARD FAIL gate fires BEFORE lance.write_dataset().
Source has 2,547,798 rows; floor at ~94% (allows cast/null-filter drift).

NOTE: This is a NEW invariant — CA emit scripts only WARN on row-count mismatch;
CO scripts HARD FAIL (sys.exit(1)) if source count falls below the floor.

Usage:
    doppler run --project hq-all --config prd -- \\
        uv run --quiet python3 apps/data-engine-x/scripts/emit_ucc_co_filings_lance.py \\
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
    "r2://dex-raw-landing-zone/ucc/state=CO/stream=filings/snapshot=2026-05-08/data.parquet"
)
LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/ucc_co/filings_lance/"
DATASET_SLUG = "ucc_co_filings_lance"

# HARD FAIL gate — fires BEFORE lance.write_dataset()
# CA emit scripts only WARN; this is new for CO cycle.
MIN_ROW_FLOOR = 2_400_000


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

    ap = argparse.ArgumentParser(description="Emit UCC CO filings Lance (s1)")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true")
    grp.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            LOG.error("FAIL: %s not set", var)
            return 64

    con = _connect_duckdb()

    # Count source rows — HARD FAIL gate fires here, BEFORE any write
    total = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('{PARQUET_URI}')"
    ).fetchone()[0]
    LOG.info("Source rows: %d (floor: %d)", total, MIN_ROW_FLOOR)

    if total < MIN_ROW_FLOOR:
        LOG.error(
            "FAIL: source row count %d is below MIN_ROW_FLOOR %d — aborting (no Lance write)",
            total, MIN_ROW_FLOOR,
        )
        return 1

    if args.dry_run:
        LOG.info("DRY RUN — exiting without writing Lance dataset")
        return 0

    import lance

    storage_options = _storage_options()

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        # Column rename map: CO lowercase source → CA parity schema (10 columns)
        reader = con.execute(
            f"""
            SELECT
                fileid AS UCC1_NUM,
                CAST(NULL AS VARCHAR) AS UCC3_NUM,
                TRY_CAST(filingdate_date AS TIMESTAMP) AS FILING_DATE,
                CAST(NULL AS TIMESTAMP) AS PROCESSED_DATE,
                transactiontype AS ACTION_TYPE,
                CAST(NULL AS VARCHAR) AS ALT_DESIGNATION_TYPE_ID,
                filingtype AS FILING_TYPE_ID,
                TRY_CAST(lapsedate_date AS TIMESTAMP) AS LAPSE_DATE,
                CAST(NULL AS INTEGER) AS PAGE_COUNT,
                YEAR(TRY_CAST(filingdate_date AS TIMESTAMP)) AS filing_year
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

        # BTREE index on UCC1_NUM — primary join key for debtors + secured parties + collateral
        LOG.info("Building BTREE index on UCC1_NUM ...")
        try:
            os.environ.setdefault("LANCE_INDEX_CACHE_SIZE", "1g")
            ds.create_scalar_index("UCC1_NUM", index_type="BTREE", replace=True)
            LOG.info("BTREE index on UCC1_NUM: OK")
        except Exception as e:
            LOG.error(
                "BTREE index on UCC1_NUM FAILED: %s — "
                "this is a HARD GATE (retry with LANCE_INDEX_CACHE_SIZE=1g or HALT with "
                "blocked-btree-memory)",
                e,
            )
            raise

        try:
            ds.optimize.compact_files()
            ds.cleanup_old_versions()
        except Exception as e:
            LOG.warning("Optimize failed (non-fatal): %s", e)

    LOG.info("s1 complete: %d rows, %.1fs", lance_count, time.time() - t0)
    if lance_count != total:
        LOG.warning("Row count mismatch: source=%d lance=%d", total, lance_count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

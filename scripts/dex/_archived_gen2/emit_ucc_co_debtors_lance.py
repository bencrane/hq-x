#!/usr/bin/env python3
"""s2 — Emit UCC CO debtors as a Lance dataset on R2.

Reads:  s3://dex-raw-landing-zone/ucc/state=CO/stream=debtors/snapshot=2026-05-08/data.parquet
Writes: s3://dex-raw-landing-zone/polaris-warehouse/ucc_co/debtors_lance/

Column rename map (CO source → CA parity schema, 15 output columns):
  fileid            → UCC1_NUM
  (none)            → UCC3_NUM               (NULL — CO has no UCC-3 amendment num)
  organizationtype  → DEBTOR_TYPE            (derived: 'Organization' if organizationname not null, else 'Individual')
  organizationname  → ORG_NAME
  lastname          → LAST_NAME
  firstname         → FIRST_NAME
  middlename        → MIDDLE_NAME
  (none)            → SUFFIX                 (NULL — CO source lacks this)
  address1          → ADDR1
  address2          → ADDR2
  (none)            → ADDR3                  (NULL — CO source lacks this)
  city              → CITY
  state             → STATE
  zipcode           → POSTAL_CODE
  country           → COUNTRY

CO-only columns (party_name_normalized, organizationjurisdiction, etc.) are intentionally
dropped from the parity output — they are not in CA's schema and would break cross-state joins.

BTREE index on UCC1_NUM (join key back to filings).
Commit-lock via Postgres advisory lock (lance_commit_lock constraint).

MIN_ROW_FLOOR = 1_900_000 — HARD FAIL gate fires BEFORE lance.write_dataset().
Source has 1,985,901 rows; floor at ~95.7%.

Usage:
    doppler run --project hq-all --config prd -- \\
        uv run --quiet python3 apps/data-engine-x/scripts/emit_ucc_co_debtors_lance.py \\
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
    "r2://dex-raw-landing-zone/ucc/state=CO/stream=debtors/snapshot=2026-05-08/data.parquet"
)
LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/ucc_co/debtors_lance/"
DATASET_SLUG = "ucc_co_debtors_lance"

# HARD FAIL gate — fires BEFORE lance.write_dataset()
MIN_ROW_FLOOR = 1_900_000


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

    ap = argparse.ArgumentParser(description="Emit UCC CO debtors Lance (s2)")
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
        # Column rename map: CO lowercase source → CA parity schema (15 columns)
        reader = con.execute(
            f"""
            SELECT
                fileid AS UCC1_NUM,
                CAST(NULL AS VARCHAR) AS UCC3_NUM,
                CASE WHEN organizationname IS NOT NULL THEN 'Organization' ELSE 'Individual' END AS DEBTOR_TYPE,
                organizationname AS ORG_NAME,
                lastname AS LAST_NAME,
                firstname AS FIRST_NAME,
                middlename AS MIDDLE_NAME,
                CAST(NULL AS VARCHAR) AS SUFFIX,
                address1 AS ADDR1,
                address2 AS ADDR2,
                CAST(NULL AS VARCHAR) AS ADDR3,
                city AS CITY,
                state AS STATE,
                zipcode AS POSTAL_CODE,
                country AS COUNTRY
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

        LOG.info("Building BTREE index on UCC1_NUM ...")
        try:
            os.environ.setdefault("LANCE_INDEX_CACHE_SIZE", "1g")
            ds.create_scalar_index("UCC1_NUM", index_type="BTREE", replace=True)
            LOG.info("BTREE index on UCC1_NUM: OK")
        except Exception as e:
            LOG.error(
                "BTREE index on UCC1_NUM FAILED: %s — HALT with blocked-btree-memory if retry fails",
                e,
            )
            raise

        try:
            ds.optimize.compact_files()
            ds.cleanup_old_versions()
        except Exception as e:
            LOG.warning("Optimize failed (non-fatal): %s", e)

    LOG.info("s2 complete: %d rows, %.1fs", lance_count, time.time() - t0)
    if lance_count != total:
        LOG.warning("Row count mismatch: source=%d lance=%d", total, lance_count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

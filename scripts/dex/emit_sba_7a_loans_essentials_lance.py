#!/usr/bin/env python3
"""Lance-emit: SBA 7(a) loans — column-rich, all decades.

Reads `s3://dex-raw-landing-zone/sba/program=7a/decade=*/` across all 4
decades (1991, 2000, 2010, 2020) and writes ALL 47 source columns to Lance at
`s3://dex-raw-landing-zone/polaris-warehouse/sba/7a_loans_essentials_lance/`.

This is a PARALLEL dataset to the existing column-thin `sba_loans_lance` union
(19 columns, all programs). This emitter is 7(a)-only and preserves the full
column set needed by the capital-expansion funnel — including franchisecode,
firstdisbursementdate, businesstype, businessage, jobssupported, etc.

DO NOT modify this emitter to widen sba_loans_lance. The two datasets are
intentionally separate:
  - sba_loans_lance       → 19-col stable cross-program union (schema-frozen)
  - 7a_loans_essentials_lance → 47-col rich 7(a)-only essentials (this file)

Sibling: apps/data-engine-x/scripts/emit_sba_loans_lance.py

Type casts required per contract:
  borrzip  DOUBLE → VARCHAR (zero-padded 5-digit; raw parquet loses leading zeros)
  naicscode DOUBLE → VARCHAR (defense in depth against leading-zero loss)

Filter: WHERE borrname IS NOT NULL (matches existing emitter convention).

Decades scope: Option A — all 4 decades (1991/2000/2010/2020). All share the
same 47-column schema (validator confirmed 2026-05-14).

Advisory lock key: 'sba_7a_loans_essentials_lance' (DISTINCT from the existing
'sba_loans_lance' key — both can run concurrently without contention).

Arrow-bridge pattern (NOT lance-duckdb extension — unstable on macOS arm64
per Lance canary cycle report 2026-05-12).

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/emit_sba_7a_loans_essentials_lance.py --apply

  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/emit_sba_7a_loans_essentials_lance.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("emit_sba_7a_loans_essentials_lance")

# Lance output URI
LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sba/7a_loans_essentials_lance/"
DATASET_SLUG = "sba_7a_loans_essentials_lance"

# R2 input glob — 7(a) only, all decades
R2_BUCKET = "dex-raw-landing-zone"
SBA_7A_GLOB = "sba/program=7a/decade=*/*.parquet"

TMP_DIR = "/tmp/lance"

# Row floor: validator measured 1,947,098 rows across all 4 decades.
# Floor of 1.9M gives ~2.5% buffer for WHERE borrname IS NOT NULL filter.
ROW_FLOOR = 1_900_000


def _r2_account_id() -> str:
    ep = os.environ["R2_ENDPOINT"]
    return ep.split("//")[-1].split(".")[0]


def _lance_storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _connect_duckdb_to_r2():
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
        );
        """
    )
    return con


def _count_rows(con) -> int:
    """Count total 7(a) rows across all decades (used for dry-run gate)."""
    uri = f"r2://{R2_BUCKET}/{SBA_7A_GLOB}"
    logger.info("counting 7a rows across all decades ...")
    n = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('{uri}', union_by_name=true, hive_partitioning=true)"
    ).fetchone()[0]
    logger.info("  7a total: %d", n)
    return n


def _build_query() -> str:
    """Build the SELECT for all 47 source columns with required type casts.

    Preserves ALL source columns. Two required casts:
      borrzip  DOUBLE → 5-digit zero-padded VARCHAR (ZIP codes lose leading zeros as DOUBLE)
      naicscode DOUBLE → VARCHAR (defense in depth)

    Duplicate columns (partition vs data) are intentionally preserved:
      decade (Hive partition BIGINT) and sba_decade (data SMALLINT) — same values
      program (Hive partition VARCHAR) and sba_program (data VARCHAR) — same values
    """
    uri = f"r2://{R2_BUCKET}/{SBA_7A_GLOB}"
    return f"""
    SELECT
        approvaldate,
        approvalfy,
        asofdate,
        bankcity,
        cast(bankfdicnumber AS VARCHAR)                    AS bankfdicnumber,
        bankname,
        cast(bankncuanumber AS VARCHAR)                    AS bankncuanumber,
        bankstate,
        bankstreet,
        bankzip,
        borrcity,
        borrname,
        borrstate,
        borrstreet,
        -- borrzip: DOUBLE in raw parquet — cast to 5-digit zero-padded VARCHAR
        lpad(cast(cast(borrzip AS BIGINT) AS VARCHAR), 5, '0') AS borrzip,
        businessage,
        businesstype,
        chargeoffdate,
        collateralind,
        congressionaldistrict,
        decade,
        firstdisbursementdate,
        fixedorvariableinterestind,
        franchisecode,
        franchisename,
        grossapproval,
        grosschargeoffamount,
        initialinterestrate,
        jobssupported,
        loanstatus,
        locationid,
        -- naicscode: DOUBLE in raw parquet — cast to VARCHAR
        cast(cast(naicscode AS BIGINT) AS VARCHAR)         AS naicscode,
        naicsdescription,
        paidinfulldate,
        processingmethod,
        program,
        projectcounty,
        projectstate,
        revolverstatus,
        sba_decade,
        sba_decade_label,
        sba_program,
        sbadistrictoffice,
        sbaguaranteedapproval,
        soldsecmrktind,
        subprogram,
        terminmonths
    FROM read_parquet('{uri}', union_by_name=true, hive_partitioning=true)
    WHERE borrname IS NOT NULL
    """


def _emit(dry_run: bool) -> int:
    """Main logic. Returns exit code 0 on success."""
    import lance

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = TMP_DIR

    logger.info("=" * 60)
    logger.info("emit_sba_7a_loans_essentials_lance")
    logger.info("output: %s", LANCE_URI)
    logger.info("slug (advisory lock key): %s", DATASET_SLUG)

    con = _connect_duckdb_to_r2()

    total = _count_rows(con)

    if dry_run:
        logger.info("DRY RUN — total rows=%d (floor=%d, pass=%s)",
                    total, ROW_FLOOR, total >= ROW_FLOOR)
        if total < ROW_FLOOR:
            logger.error("FAIL: row count %d < floor %d", total, ROW_FLOOR)
            return 1
        return 0

    # Build 7(a)-only query and write to Lance
    emit_sql = _build_query()
    storage_options = _lance_storage_options()

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        logger.info("writing Lance dataset (mode=overwrite) ...")
        reader = con.from_query(emit_sql).to_arrow_reader(batch_size=100_000)
        ds = lance.write_dataset(
            reader,
            LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t0
        lance_count = ds.count_rows()
        logger.info("wrote %d rows in %.1fs (version=%s)", lance_count, write_dur, ds.version)

        if lance_count < ROW_FLOOR:
            logger.error("FAIL: lance_count=%d < floor=%d", lance_count, ROW_FLOOR)
            return 1

        os.environ["LANCE_BYPASS_SPILLING"] = "true"
        logger.info("creating BTREE index on locationid ...")
        t_idx = time.time()
        try:
            ds.create_scalar_index("locationid", index_type="BTREE", replace=True)
            logger.info("  BTREE built in %.1fs", time.time() - t_idx)
        except Exception as e:
            logger.error("BTREE index FAILED: %s", e)
            raise

        logger.info("optimize: compact + cleanup_older_than=7d ...")
        try:
            stats = ds.optimize.compact_files()
            logger.info("  compact_files: %s", stats)
        except Exception as e:
            logger.warning("  compact_files failed (non-fatal): %s", e)
        try:
            cleanup = ds.cleanup_old_versions(older_than=timedelta(days=7))
            logger.info("  cleanup_old_versions: %s", cleanup)
        except Exception as e:
            logger.warning("  cleanup_old_versions failed (non-fatal): %s", e)

    logger.info("=" * 60)
    logger.info("OK — rows=%d", lance_count)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Lance emit: SBA 7(a) loans essentials (column-rich)")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true", help="write Lance dataset")
    grp.add_argument("--dry-run", action="store_true", help="count only, no write")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            logger.error("FAIL: %s not set in environment", var)
            return 64

    return _emit(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())

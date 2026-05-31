#!/usr/bin/env python3
"""Lance-emit: SBA canonical lenders derive.

Reads `polaris-warehouse/sba/loans_lance/` via pyarrow.dataset + Arrow-bridge
to DuckDB (NOT the lance-duckdb extension — unstable on macOS arm64).

Filters `bankname IS NOT NULL AND bankstate IS NOT NULL`. Groups by
`(bankname_normalized, COALESCE(bankfdicnumber, bankncuanumber, 'sblc'), bankstate)`
to derive one row per canonical lender. Writes to Lance at
`s3://dex-raw-landing-zone/polaris-warehouse/sba/lenders_lance/`.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow python \\
    apps/data-engine-x/scripts/emit_sba_lenders_lance.py --apply
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

from scripts._lib.entity_name_normalize import __version__ as NORMALIZER_VERSION  # noqa: E402
from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("emit_sba_lenders_lance")

LOANS_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sba/loans_lance/"
LENDERS_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sba/lenders_lance/"
DATASET_SLUG = "sba_lenders_lance"
TMP_DIR = "/tmp/lance"

# Row floor per directive §"Volume floors"
ROW_FLOOR = 4_000


def _lance_storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _normalize_entity_sql(raw_expr: str) -> str:
    """Apply entity_name_normalize.py v1.0.0 in SQL. NORMALIZER_VERSION={NORMALIZER_VERSION}"""
    suffixes = "incorporated|corporation|company|limited|pllc|llp|lp|llc|inc|ltd|corp|co|pa"
    return f"""
        CASE
          WHEN {raw_expr} IS NULL OR trim({raw_expr}) = '' THEN NULL
          ELSE NULLIF(
            trim(
              regexp_replace(
                regexp_replace(
                  regexp_replace(
                    lower(trim({raw_expr})),
                    '\\b({suffixes})\\b\\.?',
                    ' ',
                    'g'
                  ),
                  '[^\\w\\s]+',
                  ' ',
                  'g'
                ),
                '\\s+',
                ' ',
                'g'
              )
            ),
            ''
          )
        END
    """.strip()


def _emit(dry_run: bool) -> int:
    import lance

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = TMP_DIR

    logger.info("=" * 60)
    logger.info("emit_sba_lenders_lance — NORMALIZER_VERSION=%s", NORMALIZER_VERSION)
    logger.info("input:  %s", LOANS_LANCE_URI)
    logger.info("output: %s", LENDERS_LANCE_URI)

    storage_options = _lance_storage_options()

    logger.info("opening loans_lance via pyarrow (Arrow-bridge) ...")
    loans_ds = lance.dataset(LOANS_LANCE_URI, storage_options=storage_options)
    total_loans = loans_ds.count_rows()
    logger.info("loans_lance total rows: %d", total_loans)

    needed_cols = [
        "bankname", "bankstate", "bankfdicnumber", "bankncuanumber",
        "borrstate", "grossapproval", "approvaldate", "loanstatus",
        "franchisename", "naicscode", "disbursementdate",
    ]
    # Use & on Expression objects — pc.and_() is not registered in Substrait.
    import pyarrow.compute as pc
    scanner = loans_ds.scanner(
        columns=needed_cols,
        filter=pc.field("bankname").is_valid() & pc.field("bankstate").is_valid(),
    )
    logger.info("scanning loans with bankname IS NOT NULL AND bankstate IS NOT NULL ...")
    loans_arrow = scanner.to_table()
    logger.info("filtered loans: %d rows", len(loans_arrow))

    import duckdb
    con = duckdb.connect()
    con.register("loans_banks", loans_arrow)

    norm_bankname = _normalize_entity_sql("bankname")

    if dry_run:
        est = con.execute(
            f"""SELECT COUNT(DISTINCT (({norm_bankname}), bankstate))
                FROM loans_banks WHERE bankname IS NOT NULL"""
        ).fetchone()[0]
        logger.info("DRY RUN — estimated canonical lenders: %d (floor=%d, pass=%s)",
                    est, ROW_FLOOR, est >= ROW_FLOOR)
        return 0 if est >= ROW_FLOOR else 1

    lender_sql = f"""
    SELECT
        ({norm_bankname})                               AS bankname_normalized,
        MAX(bankname)                                   AS bankname_sample,
        bankstate,
        COALESCE(bankfdicnumber, bankncuanumber, 'sblc') AS lender_key,
        CASE
            WHEN bankfdicnumber IS NOT NULL THEN 'fdic_bank'
            WHEN bankncuanumber IS NOT NULL THEN 'ncua_cu'
            ELSE 'sblc'
        END                                             AS lender_type,
        MAX(bankfdicnumber)                             AS bankfdicnumber,
        MAX(bankncuanumber)                             AS bankncuanumber,
        COUNT(*)                                        AS total_loans,
        SUM(grossapproval)                              AS total_originated_dollars,
        COUNT(*) FILTER (WHERE EXTRACT(YEAR FROM approvaldate) >= 2024)
                                                        AS recent_loans,
        COUNT(*) FILTER (WHERE loanstatus = 'COMMIT')   AS pending_loans,
        MEDIAN(
            CASE WHEN disbursementdate IS NOT NULL AND approvaldate IS NOT NULL
                 THEN date_diff('day', approvaldate, disbursementdate)
            END
        )                                               AS median_time_to_disburse,
        ARRAY_AGG(DISTINCT borrstate)
            FILTER (WHERE borrstate IS NOT NULL)        AS geographic_footprint
    FROM loans_banks
    WHERE ({norm_bankname}) IS NOT NULL
    GROUP BY ({norm_bankname}), bankstate,
             COALESCE(bankfdicnumber, bankncuanumber, 'sblc'),
             CASE WHEN bankfdicnumber IS NOT NULL THEN 'fdic_bank'
                  WHEN bankncuanumber IS NOT NULL THEN 'ncua_cu'
                  ELSE 'sblc' END
    """

    # .arrow() returns RecordBatchReader; .read_all() materializes to pyarrow.Table
    lenders_arrow = con.execute(lender_sql).arrow().read_all()
    logger.info("derived lenders: %d rows", len(lenders_arrow))

    if len(lenders_arrow) < ROW_FLOOR:
        logger.error("FAIL: lenders=%d < floor=%d", len(lenders_arrow), ROW_FLOOR)
        return 1

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        logger.info("writing lenders to Lance (mode=overwrite) ...")
        ds = lance.write_dataset(
            lenders_arrow,
            LENDERS_LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t0
        lance_count = ds.count_rows()
        logger.info("wrote %d rows in %.1fs (version=%s)", lance_count, write_dur, ds.version)

        os.environ["LANCE_BYPASS_SPILLING"] = "true"
        try:
            ds.create_scalar_index("bankname_normalized", index_type="BTREE", replace=True)
        except Exception as e:
            logger.error("BTREE index FAILED: %s", e)
            raise

        try:
            ds.optimize.compact_files()
        except Exception as e:
            logger.warning("compact_files failed (non-fatal): %s", e)
        try:
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as e:
            logger.warning("cleanup_old_versions failed (non-fatal): %s", e)

    logger.info("=" * 60)
    logger.info("OK — lenders written: %d", lance_count)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Lance emit: SBA canonical lenders")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true")
    grp.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            logger.error("FAIL: %s not set", var)
            return 64

    return _emit(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())

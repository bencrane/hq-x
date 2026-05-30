#!/usr/bin/env python3
"""Lance-emit: PPP borrowers Pattern A direct-source-hydration.

Reads `polaris-warehouse/sba/loans_lance/` via pyarrow.dataset + Arrow-bridge
to DuckDB, filters to PPP loan rows (program = 'ppp'), groups by
(legal_name_normalized, borrstate, borrzip) to derive one row per canonical
PPP borrower. Writes to Lance at
`s3://dex-raw-landing-zone/polaris-warehouse/sba/ppp_borrowers_lance/`.

PPP FOIA is a frozen dataset (SBA last refresh 2024-09-30). This is a
ONE-SHOT emit — no Modal app, no cron. Re-runs happen only when SBA publishes
a new PPP FOIA extract.

Predecessor: apps/data-engine-x/scripts/emit_sba_borrowers_lance.py
Deviations from predecessor:
  1. Source filter: WHERE program = 'ppp' (lowercase — verified literal).
     Added as pyarrow scanner filter AND in DuckDB WHERE clause for safety.
  2. Column renames: total_loans → total_ppp_loans,
                    total_gross_approval → total_ppp_approval.
  3. L54 fix: ARRAY_AGG(DISTINCT ...) → STRING_AGG(DISTINCT ..., '|') for
     franchise_brands_set, naics_codes_set, lender_set.
     The predecessor emitted LIST<VARCHAR> which violates L54 (apps/data-engine-x/CLAUDE.md:202).

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --project . python3 \\
    apps/data-engine-x/scripts/emit_ppp_borrowers_lance.py --apply

  doppler run --project hq-all --config prd -- \\
    uv run --project . python3 \\
    apps/data-engine-x/scripts/emit_ppp_borrowers_lance.py --dry-run

Directive: /Users/benjamincrane/Desktop/hq/directives/2026-05-20-ppp-borrowers-lance-emit.md
Cycle: ppp-borrowers-lance-emit (Cycle 1 of 6-cycle ppp-bridge batch)
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

from scripts._lib.address_normalize import (  # noqa: E402
    normalize_address_street,
    register_address_udf,
)
from scripts._lib.catalog_hooks import register_or_update_polaris  # noqa: E402
from scripts._lib.entity_name_normalize import (  # noqa: E402
    normalize_entity_name,
)
from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("emit_ppp_borrowers_lance")

LOANS_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sba/loans_lance/"
PPP_BORROWERS_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sba/ppp_borrowers_lance/"
DATASET_SLUG = "ppp_borrowers_lance"
TMP_DIR = "/tmp/lance"

# Row floor per directive §"Volume floors"
# Validator-calibrated: 10,177,716 distinct borrowers × 0.688 ≈ 7,000,000
MIN_ROW_FLOOR = 7_000_000

# Verified lowercase PPP program literal (validator-probed 2026-05-20).
# 'PPP' or 'Paycheck Protection Program' yields 0 rows.
PPP_PROGRAM_LITERAL = "ppp"


def _lance_storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _emit(dry_run: bool) -> int:
    import lance

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = TMP_DIR
    os.environ["LANCE_BYPASS_SPILLING"] = "true"

    logger.info("=" * 60)
    logger.info("emit_ppp_borrowers_lance")
    logger.info("input:  %s", LOANS_LANCE_URI)
    logger.info("output: %s", PPP_BORROWERS_LANCE_URI)
    logger.info("PPP program literal: '%s' (verified lowercase)", PPP_PROGRAM_LITERAL)
    logger.info("MIN_ROW_FLOOR: %d", MIN_ROW_FLOOR)

    storage_options = _lance_storage_options()

    # Arrow-bridge: open Lance dataset, scan needed columns, filter nulls + PPP.
    logger.info("opening loans_lance via pyarrow (Arrow-bridge pattern) ...")
    loans_ds = lance.dataset(LOANS_LANCE_URI, storage_options=storage_options)
    logger.info("loans_lance total rows: %d", loans_ds.count_rows())

    needed_cols = [
        "program",
        "legal_name_normalized", "borrname", "borrstate", "borrzip",
        "grossapproval", "approvaldate", "loanstatus", "bankname",
        "franchisename", "naicscode", "disbursementdate",
        "loan_id",
    ]
    # Filter at scanner level: borrname IS NOT NULL AND borrstate IS NOT NULL AND program = 'ppp'
    # Use & on Expression objects — pc.and_() is not registered in Substrait.
    import pyarrow.compute as pc
    scanner = loans_ds.scanner(
        columns=needed_cols,
        filter=(
            pc.field("borrname").is_valid()
            & pc.field("borrstate").is_valid()
            & (pc.field("program") == PPP_PROGRAM_LITERAL)
        ),
    )
    logger.info(
        "scanning loans: borrname IS NOT NULL AND borrstate IS NOT NULL AND program = '%s' ...",
        PPP_PROGRAM_LITERAL,
    )
    loans_arrow = scanner.to_table()
    logger.info("filtered PPP loan rows: %d (expected ~11,467,891)", len(loans_arrow))

    # DuckDB tuning
    import duckdb
    con = duckdb.connect()
    con.execute("SET threads = 2")
    con.execute("SET memory_limit = '8GB'")
    con.execute(f"SET temp_directory = '{TMP_DIR}'")
    con.execute("SET max_temp_directory_size = '120GB'")
    con.execute("SET preserve_insertion_order = false")
    con.register("loans_ppp", loans_arrow)

    if dry_run:
        # Estimate borrower count via a quick DuckDB group-by.
        est = con.execute(
            """SELECT COUNT(DISTINCT (legal_name_normalized, borrstate, borrzip))
               FROM loans_ppp
               WHERE legal_name_normalized IS NOT NULL
                 AND borrstate IS NOT NULL
                 AND program = 'ppp'"""
        ).fetchone()[0]
        logger.info(
            "DRY RUN — estimated PPP canonical borrowers: %d (floor=%d, pass=%s)",
            est, MIN_ROW_FLOOR, est >= MIN_ROW_FLOOR,
        )
        if est < MIN_ROW_FLOOR:
            logger.error("FAIL: estimated borrowers %d < floor %d", est, MIN_ROW_FLOOR)
            return 1
        return 0

    # Full derive via DuckDB GROUP BY
    logger.info("deriving PPP canonical borrowers via DuckDB group-by ...")
    # NOTE on L54 compliance:
    #   The predecessor emit_sba_borrowers_lance.py L138-143 uses ARRAY_AGG(DISTINCT ...)
    #   which produces LIST<VARCHAR> — a violation of L54 (apps/data-engine-x/CLAUDE.md:202).
    #   This emit DEVIATES from the predecessor for the three set columns:
    #     franchise_brands_set, naics_codes_set, lender_set
    #   using STRING_AGG(DISTINCT col, '|') instead.
    #   DuckDB STRING_AGG already skips NULL values — no FILTER needed.
    ppp_borrower_sql = """
    SELECT
        legal_name_normalized,
        MAX(borrname)                                           AS borrname_sample,
        borrstate,
        borrzip,
        COUNT(*)                                                AS total_ppp_loans,
        SUM(grossapproval)                                      AS total_ppp_approval,
        MAX(approvaldate)                                       AS max_approval_date,
        MIN(approvaldate)                                       AS min_approval_date,
        MAX(loanstatus)                                         AS latest_loanstatus,
        BOOL_OR(loanstatus = 'COMMIT')                          AS has_pending_commit,
        STRING_AGG(DISTINCT franchisename, '|')                 AS franchise_brands_set,
        STRING_AGG(DISTINCT naicscode, '|')                     AS naics_codes_set,
        STRING_AGG(DISTINCT bankname, '|')                      AS lender_set,
        MEDIAN(
            CASE WHEN disbursementdate IS NOT NULL AND approvaldate IS NOT NULL
                 THEN date_diff('day', approvaldate, disbursementdate)
            END
        )                                                       AS time_to_disburse_p50
    FROM loans_ppp
    WHERE legal_name_normalized IS NOT NULL
      AND borrstate IS NOT NULL
      AND program = 'ppp'
    GROUP BY legal_name_normalized, borrstate, borrzip
    """
    # .arrow() returns RecordBatchReader; .read_all() materializes to pyarrow.Table
    ppp_borrowers_arrow = con.execute(ppp_borrower_sql).arrow().read_all()
    logger.info("derived PPP borrowers: %d rows (expected ~10,177,716)", len(ppp_borrowers_arrow))

    # HARD FAIL before Lance write if below floor
    if len(ppp_borrowers_arrow) < MIN_ROW_FLOOR:
        logger.error(
            "FAIL: PPP borrowers=%d < floor=%d. Aborting before Lance write.",
            len(ppp_borrowers_arrow), MIN_ROW_FLOOR,
        )
        return 1

    # ------------------------------------------------------------------
    # Address side-scan (v1.1.0 augmentation)
    # ------------------------------------------------------------------
    # Read raw PPP parquets (which DO have borrower_address, dropped by the
    # schema-frozen loans_lance union). Normalize entity name + address with
    # the SAME rule the loans emit applies, then attach MODE (most common
    # normalized address) per (legal_name_normalized, borrstate, borrzip5).
    logger.info("side-scanning raw PPP parquets for borrower_address ...")
    register_address_udf(con, fn_name="py_normalize_address_street")
    try:
        con.create_function(
            "py_normalize_entity", normalize_entity_name, ["VARCHAR"], "VARCHAR",
            null_handling="special",
        )
    except Exception:
        try:
            con.remove_function("py_normalize_entity")
        except Exception:
            pass
        con.create_function(
            "py_normalize_entity", normalize_entity_name, ["VARCHAR"], "VARCHAR",
            null_handling="special",
        )
    con.execute("INSTALL httpfs; LOAD httpfs;")
    r2_endpoint = os.environ["R2_ENDPOINT"].replace("https://", "")
    con.execute(f"SET s3_endpoint='{r2_endpoint}';")
    con.execute("SET s3_url_style='path';")
    con.execute("SET s3_use_ssl=true;")
    con.execute(f"SET s3_access_key_id='{os.environ['R2_ACCESS_KEY_ID']}';")
    con.execute(f"SET s3_secret_access_key='{os.environ['R2_SECRET_ACCESS_KEY']}';")

    t_side = time.time()
    con.register("ppp_borrowers", ppp_borrowers_arrow)
    raw_glob_ppp = "s3://dex-raw-landing-zone/sba/program=ppp/segment=*/*.parquet"
    side_scan_sql = f"""
    WITH raw_ppp AS (
        SELECT
            borrower_name,
            borrower_state,
            COALESCE(borrower_zip5, borrower_zip) AS borrower_zip_raw,
            borrower_address
        FROM read_parquet('{raw_glob_ppp}', union_by_name=true, hive_partitioning=true)
        WHERE borrower_name    IS NOT NULL
          AND borrower_state   IS NOT NULL
          AND borrower_address IS NOT NULL
    ),
    normalized AS (
        SELECT
            py_normalize_entity(borrower_name)        AS legal_name_normalized,
            UPPER(TRIM(borrower_state))               AS borrstate_norm,
            LPAD(SUBSTR(TRIM(borrower_zip_raw), 1, 5), 5, '0') AS borrzip5,
            py_normalize_address_street(borrower_address) AS street_norm
        FROM raw_ppp
    ),
    counted AS (
        SELECT legal_name_normalized, borrstate_norm, borrzip5, street_norm,
               COUNT(*) AS n
        FROM normalized
        WHERE legal_name_normalized IS NOT NULL
          AND street_norm           IS NOT NULL
        GROUP BY 1,2,3,4
    ),
    mode_per_borrower AS (
        SELECT legal_name_normalized, borrstate_norm AS borrstate, borrzip5,
               ARG_MAX(street_norm, n) AS borrower_address_normalized
        FROM counted
        GROUP BY 1,2,3
    )
    SELECT
        b.*,
        m.borrower_address_normalized
    FROM ppp_borrowers b
    LEFT JOIN mode_per_borrower m
      ON  b.legal_name_normalized = m.legal_name_normalized
      AND b.borrstate             = m.borrstate
      AND LPAD(SUBSTR(TRIM(b.borrzip), 1, 5), 5, '0') = m.borrzip5
    """
    ppp_borrowers_arrow = con.execute(side_scan_sql).arrow().read_all()
    side_dur = time.time() - t_side
    import pyarrow.compute as pc_
    addr_col = ppp_borrowers_arrow.column("borrower_address_normalized")
    cov = pc_.sum(pc_.is_valid(addr_col)).as_py()
    logger.info(
        "borrower_address_normalized attached: %d / %d rows (%.1f%%) in %.1fs",
        cov, len(ppp_borrowers_arrow),
        100.0 * cov / max(1, len(ppp_borrowers_arrow)),
        side_dur,
    )

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        logger.info("writing PPP borrowers to Lance (mode=overwrite) ...")
        ds = lance.write_dataset(
            ppp_borrowers_arrow,
            PPP_BORROWERS_LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t0
        lance_count = ds.count_rows()
        logger.info(
            "wrote %d rows in %.1fs (version=%s)",
            lance_count, write_dur, ds.version,
        )

        logger.info("creating BTREE index on legal_name_normalized ...")
        t_idx = time.time()
        try:
            ds.create_scalar_index(
                "legal_name_normalized", index_type="BTREE", replace=True
            )
            logger.info("  BTREE built in %.1fs", time.time() - t_idx)
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

    # Polaris catalog lifecycle hook — fires after BTREE is confirmed built and
    # the commit lock has released. Idempotent; raises on API failure.
    register_or_update_polaris(
        namespace="sba",
        table_name="ppp_borrowers_lance",
        s3_uri=PPP_BORROWERS_LANCE_URI,
        docstring=(
            "SBA PPP canonical borrower spine (Pattern A direct-source "
            "hydration; grouped by legal_name_normalized + borrstate + "
            "borrzip; BTREE on legal_name_normalized)."
        ),
    )
    logger.info("polaris registration verified: sba.ppp_borrowers_lance")

    logger.info("=" * 60)
    logger.info("OK — PPP borrowers written: %d rows", lance_count)
    logger.info(
        "SUCCESS_THRESHOLD: rows_emitted=%d >= MIN_ROW_FLOOR=%d = %s",
        lance_count, MIN_ROW_FLOOR, lance_count >= MIN_ROW_FLOOR,
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Lance emit: SBA PPP borrowers (Pattern A, one-shot frozen dataset)"
    )
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true", help="Write Lance dataset to R2")
    grp.add_argument("--dry-run", action="store_true", help="Estimate row count, no write")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            logger.error("FAIL: %s not set", var)
            return 64

    return _emit(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())

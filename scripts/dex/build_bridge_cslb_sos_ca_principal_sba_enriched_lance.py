#!/usr/bin/env python3
"""CSLB x SoS x principal x SBA enriched cohort -- Pattern A enriched-cohort emit (Lance).

Two-hop join: cohort x SBA bridge x SBA borrowers (pre-aggregated).

INNER side: bridges.cslb_sos_ca_principal_lance (PR #483; 616,622 principal-grain rows).
Right side: bridges.sba_sos_ca_owner_lance (PR #464; identity-only -- sba_legal_name_normalized
  + sos_entity_num + bridge_run_id) INNER JOINED to sba.borrowers_lance (pre-aggregated per
  legal_name_normalized; carries total_loans, total_gross_approval, max/min_approval_date,
  latest_loanstatus), then GROUPED BY sos_entity_num for per-entity rollup; LEFT JOINED
  back to the cohort spine.

Aggregate at sos_entity_num to handle multi-borrower-per-entity cases:
  sba_loan_count = sum(total_loans), sba_total_gross_approval = sum(total_gross_approval),
  sba_max_approval_date = max(max_approval_date), sba_min_approval_date = min(min_approval_date),
  sba_borrower_count = count(distinct legal_name_normalized),
  sba_latest_loanstatus_set = list_distinct(list(latest_loanstatus)),
  sba_legal_names_matched = list_distinct(list(legal_name_normalized)),
  sba_sos_bridge_run_ids = list_distinct(list(br.bridge_run_id)).

Per Pattern A enriched-cohort (PR #469): NOT a new identity bridge.
NO ops.bridges row. YES ops.data_sources row (s2). YES per-row inherited + own
bridge_run_id provenance (three inherited renames + this emit's own UUID).

Run (apply):
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      uv run python scripts/build_bridge_cslb_sos_ca_principal_sba_enriched_lance.py --apply

Dry-run (print row count + coverage stats only):
    uv run python scripts/build_bridge_cslb_sos_ca_principal_sba_enriched_lance.py
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# sys.path.insert per PR #481 fix -- allows _lib imports from worktree root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._lib.lance_commit_lock import lance_commit_lock

# ---------------------------------------------------------------------------
# Constants (load-bearing -- verify-s1.sh greps must match exactly)
# ---------------------------------------------------------------------------

DATASET_SLUG = "cslb_sos_ca_principal_sba_enriched_lance"
BRIDGE_VERSION = "1.0.0"

# Validator probe: cohort = 616,622; LEFT JOIN preserves scale; floor at 500K
# gives ~19% headroom. Secondary SBA-coverage floor enforced in verify-s3.sh
# (probe: 208,226 rows have non-NULL sba_loan_count = 33.77% of cohort).
MIN_ROWS_MATCHED = 500_000

COHORT_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/cslb_sos_ca_principal_lance"
SBA_BRIDGE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_sos_ca_owner_lance"
SBA_BORROWERS_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sba/borrowers_lance"
OUTPUT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/cslb_sos_ca_principal_sba_enriched_lance"

TMP_DIR = "/tmp/lance"

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)


def _storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _ensure_db_url() -> None:
    """Normalize DEX_DB_URL_DIRECT from DATABASE_URL fallback if needed."""
    if "DEX_DB_URL_DIRECT" not in os.environ and "DATABASE_URL" in os.environ:
        os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="CSLB x SoS x principal x SBA enriched cohort -- Pattern A enriched-cohort emit (Lance)."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Write the Lance dataset. Without this flag runs in dry-run mode (row+coverage counts only).",
    )
    args = parser.parse_args()

    _ensure_db_url()
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("TMPDIR", TMP_DIR)
    os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")

    import duckdb
    import lance

    storage_options = _storage_options()

    # Per-emit provenance (own UUID separate from any inherited upstream IDs).
    BRIDGE_RUN_ID = str(uuid.uuid4())
    generated_at = datetime.now(timezone.utc)
    generated_at_iso = generated_at.isoformat()

    logger.info(
        "emit %s starting at %s (output=%s) apply=%s",
        BRIDGE_RUN_ID, generated_at_iso, OUTPUT_LANCE_URI, args.apply,
    )

    # ---- Step 1: PyLance scanners (three inputs) ---- #

    logger.info("opening %s ...", COHORT_URI)
    ds_cohort = lance.dataset(COHORT_URI, storage_options=storage_options)
    cohort_arrow = ds_cohort.scanner().to_table()  # all 24 cohort cols
    logger.info("  cohort: %d rows x %d cols", cohort_arrow.num_rows, len(cohort_arrow.column_names))

    logger.info("opening %s ...", SBA_BRIDGE_URI)
    ds_sba_bridge = lance.dataset(SBA_BRIDGE_URI, storage_options=storage_options)
    sba_bridge_arrow = ds_sba_bridge.scanner(
        columns=["sos_entity_num", "sba_legal_name_normalized", "bridge_run_id"],
    ).to_table()
    logger.info("  sba bridge (projected): %d rows", sba_bridge_arrow.num_rows)

    logger.info("opening %s ...", SBA_BORROWERS_URI)
    ds_sba_borrowers = lance.dataset(SBA_BORROWERS_URI, storage_options=storage_options)
    sba_borrowers_arrow = ds_sba_borrowers.scanner(
        columns=[
            "legal_name_normalized",
            "total_loans",
            "total_gross_approval",
            "max_approval_date",
            "min_approval_date",
            "latest_loanstatus",
        ],
    ).to_table()
    logger.info("  sba borrowers (projected): %d rows", sba_borrowers_arrow.num_rows)

    # ---- Step 2: DuckDB two-hop with rollup-then-LEFT-JOIN ---- #
    # P6: tune memory + spill settings -- 12M-row borrowers JOIN.

    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")
    con.register("co", cohort_arrow)
    con.register("br", sba_bridge_arrow)
    con.register("bw", sba_borrowers_arrow)

    # Step 2a: per-sos_entity_num rollup over the bridge x borrowers INNER JOIN.
    # Aggregate FIRST so the cohort spine sees one row per sos_entity_num on
    # the right side of the LEFT JOIN (P4: aggregate-before-join).
    # List columns serialized as pipe-delimited VARCHAR to avoid Lance
    # "Definition buffer size too large" encoding error on LIST<VARCHAR> columns
    # (Lance 1.5.x limitation with large list batches). ARRAY_AGG/list_distinct
    # still used internally; result is cast via array_to_string for Lance write (P3).
    con.execute(
        """
        CREATE TEMP TABLE sba_agg AS
        SELECT
            br.sos_entity_num,
            count(DISTINCT bw.legal_name_normalized)                                        AS sba_borrower_count,
            sum(bw.total_loans)                                                             AS sba_loan_count,
            sum(bw.total_gross_approval)                                                    AS sba_total_gross_approval,
            max(bw.max_approval_date)                                                       AS sba_max_approval_date,
            min(bw.min_approval_date)                                                       AS sba_min_approval_date,
            array_to_string(list_distinct(list(bw.latest_loanstatus)), '|')                 AS sba_latest_loanstatus_set,
            array_to_string(list_distinct(list(bw.legal_name_normalized)), '|')             AS sba_legal_names_matched,
            array_to_string(list_distinct(list(br.bridge_run_id)), '|')                     AS sba_sos_bridge_run_ids
        FROM br
        INNER JOIN bw
            ON br.sba_legal_name_normalized = bw.legal_name_normalized
        WHERE bw.total_loans IS NOT NULL
        GROUP BY br.sos_entity_num
        """
    )

    # Step 2b: cohort spine x LEFT JOIN sba_agg.
    # P1: rename cohort's bridge_run_id -> cslb_principal_bridge_run_id;
    # this emit's own UUID lands as bridge_run_id. EXCLUDE the cohort's
    # bridge_version + generated_at (this emit re-stamps them).
    con.execute(
        f"""
        CREATE TEMP TABLE enriched AS
        SELECT
            co.* EXCLUDE (bridge_run_id, bridge_version, generated_at),
            co.bridge_run_id                                      AS cslb_principal_bridge_run_id,
            sa.sba_borrower_count,
            sa.sba_loan_count,
            sa.sba_total_gross_approval,
            sa.sba_max_approval_date,
            sa.sba_min_approval_date,
            sa.sba_latest_loanstatus_set,
            sa.sba_legal_names_matched,
            sa.sba_sos_bridge_run_ids,
            '{BRIDGE_RUN_ID}'                                     AS bridge_run_id,
            '{BRIDGE_VERSION}'                                    AS bridge_version,
            TIMESTAMP '{generated_at_iso}'                        AS generated_at
        FROM co
        LEFT JOIN sba_agg sa ON co.sos_entity_num = sa.sos_entity_num
        """
    )

    rows_out = con.execute("SELECT count(*) FROM enriched").fetchone()[0]
    rows_with_sba = con.execute(
        "SELECT count(*) FROM enriched WHERE sba_loan_count IS NOT NULL"
    ).fetchone()[0]
    coverage_pct = (rows_with_sba / rows_out * 100) if rows_out else 0.0
    sum_loans = con.execute(
        "SELECT coalesce(sum(sba_loan_count), 0) FROM enriched"
    ).fetchone()[0]
    sum_dollars = con.execute(
        "SELECT coalesce(sum(sba_total_gross_approval), 0) FROM enriched"
    ).fetchone()[0]

    logger.info(
        "enriched: %d rows (%d with SBA = %.2f%% coverage; sum_loans=%s sum_dollars=$%s)",
        rows_out, rows_with_sba, coverage_pct, sum_loans, sum_dollars,
    )

    if rows_out < MIN_ROWS_MATCHED:
        logger.error("HARD FAIL: rows=%d < floor=%d", rows_out, MIN_ROWS_MATCHED)
        return 1

    if not args.apply:
        logger.info(
            "DRY-RUN: would write %d rows (%d enriched / %d NULL-SBA = %.2f%% coverage). Pass --apply to write.",
            rows_out, rows_with_sba, rows_out - rows_with_sba, coverage_pct,
        )
        return 0

    # ---- Step 3: Lance write inside commit lock + dual BTREE ---- #

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        logger.info("writing Lance dataset to %s ...", OUTPUT_LANCE_URI)
        reader = con.from_query("SELECT * FROM enriched").to_arrow_reader(
            batch_size=100_000,
        )
        ds = lance.write_dataset(
            reader,
            OUTPUT_LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t0
        lance_count = ds.count_rows()
        logger.info(
            "wrote %d rows in %.1fs (version=%s)", lance_count, write_dur, ds.version,
        )

        try:
            ds.create_scalar_index("cslb_license_no", index_type="BTREE", replace=True)
            logger.info("BTREE on cslb_license_no: OK")
        except Exception as e:
            logger.error("BTREE on cslb_license_no FAILED: %s", e)
            raise
        try:
            ds.create_scalar_index("sos_entity_num", index_type="BTREE", replace=True)
            logger.info("BTREE on sos_entity_num: OK")
        except Exception as e:
            logger.error("BTREE on sos_entity_num FAILED: %s", e)
            raise

        try:
            ds.optimize.compact_files()
        except Exception as e:
            logger.warning("compact_files failed (non-fatal): %s", e)
        try:
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as e:
            logger.warning("cleanup_old_versions failed (non-fatal): %s", e)

    logger.info(
        "OK: bridges.cslb_sos_ca_principal_sba_enriched_lance written (%d rows; enriched=%d/%.2f%%; sum_loans=%s; sum_dollars=$%s; bridge_run_id=%s)",
        lance_count, rows_with_sba, coverage_pct, sum_loans, sum_dollars, BRIDGE_RUN_ID,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

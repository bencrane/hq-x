#!/usr/bin/env python3
"""SBA x NY construction enriched cohort -- Pattern A enriched-cohort emit (Lance).

Single-hop rollup: SBA-borrower spine + LEFT JOIN vendor_payments-aggregate per
vendor_name_normalized. Spine comes from the SBA × NY contracts bridge
(bridges.sba_ny_contracts_lance, 30,409 matched rows / 4,687 distinct SBA
borrowers); we aggregate that bridge per `sba_legal_name_normalized` to land
contract-level metrics (total $, construction-tagged $, distinct authority
count, etc.), then LEFT JOIN vendor_payments aggregates keyed on
`vendor_name_normalized = sba_legal_name_normalized` to overlay Design+Construction
Capital Project payment-level metrics (from rb9h-9fit).

INPUTS:
  bridges.sba_ny_contracts_lance (Pattern B; sba_legal_name_normalized,
    contract_id, ny_authority_name, ny_type_of_procurement, ny_contract_amount,
    ny_award_date, ny_end_date, ny_vendor_name, sba_fan_out, ny_fan_out,
    confidence_tier, bridge_run_id [renamed sba_ny_contracts_bridge_run_id]).
  nystate.vendor_payments_lance (Pattern A; vendor_name_normalized,
    paymentamount, contractnumber, typeofservice, fiscalyear, county).

OUTPUT:
  bridges.sba_ny_construction_enriched_lance — one row per SBA borrower; BTREE
  on sba_legal_name_normalized.

Per Pattern A enriched-cohort (PR #469 / PR #484): NOT a new identity bridge.
NO ops.bridges row. NO ops.match_methods registration. YES ops.data_sources
row (s2). YES per-row inherited + own bridge_run_id provenance (one inherited
rename + this emit's own UUID).

Multi-value columns serialized as pipe-delimited VARCHAR per L54 (avoids the
Lance 1.5.x LIST<VARCHAR> definition-buffer cap).
VARCHAR-typed numeric/date fields wrapped in TRY_CAST per L49.

Run (apply):
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      uv run python scripts/build_bridge_sba_ny_construction_enriched_lance.py --apply

Dry-run (print row count + coverage stats only):
    uv run python scripts/build_bridge_sba_ny_construction_enriched_lance.py
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

from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402

# ---------------------------------------------------------------------------
# Constants (load-bearing -- harness greps must match exactly)
# ---------------------------------------------------------------------------

DATASET_SLUG = "sba_ny_construction_enriched_lance"
BRIDGE_VERSION = "1.0.0"

# Validator floor: 4_000 (~85% of expected 4,687 distinct SBA borrowers in
# the underlying bridge). Catches normalizer regression / join-key drift /
# bridge-source-column shape changes. Mirrors PR #484's 500K/616K = 81%
# floor ratio.
MIN_ROWS_MATCHED = 4_000

BRIDGE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_ny_contracts_lance"
)
VENDOR_PAYMENTS_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/nystate/vendor_payments_lance"
)
OUTPUT_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_ny_construction_enriched_lance"
)

CONSTRUCTION_TYPE = "Design and Construction/Maintenance"
CONSTRUCTION_SERVICE = "Construction"

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


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "SBA x NY construction enriched cohort -- "
            "Pattern A enriched-cohort emit (Lance)."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help=(
            "Write the Lance dataset. Without this flag runs in dry-run mode "
            "(row + coverage counts only)."
        ),
    )
    args = parser.parse_args()

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

    # ---- Step 1: PyLance scanners (two inputs) ---- #

    logger.info("opening %s ...", BRIDGE_URI)
    ds_bridge = lance.dataset(BRIDGE_URI, storage_options=storage_options)
    bridge_arrow = ds_bridge.scanner(
        columns=[
            "sba_legal_name_normalized",
            "vendor_name_normalized",
            "contract_id",
            "ny_vendor_name",
            "ny_authority_name",
            "ny_type_of_procurement",
            "ny_contract_amount",
            "ny_award_date",
            "ny_end_date",
            "confidence_tier",
            "bridge_run_id",
        ],
    ).to_table()
    logger.info(
        "  bridge: %d rows x %d cols",
        bridge_arrow.num_rows, len(bridge_arrow.column_names),
    )

    logger.info("opening %s ...", VENDOR_PAYMENTS_URI)
    ds_vp = lance.dataset(VENDOR_PAYMENTS_URI, storage_options=storage_options)
    vp_arrow = ds_vp.scanner(
        columns=[
            "vendor_name_normalized",
            "paymentamount",
            "contractnumber",
            "typeofservice",
            "county",
            "fiscalyear",
        ],
    ).to_table()
    logger.info("  vendor_payments: %d rows", vp_arrow.num_rows)

    # ---- Step 2: DuckDB rollup-then-LEFT-JOIN ---- #

    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")
    con.register("br", bridge_arrow)
    con.register("vp", vp_arrow)

    # Step 2a: aggregate bridge rows per SBA borrower.
    # All multi-value list columns emitted as pipe-delimited VARCHAR per L54.
    # All VARCHAR-typed numeric / date fields wrapped in TRY_CAST per L49.
    logger.info("aggregating bridge per sba_legal_name_normalized ...")
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_agg AS
        SELECT
            br.sba_legal_name_normalized,
            MAX(br.ny_vendor_name)                                                                            AS representative_vendor_name,
            COUNT(*)                                                                                          AS ny_contract_count,
            COUNT(*) FILTER (WHERE br.ny_type_of_procurement = '{CONSTRUCTION_TYPE}')                          AS ny_construction_contract_count,
            COUNT(DISTINCT br.ny_authority_name)                                                              AS ny_distinct_authority_count,
            array_to_string(list_distinct(list(br.ny_authority_name)), '|')                                   AS ny_distinct_authorities,
            array_to_string(list_distinct(list(br.ny_type_of_procurement)), '|')                              AS ny_distinct_procurement_types,
            SUM(TRY_CAST(br.ny_contract_amount AS DOUBLE))                                                    AS ny_total_contract_amount,
            SUM(TRY_CAST(br.ny_contract_amount AS DOUBLE)) FILTER (WHERE br.ny_type_of_procurement = '{CONSTRUCTION_TYPE}') AS ny_construction_contract_amount,
            MAX(TRY_CAST(br.ny_contract_amount AS DOUBLE))                                                    AS ny_max_single_contract_amount,
            MIN(TRY_CAST(br.ny_award_date AS DATE))                                                           AS ny_earliest_award_date,
            MAX(TRY_CAST(br.ny_award_date AS DATE))                                                           AS ny_latest_award_date,
            array_to_string(list_distinct(list(br.bridge_run_id)), '|')                                       AS sba_ny_contracts_bridge_run_ids
        FROM br
        WHERE br.sba_legal_name_normalized IS NOT NULL
        GROUP BY br.sba_legal_name_normalized
        """
    )

    rows_spine = con.execute("SELECT COUNT(*) FROM bridge_agg").fetchone()[0]
    logger.info("  spine (distinct SBA borrowers): %d rows", rows_spine)

    # Step 2b: aggregate vendor_payments per vendor_name_normalized.
    logger.info("aggregating vendor_payments per vendor_name_normalized ...")
    con.execute(
        f"""
        CREATE TEMP TABLE vp_agg AS
        SELECT
            vp.vendor_name_normalized,
            COUNT(*)                                                                              AS vendor_payment_count,
            SUM(TRY_CAST(vp.paymentamount AS DOUBLE))                                             AS vendor_payment_total_amount,
            SUM(TRY_CAST(vp.paymentamount AS DOUBLE)) FILTER (WHERE vp.typeofservice = '{CONSTRUCTION_SERVICE}') AS vendor_payment_construction_only_total,
            COUNT(DISTINCT vp.contractnumber)                                                     AS vendor_payment_distinct_contractnumber_count,
            array_to_string(list_distinct(list(vp.county)), '|')                                  AS vendor_payment_distinct_counties,
            MAX(TRY_CAST(vp.paymentamount AS DOUBLE))                                             AS vendor_payment_max_single_amount
        FROM vp
        WHERE vp.vendor_name_normalized IS NOT NULL
        GROUP BY vp.vendor_name_normalized
        """
    )

    rows_vp_agg = con.execute("SELECT COUNT(*) FROM vp_agg").fetchone()[0]
    logger.info("  vp_agg (distinct vendors with payments): %d rows", rows_vp_agg)

    # Step 2c: spine LEFT JOIN vp_agg, stamping per-emit provenance.
    logger.info("LEFT JOIN spine x vp_agg + provenance stamping ...")
    con.execute(
        f"""
        CREATE TEMP TABLE enriched AS
        SELECT
            ba.sba_legal_name_normalized,
            ba.representative_vendor_name,
            ba.ny_contract_count,
            ba.ny_construction_contract_count,
            ba.ny_distinct_authority_count,
            ba.ny_distinct_authorities,
            ba.ny_distinct_procurement_types,
            ba.ny_total_contract_amount,
            ba.ny_construction_contract_amount,
            ba.ny_max_single_contract_amount,
            ba.ny_earliest_award_date,
            ba.ny_latest_award_date,
            va.vendor_payment_count,
            va.vendor_payment_total_amount,
            va.vendor_payment_construction_only_total,
            va.vendor_payment_distinct_contractnumber_count,
            va.vendor_payment_distinct_counties,
            va.vendor_payment_max_single_amount,
            ba.sba_ny_contracts_bridge_run_ids,
            '{BRIDGE_RUN_ID}'                       AS bridge_run_id,
            '{BRIDGE_VERSION}'                      AS bridge_version,
            TIMESTAMP '{generated_at_iso}'          AS generated_at
        FROM bridge_agg ba
        LEFT JOIN vp_agg va
            ON ba.sba_legal_name_normalized = va.vendor_name_normalized
        """
    )

    rows_out = con.execute("SELECT COUNT(*) FROM enriched").fetchone()[0]
    rows_with_vp = con.execute(
        "SELECT COUNT(*) FROM enriched WHERE vendor_payment_count IS NOT NULL"
    ).fetchone()[0]
    rows_with_construction = con.execute(
        "SELECT COUNT(*) FROM enriched WHERE ny_construction_contract_count > 0"
    ).fetchone()[0]
    coverage_vp_pct = (rows_with_vp / rows_out * 100) if rows_out else 0.0
    coverage_construction_pct = (rows_with_construction / rows_out * 100) if rows_out else 0.0
    sum_contract_amount = con.execute(
        "SELECT COALESCE(SUM(ny_total_contract_amount), 0) FROM enriched"
    ).fetchone()[0]
    sum_construction_amount = con.execute(
        "SELECT COALESCE(SUM(ny_construction_contract_amount), 0) FROM enriched"
    ).fetchone()[0]
    sum_vp_amount = con.execute(
        "SELECT COALESCE(SUM(vendor_payment_total_amount), 0) FROM enriched"
    ).fetchone()[0]

    logger.info(
        "enriched: %d rows  (%d with vendor_payment_count = %.2f%%; "
        "%d with construction = %.2f%%; sum_contract=$%s sum_construction=$%s sum_vp=$%s)",
        rows_out, rows_with_vp, coverage_vp_pct,
        rows_with_construction, coverage_construction_pct,
        sum_contract_amount, sum_construction_amount, sum_vp_amount,
    )

    if rows_out < MIN_ROWS_MATCHED:
        logger.error(
            "HARD FAIL: rows=%d < floor=%d",
            rows_out, MIN_ROWS_MATCHED,
        )
        return 1

    if not args.apply:
        logger.info(
            "DRY-RUN: would write %d rows  (vp coverage=%.2f%%; "
            "construction coverage=%.2f%%). Pass --apply to write.",
            rows_out, coverage_vp_pct, coverage_construction_pct,
        )
        return 0

    # ---- Step 3: Lance write inside commit lock + BTREE ---- #

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
            "wrote %d rows in %.1fs (version=%s)",
            lance_count, write_dur, ds.version,
        )

        try:
            ds.create_scalar_index(
                "sba_legal_name_normalized", index_type="BTREE", replace=True,
            )
            logger.info("BTREE on sba_legal_name_normalized: OK")
        except Exception as e:
            logger.error("BTREE on sba_legal_name_normalized FAILED: %s", e)
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
        "OK: bridges.sba_ny_construction_enriched_lance written "
        "(%d rows; vp_coverage=%.2f%%; construction_coverage=%.2f%%; "
        "sum_contract=$%s; sum_construction=$%s; sum_vp=$%s; bridge_run_id=%s)",
        lance_count, coverage_vp_pct, coverage_construction_pct,
        sum_contract_amount, sum_construction_amount, sum_vp_amount,
        BRIDGE_RUN_ID,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""SBA × NY construction × USAspending × SAM enriched cohort — Pattern A enriched-cohort emit (Lance).

Mirror of PR #485 `cslb_sos_ca_principal_usaspending_enriched_lance` shape,
extended to consume BOTH the SBA × USAspending Pattern B bridge AND the
SBA × SAM Pattern B bridge.

Three-input emit:
  COHORT spine    : bridges.sba_ny_construction_enriched_lance (4,687 rows,
                    SBA-borrower grain, 22 existing cols).
  USAspending hop : bridges.sba_ny_usaspending_lance × usaspending.recipient_grain_lance
                    INNER JOIN ON recipient_uei, GROUPED BY sba_legal_name_normalized
                    for per-borrower rollup.
  SAM hop         : bridges.sba_ny_sam_lance aggregated per sba_legal_name_normalized
                    (no further upstream join; SAM data is per-entity on the bridge).

Cohort spine LEFT JOINs both aggregate blocks.

Per Pattern A enriched-cohort (PR #469 / PR #484 / PR #485): identity-bridge
registry calls are ABSENT (no ops.bridges row, no match-method-registry
imports). YES ops.data_sources row (s4 migration). YES per-row inherited +
own bridge_run_id provenance (cohort spine bridge_run_id renamed +
USAspending bridge_run_ids list + SAM bridge_run_ids list + this emit's own UUID).

Aggregate columns (USAspending) — mirror PR #485 exactly:
  usaspending_recipient_count, usaspending_total_obligation_{30,90,180,365}d (4),
  usaspending_contract_count_{30,90,180,365}d (4), usaspending_max_contract_date,
  usaspending_earliest_contract_date_365d, usaspending_top_psc_set (pipe-delim
  per L54), usaspending_recipient_uei_set (pipe-delim), 15 BOOL_OR diversity
  flags (usaspending_is_{8a,hubzone,wosb,edwosb,sdvosb,vosb,sdb,
  minority_owned,native_american_owned,alaskan_native_corp,native_hawaiian_org,
  tribal_corp,nonprofit,educational,jv}_any), sba_ny_usaspending_bridge_run_ids
  (pipe-delim).

Aggregate columns (SAM):
  sam_match_count, sam_distinct_uei_count, sam_uei_normalized_list (pipe-delim),
  sam_cage_code_list (pipe-delim), sam_naics_primary_2digit_list (pipe-delim),
  sam_physical_address_zip5_list (pipe-delim), sam_latest_archive_date,
  sba_ny_sam_bridge_run_ids (pipe-delim).

L49 TRY_CAST applied to USAspending recipient_grain numeric/date aggregates
(belt-and-suspenders even though recipient_grain_lance schema is typed).

L54 pipe-delimited VARCHAR for every aggregated multi-value column.

Run (apply):
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      uv run python scripts/build_bridge_sba_ny_construction_usaspending_sam_enriched_lance.py --apply

Dry-run (print row count + coverage stats only):
    uv run python scripts/build_bridge_sba_ny_construction_usaspending_sam_enriched_lance.py
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

# sys.path.insert per PR #481 fix.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._lib.lance_commit_lock import lance_commit_lock

# ---------------------------------------------------------------------------
# Constants (load-bearing — match harness greps)
# ---------------------------------------------------------------------------

DATASET_SLUG = "sba_ny_construction_usaspending_sam_enriched_lance"
BRIDGE_VERSION = "1.0.0"

# Spine = bridges.sba_ny_construction_enriched_lance (4,687 rows). LEFT JOINs
# preserve scale; floor at 4,000 gives ~15% headroom (mirrors the upstream
# spine's own floor).
MIN_ROWS_MATCHED = 4_000

# Secondary floors guard against silent regression in the two cross-source
# chains. Probe (2026-05-18):
#   USAspending: 462 SBA NY borrowers matched USAspending NY recipients;
#                fewer chain through to recipient_grain (UEI alignment loss);
#                expect ~300-400 enriched rows; floor at 100 catches catastrophic
#                two-hop regression with conservative headroom.
#   SAM:         1,284 SBA NY borrowers matched SAM NY entities; aggregate
#                preserves spine scale on bridge side; floor at 800 catches
#                regression while accommodating the platinum/gold/silver
#                tier-filter loss.
MIN_USASPENDING_ENRICHED_FLOOR = 100
MIN_SAM_ENRICHED_FLOOR = 800

COHORT_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_ny_construction_enriched_lance"
)
USPENDING_BRIDGE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_ny_usaspending_lance"
)
USPENDING_RECIPIENT_GRAIN_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/recipient_grain_lance"
)
SAM_BRIDGE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_ny_sam_lance"
)
OUTPUT_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_ny_construction_usaspending_sam_enriched_lance"
)

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
    if "DEX_DB_URL_DIRECT" not in os.environ and "DATABASE_URL" in os.environ:
        os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "SBA × NY construction × USAspending × SAM enriched cohort — "
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

    # ---- Step 1: PyLance scanners (four inputs) ---- #

    logger.info("opening %s ...", COHORT_URI)
    ds_cohort = lance.dataset(COHORT_URI, storage_options=storage_options)
    cohort_arrow = ds_cohort.scanner().to_table()
    logger.info(
        "  cohort: %d rows x %d cols",
        cohort_arrow.num_rows, len(cohort_arrow.column_names),
    )

    logger.info("opening %s ...", USPENDING_BRIDGE_URI)
    ds_us_bridge = lance.dataset(USPENDING_BRIDGE_URI, storage_options=storage_options)
    us_bridge_arrow = ds_us_bridge.scanner(
        columns=["sba_legal_name_normalized", "recipient_uei", "bridge_run_id"],
    ).to_table()
    logger.info("  usaspending bridge (projected): %d rows", us_bridge_arrow.num_rows)

    logger.info("opening %s ...", USPENDING_RECIPIENT_GRAIN_URI)
    ds_rg = lance.dataset(USPENDING_RECIPIENT_GRAIN_URI, storage_options=storage_options)
    # Mirror PR #485 column list — 26 cols from upstream recipient_grain schema.
    rg_arrow = ds_rg.scanner(
        columns=[
            "recipient_uei",
            # Window totals (double)
            "total_obligation_30d", "total_obligation_90d",
            "total_obligation_180d", "total_obligation_365d",
            # Window counts (int64)
            "contract_count_30d", "contract_count_90d",
            "contract_count_180d", "contract_count_365d",
            # Dates (date32)
            "latest_contract_date", "earliest_contract_date_365d",
            # Single-valued string
            "top_psc",
            # 15 diversity-flag booleans
            "is_8a", "is_hubzone", "is_wosb", "is_edwosb",
            "is_sdvosb", "is_vosb", "is_sdb",
            "is_minority_owned", "is_native_american_owned",
            "is_alaskan_native_corp", "is_native_hawaiian_org",
            "is_tribal_corp", "is_nonprofit", "is_educational", "is_jv",
        ],
    ).to_table()
    logger.info("  usaspending recipient_grain (projected): %d rows", rg_arrow.num_rows)

    logger.info("opening %s ...", SAM_BRIDGE_URI)
    ds_sam_bridge = lance.dataset(SAM_BRIDGE_URI, storage_options=storage_options)
    sam_bridge_arrow = ds_sam_bridge.scanner(
        columns=[
            "sba_legal_name_normalized",
            "sam_uei_normalized",
            "sam_cage_code_normalized",
            "sam_naics_primary_2digit",
            "sam_physical_address_zip5",
            "sam_archive_date",
            "bridge_run_id",
        ],
    ).to_table()
    logger.info("  sam bridge (projected): %d rows", sam_bridge_arrow.num_rows)

    # ---- Step 2: DuckDB rollup-then-LEFT-JOIN ---- #

    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")
    con.register("co", cohort_arrow)
    con.register("ubr", us_bridge_arrow)
    con.register("rg", rg_arrow)
    con.register("sbr", sam_bridge_arrow)

    # Step 2a: per-sba_legal_name_normalized rollup over USAspending bridge x
    # recipient_grain INNER JOIN. Mirror PR #485 line 198-237 structure.
    # L49 TRY_CAST belt-and-suspenders on numeric/date aggregates.
    # L54 pipe-delim VARCHAR for multi-value list outputs.
    logger.info("aggregating USAspending bridge x recipient_grain per sba_legal_name_normalized ...")
    con.execute(
        """
        CREATE TEMP TABLE usaspending_agg AS
        SELECT
            ubr.sba_legal_name_normalized,
            count(DISTINCT ubr.recipient_uei)                                              AS usaspending_recipient_count,
            sum(TRY_CAST(rg.total_obligation_30d AS DOUBLE))                               AS usaspending_total_obligation_30d,
            sum(TRY_CAST(rg.total_obligation_90d AS DOUBLE))                               AS usaspending_total_obligation_90d,
            sum(TRY_CAST(rg.total_obligation_180d AS DOUBLE))                              AS usaspending_total_obligation_180d,
            sum(TRY_CAST(rg.total_obligation_365d AS DOUBLE))                              AS usaspending_total_obligation_365d,
            sum(TRY_CAST(rg.contract_count_30d AS BIGINT))                                 AS usaspending_contract_count_30d,
            sum(TRY_CAST(rg.contract_count_90d AS BIGINT))                                 AS usaspending_contract_count_90d,
            sum(TRY_CAST(rg.contract_count_180d AS BIGINT))                                AS usaspending_contract_count_180d,
            sum(TRY_CAST(rg.contract_count_365d AS BIGINT))                                AS usaspending_contract_count_365d,
            max(TRY_CAST(rg.latest_contract_date AS DATE))                                 AS usaspending_max_contract_date,
            min(TRY_CAST(rg.earliest_contract_date_365d AS DATE))                          AS usaspending_earliest_contract_date_365d,
            array_to_string(list_distinct(list(rg.top_psc)), '|')                          AS usaspending_top_psc_set,
            array_to_string(list_distinct(list(ubr.recipient_uei)), '|')                   AS usaspending_recipient_uei_set,
            BOOL_OR(rg.is_8a)                                                              AS usaspending_is_8a_any,
            BOOL_OR(rg.is_hubzone)                                                         AS usaspending_is_hubzone_any,
            BOOL_OR(rg.is_wosb)                                                            AS usaspending_is_wosb_any,
            BOOL_OR(rg.is_edwosb)                                                          AS usaspending_is_edwosb_any,
            BOOL_OR(rg.is_sdvosb)                                                          AS usaspending_is_sdvosb_any,
            BOOL_OR(rg.is_vosb)                                                            AS usaspending_is_vosb_any,
            BOOL_OR(rg.is_sdb)                                                             AS usaspending_is_sdb_any,
            BOOL_OR(rg.is_minority_owned)                                                  AS usaspending_is_minority_owned_any,
            BOOL_OR(rg.is_native_american_owned)                                           AS usaspending_is_native_american_owned_any,
            BOOL_OR(rg.is_alaskan_native_corp)                                             AS usaspending_is_alaskan_native_corp_any,
            BOOL_OR(rg.is_native_hawaiian_org)                                             AS usaspending_is_native_hawaiian_org_any,
            BOOL_OR(rg.is_tribal_corp)                                                     AS usaspending_is_tribal_corp_any,
            BOOL_OR(rg.is_nonprofit)                                                       AS usaspending_is_nonprofit_any,
            BOOL_OR(rg.is_educational)                                                     AS usaspending_is_educational_any,
            BOOL_OR(rg.is_jv)                                                              AS usaspending_is_jv_any,
            array_to_string(list_distinct(list(ubr.bridge_run_id)), '|')                   AS sba_ny_usaspending_bridge_run_ids
        FROM ubr
        INNER JOIN rg ON ubr.recipient_uei = rg.recipient_uei
        WHERE rg.recipient_uei IS NOT NULL
        GROUP BY ubr.sba_legal_name_normalized
        """
    )

    rows_us_agg = con.execute("SELECT count(*) FROM usaspending_agg").fetchone()[0]
    logger.info("  usaspending_agg: %d rows", rows_us_agg)

    # Step 2b: per-sba_legal_name_normalized rollup over SAM bridge (single-hop
    # — SAM data lives on the bridge row, no further upstream join needed).
    logger.info("aggregating SAM bridge per sba_legal_name_normalized ...")
    con.execute(
        """
        CREATE TEMP TABLE sam_agg AS
        SELECT
            sbr.sba_legal_name_normalized,
            count(*)                                                                       AS sam_match_count,
            count(DISTINCT sbr.sam_uei_normalized)                                         AS sam_distinct_uei_count,
            array_to_string(list_distinct(list(sbr.sam_uei_normalized)), '|')              AS sam_uei_normalized_list,
            array_to_string(list_distinct(list(sbr.sam_cage_code_normalized)), '|')        AS sam_cage_code_list,
            array_to_string(list_distinct(list(sbr.sam_naics_primary_2digit)), '|')        AS sam_naics_primary_2digit_list,
            array_to_string(list_distinct(list(sbr.sam_physical_address_zip5)), '|')       AS sam_physical_address_zip5_list,
            max(TRY_CAST(sbr.sam_archive_date AS DATE))                                    AS sam_latest_archive_date,
            array_to_string(list_distinct(list(sbr.bridge_run_id)), '|')                   AS sba_ny_sam_bridge_run_ids
        FROM sbr
        GROUP BY sbr.sba_legal_name_normalized
        """
    )

    rows_sam_agg = con.execute("SELECT count(*) FROM sam_agg").fetchone()[0]
    logger.info("  sam_agg: %d rows", rows_sam_agg)

    # Step 2c: cohort spine x LEFT JOIN both aggregate blocks.
    # Per L17 provenance: rename cohort's bridge_run_id ->
    # sba_ny_construction_enriched_bridge_run_id; cohort's
    # sba_ny_contracts_bridge_run_ids propagates verbatim; USAspending and SAM
    # bridge_run_ids land as pipe-delimited list_distinct cols (L54); this
    # emit's own UUID stamps as bridge_run_id. EXCLUDE cohort's bridge_version
    # + generated_at (this emit re-stamps them).
    logger.info("LEFT JOIN cohort spine x usaspending_agg x sam_agg + provenance stamping ...")
    con.execute(
        f"""
        CREATE TEMP TABLE enriched AS
        SELECT
            co.* EXCLUDE (bridge_run_id, bridge_version, generated_at),
            co.bridge_run_id                                       AS sba_ny_construction_enriched_bridge_run_id,
            -- USAspending block
            ua.usaspending_recipient_count,
            ua.usaspending_total_obligation_30d,
            ua.usaspending_total_obligation_90d,
            ua.usaspending_total_obligation_180d,
            ua.usaspending_total_obligation_365d,
            ua.usaspending_contract_count_30d,
            ua.usaspending_contract_count_90d,
            ua.usaspending_contract_count_180d,
            ua.usaspending_contract_count_365d,
            ua.usaspending_max_contract_date,
            ua.usaspending_earliest_contract_date_365d,
            ua.usaspending_top_psc_set,
            ua.usaspending_recipient_uei_set,
            ua.usaspending_is_8a_any,
            ua.usaspending_is_hubzone_any,
            ua.usaspending_is_wosb_any,
            ua.usaspending_is_edwosb_any,
            ua.usaspending_is_sdvosb_any,
            ua.usaspending_is_vosb_any,
            ua.usaspending_is_sdb_any,
            ua.usaspending_is_minority_owned_any,
            ua.usaspending_is_native_american_owned_any,
            ua.usaspending_is_alaskan_native_corp_any,
            ua.usaspending_is_native_hawaiian_org_any,
            ua.usaspending_is_tribal_corp_any,
            ua.usaspending_is_nonprofit_any,
            ua.usaspending_is_educational_any,
            ua.usaspending_is_jv_any,
            ua.sba_ny_usaspending_bridge_run_ids,
            -- SAM block
            sa.sam_match_count,
            sa.sam_distinct_uei_count,
            sa.sam_uei_normalized_list,
            sa.sam_cage_code_list,
            sa.sam_naics_primary_2digit_list,
            sa.sam_physical_address_zip5_list,
            sa.sam_latest_archive_date,
            sa.sba_ny_sam_bridge_run_ids,
            -- This emit's own provenance
            '{BRIDGE_RUN_ID}'                                      AS bridge_run_id,
            '{BRIDGE_VERSION}'                                     AS bridge_version,
            TIMESTAMP '{generated_at_iso}'                         AS generated_at
        FROM co
        LEFT JOIN usaspending_agg ua ON co.sba_legal_name_normalized = ua.sba_legal_name_normalized
        LEFT JOIN sam_agg         sa ON co.sba_legal_name_normalized = sa.sba_legal_name_normalized
        """
    )

    rows_out = con.execute("SELECT count(*) FROM enriched").fetchone()[0]
    rows_with_us = con.execute(
        "SELECT count(*) FROM enriched WHERE usaspending_recipient_count IS NOT NULL"
    ).fetchone()[0]
    rows_with_sam = con.execute(
        "SELECT count(*) FROM enriched WHERE sam_match_count IS NOT NULL"
    ).fetchone()[0]
    coverage_us_pct = (rows_with_us / rows_out * 100) if rows_out else 0.0
    coverage_sam_pct = (rows_with_sam / rows_out * 100) if rows_out else 0.0
    sum_obligation_365d = con.execute(
        "SELECT coalesce(sum(usaspending_total_obligation_365d), 0) FROM enriched"
    ).fetchone()[0]
    sum_contracts_365d = con.execute(
        "SELECT coalesce(sum(usaspending_contract_count_365d), 0) FROM enriched"
    ).fetchone()[0]
    sum_sam_uei = con.execute(
        "SELECT coalesce(sum(sam_distinct_uei_count), 0) FROM enriched"
    ).fetchone()[0]

    logger.info(
        "enriched: %d rows (%d with USAspending = %.2f%%; %d with SAM = %.2f%%; "
        "sum_us_obligation_365d=$%s sum_us_contracts_365d=%s sum_sam_uei=%s)",
        rows_out, rows_with_us, coverage_us_pct,
        rows_with_sam, coverage_sam_pct,
        sum_obligation_365d, sum_contracts_365d, sum_sam_uei,
    )

    if rows_out < MIN_ROWS_MATCHED:
        logger.error("HARD FAIL: rows=%d < MIN_ROWS_MATCHED=%d", rows_out, MIN_ROWS_MATCHED)
        return 1

    if rows_with_us < MIN_USASPENDING_ENRICHED_FLOOR:
        logger.error(
            "HARD FAIL: USAspending-enriched rows=%d < MIN_USASPENDING_ENRICHED_FLOOR=%d "
            "(two-hop USAspending chain regression)",
            rows_with_us, MIN_USASPENDING_ENRICHED_FLOOR,
        )
        return 1

    if rows_with_sam < MIN_SAM_ENRICHED_FLOOR:
        logger.error(
            "HARD FAIL: SAM-enriched rows=%d < MIN_SAM_ENRICHED_FLOOR=%d "
            "(SAM bridge chain regression)",
            rows_with_sam, MIN_SAM_ENRICHED_FLOOR,
        )
        return 1

    if not args.apply:
        logger.info(
            "DRY-RUN: would write %d rows (%d USAspending-enriched = %.2f%%; "
            "%d SAM-enriched = %.2f%%). Pass --apply to write.",
            rows_out, rows_with_us, coverage_us_pct, rows_with_sam, coverage_sam_pct,
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
        "OK: bridges.sba_ny_construction_usaspending_sam_enriched_lance written "
        "(%d rows; us_coverage=%.2f%%; sam_coverage=%.2f%%; "
        "sum_us_obligation_365d=$%s; sum_sam_uei=%s; bridge_run_id=%s)",
        lance_count, coverage_us_pct, coverage_sam_pct,
        sum_obligation_365d, sum_sam_uei, BRIDGE_RUN_ID,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

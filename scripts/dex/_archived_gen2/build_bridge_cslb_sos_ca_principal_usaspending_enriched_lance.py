#!/usr/bin/env python3
"""CSLB x SoS x principal x USAspending enriched cohort -- Pattern A enriched-cohort emit (Lance).

Two-hop join: cohort x USAspending SoS bridge x usaspending.recipient_grain (aggregated).

Identical-twin structure to PR #484 (build_bridge_cslb_sos_ca_principal_sba_enriched_lance.py),
swapping SBA-side inputs for USAspending-side. PR #485 list-encoding fix applied
(array_to_string(..., '|') for LIST<VARCHAR>).

Per Pattern A enriched-cohort (PR #469 / PR #484): identity-bridge registry calls are ABSENT
(no ops.bridges row written, no match-method-registry imports). YES ops.data_sources row.
YES per-row inherited + own bridge_run_id provenance (3 inherited renames + own UUID).

INNER side: bridges.cslb_sos_ca_principal_lance (PR #483; 616,622 principal-grain rows).
Identity hop: bridges.usaspending_sos_ca_owner_lance (PR #487; 14,964 rows; carries
  recipient_uei + sos_entity_num) INNER JOINed to usaspending.recipient_grain_lance
  (pre-aggregated per recipient_uei; 134,153 rows), then GROUPED BY sos_entity_num for
  per-entity rollup; LEFT JOINed back to the cohort spine.

Aggregate at sos_entity_num grain to handle multi-UEI-per-entity cases:
  usaspending_recipient_count = count(DISTINCT recipient_uei),
  usaspending_total_obligation_{30,90,180,365}d = sum across matched UEIs,
  usaspending_contract_count_{30,90,180,365}d = sum across matched UEIs,
  usaspending_max_contract_date = MAX(latest_contract_date),
  usaspending_earliest_contract_date_365d = MIN(earliest_contract_date_365d),
  usaspending_top_psc_set = array_to_string(list_distinct, '|'),
  usaspending_recipient_uei_set = array_to_string(list_distinct, '|'),
  15 diversity-flag booleans via BOOL_OR (TRUE if any matched UEI has the flag),
  usaspending_sos_bridge_run_ids = array_to_string(list_distinct, '|').

Validator probe: 7,733 enriched rows (~1.25% of cohort 616,622). LOW coverage is expected
(only ~1,725 of 162,316 distinct cohort entities chain through to recipient_grain).
Consumer filter: WHERE usaspending_recipient_count > 0.

Per DATA-FACTORY-ARCHITECTURE-PATTERNS.md §"Pattern A enriched-cohort emit":
NOT a new identity bridge (no new ops.bridges row, no new match method).

Run (apply):
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      uv run python scripts/build_bridge_cslb_sos_ca_principal_usaspending_enriched_lance.py --apply

Dry-run (print row count + coverage stats only):
    uv run python scripts/build_bridge_cslb_sos_ca_principal_usaspending_enriched_lance.py
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

DATASET_SLUG = "cslb_sos_ca_principal_usaspending_enriched_lance"
BRIDGE_VERSION = "1.0.0"

# Validator probe: cohort = 616,622; LEFT JOIN preserves scale; floor at 500K
# gives ~19% headroom.
MIN_ROWS_MATCHED = 500_000

# Secondary floor: probe shows ~7,733 rows enriched (1.25% coverage; ~35% headroom).
# Guards against silent regression in the two-hop USAspending chain.
MIN_ENRICHED_FLOOR = 5_000

COHORT_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/cslb_sos_ca_principal_lance"
USPENDING_BRIDGE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/usaspending_sos_ca_owner_lance"
USPENDING_RECIPIENT_GRAIN_URI = "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/recipient_grain_lance"
OUTPUT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/cslb_sos_ca_principal_usaspending_enriched_lance"

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
        description="CSLB x SoS x principal x USAspending enriched cohort -- Pattern A enriched-cohort emit (Lance)."
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
    cohort_arrow = ds_cohort.scanner().to_table()  # all cohort cols
    logger.info("  cohort: %d rows x %d cols", cohort_arrow.num_rows, len(cohort_arrow.column_names))

    logger.info("opening %s ...", USPENDING_BRIDGE_URI)
    ds_bridge = lance.dataset(USPENDING_BRIDGE_URI, storage_options=storage_options)
    bridge_arrow = ds_bridge.scanner(
        columns=["sos_entity_num", "recipient_uei", "bridge_run_id"],
    ).to_table()
    logger.info("  usaspending bridge (projected): %d rows", bridge_arrow.num_rows)

    logger.info("opening %s ...", USPENDING_RECIPIENT_GRAIN_URI)
    ds_rg = lance.dataset(USPENDING_RECIPIENT_GRAIN_URI, storage_options=storage_options)
    # Per validator contract.md §"Available recipient_grain columns" -- actual upstream schema
    # (NOT the directive's placeholder columns which do not exist upstream).
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

    # ---- Step 2: DuckDB two-hop with rollup-then-LEFT-JOIN ---- #

    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")
    con.register("co", cohort_arrow)
    con.register("br", bridge_arrow)
    con.register("rg", rg_arrow)

    # Step 2a: per-sos_entity_num rollup over the bridge x recipient_grain INNER JOIN.
    # Aggregate FIRST so cohort spine sees one row per sos_entity_num on right side of LEFT JOIN.
    # List columns serialized as pipe-delimited VARCHAR per PR #485 list-encoding fix to avoid
    # Lance "Definition buffer size too large" on LIST<VARCHAR> columns (Lance 1.5.x limitation).
    con.execute(
        """
        CREATE TEMP TABLE usaspending_agg AS
        SELECT
            br.sos_entity_num,
            count(DISTINCT br.recipient_uei)                                                 AS usaspending_recipient_count,
            sum(rg.total_obligation_30d)                                                     AS usaspending_total_obligation_30d,
            sum(rg.total_obligation_90d)                                                     AS usaspending_total_obligation_90d,
            sum(rg.total_obligation_180d)                                                    AS usaspending_total_obligation_180d,
            sum(rg.total_obligation_365d)                                                    AS usaspending_total_obligation_365d,
            sum(rg.contract_count_30d)                                                       AS usaspending_contract_count_30d,
            sum(rg.contract_count_90d)                                                       AS usaspending_contract_count_90d,
            sum(rg.contract_count_180d)                                                      AS usaspending_contract_count_180d,
            sum(rg.contract_count_365d)                                                      AS usaspending_contract_count_365d,
            max(rg.latest_contract_date)                                                     AS usaspending_max_contract_date,
            min(rg.earliest_contract_date_365d)                                              AS usaspending_earliest_contract_date_365d,
            array_to_string(list_distinct(list(rg.top_psc)), '|')                            AS usaspending_top_psc_set,
            array_to_string(list_distinct(list(br.recipient_uei)), '|')                      AS usaspending_recipient_uei_set,
            BOOL_OR(rg.is_8a)                                                                AS usaspending_is_8a_any,
            BOOL_OR(rg.is_hubzone)                                                           AS usaspending_is_hubzone_any,
            BOOL_OR(rg.is_wosb)                                                              AS usaspending_is_wosb_any,
            BOOL_OR(rg.is_edwosb)                                                            AS usaspending_is_edwosb_any,
            BOOL_OR(rg.is_sdvosb)                                                            AS usaspending_is_sdvosb_any,
            BOOL_OR(rg.is_vosb)                                                              AS usaspending_is_vosb_any,
            BOOL_OR(rg.is_sdb)                                                               AS usaspending_is_sdb_any,
            BOOL_OR(rg.is_minority_owned)                                                    AS usaspending_is_minority_owned_any,
            BOOL_OR(rg.is_native_american_owned)                                             AS usaspending_is_native_american_owned_any,
            BOOL_OR(rg.is_alaskan_native_corp)                                               AS usaspending_is_alaskan_native_corp_any,
            BOOL_OR(rg.is_native_hawaiian_org)                                               AS usaspending_is_native_hawaiian_org_any,
            BOOL_OR(rg.is_tribal_corp)                                                       AS usaspending_is_tribal_corp_any,
            BOOL_OR(rg.is_nonprofit)                                                         AS usaspending_is_nonprofit_any,
            BOOL_OR(rg.is_educational)                                                       AS usaspending_is_educational_any,
            BOOL_OR(rg.is_jv)                                                                AS usaspending_is_jv_any,
            array_to_string(list_distinct(list(br.bridge_run_id)), '|')                      AS usaspending_sos_bridge_run_ids
        FROM br
        INNER JOIN rg ON br.recipient_uei = rg.recipient_uei
        WHERE rg.recipient_uei IS NOT NULL
        GROUP BY br.sos_entity_num
        """
    )

    # Step 2b: cohort spine x LEFT JOIN usaspending_agg.
    # Per L17 provenance: rename cohort's bridge_run_id -> cslb_principal_bridge_run_id;
    # cohort's cslb_sos_bridge_run_id propagates verbatim; USAspending bridge_run_id lands
    # as usaspending_sos_bridge_run_ids (pipe-delimited list_distinct per PR #485);
    # this emit's own UUID stamps as bridge_run_id. EXCLUDE cohort's bridge_version +
    # generated_at (this emit re-stamps them).
    con.execute(
        f"""
        CREATE TEMP TABLE enriched AS
        SELECT
            co.* EXCLUDE (bridge_run_id, bridge_version, generated_at),
            co.bridge_run_id                                      AS cslb_principal_bridge_run_id,
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
            ua.usaspending_sos_bridge_run_ids,
            '{BRIDGE_RUN_ID}'                                     AS bridge_run_id,
            '{BRIDGE_VERSION}'                                    AS bridge_version,
            TIMESTAMP '{generated_at_iso}'                        AS generated_at
        FROM co
        LEFT JOIN usaspending_agg ua ON co.sos_entity_num = ua.sos_entity_num
        """
    )

    rows_out = con.execute("SELECT count(*) FROM enriched").fetchone()[0]
    rows_with_us = con.execute(
        "SELECT count(*) FROM enriched WHERE usaspending_contract_count_365d IS NOT NULL"
    ).fetchone()[0]
    coverage_pct = (rows_with_us / rows_out * 100) if rows_out else 0.0
    sum_obligation_365d = con.execute(
        "SELECT coalesce(sum(usaspending_total_obligation_365d), 0) FROM enriched"
    ).fetchone()[0]
    sum_contracts_365d = con.execute(
        "SELECT coalesce(sum(usaspending_contract_count_365d), 0) FROM enriched"
    ).fetchone()[0]

    logger.info(
        "enriched: %d rows (%d with USAspending = %.2f%% coverage; sum_contracts_365d=%s sum_obligation_365d=$%s)",
        rows_out, rows_with_us, coverage_pct, sum_contracts_365d, sum_obligation_365d,
    )

    if rows_out < MIN_ROWS_MATCHED:
        logger.error("HARD FAIL: rows=%d < MIN_ROWS_MATCHED=%d", rows_out, MIN_ROWS_MATCHED)
        return 1

    if rows_with_us < MIN_ENRICHED_FLOOR:
        logger.error(
            "HARD FAIL: enriched rows=%d < MIN_ENRICHED_FLOOR=%d (USAspending two-hop chain regression)",
            rows_with_us, MIN_ENRICHED_FLOOR,
        )
        return 1

    if not args.apply:
        logger.info(
            "DRY-RUN: would write %d rows (%d enriched = %.2f%% coverage). Pass --apply to write.",
            rows_out, rows_with_us, coverage_pct,
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
        "OK: bridges.cslb_sos_ca_principal_usaspending_enriched_lance written (%d rows; enriched=%d/%.2f%%; bridge_run_id=%s)",
        lance_count, rows_with_us, coverage_pct, BRIDGE_RUN_ID,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

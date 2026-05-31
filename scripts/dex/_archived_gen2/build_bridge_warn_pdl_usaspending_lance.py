#!/usr/bin/env python3
"""DuckDB Pattern A enriched-cohort emit: WARN x PDL x USAspending.

Cycle: warn-pdl-usaspending-cohort-emit (2026-05-20).

The prioritizable distressed-company surface for the WARN -> private-credit GTM.
One row per (WARN notice, matched PDL company): the WARN trigger details + PDL
qualification (headcount / LinkedIn / domain) + — where the company has a
federal footprint — authoritative SAM NAICS (company type) and USAspending
federal-contract health signals (the "stable underlying business" evidence).

Pattern A enriched-cohort emit — NOT a new identity bridge:
  - Match logic was already settled upstream by `warn_pdl` (Pattern B,
    company-name match) and `sam_pdl_usaspending` (itself a Pattern A cohort
    over the `sam_pdl_domain` Pattern B bridge). This script registers NO
    bridge and NO match method; it does NOT touch ops.bridges /
    ops.match_method_versions (L28).
  - Provenance (L17): every row carries the inherited `warn_pdl_bridge_run_id`
    and `sam_pdl_usaspending_bridge_run_id`, plus this emit's own fresh
    `cohort_bridge_run_id` UUID.

Inputs — all small, already-built Lance datasets, so this runs LOCALLY (not
Modal; the sam_pdl_usaspending precedent went Modal only for raw 15.5M-row
USAspending, which is already pre-aggregated inside sam_pdl_usaspending_lance):
  - bridges/warn_pdl_lance            (~57.5K rows — the spine: warn_hash_id x pdl_id)
  - warn/notices_lance                (~85K rows — WARN trigger payload, keyed hash_id)
  - bridges/sam_pdl_usaspending_lance (~295K rows — SAM NAICS + USAspending agg, keyed pdl_id)

Grain: one row per (warn_hash_id, pdl_id) — the warn_pdl_lance grain. Both
LEFT JOINs preserve every warn_pdl_lance row. sam_pdl_usaspending_lance is
de-duped to pdl_id grain first (the most federally-active UEI per pdl_id, by
lifetime_total_obligated) so the join cannot fan out.

`has_federal_footprint` = the PDL company resolved to a SAM/USAspending record.
`pdl_industry` is carried but is LOW-TRUST (self-selected LinkedIn data) —
company-type filtering must use `primary_naics` / `naics_primary_2digit`
(authoritative federal classification).

Output: polaris-warehouse/bridges/warn_pdl_usaspending_lance
  BTREE on warn_hash_id, pdl_id, uei, naics_primary_2digit.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow python \\
    apps/data-engine-x/scripts/build_bridge_warn_pdl_usaspending_lance.py --dry-run
  # then --apply
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

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("build_bridge_warn_pdl_usaspending_lance")

DATASET_SLUG = "warn_pdl_usaspending_lance"
BRIDGE_VERSION = "1.0.0"

WARN_PDL_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/warn_pdl_lance"
WARN_NOTICES_URI = "s3://dex-raw-landing-zone/polaris-warehouse/warn/notices_lance"
SAM_PDL_USA_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sam_pdl_usaspending_lance"
OUTPUT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/warn_pdl_usaspending_lance"

# The cohort is warn_pdl_lance LEFT-JOINed (no row loss) — row count equals
# warn_pdl_lance (~57.5K). Floor set well below to catch a broken join.
MIN_ROW_FLOOR = 50_000
TMP_DIR = "/tmp/lance"


def _lance_storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _materialize_inputs(storage_options: dict) -> tuple:
    """Open the three upstream Lance datasets, project to Arrow tables."""
    import lance

    logger.info("opening bridges/warn_pdl_lance ...")
    warn_pdl_ds = lance.dataset(WARN_PDL_URI, storage_options=storage_options)
    warn_pdl_arrow = warn_pdl_ds.scanner(
        columns=[
            "warn_hash_id", "warn_company", "warn_company_normalized", "warn_state",
            "pdl_id", "pdl_name", "pdl_state", "pdl_website", "pdl_linkedin_url",
            "pdl_size", "pdl_founded", "pdl_locality", "pdl_industry",
            "confidence_tier", "state_agreement", "bridge_run_id",
        ],
    ).to_table()
    logger.info("  warn_pdl_lance: %d rows", warn_pdl_arrow.num_rows)

    logger.info("opening warn/notices_lance ...")
    warn_ds = lance.dataset(WARN_NOTICES_URI, storage_options=storage_options)
    warn_arrow = warn_ds.scanner(
        columns=[
            "hash_id", "notice_date_typed", "effective_date_typed", "jobs_typed",
            "is_closure", "is_temporary", "is_superseded", "is_amendment", "location",
        ],
    ).to_table()
    logger.info("  warn/notices_lance: %d rows", warn_arrow.num_rows)

    logger.info("opening bridges/sam_pdl_usaspending_lance ...")
    spu_ds = lance.dataset(SAM_PDL_USA_URI, storage_options=storage_options)
    spu_arrow = spu_ds.scanner(
        columns=[
            "pdl_id", "uei", "legal_business_name", "primary_naics",
            "naics_primary_2digit", "entity_url", "physical_address_state_normalized",
            "has_active_award", "active_contract_count", "active_total_obligated",
            "lifetime_contract_count", "lifetime_total_obligated",
            "max_period_of_performance_end_date", "latest_action_date", "bridge_run_id",
        ],
    ).to_table()
    logger.info("  sam_pdl_usaspending_lance: %d rows", spu_arrow.num_rows)

    return warn_pdl_arrow, warn_arrow, spu_arrow


def _build_cohort(
    warn_pdl_arrow, warn_arrow, spu_arrow, *,
    cohort_bridge_run_id: str, generated_at_iso: str,
) -> tuple:
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET memory_limit='6GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET preserve_insertion_order=false")
    con.register("warn_pdl_raw", warn_pdl_arrow)
    con.register("warn_notices_raw", warn_arrow)
    con.register("sam_pdl_usa_raw", spu_arrow)

    # De-dup sam_pdl_usaspending to pdl_id grain. A PDL company can carry >1 SAM
    # UEI registration; keep the most federally-active one so the LEFT JOIN onto
    # the warn_pdl spine cannot fan out.
    con.execute("""
        CREATE TEMP TABLE sam_pdl_usa_dedup AS
        SELECT pdl_id, uei, legal_business_name, primary_naics, naics_primary_2digit,
               entity_url, physical_address_state_normalized, has_active_award,
               active_contract_count, active_total_obligated, lifetime_contract_count,
               lifetime_total_obligated, max_period_of_performance_end_date,
               latest_action_date, bridge_run_id
        FROM sam_pdl_usa_raw
        WHERE pdl_id IS NOT NULL AND trim(pdl_id) <> ''
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY pdl_id
            ORDER BY COALESCE(lifetime_total_obligated, 0) DESC, uei
        ) = 1
    """)
    spu_rows = con.execute("SELECT COUNT(*) FROM sam_pdl_usa_dedup").fetchone()[0]
    logger.info("  sam_pdl_usa_dedup (pdl_id grain): %d rows", spu_rows)

    # The cohort: warn_pdl spine + WARN trigger payload + SAM/USAspending enrichment.
    con.execute(f"""
        CREATE TEMP TABLE cohort_out AS
        SELECT
            wp.warn_hash_id,
            wp.warn_company,
            wp.warn_company_normalized,
            wp.warn_state,
            -- WARN trigger payload
            w.notice_date_typed,
            w.effective_date_typed,
            w.jobs_typed,
            TRY_CAST(w.is_closure   AS BOOLEAN) AS is_closure,
            TRY_CAST(w.is_temporary AS BOOLEAN) AS is_temporary,
            TRY_CAST(w.is_superseded AS BOOLEAN) AS is_superseded,
            TRY_CAST(w.is_amendment AS BOOLEAN) AS is_amendment,
            w.location AS warn_location,
            -- PDL identity + qualification
            wp.pdl_id,
            wp.pdl_name,
            wp.pdl_state,
            wp.pdl_website,
            wp.pdl_linkedin_url,
            wp.pdl_size,
            wp.pdl_founded,
            wp.pdl_locality,
            wp.pdl_industry,
            -- WARN<->PDL match confidence
            wp.confidence_tier AS warn_pdl_confidence_tier,
            wp.state_agreement,
            -- SAM / USAspending enrichment (LEFT JOIN — NULL when no federal footprint)
            (sp.uei IS NOT NULL)                       AS has_federal_footprint,
            sp.uei,
            sp.legal_business_name                     AS sam_legal_business_name,
            sp.primary_naics,
            sp.naics_primary_2digit,
            sp.entity_url                              AS sam_entity_url,
            sp.physical_address_state_normalized       AS sam_physical_state,
            COALESCE(sp.has_active_award, FALSE)        AS has_active_award,
            sp.active_contract_count,
            sp.active_total_obligated,
            sp.lifetime_contract_count,
            sp.lifetime_total_obligated,
            sp.max_period_of_performance_end_date,
            sp.latest_action_date,
            -- provenance
            wp.bridge_run_id                           AS warn_pdl_bridge_run_id,
            sp.bridge_run_id                           AS sam_pdl_usaspending_bridge_run_id,
            CAST('{cohort_bridge_run_id}' AS VARCHAR)  AS cohort_bridge_run_id,
            TIMESTAMP '{generated_at_iso}'             AS generated_at,
            '{BRIDGE_VERSION}'                         AS bridge_version
        FROM warn_pdl_raw wp
        LEFT JOIN warn_notices_raw w
               ON w.hash_id = wp.warn_hash_id
        LEFT JOIN sam_pdl_usa_dedup sp
               ON sp.pdl_id = wp.pdl_id
    """)

    counts = con.execute("""
        SELECT
            COUNT(*)                                              AS rows_out,
            COUNT(*) FILTER (WHERE has_federal_footprint)         AS rows_federal,
            COUNT(*) FILTER (WHERE has_active_award)              AS rows_active_award,
            COUNT(*) FILTER (WHERE naics_primary_2digit = '23')  AS rows_construction,
            COUNT(*) FILTER (WHERE warn_pdl_confidence_tier='platinum') AS rows_platinum,
            COUNT(DISTINCT warn_hash_id)                          AS distinct_notices,
            COUNT(DISTINCT pdl_id)                                AS distinct_companies
        FROM cohort_out
    """).fetchone()
    result = {
        "rows_out": counts[0],
        "rows_federal": counts[1],
        "rows_active_award": counts[2],
        "rows_construction": counts[3],
        "rows_platinum": counts[4],
        "distinct_notices": counts[5],
        "distinct_companies": counts[6],
    }
    return con, result


def _write_cohort_lance(con, storage_options: dict) -> int:
    import lance

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = TMP_DIR
    os.environ["LANCE_BYPASS_SPILLING"] = "true"

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        logger.info("writing cohort to Lance at %s ...", OUTPUT_LANCE_URI)
        reader = con.from_query("SELECT * FROM cohort_out").to_arrow_reader(
            batch_size=100_000
        )
        ds = lance.write_dataset(
            reader,
            OUTPUT_LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        lance_count = ds.count_rows()
        logger.info(
            "wrote %d rows in %.1fs (version=%s)", lance_count, time.time() - t0, ds.version
        )

        for col in ("warn_hash_id", "pdl_id", "uei", "naics_primary_2digit"):
            try:
                ds.create_scalar_index(col, index_type="BTREE", replace=True)
                logger.info("  BTREE on %s OK", col)
            except Exception as e:
                logger.warning("  BTREE on %s failed (non-fatal): %s", col, e)
        try:
            ds.optimize.compact_files()
        except Exception as e:
            logger.warning("compact_files failed (non-fatal): %s", e)
        try:
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as e:
            logger.warning("cleanup_old_versions failed (non-fatal): %s", e)

    return lance_count


def main() -> int:
    ap = argparse.ArgumentParser(description="WARN x PDL x USAspending cohort emit")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true", help="write the Lance dataset")
    grp.add_argument("--dry-run", action="store_true", help="count only, no writes")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")
    if args.apply and not os.environ.get("DEX_DB_URL_DIRECT"):
        raise SystemExit("FAIL: DEX_DB_URL_DIRECT not set (required for lance_commit_lock)")

    started_at = datetime.now(tz=timezone.utc)
    t0 = time.time()
    cohort_bridge_run_id = str(uuid.uuid4())
    storage_options = _lance_storage_options()

    logger.info("cohort emit: %s (Pattern A — no registry writes)", DATASET_SLUG)
    logger.info("cohort_bridge_run_id=%s", cohort_bridge_run_id)
    logger.info("output: %s", OUTPUT_LANCE_URI)

    try:
        warn_pdl_arrow, warn_arrow, spu_arrow = _materialize_inputs(storage_options)
        con, counts = _build_cohort(
            warn_pdl_arrow, warn_arrow, spu_arrow,
            cohort_bridge_run_id=cohort_bridge_run_id,
            generated_at_iso=started_at.isoformat(),
        )

        logger.info("-" * 60)
        logger.info("cohort composition:")
        logger.info("  rows_out (warn_hash_id x pdl_id): %d", counts["rows_out"])
        logger.info("    with federal footprint:         %d", counts["rows_federal"])
        logger.info("    with an active federal award:   %d", counts["rows_active_award"])
        logger.info("    construction (NAICS 23):        %d", counts["rows_construction"])
        logger.info("    platinum warn<->pdl match:      %d", counts["rows_platinum"])
        logger.info("  distinct WARN notices:            %d", counts["distinct_notices"])
        logger.info("  distinct PDL companies:           %d", counts["distinct_companies"])

        if counts["rows_out"] < MIN_ROW_FLOOR:
            msg = f"HARD FAIL: rows_out={counts['rows_out']:,} < floor={MIN_ROW_FLOOR:,}"
            logger.error(msg)
            return 1

        if args.dry_run:
            logger.info("DRY RUN — no Lance writes. duration=%.1fs", time.time() - t0)
            return 0

        lance_count = _write_cohort_lance(con, storage_options)
        if lance_count < MIN_ROW_FLOOR:
            logger.error(
                "HARD FAIL post-write: lance_count=%d < floor=%d", lance_count, MIN_ROW_FLOOR
            )
            return 1
        logger.info(
            "OK — cohort_bridge_run_id=%s lance_rows=%d duration=%.1fs",
            cohort_bridge_run_id, lance_count, time.time() - t0,
        )
        logger.info("    output: %s", OUTPUT_LANCE_URI)
        return 0

    except Exception:
        logger.exception("cohort emit failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

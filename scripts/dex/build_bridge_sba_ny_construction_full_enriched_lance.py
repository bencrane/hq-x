#!/usr/bin/env python3
"""SBA × NY construction × USAspending × SAM × NYC × MTA × Local Authority enriched cohort — Pattern A.

Extension of the prior cycle's `sba_ny_construction_usaspending_sam_enriched_lance`
spine. Mirror of PR #485 enriched-cohort shape; adds three new LEFT-JOIN aggregate
blocks (NYC contracts, MTA procurements, NY Local Authority procurements) per
SBA-borrower grain.

Four-input emit:
  COHORT spine    : bridges.sba_ny_construction_usaspending_sam_enriched_lance
                    (4,687 rows × ~45 cols from prior cycle).
  NYC hop         : bridges.sba_ny_nyc_contracts_lance aggregated per
                    sba_legal_name_normalized.
  MTA hop         : bridges.sba_ny_mta_lance aggregated per
                    sba_legal_name_normalized.
  Local Authority : bridges.sba_ny_local_authority_lance aggregated per
                    sba_legal_name_normalized.

Per Pattern A enriched-cohort (PR #469/#484/#485): NOT a new identity bridge
(no ops.bridges row). YES ops.data_sources row (s5 migration). YES per-row
inherited + own bridge_run_id provenance.

Aggregate columns (NYC):
  nyc_contract_count, nyc_distinct_agency_count, nyc_distinct_agencies
  (pipe-delim per L54), nyc_distinct_category_descriptions (pipe-delim),
  nyc_total_contract_amount (TRY_CAST), nyc_max_contract_amount,
  nyc_earliest_start_date, nyc_latest_start_date,
  sba_ny_nyc_contracts_bridge_run_ids (pipe-delim).

Aggregate columns (MTA):
  mta_procurement_count, mta_total_contract_amount (TRY_CAST),
  mta_max_contract_amount, mta_distinct_procurement_types (pipe-delim),
  mta_earliest_award_date, mta_latest_award_date,
  mta_any_mwbe (BOOL_OR vendor_is_a_mwbe='Yes'),
  sba_ny_mta_bridge_run_ids (pipe-delim).

Aggregate columns (Local Authority):
  la_procurement_count, la_distinct_authority_count, la_distinct_authorities
  (pipe-delim), la_total_contract_amount (TRY_CAST), la_max_contract_amount,
  la_distinct_procurement_types (pipe-delim), la_earliest_award_date,
  la_latest_award_date, sba_ny_local_authority_bridge_run_ids (pipe-delim).

L49 TRY_CAST applied to Socrata-VARCHAR numeric/date aggregates.
L54 pipe-delimited VARCHAR for all multi-value cols.

Run (apply):
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      uv run python scripts/build_bridge_sba_ny_construction_full_enriched_lance.py --apply
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._lib.lance_commit_lock import lance_commit_lock

DATASET_SLUG = "sba_ny_construction_full_enriched_lance"
BRIDGE_VERSION = "1.0.0"

# Spine = bridges.sba_ny_construction_usaspending_sam_enriched_lance (4,687 rows).
# LEFT JOINs preserve scale; floor at 4,000 gives ~15% headroom.
MIN_ROWS_MATCHED = 4_000

# Secondary floors guard against silent regression in the three new chains.
# Conservative — Pattern B bridge floors apply at the BRIDGE level; this is
# the cohort-coverage-after-LEFT-JOIN floor (typically a fraction of the
# bridge's distinct-borrower count given cohort scope).
MIN_NYC_ENRICHED_FLOOR = 50
MIN_MTA_ENRICHED_FLOOR = 30
MIN_LA_ENRICHED_FLOOR = 50

COHORT_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_ny_construction_usaspending_sam_enriched_lance"
)
NYC_BRIDGE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_ny_nyc_contracts_lance"
MTA_BRIDGE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_ny_mta_lance"
LA_BRIDGE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_ny_local_authority_lance"
OUTPUT_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_ny_construction_full_enriched_lance"
)

TMP_DIR = "/tmp/lance"

logger = logging.getLogger(__name__)
logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO, stream=sys.stdout)


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
        description="SBA × NY construction × {USAspending, SAM, NYC, MTA, Local Authority} enriched cohort."
    )
    parser.add_argument("--apply", action="store_true", default=False)
    args = parser.parse_args()

    _ensure_db_url()
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("TMPDIR", TMP_DIR)
    os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")

    import duckdb
    import lance

    storage_options = _storage_options()

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

    logger.info("opening %s ...", NYC_BRIDGE_URI)
    ds_nyc = lance.dataset(NYC_BRIDGE_URI, storage_options=storage_options)
    nyc_arrow = ds_nyc.scanner(
        columns=[
            "sba_legal_name_normalized", "contract_id",
            "nyc_agency_name", "nyc_category_description",
            "nyc_contract_amount", "nyc_start_date",
            "bridge_run_id",
        ],
    ).to_table()
    logger.info("  nyc bridge (projected): %d rows", nyc_arrow.num_rows)

    logger.info("opening %s ...", MTA_BRIDGE_URI)
    ds_mta = lance.dataset(MTA_BRIDGE_URI, storage_options=storage_options)
    mta_arrow = ds_mta.scanner(
        columns=[
            "sba_legal_name_normalized", "contract_id",
            "mta_type_of_procurement", "mta_contract_amount",
            "mta_award_date", "mta_vendor_is_a_mwbe",
            "bridge_run_id",
        ],
    ).to_table()
    logger.info("  mta bridge (projected): %d rows", mta_arrow.num_rows)

    logger.info("opening %s ...", LA_BRIDGE_URI)
    ds_la = lance.dataset(LA_BRIDGE_URI, storage_options=storage_options)
    la_arrow = ds_la.scanner(
        columns=[
            "sba_legal_name_normalized", "contract_id",
            "la_authority_name", "la_type_of_procurement",
            "la_contract_amount", "la_award_date",
            "bridge_run_id",
        ],
    ).to_table()
    logger.info("  local_authority bridge (projected): %d rows", la_arrow.num_rows)

    # ---- Step 2: DuckDB rollup-then-LEFT-JOIN ---- #

    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")
    con.register("co", cohort_arrow)
    con.register("nyc", nyc_arrow)
    con.register("mta", mta_arrow)
    con.register("la", la_arrow)

    logger.info("aggregating nyc bridge per sba_legal_name_normalized ...")
    con.execute(
        """
        CREATE TEMP TABLE nyc_agg AS
        SELECT
            nyc.sba_legal_name_normalized,
            COUNT(*)                                                                            AS nyc_contract_count,
            COUNT(DISTINCT nyc.nyc_agency_name)                                                 AS nyc_distinct_agency_count,
            array_to_string(list_distinct(list(nyc.nyc_agency_name)), '|')                      AS nyc_distinct_agencies,
            array_to_string(list_distinct(list(nyc.nyc_category_description)), '|')             AS nyc_distinct_category_descriptions,
            SUM(TRY_CAST(nyc.nyc_contract_amount AS DOUBLE))                                    AS nyc_total_contract_amount,
            MAX(TRY_CAST(nyc.nyc_contract_amount AS DOUBLE))                                    AS nyc_max_contract_amount,
            MIN(TRY_CAST(nyc.nyc_start_date AS DATE))                                           AS nyc_earliest_start_date,
            MAX(TRY_CAST(nyc.nyc_start_date AS DATE))                                           AS nyc_latest_start_date,
            array_to_string(list_distinct(list(nyc.bridge_run_id)), '|')                        AS sba_ny_nyc_contracts_bridge_run_ids
        FROM nyc
        GROUP BY nyc.sba_legal_name_normalized
        """
    )
    rows_nyc_agg = con.execute("SELECT COUNT(*) FROM nyc_agg").fetchone()[0]
    logger.info("  nyc_agg: %d rows", rows_nyc_agg)

    logger.info("aggregating mta bridge per sba_legal_name_normalized ...")
    con.execute(
        """
        CREATE TEMP TABLE mta_agg AS
        SELECT
            mta.sba_legal_name_normalized,
            COUNT(*)                                                                            AS mta_procurement_count,
            SUM(TRY_CAST(mta.mta_contract_amount AS DOUBLE))                                    AS mta_total_contract_amount,
            MAX(TRY_CAST(mta.mta_contract_amount AS DOUBLE))                                    AS mta_max_contract_amount,
            array_to_string(list_distinct(list(mta.mta_type_of_procurement)), '|')              AS mta_distinct_procurement_types,
            MIN(TRY_CAST(mta.mta_award_date AS DATE))                                           AS mta_earliest_award_date,
            MAX(TRY_CAST(mta.mta_award_date AS DATE))                                           AS mta_latest_award_date,
            BOOL_OR(mta.mta_vendor_is_a_mwbe = 'Yes')                                           AS mta_any_mwbe,
            array_to_string(list_distinct(list(mta.bridge_run_id)), '|')                        AS sba_ny_mta_bridge_run_ids
        FROM mta
        GROUP BY mta.sba_legal_name_normalized
        """
    )
    rows_mta_agg = con.execute("SELECT COUNT(*) FROM mta_agg").fetchone()[0]
    logger.info("  mta_agg: %d rows", rows_mta_agg)

    logger.info("aggregating local_authority bridge per sba_legal_name_normalized ...")
    # LA's contract_amount is formatted with dollar sign + commas (e.g. "$165,795.47")
    # unlike NYC ("300000") and MTA ("8249680.00") which are plain numeric strings.
    # regexp_replace strips $ and , before TRY_CAST to recover the numeric value.
    con.execute(
        """
        CREATE TEMP TABLE la_agg AS
        SELECT
            la.sba_legal_name_normalized,
            COUNT(*)                                                                                              AS la_procurement_count,
            COUNT(DISTINCT la.la_authority_name)                                                                  AS la_distinct_authority_count,
            array_to_string(list_distinct(list(la.la_authority_name)), '|')                                       AS la_distinct_authorities,
            SUM(TRY_CAST(regexp_replace(la.la_contract_amount, '[$,]', '', 'g') AS DOUBLE))                       AS la_total_contract_amount,
            MAX(TRY_CAST(regexp_replace(la.la_contract_amount, '[$,]', '', 'g') AS DOUBLE))                       AS la_max_contract_amount,
            array_to_string(list_distinct(list(la.la_type_of_procurement)), '|')                                  AS la_distinct_procurement_types,
            MIN(TRY_CAST(la.la_award_date AS DATE))                                                               AS la_earliest_award_date,
            MAX(TRY_CAST(la.la_award_date AS DATE))                                                               AS la_latest_award_date,
            array_to_string(list_distinct(list(la.bridge_run_id)), '|')                                           AS sba_ny_local_authority_bridge_run_ids
        FROM la
        GROUP BY la.sba_legal_name_normalized
        """
    )
    rows_la_agg = con.execute("SELECT COUNT(*) FROM la_agg").fetchone()[0]
    logger.info("  la_agg: %d rows", rows_la_agg)

    logger.info("LEFT JOIN spine x nyc_agg x mta_agg x la_agg + provenance stamping ...")
    con.execute(
        f"""
        CREATE TEMP TABLE enriched AS
        SELECT
            co.* EXCLUDE (bridge_run_id, bridge_version, generated_at),
            co.bridge_run_id                                  AS sba_ny_construction_usaspending_sam_enriched_bridge_run_id,
            -- NYC block
            na.nyc_contract_count,
            na.nyc_distinct_agency_count,
            na.nyc_distinct_agencies,
            na.nyc_distinct_category_descriptions,
            na.nyc_total_contract_amount,
            na.nyc_max_contract_amount,
            na.nyc_earliest_start_date,
            na.nyc_latest_start_date,
            na.sba_ny_nyc_contracts_bridge_run_ids,
            -- MTA block
            ma.mta_procurement_count,
            ma.mta_total_contract_amount,
            ma.mta_max_contract_amount,
            ma.mta_distinct_procurement_types,
            ma.mta_earliest_award_date,
            ma.mta_latest_award_date,
            ma.mta_any_mwbe,
            ma.sba_ny_mta_bridge_run_ids,
            -- Local Authority block
            la_a.la_procurement_count,
            la_a.la_distinct_authority_count,
            la_a.la_distinct_authorities,
            la_a.la_total_contract_amount,
            la_a.la_max_contract_amount,
            la_a.la_distinct_procurement_types,
            la_a.la_earliest_award_date,
            la_a.la_latest_award_date,
            la_a.sba_ny_local_authority_bridge_run_ids,
            -- This emit's own provenance
            '{BRIDGE_RUN_ID}'                                  AS bridge_run_id,
            '{BRIDGE_VERSION}'                                 AS bridge_version,
            TIMESTAMP '{generated_at_iso}'                     AS generated_at
        FROM co
        LEFT JOIN nyc_agg na   ON co.sba_legal_name_normalized = na.sba_legal_name_normalized
        LEFT JOIN mta_agg ma   ON co.sba_legal_name_normalized = ma.sba_legal_name_normalized
        LEFT JOIN la_agg  la_a ON co.sba_legal_name_normalized = la_a.sba_legal_name_normalized
        """
    )

    rows_out = con.execute("SELECT COUNT(*) FROM enriched").fetchone()[0]
    rows_with_nyc = con.execute(
        "SELECT COUNT(*) FROM enriched WHERE nyc_contract_count IS NOT NULL"
    ).fetchone()[0]
    rows_with_mta = con.execute(
        "SELECT COUNT(*) FROM enriched WHERE mta_procurement_count IS NOT NULL"
    ).fetchone()[0]
    rows_with_la = con.execute(
        "SELECT COUNT(*) FROM enriched WHERE la_procurement_count IS NOT NULL"
    ).fetchone()[0]

    coverage_nyc_pct = (rows_with_nyc / rows_out * 100) if rows_out else 0.0
    coverage_mta_pct = (rows_with_mta / rows_out * 100) if rows_out else 0.0
    coverage_la_pct = (rows_with_la / rows_out * 100) if rows_out else 0.0

    sum_nyc = con.execute(
        "SELECT COALESCE(SUM(nyc_total_contract_amount), 0) FROM enriched"
    ).fetchone()[0]
    sum_mta = con.execute(
        "SELECT COALESCE(SUM(mta_total_contract_amount), 0) FROM enriched"
    ).fetchone()[0]
    sum_la = con.execute(
        "SELECT COALESCE(SUM(la_total_contract_amount), 0) FROM enriched"
    ).fetchone()[0]

    logger.info(
        "enriched: %d rows (nyc=%d/%.2f%% mta=%d/%.2f%% la=%d/%.2f%%; "
        "sum_nyc=$%s sum_mta=$%s sum_la=$%s)",
        rows_out,
        rows_with_nyc, coverage_nyc_pct,
        rows_with_mta, coverage_mta_pct,
        rows_with_la, coverage_la_pct,
        sum_nyc, sum_mta, sum_la,
    )

    if rows_out < MIN_ROWS_MATCHED:
        logger.error("HARD FAIL: rows=%d < MIN_ROWS_MATCHED=%d", rows_out, MIN_ROWS_MATCHED)
        return 1

    if rows_with_nyc < MIN_NYC_ENRICHED_FLOOR:
        logger.error(
            "HARD FAIL: NYC-enriched rows=%d < MIN_NYC_ENRICHED_FLOOR=%d",
            rows_with_nyc, MIN_NYC_ENRICHED_FLOOR,
        )
        return 1

    if rows_with_mta < MIN_MTA_ENRICHED_FLOOR:
        logger.error(
            "HARD FAIL: MTA-enriched rows=%d < MIN_MTA_ENRICHED_FLOOR=%d",
            rows_with_mta, MIN_MTA_ENRICHED_FLOOR,
        )
        return 1

    if rows_with_la < MIN_LA_ENRICHED_FLOOR:
        logger.error(
            "HARD FAIL: LA-enriched rows=%d < MIN_LA_ENRICHED_FLOOR=%d",
            rows_with_la, MIN_LA_ENRICHED_FLOOR,
        )
        return 1

    if not args.apply:
        logger.info(
            "DRY-RUN: would write %d rows (nyc=%.2f%% mta=%.2f%% la=%.2f%%). Pass --apply.",
            rows_out, coverage_nyc_pct, coverage_mta_pct, coverage_la_pct,
        )
        return 0

    # ---- Step 3: Lance write inside commit lock + BTREE ---- #

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        logger.info("writing Lance dataset to %s ...", OUTPUT_LANCE_URI)
        reader = con.from_query("SELECT * FROM enriched").to_arrow_reader(batch_size=100_000)
        ds = lance.write_dataset(
            reader, OUTPUT_LANCE_URI, mode="overwrite", storage_options=storage_options,
        )
        write_dur = time.time() - t0
        lance_count = ds.count_rows()
        logger.info("wrote %d rows in %.1fs (version=%s)", lance_count, write_dur, ds.version)

        try:
            ds.create_scalar_index("sba_legal_name_normalized", index_type="BTREE", replace=True)
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
        "OK: bridges.sba_ny_construction_full_enriched_lance written (%d rows; "
        "nyc=%.2f%% mta=%.2f%% la=%.2f%%; sum_nyc=$%s sum_mta=$%s sum_la=$%s; "
        "bridge_run_id=%s)",
        lance_count, coverage_nyc_pct, coverage_mta_pct, coverage_la_pct,
        sum_nyc, sum_mta, sum_la, BRIDGE_RUN_ID,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

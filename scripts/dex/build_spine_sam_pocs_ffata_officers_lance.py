#!/usr/bin/env python3
"""Pattern A spine: per-SAM-UEI people enrichment (POCs + FFATA officers).

Combines SAM entity-registration POCs (6 kinds — govt_bus, elec_bus, past_perf,
plus alt_* of each) with FFATA highly-compensated-officer disclosures aggregated
from contract + assistance subaward filings (USAspending).

Grain: one row per SAM UEI. Fixed-width columns for each POC kind (full_name,
title) plus pipe-delimited FFATA officer name/amount lists.

Inputs:
  sam_gov/entities_lance                       (~884K rows)
  usaspending/contract_subawards_lance         (~17K rows)
  usaspending/assistance_subawards_lance       (~54K rows)

Output: polaris-warehouse/spines/sam_pocs_ffata_officers_lance
Audit:  ops.data_sources (Pattern A — no method registration)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
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
logger = logging.getLogger("build_spine_sam_pocs_ffata_officers_lance")

SPINE_NAME = "sam_pocs_ffata_officers_lance"
SPINE_VERSION = "1.0.0"

SAM_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/entities_lance"
CSA_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/contract_subawards_lance"
ASA_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/assistance_subawards_lance"
SPINE_LANCE_URI = f"s3://dex-raw-landing-zone/polaris-warehouse/spines/{SPINE_NAME}"

MIN_ROWS_OUTPUT = 500_000  # ~884K SAM entities; floor catches catastrophic regression
TMP_DIR = "/tmp/lance"
DUCKDB_TMP_DIR = "/Users/benjamincrane/dex-build-tmp"


def _lance_storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _materialize_inputs(storage_options: dict):
    import lance

    logger.info("opening sam_gov/entities_lance ...")
    sam_ds = lance.dataset(SAM_LANCE_URI, storage_options=storage_options)
    sam_cols = [
        "unique_entity_id",
        "legal_business_name", "dba_name", "entity_url", "cage_code",
        "primary_naics", "naics_code_string", "bus_type_string", "entity_structure",
        "physical_address_state_normalized", "physical_address_city", "physical_address_zip5",
        # govt_bus
        "govt_bus_poc_first_name", "govt_bus_poc_middle_initial", "govt_bus_poc_last_name", "govt_bus_poc_title",
        # elec_bus
        "elec_bus_poc_first_name", "elec_bus_poc_middle_initial", "elec_bus_poc_last_name", "elec_bus_poc_title",
        # past_perf
        "past_perf_poc_poc_first_name", "past_perf_poc_poc_middle_initial", "past_perf_poc_poc_last_name", "past_perf_poc_poc_title",
        # alt_govt_bus
        "alt_govt_bus_poc_first_name", "alt_govt_bus_poc_middle_initial", "alt_govt_bus_poc_last_name", "alt_govt_bus_poc_title",
        # alt_elec_bus
        "alt_elec_poc_bus_poc_first_name", "alt_elec_poc_bus_poc_middle_initial", "alt_elec_poc_bus_poc_last_name", "alt_elec_poc_bus_poc_title",
        # alt_past_perf
        "alt_past_perf_poc_first_name", "alt_past_perf_poc_middle_initial", "alt_past_perf_poc_last_name", "alt_past_perf_poc_title",
    ]
    sam_tbl = sam_ds.scanner(columns=sam_cols).to_table()
    logger.info("  sam entities: %d rows", sam_tbl.num_rows)

    logger.info("opening usaspending/contract_subawards_lance ...")
    csa_cols = ["subawardee_uei"] + [
        f"subawardee_highly_compensated_officer_{i}_{f}" for i in (1, 2, 3, 4, 5) for f in ("name", "amount")
    ]
    csa_tbl = lance.dataset(CSA_LANCE_URI, storage_options=storage_options).scanner(columns=csa_cols).to_table()
    logger.info("  contract_subawards: %d rows", csa_tbl.num_rows)

    logger.info("opening usaspending/assistance_subawards_lance ...")
    asa_tbl = lance.dataset(ASA_LANCE_URI, storage_options=storage_options).scanner(columns=csa_cols).to_table()
    logger.info("  assistance_subawards: %d rows", asa_tbl.num_rows)

    return sam_tbl, csa_tbl, asa_tbl


def _build_spine(sam_tbl, csa_tbl, asa_tbl, *, bridge_run_id: str, generated_at_iso: str):
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    Path(DUCKDB_TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET memory_limit='24GB'")
    con.execute(f"SET temp_directory='{DUCKDB_TMP_DIR}'")
    con.execute("SET max_temp_directory_size='240GB'")
    con.execute("SET preserve_insertion_order=false")

    con.register("sam", sam_tbl)
    con.register("csa", csa_tbl)
    con.register("asa", asa_tbl)

    # ---- Step 1: unpivot subaward FFATA officer rows to one-row-per-(uei, slot, source) ----
    logger.info("step 1: unpivot FFATA officers from contract + assistance subawards ...")
    # contract side
    con.execute("""
      CREATE TEMP TABLE ffata_long_contract AS
      SELECT subawardee_uei AS uei,
        CAST(slot AS SMALLINT) AS slot,
        CAST(name AS VARCHAR) AS officer_name,
        CAST(amount AS DOUBLE) AS officer_amount,
        'contract' AS source_kind
      FROM csa,
      LATERAL (VALUES
        (1, subawardee_highly_compensated_officer_1_name, subawardee_highly_compensated_officer_1_amount),
        (2, subawardee_highly_compensated_officer_2_name, subawardee_highly_compensated_officer_2_amount),
        (3, subawardee_highly_compensated_officer_3_name, subawardee_highly_compensated_officer_3_amount),
        (4, subawardee_highly_compensated_officer_4_name, subawardee_highly_compensated_officer_4_amount),
        (5, subawardee_highly_compensated_officer_5_name, subawardee_highly_compensated_officer_5_amount)
      ) AS t(slot, name, amount)
      WHERE subawardee_uei IS NOT NULL AND subawardee_uei <> ''
        AND name IS NOT NULL AND trim(name) <> ''
    """)
    # assistance side
    con.execute("""
      CREATE TEMP TABLE ffata_long_assistance AS
      SELECT subawardee_uei AS uei,
        CAST(slot AS SMALLINT) AS slot,
        CAST(name AS VARCHAR) AS officer_name,
        CAST(amount AS DOUBLE) AS officer_amount,
        'assistance' AS source_kind
      FROM asa,
      LATERAL (VALUES
        (1, subawardee_highly_compensated_officer_1_name, subawardee_highly_compensated_officer_1_amount),
        (2, subawardee_highly_compensated_officer_2_name, subawardee_highly_compensated_officer_2_amount),
        (3, subawardee_highly_compensated_officer_3_name, subawardee_highly_compensated_officer_3_amount),
        (4, subawardee_highly_compensated_officer_4_name, subawardee_highly_compensated_officer_4_amount),
        (5, subawardee_highly_compensated_officer_5_name, subawardee_highly_compensated_officer_5_amount)
      ) AS t(slot, name, amount)
      WHERE subawardee_uei IS NOT NULL AND subawardee_uei <> ''
        AND name IS NOT NULL AND trim(name) <> ''
    """)
    n_c = con.execute("SELECT count(*), count(DISTINCT uei) FROM ffata_long_contract").fetchone()
    n_a = con.execute("SELECT count(*), count(DISTINCT uei) FROM ffata_long_assistance").fetchone()
    logger.info("  ffata_long_contract: rows=%d distinct_uei=%d", *n_c)
    logger.info("  ffata_long_assistance: rows=%d distinct_uei=%d", *n_a)

    # ---- Step 2: per-UEI aggregate FFATA officers ----
    logger.info("step 2: aggregate FFATA per UEI (dedupe officer-name across filings) ...")
    con.execute("""
      CREATE TEMP TABLE ffata_per_uei AS
      WITH unioned AS (
        SELECT * FROM ffata_long_contract UNION ALL SELECT * FROM ffata_long_assistance
      ),
      -- Dedupe to one row per (uei, normalized_officer_name); keep highest amount per officer
      per_officer AS (
        SELECT uei,
               trim(officer_name) AS officer_name,
               max(officer_amount) AS officer_amount,
               bool_or(source_kind='contract') AS in_contract,
               bool_or(source_kind='assistance') AS in_assistance,
               count(*) AS filing_count_for_officer
        FROM unioned WHERE officer_name IS NOT NULL AND trim(officer_name) <> ''
        GROUP BY 1, 2
      ),
      -- Per UEI: pipe-aggregate officer names (sorted by amount desc), distinct officer count
      per_uei AS (
        SELECT uei,
               string_agg(officer_name, '|' ORDER BY officer_amount DESC NULLS LAST) AS ffata_officer_names_pipe,
               string_agg(CAST(officer_amount AS VARCHAR), '|' ORDER BY officer_amount DESC NULLS LAST) AS ffata_officer_amounts_pipe,
               count(*) AS ffata_distinct_officer_count,
               bool_or(in_contract) AS ffata_observed_in_contracts,
               bool_or(in_assistance) AS ffata_observed_in_assistance
        FROM per_officer GROUP BY uei
      )
      SELECT * FROM per_uei
    """)
    n_ffata_uei = con.execute("SELECT count(*) FROM ffata_per_uei").fetchone()[0]
    logger.info("  ffata_per_uei: distinct UEIs with FFATA officers: %d", n_ffata_uei)

    # ---- Step 3: per-UEI filing counts ----
    logger.info("step 3: per-UEI subaward filing counts ...")
    con.execute("""
      CREATE TEMP TABLE filing_counts AS
      WITH c AS (SELECT subawardee_uei AS uei, count(*) AS contract_filings
                 FROM csa WHERE subawardee_uei <> '' GROUP BY 1),
           a AS (SELECT subawardee_uei AS uei, count(*) AS assistance_filings
                 FROM asa WHERE subawardee_uei <> '' GROUP BY 1)
      SELECT coalesce(c.uei, a.uei) AS uei,
             coalesce(c.contract_filings, 0) AS ffata_filing_count_contract,
             coalesce(a.assistance_filings, 0) AS ffata_filing_count_assistance
      FROM c FULL OUTER JOIN a USING (uei)
    """)

    # ---- Step 4: build the final spine ----
    logger.info("step 4: assemble spine ...")
    nz = "nullif(trim({c}), '')"
    full_name = lambda f, m, l: f"trim(coalesce({nz.format(c=f)},'') || ' ' || coalesce({nz.format(c=m)},'') || ' ' || coalesce({nz.format(c=l)},''))"

    con.execute(f"""
      CREATE TEMP TABLE spine AS
      SELECT
        sam.unique_entity_id AS sam_uei,
        sam.legal_business_name AS sam_legal_business_name,
        sam.dba_name AS sam_dba_name,
        sam.entity_url AS sam_entity_url,
        sam.cage_code AS sam_cage_code,
        sam.primary_naics AS sam_primary_naics,
        sam.naics_code_string AS sam_naics_code_string,
        sam.bus_type_string AS sam_bus_type_string,
        sam.entity_structure AS sam_entity_structure,
        sam.physical_address_state_normalized AS sam_physical_state,
        sam.physical_address_city AS sam_physical_city,
        sam.physical_address_zip5 AS sam_zip5,
        -- POCs (6 kinds × full_name + title)
        nullif({full_name('govt_bus_poc_first_name','govt_bus_poc_middle_initial','govt_bus_poc_last_name')}, '') AS govt_bus_poc_full_name,
        nullif(trim(sam.govt_bus_poc_title), '') AS govt_bus_poc_title,
        nullif({full_name('elec_bus_poc_first_name','elec_bus_poc_middle_initial','elec_bus_poc_last_name')}, '') AS elec_bus_poc_full_name,
        nullif(trim(sam.elec_bus_poc_title), '') AS elec_bus_poc_title,
        nullif({full_name('past_perf_poc_poc_first_name','past_perf_poc_poc_middle_initial','past_perf_poc_poc_last_name')}, '') AS past_perf_poc_full_name,
        nullif(trim(sam.past_perf_poc_poc_title), '') AS past_perf_poc_title,
        nullif({full_name('alt_govt_bus_poc_first_name','alt_govt_bus_poc_middle_initial','alt_govt_bus_poc_last_name')}, '') AS alt_govt_bus_poc_full_name,
        nullif(trim(sam.alt_govt_bus_poc_title), '') AS alt_govt_bus_poc_title,
        nullif({full_name('alt_elec_poc_bus_poc_first_name','alt_elec_poc_bus_poc_middle_initial','alt_elec_poc_bus_poc_last_name')}, '') AS alt_elec_bus_poc_full_name,
        nullif(trim(sam.alt_elec_poc_bus_poc_title), '') AS alt_elec_bus_poc_title,
        nullif({full_name('alt_past_perf_poc_first_name','alt_past_perf_poc_middle_initial','alt_past_perf_poc_last_name')}, '') AS alt_past_perf_poc_full_name,
        nullif(trim(sam.alt_past_perf_poc_title), '') AS alt_past_perf_poc_title,
        -- FFATA officer rollup
        ffata_per_uei.ffata_officer_names_pipe,
        ffata_per_uei.ffata_officer_amounts_pipe,
        coalesce(ffata_per_uei.ffata_distinct_officer_count, 0) AS ffata_distinct_officer_count,
        coalesce(ffata_per_uei.ffata_observed_in_contracts, FALSE) AS ffata_observed_in_contracts,
        coalesce(ffata_per_uei.ffata_observed_in_assistance, FALSE) AS ffata_observed_in_assistance,
        coalesce(filing_counts.ffata_filing_count_contract, 0) AS ffata_filing_count_contract,
        coalesce(filing_counts.ffata_filing_count_assistance, 0) AS ffata_filing_count_assistance,
        -- Convenience: has-any-poc and has-any-people flags
        (
          nullif({full_name('govt_bus_poc_first_name','govt_bus_poc_middle_initial','govt_bus_poc_last_name')}, '') IS NOT NULL OR
          nullif({full_name('elec_bus_poc_first_name','elec_bus_poc_middle_initial','elec_bus_poc_last_name')}, '') IS NOT NULL OR
          nullif({full_name('past_perf_poc_poc_first_name','past_perf_poc_poc_middle_initial','past_perf_poc_poc_last_name')}, '') IS NOT NULL
        ) AS has_any_poc,
        (ffata_per_uei.ffata_distinct_officer_count > 0) AS has_ffata_officers,
        '{SPINE_VERSION}' AS spine_version,
        '{bridge_run_id}' AS spine_run_id,
        TIMESTAMP '{generated_at_iso}' AS generated_at
      FROM sam
      LEFT JOIN ffata_per_uei ON sam.unique_entity_id = ffata_per_uei.uei
      LEFT JOIN filing_counts ON sam.unique_entity_id = filing_counts.uei
      WHERE sam.unique_entity_id IS NOT NULL
    """)
    n_spine = con.execute("SELECT count(*) FROM spine").fetchone()[0]
    logger.info("  spine rows: %d", n_spine)

    # Telemetry
    q = con.execute("""
      SELECT
        sum(CASE WHEN has_any_poc THEN 1 ELSE 0 END) AS w_poc,
        sum(CASE WHEN has_ffata_officers THEN 1 ELSE 0 END) AS w_ffata,
        sum(CASE WHEN has_any_poc OR has_ffata_officers THEN 1 ELSE 0 END) AS w_any
      FROM spine
    """).fetchone()
    logger.info("  UEIs with any POC: %d", q[0])
    logger.info("  UEIs with FFATA officers: %d", q[1])
    logger.info("  UEIs with any people enrichment: %d", q[2])

    return con, n_spine


def _write_spine_lance(con, storage_options: dict) -> int:
    import lance

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = TMP_DIR

    logger.info("materializing spine to Arrow in memory ...")
    arrow_tbl = con.execute("SELECT * FROM spine").fetch_arrow_table()
    logger.info("  materialized %d rows", arrow_tbl.num_rows)

    t0 = time.time()
    with lance_commit_lock(SPINE_NAME):
        logger.info("writing spine to Lance at %s ...", SPINE_LANCE_URI)
        ds = lance.write_dataset(
            arrow_tbl, SPINE_LANCE_URI, mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t0
        lance_count = ds.count_rows()
        logger.info("wrote %d rows in %.1fs (version=%s)", lance_count, write_dur, ds.version)

        os.environ["LANCE_BYPASS_SPILLING"] = "true"
        try:
            ds.create_scalar_index("sam_uei", index_type="BTREE", replace=True)
            logger.info("BTREE index created on sam_uei")
        except Exception as e:
            logger.warning("BTREE index on sam_uei failed (non-fatal): %s", e)
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
    ap = argparse.ArgumentParser(description=__doc__)
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true")
    grp.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")

    started_at = datetime.now(tz=timezone.utc)
    t0 = time.time()
    storage_options = _lance_storage_options()
    bridge_run_id = "00000000-0000-0000-0000-000000000000" if args.dry_run else str(__import__('uuid').uuid4())

    logger.info("spine: %s v%s  run_id=%s", SPINE_NAME, SPINE_VERSION, bridge_run_id)
    logger.info("output: %s", SPINE_LANCE_URI)

    try:
        sam_tbl, csa_tbl, asa_tbl = _materialize_inputs(storage_options)
        con, n_spine = _build_spine(
            sam_tbl, csa_tbl, asa_tbl,
            bridge_run_id=bridge_run_id,
            generated_at_iso=started_at.isoformat(),
        )

        if n_spine < MIN_ROWS_OUTPUT:
            logger.error("HARD FAIL: spine rows=%d < floor=%d", n_spine, MIN_ROWS_OUTPUT)
            return 1

        if args.dry_run:
            logger.info("DRY RUN OK — no Lance write. duration=%.1fs", time.time() - t0)
            return 0

        lance_count = _write_spine_lance(con, storage_options)
        logger.info("OK — spine_run_id=%s lance_rows=%d duration=%.1fs",
                    bridge_run_id, lance_count, time.time() - t0)
        return 0

    except Exception as exc:
        logger.exception("spine build FAILED: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

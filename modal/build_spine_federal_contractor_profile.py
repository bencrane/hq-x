"""Federal Contractor Profile — cross-MV Pattern A enriched-cohort emit.

The unified backbone (Build Step #1) for the 360-degree Federal Contract
Profile. One row per recipient_uei. Extends spines.federal_contractor_master
with parsed certifications, windowed metrics, FMCSA fleet/equipment, SBA
capital-access summary, sub-award participation, and agency/NAICS/PSC/POP
top-3 mix columns.

Pipeline (PyLance -> DuckDB local Parquet -> Lance upload, mirrors the
operator-approved pattern from build_federal_master_spine.py):

  1. PyLance scanners with column projection + scanner-side filters across:
       - spines/federal_contractor_master_lance         (anchor; 102,622)
       - usaspending/recipient_grain_lance              (cert booleans + windows)
       - spines/fmcsa_sam_carriers_lance                (UEI -> DOT bridge)
       - fmcsa/carrier_essentials_lance                 (fresh fleet metrics)
       - bridges/sam_sba_borrower_lance                 (SBA name bridge)
       - sba/borrowers_lance                            (SBA loan rollup)
       - sam_gov/entities_lance                         (exclusion flags)
       - usaspending/contracts_lance                    (top-3 mix aggregation)
       - usaspending/contract_subawards_lance           (sub-award counts)
       - usaspending/assistance_subawards_lance         (sub-award counts)

  2. DuckDB plan:
       (A) Aggregate contracts at (uei, agency) / (uei, naics) / (uei, psc)
           / (uei, pop_state) / (uei, set_aside), then ROW_NUMBER pick top 3.
       (B) Aggregate sub-awards twice: UEI as prime, UEI as sub (counts + $).
       (C) Latest snapshot of fmcsa carrier_essentials.
       (D) LEFT JOIN everything onto fed_master at uei grain.

  3. STAGE 1: COPY (...) TO '/tmp/federal_contractor_profile.parquet'
     STAGE 2: lance.write_dataset(reader, OUTPUT_URI, mode='overwrite')
              inside lance_commit_lock; BTREE on uei; compact + cleanup.

Discipline:
  - All USAspending VARCHAR numerics/dates go through TRY_CAST.
  - Top-N evidence emitted as discrete typed columns (top1_*, top2_*, top3_*)
    rather than LIST<STRUCT> — dodges Lance 1.5 definition-buffer cap.
  - confidence_tier from the SBA bridge propagates as sba_link_confidence.
  - spine_run_id (uuid) + spine_version + generated_at on every row.

Modal hosting: @app.function(cpu=8, memory=49152, timeout=14400).
MUST be launched via `modal run --detach` per CLAUDE.md (attached jobs > 5min
are killed by Modal CLI disconnect).

Run via (DETACH IS MANDATORY):
    cd ~/hq-all && \\
      doppler run --project hq-all --config prd -- \\
      modal run --detach apps/data-engine-x/modal/build_spine_federal_contractor_profile.py::run
"""
from __future__ import annotations

import logging
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import modal

app = modal.App("data-engine-x-federal-contractor-profile-spine-lance-emit")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "duckdb",
        "psycopg[binary]",
        "pylance>=0.20",
        "pyarrow>=16.0",
    )
    .add_local_dir(
        Path(__file__).resolve().parent.parent / "scripts" / "dex",
        remote_path="/root/scripts",
    )
)

FUNCTION_SECRETS = [
    modal.Secret.from_name("bulk-ingest-r2"),
    modal.Secret.from_name("hqx-db"),
]

DATASET_SLUG = "federal_contractor_profile_lance"
SPINE_VERSION = "1.0.0"

FED_MASTER_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/spines/federal_contractor_master_lance"
)
RECIPIENT_GRAIN_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/recipient_grain_lance"
)
FMCSA_SAM_BRIDGE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/spines/fmcsa_sam_carriers_lance"
)
FMCSA_CARRIER_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/fmcsa/carrier_essentials_lance"
)
SAM_SBA_BRIDGE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sam_sba_borrower_lance"
)
SBA_BORROWERS_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/sba/borrowers_lance"
)
SAM_ENTITIES_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/entities_lance"
)
USA_CONTRACTS_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/contracts_lance"
)
USA_CONTRACT_SUBS_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/contract_subawards_lance"
)
USA_ASSIST_SUBS_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/assistance_subawards_lance"
)
OUTPUT_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/spines/federal_contractor_profile_lance"
)

LOCAL_PARQUET_PATH = "/tmp/federal_contractor_profile.parquet"

# Floor: 0.95 × 101,413 distinct master UEIs = 96,343. LEFT-JOIN spine anchored
# on deduped master, so row count == distinct master UEI count (101,413 expected).
MIN_ROW_FLOOR = 96_343

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)


def _r2_account_id() -> str:
    return os.environ["R2_ENDPOINT"].split("//")[-1].split(".")[0]


def _storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _connect_duckdb():
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
        )
        """
    )
    con.execute("SET memory_limit='40GB'")
    con.execute("SET threads=8")
    con.execute("SET preserve_insertion_order=false")
    con.execute("SET temp_directory='/tmp/duckdb'")
    Path("/tmp/duckdb").mkdir(parents=True, exist_ok=True)
    return con


@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=14400,
    memory=49152,
    cpu=8,
)
def emit() -> dict:
    sys.path.insert(0, "/root")
    from scripts._lib.lance_commit_lock import lance_commit_lock

    os.environ["TMPDIR"] = "/tmp/lance"
    Path("/tmp/lance").mkdir(parents=True, exist_ok=True)
    os.environ["LANCE_BYPASS_SPILLING"] = "true"
    os.environ.setdefault("LANCE_INDEX_CACHE_SIZE", "1g")

    import lance
    import pyarrow.compute as pc

    storage_options = _storage_options()
    spine_run_id = str(uuid.uuid4())
    generated_at_dt = datetime.now(tz=timezone.utc)
    generated_at_iso = generated_at_dt.isoformat()
    logger.info(
        "spine_run_id=%s starting at %s (output=%s)",
        spine_run_id, generated_at_iso, OUTPUT_LANCE_URI,
    )

    # ── Step 1: PyLance scanners with column projection ──────────────────── #
    logger.info("opening spines/federal_contractor_master_lance ...")
    t_open = time.time()
    master_ds = lance.dataset(FED_MASTER_URI, storage_options=storage_options)
    master_arrow = master_ds.scanner(
        columns=[
            "uei",
            "legal_business_name", "legal_business_name_normalized", "dba_name",
            "entity_url", "cage_code",
            "primary_naics", "naics_primary_2digit", "naics_code_string",
            "entity_structure", "state_of_incorporation",
            "physical_address_city", "physical_address_state_normalized", "physical_address_zip5",
            "registration_expiration_date", "last_update_date",
            "bus_type_string", "sba_business_types_string",
            "lifetime_contract_count", "lifetime_federal_action_obligation", "lifetime_total_obligated",
            "active_contract_count", "active_total_obligated",
            "max_period_of_performance_end_date", "has_active_award", "latest_action_date",
            "elec_bus_full_name_normalized", "elec_bus_title",
            "elec_bus_city", "elec_bus_state_or_province", "elec_bus_zippostal_code",
            "govt_bus_full_name_normalized", "govt_bus_title",
            "govt_bus_city", "govt_bus_state_or_province", "govt_bus_zippostal_code",
        ],
    ).to_table()
    logger.info("  master: %d rows (%.1fs)", master_arrow.num_rows, time.time() - t_open)

    logger.info("opening usaspending/recipient_grain_lance ...")
    t_open = time.time()
    grain_ds = lance.dataset(RECIPIENT_GRAIN_URI, storage_options=storage_options)
    grain_arrow = grain_ds.scanner(
        columns=[
            "recipient_uei",
            "total_obligation_30d", "total_obligation_90d",
            "total_obligation_180d", "total_obligation_365d",
            "contract_count_30d", "contract_count_90d",
            "contract_count_180d", "contract_count_365d",
            "top_psc",
            "is_8a", "is_hubzone", "is_wosb", "is_edwosb",
            "is_sdvosb", "is_vosb", "is_sdb",
            "is_minority_owned", "is_native_american_owned",
            "is_alaskan_native_corp", "is_native_hawaiian_org", "is_tribal_corp",
            "is_nonprofit", "is_educational", "is_jv",
        ],
    ).to_table()
    logger.info("  recipient_grain: %d rows (%.1fs)", grain_arrow.num_rows, time.time() - t_open)

    logger.info("opening spines/fmcsa_sam_carriers_lance ...")
    t_open = time.time()
    fmcsa_bridge_ds = lance.dataset(FMCSA_SAM_BRIDGE_URI, storage_options=storage_options)
    fmcsa_bridge_arrow = fmcsa_bridge_ds.scanner(
        columns=["uei", "dot_number", "fleet_size"],
        filter=pc.field("uei").is_valid(),
    ).to_table()
    logger.info("  fmcsa_sam_carriers: %d rows (%.1fs)", fmcsa_bridge_arrow.num_rows, time.time() - t_open)

    logger.info("opening fmcsa/carrier_essentials_lance (latest snapshot only) ...")
    t_open = time.time()
    carrier_ds = lance.dataset(FMCSA_CARRIER_URI, storage_options=storage_options)
    # First find max snapshot date — small scan over snapshot col only.
    snap_table = carrier_ds.scanner(columns=["snapshot"]).to_table()
    max_snap = pc.max(snap_table["snapshot"]).as_py()
    logger.info("  fmcsa latest snapshot: %s", max_snap)
    carrier_arrow = carrier_ds.scanner(
        columns=[
            "dot_number", "snapshot",
            "fleetsize_int",
            "power_units_int", "total_drivers_int", "mcs150_mileage_int",
            "hm_ind", "safety_rating",
            "operating_radius_class", "specialty_class", "fleet_bucket",
        ],
        filter=(pc.field("snapshot") == max_snap),
    ).to_table()
    logger.info("  fmcsa carrier_essentials (snapshot): %d rows (%.1fs)", carrier_arrow.num_rows, time.time() - t_open)

    logger.info("opening bridges/sam_sba_borrower_lance ...")
    t_open = time.time()
    sba_bridge_ds = lance.dataset(SAM_SBA_BRIDGE_URI, storage_options=storage_options)
    sba_bridge_arrow = sba_bridge_ds.scanner(
        columns=["sam_uei", "sba_name_normalized", "sba_state", "confidence_tier"],
        filter=pc.field("sam_uei").is_valid(),
    ).to_table()
    logger.info("  sam_sba_borrower bridge: %d rows (%.1fs)", sba_bridge_arrow.num_rows, time.time() - t_open)

    logger.info("opening sba/borrowers_lance ...")
    t_open = time.time()
    sba_borr_ds = lance.dataset(SBA_BORROWERS_URI, storage_options=storage_options)
    sba_borr_arrow = sba_borr_ds.scanner(
        columns=[
            "legal_name_normalized", "borrstate",
            "total_loans", "total_gross_approval",
            "max_approval_date", "min_approval_date",
            "latest_loanstatus",
        ],
    ).to_table()
    logger.info("  sba borrowers: %d rows (%.1fs)", sba_borr_arrow.num_rows, time.time() - t_open)

    logger.info("opening sam_gov/entities_lance (exclusion flags only) ...")
    t_open = time.time()
    sam_ent_ds = lance.dataset(SAM_ENTITIES_URI, storage_options=storage_options)
    sam_ent_arrow = sam_ent_ds.scanner(
        columns=["unique_entity_id", "exclusion_status_flag", "debt_subject_to_offset_flag"],
        filter=pc.field("unique_entity_id").is_valid(),
    ).to_table()
    logger.info("  sam entities (flags): %d rows (%.1fs)", sam_ent_arrow.num_rows, time.time() - t_open)

    logger.info("opening usaspending/contracts_lance (slim 11-col projection) ...")
    t_open = time.time()
    usa_ds = lance.dataset(USA_CONTRACTS_URI, storage_options=storage_options)
    usa_arrow = usa_ds.scanner(
        columns=[
            "recipient_uei",
            "federal_action_obligation",
            "awarding_agency_name",
            "naics_code", "naics_description",
            "product_or_service_code", "product_or_service_code_description",
            "primary_place_of_performance_state_code",
            "type_of_set_aside_code", "type_of_set_aside",
        ],
        filter="recipient_uei IS NOT NULL AND recipient_uei != ''",
    ).to_table()
    logger.info("  contracts (slim): %d rows (%.1fs)", usa_arrow.num_rows, time.time() - t_open)

    logger.info("opening sub-award tables ...")
    t_open = time.time()
    csub_ds = lance.dataset(USA_CONTRACT_SUBS_URI, storage_options=storage_options)
    csub_arrow = csub_ds.scanner(
        columns=["prime_awardee_uei", "subawardee_uei", "subaward_amount"],
    ).to_table()
    asub_ds = lance.dataset(USA_ASSIST_SUBS_URI, storage_options=storage_options)
    asub_arrow = asub_ds.scanner(
        columns=["prime_awardee_uei", "subawardee_uei", "subaward_amount"],
    ).to_table()
    logger.info("  contract_subs: %d  assist_subs: %d  (%.1fs)",
                csub_arrow.num_rows, asub_arrow.num_rows, time.time() - t_open)

    # ── Step 2: DuckDB plan ───────────────────────────────────────────────── #
    con = _connect_duckdb()
    con.register("master",        master_arrow)
    con.register("grain",         grain_arrow)
    con.register("fmcsa_bridge",  fmcsa_bridge_arrow)
    con.register("carrier_fresh", carrier_arrow)
    con.register("sba_bridge",    sba_bridge_arrow)
    con.register("sba_borr",      sba_borr_arrow)
    con.register("sam_ent",       sam_ent_arrow)
    con.register("usa",           usa_arrow)
    con.register("csub",          csub_arrow)
    con.register("asub",          asub_arrow)

    # 2a. Type-cast usaspending contracts and INNER JOIN to master UEIs.
    logger.info("step 2a: cast + join contracts to master UEIs ...")
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE usa_typed AS
        SELECT
            usa.recipient_uei                                          AS uei,
            TRY_CAST(usa.federal_action_obligation AS DOUBLE)          AS obligation_usd,
            usa.awarding_agency_name,
            usa.naics_code,
            usa.naics_description,
            usa.product_or_service_code                                AS psc_code,
            usa.product_or_service_code_description                    AS psc_description,
            usa.primary_place_of_performance_state_code                AS pop_state_code,
            usa.type_of_set_aside_code                                 AS set_aside_code,
            usa.type_of_set_aside                                      AS set_aside_label
        FROM usa
        INNER JOIN master m ON m.uei = usa.recipient_uei
        WHERE TRY_CAST(usa.federal_action_obligation AS DOUBLE) > 0
        """
    )
    typed_rows = con.execute("SELECT COUNT(*) FROM usa_typed").fetchone()[0]
    logger.info("  usa_typed: %d transactions", typed_rows)

    # 2b. Top-3 agency mix per UEI.
    logger.info("step 2b: top-3 agency mix per UEI ...")
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE agency_top3 AS
        WITH per_agency AS (
            SELECT uei, awarding_agency_name AS name, SUM(obligation_usd) AS obligation
            FROM usa_typed
            WHERE awarding_agency_name IS NOT NULL AND awarding_agency_name != ''
            GROUP BY 1, 2
        ),
        ranked AS (
            SELECT *,
                ROW_NUMBER() OVER (PARTITION BY uei ORDER BY obligation DESC NULLS LAST, name ASC) AS rn,
                SUM(obligation)   OVER (PARTITION BY uei) AS uei_total
            FROM per_agency
        )
        SELECT
            uei,
            MAX(CASE WHEN rn=1 THEN name END)                                          AS agency_top1_name,
            MAX(CASE WHEN rn=1 THEN obligation END)                                    AS agency_top1_obligation_usd,
            MAX(CASE WHEN rn=1 THEN obligation/NULLIF(uei_total,0) END)                AS agency_top1_share,
            MAX(CASE WHEN rn=2 THEN name END)                                          AS agency_top2_name,
            MAX(CASE WHEN rn=2 THEN obligation END)                                    AS agency_top2_obligation_usd,
            MAX(CASE WHEN rn=2 THEN obligation/NULLIF(uei_total,0) END)                AS agency_top2_share,
            MAX(CASE WHEN rn=3 THEN name END)                                          AS agency_top3_name,
            MAX(CASE WHEN rn=3 THEN obligation END)                                    AS agency_top3_obligation_usd,
            MAX(CASE WHEN rn=3 THEN obligation/NULLIF(uei_total,0) END)                AS agency_top3_share
        FROM ranked
        WHERE rn <= 3
        GROUP BY 1
        """
    )
    logger.info("  agency_top3: %d UEIs",
                con.execute("SELECT COUNT(*) FROM agency_top3").fetchone()[0])

    # 2c. Top-3 NAICS mix per UEI.
    logger.info("step 2c: top-3 NAICS mix per UEI ...")
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE naics_top3 AS
        WITH per_naics AS (
            SELECT uei,
                   naics_code           AS code,
                   ANY_VALUE(naics_description) AS description,
                   SUM(obligation_usd)  AS obligation
            FROM usa_typed
            WHERE naics_code IS NOT NULL AND naics_code != ''
            GROUP BY 1, 2
        ),
        ranked AS (
            SELECT *,
                ROW_NUMBER() OVER (PARTITION BY uei ORDER BY obligation DESC NULLS LAST, code ASC) AS rn,
                SUM(obligation)   OVER (PARTITION BY uei) AS uei_total
            FROM per_naics
        )
        SELECT
            uei,
            MAX(CASE WHEN rn=1 THEN code END)                                         AS naics_top1_code,
            MAX(CASE WHEN rn=1 THEN description END)                                  AS naics_top1_description,
            MAX(CASE WHEN rn=1 THEN obligation END)                                   AS naics_top1_obligation_usd,
            MAX(CASE WHEN rn=1 THEN obligation/NULLIF(uei_total,0) END)               AS naics_top1_share,
            MAX(CASE WHEN rn=2 THEN code END)                                         AS naics_top2_code,
            MAX(CASE WHEN rn=2 THEN description END)                                  AS naics_top2_description,
            MAX(CASE WHEN rn=2 THEN obligation END)                                   AS naics_top2_obligation_usd,
            MAX(CASE WHEN rn=2 THEN obligation/NULLIF(uei_total,0) END)               AS naics_top2_share,
            MAX(CASE WHEN rn=3 THEN code END)                                         AS naics_top3_code,
            MAX(CASE WHEN rn=3 THEN description END)                                  AS naics_top3_description,
            MAX(CASE WHEN rn=3 THEN obligation END)                                   AS naics_top3_obligation_usd,
            MAX(CASE WHEN rn=3 THEN obligation/NULLIF(uei_total,0) END)               AS naics_top3_share
        FROM ranked
        WHERE rn <= 3
        GROUP BY 1
        """
    )
    logger.info("  naics_top3: %d UEIs",
                con.execute("SELECT COUNT(*) FROM naics_top3").fetchone()[0])

    # 2d. Top-3 PSC mix per UEI.
    logger.info("step 2d: top-3 PSC mix per UEI ...")
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE psc_top3 AS
        WITH per_psc AS (
            SELECT uei,
                   psc_code             AS code,
                   ANY_VALUE(psc_description) AS description,
                   SUM(obligation_usd)  AS obligation
            FROM usa_typed
            WHERE psc_code IS NOT NULL AND psc_code != ''
            GROUP BY 1, 2
        ),
        ranked AS (
            SELECT *,
                ROW_NUMBER() OVER (PARTITION BY uei ORDER BY obligation DESC NULLS LAST, code ASC) AS rn,
                SUM(obligation)   OVER (PARTITION BY uei) AS uei_total
            FROM per_psc
        )
        SELECT
            uei,
            MAX(CASE WHEN rn=1 THEN code END)                                         AS psc_top1_code,
            MAX(CASE WHEN rn=1 THEN description END)                                  AS psc_top1_description,
            MAX(CASE WHEN rn=1 THEN obligation END)                                   AS psc_top1_obligation_usd,
            MAX(CASE WHEN rn=1 THEN obligation/NULLIF(uei_total,0) END)               AS psc_top1_share,
            MAX(CASE WHEN rn=2 THEN code END)                                         AS psc_top2_code,
            MAX(CASE WHEN rn=2 THEN description END)                                  AS psc_top2_description,
            MAX(CASE WHEN rn=2 THEN obligation END)                                   AS psc_top2_obligation_usd,
            MAX(CASE WHEN rn=2 THEN obligation/NULLIF(uei_total,0) END)               AS psc_top2_share,
            MAX(CASE WHEN rn=3 THEN code END)                                         AS psc_top3_code,
            MAX(CASE WHEN rn=3 THEN description END)                                  AS psc_top3_description,
            MAX(CASE WHEN rn=3 THEN obligation END)                                   AS psc_top3_obligation_usd,
            MAX(CASE WHEN rn=3 THEN obligation/NULLIF(uei_total,0) END)               AS psc_top3_share
        FROM ranked
        WHERE rn <= 3
        GROUP BY 1
        """
    )
    logger.info("  psc_top3: %d UEIs",
                con.execute("SELECT COUNT(*) FROM psc_top3").fetchone()[0])

    # 2e. Top-3 POP-state mix per UEI.
    logger.info("step 2e: top-3 POP-state mix per UEI ...")
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE pop_state_top3 AS
        WITH per_state AS (
            SELECT uei, pop_state_code AS code, SUM(obligation_usd) AS obligation
            FROM usa_typed
            WHERE pop_state_code IS NOT NULL AND pop_state_code != ''
            GROUP BY 1, 2
        ),
        ranked AS (
            SELECT *,
                ROW_NUMBER() OVER (PARTITION BY uei ORDER BY obligation DESC NULLS LAST, code ASC) AS rn,
                SUM(obligation)   OVER (PARTITION BY uei) AS uei_total
            FROM per_state
        )
        SELECT
            uei,
            MAX(CASE WHEN rn=1 THEN code END)                                         AS pop_state_top1_code,
            MAX(CASE WHEN rn=1 THEN obligation END)                                   AS pop_state_top1_obligation_usd,
            MAX(CASE WHEN rn=1 THEN obligation/NULLIF(uei_total,0) END)               AS pop_state_top1_share,
            MAX(CASE WHEN rn=2 THEN code END)                                         AS pop_state_top2_code,
            MAX(CASE WHEN rn=2 THEN obligation END)                                   AS pop_state_top2_obligation_usd,
            MAX(CASE WHEN rn=2 THEN obligation/NULLIF(uei_total,0) END)               AS pop_state_top2_share,
            MAX(CASE WHEN rn=3 THEN code END)                                         AS pop_state_top3_code,
            MAX(CASE WHEN rn=3 THEN obligation END)                                   AS pop_state_top3_obligation_usd,
            MAX(CASE WHEN rn=3 THEN obligation/NULLIF(uei_total,0) END)               AS pop_state_top3_share
        FROM ranked
        WHERE rn <= 3
        GROUP BY 1
        """
    )
    logger.info("  pop_state_top3: %d UEIs",
                con.execute("SELECT COUNT(*) FROM pop_state_top3").fetchone()[0])

    # 2f. Set-aside funnel per UEI (full mix; emit top-3).
    logger.info("step 2f: top-3 set-aside mix per UEI ...")
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE set_aside_top3 AS
        WITH per_sa AS (
            SELECT uei,
                   set_aside_code              AS code,
                   ANY_VALUE(set_aside_label)  AS label,
                   SUM(obligation_usd)         AS obligation,
                   COUNT(*)                    AS action_count
            FROM usa_typed
            WHERE set_aside_code IS NOT NULL AND set_aside_code != ''
            GROUP BY 1, 2
        ),
        ranked AS (
            SELECT *,
                ROW_NUMBER() OVER (PARTITION BY uei ORDER BY obligation DESC NULLS LAST, code ASC) AS rn
            FROM per_sa
        )
        SELECT
            uei,
            MAX(CASE WHEN rn=1 THEN code END)         AS set_aside_top1_code,
            MAX(CASE WHEN rn=1 THEN label END)        AS set_aside_top1_label,
            MAX(CASE WHEN rn=1 THEN obligation END)   AS set_aside_top1_obligation_usd,
            MAX(CASE WHEN rn=1 THEN action_count END) AS set_aside_top1_action_count,
            MAX(CASE WHEN rn=2 THEN code END)         AS set_aside_top2_code,
            MAX(CASE WHEN rn=2 THEN label END)        AS set_aside_top2_label,
            MAX(CASE WHEN rn=2 THEN obligation END)   AS set_aside_top2_obligation_usd,
            MAX(CASE WHEN rn=2 THEN action_count END) AS set_aside_top2_action_count,
            MAX(CASE WHEN rn=3 THEN code END)         AS set_aside_top3_code,
            MAX(CASE WHEN rn=3 THEN label END)        AS set_aside_top3_label,
            MAX(CASE WHEN rn=3 THEN obligation END)   AS set_aside_top3_obligation_usd,
            MAX(CASE WHEN rn=3 THEN action_count END) AS set_aside_top3_action_count
        FROM ranked
        WHERE rn <= 3
        GROUP BY 1
        """
    )
    logger.info("  set_aside_top3: %d UEIs",
                con.execute("SELECT COUNT(*) FROM set_aside_top3").fetchone()[0])

    # 2g. Sub-award participation: UEI as prime vs UEI as sub, contract + assistance.
    logger.info("step 2g: subaward counts (as prime / as sub) ...")
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE subaward_rollup AS
        WITH all_subs AS (
            SELECT prime_awardee_uei, subawardee_uei, COALESCE(subaward_amount, 0) AS amt
            FROM csub
            UNION ALL
            SELECT prime_awardee_uei, subawardee_uei, COALESCE(subaward_amount, 0) AS amt
            FROM asub
        ),
        as_prime AS (
            SELECT prime_awardee_uei AS uei,
                   COUNT(*) AS subaward_as_prime_count,
                   SUM(amt) AS subaward_as_prime_total_distributed_usd
            FROM all_subs
            WHERE prime_awardee_uei IS NOT NULL AND prime_awardee_uei != ''
            GROUP BY 1
        ),
        as_sub AS (
            SELECT subawardee_uei AS uei,
                   COUNT(*) AS subaward_as_sub_count,
                   SUM(amt) AS subaward_as_sub_total_received_usd
            FROM all_subs
            WHERE subawardee_uei IS NOT NULL AND subawardee_uei != ''
            GROUP BY 1
        )
        SELECT
            COALESCE(p.uei, s.uei) AS uei,
            COALESCE(p.subaward_as_prime_count, 0)                       AS subaward_as_prime_count,
            COALESCE(p.subaward_as_prime_total_distributed_usd, 0)       AS subaward_as_prime_total_distributed_usd,
            COALESCE(s.subaward_as_sub_count, 0)                         AS subaward_as_sub_count,
            COALESCE(s.subaward_as_sub_total_received_usd, 0)            AS subaward_as_sub_total_received_usd
        FROM as_prime p
        FULL OUTER JOIN as_sub s ON s.uei = p.uei
        """
    )
    logger.info("  subaward_rollup: %d UEIs",
                con.execute("SELECT COUNT(*) FROM subaward_rollup").fetchone()[0])

    # 2h. FMCSA: bridge UEI -> dot_number -> fresh carrier_essentials.
    #     A UEI may map to multiple DOTs (fleet of subsidiary carriers); take the
    #     DOT with the largest fleetsize_int as the "primary" carrier.
    logger.info("step 2h: FMCSA primary carrier per UEI ...")
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE fmcsa_per_uei AS
        WITH joined AS (
            SELECT
                b.uei,
                b.dot_number,
                COALESCE(c.fleetsize_int, b.fleet_size)                     AS fleet_size,   /* prefer carrier_essentials.fleetsize_int (#749), fall back to bridge */
                c.power_units_int,
                c.total_drivers_int,
                c.mcs150_mileage_int,
                c.hm_ind,
                c.safety_rating,
                c.operating_radius_class,
                c.specialty_class,
                c.fleet_bucket
            FROM fmcsa_bridge b
            LEFT JOIN carrier_fresh c ON c.dot_number = b.dot_number
        ),
        ranked AS (
            SELECT *,
                ROW_NUMBER() OVER (PARTITION BY uei
                                   ORDER BY COALESCE(fleet_size, 0) DESC NULLS LAST,
                                            dot_number ASC) AS rn,
                COUNT(*)                  OVER (PARTITION BY uei) AS dot_count,
                SUM(COALESCE(fleet_size,        0)) OVER (PARTITION BY uei) AS total_fleetsize_across_dots,
                SUM(COALESCE(power_units_int,   0)) OVER (PARTITION BY uei) AS total_power_units_across_dots,
                SUM(COALESCE(total_drivers_int, 0)) OVER (PARTITION BY uei) AS total_drivers_across_dots
            FROM joined
        )
        SELECT
            uei,
            dot_number                       AS primary_dot_number,
            dot_count                        AS dot_number_count,
            fleet_size                       AS primary_fleet_size,
            power_units_int                  AS primary_power_units,
            total_drivers_int                AS primary_total_drivers,
            mcs150_mileage_int               AS primary_mcs150_mileage,
            hm_ind                           AS primary_hazmat_indicator,
            safety_rating                    AS primary_safety_rating,
            operating_radius_class           AS primary_operating_radius_class,
            specialty_class                  AS primary_specialty_class,
            fleet_bucket                     AS primary_fleet_bucket,
            total_fleetsize_across_dots,
            total_power_units_across_dots,
            total_drivers_across_dots
        FROM ranked
        WHERE rn = 1
        """
    )
    logger.info("  fmcsa_per_uei: %d UEIs",
                con.execute("SELECT COUNT(*) FROM fmcsa_per_uei").fetchone()[0])

    # 2i. SBA capital-access rollup per UEI via name bridge.
    logger.info("step 2i: SBA capital rollup per UEI ...")
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE sba_per_uei AS
        WITH joined AS (
            SELECT
                b.sam_uei                                                AS uei,
                b.confidence_tier                                        AS link_confidence,
                bo.total_loans                                           AS borrower_total_loans,
                bo.total_gross_approval                                  AS borrower_total_gross_approval,
                bo.max_approval_date                                     AS borrower_max_approval_date,
                bo.min_approval_date                                     AS borrower_min_approval_date,
                bo.latest_loanstatus                                     AS borrower_latest_loanstatus
            FROM sba_bridge b
            INNER JOIN sba_borr bo
              ON bo.legal_name_normalized = b.sba_name_normalized
             AND bo.borrstate             = b.sba_state
        ),
        agg AS (
            SELECT
                uei,
                /* Highest available bridge tier. Bridge native tiers are
                   platinum > gold > silver (verified against live data
                   2026-05-25: 158,120 platinum / 157,674 gold / 5,146 silver). */
                MAX(CASE link_confidence WHEN 'platinum' THEN 3
                                        WHEN 'gold'     THEN 2
                                        WHEN 'silver'   THEN 1
                                        ELSE 0 END)                     AS conf_rank,
                SUM(COALESCE(borrower_total_loans, 0))                  AS sba_lifetime_loan_count,
                SUM(COALESCE(borrower_total_gross_approval, 0))         AS sba_lifetime_gross_approval_usd,
                MIN(borrower_min_approval_date)                         AS sba_earliest_loan_date,
                MAX(borrower_max_approval_date)                         AS sba_latest_loan_date,
                ANY_VALUE(borrower_latest_loanstatus)                   AS sba_latest_loan_status
            FROM joined
            GROUP BY 1
        )
        SELECT
            uei,
            CASE conf_rank WHEN 3 THEN 'platinum'
                           WHEN 2 THEN 'gold'
                           WHEN 1 THEN 'silver'
                           ELSE NULL END                                AS sba_link_confidence,
            sba_lifetime_loan_count,
            sba_lifetime_gross_approval_usd,
            sba_earliest_loan_date,
            sba_latest_loan_date,
            sba_latest_loan_status
        FROM agg
        """
    )
    logger.info("  sba_per_uei: %d UEIs",
                con.execute("SELECT COUNT(*) FROM sba_per_uei").fetchone()[0])

    # 2j. SAM exclusion flags per UEI (deduped — SAM has snapshot fan-out).
    logger.info("step 2j: SAM exclusion flags per UEI (deduped) ...")
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE sam_flags AS
        SELECT
            unique_entity_id                                                       AS uei,
            BOOL_OR(UPPER(COALESCE(exclusion_status_flag, '')) = 'Y')              AS is_excluded,
            BOOL_OR(UPPER(COALESCE(debt_subject_to_offset_flag, '')) = 'Y')        AS has_debt_subject_to_offset
        FROM sam_ent
        GROUP BY 1
        """
    )

    # 2k. Dedupe master at UEI grain — upstream master_lance has 1,209 dup UEIs
    #     (102,622 rows / 101,413 distinct). The profile backbone is a per-UEI
    #     lookup substrate; one row per UEI is the contract. Pick the row with
    #     the latest last_update_date (most recently refreshed SAM snapshot).
    logger.info("step 2k: dedupe master at UEI grain ...")
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE master_dedup AS
        SELECT * EXCLUDE (rn)
        FROM (
            SELECT m.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY uei
                       ORDER BY TRY_CAST(last_update_date AS DATE) DESC NULLS LAST,
                                TRY_CAST(registration_expiration_date AS DATE) DESC NULLS LAST
                   ) AS rn
            FROM master m
        )
        WHERE rn = 1
        """
    )
    master_rows = con.execute("SELECT COUNT(*) FROM master_dedup").fetchone()[0]
    logger.info("  master_dedup: %d UEIs (down from %d source rows)",
                master_rows, master_arrow.num_rows)

    # ── STAGE 1: COPY → local Parquet ─────────────────────────────────────── #
    logger.info("STAGE 1 START: COPY → %s", LOCAL_PARQUET_PATH)
    t_stage1 = time.time()
    con.execute(
        f"""
        COPY (
            SELECT
                m.uei,

                /* identity */
                m.legal_business_name,
                m.legal_business_name_normalized,
                m.dba_name,
                m.entity_url,
                m.cage_code,
                m.primary_naics,
                m.naics_primary_2digit,
                m.naics_code_string,
                m.entity_structure,
                m.state_of_incorporation,
                m.physical_address_city,
                m.physical_address_state_normalized,
                m.physical_address_zip5,
                TRY_CAST(m.registration_expiration_date AS DATE)        AS registration_expiration_date,
                TRY_CAST(m.last_update_date AS DATE)                    AS last_update_date,
                m.bus_type_string,
                m.sba_business_types_string,
                COALESCE(sf.is_excluded, FALSE)                         AS is_excluded,
                COALESCE(sf.has_debt_subject_to_offset, FALSE)          AS has_debt_subject_to_offset,

                /* prime contract lifetime + active rollups (from master) */
                m.lifetime_contract_count,
                m.lifetime_federal_action_obligation                    AS lifetime_federal_action_obligation_usd,
                m.lifetime_total_obligated                              AS lifetime_total_obligated_usd,
                m.active_contract_count,
                m.active_total_obligated                                AS active_total_obligated_usd,
                m.max_period_of_performance_end_date,
                m.has_active_award,
                m.latest_action_date,

                /* windowed metrics (from recipient_grain) */
                g.total_obligation_30d                                  AS obligation_30d_usd,
                g.total_obligation_90d                                  AS obligation_90d_usd,
                g.total_obligation_180d                                 AS obligation_180d_usd,
                g.total_obligation_365d                                 AS obligation_365d_usd,
                g.contract_count_30d,
                g.contract_count_90d,
                g.contract_count_180d,
                g.contract_count_365d,
                g.top_psc                                               AS recipient_grain_top_psc,

                /* certification booleans (from recipient_grain) */
                CASE WHEN g.recipient_uei IS NOT NULL
                     THEN 'recipient_grain' ELSE NULL END               AS cert_source,
                g.is_8a,
                g.is_hubzone,
                g.is_wosb,
                g.is_edwosb,
                g.is_sdvosb,
                g.is_vosb,
                g.is_sdb,
                g.is_minority_owned,
                g.is_native_american_owned,
                g.is_alaskan_native_corp,
                g.is_native_hawaiian_org,
                g.is_tribal_corp,
                g.is_nonprofit,
                g.is_educational,
                g.is_jv,

                /* top-3 agency mix */
                a3.agency_top1_name, a3.agency_top1_obligation_usd, a3.agency_top1_share,
                a3.agency_top2_name, a3.agency_top2_obligation_usd, a3.agency_top2_share,
                a3.agency_top3_name, a3.agency_top3_obligation_usd, a3.agency_top3_share,

                /* top-3 NAICS mix */
                n3.naics_top1_code, n3.naics_top1_description, n3.naics_top1_obligation_usd, n3.naics_top1_share,
                n3.naics_top2_code, n3.naics_top2_description, n3.naics_top2_obligation_usd, n3.naics_top2_share,
                n3.naics_top3_code, n3.naics_top3_description, n3.naics_top3_obligation_usd, n3.naics_top3_share,

                /* top-3 PSC mix */
                p3.psc_top1_code, p3.psc_top1_description, p3.psc_top1_obligation_usd, p3.psc_top1_share,
                p3.psc_top2_code, p3.psc_top2_description, p3.psc_top2_obligation_usd, p3.psc_top2_share,
                p3.psc_top3_code, p3.psc_top3_description, p3.psc_top3_obligation_usd, p3.psc_top3_share,

                /* top-3 POP-state mix */
                s3.pop_state_top1_code, s3.pop_state_top1_obligation_usd, s3.pop_state_top1_share,
                s3.pop_state_top2_code, s3.pop_state_top2_obligation_usd, s3.pop_state_top2_share,
                s3.pop_state_top3_code, s3.pop_state_top3_obligation_usd, s3.pop_state_top3_share,

                /* top-3 set-aside funnel */
                sa3.set_aside_top1_code, sa3.set_aside_top1_label,
                  sa3.set_aside_top1_obligation_usd, sa3.set_aside_top1_action_count,
                sa3.set_aside_top2_code, sa3.set_aside_top2_label,
                  sa3.set_aside_top2_obligation_usd, sa3.set_aside_top2_action_count,
                sa3.set_aside_top3_code, sa3.set_aside_top3_label,
                  sa3.set_aside_top3_obligation_usd, sa3.set_aside_top3_action_count,

                /* sub-award participation */
                COALESCE(sub.subaward_as_prime_count, 0)                          AS subaward_as_prime_count,
                COALESCE(sub.subaward_as_prime_total_distributed_usd, 0.0)        AS subaward_as_prime_total_distributed_usd,
                COALESCE(sub.subaward_as_sub_count, 0)                            AS subaward_as_sub_count,
                COALESCE(sub.subaward_as_sub_total_received_usd, 0.0)             AS subaward_as_sub_total_received_usd,

                /* FMCSA fleet (nullable when no DOT linkage) */
                f.primary_dot_number,
                f.dot_number_count,
                f.primary_fleet_size,
                f.primary_power_units,
                f.primary_total_drivers,
                f.primary_mcs150_mileage,
                f.primary_hazmat_indicator,
                f.primary_safety_rating,
                f.primary_operating_radius_class,
                f.primary_specialty_class,
                f.primary_fleet_bucket,
                f.total_fleetsize_across_dots,
                f.total_power_units_across_dots,
                f.total_drivers_across_dots,

                /* SBA capital access (nullable when no bridge match) */
                sb.sba_link_confidence,
                sb.sba_lifetime_loan_count,
                sb.sba_lifetime_gross_approval_usd,
                sb.sba_earliest_loan_date,
                sb.sba_latest_loan_date,
                sb.sba_latest_loan_status,

                /* primary POCs (from master) */
                m.elec_bus_full_name_normalized,
                m.elec_bus_title,
                m.elec_bus_city,
                m.elec_bus_state_or_province,
                m.elec_bus_zippostal_code,
                m.govt_bus_full_name_normalized,
                m.govt_bus_title,
                m.govt_bus_city,
                m.govt_bus_state_or_province,
                m.govt_bus_zippostal_code,

                /* provenance */
                CAST('{spine_run_id}' AS VARCHAR)                                 AS spine_run_id,
                '{SPINE_VERSION}'                                                 AS spine_version,
                TIMESTAMP '{generated_at_iso}'                                    AS generated_at
            FROM master_dedup m
            LEFT JOIN grain          g   ON g.recipient_uei = m.uei
            LEFT JOIN agency_top3    a3  ON a3.uei          = m.uei
            LEFT JOIN naics_top3     n3  ON n3.uei          = m.uei
            LEFT JOIN psc_top3       p3  ON p3.uei          = m.uei
            LEFT JOIN pop_state_top3 s3  ON s3.uei          = m.uei
            LEFT JOIN set_aside_top3 sa3 ON sa3.uei         = m.uei
            LEFT JOIN subaward_rollup sub ON sub.uei        = m.uei
            LEFT JOIN fmcsa_per_uei  f   ON f.uei           = m.uei
            LEFT JOIN sba_per_uei    sb  ON sb.uei          = m.uei
            LEFT JOIN sam_flags      sf  ON sf.uei          = m.uei
        ) TO '{LOCAL_PARQUET_PATH}' (FORMAT PARQUET)
        """
    )
    stage1_dur = time.time() - t_stage1
    parquet_bytes = Path(LOCAL_PARQUET_PATH).stat().st_size
    stage1_rows = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('{LOCAL_PARQUET_PATH}')"
    ).fetchone()[0]
    logger.info(
        "STAGE 1 COMPLETE: rows=%d size=%.1f MB path=%s duration=%.1fs",
        stage1_rows, parquet_bytes / (1024 * 1024), LOCAL_PARQUET_PATH, stage1_dur,
    )

    if stage1_rows < MIN_ROW_FLOOR:
        raise RuntimeError(
            f"FLOOR FAIL: stage1_rows={stage1_rows} < MIN_ROW_FLOOR={MIN_ROW_FLOOR}"
        )

    # ── STAGE 2: Lance upload from local Parquet ──────────────────────────── #
    logger.info("STAGE 2 START: lance.write_dataset → %s", OUTPUT_LANCE_URI)
    t_stage2 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        reader = con.execute(
            f"SELECT * FROM read_parquet('{LOCAL_PARQUET_PATH}')"
        ).to_arrow_reader(batch_size=100_000)
        ds = lance.write_dataset(
            reader,
            OUTPUT_LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t_stage2
        lance_rows = ds.count_rows()
        logger.info(
            "lance.write_dataset: wrote %d rows in %.1fs (version=%s)",
            lance_rows, write_dur, ds.version,
        )

    # BTREE on uei.
    t_btree = time.time()
    try:
        ds.create_scalar_index("uei", index_type="BTREE", replace=True)
        btree_dur = time.time() - t_btree
        logger.info("BTREE on uei: OK (%.1fs)", btree_dur)
    except Exception as e:  # noqa: BLE001
        logger.error("BTREE on uei FAILED: %s", e)
        raise

    # Compact + cleanup_old_versions.
    try:
        ds.optimize.compact_files()
        ds.cleanup_old_versions(older_than=timedelta(days=7))
        logger.info("compact_files + cleanup_old_versions: OK")
    except Exception as e:  # noqa: BLE001
        logger.warning("optimize/cleanup failed (non-fatal): %s", e)

    final_rows = ds.count_rows()
    logger.info(
        "STAGE 2 COMPLETE: rows=%d write=%.1fs btree=%.1fs",
        final_rows, write_dur, btree_dur,
    )
    return {
        "status": "succeeded",
        "stage1_rows": stage1_rows,
        "stage1_size_mb": round(parquet_bytes / (1024 * 1024), 1),
        "stage1_duration_s": round(stage1_dur, 1),
        "lance_rows": final_rows,
        "lance_uri": OUTPUT_LANCE_URI,
        "lance_write_duration_s": round(write_dur, 1),
        "btree_duration_s": round(btree_dur, 1),
        "spine_run_id": spine_run_id,
        "spine_version": SPINE_VERSION,
    }


@app.local_entrypoint()
def run() -> None:
    """`modal run --detach apps/data-engine-x/modal/build_spine_federal_contractor_profile.py::run`"""
    import json
    out = emit.remote()
    print(json.dumps(out, indent=2, default=str))

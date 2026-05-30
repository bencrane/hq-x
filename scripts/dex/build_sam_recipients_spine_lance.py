#!/usr/bin/env python3
"""Lance-emit: spines/sam_recipients_lance — canonical federal-recipient spine.

One row per distinct UEI across the four federal-contracting populations:
  1. SAM-registered entities    (sam_gov/entities_lance, ~884K)
  2. Prime contract recipients  (usaspending/contracts_lance, ~15.5M txn rows
                                  → DISTINCT recipient_uei, 134K primes)
  3. Contract subawardees       (usaspending/contract_subawards_lance, 16.9K
                                  → DISTINCT subawardee_uei, 7K)
  4. Assistance subawardees     (usaspending/assistance_subawards_lance, 54K
                                  → DISTINCT subawardee_uei, 22K)

Identity coalesced from richest source (SAM > contracts > subawards).
Role flags + per-role first/last activity dates + obligated totals.

Addresses (12 columns, two roles — matches the convention every existing
SAM address bridge expects, e.g. build_bridge_sam_ppp_address_lance.py
build_bridge_sam_overture_address_lance.py):
  physical_address_line_1 / _line_2 / _city / _state_normalized / _zip5
                          / _base_normalized
  mailing_address_line_1  / _line_2 / _city / _state_normalized / _zip5
                          / _base_normalized

Address coalesce hierarchy (physical role):
  SAM physical > contracts recipient > subawardee (contract or assistance)
Address coalesce hierarchy (mailing role):
  SAM mailing only — no other source has a mailing role distinct from
  physical.

Normalization:
  - SAM physical/mailing: pre-baked columns on sam_gov/entities_lance from
    augment_sam_entities_address_normalize_lance.py (read-only consumer).
  - Contracts + subawards: normalized at spine-build time via the
    py_normalize_address_street DuckDB UDF (scripts._lib.address_normalize).
  - ZIP5: pre-baked on subawards (subawardee_zip5) and SAM
    (physical_address_zip5, mailing_address_zip5); LPAD/SUBSTR-derived for
    contracts (recipient_zip_4_code), matching emit_sba_borrowers_lance.py.
  - State: pre-baked on SAM physical + subawards; UPPER(TRIM(...)) for SAM
    mailing (no augmented column for that side) and for contracts.

Output:
  s3://dex-raw-landing-zone/polaris-warehouse/spines/sam_recipients_lance/

BTREE indexes on:
  uei, legal_business_name_normalized, corporate_website,
  physical_address_base_normalized, physical_address_zip5,
  physical_address_state_normalized, mailing_address_base_normalized,
  mailing_address_zip5.

Hard-fail policy:
  - Row floor 850,000 (SAM ~876K + ~20K non-SAM subawardees/lapsed primes).
  - All BTREE index builds raise on failure (no try/except swallow).

Reads upstream Lance datasets read-only. Overwrites only its own output URI.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with requests python \\
    apps/data-engine-x/scripts/build_sam_recipients_spine_lance.py --apply

  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with requests python \\
    apps/data-engine-x/scripts/build_sam_recipients_spine_lance.py --dry-run
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

os.environ["LANCE_BYPASS_SPILLING"] = "true"
os.environ["TMPDIR"] = "/tmp/lance"
Path("/tmp/lance").mkdir(parents=True, exist_ok=True)

from scripts._lib.address_normalize import (  # noqa: E402
    __version__ as ADDR_NORMALIZER_VERSION,
    register_address_udf,
)
from scripts._lib.catalog_hooks import register_or_update_polaris  # noqa: E402
from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("build_sam_recipients_spine_lance")

R2_BUCKET = "dex-raw-landing-zone"

SAM_SOURCE_URI = f"s3://{R2_BUCKET}/polaris-warehouse/sam_gov/entities_lance/"
CONTRACTS_URI = f"s3://{R2_BUCKET}/polaris-warehouse/usaspending/contracts_lance/"
CONTRACT_SUBS_URI = f"s3://{R2_BUCKET}/polaris-warehouse/usaspending/contract_subawards_lance/"
ASSISTANCE_SUBS_URI = f"s3://{R2_BUCKET}/polaris-warehouse/usaspending/assistance_subawards_lance/"

OUT_URI = f"s3://{R2_BUCKET}/polaris-warehouse/spines/sam_recipients_lance/"
DATASET_SLUG = "sam_recipients_lance"
POLARIS_NAMESPACE = "spines"
POLARIS_TABLE = "sam_recipients_lance"

ROW_FLOOR = 850_000

BTREE_COLS = (
    "uei",
    "legal_business_name_normalized",
    "corporate_website",
    "physical_address_base_normalized",
    "physical_address_zip5",
    "physical_address_state_normalized",
    "mailing_address_base_normalized",
    "mailing_address_zip5",
)


def _lance_storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _scan_arrow(uri: str, columns: list[str], storage_options: dict, filter_expr=None):
    import lance
    ds = lance.dataset(uri, storage_options=storage_options)
    scanner = ds.scanner(columns=columns, filter=filter_expr)
    return scanner.to_table()


# Reusable SQL fragment: clean ZIP to 5-digit text, matching the
# emit_sba_borrowers_lance.py convention. Never casts to BIGINT.
_ZIP5_SQL = (
    "LPAD(SUBSTR(REGEXP_REPLACE(TRIM(CAST({col} AS VARCHAR)), "
    "'(\\.0+|-\\d+).*$', ''), 1, 5), 5, '0')"
)


def _build(dry_run: bool) -> int:
    import duckdb
    import lance
    import pyarrow.compute as pc

    storage_options = _lance_storage_options()

    # ── 1. SAM-registered entities (READ-ONLY from sam_gov/entities_lance) ──
    # Switched away from spines/sam_entities_lance because that 12-col
    # projection deliberately omits addresses. sam_gov/entities_lance carries
    # the pre-baked physical/mailing normalized columns from the augment.
    logger.info("scanning sam_gov/entities_lance ...")
    sam_arrow = _scan_arrow(
        SAM_SOURCE_URI,
        columns=[
            # Identity
            "unique_entity_id",
            "uei_normalized",
            "cage_code",
            "legal_business_name",
            "legal_business_name_normalized",
            "dba_name",
            "dba_name_normalized",
            "entity_url",
            "sam_extract_code",
            "registration_expiration_date",
            "initial_registration_date",
            # Physical address — raw
            "physical_address_line_1",
            "physical_address_line_2",
            "physical_address_city",
            # Physical address — pre-baked normalized
            "physical_address_state_normalized",
            "physical_address_zip5",
            "physical_address_base_normalized",
            # Mailing address — raw
            "mailing_address_line_1",
            "mailing_address_line_2",
            "mailing_address_city",
            "mailing_address_state_or_province",
            # Mailing address — pre-baked normalized (only zip5 + base; state
            # not augmented, so we UPPER(TRIM) at spine-build time below)
            "mailing_address_zip5",
            "mailing_address_base_normalized",
        ],
        storage_options=storage_options,
        filter_expr=pc.field("unique_entity_id").is_valid(),
    )
    logger.info("  sam rows: %d", sam_arrow.num_rows)

    # ── 2. Prime contract recipients (heavy: 15.5M rows) ────────────────────
    logger.info("scanning usaspending/contracts_lance (heavy) ...")
    t0 = time.time()
    contracts_arrow = _scan_arrow(
        CONTRACTS_URI,
        columns=[
            "recipient_uei",
            "recipient_name",
            "recipient_address_line_1",
            "recipient_address_line_2",
            "recipient_city_name",
            "recipient_state_code",
            "recipient_zip_4_code",
            "action_date",
            "total_dollars_obligated",
            "naics_code",
        ],
        storage_options=storage_options,
        filter_expr=pc.field("recipient_uei").is_valid(),
    )
    logger.info("  contract txns with uei: %d rows in %.1fs",
                contracts_arrow.num_rows, time.time() - t0)

    # ── 3. Contract subawardees ─────────────────────────────────────────────
    logger.info("scanning usaspending/contract_subawards_lance ...")
    contract_subs_arrow = _scan_arrow(
        CONTRACT_SUBS_URI,
        columns=[
            "subawardee_uei",
            "subawardee_name",
            "subawardee_address_line_1",
            "subawardee_city_name",
            "subawardee_state_normalized",
            "subawardee_zip5",
            "subaward_action_date",
            "subaward_amount",
        ],
        storage_options=storage_options,
        filter_expr=pc.field("subawardee_uei").is_valid(),
    )
    logger.info("  contract subaward rows with uei: %d", contract_subs_arrow.num_rows)

    # ── 4. Assistance subawardees ───────────────────────────────────────────
    logger.info("scanning usaspending/assistance_subawards_lance ...")
    assistance_subs_arrow = _scan_arrow(
        ASSISTANCE_SUBS_URI,
        columns=[
            "subawardee_uei",
            "subawardee_name",
            "subawardee_address_line_1",
            "subawardee_city_name",
            "subawardee_state_normalized",
            "subawardee_zip5",
            "subaward_action_date",
            "subaward_amount",
        ],
        storage_options=storage_options,
        filter_expr=pc.field("subawardee_uei").is_valid(),
    )
    logger.info("  assistance subaward rows with uei: %d", assistance_subs_arrow.num_rows)

    # ── DuckDB derivation ───────────────────────────────────────────────────
    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET memory_limit='12GB'")
    con.execute("SET temp_directory='/tmp/lance'")
    con.execute("SET max_temp_directory_size='80GB'")
    con.execute("SET preserve_insertion_order=false")
    register_address_udf(con, fn_name="py_normalize_address_street")
    logger.info("  registered py_normalize_address_street (address_normalize v%s)",
                ADDR_NORMALIZER_VERSION)

    con.register("sam_arrow", sam_arrow)
    con.register("contracts_arrow", contracts_arrow)
    con.register("contract_subs_arrow", contract_subs_arrow)
    con.register("assistance_subs_arrow", assistance_subs_arrow)

    contracts_zip5 = _ZIP5_SQL.format(col="recipient_zip_4_code")

    logger.info("deriving role aggregates with address columns ...")
    t_agg = time.time()

    # SAM aggregate — pre-baked physical/mailing normalized cols + mailing
    # state computed inline (no augmented column for that).
    con.execute("""
        CREATE TEMP TABLE sam_agg AS
        SELECT
            unique_entity_id                                 AS uei,
            ANY_VALUE(uei_normalized)                        AS uei_normalized,
            ANY_VALUE(cage_code)                             AS cage_code,
            ANY_VALUE(legal_business_name)                   AS legal_business_name,
            ANY_VALUE(legal_business_name_normalized)        AS legal_business_name_normalized,
            ANY_VALUE(dba_name)                              AS dba_name,
            ANY_VALUE(dba_name_normalized)                   AS dba_name_normalized,
            ANY_VALUE(LOWER(NULLIF(TRIM(entity_url), '')))   AS corporate_website,
            CASE WHEN ANY_VALUE(sam_extract_code) = 'A'
                 THEN 'ACTIVE' ELSE 'INACTIVE_OR_OTHER' END  AS sam_status,
            MAX(TRY_STRPTIME(initial_registration_date,
                ['%Y-%m-%d','%m/%d/%Y','%Y%m%d','%m%d%Y'])::DATE)  AS sam_initial_registration_date,
            MAX(TRY_STRPTIME(registration_expiration_date,
                ['%Y-%m-%d','%m/%d/%Y','%Y%m%d','%m%d%Y'])::DATE)  AS sam_expiration_date,
            -- Physical address
            ANY_VALUE(physical_address_line_1)               AS sam_physical_line_1,
            ANY_VALUE(physical_address_line_2)               AS sam_physical_line_2,
            ANY_VALUE(physical_address_city)                 AS sam_physical_city,
            ANY_VALUE(physical_address_state_normalized)     AS sam_physical_state_norm,
            ANY_VALUE(physical_address_zip5)                 AS sam_physical_zip5,
            ANY_VALUE(physical_address_base_normalized)      AS sam_physical_base_norm,
            -- Mailing address
            ANY_VALUE(mailing_address_line_1)                AS sam_mailing_line_1,
            ANY_VALUE(mailing_address_line_2)                AS sam_mailing_line_2,
            ANY_VALUE(mailing_address_city)                  AS sam_mailing_city,
            ANY_VALUE(UPPER(NULLIF(TRIM(mailing_address_state_or_province), '')))
                                                             AS sam_mailing_state_norm,
            ANY_VALUE(mailing_address_zip5)                  AS sam_mailing_zip5,
            ANY_VALUE(mailing_address_base_normalized)       AS sam_mailing_base_norm
        FROM sam_arrow
        WHERE unique_entity_id IS NOT NULL
        GROUP BY unique_entity_id
    """)

    # Contracts: aggregate FIRST (15.5M rows → 134K distinct UEIs), then
    # apply the Python UDF once per UEI instead of 15.5M times. ARG_MAX on
    # raw line_1/line_2 by action_date picks the most-recent transaction's
    # address (recipients sometimes report varying addresses across actions).
    con.execute(f"""
        CREATE TEMP TABLE prime_agg AS
        WITH agg AS (
            SELECT
                recipient_uei                                AS uei,
                ARG_MAX(recipient_name, action_date)         AS recipient_name,
                ARG_MAX(recipient_address_line_1, action_date) AS prime_line_1,
                ARG_MAX(recipient_address_line_2, action_date) AS prime_line_2,
                ARG_MAX(recipient_city_name, action_date)    AS prime_city,
                ARG_MAX(UPPER(NULLIF(TRIM(recipient_state_code), '')), action_date)
                                                             AS prime_state_norm,
                ARG_MAX({contracts_zip5}, action_date)       AS prime_zip5,
                COUNT(*)                                     AS prime_contract_txn_count,
                SUM(TRY_CAST(total_dollars_obligated AS DOUBLE))
                                                             AS prime_contract_obligated_total,
                MIN(action_date)                             AS prime_first_action_date,
                MAX(action_date)                             AS prime_last_action_date
            FROM contracts_arrow
            WHERE recipient_uei IS NOT NULL
            GROUP BY recipient_uei
        )
        SELECT
            uei,
            recipient_name,
            prime_line_1,
            prime_line_2,
            prime_city,
            prime_state_norm,
            prime_zip5,
            py_normalize_address_street(
                NULLIF(TRIM(
                    COALESCE(prime_line_1, '')
                    || CASE WHEN prime_line_2 IS NOT NULL
                             AND TRIM(prime_line_2) <> ''
                            THEN ' ' || prime_line_2 ELSE '' END
                ), '')
            )                                                AS prime_base_norm,
            prime_contract_txn_count,
            prime_contract_obligated_total,
            prime_first_action_date,
            prime_last_action_date
        FROM agg
    """)

    # Contract subawards — aggregate first, then normalize once per UEI.
    # State + zip5 are pre-baked on the source.
    con.execute("""
        CREATE TEMP TABLE contract_sub_agg AS
        WITH agg AS (
            SELECT
                subawardee_uei                                AS uei,
                ARG_MAX(subawardee_name, subaward_action_date) AS subawardee_name,
                ARG_MAX(subawardee_address_line_1, subaward_action_date)
                                                              AS csub_line_1,
                ARG_MAX(subawardee_city_name, subaward_action_date)
                                                              AS csub_city,
                ARG_MAX(NULLIF(TRIM(subawardee_state_normalized), ''), subaward_action_date)
                                                              AS csub_state_norm,
                ARG_MAX(NULLIF(TRIM(subawardee_zip5), ''), subaward_action_date)
                                                              AS csub_zip5,
                COUNT(*)                                      AS contract_subaward_count,
                SUM(TRY_CAST(subaward_amount AS DOUBLE))      AS contract_subaward_amount_total,
                MIN(subaward_action_date)                     AS contract_subaward_first_date,
                MAX(subaward_action_date)                     AS contract_subaward_last_date
            FROM contract_subs_arrow
            WHERE subawardee_uei IS NOT NULL
            GROUP BY subawardee_uei
        )
        SELECT
            uei, subawardee_name,
            csub_line_1, csub_city, csub_state_norm, csub_zip5,
            py_normalize_address_street(csub_line_1)          AS csub_base_norm,
            contract_subaward_count, contract_subaward_amount_total,
            contract_subaward_first_date, contract_subaward_last_date
        FROM agg
    """)

    con.execute("""
        CREATE TEMP TABLE assistance_sub_agg AS
        WITH agg AS (
            SELECT
                subawardee_uei                                AS uei,
                ARG_MAX(subawardee_name, subaward_action_date) AS subawardee_name,
                ARG_MAX(subawardee_address_line_1, subaward_action_date)
                                                              AS asub_line_1,
                ARG_MAX(subawardee_city_name, subaward_action_date)
                                                              AS asub_city,
                ARG_MAX(NULLIF(TRIM(subawardee_state_normalized), ''), subaward_action_date)
                                                              AS asub_state_norm,
                ARG_MAX(NULLIF(TRIM(subawardee_zip5), ''), subaward_action_date)
                                                              AS asub_zip5,
                COUNT(*)                                      AS assistance_subaward_count,
                SUM(TRY_CAST(subaward_amount AS DOUBLE))      AS assistance_subaward_amount_total,
                MIN(subaward_action_date)                     AS assistance_subaward_first_date,
                MAX(subaward_action_date)                     AS assistance_subaward_last_date
            FROM assistance_subs_arrow
            WHERE subawardee_uei IS NOT NULL
            GROUP BY subawardee_uei
        )
        SELECT
            uei, subawardee_name,
            asub_line_1, asub_city, asub_state_norm, asub_zip5,
            py_normalize_address_street(asub_line_1)          AS asub_base_norm,
            assistance_subaward_count, assistance_subaward_amount_total,
            assistance_subaward_first_date, assistance_subaward_last_date
        FROM agg
    """)

    for tbl in ("sam_agg", "prime_agg", "contract_sub_agg", "assistance_sub_agg"):
        n = con.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        logger.info("  %s: %d distinct uei", tbl, n)
    logger.info("  aggregates built in %.1fs", time.time() - t_agg)

    logger.info("unioning + coalescing identity + addresses ...")
    spine_sql = """
    WITH all_ueis AS (
        SELECT uei FROM sam_agg
        UNION
        SELECT uei FROM prime_agg
        UNION
        SELECT uei FROM contract_sub_agg
        UNION
        SELECT uei FROM assistance_sub_agg
    )
    SELECT
        u.uei,
        COALESCE(s.uei_normalized, UPPER(TRIM(u.uei)))    AS uei_normalized,
        COALESCE(s.legal_business_name,
                 p.recipient_name,
                 cs.subawardee_name,
                 as_.subawardee_name)                     AS legal_business_name,
        s.legal_business_name_normalized,
        s.dba_name,
        s.dba_name_normalized,
        s.cage_code,
        s.corporate_website,
        COALESCE(s.sam_status,
                 CASE WHEN p.uei IS NOT NULL
                       OR cs.uei IS NOT NULL
                       OR as_.uei IS NOT NULL
                      THEN 'NOT_IN_SAM' END)              AS sam_status,
        s.sam_initial_registration_date,
        s.sam_expiration_date,
        (s.uei IS NOT NULL)                               AS is_sam_registered,
        (p.uei IS NOT NULL)                               AS is_prime_contractor,
        (cs.uei IS NOT NULL)                              AS is_contract_subawardee,
        (as_.uei IS NOT NULL)                             AS is_assistance_subawardee,
        (s.sam_status = 'ACTIVE')                         AS sam_active,
        -- ── Physical address ── coalesce: SAM > contracts > csub > asub ──
        COALESCE(s.sam_physical_line_1, p.prime_line_1, cs.csub_line_1, as_.asub_line_1)
            AS physical_address_line_1,
        COALESCE(s.sam_physical_line_2, p.prime_line_2)
            AS physical_address_line_2,
        COALESCE(s.sam_physical_city, p.prime_city, cs.csub_city, as_.asub_city)
            AS physical_address_city,
        COALESCE(s.sam_physical_state_norm, p.prime_state_norm, cs.csub_state_norm, as_.asub_state_norm)
            AS physical_address_state_normalized,
        COALESCE(s.sam_physical_zip5, p.prime_zip5, cs.csub_zip5, as_.asub_zip5)
            AS physical_address_zip5,
        COALESCE(s.sam_physical_base_norm, p.prime_base_norm, cs.csub_base_norm, as_.asub_base_norm)
            AS physical_address_base_normalized,
        -- ── Mailing address ── SAM only ──
        s.sam_mailing_line_1                              AS mailing_address_line_1,
        s.sam_mailing_line_2                              AS mailing_address_line_2,
        s.sam_mailing_city                                AS mailing_address_city,
        s.sam_mailing_state_norm                          AS mailing_address_state_normalized,
        s.sam_mailing_zip5                                AS mailing_address_zip5,
        s.sam_mailing_base_norm                           AS mailing_address_base_normalized,
        -- ── Per-role aggregates ──
        p.prime_contract_txn_count,
        p.prime_contract_obligated_total,
        p.prime_first_action_date,
        p.prime_last_action_date,
        cs.contract_subaward_count,
        cs.contract_subaward_amount_total,
        cs.contract_subaward_first_date,
        cs.contract_subaward_last_date,
        as_.assistance_subaward_count,
        as_.assistance_subaward_amount_total,
        as_.assistance_subaward_first_date,
        as_.assistance_subaward_last_date
    FROM all_ueis u
    LEFT JOIN sam_agg            s   ON s.uei  = u.uei
    LEFT JOIN prime_agg          p   ON p.uei  = u.uei
    LEFT JOIN contract_sub_agg   cs  ON cs.uei = u.uei
    LEFT JOIN assistance_sub_agg as_ ON as_.uei = u.uei
    """

    spine_arrow = con.execute(spine_sql).arrow().read_all()
    spine_rows = spine_arrow.num_rows
    logger.info("  union spine: %d rows × %d cols", spine_rows, spine_arrow.num_columns)

    if spine_rows < ROW_FLOOR:
        logger.error("FAIL: spine rows %d < floor %d", spine_rows, ROW_FLOOR)
        return 1

    con.register("spine_arrow", spine_arrow)
    breakdown = con.execute("""
        SELECT
            COUNT(*)                                                                  AS total,
            COUNT(*) FILTER (WHERE is_sam_registered)                                 AS sam_registered,
            COUNT(*) FILTER (WHERE is_prime_contractor)                               AS prime_contractor,
            COUNT(*) FILTER (WHERE is_contract_subawardee)                            AS contract_sub,
            COUNT(*) FILTER (WHERE is_assistance_subawardee)                          AS assistance_sub,
            COUNT(*) FILTER (WHERE is_prime_contractor AND NOT is_sam_registered)     AS prime_no_sam,
            COUNT(*) FILTER (WHERE (is_contract_subawardee OR is_assistance_subawardee)
                              AND NOT is_sam_registered)                              AS subawardee_no_sam,
            COUNT(*) FILTER (WHERE corporate_website IS NOT NULL AND corporate_website <> '')
                                                                                      AS with_website,
            COUNT(*) FILTER (WHERE physical_address_base_normalized IS NOT NULL)      AS with_physical_norm,
            COUNT(*) FILTER (WHERE physical_address_zip5 IS NOT NULL)                 AS with_physical_zip5,
            COUNT(*) FILTER (WHERE mailing_address_base_normalized IS NOT NULL)       AS with_mailing_norm,
            COUNT(*) FILTER (WHERE mailing_address_zip5 IS NOT NULL)                  AS with_mailing_zip5
        FROM spine_arrow
    """).fetchone()
    cols = ("total", "sam_registered", "prime_contractor", "contract_sub",
            "assistance_sub", "prime_no_sam", "subawardee_no_sam", "with_website",
            "with_physical_norm", "with_physical_zip5",
            "with_mailing_norm", "with_mailing_zip5")
    logger.info("role + address coverage breakdown:")
    for c, v in zip(cols, breakdown):
        logger.info("  %s: %d", c, v)

    if dry_run:
        logger.info("DRY RUN — skipping Lance write")
        return 0

    t_write = time.time()
    with lance_commit_lock(DATASET_SLUG):
        logger.info("writing Lance dataset (mode=overwrite) ...")
        ds = lance.write_dataset(
            spine_arrow,
            OUT_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        logger.info("  wrote %d rows in %.1fs (version=%s)",
                    ds.count_rows(), time.time() - t_write, ds.version)

        for col in BTREE_COLS:
            logger.info("building BTREE index on %s ...", col)
            t_idx = time.time()
            ds.create_scalar_index(col, index_type="BTREE", replace=True)
            logger.info("  BTREE(%s): OK in %.1fs", col, time.time() - t_idx)

        try:
            ds.optimize.compact_files()
        except Exception as e:
            logger.warning("compact_files failed (non-fatal): %s", e)
        try:
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as e:
            logger.warning("cleanup_old_versions failed (non-fatal): %s", e)

    logger.info("registering Polaris catalog entry ...")
    register_or_update_polaris(
        namespace=POLARIS_NAMESPACE,
        table_name=POLARIS_TABLE,
        s3_uri=OUT_URI,
        docstring=(
            "Canonical federal-recipient spine — one row per distinct UEI "
            "across SAM-registered entities (sam_gov/entities_lance), prime "
            "contract recipients (usaspending/contracts_lance), contract "
            "subawardees (usaspending/contract_subawards_lance), and "
            "assistance subawardees (usaspending/assistance_subawards_lance). "
            "Role flags + per-role first/last activity dates + obligated "
            "totals. Identity coalesced (SAM > contracts > subawards). "
            "Physical + mailing addresses with raw + normalized columns "
            "(physical_address_base_normalized/_zip5/_state_normalized; "
            "mailing same shape) matching the convention every existing SAM "
            "address bridge expects. SAM addresses pre-baked; contracts + "
            "subawards normalized at spine-build via address_normalize "
            f"v{ADDR_NORMALIZER_VERSION}. BTREE on uei, "
            "legal_business_name_normalized, corporate_website, "
            "physical_address_base_normalized, physical_address_zip5, "
            "physical_address_state_normalized, "
            "mailing_address_base_normalized, mailing_address_zip5."
        ),
    )

    logger.info("=" * 60)
    logger.info("OK — spines/sam_recipients_lance: %d rows", spine_rows)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Lance build: canonical SAM federal-recipient spine"
    )
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true")
    grp.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            logger.error("FAIL: %s not set", var)
            return 64

    return _build(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())

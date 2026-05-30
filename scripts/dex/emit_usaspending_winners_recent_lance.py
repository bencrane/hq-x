#!/usr/bin/env python3
"""Emit usaspending/winners_recent_lance — rolling 5-year FPDS+FABS+subaward feed.

Derived Lance view (Layer 1 of the usaspending-derived-views-daily cycle).
Reads from four source datasets (transaction_fpds_lance, transaction_fabs_lance,
subaward_lance, sam_gov/entities_lance, sam_gov/entity_pocs_lance) and produces
a single pre-joined, pre-enriched view of the last 5 years of USAspending
transactions.

Schema:
  - One row per source transaction (per-transaction grain preserved).
  - FPDS + FABS columns (both share identical abbreviated transaction_search MV names
    per contract.md §Column-name reconciliation — NO COALESCE coercion needed):
    transaction_id, award_id, recipient_uei, recipient_name,
    action_date, federal_action_obligation, naics_code, naics_description,
    product_or_service_code, awarding_toptier_agency_name,
    awarding_subtier_agency_name, transaction_description, kind discriminator.
  - Part 1/2 filter columns (new Phase 3):
    recipient_location_state_code, pop_state_code, type_set_aside,
    type_of_idc, idv_type, parent_award_id.
    (FPDS: all 6 populated. FABS: recipient_location_state_code + pop_state_code
    populated; type_set_aside/type_of_idc/idv_type/parent_award_id are NULL for
    assistance rows — these are FPDS-only concepts but both datasets share the
    column name per USAspending transaction_search MV shape.)
  - SCIF computed column (new Phase 3):
    requires_scif_infrastructure BOOLEAN — broad-DoD + $1M threshold.
    (contract.md §SCIF rule SQL — broad-DoD only, operator's other 4 agencies
    don't exist as awarding_toptier_agency_name in USAspending data.)
  - SAM 8-col inline: entity_url, legal_business_name, physical_address_city,
    physical_address_province_or_state, entity_structure, primary_naics, cage_code.
    (LEFT JOIN — NULL when recipient_uei not in SAM or not registered.)
    SAM join hit rate for subawardee UEIs: 73.24% — 27% of sub rows get NULL
    SAM fields (small subcontractors not SAM-registered). NO COALESCE fallback
    per contract.md §SAM entity LEFT JOIN for sub rows.
  - pocs_count INT: count of rows in entity_pocs_lance for this UEI.
  - kind VARCHAR discriminator: 'contract' | 'assistance' | 'subaward'.

Cycle #3 (subaward parity): adds subaward_lance as a 3rd UNION ALL leg.
  - Column mapping per contract.md §Column mapping: sub_awardee_or_recipient_uei
    → recipient_uei (BTREE), sub_action_date → action_date (no BTREE),
    subaward_amount → federal_action_obligation, sub_naics → naics_code,
    subaward_description → transaction_description, broker_subaward_id → transaction_id,
    awarding_toptier_agency_name direct (90.4% populated — no prime join needed),
    sub_legal_entity_state_code → recipient_location_state_code,
    sub_place_of_perform_state_code → pop_state_code.
  - Signal semantics: STRICT — subaward history included in _uei_first_seen +
    _uei_lifetime_stats aggregates (contract.md §Signal semantics decisions).
  - SCIF rule unchanged: awarding_toptier_agency_name = 'Department of Defense'
    AND subaward_amount >= $1M — fires on ~47,627 sub rows (1.52%).

SAM dedup: sam_gov/entities_lance has 0.76% multi-CAGE UEIs (6,621/876,399).
  Use DISTINCT ON (unique_entity_id) first-row-wins before join to avoid
  duplicating transaction rows. (contract.md §SAM dedup pattern)

BTREE on: recipient_uei, action_date, naics_code, awarding_toptier_agency_name,
  kind, recipient_location_state_code, parent_award_id. (7 total — Phase 3 adds
  recipient_location_state_code and parent_award_id per contract.md §Substrate.)

MIN_ROW_FLOOR: 27,000,000 (Cycle #3 bump — 5y FPDS+FABS+sub = ~42.4M rows,
  27M = 64% conservative floor after SAM-dedup + UEI NOT NULL filter).

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python apps/data-engine-x/scripts/emit_usaspending_winners_recent_lance.py --apply

  doppler run --project hq-all --config prd -- \\
    uv run python apps/data-engine-x/scripts/emit_usaspending_winners_recent_lance.py --dry-run
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("emit_usaspending_winners_recent_lance")

DATASET_SLUG = "usaspending_winners_recent_lance"
OUT_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/winners_recent_lance"
)
FPDS_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/transaction_fpds_lance"
)
FABS_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/transaction_fabs_lance"
)
# Cycle #3: subaward_lance — 9.8M rows total, ~3.13M in 5y window.
# BTREE on sub_awardee_or_recipient_uei. NO BTREE on sub_action_date.
SUB_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/subaward_lance"
)
SAM_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/entities_lance"
)
POCS_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/entity_pocs_lance"
)

# Phase 3: extended from 90d to 5 years.
# int(5 * 365.25) = 1826 days per contract.md §Substrate changes table.
WINDOW_DAYS = int(5 * 365.25)

# MIN_ROWS: validator-stamped 5y floor.
# Cycle #3: FPDS 5y = 31.3M + FABS 5y = 8.0M + sub 5y = ~3.1M = ~42.4M raw.
# 27M = 64% of 42.4M (conservative after SAM-dedup + UEI NOT NULL filter).
# Cycle #2 floor was 25M (FPDS+FABS only). Sub leg adds ~2.7M rows → floor bumped.
# Override via MIN_FLOOR_WINNERS_RECENT env var (existing Phase 2 hook).
MIN_ROWS = int(os.environ.get("MIN_FLOOR_WINNERS_RECENT", 27_000_000))

TMP_DIR = "/tmp/lance"

# Phase 3: 7 BTREE columns (5 from Phase 2 + 2 new Phase 3 filter columns).
# requires_scif_infrastructure is boolean (low cardinality) — BTREE optional;
# omitted per contract.md §Substrate changes table note.
BTREE_COLS = [
    "recipient_uei",
    "action_date",
    "naics_code",
    "awarding_toptier_agency_name",
    "kind",
    # Phase 3 additions (contract.md §Substrate — Part 1/2 filter BTREEs)
    "recipient_location_state_code",
    "parent_award_id",
]


def _lance_storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true", help="write Lance output")
    grp.add_argument("--dry-run", action="store_true", help="count only, no writes")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")

    import duckdb
    import lance

    storage_options = _lance_storage_options()

    # 5-year window (anchor from today; emits at 08:30 UTC, data through prior day)
    until_date = date.today()
    since_date = until_date - timedelta(days=WINDOW_DAYS)
    since_str = since_date.isoformat()
    until_str = until_date.isoformat()
    logger.info("window: %s to %s (%d days)", since_str, until_str, WINDOW_DAYS)

    # -- Load source datasets via Arrow-bridge --------------------------------
    logger.info("opening transaction_fpds_lance ...")
    fpds_ds = lance.dataset(FPDS_LANCE_URI, storage_options=storage_options)
    fpds_arrow = fpds_ds.scanner(
        filter=f"action_date >= '{since_str}' AND action_date <= '{until_str}'",
        columns=[
            "transaction_id",
            "award_id",
            "recipient_uei",
            "recipient_name",
            "action_date",
            "federal_action_obligation",
            "naics_code",
            "naics_description",
            "product_or_service_code",
            "awarding_toptier_agency_name",
            "awarding_subtier_agency_name",
            "transaction_description",
            # Phase 3: Part 1/2 filter columns (contract.md §Column-name reconciliation)
            "recipient_location_state_code",
            "pop_state_code",
            "type_set_aside",
            "type_of_idc",
            "idv_type",
            "parent_award_id",
        ],
    ).to_table()
    logger.info("  fpds rows in window: %d", len(fpds_arrow))

    logger.info("opening transaction_fabs_lance ...")
    fabs_ds = lance.dataset(FABS_LANCE_URI, storage_options=storage_options)
    fabs_arrow = fabs_ds.scanner(
        filter=f"action_date >= '{since_str}' AND action_date <= '{until_str}'",
        columns=[
            "transaction_id",
            "award_id",
            "recipient_uei",
            "recipient_name",
            "action_date",
            "federal_action_obligation",
            "cfda_number",
            "cfda_title",
            "awarding_toptier_agency_name",
            "awarding_subtier_agency_name",
            "transaction_description",
            # Phase 3: FABS shares these 2 state columns (same MV schema as FPDS).
            # The 4 FPDS-only contract cols are not projected from FABS scanner;
            # they get NULLs in the CTAS below (assistance rows have no IDC/IDV/parent).
            "recipient_location_state_code",
            "pop_state_code",
        ],
    ).to_table()
    logger.info("  fabs rows in window: %d", len(fabs_arrow))

    # Cycle #3: subaward_lance — minimal 17-col projection (contract.md §File-creation list).
    # NO date-range push-down: sub_action_date has no BTREE — full 9.8M row scan.
    # Filter in DuckDB instead (avoids re-opening for the sub_rows CTAS below).
    logger.info("opening subaward_lance ...")
    sub_ds = lance.dataset(SUB_LANCE_URI, storage_options=storage_options)
    sub_arrow = sub_ds.scanner(
        columns=[
            "broker_subaward_id",
            "award_id",
            "sub_awardee_or_recipient_uei",
            "sub_awardee_or_recipient_legal",
            "sub_action_date",
            "subaward_amount",
            "sub_naics",
            "awarding_toptier_agency_name",
            "awarding_subtier_agency_name",
            "subaward_description",
            "cfda_number",
            "cfda_title",
            "product_or_service_code",
            "type_set_aside",
            "sub_legal_entity_state_code",
            "sub_place_of_perform_state_code",
            "parent_award_id",
        ],
    ).to_table()
    logger.info("  subaward rows total: %d", len(sub_arrow))

    logger.info("opening sam_gov/entities_lance (all rows for JOIN) ...")
    sam_ds = lance.dataset(SAM_LANCE_URI, storage_options=storage_options)
    sam_arrow = sam_ds.scanner(
        columns=[
            "unique_entity_id",
            "entity_url",
            "legal_business_name",
            "physical_address_city",
            "physical_address_province_or_state",
            "entity_structure",
            "primary_naics",
            "cage_code",
        ],
    ).to_table()
    logger.info("  sam entities rows: %d", len(sam_arrow))

    logger.info("opening sam_gov/entity_pocs_lance (count aggregation) ...")
    pocs_ds = lance.dataset(POCS_LANCE_URI, storage_options=storage_options)
    pocs_arrow = pocs_ds.scanner(
        columns=["uei"],
    ).to_table()
    logger.info("  pocs rows: %d", len(pocs_arrow))

    # -- DuckDB aggregation ---------------------------------------------------
    con = duckdb.connect()
    # Cycle #3: peaks ~27 GB RSS (Cycle #2 ~25 GB + sub leg ~1.5-2 GB).
    # Modal envelope is 32 GB; 20 GB DuckDB limit leaves ~7-12 GB for Arrow tables.
    con.execute("SET memory_limit='20GB'")
    con.register("fpds", fpds_arrow)
    con.register("fabs", fabs_arrow)
    con.register("sub_raw", sub_arrow)
    con.register("sam_raw", sam_arrow)
    con.register("pocs_raw", pocs_arrow)

    # SAM dedup: 0.76% of UEIs have multiple CAGE rows. Keep first row per UEI.
    # contract.md §SAM dedup pattern. (C14: load-bearing invariant)
    con.execute("""
        CREATE TEMP TABLE sam_deduped AS
        SELECT DISTINCT ON (unique_entity_id)
            unique_entity_id,
            entity_url,
            legal_business_name,
            physical_address_city,
            physical_address_province_or_state,
            entity_structure,
            primary_naics,
            cage_code
        FROM sam_raw
        WHERE unique_entity_id IS NOT NULL
    """)
    logger.info("  sam_deduped rows: %d", con.execute("SELECT COUNT(*) FROM sam_deduped").fetchone()[0])

    # POC count aggregation: pocs_count INT column (not the full array)
    con.execute("""
        CREATE TEMP TABLE pocs_count AS
        SELECT uei, COUNT(*) AS pocs_count
        FROM pocs_raw
        WHERE uei IS NOT NULL
        GROUP BY uei
    """)

    # -- Cycle #2/#3: synthetic signal aggregates ----------------------------
    # _uei_first_seen: per-UEI earliest action_date across FULL corpus (all years,
    # FPDS + FABS + subaward). Used for is_first_time_winner strict-equality check.
    # Cycle #3: adds 3rd UNION ALL leg over subaward_raw (STRICT semantics per
    # contract.md §Signal semantics — 51.30% of subawardee UEIs never appear in
    # FPDS+FABS; PRIME-ONLY would trivially yield FALSE on all sub rows).
    # TRY_CAST required: action_date is VARCHAR in all three sources.
    logger.info("building _uei_first_seen aggregate (full corpus UNION ALL — FPDS+FABS+sub) ...")
    con.execute("""
        CREATE TEMP TABLE _uei_first_seen AS
        SELECT
            recipient_uei,
            MIN(TRY_CAST(action_date AS DATE)) AS first_seen_date
        FROM (
            SELECT recipient_uei, action_date
            FROM fpds
            WHERE recipient_uei IS NOT NULL
            UNION ALL
            SELECT recipient_uei, action_date
            FROM fabs
            WHERE recipient_uei IS NOT NULL
            UNION ALL
            -- Cycle #3: subaward history (STRICT semantics — contract.md §Signal semantics)
            SELECT sub_awardee_or_recipient_uei AS recipient_uei,
                   sub_action_date AS action_date
            FROM sub_raw
            WHERE sub_awardee_or_recipient_uei IS NOT NULL
        )
        WHERE TRY_CAST(action_date AS DATE) IS NOT NULL
        GROUP BY recipient_uei
    """)
    logger.info("  _uei_first_seen rows: %d", con.execute("SELECT COUNT(*) FROM _uei_first_seen").fetchone()[0])

    # _uei_lifetime_stats: per-UEI lifetime AVG award $ across FULL corpus (all years).
    # Used for is_breakout_tier: lifetime_avg < $150k AND current >= $1.36M.
    # Cycle #3: adds 3rd UNION ALL leg over subaward_raw (STRICT semantics).
    # TRY_CAST required: obligation is VARCHAR in all three sources.
    logger.info("building _uei_lifetime_stats aggregate (full corpus UNION ALL — FPDS+FABS+sub) ...")
    con.execute("""
        CREATE TEMP TABLE _uei_lifetime_stats AS
        SELECT
            recipient_uei,
            AVG(TRY_CAST(federal_action_obligation AS DOUBLE)) AS lifetime_avg_award
        FROM (
            SELECT recipient_uei, federal_action_obligation
            FROM fpds
            WHERE recipient_uei IS NOT NULL
            UNION ALL
            SELECT recipient_uei, federal_action_obligation
            FROM fabs
            WHERE recipient_uei IS NOT NULL
            UNION ALL
            -- Cycle #3: subaward lifetime amount (STRICT semantics — contract.md §Signal semantics)
            SELECT sub_awardee_or_recipient_uei AS recipient_uei,
                   subaward_amount AS federal_action_obligation
            FROM sub_raw
            WHERE sub_awardee_or_recipient_uei IS NOT NULL
        )
        WHERE TRY_CAST(federal_action_obligation AS DOUBLE) IS NOT NULL
        GROUP BY recipient_uei
    """)
    logger.info("  _uei_lifetime_stats rows: %d", con.execute("SELECT COUNT(*) FROM _uei_lifetime_stats").fetchone()[0])

    # FPDS rows with kind='contract'.
    # Phase 3: +6 new filter columns projected verbatim (same names in both datasets
    # per contract.md §Column-name reconciliation — FPDS has all 6 populated).
    # Column order MUST match fabs_rows CTAS below for UNION ALL correctness.
    con.execute("""
        CREATE TEMP TABLE fpds_rows AS
        SELECT
            'contract'                         AS kind,
            transaction_id,
            award_id,
            recipient_uei,
            recipient_name,
            action_date::VARCHAR               AS action_date,
            federal_action_obligation,
            naics_code,
            naics_description,
            product_or_service_code,
            awarding_toptier_agency_name,
            awarding_subtier_agency_name,
            transaction_description,
            NULL::VARCHAR                      AS cfda_number,
            NULL::VARCHAR                      AS cfda_title,
            -- Phase 3: Part 1/2 filter columns (FPDS: all populated)
            recipient_location_state_code,
            pop_state_code,
            type_set_aside,
            type_of_idc,
            idv_type,
            parent_award_id
        FROM fpds
        WHERE recipient_uei IS NOT NULL
    """)

    # FABS rows with kind='assistance'.
    # Phase 3: FABS has recipient_location_state_code + pop_state_code populated.
    # The 4 FPDS-only contract columns (type_set_aside, type_of_idc, idv_type,
    # parent_award_id) are NULL for assistance rows — this is correct and expected.
    # Column order MUST match fpds_rows CTAS above for UNION ALL safety.
    con.execute("""
        CREATE TEMP TABLE fabs_rows AS
        SELECT
            'assistance'                       AS kind,
            transaction_id,
            award_id,
            recipient_uei,
            recipient_name,
            action_date::VARCHAR               AS action_date,
            federal_action_obligation,
            NULL::VARCHAR                      AS naics_code,
            NULL::VARCHAR                      AS naics_description,
            NULL::VARCHAR                      AS product_or_service_code,
            awarding_toptier_agency_name,
            awarding_subtier_agency_name,
            transaction_description,
            cfda_number,
            cfda_title,
            -- Phase 3: FABS state columns populated; contract-only cols are NULL
            recipient_location_state_code,
            pop_state_code,
            NULL::VARCHAR                      AS type_set_aside,
            NULL::VARCHAR                      AS type_of_idc,
            NULL::VARCHAR                      AS idv_type,
            NULL::VARCHAR                      AS parent_award_id
        FROM fabs
        WHERE recipient_uei IS NOT NULL
    """)

    # Cycle #3: subaward rows with kind='subaward'.
    # Column mapping per contract.md §Column mapping (validator-stamped exact names):
    #   sub_awardee_or_recipient_uei → recipient_uei (BTREE on source)
    #   sub_action_date → action_date (VARCHAR, TRY_CAST AS DATE for ORDER BY)
    #   subaward_amount → federal_action_obligation (VARCHAR)
    #   sub_naics → naics_code (col 130)
    #   awarding_toptier_agency_name → direct (col 143; 90.4% populated; NO join needed)
    #   subaward_description → transaction_description
    #   broker_subaward_id → transaction_id
    #   sub_legal_entity_state_code → recipient_location_state_code
    #   sub_place_of_perform_state_code → pop_state_code
    # Null-padded cols: naics_description, type_of_idc, idv_type (not sub-grain semantics)
    # type_set_aside, product_or_service_code, cfda_number, cfda_title present in subaward_lance.
    con.execute(f"""
        CREATE TEMP TABLE sub_rows AS
        SELECT
            'subaward'                             AS kind,
            broker_subaward_id                     AS transaction_id,
            award_id,
            sub_awardee_or_recipient_uei           AS recipient_uei,
            sub_awardee_or_recipient_legal         AS recipient_name,
            sub_action_date::VARCHAR               AS action_date,
            subaward_amount                        AS federal_action_obligation,
            sub_naics                              AS naics_code,
            NULL::VARCHAR                          AS naics_description,
            product_or_service_code,
            awarding_toptier_agency_name,
            awarding_subtier_agency_name,
            subaward_description                   AS transaction_description,
            cfda_number,
            cfda_title,
            -- Subawardee state columns (contract.md §Column mapping)
            sub_legal_entity_state_code            AS recipient_location_state_code,
            sub_place_of_perform_state_code        AS pop_state_code,
            type_set_aside,
            NULL::VARCHAR                          AS type_of_idc,
            NULL::VARCHAR                          AS idv_type,
            parent_award_id
        FROM sub_raw
        WHERE sub_awardee_or_recipient_uei IS NOT NULL
          AND TRY_CAST(sub_action_date AS DATE) >= '{since_str}'
          AND TRY_CAST(sub_action_date AS DATE) <= '{until_str}'
    """)
    sub_count = con.execute("SELECT COUNT(*) FROM sub_rows").fetchone()[0]
    logger.info("  sub_rows in 5y window: %d", sub_count)

    # UNION all transactions — sorted by action_date DESC so date-range scans
    # read co-located row groups (critical for Lance byte-range efficiency).
    # Cycle #3: adds sub_rows leg (kind='subaward') — column order matches FPDS/FABS.
    # Phase 3: ORDER BY action_date DESC + recipient_uei DESC per Phase 2 finding
    # (contract.md C6: max_rows_per_file + ORDER BY preserves range-read benefit).
    con.execute("""
        CREATE TEMP TABLE all_tx AS
        SELECT * FROM fpds_rows
        UNION ALL
        SELECT * FROM fabs_rows
        UNION ALL
        SELECT * FROM sub_rows
        ORDER BY action_date DESC, recipient_uei DESC
    """)

    # Final JOIN: transactions LEFT JOIN sam_deduped LEFT JOIN pocs_count
    #             LEFT JOIN _uei_first_seen LEFT JOIN _uei_lifetime_stats.
    # Phase 3: adds 6 filter columns + 1 SCIF computed column.
    # Cycle #2: adds is_first_time_winner + is_breakout_tier synthetic signals.
    # SCIF rule per contract.md §SCIF rule SQL (broad-DoD + $1M):
    #   NSA/DIA/NRO/DARPA are subtier-only — they don't appear as
    #   awarding_toptier_agency_name in USAspending data. Broad-DoD + $1M
    #   captures all relevant UEIs since those subtiers roll up to DoD.
    con.execute("""
        CREATE TEMP TABLE result AS
        SELECT
            t.kind,
            t.transaction_id,
            t.award_id,
            t.recipient_uei,
            t.recipient_name,
            t.action_date,
            t.federal_action_obligation,
            t.naics_code,
            t.naics_description,
            t.product_or_service_code,
            t.awarding_toptier_agency_name,
            t.awarding_subtier_agency_name,
            t.transaction_description,
            t.cfda_number,
            t.cfda_title,
            -- Phase 3: Part 1/2 filter columns (contract.md §Final emit projection)
            t.recipient_location_state_code,
            t.pop_state_code,
            t.type_set_aside,
            t.type_of_idc,
            t.idv_type,
            t.parent_award_id,
            -- Phase 3: SCIF computed signal (contract.md §SCIF rule SQL)
            -- Broad-DoD + $1M heuristic (Option A — Cycle #2 can refine to subtier-precise)
            CASE WHEN t.awarding_toptier_agency_name = 'Department of Defense'
                      AND TRY_CAST(t.federal_action_obligation AS DOUBLE) >= 1000000
                 THEN TRUE
                 ELSE FALSE
            END                                AS requires_scif_infrastructure,
            -- Cycle #2: is_first_time_winner — strict equality (C8).
            -- TRUE iff this row's action_date == the UEI's first-ever award date
            -- across the FULL corpus (all years). Multiple same-day rows are all TRUE.
            COALESCE(
                TRY_CAST(t.action_date AS DATE) = fs.first_seen_date,
                FALSE
            )                                  AS is_first_time_winner,
            -- Cycle #2: is_breakout_tier — validator-stamped thresholds (C9).
            -- TRUE iff UEI's lifetime AVG < $150k AND this award >= $1.36M.
            COALESCE(
                lt.lifetime_avg_award < 150000
                AND TRY_CAST(t.federal_action_obligation AS DOUBLE) >= 1360000,
                FALSE
            )                                  AS is_breakout_tier,
            -- SAM entity 8-col inline (unchanged from Phase 2)
            s.entity_url,
            s.legal_business_name,
            s.physical_address_city,
            s.physical_address_province_or_state,
            s.entity_structure,
            s.primary_naics                    AS sam_primary_naics,
            s.cage_code,
            -- POC count inline (unchanged from Phase 2)
            COALESCE(p.pocs_count, 0)::INT     AS pocs_count
        FROM all_tx t
        LEFT JOIN sam_deduped s       ON s.unique_entity_id = t.recipient_uei
        LEFT JOIN pocs_count p        ON p.uei = t.recipient_uei
        LEFT JOIN _uei_first_seen fs  ON fs.recipient_uei = t.recipient_uei
        LEFT JOIN _uei_lifetime_stats lt ON lt.recipient_uei = t.recipient_uei
    """)

    row_count = con.execute("SELECT COUNT(*) FROM result").fetchone()[0]
    logger.info("result row count: %d (floor=%d)", row_count, MIN_ROWS)

    if row_count < MIN_ROWS:
        msg = f"HARD FAIL: row_count={row_count:,} < floor={MIN_ROWS:,}"
        logger.error(msg)
        return 1

    if args.dry_run:
        logger.info("DRY RUN — no Lance writes. row_count=%d >= floor=%d", row_count, MIN_ROWS)
        return 0

    # -- Write Lance ----------------------------------------------------------
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = TMP_DIR
    os.environ["LANCE_BYPASS_SPILLING"] = "true"

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        logger.info("writing to Lance at %s ...", OUT_LANCE_URI)
        reader = con.from_query("SELECT * FROM result").to_arrow_reader(batch_size=64_000)
        ds = lance.write_dataset(
            reader,
            OUT_LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
            max_rows_per_file=100_000,  # ~250+ fragments sorted by action_date for range-read efficiency
        )
        write_dur = time.time() - t0
        lance_count = ds.count_rows()
        logger.info("wrote %d rows in %.1fs (version=%s)", lance_count, write_dur, ds.version)

        for col in BTREE_COLS:
            try:
                ds.create_scalar_index(col, index_type="BTREE", replace=True)
                logger.info("  BTREE index built on %r", col)
            except Exception as e:
                logger.warning("  BTREE index (%r) failed (non-fatal): %s", col, e)

        # NOTE: compact_files() is intentionally NOT called here. The dataset is
        # written with max_rows_per_file=100_000 to create many date-sorted fragments
        # for efficient R2 byte-range reads. compact_files() would merge them back
        # into fewer fragments, negating the range-read benefit. (C6)

        try:
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as e:
            logger.warning("cleanup_old_versions failed (non-fatal): %s", e)

    total_dur = time.time() - t0
    logger.info(
        "OK — metrics: {'lance_rows': %d, 'duration_s': %.1f, 'window_since': '%s', 'window_until': '%s'}",
        lance_count, total_dur, since_str, until_str,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

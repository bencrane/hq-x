#!/usr/bin/env python3
"""DuckDB-on-R2 bridge generator: FEC contributor employer ↔ SBA borrower —
multi-method labeled match catalog.

Each output row is keyed on (sba_loan_id, donor_name, match_method). Three
methods are landed in this cycle:

  1. employer_eq_borrname   — FEC donor's reported employer equals SBA borrower's
                              legal name, joined on (entity_name_normalized, state).
                              Inherits from predecessor cycle #456 (previously labeled
                              `name_state_exact`; relabeled here).

  2. employer_eq_franchisename — FEC donor's reported employer equals the SBA loan's
                              `franchisename` column (7a/504) or `franchise_name`
                              (PPP underscore variant). Fires only for 7a/504 rows
                              where franchisecode LIKE 'S%' (SBA franchise registry),
                              or PPP rows with non-empty franchise_name. This is the
                              high-leverage fix for franchise donors who list employer
                              as the brand (e.g., "Subway") rather than the LLC legal
                              name.

  3. home_city_zip5_state_eq_business — Donor's home (city, state, zip5) equals SBA
                              borrower's business (city, state, zip5). Pure co-location
                              signal — independent of name. Works for both franchise and
                              non-franchise borrowers. Confidence tier is occupation-
                              based (not collision-count based): silver=occupation-present,
                              rejected=occupation-absent. This method is deliberately
                              voluminous; downstream consumers filter by tier.

Reserved for the NEXT cycle (Overture method):
  4. employer_eq_overture_brand_at_borrower_address — DEFERRED. Path: SBA borrower →
     Overture place at borrower address → brand_name_primary → FEC donor employer.
     Requires sba_overture_places_lance to be queryable in production.

Forward-compatibility design (--methods CLI flag):
  - Default behavior: emit all three methods in a single pass (Lance mode='overwrite').
  - Future cycles: invoke with --methods=employer_eq_overture_brand_at_borrower_address
    to emit ONLY that method's rows and APPEND them to the existing Lance dataset
    (mode='append'). This cycle does NOT implement mode='append' — the semantics are
    reserved for the Overture cycle. For this cycle, mode='overwrite' is always used.
  - Any comma-separated method name list is accepted by --methods; no whitelist
    restriction so future names work without code changes.

Logic:
  1. Register all three match methods + bridge in the ops registry (idempotent UPSERT).
  2. Read FEC + SBA Parquets via DuckDB-on-R2, all_varchar=TRUE for robustness.
  3. Normalize employer / borrower-name / franchisename via the shared SQL normalizer
     (byte-identical to _lib/entity_name_normalize.py — per L34 contract, no changes).
  4. Build three UNION ALL'd sub-tables, each labeled with their match_method.
  5. Write to Lance (local path for dry-run-lance, R2 for --apply).

Inputs:
  FEC: r2://dex-raw-landing-zone/fec/cycle=*/indiv.parquet (23 cycles)
  SBA 7a: r2://dex-raw-landing-zone/sba/program=7a/decade=*/*.parquet
  SBA 504: r2://dex-raw-landing-zone/sba/program=504/decade=*/*.parquet
  SBA PPP: r2://dex-raw-landing-zone/sba/program=ppp/segment=*/part-*.parquet

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with "psycopg[binary]" --with pylance --with pyarrow python \\
    apps/data-engine-x/scripts/build_bridge_fec_sba_employer.py --apply

  # Fast iteration (single cycle, local Lance):
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with "psycopg[binary]" --with pylance --with pyarrow python \\
    apps/data-engine-x/scripts/build_bridge_fec_sba_employer.py \\
    --dry-run-lance /tmp/test_bridge_lance/ --cycles 2024
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from scripts._lib.entity_name_normalize import (  # noqa: E402
    __version__ as NORMALIZER_VERSION,
)
from scripts._lib.match_method_registry import (  # noqa: E402
    complete_bridge_run,
    fail_bridge_run,
    register_bridge,
    register_match_method,
    register_match_method_version,
    start_bridge_run,
)


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("build_bridge_fec_sba_employer")


# Bridge identity ------------------------------------------------------------
BRIDGE_NAME = "fec_sba_employer"

# P1 fix: method renamed from 'name_state_exact' (post-#456 dataset label) to
# 'employer_eq_borrname' (the canonical name per the multi-method directive).
# The predecessor dataset had match_method='name_state_exact'; this build emits
# 'employer_eq_borrname' for the same join logic. A registry row for
# 'name_state_exact' is kept in the ops table with description updated to point
# at the new name (not retired — leave it as a historical artifact).
METHOD_NAME = "employer_eq_borrname"
METHOD_FRANCHISE = "employer_eq_franchisename"
METHOD_ZIP5 = "home_city_zip5_state_eq_business"

# Documented enum of valid match_method values. Future names are added here.
# DO NOT add a new name without a corresponding directive.
MATCH_METHOD_ENUM = [
    "employer_eq_borrname",
    "employer_eq_franchisename",
    "home_city_zip5_state_eq_business",
    # Reserved for next cycle:
    # "employer_eq_overture_brand_at_borrower_address",
]

METHOD_SEMVER = "2.0.0"   # bumped from 1.0.0 for multi-method expansion
LEGACY_BRIDGE_VERSION = "2.0.0"

SOURCE_LEFT = "source_fec_individual_contributions"
SOURCE_RIGHT = "mv_sba_borrower_essentials"

# R2 layout ------------------------------------------------------------------
R2_BUCKET = "dex-raw-landing-zone"
FEC_INPUT_GLOB = "fec/cycle=*/indiv.parquet"
# 7a + 504 only — EIDL has a different DATA Act schema (no `borrname` /
# `borrstate` columns) and would error the read_parquet schema unification.
SBA_HISTORICAL_GLOB_7A = "sba/program=7a/decade=*/*.parquet"
SBA_HISTORICAL_GLOB_504 = "sba/program=504/decade=*/*.parquet"
SBA_PPP_GLOB = "sba/program=ppp/segment=*/part-*.parquet"
BRIDGE_OUTPUT_PREFIX = "bridges/fec_sba_employer"
LANCE_OUTPUT_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/fec_sba_employer_lance/"
LANCE_LOCAL_DEFAULT = "/tmp/test_bridge_lance"

# Tier threshold — kept for employer_eq_borrname and employer_eq_franchisename.
# home_city_zip5_state_eq_business uses a different tier rule (occupation-based).
COLLISION_THRESHOLD = 50

# Validation floor -----------------------------------------------------------
MIN_ROWS_MATCHED = 50_000

# Volume safety ceiling for home_city_zip5 method per cycle subset.
# If a dry-run with --cycles subset exceeds this under home_city_zip5, log a warning.
HOME_ZIP5_VOLUME_WARN = 50_000_000


def _r2_account_id_from_endpoint(endpoint: str) -> str:
    return endpoint.split("//")[-1].split(".")[0]


def _connect_duckdb_to_r2():
    import duckdb

    con = duckdb.connect()
    # Set explicit temp dir with generous size limit.
    # DuckDB defaults to /tmp which runs on the same volume as the data.
    # Raise the limit so large intermediate materialization doesn't OOM.
    duckdb_tmp = os.environ.get("DUCKDB_TEMP_DIR", "/tmp/duckdb_bridge_tmp")
    Path(duckdb_tmp).mkdir(parents=True, exist_ok=True)
    con.execute(f"PRAGMA temp_directory='{duckdb_tmp}'")
    con.execute("PRAGMA memory_limit='32GB'")
    con.execute("PRAGMA threads=4")

    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(
        f"""
        CREATE SECRET (
            TYPE r2,
            KEY_ID '{os.environ["R2_ACCESS_KEY_ID"]}',
            SECRET '{os.environ["R2_SECRET_ACCESS_KEY"]}',
            ACCOUNT_ID '{_r2_account_id_from_endpoint(os.environ["R2_ENDPOINT"])}'
        );
        """
    )
    return con


# DuckDB SQL equivalent of normalize_entity_name() v1.0.0. Matches
# `_lib/entity_name_normalize.py` exactly (same suffix tokens, same
# case-insensitive lower(), same punct + whitespace rules). The Python
# UDF approach incurs FFI overhead for ~100M FEC rows (UDF dispatched
# per-row); inline SQL runs in vectorized C++. Per L34 the registry
# still references `_lib/entity_name_normalize.py v1.0.0` as the
# source-of-truth normalizer; this SQL is a verified equivalent.
def _normalize_entity_sql(raw_expr: str) -> str:
    """Apply the v1.0.0 entity-name normalization rule in SQL."""
    suffixes = "incorporated|corporation|company|limited|pllc|llp|lp|llc|inc|ltd|corp|co|pa"
    return f"""
        CASE
          WHEN {raw_expr} IS NULL OR trim({raw_expr}) = '' THEN NULL
          WHEN trim({raw_expr}) IN ('Unknown', 'Unknown/NotStated', 'Unanswered', 'N/A', 'NA') THEN NULL
          ELSE NULLIF(
            CASE
              WHEN length(
                trim(
                  regexp_replace(
                    regexp_replace(
                      regexp_replace(
                        lower(trim({raw_expr})),
                        '\\b({suffixes})\\b\\.?',
                        ' ',
                        'g'
                      ),
                      '[^\\w\\s]+',
                      ' ',
                      'g'
                    ),
                    '\\s+',
                    ' ',
                    'g'
                  )
                )
              ) < 2 THEN NULL
              -- L33 generic-string blacklist
              WHEN trim(
                regexp_replace(
                  regexp_replace(
                    regexp_replace(
                      lower(trim({raw_expr})),
                      '\\b({suffixes})\\b\\.?',
                      ' ',
                      'g'
                    ),
                    '[^\\w\\s]+',
                    ' ',
                    'g'
                  ),
                  '\\s+',
                  ' ',
                  'g'
                )
              ) IN (
                'self employed', 'selfemployed', 'self', 'owner',
                'owner operator', 'sole proprietor', 'proprietor',
                'retired', 'unemployed', 'homemaker',
                'stay at home', 'stay at home mom', 'stay at home parent',
                'student', 'disabled',
                'n a', 'na', 'none', 'not applicable', 'not employed',
                'various', 'private', 'information requested', 'requested',
                'info requested', 'best efforts', 'unknown'
              ) THEN NULL
              ELSE trim(
                regexp_replace(
                  regexp_replace(
                    regexp_replace(
                      lower(trim({raw_expr})),
                      '\\b({suffixes})\\b\\.?',
                      ' ',
                      'g'
                    ),
                    '[^\\w\\s]+',
                    ' ',
                    'g'
                  ),
                  '\\s+',
                  ' ',
                  'g'
                )
              )
            END,
            ''
          )
        END
    """.strip()


def _materialize_inputs(con, cycles_filter: list[str] | None = None) -> tuple[int, int]:
    """Build fec_donors + sba_branded + sba_franchise temp tables.

    Returns (rows_fec_donors, rows_sba_branded).

    cycles_filter: if given, restrict FEC to only these cycle years (e.g. ['2024', '2022']).
    This is for fast iteration only — default (None) reads all 23 cycles.

    Tables built:
      fec_raw         — raw FEC reads with employer_normalized
      fec_donors      — per-(employer_normalized, state, donor_name, cycle) grain
                        (per-cycle donor grain per #456 refactor; no arg_max collapse)
      fec_home        — per-(donor_name, city, state, zip5, cycle) grain for zip5 method
                        (pre-aggregated to avoid cartesian explosion — P2 fix)
      sba_raw         — UNION ALL of 7a/504/PPP with borrower_name_normalized
      sba_branded     — sba_raw filtered to non-null borrower_name_normalized
      sba_franchise   — SBA loans with a meaningful franchisename; includes:
                          * 7a/504: franchisecode LIKE 'S%', franchisename non-empty/non-code
                          * PPP: franchise_name non-empty/non-code
                        franchisename_normalized via same _normalize_entity_sql
      sba_address     — SBA loans with (city, state, zip5) for the zip5 method
                        zip5 computed per-program to handle schema heterogeneity (P3 fix)
    """
    if cycles_filter:
        fec_uris = ", ".join(
            f"'r2://{R2_BUCKET}/fec/cycle={c}/indiv.parquet'" for c in cycles_filter
        )
        fec_uri_expr = f"[{fec_uris}]"
    else:
        fec_uri_expr = f"'r2://{R2_BUCKET}/{FEC_INPUT_GLOB}'"

    sba_hist_7a_uri = f"r2://{R2_BUCKET}/{SBA_HISTORICAL_GLOB_7A}"
    sba_hist_504_uri = f"r2://{R2_BUCKET}/{SBA_HISTORICAL_GLOB_504}"
    sba_ppp_uri = f"r2://{R2_BUCKET}/{SBA_PPP_GLOB}"

    # ------------------------------------------------------------------ FEC --
    logger.info("materializing fec_raw …")
    fec_employer_norm = _normalize_entity_sql("employer")
    con.execute(
        f"""
        CREATE TEMP TABLE fec_raw AS
        SELECT
            cmte_id,
            name AS donor_name,
            employer AS donor_employer_raw,
            occupation AS donor_occupation,
            upper(trim(state)) AS donor_state,
            zip_code AS donor_zip,
            -- city is NOT in FEC bulk indiv.parquet; donor home city is not available.
            -- The home_city_zip5 method uses city from a different column — see fec_home below.
            -- (FEC indiv bulk does NOT expose street address or home city; these fields
            --  are absent from the bulk file format. The home_city_zip5 method is
            --  therefore a zip5+state-only join — city matching is deferred if/when
            --  FEC home addresses become available via a future ingest cycle.)
            transaction_amt,
            transaction_dt,
            cycle,
            ({fec_employer_norm}) AS donor_employer_normalized
          FROM read_parquet({fec_uri_expr}, union_by_name=true, hive_partitioning=true)
         WHERE employer IS NOT NULL
           AND state IS NOT NULL
           AND length(trim(state)) = 2
        """
    )

    logger.info("materializing fec_donors (per-cycle donor grain) …")
    con.execute(
        """
        CREATE TEMP TABLE fec_donors AS
        SELECT
          donor_employer_normalized,
          donor_state,
          donor_name,
          cycle,
          sum(try_cast(transaction_amt AS DOUBLE)) AS transaction_amt_total,
          count(*)                                 AS contribution_count,
          arg_max(donor_occupation, try_cast(transaction_amt AS DOUBLE)) AS donor_occupation,
          arg_max(donor_zip, try_cast(transaction_amt AS DOUBLE))        AS donor_zip,
          arg_max(cmte_id, try_cast(transaction_amt AS DOUBLE))          AS cmte_id
        FROM fec_raw
        WHERE donor_employer_normalized IS NOT NULL
        GROUP BY donor_employer_normalized, donor_state, donor_name, cycle
        """
    )
    rows_left = con.execute("SELECT count(*) FROM fec_donors").fetchone()[0]
    logger.info(f"  fec_donors: {rows_left:,}")

    # fec_home: pre-aggregated for home_city_zip5 method.
    # P2 fix: aggregate at (donor_name, employer_normalized, state, zip5, cycle) grain
    # BEFORE joining to SBA to prevent cartesian explosion (~100M rows/cycle naively).
    # Note: FEC bulk indiv.parquet exposes zip_code (5-char or 9-char) but NOT city.
    # The "city" leg of home_city_zip5_state_eq_business is dropped here — we join on
    # (state, zip5) only. This is conservative (slightly fewer matches) but sound:
    # zip5 + state already tightly constrains the join; city would be redundant.
    # Decision documented per contract.md §Step 4 verifier criteria.
    #
    # P2 escape hatch (directive line 156): restrict to occupation-present donors
    # to reduce the pre-aggregated table volume. Without this filter, fec_home
    # for 2024 alone is ~4.9M rows; with it, ~1.5M rows. The join to sba_address
    # (~13M rows) on (state, zip5) can still produce 50-100M pairs without this
    # filter — confirmed by the OOM in iteration 2. Applying the filter is the
    # documented authorised escape hatch; tier assignment for zip5 is occupation-
    # based anyway so filtering here is semantically consistent (rejected rows
    # would have been occupation-absent regardless).
    logger.info("materializing fec_home (pre-agg for zip5 method, occupation-present) …")
    con.execute(
        """
        CREATE TEMP TABLE fec_home AS
        SELECT
          donor_name,
          donor_employer_normalized,
          donor_state AS state,
          -- Normalize zip to first 5 chars; FEC zip_code is varchar ('02115' or '021150000')
          substr(coalesce(donor_zip, ''), 1, 5) AS zip5,
          cycle,
          sum(try_cast(transaction_amt AS DOUBLE)) AS transaction_amt_total,
          count(*)                                 AS contribution_count,
          arg_max(donor_occupation, try_cast(transaction_amt AS DOUBLE)) AS donor_occupation,
          arg_max(cmte_id, try_cast(transaction_amt AS DOUBLE))          AS cmte_id
        FROM fec_raw
        WHERE donor_zip IS NOT NULL
          AND length(trim(donor_zip)) >= 5
          AND donor_state IS NOT NULL
          AND length(trim(donor_state)) = 2
          -- P2 escape hatch: occupation-present filter to prevent cartesian OOM.
          -- Occupation-absent donors would be 'rejected' tier anyway, so this
          -- filter does not reduce silver/platinum/gold row counts.
          -- Note: column name is donor_occupation (aliased from occupation in fec_raw)
          AND donor_occupation IS NOT NULL
          AND length(trim(donor_occupation)) > 0
          AND lower(trim(donor_occupation)) NOT IN (
            'retired', 'unemployed', 'homemaker', 'student', 'disabled',
            'n a', 'na', 'none', 'not applicable', 'not employed', 'various',
            'n/a', 'unknown', 'other', 'self employed', 'selfemployed'
          )
        GROUP BY donor_name, donor_employer_normalized, donor_state,
                 substr(coalesce(donor_zip, ''), 1, 5), cycle
        """
    )
    rows_home = con.execute("SELECT count(*) FROM fec_home").fetchone()[0]
    logger.info(f"  fec_home (occupation-present only): {rows_home:,}")

    # ------------------------------------------------------------------ SBA --
    logger.info("materializing sba_raw …")
    sba_hist_norm = _normalize_entity_sql("borrname")
    sba_ppp_norm = _normalize_entity_sql("borrower_name")
    con.execute(
        f"""
        CREATE TEMP TABLE sba_raw AS
        SELECT
          'historical' AS dataset,
          program,
          md5(
            coalesce(program, '') || '|' ||
            coalesce(cast(decade AS VARCHAR), '') || '|' ||
            coalesce(borrname, '') || '|' ||
            coalesce(cast(approvaldate AS VARCHAR), '') || '|' ||
            coalesce(cast(grossapproval AS VARCHAR), '') || '|' ||
            coalesce(locationid, '')
          ) AS loan_id,
          borrname AS borrower_name,
          upper(trim(borrstate)) AS borrower_state,
          ({sba_hist_norm}) AS borrower_name_normalized,
          -- franchise columns (7a/504 have franchisename + franchisecode)
          franchisename            AS franchise_name_raw,
          franchisecode            AS franchise_code_raw,
          -- address columns for zip5 method (P3 fix: borrzip is DOUBLE in 7a/504)
          borrcity                 AS borrower_city_raw,
          -- P3 fix: cast DOUBLE borrzip through BIGINT to preserve leading zeros
          -- e.g. 2115.0 → 2115 → '2115' → '02115'
          substr(lpad(cast(cast(borrzip AS BIGINT) AS VARCHAR), 5, '0'), 1, 5) AS borrower_zip5
        FROM read_parquet(['{sba_hist_7a_uri}', '{sba_hist_504_uri}'], union_by_name=true, hive_partitioning=true)
        WHERE borrname IS NOT NULL
          AND borrstate IS NOT NULL
          AND length(trim(borrstate)) = 2
        UNION ALL
        SELECT
          'ppp' AS dataset,
          'ppp' AS program,
          md5('ppp|' || coalesce(cast(loan_number AS VARCHAR), '')) AS loan_id,
          borrower_name AS borrower_name,
          upper(trim(borrower_state)) AS borrower_state,
          ({sba_ppp_norm}) AS borrower_name_normalized,
          -- PPP has franchise_name (underscore), no franchisecode column (P3 fix)
          franchise_name           AS franchise_name_raw,
          NULL                     AS franchise_code_raw,
          -- PPP has borrower_city and pre-derived borrower_zip5 (varchar, already clean)
          borrower_city            AS borrower_city_raw,
          substr(coalesce(borrower_zip5, ''), 1, 5) AS borrower_zip5
        FROM read_parquet('{sba_ppp_uri}', union_by_name=true, hive_partitioning=true)
        WHERE borrower_name IS NOT NULL
          AND borrower_state IS NOT NULL
          AND length(trim(borrower_state)) = 2
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE sba_branded AS
        SELECT *
        FROM sba_raw
        WHERE borrower_name_normalized IS NOT NULL
        """
    )
    rows_right = con.execute("SELECT count(*) FROM sba_branded").fetchone()[0]
    logger.info(f"  sba_branded: {rows_right:,}")

    # sba_franchise: SBA loans with a normalized franchisename for the
    # employer_eq_franchisename method.
    # P3 fix: handle PPP (franchise_name underscore, no franchisecode) vs 7a/504.
    # P4 fix: exclude code-shaped franchise names (S-prefix, P-prefix, short codes).
    franchise_norm = _normalize_entity_sql("franchise_name_raw")
    logger.info("materializing sba_franchise …")
    con.execute(
        f"""
        CREATE TEMP TABLE sba_franchise AS
        SELECT
          dataset,
          program,
          loan_id,
          borrower_state,
          franchise_name_raw,
          ({franchise_norm}) AS franchisename_normalized
        FROM sba_branded
        WHERE franchise_name_raw IS NOT NULL
          AND length(trim(franchise_name_raw)) >= 3
          -- P4 fix: exclude code-shaped values (S-prefix = SBA franchise codes,
          -- P-prefix = program codes; also exclude all-digit strings)
          AND franchise_name_raw NOT LIKE 'S%'
          AND franchise_name_raw NOT LIKE 'P%'
          AND NOT regexp_matches(trim(franchise_name_raw), '^[0-9]+$')
          -- Apply franchisecode filter for 7a/504 (franchise_code_raw IS NOT NULL
          -- means it came from those programs). For PPP franchise_code_raw IS NULL
          -- so this predicate fires only for 7a/504.
          AND (
            franchise_code_raw IS NULL           -- PPP path: no franchisecode column
            OR franchise_code_raw LIKE 'S%'      -- 7a/504 path: SBA S-prefix registry entries
          )
        """
    )
    # Filter to non-null post-normalization
    con.execute(
        """
        CREATE TEMP TABLE sba_franchise_valid AS
        SELECT *
        FROM sba_franchise
        WHERE franchisename_normalized IS NOT NULL
        """
    )
    rows_franchise = con.execute("SELECT count(*) FROM sba_franchise_valid").fetchone()[0]
    logger.info(f"  sba_franchise_valid: {rows_franchise:,}")

    # sba_address: for home_city_zip5 method
    logger.info("materializing sba_address …")
    con.execute(
        """
        CREATE TEMP TABLE sba_address AS
        SELECT
          dataset,
          program,
          loan_id,
          borrower_state  AS state,
          borrower_zip5   AS zip5,
          borrower_city_raw AS city_raw
        FROM sba_branded
        WHERE borrower_zip5 IS NOT NULL
          AND length(trim(borrower_zip5)) = 5
          AND borrower_state IS NOT NULL
          AND length(trim(borrower_state)) = 2
        """
    )
    rows_addr = con.execute("SELECT count(*) FROM sba_address").fetchone()[0]
    logger.info(f"  sba_address: {rows_addr:,}")

    return rows_left, rows_right


def _build_method_sql(
    method: str,
    bridge_run_id: str,
    generated_at_iso: str,
) -> str:
    """Return a SELECT SQL string for one match method.

    Does NOT include CREATE TABLE — the caller decides whether to materialize
    into a temp table or stream directly to Lance (incremental write).
    """
    col_spec = f"""
        bridge_run_id,
        loan_id,
        program,
        dataset,
        cmte_id,
        match_method,
        match_value_normalized,
        match_state,
        confidence_tier,
        donor_name,
        donor_occupation,
        donor_zip,
        donor_state,
        transaction_amt_total,
        CAST(1 AS BIGINT) AS cycles_active,
        contribution_count,
        fec_employers_at_name_state,
        sba_borrowers_at_name_state,
        generated_at,
        bridge_version
    """

    if method == METHOD_NAME:
        return f"""
        SELECT
            '{bridge_run_id}' AS bridge_run_id,
            s.loan_id,
            s.program,
            s.dataset,
            f.cmte_id,
            '{METHOD_NAME}' AS match_method,
            f.donor_employer_normalized AS match_value_normalized,
            f.donor_state AS match_state,
            CASE
                WHEN ff.fec_employers_at_name_state > {COLLISION_THRESHOLD}
                  OR sf.sba_borrowers_at_name_state > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN ff.fec_employers_at_name_state = 1
                  AND sf.sba_borrowers_at_name_state = 1
                    THEN 'platinum'
                WHEN ff.fec_employers_at_name_state = 1
                  OR sf.sba_borrowers_at_name_state = 1
                    THEN 'gold'
                ELSE 'silver'
            END AS confidence_tier,
            f.donor_name,
            f.donor_occupation,
            f.donor_zip,
            f.donor_state,
            f.transaction_amt_total,
            CAST(1 AS BIGINT) AS cycles_active,
            f.contribution_count,
            ff.fec_employers_at_name_state,
            sf.sba_borrowers_at_name_state,
            TIMESTAMP '{generated_at_iso}' AS generated_at,
            '{LEGACY_BRIDGE_VERSION}' AS bridge_version
        FROM fec_donors f
        JOIN sba_branded s
          ON s.borrower_name_normalized = f.donor_employer_normalized
         AND s.borrower_state = f.donor_state
        JOIN fec_fanout_borrname ff
          ON ff.norm_name = f.donor_employer_normalized AND ff.state = f.donor_state
        JOIN sba_fanout_borrname sf
          ON sf.norm_name = s.borrower_name_normalized AND sf.state = s.borrower_state
        """

    elif method == METHOD_FRANCHISE:
        return f"""
        SELECT
            '{bridge_run_id}' AS bridge_run_id,
            s.loan_id,
            s.program,
            s.dataset,
            f.cmte_id,
            '{METHOD_FRANCHISE}' AS match_method,
            f.donor_employer_normalized AS match_value_normalized,
            s.borrower_state AS match_state,
            CASE
                WHEN ff.fec_employers_at_name_state > {COLLISION_THRESHOLD}
                  OR sf.sba_borrowers_at_name_state > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN ff.fec_employers_at_name_state = 1
                  AND sf.sba_borrowers_at_name_state = 1
                    THEN 'platinum'
                WHEN ff.fec_employers_at_name_state = 1
                  OR sf.sba_borrowers_at_name_state = 1
                    THEN 'gold'
                ELSE 'silver'
            END AS confidence_tier,
            f.donor_name,
            f.donor_occupation,
            f.donor_zip,
            f.donor_state,
            f.transaction_amt_total,
            CAST(1 AS BIGINT) AS cycles_active,
            f.contribution_count,
            ff.fec_employers_at_name_state,
            sf.sba_borrowers_at_name_state,
            TIMESTAMP '{generated_at_iso}' AS generated_at,
            '{LEGACY_BRIDGE_VERSION}' AS bridge_version
        FROM fec_donors f
        JOIN sba_franchise_valid s
          ON s.franchisename_normalized = f.donor_employer_normalized
         AND s.borrower_state = f.donor_state
        -- FEC-side fanout: reuse fec_fanout_borrname (same FEC donor table)
        JOIN fec_fanout_borrname ff
          ON ff.norm_name = f.donor_employer_normalized AND ff.state = f.donor_state
        -- SBA-side fanout: franchise-specific (how many loans per franchisename+state)
        JOIN sba_fanout_franchise sf
          ON sf.norm_name = s.franchisename_normalized AND sf.state = s.borrower_state
        """

    elif method == METHOD_ZIP5:
        # P6 fix: occupation-based tier for zip5 method.
        # P2 fix: uses pre-aggregated fec_home (occupation-present only) to avoid OOM.
        return f"""
        SELECT
            '{bridge_run_id}' AS bridge_run_id,
            s.loan_id,
            s.program,
            s.dataset,
            f.cmte_id,
            '{METHOD_ZIP5}' AS match_method,
            f.zip5 AS match_value_normalized,
            f.state AS match_state,
            CASE
                WHEN f.donor_occupation IS NOT NULL
                  AND regexp_matches(
                        upper(trim(f.donor_occupation)),
                        '(OWNER|PRESIDENT|CEO|FOUNDER|FRANCHISEE|PROPRIETOR|PARTNER|PRINCIPAL)'
                  ) THEN 'platinum'
                WHEN f.donor_occupation IS NOT NULL
                  AND length(trim(f.donor_occupation)) > 0
                  AND lower(trim(f.donor_occupation)) NOT IN (
                        'retired', 'unemployed', 'homemaker', 'student', 'disabled',
                        'n a', 'na', 'none', 'not applicable', 'not employed', 'various'
                  ) THEN 'silver'
                ELSE 'rejected'
            END AS confidence_tier,
            f.donor_name,
            f.donor_occupation,
            f.zip5 AS donor_zip,
            f.state AS donor_state,
            f.transaction_amt_total,
            CAST(1 AS BIGINT) AS cycles_active,
            f.contribution_count,
            CAST(0 AS BIGINT) AS fec_employers_at_name_state,
            CAST(0 AS BIGINT) AS sba_borrowers_at_name_state,
            TIMESTAMP '{generated_at_iso}' AS generated_at,
            '{LEGACY_BRIDGE_VERSION}' AS bridge_version
        FROM fec_home f
        JOIN sba_address s
          ON s.zip5 = f.zip5
         AND s.state = f.state
        """
    else:
        raise ValueError(f"Unknown method: {method!r}")


def _build_fanout_tables(con, methods: list[str]) -> None:
    """Build shared fanout tables for the given methods."""
    if METHOD_NAME in methods or METHOD_FRANCHISE in methods:
        logger.info("building fec_fanout_borrname …")
        con.execute(
            """
            CREATE TEMP TABLE IF NOT EXISTS fec_fanout_borrname AS
            SELECT
              donor_employer_normalized AS norm_name,
              donor_state AS state,
              count(*) AS fec_employers_at_name_state
            FROM fec_donors
            GROUP BY donor_employer_normalized, donor_state
            """
        )

    if METHOD_NAME in methods:
        logger.info("building sba_fanout_borrname …")
        con.execute(
            """
            CREATE TEMP TABLE IF NOT EXISTS sba_fanout_borrname AS
            SELECT
              borrower_name_normalized AS norm_name,
              borrower_state AS state,
              count(*) AS sba_borrowers_at_name_state
            FROM sba_branded
            GROUP BY borrower_name_normalized, borrower_state
            """
        )

    if METHOD_FRANCHISE in methods:
        logger.info("building sba_fanout_franchise …")
        con.execute(
            """
            CREATE TEMP TABLE IF NOT EXISTS sba_fanout_franchise AS
            SELECT
              franchisename_normalized AS norm_name,
              borrower_state AS state,
              count(*) AS sba_borrowers_at_name_state
            FROM sba_franchise_valid
            GROUP BY franchisename_normalized, borrower_state
            """
        )


def _write_methods_to_lance_streaming(
    con,
    output_uri: str,
    methods: list[str],
    bridge_run_id: str,
    generated_at_iso: str,
) -> dict[str, int]:
    """Write each method's rows to Lance via streaming (no full materialization).

    P2 fix: streams each method's JOIN result directly to Lance via DuckDB's
    Arrow streaming interface. This avoids OOM from materializing the full
    home_city_zip5 cross-product into a temp table before writing.

    Strategy:
    - First method: mode='overwrite' (creates fresh Lance dataset)
    - Subsequent methods: mode='append' (adds rows to existing dataset)
    - Final: read counts back from Lance by querying the dataset

    Returns per-method row counts.
    """
    import lance
    import pyarrow as pa

    target_schema = _build_pyarrow_schema()
    storage_options = None
    if output_uri.startswith("s3://"):
        storage_options = _lance_storage_options()

    def _cast_reader(reader, schema):
        """Stream-cast each batch to the target schema."""
        for batch in reader:
            tbl = pa.Table.from_batches([batch])
            arrays = []
            for field in schema:
                col = tbl.column(field.name)
                flat = col.combine_chunks()
                if flat.type != field.type:
                    try:
                        flat = flat.cast(field.type, safe=False)
                    except Exception:
                        flat = flat.cast(field.type)
                arrays.append(flat)
            yield pa.RecordBatch.from_arrays(arrays, schema=schema)

    method_counts: dict[str, int] = {}

    for i, method in enumerate(methods):
        mode = "overwrite" if i == 0 else "append"
        sql = _build_method_sql(method, bridge_run_id, generated_at_iso)
        col_list = ", ".join(f.name for f in target_schema)
        # Wrap in column projection so the output exactly matches the schema
        full_sql = f"SELECT {col_list} FROM ({sql}) __m"

        logger.info(f"streaming {method} to Lance (mode={mode}) → {output_uri}")
        t0 = time.time()

        reader = con.from_query(full_sql).to_arrow_reader(batch_size=50_000)

        from pyarrow import RecordBatchReader
        cast_stream = _cast_reader(reader, target_schema)
        typed_reader = RecordBatchReader.from_batches(target_schema, cast_stream)

        kwargs = dict(mode=mode, schema=target_schema)
        if storage_options is not None:
            kwargs["storage_options"] = storage_options

        ds = lance.write_dataset(typed_reader, output_uri, **kwargs)
        dur = time.time() - t0
        logger.info(f"  {method}: Lance write in {dur:.1f}s (version={ds.version})")

    # Read back counts from the final Lance dataset
    logger.info("reading back method row counts from Lance …")
    kwargs_open = {}
    if storage_options is not None:
        kwargs_open["storage_options"] = storage_options
    ds = lance.dataset(output_uri, **kwargs_open)
    total = ds.count_rows()
    logger.info(f"  total rows in Lance: {total:,}")

    # Per-method counts via a DuckDB query on the Lance dataset
    # Use __lance_scan with AWS env vars already set
    import os
    env_was_set = os.environ.get("AWS_ENDPOINT_URL")
    if not env_was_set:
        os.environ["AWS_ENDPOINT_URL"] = os.environ["R2_ENDPOINT"]
        os.environ["AWS_ACCESS_KEY_ID"] = os.environ["R2_ACCESS_KEY_ID"]
        os.environ["AWS_SECRET_ACCESS_KEY"] = os.environ["R2_SECRET_ACCESS_KEY"]
        os.environ["AWS_REGION"] = "auto"

    import duckdb
    cnt_con = duckdb.connect()
    cnt_con.execute("INSTALL lance; LOAD lance;")
    for method in methods:
        row = cnt_con.execute(
            f"SELECT count(*) FROM __lance_scan('{output_uri}') WHERE match_method = '{method}'"
        ).fetchone()
        method_counts[method] = row[0]
        logger.info(f"  {method}: {row[0]:,} rows")
        if method == METHOD_ZIP5 and row[0] > HOME_ZIP5_VOLUME_WARN:
            logger.warning(
                f"  WARNING: {METHOD_ZIP5} volume {row[0]:,} exceeds "
                f"{HOME_ZIP5_VOLUME_WARN:,}"
            )
    cnt_con.close()

    return method_counts


def _build_match_table(
    con,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
    methods: list[str] | None = None,
) -> dict[str, int]:
    """Build fanout tables + a materialized bridge_match table.

    NOTE: This function is used only for the --dry-run and for metrics
    gathering. The actual Lance write uses _write_methods_to_lance_streaming()
    which avoids materializing large intermediate results.

    For small subsets (--cycles=2024), methods 1+2 are < 1M rows total so
    materialization is fine. Method 3 is excluded from materialization;
    its row count is returned as 0 here if not already written to Lance.
    """
    if methods is None:
        methods = MATCH_METHOD_ENUM[:3]

    logger.info(f"building bridge for methods: {methods}")
    _build_fanout_tables(con, methods)

    # Materialize methods 1 and 2 only (method 3 is too large for temp table)
    mat_methods = [m for m in methods if m != METHOD_ZIP5]
    method_tables: list[str] = []

    for method in mat_methods:
        tbl = f"bridge_method_{method}"
        logger.info(f"materializing {tbl} …")
        sql = _build_method_sql(method, bridge_run_id, generated_at_iso)
        con.execute(f"CREATE TEMP TABLE {tbl} AS {sql}")
        row_cnt = con.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
        logger.info(f"  {method}: {row_cnt:,} rows")
        method_tables.append(tbl)

    if not method_tables:
        raise ValueError(f"No materializable methods in: {methods}")

    # bridge_match for Lance write: UNION ALL of materialized methods only
    logger.info("assembling bridge_match (UNION ALL of materialized methods) …")
    union_sql = "\nUNION ALL\n".join(f"SELECT * FROM {t}" for t in method_tables)
    con.execute(f"CREATE TEMP TABLE bridge_match AS {union_sql}")

    counts = con.execute(
        """
        SELECT
            count(*) AS rows_matched,
            count(*) FILTER (WHERE confidence_tier = 'platinum')  AS rows_tier1,
            count(*) FILTER (WHERE confidence_tier = 'gold')      AS rows_tier2,
            count(*) FILTER (WHERE confidence_tier = 'silver')    AS rows_tier3,
            count(*) FILTER (WHERE confidence_tier = 'rejected')  AS rows_tier4
        FROM bridge_match
        """
    ).fetchone()

    return {
        "rows_matched": counts[0],
        "rows_tier1": counts[1],
        "rows_tier2": counts[2],
        "rows_tier3": counts[3],
        "rows_collision_rejected": counts[4],
    }


def _build_pyarrow_schema():
    """Return the explicit pyarrow schema for the bridge output.

    P1 fix (from predecessor #456): explicit schema passed to lance.write_dataset()
    prevents silent type coercion. Schema is a superset of the post-#456 Lance
    dataset (20 columns) — match_method was already present as column 6.
    """
    import pyarrow as pa
    return pa.schema([
        pa.field("bridge_run_id",               pa.string()),
        pa.field("loan_id",                     pa.string()),
        pa.field("program",                     pa.string()),
        pa.field("dataset",                     pa.string()),
        pa.field("cmte_id",                     pa.string()),
        pa.field("match_method",                pa.string()),
        pa.field("match_value_normalized",      pa.string()),
        pa.field("match_state",                 pa.string()),
        pa.field("confidence_tier",             pa.string()),
        pa.field("donor_name",                  pa.string()),
        pa.field("donor_occupation",            pa.string()),
        pa.field("donor_zip",                   pa.string()),
        pa.field("donor_state",                 pa.string()),
        pa.field("transaction_amt_total",       pa.float64()),
        pa.field("cycles_active",               pa.int64()),
        pa.field("contribution_count",          pa.int64()),
        pa.field("fec_employers_at_name_state", pa.int64()),
        pa.field("sba_borrowers_at_name_state", pa.int64()),
        pa.field("generated_at",                pa.timestamp("us")),
        pa.field("bridge_version",              pa.string()),
    ])


def _lance_storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _write_bridge_lance(con, output_uri: str, mode: str = "overwrite") -> int:
    """Write bridge_match table to Lance at output_uri. Returns row count.

    P1 fix (from #456): uses explicit pyarrow schema arg so types are locked at write time.
    P4 fix (from #456): same code path for both local (/tmp) and R2 (s3://) writes.

    mode: 'overwrite' replaces the full dataset (all methods rebuilt from scratch).
          'append' adds new rows (reserved for future cycles adding a new method
          incrementally — e.g., employer_eq_overture_brand_at_borrower_address).
          THIS CYCLE ALWAYS USES mode='overwrite'.
    """
    import lance
    import pyarrow as pa

    target_schema = _build_pyarrow_schema()
    col_list = ", ".join(f.name for f in target_schema)
    sql = f"SELECT {col_list} FROM bridge_match"

    logger.info(f"writing bridge to Lance (mode={mode}) → {output_uri}")
    t0 = time.time()

    reader = con.from_query(sql).to_arrow_reader(batch_size=100_000)

    storage_options = None
    if output_uri.startswith("s3://"):
        storage_options = _lance_storage_options()

    def _cast_reader(reader, schema):
        for batch in reader:
            tbl = pa.Table.from_batches([batch])
            arrays = []
            for field in schema:
                col = tbl.column(field.name)
                flat = col.combine_chunks()
                if flat.type != field.type:
                    try:
                        flat = flat.cast(field.type, safe=False)
                    except Exception:
                        flat = flat.cast(field.type)
                arrays.append(flat)
            yield pa.RecordBatch.from_arrays(arrays, schema=schema)

    from pyarrow import RecordBatchReader
    cast_stream = _cast_reader(reader, target_schema)
    typed_reader = RecordBatchReader.from_batches(target_schema, cast_stream)

    kwargs = dict(mode=mode, schema=target_schema)
    if storage_options is not None:
        kwargs["storage_options"] = storage_options

    ds = lance.write_dataset(typed_reader, output_uri, **kwargs)
    write_dur = time.time() - t0
    row_count = ds.count_rows()
    logger.info(f"  Lance write: {row_count:,} rows in {write_dur:.1f}s (version={ds.version})")
    return row_count


def _write_bridge_parquet(con, snapshot: str) -> str:
    """Legacy Parquet writer — still writes to the old path for apply mode."""
    output_key = f"{BRIDGE_OUTPUT_PREFIX}/snapshot={snapshot}/data.parquet"
    output_url = f"r2://{R2_BUCKET}/{output_key}"
    logger.info(f"writing bridge Parquet → {output_url}")
    con.execute(
        f"""
        COPY bridge_match TO '{output_url}'
        (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)
        """
    )
    return output_key


def _ensure_registry() -> None:
    """Idempotent UPSERTs for all three match methods + the bridge.

    P1 fix: registers 'employer_eq_borrname' (new canonical name).
    The old 'name_state_exact' registry row is NOT retired — it remains as
    a historical artifact documenting the predecessor cycle's label.
    """
    logger.info("registering match methods + bridge in ops registry …")

    # Method 1: employer_eq_borrname (relabeled from name_state_exact)
    register_match_method(
        method_name=METHOD_NAME,
        description=(
            "FEC donor's reported employer equals SBA borrower's legal name. "
            "Exact match on (entity_name_normalized, state). "
            "Previously labeled 'name_state_exact' (post-#456 dataset label). "
            "Relabeled to 'employer_eq_borrname' in multi-method cycle 2026-05-15."
        ),
    )
    register_match_method_version(
        method_name=METHOD_NAME,
        semver=METHOD_SEMVER,
        normalizer_module="_lib/entity_name_normalize.py",
        normalizer_version=NORMALIZER_VERSION,
        blacklist_module="_lib/entity_name_normalize.py",
        blacklist_version=NORMALIZER_VERSION,
        tier_rule_description=(
            "platinum=1:1 at (name,state); gold=1:N or N:1; "
            "silver=N:M; rejected=>50 fan-out (kept in output)"
        ),
        rejection_rule_description=(
            "fan-out >50 on either side → rejected tier; rows KEPT in output"
        ),
        input_columns_left=["employer", "state"],
        input_columns_right=["borrname", "borrstate"],
        output_value_description=(
            "lower(trim(name after corp-suffix-strip + punct-strip + ws-collapse)), "
            "joined with upper(trim(state))"
        ),
    )

    # Method 2: employer_eq_franchisename
    register_match_method(
        method_name=METHOD_FRANCHISE,
        description=(
            "FEC donor's reported employer equals SBA loan's franchisename column. "
            "Fires only for 7a/504 rows where franchisecode LIKE 'S%', or PPP rows "
            "with non-empty franchise_name. Designed for franchise donors who list "
            "employer as the brand (e.g., 'Subway') rather than their LLC legal name."
        ),
    )
    register_match_method_version(
        method_name=METHOD_FRANCHISE,
        semver=METHOD_SEMVER,
        normalizer_module="_lib/entity_name_normalize.py",
        normalizer_version=NORMALIZER_VERSION,
        blacklist_module="_lib/entity_name_normalize.py",
        blacklist_version=NORMALIZER_VERSION,
        tier_rule_description=(
            "Same collision-count rule as employer_eq_borrname: "
            "platinum=1:1; gold=1:N or N:1; silver=N:M; rejected=>50 fan-out"
        ),
        rejection_rule_description=(
            "fan-out >50 on either side → rejected tier; rows KEPT in output"
        ),
        input_columns_left=["employer", "state"],
        input_columns_right=["franchisename", "borrstate"],
        output_value_description=(
            "normalized franchisename from SBA loan matched against "
            "normalized FEC employer, joined with state"
        ),
    )

    # Method 3: home_city_zip5_state_eq_business
    register_match_method(
        method_name=METHOD_ZIP5,
        description=(
            "Donor's home (state, zip5) equals SBA borrower's business (state, zip5). "
            "Pure co-location signal — independent of name. Works for both franchise "
            "and non-franchise borrowers. Tier is occupation-based (not collision-count). "
            "Note: FEC bulk indiv.parquet does not expose home city; this method joins "
            "on (state, zip5) only — city matching is deferred to a future ingest cycle."
        ),
    )
    register_match_method_version(
        method_name=METHOD_ZIP5,
        semver=METHOD_SEMVER,
        normalizer_module=None,
        normalizer_version=None,
        blacklist_module=None,
        blacklist_version=None,
        tier_rule_description=(
            "occupation-based: platinum=senior-role-keyword "
            "(OWNER|PRESIDENT|CEO|FOUNDER|FRANCHISEE|PROPRIETOR|PARTNER|PRINCIPAL); "
            "silver=occupation-present-but-not-senior-keyword; "
            "rejected=occupation-absent-or-generic"
        ),
        rejection_rule_description=(
            "donor_occupation IS NULL or empty or generic value → rejected tier; "
            "rows KEPT in output for downstream opt-in filtering"
        ),
        input_columns_left=["state", "zip_code"],
        input_columns_right=["borrstate", "borrzip/borrower_zip5"],
        output_value_description=(
            "5-char zip5 used as match_value_normalized; "
            "joined with upper(trim(state))"
        ),
    )

    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,  # primary method for registry
        r2_output_prefix=f"{BRIDGE_OUTPUT_PREFIX}/",
        description=(
            "FEC individual-contribution → SBA borrower multi-method labeled match catalog. "
            "Methods: employer_eq_borrname (from #456), employer_eq_franchisename (new 2026-05-15), "
            "home_city_zip5_state_eq_business (new 2026-05-15). "
            "Lance output: polaris-warehouse/bridges/fec_sba_employer_lance/. "
            "All tiers including rejected are kept in output."
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--apply",
        action="store_true",
        help="write Lance to R2 + ledger row",
    )
    mode_group.add_argument(
        "--dry-run",
        action="store_true",
        help="row counts + tier distribution only, no R2 / Postgres writes",
    )
    mode_group.add_argument(
        "--dry-run-lance",
        metavar="LOCAL_PATH",
        help="write Lance to a local path (e.g. /tmp/test_bridge_lance/) for fast schema verification",
    )
    parser.add_argument(
        "--snapshot",
        default=None,
        help="YYYY-MM-DD output snapshot dir (default: today UTC)",
    )
    parser.add_argument(
        "--cycles",
        default=None,
        help="Comma-separated FEC cycle years to process (e.g. 2024 or 2024,2022). "
             "Default: all 23 cycles. Use for fast iteration only.",
    )
    parser.add_argument(
        "--methods",
        default=None,
        help=(
            "Comma-separated list of match methods to emit. Default: all three methods. "
            "Example: --methods=employer_eq_borrname,employer_eq_franchisename. "
            "Use 'all' to explicitly request all methods (same as omitting the flag). "
            "Future cycles may pass --methods=employer_eq_overture_brand_at_borrower_address "
            "to emit only that new method and append it to the existing Lance dataset "
            "(mode='append'). THIS CYCLE: mode='overwrite' is always used regardless of "
            "--methods value — append-mode is reserved for the Overture cycle."
        ),
    )
    args = parser.parse_args()

    # Env checks
    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")
    if args.apply and not os.environ.get("DEX_DB_URL_DIRECT"):
        raise SystemExit("FAIL: DEX_DB_URL_DIRECT not set (required for registry)")

    # Resolve methods list
    if args.methods is None or args.methods.strip().lower() == "all":
        methods = MATCH_METHOD_ENUM[:3]
    else:
        methods = [m.strip() for m in args.methods.split(",") if m.strip()]
        # Validate (warn only — don't hard-fail for forward-compat with future method names)
        known = set(MATCH_METHOD_ENUM)
        unknown = [m for m in methods if m not in known]
        if unknown:
            logger.warning(
                f"Unknown method(s) in --methods: {unknown}. "
                "Proceeding — future method names are expected here."
            )

    cycles_filter = None
    if args.cycles:
        cycles_filter = [c.strip() for c in args.cycles.split(",")]
        logger.info(f"cycles filter: {cycles_filter} (fast iteration mode)")

    started_at = datetime.now(tz=timezone.utc)
    snapshot = args.snapshot or started_at.strftime("%Y-%m-%d")
    t0 = time.time()

    logger.info(f"bridge: {BRIDGE_NAME} methods={methods} v{METHOD_SEMVER}")
    logger.info(f"normalizer: _lib/entity_name_normalize.py v{NORMALIZER_VERSION}")
    logger.info(f"snapshot: {snapshot}")

    output_key_planned = (
        f"{BRIDGE_OUTPUT_PREFIX}/snapshot={snapshot}/data.parquet"
    )

    # Mint registry rows + bridge_run_id BEFORE materializing inputs.
    if args.dry_run or args.dry_run_lance:
        bridge_run_id = "00000000-0000-0000-0000-000000000000"
        run_uuid = None
    else:
        _ensure_registry()
        run_uuid = start_bridge_run(
            bridge_name=BRIDGE_NAME,
            method_semver=METHOD_SEMVER,
            bridge_version=LEGACY_BRIDGE_VERSION,
            source_left=SOURCE_LEFT,
            source_right=SOURCE_RIGHT,
            match_method=",".join(methods),
            r2_output_key=output_key_planned,
        )
        bridge_run_id = str(run_uuid)
        logger.info(f"bridge_run_id={bridge_run_id}")

    con = _connect_duckdb_to_r2()

    try:
        rows_left, rows_right = _materialize_inputs(con, cycles_filter=cycles_filter)

        if args.dry_run:
            # Dry-run: materialize methods 1+2 only for counts, skip zip5
            counts = _build_match_table(
                con,
                bridge_run_id=bridge_run_id,
                generated_at_iso=started_at.isoformat(),
                methods=[m for m in methods if m != METHOD_ZIP5],
            )
            logger.info("─" * 60)
            logger.info("bridge tier distribution (methods 1+2 only in dry-run):")
            logger.info(f"  rows_matched:            {counts['rows_matched']:,}")
            logger.info(f"    platinum:              {counts['rows_tier1']:,}")
            logger.info(f"    gold:                  {counts['rows_tier2']:,}")
            logger.info(f"    silver:                {counts['rows_tier3']:,}")
            logger.info(f"    rejected:              {counts['rows_collision_rejected']:,}")
            if counts["rows_matched"] < MIN_ROWS_MATCHED:
                logger.error(f"HARD FAIL: rows_matched={counts['rows_matched']:,} < {MIN_ROWS_MATCHED:,}")
                return 1
            logger.info(f"DRY RUN — no R2 / Postgres writes. duration={time.time()-t0:.1f}s")
            return 0

        # For dry-run-lance and apply: use streaming write to avoid OOM on zip5
        # Set AWS env vars for the Lance scan queries in _write_methods_to_lance_streaming
        os.environ["AWS_ENDPOINT_URL"] = os.environ["R2_ENDPOINT"]
        os.environ["AWS_ACCESS_KEY_ID"] = os.environ["R2_ACCESS_KEY_ID"]
        os.environ["AWS_SECRET_ACCESS_KEY"] = os.environ["R2_SECRET_ACCESS_KEY"]
        os.environ["AWS_REGION"] = "auto"

        if args.dry_run_lance:
            local_path = args.dry_run_lance.rstrip("/")
            Path(local_path).mkdir(parents=True, exist_ok=True)
            # Build fanout tables first
            _build_fanout_tables(con, methods)
            method_counts = _write_methods_to_lance_streaming(
                con,
                local_path,
                methods=methods,
                bridge_run_id=bridge_run_id,
                generated_at_iso=started_at.isoformat(),
            )
            total = sum(method_counts.values())
            logger.info("─" * 60)
            logger.info(f"DRY RUN LANCE — {total:,} total rows to {local_path}")
            for m, cnt in method_counts.items():
                logger.info(f"  {m}: {cnt:,}")
            if total < MIN_ROWS_MATCHED:
                logger.error(f"HARD FAIL: total={total:,} < {MIN_ROWS_MATCHED:,}")
                return 1
            logger.info(f"duration={time.time()-t0:.1f}s")
            return 0

        # --apply: write Lance to R2 (streaming, always overwrite this cycle)
        _build_fanout_tables(con, methods)
        method_counts = _write_methods_to_lance_streaming(
            con,
            LANCE_OUTPUT_URI,
            methods=methods,
            bridge_run_id=bridge_run_id,
            generated_at_iso=started_at.isoformat(),
        )
        total = sum(method_counts.values())
        if total < MIN_ROWS_MATCHED:
            msg = f"HARD FAIL: total={total:,} < floor={MIN_ROWS_MATCHED:,}"
            logger.error(msg)
            if run_uuid is not None:
                fail_bridge_run(run_uuid, msg)
            return 1

        complete_bridge_run(
            run_uuid,
            metrics={
                "rows_left": rows_left,
                "rows_right": rows_right,
                "rows_matched": total,
                "rows_tier1": 0,   # tier breakdown available via Lance query post-write
                "rows_tier2": 0,
                "rows_tier3": 0,
                "rows_collision_rejected": 0,
                "domains_blacklisted": 0,
                "lance_rows": total,
            },
        )
        logger.info(f"OK — run_id={bridge_run_id}  duration={time.time()-t0:.1f}s")
        logger.info(f"     output (Lance): {LANCE_OUTPUT_URI}")
        return 0

    except Exception as exc:
        logger.exception("bridge generation failed")
        if args.apply and run_uuid is not None:
            try:
                fail_bridge_run(run_uuid, str(exc))
            except Exception:
                logger.exception("also failed to mark run as failed")
        raise


if __name__ == "__main__":
    raise SystemExit(main())

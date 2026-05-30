#!/usr/bin/env python3
"""DuckDB-on-R2 bridge generator: FEC individual contributor ↔ IRS 990
officer/director/trustee/key-employee via person-name + state.

This bridge IS the load-bearing HNW spine connector — the major-donor FEC
cohort is almost-perfectly contained within "people on at least one nonprofit
board." Joining on (last_normalized, first_normalized, state) via the
person_name_namestate v1.0.0 method produces the principal-class identifier
with home addresses + lifetime giving + nonprofit affiliations attached.

Architecture (Pattern B):
  1. Reuse already-registered match-method `person_name_namestate` v1.0.0
     (registered in PR #282; do NOT register again per L21).
  2. Idempotent UPSERT for the bridge row in `ops.bridges`.
  3. Read FEC `fec/cycle=*/indiv.parquet` + IRS 990 `irs-990/year=*/persons_*` /
     `filings_*` Parquets via DuckDB-on-R2.
  4. Normalize FEC raw `name` and 990 `person_name_raw` via the shared
     person normalizer (Python UDF wrapper around normalize_person_name).
     L34 mitigation: pre-aggregate FEC by raw `(name, state)` BEFORE applying
     the UDF, so the UDF runs on ~10-20M unique tuples instead of ~280M raw
     contributions.
  5. Aggregate per (last_v1, first_v1, state) on each side.
  6. INNER JOIN on (last_v1, first_v1, state) with state strict-equality
     (FEC donor home-state == 990 org-state).
  7. Compute fan-out per canonical key:
        platinum (1:1) — one distinct raw FEC name + one distinct 990 EIN
        gold     (1:N | N:1) — exactly one fan-out side
        silver   (N:M ≤ 50) — both fan out, but ≤ 50
        rejected — fan-out > 50 (excluded from the bridge Parquet)
  8. Write Parquet → bridges/fec_990_namestate/snapshot=<YYYY-MM-DD>/data.parquet
     with bridge_run_id column embedded per row.
  9. HARD FAIL if rows_matched_platinum < 100K (validation floor per §10
     gate 3).

Inputs:
  FEC: r2://dex-raw-landing-zone/fec/cycle=*/indiv.parquet (24 cycles, ~280M)
  IRS 990 persons: r2://dex-raw-landing-zone/irs-990/year=*/persons_990.parquet
                   + persons_990pf.parquet (years 2019-2025, ~35M total)
  IRS 990 filings: r2://dex-raw-landing-zone/irs-990/year=*/filings_990.parquet
                   + filings_990pf.parquet — joined for canonical org state
  IRS 990 comp:    r2://dex-raw-landing-zone/irs-990/year=*/compensation_990.parquet
                   + compensation_990pf.parquet — LEFT JOIN for max comp

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with "psycopg[binary]" --with nameparser \\
    --with unidecode python \\
    apps/data-engine-x/scripts/build_bridge_fec_990_namestate.py --apply

  # Smoke run on a subset
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with "psycopg[binary]" --with nameparser \\
    --with unidecode python \\
    apps/data-engine-x/scripts/build_bridge_fec_990_namestate.py \\
      --fec-cycles 2024 --irs-990-years 2023 --max-rows 100000 --apply

  # Dry-run (no R2 / Postgres writes)
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with "psycopg[binary]" --with nameparser \\
    --with unidecode python \\
    apps/data-engine-x/scripts/build_bridge_fec_990_namestate.py --dry-run
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

from scripts._lib.person_name_normalize import (  # noqa: E402
    NICKNAME_TO_FORMAL,
    PERSON_NAME_BLACKLIST,
    __version__ as NORMALIZER_VERSION,
    py_normalize_person_name_for_duckdb,
)
from scripts._lib.person_name_normalize import _SHORT_NAME_WHITELIST  # noqa: E402
from scripts._lib.match_method_registry import (  # noqa: E402
    complete_bridge_run,
    fail_bridge_run,
    register_bridge,
    start_bridge_run,
)


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("build_bridge_fec_990_namestate")


BRIDGE_NAME = "fec_990_namestate"
METHOD_NAME = "person_name_namestate"
METHOD_SEMVER = "1.0.0"
LEGACY_BRIDGE_VERSION = "1.0.0"

SOURCE_LEFT = "source_fec_individual_contributions"
SOURCE_RIGHT = "source_irs_990_persons"

R2_BUCKET = "dex-raw-landing-zone"
FEC_INPUT_GLOB = "fec/cycle=*/indiv.parquet"
P990_MAINLINE_GLOB = "irs-990/year=*/persons_990.parquet"
P990_PF_GLOB = "irs-990/year=*/persons_990pf.parquet"
F990_MAINLINE_GLOB = "irs-990/year=*/filings_990.parquet"
F990_PF_GLOB = "irs-990/year=*/filings_990pf.parquet"
C990_MAINLINE_GLOB = "irs-990/year=*/compensation_990.parquet"
C990_PF_GLOB = "irs-990/year=*/compensation_990pf.parquet"
BRIDGE_OUTPUT_PREFIX = "bridges/fec_990_namestate"

COLLISION_THRESHOLD = 50  # >50 fan-out on either side → rejected
MIN_ROWS_PLATINUM = 100_000  # §10 gate 3


def _r2_account_id_from_endpoint(endpoint: str) -> str:
    return endpoint.split("//")[-1].split(".")[0]


def _connect_duckdb_to_r2():
    import duckdb

    con = duckdb.connect()
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
    # Register the Python person-name normalizer as a DuckDB UDF.
    # Per L34: the standard Python UDF is the slow path (~11K rows/s). FEC's
    # `name` raw column is comma-format ("SMITH, ROBERT J.") and needs full
    # HumanName parsing — so we keep the Python UDF for the FEC side, but
    # pre-aggregate by raw (name, state) first to reduce the UDF call count
    # from ~280M raw rows to ~10-20M unique tuples.
    #
    # The 990 side bypasses the UDF entirely: persons_990 ships pre-parsed
    # `person_first_normalized` + `person_last_normalized` columns from the
    # OLD normalizer (lowercase + accent-strip only). v1.0.0 of the normalizer
    # adds nickname collapse + person blacklist on top of those steps. The
    # script materializes NICKNAME_TO_FORMAL + PERSON_NAME_BLACKLIST as
    # DuckDB temp tables and applies them via SQL JOINs — orders of magnitude
    # faster than per-row Python UDF calls.
    con.create_function(
        "py_normalize_person",
        py_normalize_person_name_for_duckdb,
        ["VARCHAR"],
        "VARCHAR",
        null_handling="special",
    )
    _materialize_normalizer_lookup_tables(con)
    return con


def _materialize_normalizer_lookup_tables(con) -> None:
    """Build temp tables for nickname dict + blacklist + short-name whitelist.

    Lifts the Python constants from `_lib/person_name_normalize.py` v1.0.0 so
    the bridge SQL can apply nickname collapse + blacklist filtering inline
    (instead of calling out to a Python UDF for every row).
    """
    # Nicknames
    nickname_rows = sorted(NICKNAME_TO_FORMAL.items())
    con.execute(
        "CREATE TEMP TABLE nickname_lookup (nickname VARCHAR PRIMARY KEY, formal VARCHAR);"
    )
    con.executemany(
        "INSERT INTO nickname_lookup VALUES (?, ?)",
        nickname_rows,
    )

    # Person-name blacklist
    bl_rows = [(s,) for s in sorted(PERSON_NAME_BLACKLIST)]
    con.execute(
        "CREATE TEMP TABLE person_blacklist (token VARCHAR PRIMARY KEY);"
    )
    con.executemany("INSERT INTO person_blacklist VALUES (?)", bl_rows)

    # Short-name whitelist (2-char names that are valid)
    sn_rows = [(s,) for s in sorted(_SHORT_NAME_WHITELIST)]
    con.execute(
        "CREATE TEMP TABLE short_name_whitelist (token VARCHAR PRIMARY KEY);"
    )
    con.executemany("INSERT INTO short_name_whitelist VALUES (?)", sn_rows)


def _is_rejected_sql(component_expr: str) -> str:
    """SQL fragment that returns TRUE if a normalized component should reject.

    Mirrors `_lib/person_name_normalize._is_rejected`:
      - empty
      - in PERSON_NAME_BLACKLIST
      - single character
      - exactly 2 chars and not in _SHORT_NAME_WHITELIST
    """
    return f"""(
      {component_expr} IS NULL
      OR length({component_expr}) = 0
      OR length({component_expr}) = 1
      OR {component_expr} IN (SELECT token FROM person_blacklist)
      OR (length({component_expr}) = 2
          AND {component_expr} NOT IN (SELECT token FROM short_name_whitelist))
    )"""


def _build_filings_uris(years: list[int] | None) -> tuple[list[str], list[str]]:
    """Return (mainline_uris, pf_uris) covering selected years (or all)."""
    if years is None:
        return (
            [f"r2://{R2_BUCKET}/{F990_MAINLINE_GLOB}"],
            [f"r2://{R2_BUCKET}/{F990_PF_GLOB}"],
        )
    main = [f"r2://{R2_BUCKET}/irs-990/year={y}/filings_990.parquet" for y in years]
    pf = [f"r2://{R2_BUCKET}/irs-990/year={y}/filings_990pf.parquet" for y in years]
    return main, pf


def _build_persons_uris(years: list[int] | None) -> tuple[list[str], list[str]]:
    if years is None:
        return (
            [f"r2://{R2_BUCKET}/{P990_MAINLINE_GLOB}"],
            [f"r2://{R2_BUCKET}/{P990_PF_GLOB}"],
        )
    main = [f"r2://{R2_BUCKET}/irs-990/year={y}/persons_990.parquet" for y in years]
    pf = [f"r2://{R2_BUCKET}/irs-990/year={y}/persons_990pf.parquet" for y in years]
    return main, pf


def _build_comp_uris(years: list[int] | None) -> tuple[list[str], list[str]]:
    if years is None:
        return (
            [f"r2://{R2_BUCKET}/{C990_MAINLINE_GLOB}"],
            [f"r2://{R2_BUCKET}/{C990_PF_GLOB}"],
        )
    main = [f"r2://{R2_BUCKET}/irs-990/year={y}/compensation_990.parquet" for y in years]
    pf = [f"r2://{R2_BUCKET}/irs-990/year={y}/compensation_990pf.parquet" for y in years]
    return main, pf


def _build_fec_uris(cycles: list[int] | None) -> list[str]:
    if cycles is None:
        return [f"r2://{R2_BUCKET}/{FEC_INPUT_GLOB}"]
    return [f"r2://{R2_BUCKET}/fec/cycle={c}/indiv.parquet" for c in cycles]


def _materialize_990_side(
    con,
    *,
    persons_main_uris: list[str],
    persons_pf_uris: list[str],
    filings_main_uris: list[str],
    filings_pf_uris: list[str],
    comp_main_uris: list[str],
    comp_pf_uris: list[str],
    max_rows: int | None,
) -> int:
    """Build p990_canonical TEMP TABLE: one row per (last_v1, first_v1, state).

    Joins persons_990 + persons_990pf to filings_*, applies the v1.0.0
    person normalizer to person_name_raw, LEFT JOINs compensation for max
    comp, and aggregates per canonical (last, first, state) key.
    """
    persons_main_list = ", ".join(f"'{u}'" for u in persons_main_uris)
    persons_pf_list = ", ".join(f"'{u}'" for u in persons_pf_uris)
    filings_main_list = ", ".join(f"'{u}'" for u in filings_main_uris)
    filings_pf_list = ", ".join(f"'{u}'" for u in filings_pf_uris)
    comp_main_list = ", ".join(f"'{u}'" for u in comp_main_uris)
    comp_pf_list = ", ".join(f"'{u}'" for u in comp_pf_uris)

    max_rows_clause = f"LIMIT {max_rows}" if max_rows else ""

    logger.info("materializing p990_raw (persons UNION pf, JOIN filings, normalize) …")
    con.execute(
        f"""
        CREATE TEMP TABLE p990_raw AS
        WITH persons AS (
          SELECT
            org_ein_normalized,
            tax_period_year,
            person_name_raw,
            person_first_normalized AS person_first_norm_old,
            person_last_normalized AS person_last_norm_old,
            person_role,
            FALSE AS is_pf
          FROM read_parquet([{persons_main_list}])
          WHERE person_name_raw IS NOT NULL
            AND length(trim(person_name_raw)) > 0
            AND org_ein_normalized IS NOT NULL
            AND tax_period_year IS NOT NULL
          UNION ALL
          SELECT
            org_ein_normalized,
            tax_period_year,
            person_name_raw,
            person_first_normalized AS person_first_norm_old,
            person_last_normalized AS person_last_norm_old,
            person_role,
            TRUE AS is_pf
          FROM read_parquet([{persons_pf_list}])
          WHERE person_name_raw IS NOT NULL
            AND length(trim(person_name_raw)) > 0
            AND org_ein_normalized IS NOT NULL
            AND tax_period_year IS NOT NULL
        ),
        filings AS (
          SELECT
            org_ein_normalized,
            tax_period_year,
            upper(trim(org_address_state)) AS org_state,
            try_cast(total_assets AS DOUBLE) AS total_assets
          FROM read_parquet([{filings_main_list}])
          WHERE org_address_state IS NOT NULL
            AND length(trim(org_address_state)) = 2
          UNION ALL
          SELECT
            org_ein_normalized,
            tax_period_year,
            upper(trim(org_address_state)) AS org_state,
            try_cast(total_assets AS DOUBLE) AS total_assets
          FROM read_parquet([{filings_pf_list}])
          WHERE org_address_state IS NOT NULL
            AND length(trim(org_address_state)) = 2
        ),
        comp AS (
          SELECT
            org_ein_normalized,
            tax_period_year,
            person_first_normalized AS person_first_norm_old,
            person_last_normalized AS person_last_norm_old,
            person_role,
            try_cast(comp_total_all AS DOUBLE) AS comp_total_all
          FROM read_parquet([{comp_main_list}])
          UNION ALL
          SELECT
            org_ein_normalized,
            tax_period_year,
            person_first_normalized AS person_first_norm_old,
            person_last_normalized AS person_last_norm_old,
            person_role,
            try_cast(comp_total_all AS DOUBLE) AS comp_total_all
          FROM read_parquet([{comp_pf_list}])
        )
        SELECT
          p.org_ein_normalized,
          p.tax_period_year,
          p.person_name_raw,
          p.person_first_norm_old,
          p.person_last_norm_old,
          p.person_role,
          p.is_pf,
          f.org_state,
          f.total_assets,
          c.comp_total_all
        FROM persons p
        JOIN filings f
          ON f.org_ein_normalized = p.org_ein_normalized
         AND f.tax_period_year = p.tax_period_year
        LEFT JOIN comp c
          ON c.org_ein_normalized = p.org_ein_normalized
         AND c.tax_period_year = p.tax_period_year
         AND c.person_first_norm_old IS NOT DISTINCT FROM p.person_first_norm_old
         AND c.person_last_norm_old IS NOT DISTINCT FROM p.person_last_norm_old
         AND c.person_role IS NOT DISTINCT FROM p.person_role
        {max_rows_clause}
        """
    )
    rows_990_raw = con.execute("SELECT count(*) FROM p990_raw").fetchone()[0]
    logger.info(f"  p990_raw: {rows_990_raw:,} rows")

    # 990 side normalization uses SQL-native path:
    #   - person_first_normalized / person_last_normalized are already
    #     lowercase + accent-stripped from the upstream OLD normalizer.
    #   - We layer nickname collapse + blacklist + length filters in pure
    #     SQL via JOIN with nickname_lookup + person_blacklist temp tables.
    #   - Punct strip is applied via regexp_replace to handle the rare cases
    #     where the upstream OLD normalizer left punctuation in.
    logger.info("normalizing p990 names via SQL macros (nickname JOIN + blacklist) …")
    pre_first = (
        "regexp_replace(lower(coalesce(p.person_first_norm_old, '')), "
        "'[^a-z0-9''\\- ]+', ' ', 'g')"
    )
    pre_last = (
        "regexp_replace(lower(coalesce(p.person_last_norm_old, '')), "
        "'[^a-z0-9''\\- ]+', ' ', 'g')"
    )
    con.execute(
        f"""
        CREATE TEMP TABLE p990_pre AS
        SELECT
          p.org_ein_normalized,
          p.tax_period_year,
          p.person_name_raw,
          p.person_role,
          p.is_pf,
          p.org_state,
          p.total_assets,
          p.comp_total_all,
          regexp_replace(trim({pre_first}), '\\s+', ' ', 'g') AS first_norm_input,
          regexp_replace(trim({pre_last}), '\\s+', ' ', 'g') AS last_norm_input
        FROM p990_raw p
        """
    )
    con.execute(
        f"""
        CREATE TEMP TABLE p990_split AS
        SELECT
          a.org_ein_normalized,
          a.tax_period_year,
          a.person_name_raw,
          a.person_role,
          a.is_pf,
          a.org_state,
          a.total_assets,
          a.comp_total_all,
          a.last_norm_input AS last_normalized,
          coalesce(n.formal, a.first_norm_input) AS first_normalized
        FROM p990_pre a
        LEFT JOIN nickname_lookup n
          ON n.nickname = a.first_norm_input
        WHERE NOT {_is_rejected_sql("a.first_norm_input")}
          AND NOT {_is_rejected_sql("a.last_norm_input")}
          AND NOT {_is_rejected_sql("coalesce(n.formal, a.first_norm_input)")}
        """
    )
    rows_990_norm = con.execute("SELECT count(*) FROM p990_split").fetchone()[0]
    logger.info(f"  p990_split (post-normalize, blacklist applied): {rows_990_norm:,}")

    # Aggregate distinct-EIN total_assets first to avoid double-counting
    # the same ein across multiple tax_period_years for the same person.
    logger.info("aggregating per (last_v1, first_v1, org_state) …")
    con.execute(
        """
        CREATE TEMP TABLE p990_per_ein AS
        SELECT
          last_normalized,
          first_normalized,
          org_state,
          org_ein_normalized,
          bool_or(is_pf) AS is_pf_any,
          max(total_assets) AS max_total_assets,
          max(comp_total_all) AS max_comp,
          arg_max(person_name_raw, coalesce(comp_total_all, 0.0)) AS raw_name_sample,
          array_agg(DISTINCT person_role) AS roles
        FROM p990_split
        GROUP BY last_normalized, first_normalized, org_state, org_ein_normalized
        """
    )

    con.execute(
        """
        CREATE TEMP TABLE p990_canonical AS
        SELECT
          last_normalized,
          first_normalized,
          org_state AS state,
          count(DISTINCT org_ein_normalized) AS person_990_count_at_key,
          array_agg(DISTINCT org_ein_normalized) AS person_990_ein_set,
          flatten(array_agg(DISTINCT roles)) AS person_990_role_set_raw,
          bool_or(is_pf_any) AS person_990_is_pf_any,
          max(max_comp) AS person_990_max_compensation,
          sum(max_total_assets) AS person_990_total_org_assets,
          arg_max(raw_name_sample, coalesce(max_comp, 0.0)) AS raw_name_sample
        FROM p990_per_ein
        GROUP BY last_normalized, first_normalized, org_state
        """
    )
    n = con.execute("SELECT count(*) FROM p990_canonical").fetchone()[0]
    logger.info(f"  p990_canonical (distinct people-states): {n:,}")
    return n


def _materialize_fec_side(
    con, *, fec_uris: list[str], max_rows: int | None,
) -> int:
    """Build fec_canonical TEMP TABLE: one row per (last_v1, first_v1, state).

    Pre-aggregates by raw (name, state) BEFORE applying the UDF (L34 mitigation),
    then re-aggregates by canonical (last_v1, first_v1, state).
    """
    fec_list = ", ".join(f"'{u}'" for u in fec_uris)
    max_rows_clause = f"LIMIT {max_rows}" if max_rows else ""

    logger.info("materializing fec_per_raw_name (pre-agg by raw name+state) …")
    con.execute(
        f"""
        CREATE TEMP TABLE fec_raw AS
        SELECT
          name,
          upper(trim(state)) AS state,
          transaction_amt,
          transaction_dt,
          cycle_year,
          employer,
          occupation,
          zip5,
          city
        FROM read_parquet([{fec_list}], union_by_name=true)
        WHERE name IS NOT NULL
          AND state IS NOT NULL
          AND length(trim(state)) = 2
        {max_rows_clause}
        """
    )
    rows_fec_raw = con.execute("SELECT count(*) FROM fec_raw").fetchone()[0]
    logger.info(f"  fec_raw: {rows_fec_raw:,} contribution rows")

    con.execute(
        """
        CREATE TEMP TABLE fec_per_raw_name AS
        SELECT
          name,
          state,
          sum(transaction_amt) AS sum_amt,
          count(*) AS cnt,
          min(transaction_dt) AS min_date,
          max(transaction_dt) AS max_date,
          array_agg(DISTINCT cycle_year) AS cycles_active,
          arg_max(employer, transaction_amt) AS employer_top,
          arg_max(occupation, transaction_amt) AS occupation_top,
          arg_max(zip5, transaction_amt) AS zip5_top,
          arg_max(city, transaction_amt) AS city_top
        FROM fec_raw
        GROUP BY name, state
        """
    )
    rows_fec_unique = con.execute("SELECT count(*) FROM fec_per_raw_name").fetchone()[0]
    logger.info(
        f"  fec_per_raw_name (unique raw name+state tuples): {rows_fec_unique:,} "
        f"(reduction from raw: {rows_fec_raw / max(rows_fec_unique, 1):.1f}x)"
    )

    logger.info("normalizing fec names via py_normalize_person (UDF) …")
    con.execute(
        """
        CREATE TEMP TABLE fec_normalized AS
        SELECT
          *,
          py_normalize_person(name) AS norm_pipe
        FROM fec_per_raw_name
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE fec_split AS
        SELECT
          *,
          string_split(norm_pipe, '|')[1] AS last_normalized,
          string_split(norm_pipe, '|')[2] AS first_normalized
        FROM fec_normalized
        WHERE norm_pipe IS NOT NULL
        """
    )
    rows_fec_norm = con.execute("SELECT count(*) FROM fec_split").fetchone()[0]
    logger.info(
        f"  fec_split (post-normalize, blacklist applied): {rows_fec_norm:,} unique tuples"
    )

    logger.info("aggregating per (last_v1, first_v1, state) …")
    con.execute(
        """
        CREATE TEMP TABLE fec_canonical AS
        SELECT
          last_normalized,
          first_normalized,
          state,
          count(DISTINCT name) AS fec_donor_count_at_key,
          arg_max(name, sum_amt) AS fec_donor_raw_name_sample,
          arg_max(employer_top, sum_amt) AS fec_donor_employer,
          arg_max(occupation_top, sum_amt) AS fec_donor_occupation,
          arg_max(zip5_top, sum_amt) AS fec_donor_zip5,
          arg_max(city_top, sum_amt) AS fec_donor_city,
          sum(sum_amt) AS fec_donor_lifetime_giving_total,
          sum(cnt) AS fec_donor_lifetime_giving_count,
          min(min_date) AS fec_donor_first_giving_date,
          max(max_date) AS fec_donor_latest_giving_date,
          list_distinct(flatten(array_agg(cycles_active))) AS fec_donor_cycles_active
        FROM fec_split
        GROUP BY last_normalized, first_normalized, state
        """
    )
    n = con.execute("SELECT count(*) FROM fec_canonical").fetchone()[0]
    logger.info(f"  fec_canonical (distinct donor-states): {n:,}")
    return n


def _build_match_table(
    con, *, bridge_run_id: str, generated_at_iso: str,
) -> dict[str, int]:
    """JOIN both sides on (last_v1, first_v1, state); compute tier; write to bridge_match."""
    logger.info("computing JOIN + tier classification …")
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
          '{bridge_run_id}' AS bridge_run_id,
          '{METHOD_NAME}' AS match_method,
          '{METHOD_SEMVER}' AS match_method_semver,
          f.last_normalized,
          f.first_normalized,
          f.state,
          CASE
            WHEN f.fec_donor_count_at_key > {COLLISION_THRESHOLD}
              OR p.person_990_count_at_key > {COLLISION_THRESHOLD}
                THEN 'rejected'
            WHEN f.fec_donor_count_at_key = 1
             AND p.person_990_count_at_key = 1
                THEN 'platinum'
            WHEN f.fec_donor_count_at_key = 1
              OR p.person_990_count_at_key = 1
                THEN 'gold'
            ELSE 'silver'
          END AS confidence_tier,
          f.fec_donor_count_at_key,
          p.person_990_count_at_key,
          f.fec_donor_raw_name_sample,
          f.fec_donor_employer,
          f.fec_donor_occupation,
          f.fec_donor_zip5,
          f.fec_donor_city,
          f.fec_donor_lifetime_giving_total,
          f.fec_donor_lifetime_giving_count,
          f.fec_donor_first_giving_date,
          f.fec_donor_latest_giving_date,
          f.fec_donor_cycles_active,
          p.raw_name_sample AS person_990_raw_name_sample,
          p.person_990_role_set_raw AS person_990_role_set,
          p.person_990_ein_set,
          cast(p.person_990_count_at_key AS INTEGER) AS person_990_org_count,
          p.person_990_max_compensation,
          p.person_990_total_org_assets,
          p.person_990_is_pf_any,
          TIMESTAMP '{generated_at_iso}' AS generated_at
        FROM fec_canonical f
        JOIN p990_canonical p
          ON p.last_normalized = f.last_normalized
         AND p.first_normalized = f.first_normalized
         AND p.state = f.state
        """
    )

    con.execute(
        """
        CREATE TEMP TABLE bridge_match AS
        SELECT *
        FROM bridge_all
        WHERE confidence_tier <> 'rejected'
        """
    )

    counts = con.execute(
        """
        SELECT
          count(*) AS rows_matched,
          count(*) FILTER (WHERE confidence_tier = 'platinum') AS rows_platinum,
          count(*) FILTER (WHERE confidence_tier = 'gold') AS rows_gold,
          count(*) FILTER (WHERE confidence_tier = 'silver') AS rows_silver
        FROM bridge_match
        """
    ).fetchone()
    rejected = con.execute(
        "SELECT count(*) FROM bridge_all WHERE confidence_tier = 'rejected'"
    ).fetchone()[0]

    return {
        "rows_matched": counts[0],
        "rows_tier1": counts[1],
        "rows_tier2": counts[2],
        "rows_tier3": counts[3],
        "rows_collision_rejected": rejected,
    }


def _finalize_columns(con) -> None:
    """Reshape bridge_match into the v1 wire schema. The 990 raw_name_sample
    is preserved as a single full-string column rather than split — splitting
    via nameparser at scale is awkward, the v1-normalized last+first columns
    already give the canonical readable identity, and the full raw_name is
    available verbatim for forensic spot-checks."""
    logger.info("finalizing bridge column layout …")
    con.execute(
        """
        CREATE TEMP TABLE bridge_match_v2 AS
        SELECT
          bridge_run_id,
          match_method,
          match_method_semver,
          last_normalized,
          first_normalized,
          state,
          confidence_tier,
          fec_donor_count_at_key,
          person_990_count_at_key,
          fec_donor_raw_name_sample,
          fec_donor_employer,
          fec_donor_occupation,
          fec_donor_zip5,
          fec_donor_city,
          fec_donor_lifetime_giving_total,
          fec_donor_lifetime_giving_count,
          fec_donor_first_giving_date,
          fec_donor_latest_giving_date,
          fec_donor_cycles_active,
          person_990_raw_name_sample,
          person_990_role_set,
          person_990_ein_set,
          person_990_org_count,
          person_990_max_compensation,
          person_990_total_org_assets,
          person_990_is_pf_any,
          generated_at
        FROM bridge_match
        """
    )
    con.execute("DROP TABLE bridge_match")
    con.execute("ALTER TABLE bridge_match_v2 RENAME TO bridge_match")


def _write_bridge_parquet(con, snapshot: str) -> str:
    output_key = f"{BRIDGE_OUTPUT_PREFIX}/snapshot={snapshot}/data.parquet"
    output_url = f"r2://{R2_BUCKET}/{output_key}"
    logger.info(f"writing bridge → {output_url}")
    con.execute(
        f"""
        COPY bridge_match TO '{output_url}'
        (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)
        """
    )
    return output_key


def _ensure_registry() -> None:
    """Idempotent UPSERT for the bridge row. Method already registered by PR #282."""
    logger.info("registering bridge in ops.bridges (method already registered) …")
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=f"{BRIDGE_OUTPUT_PREFIX}/",
        description=(
            "FEC individual contributions × IRS 990 officers/directors/trustees/"
            "key-employees matched on (last_normalized, first_normalized) via "
            "person_name_namestate v1.0.0 AND state-equality (FEC donor home-state "
            "== 990 org-state). Strict v1 match; cross-state board service "
            "deliberately out of scope."
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write Parquet + ledger row")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="row counts + tier distribution only, no R2 / Postgres writes",
    )
    parser.add_argument(
        "--snapshot",
        default=None,
        help="YYYY-MM-DD output snapshot dir (default: today UTC)",
    )
    parser.add_argument(
        "--fec-cycles",
        type=int,
        nargs="*",
        default=None,
        help="FEC cycles to include (default: all cycles 1980-2026)",
    )
    parser.add_argument(
        "--irs-990-years",
        type=int,
        nargs="*",
        default=None,
        help="IRS 990 years to include (default: all years 2019-2025)",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=None,
        help="cap rows on each side for smoke runs",
    )
    args = parser.parse_args()

    if not args.apply and not args.dry_run:
        parser.error("must pass --apply or --dry-run")
    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are mutually exclusive")

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")
    if not os.environ.get("DEX_DB_URL_DIRECT"):
        raise SystemExit("FAIL: DEX_DB_URL_DIRECT not set (required for registry)")

    started_at = datetime.now(tz=timezone.utc)
    snapshot = args.snapshot or started_at.strftime("%Y-%m-%d")
    t0 = time.time()

    logger.info(f"bridge: {BRIDGE_NAME} (method={METHOD_NAME} v{METHOD_SEMVER})")
    logger.info(f"normalizer: _lib/person_name_normalize.py v{NORMALIZER_VERSION}")
    logger.info(f"snapshot: {snapshot}")
    if args.fec_cycles:
        logger.info(f"FEC cycles (subset): {args.fec_cycles}")
    if args.irs_990_years:
        logger.info(f"IRS 990 years (subset): {args.irs_990_years}")
    if args.max_rows:
        logger.info(f"max_rows cap: {args.max_rows:,}")

    output_key_planned = (
        f"{BRIDGE_OUTPUT_PREFIX}/snapshot={snapshot}/data.parquet"
    )

    if args.dry_run:
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
            match_method=METHOD_NAME,
            r2_output_key=output_key_planned,
        )
        bridge_run_id = str(run_uuid)
        logger.info(f"bridge_run_id={bridge_run_id}")

    persons_main_uris, persons_pf_uris = _build_persons_uris(args.irs_990_years)
    filings_main_uris, filings_pf_uris = _build_filings_uris(args.irs_990_years)
    comp_main_uris, comp_pf_uris = _build_comp_uris(args.irs_990_years)
    fec_uris = _build_fec_uris(args.fec_cycles)

    con = _connect_duckdb_to_r2()

    try:
        rows_990 = _materialize_990_side(
            con,
            persons_main_uris=persons_main_uris,
            persons_pf_uris=persons_pf_uris,
            filings_main_uris=filings_main_uris,
            filings_pf_uris=filings_pf_uris,
            comp_main_uris=comp_main_uris,
            comp_pf_uris=comp_pf_uris,
            max_rows=args.max_rows,
        )
        rows_fec = _materialize_fec_side(
            con, fec_uris=fec_uris, max_rows=args.max_rows,
        )
        counts = _build_match_table(
            con,
            bridge_run_id=bridge_run_id,
            generated_at_iso=started_at.isoformat(),
        )
        _finalize_columns(con)

        logger.info("─" * 60)
        logger.info("bridge tier distribution:")
        logger.info(f"  rows_matched:            {counts['rows_matched']:,}")
        logger.info(f"    platinum (1:1):        {counts['rows_tier1']:,}")
        logger.info(f"    gold     (1:N | N:1):  {counts['rows_tier2']:,}")
        logger.info(f"    silver   (N:M ≤{COLLISION_THRESHOLD}):    {counts['rows_tier3']:,}")
        logger.info(f"  rows_collision_rejected: {counts['rows_collision_rejected']:,}")

        if counts["rows_matched"] > 0:
            tier1_share = counts["rows_tier1"] / counts["rows_matched"]
            logger.info(f"  platinum share:          {tier1_share:.1%}")

        # HARD FAIL: validation floor on platinum
        if counts["rows_tier1"] < MIN_ROWS_PLATINUM and not args.max_rows:
            msg = (
                f"HARD FAIL: rows_platinum={counts['rows_tier1']:,} < "
                f"floor={MIN_ROWS_PLATINUM:,} — bridge too thin to ship"
            )
            logger.error(msg)
            if args.apply and run_uuid is not None:
                fail_bridge_run(run_uuid, msg)
            return 1

        if args.dry_run:
            logger.info(f"DRY RUN — no R2 / Postgres writes. duration={time.time()-t0:.1f}s")
            return 0

        r2_output_key = _write_bridge_parquet(con, snapshot)
        complete_bridge_run(
            run_uuid,
            metrics={
                "rows_left": rows_fec,
                "rows_right": rows_990,
                "rows_matched": counts["rows_matched"],
                "rows_tier1": counts["rows_tier1"],
                "rows_tier2": counts["rows_tier2"],
                "rows_tier3": counts["rows_tier3"],
                "rows_collision_rejected": counts["rows_collision_rejected"],
                "domains_blacklisted": 0,
            },
        )
        logger.info(f"OK — run_id={bridge_run_id}  duration={time.time()-t0:.1f}s")
        logger.info(f"     output: r2://{R2_BUCKET}/{r2_output_key}")
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

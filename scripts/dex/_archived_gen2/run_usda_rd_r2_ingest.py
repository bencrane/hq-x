#!/usr/bin/env python3
"""USDA Rural Development → R2 Fuel Tank ingest (Postgres-sourced).

Mirrors the existing `entities.usda_rd_*` Postgres tables into Cloudflare R2
as ZSTD-compressed Parquet, with per-FY partitioning for the longitudinal
streams and per-snapshot partitioning for the static lookups.

Source posture: this script reads from Postgres because the upstream USDA
Rural Data Gateway publishes its data exclusively through interactive
Tableau Public dashboards (no static per-FY URL convention). The Postgres
tables are populated upstream by `app/services/usda_rd_ingest.py` from
operator-exported TSVs of those same Tableau dashboards. See blocker
~/Desktop/hq/blockers/2026-05-08-2026-05-08-usda-rd-r2-ingest.md for the
full sourcing rationale (operator picked option B: Postgres source).

Five streams:

  investments           per-FY (investment_year)        entities.usda_rd_investments
  loans                 per-FY (close_fiscal_year)      entities.usda_rd_loans
  lenders               per-snapshot                    entities.mv_usda_rd_lenders
  investment_dashboard  per-snapshot                    entities.usda_rd_investment_dashboard
  eligibility_areas     per-snapshot                    [NOT POPULATED in Postgres]

R2 layout:
  s3://dex-raw-landing-zone/usda-rd/{stream}/year={YYYY}/data.parquet
  s3://dex-raw-landing-zone/usda-rd/{stream}/snapshot={YYYY-MM-DD}/data.parquet

Audit ledger: ops.usda_rd_r2_ingest_runs.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_usda_rd_r2_ingest.py \\
      --stream investments --years 2024 --max-rows 50000

  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_usda_rd_r2_ingest.py --all

  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_usda_rd_r2_ingest.py \\
      --stream investments --years 2016-2026

  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_usda_rd_r2_ingest.py \\
      --stream lenders --r2-prefix-override 'usda-rd/_smoke/lenders'

See directive ~/Desktop/hq/directives/2026-05-08-usda-rd-r2-ingest.md.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import boto3
import duckdb
import psycopg
from psycopg.types.json import Jsonb

R2_BUCKET = "dex-raw-landing-zone"

# Per-FY streams: legal year span we'll consider for --all and --years.
# Matches the actual data range present in entities.usda_rd_investments
# and entities.usda_rd_loans (loans goes back further but coverage is
# sparse pre-2014; default span here covers the well-published modern era).
DEFAULT_YEAR_SPAN: tuple[int, ...] = tuple(range(2014, 2027))


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("usda-rd-r2-ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# Env / clients
# --------------------------------------------------------------------------- #


def _required_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"{name} is not set in the environment.")
    return v


def _r2_client() -> "boto3.client":
    return boto3.client(
        "s3",
        endpoint_url=_required_env("R2_ENDPOINT"),
        aws_access_key_id=_required_env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=_required_env("R2_SECRET_ACCESS_KEY"),
        region_name="auto",
    )


def _database_url() -> str:
    return _required_env("DEX_DB_URL_POOLED")


# --------------------------------------------------------------------------- #
# Stream specs
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class StreamSpec:
    """Parameters that change between streams."""
    name: str                        # 'investments' | 'loans' | 'lenders' | ...
    source_table: str                # 'entities.usda_rd_investments' | ...
    partition_kind: str              # 'year' | 'snapshot'
    fy_column: str | None            # 'investment_year' for per-FY streams
    raw_columns: tuple[str, ...]
    # Column expressions added during DuckDB transform (uses macros from
    # `_NORMALIZE_MACROS_SQL`). Each entry is (output_column, sql_expr).
    normalized_columns: tuple[tuple[str, str], ...]
    typed_overrides: tuple[tuple[str, str], ...]


# Raw-column lists mirror the `\d` of each Postgres table at the time this
# script was written. Numeric / date casts are at the typed_overrides level.

_INVESTMENTS_RAW: tuple[str, ...] = (
    "borrower_name", "city", "congressional_district", "county",
    "county_fips", "funding_code", "investment_type", "investment_year",
    "lender_name", "naics_industry_sector_code", "naics_industry_sector",
    "naics_industry_subsector_code", "naics_industry_subsector",
    "portfolio_group", "program", "program_area", "project_description",
    "project_name", "state_name", "investment_dollars",
    "number_of_investments",
)

_LOANS_RAW: tuple[str, ...] = (
    "borrower_name", "borrower_state", "close_fiscal_year",
    "delinquency_description", "holding_lender_name", "holding_lender_state",
    "holding_lender_type", "loan_id", "loan_state", "maturity_year",
    "program", "program_area", "project_description", "project_state",
    "guaranteed_percentage", "note_interest_rate", "original_loan_amount",
    "over_90_days_delinquent_amount", "unpaid_principal",
)

_INVESTMENT_DASHBOARD_RAW: tuple[str, ...] = (
    "fiscal_year", "state_name", "county", "congressional_district",
    "program_area", "program", "zip_code",
    "persistent_poverty_community_status", "borrower_name", "project_name",
    "investment_type", "city", "lender_name", "funding_code",
    "naics_industry_sector", "county_fips", "naics_national_industry_code",
    "naics_national_industry", "portfolio_type",
    "project_announced_description", "investment_dollars",
    "number_of_investments",
)

_LENDERS_RAW: tuple[str, ...] = (
    "lender_state_key", "lender_key", "lender_name_display", "lender_type",
    "lender_state", "record_count", "loans_count", "investments_count",
    "investment_dashboard_count", "in_loans", "in_investments",
    "in_investment_dashboard", "loans_original_dollars",
    "loans_unpaid_principal", "investment_dollars", "total_dollars",
    "first_year", "last_year", "distinct_program_count", "programs",
    "program_areas", "is_commercial_rural_lender",
)


_INVESTMENTS_SPEC = StreamSpec(
    name="investments",
    source_table="entities.usda_rd_investments",
    partition_kind="year",
    fy_column="investment_year",
    raw_columns=_INVESTMENTS_RAW,
    typed_overrides=(
        ("investment_dollars",
         "TRY_CAST(NULLIF(investment_dollars, '') AS DOUBLE) AS investment_dollars"),
        ("number_of_investments",
         "TRY_CAST(NULLIF(number_of_investments, '') AS BIGINT) AS number_of_investments"),
    ),
    normalized_columns=(
        ("lender_name_normalized", "usda_rd_normalize_org(lender_name) AS lender_name_normalized"),
        ("borrower_name_normalized", "usda_rd_normalize_org(borrower_name) AS borrower_name_normalized"),
        ("county_fips_normalized", "usda_rd_normalize_county_fips(county_fips) AS county_fips_normalized"),
        ("state_normalized", "usda_rd_normalize_state(state_name) AS state_normalized"),
        ("naics_2digit", "usda_rd_naics_2digit(naics_industry_sector_code) AS naics_2digit"),
        ("program_normalized", "usda_rd_normalize_program(program) AS program_normalized"),
    ),
)

_LOANS_SPEC = StreamSpec(
    name="loans",
    source_table="entities.usda_rd_loans",
    partition_kind="year",
    fy_column="close_fiscal_year",
    raw_columns=_LOANS_RAW,
    typed_overrides=(
        ("guaranteed_percentage",
         "TRY_CAST(NULLIF(guaranteed_percentage, '') AS DOUBLE) AS guaranteed_percentage"),
        ("note_interest_rate",
         "TRY_CAST(NULLIF(note_interest_rate, '') AS DOUBLE) AS note_interest_rate"),
        ("original_loan_amount",
         "TRY_CAST(NULLIF(original_loan_amount, '') AS DOUBLE) AS original_loan_amount"),
        ("over_90_days_delinquent_amount",
         "TRY_CAST(NULLIF(over_90_days_delinquent_amount, '') AS DOUBLE) AS over_90_days_delinquent_amount"),
        ("unpaid_principal",
         "TRY_CAST(NULLIF(unpaid_principal, '') AS DOUBLE) AS unpaid_principal"),
    ),
    normalized_columns=(
        ("lender_name_normalized", "usda_rd_normalize_org(holding_lender_name) AS lender_name_normalized"),
        ("borrower_name_normalized", "usda_rd_normalize_org(borrower_name) AS borrower_name_normalized"),
        ("borrower_state_normalized", "usda_rd_normalize_state(borrower_state) AS borrower_state_normalized"),
        ("loan_state_normalized", "usda_rd_normalize_state(loan_state) AS loan_state_normalized"),
        ("lender_state_normalized", "usda_rd_normalize_state(holding_lender_state) AS lender_state_normalized"),
        ("program_normalized", "usda_rd_normalize_program(program) AS program_normalized"),
    ),
)

_INVESTMENT_DASHBOARD_SPEC = StreamSpec(
    name="investment_dashboard",
    source_table="entities.usda_rd_investment_dashboard",
    partition_kind="snapshot",
    fy_column=None,
    raw_columns=_INVESTMENT_DASHBOARD_RAW,
    typed_overrides=(
        ("investment_dollars",
         "TRY_CAST(NULLIF(investment_dollars, '') AS DOUBLE) AS investment_dollars"),
        ("number_of_investments",
         "TRY_CAST(NULLIF(number_of_investments, '') AS BIGINT) AS number_of_investments"),
    ),
    normalized_columns=(
        ("lender_name_normalized", "usda_rd_normalize_org(lender_name) AS lender_name_normalized"),
        ("borrower_name_normalized", "usda_rd_normalize_org(borrower_name) AS borrower_name_normalized"),
        ("county_fips_normalized", "usda_rd_normalize_county_fips(county_fips) AS county_fips_normalized"),
        ("state_normalized", "usda_rd_normalize_state(state_name) AS state_normalized"),
        ("zip5", "usda_rd_zip5(zip_code) AS zip5"),
        ("program_normalized", "usda_rd_normalize_program(program) AS program_normalized"),
    ),
)

_LENDERS_SPEC = StreamSpec(
    name="lenders",
    source_table="entities.mv_usda_rd_lenders",
    partition_kind="snapshot",
    fy_column=None,
    raw_columns=_LENDERS_RAW,
    typed_overrides=(),
    normalized_columns=(
        # The MV's `lender_key` is already an UPPER(TRIM(...)) of the raw
        # lender name; we re-derive lender_name_normalized from the display
        # name for join parity with the per-FY streams.
        ("lender_name_normalized", "usda_rd_normalize_org(lender_name_display) AS lender_name_normalized"),
        ("lender_state_normalized", "usda_rd_normalize_state(lender_state) AS lender_state_normalized"),
    ),
)

STREAM_SPECS: dict[str, StreamSpec] = {
    s.name: s
    for s in (_INVESTMENTS_SPEC, _LOANS_SPEC, _INVESTMENT_DASHBOARD_SPEC, _LENDERS_SPEC)
}


# --------------------------------------------------------------------------- #
# DuckDB normalization macros
# --------------------------------------------------------------------------- #


_NORMALIZE_MACROS_SQL = r"""
-- Org-name normalizer: lowercase + punctuation strip + iterative trailing
-- corp-suffix strip (LLC, INC, NA, ...). Mirrors
-- scripts/_lib/usda_rd_normalize.py:_normalize_org_name including the
-- "n a" → "na" coalesce so "U.S. Bank N.A." → "u s bank".
CREATE OR REPLACE MACRO usda_rd_normalize_org(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(raw) = '' THEN NULL
    ELSE NULLIF(
      (
        WITH parts AS (
          SELECT string_split(
            trim(regexp_replace(
              regexp_replace(lower(raw), '[.,&]+', ' ', 'g'),
              '\s+', ' ', 'g'
            )),
            ' '
          ) AS p
        ),
        coalesced AS (
          SELECT
            CASE
              WHEN length(p) >= 3 AND p[length(p)] = 'a' AND p[length(p)-1] = 'n'
                THEN list_concat(p[1:length(p)-2], ['na'])
              ELSE p
            END AS p
          FROM parts
        )
        SELECT
          array_to_string(
            CASE
              WHEN length(p) >= 4 AND p[length(p)] IN ('llc','inc','incorporated','corp','corporation','co','company','ltd','limited','lp','llp','pa','pc','pllc','na')
                                 AND p[length(p)-1] IN ('llc','inc','incorporated','corp','corporation','co','company','ltd','limited','lp','llp','pa','pc','pllc','na')
                                 AND p[length(p)-2] IN ('llc','inc','incorporated','corp','corporation','co','company','ltd','limited','lp','llp','pa','pc','pllc','na')
                THEN p[1:length(p)-3]
              WHEN length(p) >= 3 AND p[length(p)] IN ('llc','inc','incorporated','corp','corporation','co','company','ltd','limited','lp','llp','pa','pc','pllc','na')
                                 AND p[length(p)-1] IN ('llc','inc','incorporated','corp','corporation','co','company','ltd','limited','lp','llp','pa','pc','pllc','na')
                THEN p[1:length(p)-2]
              WHEN length(p) >= 2 AND p[length(p)] IN ('llc','inc','incorporated','corp','corporation','co','company','ltd','limited','lp','llp','pa','pc','pllc','na')
                THEN p[1:length(p)-1]
              ELSE p
            END,
            ' '
          )
        FROM coalesced
      ),
      ''
    )
  END
);

-- Coerce a county FIPS to 5-digit zero-padded.
CREATE OR REPLACE MACRO usda_rd_normalize_county_fips(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(raw) = '' THEN NULL
    WHEN length(regexp_replace(raw, '\D', '', 'g')) BETWEEN 4 AND 5
      THEN lpad(regexp_replace(raw, '\D', '', 'g'), 5, '0')
    ELSE NULL
  END
);

-- 2-letter state abbrev. Pass through 2-letter alpha; coerce known full
-- names; everything else NULL.
CREATE OR REPLACE MACRO usda_rd_normalize_state(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(raw) = '' THEN NULL
    WHEN length(trim(raw)) = 2 AND regexp_matches(trim(raw), '^[A-Za-z]{2}$')
      THEN upper(trim(raw))
    ELSE
      CASE upper(trim(raw))
        WHEN 'ALABAMA' THEN 'AL' WHEN 'ALASKA' THEN 'AK'
        WHEN 'ARIZONA' THEN 'AZ' WHEN 'ARKANSAS' THEN 'AR'
        WHEN 'CALIFORNIA' THEN 'CA' WHEN 'COLORADO' THEN 'CO'
        WHEN 'CONNECTICUT' THEN 'CT' WHEN 'DELAWARE' THEN 'DE'
        WHEN 'DISTRICT OF COLUMBIA' THEN 'DC' WHEN 'FLORIDA' THEN 'FL'
        WHEN 'GEORGIA' THEN 'GA' WHEN 'HAWAII' THEN 'HI' WHEN 'IDAHO' THEN 'ID'
        WHEN 'ILLINOIS' THEN 'IL' WHEN 'INDIANA' THEN 'IN' WHEN 'IOWA' THEN 'IA'
        WHEN 'KANSAS' THEN 'KS' WHEN 'KENTUCKY' THEN 'KY'
        WHEN 'LOUISIANA' THEN 'LA' WHEN 'MAINE' THEN 'ME' WHEN 'MARYLAND' THEN 'MD'
        WHEN 'MASSACHUSETTS' THEN 'MA' WHEN 'MICHIGAN' THEN 'MI'
        WHEN 'MINNESOTA' THEN 'MN' WHEN 'MISSISSIPPI' THEN 'MS'
        WHEN 'MISSOURI' THEN 'MO' WHEN 'MONTANA' THEN 'MT' WHEN 'NEBRASKA' THEN 'NE'
        WHEN 'NEVADA' THEN 'NV' WHEN 'NEW HAMPSHIRE' THEN 'NH'
        WHEN 'NEW JERSEY' THEN 'NJ' WHEN 'NEW MEXICO' THEN 'NM'
        WHEN 'NEW YORK' THEN 'NY' WHEN 'NORTH CAROLINA' THEN 'NC'
        WHEN 'NORTH DAKOTA' THEN 'ND' WHEN 'OHIO' THEN 'OH'
        WHEN 'OKLAHOMA' THEN 'OK' WHEN 'OREGON' THEN 'OR' WHEN 'PENNSYLVANIA' THEN 'PA'
        WHEN 'RHODE ISLAND' THEN 'RI' WHEN 'SOUTH CAROLINA' THEN 'SC'
        WHEN 'SOUTH DAKOTA' THEN 'SD' WHEN 'TENNESSEE' THEN 'TN'
        WHEN 'TEXAS' THEN 'TX' WHEN 'UTAH' THEN 'UT' WHEN 'VERMONT' THEN 'VT'
        WHEN 'VIRGINIA' THEN 'VA' WHEN 'WASHINGTON' THEN 'WA'
        WHEN 'WEST VIRGINIA' THEN 'WV' WHEN 'WISCONSIN' THEN 'WI'
        WHEN 'WYOMING' THEN 'WY' WHEN 'PUERTO RICO' THEN 'PR'
        WHEN 'VIRGIN ISLANDS' THEN 'VI' WHEN 'GUAM' THEN 'GU'
        WHEN 'AMERICAN SAMOA' THEN 'AS'
        WHEN 'NORTHERN MARIANA ISLANDS' THEN 'MP'
        WHEN 'FEDERATED STATES OF MICRONESIA' THEN 'FM'
        WHEN 'MARSHALL ISLANDS' THEN 'MH' WHEN 'PALAU' THEN 'PW'
        ELSE NULL
      END
  END
);

-- Program name → uppercase + whitespace-collapsed.
CREATE OR REPLACE MACRO usda_rd_normalize_program(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(raw) = '' THEN NULL
    ELSE NULLIF(trim(regexp_replace(upper(raw), '\s+', ' ', 'g')), '')
  END
);

-- 2-digit NAICS.
CREATE OR REPLACE MACRO usda_rd_naics_2digit(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(raw) = '' THEN NULL
    WHEN length(regexp_replace(raw, '\D', '', 'g')) < 2 THEN NULL
    ELSE substr(regexp_replace(raw, '\D', '', 'g'), 1, 2)
  END
);

-- 5-digit ZIP slicer.
CREATE OR REPLACE MACRO usda_rd_zip5(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(raw) = '' THEN NULL
    WHEN length(regexp_replace(raw, '\D', '', 'g')) < 5 THEN NULL
    ELSE substr(regexp_replace(raw, '\D', '', 'g'), 1, 5)
  END
);
"""


# --------------------------------------------------------------------------- #
# Postgres → DuckDB → Parquet
# --------------------------------------------------------------------------- #


def _build_select(
    spec: StreamSpec, *,
    partition_value: str,
    partition_kind: str,
    max_rows: int | None,
) -> str:
    """Build the projection SELECT that DuckDB will run against Postgres
    via the `postgres_scanner` extension. Each typed override replaces the
    raw column reference; normalized columns are appended; partition
    metadata column is appended last."""
    typed_map = {col: expr for col, expr in spec.typed_overrides}
    select_parts: list[str] = []
    for col in spec.raw_columns:
        if col in typed_map:
            select_parts.append(typed_map[col])
        else:
            select_parts.append(col)
    for _, expr in spec.normalized_columns:
        select_parts.append(expr)

    # Partition metadata column at the tail.
    if partition_kind == "year":
        select_parts.append(
            f"CAST({int(partition_value)} AS SMALLINT) AS usda_rd_fy"
        )
    else:
        select_parts.append(
            f"CAST('{partition_value}' AS DATE) AS usda_rd_snapshot_date"
        )

    where = ""
    if spec.fy_column is not None:
        where = f"WHERE \"{spec.fy_column}\" = '{partition_value}'"
    limit = f"LIMIT {int(max_rows)}" if max_rows is not None else ""
    return (
        f"SELECT {', '.join(select_parts)} "
        f"FROM pgdb.{spec.source_table} {where} {limit}".strip()
    )


def transform_to_parquet(
    spec: StreamSpec, parquet_path: Path,
    *,
    partition_value: str,
    partition_kind: str,
    max_rows: int | None,
    log_prefix: str,
) -> tuple[int, int, dict[str, float]]:
    """Connect DuckDB → ATTACH Postgres → register macros → COPY-out the
    projection as ZSTD Parquet. Returns (source_rows, parquet_rows, null_rates).

    `source_rows` is computed via a separate count(*) on the same partition
    filter so row-count parity can be verified against the Parquet output.
    """
    db_url = _database_url()
    con = duckdb.connect(":memory:")
    con.execute("PRAGMA threads=4;")
    con.execute("PRAGMA memory_limit='6GB';")
    con.execute("INSTALL postgres; LOAD postgres;")
    con.execute(f"ATTACH '{db_url}' AS pgdb (TYPE postgres, READ_ONLY);")
    con.execute(_NORMALIZE_MACROS_SQL)

    # source row count — for parity check.
    where = (
        f"WHERE \"{spec.fy_column}\" = '{partition_value}'"
        if spec.fy_column is not None else ""
    )
    src_row = con.execute(
        f"SELECT count(*) FROM pgdb.{spec.source_table} {where}"
    ).fetchone()
    source_rows = int(src_row[0]) if src_row else 0
    log.info("%s   pg row count: %s", log_prefix, f"{source_rows:,}")

    select_sql = _build_select(
        spec,
        partition_value=partition_value,
        partition_kind=partition_kind,
        max_rows=max_rows,
    )

    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    parquet_path_str = str(parquet_path).replace("'", "''")
    con.execute(f"""
        COPY ({select_sql}) TO '{parquet_path_str}'
        (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000);
    """)
    log.info(
        "%s   parquet write: %.1f MB in %.1fs",
        log_prefix,
        parquet_path.stat().st_size / (1 << 20),
        time.monotonic() - t0,
    )

    # Per-partition null-rate sanity check on the normalized columns the
    # downstream MVs care about. Only counts rows where the *raw* column
    # is non-empty (so we don't penalize tables that legitimately lack a
    # column for some streams).
    null_rates = _compute_null_rates(con, spec, parquet_path)

    rows_pq_row = con.execute(
        f"SELECT count(*) FROM read_parquet('{parquet_path_str}')"
    ).fetchone()
    rows_pq = int(rows_pq_row[0]) if rows_pq_row else 0
    log.info(
        "%s   parquet rows: %s; null-rate %s",
        log_prefix, f"{rows_pq:,}",
        ", ".join(f"{k}={v:.2f}%" for k, v in null_rates.items())
        if null_rates else "n/a",
    )
    con.close()
    return source_rows, rows_pq, null_rates


def _compute_null_rates(
    con: duckdb.DuckDBPyConnection, spec: StreamSpec, parquet_path: Path,
) -> dict[str, float]:
    """Compute % rows where each per-stream-relevant normalized column is
    NULL among rows where the raw source column is non-empty.

    investments / investment_dashboard:
      lender_name_null_pct, county_fips_null_pct, naics_2digit_null_pct
    loans:
      lender_name_null_pct, borrower_name_null_pct
    lenders:
      lender_name_null_pct, state_normalized_null_pct
    """
    parquet_path_str = str(parquet_path).replace("'", "''")
    rates: dict[str, float] = {}

    def pct(num_expr: str, denom_expr: str) -> float:
        row = con.execute(f"""
            SELECT
              count(*) FILTER (WHERE {denom_expr}) AS denom,
              count(*) FILTER (WHERE {denom_expr} AND ({num_expr})) AS num
            FROM read_parquet('{parquet_path_str}')
        """).fetchone()
        if row is None or row[0] == 0:
            return 0.0
        return round(100.0 * int(row[1]) / int(row[0]), 4)

    if spec.name in ("investments", "investment_dashboard"):
        rates["lender_name_null_pct"] = pct(
            "lender_name_normalized IS NULL",
            "lender_name IS NOT NULL AND trim(lender_name) <> ''"
        )
        rates["county_fips_null_pct"] = pct(
            "county_fips_normalized IS NULL",
            "county_fips IS NOT NULL AND trim(county_fips) <> ''"
        )
        if spec.name == "investments":
            rates["naics_2digit_null_pct"] = pct(
                "naics_2digit IS NULL",
                "naics_industry_sector_code IS NOT NULL AND trim(naics_industry_sector_code) <> ''"
            )
    elif spec.name == "loans":
        rates["lender_name_null_pct"] = pct(
            "lender_name_normalized IS NULL",
            "holding_lender_name IS NOT NULL AND trim(holding_lender_name) <> ''"
        )
        rates["borrower_name_null_pct"] = pct(
            "borrower_name_normalized IS NULL",
            "borrower_name IS NOT NULL AND trim(borrower_name) <> ''"
        )
    elif spec.name == "lenders":
        rates["lender_name_null_pct"] = pct(
            "lender_name_normalized IS NULL",
            "lender_name_display IS NOT NULL AND trim(lender_name_display) <> ''"
        )
        rates["state_normalized_null_pct"] = pct(
            "lender_state_normalized IS NULL",
            "lender_state IS NOT NULL AND trim(lender_state) <> ''"
        )
    return rates


def upload_to_r2(parquet_path: Path, *, key: str) -> int:
    s3 = _r2_client()
    s3.upload_file(
        str(parquet_path), R2_BUCKET, key,
        ExtraArgs={"ContentType": "application/x-parquet"},
    )
    return parquet_path.stat().st_size


# --------------------------------------------------------------------------- #
# Audit-row helpers
# --------------------------------------------------------------------------- #


def insert_run_row(
    conn: psycopg.Connection, *,
    stream: str, partition_kind: str, partition_value: str,
    source_table: str,
) -> str:
    sql = """
    INSERT INTO ops.usda_rd_r2_ingest_runs (
        stream, partition_kind, partition_value, status, source_table
    ) VALUES (%s, %s, %s, 'running', %s)
    RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (stream, partition_kind, partition_value, source_table))
        row_id = cur.fetchone()[0]
    conn.commit()
    return str(row_id)


def finalize_run_row(
    conn: psycopg.Connection, run_id: str,
    *,
    status: str,
    started_wall: float,
    source_row_count: int | None,
    parquet_row_count: int | None,
    parquet_bytes: int | None,
    parquet_columns: int | None,
    r2_bucket: str | None, r2_prefix: str | None,
    r2_object_key: str | None, r2_total_bytes: int | None,
    null_rates: dict[str, float] | None,
    error_message: str | None,
    notes: dict[str, Any] | None,
) -> None:
    duration = round(time.monotonic() - started_wall, 3)
    rates = null_rates or {}
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE ops.usda_rd_r2_ingest_runs
               SET status = %s,
                   source_row_count = %s,
                   parquet_row_count = %s,
                   parquet_bytes_written = %s,
                   parquet_column_count = %s,
                   r2_bucket = %s, r2_prefix = %s,
                   r2_object_key = %s, r2_total_bytes = %s,
                   lender_name_null_pct = %s,
                   borrower_name_null_pct = %s,
                   county_fips_null_pct = %s,
                   state_normalized_null_pct = %s,
                   naics_2digit_null_pct = %s,
                   program_normalized_null_pct = %s,
                   finished_at = now(), duration_seconds = %s,
                   error_message = %s, notes = %s
             WHERE id = %s;
        """, (
            status, source_row_count, parquet_row_count, parquet_bytes,
            parquet_columns, r2_bucket, r2_prefix, r2_object_key,
            r2_total_bytes,
            rates.get("lender_name_null_pct"),
            rates.get("borrower_name_null_pct"),
            rates.get("county_fips_null_pct"),
            rates.get("state_normalized_null_pct"),
            rates.get("naics_2digit_null_pct"),
            rates.get("program_normalized_null_pct"),
            duration, error_message,
            Jsonb(notes) if notes else None, run_id,
        ))
    conn.commit()


# --------------------------------------------------------------------------- #
# Per-(stream, partition) main
# --------------------------------------------------------------------------- #


def ingest_partition(
    spec: StreamSpec, *,
    partition_value: str,
    workdir: Path,
    max_rows: int | None,
    r2_prefix_override: str | None,
    dry_run: bool,
) -> int:
    log_prefix = f"[{spec.name}/{spec.partition_kind}={partition_value}]"
    started_wall = time.monotonic()

    if spec.partition_kind == "year":
        target_prefix = (
            r2_prefix_override or
            f"usda-rd/{spec.name}/year={partition_value}"
        )
    else:
        target_prefix = (
            r2_prefix_override or
            f"usda-rd/{spec.name}/snapshot={partition_value}"
        )
    target_key = target_prefix.rstrip("/") + "/data.parquet"

    log.info("%s start → s3://%s/%s", log_prefix, R2_BUCKET, target_key)
    if dry_run:
        log.info("%s DRY RUN — exiting before pg connect", log_prefix)
        return 0

    parquet_path = workdir / f"{spec.name}_{partition_value}.parquet"
    parquet_path.parent.mkdir(parents=True, exist_ok=True)

    with psycopg.connect(_database_url()) as conn:
        run_id = insert_run_row(
            conn,
            stream=spec.name,
            partition_kind=spec.partition_kind,
            partition_value=partition_value,
            source_table=spec.source_table,
        )
        log.info("%s run id=%s", log_prefix, run_id)

        try:
            source_rows, parquet_rows, null_rates = transform_to_parquet(
                spec, parquet_path,
                partition_value=partition_value,
                partition_kind=spec.partition_kind,
                max_rows=max_rows,
                log_prefix=log_prefix,
            )

            if max_rows is None and source_rows > 0:
                variance = abs(parquet_rows - source_rows) / source_rows
                if variance > 0.001:
                    raise RuntimeError(
                        f"row-count variance {variance:.4%} > 0.1% "
                        f"(pg={source_rows:,} pq={parquet_rows:,})"
                    )

            if source_rows == 0:
                log.warning("%s source has 0 rows for partition; "
                            "writing audit no_change and skipping R2 upload",
                            log_prefix)
                finalize_run_row(
                    conn, run_id, status="no_change",
                    started_wall=started_wall,
                    source_row_count=0,
                    parquet_row_count=0,
                    parquet_bytes=None, parquet_columns=None,
                    r2_bucket=None, r2_prefix=None,
                    r2_object_key=None, r2_total_bytes=None,
                    null_rates=None,
                    error_message=None,
                    notes={"reason": "source partition empty"},
                )
                return 0

            uploaded = upload_to_r2(parquet_path, key=target_key)
            log.info("%s uploaded → s3://%s/%s (%.1f MB)",
                     log_prefix, R2_BUCKET, target_key, uploaded / (1 << 20))

            column_count = (
                len(spec.raw_columns)
                + len(spec.normalized_columns)
                + 1  # partition metadata col
            )
            finalize_run_row(
                conn, run_id, status="completed",
                started_wall=started_wall,
                source_row_count=source_rows,
                parquet_row_count=parquet_rows,
                parquet_bytes=uploaded,
                parquet_columns=column_count,
                r2_bucket=R2_BUCKET,
                r2_prefix=target_prefix.rstrip("/") + "/",
                r2_object_key=target_key,
                r2_total_bytes=uploaded,
                null_rates=null_rates,
                error_message=None,
                notes={
                    "max_rows": max_rows,
                    "r2_prefix_override": r2_prefix_override,
                },
            )
            log.info("%s DONE rows=%s wall=%.1fs",
                     log_prefix, f"{parquet_rows:,}",
                     time.monotonic() - started_wall)
            return 0

        except Exception as exc:
            log.exception("%s ingest failed", log_prefix)
            try:
                finalize_run_row(
                    conn, run_id, status="failed",
                    started_wall=started_wall,
                    source_row_count=None, parquet_row_count=None,
                    parquet_bytes=None, parquet_columns=None,
                    r2_bucket=None, r2_prefix=None,
                    r2_object_key=None, r2_total_bytes=None,
                    null_rates=None,
                    error_message=str(exc), notes=None,
                )
            except Exception:
                log.exception("%s failed to finalize audit row on error",
                              log_prefix)
            return 1

        finally:
            try:
                parquet_path.unlink(missing_ok=True)
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_year_range(s: str) -> list[int]:
    if "-" in s:
        a, b = s.split("-", 1)
        ya, yb = int(a), int(b)
    else:
        ya = yb = int(s)
    return [y for y in range(ya, yb + 1) if y in DEFAULT_YEAR_SPAN]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--stream",
                   choices=list(STREAM_SPECS.keys()),
                   default=None,
                   help="Single stream to ingest. With --all, ignored.")
    p.add_argument("--years", default=None,
                   help="Year range, e.g., 2016-2026. Per-FY streams only.")
    p.add_argument("--all", action="store_true",
                   help="Ingest all streams (per-FY span DEFAULT_YEAR_SPAN).")
    p.add_argument("--snapshot-date", default=None,
                   help="ISO date (YYYY-MM-DD) for snapshot-partitioned "
                        "streams. Default: today UTC.")
    p.add_argument("--max-rows", type=int, default=None,
                   help="Smoke testing: cap rows per partition.")
    p.add_argument("--workdir", default=None,
                   help="Staging dir. Default /tmp/usda_rd_r2_ingest.")
    p.add_argument("--r2-prefix-override", default=None,
                   help="Replace canonical usda-rd/{stream}/{kind}={val}/ "
                        "prefix (smoke testing).")
    p.add_argument("--dry-run", action="store_true",
                   help="Probe only; no DB writes, no R2 uploads.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    workdir = Path(args.workdir or "/tmp/usda_rd_r2_ingest")
    workdir.mkdir(parents=True, exist_ok=True)

    if args.snapshot_date:
        snap = datetime.strptime(args.snapshot_date, "%Y-%m-%d").date()
    else:
        snap = datetime.now(timezone.utc).date()
    snap_str = snap.isoformat()

    if args.all:
        plan: list[tuple[StreamSpec, str]] = []
        for spec in (_INVESTMENTS_SPEC, _LOANS_SPEC):
            for y in DEFAULT_YEAR_SPAN:
                plan.append((spec, str(y)))
        for spec in (_LENDERS_SPEC, _INVESTMENT_DASHBOARD_SPEC):
            plan.append((spec, snap_str))
    else:
        if args.stream is None:
            log.error("must pass --stream <name> or --all")
            return 2
        spec = STREAM_SPECS[args.stream]
        if spec.partition_kind == "year":
            if args.years:
                years = parse_year_range(args.years)
            else:
                years = list(DEFAULT_YEAR_SPAN)
            plan = [(spec, str(y)) for y in years]
        else:
            plan = [(spec, snap_str)]

    log.info("plan: %d (stream, partition) tuples", len(plan))
    rc = 0
    for spec, partition_value in plan:
        log.info("=" * 70)
        log.info("=== INGEST: %s / %s=%s ===",
                 spec.name, spec.partition_kind, partition_value)
        log.info("=" * 70)
        rc_one = ingest_partition(
            spec,
            partition_value=partition_value,
            workdir=workdir,
            max_rows=args.max_rows,
            r2_prefix_override=args.r2_prefix_override,
            dry_run=args.dry_run,
        )
        if rc_one != 0:
            rc = rc_one
            log.error("[%s/%s] failed; continuing with remaining partitions",
                      spec.name, partition_value)
    return rc


if __name__ == "__main__":
    sys.exit(main())

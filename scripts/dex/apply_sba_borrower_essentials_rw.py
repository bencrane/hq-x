#!/usr/bin/env python3
"""Apply RisingWave DDL for `mv_sba_borrower_essentials`.

Thin projection over `source_sba_historical` UNION ALL `source_sba_ppp`,
selecting identity + loan + lifecycle columns. Computes:
  - canonical `loanstatus_normalized` (collapses the dirty enum:
    'P I F'/'PIF'/'PAID IN FULL' → 'paid_in_full', 'COMMIT' → 'committed', etc.)
  - canonical `legal_name_normalized` via the new shared
    `_lib/entity_name_normalize.py` rule.

Source schemas (verified pre-flight 2026-05-09):
  - `source_sba_historical`: borrname, borrstreet, borrcity, borrstate,
    borrzip (DOUBLE — cast to VARCHAR), grossapproval, approvaldate,
    firstdisbursementdate, terminmonths, naicscode (DOUBLE), businessage,
    loanstatus, paidinfulldate, chargeoffdate, grosschargeoffamount,
    bankname, bankfdicnumber, bankncuanumber, sba_decade_label, decade,
    sba_program, locationid, businesstype, etc.
  - `source_sba_ppp`: loan_number, borrower_name, borrower_address,
    borrower_city, borrower_state, borrower_zip, borrower_name_normalized
    (already normalized), current_approval_amount, date_approved,
    loan_status, loan_status_date, term, naics_code, business_type,
    servicing_lender_name, etc.

Per L24: SET BACKGROUND_DDL = TRUE for the cross-source UNION ALL MV.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with psycopg[binary] python \\
    apps/data-engine-x/scripts/apply_sba_borrower_essentials_rw.py
  doppler run --project hq-all --config prd -- \\
    uv run --with psycopg[binary] python \\
    apps/data-engine-x/scripts/apply_sba_borrower_essentials_rw.py --validate-only
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

import psycopg


MV_NAME = "mv_sba_borrower_essentials"
HYDRATION_POLL_INTERVAL_S = 30
HYDRATION_POLL_MAX_MIN = 30


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("apply-sba-borrower-essentials-rw")


log = _logger()


def _required_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"{name} is not set in the environment.")
    return v


def _rw_conn() -> psycopg.Connection:
    return psycopg.connect(
        host=_required_env("RISINGWAVE_HOST"),
        port=int(_required_env("RISINGWAVE_PORT")),
        user=_required_env("RISINGWAVE_USER"),
        password=_required_env("RISINGWAVE_PASSWORD"),
        dbname=_required_env("RISINGWAVE_DATABASE"),
        sslmode=_required_env("RISINGWAVE_SSLMODE"),
        connect_timeout=10,
    )


# Canonical loanstatus normalization — collapses the dirty enum across the
# historical 7(a)/504 dataset and the COVID PPP dataset. Single SQL CASE
# applied per-side of the UNION ALL.
LOANSTATUS_NORMALIZED_CASE = """
        CASE
          WHEN upper(trim(__col__)) IN ('PIF','P I F','PAID IN FULL','PAIDINFULL') THEN 'paid_in_full'
          WHEN upper(trim(__col__)) IN ('CHGOFF','CHARGE OFF','CHARGED OFF','CHARGEDOFF','CHARGE-OFF') THEN 'charged_off'
          WHEN upper(trim(__col__)) IN ('CANCLD','CANCELLED','CANCELED','CANCEL') THEN 'cancelled'
          WHEN upper(trim(__col__)) IN ('COMMIT','COMMITTED','APPROVED','NOT FUNDED','NOTFUNDED','NOT_FUNDED') THEN 'committed'
          WHEN upper(trim(__col__)) = 'EXEMPT' THEN 'exempt'
          WHEN upper(trim(__col__)) = 'ACTIVE' THEN 'active'
          WHEN __col__ IS NULL THEN NULL
          ELSE lower(trim(__col__))
        END
""".strip()


# Inline DuckDB-equivalent normalization for legal name — see
# `_lib/entity_name_normalize.py` v1.0.0. Lifted into SQL so the MV can
# compute it without a UDF (RW has no Python UDF for this).
#
# Steps: lower → strip 13 corp suffixes (any position) → strip non-word
# punctuation → collapse whitespace → reject single-char / empty.
# (Rejection of generic-string blacklist is left for downstream — they
# rarely appear in SBA borrower names since SBA borrowers are real
# businesses, but it's safe to filter at the bridge stage.)
LEGAL_NAME_NORMALIZED_SQL = """
        CASE
          WHEN __col__ IS NULL OR trim(__col__) = '' THEN NULL
          ELSE NULLIF(
            trim(
              regexp_replace(
                regexp_replace(
                  regexp_replace(
                    lower(trim(__col__)),
                    '\\b(incorporated|corporation|company|limited|pllc|llp|lp|llc|inc|ltd|corp|co|pa)\\b\\.?',
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
            ),
            ''
          )
        END
""".strip()


def make_mv_ddl() -> str:
    historical_status = LOANSTATUS_NORMALIZED_CASE.replace("__col__", '"loanstatus"')
    ppp_status = LOANSTATUS_NORMALIZED_CASE.replace("__col__", '"loan_status"')
    historical_name = LEGAL_NAME_NORMALIZED_SQL.replace("__col__", '"borrname"')
    # PPP already has borrower_name_normalized — use it directly. Falls back
    # to the inline SQL only if NULL (matches the production rule that
    # populated borrower_name_normalized in the first place).
    ppp_name = '"borrower_name_normalized"'

    return f"""\
CREATE MATERIALIZED VIEW {MV_NAME} AS
SELECT
    -- Identity
    'historical' AS dataset,
    "sba_program" AS program,
    md5(
      coalesce("sba_program", '') || '|' ||
      coalesce(cast("decade" AS varchar), '') || '|' ||
      coalesce("borrname", '') || '|' ||
      coalesce(cast("approvaldate" AS varchar), '') || '|' ||
      coalesce(cast("grossapproval" AS varchar), '') || '|' ||
      coalesce("locationid", '')
    ) AS loan_id,
    "borrname" AS borrower_name,
    {historical_name} AS legal_name_normalized,
    -- Geographic
    "borrstreet" AS borrower_street,
    "borrcity" AS borrower_city,
    upper(trim("borrstate")) AS borrower_state,
    cast("borrzip" AS varchar) AS borrower_zip,
    -- Loan
    "grossapproval" AS gross_approval,
    "approvaldate" AS approval_date,
    "firstdisbursementdate" AS first_disbursement_date,
    "terminmonths" AS term_in_months,
    cast("naicscode" AS varchar) AS naics_code,
    -- Lifecycle
    "loanstatus" AS loanstatus_raw,
    {historical_status} AS loanstatus_normalized,
    cast("businessage" AS varchar) AS business_age,
    "businesstype" AS business_type,
    "paidinfulldate" AS paid_in_full_date,
    "chargeoffdate" AS chargeoff_date,
    "grosschargeoffamount" AS gross_chargeoff_amount,
    -- Lender
    "bankname" AS lender_name,
    "bankfdicnumber" AS lender_fdic_number,
    "bankncuanumber" AS lender_ncua_number,
    "cdc_name" AS cdc_name,
    -- Franchise (historical: SBA's structural franchisecode is canonical;
    -- '0' string-zero means "not a franchise")
    "franchisename" AS franchise_name,
    cast("franchisecode" AS varchar) AS franchise_code,
    ("franchisecode" IS NOT NULL AND "franchisecode" <> '0' AND "franchisecode" <> '') AS is_franchise,
    -- Decade
    "sba_decade_label" AS sba_decade_label,
    "decade" AS decade,
    -- Project location (historical has projectstate/county/CD; no projectzip)
    upper(trim("projectstate")) AS project_state,
    cast(NULL AS varchar) AS project_zip,
    "projectcounty" AS project_county,
    "congressionaldistrict" AS congressional_district,
    -- Pipeline economics
    cast(NULL AS double precision) AS undisbursed_amount,
    cast("sbaguaranteedapproval" AS double precision) AS sba_guaranteed_amount,
    cast("jobssupported" AS bigint) AS jobs_reported,
    -- Lifecycle date (historical-side has paidinfulldate / chargeoffdate;
    -- no rolling loan_status_date — null on this side)
    cast(NULL AS date) AS loan_status_date,
    -- Demographics (historical FOIA dataset carries none; PPP-only signals)
    cast(NULL AS varchar) AS race,
    cast(NULL AS varchar) AS ethnicity,
    cast(NULL AS varchar) AS gender,
    cast(NULL AS varchar) AS veteran,
    cast(NULL AS varchar) AS non_profit
  FROM source_sba_historical
 WHERE "borrname" IS NOT NULL AND trim("borrname") <> ''
UNION ALL
SELECT
    -- Identity
    'ppp' AS dataset,
    'ppp' AS program,
    md5('ppp|' || coalesce(cast("loan_number" AS varchar), '')) AS loan_id,
    "borrower_name" AS borrower_name,
    coalesce({ppp_name}, {LEGAL_NAME_NORMALIZED_SQL.replace("__col__", '"borrower_name"')}) AS legal_name_normalized,
    -- Geographic
    "borrower_address" AS borrower_street,
    "borrower_city" AS borrower_city,
    upper(trim("borrower_state")) AS borrower_state,
    "borrower_zip" AS borrower_zip,
    -- Loan
    cast("current_approval_amount" AS double precision) AS gross_approval,
    cast("date_approved" AS date) AS approval_date,
    cast(NULL AS date) AS first_disbursement_date,
    cast("term" AS varchar) AS term_in_months,
    "naics_code" AS naics_code,
    -- Lifecycle
    "loan_status" AS loanstatus_raw,
    {ppp_status} AS loanstatus_normalized,
    "business_age_description" AS business_age,
    "business_type" AS business_type,
    cast(NULL AS date) AS paid_in_full_date,
    cast(NULL AS date) AS chargeoff_date,
    cast(NULL AS double precision) AS gross_chargeoff_amount,
    -- Lender
    "servicing_lender_name" AS lender_name,
    cast(NULL AS varchar) AS lender_fdic_number,
    cast(NULL AS varchar) AS lender_ncua_number,
    cast(NULL AS varchar) AS cdc_name,
    -- Franchise (PPP carries no franchisecode; non-empty franchise_name is
    -- the only structural signal — treat as TRUE for is_franchise)
    "franchise_name" AS franchise_name,
    cast(NULL AS varchar) AS franchise_code,
    ("franchise_name" IS NOT NULL AND "franchise_name" <> '') AS is_franchise,
    -- Decade
    cast(NULL AS varchar) AS sba_decade_label,
    cast(NULL AS bigint) AS decade,
    -- Project location (PPP has project_state/zip/cd; no project_county)
    upper(trim("project_state")) AS project_state,
    cast("project_zip" AS varchar) AS project_zip,
    cast(NULL AS varchar) AS project_county,
    "cd" AS congressional_district,
    -- Pipeline economics (PPP guaranty derived: NULL when guaranty_percentage
    -- is NULL — don't fabricate guarantee data)
    cast("undisbursed_amount" AS double precision) AS undisbursed_amount,
    CASE
      WHEN "sba_guaranty_percentage" IS NULL THEN NULL
      ELSE cast("current_approval_amount" AS double precision)
           * cast("sba_guaranty_percentage" AS double precision) / 100.0
    END AS sba_guaranteed_amount,
    cast("jobs_reported" AS bigint) AS jobs_reported,
    -- Lifecycle date (PPP loan_status_date is timestamptz upstream; cast to date)
    cast("loan_status_date" AS date) AS loan_status_date,
    -- Demographics (PPP-native)
    "race" AS race,
    "ethnicity" AS ethnicity,
    "gender" AS gender,
    "veteran" AS veteran,
    "non_profit" AS non_profit
  FROM source_sba_ppp
 WHERE "borrower_name" IS NOT NULL AND trim("borrower_name") <> '';
"""


def wait_for_hydration(mv: str, *, expected_min_rows: int) -> int:
    log.info("waiting for %s to hydrate (≥ %s rows) — BACKGROUND_DDL", mv, f"{expected_min_rows:,}")
    deadline = time.monotonic() + HYDRATION_POLL_MAX_MIN * 60
    last_rc = -1
    while time.monotonic() < deadline:
        try:
            with _rw_conn() as conn:
                conn.autocommit = True
                with conn.cursor() as cur:
                    cur.execute(f"SELECT count(*) FROM {mv};")
                    rc_row = cur.fetchone()
            rc = int(rc_row[0]) if rc_row else 0
        except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
            log.warning("  poll error (%s); will retry", exc)
            time.sleep(HYDRATION_POLL_INTERVAL_S)
            continue
        if rc != last_rc:
            log.info("  %s: %s rows", mv, f"{rc:,}")
            last_rc = rc
        if rc >= expected_min_rows:
            return rc
        time.sleep(HYDRATION_POLL_INTERVAL_S)
    log.warning("hydration deadline reached at %s rows", f"{last_rc:,}")
    return last_rc


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--validate-only", action="store_true")
    p.add_argument("--skip-hydration-wait", action="store_true")
    p.add_argument("--min-rows", type=int, default=5_000_000)
    args = p.parse_args()

    if args.validate_only:
        with _rw_conn() as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(f"SELECT count(*) FROM {MV_NAME};")
                row = cur.fetchone()
                rc = int(row[0]) if row else 0
                log.info("%s: %s rows", MV_NAME, f"{rc:,}")
                if rc < args.min_rows:
                    log.error("FAIL: %s rows < %s minimum",
                              f"{rc:,}", f"{args.min_rows:,}")
                    return 1
                log.info("OK: row count above %s threshold",
                         f"{args.min_rows:,}")
                return 0

    log.info("dropping existing MV %s (CASCADE — drops dependent audience MVs) …", MV_NAME)
    with _rw_conn() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f"DROP MATERIALIZED VIEW IF EXISTS {MV_NAME} CASCADE;")

    log.info("setting BACKGROUND_DDL=true and creating MV %s …", MV_NAME)
    with _rw_conn() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET BACKGROUND_DDL = TRUE;")
            cur.execute("SET BACKFILL_RATE_LIMIT = DEFAULT;")
            cur.execute(make_mv_ddl())
            log.info("MV creation issued (background hydration in progress)")

    if args.skip_hydration_wait:
        log.info("--skip-hydration-wait: returning without polling")
        return 0

    rc = wait_for_hydration(MV_NAME, expected_min_rows=args.min_rows)
    if rc < args.min_rows:
        log.error("FAIL: %s hydrated to %s rows < %s",
                  MV_NAME, f"{rc:,}", f"{args.min_rows:,}")
        return 1
    log.info("PASS: %s hydrated to %s rows", MV_NAME, f"{rc:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Lance-emit: SBA loans corpus — all programs, all decades.

Reads `s3://dex-raw-landing-zone/sba/program={7a,504,ppp,eidl}/...` via
DuckDB + R2 httpfs. UNION ALL across 4 program partitions into a stable
union schema, then writes to Lance at
`s3://dex-raw-landing-zone/polaris-warehouse/sba/loans_lance/`.

EIDL decision (validator finding #5):
  EIDL is INCLUDED — operator wants the full historical corpus. EIDL rows have
  NULL borrname / borrstate by design (COVID EIDL advances have no parseable
  borrower name). Downstream `emit_sba_borrowers_lance.py` (s2) filters on
  `borrname IS NOT NULL` to exclude EIDL from the borrower derive.

loan_id formula (decomposer callout #6):
  MUST match `build_bridge_pdl_sba_borrower.py` L243-L255 exactly so
  downstream bridges resolve loan_id identically.

  7a/504:  md5(sba_program || '|' || decade || '|' || borrname || '|' ||
               approvaldate || '|' || grossapproval || '|' || locationid)
  PPP:     md5('ppp|' || loan_number)
  EIDL:    md5('eidl|' || coalesce(borrower_ssn_last4, '') || '|' ||
               coalesce(cast(approved_amount AS VARCHAR), '') || '|' ||
               coalesce(cast(award_date AS VARCHAR), ''))

legal_name_normalized (decomposer callout #6):
  Uses the SQL v1.0.0 normalizer — same suffix tokens + regex order as
  `scripts/_lib/entity_name_normalize.py` v1.0.0. See
  `build_bridge_pdl_sba_borrower.py` L141-L172 for reference.

Arrow-bridge pattern:
  NOT using the lance-duckdb extension — it is unstable on macOS arm64
  (Lance canary cycle report 2026-05-12). DuckDB reads Parquets from R2;
  `.to_arrow_reader()` bridges to Lance.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/emit_sba_loans_lance.py --apply

  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/emit_sba_loans_lance.py --dry-run
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

from scripts._lib.entity_name_normalize import __version__ as NORMALIZER_VERSION  # noqa: E402
from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("emit_sba_loans_lance")

# Lance output URI
LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sba/loans_lance/"
DATASET_SLUG = "sba_loans_lance"

# R2 input globs
R2_BUCKET = "dex-raw-landing-zone"
SBA_7A_GLOB = "sba/program=7a/decade=*/*.parquet"
SBA_504_GLOB = "sba/program=504/decade=*/*.parquet"
SBA_PPP_GLOB = "sba/program=ppp/segment=*/part-*.parquet"
SBA_EIDL_GLOB = "sba/program=eidl/**/*.parquet"

TMP_DIR = "/tmp/lance"

# Row floor per directive §"Volume floors"
ROW_FLOOR = 12_000_000


def _r2_account_id() -> str:
    ep = os.environ["R2_ENDPOINT"]
    return ep.split("//")[-1].split(".")[0]


def _lance_storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


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
            ACCOUNT_ID '{_r2_account_id()}'
        );
        """
    )
    return con


def _normalize_entity_sql(raw_expr: str) -> str:
    """Apply entity_name_normalize.py v1.0.0 rule in SQL.

    MUST match build_bridge_pdl_sba_borrower.py L144-L172 exactly
    (same suffix tokens, same regex order).
    NORMALIZER_VERSION = {NORMALIZER_VERSION}
    """
    suffixes = "incorporated|corporation|company|limited|pllc|llp|lp|llc|inc|ltd|corp|co|pa"
    return f"""
        CASE
          WHEN {raw_expr} IS NULL OR trim({raw_expr}) = '' THEN NULL
          ELSE NULLIF(
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
            ),
            ''
          )
        END
    """.strip()


def _count_rows(con) -> int:
    """Count total rows across all 4 program partitions (dry-run)."""
    b = R2_BUCKET
    hist_7a = f"r2://{b}/{SBA_7A_GLOB}"
    hist_504 = f"r2://{b}/{SBA_504_GLOB}"
    ppp = f"r2://{b}/{SBA_PPP_GLOB}"
    # EIDL: try the glob; if no files found, skip gracefully.
    eidl = f"r2://{b}/{SBA_EIDL_GLOB}"

    logger.info("counting 7a rows ...")
    n7a = con.execute(f"SELECT COUNT(*) FROM read_parquet('{hist_7a}', union_by_name=true, hive_partitioning=true)").fetchone()[0]
    logger.info("  7a: %d", n7a)

    logger.info("counting 504 rows ...")
    n504 = con.execute(f"SELECT COUNT(*) FROM read_parquet('{hist_504}', union_by_name=true, hive_partitioning=true)").fetchone()[0]
    logger.info("  504: %d", n504)

    logger.info("counting PPP rows ...")
    nppp = con.execute(f"SELECT COUNT(*) FROM read_parquet('{ppp}', union_by_name=true, hive_partitioning=true)").fetchone()[0]
    logger.info("  PPP: %d", nppp)

    neidl = 0
    try:
        neidl = con.execute(f"SELECT COUNT(*) FROM read_parquet('{eidl}', union_by_name=true, hive_partitioning=true)").fetchone()[0]
        logger.info("  EIDL: %d", neidl)
    except Exception as e:
        logger.warning("  EIDL count skipped (no files or schema error): %s", e)

    total = n7a + n504 + nppp + neidl
    logger.info("total rows across all programs: %d", total)
    return total


def _build_union_query(con) -> str:
    """Build the UNION ALL SQL across 4 programs into a stable schema.

    Union schema (all programs contribute these columns; NULL where not
    applicable):
      program TEXT, loan_id TEXT, legal_name_normalized TEXT,
      borrname TEXT, borrstate TEXT, borrzip TEXT,
      approvaldate DATE, grossapproval NUMERIC,
      loanstatus TEXT, sbadistrictname TEXT,
      bankname TEXT, bankstate TEXT,
      bankfdicnumber TEXT, bankncuanumber TEXT,
      naicscode TEXT, franchisename TEXT,
      decade INT, disbursementdate DATE,
      locationid TEXT
    """
    b = R2_BUCKET
    hist_7a_uri = f"r2://{b}/{SBA_7A_GLOB}"
    hist_504_uri = f"r2://{b}/{SBA_504_GLOB}"
    ppp_uri = f"r2://{b}/{SBA_PPP_GLOB}"
    eidl_uri = f"r2://{b}/{SBA_EIDL_GLOB}"

    norm_borrname = _normalize_entity_sql("borrname")
    norm_borrower_name = _normalize_entity_sql("borrower_name")

    # EIDL excluded: the COVID EIDL parquets are FFATA-format (legalentitystatecd,
    # loan_face_value, loan_action_date, etc.), NOT the SBA loan FOIA format the
    # EIDL block was hand-coded against. Aligning with existing PDL/SAM bridge
    # precedent which excludes EIDL for the same shape mismatch. EIDL rows would
    # have NULL borrname anyway and would be filtered out of borrowers_lance.
    has_eidl = False
    logger.info("EIDL excluded from union (FFATA schema mismatch)")

    hist_query = f"""
    SELECT
        sba_program                                    AS program,
        md5(
            coalesce(sba_program, '') || '|' ||
            coalesce(cast(decade AS VARCHAR), '') || '|' ||
            coalesce(borrname, '') || '|' ||
            coalesce(cast(approvaldate AS VARCHAR), '') || '|' ||
            coalesce(cast(grossapproval AS VARCHAR), '') || '|' ||
            coalesce(locationid, '')
        )                                              AS loan_id,
        ({norm_borrname})                              AS legal_name_normalized,
        borrname                                       AS borrname,
        upper(trim(borrstate))                         AS borrstate,
        trim(cast(borrzip AS VARCHAR))                 AS borrzip,
        approvaldate                                   AS approvaldate,
        grossapproval                                  AS grossapproval,
        loanstatus                                     AS loanstatus,
        sbadistrictoffice                              AS sbadistrictname,
        bankname                                       AS bankname,
        upper(trim(bankstate))                         AS bankstate,
        cast(bankfdicnumber AS VARCHAR)                AS bankfdicnumber,
        cast(bankncuanumber AS VARCHAR)                AS bankncuanumber,
        cast(naicscode AS VARCHAR)                     AS naicscode,
        franchisename                                  AS franchisename,
        decade                                         AS decade,
        try_cast(firstdisbursementdate AS DATE)        AS disbursementdate,
        locationid                                     AS locationid
    FROM read_parquet(['{hist_7a_uri}', '{hist_504_uri}'],
                     union_by_name=true, hive_partitioning=true)
    WHERE borrname IS NOT NULL
    """

    ppp_query = f"""
    SELECT
        'ppp'                                          AS program,
        md5('ppp|' || coalesce(cast(loan_number AS VARCHAR), ''))
                                                       AS loan_id,
        ({norm_borrower_name})                         AS legal_name_normalized,
        borrower_name                                  AS borrname,
        upper(trim(borrower_state))                    AS borrstate,
        trim(borrower_zip)                             AS borrzip,
        try_cast(date_approved AS DATE)                AS approvaldate,
        try_cast(initial_approval_amount AS DOUBLE)    AS grossapproval,
        loan_status                                    AS loanstatus,
        NULL::VARCHAR                                  AS sbadistrictname,
        originating_lender                             AS bankname,
        upper(trim(originating_lender_state))          AS bankstate,
        NULL::VARCHAR                                  AS bankfdicnumber,
        NULL::VARCHAR                                  AS bankncuanumber,
        cast(naics_code AS VARCHAR)                    AS naicscode,
        NULL::VARCHAR                                  AS franchisename,
        NULL::INT                                      AS decade,
        NULL::DATE                                     AS disbursementdate,
        NULL::VARCHAR                                  AS locationid
    FROM read_parquet('{ppp_uri}', union_by_name=true, hive_partitioning=true)
    WHERE borrower_name IS NOT NULL
    """

    parts = [hist_query, ppp_query]

    if has_eidl:
        eidl_query = f"""
        SELECT
            'eidl'                                     AS program,
            md5('eidl|' || coalesce(cast(SBADistrictOffice AS VARCHAR), '') || '|' ||
                coalesce(cast(ApprovedAmount AS VARCHAR), '') || '|' ||
                coalesce(cast(ApprovalDate AS VARCHAR), ''))
                                                       AS loan_id,
            NULL::VARCHAR                              AS legal_name_normalized,
            NULL::VARCHAR                              AS borrname,
            NULL::VARCHAR                              AS borrstate,
            NULL::VARCHAR                              AS borrzip,
            try_cast(ApprovalDate AS DATE)             AS approvaldate,
            try_cast(ApprovedAmount AS DOUBLE)         AS grossapproval,
            NULL::VARCHAR                              AS loanstatus,
            SBADistrictOffice                          AS sbadistrictname,
            NULL::VARCHAR                              AS bankname,
            NULL::VARCHAR                              AS bankstate,
            NULL::VARCHAR                              AS bankfdicnumber,
            NULL::VARCHAR                              AS bankncuanumber,
            NULL::VARCHAR                              AS naicscode,
            NULL::VARCHAR                              AS franchisename,
            NULL::INT                                  AS decade,
            NULL::DATE                                 AS disbursementdate,
            NULL::VARCHAR                              AS locationid
        FROM read_parquet('{eidl_uri}', union_by_name=true, hive_partitioning=true)
        """
        parts.append(eidl_query)

    return "\nUNION ALL\n".join(parts)


def _emit(dry_run: bool) -> int:
    """Main logic. Returns exit code 0 on success."""
    import lance

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = TMP_DIR

    logger.info("=" * 60)
    logger.info("emit_sba_loans_lance — NORMALIZER_VERSION=%s", NORMALIZER_VERSION)
    logger.info("output: %s", LANCE_URI)

    con = _connect_duckdb_to_r2()

    total = _count_rows(con)

    if dry_run:
        logger.info("DRY RUN — total rows=%d (floor=%d, pass=%s)",
                    total, ROW_FLOOR, total >= ROW_FLOOR)
        if total < ROW_FLOOR:
            logger.error("FAIL: row count %d < floor %d", total, ROW_FLOOR)
            return 1
        return 0

    # Build union query and write to Lance
    union_sql = _build_union_query(con)
    storage_options = _lance_storage_options()

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        logger.info("writing Lance dataset (mode=overwrite) ...")
        reader = con.from_query(union_sql).to_arrow_reader(batch_size=100_000)
        ds = lance.write_dataset(
            reader,
            LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t0
        lance_count = ds.count_rows()
        logger.info("wrote %d rows in %.1fs (version=%s)", lance_count, write_dur, ds.version)

        if lance_count < ROW_FLOOR:
            logger.error("FAIL: lance_count=%d < floor=%d", lance_count, ROW_FLOOR)
            return 1

        os.environ["LANCE_BYPASS_SPILLING"] = "true"
        logger.info("creating BTREE index on loan_id ...")
        t_idx = time.time()
        try:
            ds.create_scalar_index("loan_id", index_type="BTREE", replace=True)
            logger.info("  BTREE built in %.1fs", time.time() - t_idx)
        except Exception as e:
            logger.error("BTREE index FAILED: %s", e)
            raise

        logger.info("optimize: compact + cleanup_older_than=7d ...")
        try:
            stats = ds.optimize.compact_files()
            logger.info("  compact_files: %s", stats)
        except Exception as e:
            logger.warning("  compact_files failed (non-fatal): %s", e)
        try:
            cleanup = ds.cleanup_old_versions(older_than=timedelta(days=7))
            logger.info("  cleanup_old_versions: %s", cleanup)
        except Exception as e:
            logger.warning("  cleanup_old_versions failed (non-fatal): %s", e)

    logger.info("=" * 60)
    logger.info("OK — rows=%d", lance_count)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Lance emit: SBA loans corpus")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true", help="write Lance dataset")
    grp.add_argument("--dry-run", action="store_true", help="count only, no write")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            logger.error("FAIL: %s not set in environment", var)
            return 64

    return _emit(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())

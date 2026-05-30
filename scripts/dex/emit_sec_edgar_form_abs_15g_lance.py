#!/usr/bin/env python3
"""Lance-emit: SEC EDGAR Form ABS-15G — filings + repurchase_summary streams.

Reads parquet from
  s3://dex-raw-landing-zone/sec-edgar/form-abs-15g/year=*/...
via DuckDB + R2 httpfs. UNION ALL across:
  - sec-edgar/form-abs-15g/year=*/filings/data.parquet  (per-year filing-level rows)
  - sec-edgar/form-abs-15g/year=*/quarter=*/repurchase_summary/data.parquet  (per-quarter)

Writes Lance dataset to
  s3://dex-raw-landing-zone/polaris-warehouse/sec_edgar/form_abs_15g_lance/

Union schema: each row tagged with ``stream`` column ('filings' or 'repurchase_summary').
Columns are the union of the two parquet schemas, with NULLs where a column is
absent on the source side. BTREE scalar index on ``accession_number``.

Arrow-bridge pattern: NOT using lance-duckdb extension. DuckDB reads R2 parquet
and ``.to_arrow_reader()`` bridges to Lance write.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/emit_sec_edgar_form_abs_15g_lance.py --apply

  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/emit_sec_edgar_form_abs_15g_lance.py --dry-run
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

from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("emit_sec_edgar_form_abs_15g_lance")

LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sec_edgar/form_abs_15g_lance/"
DATASET_SLUG = "sec_edgar_form_abs_15g_lance"

R2_BUCKET = "dex-raw-landing-zone"
FILINGS_GLOB = "sec-edgar/form-abs-15g/year=*/filings/*.parquet"
REPURCHASE_SUMMARY_GLOB = "sec-edgar/form-abs-15g/year=*/quarter=*/repurchase_summary/*.parquet"

TMP_DIR = "/tmp/lance"

# Row floor per directive §"Volume floors". Smoke pass = >0; deploy-verify
# checks the full floor of 10000 post-backfill.
ROW_FLOOR_SMOKE = 1
ROW_FLOOR_BACKFILL = 10_000


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


def _count_rows(con) -> tuple[int, int]:
    b = R2_BUCKET
    filings_uri = f"r2://{b}/{FILINGS_GLOB}"
    summary_uri = f"r2://{b}/{REPURCHASE_SUMMARY_GLOB}"

    logger.info("counting filings rows ...")
    try:
        n_filings = con.execute(
            f"SELECT COUNT(*) FROM read_parquet('{filings_uri}', union_by_name=true, hive_partitioning=true)"
        ).fetchone()[0]
    except Exception as e:
        logger.warning("  filings count error (no files yet?): %s", e)
        n_filings = 0
    logger.info("  filings: %d", n_filings)

    logger.info("counting repurchase_summary rows ...")
    try:
        n_summary = con.execute(
            f"SELECT COUNT(*) FROM read_parquet('{summary_uri}', union_by_name=true, hive_partitioning=true)"
        ).fetchone()[0]
    except Exception as e:
        logger.warning("  repurchase_summary count error (no files yet?): %s", e)
        n_summary = 0
    logger.info("  repurchase_summary: %d", n_summary)

    return n_filings, n_summary


def _build_union_query() -> str:
    """Build the UNION ALL SQL across the two streams into a stable schema."""
    b = R2_BUCKET
    filings_uri = f"r2://{b}/{FILINGS_GLOB}"
    summary_uri = f"r2://{b}/{REPURCHASE_SUMMARY_GLOB}"

    # Filings stream: all filing-level columns. Repurchase-summary-only columns
    # are NULL on this side. Tag stream='filings'.
    filings_q = f"""
    SELECT
        'filings'                                  AS stream,
        accession_number                           AS accession_number,
        cik_normalized                             AS cik_normalized,
        filer_name_raw                             AS filer_name_raw,
        filer_name_normalized                      AS filer_name_normalized,
        depositor_name_raw                         AS depositor_name_raw,
        depositor_name_normalized                  AS depositor_name_normalized,
        sponsor_name_raw                           AS sponsor_name_raw,
        sponsor_name_normalized                    AS sponsor_name_normalized,
        trustee_name_raw                           AS trustee_name_raw,
        trustee_name_normalized                    AS trustee_name_normalized,
        filer_lei_normalized                       AS filer_lei_normalized,
        form_type                                  AS form_type,
        filing_date                                AS filing_date,
        period_of_report                           AS period_of_report,
        report_year                                AS report_year,
        report_quarter                             AS report_quarter,
        asset_class_count                          AS asset_class_count,
        total_demand_count                         AS total_demand_count,
        total_repurchase_count                     AS total_repurchase_count,
        total_replacement_count                    AS total_replacement_count,
        total_dollar_amount                        AS total_dollar_amount,
        primary_doc_url                            AS primary_doc_url,
        exhibit_url                                AS exhibit_url,
        raw_xml_r2_uri                             AS raw_xml_r2_uri,
        exhibit_format                             AS exhibit_format,
        NULL::VARCHAR                              AS asset_class,
        NULL::VARCHAR                              AS asset_class_raw,
        NULL::VARCHAR                              AS reporting_period,
        NULL::BIGINT                               AS demand_count,
        NULL::BIGINT                               AS repurchase_count,
        NULL::BIGINT                               AS replacement_count,
        NULL::BIGINT                               AS dollar_amount
    FROM read_parquet('{filings_uri}', union_by_name=true, hive_partitioning=true)
    """

    # Repurchase-summary stream: only the per-asset-class columns. Filings-only
    # columns are NULL on this side. Tag stream='repurchase_summary'.
    summary_q = f"""
    SELECT
        'repurchase_summary'                       AS stream,
        accession_number                           AS accession_number,
        cik_normalized                             AS cik_normalized,
        NULL::VARCHAR                              AS filer_name_raw,
        NULL::VARCHAR                              AS filer_name_normalized,
        NULL::VARCHAR                              AS depositor_name_raw,
        NULL::VARCHAR                              AS depositor_name_normalized,
        NULL::VARCHAR                              AS sponsor_name_raw,
        NULL::VARCHAR                              AS sponsor_name_normalized,
        NULL::VARCHAR                              AS trustee_name_raw,
        NULL::VARCHAR                              AS trustee_name_normalized,
        NULL::VARCHAR                              AS filer_lei_normalized,
        NULL::VARCHAR                              AS form_type,
        NULL::VARCHAR                              AS filing_date,
        NULL::VARCHAR                              AS period_of_report,
        report_year                                AS report_year,
        report_quarter                             AS report_quarter,
        NULL::INTEGER                              AS asset_class_count,
        NULL::BIGINT                               AS total_demand_count,
        NULL::BIGINT                               AS total_repurchase_count,
        NULL::BIGINT                               AS total_replacement_count,
        NULL::BIGINT                               AS total_dollar_amount,
        NULL::VARCHAR                              AS primary_doc_url,
        NULL::VARCHAR                              AS exhibit_url,
        NULL::VARCHAR                              AS raw_xml_r2_uri,
        NULL::VARCHAR                              AS exhibit_format,
        asset_class                                AS asset_class,
        asset_class_raw                            AS asset_class_raw,
        reporting_period                           AS reporting_period,
        demand_count                               AS demand_count,
        repurchase_count                           AS repurchase_count,
        replacement_count                          AS replacement_count,
        dollar_amount                              AS dollar_amount
    FROM read_parquet('{summary_uri}', union_by_name=true, hive_partitioning=true)
    """

    return f"{filings_q}\nUNION ALL\n{summary_q}"


def _emit(dry_run: bool) -> int:
    import lance

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = TMP_DIR

    logger.info("=" * 60)
    logger.info("emit_sec_edgar_form_abs_15g_lance — output: %s", LANCE_URI)

    con = _connect_duckdb_to_r2()

    n_filings, n_summary = _count_rows(con)
    total = n_filings + n_summary

    if dry_run:
        logger.info(
            "DRY RUN — total rows=%d (smoke floor=%d, backfill floor=%d, smoke pass=%s)",
            total, ROW_FLOOR_SMOKE, ROW_FLOOR_BACKFILL, total >= ROW_FLOOR_SMOKE,
        )
        if total < ROW_FLOOR_SMOKE:
            logger.error("FAIL: row count %d < smoke floor %d", total, ROW_FLOOR_SMOKE)
            return 1
        return 0

    if total < ROW_FLOOR_SMOKE:
        logger.error("FAIL: row count %d < smoke floor %d — refusing to write empty Lance",
                     total, ROW_FLOOR_SMOKE)
        return 1

    union_sql = _build_union_query()
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

        os.environ["LANCE_BYPASS_SPILLING"] = "true"
        logger.info("creating BTREE index on accession_number ...")
        t_idx = time.time()
        try:
            ds.create_scalar_index("accession_number", index_type="BTREE", replace=True)
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
    ap = argparse.ArgumentParser(description="Lance emit: SEC EDGAR Form ABS-15G")
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

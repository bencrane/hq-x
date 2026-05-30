#!/usr/bin/env python3
"""Lance-emit: SBIR + STTR awards (Pattern A, award-grain).

Reads the latest snapshot at
  s3://dex-raw-landing-zone/sbir/snapshot=*/awards.parquet
and writes to Lance at
  s3://dex-raw-landing-zone/polaris-warehouse/sbir/awards_lance/

Award-grain (one row per award). Same contact fields the firm self-reported
on the federal grant proposal:
  - company, company_website
  - contact_name/title/phone/email
  - pi_name/title/phone/email   (Principal Investigator — typically founder/CTO)
  - ri_name, ri_poc_name, ri_poc_phone (Research Institution partner POC)
  - uei, duns, hubzone_owned, woman_owned, socially_economically_disadvantaged
  - award_amount, award_year, phase, program, agency, branch
  - address1/2, city, state, zip

Typing: snapshot_date and award/contract/proposal/notification dates are cast
to DATE via try_strptime. award_amount, award_year, number_employees stay
VARCHAR (source is dirty $-formatted; downstream callers TRY_CAST as needed).

BTREE indexes built post-write on the high-value lookup keys:
  uei, company_website, contact_email, pi_email, agency_tracking_number.

Hard-fail policy: row floor 200,000 (raw ingest smoke gate was 200K–240K).
All BTREE index builds raise on failure — no try/except swallow.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with requests python \\
    apps/data-engine-x/scripts/emit_sbir_awards_lance.py --apply

  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with requests python \\
    apps/data-engine-x/scripts/emit_sbir_awards_lance.py --dry-run
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

# Set BEFORE any lance import — prevents DataFusion sort-spill OOM on BTREE
# builds (C4 invariant; precedent in SAM v2 longitudinal emit).
os.environ["LANCE_BYPASS_SPILLING"] = "true"
os.environ["TMPDIR"] = "/tmp/lance"
Path("/tmp/lance").mkdir(parents=True, exist_ok=True)

from scripts._lib.catalog_hooks import register_or_update_polaris  # noqa: E402
from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("emit_sbir_awards_lance")

R2_BUCKET = "dex-raw-landing-zone"
SRC_GLOB = f"s3://{R2_BUCKET}/sbir/snapshot=*/awards.parquet"
LANCE_URI = f"s3://{R2_BUCKET}/polaris-warehouse/sbir/awards_lance/"
DATASET_SLUG = "sbir_awards_lance"
POLARIS_NAMESPACE = "sbir"
POLARIS_TABLE = "awards_lance"

# Smoke gate matches the R2 ingest's MIN_ROW_COUNT (200K from the directive).
ROW_FLOOR = 200_000

BTREE_COLS = (
    "uei",
    "company_website",
    "contact_email",
    "pi_email",
    "agency_tracking_number",
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


def _configure_duckdb_r2(con) -> None:
    con.execute("INSTALL httpfs; LOAD httpfs;")
    endpoint = os.environ["R2_ENDPOINT"].replace("https://", "").replace("http://", "")
    con.execute(f"SET s3_endpoint='{endpoint}';")
    con.execute("SET s3_url_style='path';")
    con.execute("SET s3_use_ssl=true;")
    con.execute(f"SET s3_access_key_id='{os.environ['R2_ACCESS_KEY_ID']}';")
    con.execute(f"SET s3_secret_access_key='{os.environ['R2_SECRET_ACCESS_KEY']}';")


# Source columns from the R2 parquet (44 = 41 source + 3 provenance).
# We keep all 41 source + DATE-cast snapshot_date; drop source_etag /
# source_last_modified (R2-ingest-only provenance, not Lance-query useful).
PROJECT_SQL = f"""
SELECT
    company,
    award_title,
    agency,
    branch,
    phase,
    program,
    agency_tracking_number,
    contract,
    try_strptime(proposal_award_date,
        ['%Y-%m-%d','%m/%d/%Y','%Y%m%d','%m%d%Y'])::DATE        AS proposal_award_date,
    try_strptime(contract_end_date,
        ['%Y-%m-%d','%m/%d/%Y','%Y%m%d','%m%d%Y'])::DATE        AS contract_end_date,
    solicitation_number,
    solicitation_year,
    try_strptime(solicitation_close_date,
        ['%Y-%m-%d','%m/%d/%Y','%Y%m%d','%m%d%Y'])::DATE        AS solicitation_close_date,
    try_strptime(proposal_receipt_date,
        ['%Y-%m-%d','%m/%d/%Y','%Y%m%d','%m%d%Y'])::DATE        AS proposal_receipt_date,
    try_strptime(date_of_notification,
        ['%Y-%m-%d','%m/%d/%Y','%Y%m%d','%m%d%Y'])::DATE        AS date_of_notification,
    topic_code,
    award_year,
    award_amount,
    uei,
    duns,
    hubzone_owned,
    socially_economically_disadvantaged,
    woman_owned,
    number_employees,
    LOWER(NULLIF(TRIM(company_website), ''))                    AS company_website,
    address1,
    address2,
    city,
    state,
    zip,
    contact_name,
    contact_title,
    contact_phone,
    LOWER(NULLIF(TRIM(contact_email), ''))                      AS contact_email,
    pi_name,
    pi_title,
    pi_phone,
    LOWER(NULLIF(TRIM(pi_email), ''))                           AS pi_email,
    ri_name,
    ri_poc_name,
    ri_poc_phone,
    try_strptime(snapshot_date,
        ['%Y-%m-%d'])::DATE                                     AS snapshot_date
FROM read_parquet('{SRC_GLOB}', union_by_name=true, hive_partitioning=true)
"""


def _emit(dry_run: bool) -> int:
    import duckdb
    import lance

    logger.info("=" * 60)
    logger.info("emit_sbir_awards_lance")
    logger.info("input:  %s", SRC_GLOB)
    logger.info("output: %s", LANCE_URI)

    storage_options = _lance_storage_options()

    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='8GB'")
    con.execute("SET temp_directory='/tmp/lance'")
    con.execute("SET preserve_insertion_order=false")
    _configure_duckdb_r2(con)

    logger.info("counting rows in source parquet ...")
    src_rows = con.execute(
        f"SELECT count(*) FROM read_parquet('{SRC_GLOB}', "
        f"union_by_name=true, hive_partitioning=true)"
    ).fetchone()[0]
    logger.info("  source rows: %d", src_rows)

    if src_rows < ROW_FLOOR:
        logger.error("FAIL: source rows %d < floor %d", src_rows, ROW_FLOOR)
        return 1

    if dry_run:
        logger.info("DRY RUN — would project %d rows to Lance with %d BTREE indexes",
                    src_rows, len(BTREE_COLS))
        return 0

    logger.info("projecting + casting in DuckDB ...")
    arrow_table = con.execute(PROJECT_SQL).arrow().read_all()
    logger.info("  projected: %d rows × %d cols",
                arrow_table.num_rows, arrow_table.num_columns)

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        logger.info("writing Lance dataset (mode=overwrite) ...")
        ds = lance.write_dataset(
            arrow_table,
            LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t0
        lance_count = ds.count_rows()
        logger.info("  wrote %d rows in %.1fs (version=%s)",
                    lance_count, write_dur, ds.version)

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
        s3_uri=LANCE_URI,
        docstring=(
            "SBIR + STTR awards (Pattern A, award-grain). One row per federal "
            "Small Business Innovation Research / Small Business Technology "
            "Transfer award. Self-reported firm + PI contact info "
            "(company_website, contact_email, pi_email, contact_phone, pi_phone) "
            "from grant proposals. BTREE on uei, company_website, contact_email, "
            "pi_email, agency_tracking_number. Source: data.www.sbir.gov bulk CSV "
            "(monthly Modal cron via run_sbir_awards_r2_ingest.py)."
        ),
    )

    logger.info("=" * 60)
    logger.info("OK — sbir/awards_lance: %d rows", lance_count)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Lance emit: SBIR + STTR awards")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true")
    grp.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            logger.error("FAIL: %s not set", var)
            return 64

    return _emit(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())

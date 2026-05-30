#!/usr/bin/env python3
"""Emit usaspending/awards_by_state_month_lance — ALL-years recipient_state×month rollup.

Derived Lance view (Layer 1 of the usaspending-derived-views-daily cycle).
Reads from awards_lance (all years in source) and produces a pre-aggregated
(recipient_state, action_month, kind) rollup.

Schema (one row per (recipient_state, action_month, kind) tuple):
  recipient_state   VARCHAR  — 2-letter state code (from recipient_location_state_code)
  action_month      VARCHAR  — 'YYYY-MM' string
  kind              VARCHAR  — 'contract' | 'assistance' | 'other'
  award_count       INT
  total_obligation  DOUBLE
  unique_recipients INT

NOTE: awards_lance uses recipient_location_state_code (NOT pop_state_code).
The derived view emits this as recipient_state per contract.md §awards schema.
(decision: use recipient_location_state_code aliased to recipient_state)

BTREE on: recipient_state, action_month.

MIN_ROW_FLOOR: 20,000 (validator-stamped; ~25K rows expected for all years).

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python apps/data-engine-x/scripts/emit_usaspending_awards_by_state_month_lance.py --apply

  doppler run --project hq-all --config prd -- \\
    uv run python apps/data-engine-x/scripts/emit_usaspending_awards_by_state_month_lance.py --dry-run
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
logger = logging.getLogger("emit_usaspending_awards_by_state_month_lance")

DATASET_SLUG = "usaspending_awards_by_state_month_lance"
OUT_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/awards_by_state_month_lance"
)
AWARDS_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/awards_lance"
)

MIN_ROWS = 20_000
TMP_DIR = "/tmp/lance"

BTREE_COLS = ["recipient_state", "action_month"]


def _lance_storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
    }


def _get_state_column(ds) -> str:
    """Detect whether awards_lance has recipient_location_state_code or pop_state_code."""
    schema_names = [f.name for f in ds.schema]
    if "recipient_location_state_code" in schema_names:
        return "recipient_location_state_code"
    elif "recipient_state_code" in schema_names:
        return "recipient_state_code"
    elif "pop_state_code" in schema_names:
        return "pop_state_code"
    else:
        raise ValueError(f"No state column found in awards_lance schema. Available: {schema_names[:20]}")


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

    # -- Load awards dataset --------------------------------------------------
    logger.info("opening awards_lance (all years) ...")
    awards_ds = lance.dataset(AWARDS_LANCE_URI, storage_options=storage_options)

    # Detect state column name at runtime (schema drift guard)
    state_col = _get_state_column(awards_ds)
    logger.info("  using state column: %r", state_col)

    awards_arrow = awards_ds.scanner(
        columns=[
            "date_signed",
            "total_obligation",
            "recipient_uei",
            state_col,
            "type",
        ],
    ).to_table()
    logger.info("  awards rows: %d", len(awards_arrow))

    # -- DuckDB aggregation ---------------------------------------------------
    con = duckdb.connect()
    con.execute("SET memory_limit='12GB'")
    con.register("awards", awards_arrow)

    # Alias the state column to recipient_state in output
    state_expr = f'"{state_col}"' if state_col != "recipient_state" else "recipient_state"
    con.execute(f"""
        CREATE TEMP TABLE result AS
        SELECT
            {state_expr}                                           AS recipient_state,
            strftime(TRY_CAST(date_signed AS DATE), '%Y-%m')      AS action_month,
            CASE
                WHEN type IN ('A', 'B', 'C', 'D') THEN 'contract'
                WHEN type IS NOT NULL              THEN 'assistance'
                ELSE 'other'
            END                                                    AS kind,
            COUNT(*)::INT                                          AS award_count,
            SUM(TRY_CAST(total_obligation AS DOUBLE))              AS total_obligation,
            COUNT(DISTINCT recipient_uei)::INT                     AS unique_recipients
        FROM awards
        WHERE {state_expr} IS NOT NULL
          AND date_signed IS NOT NULL
        GROUP BY
            recipient_state,
            action_month,
            kind
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
            max_rows_per_file=1_000_000,
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

        try:
            ds.optimize.compact_files()
        except Exception as e:
            logger.warning("compact_files failed (non-fatal): %s", e)

        try:
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as e:
            logger.warning("cleanup_old_versions failed (non-fatal): %s", e)

    total_dur = time.time() - t0
    logger.info(
        "OK — metrics: {'lance_rows': %d, 'duration_s': %.1f}",
        lance_count, total_dur,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

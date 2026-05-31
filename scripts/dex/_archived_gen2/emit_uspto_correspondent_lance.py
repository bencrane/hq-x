#!/usr/bin/env python3
"""Lance-emit: USPTO trademark correspondent_domrep_attorney bulk corpus.

Reads `s3://dex-raw-landing-zone/uspto-trademarks/year=*/correspondent_domrep_attorney.parquet`
via DuckDB + R2 httpfs. 1:1 with case_file per the 5/9 correspondent ingest
(floor 11M). Writes Lance to
`s3://dex-raw-landing-zone/polaris-warehouse/uspto/correspondent_domrep_attorney_lance/`.

Carries:
  serial_no, attorney_name_normalized, attorney_no,
  domestic_rep_name_normalized, caddr_1, caddr_2, caddr_3, caddr_4, caddr_5.

Computed column:
  is_pro_se = (attorney_name_normalized IS NULL OR attorney_name_normalized = '')

This is the upstream signal that build_bridge_uspto_sba_capital_matching_lance.py
uses to set expected_recipient_kind = 'owner' (pro-se → email goes to founder)
vs 'attorney' (firm-filed → email goes to firm).

Volume floor: >= 11,000,000 rows (per directive §"Volume floors").

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/emit_uspto_correspondent_lance.py --apply

  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/emit_uspto_correspondent_lance.py --dry-run
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
logger = logging.getLogger("emit_uspto_correspondent_lance")

# Lance output URI
LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/uspto/correspondent_domrep_attorney_lance/"
)
DATASET_SLUG = "uspto_correspondent_domrep_attorney_lance"

# R2 input glob
R2_BUCKET = "dex-raw-landing-zone"
USPTO_CORR_GLOB = "uspto-trademarks/year=*/correspondent_domrep_attorney.parquet"

TMP_DIR = "/tmp/lance"

# Row floor per directive §"Volume floors"
ROW_FLOOR = 11_000_000


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


def _normalize_name_sql(raw_expr: str) -> str:
    """Light name normalization for attorney/rep names (lowercase + trim).

    Uses entity_name_normalize v1.0.0 suffix-stripping approach.
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


def _build_select_sql() -> str:
    """Build DuckDB SELECT for correspondent_domrep_attorney corpus."""
    b = R2_BUCKET

    norm_attorney = _normalize_name_sql("attorney_name")
    norm_dom_rep = _normalize_name_sql("domestic_rep_name")

    return f"""
    SELECT
        serial_no,
        ({norm_attorney})               AS attorney_name_normalized,
        attorney_no,
        ({norm_dom_rep})                AS domestic_rep_name_normalized,
        caddr_1,
        caddr_2,
        caddr_3,
        caddr_4,
        caddr_5,
        -- is_pro_se: TRUE when no attorney name present (pro-se = self-represented)
        CASE
          WHEN ({norm_attorney}) IS NULL OR trim(coalesce(({norm_attorney}), '')) = ''
          THEN TRUE
          ELSE FALSE
        END AS is_pro_se
    FROM read_parquet(
        'r2://{b}/{USPTO_CORR_GLOB}',
        union_by_name=true,
        hive_partitioning=true
    )
    WHERE serial_no IS NOT NULL
    """


def _count_rows(con) -> int:
    """Count total correspondent rows (dry-run)."""
    b = R2_BUCKET
    logger.info("counting correspondent_domrep_attorney rows across all years ...")
    result = con.execute(
        f"SELECT COUNT(*) FROM read_parquet("
        f"'r2://{b}/{USPTO_CORR_GLOB}', union_by_name=true, hive_partitioning=true) "
        f"WHERE serial_no IS NOT NULL"
    ).fetchone()
    total = result[0]
    logger.info("total correspondent rows: %d", total)
    return total


def _emit(dry_run: bool) -> int:
    """Main logic. Returns exit code 0 on success."""
    import lance

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = TMP_DIR

    logger.info("=" * 60)
    logger.info(
        "emit_uspto_correspondent_lance — NORMALIZER_VERSION=%s", NORMALIZER_VERSION
    )
    logger.info("output: %s", LANCE_URI)

    con = _connect_duckdb_to_r2()

    total = _count_rows(con)

    if dry_run:
        logger.info(
            "DRY RUN — total rows=%d (floor=%d, pass=%s)",
            total, ROW_FLOOR, total >= ROW_FLOOR,
        )
        if total < ROW_FLOOR:
            logger.error("FAIL: row count %d < floor %d", total, ROW_FLOOR)
            return 1
        return 0

    select_sql = _build_select_sql()
    storage_options = _lance_storage_options()

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        logger.info("writing Lance dataset (mode=overwrite) ...")
        reader = con.from_query(select_sql).to_arrow_reader(batch_size=100_000)
        ds = lance.write_dataset(
            reader,
            LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t0
        lance_count = ds.count_rows()
        logger.info(
            "wrote %d rows in %.1fs (version=%s)", lance_count, write_dur, ds.version
        )

        if lance_count < ROW_FLOOR:
            logger.error("FAIL: lance_count=%d < floor=%d", lance_count, ROW_FLOOR)
            return 1

        os.environ["LANCE_BYPASS_SPILLING"] = "true"

        # BTREE index on serial_no (join key from bridge → case_file_owner)
        logger.info("creating BTREE index on serial_no ...")
        t_idx = time.time()
        try:
            ds.create_scalar_index("serial_no", index_type="BTREE", replace=True)
            logger.info("  BTREE(serial_no) built in %.1fs", time.time() - t_idx)
        except Exception as e:
            logger.warning("BTREE index on serial_no failed (non-fatal): %s", e)

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
    logger.info("OK — rows=%d  duration=%.1fs", lance_count, time.time() - t0)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Lance emit: USPTO trademark correspondent_domrep_attorney"
    )
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

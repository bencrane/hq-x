#!/usr/bin/env python3
"""Lance-emit: USPTO trademark case_file bulk corpus.

Reads `s3://dex-raw-landing-zone/uspto-trademarks/year=*/case_file.parquet`
via DuckDB + R2 httpfs (R2 env from Doppler; bash -c wrapper per
CLAUDE.md §"Doppler shell gotcha"). Writes Lance to
`s3://dex-raw-landing-zone/polaris-warehouse/uspto/case_file_lance/`.

Preserves all case_file columns; adds derived columns:
  - mark_text_normalized      (lowercased + whitespace-collapsed)
  - legal_name_normalized     (via _lib/entity_name_normalize v1.0.0)
  - case_file_year            (extracted from year= partition via filename)

CRITICAL: imports _lib/entity_name_normalize.__version__ == "1.0.0" and
asserts parity at module load. Any drift collapses the
(legal_name_normalized, state) join key in build_bridge_uspto_sba_capital_matching_lance.py.

Volume floor: >= 11,000,000 rows (per directive §"Volume floors"; TCFD bulk
publishes ~11.5M case files).

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/emit_uspto_case_file_lance.py --apply

  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/emit_uspto_case_file_lance.py --dry-run
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
logger = logging.getLogger("emit_uspto_case_file_lance")

EXPECTED_NORMALIZER_VERSION = "1.0.0"
if NORMALIZER_VERSION != EXPECTED_NORMALIZER_VERSION:
    raise SystemExit(
        f"FAIL: entity_name_normalize.__version__={NORMALIZER_VERSION!r} "
        f"!= {EXPECTED_NORMALIZER_VERSION!r}. Cross-source join key will collapse. "
        "Abort."
    )

# Lance output URI
LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/uspto/case_file_lance/"
DATASET_SLUG = "uspto_case_file_lance"

# R2 input glob (hive-partitioned by year)
R2_BUCKET = "dex-raw-landing-zone"
USPTO_CASE_FILE_GLOB = "uspto-trademarks/year=*/case_file.parquet"

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


def _normalize_entity_sql(raw_expr: str) -> str:
    """Apply entity_name_normalize.py v1.0.0 rule in SQL.

    MUST match emit_sba_loans_lance.py / build_bridge_pdl_sba_borrower.py exactly
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


def _build_select_sql() -> str:
    """Build DuckDB SELECT across all years of case_file.parquet.

    Hot-fix 2026-05-13 (post-merge column-name correction): The bulk-ingest at
    `run_uspto_trademarks_r2_ingest.py` already pre-computes `mark_text_normalized`
    AND `case_file_year` in the source Parquets, AND there is no `mark_identification`
    column (closest is `mark_id_char`, a 4-char draw code). Audit pseudocode
    assumed raw column names. Simplifying to passthrough — the schema we want is
    already on disk. `legal_name_normalized` is removed: it is an owner attribute,
    not a mark attribute; the bridge joins on the OWNER side's value (s2).
    """
    b = R2_BUCKET
    return f"""
    SELECT *
    FROM read_parquet(
        'r2://{b}/{USPTO_CASE_FILE_GLOB}',
        union_by_name=true,
        hive_partitioning=true
    )
    """


def _count_rows(con) -> int:
    """Count total rows (dry-run)."""
    b = R2_BUCKET
    glob_uri = f"r2://{b}/{USPTO_CASE_FILE_GLOB}"
    logger.info("counting case_file rows across all years ...")
    result = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('r2://{b}/{USPTO_CASE_FILE_GLOB}', "
        f"union_by_name=true, hive_partitioning=true)"
    ).fetchone()
    total = result[0]
    logger.info("total case_file rows: %d", total)
    return total


def _emit(dry_run: bool) -> int:
    """Main logic. Returns exit code 0 on success."""
    import lance

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = TMP_DIR

    logger.info("=" * 60)
    logger.info("emit_uspto_case_file_lance — NORMALIZER_VERSION=%s", NORMALIZER_VERSION)
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

        # BTREE index on serial_no (dominant access pattern from bridge)
        logger.info("creating BTREE index on serial_no ...")
        t_idx = time.time()
        try:
            ds.create_scalar_index("serial_no", index_type="BTREE", replace=True)
            logger.info("  BTREE(serial_no) built in %.1fs", time.time() - t_idx)
        except Exception as e:
            logger.warning("BTREE index on serial_no failed (non-fatal): %s", e)

        # BTREE on legal_name_normalized (join key for bridge)
        try:
            ds.create_scalar_index(
                "legal_name_normalized", index_type="BTREE", replace=True
            )
            logger.info("  BTREE(legal_name_normalized) built")
        except Exception as e:
            logger.warning(
                "BTREE index on legal_name_normalized failed (non-fatal): %s", e
            )

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
    ap = argparse.ArgumentParser(description="Lance emit: USPTO trademark case_file")
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

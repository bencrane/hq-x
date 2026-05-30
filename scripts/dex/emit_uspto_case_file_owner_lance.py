#!/usr/bin/env python3
"""Lance-emit: USPTO trademark case_file_owner — primary applicants (own_seq=1).

Reads `s3://dex-raw-landing-zone/uspto-trademarks/year=*/case_file_owner.parquet`
via DuckDB + R2 httpfs. Filters to own_seq = '1' (primary applicant; covers
~98.5% of marks per directive 2026-05-10 §"own_seq filter probe"). Writes
Lance to `s3://dex-raw-landing-zone/polaris-warehouse/uspto/case_file_owner_lance/`.

CRITICAL: legal_name_normalized derived via _lib/entity_name_normalize.__version__
'1.0.0' — same module + same version as SBA path so the JOIN in
build_bridge_uspto_sba_capital_matching_lance.py works. Any version drift
collapses the (legal_name_normalized, state) join key.

own_seq format note: the directive §"Failure modes" calls out that own_seq
may be stored as string '1', integer 1, or '01'. We cast to VARCHAR and compare
'1' so it works regardless of the stored type.

Volume floor: >= 11,000,000 rows (own_seq=1 covers 1:1 with case_file on the
primary-applicant slice per directive §"Volume floors").

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/emit_uspto_case_file_owner_lance.py --apply

  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/emit_uspto_case_file_owner_lance.py --dry-run
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
logger = logging.getLogger("emit_uspto_case_file_owner_lance")

EXPECTED_NORMALIZER_VERSION = "1.0.0"
if NORMALIZER_VERSION != EXPECTED_NORMALIZER_VERSION:
    raise SystemExit(
        f"FAIL: entity_name_normalize.__version__={NORMALIZER_VERSION!r} "
        f"!= {EXPECTED_NORMALIZER_VERSION!r}. Cross-source join key will collapse. "
        "Abort."
    )

# Lance output URI
LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/uspto/case_file_owner_lance/"
DATASET_SLUG = "uspto_case_file_owner_lance"

# R2 input glob
R2_BUCKET = "dex-raw-landing-zone"
USPTO_OWNER_GLOB = "uspto-trademarks/year=*/case_file_owner.parquet"

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
    """Build DuckDB SELECT for primary-applicant owners (own_seq = '1').

    own_seq cast: directive §"Failure modes" warns the stored type may be
    string, integer, or '01'. CAST(own_seq AS VARCHAR) = '1' handles all three.
    """
    b = R2_BUCKET

    norm_own_name = _normalize_entity_sql("own_name")
    norm_legal_name = _normalize_entity_sql("own_name")

    # Hot-fix 2026-05-13: source Parquets are pre-normalized by
    # run_uspto_trademarks_r2_ingest.py — use existing _normalized / _5 columns
    # instead of the audit's assumed raw names (owner_state / owner_country /
    # owner_zip do not exist).
    norm_state = "upper(trim(cast(owner_state_normalized AS VARCHAR)))"
    norm_country = "upper(trim(cast(owner_country_normalized AS VARCHAR)))"
    norm_zip5 = "left(trim(cast(owner_zip5 AS VARCHAR)), 5)"
    norm_kind = "upper(trim(cast(own_entity_cd AS VARCHAR)))"

    return f"""
    SELECT
        serial_no,
        own_name,
        ({norm_own_name})               AS owner_name_normalized,
        ({norm_legal_name})             AS legal_name_normalized,
        {norm_zip5}                     AS owner_zip5,
        {norm_state}                    AS owner_state_normalized,
        {norm_country}                  AS owner_country_normalized,
        {norm_kind}                     AS owner_kind_normalized,
        own_addr_1,
        own_addr_city,
        own_entity_cd
    FROM read_parquet(
        'r2://{b}/{USPTO_OWNER_GLOB}',
        union_by_name=true,
        hive_partitioning=true
    )
    WHERE CAST(own_seq AS VARCHAR) = '1'
      AND own_name IS NOT NULL
      AND trim(own_name) <> ''
    """


def _count_rows(con) -> int:
    """Count primary-applicant rows (dry-run)."""
    b = R2_BUCKET
    logger.info("counting case_file_owner rows (own_seq='1') across all years ...")
    result = con.execute(
        f"SELECT COUNT(*) FROM read_parquet("
        f"'r2://{b}/{USPTO_OWNER_GLOB}', union_by_name=true, hive_partitioning=true) "
        f"WHERE CAST(own_seq AS VARCHAR) = '1' AND own_name IS NOT NULL AND trim(own_name) <> ''"
    ).fetchone()
    total = result[0]
    logger.info("total case_file_owner rows (own_seq=1): %d", total)
    return total


def _emit(dry_run: bool) -> int:
    """Main logic. Returns exit code 0 on success."""
    import lance

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = TMP_DIR

    logger.info("=" * 60)
    logger.info("emit_uspto_case_file_owner_lance — NORMALIZER_VERSION=%s", NORMALIZER_VERSION)
    logger.info("output: %s", LANCE_URI)
    logger.info("filter: own_seq = '1' (primary applicant)")

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

        # BTREE index on legal_name_normalized (primary join key for bridge)
        logger.info("creating BTREE index on legal_name_normalized ...")
        t_idx = time.time()
        try:
            ds.create_scalar_index(
                "legal_name_normalized", index_type="BTREE", replace=True
            )
            logger.info("  BTREE(legal_name_normalized) built in %.1fs", time.time() - t_idx)
        except Exception as e:
            logger.warning("BTREE index on legal_name_normalized failed (non-fatal): %s", e)

        # BTREE index on serial_no (join key for bridge → case_file)
        try:
            ds.create_scalar_index("serial_no", index_type="BTREE", replace=True)
            logger.info("  BTREE(serial_no) built")
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
        description="Lance emit: USPTO trademark case_file_owner (own_seq=1)"
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

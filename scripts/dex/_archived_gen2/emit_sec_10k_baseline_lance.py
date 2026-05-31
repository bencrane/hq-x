#!/usr/bin/env python3
"""Lance-emit: SEC 10-K corporate baseline spine — parent-row flatten.

Source: ``s3://dex-raw-landing-zone/polaris-warehouse/sec_edgar/form_10k_lance/``
        (3.21M rows; union of 7 streams per filing).

Output: ``s3://dex-raw-landing-zone/polaris-warehouse/spines/sec_10k_baseline_lance/``
        — one row per 10-K / 10-K/A parent filing.

Filter:  ``form_type IN ('10-K', '10-K/A') AND cik_normalized IS NOT NULL``
         (drops the 3.09M nested child-stream rows).

Projection:
  cik, ein, lei, legal_name, accession_number,
  filing_date::DATE, period_end_date::DATE, fiscal_year::BIGINT

Dedup: ``QUALIFY row_number() OVER (PARTITION BY cik, fiscal_year
ORDER BY filing_date DESC, accession_number DESC) = 1`` — strict 1:1 per
(cik, fiscal_year); 10-K/A amendments win over their original 10-K, and
duplicate-period clusters collapse to the latest filing.

Indices: BTREE on ``cik``, ``legal_name``, ``filing_date`` (hard — script
exits non-zero if any of the three fail to build).

Usage:

  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow python \\
    apps/data-engine-x/scripts/emit_sec_10k_baseline_lance.py --apply

  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow python \\
    apps/data-engine-x/scripts/emit_sec_10k_baseline_lance.py --dry-run
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
logger = logging.getLogger("emit_sec_10k_baseline_lance")

SOURCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/sec_edgar/form_10k_lance/"
)
LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/spines/sec_10k_baseline_lance/"
)
DATASET_SLUG = "spines_sec_10k_baseline_lance"
TMP_DIR = "/tmp/lance"

INDEX_COLUMNS: tuple[str, ...] = ("cik", "legal_name", "filing_date")

PROJECTION_SQL = """
SELECT
  cik_normalized                                          AS cik,
  filer_ein_normalized                                    AS ein,
  filer_lei_normalized                                    AS lei,
  filer_legal_name_normalized                             AS legal_name,
  accession_number                                        AS accession_number,
  CAST(try_strptime(filing_date,
                    ['%Y-%m-%d', '%m/%d/%Y']) AS DATE)    AS filing_date,
  CAST(try_strptime(period_of_report,
                    ['%Y-%m-%d', '%m/%d/%Y']) AS DATE)    AS period_end_date,
  CAST(form_10k_year AS BIGINT)                           AS fiscal_year
FROM source_parents
WHERE form_type IN ('10-K', '10-K/A')
  AND cik_normalized IS NOT NULL
QUALIFY row_number() OVER (
  PARTITION BY cik_normalized, form_10k_year
  ORDER BY filing_date DESC, accession_number DESC
) = 1
"""


def _lance_storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _emit(dry_run: bool) -> int:
    import duckdb
    import lance
    import pyarrow.compute as pc

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = TMP_DIR
    os.environ["LANCE_BYPASS_SPILLING"] = "true"

    storage = _lance_storage_options()
    logger.info("=" * 60)
    logger.info("emit_sec_10k_baseline_lance")
    logger.info("source: %s", SOURCE_URI)
    logger.info("target: %s", LANCE_URI)

    ds_src = lance.dataset(SOURCE_URI, storage_options=storage)
    src_total = ds_src.count_rows()
    logger.info("source row_count: %d", src_total)

    needed = [
        "cik_normalized",
        "filer_ein_normalized",
        "filer_lei_normalized",
        "filer_legal_name_normalized",
        "accession_number",
        "filing_date",
        "period_of_report",
        "form_10k_year",
        "form_type",
    ]
    filt = (
        pc.field("form_type").isin(["10-K", "10-K/A"])
        & pc.field("cik_normalized").is_valid()
    )
    src_tbl = ds_src.scanner(columns=needed, filter=filt).to_table()
    logger.info("post-pylance-filter rows: %d", src_tbl.num_rows)

    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET preserve_insertion_order=false")
    con.register("source_parents", src_tbl)

    con.execute(f"CREATE TEMP TABLE proj AS {PROJECTION_SQL}")
    rows, distinct_cik_fy, distinct_cik = con.execute(
        """
        SELECT count(*),
               count(DISTINCT (cik, fiscal_year)),
               count(DISTINCT cik)
        FROM proj
        """
    ).fetchone()
    logger.info(
        "projection rows=%d  distinct(cik,fiscal_year)=%d  distinct(cik)=%d",
        rows, distinct_cik_fy, distinct_cik,
    )
    if rows != distinct_cik_fy:
        logger.error(
            "1:1 (cik, fiscal_year) FAILED — rows=%d distinct(cik,fy)=%d "
            "(difference=%d). QUALIFY window-filter did not collapse to "
            "exactly one row per (cik, fiscal_year).",
            rows, distinct_cik_fy, rows - distinct_cik_fy,
        )
        return 4

    if dry_run:
        logger.info("[DRY-RUN] would write %d rows to %s", rows, LANCE_URI)
        return 0

    reader = con.from_query("SELECT * FROM proj").to_arrow_reader(
        batch_size=100_000,
    )

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        logger.info("writing Lance dataset (mode=overwrite) ...")
        ds = lance.write_dataset(
            reader,
            LANCE_URI,
            mode="overwrite",
            storage_options=storage,
        )
        write_dur = time.time() - t0
        lance_count = ds.count_rows()
        logger.info(
            "wrote %d rows in %.1fs (version=%s)",
            lance_count, write_dur, ds.version,
        )
        if lance_count != rows:
            logger.error(
                "post-write row count mismatch: projected=%d lance=%d",
                rows, lance_count,
            )
            return 2

        for col in INDEX_COLUMNS:
            t_idx = time.time()
            ds.create_scalar_index(col, index_type="BTREE", replace=True)
            logger.info(
                "BTREE built on %s in %.1fs", col, time.time() - t_idx,
            )

        idx_fields = {tuple(i["fields"]) for i in ds.list_indices()}
        missing = [c for c in INDEX_COLUMNS if (c,) not in idx_fields]
        if missing:
            logger.error("BTREE missing on: %s", missing)
            return 3
        logger.info("BTREE indices verified: %s", sorted(idx_fields))

        stats = ds.optimize.compact_files()
        logger.info("compact_files: %s", stats)
        cleanup = ds.cleanup_old_versions(older_than=timedelta(days=7))
        logger.info("cleanup_old_versions: %s", cleanup)

    logger.info("=" * 60)
    logger.info("OK — lance rows written: %d", lance_count)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
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

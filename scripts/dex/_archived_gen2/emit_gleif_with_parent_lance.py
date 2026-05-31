#!/usr/bin/env python3
"""Derive GLEIF LEI-with-parent Lance dataset.

Cycle: ucc-gleif-identity-spine (s2).

Reads:
  - polaris-warehouse/gleif/lei_records_lance/  (3.3M rows, Wave 2)
  - polaris-warehouse/gleif/relationship_records_lance/  (s1 output, 647K rows)

Arrow-bridge pattern (NOT the lance-duckdb extension — unstable on macOS arm64).

Logic (one-hop, per directive §"Audit's specific risk areas" #2):
  - Filter relationship_records_lance to IS_ULTIMATELY_CONSOLIDATED_BY + ACTIVE.
  - For each LEI in lei_records_lance, LEFT JOIN to find its parent via
    start_node_lei = lei → parent is end_node_lei.
  - COALESCE: if no Level-2 row exists, self-parent (ultimate_parent_lei = lei).

Output schema:
  lei                              string
  legal_name                       string
  legal_name_normalized            string
  ultimate_parent_lei              string  (= lei if self-parent)
  ultimate_parent_name             string
  ultimate_parent_name_normalized  string
  chain_depth                      int8    (0 = self-parent, 1 = one-hop Level-2)

Floor: ≥ 3,000,000 rows (1:1 with Level-1 LEI records).

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow python \\
    apps/data-engine-x/scripts/emit_gleif_with_parent_lance.py --apply

  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow python \\
    apps/data-engine-x/scripts/emit_gleif_with_parent_lance.py --dry-run
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
logger = logging.getLogger("emit_gleif_with_parent_lance")

DATASET_SLUG = "gleif_lei_with_parent_lance"
LEI_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/gleif/lei_records_lance"
REL_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/gleif/relationship_records_lance"
OUT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/gleif/lei_with_parent_lance"

MIN_ROWS = 3_000_000
TMP_DIR = "/tmp/lance"


def _lance_storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true", help="write Lance + output")
    grp.add_argument("--dry-run", action="store_true", help="count only, no writes")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")

    import lance
    import pyarrow.compute as pc

    storage_options = _lance_storage_options()

    logger.info("opening lei_records_lance via Arrow-bridge ...")
    lei_ds = lance.dataset(LEI_LANCE_URI, storage_options=storage_options)
    lei_arrow = lei_ds.scanner(
        columns=["lei", "legal_name", "legal_name_normalized"]
    ).to_table()
    rows_lei = len(lei_arrow)
    logger.info("  lei_records_lance: %d rows", rows_lei)

    logger.info("opening relationship_records_lance via Arrow-bridge ...")
    rel_ds = lance.dataset(REL_LANCE_URI, storage_options=storage_options)
    # Filter at scan time: only ACTIVE IS_ULTIMATELY_CONSOLIDATED_BY rows
    rel_arrow = rel_ds.scanner(
        columns=["start_node_lei", "end_node_lei", "relationship_type", "relationship_status"],
        filter=(
            (pc.field("relationship_type") == "IS_ULTIMATELY_CONSOLIDATED_BY")
            & (pc.field("relationship_status") == "ACTIVE")
        ),
    ).to_table()
    rows_rel = len(rel_arrow)
    logger.info(
        "  relationship_records_lance (IS_ULTIMATELY_CONSOLIDATED_BY + ACTIVE): %d rows",
        rows_rel,
    )

    import duckdb

    con = duckdb.connect()
    con.register("lei", lei_arrow)
    con.register("rel", rel_arrow)

    # Semantics (validator finding):
    #   start_node_lei = CHILD entity (the entity being consolidated)
    #   end_node_lei   = PARENT entity (the consolidating entity)
    # Self-parent fallback: COALESCE(parent_lei, lei) so the 3.1M LEIs
    # without a Level-2 row map to themselves.
    con.execute("""
        CREATE TEMP TABLE with_parent AS
        SELECT
            l.lei,
            l.legal_name,
            l.legal_name_normalized,
            COALESCE(r.end_node_lei, l.lei)                          AS ultimate_parent_lei,
            COALESCE(p.legal_name, l.legal_name)                     AS ultimate_parent_name,
            COALESCE(p.legal_name_normalized, l.legal_name_normalized)
                                                                     AS ultimate_parent_name_normalized,
            CASE WHEN r.end_node_lei IS NULL THEN 0 ELSE 1 END       AS chain_depth
        FROM lei l
        LEFT JOIN rel r ON r.start_node_lei = l.lei
        LEFT JOIN lei p ON p.lei = r.end_node_lei
    """)

    row_count = con.execute("SELECT COUNT(*) FROM with_parent").fetchone()[0]
    logger.info("with_parent row count: %d", row_count)

    chain_dist = con.execute(
        "SELECT chain_depth, COUNT(*) FROM with_parent GROUP BY chain_depth ORDER BY chain_depth"
    ).fetchall()
    logger.info("chain_depth distribution: %s", chain_dist)

    if row_count < MIN_ROWS:
        msg = f"HARD FAIL: row_count={row_count:,} < floor={MIN_ROWS:,}"
        logger.error(msg)
        return 1

    if args.dry_run:
        logger.info("DRY RUN — no Lance writes")
        return 0

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = TMP_DIR
    os.environ["LANCE_BYPASS_SPILLING"] = "true"

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        logger.info("writing to Lance at %s ...", OUT_LANCE_URI)
        reader = con.from_query("SELECT * FROM with_parent").to_arrow_reader(batch_size=100_000)
        ds = lance.write_dataset(
            reader,
            OUT_LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t0
        lance_count = ds.count_rows()
        logger.info("wrote %d rows in %.1fs (version=%s)", lance_count, write_dur, ds.version)

        try:
            ds.create_scalar_index("lei", index_type="BTREE", replace=True)
        except Exception as e:
            logger.warning("BTREE index failed (non-fatal): %s", e)
        try:
            ds.create_scalar_index("ultimate_parent_lei", index_type="BTREE", replace=True)
        except Exception as e:
            logger.warning("BTREE index (ultimate_parent_lei) failed (non-fatal): %s", e)
        try:
            ds.optimize.compact_files()
        except Exception as e:
            logger.warning("compact_files failed (non-fatal): %s", e)
        try:
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as e:
            logger.warning("cleanup_old_versions failed (non-fatal): %s", e)

    logger.info("OK — %d rows written to %s", lance_count, OUT_LANCE_URI)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

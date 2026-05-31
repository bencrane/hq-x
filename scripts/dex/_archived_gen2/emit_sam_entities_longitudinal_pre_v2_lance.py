#!/usr/bin/env python3
"""Lance-emit: SAM.gov pre-v2 entity history (longitudinal, 12 semiannual snapshots).

Lane WHO Phase 1 — SAM.gov historical longitudinal Lance emit.

Source: R2 prefix s3://dex-raw-landing-zone/sam-gov/historical-pre-v2/snapshot=*/data.parquet
Era: 2014-NOV..2020-MAY (12 semiannual snapshots)
Schema: 131-column pre-v2 SAM schema (stable across all 12 snapshots per validator probe).
PK: cage_code (DUNS is 100% redacted to "No longer available" — see validator.json P3).
Row floor: 7,334,504 (7,720,531 * 0.95, validator-stamped 2026-05-22).

IMPORTANT design notes:
- snapshot_date DATE is materialized as a literal per-snapshot in the UNION ALL pipeline.
  Do NOT use hive partition globbing (avoids picking up legacy YYYY-MON/ dirs alongside
  snapshot=YYYY-MM-DD/ dirs).
- Row identity: (cage_code, snapshot_date) composite. Do NOT dedup on cage_code alone.
- LANCE_BYPASS_SPILLING=true is set at the top of the script (before create_scalar_index).
- Mode: overwrite (one-shot bulk build).
- lance_commit_lock wraps every lance.write_dataset call.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance python \\
    apps/data-engine-x/scripts/emit_sam_entities_longitudinal_pre_v2_lance.py --apply

  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance python \\
    apps/data-engine-x/scripts/emit_sam_entities_longitudinal_pre_v2_lance.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

# Set LANCE_BYPASS_SPILLING before any lance import to prevent DataFusion OOM
# during BTREE index build on multi-million-row datasets. C4 invariant.
os.environ["LANCE_BYPASS_SPILLING"] = "true"
os.environ["TMPDIR"] = "/tmp/lance"
Path("/tmp/lance").mkdir(parents=True, exist_ok=True)

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
LOG = logging.getLogger(__name__)

DATASET_SLUG = "entities_longitudinal_pre_v2_lance"
R2_BUCKET = "dex-raw-landing-zone"
LANCE_URI = f"s3://{R2_BUCKET}/polaris-warehouse/sam_gov/{DATASET_SLUG}"

# Validator-stamped pre-v2 snapshots. Derived from per-snapshot-rowcounts.json.
# Each tuple: (snapshot_date, r2_path)
# Using snapshot=YYYY-MM-DD/ shape only (not legacy YYYY-MON/ shape to avoid duplication).
PRE_V2_SNAPSHOTS = [
    ("2014-11-30", f"r2://{R2_BUCKET}/sam-gov/historical-pre-v2/snapshot=2014-11-30/data.parquet"),
    ("2015-05-31", f"r2://{R2_BUCKET}/sam-gov/historical-pre-v2/snapshot=2015-05-31/data.parquet"),
    ("2015-11-30", f"r2://{R2_BUCKET}/sam-gov/historical-pre-v2/snapshot=2015-11-30/data.parquet"),
    ("2016-05-31", f"r2://{R2_BUCKET}/sam-gov/historical-pre-v2/snapshot=2016-05-31/data.parquet"),
    ("2016-11-30", f"r2://{R2_BUCKET}/sam-gov/historical-pre-v2/snapshot=2016-11-30/data.parquet"),
    ("2017-05-31", f"r2://{R2_BUCKET}/sam-gov/historical-pre-v2/snapshot=2017-05-31/data.parquet"),
    ("2017-11-30", f"r2://{R2_BUCKET}/sam-gov/historical-pre-v2/snapshot=2017-11-30/data.parquet"),
    ("2018-05-31", f"r2://{R2_BUCKET}/sam-gov/historical-pre-v2/snapshot=2018-05-31/data.parquet"),
    ("2018-11-30", f"r2://{R2_BUCKET}/sam-gov/historical-pre-v2/snapshot=2018-11-30/data.parquet"),
    ("2019-05-31", f"r2://{R2_BUCKET}/sam-gov/historical-pre-v2/snapshot=2019-05-31/data.parquet"),
    ("2019-11-30", f"r2://{R2_BUCKET}/sam-gov/historical-pre-v2/snapshot=2019-11-30/data.parquet"),
    ("2020-05-31", f"r2://{R2_BUCKET}/sam-gov/historical-pre-v2/snapshot=2020-05-31/data.parquet"),
]

ROW_FLOOR = 7_334_504  # 7,720,531 * 0.95 (validator-stamped)
PK_COLUMN = "cage_code"


def _r2_account_id() -> str:
    ep = os.environ["R2_ENDPOINT"]
    return ep.split("//")[-1].split(".")[0]


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
        )
        """
    )
    return con


def _lance_storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _build_union_all_sql() -> str:
    """Build a UNION ALL SQL query that injects snapshot_date as a literal DATE per snapshot.

    Each snapshot gets `DATE 'YYYY-MM-DD' AS snapshot_date` injected.
    NO hive_partitioning — explicit per-snapshot SELECT avoids picking up legacy dirs.
    Row identity: (cage_code, snapshot_date) composite. No dedup on cage_code alone.
    """
    parts = []
    for snap_date, r2_path in PRE_V2_SNAPSHOTS:
        parts.append(
            f"SELECT *, DATE '{snap_date}' AS snapshot_date "
            f"FROM read_parquet('{r2_path}')"
        )
    return "\nUNION ALL\n".join(parts)


def dry_run(con) -> int:
    """Count rows and print schema preview without writing Lance dataset."""
    LOG.info("DRY RUN — counting rows across %d pre-v2 snapshots ...", len(PRE_V2_SNAPSHOTS))
    sql = _build_union_all_sql()
    total = con.execute(f"SELECT COUNT(*) FROM ({sql}) t").fetchone()[0]
    LOG.info("total rows (UNION ALL): %d", total)
    LOG.info("row floor (5%% slack): %d", ROW_FLOOR)
    if total < ROW_FLOOR:
        LOG.error("FAIL: row count %d < floor %d", total, ROW_FLOOR)
        return 1

    # Check snapshot_date distinct count and NULL count
    nd = con.execute(
        f"SELECT COUNT(DISTINCT snapshot_date) FROM ({sql}) t"
    ).fetchone()[0]
    nulls = con.execute(
        f"SELECT COUNT(*) FROM ({sql}) t WHERE snapshot_date IS NULL"
    ).fetchone()[0]
    dtype = con.execute(
        f"SELECT typeof(snapshot_date) FROM ({sql}) t LIMIT 1"
    ).fetchone()[0]
    LOG.info("snapshot_date dtype=%s, distinct_values=%d, nulls=%d", dtype, nd, nulls)
    if nulls > 0:
        LOG.error("FAIL: %d NULL snapshot_date values — materialization broken", nulls)
        return 1
    if nd != len(PRE_V2_SNAPSHOTS):
        LOG.error("FAIL: expected %d distinct snapshot_date values, got %d", len(PRE_V2_SNAPSHOTS), nd)
        return 1

    LOG.info("DRY RUN complete — OK. %d rows, %d snapshots.", total, len(PRE_V2_SNAPSHOTS))
    return 0


def apply(con) -> int:
    """Emit the longitudinal pre-v2 Lance dataset."""
    import lance

    LOG.info("=" * 60)
    LOG.info("emit: %s", DATASET_SLUG)
    LOG.info("output: %s", LANCE_URI)
    LOG.info("snapshots: %d (%s..%s)", len(PRE_V2_SNAPSHOTS),
             PRE_V2_SNAPSHOTS[0][0], PRE_V2_SNAPSHOTS[-1][0])

    sql = _build_union_all_sql()
    storage_options = _lance_storage_options()

    LOG.info("counting rows ...")
    total_parquet = con.execute(f"SELECT COUNT(*) FROM ({sql}) t").fetchone()[0]
    LOG.info("parquet total rows: %d", total_parquet)
    if total_parquet < ROW_FLOOR:
        LOG.error("FAIL: row count %d < floor %d — aborting before write.", total_parquet, ROW_FLOOR)
        return 1

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        LOG.info("streaming DuckDB → Arrow reader → Lance (mode=overwrite) ...")
        reader = con.from_query(f"SELECT * FROM ({sql}) t").to_arrow_reader(batch_size=100_000)
        ds = lance.write_dataset(
            reader,
            LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t0
        lance_rows = ds.count_rows()
        LOG.info(
            "wrote %d rows in %.1fs (version=%s)",
            lance_rows, write_dur, ds.version,
        )

        if lance_rows != total_parquet:
            LOG.error("FAIL: row mismatch parquet=%d lance=%d", total_parquet, lance_rows)
            return 1

        # BTREE on PK (cage_code)
        LOG.info("creating BTREE scalar index on %s ...", PK_COLUMN)
        t_idx = time.time()
        ds.create_scalar_index(PK_COLUMN, index_type="BTREE", replace=True)
        LOG.info("  PK index built in %.1fs", time.time() - t_idx)

        # BTREE on snapshot_date
        LOG.info("creating BTREE scalar index on snapshot_date ...")
        t_idx2 = time.time()
        ds.create_scalar_index("snapshot_date", index_type="BTREE", replace=True)
        LOG.info("  snapshot_date index built in %.1fs", time.time() - t_idx2)

        # Compact + cleanup
        LOG.info("optimize: compact_files + cleanup_old_versions ...")
        try:
            stats = ds.optimize.compact_files()
            LOG.info("  compact_files: %s", stats)
        except Exception as e:
            LOG.warning("  compact_files failed (non-fatal): %s", e)
        try:
            cleanup = ds.cleanup_old_versions(older_than=timedelta(days=7))
            LOG.info("  cleanup_old_versions: %s", cleanup)
        except Exception as e:
            LOG.warning("  cleanup_old_versions failed (non-fatal): %s", e)

    total_dur = time.time() - t0
    LOG.info("=" * 60)
    LOG.info(
        "DONE: %s | rows=%d | dur=%.1fs",
        DATASET_SLUG, lance_rows, total_dur,
    )
    return 0


def main() -> int:
    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            LOG.error("FAIL: %s not set in environment", var)
            return 64

    ap = argparse.ArgumentParser(description=f"Lance emit: {DATASET_SLUG}")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true", help="write Lance dataset")
    grp.add_argument("--dry-run", action="store_true", help="count rows only")
    args = ap.parse_args()

    con = _connect_duckdb_to_r2()
    if args.dry_run:
        return dry_run(con)
    return apply(con)


if __name__ == "__main__":
    raise SystemExit(main())

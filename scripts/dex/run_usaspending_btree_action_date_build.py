"""Build BTREE scalar index on one USAspending Lance dataset.

Called by modal/usaspending_btree_action_date_emit_app.py via subprocess.run.
NOT intended for direct invocation outside of Modal.

Usage (inside Modal container):
    python run_usaspending_btree_action_date_build.py --table {fpds|fabs|awards}

Tables:
    fpds   → s3://…/usaspending/transaction_fpds_lance  → BTREE on action_date
    fabs   → s3://…/usaspending/transaction_fabs_lance  → BTREE on action_date
    awards → s3://…/usaspending/awards_lance            → BTREE on date_signed

Idempotent via ``replace=True`` — re-running is safe (Lance skips if unchanged).
Wrapped in lance_commit_lock per C6.
LANCE_BYPASS_SPILLING=true must be set in env BEFORE calling create_scalar_index;
the Modal wrapper sets it in _ensure_tmpdir() before invoking this script.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("usaspending_btree_build")

_TABLE_CONFIG = {
    "fpds": {
        "uri": "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/transaction_fpds_lance",
        "column": "action_date",
        "dataset_slug": "usaspending_fpds_btree_action_date",
    },
    "fabs": {
        "uri": "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/transaction_fabs_lance",
        "column": "action_date",
        "dataset_slug": "usaspending_fabs_btree_action_date",
    },
    "awards": {
        "uri": "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/awards_lance",
        "column": "date_signed",
        "dataset_slug": "usaspending_awards_btree_date_signed",
    },
}


def _r2_storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
    }


def build(table: str) -> None:
    cfg = _TABLE_CONFIG[table]
    uri = cfg["uri"]
    column = cfg["column"]
    slug = cfg["dataset_slug"]

    # Guard: LANCE_BYPASS_SPILLING must be set before create_scalar_index.
    if os.environ.get("LANCE_BYPASS_SPILLING") != "true":
        logger.warning(
            "LANCE_BYPASS_SPILLING not set to 'true'; setting now. "
            "OOM risk on large sort operations."
        )
        os.environ["LANCE_BYPASS_SPILLING"] = "true"

    import lance
    from scripts._lib.lance_commit_lock import lance_commit_lock

    storage = _r2_storage_options()

    logger.info("opening Lance dataset table=%s uri=%s", table, uri)
    ds = lance.dataset(uri, storage_options=storage)
    row_count = ds.count_rows()
    logger.info("table=%s row_count=%d", table, row_count)

    # Pre-check: log existing indices.
    existing = ds.list_indices()
    logger.info("existing indices for table=%s: %s", table, existing)
    already_have = any(
        ix.get("columns") == [column] and ix.get("type") == "BTree"
        for ix in existing
    )
    if already_have:
        logger.info(
            "BTREE on column=%s already exists for table=%s — replace=True will refresh",
            column, table,
        )

    t0 = time.time()
    started_at = datetime.now(timezone.utc).isoformat()
    logger.info(
        "acquiring lance_commit_lock slug=%s and calling create_scalar_index "
        "table=%s column=%s at %s",
        slug, table, column, started_at,
    )

    with lance_commit_lock(slug):
        # Re-open inside the lock for freshness (per precedent pattern).
        ds = lance.dataset(uri, storage_options=storage)
        ds.create_scalar_index(column, index_type="BTREE", replace=True)

    duration_s = round(time.time() - t0, 1)

    # Verify: confirm index appears in list_indices().
    ds2 = lance.dataset(uri, storage_options=storage)
    indices_after = ds2.list_indices()
    confirmed = any(
        ix.get("columns") == [column] and ix.get("type") == "BTree"
        for ix in indices_after
    )

    logger.info(
        "OK — metrics: %s",
        {
            "table": table,
            "column": column,
            "row_count": row_count,
            "duration_s": duration_s,
            "btree_confirmed": confirmed,
            "indices_after": indices_after,
        },
    )

    if not confirmed:
        raise RuntimeError(
            f"BTREE index on {column} not confirmed in list_indices() after build for {table}. "
            f"indices_after={indices_after}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Build BTREE index on USAspending Lance dataset")
    parser.add_argument("--table", required=True, choices=list(_TABLE_CONFIG), help="Dataset to index")
    args = parser.parse_args()
    build(args.table)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Add BTREE scalar index on pdl_id to pdl.free_companies_lance.

Bridge lookups (`bridge.pdl_company_id` → `pdl.pdl_id`) were full scans of
8.8M rows. This adds the missing BTREE in addition to the pre-existing
`legal_name_normalized` BTREE. Idempotent: `replace=True` re-creates
without dropping siblings. One-shot.

Modeled on `build_bridge_ucc_pdl_lance.py:_write_bridge_lance()` (the
canonical index-creation pattern: TMPDIR=/tmp/lance + LANCE_BYPASS_SPILLING=true
+ wrapped in `lance_commit_lock`).

Usage:
    doppler run --project hq-all --config prd -- \\
        uv run --with pylance --with pyarrow --with "psycopg[binary]" python \\
        apps/data-engine-x/scripts/add_pdl_id_btree_index.py
"""
from __future__ import annotations

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
logger = logging.getLogger("add_pdl_id_btree_index")

PDL_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/pdl/free_companies_lance"
DATASET_SLUG = "pdl_free_companies_lance"
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
    import lance

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "DEX_DB_URL_DIRECT"):
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = TMP_DIR
    os.environ["LANCE_BYPASS_SPILLING"] = "true"

    storage_options = _lance_storage_options()
    logger.info("opening %s ...", PDL_LANCE_URI)

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        ds = lance.dataset(PDL_LANCE_URI, storage_options=storage_options)
        logger.info("dataset version=%s rows=%d", ds.version, ds.count_rows())
        existing = [i["name"] for i in ds.list_indices()]
        logger.info("existing indices: %s", existing)

        logger.info("creating BTREE on pdl_id (replace=True) ...")
        ds.create_scalar_index("pdl_id", index_type="BTREE", replace=True)
        logger.info("  BTREE on pdl_id created in %.1fs", time.time() - t0)

        post = [i["name"] for i in ds.list_indices()]
        logger.info("post-create indices: %s", post)

        try:
            ds.optimize.compact_files()
        except Exception as e:
            logger.warning("compact_files failed (non-fatal): %s", e)
        try:
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as e:
            logger.warning("cleanup_old_versions failed (non-fatal): %s", e)

    logger.info("OK — total duration=%.1fs", time.time() - t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Augment `sos/ca_agents_lance` with a normalized address column.

Adds:
  - `physical_address_base_normalized` — from
    `physical_address1+physical_address2+physical_address3` via
    `_lib.address_normalize.normalize_address_street` (base form,
    unit-stripped).

Why:
  Agents-for-service-of-process carry their own physical address (typically
  the law firm / corporate-services-of-process firm). Any joins keyed on
  agent address (CSC × sos_agent, etc.) need the same pre-baked join key.

  Mirrors PR #786 (SAM) / #783 (Overture) / #782 (SBA) pre-bake pattern.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with pylance --with pyarrow python \\
    apps/data-engine-x/scripts/augment_sos_ca_agents_address_normalize_lance.py --apply
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from scripts._lib.address_normalize import (  # noqa: E402
    __version__ as ADDR_NORMALIZER_VERSION,
    join_address_lines,
    normalize_address_street,
)
from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("augment_sos_ca_agents_address_normalize_lance")

LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sos/ca_agents_lance"
DATASET_SLUG = "sos_ca_agents_lance"
NEW_COLS = ("physical_address_base_normalized",)
READ_COLS = ["physical_address1", "physical_address2", "physical_address3"]


def _lance_storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _batch_udf(batch):
    import pyarrow as pa

    a1 = batch.column("physical_address1").to_pylist()
    a2 = batch.column("physical_address2").to_pylist()
    a3 = batch.column("physical_address3").to_pylist()

    out = [normalize_address_street(join_address_lines(x, y, z)) for x, y, z in zip(a1, a2, a3)]

    return pa.RecordBatch.from_arrays(
        [pa.array(out, type=pa.string())],
        names=list(NEW_COLS),
    )


def _emit(dry_run: bool) -> int:
    import lance
    import pyarrow as pa
    import pyarrow.compute as pc

    storage_options = _lance_storage_options()
    logger.info("opening %s ...", LANCE_URI)
    ds = lance.dataset(LANCE_URI, storage_options=storage_options)
    rows = ds.count_rows()
    logger.info("rows: %d  version: %d", rows, ds.version)

    existing_cols = {f.name for f in ds.schema}
    already_present = [c for c in NEW_COLS if c in existing_cols]
    if already_present:
        logger.error("FAIL: column(s) already present: %s", already_present)
        return 1

    missing_read_cols = [c for c in READ_COLS if c not in existing_cols]
    if missing_read_cols:
        logger.error("FAIL: required source col(s) missing: %s", missing_read_cols)
        return 1

    if dry_run:
        sample = ds.scanner(columns=READ_COLS, limit=10).to_table()
        logger.info("DRY RUN — sample normalization (10 rows):")
        for r in sample.to_pylist():
            joined = join_address_lines(r["physical_address1"], r["physical_address2"], r["physical_address3"])
            norm = normalize_address_street(joined)
            logger.info("  %r + %r + %r -> %r -> %r",
                        r["physical_address1"], r["physical_address2"], r["physical_address3"], joined, norm)
        logger.info("DRY RUN OK.")
        return 0

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        logger.info("augmenting via add_columns (normalizer v%s) ...", ADDR_NORMALIZER_VERSION)
        from lance import batch_udf

        @batch_udf(
            output_schema=pa.schema([pa.field("physical_address_base_normalized", pa.string())])
        )
        def udf(batch):
            return _batch_udf(batch)

        ds.add_columns(udf, read_columns=READ_COLS, batch_size=50_000)
        dur = time.time() - t0
        ds = lance.dataset(LANCE_URI, storage_options=storage_options)
        logger.info("add_columns completed in %.1fs (new v=%d, rows=%d)", dur, ds.version, ds.count_rows())

        cov_tbl = ds.to_table(columns=["physical_address_base_normalized"])
        n = cov_tbl.num_rows
        nn = pc.sum(pc.is_valid(cov_tbl["physical_address_base_normalized"])).as_py()
        logger.info("coverage: physical_address_base_normalized=%d/%d (%.1f%%)", nn, n, 100.0 * nn / max(1, n))

        try:
            ds.optimize.compact_files()
            logger.info("compact_files done")
        except Exception as e:
            logger.warning("compact_files failed (non-fatal): %s", e)

    logger.info("OK — duration=%.1fs", time.time() - t0)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true")
    grp.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")

    return _emit(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())

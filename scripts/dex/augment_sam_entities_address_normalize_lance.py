#!/usr/bin/env python3
"""Augment `sam_gov/entities_lance` with two normalized address columns.

Adds:
  - `physical_address_base_normalized` — `_lib.address_normalize.normalize_address_street`
    applied to the joined `physical_address_line_1 + physical_address_line_2`
    (base form, unit-stripped).
  - `mailing_address_base_normalized`  — same normalizer applied to
    `mailing_address_line_1 + mailing_address_line_2`.

Why:
  Every SAM-keyed address bridge today re-normalizes 884K SAM rows via the
  Python UDF path at build time. Baking the normalized form once into the
  source dataset removes that cost for every downstream address-keyed bridge
  (sam_overture_address future re-builds, sam_ppp_address physical+mailing,
  sam_sba_address physical+mailing, sam_fmcsa_address, sam_ncua_address, etc).
  Mirrors the pre-bake pattern already applied to `overture/us_places_lance`,
  `sba/borrowers_lance`, and `sba/ppp_borrowers_lance`.

Uses Lance `add_columns` with a BatchUDF — appends columns in place without
rewriting existing fragments. Idempotent: running twice is a no-op (the
second `add_columns` call will fail on duplicate column names; the script
checks first and exits cleanly).

This is a ONE-SHOT augment for the current SAM monthly snapshot. The
upstream `emit_sam_entities_lance.py` framework wrapper does not currently
re-derive these columns on its own — when the next monthly snapshot lands
and the framework re-emits, these columns will be absent until this script
is re-run. The clean long-term fix is to fold this normalization into the
framework's projection step; that's a separate change.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with pylance --with pyarrow python \\
    apps/data-engine-x/scripts/augment_sam_entities_address_normalize_lance.py --apply

  doppler run --project hq-all --config prd -- \\
    uv run --with pylance --with pyarrow python \\
    apps/data-engine-x/scripts/augment_sam_entities_address_normalize_lance.py --dry-run
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
    join_sam_line_1_2,
    normalize_address_street,
)
from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("augment_sam_entities_address_normalize_lance")

LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/entities_lance"
DATASET_SLUG = "sam_entities_lance"
NEW_COLS = ("physical_address_base_normalized", "mailing_address_base_normalized")
READ_COLS = [
    "physical_address_line_1",
    "physical_address_line_2",
    "mailing_address_line_1",
    "mailing_address_line_2",
]


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
    """BatchUDF for lance.add_columns.

    Receives a pyarrow RecordBatch containing the columns in `READ_COLS`,
    returns a RecordBatch with the two derived columns in `NEW_COLS`.
    """
    import pyarrow as pa

    p1 = batch.column("physical_address_line_1").to_pylist()
    p2 = batch.column("physical_address_line_2").to_pylist()
    m1 = batch.column("mailing_address_line_1").to_pylist()
    m2 = batch.column("mailing_address_line_2").to_pylist()

    phys_out = [normalize_address_street(join_sam_line_1_2(a, b)) for a, b in zip(p1, p2)]
    mail_out = [normalize_address_street(join_sam_line_1_2(a, b)) for a, b in zip(m1, m2)]

    return pa.RecordBatch.from_arrays(
        [
            pa.array(phys_out, type=pa.string()),
            pa.array(mail_out, type=pa.string()),
        ],
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
        logger.error(
            "FAIL: column(s) already present in dataset — refusing to overwrite: %s",
            already_present,
        )
        logger.error(
            "If a re-bake is intended, drop the existing columns first or use mode='overwrite'."
        )
        return 1

    missing_read_cols = [c for c in READ_COLS if c not in existing_cols]
    if missing_read_cols:
        logger.error(
            "FAIL: required source column(s) missing from dataset: %s",
            missing_read_cols,
        )
        return 1

    if dry_run:
        # Sanity: probe normalizer on a small sample.
        sample = ds.scanner(columns=READ_COLS, limit=10).to_table()
        sample_rows = sample.to_pylist()
        logger.info("DRY RUN — sample normalization (10 rows):")
        for r in sample_rows:
            phys = normalize_address_street(
                join_sam_line_1_2(r["physical_address_line_1"], r["physical_address_line_2"])
            )
            mail = normalize_address_street(
                join_sam_line_1_2(r["mailing_address_line_1"], r["mailing_address_line_2"])
            )
            logger.info(
                "  P: %r + %r -> %r",
                r["physical_address_line_1"],
                r["physical_address_line_2"],
                phys,
            )
            logger.info(
                "  M: %r + %r -> %r",
                r["mailing_address_line_1"],
                r["mailing_address_line_2"],
                mail,
            )
        logger.info("DRY RUN OK — no Lance writes.")
        return 0

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        logger.info(
            "augmenting Lance dataset via add_columns (UDF, normalizer v%s) ...",
            ADDR_NORMALIZER_VERSION,
        )
        # Lance add_columns with a Python BatchUDF.
        from lance import batch_udf

        @batch_udf(
            output_schema=pa.schema(
                [
                    pa.field("physical_address_base_normalized", pa.string()),
                    pa.field("mailing_address_base_normalized", pa.string()),
                ]
            )
        )
        def udf(batch):
            return _batch_udf(batch)

        ds.add_columns(udf, read_columns=READ_COLS, batch_size=50_000)
        dur = time.time() - t0
        # Re-open to refresh schema after augment.
        ds = lance.dataset(LANCE_URI, storage_options=storage_options)
        logger.info(
            "add_columns completed in %.1fs (new version=%d, rows=%d)",
            dur,
            ds.version,
            ds.count_rows(),
        )

        # Coverage report
        cov_tbl = ds.to_table(
            columns=[
                "physical_address_base_normalized",
                "mailing_address_base_normalized",
            ]
        )
        n = cov_tbl.num_rows
        p_nn = pc.sum(pc.is_valid(cov_tbl["physical_address_base_normalized"])).as_py()
        m_nn = pc.sum(pc.is_valid(cov_tbl["mailing_address_base_normalized"])).as_py()
        logger.info(
            "coverage: physical=%d/%d (%.1f%%)  mailing=%d/%d (%.1f%%)",
            p_nn, n, 100.0 * p_nn / max(1, n),
            m_nn, n, 100.0 * m_nn / max(1, n),
        )

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

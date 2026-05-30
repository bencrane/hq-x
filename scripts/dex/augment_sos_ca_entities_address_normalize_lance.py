#!/usr/bin/env python3
"""Augment `sos/ca_entities_lance` with three normalized address columns.

Adds:
  - `principal_address_base_normalized`        — from principal_address+principal_address2
  - `principal_address_in_ca_base_normalized`  — from principal_address_in_ca+principal_address2_in_ca
  - `mailing_address_base_normalized`          — from mailing_address+mailing_address2+mailing_address3

All produced by `_lib.address_normalize.normalize_address_street` (base form,
unit-stripped).

Why:
  CA SoS exposes THREE address roles per entity: principal (registered HQ),
  principal-in-CA (registered CA service address — typically the agent for
  service of process), and mailing. Any bridge keyed on physical address
  (ucc_ca_debtor × sos_ca_owner address axis, sam × sos_ca_owner address
  axis, sos_ca × overture address axis) needs the same normalized join key
  available without paying the Python UDF cost on 9.39M SoS rows each time.

  Mirrors the pre-bake pattern already applied to `sam_gov/entities_lance`
  (PR #786, two address types: physical+mailing), `overture/us_places_lance`
  (PR #783), and `sba/borrowers_lance` / `sba/ppp_borrowers_lance` (PR #782).

Uses Lance `add_columns` with a BatchUDF — appends columns in place without
rewriting existing fragments. Idempotent on duplicate-column check.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with pylance --with pyarrow python \\
    apps/data-engine-x/scripts/augment_sos_ca_entities_address_normalize_lance.py --apply
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
logger = logging.getLogger("augment_sos_ca_entities_address_normalize_lance")

LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sos/ca_entities_lance"
DATASET_SLUG = "sos_ca_entities_lance"
NEW_COLS = (
    "principal_address_base_normalized",
    "principal_address_in_ca_base_normalized",
    "mailing_address_base_normalized",
)
READ_COLS = [
    "principal_address",
    "principal_address2",
    "principal_address_in_ca",
    "principal_address2_in_ca",
    "mailing_address",
    "mailing_address2",
    "mailing_address3",
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
    import pyarrow as pa

    p1 = batch.column("principal_address").to_pylist()
    p2 = batch.column("principal_address2").to_pylist()
    pc1 = batch.column("principal_address_in_ca").to_pylist()
    pc2 = batch.column("principal_address2_in_ca").to_pylist()
    m1 = batch.column("mailing_address").to_pylist()
    m2 = batch.column("mailing_address2").to_pylist()
    m3 = batch.column("mailing_address3").to_pylist()

    principal = [normalize_address_street(join_address_lines(a, b)) for a, b in zip(p1, p2)]
    principal_ca = [normalize_address_street(join_address_lines(a, b)) for a, b in zip(pc1, pc2)]
    mailing = [normalize_address_street(join_address_lines(a, b, c)) for a, b, c in zip(m1, m2, m3)]

    return pa.RecordBatch.from_arrays(
        [
            pa.array(principal, type=pa.string()),
            pa.array(principal_ca, type=pa.string()),
            pa.array(mailing, type=pa.string()),
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
        return 1

    missing_read_cols = [c for c in READ_COLS if c not in existing_cols]
    if missing_read_cols:
        logger.error(
            "FAIL: required source column(s) missing from dataset: %s",
            missing_read_cols,
        )
        return 1

    if dry_run:
        sample = ds.scanner(columns=READ_COLS, limit=10).to_table()
        logger.info("DRY RUN — sample normalization (10 rows):")
        for r in sample.to_pylist():
            p = normalize_address_street(join_address_lines(r["principal_address"], r["principal_address2"]))
            pc_ = normalize_address_street(join_address_lines(r["principal_address_in_ca"], r["principal_address2_in_ca"]))
            m = normalize_address_street(join_address_lines(r["mailing_address"], r["mailing_address2"], r["mailing_address3"]))
            logger.info("  P: %r/%r -> %r", r["principal_address"], r["principal_address2"], p)
            logger.info("  PCA: %r/%r -> %r", r["principal_address_in_ca"], r["principal_address2_in_ca"], pc_)
            logger.info("  M: %r/%r/%r -> %r", r["mailing_address"], r["mailing_address2"], r["mailing_address3"], m)
        logger.info("DRY RUN OK — no Lance writes.")
        return 0

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        logger.info(
            "augmenting Lance dataset via add_columns (UDF, normalizer v%s) ...",
            ADDR_NORMALIZER_VERSION,
        )
        from lance import batch_udf

        @batch_udf(
            output_schema=pa.schema(
                [
                    pa.field("principal_address_base_normalized", pa.string()),
                    pa.field("principal_address_in_ca_base_normalized", pa.string()),
                    pa.field("mailing_address_base_normalized", pa.string()),
                ]
            )
        )
        def udf(batch):
            return _batch_udf(batch)

        ds.add_columns(udf, read_columns=READ_COLS, batch_size=50_000)
        dur = time.time() - t0
        ds = lance.dataset(LANCE_URI, storage_options=storage_options)
        logger.info(
            "add_columns completed in %.1fs (new version=%d, rows=%d)",
            dur, ds.version, ds.count_rows(),
        )

        cov_tbl = ds.to_table(columns=list(NEW_COLS))
        n = cov_tbl.num_rows
        for c in NEW_COLS:
            nn = pc.sum(pc.is_valid(cov_tbl[c])).as_py()
            logger.info("coverage: %s=%d/%d (%.1f%%)", c, nn, n, 100.0 * nn / max(1, n))

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

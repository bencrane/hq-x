#!/usr/bin/env python3
"""s4 — Emit UCC CO collateral as a Lance dataset on R2.

Reads:
  - s3://dex-raw-landing-zone/ucc/state=CO/stream=collateral/snapshot=2026-05-08/data.parquet
  - s3://dex-raw-landing-zone/polaris-warehouse/ucc_co/filings_lance/  (written by s1)

Writes: s3://dex-raw-landing-zone/polaris-warehouse/ucc_co/collateral_lance/

DEPENDENCY: s4 MUST run AFTER s1 (emit_ucc_co_filings_lance.py) has completed and
ucc_co/filings_lance exists on R2. The UCC1_NUM column is populated via a LEFT JOIN
on fileid → filings_lance.UCC1_NUM at emit time (audit decision: option (a), emit-time
enrichment vs downstream join). Orphan rate is 0.0% per reviewer's probe.

Output schema (17 columns: 16 source columns preserved verbatim + 1 derived UCC1_NUM):
  UCC1_NUM                         -- derived from JOIN: filings.fileid (CA-parity primary key)
  collateralid                     -- preserved
  fileid                           -- preserved (natural CO key; both indexes for downstream)
  actiontypecode                   -- preserved
  actiontype                       -- preserved
  recordstatuscode                 -- preserved
  recordstatus                     -- preserved
  farmproductflag                  -- preserved (CO-specific: agricultural-skew indicator)
  farmproductallyears              -- preserved
  collateraldescription            -- preserved
  additionalcollateraldescription  -- preserved
  farmproductstartyear             -- preserved
  farmproductendyear               -- preserved
  actionreferenceid                -- preserved
  countyid                         -- preserved
  county                           -- preserved
  collateral_description_normalized -- preserved

BTREE indexes on BOTH UCC1_NUM AND fileid (per directive surface spec).
Commit-lock via Postgres advisory lock (lance_commit_lock constraint).

MIN_ROW_FLOOR = 1_600_000 — HARD FAIL gate fires BEFORE lance.write_dataset().
Source has 1,682,948 rows; floor at ~95.1%.

Usage:
    doppler run --project hq-all --config prd -- \\
        uv run --quiet python3 apps/data-engine-x/scripts/emit_ucc_co_collateral_lance.py \\
            --apply
"""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
LOG = logging.getLogger(__name__)

PARQUET_URI = (
    "r2://dex-raw-landing-zone/ucc/state=CO/stream=collateral/snapshot=2026-05-08/data.parquet"
)
FILINGS_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/ucc_co/filings_lance/"
)
LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/ucc_co/collateral_lance/"
DATASET_SLUG = "ucc_co_collateral_lance"

# HARD FAIL gate — fires BEFORE lance.write_dataset()
MIN_ROW_FLOOR = 1_600_000


def _r2_account_id() -> str:
    ep = os.environ["R2_ENDPOINT"]
    return ep.split("//")[-1].split(".")[0]


def _connect_duckdb():
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


def _storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _load_filings_lance_as_pandas(storage_options: dict):
    """Load ucc_co/filings_lance UCC1_NUM column into a pandas DataFrame for DuckDB join.

    Uses PyArrow Lance read to materialize only the UCC1_NUM column (~2.5M rows × 1 col,
    ~40 MB in memory — well within budget). Registered into DuckDB so the collateral
    LEFT JOIN can run entirely in-process.
    """
    import lance

    LOG.info("Loading filings_lance from %s (UCC1_NUM column only) ...", FILINGS_LANCE_URI)
    ds = lance.dataset(FILINGS_LANCE_URI, storage_options=storage_options)
    # Read only UCC1_NUM — the rest of filings schema is not needed for this join
    tbl = ds.to_table(columns=["UCC1_NUM"])
    LOG.info("Loaded %d filings rows", len(tbl))
    return tbl


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Emit UCC CO collateral Lance (s4)")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true")
    grp.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            LOG.error("FAIL: %s not set", var)
            return 64

    con = _connect_duckdb()

    # Count source rows — HARD FAIL gate fires here, BEFORE any write
    total = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('{PARQUET_URI}')"
    ).fetchone()[0]
    LOG.info("Source rows: %d (floor: %d)", total, MIN_ROW_FLOOR)

    if total < MIN_ROW_FLOOR:
        LOG.error(
            "FAIL: source row count %d is below MIN_ROW_FLOOR %d — aborting (no Lance write)",
            total, MIN_ROW_FLOOR,
        )
        return 1

    if args.dry_run:
        LOG.info("DRY RUN — exiting without writing Lance dataset")
        return 0

    storage_options = _storage_options()

    # Load filings_lance (s1 output) and register with DuckDB for the LEFT JOIN
    filings_arrow = _load_filings_lance_as_pandas(storage_options)
    con.register("filings_lance", filings_arrow)

    import lance

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        # Preserve all 16 source columns + derive UCC1_NUM via LEFT JOIN to filings_lance.
        # Orphan rate is 0.0% per reviewer probe: LEFT JOIN UCC1_NUM is effectively never NULL.
        reader = con.execute(
            f"""
            WITH collateral AS (
                SELECT
                    collateralid,
                    fileid,
                    actiontypecode,
                    actiontype,
                    recordstatuscode,
                    recordstatus,
                    farmproductflag,
                    farmproductallyears,
                    collateraldescription,
                    additionalcollateraldescription,
                    farmproductstartyear,
                    farmproductendyear,
                    actionreferenceid,
                    countyid,
                    county,
                    collateral_description_normalized
                FROM read_parquet('{PARQUET_URI}')
            )
            SELECT
                f.UCC1_NUM,
                c.collateralid,
                c.fileid,
                c.actiontypecode,
                c.actiontype,
                c.recordstatuscode,
                c.recordstatus,
                c.farmproductflag,
                c.farmproductallyears,
                c.collateraldescription,
                c.additionalcollateraldescription,
                c.farmproductstartyear,
                c.farmproductendyear,
                c.actionreferenceid,
                c.countyid,
                c.county,
                c.collateral_description_normalized
            FROM collateral c
            LEFT JOIN filings_lance f ON c.fileid = f.UCC1_NUM
            """
        ).to_arrow_reader(batch_size=100_000)

        LOG.info("Writing Lance dataset (mode=overwrite) to %s ...", LANCE_URI)
        ds = lance.write_dataset(
            reader,
            LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t0
        lance_count = ds.count_rows()
        LOG.info("Wrote %d rows in %.1fs (version=%s)", lance_count, write_dur, ds.version)

        # Two BTREE indexes required per directive: UCC1_NUM AND fileid
        LOG.info("Building BTREE index on UCC1_NUM ...")
        try:
            os.environ.setdefault("LANCE_INDEX_CACHE_SIZE", "1g")
            ds.create_scalar_index("UCC1_NUM", index_type="BTREE", replace=True)
            LOG.info("BTREE index on UCC1_NUM: OK")
        except Exception as e:
            LOG.error(
                "BTREE index on UCC1_NUM FAILED: %s — HALT with blocked-btree-memory if retry fails",
                e,
            )
            raise

        LOG.info("Building BTREE index on fileid ...")
        try:
            ds.create_scalar_index("fileid", index_type="BTREE", replace=True)
            LOG.info("BTREE index on fileid: OK")
        except Exception as e:
            LOG.error(
                "BTREE index on fileid FAILED: %s — HALT with blocked-btree-memory if retry fails",
                e,
            )
            raise

        try:
            ds.optimize.compact_files()
            ds.cleanup_old_versions()
        except Exception as e:
            LOG.warning("Optimize failed (non-fatal): %s", e)

    LOG.info("s4 complete: %d rows, %.1fs", lance_count, time.time() - t0)
    if lance_count != total:
        LOG.warning("Row count mismatch: source=%d lance=%d", total, lance_count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

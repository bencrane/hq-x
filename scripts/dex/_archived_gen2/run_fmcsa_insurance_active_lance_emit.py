#!/usr/bin/env python3
"""Re-emit FMCSA insurance_active (actpendinsur_essentials) as a Lance dataset on R2.

Mirrors the Wave 1 canary ``run_fmcsa_carrier_essentials_lance_emit.py`` exactly.

Reads the daily derived Parquet at
  ``r2://dex-raw-landing-zone/fmcsa-derived/actpendinsur_essentials/snapshot=YYYY-MM-DD/data.parquet``
(produced by ``data-engine-x-fmcsa-factory-daily`` at 06:00 UTC) and overwrites
the Lance dataset at
  ``s3://dex-raw-landing-zone/polaris-warehouse/fmcsa/insurance_active_lance/``
with the latest snapshot's rows, preserving schema verbatim.

BTREE scalar index on ``dot_number`` for per-DOT random access (<100ms warm).

Usage
-----
    # Dry run:
    doppler run --project hq-all --config prd -- \\
        uv run --with pylance python3 \\
        apps/data-engine-x/scripts/run_fmcsa_insurance_active_lance_emit.py --dry-run

    # Apply:
    doppler run --project hq-all --config prd -- \\
        uv run --with pylance python3 \\
        apps/data-engine-x/scripts/run_fmcsa_insurance_active_lance_emit.py --apply

Required env (from Doppler):
    R2_ENDPOINT
    R2_ACCESS_KEY_ID
    R2_SECRET_ACCESS_KEY
    DEX_DB_URL_DIRECT       (for the commit lock)
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
LOG = logging.getLogger("fmcsa-insurance-active-lance-emit")

R2_BUCKET = "dex-raw-landing-zone"
INPUT_PREFIX = "fmcsa-derived/actpendinsur_essentials"
LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/fmcsa/insurance_active_lance"
)
DATASET_SLUG = "fmcsa_insurance_active_lance"  # commit-lock key
TMP_DIR = "/tmp/lance"


def _r2_account_id() -> str:
    ep = os.environ["R2_ENDPOINT"]
    return ep.split("//")[-1].split(".")[0]


def _ensure_tmpdir() -> None:
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = TMP_DIR


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


def _detect_latest_snapshot(con) -> str:
    rows = con.execute(
        f"SELECT file FROM glob('r2://{R2_BUCKET}/{INPUT_PREFIX}/snapshot=*/data.parquet')"
    ).fetchall()
    if not rows:
        raise SystemExit(
            f"FAIL: no Parquet files found under r2://{R2_BUCKET}/{INPUT_PREFIX}/"
        )
    snapshots: set[str] = set()
    for (path,) in rows:
        for p in path.split("/"):
            if p.startswith("snapshot=") and len(p) == len("snapshot=YYYY-MM-DD"):
                snapshots.add(p[len("snapshot="):])
    if not snapshots:
        raise SystemExit("FAIL: no snapshot=YYYY-MM-DD dirs detected")
    return max(snapshots)


def _lance_storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _scan_parquet_to_arrow(con, parquet_uri: str):
    rel = con.from_query(f"SELECT * FROM read_parquet('{parquet_uri}')")
    return rel.to_arrow_reader(batch_size=100_000)


def emit_lance(snapshot: str) -> dict:
    """Read the Parquet snapshot, write to Lance, optimize. Returns metrics."""
    import lance

    parquet_uri = (
        f"r2://{R2_BUCKET}/{INPUT_PREFIX}/snapshot={snapshot}/data.parquet"
    )
    LOG.info("input:  %s", parquet_uri)
    LOG.info("output: %s", LANCE_URI)

    con = _connect_duckdb_to_r2()

    parquet_count = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('{parquet_uri}')"
    ).fetchone()[0]
    LOG.info("parquet rows: %d", parquet_count)

    storage_options = _lance_storage_options()

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        reader = _scan_parquet_to_arrow(con, parquet_uri)
        LOG.info("writing Lance dataset (mode=overwrite) ...")
        ds = lance.write_dataset(
            reader,
            LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t0
        lance_count = ds.count_rows()
        LOG.info(
            "wrote %d rows to Lance in %.1fs (version=%s)",
            lance_count, write_dur, ds.version,
        )

        os.environ["LANCE_BYPASS_SPILLING"] = "true"
        t_idx = time.time()
        LOG.info("creating BTREE scalar index on dot_number ...")
        try:
            ds.create_scalar_index("dot_number", index_type="BTREE", replace=True)
            idx_dur = time.time() - t_idx
            LOG.info("  index built in %.1fs", idx_dur)
        except Exception as e:
            LOG.warning("  BTREE index build failed (non-fatal): %s", e)
            idx_dur = None

        t1 = time.time()
        LOG.info("optimize: cleanup_older_than=7d ...")
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
        opt_dur = time.time() - t1
        LOG.info("optimize done in %.1fs", opt_dur)

    return {
        "snapshot": snapshot,
        "parquet_rows": parquet_count,
        "lance_rows": lance_count,
        "lance_version": ds.version,
        "write_seconds": round(write_dur, 1),
        "index_seconds": round(idx_dur, 1) if idx_dur is not None else None,
        "optimize_seconds": round(opt_dur, 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true", help="write Lance dataset")
    grp.add_argument("--dry-run", action="store_true", help="counts only")
    ap.add_argument(
        "--snapshot",
        default=None,
        help="YYYY-MM-DD snapshot under input prefix (default: latest)",
    )
    args = ap.parse_args()

    for var in (
        "R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
    ):
        if not os.environ.get(var):
            LOG.error("FAIL: %s not set in environment", var)
            return 64

    _ensure_tmpdir()

    if args.dry_run:
        con = _connect_duckdb_to_r2()
        snapshot = args.snapshot or _detect_latest_snapshot(con)
        parquet_uri = (
            f"r2://{R2_BUCKET}/{INPUT_PREFIX}/snapshot={snapshot}/data.parquet"
        )
        total = con.execute(
            f"SELECT COUNT(*) FROM read_parquet('{parquet_uri}')"
        ).fetchone()[0]
        LOG.info("DRY RUN — snapshot=%s rows=%d", snapshot, total)
        return 0

    con = _connect_duckdb_to_r2()
    snapshot = args.snapshot or _detect_latest_snapshot(con)
    con.close()

    metrics = emit_lance(snapshot)
    LOG.info("OK — metrics: %s", metrics)
    if metrics["parquet_rows"] != metrics["lance_rows"]:
        LOG.error(
            "FAIL: row count mismatch parquet=%d lance=%d",
            metrics["parquet_rows"], metrics["lance_rows"],
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

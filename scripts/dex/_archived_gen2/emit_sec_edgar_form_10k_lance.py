#!/usr/bin/env python3
"""Lance-emit: SEC EDGAR Form 10-K — union across 7 streams.

Source: ``s3://dex-raw-landing-zone/sec-edgar/form-10k/year={YYYY}/{stream}/data.parquet``
written by ``scripts/run_sec_edgar_form_10k_r2_ingest.py`` (one parquet per
``(year, stream)`` pair across 7 streams: filings, officers_directors,
executive_compensation, security_ownership, properties, legal_proceedings,
risk_factors).

Output: a SINGLE Lance dataset at
``s3://dex-raw-landing-zone/polaris-warehouse/sec_edgar/form_10k_lance/``
that unions all 7 stream parquets via DuckDB ``union_by_name=true`` so the
heterogeneous typed columns from each stream coexist (NULLs fill missing
columns). The ``stream`` discriminator column is preserved from the Hive
partition; ``form_10k_year`` is already an int16 column inside each parquet
so no transform needed.

BTREE index on ``accession_number`` enables per-filing lookups (the most
common downstream query: "find the executive_compensation rows for filing
0001193125-24-123456").

Row floor: ≥ 50,000 (per directive §"Volume floors"). Backfill-dependent —
if R2 prefix has < 50K rows at emit time, the script exits 1 so the cycle
report can record `partial`.

Usage:

  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow python \\
    apps/data-engine-x/scripts/emit_sec_edgar_form_10k_lance.py --apply

  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow python \\
    apps/data-engine-x/scripts/emit_sec_edgar_form_10k_lance.py --dry-run

See directive ``~/Desktop/hq/directives/2026-05-12-sec-10k-activation.md``.
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
logger = logging.getLogger("emit_sec_edgar_form_10k_lance")

R2_BUCKET = "dex-raw-landing-zone"
PARQUET_INPUT_PREFIX = "sec-edgar/form-10k"  # year=YYYY/{stream}/data.parquet
LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/sec_edgar/form_10k_lance/"
)
DATASET_SLUG = "sec_edgar_form_10k_lance"
TMP_DIR = "/tmp/lance"

# Row floor per directive §"Volume floors".
ROW_FLOOR = 50_000

STREAMS: tuple[str, ...] = (
    "filings",
    "officers_directors",
    "executive_compensation",
    "security_ownership",
    "properties",
    "legal_proceedings",
    "risk_factors",
)


def _r2_account_id() -> str:
    ep = os.environ["R2_ENDPOINT"]
    return ep.split("//")[-1].split(".")[0]


def _lance_storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


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


def _stream_glob(stream: str) -> str:
    return (
        f"r2://{R2_BUCKET}/{PARQUET_INPUT_PREFIX}/year=*/"
        f"{stream}/data.parquet"
    )


def _count_per_stream(con) -> dict[str, int]:
    counts: dict[str, int] = {}
    for stream in STREAMS:
        glob = _stream_glob(stream)
        try:
            n = con.execute(
                f"SELECT COUNT(*) FROM read_parquet('{glob}', hive_partitioning=1)"
            ).fetchone()[0]
        except Exception as exc:  # noqa: BLE001
            # No parquet found for this stream → 0 (expected when backfill
            # hasn't covered every stream yet).
            logger.info("  stream=%s: not yet present (%s)", stream, type(exc).__name__)
            n = 0
        counts[stream] = int(n)
        logger.info("  stream=%s: %d rows", stream, n)
    return counts


def _emit(dry_run: bool) -> int:
    import lance

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = TMP_DIR

    logger.info("=" * 60)
    logger.info("emit_sec_edgar_form_10k_lance")
    logger.info("input:  r2://%s/%s/year=*/{stream}/data.parquet",
                R2_BUCKET, PARQUET_INPUT_PREFIX)
    logger.info("output: %s", LANCE_URI)

    con = _connect_duckdb_to_r2()

    logger.info("counting per-stream rows...")
    per_stream = _count_per_stream(con)
    total = sum(per_stream.values())
    logger.info("TOTAL across %d streams: %d (floor=%d)",
                len(STREAMS), total, ROW_FLOOR)

    if total < ROW_FLOOR:
        if dry_run:
            logger.warning(
                "DRY RUN — total %d < floor %d. Backfill is incomplete; "
                "rerun after R2 prefix sec-edgar/form-10k/ holds ≥50K rows.",
                total, ROW_FLOOR,
            )
            return 1
        logger.error("FAIL: total=%d < floor=%d. Refusing to emit Lance "
                     "until backfill clears the floor.", total, ROW_FLOOR)
        return 1

    if dry_run:
        logger.info("DRY RUN — total %d >= floor %d. Would emit Lance.",
                    total, ROW_FLOOR)
        return 0

    # Build the union ARROW reader. union_by_name=true lets streams have
    # divergent schemas; NULLs fill missing columns. Hive partitioning
    # surfaces year + stream as columns.
    union_globs = ",".join(f"'{_stream_glob(s)}'" for s in STREAMS)
    logger.info("creating Arrow reader for union of %d streams ...", len(STREAMS))
    reader = con.from_query(
        f"""
        SELECT *
        FROM read_parquet([{union_globs}],
                          hive_partitioning=1,
                          union_by_name=true)
        """
    ).to_arrow_reader(batch_size=100_000)

    storage_options = _lance_storage_options()

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        logger.info("writing Lance dataset (mode=overwrite) ...")
        ds = lance.write_dataset(
            reader,
            LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t0
        lance_count = ds.count_rows()
        logger.info(
            "wrote %d rows in %.1fs (version=%s)",
            lance_count, write_dur, ds.version,
        )

        if lance_count < ROW_FLOOR:
            logger.error("FAIL: post-write lance_count=%d < floor=%d",
                         lance_count, ROW_FLOOR)
            return 1

        os.environ["LANCE_BYPASS_SPILLING"] = "true"
        logger.info("creating BTREE index on accession_number ...")
        t_idx = time.time()
        try:
            ds.create_scalar_index(
                "accession_number", index_type="BTREE", replace=True,
            )
            logger.info("  BTREE built in %.1fs", time.time() - t_idx)
        except Exception as exc:  # noqa: BLE001
            # Index is best-effort on a union dataset (accession_number may
            # be NULL for some rows — though unlikely given parser shape).
            logger.warning("BTREE index FAILED (non-fatal): %s", exc)

        try:
            stats = ds.optimize.compact_files()
            logger.info("compact_files: %s", stats)
        except Exception as exc:  # noqa: BLE001
            logger.warning("compact_files failed (non-fatal): %s", exc)
        try:
            cleanup = ds.cleanup_old_versions(older_than=timedelta(days=7))
            logger.info("cleanup_old_versions: %s", cleanup)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cleanup_old_versions failed (non-fatal): %s", exc)

    logger.info("=" * 60)
    logger.info("OK — lance rows written: %d (floor=%d)", lance_count, ROW_FLOOR)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true")
    grp.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            logger.error("FAIL: %s not set in environment", var)
            return 64

    return _emit(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())

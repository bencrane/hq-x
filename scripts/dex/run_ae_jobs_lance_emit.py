"""AE jobs Lance emit (Pattern A — Volume-King, local).

Reads the ZSTD Parquet at:
    s3://dex-raw-landing-zone/ae-jobs/snapshot=YYYY-MM-DD/data.parquet
(written by run_ae_jobs_csv_to_r2.py) and writes a Lance dataset at:
    s3://dex-raw-landing-zone/polaris-warehouse/ae_jobs/jobs_lance

Multi-BTREE: job_posting_id (natural PK) + company_id + country_code +
job_posted_date + title_id. These are the matching-engine join axes —
company-side spine, geo filter, freshness ordering, title canonicalization.

L9 / L57: Parquet schema is already typed VARCHAR (pinned at upstream
CSV write); no all_varchar flag on read_parquet here.

Direct-style emit (not via _lib/lance_emit.py wrapper) because we need
multiple BTREE indices and the wrapper only accepts one.

Usage:
    doppler run --project hq-all --config prd -- python3 \\
        apps/data-engine-x/scripts/run_ae_jobs_lance_emit.py \\
        --snapshot-date 2026-05-19 \\
        --apply

    --dry-run                 → counts only, no Lance write
    --skip-if-rows-match      → skip if Lance dataset already at expected rows
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)

DATASET_SLUG = "ae_jobs_jobs_lance"
R2_BUCKET = "dex-raw-landing-zone"
LANCE_URI = f"s3://{R2_BUCKET}/polaris-warehouse/ae_jobs/jobs_lance"

BTREE_COLUMNS = [
    "job_posting_id",
    "company_id",
    "country_code",
    "job_posted_date",
    "title_id",
]

MIN_ROW_FLOOR = 80_000  # CSV has 89,498 rows (3.76M wc -l lines, but
                        # job_description_formatted has embedded newlines).


def _r2_account_id() -> str:
    return os.environ["R2_ENDPOINT"].split("//")[-1].split(".")[0]


def _storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


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
    con.execute("SET threads=4")
    con.execute("SET memory_limit='8GB'")
    return con


def _bridge_db_url() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def emit(snapshot_date: str, skip_if_rows_match: bool) -> dict:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts._lib.lance_commit_lock import lance_commit_lock

    os.environ["TMPDIR"] = "/tmp/lance"
    os.environ["LANCE_BYPASS_SPILLING"] = "true"
    Path("/tmp/lance").mkdir(parents=True, exist_ok=True)

    import lance

    parquet_uri = (
        f"r2://{R2_BUCKET}/ae-jobs/snapshot={snapshot_date}/data.parquet"
    )
    logger.info("input:  %s", parquet_uri)
    logger.info("output: %s", LANCE_URI)

    con = _connect_duckdb()
    parquet_rows = con.execute(
        f"SELECT count(*) FROM read_parquet('{parquet_uri}')"
    ).fetchone()[0]
    logger.info("parquet rows: %d (floor %d)", parquet_rows, MIN_ROW_FLOOR)

    if parquet_rows < MIN_ROW_FLOOR:
        msg = (f"FAIL: parquet rows {parquet_rows} below floor "
               f"{MIN_ROW_FLOOR}")
        logger.error(msg)
        raise SystemExit(msg)

    storage_options = _storage_options()

    if skip_if_rows_match:
        try:
            existing = lance.dataset(
                LANCE_URI, storage_options=storage_options
            )
            existing_rows = existing.count_rows()
            if existing_rows == parquet_rows:
                logger.info(
                    "skip-if-rows-match: Lance has %d rows, matches Parquet — "
                    "skipping write (will still attempt BTREEs)",
                    existing_rows,
                )
                _build_btrees(existing)
                return {
                    "status": "skipped_write",
                    "parquet_rows": parquet_rows,
                    "lance_rows": existing_rows,
                    "lance_uri": LANCE_URI,
                }
        except Exception:
            pass

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        reader = con.from_query(
            f"SELECT * FROM read_parquet('{parquet_uri}')"
        ).to_arrow_reader(batch_size=100_000)

        logger.info("writing Lance dataset (mode=overwrite) ...")
        ds = lance.write_dataset(
            reader,
            LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t0
        lance_rows = ds.count_rows()
        logger.info(
            "wrote %d rows in %.1fs (version=%s)",
            lance_rows, write_dur, ds.version,
        )

        _build_btrees(ds)

        t1 = time.time()
        try:
            stats = ds.optimize.compact_files()
            logger.info("compact_files: %s", stats)
        except Exception as e:
            logger.warning("compact_files failed (non-fatal): %s", e)
        try:
            cleanup = ds.cleanup_old_versions(older_than=timedelta(days=7))
            logger.info("cleanup_old_versions: %s", cleanup)
        except Exception as e:
            logger.warning("cleanup_old_versions failed (non-fatal): %s", e)
        logger.info("optimize done in %.1fs", time.time() - t1)

    return {
        "status": "succeeded",
        "parquet_rows": parquet_rows,
        "lance_rows": lance_rows,
        "lance_version": ds.version,
        "lance_uri": LANCE_URI,
        "duration_s": round(time.time() - t0, 1),
    }


def _build_btrees(ds) -> None:
    for col in BTREE_COLUMNS:
        t = time.time()
        try:
            ds.create_scalar_index(col, index_type="BTREE", replace=True)
            logger.info("BTREE on %s: OK (%.1fs)", col, time.time() - t)
        except Exception as e:
            logger.error("BTREE on %s FAILED: %s", col, e)
            raise


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snapshot-date", required=True,
                    help="YYYY-MM-DD partition under ae-jobs/snapshot=...")
    ap.add_argument("--skip-if-rows-match", action="store_true",
                    help="skip the write if Lance already has matching rows")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true")
    grp.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    for var in (
        "R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
    ):
        if not os.environ.get(var):
            logger.error("FAIL: %s not set", var)
            return 64

    if args.dry_run:
        con = _connect_duckdb()
        parquet_uri = (
            f"r2://{R2_BUCKET}/ae-jobs/snapshot={args.snapshot_date}/"
            f"data.parquet"
        )
        rows = con.execute(
            f"SELECT count(*) FROM read_parquet('{parquet_uri}')"
        ).fetchone()[0]
        logger.info("DRY RUN — parquet rows=%d", rows)
        return 0

    metrics = emit(args.snapshot_date, args.skip_if_rows_match)
    logger.info("OK — metrics: %s", metrics)
    if (metrics.get("status") == "succeeded"
            and metrics["parquet_rows"] != metrics["lance_rows"]):
        logger.error(
            "FAIL: row count mismatch parquet=%d lance=%d",
            metrics["parquet_rows"], metrics["lance_rows"],
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

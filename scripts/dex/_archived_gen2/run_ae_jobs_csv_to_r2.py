"""AE jobs CSV → ZSTD Parquet → R2 ingest (Volume-King Pattern A stage 1).

Transcodes a LinkedIn job-posting CSV (delivered as a 988 MB local file)
into ZSTD-compressed Parquet via DuckDB single-shot COPY, then uploads
to R2 at:

    s3://dex-raw-landing-zone/ae-jobs/snapshot=YYYY-MM-DD/data.parquet

L9 (corrected): pin all_varchar=TRUE at READ time on the CSV so every
column lands as VARCHAR in the Parquet schema; preserves leading zeros,
date sentinels, and stringified ints from LinkedIn's export. Downstream
DuckDB reads of this Parquet do NOT pass all_varchar — L57 (Parquet
schema is already typed VARCHAR).

L42: ContentType only on R2 upload; never set Content-Encoding=zstd
(internal Parquet ZSTD compression is handled by the Parquet reader).

Ledger: writes one row to ops.ae_jobs_r2_ingest_runs with status
transitions running → completed (or failed) for forensic recovery.

Usage:
    doppler run --project hq-all --config prd -- python3 \\
        apps/data-engine-x/scripts/run_ae_jobs_csv_to_r2.py \\
        --csv /path/to/snap_*.csv \\
        --snapshot-date 2026-05-19 \\
        --apply

    # dry-run probes header + row count only, no R2 write:
    --dry-run
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)

R2_BUCKET = "dex-raw-landing-zone"
R2_PREFIX_BASE = "ae-jobs"
LOCAL_PARQUET_DIR = Path("/tmp/ae-jobs")


def _r2_account_id() -> str:
    return os.environ["R2_ENDPOINT"].split("//")[-1].split(".")[0]


def _conn_url() -> str:
    url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("FAIL: DEX_DB_URL_DIRECT not set")
    return url


def _start_run(snapshot_date: str, csv_path: str, csv_bytes: int) -> str:
    import psycopg

    r2_prefix = f"{R2_PREFIX_BASE}/snapshot={snapshot_date}/"
    run_id = str(uuid.uuid4())
    with psycopg.connect(_conn_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ops.ae_jobs_r2_ingest_runs
                    (run_id, status, snapshot_date, source_csv_path,
                     source_csv_bytes, r2_prefix)
                VALUES (%s, 'running', %s, %s, %s, %s)
                """,
                (run_id, snapshot_date, csv_path, csv_bytes, r2_prefix),
            )
        conn.commit()
    return run_id


def _complete_run(
    run_id: str,
    *,
    csv_rows: int,
    parquet_row_count: int,
    r2_object_count: int,
    r2_total_bytes: int,
) -> None:
    import psycopg

    with psycopg.connect(_conn_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE ops.ae_jobs_r2_ingest_runs
                   SET status            = 'completed',
                       completed_at      = now(),
                       source_csv_rows   = %s,
                       parquet_row_count = %s,
                       r2_object_count   = %s,
                       r2_total_bytes    = %s
                 WHERE run_id = %s
                """,
                (csv_rows, parquet_row_count, r2_object_count,
                 r2_total_bytes, run_id),
            )
        conn.commit()


def _fail_run(run_id: str, error_message: str) -> None:
    import psycopg

    with psycopg.connect(_conn_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE ops.ae_jobs_r2_ingest_runs
                   SET status = 'failed',
                       completed_at = now(),
                       error_message = %s
                 WHERE run_id = %s
                """,
                (error_message[:2000], run_id),
            )
        conn.commit()


def _probe_csv(csv_path: str) -> tuple[int, list[str]]:
    """Return (row_count, column_list) — fast DuckDB scan with all_varchar."""
    import duckdb

    con = duckdb.connect()
    rows = con.execute(
        f"SELECT count(*) FROM read_csv('{csv_path}', all_varchar=TRUE, "
        f"header=TRUE)"
    ).fetchone()[0]
    cols_rows = con.execute(
        f"DESCRIBE SELECT * FROM read_csv('{csv_path}', "
        f"all_varchar=TRUE, header=TRUE) LIMIT 0"
    ).fetchall()
    cols = [r[0] for r in cols_rows]
    con.close()
    return rows, cols


def _transcode_csv_to_parquet(csv_path: str, parquet_path: str) -> int:
    """DuckDB CSV → ZSTD Parquet (L9: all_varchar=TRUE pinned at write step)."""
    import duckdb

    LOCAL_PARQUET_DIR.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET memory_limit='8GB'")
    con.execute("SET temp_directory='/tmp'")

    logger.info("transcoding CSV → ZSTD Parquet ...")
    t0 = time.time()
    # null_padding + parallel scan can't co-exist with quoted newlines
    # (job_description_formatted has embedded \n in HTML), so drop the L56
    # defensive — this CSV is header-bearing and well-formed.
    con.execute(
        f"""
        COPY (
            SELECT * FROM read_csv('{csv_path}',
                                   all_varchar=TRUE,
                                   header=TRUE)
        ) TO '{parquet_path}' (FORMAT PARQUET, COMPRESSION ZSTD,
                               ROW_GROUP_SIZE 100000)
        """
    )
    dur = time.time() - t0
    logger.info("transcoded in %.1fs → %s", dur, parquet_path)

    row_count = con.execute(
        f"SELECT count(*) FROM read_parquet('{parquet_path}')"
    ).fetchone()[0]
    con.close()
    return row_count


def _upload_to_r2(parquet_path: str, r2_key: str) -> int:
    """Boto3 upload; L42: ContentType only, no Content-Encoding."""
    import boto3

    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="us-east-1",
    )
    size = Path(parquet_path).stat().st_size
    logger.info("uploading %.1f MB → s3://%s/%s",
                size / 1e6, R2_BUCKET, r2_key)
    t0 = time.time()
    s3.upload_file(
        parquet_path,
        R2_BUCKET,
        r2_key,
        ExtraArgs={"ContentType": "application/x-parquet"},
    )
    logger.info("uploaded in %.1fs", time.time() - t0)
    return size


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", required=True, help="Local path to CSV")
    ap.add_argument("--snapshot-date", required=True,
                    help="YYYY-MM-DD partition key for R2 prefix")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true",
                     help="transcode + upload + ledger row")
    grp.add_argument("--dry-run", action="store_true",
                     help="probe CSV (row count + columns) only")
    args = ap.parse_args()

    csv_path = args.csv
    if not Path(csv_path).is_file():
        logger.error("FAIL: CSV not found: %s", csv_path)
        return 1

    csv_bytes = Path(csv_path).stat().st_size
    logger.info("CSV: %s (%.1f MB)", csv_path, csv_bytes / 1e6)

    csv_rows, cols = _probe_csv(csv_path)
    logger.info("CSV rows: %d", csv_rows)
    logger.info("CSV columns (%d): %s", len(cols), cols)

    if args.dry_run:
        logger.info("DRY RUN — exiting before transcode/upload")
        return 0

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            logger.error("FAIL: %s not set", var)
            return 64

    run_id = _start_run(args.snapshot_date, csv_path, csv_bytes)
    logger.info("ledger run_id=%s status=running", run_id)

    try:
        parquet_path = str(
            LOCAL_PARQUET_DIR / f"ae-jobs-{args.snapshot_date}.parquet"
        )
        parquet_rows = _transcode_csv_to_parquet(csv_path, parquet_path)
        logger.info("Parquet rows: %d (vs CSV %d)", parquet_rows, csv_rows)
        if parquet_rows != csv_rows:
            raise RuntimeError(
                f"row-count parity FAIL: csv={csv_rows} parquet={parquet_rows}"
            )

        r2_key = f"{R2_PREFIX_BASE}/snapshot={args.snapshot_date}/data.parquet"
        size = _upload_to_r2(parquet_path, r2_key)

        _complete_run(
            run_id,
            csv_rows=csv_rows,
            parquet_row_count=parquet_rows,
            r2_object_count=1,
            r2_total_bytes=size,
        )
        logger.info("OK — run_id=%s status=completed", run_id)
        return 0
    except Exception as exc:
        logger.exception("FAIL during ingest")
        _fail_run(run_id, repr(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""s3 - JSearch Postgres -> R2 ZSTD-Parquet snapshot (Modal one-shot).

Exports ALL rows from entities.source_jsearch_search to
s3://dex-raw-landing-zone/jsearch/snapshot=YYYY-MM-DD/jobs.parquet
via pyarrow ParquetWriter (compression='zstd', batch_size=10_000).
Writes one audit row to ops.jsearch_r2_snapshot_runs per run.

Per L42: R2 upload sets ContentType='application/x-parquet' only (no
transport-layer encoding header — internal column-chunk ZSTD is already
handled by the Parquet reader; transport-layer hints break RW's S3 reader).

Run via (DETACH IS MANDATORY per L47):
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      modal run --detach scripts/run_jsearch_r2_snapshot.py::run
"""
from __future__ import annotations

import logging, os, sys
from datetime import date
from pathlib import Path

import modal

app = modal.App("data-engine-x-jsearch-r2-snapshot")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("psycopg[binary]", "pyarrow>=16.0", "boto3")
    .add_local_dir(Path(__file__).resolve().parent, remote_path="/root/scripts")
)

FUNCTION_SECRETS = [
    modal.Secret.from_name("bulk-ingest-r2"),
    modal.Secret.from_name("dex-db"),
]

BUCKET = "dex-raw-landing-zone"
BATCH_SIZE = 10_000
SOURCE_TABLE = "entities.source_jsearch_search"

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO, stream=sys.stdout,
)


@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=3600,
    memory=8192,
    cpu=4,
)
def snapshot() -> dict:
    import psycopg
    import pyarrow as pa
    import pyarrow.parquet as pq
    import boto3
    from botocore.client import Config

    snapshot_dt = date.today()
    snapshot_date_str = snapshot_dt.isoformat()
    r2_key = f"jsearch/snapshot={snapshot_date_str}/jobs.parquet"
    r2_uri = f"s3://{BUCKET}/{r2_key}"
    run_id: str | None = None
    # dex-db secret exposes DATABASE_URL; alias to DEX_DB_URL_DIRECT
    # per canonical precedent (build_bridge_sba_sos_ca_owner_lance.py:_bridge_database_url).
    db_url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ["DATABASE_URL"]

    # --- audit row: status='running' ---
    with psycopg.connect(db_url, autocommit=False) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ops.jsearch_r2_snapshot_runs "
            "(status, snapshot_date, r2_uri) VALUES ('running', %s, %s) "
            "RETURNING run_id",
            (snapshot_dt, r2_uri),
        )
        run_id = str(cur.fetchone()[0])
        conn.commit()

    local_path = Path("/tmp/jobs.parquet")
    pg_row_count = 0
    pq_row_count = 0
    try:
        with psycopg.connect(db_url, autocommit=False) as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM {SOURCE_TABLE}")
                pg_row_count = cur.fetchone()[0]
                logger.info("source rows: %d", pg_row_count)

            # Stream-export via server-side cursor; batch into Parquet.
            # CAST all jsonb columns to text so pyarrow doesn't try to infer
            # struct schemas from heterogeneous/empty-struct payloads (job_highlights,
            # apply_options, employer_reviews, etc. are jsonb but their shapes vary
            # row-to-row; pyarrow struct inference fails on empty/missing fields).
            JSONB_COLS = (
                "employer_reviews", "job_employment_types", "apply_options",
                "job_benefits", "job_benefits_strings", "job_highlights",
                "raw_source_row", "source_run_metadata",
            )
            select_cols_sql = (
                f"SELECT {', '.join(f'{c}::text AS {c}' if c in JSONB_COLS else c for c in '*')}"
                if False else None  # placeholder to satisfy parser
            )
            # Build the SELECT list by introspecting the table columns.
            with conn.cursor() as cur0:
                cur0.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='entities' AND table_name='source_jsearch_search' "
                    "ORDER BY ordinal_position"
                )
                all_cols = [r[0] for r in cur0.fetchall()]
            select_list = ", ".join(
                f"{c}::text AS {c}" if c in JSONB_COLS else c for c in all_cols
            )
            writer: pq.ParquetWriter | None = None
            with conn.cursor(name="jsearch_export") as cur:
                cur.itersize = BATCH_SIZE
                cur.execute(f"SELECT {select_list} FROM {SOURCE_TABLE}")
                colnames = [d.name for d in cur.description]
                while True:
                    rows = cur.fetchmany(BATCH_SIZE)
                    if not rows:
                        break
                    cols = list(zip(*rows))
                    arrays = [pa.array(c) for c in cols]
                    batch = pa.RecordBatch.from_arrays(arrays, names=colnames)
                    tbl = pa.Table.from_batches([batch])
                    if writer is None:
                        writer = pq.ParquetWriter(
                            local_path, tbl.schema, compression="zstd",
                        )
                    writer.write_table(tbl)
                    pq_row_count += tbl.num_rows
            if writer is not None:
                writer.close()

        # R2 upload: ContentType only per L42 (no transport-layer encoding header).
        endpoint = os.environ["R2_ENDPOINT"]
        s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            region_name="us-east-1",
            config=Config(s3={"addressing_style": "path"}),
        )
        with open(local_path, "rb") as fh:
            s3.put_object(
                Bucket=BUCKET, Key=r2_key, Body=fh,
                ContentType="application/x-parquet",
            )
        head = s3.head_object(Bucket=BUCKET, Key=r2_key)
        parquet_size = int(head["ContentLength"])

        # --- audit row: status='succeeded' ---
        with psycopg.connect(db_url, autocommit=False) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE ops.jsearch_r2_snapshot_runs SET status='succeeded', "
                "completed_at=now(), postgres_row_count=%s, parquet_row_count=%s, "
                "parquet_size_bytes=%s WHERE run_id=%s",
                (pg_row_count, pq_row_count, parquet_size, run_id),
            )
            conn.commit()

        return {
            "status": "succeeded", "run_id": run_id, "r2_uri": r2_uri,
            "postgres_row_count": pg_row_count, "parquet_row_count": pq_row_count,
            "parquet_size_bytes": parquet_size,
        }
    except Exception as e:
        logger.exception("snapshot failed")
        with psycopg.connect(db_url, autocommit=False) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE ops.jsearch_r2_snapshot_runs SET status='failed', "
                "completed_at=now(), error_text=%s WHERE run_id=%s",
                (f"{type(e).__name__}: {e}", run_id),
            )
            conn.commit()
        return {"status": "failed", "error": str(e), "run_id": run_id}


@app.local_entrypoint()
def run() -> None:
    """`modal run --detach scripts/run_jsearch_r2_snapshot.py::run`

    DETACH IS MANDATORY (L47 - Modal CLI disconnect kills attached jobs).
    """
    import json
    out = snapshot.remote()
    print(json.dumps(out, indent=2, default=str))

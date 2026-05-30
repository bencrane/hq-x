"""s5 - Glassdoor Postgres -> R2 ZSTD-Parquet snapshot (Modal one-shot).

Exports ALL rows from 4 entities.source_glassdoor_* tables to
s3://dex-raw-landing-zone/glassdoor/<endpoint>/snapshot=YYYY-MM-DD/data.parquet
via pyarrow ParquetWriter (compression='zstd', batch_size=10_000).
Writes one audit row per source table to ops.glassdoor_ingest_runs with
endpoint='r2_snapshot' (unified-ledger discriminator per audit decision).

Per L42: R2 upload sets ContentType='application/x-parquet' only (no
transport-layer encoding header — internal column-chunk ZSTD is already
handled by the Parquet reader; transport-layer hints break RW's S3 reader).
# L42-OK-VERIFIED

Run via (DETACH IS MANDATORY per L47):
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      modal run --detach scripts/run_glassdoor_r2_snapshot.py::run
"""
from __future__ import annotations

import logging, os, sys
from datetime import date
from pathlib import Path

import modal

app = modal.App("data-engine-x-glassdoor-r2-snapshot")

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

# (endpoint_slug, fully_qualified_postgres_table)
SOURCE_TABLES: list[tuple[str, str]] = [
    ("company_search",      "entities.source_glassdoor_company_search"),
    ("company_overview",    "entities.source_glassdoor_company_overview"),
    ("company_salaries",    "entities.source_glassdoor_company_salaries"),
    ("company_salaries_v2", "entities.source_glassdoor_company_salaries_v2"),
]

# Columns whose values may be jsonb — cast to text at SELECT-time so pyarrow
# doesn't try to infer struct schemas from heterogeneous payloads. Conservative
# superset; non-existent columns on a given table are pruned at runtime.
JSONB_COLUMN_NAMES = {
    "raw_source_row", "source_run_metadata",
    "competitors", "office_locations", "best_places_to_work_awards",
}

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
    import uuid as _uuid
    import psycopg
    import pyarrow as pa
    import pyarrow.parquet as pq
    import boto3
    from botocore.client import Config

    snapshot_dt = date.today()
    snapshot_date_str = snapshot_dt.isoformat()
    # dex-db secret exposes DATABASE_URL; alias to DEX_DB_URL_DIRECT.
    db_url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ["DATABASE_URL"]

    endpoint = os.environ["R2_ENDPOINT"]
    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="us-east-1",
        config=Config(s3={"addressing_style": "path"}),
    )

    results: list[dict] = []
    for slug, src_table in SOURCE_TABLES:
        r2_key = f"glassdoor/{slug}/snapshot={snapshot_date_str}/data.parquet"
        r2_uri = f"s3://{BUCKET}/{r2_key}"
        local_path = Path(f"/tmp/{slug}.parquet")
        logger.info("=== snapshotting %s -> %s ===", src_table, r2_uri)

        # --- audit row: status='running' in unified ledger ---
        run_id = str(_uuid.uuid4())
        request_params = {"source_table": src_table, "snapshot_date": snapshot_date_str}
        import json as _json
        with psycopg.connect(db_url, autocommit=False) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ops.glassdoor_ingest_runs "
                "(run_id, endpoint, status, attempt, request_params, "
                " source_filename, source_download_url, invoked_by) "
                "VALUES (%s, 'r2_snapshot', 'running', 1, %s::jsonb, %s, %s, "
                "        'modal:glassdoor_r2_snapshot')",
                (run_id, _json.dumps(request_params), src_table, r2_uri),
            )
            conn.commit()

        pg_row_count = 0
        pq_row_count = 0
        try:
            with psycopg.connect(db_url, autocommit=False) as conn:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT COUNT(*) FROM {src_table}")
                    pg_row_count = cur.fetchone()[0]
                    logger.info("source rows: %d", pg_row_count)

                # Introspect columns to build a typed SELECT that casts jsonb -> text.
                schema_part, table_part = src_table.split(".", 1)
                with conn.cursor() as cur0:
                    cur0.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema=%s AND table_name=%s "
                        "ORDER BY ordinal_position",
                        (schema_part, table_part),
                    )
                    all_cols = [r[0] for r in cur0.fetchall()]
                select_list = ", ".join(
                    f"{c}::text AS {c}" if c in JSONB_COLUMN_NAMES else c
                    for c in all_cols
                )
                writer: pq.ParquetWriter | None = None
                with conn.cursor(name=f"gd_export_{slug}") as cur:
                    cur.itersize = BATCH_SIZE
                    cur.execute(f"SELECT {select_list} FROM {src_table}")
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
                else:
                    # Empty table — write an empty parquet so the R2 key exists.
                    empty_tbl = pa.Table.from_pylist([], schema=pa.schema([
                        pa.field(c, pa.string()) for c in all_cols
                    ]))
                    pq.write_table(empty_tbl, local_path, compression="zstd")

            # R2 upload: ContentType only per L42 (no transport-layer encoding header).
            # L42-OK-VERIFIED
            with open(local_path, "rb") as fh:
                s3.put_object(
                    Bucket=BUCKET, Key=r2_key, Body=fh,
                    ContentType="application/x-parquet",
                )
            head = s3.head_object(Bucket=BUCKET, Key=r2_key)
            parquet_size = int(head["ContentLength"])

            # audit row: status='completed' on the unified ledger.
            with psycopg.connect(db_url, autocommit=False) as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE ops.glassdoor_ingest_runs SET status='completed', "
                    "  completed_at=now(), rows_seen=%s, rows_upserted=%s, "
                    "  duration_seconds=EXTRACT(EPOCH FROM (now() - started_at)) "
                    "WHERE run_id=%s AND endpoint='r2_snapshot'",
                    (pg_row_count, pq_row_count, run_id),
                )
                conn.commit()

            results.append({
                "endpoint": slug, "status": "succeeded",
                "run_id": run_id, "r2_uri": r2_uri,
                "postgres_row_count": pg_row_count,
                "parquet_row_count": pq_row_count,
                "parquet_size_bytes": parquet_size,
            })
        except Exception as e:
            logger.exception("snapshot failed for %s", src_table)
            with psycopg.connect(db_url, autocommit=False) as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE ops.glassdoor_ingest_runs SET status='failed', "
                    "  completed_at=now(), error_text=%s, error_class='unknown' "
                    "WHERE run_id=%s AND endpoint='r2_snapshot'",
                    (f"{type(e).__name__}: {e}", run_id),
                )
                conn.commit()
            results.append({
                "endpoint": slug, "status": "failed",
                "error": str(e), "run_id": run_id,
            })

    return {"results": results}


@app.local_entrypoint()
def run() -> None:
    """`modal run --detach scripts/run_glassdoor_r2_snapshot.py::run`

    DETACH IS MANDATORY (L47 - Modal CLI disconnect kills attached jobs).
    """
    import json
    out = snapshot.remote()
    print(json.dumps(out, indent=2, default=str))

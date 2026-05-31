"""Clay Find People AE/US → Lance emit (Pattern A, one-shot).

Reads entities.source_clay_find_people WHERE _snapshot_date = ? via
Doppler-injected DEX_DB_URL_POOLED (psycopg). Writes a ZSTD Parquet to
R2 for archival, then writes a Lance dataset to the Polaris warehouse.

Snapshot date selection (C6 per-snapshot precedence):
  1. --snapshot YYYY-MM-DD CLI argument.
  2. CLAY_FIND_PEOPLE_SNAPSHOT env var.
  3. Default: SELECT MAX(_snapshot_date) FROM entities.source_clay_find_people.

Output Lance dataset:
  s3://dex-raw-landing-zone/polaris-warehouse/clay/find_people_ae_us_lance

BTREE indexes on:
  linkedin_url_normalized, domain, latest_experience_start_date, _snapshot_date

BTREEs support filter pushdown when the Lance dataset accumulates multiple
snapshots; downstream matchers filter by _snapshot_date.

IMPORTANT — DETACH IS MANDATORY:
  Run via `modal run --detach` per L47. The local_entrypoint enforces
  this requirement via comment; the operator MUST NOT omit --detach.

Secrets:
    dex-db    — DEX_DB_URL_DIRECT for lance_commit_lock.
    bulk-ingest-r2     — R2 credentials (R2_ENDPOINT, R2_ACCESS_KEY_ID,
                         R2_SECRET_ACCESS_KEY).

Run via (DETACH IS MANDATORY):
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      modal run --detach modal/clay_find_people_ae_us_lance_emit_app.py::run

Or with explicit snapshot:
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      modal run --detach modal/clay_find_people_ae_us_lance_emit_app.py::run \\
      --snapshot 2026-05-17
"""
from __future__ import annotations

import logging
import os
import sys
import time
import uuid
from datetime import timedelta
from pathlib import Path

import modal

app = modal.App("data-engine-x-clay-find-people-ae-us-lance-emit")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install_from_pyproject("modal/pyproject.toml")
    .pip_install(
        "duckdb",
        "psycopg[binary]",
        "pylance>=6.0,<7.0",
        "lancedb>=0.30,<0.32",
        "pyarrow>=16.0",
        "boto3",
    )
    .add_local_dir(
        Path(__file__).resolve().parent.parent / "scripts" / "dex",
        remote_path="/root/scripts",
    )
    .add_local_dir(
        Path(__file__).resolve().parent / "landing",
        remote_path="/root/landing",
    )
)

FUNCTION_SECRETS = [
    modal.Secret.from_name("hqx-db"),
    modal.Secret.from_name("bulk-ingest-r2"),
]

DATASET_SLUG = "clay_find_people_ae_us_lance"
LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/clay/find_people_ae_us_lance"
R2_PARQUET_PREFIX = "clay/find_people_ae_us/snapshot={snapshot_date}/data.parquet"

# Smoke-mode floor = 3 (3-row sample); full Phase 1 floor = 80,000.
# Select via --min-rows CLI arg or CLAY_FIND_PEOPLE_MIN_ROWS env var.
DEFAULT_MIN_ROW_FLOOR = 3

# BTREE columns per validator §6c.
BTREE_COLUMNS = [
    "linkedin_url_normalized",
    "domain",
    "latest_experience_start_date",
    "_snapshot_date",
]

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)


def _storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _r2_account_id() -> str:
    return os.environ["R2_ENDPOINT"].split("//")[-1].split(".")[0]


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


def _db_url() -> str:
    """Prefer DEX_DB_URL_POOLED for reads; fallback to DATABASE_URL."""
    return (
        os.environ.get("DEX_DB_URL_POOLED")
        or os.environ.get("DATABASE_URL")
        or os.environ["DEX_DB_URL_DIRECT"]
    )


def _resolve_snapshot_date(explicit_snapshot: str | None) -> str:
    """Resolve snapshot date per precedence: arg > env > MAX from DB."""
    if explicit_snapshot:
        return explicit_snapshot

    env_snap = os.environ.get("CLAY_FIND_PEOPLE_SNAPSHOT")
    if env_snap:
        return env_snap

    # Default: MAX(_snapshot_date) from staging table.
    import psycopg

    with psycopg.connect(_db_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT MAX(_snapshot_date)::text FROM entities.source_clay_find_people"
            )
            row = cur.fetchone()
    if row is None or row[0] is None:
        raise RuntimeError(
            "entities.source_clay_find_people is empty — no snapshot to emit"
        )
    return row[0]


def _existing_btree_columns(ds) -> set:
    cols = set()
    for idx in ds.list_indices():
        fields = idx.get("fields") if isinstance(idx, dict) else []
        itype = idx.get("type") if isinstance(idx, dict) else ""
        if "BTREE" in str(itype).upper() or "BTREE" in str(idx).upper():
            for f in (fields or []):
                cols.add(str(f))
    return cols


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=10800,
    memory=32768,
    cpu=8,
)
def emit(snapshot_date: str, min_row_floor: int = DEFAULT_MIN_ROW_FLOOR) -> dict:
    """Pattern A Lance emit: read staging → write Parquet to R2 → write Lance.

    BTREE on linkedin_url_normalized, domain, latest_experience_start_date,
    _snapshot_date. Compact + cleanup_old_versions(timedelta(days=7)).

    MUST be launched via `modal run --detach` per L47.
    """
    sys.path.insert(0, "/root")
    from landing.ledger import HeartbeatLoop  # noqa: E402
    from scripts._lib.lance_commit_lock import lance_commit_lock

    os.environ["TMPDIR"] = "/tmp/lance"
    Path("/tmp/lance").mkdir(parents=True, exist_ok=True)
    # DataFusion external-sort spill OOMs on multi-million-row string BTREEs.
    os.environ["LANCE_BYPASS_SPILLING"] = "true"
    os.environ.setdefault("LANCE_INDEX_CACHE_SIZE", "1g")

    import lance
    import pyarrow as pa
    import psycopg

    storage_options = _storage_options()
    run_id = str(uuid.uuid4())
    hb_cm = HeartbeatLoop(
        cron_app=app.name,
        cron_function="emit",
        run_id=run_id,
    )
    hb_cm.__enter__()
    hb_cm.set_stage("step_1_read_staging", {"snapshot_date": snapshot_date})

    # Step 1: read staging table for the target snapshot.
    logger.info("reading staging table for _snapshot_date = %s ...", snapshot_date)
    t0 = time.time()

    with psycopg.connect(_db_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    url,
                    name,
                    domain,
                    last_name,
                    first_name,
                    location_name,
                    company_table_id,
                    company_record_id,
                    matched_experience,
                    structured_location::text AS structured_location,
                    latest_experience_title,
                    latest_experience_company,
                    latest_experience_start_date,
                    linkedin_url_normalized,
                    _snapshot_date::text AS _snapshot_date,
                    source_provider,
                    ingested_at::text AS ingested_at
                FROM entities.source_clay_find_people
                WHERE _snapshot_date = %s::date
                """,
                (snapshot_date,),
            )
            rows = cur.fetchall()
            col_names = [desc[0] for desc in cur.description]

    staging_rows = len(rows)
    logger.info(
        "read %d rows from staging (elapsed=%.1fs)", staging_rows, time.time() - t0
    )

    if staging_rows < min_row_floor:
        msg = f"FAIL: staging row count {staging_rows} below floor {min_row_floor}"
        logger.error(msg)
        hb_cm.__exit__(None, None, None)
        return {"status": "failed", "error": msg, "rows_staging": staging_rows}

    # Build PyArrow table from rows.
    hb_cm.set_stage("step_2_build_arrow", {"rows_staging": staging_rows})
    arrow_data = {col: [row[i] for row in rows] for i, col in enumerate(col_names)}
    arrow_table = pa.table(arrow_data)
    logger.info("arrow table: %d rows x %d cols", arrow_table.num_rows, len(col_names))

    # Step 2: write ZSTD Parquet to R2 for archival (L42 compliant: ContentType only).
    hb_cm.set_stage("step_3_r2_parquet_upload")
    import tempfile
    import boto3
    import pyarrow.parquet as pq

    r2_key = R2_PARQUET_PREFIX.format(snapshot_date=snapshot_date)
    r2_bucket = "dex-raw-landing-zone"
    endpoint_url = os.environ["R2_ENDPOINT"]

    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp_f:
        tmp_path = tmp_f.name

    pq.write_table(arrow_table, tmp_path, compression="zstd", compression_level=9)
    logger.info("wrote Parquet to temp: %s", tmp_path)

    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="us-east-1",
    )
    # L42: upload with ContentType only; no ContentEncoding header.
    s3.upload_file(
        tmp_path,
        r2_bucket,
        r2_key,
        ExtraArgs={"ContentType": "application/x-parquet"},
    )
    logger.info("uploaded Parquet to R2: s3://%s/%s", r2_bucket, r2_key)
    os.unlink(tmp_path)

    # Step 3: Lance write inside commit_lock.
    hb_cm.set_stage("step_4_lance_write")
    write_t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        con = _connect_duckdb()
        con.register("staging_rows", arrow_table)
        reader = con.execute("SELECT * FROM staging_rows").to_arrow_reader(
            batch_size=100_000
        )

        logger.info("writing Lance dataset to %s ...", LANCE_URI)
        ds = lance.write_dataset(
            reader,
            LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - write_t0
        lance_rows = ds.count_rows()
        logger.info(
            "wrote %d rows in %.1fs (version=%s)",
            lance_rows, write_dur, ds.version,
        )

    # Step 4: floor gate on Lance row count.
    if lance_rows < min_row_floor:
        msg = f"FAIL: Lance row count {lance_rows} below floor {min_row_floor}"
        logger.error(msg)
        hb_cm.__exit__(None, None, None)
        return {"status": "failed", "error": msg, "rows_lance": lance_rows}

    if lance_rows != staging_rows:
        logger.warning(
            "row count mismatch: staging=%d, lance=%d", staging_rows, lance_rows
        )

    # Step 5: BTREE on linkedin_url_normalized, domain, latest_experience_start_date,
    # _snapshot_date (validator §6c).
    hb_cm.set_stage("step_5_btree_indexes")
    t_btree = time.time()
    existing_btree = _existing_btree_columns(ds)
    logger.info("existing BTREE columns: %s", sorted(existing_btree))
    for col in BTREE_COLUMNS:
        if col in existing_btree:
            logger.info("BTREE on %s already present — skipping", col)
            continue
        try:
            ds.create_scalar_index(col, index_type="BTREE", replace=True)
            logger.info("BTREE on %s: OK", col)
        except Exception as e:
            logger.error("BTREE on %s FAILED: %s", col, e)
            raise

    # Step 6: compact + cleanup_old_versions(timedelta(days=7)).
    try:
        ds.optimize.compact_files()
        ds.cleanup_old_versions(older_than=timedelta(days=7))
    except Exception as e:
        logger.warning("Optimize failed (non-fatal): %s", e)

    btree_dur = time.time() - t_btree
    final_rows = ds.count_rows()
    logger.info(
        "emit complete: %d rows, write=%.1fs btree=%.1fs",
        final_rows, write_dur, btree_dur,
    )
    hb_cm.__exit__(None, None, None)
    return {
        "status": "succeeded",
        "run_id": run_id,
        "snapshot_date": snapshot_date,
        "rows_staging": staging_rows,
        "rows_lance": final_rows,
        "lance_uri": LANCE_URI,
        "r2_parquet": f"s3://{r2_bucket}/{r2_key}",
        "write_duration_s": round(write_dur, 1),
        "btree_duration_s": round(btree_dur, 1),
    }


@app.local_entrypoint()
def run(
    snapshot: str = "",
    min_rows: int = DEFAULT_MIN_ROW_FLOOR,
) -> None:
    """Launch the Clay Find People AE/US Lance emit.

    DETACH IS MANDATORY — run with `modal run --detach`:
        cd ~/hq-all/apps/data-engine-x && \\
          doppler run --project hq-all --config prd -- \\
          modal run --detach modal/clay_find_people_ae_us_lance_emit_app.py::run

    Args:
        snapshot: YYYY-MM-DD snapshot date override (optional).
                  Precedence: arg > CLAY_FIND_PEOPLE_SNAPSHOT env > MAX(_snapshot_date).
        min_rows: minimum row floor for smoke/full push mode (default=3 smoke).
                  For full 120k Phase 1 push, set --min-rows=80000.
    """
    import json

    # Resolve snapshot date locally (pre-flight check) before dispatching to Modal.
    resolved_snapshot = _resolve_snapshot_date(snapshot or None)
    logger.info("resolved snapshot_date = %s, min_row_floor = %d", resolved_snapshot, min_rows)

    out = emit.remote(resolved_snapshot, min_rows)
    print(json.dumps(out, indent=2, default=str))

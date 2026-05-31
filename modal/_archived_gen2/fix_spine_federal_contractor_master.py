"""Repair spines/federal_contractor_master_lance — enforce strict 1:1 UEI grain.

A downstream MV detected 1,209 duplicate UEIs in the P0 master spine
(102,622 rows / 101,413 distinct UEIs as of 2026-05-25). Cause: the upstream
emit landed >1 row per UEI from longitudinal SAM snapshots / re-emitted
recipient grain. The spine contract has always been 1 row per UEI.

This is a one-shot structural repair (not a recurring builder). It reads the
current dataset, picks the freshest row per UEI via window function, and
overwrites the dataset in place. Lance versioning keeps the pre-fix version
on disk as a rollback if needed (cleanup_old_versions is NOT called).

Pipeline:
  1. lance.dataset(federal_contractor_master_lance) → log row count.
  2. Stream Arrow into DuckDB; register as `master_in`.
  3. dedup TEMP TABLE via
        ROW_NUMBER() OVER (PARTITION BY uei
                           ORDER BY last_update_date DESC NULLS LAST) = 1
  4. Hard pre-write gate: COUNT(*) == COUNT(DISTINCT uei) on the deduped set.
  5. COPY (...) TO '/tmp/federal_contractor_master_dedup.parquet'
        FORMAT PARQUET, COMPRESSION ZSTD.
  6. pa.parquet.read_table → lance.write_dataset(mode='overwrite')
        inside lance_commit_lock.
  7. Hard post-write gate: ds.count_rows() == expected distinct UEI count.
  8. Rebuild BTREE on uei, primary_naics, state_of_incorporation
        (create_scalar_index with replace=True).

Modal hosting: @app.function(memory=49152, cpu=8, timeout=14400).
MUST be launched via `modal run --detach` per CLAUDE.md (Modal CLI disconnect
kills attached jobs >5min; the 4h timeout is the heavy-compute precedent).

Run via:
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      modal run --detach \\
        modal/fix_spine_federal_contractor_master.py::run
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

import modal

app = modal.App("data-engine-x-fix-federal-contractor-master-spine")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "duckdb",
        "psycopg[binary]",
        "pylance>=0.20",
        "pyarrow>=16.0",
        "boto3",
    )
    .add_local_dir(
        Path(__file__).resolve().parent.parent / "scripts" / "dex",
        remote_path="/root/scripts",
    )
)

FUNCTION_SECRETS = [
    modal.Secret.from_name("bulk-ingest-r2"),
    modal.Secret.from_name("fmcsa-ingest-db"),
]

DATASET_SLUG = "federal_contractor_master_lance"

MASTER_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/spines/federal_contractor_master_lance"
)
LOCAL_PARQUET_PATH = "/tmp/federal_contractor_master_dedup.parquet"

# Stamped pre-fix at 102,622 rows / 101,413 distinct UEI (1,209 dupes).
EXPECTED_DISTINCT_UEI = 101_413
# Soft floor: anything materially below the known distinct count is a bug.
MIN_ROW_FLOOR = 100_000

BTREE_COLUMNS = ["uei", "primary_naics", "state_of_incorporation"]

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


def _connect_duckdb():
    import duckdb

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("SET memory_limit='40GB'")
    con.execute("SET threads=8")
    con.execute("SET preserve_insertion_order=false")
    con.execute("SET temp_directory='/tmp/duckdb'")
    Path("/tmp/duckdb").mkdir(parents=True, exist_ok=True)
    return con


def _existing_btree_columns(ds) -> set:
    cols: set = set()
    for idx in ds.list_indices():
        fields = idx.get("fields") if isinstance(idx, dict) else []
        itype = idx.get("type") if isinstance(idx, dict) else ""
        if "BTREE" in str(itype).upper() or "BTREE" in str(idx).upper():
            for f in (fields or []):
                cols.add(str(f))
    return cols


@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=14400,
    memory=49152,
    cpu=8,
)
def dedupe() -> dict:
    sys.path.insert(0, "/root")
    from scripts._lib.lance_commit_lock import lance_commit_lock

    os.environ["TMPDIR"] = "/tmp/lance"
    Path("/tmp/lance").mkdir(parents=True, exist_ok=True)
    os.environ["LANCE_BYPASS_SPILLING"] = "true"
    os.environ.setdefault("LANCE_INDEX_CACHE_SIZE", "1g")

    import lance
    import pyarrow.parquet as pq

    storage_options = _storage_options()
    t_total = time.time()

    # ───── Step 1: open dataset, log starting state ─────────────────────────
    logger.info("opening %s ...", MASTER_LANCE_URI)
    ds_in = lance.dataset(MASTER_LANCE_URI, storage_options=storage_options)
    rows_before = ds_in.count_rows()
    version_before = ds_in.version
    schema_names = [f.name for f in ds_in.schema]
    logger.info(
        "  starting: %d rows, version=%s, %d columns",
        rows_before, version_before, len(schema_names),
    )
    for required in ("uei", "last_update_date", "primary_naics", "state_of_incorporation"):
        if required not in schema_names:
            raise RuntimeError(
                f"FAIL: required column {required!r} missing from master spine "
                f"schema; cannot dedupe. Schema: {schema_names}"
            )

    # ───── Step 2: stream into DuckDB ───────────────────────────────────────
    logger.info("scanning Lance → Arrow ...")
    t_scan = time.time()
    master_arrow = ds_in.scanner().to_table()
    logger.info(
        "  arrow table: %d rows x %d cols (%.1fs)",
        master_arrow.num_rows, master_arrow.num_columns, time.time() - t_scan,
    )

    con = _connect_duckdb()
    con.register("master_in", master_arrow)

    distinct_before = con.execute(
        "SELECT COUNT(DISTINCT uei) FROM master_in"
    ).fetchone()[0]
    dupe_count = rows_before - distinct_before
    logger.info(
        "  pre-dedup: rows=%d distinct_uei=%d dupes=%d",
        rows_before, distinct_before, dupe_count,
    )

    # ───── Step 3: window-function dedup ────────────────────────────────────
    logger.info(
        "step 3: ROW_NUMBER() PARTITION BY uei ORDER BY last_update_date DESC ..."
    )
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE master_dedup AS
        WITH ranked AS (
            SELECT
                m.*,
                ROW_NUMBER() OVER (
                    PARTITION BY uei
                    ORDER BY last_update_date DESC NULLS LAST
                ) AS rn
            FROM master_in m
        )
        SELECT * EXCLUDE (rn)
        FROM ranked
        WHERE rn = 1
        """
    )
    rows_dedup, distinct_dedup = con.execute(
        "SELECT COUNT(*), COUNT(DISTINCT uei) FROM master_dedup"
    ).fetchone()
    logger.info(
        "  master_dedup: rows=%d distinct_uei=%d",
        rows_dedup, distinct_dedup,
    )

    # ───── Step 4: pre-write invariant gates ────────────────────────────────
    if rows_dedup != distinct_dedup:
        msg = (
            f"FAIL pre-write: dedup result has rows={rows_dedup} but "
            f"distinct_uei={distinct_dedup} — window function did not "
            f"collapse to 1:1 UEI grain. ABORTING before R2 overwrite."
        )
        logger.error(msg)
        return {"status": "failed", "error": msg}
    if rows_dedup != distinct_before:
        msg = (
            f"FAIL pre-write: dedup rows={rows_dedup} != pre-dedup "
            f"distinct_uei={distinct_before}. Row loss detected. "
            f"ABORTING before R2 overwrite."
        )
        logger.error(msg)
        return {"status": "failed", "error": msg}
    if rows_dedup < MIN_ROW_FLOOR:
        msg = (
            f"FAIL pre-write: dedup rows={rows_dedup} below floor "
            f"{MIN_ROW_FLOOR}. ABORTING before R2 overwrite."
        )
        logger.error(msg)
        return {"status": "failed", "error": msg}
    logger.info(
        "pre-write gates PASS: 1:1 UEI grain confirmed (%d rows = %d distinct)",
        rows_dedup, distinct_dedup,
    )

    # ───── Step 5: COPY to local Parquet ─────────────────────────────────────
    logger.info("step 5: COPY master_dedup → %s ...", LOCAL_PARQUET_PATH)
    t_copy = time.time()
    con.execute(
        f"""
        COPY (SELECT * FROM master_dedup)
        TO '{LOCAL_PARQUET_PATH}'
        (FORMAT PARQUET, COMPRESSION ZSTD)
        """
    )
    copy_dur = time.time() - t_copy
    parquet_size = Path(LOCAL_PARQUET_PATH).stat().st_size
    logger.info(
        "  parquet written: %.1f MB in %.1fs",
        parquet_size / (1024 * 1024), copy_dur,
    )

    # ───── Step 6: lance.write_dataset(mode='overwrite') ────────────────────
    logger.info("step 6: lance.write_dataset → %s (mode=overwrite)", MASTER_LANCE_URI)
    t_write = time.time()
    with lance_commit_lock(DATASET_SLUG):
        table = pq.read_table(LOCAL_PARQUET_PATH)
        ds_out = lance.write_dataset(
            table,
            MASTER_LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t_write
        rows_after = ds_out.count_rows()
        version_after = ds_out.version
        logger.info(
            "  lance write: %d rows in %.1fs (version: %s → %s)",
            rows_after, write_dur, version_before, version_after,
        )

    # ───── Step 7: post-write invariant gate ────────────────────────────────
    if rows_after != rows_dedup:
        msg = (
            f"FAIL post-write: ds.count_rows()={rows_after} != "
            f"expected dedup rows={rows_dedup}. Lance write did not land "
            f"the expected payload. Rollback via Lance version {version_before}."
        )
        logger.error(msg)
        return {"status": "failed", "error": msg, "version_before": version_before}
    logger.info(
        "post-write gate PASS: %d rows on disk == %d expected",
        rows_after, rows_dedup,
    )

    # ───── Step 8: rebuild BTREE × 3 ────────────────────────────────────────
    # Overwrite mode invalidates prior indices — rebuild from scratch.
    t_btree = time.time()
    existing_btree = _existing_btree_columns(ds_out)
    logger.info("existing BTREE columns post-overwrite: %s", sorted(existing_btree))
    for col in BTREE_COLUMNS:
        try:
            ds_out.create_scalar_index(col, index_type="BTREE", replace=True)
            logger.info("BTREE on %s: OK", col)
        except Exception as e:  # noqa: BLE001
            logger.error("BTREE on %s FAILED: %s", col, e)
            raise
    btree_dur = time.time() - t_btree

    # ───── Final reconciliation: re-open the freshly-written dataset ─────────
    ds_verify = lance.dataset(MASTER_LANCE_URI, storage_options=storage_options)
    verify_arrow = ds_verify.scanner(columns=["uei"]).to_table()
    con2 = _connect_duckdb()
    con2.register("verify_in", verify_arrow)
    verify_rows, verify_distinct = con2.execute(
        "SELECT COUNT(*), COUNT(DISTINCT uei) FROM verify_in"
    ).fetchone()
    logger.info(
        "final reconciliation: rows=%d distinct_uei=%d (must be equal)",
        verify_rows, verify_distinct,
    )
    if verify_rows != verify_distinct:
        msg = (
            f"FAIL final reconciliation: rows={verify_rows} != "
            f"distinct_uei={verify_distinct}. Spine is NOT 1:1. "
            f"Rollback via Lance version {version_before}."
        )
        logger.error(msg)
        return {
            "status": "failed",
            "error": msg,
            "version_before": version_before,
            "version_after": version_after,
        }

    total_dur = time.time() - t_total
    logger.info(
        "DONE — federal_contractor_master_lance now strict 1:1 UEI grain. "
        "rows=%d (was %d, removed %d dupes) total=%.1fs "
        "(scan+dedup=%.1fs write=%.1fs btree=%.1fs)",
        verify_rows, rows_before, rows_before - verify_rows,
        total_dur, t_copy - t_total, write_dur, btree_dur,
    )

    return {
        "status": "succeeded",
        "rows_before": rows_before,
        "rows_after": verify_rows,
        "distinct_uei_after": verify_distinct,
        "dupes_removed": rows_before - verify_rows,
        "version_before": str(version_before),
        "version_after": str(version_after),
        "expected_distinct_uei": EXPECTED_DISTINCT_UEI,
        "lance_uri": MASTER_LANCE_URI,
        "parquet_bytes": parquet_size,
        "copy_duration_s": round(copy_dur, 1),
        "write_duration_s": round(write_dur, 1),
        "btree_duration_s": round(btree_dur, 1),
        "total_duration_s": round(total_dur, 1),
        "btree_columns": BTREE_COLUMNS,
    }


@app.local_entrypoint()
def run() -> None:
    """`modal run --detach modal/fix_spine_federal_contractor_master.py::run`

    DETACH IS MANDATORY (CLAUDE.md — Modal CLI disconnect kills attached jobs
    over the 4h heavy-compute window).
    """
    out = dedupe.remote()
    print(json.dumps(out, indent=2, default=str))

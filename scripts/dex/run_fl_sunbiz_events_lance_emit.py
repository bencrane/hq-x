"""s4 - FL Sunbiz events Lance emit (Pattern A, Modal-hosted).

Cadence — **Operator-Only Bulk Run (Quarterly Batch).** State SoS pipelines
(CA / FL / NY / CO) are retired from automated schedules per the 2026-05-25
operational policy shift. Trigger manually, point-in-time. See
``apps/data-engine-x/modal/INDEX.md`` §"State SoS pipelines".

Reads s1's events ZSTD Parquet at
    s3://dex-raw-landing-zone/sos-fl/release=2026-05-16/events/data.parquet
and writes a Lance dataset at
    s3://dex-raw-landing-zone/polaris-warehouse/sos/fl_events_lance

Event-grain: each row is one entry from corevt.txt. Schema mirrors the 25
COREVENT fields from the pinned layout (event_doc_number is the entity-grain
join key matching cordata's entity_num; event_effective_date is the temporal
ordering column for change-history queries).

BTREE indices on event_doc_number + event_effective_date (audit p392 reconciles
the directive's conceptual "entity_num + event_date" to these actual COREVENT
layout names).

Pattern A discipline + LANCE_BYPASS_SPILLING=true + idempotent resume mirror s2/s3.

Modal hosting (validator p191): @app.function(cpu=8, memory=32768, timeout=7200).

Run via:
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      modal run scripts/run_fl_sunbiz_events_lance_emit.py::run
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

import modal

app = modal.App("data-engine-x-fl-sunbiz-events-lance-emit")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "duckdb",
        "psycopg[binary]",
        "pylance>=0.20",
        "pyarrow>=16.0",
    )
    .add_local_dir(
        Path(__file__).resolve().parent,
        remote_path="/root/scripts",
    )
)

FUNCTION_SECRETS = [
    modal.Secret.from_name("bulk-ingest-r2"),
    modal.Secret.from_name("dex-db"),
]

DATASET_SLUG = "fl_sunbiz_events_lance"
PARQUET_URI = "r2://dex-raw-landing-zone/sos-fl/release=2026-05-16/events/data.parquet"
LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sos/fl_events_lance"

# Validator p51: 9.6 GB / 664 byte/line = ~14.4M events; floor 80%.
MIN_ROW_FLOOR = 12_000_000

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)


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


def _bridge_database_url() -> None:
    if "DEX_DB_URL_DIRECT" not in os.environ and "DATABASE_URL" in os.environ:
        os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


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


def _existing_dataset(uri: str, storage_options: dict):
    import lance

    try:
        ds = lance.dataset(uri, storage_options=storage_options)
        return ds, ds.count_rows()
    except Exception:  # noqa: BLE001
        return None, 0


def _existing_btree_columns(ds) -> set:
    cols = set()
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
    timeout=7200,
    memory=32768,
    cpu=8,
)
def emit() -> dict:
    """Pattern A Lance emit: read s1 events Parquet, write Lance + BTREE."""
    sys.path.insert(0, "/root")
    from scripts._lib.lance_commit_lock import lance_commit_lock

    _bridge_database_url()
    os.environ["TMPDIR"] = "/tmp/lance"
    Path("/tmp/lance").mkdir(parents=True, exist_ok=True)
    os.environ["LANCE_BYPASS_SPILLING"] = "true"
    os.environ.setdefault("LANCE_INDEX_CACHE_SIZE", "1g")

    import lance  # noqa: E402

    storage_options = _storage_options()

    existing_ds, existing_rows = _existing_dataset(LANCE_URI, storage_options)
    if existing_ds is not None and existing_rows >= MIN_ROW_FLOOR:
        logger.info(
            "idempotent resume: existing dataset has %d rows (>= floor %d) - "
            "skipping write, will (re-)build BTREE only",
            existing_rows, MIN_ROW_FLOOR,
        )
        rows_src = existing_rows
        ds = existing_ds
        write_dur = 0.0
        skipped_write = True
    else:
        con = _connect_duckdb()
        rows_src = con.execute(
            f"SELECT COUNT(*) FROM read_parquet('{PARQUET_URI}')"
        ).fetchone()[0]
        logger.info("source parquet rows: %d (floor %d)", rows_src, MIN_ROW_FLOOR)
        if rows_src < MIN_ROW_FLOOR:
            msg = f"FAIL: source row count {rows_src} below floor {MIN_ROW_FLOOR}"
            logger.error(msg)
            return {"status": "failed", "error": msg, "rows": rows_src}

        t0 = time.time()
        with lance_commit_lock(DATASET_SLUG):
            reader = con.execute(
                f"""
                SELECT
                    event_doc_number,
                    event_seq_number,
                    event_code,
                    event_desc,
                    event_effective_date,
                    event_filed_date,
                    event_note_1,
                    event_note_2,
                    event_note_3,
                    event_cons_mer_number,
                    event_cor_name,
                    event_name_seq,
                    event_x_name_seq,
                    event_name_chg,
                    event_x_name_chg,
                    event_address1,
                    event_address2,
                    event_city,
                    event_state,
                    event_zip,
                    event_mail_address1,
                    event_mail_address2,
                    event_mail_city,
                    event_mail_state,
                    event_mail_zip
                FROM read_parquet('{PARQUET_URI}')
                """
            ).to_arrow_reader(batch_size=100_000)

            logger.info("writing Lance dataset to %s ...", LANCE_URI)
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
        skipped_write = False

    # BTREE indices
    t_btree = time.time()
    existing_btree = _existing_btree_columns(ds)
    logger.info("existing BTREE columns: %s", sorted(existing_btree))

    if "event_doc_number" not in existing_btree:
        try:
            ds.create_scalar_index("event_doc_number", index_type="BTREE", replace=True)
            logger.info("BTREE on event_doc_number: OK")
        except Exception as e:
            logger.error("BTREE on event_doc_number FAILED: %s", e)
            raise
    else:
        logger.info("BTREE on event_doc_number already present - skipping")

    if "event_effective_date" not in existing_btree:
        try:
            ds.create_scalar_index("event_effective_date", index_type="BTREE", replace=True)
            logger.info("BTREE on event_effective_date: OK")
        except Exception as e:
            logger.error("BTREE on event_effective_date FAILED: %s", e)
            raise
    else:
        logger.info("BTREE on event_effective_date already present - skipping")

    try:
        ds.optimize.compact_files()
        ds.cleanup_old_versions(older_than=timedelta(days=7))
    except Exception as e:
        logger.warning("Optimize failed (non-fatal): %s", e)

    lance_count = ds.count_rows()
    btree_dur = time.time() - t_btree
    logger.info(
        "s4 complete: %d rows, write=%.1fs btree=%.1fs (skipped_write=%s)",
        lance_count, write_dur, btree_dur, skipped_write,
    )
    return {
        "status": "succeeded",
        "rows_src": rows_src,
        "rows_lance": lance_count,
        "lance_uri": LANCE_URI,
        "write_duration_s": round(write_dur, 1),
        "btree_duration_s": round(btree_dur, 1),
        "skipped_write": skipped_write,
    }


@app.local_entrypoint()
def run() -> None:
    """`modal run scripts/run_fl_sunbiz_events_lance_emit.py::run`"""
    import json
    out = emit.remote()
    print(json.dumps(out, indent=2, default=str))

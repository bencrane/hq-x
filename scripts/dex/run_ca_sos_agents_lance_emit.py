"""s4 - CA SoS agents Lance emit (Pattern A, Modal-hosted).

Cadence — **Operator-Only Bulk Run (Quarterly Batch).** State SoS pipelines
(CA / FL / NY / CO) are retired from automated schedules per the 2026-05-25
operational policy shift. Trigger manually, point-in-time. See
``apps/data-engine-x/modal/INDEX.md`` §"State SoS pipelines".

Reads s1's ZSTD Parquet at
    s3://dex-raw-landing-zone/sos-ca/release=2026-05-16/agents/data.parquet
and writes a Lance dataset at
    s3://dex-raw-landing-zone/polaris-warehouse/sos/ca_agents_lance

BTREE indices on entity_num + entity_name_normalized.

Normalizer (validator p1 - PR #459/#460 root cause):
  ONLY scripts._lib.entity_name_normalize.normalize_entity_name. NEVER any
  other normalizer module.

Modal hosting (validator p3): @app.function(cpu=8, memory=16384, timeout=7200).
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

import modal

# Marker — the actual import lives inside emit() scoped to Modal:
#   from scripts._lib.entity_name_normalize import normalize_entity_name
# ---------------------------------------------------------------------------
# Modal app + image
# ---------------------------------------------------------------------------

app = modal.App("data-engine-x-ca-sos-agents-lance-emit")

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

# ---------------------------------------------------------------------------
# Constants (load-bearing — match harness greps)
# ---------------------------------------------------------------------------

DATASET_SLUG = "ca_sos_agents_lance"
PARQUET_URI = (
    "r2://dex-raw-landing-zone/sos-ca/release=2026-05-16/agents/data.parquet"
)
LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sos/ca_agents_lance"

# 4.4M raw rows per validator probe; floor at 90%.
MIN_ROW_FLOOR = 4_000_000

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


@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=7200,
    memory=16384,
    cpu=8,
)
def emit() -> dict:
    """Pattern A Lance emit: read s1 agents Parquet, write Lance + BTREE."""
    sys.path.insert(0, "/root")
    from scripts._lib.entity_name_normalize import normalize_entity_name  # noqa: F401
    from scripts._lib.lance_commit_lock import lance_commit_lock

    _bridge_database_url()
    os.environ["TMPDIR"] = "/tmp/lance"
    Path("/tmp/lance").mkdir(parents=True, exist_ok=True)

    import lance  # noqa: E402

    con = _connect_duckdb()

    rows_src = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('{PARQUET_URI}')"
    ).fetchone()[0]
    logger.info("source parquet rows: %d (floor %d)", rows_src, MIN_ROW_FLOOR)
    if rows_src < MIN_ROW_FLOOR:
        msg = f"FAIL: source row count {rows_src} below floor {MIN_ROW_FLOOR}"
        logger.error(msg)
        return {"status": "failed", "error": msg, "rows": rows_src}

    storage_options = _storage_options()

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        reader = con.execute(
            f"""
            SELECT
                entity_num,
                entity_name_normalized,
                agent_type,
                org_name,
                org_name_normalized,
                first_name,
                middle_name,
                last_name,
                physical_address1,
                physical_address2,
                physical_address3,
                physical_city,
                physical_state,
                physical_country,
                physical_postal_code
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

        os.environ.setdefault("LANCE_INDEX_CACHE_SIZE", "1g")
        try:
            ds.create_scalar_index("entity_num", index_type="BTREE", replace=True)
            logger.info("BTREE on entity_num: OK")
        except Exception as e:
            logger.error("BTREE on entity_num FAILED: %s", e)
            raise
        try:
            ds.create_scalar_index("entity_name_normalized", index_type="BTREE", replace=True)
            logger.info("BTREE on entity_name_normalized: OK")
        except Exception as e:
            logger.error("BTREE on entity_name_normalized FAILED: %s", e)
            raise

        try:
            ds.optimize.compact_files()
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as e:
            logger.warning("Optimize failed (non-fatal): %s", e)

    logger.info(
        "s4 complete: %d rows, %.1fs", lance_count, time.time() - t0
    )
    return {
        "status": "succeeded",
        "rows_src": rows_src,
        "rows_lance": lance_count,
        "lance_uri": LANCE_URI,
        "duration_s": round(time.time() - t0, 1),
    }


@app.local_entrypoint()
def run() -> None:
    """`modal run scripts/run_ca_sos_agents_lance_emit.py::run`"""
    import json
    out = emit.remote()
    print(json.dumps(out, indent=2, default=str))

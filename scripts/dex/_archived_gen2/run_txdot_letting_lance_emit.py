"""TXDOT letting Lance emit (Pattern A, Modal-hosted).

Reads s2's ZSTD Parquet at
    r2://dex-raw-landing-zone/txstate/txdot_letting/snapshot=*/data.parquet
(latest snapshot) and writes a Lance dataset at
    s3://dex-raw-landing-zone/polaris-warehouse/txstate/txdot_letting_lance

BTREEs:
  - controlling_project_id_ccsj   (project identifier)
  - vendor_name_normalized         (computed DuckDB UDF column - L34)
  - project_actual_let_date_typed  (TRY_CAST to DATE - typed sibling per L29/L49)

Key invariants:
  L9:  applies at s2 (read_csv_auto preserves VARCHAR via all_varchar=TRUE);
       this script reads the all-VARCHAR Parquet s2 wrote. DuckDB has no
       all_varchar parameter on read_parquet (CSV reader only).
  L29/L49: TRY_CAST(project_actual_let_date AS DATE) AS project_actual_let_date_typed
           - typed sibling column for BTREE (BTREE requires typed column).
  L34: con.create_function('py_normalize_entity', normalize_entity_name, [VARCHAR], VARCHAR)
       for vendor_name_normalized computed column.
  Column-name shape: Socrata CSV-bulk export preserves the original CSV header
  (UPPERCASE with spaces and special chars), e.g. "VENDOR NAME", "BID CODE",
  "CONTROLLING PROJECT ID (CCSJ)". The SELECT clause below explicitly aliases
  each upstream column to snake_case for downstream Lance / Pattern C ergonomics.
  Audit-resolved clarification #1: standalone Modal-hosted emit (NOT LanceEmitConfig thin-wrap)

Pattern A discipline:
  - lance_commit_lock("txdot_letting_lance") around the write
  - LANCE_BYPASS_SPILLING=true + TMPDIR=/tmp/lance
  - mode="overwrite" (snapshot substrate - latest snapshot overwrites prior)
  - compact_files() + cleanup_old_versions(timedelta(days=7))

Modal hosting: @app.function(cpu=8, memory=32768, timeout=7200).

Run via:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run --detach scripts/run_txdot_letting_lance_emit.py::run
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

import modal

# ---------------------------------------------------------------------------
# Modal app + image
# ---------------------------------------------------------------------------

app = modal.App("data-engine-x-txdot-letting-lance-emit")

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
# Constants (load-bearing - match harness greps)
# ---------------------------------------------------------------------------

DATASET_SLUG = "txdot_letting_lance"
R2_BUCKET = "dex-raw-landing-zone"
PARQUET_INPUT_PREFIX = "txstate/txdot_letting"
PARQUET_FILE_PATTERN = "data.parquet"
LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/txstate/txdot_letting_lance"

# Volume floor - live Socrata count 1,028,898 (2026-05-18). Floor at ~88%.
MIN_ROW_FLOOR = 900_000

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
    """Glob for snapshot=YYYY-MM-DD partitions and return the most-recent date string."""
    prefix_uri = f"r2://{R2_BUCKET}/{PARQUET_INPUT_PREFIX}/snapshot=*/{PARQUET_FILE_PATTERN}"
    rows = con.execute(
        f"SELECT DISTINCT regexp_extract(filename, 'snapshot=([^/]+)', 1) AS snap "
        f"FROM read_parquet('{prefix_uri}', filename=true) "
        f"ORDER BY snap DESC LIMIT 1"
    ).fetchall()
    if not rows or not rows[0][0]:
        raise RuntimeError(
            f"No snapshot partitions found under r2://{R2_BUCKET}/{PARQUET_INPUT_PREFIX}/"
        )
    return rows[0][0]


@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=7200,
    memory=32768,
    cpu=8,
)
def emit() -> dict:
    """Pattern A Lance emit: read latest TXDOT Parquet snapshot, write Lance + BTREE.

    Key column notes:
    - vendor_name_normalized: computed via DuckDB UDF py_normalize_entity (L34)
    - project_actual_let_date_typed: TRY_CAST to DATE (L29/L49); BTREE on typed sibling
    - source columns preserved as VARCHAR per s2 ingest (L9 at CSV read step)
    """
    sys.path.insert(0, "/root")
    from scripts._lib.lance_commit_lock import lance_commit_lock
    from scripts._lib.entity_name_normalize import (
        normalize_entity_name,
        __version__ as NORMALIZER_VERSION,
    )

    _bridge_database_url()

    os.environ["TMPDIR"] = "/tmp/lance"
    Path("/tmp/lance").mkdir(parents=True, exist_ok=True)
    # BTREE on 1M+ rows hits DataFusion's sort spill pool budget.
    # Documented fix: bypass spilling and rely on container memory (32GB above).
    os.environ["LANCE_BYPASS_SPILLING"] = "true"
    os.environ.setdefault("LANCE_INDEX_CACHE_SIZE", "1g")

    import lance  # noqa: E402

    storage_options = _storage_options()
    con = _connect_duckdb_to_r2()

    # Register Python normalizer as DuckDB UDF (L34).
    # null_handling="special" ensures None inputs return None without error.
    con.create_function(
        "py_normalize_entity",
        normalize_entity_name,
        ["VARCHAR"],
        "VARCHAR",
        null_handling="special",
    )

    snapshot = _detect_latest_snapshot(con)
    input_uri = (
        f"r2://{R2_BUCKET}/{PARQUET_INPUT_PREFIX}/snapshot={snapshot}/{PARQUET_FILE_PATTERN}"
    )
    logger.info("latest snapshot: %s, reading from %s", snapshot, input_uri)

    rows_src = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('{input_uri}')"
    ).fetchone()[0]
    logger.info("source parquet rows: %d (floor %d)", rows_src, MIN_ROW_FLOOR)
    if rows_src < MIN_ROW_FLOOR:
        msg = f"FAIL: source row count {rows_src} below floor {MIN_ROW_FLOOR}"
        logger.error(msg)
        return {"status": "failed", "error": msg, "rows": rows_src}

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        # Socrata CSV-bulk export preserves the original CSV header (UPPERCASE
        # with spaces), so we explicitly alias to snake_case at the read step.
        # The Parquet here is already all-VARCHAR per s2's CSV ingest path
        # (L9 applies at the s2 read_csv_auto step, not here - read_parquet
        # has no all_varchar param in DuckDB).
        # L29/L49: TRY_CAST to DATE for BTREE-bearing typed sibling column.
        # L34: py_normalize_entity is the DuckDB UDF wrapping normalize_entity_name.
        reader = con.execute(
            f"""
            SELECT
                "A+B ENGINEER'S ESTIMATE AMOUNT"      AS a_plus_b_engineers_estimate_amount,
                "ALTERNATIVE BID CODE"                AS alternative_bid_code,
                "BID CODE"                            AS bid_code,
                "BID ITEM DESCRIPTION"                AS bid_item_description,
                "BID ITEM QUANTITY"                   AS bid_item_quantity,
                "BID ITEM SEQUENCE NUMBER"            AS bid_item_sequence_number,
                "BID ITEM UNIT PRICE AMOUNT"          AS bid_item_unit_price_amount,
                "BID RANK SEQUENCE NUMBER"            AS bid_rank_sequence_number,
                "BID SUBMIT SEQUENCE NUMBER"          AS bid_submit_sequence_number,
                "BID TOTAL AMOUNT"                    AS bid_total_amount,
                "CONTROL SECTION JOB (CSJ)"           AS control_section_job_csj,
                "CONTROLLING PROJECT ID (CCSJ)"       AS controlling_project_id_ccsj,
                "COUNTY"                              AS county,
                "DBE GOAL PERCENT"                    AS dbe_goal_percent,
                "DISTRICT/ DIVISION"                  AS district_division,
                "ENGINEER'S ESTIMATE UNIT PRICE"      AS engineers_estimate_unit_price,
                "FEDERAL PROJECT NUMBER"              AS federal_project_number,
                "FORCE ACCOUNT AMOUNT"                AS force_account_amount,
                "FORCE ACCOUNT DESCRIPTION"           AS force_account_description,
                "HIGHWAY"                             AS highway,
                "LOW BIDDER FLAG"                     AS low_bidder_flag,
                "MAXIMUM NUMBER OF WORKING DAYS"      AS maximum_number_of_working_days,
                "MEASUREMENT UNIT"                    AS measurement_unit,
                "NBI NUMBER"                          AS nbi_number,
                "PROJECT ACTUAL LET DATE"             AS project_actual_let_date,
                "PROJECT CLASSIFICATION"              AS project_classification,
                "PROJECT ESTIMATED LET DATE"          AS project_estimated_let_date,
                "PROJECT ID"                          AS project_id,
                "PROJECT LIMITS FROM"                 AS project_limits_from,
                "PROJECT LIMITS TO"                   AS project_limits_to,
                "PROJECT NAME"                        AS project_name,
                "PROJECT NUMBER"                      AS project_number,
                "PROJECT TYPE"                        AS project_type,
                "SEALED ENGINEER'S ESTIMATE CONTRACT" AS sealed_engineers_estimate_contract,
                "SEALED ENGINEER'S ESTIMATE PROJECT"  AS sealed_engineers_estimate_project,
                "SHORT DESCRIPTION"                   AS short_description,
                "SPEC BOOK YEAR"                      AS spec_book_year,
                "SPECIFICATION CODE"                  AS specification_code,
                "STATE PROJECT NUMBER"                AS state_project_number,
                "SPECIFICATION DESCRIPTION"           AS specification_description,
                "UTILITY ID"                          AS utility_id,
                "VENDOR NAME"                         AS vendor_name,
                py_normalize_entity("VENDOR NAME")    AS vendor_name_normalized,
                TRY_CAST("PROJECT ACTUAL LET DATE" AS DATE) AS project_actual_let_date_typed
            FROM read_parquet('{input_uri}')
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

    # 3 BTREE columns. The date BTREE uses the TYPED column name (project_actual_let_date_typed).
    # Downstream consumers query 'project_actual_let_date_typed' for date-range pushdown.
    t_btree = time.time()
    for col in (
        "controlling_project_id_ccsj",
        "vendor_name_normalized",
        "project_actual_let_date_typed",
    ):
        try:
            ds.create_scalar_index(col, index_type="BTREE", replace=True)
            logger.info("BTREE on %s: OK", col)
        except Exception as e:
            logger.error("BTREE on %s FAILED: %s", col, e)
            raise

    try:
        ds.optimize.compact_files()
        ds.cleanup_old_versions(older_than=timedelta(days=7))
    except Exception as e:
        logger.warning("Optimize failed (non-fatal): %s", e)

    lance_count = ds.count_rows()
    btree_dur = time.time() - t_btree
    logger.info(
        "emit complete: %d rows, write=%.1fs btree=%.1fs normalizer=%s",
        lance_count, write_dur, btree_dur, NORMALIZER_VERSION,
    )
    return {
        "status": "succeeded",
        "rows_src": rows_src,
        "rows_lance": lance_count,
        "lance_uri": LANCE_URI,
        "snapshot": snapshot,
        "write_duration_s": round(write_dur, 1),
        "btree_duration_s": round(btree_dur, 1),
        "normalizer_version": NORMALIZER_VERSION,
    }


@app.local_entrypoint()
def run() -> None:
    """`modal run --detach scripts/run_txdot_letting_lance_emit.py::run`"""
    import json
    out = emit.remote()
    print(json.dumps(out, indent=2, default=str))

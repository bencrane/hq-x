"""s1 — USAspending contract subawards Lance emit (Pattern A pull-through).

Reads r2://dex-raw-landing-zone/usaspending/contract_subawards/year=2026/data.parquet
(one-shot CSV-bulk ingest 2026-05-09 via scripts/run_usaspending_subawards_ingest.py;
16,879 rows per validator probe 2026-05-16) via DuckDB-on-R2 (httpfs + R2 SECRET).
TRY_CASTs 22 trap VARCHARs at write-time (L49 — *_amount AS DOUBLE,
*_date AS DATE, *_fiscal_year AS INTEGER) so downstream consumers don't re-cast.
Writes Lance to s3://dex-raw-landing-zone/polaris-warehouse/usaspending/
contract_subawards_lance with BTREE x 4: prime_award_unique_key + subaward_number
+ subawardee_uei + prime_awardee_uei (all 4 validator-confirmed present).

Floor 15,191 (0.9 x 16,879 measured).

Modal hosting: @app.function(cpu=4, memory=8192, timeout=3600). MUST be launched
via `modal run --detach` per L47 (Modal CLI disconnect kills attached jobs;
3600s timeout window > coffee-break threshold even at small dataset sizes).

Run via (DETACH IS MANDATORY):
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      modal run --detach scripts/run_usaspending_contract_subawards_lance_emit.py::run
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

import modal

app = modal.App("data-engine-x-usaspending-contract-subawards-lance-emit")

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
        Path(__file__).resolve().parent,
        remote_path="/root/scripts",
    )
)

FUNCTION_SECRETS = [
    modal.Secret.from_name("bulk-ingest-r2"),
    modal.Secret.from_name("dex-db"),
]

DATASET_SLUG = "contract_subawards_lance"
SRC_PARQUET_URI = (
    "r2://dex-raw-landing-zone/usaspending/contract_subawards/"
    "year=2026/data.parquet"
)
LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/contract_subawards_lance"
MIN_ROW_FLOOR = 15_191
BTREE_COLUMNS = [
    "prime_award_unique_key",
    "subaward_number",
    "subawardee_uei",
    "prime_awardee_uei",
    "subawardee_name_normalized",
]

# L49 — 22 trap VARCHARs in contract feed (validator probe 2026-05-16).
# Cast policy: *_amount and the IIJA/COVID supplementals → DOUBLE;
# *_date (including *_fiscal_year_date) → DATE; *_fiscal_year (integer year
# stored as string) → INTEGER. subaward_sam_report_last_modified_date is a
# date-shaped VARCHAR that occasionally carries a TIMESTAMP suffix → DATE
# is the practical cast (TIMESTAMP would also work; DATE matches the
# operator-friendly column semantic).
TRY_CAST_DOUBLE = [
    "prime_award_amount",
    "prime_award_outlayed_amount_from_covid_19_supplementals",
    "prime_award_obligated_amount_from_covid_19_supplementals",
    "prime_award_outlayed_amount_from_iija_supplemental",
    "prime_award_obligated_amount_from_iija_supplemental",
    "prime_award_total_outlayed_amount",
    "subaward_amount",
    "subawardee_highly_compensated_officer_1_amount",
    "subawardee_highly_compensated_officer_2_amount",
    "subawardee_highly_compensated_officer_3_amount",
    "subawardee_highly_compensated_officer_4_amount",
    "subawardee_highly_compensated_officer_5_amount",
]
TRY_CAST_DATE = [
    "prime_award_base_action_date",
    "prime_award_latest_action_date",
    "prime_award_period_of_performance_start_date",
    "prime_award_period_of_performance_current_end_date",
    "prime_award_period_of_performance_potential_end_date",  # contract-only
    "subaward_action_date",
    "subaward_sam_report_last_modified_date",
]
TRY_CAST_INTEGER = [
    "prime_award_base_action_date_fiscal_year",
    "prime_award_latest_action_date_fiscal_year",
    "subaward_action_date_fiscal_year",
]
# Total = 12 DOUBLE + 7 DATE + 3 INTEGER = 22. Matches validator probe.

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


def _existing_btree_columns(ds) -> set:
    cols = set()
    for idx in ds.list_indices():
        fields = idx.get("fields") if isinstance(idx, dict) else []
        itype = idx.get("type") if isinstance(idx, dict) else ""
        if "BTREE" in str(itype).upper() or "BTREE" in str(idx).upper():
            for f in (fields or []):
                cols.add(str(f))
    return cols


def _build_select_sql(parquet_uri: str) -> str:
    """Project ALL columns, with TRY_CAST applied to trap VARCHARs.

    `* EXCLUDE (...)` removes the raw VARCHAR trap columns; the explicit
    TRY_CAST aliases re-add them with the same names + corrected types.

    Example resolved SQL fragment (documentation-only; not used at runtime):

        SELECT
            * EXCLUDE (prime_award_amount, ...),
            TRY_CAST(prime_award_amount AS DOUBLE) AS prime_award_amount,
            TRY_CAST(prime_award_outlayed_amount_from_covid_19_supplementals AS DOUBLE)
                AS prime_award_outlayed_amount_from_covid_19_supplementals,
            TRY_CAST(prime_award_total_outlayed_amount AS DOUBLE)
                AS prime_award_total_outlayed_amount,
            TRY_CAST(subaward_amount AS DOUBLE) AS subaward_amount,
            TRY_CAST(subawardee_highly_compensated_officer_1_amount AS DOUBLE)
                AS subawardee_highly_compensated_officer_1_amount,
            TRY_CAST(prime_award_base_action_date AS DATE)
                AS prime_award_base_action_date,
            TRY_CAST(prime_award_period_of_performance_current_end_date AS DATE)
                AS prime_award_period_of_performance_current_end_date,
            TRY_CAST(prime_award_period_of_performance_potential_end_date AS DATE)
                AS prime_award_period_of_performance_potential_end_date,
            TRY_CAST(subaward_action_date AS DATE) AS subaward_action_date,
            TRY_CAST(subaward_sam_report_last_modified_date AS DATE)
                AS subaward_sam_report_last_modified_date,
            TRY_CAST(prime_award_base_action_date_fiscal_year AS INTEGER)
                AS prime_award_base_action_date_fiscal_year,
            ...
        FROM read_parquet('{parquet_uri}')
    """
    trap_all = TRY_CAST_DOUBLE + TRY_CAST_DATE + TRY_CAST_INTEGER
    exclude_list = ", ".join(trap_all)
    cast_lines = []
    for col in TRY_CAST_DOUBLE:
        cast_lines.append(f"TRY_CAST({col} AS DOUBLE) AS {col}")
    for col in TRY_CAST_DATE:
        cast_lines.append(f"TRY_CAST({col} AS DATE) AS {col}")
    for col in TRY_CAST_INTEGER:
        cast_lines.append(f"TRY_CAST({col} AS INTEGER) AS {col}")
    cast_clause = ",\n            ".join(cast_lines)
    return f"""
        SELECT
            * EXCLUDE ({exclude_list}),
            {cast_clause}
        FROM read_parquet('{parquet_uri}')
    """


@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=3600,
    memory=8192,
    cpu=4,
)
def emit() -> dict:
    sys.path.insert(0, "/root")
    from scripts._lib.lance_commit_lock import lance_commit_lock

    os.environ["TMPDIR"] = "/tmp/lance"
    Path("/tmp/lance").mkdir(parents=True, exist_ok=True)
    os.environ["LANCE_BYPASS_SPILLING"] = "true"

    import lance

    storage_options = _storage_options()
    con = _connect_duckdb()
    select_sql = _build_select_sql(SRC_PARQUET_URI)

    src_rows = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('{SRC_PARQUET_URI}')"
    ).fetchone()[0]
    logger.info("source parquet rows: %d", src_rows)

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        reader = con.execute(select_sql).to_arrow_reader(batch_size=100_000)
        logger.info("writing Lance dataset to %s ...", LANCE_URI)
        ds = lance.write_dataset(
            reader,
            LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t0
        rows = ds.count_rows()
        logger.info(
            "wrote %d rows in %.1fs (version=%s)",
            rows, write_dur, ds.version,
        )

    if rows < MIN_ROW_FLOOR:
        msg = f"FAIL: row count {rows} below floor {MIN_ROW_FLOOR}"
        logger.error(msg)
        return {"status": "failed", "error": msg, "rows": rows}

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

    try:
        ds.optimize.compact_files()
        ds.cleanup_old_versions(older_than=timedelta(days=7))
    except Exception as e:
        logger.warning("Optimize failed (non-fatal): %s", e)

    btree_dur = time.time() - t_btree
    final_rows = ds.count_rows()
    return {
        "status": "succeeded",
        "rows_lance": final_rows,
        "lance_uri": LANCE_URI,
        "write_duration_s": round(write_dur, 1),
        "btree_duration_s": round(btree_dur, 1),
    }


@app.local_entrypoint()
def run() -> None:
    """`modal run --detach scripts/run_usaspending_contract_subawards_lance_emit.py::run`"""
    import json
    out = emit.remote()
    print(json.dumps(out, indent=2, default=str))

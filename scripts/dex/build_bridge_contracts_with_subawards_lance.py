"""s1 — Contracts × rolled-up subawards Pattern A enriched-cohort emit (Modal-hosted).

Reads two upstream Lance datasets (read-only consumers; no source mods):
  - usaspending/contracts_lance              (~15.5M rows transaction grain;
                                              13,143,940 distinct contract_award_unique_key)
  - usaspending/contract_subawards_lance     (16,879 rows FSRS subaward grain;
                                              4,993 distinct prime_award_unique_key)

Output Lance dataset (1 row per prime_award_unique_key):
  bridges/contracts_with_subawards_lance
  - Floor: 11,829,546 rows (0.9 × 13,143,940 validator-stamped 2026-05-16T22:45Z).
  - LEFT JOIN preserves all primes; ~99.96% have NULL subaward rollup fields
    (only 4,847 primes — 0.04% — have FSRS subaward reports).
  - BTREE on prime_award_unique_key + prime_awardee_uei + prime_naics_code.

Pattern A discipline (NOT Pattern B):
  - This is an enriched-cohort rollup at PRIME-AWARD grain, NOT a new bridge.
  - Match logic is trivial exact-equality on prime_award_unique_key (no fuzzy).
  - NO register_match_method, NO register_bridge, NO ops.bridges INSERT,
    NO ops.match_method_versions INSERT.
  - Provenance: per-emit bridge_run_id (UUID) propagated as column.

USAspending column-name drift (validator Findings 1+2 resolved by audit):
  - contracts_lance prime key is `contract_award_unique_key`; SELECT aliases
    to `prime_award_unique_key` to match subaward side.
  - 12 prime-side aliases per validator-stamped mapping (see SQL below).

TRY_CAST scope (validator Finding 5 resolved by audit):
  - Contracts side ONLY (4 cols): period_of_performance_start_date,
    period_of_performance_current_end_date, action_date (all → DATE);
    total_dollars_obligated (→ DOUBLE).
  - Subaward side: subaward_amount is already DOUBLE; subaward_action_date is
    already date32. No TRY_CAST.

Subaward NAICS DROPPED (validator Finding 3 resolved by audit):
  - FSRS captures NO subaward-specific NAICS; prime_award_naics_code is the
    PRIME's NAICS carried-through. Output omits `subaward_naics_array`.

Modal hosting: @app.function(memory=49152, timeout=14400, cpu=8).
  - 49 GiB memory: validator Finding 4 — PR #469 precedent; 13M-row Lance write
    is 52× PR #469's output, plus full-table contracts scan + ROW_NUMBER dedup.
  - 14400s (4h) timeout: PR #469 precedent for the heaviest emit class.

MUST be launched via `modal run --detach` per CLAUDE.md L47.

Run via (DETACH IS MANDATORY):
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      modal run --detach scripts/build_bridge_contracts_with_subawards_lance.py::run
"""
from __future__ import annotations

import logging
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import modal

app = modal.App("data-engine-x-contracts-with-subawards-lance-emit")

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

DATASET_SLUG = "contracts_with_subawards_lance"

CONTRACTS_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/contracts_lance"
)
CONTRACT_SUBAWARDS_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/contract_subawards_lance"
)
OUTPUT_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/contracts_with_subawards_lance"
)

# Validator-stamped floor (2026-05-16T22:45Z probe):
# 0.9 × 13,143,940 distinct contract_award_unique_key in usaspending.contracts_lance.
MIN_ROW_FLOOR = 11_829_546

BRIDGE_VERSION = "1.0.0"

BTREE_COLUMNS = [
    "prime_award_unique_key",
    "prime_awardee_uei",
    "prime_naics_code",
]

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
    timeout=14400,    # 4h — PR #469 precedent for heaviest emit class
    memory=49152,    # 49 GiB — 13M-row Lance write + ROW_NUMBER dedup over 15.5M
    cpu=8,
)
def emit() -> dict:
    """Pattern A enriched-cohort emit at prime_award_unique_key grain.

    Match logic is trivial exact-equality on prime_award_unique_key (no bridge
    or method registration). USAspending contracts-side TRY_CAST per CLAUDE.md
    L49 + validator probe Finding 5 (4 cols: 3 dates + 1 double).
    """
    sys.path.insert(0, "/root")
    from scripts._lib.lance_commit_lock import lance_commit_lock

    os.environ["TMPDIR"] = "/tmp/lance"
    Path("/tmp/lance").mkdir(parents=True, exist_ok=True)
    # DataFusion external-sort spill OOMs on multi-million-row string BTREEs.
    os.environ["LANCE_BYPASS_SPILLING"] = "true"
    os.environ.setdefault("LANCE_INDEX_CACHE_SIZE", "1g")

    import lance  # noqa: E402

    storage_options = _storage_options()

    # Per-emit provenance.
    emit_bridge_run_id = str(uuid.uuid4())
    generated_at_dt = datetime.now(tz=timezone.utc)
    generated_at_iso = generated_at_dt.isoformat()
    logger.info(
        "emit %s starting at %s (output=%s)",
        emit_bridge_run_id, generated_at_iso, OUTPUT_LANCE_URI,
    )

    # ---- Step 1: open both upstream Lance datasets via PyLance scanners ---- #
    # Reviewer-fixed (2026-05-16T23:30Z, Cycle B): replaced audit's
    # `read_parquet('s3://.../*.parquet')` with PyLance scanner + Arrow register
    # — Lance datasets store fragments as `.lance` files (NOT `.parquet`), so
    # the glob would match zero files. PR #469 precedent (scripts/build_bridge_
    # sam_pdl_usaspending_lance.py:200-286) is the only correct pattern.
    logger.info("opening usaspending/contracts_lance (project 13 cols) ...")
    contracts_ds = lance.dataset(CONTRACTS_LANCE_URI, storage_options=storage_options)
    logger.info("  contracts_lance: %d rows", contracts_ds.count_rows())
    contracts_cols = [
        "contract_award_unique_key",
        "recipient_uei",
        "recipient_name",
        "naics_code",
        "naics_description",
        "period_of_performance_start_date",
        "period_of_performance_current_end_date",
        "total_dollars_obligated",
        "action_date",
        "awarding_agency_name",
        "awarding_sub_agency_name",
        "primary_place_of_performance_state_code",
        "primary_place_of_performance_county_name",
    ]
    contracts_arrow = contracts_ds.scanner(
        columns=contracts_cols,
        filter="contract_award_unique_key IS NOT NULL AND contract_award_unique_key != ''",
    ).to_table()
    logger.info(
        "  contracts_lance (non-null contract_award_unique_key): %d rows x %d cols",
        contracts_arrow.num_rows, len(contracts_cols),
    )

    logger.info("opening usaspending/contract_subawards_lance (project 6 cols) ...")
    subawards_ds = lance.dataset(CONTRACT_SUBAWARDS_LANCE_URI, storage_options=storage_options)
    logger.info("  contract_subawards_lance: %d rows", subawards_ds.count_rows())
    subawards_cols = [
        "prime_award_unique_key",
        "subaward_amount",
        "subaward_action_date",
        "subawardee_uei",
        "subawardee_name",
        "subaward_primary_place_of_performance_state_code",
    ]
    subawards_arrow = subawards_ds.scanner(
        columns=subawards_cols,
        filter="prime_award_unique_key IS NOT NULL AND prime_award_unique_key != ''",
    ).to_table()
    logger.info(
        "  contract_subawards_lance (non-null prime_award_unique_key): %d rows x %d cols",
        subawards_arrow.num_rows, len(subawards_cols),
    )

    # ---- Step 2: build DuckDB plan over both via Arrow register ---- #
    # PR #469 precedent: PyLance scanner emits Arrow; DuckDB consumes via
    # con.register(). DO NOT use read_parquet() — Lance datasets are .lance
    # files (NOT parquet), and read_parquet would also bypass Lance deletion
    # vectors + version manifests. (Note: DuckDB *does* support Postgres-style
    # DISTINCT ON since v0.9, but ROW_NUMBER + WHERE rn=1 is functionally
    # equivalent and is the audit-frozen pattern.)
    con = _connect_duckdb()
    con.register("contracts_proj", contracts_arrow)
    con.register("subawards_proj", subawards_arrow)

    # 2a. contracts side: dedup latest-action-date per prime + alias 12 cols.
    # Drift Finding 1: contract_award_unique_key → prime_award_unique_key.
    # Drift Finding 2: 12 prime-side aliases per validator mapping.
    # Drift Finding 5: TRY_CAST only on dates + total_dollars_obligated.
    logger.info("step 2a: contracts_lance — latest action per prime + 12 aliases ...")
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE contracts_latest_per_prime AS
        WITH ranked AS (
            SELECT
                contract_award_unique_key                              AS prime_award_unique_key,
                recipient_uei                                          AS prime_awardee_uei,
                recipient_name                                         AS prime_awardee_name,
                naics_code                                             AS prime_naics_code,
                naics_description                                      AS prime_naics_description,
                TRY_CAST(period_of_performance_start_date AS DATE)     AS prime_period_of_performance_start_date,
                TRY_CAST(period_of_performance_current_end_date AS DATE) AS prime_period_of_performance_current_end_date,
                TRY_CAST(total_dollars_obligated AS DOUBLE)            AS prime_total_dollars_obligated,
                TRY_CAST(action_date AS DATE)                          AS prime_action_date_latest,
                awarding_agency_name                                   AS prime_awarding_agency_name,
                awarding_sub_agency_name                               AS prime_awarding_sub_agency_name,
                primary_place_of_performance_state_code                AS prime_place_of_performance_state_code,
                primary_place_of_performance_county_name               AS prime_place_of_performance_county_name,
                ROW_NUMBER() OVER (
                    PARTITION BY contract_award_unique_key
                    ORDER BY TRY_CAST(action_date AS DATE) DESC NULLS LAST
                )                                                      AS rn
            FROM contracts_proj
        )
        SELECT * EXCLUDE (rn) FROM ranked WHERE rn = 1
        """
    )
    prime_rows = con.execute("SELECT COUNT(*) FROM contracts_latest_per_prime").fetchone()[0]
    logger.info("  contracts_latest_per_prime: %d rows (one per prime_award_unique_key)", prime_rows)

    # 2b. subaward rollup: GROUP BY prime_award_unique_key.
    # Drift Finding 5: subaward_amount/subaward_action_date already typed; no TRY_CAST.
    # Drift Finding 3: subaward_naics_array DROPPED (FSRS carries no subaward NAICS).
    logger.info("step 2b: contract_subawards_lance — rollup per prime_award_unique_key ...")
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE subaward_rollup AS
        SELECT
            prime_award_unique_key,
            COUNT(*)                                  AS subaward_count,
            SUM(subaward_amount)                      AS total_subaward_amount,
            MIN(subaward_action_date)                 AS earliest_subaward_action_date,
            MAX(subaward_action_date)                 AS latest_subaward_action_date,
            array_agg(DISTINCT subawardee_uei)        AS subawardee_uei_array,
            array_agg(DISTINCT subawardee_name)       AS subawardee_name_array,
            array_agg(DISTINCT subaward_primary_place_of_performance_state_code) AS subaward_state_array
        FROM subawards_proj
        GROUP BY prime_award_unique_key
        """
    )
    rollup_rows = con.execute("SELECT COUNT(*) FROM subaward_rollup").fetchone()[0]
    logger.info("  subaward_rollup: %d distinct primes with subaward reports", rollup_rows)

    # 2c. LEFT JOIN + emit provenance.
    logger.info("step 2c: LEFT JOIN contracts_latest_per_prime × subaward_rollup ...")
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE bridge_out AS
        SELECT
            c.*,
            sr.subaward_count,
            sr.total_subaward_amount,
            sr.earliest_subaward_action_date,
            sr.latest_subaward_action_date,
            sr.subawardee_uei_array,
            sr.subawardee_name_array,
            sr.subaward_state_array,
            CAST('{emit_bridge_run_id}' AS VARCHAR)             AS bridge_run_id,
            TIMESTAMP '{generated_at_iso}'                      AS generated_at,
            '{BRIDGE_VERSION}'                                  AS bridge_version
        FROM contracts_latest_per_prime c
        LEFT JOIN subaward_rollup sr
               ON sr.prime_award_unique_key = c.prime_award_unique_key
        """
    )

    forensic_counts = con.execute(
        """
        SELECT
            COUNT(*)                                              AS rows_out,
            COUNT(*) FILTER (WHERE subaward_count IS NOT NULL)    AS rows_with_subaward_history,
            COUNT(*) FILTER (WHERE prime_naics_code = '23')       AS rows_construction_naics_23
        FROM bridge_out
        """
    ).fetchone()
    logger.info(
        "forensic counts: rows_out=%d with_subaward_history=%d construction(NAICS 23)=%d",
        forensic_counts[0], forensic_counts[1], forensic_counts[2],
    )

    # Forensic count for the 146-prime shortfall flagged in validator probe:
    # subaward primes NOT joinable back into contracts_lance.
    shortfall = con.execute(
        """
        SELECT COUNT(*) FROM subaward_rollup sr
        WHERE NOT EXISTS (
            SELECT 1 FROM contracts_latest_per_prime c
             WHERE c.prime_award_unique_key = sr.prime_award_unique_key
        )
        """
    ).fetchone()[0]
    logger.info(
        "forensic: subaward primes NOT in contracts_latest_per_prime = %d "
        "(validator predicted ~146; FSRS aging or parent IDV keys)",
        shortfall,
    )

    # ---- Step 3: Lance write inside commit_lock; batch_size=100_000 ---- #
    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        reader = con.execute("SELECT * FROM bridge_out").to_arrow_reader(
            batch_size=100_000,
        )

        logger.info("writing Lance dataset to %s ...", OUTPUT_LANCE_URI)
        ds = lance.write_dataset(
            reader,
            OUTPUT_LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t0
        rows = ds.count_rows()
        logger.info(
            "wrote %d rows in %.1fs (version=%s)",
            rows, write_dur, ds.version,
        )

    # ---- Step 4: floor gate ---- #
    if rows < MIN_ROW_FLOOR:
        msg = f"FAIL: row count {rows} below floor {MIN_ROW_FLOOR}"
        logger.error(msg)
        return {"status": "failed", "error": msg, "rows": rows}

    # ---- Step 5: BTREE × 3 ---- #
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
        except Exception as e:  # noqa: BLE001
            logger.error("BTREE on %s FAILED: %s", col, e)
            raise

    # ---- Step 6: compact + cleanup_old_versions(timedelta(days=7)) ---- #
    try:
        ds.optimize.compact_files()
        ds.cleanup_old_versions(older_than=timedelta(days=7))
    except Exception as e:  # noqa: BLE001
        logger.warning("Optimize failed (non-fatal): %s", e)

    btree_dur = time.time() - t_btree
    final_rows = ds.count_rows()
    logger.info(
        "s1 complete: %d rows, write=%.1fs btree=%.1fs",
        final_rows, write_dur, btree_dur,
    )
    return {
        "status": "succeeded",
        "rows_lance": final_rows,
        "lance_uri": OUTPUT_LANCE_URI,
        "bridge_run_id": emit_bridge_run_id,
        "write_duration_s": round(write_dur, 1),
        "btree_duration_s": round(btree_dur, 1),
        "rows_with_subaward_history": forensic_counts[1],
        "rows_construction_naics_23": forensic_counts[2],
        "shortfall_subaward_primes_not_in_contracts": shortfall,
    }


@app.local_entrypoint()
def run() -> None:
    """`modal run --detach scripts/build_bridge_contracts_with_subawards_lance.py::run`

    DETACH IS MANDATORY (CLAUDE.md L47 — Modal CLI disconnect kills attached
    jobs over a 4h window).
    """
    import json
    out = emit.remote()
    print(json.dumps(out, indent=2, default=str))

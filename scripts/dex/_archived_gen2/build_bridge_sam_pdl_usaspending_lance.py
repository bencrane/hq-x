"""s1 - SAM x PDL x USAspending 3-way Pattern A enriched-cohort emit (Modal-hosted).

Reads four upstream Lance datasets via PyLance scanners (no full materialization
of USAspending; scanner stream into DuckDB Arrow-bridge) and writes a 3-way
roll-up at entity grain (1 row per UEI in the SAM x PDL intersection):

  - sam_gov/entities_lance                 (884,203 entities; project ~18 cols)
  - bridges/sam_pdl_domain_lance           (275,418 distinct UEIs; full read)
  - pdl/free_companies_lance               (project pdl_id + 7 enrichment cols)
  - usaspending/contracts_lance            (15.5M rows; project 5 cols, group by recipient_uei)

Pattern A discipline (NOT Pattern B):
  - This is an enriched-cohort rollup, NOT a new cross-source identity bridge.
  - Match logic was already settled by sam_pdl_domain_lance (Pattern B, PR #436).
  - This script does NOT register any new bridge or match method, and does NOT
    INSERT or UPDATE any rows in ops.bridges / ops.match_method_versions.
  - Provenance: per-row sam_pdl_bridge_run_id (inherited from sam_pdl_domain_lance)
    + this emit's bridge_run_id UUID propagated as a column.

USAspending VARCHAR type traps (validator p2):
  - period_of_performance_current_end_date is VARCHAR -> TRY_CAST AS DATE
  - action_date is VARCHAR (ISO-8601) -> TRY_CAST AS DATE for window filter
  - federal_action_obligation is VARCHAR -> TRY_CAST AS DOUBLE for SUM
  - total_dollars_obligated is VARCHAR -> TRY_CAST AS DOUBLE for SUM
  - Precedent: run_govcontract_match_subset.py:113,117,166.

Output Lance dataset: polaris-warehouse/bridges/sam_pdl_usaspending_lance
  - 1 row per UEI in SAM x PDL intersection (LEFT JOIN USAspending rollup;
    NULL roll-up fields for ~81% of UEIs without contract history).
  - BTREE indices on: uei, naics_primary_2digit, legal_business_name_normalized.
  - Floor: 247,876 rows (0.9 x 275,418 distinct SAM x PDL UEIs; baseline probe
    2026-05-16T19:55Z).

Modal hosting: @app.function(memory=49152, timeout=14400, cpu=8).
  - 49 GiB memory floor: 15.5M-row USAspending GROUP BY + 275K JOIN spine.
  - 14400s (4h) timeout: realistic upper bound for the heaviest emit.

MUST be launched via `modal run --detach` per FL-cycle root-cause lesson
(Modal CLI disconnect kills attached jobs; a 4h foreground attach reliably
disconnects on laptop sleep, ssh-tunnel timeout, or wifi flap).

Run via (DETACH IS MANDATORY):
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      modal run --detach scripts/build_bridge_sam_pdl_usaspending_lance.py::run
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

app = modal.App("data-engine-x-sam-pdl-usaspending-bridge-lance-emit")

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

DATASET_SLUG = "sam_pdl_usaspending_lance"

SAM_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/entities_lance"
)
PDL_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/pdl/free_companies_lance"
)
SAM_PDL_BRIDGE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sam_pdl_domain_lance"
)
USASPENDING_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/contracts_lance"
)
OUTPUT_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sam_pdl_usaspending_lance"
)

# Validator floor (2026-05-16T19:55Z probe): 0.9 x 275,418 distinct SAM x PDL UEIs.
MIN_ROW_FLOOR = 247_876

BRIDGE_VERSION = "1.0.0"

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
    # The DuckDB session does the heaviest work on the 15.5M-row USAspending
    # GROUP BY; give it room within the 49 GiB Modal container.
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
    timeout=14400,    # 4h — heaviest emit (15.5M-row USAspending GROUP BY)
    memory=49152,    # 49 GiB — DuckDB GROUP BY headroom + 275K JOIN spine
    cpu=8,
)
def emit() -> dict:
    """Pattern A 3-way enriched-cohort emit.

    Match logic inherited from sam_pdl_domain_lance (Pattern B, PR #436); this
    function performs no bridge registration and no match-method registration.
    USAspending VARCHAR fields wrapped in TRY_CAST (validator p2; precedent
    run_govcontract_match_subset.py:113).
    """
    sys.path.insert(0, "/root")
    from scripts._lib.lance_commit_lock import lance_commit_lock

    os.environ["TMPDIR"] = "/tmp/lance"
    Path("/tmp/lance").mkdir(parents=True, exist_ok=True)
    # DataFusion external-sort spill OOMs on multi-million-row string BTREEs.
    os.environ["LANCE_BYPASS_SPILLING"] = "true"
    os.environ.setdefault("LANCE_INDEX_CACHE_SIZE", "1g")

    import lance  # noqa: E402
    import pyarrow.compute as pc  # noqa: E402

    storage_options = _storage_options()

    # Per-emit provenance.
    emit_bridge_run_id = str(uuid.uuid4())
    generated_at_dt = datetime.now(tz=timezone.utc)
    generated_at_iso = generated_at_dt.isoformat()
    logger.info(
        "emit %s starting at %s (output=%s)",
        emit_bridge_run_id, generated_at_iso, OUTPUT_LANCE_URI,
    )

    # ---- Step 1: load upstream Lance datasets via PyLance scanners ---- #
    logger.info("opening sam_gov/entities_lance ...")
    sam_ds = lance.dataset(SAM_LANCE_URI, storage_options=storage_options)
    sam_cols = [
        "unique_entity_id",
        "legal_business_name",
        "legal_business_name_normalized",
        "dba_name",
        "primary_naics",
        "naics_primary_2digit",
        "naics_code_string",
        "entity_url",
        "entity_structure",
        "state_of_incorporation",
        "physical_address_city",
        "physical_address_state_normalized",
        "physical_address_zip5",
        "cage_code",
        "registration_expiration_date",
        "last_update_date",
        "bus_type_string",
        "sba_business_types_string",
    ]
    available_sam = {f.name for f in sam_ds.schema}
    sam_scan_cols = [c for c in sam_cols if c in available_sam]
    sam_arrow = sam_ds.scanner(
        columns=sam_scan_cols,
        filter=pc.field("unique_entity_id").is_valid(),
    ).to_table()
    logger.info(
        "  sam_entities_lance: %d rows x %d cols", sam_arrow.num_rows, len(sam_scan_cols),
    )

    logger.info("opening bridges/sam_pdl_domain_lance ...")
    bridge_ds = lance.dataset(SAM_PDL_BRIDGE_URI, storage_options=storage_options)
    try:
        bridge_versions = bridge_ds.versions()
        if bridge_versions:
            latest = bridge_versions[-1]
            ts_raw = (
                latest.get("timestamp")
                if isinstance(latest, dict)
                else getattr(latest, "timestamp", None)
            )
            logger.info(
                "  sam_pdl_domain_lance latest version: %s timestamp=%s",
                latest, ts_raw,
            )
    except Exception as e:  # noqa: BLE001
        logger.info("  sam_pdl_domain_lance freshness probe failed (non-fatal): %s", e)
    bridge_arrow = bridge_ds.scanner(
        columns=["uei", "pdl_company_id", "bridge_run_id"]
    ).to_table()
    logger.info(
        "  sam_pdl_domain_lance: %d rows (%d distinct UEIs estimated)",
        bridge_arrow.num_rows, len(set(bridge_arrow.column("uei").to_pylist())),
    )

    logger.info("opening pdl/free_companies_lance ...")
    pdl_ds = lance.dataset(PDL_LANCE_URI, storage_options=storage_options)
    pdl_arrow = pdl_ds.scanner(
        columns=[
            "pdl_id",
            "pdl_industry",
            "pdl_website",
            "pdl_linkedin_url",
            "pdl_size",
            "pdl_founded",
            "pdl_locality",
            "pdl_name",
        ],
    ).to_table()
    logger.info("  pdl/free_companies_lance: %d rows", pdl_arrow.num_rows)

    logger.info("opening usaspending/contracts_lance (project 5 cols) ...")
    usa_ds = lance.dataset(USASPENDING_URI, storage_options=storage_options)
    usa_arrow = usa_ds.scanner(
        columns=[
            "recipient_uei",
            "action_date",
            "period_of_performance_current_end_date",
            "federal_action_obligation",
            "total_dollars_obligated",
        ],
        filter="recipient_uei IS NOT NULL AND recipient_uei != ''",
    ).to_table()
    logger.info("  usaspending/contracts_lance (non-null UEI): %d rows", usa_arrow.num_rows)

    # ---- Step 2: build DuckDB plan ---- #
    con = _connect_duckdb()
    con.register("sam_proj", sam_arrow)
    con.register("sam_pdl_bridge_proj", bridge_arrow)
    con.register("pdl_proj", pdl_arrow)
    con.register("usaspending_proj", usa_arrow)

    # 2a. Pre-aggregate USAspending rollup at recipient_uei grain.
    # ALL 5 fields land as VARCHAR in usaspending.contracts_lance; TRY_CAST is
    # MANDATORY (precedent: scripts/run_govcontract_match_subset.py:113).
    logger.info("step 2a: pre-aggregating USAspending rollup at recipient_uei ...")
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE usaspending_typed AS
        SELECT
            recipient_uei,
            TRY_CAST(period_of_performance_current_end_date AS DATE) AS popoe_date,
            TRY_CAST(action_date                            AS DATE) AS action_date_typed,
            TRY_CAST(federal_action_obligation              AS DOUBLE) AS obligation_dbl,
            TRY_CAST(total_dollars_obligated                AS DOUBLE) AS total_obligated_dbl
        FROM usaspending_proj
        WHERE recipient_uei IS NOT NULL AND recipient_uei != ''
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE usaspending_rollup AS
        SELECT
            recipient_uei,
            COUNT(*)                                                  AS lifetime_contract_count,
            SUM(COALESCE(total_obligated_dbl, 0))                     AS lifetime_total_obligated,
            SUM(CASE WHEN popoe_date >= CURRENT_DATE
                     THEN 1 ELSE 0 END)                               AS active_contract_count,
            SUM(CASE WHEN popoe_date >= CURRENT_DATE
                     THEN COALESCE(total_obligated_dbl, 0) ELSE 0 END) AS active_total_obligated,
            MAX(popoe_date)                                           AS max_period_of_performance_end_date,
            (MAX(popoe_date) IS NOT NULL
                AND MAX(popoe_date) >= CURRENT_DATE)                  AS has_active_award,
            MAX(action_date_typed)                                    AS latest_action_date
        FROM usaspending_typed
        GROUP BY recipient_uei
        """
    )
    rollup_rows = con.execute("SELECT COUNT(*) FROM usaspending_rollup").fetchone()[0]
    logger.info("  usaspending_rollup: %d UEIs", rollup_rows)

    # 2b. SAM x sam_pdl_bridge x PDL INNER JOIN — entity-grain spine.
    logger.info("step 2b: SAM x sam_pdl_bridge x PDL inner join (entity-grain spine) ...")
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE sam_pdl_spine AS
        SELECT
            sam.unique_entity_id                              AS uei,
            sam.legal_business_name,
            sam.legal_business_name_normalized,
            sam.dba_name,
            sam.primary_naics,
            sam.naics_primary_2digit,
            sam.naics_code_string,
            sam.entity_url,
            sam.entity_structure,
            sam.state_of_incorporation,
            sam.physical_address_city,
            sam.physical_address_state_normalized,
            sam.physical_address_zip5,
            sam.cage_code,
            sam.registration_expiration_date,
            sam.last_update_date,
            sam.bus_type_string,
            sam.sba_business_types_string,
            pdl.pdl_id,
            pdl.pdl_industry,
            pdl.pdl_website,
            pdl.pdl_linkedin_url,
            pdl.pdl_size,
            pdl.pdl_founded,
            pdl.pdl_locality,
            pdl.pdl_name,
            br.bridge_run_id AS sam_pdl_bridge_run_id
        FROM sam_proj sam
        INNER JOIN sam_pdl_bridge_proj br ON br.uei = sam.unique_entity_id
        INNER JOIN pdl_proj             pdl ON pdl.pdl_id = br.pdl_company_id
        """
    )
    spine_rows = con.execute("SELECT COUNT(*) FROM sam_pdl_spine").fetchone()[0]
    logger.info("  sam_pdl_spine: %d rows", spine_rows)

    # 2c. LEFT JOIN USAspending rollup; add emit provenance columns.
    logger.info("step 2c: LEFT JOIN USAspending rollup + emit provenance ...")
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE bridge_out AS
        SELECT
            spine.*,
            rollup.lifetime_contract_count,
            rollup.lifetime_total_obligated,
            rollup.active_contract_count,
            rollup.active_total_obligated,
            rollup.max_period_of_performance_end_date,
            COALESCE(rollup.has_active_award, FALSE)            AS has_active_award,
            rollup.latest_action_date,
            CAST('{emit_bridge_run_id}' AS VARCHAR)             AS bridge_run_id,
            TIMESTAMP '{generated_at_iso}'                      AS generated_at,
            '{BRIDGE_VERSION}'                                  AS bridge_version
        FROM sam_pdl_spine spine
        LEFT JOIN usaspending_rollup rollup
               ON rollup.recipient_uei = spine.uei
        """
    )

    forensic_counts = con.execute(
        """
        SELECT
            COUNT(*)                                         AS rows_out,
            COUNT(*) FILTER (WHERE has_active_award)          AS rows_active,
            COUNT(*) FILTER (WHERE lifetime_contract_count IS NOT NULL) AS rows_any_history,
            COUNT(*) FILTER (WHERE naics_primary_2digit = '23') AS rows_construction
        FROM bridge_out
        """
    ).fetchone()
    logger.info(
        "forensic counts: rows_out=%d active=%d any_history=%d construction(NAICS 23)=%d",
        forensic_counts[0], forensic_counts[1], forensic_counts[2], forensic_counts[3],
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

    # ---- Step 5: BTREE on uei + naics_primary_2digit + legal_business_name_normalized ---- #
    t_btree = time.time()
    existing_btree = _existing_btree_columns(ds)
    logger.info("existing BTREE columns: %s", sorted(existing_btree))
    for col in (
        "uei",
        "naics_primary_2digit",
        "legal_business_name_normalized",
    ):
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
        "rows_with_any_history": forensic_counts[2],
        "rows_active": forensic_counts[1],
        "rows_construction_naics_23": forensic_counts[3],
    }


@app.local_entrypoint()
def run() -> None:
    """`modal run --detach scripts/build_bridge_sam_pdl_usaspending_lance.py::run`

    DETACH IS MANDATORY (validator p1 — FL-cycle root-cause lesson;
    Modal CLI disconnect kills attached jobs over a 4h window).
    """
    import json
    out = emit.remote()
    print(json.dumps(out, indent=2, default=str))

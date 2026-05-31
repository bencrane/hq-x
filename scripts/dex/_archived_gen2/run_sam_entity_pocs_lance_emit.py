"""s1 - SAM entity-POC long-form Lance emit (Pattern A, Modal-hosted).

Reads sam_gov.entities_lance (884,203 entities, 153 cols; PyLance scanner with
column projection to UEI + 6 POC kinds) and writes a long-form Lance dataset
at polaris-warehouse/sam_gov/entity_pocs_lance.

One row per (uei, poc_kind) where at least one of (first_name, middle_initial,
last_name, title) is non-null. 6 POC kinds. Schema introspection on the
canonical sam_gov.entities_lance shows TWO prefix-shapes per kind:

  symmetric (name + address both under one prefix; 11 cols each):
    govt_bus       name_prefix=govt_bus_poc_         addr_prefix=govt_bus_poc_
    alt_govt_bus   name_prefix=alt_govt_bus_poc_     addr_prefix=alt_govt_bus_poc_
    alt_past_perf  name_prefix=alt_past_perf_poc_    addr_prefix=alt_past_perf_poc_
    elec_bus       name_prefix=elec_bus_poc_         addr_prefix=elec_bus_poc_

  split (person-name under nested `_poc` infix; address under outer prefix):
    past_perf      name_prefix=past_perf_poc_poc_    addr_prefix=past_perf_poc_
    alt_elec_bus   name_prefix=alt_elec_poc_bus_poc_ addr_prefix=alt_elec_poc_bus_

Floor 1,885,939 (validator §"POC floor calibration"; 0.7 × measured 2,694,200).

Normalizer: in-script DuckDB `lower(trim(concat_ws(' ', first_name,
middle_initial, last_name)))`. The _lib/entity_name_normalize helper is for
legal entity (company) names — NOT used here per validator §"Critical executor
constraints / Normalizer".

Modal hosting: @app.function(cpu=8, memory=32768, timeout=10800). MUST be
launched via `modal run --detach` per FL-cycle root-cause lesson (Modal CLI
disconnect kills attached jobs; 10800s timeout window means this matters).

Run via (DETACH IS MANDATORY):
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      modal run --detach scripts/run_sam_entity_pocs_lance_emit.py::run
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

import modal

app = modal.App("data-engine-x-sam-entity-pocs-lance-emit")

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

DATASET_SLUG = "sam_entity_pocs_lance"
SRC_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/entities_lance"
LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/entity_pocs_lance"

# Validator §"POC floor calibration": 0.7 × 2,694,200 measured non-null last_name = 1,885,939.
MIN_ROW_FLOOR = 1_885_939

# 6 POC kinds with EXACT column-prefix mapping per schema introspection.
# Each entry: (kind_literal, name_prefix, addr_prefix). Name fields live under
# name_prefix; address fields under addr_prefix. For 4 of 6 kinds these are
# the same; for past_perf + alt_elec_bus they differ (validator p2 — the
# `past_perf_poc_poc_` and `alt_elec_poc_bus_poc_` infixes are person-name
# subobjects; the outer `past_perf_poc_` / `alt_elec_poc_bus_` prefixes
# carry the address fields).
POC_KINDS = [
    ("govt_bus",      "govt_bus_poc_",         "govt_bus_poc_"),
    ("alt_govt_bus",  "alt_govt_bus_poc_",     "alt_govt_bus_poc_"),
    ("past_perf",     "past_perf_poc_poc_",    "past_perf_poc_"),
    ("alt_past_perf", "alt_past_perf_poc_",    "alt_past_perf_poc_"),
    ("elec_bus",      "elec_bus_poc_",         "elec_bus_poc_"),
    ("alt_elec_bus",  "alt_elec_poc_bus_poc_", "alt_elec_poc_bus_"),
]

# Name fields (under name_prefix per kind). All 6 kinds have all 4.
POC_NAME_FIELDS = ["first_name", "middle_initial", "last_name", "title"]

# Address fields (under addr_prefix per kind). All 6 kinds have all 7.
POC_ADDR_FIELDS = [
    "st_add_1",
    "st_add_2",
    "city",
    "state_or_province",
    "zippostal_code",
    "zip_code_4",
    "country_code",
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
    timeout=10800,
    memory=32768,
    cpu=8,
)
def emit() -> dict:
    """Pattern A long-form Lance emit: explode 6 POC kinds → one row per
    (uei, poc_kind) where ≥1 of (first_name, middle_initial, last_name, title)
    non-null. BTREE on uei + poc_kind + full_name_normalized.
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

    # Step 1: read upstream sam_gov.entities_lance via PyLance scanner with
    # projection of ONLY the columns needed
    # (UEI + per-kind name fields under name_prefix + per-kind address fields
    #  under addr_prefix; ~67 cols total).
    project_cols = ["unique_entity_id"]
    for _kind, name_prefix, addr_prefix in POC_KINDS:
        for fld in POC_NAME_FIELDS:
            project_cols.append(f"{name_prefix}{fld}")
        for fld in POC_ADDR_FIELDS:
            project_cols.append(f"{addr_prefix}{fld}")
    # de-dup while preserving order (for 4 symmetric kinds, the prefixes are
    # identical so no dup; but defensive in case of future schema overlap).
    seen: set = set()
    project_cols_unique = []
    for c in project_cols:
        if c not in seen:
            project_cols_unique.append(c)
            seen.add(c)
    project_cols = project_cols_unique

    src_ds = lance.dataset(SRC_LANCE_URI, storage_options=storage_options)
    src_table = src_ds.scanner(columns=project_cols).to_table()
    logger.info(
        "source: %d entities x %d projected cols",
        src_table.num_rows, len(project_cols),
    )

    # Step 2: build UNION ALL of 6 SELECTs in DuckDB, one per POC kind.
    # Each SELECT aliases the per-kind prefix-qualified columns to canonical
    # short names + emits the kind literal + computes full_name_normalized
    # inline + filters NOT (first/middle/last/title ALL NULL).
    con = _connect_duckdb()
    con.register("sam_entities_proj", src_table)

    # Example resolved SQL for kind='govt_bus' (name_prefix=govt_bus_poc_):
    _GOVT_BUS_EXAMPLE_SQL = """
        SELECT
            unique_entity_id AS uei,
            'govt_bus' AS poc_kind,
            govt_bus_poc_first_name AS first_name,
            govt_bus_poc_middle_initial AS middle_initial,
            govt_bus_poc_last_name AS last_name,
            govt_bus_poc_title AS title,
            ... ,
            lower(trim(concat_ws(' ',
                govt_bus_poc_first_name,
                govt_bus_poc_middle_initial,
                govt_bus_poc_last_name
            ))) AS full_name_normalized
        FROM sam_entities_proj
        WHERE NOT (
            govt_bus_poc_first_name IS NULL
            AND govt_bus_poc_middle_initial IS NULL
            AND govt_bus_poc_last_name IS NULL
            AND govt_bus_poc_title IS NULL
        )
    """
    del _GOVT_BUS_EXAMPLE_SQL  # documentation-only literal; not used at runtime
    # The same shape is produced for each of the 6 POC kinds via the loop
    # below, with name_prefix/addr_prefix substituted per kind.
    union_sql_parts = []
    for kind, name_prefix, addr_prefix in POC_KINDS:
        name_aliases = ", ".join(
            f"{name_prefix}{fld} AS {fld}" for fld in POC_NAME_FIELDS
        )
        addr_aliases = ", ".join(
            f"{addr_prefix}{fld} AS {fld}" for fld in POC_ADDR_FIELDS
        )
        union_sql_parts.append(f"""
            SELECT
                unique_entity_id AS uei,
                '{kind}' AS poc_kind,
                {name_aliases},
                {addr_aliases},
                lower(trim(concat_ws(' ',
                    {name_prefix}first_name,
                    {name_prefix}middle_initial,
                    {name_prefix}last_name
                ))) AS full_name_normalized
            FROM sam_entities_proj
            WHERE NOT (
                {name_prefix}first_name IS NULL
                AND {name_prefix}middle_initial IS NULL
                AND {name_prefix}last_name IS NULL
                AND {name_prefix}title IS NULL
            )
        """)
    union_sql = " UNION ALL ".join(union_sql_parts)

    # Per-kind row counts (forensic logging — validator p3 mitigation).
    for kind, name_prefix, _addr_prefix in POC_KINDS:
        n = con.execute(f"""
            SELECT COUNT(*)
            FROM sam_entities_proj
            WHERE NOT (
                {name_prefix}first_name IS NULL
                AND {name_prefix}middle_initial IS NULL
                AND {name_prefix}last_name IS NULL
                AND {name_prefix}title IS NULL
            )
        """).fetchone()[0]
        logger.info("per-kind row count: %s = %d", kind, n)

    # Step 3: Lance write inside commit_lock; batch_size=100_000.
    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        reader = con.execute(f"""
            SELECT
                uei, poc_kind,
                first_name, middle_initial, last_name,
                full_name_normalized,
                title,
                st_add_1, st_add_2, city, state_or_province,
                zippostal_code, zip_code_4, country_code
            FROM ({union_sql})
        """).to_arrow_reader(batch_size=100_000)

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

    # Step 4: floor gate — fail-loud if below 1,885,939.
    if rows < MIN_ROW_FLOOR:
        msg = f"FAIL: row count {rows} below floor {MIN_ROW_FLOOR}"
        logger.error(msg)
        return {"status": "failed", "error": msg, "rows": rows}

    # Step 5: BTREE on uei + poc_kind + full_name_normalized.
    t_btree = time.time()
    existing_btree = _existing_btree_columns(ds)
    logger.info("existing BTREE columns: %s", sorted(existing_btree))
    for col in ("uei", "poc_kind", "full_name_normalized"):
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
        "s1 complete: %d rows, write=%.1fs btree=%.1fs",
        final_rows, write_dur, btree_dur,
    )
    return {
        "status": "succeeded",
        "rows_lance": final_rows,
        "lance_uri": LANCE_URI,
        "write_duration_s": round(write_dur, 1),
        "btree_duration_s": round(btree_dur, 1),
    }


@app.local_entrypoint()
def run() -> None:
    """`modal run --detach scripts/run_sam_entity_pocs_lance_emit.py::run`"""
    import json
    out = emit.remote()
    print(json.dumps(out, indent=2, default=str))

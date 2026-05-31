"""SEC ADV Schedule A principals × CA SoS principals — Pattern B Lance bridge.

First person-grain bridge in the canonical Lance substrate (PRs 1-516 are all
firm-grain). State-scoped to California (FL/TX/NY follow-up out of scope).

Architecture decisions (load-bearing for benchmark gates):

  L9 footnote (composite-key pre-dedup):
    Python builds distinct (crd_number, full_legal_name) tuples for the CA-firm
    ADV cohort, and distinct (entity_num, first_name, last_name) tuples for the
    CA SoS person cohort, BEFORE handing to DuckDB. Prevents OOM/spill patterns
    seen in UCC × PDL (single-Arrow-table approach blew 12GB at 14.9M rows).

  L17 (bridge_run_id per row):
    start_bridge_run() returns a UUID. Every output row carries bridge_run_id
    (string) + generated_at (timestamp) + bridge_version.

  L21 Path B (register NEW person_name_state_exact_ca v1.0.0):
    Validator pre-flight (2026-05-18T20:36:34Z) found person_name_namestate v1.0.0
    (PR #282) is bound to normalizer_version=1.0.0. The current module is v1.1.0.
    Reusing would violate L31. No national person_name_state_exact exists. Path B:
    register new state-scoped method. Bridge calls all three registry helpers.

  L31 (normalizer citation):
    normalizer_module='scripts._lib.person_name_normalize', version='1.1.0'.

  L34 (DuckDB UDF):
    ADV side: py_normalize_person_name_for_duckdb wraps normalize_person_name
    (handles comma-format "LAST, FIRST, MIDDLE"). Returns "last|first".
    SoS side: thin in-bridge wrapper py_normalize_first_last_for_duckdb(first, last)
    calls normalize_first_last — avoids HumanName re-parse cost on 14.9M rows.

  Name-normalization plan (validator-verified pre-flight):
    ADV: full_legal_name → py_normalize_person UDF → "last|first" → split
    SoS: (first_name, last_name) → py_normalize_first_last UDF → "last|first" → split
    JOIN ON (last_normalized, first_normalized) composite key (both sides).

  State-scoping policy (validator decision, firm-state filter):
    CA-firm scope = base_a_lance.raw_json["1F1-Country"]='United States' AND
    base_a_lance.raw_json["1F1-State"]='CA', latest filing per crd_number (dedup).
    Schedule A/B has no principal-state column — firm-state is the only viable scope.
    → 3,212 CA firms → 250K CA-firm principal rows → ~13.8K distinct (last,first) pairs.

  G9 smoke fixture (validator-verified):
    firm_crd=171912 → ADV "ZIOMEK, JOSEPH, JOHN" (Managing Member & CCO) →
    normalizes to "ziomek|joseph" → CA SoS entity_num=2940281 (Joseph Ziomek, CEO).
    Expected tier: platinum or gold.

Modal hosting (L47):
    @app.function(cpu=8, memory=24576, timeout=10800)
    Run: modal run scripts/build_bridge_sec_adv_principal_sos_ca_principal_lance.py::run
    (or --detach for background)

Usage:
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      modal run scripts/build_bridge_sec_adv_principal_sos_ca_principal_lance.py::run
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import modal

# Marker: actual imports live inside emit() scoped to Modal:
#   from scripts._lib.person_name_normalize import (
#       normalize_first_last,
#       py_normalize_person_name_for_duckdb,
#       __version__ as NORMALIZER_VERSION,
#   )
#   from scripts._lib.lance_commit_lock import lance_commit_lock
#   from scripts._lib.match_method_registry import (
#       register_match_method, register_match_method_version, register_bridge,
#       start_bridge_run, complete_bridge_run, fail_bridge_run,
#   )

# ---------------------------------------------------------------------------
# Modal app + image
# ---------------------------------------------------------------------------

app = modal.App("data-engine-x-sec-adv-principal-sos-ca-principal-lance")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "duckdb",
        "psycopg[binary]",
        "pylance>=0.20",
        "pyarrow>=16.0",
        "nameparser>=1.1.3",
        "unidecode>=1.3.8",
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

BRIDGE_NAME = "sec_adv_principal_sos_ca_principal_lance"
METHOD_NAME = "person_name_state_exact_ca"
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "1.0.0"
NORMALIZER_MODULE = "scripts._lib.person_name_normalize"

COLLISION_THRESHOLD = 50
# Validator-calibrated 2026-05-18T20:36:34Z post baseline cohort probe.
# 250K CA-firm principal rows → 13.8K distinct (last,first) pairs →
# 109K post-fan-out-rejection kept rows → 0.5× floor = 54,682 → 50,000.
MIN_ROWS_MATCHED = 50_000

DATASET_SLUG = BRIDGE_NAME
SOURCE_LEFT = "sec_adv_schedule_a_b_lance"
SOURCE_RIGHT = "sos_ca_principals_lance"

ADV_SCHEDULE_AB_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/sec_adv/schedule_a_b_lance"
)
ADV_BASE_A_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/sec_adv/base_a_lance"
)
SOS_PRINCIPALS_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/sos/ca_principals_lance"
)
BRIDGE_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/"
    "sec_adv_principal_sos_ca_principal_lance"
)

TMP_DIR = "/tmp/lance"

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


def _bridge_database_url() -> None:
    if "DEX_DB_URL_DIRECT" not in os.environ and "DATABASE_URL" in os.environ:
        os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


def _materialize_inputs(storage_options: dict) -> tuple:
    """Load ADV CA-firm principals + CA SoS person principals into Arrow tables.

    ADV side:
      1. Read base_a_lance to get latest-filing CRDs for CA firms
         (1F1-Country='United States' AND 1F1-State='CA'), dedup per crd_number.
      2. Read schedule_a_b_lance filtered to entity_type='I' and crd_number in
         the CA-firm set. Pre-dedup to distinct (crd_number, full_legal_name).

    SoS side:
      - Read ca_principals_lance: first_name, last_name (NOT full_name_normalized
        — non-canonical), plus key address/position columns.
      - Filter: org_name IS NULL (exclude entity-principal rows).
      - Pre-dedup to distinct (entity_num, first_name, last_name).

    Returns (adv_branded_arrow, sos_person_tbl, rows_adv_raw, rows_sos_raw).
    """
    import lance
    import pyarrow as pa
    import pyarrow.compute as pc

    logger.info("opening sec_adv/base_a_lance to build CA-firm CRD set ...")
    base_a_ds = lance.dataset(ADV_BASE_A_LANCE_URI, storage_options=storage_options)

    # Scan raw_json fields for state+country filter; base_a stores as struct/json.
    # Per cycle 3 pattern (build_bridge_sec_adv_pdl_name_state_lance.py): latest
    # filing per crd_number via ORDER BY date_submitted DESC in Python.
    base_a_tbl = base_a_ds.scanner(
        columns=["crd_number", "raw_json", "date_submitted"],
    ).to_table()
    rows_base_a_total = len(base_a_tbl)
    logger.info("  base_a_lance total rows: %d", rows_base_a_total)

    # Extract 1F1-State and 1F1-Country from raw_json struct.
    # raw_json is a struct column; access sub-fields via pc.struct_field.
    raw_json_col = base_a_tbl.column("raw_json")
    try:
        state_col = pc.struct_field(raw_json_col, "1F1-State")
        country_col = pc.struct_field(raw_json_col, "1F1-Country")
    except Exception as e:
        logger.warning("struct_field access failed (%s); trying cast to dict", e)
        # Fallback: if raw_json is a string column (JSON text), parse in Python
        import json as _json
        rows_list = base_a_tbl.to_pylist()
        ca_crds: dict[int, str] = {}  # crd_number → date_submitted (latest)
        for row in rows_list:
            rj = row.get("raw_json")
            if isinstance(rj, str):
                try:
                    rj = _json.loads(rj)
                except Exception:
                    continue
            if not isinstance(rj, dict):
                continue
            if rj.get("1F1-Country") != "United States":
                continue
            if rj.get("1F1-State") != "CA":
                continue
            crd = row["crd_number"]
            dt = row.get("date_submitted") or ""
            if crd not in ca_crds or str(dt) > str(ca_crds[crd]):
                ca_crds[crd] = str(dt)
        ca_firm_crds: set[int] = set(ca_crds.keys())
        del rows_list, ca_crds
    else:
        # Vectorized path: filter on state + country columns
        crd_col = base_a_tbl.column("crd_number")
        date_col = base_a_tbl.column("date_submitted")
        is_us = pc.equal(country_col, "United States")
        is_ca = pc.equal(state_col, "CA")
        is_ca_firm = pc.and_(is_us, is_ca)
        ca_mask = is_ca_firm.to_pylist()

        # Dedup to latest filing per crd_number (latest date_submitted)
        ca_crds_latest: dict[int, str] = {}
        for i, keep in enumerate(ca_mask):
            if not keep:
                continue
            crd = crd_col[i].as_py()
            dt = str(date_col[i].as_py() or "")
            if crd is None:
                continue
            if crd not in ca_crds_latest or dt > ca_crds_latest[crd]:
                ca_crds_latest[crd] = dt
        ca_firm_crds = set(ca_crds_latest.keys())
        del base_a_tbl, ca_crds_latest

    logger.info("  CA-firm CRDs (latest filing per firm): %d", len(ca_firm_crds))

    # --- ADV Schedule A/B side ---
    logger.info("opening sec_adv/schedule_a_b_lance ...")
    sched_ds = lance.dataset(ADV_SCHEDULE_AB_LANCE_URI, storage_options=storage_options)

    # Filter entity_type='I' at scanner level; then filter to CA-firm CRDs in Python.
    sched_filter = pc.equal(pc.field("entity_type"), "I")
    sched_tbl = sched_ds.scanner(
        columns=[
            "crd_number",
            "full_legal_name",
            "entity_type",
            "title_or_status",
            "owner_id",
            "ownership_code",
            "control_person",
        ],
        filter=sched_filter,
    ).to_table()
    rows_sched_raw = len(sched_tbl)
    logger.info("  schedule_a_b_lance (entity_type=I): %d rows", rows_sched_raw)

    # Filter to CA-firm scope + pre-dedup to distinct (crd_number, full_legal_name)
    sched_list = sched_tbl.to_pylist()
    del sched_tbl

    seen_pairs: set[tuple[int, str]] = set()
    deduped_rows: list[dict] = []
    for row in sched_list:
        crd = row.get("crd_number")
        if crd not in ca_firm_crds:
            continue
        name = row.get("full_legal_name")
        if not name or not isinstance(name, str) or not name.strip():
            continue
        key = (crd, name)
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        deduped_rows.append(row)

    del sched_list, seen_pairs
    rows_adv_raw = len(deduped_rows)
    logger.info("  adv_branded (distinct crd+name, CA-firm scope): %d rows", rows_adv_raw)

    # Build Arrow table for adv_branded
    import pyarrow as pa  # noqa: F811
    adv_branded_arrow = pa.table(
        {
            "firm_crd": pa.array(
                [r["crd_number"] for r in deduped_rows], type=pa.int64()
            ),
            "adv_full_legal_name_raw": pa.array(
                [r["full_legal_name"] for r in deduped_rows], type=pa.string()
            ),
            "adv_title_or_status": pa.array(
                [r.get("title_or_status") for r in deduped_rows], type=pa.string()
            ),
            "adv_owner_id": pa.array(
                [r.get("owner_id") for r in deduped_rows], type=pa.string()
            ),
            "adv_entity_type": pa.array(
                [r.get("entity_type") for r in deduped_rows], type=pa.string()
            ),
            "adv_ownership_code": pa.array(
                [r.get("ownership_code") for r in deduped_rows], type=pa.string()
            ),
            "adv_control_person": pa.array(
                [r.get("control_person") for r in deduped_rows], type=pa.bool_()
            ),
        }
    )
    del deduped_rows

    # --- CA SoS side ---
    logger.info("opening sos/ca_principals_lance ...")
    sos_ds = lance.dataset(SOS_PRINCIPALS_LANCE_URI, storage_options=storage_options)

    # org_name IS NULL filter = person rows only (not entity-principal rows)
    # full_name_normalized is non-canonical; DO NOT include in matching columns.
    # Pre-dedup to distinct (entity_num, first_name, last_name) in Python.
    sos_filter = pc.field("org_name").is_null()
    sos_tbl = sos_ds.scanner(
        columns=[
            "entity_num",
            "first_name",
            "middle_name",
            "last_name",
            "position_type",
            "address1",
            "address2",
            "address3",
            "city",
            "state",
            "country",
            "postal_code",
        ],
        filter=sos_filter,
    ).to_table()
    rows_sos_raw = len(sos_tbl)
    logger.info("  sos ca_principals_lance (person rows, org_name IS NULL): %d rows", rows_sos_raw)

    # Pre-dedup to distinct (entity_num, first_name, last_name)
    sos_list = sos_tbl.to_pylist()
    del sos_tbl

    sos_seen: set[tuple[str, str, str]] = set()
    sos_deduped: list[dict] = []
    for row in sos_list:
        entity_num = row.get("entity_num")
        fn = row.get("first_name")
        ln = row.get("last_name")
        if not fn or not ln or not entity_num:
            continue
        key = (str(entity_num), str(fn), str(ln))
        if key in sos_seen:
            continue
        sos_seen.add(key)
        sos_deduped.append(row)

    del sos_list, sos_seen
    logger.info("  sos_person_deduped (distinct entity_num+first+last): %d rows", len(sos_deduped))

    sos_person_arrow = pa.table(
        {
            "sos_entity_num": pa.array(
                [r["entity_num"] for r in sos_deduped], type=pa.string()
            ),
            "sos_first_name": pa.array(
                [r.get("first_name") for r in sos_deduped], type=pa.string()
            ),
            "sos_middle_name": pa.array(
                [r.get("middle_name") for r in sos_deduped], type=pa.string()
            ),
            "sos_last_name": pa.array(
                [r.get("last_name") for r in sos_deduped], type=pa.string()
            ),
            "sos_position_type": pa.array(
                [r.get("position_type") for r in sos_deduped], type=pa.string()
            ),
            "sos_address1": pa.array(
                [r.get("address1") for r in sos_deduped], type=pa.string()
            ),
            "sos_address2": pa.array(
                [r.get("address2") for r in sos_deduped], type=pa.string()
            ),
            "sos_address3": pa.array(
                [r.get("address3") for r in sos_deduped], type=pa.string()
            ),
            "sos_city": pa.array(
                [r.get("city") for r in sos_deduped], type=pa.string()
            ),
            "sos_state": pa.array(
                [r.get("state") for r in sos_deduped], type=pa.string()
            ),
            "sos_country": pa.array(
                [r.get("country") for r in sos_deduped], type=pa.string()
            ),
            "sos_postal_code": pa.array(
                [r.get("postal_code") for r in sos_deduped], type=pa.string()
            ),
        }
    )
    del sos_deduped

    return adv_branded_arrow, sos_person_arrow, rows_adv_raw, rows_sos_raw


def _build_match_table(
    adv_branded_arrow,
    sos_person_arrow,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
    normalize_first_last,
    py_normalize_person_name_for_duckdb,
) -> tuple:
    """Normalize both sides via DuckDB UDFs + composite-key JOIN + tier classification."""
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET memory_limit='16GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")

    # Register Arrow tables
    con.register("adv_raw", adv_branded_arrow)
    con.register("sos_raw", sos_person_arrow)

    rows_adv_reg = con.execute("SELECT COUNT(*) FROM adv_raw").fetchone()[0]
    rows_sos_reg = con.execute("SELECT COUNT(*) FROM sos_raw").fetchone()[0]
    logger.info(
        "  registered: adv_raw=%d  sos_raw=%d",
        rows_adv_reg, rows_sos_reg,
    )

    # Register ADV-side UDF: py_normalize_person_name_for_duckdb(VARCHAR) → VARCHAR
    # Returns "last|first" pipe-delimited. Handles "LAST, FIRST, MIDDLE" comma format.
    # Use string type names (["VARCHAR"]) — duckdb.typing not available in DuckDB 1.5.x.
    con.create_function(
        "py_normalize_person",
        py_normalize_person_name_for_duckdb,
        ["VARCHAR"],
        "VARCHAR",
        null_handling="special",
    )

    # Register CA SoS thin wrapper UDF: normalize_first_last(first, last) → "last|first"
    # Avoids re-parsing via HumanName on 14.9M pre-split SoS rows (perf ~3x gain).
    def py_normalize_first_last_for_duckdb(
        first: str | None, last: str | None
    ) -> str | None:
        result = normalize_first_last(first, last)
        if result is None:
            return None
        last_norm, first_norm = result
        return f"{last_norm}|{first_norm}"

    con.create_function(
        "py_normalize_first_last",
        py_normalize_first_last_for_duckdb,
        ["VARCHAR", "VARCHAR"],
        "VARCHAR",
        null_handling="special",
    )

    # --- ADV side normalization ---
    logger.info("normalizing ADV side via py_normalize_person UDF ...")
    con.execute(
        """
        CREATE TEMP TABLE adv_branded AS
        SELECT
            firm_crd,
            adv_full_legal_name_raw,
            adv_title_or_status,
            adv_owner_id,
            adv_entity_type,
            adv_ownership_code,
            adv_control_person,
            py_normalize_person(adv_full_legal_name_raw) AS adv_norm_pipe
        FROM adv_raw
        WHERE adv_full_legal_name_raw IS NOT NULL
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE adv_split AS
        SELECT
            firm_crd,
            adv_full_legal_name_raw,
            adv_title_or_status,
            adv_owner_id,
            adv_entity_type,
            adv_ownership_code,
            adv_control_person,
            string_split(adv_norm_pipe, '|')[1] AS last_normalized,
            string_split(adv_norm_pipe, '|')[2] AS first_normalized
        FROM adv_branded
        WHERE adv_norm_pipe IS NOT NULL
        """
    )
    rows_adv_norm = con.execute("SELECT COUNT(*) FROM adv_split").fetchone()[0]
    logger.info("  adv_split (post-normalize): %d rows", rows_adv_norm)

    # --- CA SoS side normalization ---
    logger.info("normalizing CA SoS side via py_normalize_first_last UDF ...")
    con.execute(
        """
        CREATE TEMP TABLE sos_branded AS
        SELECT
            sos_entity_num,
            sos_first_name,
            sos_middle_name,
            sos_last_name,
            sos_position_type,
            sos_address1,
            sos_address2,
            sos_address3,
            sos_city,
            sos_state,
            sos_country,
            sos_postal_code,
            py_normalize_first_last(sos_first_name, sos_last_name) AS sos_norm_pipe
        FROM sos_raw
        WHERE sos_first_name IS NOT NULL AND sos_last_name IS NOT NULL
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE sos_split AS
        SELECT
            sos_entity_num,
            sos_first_name,
            sos_middle_name,
            sos_last_name,
            sos_position_type,
            sos_address1,
            sos_address2,
            sos_address3,
            sos_city,
            sos_state,
            sos_country,
            sos_postal_code,
            string_split(sos_norm_pipe, '|')[1] AS last_normalized,
            string_split(sos_norm_pipe, '|')[2] AS first_normalized
        FROM sos_branded
        WHERE sos_norm_pipe IS NOT NULL
        """
    )
    rows_sos_norm = con.execute("SELECT COUNT(*) FROM sos_split").fetchone()[0]
    logger.info("  sos_split (post-normalize): %d rows", rows_sos_norm)

    # --- Fan-out counts per composite key ---
    logger.info("computing fan-out per (last_normalized, first_normalized) ...")
    con.execute(
        """
        CREATE TEMP TABLE adv_fanout AS
        SELECT
            last_normalized,
            first_normalized,
            COUNT(*) AS adv_fan_out
        FROM adv_split
        GROUP BY last_normalized, first_normalized
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE sos_fanout AS
        SELECT
            last_normalized,
            first_normalized,
            COUNT(DISTINCT sos_entity_num) AS sos_ca_fan_out
        FROM sos_split
        GROUP BY last_normalized, first_normalized
        """
    )

    # --- JOIN + tier classification ---
    logger.info("running composite-key JOIN + tier classification ...")
    con.execute(
        f"""
        CREATE TEMP TABLE matched AS
        SELECT
            a.firm_crd,
            a.adv_full_legal_name_raw,
            a.adv_title_or_status,
            a.adv_owner_id,
            a.adv_entity_type,
            a.adv_ownership_code,
            a.adv_control_person,
            s.sos_entity_num,
            s.sos_first_name,
            s.sos_middle_name,
            s.sos_last_name,
            s.sos_position_type,
            s.sos_address1,
            s.sos_address2,
            s.sos_address3,
            s.sos_city,
            s.sos_state,
            s.sos_country,
            s.sos_postal_code,
            a.last_normalized AS last_normalized,
            a.first_normalized AS first_normalized,
            concat(a.last_normalized, '|', a.first_normalized) AS match_value,
            af.adv_fan_out,
            sf.sos_ca_fan_out
        FROM adv_split a
        JOIN sos_split s
          ON a.last_normalized = s.last_normalized
         AND a.first_normalized = s.first_normalized
        JOIN adv_fanout af
          ON af.last_normalized = a.last_normalized
         AND af.first_normalized = a.first_normalized
        JOIN sos_fanout sf
          ON sf.last_normalized = a.last_normalized
         AND sf.first_normalized = a.first_normalized
        """
    )
    rows_matched_pre = con.execute("SELECT COUNT(*) FROM matched").fetchone()[0]
    logger.info("  matched (pre-tier-rejection): %d rows", rows_matched_pre)

    # --- Tier classification + provenance ---
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            m.firm_crd,
            m.adv_full_legal_name_raw,
            m.adv_title_or_status,
            m.adv_owner_id,
            m.adv_entity_type,
            m.adv_ownership_code,
            m.adv_control_person,
            m.sos_entity_num,
            m.sos_first_name,
            m.sos_middle_name,
            m.sos_last_name,
            m.sos_position_type,
            m.sos_address1,
            m.sos_address2,
            m.sos_address3,
            m.sos_city,
            m.sos_state,
            m.sos_country,
            m.sos_postal_code,
            m.match_value,
            m.adv_fan_out,
            m.sos_ca_fan_out,
            '{METHOD_NAME}' AS match_method,
            '{BRIDGE_VERSION}' AS bridge_version,
            '{bridge_run_id}' AS bridge_run_id,
            CASE
                WHEN m.adv_fan_out > {COLLISION_THRESHOLD}
                  OR m.sos_ca_fan_out > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN m.adv_fan_out = 1 AND m.sos_ca_fan_out = 1
                    THEN 'platinum'
                WHEN m.adv_fan_out = 1 OR m.sos_ca_fan_out = 1
                    THEN 'gold'
                ELSE 'silver'
            END AS confidence_tier,
            TIMESTAMPTZ '{generated_at_iso}' AS generated_at
        FROM matched m
        """
    )

    con.execute(
        """
        CREATE TEMP TABLE bridge_match AS
        SELECT * FROM bridge_all WHERE confidence_tier <> 'rejected'
        """
    )

    counts_row = con.execute(
        """
        SELECT
            COUNT(*),
            COUNT(*) FILTER (WHERE confidence_tier = 'platinum'),
            COUNT(*) FILTER (WHERE confidence_tier = 'gold'),
            COUNT(*) FILTER (WHERE confidence_tier = 'silver')
        FROM bridge_match
        """
    ).fetchone()
    rejected = con.execute(
        "SELECT COUNT(*) FROM bridge_all WHERE confidence_tier = 'rejected'"
    ).fetchone()[0]

    counts = {
        "rows_matched": counts_row[0],
        "rows_tier1": counts_row[1],
        "rows_tier2": counts_row[2],
        "rows_tier3": counts_row[3],
        "rows_collision_rejected": rejected,
    }
    return con, counts


def _write_bridge_lance(con, storage_options: dict, lance_commit_lock) -> int:
    """Write bridge_match to Lance + BTREE on firm_crd + compact + cleanup."""
    import lance

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = TMP_DIR
    os.environ["LANCE_BYPASS_SPILLING"] = "true"

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        logger.info("writing bridge to Lance at %s ...", BRIDGE_LANCE_URI)
        reader = con.from_query("SELECT * FROM bridge_match").to_arrow_reader(
            batch_size=100_000
        )
        ds = lance.write_dataset(
            reader,
            BRIDGE_LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t0
        lance_count = ds.count_rows()
        logger.info(
            "wrote %d rows in %.1fs (version=%s)",
            lance_count, write_dur, ds.version,
        )

        try:
            ds.create_scalar_index("firm_crd", index_type="BTREE", replace=True)
            logger.info("BTREE on firm_crd: OK")
        except Exception as e:
            logger.error("BTREE on firm_crd FAILED: %s", e)
            raise

        try:
            ds.optimize.compact_files()
        except Exception as e:
            logger.warning("compact_files failed (non-fatal): %s", e)

        try:
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as e:
            logger.warning("cleanup_old_versions failed (non-fatal): %s", e)

    return lance_count


@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=10800,
    memory=24576,
    cpu=8,
)
def emit() -> dict:
    """Build the SEC ADV Schedule A principals × CA SoS principals bridge."""
    sys.path.insert(0, "/root")
    from scripts._lib.person_name_normalize import (  # noqa: F401
        __version__ as NORMALIZER_VERSION,
        normalize_first_last,
        py_normalize_person_name_for_duckdb,
    )
    from scripts._lib.lance_commit_lock import lance_commit_lock
    from scripts._lib.match_method_registry import (
        complete_bridge_run,
        fail_bridge_run,
        register_bridge,
        register_match_method,
        register_match_method_version,
        start_bridge_run,
    )

    _bridge_database_url()
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)

    storage_options = _storage_options()
    started_at = datetime.now(timezone.utc)
    t0 = time.time()

    logger.info(
        "bridge: %s  method=%s v%s  normalizer=%s v%s",
        BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER, NORMALIZER_MODULE, NORMALIZER_VERSION,
    )
    logger.info(
        "inputs: %s (firm-CA-scoped, entity_type=I) + %s",
        ADV_SCHEDULE_AB_LANCE_URI, SOS_PRINCIPALS_LANCE_URI,
    )
    logger.info("output: %s", BRIDGE_LANCE_URI)
    logger.info("MIN_ROWS_MATCHED floor: %d", MIN_ROWS_MATCHED)

    # L21 Path B: register new state-scoped method person_name_state_exact_ca v1.0.0
    # Bound to normalizer_version=1.1.0. DO NOT modify existing method rows.
    register_match_method(
        method_name=METHOD_NAME,
        description=(
            "Exact match on (normalized last_name, normalized first_name) within "
            "California — state-scoped person-name variant. ADV firm-state filter: "
            "base_a_lance.raw_json['1F1-State']='CA'. Normalizer: "
            f"{NORMALIZER_MODULE} v{NORMALIZER_VERSION}. First registered "
            "for SEC ADV Schedule A principals × CA SoS principals bridge (PR #517+)."
        ),
    )
    register_match_method_version(
        method_name=METHOD_NAME,
        semver=METHOD_SEMVER,
        normalizer_module=NORMALIZER_MODULE,
        normalizer_version=NORMALIZER_VERSION,
        blacklist_module=NORMALIZER_MODULE,
        blacklist_version=NORMALIZER_VERSION,
        tier_rule_description=(
            f"platinum=1:1; gold=1:N or N:1; silver=N:M ≤{COLLISION_THRESHOLD}; "
            f"rejected=>50 (COLLISION_THRESHOLD={COLLISION_THRESHOLD})"
        ),
        rejection_rule_description=(
            f"fan-out >{COLLISION_THRESHOLD} on either side collapsed to "
            "rows_collision_rejected"
        ),
        input_columns_left=[
            "schedule_a_b.full_legal_name",
            "base_a.raw_json.1F1-State",
        ],
        input_columns_right=[
            "ca_principals.first_name",
            "ca_principals.last_name",
            "ca_principals.state",
        ],
        output_value_description=(
            "normalize_person_name(adv.full_legal_name) == "
            "normalize_first_last(sos.first_name, sos.last_name) "
            "— both return 'last|first' pipe-delimited; matched on "
            "(last_normalized, first_normalized) composite key for firm 1F1-State='CA'"
        ),
    )
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "SEC ADV Schedule A principals × CA SoS principals via normalized "
            "person name within California. First person-grain bridge in canonical "
            "Lance substrate. Firm-state scoped (CA RIAs only). "
            "Attaches CA business-entity officer/director identity to RIA principals. "
            "Opens FL/TX/NY follow-up cycles."
        ),
    )
    run_uuid = start_bridge_run(
        bridge_name=BRIDGE_NAME,
        method_semver=METHOD_SEMVER,
        bridge_version=BRIDGE_VERSION,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        match_method=METHOD_NAME,
        r2_output_key=BRIDGE_LANCE_URI,
    )
    bridge_run_id = str(run_uuid)
    logger.info("bridge_run_id=%s", bridge_run_id)

    try:
        adv_branded_arrow, sos_person_arrow, rows_left, rows_right = (
            _materialize_inputs(storage_options)
        )
        con, counts = _build_match_table(
            adv_branded_arrow,
            sos_person_arrow,
            bridge_run_id=bridge_run_id,
            generated_at_iso=started_at.isoformat(),
            normalize_first_last=normalize_first_last,
            py_normalize_person_name_for_duckdb=py_normalize_person_name_for_duckdb,
        )

        logger.info("-" * 60)
        logger.info("bridge tier distribution:")
        logger.info("  rows_matched:            %d", counts["rows_matched"])
        logger.info("    platinum (1:1):         %d", counts["rows_tier1"])
        logger.info("    gold     (1:N | N:1):   %d", counts["rows_tier2"])
        logger.info(
            "    silver   (N:M ≤%d):     %d",
            COLLISION_THRESHOLD, counts["rows_tier3"],
        )
        logger.info("  rows_collision_rejected:  %d", counts["rows_collision_rejected"])

        if counts["rows_matched"] < MIN_ROWS_MATCHED:
            msg = (
                f"HARD FAIL: rows_matched={counts['rows_matched']:,} < "
                f"floor={MIN_ROWS_MATCHED:,} — bridge too thin to ship. "
                "Diagnose: check normalizer rejection rate, state-scope filter, "
                "composite-key JOIN shape."
            )
            logger.error(msg)
            fail_bridge_run(run_uuid, msg)
            return {"status": "failed", "error": msg, "counts": counts}

        lance_count = _write_bridge_lance(con, storage_options, lance_commit_lock)
        complete_bridge_run(
            run_uuid,
            metrics={
                "rows_left": rows_left,
                "rows_right": rows_right,
                "rows_matched": counts["rows_matched"],
                "rows_tier1": counts["rows_tier1"],
                "rows_tier2": counts["rows_tier2"],
                "rows_tier3": counts["rows_tier3"],
                "rows_collision_rejected": counts["rows_collision_rejected"],
                "lance_rows": lance_count,
            },
        )
        logger.info(
            "OK - run_id=%s  duration=%.1fs", bridge_run_id, time.time() - t0
        )
        return {
            "status": "succeeded",
            "bridge_run_id": bridge_run_id,
            "rows_left": rows_left,
            "rows_right": rows_right,
            "rows_matched": counts["rows_matched"],
            "rows_tier1": counts["rows_tier1"],
            "rows_tier2": counts["rows_tier2"],
            "rows_tier3": counts["rows_tier3"],
            "rows_collision_rejected": counts["rows_collision_rejected"],
            "lance_count": lance_count,
            "duration_s": round(time.time() - t0, 1),
        }
    except Exception as exc:
        logger.exception("bridge generation failed")
        try:
            fail_bridge_run(run_uuid, repr(exc))
        except Exception:
            logger.exception("also failed to mark run as failed")
        raise


@app.local_entrypoint()
def run() -> None:
    """`modal run scripts/build_bridge_sec_adv_principal_sos_ca_principal_lance.py::run`"""
    import json
    out = emit.remote()
    print(json.dumps(out, indent=2, default=str))

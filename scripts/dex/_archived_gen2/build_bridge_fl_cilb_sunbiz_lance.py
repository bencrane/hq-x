"""FL CILB licensees x FL Sunbiz entities bridge (Pattern B, REUSER).

Pattern B exact-match bridge: FL CILB licensees (licensee_name_normalized)
x FL Sunbiz entities (entity_name_normalized).
Normalizer is canonical scripts._lib.entity_name_normalize on BOTH sides.

Method: legal_name_state_exact_fl v1.0.0 — REUSE per L21 (already registered
by PR #467 build_bridge_sba_sos_fl_owner_lance.py). This cycle does NOT call
L21 REUSER guard: method row was published by PR #467; calling the register functions
would corrupt publisher PR #467's input_columns_left={legal_name_normalized,borrstate} config.
Only: register_bridge + start_bridge_run + complete_bridge_run + fail_bridge_run.

Bridge version: 1.0.0
COLLISION_THRESHOLD = 50
MIN_ROWS_MATCHED = 100_000 (validator-refined calibration 2026-05-18 — ~47%
of expected ~212K; cslb_sos_ca_owner precedent rows_matched/rows_left=82.7%.
Hard-fail = canonical normalizer-drift signal).

Inputs:
    s3://dex-raw-landing-zone/polaris-warehouse/licensure/fl_cilb_lance
        (full scan — all FL CILB licensees, left side)
    s3://dex-raw-landing-zone/polaris-warehouse/sos/fl_entities_lance
        (full scan — all FL Sunbiz entities, right side)

Output:
    s3://dex-raw-landing-zone/polaris-warehouse/bridges/fl_cilb_sunbiz_lance
    (BTREE on licensee_name_normalized)

Per-row provenance: bridge_run_id UUID (L17), bridge_version, match_method,
match_value_normalized, match_state, confidence_tier (platinum/gold/silver/rejected).

Tier rule:
    platinum = 1:1
    gold     = 1:N | N:1
    silver   = N:M (both <= 50)
    rejected = >50 on either side

Modal hosting: @app.function(cpu=8, memory=32768, timeout=10800)

Run via:
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      modal run scripts/build_bridge_fl_cilb_sunbiz_lance.py::run

Directive: /Users/benjamincrane/Desktop/hq/directives/2026-05-18-fl-cilb-ingest-and-bridges.md
Precedent: PR #482 build_bridge_cslb_sos_ca_owner_lance.py (same license x state-entity shape).
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import modal

# ---------------------------------------------------------------------------
# Modal app + image
# ---------------------------------------------------------------------------

app = modal.App("data-engine-x-fl-cilb-sunbiz-lance")

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

BRIDGE_NAME = "fl_cilb_sunbiz"
METHOD_NAME = "legal_name_state_exact_fl"
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "1.0.0"

COLLISION_THRESHOLD = 50
# Validator-refined calibration 2026-05-18: cslb_sos_ca_owner precedent
# rows_matched/rows_left=82.7%; FL should mirror: 265K licensees x 80% ~212K.
# Conservative floor 100K = ~47% of expected. Hard-fail = normalizer-drift signal.
MIN_ROWS_MATCHED = 100_000

DATASET_SLUG = "fl_cilb_sunbiz_lance"
SOURCE_LEFT = "fl_cilb_lance"
SOURCE_RIGHT = "fl_entities_lance"

FL_CILB_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/licensure/fl_cilb_lance"
)
FL_ENTITIES_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/sos/fl_entities_lance"
)
BRIDGE_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/fl_cilb_sunbiz_lance"
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


def _materialize_inputs(storage_options: dict):
    """Load FL CILB licensees (left) + FL Sunbiz entities (right) into Arrow tables.

    FL CILB side: licensee_name_normalized + license cols, is_valid filter.
    FL Sunbiz side: entity_name_normalized + entity cols, is_valid filter.
    Normalizer: scripts._lib.entity_name_normalize.normalize_entity_name on BOTH sides.
    """
    import lance
    import pyarrow as pa
    import pyarrow.compute as pc

    logger.info("opening licensure/fl_cilb_lance (left) ...")
    cilb_ds = lance.dataset(FL_CILB_LANCE_URI, storage_options=storage_options)
    cilb_filter = pc.field("licensee_name_normalized").is_valid()
    cilb_tbl = cilb_ds.scanner(
        columns=[
            "license_number",
            "licensee_name",
            "licensee_name_normalized",
            "occupation_code",
            "primary_status",
            "secondary_status",
            "city",
            "state",
            "zip",
            "county_code",
            "effective_date",
            "expiration_date",
            "alternate_license_number",
        ],
        filter=cilb_filter,
    ).to_table()
    rows_cilb = len(cilb_tbl)
    logger.info("  fl_cilb_lance (post-filter): %d rows", rows_cilb)

    logger.info("opening sos/fl_entities_lance (right) ...")
    sos_ds = lance.dataset(FL_ENTITIES_LANCE_URI, storage_options=storage_options)
    sos_filter = pc.field("entity_name_normalized").is_valid()
    sos_tbl = sos_ds.scanner(
        columns=[
            "entity_num",
            "entity_name",
            "entity_name_normalized",
            "status",
            "filing_type",
            "city",
            "state",
            "zip",
            "fei_number",
            "file_date",
            "last_transaction_date",
            "registered_agent_name",
            "registered_agent_type",
        ],
        filter=sos_filter,
    ).to_table()
    rows_sos = len(sos_tbl)
    logger.info("  fl_entities_lance (post-filter): %d rows", rows_sos)

    return cilb_tbl, sos_tbl, rows_cilb, rows_sos


def _build_match_table(
    cilb_tbl,
    sos_tbl,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
):
    """Run exact-equality JOIN + fan-out tiering in DuckDB (Arrow bridge)."""
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET memory_limit='16GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")

    con.register("cilb_licensees", cilb_tbl)
    con.register("sos_entities", sos_tbl)

    rows_cilb_reg = con.execute("SELECT COUNT(*) FROM cilb_licensees").fetchone()[0]
    rows_sos_reg = con.execute("SELECT COUNT(*) FROM sos_entities").fetchone()[0]
    logger.info(
        "  registered: cilb_licensees=%d  sos_entities=%d",
        rows_cilb_reg, rows_sos_reg,
    )

    # 1. Inner join on normalized name (licensee_name_normalized = entity_name_normalized)
    con.execute(
        """
        CREATE TEMP TABLE matched AS
        SELECT
            c.license_number              AS cilb_license_number,
            c.licensee_name               AS cilb_licensee_name,
            c.licensee_name_normalized,
            c.occupation_code             AS cilb_occupation_code,
            c.primary_status              AS cilb_primary_status,
            c.secondary_status            AS cilb_secondary_status,
            c.city                        AS cilb_city,
            c.state                       AS cilb_state,
            c.zip                         AS cilb_zip,
            c.county_code                 AS cilb_county_code,
            c.effective_date              AS cilb_effective_date,
            c.expiration_date             AS cilb_expiration_date,
            c.alternate_license_number    AS cilb_alternate_license_number,
            e.entity_num                  AS sos_entity_num,
            e.entity_name                 AS sos_entity_name,
            e.entity_name_normalized      AS sos_entity_name_normalized,
            e.status                      AS sos_status,
            e.filing_type                 AS sos_filing_type,
            e.city                        AS sos_city,
            e.state                       AS sos_state,
            e.zip                         AS sos_zip,
            e.fei_number                  AS sos_fei_number,
            e.file_date                   AS sos_file_date,
            e.last_transaction_date       AS sos_last_transaction_date,
            e.registered_agent_name       AS sos_registered_agent_name,
            e.registered_agent_type       AS sos_registered_agent_type
        FROM cilb_licensees c
        JOIN sos_entities e
          ON c.licensee_name_normalized = e.entity_name_normalized
        """
    )
    rows_matched_pre = con.execute("SELECT COUNT(*) FROM matched").fetchone()[0]
    logger.info("  matched (pre-tier): %d rows", rows_matched_pre)

    # 2. Fan-out counts on both sides
    con.execute(
        """
        CREATE TEMP TABLE cilb_fanout AS
        SELECT licensee_name_normalized, COUNT(*) AS cilb_fan_out
        FROM matched
        GROUP BY 1
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE sos_fanout AS
        SELECT licensee_name_normalized, COUNT(*) AS sos_fan_out
        FROM matched
        GROUP BY 1
        """
    )

    # 3. Tier + provenance
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            m.*,
            '{METHOD_NAME}'              AS match_method,
            m.licensee_name_normalized   AS match_value_normalized,
            'FL'                         AS match_state,
            cf.cilb_fan_out,
            sf.sos_fan_out,
            CASE
                WHEN cf.cilb_fan_out > {COLLISION_THRESHOLD}
                  OR sf.sos_fan_out > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN cf.cilb_fan_out = 1 AND sf.sos_fan_out = 1
                    THEN 'platinum'
                WHEN cf.cilb_fan_out = 1 OR sf.sos_fan_out = 1
                    THEN 'gold'
                ELSE 'silver'
            END                              AS confidence_tier,
            TIMESTAMP '{generated_at_iso}'   AS generated_at,
            '{BRIDGE_VERSION}'               AS bridge_version,
            '{bridge_run_id}'                AS bridge_run_id
        FROM matched m
        JOIN cilb_fanout cf USING (licensee_name_normalized)
        JOIN sos_fanout sf USING (licensee_name_normalized)
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


def _write_bridge_lance(con, storage_options: dict) -> int:
    """Write bridge_match to Lance via Arrow-bridge pattern + BTREE."""
    import lance
    from scripts._lib.lance_commit_lock import lance_commit_lock

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

        # BTREE on licensee_name_normalized (per directive s4 spec)
        try:
            ds.create_scalar_index("licensee_name_normalized", index_type="BTREE", replace=True)
            logger.info("BTREE on licensee_name_normalized: OK")
        except Exception as e:
            logger.error("BTREE on licensee_name_normalized FAILED: %s", e)
            raise
        try:
            ds.create_scalar_index("sos_entity_num", index_type="BTREE", replace=True)
            logger.info("BTREE on sos_entity_num: OK")
        except Exception as e:
            logger.error("BTREE on sos_entity_num FAILED: %s", e)
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
    memory=32768,
    cpu=8,
)
def emit() -> dict:
    """Build the FL CILB x FL Sunbiz bridge (REUSER of legal_name_state_exact_fl)."""
    sys.path.insert(0, "/root")
    from scripts._lib.entity_name_normalize import (  # noqa: F401
        __version__ as NORMALIZER_VERSION,
    )
    # REUSER imports only — L21 guard: method row published by PR #467.
    # Calling method-registry write functions would corrupt publisher PR #467's
    # input_columns_left={legal_name_normalized,borrstate} configuration.
    from scripts._lib.match_method_registry import (
        complete_bridge_run,
        fail_bridge_run,
        register_bridge,
        start_bridge_run,
    )

    _bridge_database_url()
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)

    storage_options = _storage_options()
    started_at = datetime.now(timezone.utc)
    t0 = time.time()

    logger.info(
        "bridge: %s  method=%s v%s  normalizer=v%s",
        BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER, NORMALIZER_VERSION,
    )
    logger.info("inputs: %s + %s", FL_CILB_LANCE_URI, FL_ENTITIES_LANCE_URI)
    logger.info("output: %s", BRIDGE_LANCE_URI)

    # REUSER: register_bridge only (idempotent). L21 guard: does NOT call
    # method-registry write functions — those would overwrite publisher PR #467's config.
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "FL CILB licensees x FL Sunbiz entities via "
            "legal_name_state_exact_fl v1.0.0 (REUSER of PR #467 publisher). "
            "License-to-entity identity layer: joins FL-state licensure to "
            "FL-state entity registry for contractor owner-identity resolution."
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
        cilb_tbl, sos_tbl, rows_left, rows_right = _materialize_inputs(storage_options)
        con, counts = _build_match_table(
            cilb_tbl, sos_tbl,
            bridge_run_id=bridge_run_id,
            generated_at_iso=started_at.isoformat(),
        )

        logger.info("-" * 60)
        logger.info("bridge tier distribution:")
        logger.info("  rows_matched:            %d", counts["rows_matched"])
        logger.info("    platinum (1:1):         %d", counts["rows_tier1"])
        logger.info("    gold     (1:N | N:1):   %d", counts["rows_tier2"])
        logger.info(
            "    silver   (N:M <=%d):    %d",
            COLLISION_THRESHOLD, counts["rows_tier3"],
        )
        logger.info("  rows_collision_rejected:  %d", counts["rows_collision_rejected"])

        if counts["rows_matched"] < MIN_ROWS_MATCHED:
            msg = (
                f"HARD FAIL: rows_matched={counts['rows_matched']:,} < "
                f"floor={MIN_ROWS_MATCHED:,}. "
                "Canonical normalizer-drift signal — check that both sides use "
                "scripts._lib.entity_name_normalize.normalize_entity_name."
            )
            logger.error(msg)
            fail_bridge_run(run_uuid, msg)
            return {"status": "failed", "error": msg, "counts": counts}

        lance_count = _write_bridge_lance(con, storage_options)
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
    """`modal run scripts/build_bridge_fl_cilb_sunbiz_lance.py::run`"""
    import json
    out = emit.remote()
    print(json.dumps(out, indent=2, default=str))

"""s5 - SBA x FL Sunbiz entities owner-identity bridge (Pattern B).

Pattern B exact-match bridge: SBA 7(a) / 504 borrowers (legal_name_normalized,
filtered to borrstate='FL') x FL Sunbiz entities (entity_name_normalized).
Normalizer is canonical scripts._lib.entity_name_normalize on BOTH sides.

Method: legal_name_state_exact_fl v1.0.0  (NEW state-variant - does NOT
overwrite the existing legal_name_state_exact_ca row from PR #464 SBA x CA SoS).
Bridge version: 1.0.0
COLLISION_THRESHOLD = 50 (rows where either side's fan-out exceeds this are
rejected - N:M up to 50x50 = 2,500-row joins per matched name).
MIN_ROWS_MATCHED = 242_161 (validator p176 - 0.5 x scaled-distinct-name overlap
of 484,322 from the 100K-record cordata0.txt baseline probe).

Inputs (directive p21 - join SoS ENTITIES, NOT officers):
    s3://dex-raw-landing-zone/polaris-warehouse/sba/borrowers_lance
        (filter pc.field('borrstate') == 'FL')
    s3://dex-raw-landing-zone/polaris-warehouse/sos/fl_entities_lance
        (full scan)

Output:
    s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_sos_fl_owner_lance
    (BTREE on sba_legal_name_normalized)

Normalizer (validator p1 / p189 - PR #459/#460 root cause):
    ONLY scripts._lib.entity_name_normalize on both sides. SBA side already
    produced by entity_name_normalize per existing SBA x Overture / SBA x PDL /
    SBA x CA SoS bridges; FL SoS side produced by s1's projector. NEVER use a
    different normalizer - the terminal-only vs global suffix-strip divergence
    in the divergent normalizer caused the UCC x Overture x PDL reverts within
    the last ~3h of repo history.

Provenance lifecycle (idempotent UPSERTs):
    register_match_method      -> ops.match_methods           (idempotent on method_name)
    register_match_method_version -> ops.match_method_versions (idempotent on (method_name, semver))
    register_bridge            -> ops.bridges                 (idempotent)
    start_bridge_run           -> ops.bridge_generation_runs  (status=running)
    write Lance + BTREE + tier counts
    complete_bridge_run        -> status=completed + metrics

The NEW state-variant legal_name_state_exact_fl creates new rows in
ops.match_methods and ops.match_method_versions; it DOES NOT overwrite the
existing legal_name_state_exact_ca row from PR #464 (different method_name,
different (method_name, semver) tuple).

Tier rule:
    platinum = 1:1
    gold     = 1:N | N:1
    silver   = N:M (both <= 50)
    rejected = >50 on either side

Modal hosting (validator p191): @app.function(cpu=8, memory=32768, timeout=10800)

Run via:
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      modal run scripts/build_bridge_sba_sos_fl_owner_lance.py::run
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

app = modal.App("data-engine-x-sba-sos-fl-owner-lance")

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

BRIDGE_NAME = "sba_sos_fl_owner"
METHOD_NAME = "legal_name_state_exact_fl"
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "1.0.0"

COLLISION_THRESHOLD = 50
# Validator p176 floor: 808,548 SBA FL distinct normalized names * 99,887 SoS
# sample distinct names (100K cordata0 sample) -> 3,842 sample overlap ->
# scale x12.61 (1.26M records in cordata0) x10 (10 shards) = 484,322 scaled
# distinct-name overlap -> x0.5 safety = 242,161 floor. Row-level fan-out
# (N:M up to COLLISION_THRESHOLD=50) lifts actual rows above this floor.
MIN_ROWS_MATCHED = 242_161

DATASET_SLUG = "sba_sos_fl_owner_lance"
SOURCE_LEFT = "sba_borrowers_lance"
SOURCE_RIGHT = "fl_entities_lance"

SBA_BORROWERS_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/sba/borrowers_lance"
)
SOS_ENTITIES_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/sos/fl_entities_lance"
)
BRIDGE_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_sos_fl_owner_lance"
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
    """Load SBA borrowers (borrstate='FL') + FL SoS entities into Arrow tables.

    SBA side: legal_name_normalized + key SBA cols, filter borrstate='FL'.
        Pre-dedup to distinct legal_name_normalized list (Python-side).
    SoS side: entity_name_normalized + entity_num + identity cols, is_valid filter.
    """
    import lance
    import pyarrow as pa
    import pyarrow.compute as pc

    logger.info("opening sba/borrowers_lance ...")
    sba_ds = lance.dataset(SBA_BORROWERS_LANCE_URI, storage_options=storage_options)
    sba_filter = pc.field("borrstate") == "FL"
    sba_tbl = sba_ds.scanner(
        columns=["legal_name_normalized", "borrstate"],
        filter=sba_filter,
    ).to_table()
    rows_sba_raw = len(sba_tbl)
    logger.info("  sba borrowers_lance (borrstate=FL): %d rows", rows_sba_raw)

    # Pre-dedup SBA side: distinct legal_name_normalized
    names = sba_tbl.column("legal_name_normalized").to_pylist()
    distinct_sba: set[str] = set()
    for nm in names:
        if not nm:
            continue
        if isinstance(nm, str) and len(nm) >= 2:
            distinct_sba.add(nm)
    del sba_tbl, names

    logger.info(
        "  sba_branded (distinct legal_name_normalized, FL only): %d names",
        len(distinct_sba),
    )

    sba_branded_arrow = pa.table(
        {
            "sba_legal_name_normalized": pa.array(
                sorted(distinct_sba), type=pa.string()
            ),
        }
    )
    del distinct_sba

    logger.info("opening sos/fl_entities_lance ...")
    sos_ds = lance.dataset(SOS_ENTITIES_LANCE_URI, storage_options=storage_options)
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
    logger.info("  sos fl_entities_lance (post-filter): %d rows", rows_sos)

    return sba_branded_arrow, sos_tbl, rows_sba_raw, rows_sos


def _build_match_table(
    sba_branded_arrow,
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

    con.register("sba_branded", sba_branded_arrow)
    con.register("sos_entities", sos_tbl)

    rows_sba_reg = con.execute("SELECT COUNT(*) FROM sba_branded").fetchone()[0]
    rows_sos_reg = con.execute("SELECT COUNT(*) FROM sos_entities").fetchone()[0]
    logger.info(
        "  registered: sba_branded=%d  sos_entities=%d",
        rows_sba_reg, rows_sos_reg,
    )

    # 1. Inner join on normalized name; SBA side is already distinct names.
    con.execute(
        """
        CREATE TEMP TABLE matched AS
        SELECT
            s.sba_legal_name_normalized,
            e.entity_num                AS entity_num,
            e.entity_name               AS sos_entity_name,
            e.entity_name_normalized    AS sos_entity_name_normalized,
            e.status                    AS sos_status,
            e.filing_type               AS sos_filing_type,
            e.city                      AS sos_city,
            e.state                     AS sos_state,
            e.zip                       AS sos_zip,
            e.fei_number                AS sos_fei_number,
            e.file_date                 AS sos_file_date,
            e.last_transaction_date     AS sos_last_transaction_date,
            e.registered_agent_name     AS sos_registered_agent_name,
            e.registered_agent_type     AS sos_registered_agent_type
        FROM sba_branded s
        JOIN sos_entities e
          ON s.sba_legal_name_normalized = e.entity_name_normalized
        """
    )
    rows_matched_pre = con.execute("SELECT COUNT(*) FROM matched").fetchone()[0]
    logger.info("  matched (pre-tier): %d rows", rows_matched_pre)

    # 2. Fan-out counts. SBA side is already distinct (sba_fan_out=1); SoS
    #    fan-out comes from same-normalized-name entities across multiple
    #    FL SoS records.
    con.execute(
        """
        CREATE TEMP TABLE sos_fanout AS
        SELECT sba_legal_name_normalized, COUNT(*) AS sos_fan_out
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
            m.sba_legal_name_normalized  AS match_value_normalized,
            'FL'                         AS match_state,
            1                            AS sba_fan_out,
            sf.sos_fan_out,
            CASE
                WHEN sf.sos_fan_out > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN sf.sos_fan_out = 1
                    THEN 'platinum'
                WHEN sf.sos_fan_out <= {COLLISION_THRESHOLD}
                    THEN 'gold'
                ELSE 'silver'
            END                              AS confidence_tier,
            TIMESTAMP '{generated_at_iso}'   AS generated_at,
            '{BRIDGE_VERSION}'               AS bridge_version,
            '{bridge_run_id}'                AS bridge_run_id
        FROM matched m
        JOIN sos_fanout sf USING (sba_legal_name_normalized)
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
    """Write bridge_match to Lance via Arrow-bridge pattern + BTREE."""
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
            ds.create_scalar_index("sba_legal_name_normalized", index_type="BTREE", replace=True)
            logger.info("BTREE on sba_legal_name_normalized: OK")
        except Exception as e:
            logger.error("BTREE on sba_legal_name_normalized FAILED: %s", e)
            raise
        try:
            ds.create_scalar_index("entity_num", index_type="BTREE", replace=True)
            logger.info("BTREE on entity_num: OK")
        except Exception as e:
            logger.error("BTREE on entity_num FAILED: %s", e)
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
    """Build the SBA x FL SoS owner-identity bridge."""
    sys.path.insert(0, "/root")
    from scripts._lib.entity_name_normalize import (  # noqa: F401
        __version__ as NORMALIZER_VERSION,
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
        "bridge: %s  method=%s v%s  normalizer=v%s",
        BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER, NORMALIZER_VERSION,
    )
    logger.info("inputs: %s + %s", SBA_BORROWERS_LANCE_URI, SOS_ENTITIES_LANCE_URI)
    logger.info("output: %s", BRIDGE_LANCE_URI)

    # Provenance: register method + method version + bridge (idempotent).
    # legal_name_state_exact_fl is a NEW state-variant - does NOT overwrite
    # the existing legal_name_state_exact_ca row from PR #464.
    register_match_method(
        method_name=METHOD_NAME,
        description=(
            "FL state-variant of legal_name_state_exact - exact equality on "
            "legal-entity-name normalization, scoped to FL SBA borrowers x "
            "FL Sunbiz entities. _lib/entity_name_normalize "
            f"v{NORMALIZER_VERSION} on both sides."
        ),
    )
    register_match_method_version(
        method_name=METHOD_NAME,
        semver=METHOD_SEMVER,
        normalizer_module="_lib/entity_name_normalize.py",
        normalizer_version=NORMALIZER_VERSION,
        blacklist_module="_lib/entity_name_normalize.py",
        blacklist_version=NORMALIZER_VERSION,
        tier_rule_description=(
            "platinum=1:1; gold=1:N or N:1; silver=N:M <=50; rejected=>50 "
            f"(COLLISION_THRESHOLD={COLLISION_THRESHOLD})"
        ),
        rejection_rule_description="fan-out >50 collapsed to rows_collision_rejected",
        input_columns_left=["legal_name_normalized", "borrstate"],
        input_columns_right=["entity_name_normalized", "state"],
        output_value_description=(
            "normalize_entity_name(sba.legal_name_normalized) == "
            "normalize_entity_name(sos.entity_name_normalized) for borrstate='FL'"
        ),
    )
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "SBA x FL Sunbiz entities owner-identity bridge - legal-name exact "
            "match for borrstate='FL'. Attaches FL LLC owner identity (entity "
            "details + fei_number + registered-agent name) to FL SBA borrower "
            "cohorts. Chainable to fl_officers_lance via entity_num for named "
            "officer/director identity."
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
        sba_branded_arrow, sos_tbl, rows_left, rows_right = _materialize_inputs(
            storage_options
        )
        con, counts = _build_match_table(
            sba_branded_arrow, sos_tbl,
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
                f"floor={MIN_ROWS_MATCHED:,}"
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
    """`modal run scripts/build_bridge_sba_sos_fl_owner_lance.py::run`"""
    import json
    out = emit.remote()
    print(json.dumps(out, indent=2, default=str))

"""SAM × CA SoS principals Pattern A enriched-cohort Lance emit.

Chains the just-shipped `bridges/sam_sos_ca_entities_lance` (84,481
sam_uei ↔ sos_entity_num pairs, PR #560) against `sos/ca_principals_lance`
(18,670,722 rows) to produce one row per (sam_uei, sos_entity_num, principal)
triple.

Surfaces the corporate-officer identity layer (CEO, CFO, Manager, Member,
Director, Agent for Service, etc.) for SAM-registered California entities.
SAM POCs are sales/admin contacts — this cohort reaches the executive rank.

Pattern A enriched-cohort discipline (DATA-FACTORY-ARCHITECTURE-PATTERNS.md
§"Pattern A enriched-cohort emit", L28):
  - Does NOT register a new `ops.bridges` row (no new cross-source identity match).
  - Does NOT register a new `ops.match_method_versions` row (no new method).
  - Does NOT call any match-method-registry helpers (L28 — Pattern B only).
  - Carries inherited `entities_bridge_run_id` per row (from the predecessor bridge)
    plus a fresh `cohort_bridge_run_id` UUID for this emit run's lineage.

Input LEFT (predecessor bridge, 84,481 rows, 19 cols):
    s3://dex-raw-landing-zone/polaris-warehouse/bridges/sam_sos_ca_entities_lance
    Projected 7 cols: sam_uei, sos_entity_num, entity_name_normalized,
    entity_status, confidence_tier, match_value, bridge_run_id

Input RIGHT (CA SoS principals, 18,670,722 rows, 16 cols):
    s3://dex-raw-landing-zone/polaris-warehouse/sos/ca_principals_lance
    Projected 12 cols: entity_num, first_name, middle_name, last_name,
    full_name_normalized, org_name, org_name_normalized, position_type,
    city, state, country, postal_code

JOIN: DuckDB INNER JOIN on bridge.sos_entity_num = principals.entity_num
    No filter at scanner layer — hash-join in DuckDB handles the work.
    Validator baseline: 272,591 rows deterministic across 3 runs.

Output per row: (sam_uei, sos_entity_num, sos_entity_name_normalized,
    sos_entity_status, principal_first_name, principal_middle_name,
    principal_last_name, principal_full_name_normalized, principal_org_name,
    principal_org_name_normalized, principal_position_type, principal_city,
    principal_state, principal_postal_code, principal_country,
    entities_bridge_run_id, entities_bridge_confidence_tier,
    entities_bridge_match_value, cohort_bridge_run_id, cohort_bridge_version,
    generated_at)

BTREE on: sam_uei, sos_entity_num, principal_full_name_normalized.

Predecessor: bridges/sam_sos_ca_entities_lance (PR #560, merged 2026-05-19).
Directive: ~/Desktop/hq/directives/2026-05-19-sam-sos-ca-principals-cohort.md
Validator: ~/Desktop/hq/scope-status/sam-sos-ca-principals-cohort/validator.json

Run (plain Python via Doppler, no Modal needed for local dev):
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      uv run --project . python3 scripts/build_bridge_sam_sos_ca_principals_lance.py --apply

Dry-run (probe + row count, no Lance write):
    uv run --project . python3 scripts/build_bridge_sam_sos_ca_principals_lance.py
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# sys.path.insert per PR #481 / PR #557 pattern — allows _lib imports from worktree root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Pattern A enriched-cohort: ONLY import lance_commit_lock from _lib.
# Per L28: match-method-registry helpers are Pattern B only — NOT imported here.
from scripts._lib.lance_commit_lock import lance_commit_lock

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

R2_BUCKET = "dex-raw-landing-zone"

BRIDGE_LANCE_URI = (
    f"s3://{R2_BUCKET}/polaris-warehouse/bridges/sam_sos_ca_entities_lance"
)
PRINCIPALS_LANCE_URI = (
    f"s3://{R2_BUCKET}/polaris-warehouse/sos/ca_principals_lance"
)
OUTPUT_LANCE_URI = (
    f"s3://{R2_BUCKET}/polaris-warehouse/bridges/sam_sos_ca_principals_lance"
)

DATASET_SLUG = "sam_sos_ca_principals_lance"
POLARIS_NAMESPACE = "bridges"

# Validator-calibrated floor: floor(0.70 × 272,591) = 190,813, rounded to 190,000
# per L# convention. Guards against schema regression, accidental tier-filter,
# entity-status-filter, or Active-only filter at emit time.
MIN_ROW_FLOOR = 190_000

COHORT_BRIDGE_VERSION = "1.0.0"

TMP_DIR = "/tmp/lance"

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _duckdb_conn():
    """Configure DuckDB session per directive §DuckDB tuning."""
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")
    return con


def _register_polaris(table_name: str, doc: str) -> None:
    """Register Lance dataset in Polaris Generic Table registry.

    Mirrors build_bridge_sam_construction_contractors_lance.py:108-121 (PR #557).
    Non-fatal if POLARIS_PUBLIC_URL not set (state-procurement runbook §"Gotchas" #3).
    """
    script = Path(__file__).resolve().parent / "init_polaris_lance_generic.py"
    cmd = [
        sys.executable, str(script),
        "--namespace", POLARIS_NAMESPACE,
        "--table", table_name,
        "--doc", doc,
    ]
    logger.info("registering Polaris: %s.%s", POLARIS_NAMESPACE, table_name)
    try:
        subprocess.run(cmd, check=True, timeout=60)
        logger.info("Polaris registration OK: %s.%s", POLARIS_NAMESPACE, table_name)
    except Exception as exc:
        logger.warning("Polaris registration error (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Data loading (PyLance scanner per Pattern A canonical PR #469 L200-286)
# ---------------------------------------------------------------------------

def _load_bridge_arrow(storage_options: dict):
    """Load predecessor bridge projecting the 7 validator-confirmed columns.

    Column names are SOURCE names from bridges/sam_sos_ca_entities_lance schema.
    DuckDB renames them with prefixes in the JOIN SELECT — NOT at the scanner layer.
    Per p1 prediction: DO NOT use rename targets (e.g. sos_entity_name_normalized)
    as source scanner column names.
    """
    import lance

    logger.info("opening bridges/sam_sos_ca_entities_lance (7 cols) ...")
    ds = lance.dataset(BRIDGE_LANCE_URI, storage_options=storage_options)
    # Validator-probed columns (verbatim names from bridge schema, PR #560):
    bridge_cols = [
        "sam_uei",
        "sos_entity_num",
        "entity_name_normalized",  # renamed sos_entity_name_normalized in DuckDB SELECT
        "entity_status",           # renamed sos_entity_status in DuckDB SELECT
        "confidence_tier",         # renamed entities_bridge_confidence_tier in DuckDB SELECT
        "match_value",             # renamed entities_bridge_match_value in DuckDB SELECT
        "bridge_run_id",           # renamed entities_bridge_run_id in DuckDB SELECT
    ]
    tbl = ds.scanner(columns=bridge_cols).to_table()
    logger.info(
        "  bridges/sam_sos_ca_entities_lance: %d rows × %d cols",
        tbl.num_rows, tbl.num_columns,
    )
    return tbl


def _load_principals_arrow(storage_options: dict):
    """Load CA SoS principals projecting the 12 validator-confirmed columns.

    Column names are SOURCE names from sos/ca_principals_lance schema.
    DuckDB renames them with principal_* prefix in the JOIN SELECT.
    """
    import lance

    logger.info("opening sos/ca_principals_lance (12 cols) ...")
    ds = lance.dataset(PRINCIPALS_LANCE_URI, storage_options=storage_options)
    # Validator-probed columns (verbatim names from principals schema):
    principal_cols = [
        "entity_num",              # JOIN key: bridge.sos_entity_num = principals.entity_num
        "first_name",              # renamed principal_first_name in DuckDB SELECT
        "middle_name",             # renamed principal_middle_name
        "last_name",               # renamed principal_last_name
        "full_name_normalized",    # renamed principal_full_name_normalized (BTREE target)
        "org_name",                # renamed principal_org_name
        "org_name_normalized",     # renamed principal_org_name_normalized
        "position_type",           # renamed principal_position_type
        "city",                    # renamed principal_city
        "state",                   # renamed principal_state
        "country",                 # renamed principal_country
        "postal_code",             # renamed principal_postal_code
    ]
    tbl = ds.scanner(columns=principal_cols).to_table()
    logger.info(
        "  sos/ca_principals_lance: %d rows × %d cols",
        tbl.num_rows, tbl.num_columns,
    )
    return tbl


# ---------------------------------------------------------------------------
# Emit
# ---------------------------------------------------------------------------

def emit() -> int:
    """Pattern A enriched-cohort emit — bridge × principals INNER JOIN.

    Returns rows_emitted (integer). Raises RuntimeError on floor failure.
    """
    import lance

    os.environ["TMPDIR"] = TMP_DIR
    os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)

    # Per-emit provenance (L28 Pattern A enriched-cohort):
    # cohort_bridge_run_id = fresh UUID for THIS emit; identical on every row.
    # entities_bridge_run_id is inherited per-row from the predecessor bridge.
    cohort_bridge_run_id = str(uuid.uuid4())
    generated_at_dt = datetime.now(tz=timezone.utc)
    generated_at_iso = generated_at_dt.isoformat()

    logger.info(
        "cohort emit starting — cohort_bridge_run_id=%s  generated_at=%s",
        cohort_bridge_run_id, generated_at_iso,
    )
    logger.info("left:   %s", BRIDGE_LANCE_URI)
    logger.info("right:  %s", PRINCIPALS_LANCE_URI)
    logger.info("output: %s", OUTPUT_LANCE_URI)

    storage_options = _storage_options()

    # ---- Step 1: load upstream Lance datasets via PyLance scanners ---- #
    bridge_arrow = _load_bridge_arrow(storage_options)
    principals_arrow = _load_principals_arrow(storage_options)

    # ---- Step 2: INNER JOIN in DuckDB (NOT Lance-native) ---- #
    con = _duckdb_conn()
    con.register("bridge", bridge_arrow)
    con.register("principals", principals_arrow)

    logger.info("running INNER JOIN bridge × principals ...")
    # NOTE: No filter on confidence_tier (constraint #10 — all 3 tiers propagate).
    # NOTE: No filter on entity_status (constraint #11 — all status values propagate).
    # NOTE: Single-key join only — no OR-predicate at scanner layer (constraint #15).
    con.execute(
        f"""
        CREATE TEMP TABLE cohort AS
        SELECT
            b.sam_uei,
            b.sos_entity_num,
            b.entity_name_normalized               AS sos_entity_name_normalized,
            b.entity_status                        AS sos_entity_status,
            p.first_name                           AS principal_first_name,
            p.middle_name                          AS principal_middle_name,
            p.last_name                            AS principal_last_name,
            p.full_name_normalized                 AS principal_full_name_normalized,
            p.org_name                             AS principal_org_name,
            p.org_name_normalized                  AS principal_org_name_normalized,
            p.position_type                        AS principal_position_type,
            p.city                                 AS principal_city,
            p.state                                AS principal_state,
            p.postal_code                          AS principal_postal_code,
            p.country                              AS principal_country,
            b.bridge_run_id                        AS entities_bridge_run_id,
            b.confidence_tier                      AS entities_bridge_confidence_tier,
            b.match_value                          AS entities_bridge_match_value,
            CAST('{cohort_bridge_run_id}' AS VARCHAR)  AS cohort_bridge_run_id,
            '{COHORT_BRIDGE_VERSION}'              AS cohort_bridge_version,
            TIMESTAMP '{generated_at_iso}'         AS generated_at
        FROM bridge b
        INNER JOIN principals p ON p.entity_num = b.sos_entity_num
        """
    )

    rows_emitted = con.execute("SELECT COUNT(*) FROM cohort").fetchone()[0]
    logger.info("cohort JOIN rows: %d", rows_emitted)

    # Forensic breakdown for logging
    forensic = con.execute(
        """
        SELECT
            COUNT(DISTINCT sam_uei)                     AS distinct_uei,
            COUNT(DISTINCT sos_entity_num)              AS distinct_entity_num,
            COUNT(*) FILTER (WHERE entities_bridge_confidence_tier = 'platinum') AS platinum,
            COUNT(*) FILTER (WHERE entities_bridge_confidence_tier = 'gold')     AS gold,
            COUNT(*) FILTER (WHERE entities_bridge_confidence_tier = 'silver')   AS silver,
            COUNT(DISTINCT sos_entity_status)           AS distinct_status_count
        FROM cohort
        """
    ).fetchone()
    logger.info(
        "cohort: distinct_uei=%d  distinct_entity_num=%d  "
        "platinum=%d  gold=%d  silver=%d  distinct_status=%d",
        forensic[0], forensic[1], forensic[2], forensic[3],
        forensic[4], forensic[5],
    )

    # ---- HARD FAIL: floor check BEFORE Lance write ---- #
    if rows_emitted < MIN_ROW_FLOOR:
        msg = (
            f"HARD FAIL: rows_emitted={rows_emitted:,} < floor={MIN_ROW_FLOOR:,}. "
            f"Likely cause: schema regression (wrong join column), accidental "
            f"tier-filter, or entity-status-filter. Aborting before Lance write."
        )
        logger.error(msg)
        sys.exit(1)

    # ---- Step 3: Lance write inside commit_lock ---- #
    import time
    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        reader = con.execute("SELECT * FROM cohort").to_arrow_reader(
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
        lance_rows = ds.count_rows()
        logger.info(
            "wrote %d rows in %.1fs (Lance version=%s)",
            lance_rows, write_dur, ds.version,
        )

    # ---- Step 4: BTREE on 3 columns (constraint #2, #3, #4) ---- #
    t_btree = time.time()
    for col in ("sam_uei", "sos_entity_num", "principal_full_name_normalized"):
        try:
            ds.create_scalar_index(col, index_type="BTREE", replace=True)
            logger.info("BTREE on %s: OK", col)
        except Exception as exc:
            logger.error("BTREE on %s FAILED: %s", col, exc)
            raise

    # ---- Step 5: compact + cleanup_old_versions (constraint #17) ---- #
    try:
        ds.optimize.compact_files()
        ds.cleanup_old_versions(older_than=timedelta(days=7))
        logger.info("optimize + cleanup_old_versions OK")
    except Exception as exc:
        logger.warning("optimize failed (non-fatal): %s", exc)

    btree_dur = time.time() - t_btree

    logger.info(
        "cohort emit complete: rows=%d  write=%.1fs  btree=%.1fs  "
        "cohort_bridge_run_id=%s",
        lance_rows, write_dur, btree_dur, cohort_bridge_run_id,
    )

    # ---- Step 6: Polaris registration (constraint #19) ---- #
    _register_polaris(
        DATASET_SLUG,
        (
            "bridges.sam_sos_ca_principals_lance — SAM × CA SoS principals enriched-cohort "
            "(Pattern A). Joins bridges.sam_sos_ca_entities_lance to sos.ca_principals_lance "
            "for corporate-officer identity layer (CEO, CFO, Manager, etc.) on SAM-registered "
            "CA entities. BTREE on sam_uei + sos_entity_num + principal_full_name_normalized. "
            "Provenance: inherited entities_bridge_run_id + fresh cohort_bridge_run_id (L28). "
            "Predecessor PR #560. Directive: 2026-05-19-sam-sos-ca-principals-cohort.md."
        ),
    )

    return lance_rows


# ---------------------------------------------------------------------------
# main (argparse CLI with --apply flag)
# ---------------------------------------------------------------------------

def main() -> int:
    """CLI entrypoint.

    Without --apply: dry-run (probe only, report row counts, exit 0).
    With --apply:    full pipeline (probe, floor check, Lance write, BTREE, optimize).
    """
    parser = argparse.ArgumentParser(
        description=(
            "SAM × CA SoS principals Pattern A enriched-cohort Lance emit. "
            "Joins bridges/sam_sos_ca_entities_lance × sos/ca_principals_lance."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Execute full pipeline: Lance write + BTREE + optimize. "
             "Without this flag: dry-run (probe only, no Lance write).",
    )
    args = parser.parse_args()

    os.environ["TMPDIR"] = TMP_DIR
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)

    if not args.apply:
        # Dry-run: load inputs, probe join row count, report.
        logger.info("DRY-RUN: probing join without writing to R2 ...")
        import lance
        import duckdb

        storage_options = _storage_options()
        bridge_arrow = _load_bridge_arrow(storage_options)
        principals_arrow = _load_principals_arrow(storage_options)

        con = _duckdb_conn()
        con.register("bridge", bridge_arrow)
        con.register("principals", principals_arrow)
        rows = con.execute(
            """
            SELECT COUNT(*) FROM bridge b
            INNER JOIN principals p ON p.entity_num = b.sos_entity_num
            """
        ).fetchone()[0]
        distinct_uei = con.execute(
            """
            SELECT COUNT(DISTINCT b.sam_uei) FROM bridge b
            INNER JOIN principals p ON p.entity_num = b.sos_entity_num
            """
        ).fetchone()[0]
        logger.info(
            "DRY-RUN: rows_emitted=%d  distinct_uei=%d  floor=%d  "
            "floor_ok=%s",
            rows, distinct_uei, MIN_ROW_FLOOR, rows >= MIN_ROW_FLOOR,
        )
        logger.info("DRY-RUN complete — pass --apply to write to R2.")
        return 0

    rows_emitted = emit()
    logger.info("SUCCESS: rows_emitted=%d", rows_emitted)
    return 0


if __name__ == "__main__":
    sys.exit(main())

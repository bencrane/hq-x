"""SAM × FL Sunbiz officers Pattern A enriched-cohort Lance emit.

Chains the just-shipped `bridges/sam_sos_fl_entities_lance` (56,078
sam_uei ↔ sos_entity_num pairs, PR #563) against `sos/fl_officers_lance`
(20,593,089 rows) to produce one row per (sam_uei, sos_entity_num, officer)
triple.

Surfaces the corporate-officer identity layer (Sunbiz title codes MGR, P, D,
AMBR, CEO, VP, Pres, etc.) for SAM-registered Florida entities.
SAM POCs are sales/admin contacts — this cohort reaches the executive/officer
rank per FL Sunbiz quarterly data.

FL mirror of `bridges/sam_sos_ca_principals_lance` (PR #561). FL Sunbiz
officers schema DIFFERS from CA SoS principals:
  - officer_title (FL, 1-4 char Sunbiz codes: MGR/P/D/AMBR/etc.) vs
    position_type (CA, full strings like 'CEO'/'Director')
  - officer_type discriminator (P=person / C=corporate) — FL only
  - single-line officer_address instead of CA's multi-line address fields
  - NO org_name, org_name_normalized, country, postal_code (FL Sunbiz
    fixed-width record doesn't carry these)

Pattern A enriched-cohort discipline (DATA-FACTORY-ARCHITECTURE-PATTERNS.md
§"Pattern A enriched-cohort emit", L28):
  - Does NOT register a new `ops.bridges` row (no new cross-source identity match).
  - Does NOT register a new `ops.match_method_versions` row (no new method).
  - Does NOT call any match-method-registry helpers (L28 — Pattern B only).
  - Carries inherited `entities_bridge_run_id` per row (from the predecessor bridge)
    plus a fresh `cohort_bridge_run_id` UUID for this emit run's lineage.

Input LEFT (predecessor bridge, 56,078 rows, 17 cols):
    s3://dex-raw-landing-zone/polaris-warehouse/bridges/sam_sos_fl_entities_lance
    Projected 7 cols: sam_uei, sos_entity_num, entity_name_normalized,
    entity_status, confidence_tier, match_value, bridge_run_id

Input RIGHT (FL Sunbiz officers, 20,593,089 rows, 14 cols):
    s3://dex-raw-landing-zone/polaris-warehouse/sos/fl_officers_lance
    Projected 12 cols (validator-confirmed FL schema — NOT CA schema):
    entity_num, first_name, middle_name, last_name, full_name_normalized,
    officer_title, officer_type, officer_name, officer_address,
    officer_city, officer_state, officer_zip4

JOIN: DuckDB INNER JOIN on bridge.sos_entity_num = officers.entity_num
    No filter at scanner layer — hash-join in DuckDB handles the work.
    Validator baseline: 121,678 rows deterministic across 3 runs.

Output per row: (sam_uei, sos_entity_num, sos_entity_name_normalized,
    sos_entity_status, officer_first_name, officer_middle_name,
    officer_last_name, officer_full_name_normalized, officer_position_type,
    officer_type, officer_raw_name, officer_address, officer_city,
    officer_state, officer_zip4, entities_bridge_run_id,
    entities_bridge_confidence_tier, entities_bridge_match_value,
    cohort_bridge_run_id, cohort_bridge_version, generated_at)

BTREE on: sam_uei, sos_entity_num, officer_full_name_normalized.

Predecessor: bridges/sam_sos_fl_entities_lance (PR #563, merged 2026-05-19).
Precedent:   bridges/sam_sos_ca_principals_lance (PR #561, merged 2026-05-19).
Directive: ~/Desktop/hq/directives/2026-05-19-sam-sos-fl-officers-cohort.md
Validator: ~/Desktop/hq/scope-status/sam-sos-fl-officers-cohort/validator.json

Run (plain Python via Doppler, no Modal needed for local dev):
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      uv run --project . python3 scripts/build_bridge_sam_sos_fl_officers_lance.py --apply

Dry-run (probe + row count, no Lance write):
    uv run --project . python3 scripts/build_bridge_sam_sos_fl_officers_lance.py
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
    f"s3://{R2_BUCKET}/polaris-warehouse/bridges/sam_sos_fl_entities_lance"
)
OFFICERS_LANCE_URI = (
    f"s3://{R2_BUCKET}/polaris-warehouse/sos/fl_officers_lance"
)
OUTPUT_LANCE_URI = (
    f"s3://{R2_BUCKET}/polaris-warehouse/bridges/sam_sos_fl_officers_lance"
)

DATASET_SLUG = "sam_sos_fl_officers_lance"
POLARIS_NAMESPACE = "bridges"

# Validator-calibrated floor: floor(0.70 × 121,678) = 85,175, rounded to 85,000
# per L# convention. Guards against schema regression (wrong join column),
# accidental tier-filter, entity-status-filter, or officer-type-filter at emit time.
MIN_ROW_FLOOR = 85_000

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

    Mirrors build_bridge_sam_sos_ca_principals_lance.py:_register_polaris (PR #561).
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
# Data loading (PyLance scanner per Pattern A canonical PR #469 / PR #561)
# ---------------------------------------------------------------------------

def _load_bridge_arrow(storage_options: dict):
    """Load predecessor FL entities bridge projecting the 7 validator-confirmed columns.

    Column names are SOURCE names from bridges/sam_sos_fl_entities_lance schema
    (validator.json#schema_verification.predecessor_bridge.required_cols_for_cohort).
    DuckDB renames them with prefixes in the JOIN SELECT — NOT at the scanner layer.
    Per p1 prediction: DO NOT use rename targets (e.g. sos_entity_name_normalized)
    as source scanner column names.
    """
    import lance

    logger.info("opening bridges/sam_sos_fl_entities_lance (7 cols) ...")
    ds = lance.dataset(BRIDGE_LANCE_URI, storage_options=storage_options)
    # Validator-probed columns (verbatim names from FL entities bridge schema, PR #563):
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
        "  bridges/sam_sos_fl_entities_lance: %d rows × %d cols",
        tbl.num_rows, tbl.num_columns,
    )
    return tbl


def _load_officers_arrow(storage_options: dict):
    """Load FL Sunbiz officers projecting the 12 validator-confirmed columns.

    Column names are SOURCE names from sos/fl_officers_lance schema
    (validator.json#schema_verification.fl_officers.columns — 14-col FL schema).
    DuckDB renames them with officer_* prefix in the JOIN SELECT.

    FL schema DIFFERS from CA principals — do NOT project CA column names:
      - officer_title (FL) vs position_type (CA)
      - NO org_name, org_name_normalized, country, postal_code (absent in FL)
      - FL adds: officer_type (P/C), officer_name (raw concat), officer_address
        (single-line), officer_city, officer_state, officer_zip4
    """
    import lance

    logger.info("opening sos/fl_officers_lance (12 cols) ...")
    ds = lance.dataset(OFFICERS_LANCE_URI, storage_options=storage_options)
    # Validator-probed columns (verbatim names from FL officers schema, 14-col total):
    officer_cols = [
        "entity_num",          # JOIN key: bridge.sos_entity_num = officers.entity_num
        "first_name",          # renamed officer_first_name in DuckDB SELECT
        "middle_name",         # renamed officer_middle_name
        "last_name",           # renamed officer_last_name
        "full_name_normalized",  # renamed officer_full_name_normalized (BTREE target)
        "officer_title",       # renamed officer_position_type in DuckDB SELECT
                               # (FL Sunbiz 1-4 char codes: MGR/P/D/AMBR/etc. — propagate verbatim)
        "officer_type",        # P=person / C=corporate — FL-specific, renamed officer_type
        "officer_name",        # raw concat from FL source — renamed officer_raw_name
        "officer_address",     # single-line address — FL fixed-width, renamed officer_address
        "officer_city",        # renamed officer_city
        "officer_state",       # renamed officer_state
        "officer_zip4",        # renamed officer_zip4
    ]
    tbl = ds.scanner(columns=officer_cols).to_table()
    logger.info(
        "  sos/fl_officers_lance: %d rows × %d cols",
        tbl.num_rows, tbl.num_columns,
    )
    return tbl


# ---------------------------------------------------------------------------
# Emit
# ---------------------------------------------------------------------------

def emit() -> int:
    """Pattern A enriched-cohort emit — bridge × officers INNER JOIN.

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
    logger.info("right:  %s", OFFICERS_LANCE_URI)
    logger.info("output: %s", OUTPUT_LANCE_URI)

    storage_options = _storage_options()

    # ---- Step 1: load upstream Lance datasets via PyLance scanners ---- #
    bridge_arrow = _load_bridge_arrow(storage_options)
    officers_arrow = _load_officers_arrow(storage_options)

    # ---- Step 2: INNER JOIN in DuckDB (NOT Lance-native) ---- #
    con = _duckdb_conn()
    con.register("bridge", bridge_arrow)
    con.register("officers", officers_arrow)

    logger.info("running INNER JOIN bridge × officers ...")
    # NOTE: No filter on confidence_tier (constraint #9 — all 3 tiers propagate).
    # NOTE: No filter on entity_status (constraint #10 — both 'A' and 'I' propagate).
    # NOTE: No filter on officer_type (constraint #7 — both 'P' and 'C' propagate).
    # NOTE: Single-key join only — no OR-predicate at scanner layer (constraint #14 / L51).
    # NOTE: officer_title renamed → officer_position_type for downstream parallelism with
    #       CA principals cohort (which uses principal_position_type). The Sunbiz 1-4 char
    #       codes (MGR/P/D/AMBR/Pres/Mana/etc.) are propagated verbatim — no translation.
    con.execute(
        f"""
        CREATE TEMP TABLE cohort AS
        SELECT
            b.sam_uei,
            b.sos_entity_num,
            b.entity_name_normalized               AS sos_entity_name_normalized,
            b.entity_status                        AS sos_entity_status,
            o.first_name                           AS officer_first_name,
            o.middle_name                          AS officer_middle_name,
            o.last_name                            AS officer_last_name,
            o.full_name_normalized                 AS officer_full_name_normalized,
            o.officer_title                        AS officer_position_type,
            o.officer_type                         AS officer_type,
            o.officer_name                         AS officer_raw_name,
            o.officer_address                      AS officer_address,
            o.officer_city                         AS officer_city,
            o.officer_state                        AS officer_state,
            o.officer_zip4                         AS officer_zip4,
            b.bridge_run_id                        AS entities_bridge_run_id,
            b.confidence_tier                      AS entities_bridge_confidence_tier,
            b.match_value                          AS entities_bridge_match_value,
            CAST('{cohort_bridge_run_id}' AS VARCHAR)  AS cohort_bridge_run_id,
            '{COHORT_BRIDGE_VERSION}'              AS cohort_bridge_version,
            TIMESTAMP '{generated_at_iso}'         AS generated_at
        FROM bridge b
        INNER JOIN officers o ON o.entity_num = b.sos_entity_num
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
            COUNT(DISTINCT sos_entity_status)           AS distinct_status_count,
            COUNT(*) FILTER (WHERE officer_type = 'P')  AS person_officers,
            COUNT(*) FILTER (WHERE officer_type = 'C')  AS corporate_officers
        FROM cohort
        """
    ).fetchone()
    logger.info(
        "cohort: distinct_uei=%d  distinct_entity_num=%d  "
        "platinum=%d  gold=%d  silver=%d  distinct_status=%d  "
        "person_officers=%d  corporate_officers=%d",
        forensic[0], forensic[1], forensic[2], forensic[3],
        forensic[4], forensic[5], forensic[6], forensic[7],
    )

    # ---- HARD FAIL: floor check BEFORE Lance write ---- #
    if rows_emitted < MIN_ROW_FLOOR:
        msg = (
            f"HARD FAIL: rows_emitted={rows_emitted:,} < floor={MIN_ROW_FLOOR:,}. "
            f"Likely cause: schema regression (wrong join column), accidental "
            f"tier-filter, entity-status-filter, or officer-type-filter. "
            f"Aborting before Lance write."
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
    for col in ("sam_uei", "sos_entity_num", "officer_full_name_normalized"):
        try:
            ds.create_scalar_index(col, index_type="BTREE", replace=True)
            logger.info("BTREE on %s: OK", col)
        except Exception as exc:
            logger.error("BTREE on %s FAILED: %s", col, exc)
            raise

    # ---- Step 5: compact + cleanup_old_versions (constraint #16) ---- #
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

    # ---- Step 6: Polaris registration (constraint #18) ---- #
    _register_polaris(
        DATASET_SLUG,
        (
            "bridges.sam_sos_fl_officers_lance — SAM × FL Sunbiz officers enriched-cohort "
            "(Pattern A). Joins bridges.sam_sos_fl_entities_lance to sos.fl_officers_lance "
            "for corporate-officer identity layer (Sunbiz title codes: MGR/P/D/AMBR/etc.) "
            "on SAM-registered FL entities. BTREE on sam_uei + sos_entity_num + "
            "officer_full_name_normalized. Provenance: inherited entities_bridge_run_id + "
            "fresh cohort_bridge_run_id (L28). Predecessor PR #563. "
            "Directive: 2026-05-19-sam-sos-fl-officers-cohort.md."
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
            "SAM × FL Sunbiz officers Pattern A enriched-cohort Lance emit. "
            "Joins bridges/sam_sos_fl_entities_lance × sos/fl_officers_lance."
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
        officers_arrow = _load_officers_arrow(storage_options)

        con = _duckdb_conn()
        con.register("bridge", bridge_arrow)
        con.register("officers", officers_arrow)
        rows = con.execute(
            """
            SELECT COUNT(*) FROM bridge b
            INNER JOIN officers o ON o.entity_num = b.sos_entity_num
            """
        ).fetchone()[0]
        distinct_uei = con.execute(
            """
            SELECT COUNT(DISTINCT b.sam_uei) FROM bridge b
            INNER JOIN officers o ON o.entity_num = b.sos_entity_num
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

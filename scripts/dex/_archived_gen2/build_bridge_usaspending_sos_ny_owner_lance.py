"""USAspending × NY SoS Active Corporations Pattern B Lance bridge.

Pattern B exact-match bridge: USAspending federal contract recipients
(contracts_lance filtered recipient_state_code='NY' + DISTINCT (recipient_uei,
recipient_name) → normalized on-the-fly Python-side) × NY DoS Active
Corporations (entity_name_normalized pre-normalized at PRs #567+#568).

Method: legal_name_state_exact_ny v1.0.0 (REUSED — the method + version rows
were registered by PR #513's build_bridge_sba_ny_contracts_lance.py on 2026-05-18;
this script ONLY calls register_bridge + start_bridge_run +
complete_bridge_run + fail_bridge_run per L21 — the method-definition
and method-version-definition helpers are INTENTIONALLY OMITTED; calling
them would UPSERT over the SBA-shape input_columns_left config
{legal_name_normalized, borrstate} and corrupt sba_ny_contracts,
sba_ny_usaspending, sba_ny_sam, sba_ny_nyc_contracts, sba_ny_mta,
sba_ny_local_authority, and sam_sos_ny_entities bridges' provenance trail).
This is the EIGHTH REUSER of this method (publisher: sba_ny_contracts PR #513;
prior reusers: sba_ny_usaspending, sba_ny_sam, sba_ny_nyc_contracts, sba_ny_mta,
sba_ny_local_authority, sam_sos_ny_entities from PR #569).

USAspending-side PK: recipient_uei. Output column: recipient_uei.
NY DoS PK: dos_id. Output column: sos_dos_id (NY-specific naming per validator p3).

USAspending-side filter: recipient_state_code='NY' (single column — simpler than
SAM's 2-col OR-filter; USAspending contracts_lance has one state column).

Normalizer (CRITICAL — use _lib Python-side on raw recipient_name ONLY):
    USAspending has NO pre-normalized name column. With ~5,200 LEFT rows
    post-NY-filter + DISTINCT, call normalize_entity_name(name) in a
    Python list comprehension and attach recipient_name_normalized as an extra
    column on the Arrow table BEFORE registering with DuckDB. NOT a DuckDB UDF.
    NY DoS pre-normalized entity_name_normalized agrees 100% with _lib on
    100-row sample (validator pre-flight 1) — join on NY pre-norm directly.

NY DoS status column: NONE — the source dataset is "Active Corporations" (all rows
are Active by dataset definition). Build script projects the constant string
'A' AS sos_entity_status to preserve CA/FL bridge output column shape for
downstream consumers (validator p4 — avoids missing-column error).

Bridge version: 1.0.0
COLLISION_THRESHOLD = 50 (rows where either side's fan-out exceeds this are
rejected — N:M up to 50×50 = 2,500-row joins per matched name).
MIN_ROWS_MATCHED = 2_832 (validator-calibrated 2026-05-19 post full-corpus
probe: 4,046 non-rejected matches — 3,818 platinum + 224 gold + 4 silver
+ 0 rejected. 2,832 floor = floor(4046 × 0.70); catches catastrophic failure
without false-tripping; NY USAspending cohort is ~2.6× smaller than CA's
per pure cohort-size effect, not normalizer regression).

Inputs:
    s3://dex-raw-landing-zone/polaris-warehouse/usaspending/contracts_lance
        (recipient_uei + recipient_name + recipient_state_code; filter NY;
        DISTINCT (recipient_uei, recipient_name) → ~5,202 rows; normalize
        recipient_name Python-side via normalize_entity_name)
    s3://dex-raw-landing-zone/polaris-warehouse/sos/ny_active_corporations_lance
        (entity_name_normalized pre-normalized at PRs #567+#568; dos_id PK)

Output:
    s3://dex-raw-landing-zone/polaris-warehouse/bridges/usaspending_sos_ny_owner_lance
    (BTREE on recipient_uei AND sos_dos_id; dual-BTREE per contract §C2/C3)

Match method REUSE (validator p2 / L21):
    register_bridge                          -> ops.bridges                (idempotent)
    start_bridge_run                         -> ops.bridge_generation_runs (status=running)
    write Lance + BTREE + tier counts
    complete_bridge_run                      -> status=completed + metrics
    fail_bridge_run (on error or dry-run)    -> status=failed + error
    (Method-definition and method-version-definition helpers are INTENTIONALLY
    NOT IMPORTED — see L21 / validator p2 — REUSER pattern mirrors PR #569.)

Tier rule (symmetric two-sided per CA precedent L339-358):
    platinum = BOTH fan_out == 1 (1:1 exact)
    gold     = ONE side fan_out == 1 (1:N | N:1)
    silver   = BOTH fan_out <= 50 (N:M below collision threshold)
    rejected = EITHER fan_out >  50 (collision)

Plain Python — NOT Modal:
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      uv run --project . python3 scripts/build_bridge_usaspending_sos_ny_owner_lance.py --apply

Dry-run (no Lance write, bridge run marked failed-dry-run):
    uv run --project . python3 scripts/build_bridge_usaspending_sos_ny_owner_lance.py
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# sys.path.insert per PR #481 pattern — allows _lib imports from worktree root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._lib.entity_name_normalize import (
    normalize_entity_name,
    __version__ as NORMALIZER_VERSION,
)
from scripts._lib.lance_commit_lock import lance_commit_lock
# CRITICAL validator p2 / L21: the method-definition and method-version-definition
# helpers are INTENTIONALLY OMITTED — this cycle is the EIGHTH REUSER of
# legal_name_state_exact_ny v1.0.0 (publisher: PR #513 sba_ny_contracts;
# prior reusers: sba_ny_usaspending, sba_ny_sam, sba_ny_nyc_contracts,
# sba_ny_mta, sba_ny_local_authority, sam_sos_ny_entities from PR #569).
# Calling register_match_method / register_match_method_version would UPSERT
# over PR #513's input_columns_left config {legal_name_normalized, borrstate}
# and corrupt the 7 sibling bridges' provenance trail.
from scripts._lib.match_method_registry import (
    complete_bridge_run,
    fail_bridge_run,
    register_bridge,
    start_bridge_run,
)

# ---------------------------------------------------------------------------
# Constants (load-bearing — match harness greps in verify constraints)
# ---------------------------------------------------------------------------

BRIDGE_NAME = "usaspending_sos_ny_owner"          # NAKED — no _lance suffix (ops.bridges convention)
DATASET_SLUG = "usaspending_sos_ny_owner_lance"   # _lance suffix for R2/Polaris/ops.data_sources
METHOD_NAME = "legal_name_state_exact_ny"         # REUSED — registered by PR #513
METHOD_SEMVER = "1.0.0"                           # REUSED — version row from PR #513
BRIDGE_VERSION = "1.0.0"

COLLISION_THRESHOLD = 50
# Validator-calibrated 2026-05-19 post full-corpus baseline probe.
# USAspending NY rows (recipient_state_code='NY'): 589,434.
# DISTINCT (recipient_uei, recipient_name): 5,203 → 5,202 after normalize+filter.
# Probe yield: 4,046 non-rejected (3,818 platinum + 224 gold + 4 silver + 0 rejected).
# Floor = floor(4046 × 0.70) = 2,832 (~70% of probe yield).
MIN_ROWS_MATCHED = 2_832

SOURCE_LEFT = "usaspending_contracts_lance"
SOURCE_RIGHT = "sos_ny_active_corporations_lance"

LEFT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/contracts_lance"
RIGHT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sos/ny_active_corporations_lance"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/usaspending_sos_ny_owner_lance"

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


def _ensure_db_url() -> None:
    """Normalize DEX_DB_URL_DIRECT from DATABASE_URL fallback if needed."""
    if "DEX_DB_URL_DIRECT" not in os.environ and "DATABASE_URL" in os.environ:
        os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


def _materialize_inputs(storage_options: dict) -> tuple:
    """Load USAspending NY-state recipients + NY DoS Active Corporations into Arrow tables.

    USAspending side (validator-confirmed column names):
      - Read contracts_lance with push-down filter recipient_state_code='NY'.
        Single-column filter (USAspending has ONE state column; simpler than SAM's 2-col OR).
        Lance scanner supports single-column equality push-down directly.
      - Project: recipient_uei, recipient_name, recipient_state_code.
      - DuckDB DISTINCT (recipient_uei, recipient_name) to collapse duplicates.
      - Python-side normalize: normalize_entity_name(recipient_name) list comprehension
        → recipient_name_normalized column attached to Arrow table BEFORE DuckDB registration.
        NOT a DuckDB UDF. Do NOT use any pre-normalized column from USAspending
        (there is none in contracts_lance).
      - Filter rows where normalization returned None (L33-blacklisted generic strings).

    NY DoS side (IDENTICAL to PR #569 SAM-NY bridge):
      - Read dos_id + current_entity_name + entity_name_normalized.
      - Filter to rows where entity_name_normalized is_valid().
      - No status column — all rows in ny_active_corporations_lance are Active by dataset
        definition. Constant 'A' is projected in DuckDB SELECT as sos_entity_status.
    """
    import lance
    import pyarrow as pa
    import pyarrow.compute as pc
    import duckdb

    logger.info("opening usaspending/contracts_lance (NY filter + DISTINCT) ...")
    usa_ds = lance.dataset(LEFT_LANCE_URI, storage_options=storage_options)

    # Single-column push-down filter: recipient_state_code='NY'.
    # USAspending contracts_lance has exactly one state column — no OR-predicate needed.
    # This is simpler than SAM's two-column OR-filter (PR #569 pattern).
    usa_raw = usa_ds.scanner(
        columns=["recipient_uei", "recipient_name", "recipient_state_code"],
        filter=pc.field("recipient_state_code") == "NY",
    ).to_table()
    logger.info("  contracts_lance NY rows scanned: %d", len(usa_raw))

    # Apply DISTINCT in DuckDB: (recipient_uei, recipient_name).
    con_distinct = duckdb.connect()
    con_distinct.register("usa_raw", usa_raw)
    left_distinct_arrow = con_distinct.execute(
        """
        SELECT DISTINCT recipient_uei, recipient_name
        FROM usa_raw
        WHERE recipient_name IS NOT NULL
          AND recipient_uei IS NOT NULL
        """
    ).arrow().read_all()
    rows_after_distinct = len(left_distinct_arrow)
    logger.info("  contracts_lance NY distinct (recipient_uei, recipient_name): %d rows", rows_after_distinct)

    # Python-side normalize: attach recipient_name_normalized column.
    # CRITICAL: NOT a DuckDB UDF and NOT any USAspending pre-normalized column (none exists).
    # Mirror PR #569 SAM-NY precedent L237-248.
    names_raw = left_distinct_arrow.column("recipient_name").to_pylist()
    normalized_names = [normalize_entity_name(n) for n in names_raw]
    left_arrow = left_distinct_arrow.append_column(
        "recipient_name_normalized",
        pa.array(normalized_names, type=pa.string()),
    )

    # Filter rows where normalization returned None (L33-blacklisted generic strings).
    valid_mask = pc.is_valid(left_arrow.column("recipient_name_normalized"))
    left_arrow = left_arrow.filter(valid_mask)
    rows_left = len(left_arrow)
    logger.info("  after normalize + filter (non-None normalized): %d rows", rows_left)

    logger.info("opening sos/ny_active_corporations_lance ...")
    sos_ds = lance.dataset(RIGHT_LANCE_URI, storage_options=storage_options)
    sos_filter = pc.field("entity_name_normalized").is_valid()
    sos_tbl = sos_ds.scanner(
        columns=[
            "dos_id",
            "current_entity_name",
            "entity_name_normalized",
        ],
        filter=sos_filter,
    ).to_table()
    rows_sos = len(sos_tbl)
    logger.info("  sos ny_active_corporations_lance (entity_name_normalized is_valid): %d rows", rows_sos)

    return left_arrow, sos_tbl, rows_left, rows_sos


def _build_match_table(
    left_tbl,
    sos_tbl,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
) -> tuple:
    """Run exact-equality JOIN + fan-out tiering in DuckDB (Arrow bridge).

    Join key: recipient_name_normalized (USAspending, Python-normalized) = entity_name_normalized (NY DoS).
    NY DoS pre-normalized entity_name_normalized agrees 100% with _lib on 100-row sample
    (validator pre-flight 1) — join on NY pre-norm directly (mirror of PR #569 pattern).
    Fan-out: separate recipient_fan_out (per normalized name across USAspending rows) and
    sos_fan_out (per normalized name across distinct NY dos_ids).
    Mirrors PR #569 SAM-NY precedent L267-408.

    NY DoS has NO status column — all rows are Active by dataset definition.
    Constant 'A' is projected as sos_entity_status to preserve CA/FL output column shape
    for downstream consumers (validator p4 — avoids missing-column error).
    PK projection: sos_dos_id (NY-specific per validator p3).
    """
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")

    con.register("l", left_tbl)
    con.register("sos", sos_tbl)

    rows_l_reg = con.execute("SELECT COUNT(*) FROM l").fetchone()[0]
    rows_sos_reg = con.execute("SELECT COUNT(*) FROM sos").fetchone()[0]
    logger.info("  registered: left=%d  sos=%d", rows_l_reg, rows_sos_reg)

    # 1. Inner JOIN on normalized name (name-only composite key; state-implicit from NY filter).
    #    NY DoS has no status column — project constant 'A' AS sos_entity_status per validator p4.
    #    Output column sos_dos_id — NY-specific PK naming per validator p3.
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_raw AS
        SELECT
            l.recipient_uei                      AS recipient_uei,
            l.recipient_name                     AS recipient_name_raw,
            l.recipient_name_normalized,
            s.dos_id                             AS sos_dos_id,
            s.current_entity_name                AS sos_entity_name,
            s.entity_name_normalized             AS sos_entity_name_normalized,
            'A'                                  AS sos_entity_status,
            '{METHOD_NAME}'                      AS match_method,
            l.recipient_name_normalized          AS match_value,
            '{BRIDGE_VERSION}'                   AS bridge_version,
            '{bridge_run_id}'                    AS bridge_run_id,
            TIMESTAMP '{generated_at_iso}'       AS generated_at
        FROM l
        JOIN sos s
          ON l.recipient_name_normalized = s.entity_name_normalized
        """
    )
    rows_matched_pre = con.execute("SELECT COUNT(*) FROM bridge_raw").fetchone()[0]
    logger.info("  bridge_raw (pre-tier): %d rows", rows_matched_pre)

    # 2. Fan-out counts (symmetric two-sided per PR #569 precedent).
    #    recipient_fan_out: # of USAspending rows sharing this normalized name.
    #    sos_fan_out: # of distinct NY dos_ids for this normalized name.
    con.execute(
        """
        CREATE TEMP TABLE recipient_fanout AS
        SELECT recipient_name_normalized, COUNT(*) AS recipient_fan_out
        FROM bridge_raw GROUP BY 1
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE sos_fanout AS
        SELECT recipient_name_normalized,
               COUNT(DISTINCT sos_dos_id) AS sos_fan_out
        FROM bridge_raw GROUP BY 1
        """
    )

    # 3. Tier rule (symmetric two-sided per PR #569 / CA precedent):
    #    platinum = BOTH fan_out == 1
    #    gold     = ONE side fan_out == 1
    #    silver   = BOTH <= COLLISION_THRESHOLD (else)
    #    rejected = EITHER > COLLISION_THRESHOLD
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            b.*,
            rf.recipient_fan_out,
            uf.sos_fan_out,
            CASE
                WHEN rf.recipient_fan_out > {COLLISION_THRESHOLD}
                  OR uf.sos_fan_out > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN rf.recipient_fan_out = 1 AND uf.sos_fan_out = 1
                    THEN 'platinum'
                WHEN rf.recipient_fan_out = 1 OR  uf.sos_fan_out = 1
                    THEN 'gold'
                ELSE 'silver'
            END AS confidence_tier
        FROM bridge_raw b
        JOIN recipient_fanout rf USING (recipient_name_normalized)
        JOIN sos_fanout uf USING (recipient_name_normalized)
        """
    )

    # 4. Filter rejected rows before write (PR #569 precedent L380).
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
    """Write bridge_match to Lance via Arrow-bridge + dual BTREE (recipient_uei, sos_dos_id).

    Mirrors PR #569 SAM-NY precedent L411-467.
    Dual-BTREE on recipient_uei AND sos_dos_id per Pattern B contract (§C2/C3).
    sos_dos_id is the NY-specific PK column (validator p3).
    """
    import lance

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")
    os.environ["TMPDIR"] = TMP_DIR

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

        # Dual BTREE per contract §C2/C3 (PR #569 precedent L443-456).
        # NY-specific PK: sos_dos_id (validator p3).
        try:
            ds.create_scalar_index("recipient_uei", index_type="BTREE", replace=True)
            logger.info("BTREE on recipient_uei: OK")
        except Exception as e:
            logger.error("BTREE on recipient_uei FAILED: %s", e)
            raise
        try:
            ds.create_scalar_index("sos_dos_id", index_type="BTREE", replace=True)
            logger.info("BTREE on sos_dos_id: OK")
        except Exception as e:
            logger.error("BTREE on sos_dos_id FAILED: %s", e)
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


def _register_polaris(app_dir: Path) -> None:
    """Register the bridge Lance dataset in Polaris Generic Table registry.

    Silent-fail on absent POLARIS_PUBLIC_URL per state-procurement-ingest-runbook §"Gotchas" item 3.
    Namespace: bridges, table: usaspending_sos_ny_owner_lance.
    """
    import subprocess

    polaris_url = os.environ.get("POLARIS_PUBLIC_URL", "")
    if not polaris_url:
        logger.info("POLARIS_PUBLIC_URL not set — skipping Polaris registration (silent-fail accepted)")
        return

    script = app_dir / "scripts" / "init_polaris_lance_generic.py"
    if not script.exists():
        logger.warning("init_polaris_lance_generic.py not found at %s — skipping Polaris registration", script)
        return

    try:
        result = subprocess.run(
            [sys.executable, str(script), "--namespace", "bridges", "--table", DATASET_SLUG],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            logger.info("Polaris registration OK: %s", result.stdout.strip())
        else:
            logger.warning("Polaris registration failed (non-fatal): %s", result.stderr.strip())
    except Exception as e:
        logger.warning("Polaris registration exception (non-fatal): %s", e)


def main() -> int:
    """Build the USAspending NY recipients × NY DoS Active Corporations Pattern B bridge."""
    parser = argparse.ArgumentParser(
        description="USAspending NY recipients × NY DoS Active Corporations Pattern B bridge generator."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Write to Lance + register ops. Without this flag runs in dry-run mode.",
    )
    args = parser.parse_args()

    _ensure_db_url()
    os.environ["TMPDIR"] = TMP_DIR
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)

    storage_options = _storage_options()
    started_at = datetime.now(timezone.utc)
    t0 = time.time()

    logger.info(
        "bridge: %s  method=%s v%s (REUSED from PR #513, NY method publisher)  normalizer=v%s  apply=%s",
        BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER, NORMALIZER_VERSION, args.apply,
    )
    logger.info("left:  %s", LEFT_LANCE_URI)
    logger.info("right: %s", RIGHT_LANCE_URI)
    logger.info("out:   %s", BRIDGE_LANCE_URI)

    # Idempotent UPSERT on bridge_name — safe to call even on re-runs.
    # REUSER: only register_bridge (no method-definition helpers per L21/p2).
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "USAspending NY-state federal contract recipients × NY DoS Active Corporations — "
            "legal-name exact match. "
            "Reuses legal_name_state_exact_ny v1.0.0 method registered by PR #513 "
            "(sba_ny_contracts publisher). REUSER #8 — NY USAspending analog of "
            "bridges.usaspending_sos_ca_owner_lance (PR #487) and "
            "bridges.sam_sos_ny_entities_lance (PR #569). "
            "Filter: recipient_state_code='NY' (single-column push-down). "
            "DISTINCT (recipient_uei, recipient_name) = ~5,202 NY USAspending recipients "
            "matched against 4.2M NY DoS active corporations. "
            "Normalizer: _lib.entity_name_normalize applied Python-side to raw recipient_name "
            "(no pre-normalized column in contracts_lance); NY DoS side reads pre-normalized "
            "entity_name_normalized (PRs #567+#568 parity confirmed — 100% agreement on 100-row sample). "
            "Output column sos_dos_id is NY-specific PK per validator p3. "
            "sos_entity_status = constant 'A' (no status column in NY dataset — all-Active by definition). "
            "Probe yield: 4,046 non-rejected (3,818 platinum + 224 gold + 4 silver + 0 rejected). "
            "MIN_ROWS_MATCHED=2,832 (validator-calibrated 2026-05-19 at ~70% of probe yield)."
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

    if not args.apply:
        # Dry-run: mark as failed so the run is not left orphaned.
        msg = "dry-run; no Lance write (pass --apply to execute)"
        logger.info("DRY-RUN: %s", msg)
        fail_bridge_run(run_uuid, msg)
        logger.info("bridge_run marked failed-dry-run (run_id=%s)", bridge_run_id)
        return 0

    try:
        left_tbl, sos_tbl, rows_left, rows_right = _materialize_inputs(storage_options)
        con, counts = _build_match_table(
            left_tbl, sos_tbl,
            bridge_run_id=bridge_run_id,
            generated_at_iso=started_at.isoformat(),
        )

        logger.info("-" * 60)
        logger.info("bridge tier distribution:")
        logger.info("  rows_matched:            %d", counts["rows_matched"])
        logger.info("    platinum (1:1):         %d", counts["rows_tier1"])
        logger.info("    gold     (1:N | N:1):   %d", counts["rows_tier2"])
        logger.info(
            "    silver   (N:M <= %d):   %d",
            COLLISION_THRESHOLD, counts["rows_tier3"],
        )
        logger.info(
            "  rows_collision_rejected:  %d",
            counts["rows_collision_rejected"],
        )

        if counts["rows_matched"] < MIN_ROWS_MATCHED:
            msg = (
                f"HARD FAIL: rows_matched={counts['rows_matched']:,} < "
                f"floor={MIN_ROWS_MATCHED:,}"
            )
            logger.error(msg)
            fail_bridge_run(run_uuid, msg)
            return 1

        lance_count = _write_bridge_lance(con, storage_options)

        # Polaris registration (silent-fail on absent POLARIS_PUBLIC_URL).
        app_dir = Path(__file__).resolve().parent.parent
        _register_polaris(app_dir)

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
            "OK - bridge_run_id=%s  lance_rows=%d  duration=%.1fs",
            bridge_run_id, lance_count, time.time() - t0,
        )
        return 0

    except Exception as exc:
        logger.exception("bridge generation failed")
        try:
            fail_bridge_run(run_uuid, repr(exc))
        except Exception:
            logger.exception("also failed to mark run as failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())

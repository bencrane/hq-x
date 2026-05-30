"""USAspending × FL Sunbiz entities Pattern B Lance bridge (Cycle 6 — final matrix gap-fill).

Pattern B exact-match bridge: USAspending federal contract recipients
(contracts_lance filtered recipient_state_code='FL' + DISTINCT (recipient_uei,
recipient_name) → normalized on-the-fly Python-side) × FL Sunbiz entities
(entity_name_normalized pre-normalized at PR #467; entity_num PK; status col
is 'status' NOT 'entity_status').

This is the FL variant of build_bridge_usaspending_sos_ca_owner_lance.py
(PR #487). USAspending-LEFT handling mirrors the CA precedent verbatim;
FL-right handling mirrors build_bridge_ppp_sos_fl_entities_lance.py (Cycle 3,
PR #576).

CRITICAL — LEFT source is contract-ACTION-grain:
    usaspending/contracts_lance is 15,515,568 rows (one row per contract action).
    A recipient with N contracts appears N times. The bridge MUST dedup to
    recipient-grain (recipient_uei, recipient_name) via SELECT DISTINCT BEFORE
    the name+state join or fan-out explodes. FL-filter + DISTINCT collapses
    644,480 FL contract-action rows → 7,222 distinct recipient pairs.
    [validator p1, constraint #8]

Left state filter: single column recipient_state_code='FL' (recipient-ADDRESS
state). NOT place-of-performance state, NOT an OR across two state columns.
[validator p2, constraint #18]

Normalizer (USAspending side — in-process, NOT a DuckDB UDF):
    contracts_lance has NO pre-normalized recipient-name column. Normalize
    recipient_name Python-side via normalize_entity_name(n) list-comp; attach
    recipient_name_normalized as Arrow column BEFORE DuckDB registration.
    [validator p3]

FL Sunbiz side — join pre-normalized column DIRECTLY:
    fl_entities_lance.entity_name_normalized is _lib v1.0.0-parity (validator
    measured 500/500 = 100.0%, 0 mismatches 2026-05-20). Join directly; do NOT
    call normalize_entity_name on the FL column.
    [validator p3]

Method: legal_name_state_exact_fl v1.0.0 (REUSED — the method + version rows
were registered by PR #467's build_bridge_sba_sos_fl_owner_lance.py;
this script ONLY calls register_bridge + start_bridge_run +
complete_bridge_run + fail_bridge_run per L21 — the method-definition
and method-version-definition helpers are INTENTIONALLY OMITTED; calling
them would UPSERT over the shared match_method_versions row and corrupt
the provenance trail of sam_sos_fl_entities, sba_sos_fl_owner, sba_fl_cilb,
fl_cilb_sunbiz, and ppp_sos_fl_entities bridges).
This is the SIXTH REUSER of this method.
[validator p4, constraint #6, constraint #14]

Bridge version: 1.0.0
COLLISION_THRESHOLD = 50
MIN_ROWS_MATCHED = 5_665 (validator-calibrated 2026-05-20, floor(8,093 × 0.70))

Closes the FINAL matrix gap: usaspending_sos_ca_owner and usaspending_sos_ny_owner
already exist; USAspending × FL SoS had never been built.
After this cycle: every (CA, FL, NY) SoS is bridged to SBA, USAspending, and SAM.

Inputs:
    s3://dex-raw-landing-zone/polaris-warehouse/usaspending/contracts_lance
        (filter recipient_state_code='FL', DISTINCT (recipient_uei, recipient_name)
        → 7,222 rows; normalize recipient_name Python-side via normalize_entity_name)
    s3://dex-raw-landing-zone/polaris-warehouse/sos/fl_entities_lance
        (entity_name_normalized pre-normalized at PR #467; entity_num PK;
        status col is 'status' ('A'/'I'); filter entity_name_normalized is_valid())

Output:
    s3://dex-raw-landing-zone/polaris-warehouse/bridges/usaspending_sos_fl_owner_lance
    (BTREE on recipient_uei AND sos_entity_num; dual-BTREE per validator constraint #2/#3)

Tier rule (symmetric two-sided, verbatim from CA precedent):
    platinum = BOTH fan_out == 1
    gold     = ONE side fan_out == 1
    silver   = BOTH fan_out <= 50
    rejected = EITHER fan_out > 50 (filtered out before write)

Run:
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      uv run --project . python3 scripts/build_bridge_usaspending_sos_fl_owner_lance.py --apply

Dry-run (no Lance write, bridge run marked failed-dry-run):
    uv run --project . python3 scripts/build_bridge_usaspending_sos_fl_owner_lance.py

Deferred Polaris registration (constraint #10 soft — Polaris/Railway down 2026-05-20):
    python apps/data-engine-x/scripts/init_polaris_lance_generic.py \\
        --namespace bridges --table usaspending_sos_fl_owner_lance \\
        --doc "USAspending x FL Sunbiz entities bridge (Pattern B). Resolves FL-recipient \\
               USAspending contractors against sos.fl_entities_lance via legal_name_state_exact_fl."
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
# CRITICAL validator p4 / L21: the method-definition and method-version-definition
# helpers are INTENTIONALLY OMITTED — this cycle REUSES legal_name_state_exact_fl
# v1.0.0 registered by PR #467 (sba_sos_fl_owner publisher). Calling those helpers
# would UPSERT over the shared match_method_versions row and corrupt the provenance
# trail of sam_sos_fl_entities, sba_sos_fl_owner, sba_fl_cilb, fl_cilb_sunbiz,
# and ppp_sos_fl_entities. This script is a REUSER — the method-definition
# imports are absent by design (L21). [constraint #14]
from scripts._lib.match_method_registry import (
    complete_bridge_run,
    fail_bridge_run,
    register_bridge,
    start_bridge_run,
)

# ---------------------------------------------------------------------------
# Constants (load-bearing — match harness greps in verify constraints)
# ---------------------------------------------------------------------------

BRIDGE_NAME = "usaspending_sos_fl_owner"          # NAKED — no _lance suffix (ops.bridges convention)
DATASET_SLUG = "usaspending_sos_fl_owner_lance"   # _lance suffix for R2/Polaris/ops.data_sources
METHOD_NAME = "legal_name_state_exact_fl"         # REUSED — registered by PR #467
METHOD_SEMVER = "1.0.0"                           # REUSED — version row from PR #467
BRIDGE_VERSION = "1.0.0"

COLLISION_THRESHOLD = 50
# Validator-calibrated 2026-05-20 post full-corpus baseline probe.
# contracts_lance FL-filtered + DISTINCT = 7,222 LEFT rows.
# Probe yield: 8,093 non-rejected (4,995 platinum + 195 gold + 2,903 silver + 0 rejected).
# Max fan-out: recipient 35, sos 13 (both under COLLISION_THRESHOLD → 0 rejected).
# Floor = floor(8,093 × 0.70) = 5,665 (~70%) — catches catastrophic failure
# (wrong state column, missing recipient-grain dedup, normalizer drift) without
# false-tripping on a clean recipe run. [validator p5, constraint #1]
MIN_ROWS_MATCHED = 5_665

SOURCE_LEFT = "usaspending_contracts_lance"
SOURCE_RIGHT = "sos_fl_entities_lance"

LEFT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/contracts_lance"
RIGHT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sos/fl_entities_lance"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/usaspending_sos_fl_owner_lance"

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
    """Load USAspending FL recipients + FL Sunbiz entities into Arrow tables.

    USAspending side (contract-ACTION-grain dedup → recipient-grain):
      - Read contracts_lance (15.5M rows — one row per contract action).
      - Push-down filter: recipient_state_code = 'FL' (single column, NOT
        primary_place_of_performance_state_code; NOT an OR-filter).
        [validator p2, constraint #18]
      - DuckDB DISTINCT (recipient_uei, recipient_name) → 7,222 canonical FL
        recipient pairs from 644,480 FL contract-action rows. [validator p1, constraint #8]
      - Python-side normalize: normalize_entity_name(name) list comprehension
        → recipient_name_normalized column attached to Arrow table BEFORE
        DuckDB registration. NOT a DuckDB UDF. [validator p3]
      - Filter rows where normalization returned None (L33-blacklisted generic strings).

    FL Sunbiz side (mirror ppp_sos_fl_entities_lance Cycle 3 / PR #576):
      - Read entity_num + entity_name_normalized + status (FL column is 'status',
        NOT 'entity_status' — FL Sunbiz has no entity_status column; projecting
        it directly is a build error; renamed AS sos_entity_status in DuckDB SELECT).
      - Filter to rows where entity_name_normalized is_valid().
      - Join FL pre-normalized entity_name_normalized DIRECTLY — do NOT call
        normalize_entity_name on the FL column (100% _lib parity confirmed).
        [validator p3]
    """
    import lance
    import pyarrow as pa
    import pyarrow.compute as pc
    import duckdb

    logger.info("opening usaspending/contracts_lance (FL filter + DISTINCT) ...")
    contracts_ds = lance.dataset(LEFT_LANCE_URI, storage_options=storage_options)

    # Push-down filter: recipient_state_code = 'FL' (single column — NOT an OR-filter;
    # NOT primary_place_of_performance_state_code). [validator p2, constraint #18]
    fl_filter = pc.field("recipient_state_code") == "FL"
    contracts_raw = contracts_ds.scanner(
        columns=["recipient_uei", "recipient_name", "recipient_state_code"],
        filter=fl_filter,
    ).to_table()
    logger.info("  contracts_lance FL rows (pre-DISTINCT): %d", len(contracts_raw))

    # DISTINCT (recipient_uei, recipient_name) in DuckDB to collapse 644K
    # contract-action rows → ~7.2K canonical FL UEI+name pairs. [validator p1, constraint #8]
    con_distinct = duckdb.connect()
    con_distinct.register("l_raw", contracts_raw)
    left_distinct_arrow = con_distinct.execute(
        """
        SELECT DISTINCT recipient_uei, recipient_name
        FROM l_raw
        WHERE recipient_name IS NOT NULL AND recipient_uei IS NOT NULL
        """
    ).arrow().read_all()
    rows_after_distinct = len(left_distinct_arrow)
    logger.info("  contracts_lance FL distinct (recipient_uei, recipient_name): %d rows", rows_after_distinct)

    # Python-side normalize: attach recipient_name_normalized column.
    # NOT a DuckDB UDF — corpus is ~7.2K rows, cheap to normalize in-process.
    # contracts_lance has NO pre-normalized recipient-name column. [validator p3]
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

    logger.info("opening sos/fl_entities_lance ...")
    fl_ds = lance.dataset(RIGHT_LANCE_URI, storage_options=storage_options)
    fl_filter = pc.field("entity_name_normalized").is_valid()
    # FL Sunbiz columns: entity_num (PK), entity_name_normalized (join key),
    # status (NOT entity_status — FL Sunbiz has no entity_status column;
    # renamed AS sos_entity_status in DuckDB SELECT below). [validator parity check]
    fl_tbl = fl_ds.scanner(
        columns=[
            "entity_num",
            "entity_name_normalized",
            "status",
        ],
        filter=fl_filter,
    ).to_table()
    rows_fl = len(fl_tbl)
    logger.info("  sos fl_entities_lance (entity_name_normalized is_valid): %d rows", rows_fl)

    return left_arrow, fl_tbl, rows_left, rows_fl


def _build_match_table(
    left_tbl,
    fl_tbl,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
) -> tuple:
    """Run exact-equality JOIN + fan-out tiering in DuckDB (Arrow bridge).

    Join key: recipient_name_normalized (USAspending, normalized Python-side)
    = entity_name_normalized (FL Sunbiz, pre-normalized).
    DO NOT re-normalize the FL column — join pre-normalized directly.
    [validator p3]

    Fan-out: separate recipient_fan_out (# of USAspending rows sharing this
    normalized name) and sos_fan_out (# of distinct FL entity_nums per name).
    Mirrors CA precedent's symmetric two-sided tier rule verbatim.
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
    con.register("fl", fl_tbl)

    rows_l_reg = con.execute("SELECT COUNT(*) FROM l").fetchone()[0]
    rows_fl_reg = con.execute("SELECT COUNT(*) FROM fl").fetchone()[0]
    logger.info("  registered: left=%d  fl=%d", rows_l_reg, rows_fl_reg)

    # 1. Inner JOIN on normalized name.
    #    USAspending side: recipient_name_normalized (normalized Python-side).
    #    FL side: entity_name_normalized (pre-normalized, join directly).
    #    FL 'status' renamed to 'sos_entity_status' (FL has no entity_status column;
    #    projecting it directly is a build error — mirror PPP-FL precedent L214-228).
    #    LEFT output cols: recipient_uei, recipient_name_raw, recipient_name_normalized
    #    (NOT usaspending_*-prefixed — CA precedent confirmed bare recipient_* naming).
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_raw AS
        SELECT
            l.recipient_uei,
            l.recipient_name                     AS recipient_name_raw,
            l.recipient_name_normalized,
            f.entity_num                         AS sos_entity_num,
            f.entity_name_normalized             AS sos_entity_name_normalized,
            f.status                             AS sos_entity_status,
            '{METHOD_NAME}'                      AS match_method,
            l.recipient_name_normalized          AS match_value,
            '{BRIDGE_VERSION}'                   AS bridge_version,
            '{bridge_run_id}'                    AS bridge_run_id,
            TIMESTAMP '{generated_at_iso}'       AS generated_at
        FROM l
        JOIN fl f
          ON l.recipient_name_normalized = f.entity_name_normalized
        """
    )
    rows_matched_pre = con.execute("SELECT COUNT(*) FROM bridge_raw").fetchone()[0]
    logger.info("  bridge_raw (pre-tier): %d rows", rows_matched_pre)

    # 2. Fan-out counts (symmetric two-sided, mirrors CA precedent).
    #    recipient_fan_out: # of USAspending rows sharing this normalized name.
    #    sos_fan_out: # of distinct FL entity_nums for this normalized name.
    con.execute(
        """
        CREATE TEMP TABLE usaspending_fanout AS
        SELECT recipient_name_normalized, COUNT(*) AS recipient_fan_out
        FROM bridge_raw GROUP BY 1
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE sos_fanout AS
        SELECT recipient_name_normalized,
               COUNT(DISTINCT sos_entity_num) AS sos_fan_out
        FROM bridge_raw GROUP BY 1
        """
    )

    # 3. Tier rule (symmetric two-sided, verbatim from CA precedent):
    #    platinum = BOTH fan_out == 1
    #    gold     = ONE side fan_out == 1
    #    silver   = BOTH <= COLLISION_THRESHOLD (else)
    #    rejected = EITHER > COLLISION_THRESHOLD
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            b.*,
            uf.recipient_fan_out,
            sf.sos_fan_out,
            CASE
                WHEN uf.recipient_fan_out > {COLLISION_THRESHOLD}
                  OR sf.sos_fan_out        > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN uf.recipient_fan_out = 1 AND sf.sos_fan_out = 1
                    THEN 'platinum'
                WHEN uf.recipient_fan_out = 1 OR  sf.sos_fan_out = 1
                    THEN 'gold'
                ELSE 'silver'
            END AS confidence_tier
        FROM bridge_raw b
        JOIN usaspending_fanout uf USING (recipient_name_normalized)
        JOIN sos_fanout         sf USING (recipient_name_normalized)
        """
    )

    # 4. Filter rejected rows before write. [constraint #7]
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
    """Write bridge_match to Lance via Arrow-bridge + dual BTREE.

    BTREE on recipient_uei (constraint #2 — CA+NY precedents both index recipient_uei,
    NOT the normalized name) and sos_entity_num (constraint #3 — FL PK).
    All within lance_commit_lock("usaspending_sos_fl_owner_lance"). [constraint #9]
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

        # Dual BTREE per constraint #2/#3 and CA+NY precedent pattern.
        # BTREE on recipient_uei (USAspending-side join key — NOT the normalized name).
        # BTREE on sos_entity_num (FL PK).
        try:
            ds.create_scalar_index("recipient_uei", index_type="BTREE", replace=True)
            logger.info("BTREE on recipient_uei: OK")
        except Exception as e:
            logger.error("BTREE on recipient_uei FAILED: %s", e)
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


def main() -> int:
    """Build the USAspending FL recipients × FL Sunbiz entities Pattern B bridge."""
    parser = argparse.ArgumentParser(
        description=(
            "USAspending FL recipients × FL Sunbiz entities Pattern B bridge generator. "
            "Closes the final USAspending × FL SoS matrix gap (Cycle 6 of 6)."
        )
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
        "bridge: %s  method=%s v%s (REUSED from PR #467, 6th REUSER)  normalizer=v%s  apply=%s",
        BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER, NORMALIZER_VERSION, args.apply,
    )
    logger.info("left:  %s", LEFT_LANCE_URI)
    logger.info("right: %s", RIGHT_LANCE_URI)
    logger.info("out:   %s", BRIDGE_LANCE_URI)

    # Idempotent UPSERT on bridge_name — safe to call even on re-runs.
    # REUSER: only register_bridge (no method-definition helpers per L21/validator p4).
    # source_left='usaspending_contracts_lance', source_right='sos_fl_entities_lance'
    # per validator constraint #4. [constraint #4]
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "USAspending FL-state recipients × FL Sunbiz entities — legal-name exact match. "
            "Reuses legal_name_state_exact_fl v1.0.0 method registered by PR #467 "
            "(sba_sos_fl_owner publisher). Sixth REUSER (prior: fl_cilb_sunbiz, sba_fl_cilb, "
            "sba_sos_fl_owner PR #467; sam_sos_fl_entities PR #563; ppp_sos_fl_entities PR #576). "
            "FL variant of usaspending_sos_ca_owner_lance (PR #487). "
            "LEFT: usaspending.contracts_lance filtered recipient_state_code='FL' + "
            "DISTINCT (recipient_uei, recipient_name) → 7,222 rows from 644,480 FL "
            "contract-action rows; normalize recipient_name Python-side. "
            "RIGHT: sos.fl_entities_lance — join entity_name_normalized directly (pre-normalized, "
            "100% _lib v1.0.0 parity). FL status col renamed status→sos_entity_status. "
            "BTREE on recipient_uei + sos_entity_num. "
            "Closes the final USAspending × FL SoS matrix gap (Cycle 6 of 6)."
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
        left_tbl, fl_tbl, rows_left, rows_right = _materialize_inputs(storage_options)
        con, counts = _build_match_table(
            left_tbl, fl_tbl,
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

        # HARD FAIL before Lance write if below floor. [constraint #1, validator p5]
        if counts["rows_matched"] < MIN_ROWS_MATCHED:
            msg = (
                f"HARD FAIL: rows_matched={counts['rows_matched']:,} < "
                f"floor={MIN_ROWS_MATCHED:,}"
            )
            logger.error(msg)
            fail_bridge_run(run_uuid, msg)
            return 1

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
            "OK - bridge_run_id=%s  lance_rows=%d  duration=%.1fs",
            bridge_run_id, lance_count, time.time() - t0,
        )
        print(f'{{"status":"completed","rows_matched":{counts["rows_matched"]}}}')
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

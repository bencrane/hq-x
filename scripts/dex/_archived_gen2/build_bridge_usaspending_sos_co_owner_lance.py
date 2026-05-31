"""USAspending CO recipients × CO SoS entities owner-identity bridge (Pattern B).

Pattern B exact-match bridge: USAspending federal contract recipients
(contracts_lance filtered recipient_state_code='CO' + DISTINCT (recipient_uei,
recipient_name) → normalized on-the-fly Python-side) × CO SoS entities
(entity_name_normalized).

Validator probe (2026-05-27):
    contracts_lance CO pre-DISTINCT: 145,842
    DISTINCT (recipient_uei, recipient_name): 3,303 → 3,303 after normalize+filter
    CO SoS rows (entity_name_normalized valid): 3,049,009
    Raw matched rows: 3,520
    Tier distribution: 2,291 platinum + 79 gold + 1,150 silver + 0 rejected
    MIN_ROWS_MATCHED = 2,400 (0.68 × probed; tracks NY-sibling 0.70 convention).

Method: legal_name_state_exact_co v1.0.0 (REUSED — the method + version rows
    were published by build_bridge_sec_bdc_sos_co_entities_lance.py).
    This script ONLY calls register_bridge + start_bridge_run +
    complete_bridge_run + fail_bridge_run per L21 — the method-definition
    and method-version-definition helpers are INTENTIONALLY OMITTED;
    calling them would UPSERT over the publisher's input_columns_left config
    and corrupt other reusers' provenance trail).

Bridge version: 1.0.0
COLLISION_THRESHOLD = 50 (rows where either side's fan-out exceeds this are
    rejected — N:M up to 50×50 = 2,500-row joins per matched name).

Inputs:
    s3://dex-raw-landing-zone/polaris-warehouse/usaspending/contracts_lance
        (recipient_uei + recipient_name + recipient_state_code; filter CO;
        DISTINCT (recipient_uei, recipient_name) → 3,303 rows; normalize
        recipient_name Python-side via normalize_entity_name)
    s3://dex-raw-landing-zone/polaris-warehouse/sos/co_entities_lance
        (entity_name_normalized pre-normalized at the CO emit;
        entityid PK; no jurisdiction filter — foreign LLCs registered in CO
        appear here and are included intentionally for downstream chain)

Output:
    s3://dex-raw-landing-zone/polaris-warehouse/bridges/usaspending_sos_co_owner_lance
    (BTREE on recipient_uei AND sos_entity_id; dual-BTREE per validator p6
    precedent PR #466/#482/CA/NY)

Normalizer:
    USAspending side has NO pre-normalized name column. With only 3.3K LEFT
    rows post-CO-filter + DISTINCT, call normalize_entity_name(name) in a
    Python list comprehension and attach recipient_name_normalized as an extra
    column on the Arrow table BEFORE registering with DuckDB. NOT a DuckDB UDF
    — corpus is small enough to normalize in-process.

Tier rule (symmetric two-sided per CA/FL/NY sibling):
    platinum = BOTH fan_out == 1 (1:1 exact)
    gold     = ONE side fan_out == 1 (1:N | N:1)
    silver   = BOTH fan_out <= 50 (N:M below collision threshold)
    rejected = EITHER fan_out >  50 (collision)

Usage:
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      uv run python scripts/build_bridge_usaspending_sos_co_owner_lance.py --apply

    Dry-run (no Lance write, bridge run marked failed-dry-run):
      uv run python scripts/build_bridge_usaspending_sos_co_owner_lance.py
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._lib.entity_name_normalize import (
    normalize_entity_name,
    __version__ as NORMALIZER_VERSION,
)
from scripts._lib.lance_commit_lock import lance_commit_lock
from scripts._lib.match_method_registry import (
    complete_bridge_run,
    fail_bridge_run,
    register_bridge,
    start_bridge_run,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BRIDGE_NAME = "usaspending_sos_co_owner"          # NAKED — no _lance suffix (ops.bridges convention)
DATASET_SLUG = "usaspending_sos_co_owner_lance"   # _lance suffix for R2/Polaris/ops.data_sources
METHOD_NAME = "legal_name_state_exact_co"         # REUSED — published by sec_bdc_sos_co_entities
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "1.0.0"

COLLISION_THRESHOLD = 50
# Validator probe (2026-05-27): 3,520 matched rows (2,291 platinum + 79 gold +
# 1,150 silver + 0 rejected). Floor at 2,400 ≈ 0.68 × probed; catches catastrophic
# normalizer/CO-filter regression without false-tripping on PDL/SoS upstream drift.
MIN_ROWS_MATCHED = 2_400

SOURCE_LEFT = "usaspending_contracts_lance"
SOURCE_RIGHT = "sos_co_entities_lance"

LEFT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/contracts_lance"
RIGHT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sos/co_entities_lance"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/usaspending_sos_co_owner_lance"

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
    if "DEX_DB_URL_DIRECT" not in os.environ and "DATABASE_URL" in os.environ:
        os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


def _materialize_inputs(storage_options: dict) -> tuple:
    """Load USAspending CO recipients + CO SoS entities into Arrow tables."""
    import lance
    import pyarrow as pa
    import pyarrow.compute as pc
    import duckdb

    logger.info("opening usaspending/contracts_lance (CO filter + DISTINCT) ...")
    contracts_ds = lance.dataset(LEFT_LANCE_URI, storage_options=storage_options)
    co_filter = pc.field("recipient_state_code") == "CO"
    contracts_raw = contracts_ds.scanner(
        columns=["recipient_uei", "recipient_name", "recipient_state_code"],
        filter=co_filter,
    ).to_table()
    logger.info("  contracts_lance CO rows (pre-DISTINCT): %d", len(contracts_raw))

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
    logger.info("  contracts_lance CO distinct (recipient_uei, recipient_name): %d rows", rows_after_distinct)

    names_raw = left_distinct_arrow.column("recipient_name").to_pylist()
    normalized_names = [normalize_entity_name(n) for n in names_raw]
    left_arrow = left_distinct_arrow.append_column(
        "recipient_name_normalized",
        pa.array(normalized_names, type=pa.string()),
    )

    valid_mask = pc.is_valid(left_arrow.column("recipient_name_normalized"))
    left_arrow = left_arrow.filter(valid_mask)
    rows_left = len(left_arrow)
    logger.info("  after normalize + filter (non-None normalized): %d rows", rows_left)

    logger.info("opening sos/co_entities_lance ...")
    sos_ds = lance.dataset(RIGHT_LANCE_URI, storage_options=storage_options)
    sos_filter = pc.field("entity_name_normalized").is_valid()
    sos_tbl = sos_ds.scanner(
        columns=[
            "entityid",
            "entityname",
            "entity_name_normalized",
            "entitystatus",
            "entitytype",
            "jurisdictonofformation",
            "entityformdate",
        ],
        filter=sos_filter,
    ).to_table()
    rows_sos = len(sos_tbl)
    logger.info("  sos co_entities_lance (entity_name_normalized is_valid): %d rows", rows_sos)

    return left_arrow, sos_tbl, rows_left, rows_sos


def _build_match_table(
    left_tbl,
    sos_tbl,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
) -> tuple:
    """Exact-equality JOIN + fan-out tiering in DuckDB (Arrow bridge).

    Join key: recipient_name_normalized (USAspending) = entity_name_normalized (CO SoS).
    State implicit from CO-filter on LEFT + CO-SoS dataset name.
    Fan-out: separate recipient_fan_out (per normalized name across USAspending rows)
    and sos_fan_out (per normalized name across distinct CO SoS entityid).

    CO SoS column naming differs from CA/FL: entityid (no underscore),
    entityname, entitystatus, entitytype, jurisdictonofformation, entityformdate.
    Output columns aliased to sos_entity_id / sos_entity_name / etc. for parity
    with CA/FL/NY sibling output shape.
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

    con.execute(
        f"""
        CREATE TEMP TABLE bridge_raw AS
        SELECT
            l.recipient_uei,
            l.recipient_name                     AS recipient_name_raw,
            l.recipient_name_normalized,
            s.entityid                           AS sos_entity_id,
            s.entityname                         AS sos_entity_name,
            s.entity_name_normalized,
            s.entitystatus                       AS sos_entity_status,
            s.entitytype                         AS sos_entity_type,
            s.jurisdictonofformation             AS sos_jurisdiction,
            s.entityformdate                     AS sos_form_date,
            '{METHOD_NAME}'                      AS match_method,
            l.recipient_name_normalized          AS match_value_normalized,
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
               COUNT(DISTINCT sos_entity_id) AS sos_fan_out
        FROM bridge_raw GROUP BY 1
        """
    )

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
    """Write bridge_match to Lance via Arrow-bridge + dual BTREE (recipient_uei, sos_entity_id)."""
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

        try:
            ds.create_scalar_index("recipient_uei", index_type="BTREE", replace=True)
            logger.info("BTREE on recipient_uei: OK")
        except Exception as e:
            logger.error("BTREE on recipient_uei FAILED: %s", e)
            raise
        try:
            ds.create_scalar_index("sos_entity_id", index_type="BTREE", replace=True)
            logger.info("BTREE on sos_entity_id: OK")
        except Exception as e:
            logger.error("BTREE on sos_entity_id FAILED: %s", e)
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
    parser = argparse.ArgumentParser(
        description="USAspending CO recipients × CO SoS entities Pattern B bridge generator."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Write to Lance + register. Without this flag runs in dry-run mode.",
    )
    args = parser.parse_args()

    _ensure_db_url()
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)

    storage_options = _storage_options()
    started_at = datetime.now(timezone.utc)
    t0 = time.time()

    logger.info(
        "bridge: %s  method=%s v%s (REUSED)  normalizer=v%s  apply=%s",
        BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER, NORMALIZER_VERSION, args.apply,
    )
    logger.info("left:  %s", LEFT_LANCE_URI)
    logger.info("right: %s", RIGHT_LANCE_URI)
    logger.info("out:   %s", BRIDGE_LANCE_URI)

    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "USAspending CO recipients x CO SoS entities — legal-name exact match. "
            "Reuses legal_name_state_exact_co v1.0.0 method (publisher: sec_bdc_sos_co_entities). "
            "Spine: usaspending.contracts_lance filtered recipient_state_code='CO' + "
            "DISTINCT (recipient_uei, recipient_name); downstream chain back to "
            "the recipient UEI aggregation dataset."
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

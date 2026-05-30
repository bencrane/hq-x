"""SAM × CO SoS entities Pattern B Lance bridge.

Sibling of sam_sos_ca_entities_lance / sam_sos_fl_entities_lance / sam_sos_ny_entities_lance.
Anchors on SAM-registered Colorado-state entities × CO SoS entities (entity_name_normalized).

Method: legal_name_state_exact_co v1.0.0 (REUSED — registered by prior CO bridge).
  REUSER pattern: only register_bridge + start/complete/fail run helpers;
  method-definition helpers INTENTIONALLY OMITTED.

SAM-side filter: physical_address_state_normalized='CO' OR mailing_address_state_or_province='CO'.

Inputs:
  SAM:    sam_gov/entities_lance (~884K rows; CO state filter)
  CO SoS: sos/co_entities_lance  (~3.05M rows; entitystatus='Good Standing' = ~1.04M)

Output: polaris-warehouse/bridges/sam_sos_co_entities_lance
Audit:  ops.bridge_generation_runs (bridge_name='sam_sos_co_entities')
Floor:  ≥ 15,000 matched rows.
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

BRIDGE_NAME = "sam_sos_co_entities"
DATASET_SLUG = "sam_sos_co_entities_lance"
METHOD_NAME = "legal_name_state_exact_co"
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "1.0.0"

COLLISION_THRESHOLD = 50
MIN_ROWS_MATCHED = 15_000

SOURCE_LEFT = "sam_gov_entities_lance"
SOURCE_RIGHT = "sos_co_entities_lance"

LEFT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/entities_lance"
RIGHT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sos/co_entities_lance"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sam_sos_co_entities_lance"

TMP_DIR = "/tmp/lance"
DUCKDB_TMP_DIR = "/Users/benjamincrane/dex-build-tmp"

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
    import lance
    import pyarrow as pa
    import pyarrow.compute as pc
    import duckdb

    logger.info("opening sam_gov/entities_lance (CO filter + DISTINCT) ...")
    sam_ds = lance.dataset(LEFT_LANCE_URI, storage_options=storage_options)
    sam_raw = sam_ds.scanner(
        columns=[
            "unique_entity_id",
            "legal_business_name",
            "physical_address_state_normalized",
            "mailing_address_state_or_province",
        ],
    ).to_table()
    logger.info("  entities_lance total rows scanned: %d", len(sam_raw))

    con_distinct = duckdb.connect()
    con_distinct.register("sam_raw", sam_raw)
    left_distinct_arrow = con_distinct.execute(
        """
        SELECT DISTINCT unique_entity_id, legal_business_name
        FROM sam_raw
        WHERE (physical_address_state_normalized = 'CO' OR mailing_address_state_or_province = 'CO')
          AND legal_business_name IS NOT NULL
          AND unique_entity_id IS NOT NULL
        """
    ).fetch_arrow_table()
    rows_after_distinct = len(left_distinct_arrow)
    logger.info(
        "  entities_lance CO distinct (unique_entity_id, legal_business_name): %d rows",
        rows_after_distinct,
    )

    names_raw = left_distinct_arrow.column("legal_business_name").to_pylist()
    normalized_names = [normalize_entity_name(n) for n in names_raw]
    left_arrow = left_distinct_arrow.append_column(
        "sam_legal_name_normalized",
        pa.array(normalized_names, type=pa.string()),
    )
    valid_mask = pc.is_valid(left_arrow.column("sam_legal_name_normalized"))
    left_arrow = left_arrow.filter(valid_mask)
    rows_left = len(left_arrow)
    logger.info("  after normalize + filter (non-None normalized): %d rows", rows_left)

    logger.info("opening sos/co_entities_lance ...")
    sos_ds = lance.dataset(RIGHT_LANCE_URI, storage_options=storage_options)
    # Filter to Good Standing (CO's analog of Active)
    sos_filter = (
        pc.field("entity_name_normalized").is_valid()
        & pc.equal(pc.field("entitystatus"), "Good Standing")
    )
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
    logger.info(
        "  sos co_entities_lance (Good Standing, entity_name_normalized valid): %d rows",
        rows_sos,
    )

    return left_arrow, sos_tbl, rows_left, rows_sos


def _build_match_table(left_tbl, sos_tbl, *, bridge_run_id: str, generated_at_iso: str) -> tuple:
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    Path(DUCKDB_TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET memory_limit='24GB'")
    con.execute(f"SET temp_directory='{DUCKDB_TMP_DIR}'")
    con.execute("SET max_temp_directory_size='240GB'")
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
            l.unique_entity_id                   AS sam_uei,
            l.legal_business_name                AS sam_legal_name_raw,
            l.sam_legal_name_normalized,
            s.entityid                           AS sos_entity_id,
            s.entityname                         AS sos_entity_name,
            s.entity_name_normalized,
            s.entitystatus                       AS sos_entity_status,
            s.entitytype                         AS sos_entity_type,
            s.jurisdictonofformation             AS sos_jurisdiction,
            s.entityformdate                     AS sos_form_date,
            '{METHOD_NAME}'                      AS match_method,
            l.sam_legal_name_normalized          AS match_value_normalized,
            '{BRIDGE_VERSION}'                   AS bridge_version,
            '{bridge_run_id}'                    AS bridge_run_id,
            TIMESTAMP '{generated_at_iso}'       AS generated_at
        FROM l
        JOIN sos s
          ON l.sam_legal_name_normalized = s.entity_name_normalized
        """
    )
    rows_matched_pre = con.execute("SELECT COUNT(*) FROM bridge_raw").fetchone()[0]
    logger.info("  bridge_raw (pre-tier): %d rows", rows_matched_pre)

    con.execute(
        """
        CREATE TEMP TABLE sam_fanout AS
        SELECT sam_legal_name_normalized, COUNT(*) AS sam_fan_out
        FROM bridge_raw GROUP BY 1
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE sos_fanout AS
        SELECT sam_legal_name_normalized,
               COUNT(DISTINCT sos_entity_id) AS sos_fan_out
        FROM bridge_raw GROUP BY 1
        """
    )

    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            b.*,
            sf.sam_fan_out,
            uf.sos_fan_out,
            CASE
                WHEN sf.sam_fan_out > {COLLISION_THRESHOLD}
                  OR uf.sos_fan_out > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN sf.sam_fan_out = 1 AND uf.sos_fan_out = 1
                    THEN 'platinum'
                WHEN sf.sam_fan_out = 1 OR  uf.sos_fan_out = 1
                    THEN 'gold'
                ELSE 'silver'
            END AS confidence_tier
        FROM bridge_raw b
        JOIN sam_fanout sf USING (sam_legal_name_normalized)
        JOIN sos_fanout uf USING (sam_legal_name_normalized)
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
    import lance

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")
    os.environ["TMPDIR"] = TMP_DIR

    logger.info("materializing bridge_match to Arrow in memory ...")
    arrow_tbl = con.execute("SELECT * FROM bridge_match").fetch_arrow_table()
    logger.info("  materialized %d rows", arrow_tbl.num_rows)

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        logger.info("writing bridge to Lance at %s ...", BRIDGE_LANCE_URI)
        ds = lance.write_dataset(
            arrow_tbl, BRIDGE_LANCE_URI, mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t0
        lance_count = ds.count_rows()
        logger.info(
            "wrote %d rows in %.1fs (version=%s)",
            lance_count, write_dur, ds.version,
        )

        for col in ("sam_uei", "sos_entity_id"):
            try:
                ds.create_scalar_index(col, index_type="BTREE", replace=True)
                logger.info("BTREE on %s: OK", col)
            except Exception as e:
                logger.error("BTREE on %s FAILED: %s", col, e)
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
        description="SAM.gov CO entities × CO SoS entities Pattern B bridge generator."
    )
    parser.add_argument("--apply", action="store_true", default=False)
    args = parser.parse_args()

    _ensure_db_url()
    os.environ["TMPDIR"] = TMP_DIR
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
            "SAM.gov CO-state entities × CO SoS entities — legal-name exact match. "
            "Reuses legal_name_state_exact_co v1.0.0 method. "
            "Filter: physical_address_state_normalized='CO' OR mailing_address_state_or_province='CO'; "
            "CO SoS filtered to entitystatus='Good Standing'."
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
        logger.info("  rows_collision_rejected:  %d", counts["rows_collision_rejected"])

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

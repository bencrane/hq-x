"""SAM × CA SoS entities Pattern B Lance bridge.

Pattern B exact-match bridge: SAM.gov registered CA-state entities
(physical_address_state_normalized='CA' OR mailing_address_state_or_province='CA',
with Python-side normalize_entity_name applied to raw legal_business_name)
× CA SoS entities (entity_name_normalized pre-normalized at PR #464).

Method: legal_name_state_exact_ca v1.0.0 (REUSED — the method + version rows
were registered by PR #464's build_bridge_sba_sos_ca_owner_lance.py;
this script ONLY calls register_bridge + start_bridge_run +
complete_bridge_run + fail_bridge_run per L21 — the method-definition
and method-version-definition helpers are INTENTIONALLY OMITTED; calling
them would UPSERT over the SBA-shape input_columns_left config
{legal_name_normalized, borrstate} and corrupt SBA × SoS, UCC × SoS,
CSLB × SoS, and USAspending × SoS bridges' provenance trail).
This is the FOURTH REUSER of this method (prior reusers: PR #466 UCC CA × CA SoS,
PR #482 CSLB × CA SoS, PR #481 USAspending × CA SoS; publisher: PR #464 SBA).

SAM-side PK: unique_entity_id (raw UEI string). Output column: sam_uei.
CA SoS PK: entity_num. Output column: sos_entity_num.

SAM-side filter: physical_address_state_normalized='CA' OR mailing_address_state_or_province='CA'
(validator-corrected column names — the directive's original *_state_or_province_code
names DO NOT EXIST in sam_gov.entities_lance).

Normalizer (CRITICAL — use _lib Python-side on raw legal_business_name ONLY):
    The SAM pre-supplied normalized column diverges from _lib.entity_name_normalize at ~8.4%
    (validator-measured 2026-05-19 via 500-row CA sample; SAM's column keeps 'co' suffix + hyphens
    that _lib strips). This bridge MUST apply normalize_entity_name Python-side on the
    raw legal_business_name, mirroring USAspending precedent at L217-228.

Bridge version: 1.0.0
COLLISION_THRESHOLD = 50 (rows where either side's fan-out exceeds this are
rejected — N:M up to 50×50 = 2,500-row joins per matched name).
MIN_ROWS_MATCHED = 59_000 (validator-calibrated 2026-05-19 post full-corpus
probe: 84,481 non-rejected matches — 53,726 platinum + 1,742 gold + 29,013 silver
+ 875 rejected. 59K floor catches catastrophic failure without false-tripping;
~70% of probe yield per DATA-FACTORY-LESSONS.md floor convention).

Inputs:
    s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/entities_lance
        (unique_entity_id + legal_business_name; filter CA on physical_address_state_normalized
        OR mailing_address_state_or_province; DISTINCT (unique_entity_id, legal_business_name);
        normalize legal_business_name Python-side via normalize_entity_name)
    s3://dex-raw-landing-zone/polaris-warehouse/sos/ca_entities_lance
        (entity_name_normalized pre-normalized at PR #464; entity_num PK)

Output:
    s3://dex-raw-landing-zone/polaris-warehouse/bridges/sam_sos_ca_entities_lance
    (BTREE on sam_uei AND sos_entity_num; dual-BTREE per contract §C2/C3)

Match method REUSE (validator p3 / L21):
    register_bridge                          -> ops.bridges                (idempotent)
    start_bridge_run                         -> ops.bridge_generation_runs (status=running)
    write Lance + BTREE + tier counts
    complete_bridge_run                      -> status=completed + metrics
    fail_bridge_run (on error or dry-run)    -> status=failed + error
    (Method-definition and method-version-definition helpers are INTENTIONALLY
    NOT IMPORTED — see L21 / validator p3 — REUSER pattern mirrors PR #482/#481.)

Tier rule (symmetric two-sided per USAspending precedent L339-358):
    platinum = BOTH fan_out == 1 (1:1 exact)
    gold     = ONE side fan_out == 1 (1:N | N:1)
    silver   = BOTH fan_out <= 50 (N:M below collision threshold)
    rejected = EITHER fan_out >  50 (collision)

Plain Python — NOT Modal (USAspending precedent pattern):
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      uv run --project . python3 scripts/build_bridge_sam_sos_ca_entities_lance.py --apply

Dry-run (no Lance write, bridge run marked failed-dry-run):
    uv run --project . python3 scripts/build_bridge_sam_sos_ca_entities_lance.py
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
# CRITICAL validator p3 / L21: the method-definition and method-version-definition
# helpers are INTENTIONALLY OMITTED — this cycle is the FOURTH REUSER of
# legal_name_state_exact_ca v1.0.0 (publisher: PR #464 SBA; prior reusers:
# PR #466 UCC CA × CA SoS, PR #482 CSLB × CA SoS, PR #481 USAspending × CA SoS).
# Calling those helpers would UPSERT over the SBA-shape input_columns_left config
# {legal_name_normalized, borrstate} and corrupt the SBA × SoS, UCC × SoS,
# CSLB × SoS, and USAspending × SoS bridges' provenance trail.
from scripts._lib.match_method_registry import (
    complete_bridge_run,
    fail_bridge_run,
    register_bridge,
    start_bridge_run,
)

# ---------------------------------------------------------------------------
# Constants (load-bearing — match harness greps in verify constraints)
# ---------------------------------------------------------------------------

BRIDGE_NAME = "sam_sos_ca_entities"          # NAKED — no _lance suffix (ops.bridges convention)
DATASET_SLUG = "sam_sos_ca_entities_lance"   # _lance suffix for R2/Polaris/ops.data_sources
METHOD_NAME = "legal_name_state_exact_ca"    # REUSED — registered by PR #464
METHOD_SEMVER = "1.0.0"                      # REUSED — version row from PR #464
BRIDGE_VERSION = "1.0.0"

COLLISION_THRESHOLD = 50
# Validator-calibrated 2026-05-19 post full-corpus baseline probe.
# SAM CA entities (physical OR mailing) = 83,061; distinct (uei, name) = 82,428.
# Probe yield: 84,481 non-rejected (53,726 platinum + 1,742 gold + 29,013 silver
# + 875 rejected). Floor = 59,000 (~70% of measured; catches catastrophic failure
# from schema/normalizer regression without false-tripping on clean recipe runs).
MIN_ROWS_MATCHED = 59_000

SOURCE_LEFT = "sam_gov_entities_lance"
SOURCE_RIGHT = "sos_ca_entities_lance"

LEFT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/entities_lance"
RIGHT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sos/ca_entities_lance"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sam_sos_ca_entities_lance"

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
    """Load SAM CA-state entities + CA SoS entities into Arrow tables.

    SAM side (validator-corrected column names):
      - Read entities_lance with push-down filter on BOTH CA-state columns:
        physical_address_state_normalized='CA' OR mailing_address_state_or_province='CA'
        (column names verified against actual SAM Lance schema by validator 2026-05-19;
        the directive's original *_state_or_province_code names DO NOT EXIST).
      - Project: unique_entity_id, legal_business_name
      - DuckDB DISTINCT (unique_entity_id, legal_business_name) to collapse duplicates.
      - Python-side normalize: normalize_entity_name(legal_business_name) list comprehension
        → sam_legal_name_normalized column attached to Arrow table BEFORE DuckDB registration.
        NOT a DuckDB UDF. Do NOT use SAM's pre-supplied normalized name column
        (diverges at ~8.4% per validator probe — SAM keeps 'co' suffix + hyphens; _lib strips both).
      - Filter rows where normalization returned None (L33-blacklisted generic strings).

    SoS side:
      - Read entity_num + entity_name_normalized + entity metadata columns.
      - Filter to rows where entity_name_normalized is_valid().
    """
    import lance
    import pyarrow as pa
    import pyarrow.compute as pc
    import duckdb

    logger.info("opening sam_gov/entities_lance (CA filter + DISTINCT) ...")
    sam_ds = lance.dataset(LEFT_LANCE_URI, storage_options=storage_options)

    # Lance scanner does not support OR-predicate push-down via substrait for
    # compound expressions across two columns. Load the three columns needed for
    # the CA state OR-filter, then apply in DuckDB — the dataset is ~884K rows,
    # manageable in-process. Validator-corrected column names: physical_address_state_normalized
    # and mailing_address_state_or_province (NOT *_state_or_province_code).
    sam_raw = sam_ds.scanner(
        columns=[
            "unique_entity_id",
            "legal_business_name",
            "physical_address_state_normalized",
            "mailing_address_state_or_province",
        ],
    ).to_table()
    logger.info("  entities_lance total rows scanned: %d", len(sam_raw))

    # Apply CA filter + DISTINCT in DuckDB:
    # physical_address_state_normalized='CA' OR mailing_address_state_or_province='CA'
    con_distinct = duckdb.connect()
    con_distinct.register("sam_raw", sam_raw)
    left_distinct_arrow = con_distinct.execute(
        """
        SELECT DISTINCT unique_entity_id, legal_business_name
        FROM sam_raw
        WHERE (physical_address_state_normalized = 'CA' OR mailing_address_state_or_province = 'CA')
          AND legal_business_name IS NOT NULL
          AND unique_entity_id IS NOT NULL
        """
    ).arrow().read_all()
    rows_after_distinct = len(left_distinct_arrow)
    logger.info("  entities_lance CA distinct (unique_entity_id, legal_business_name): %d rows", rows_after_distinct)

    # Python-side normalize: attach sam_legal_name_normalized column.
    # CRITICAL: NOT a DuckDB UDF and NOT the SAM pre-normalized column.
    # Mirror USAspending precedent at L217-228.
    names_raw = left_distinct_arrow.column("legal_business_name").to_pylist()
    normalized_names = [normalize_entity_name(n) for n in names_raw]
    left_arrow = left_distinct_arrow.append_column(
        "sam_legal_name_normalized",
        pa.array(normalized_names, type=pa.string()),
    )

    # Filter rows where normalization returned None (L33-blacklisted generic strings).
    valid_mask = pc.is_valid(left_arrow.column("sam_legal_name_normalized"))
    left_arrow = left_arrow.filter(valid_mask)
    rows_left = len(left_arrow)
    logger.info("  after normalize + filter (non-None normalized): %d rows", rows_left)

    logger.info("opening sos/ca_entities_lance ...")
    sos_ds = lance.dataset(RIGHT_LANCE_URI, storage_options=storage_options)
    sos_filter = pc.field("entity_name_normalized").is_valid()
    sos_tbl = sos_ds.scanner(
        columns=[
            "entity_num",
            "entity_name",
            "entity_name_normalized",
            "entity_status",
            "standing_sos",
            "entity_type",
            "llc_management_structure",
            "initial_filing_date",
        ],
        filter=sos_filter,
    ).to_table()
    rows_sos = len(sos_tbl)
    logger.info("  sos ca_entities_lance (entity_name_normalized is_valid): %d rows", rows_sos)

    return left_arrow, sos_tbl, rows_left, rows_sos


def _build_match_table(
    left_tbl,
    sos_tbl,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
) -> tuple:
    """Run exact-equality JOIN + fan-out tiering in DuckDB (Arrow bridge).

    Join key: sam_legal_name_normalized (SAM, Python-normalized) = entity_name_normalized (SoS).
    Fan-out: separate sam_fan_out (per normalized name across SAM rows) and
    sos_fan_out (per normalized name across distinct SoS entity_nums).
    Mirrors USAspending precedent at L254-389.
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

    # 1. Inner JOIN on normalized name (name-only composite key; state-implicit from CA filter).
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_raw AS
        SELECT
            l.unique_entity_id                   AS sam_uei,
            l.legal_business_name                AS sam_legal_name_raw,
            l.sam_legal_name_normalized,
            s.entity_num                         AS sos_entity_num,
            s.entity_name                        AS sos_entity_name,
            s.entity_name_normalized,
            s.entity_status,
            s.standing_sos,
            s.entity_type,
            s.llc_management_structure,
            s.initial_filing_date,
            '{METHOD_NAME}'                      AS match_method,
            l.sam_legal_name_normalized          AS match_value,
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

    # 2. Fan-out counts (symmetric two-sided per USAspending precedent L314-330).
    #    sam_fan_out: # of SAM rows sharing this normalized name.
    #    sos_fan_out: # of distinct SoS entity_nums for this normalized name.
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
               COUNT(DISTINCT sos_entity_num) AS sos_fan_out
        FROM bridge_raw GROUP BY 1
        """
    )

    # 3. Tier rule (symmetric two-sided per USAspending precedent L338-358):
    #    platinum = BOTH fan_out == 1
    #    gold     = ONE side fan_out == 1
    #    silver   = BOTH <= COLLISION_THRESHOLD (else)
    #    rejected = EITHER > COLLISION_THRESHOLD
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

    # 4. Filter rejected rows before write (precedent L364).
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
    """Write bridge_match to Lance via Arrow-bridge + dual BTREE (sam_uei, sos_entity_num).

    Mirrors USAspending precedent at L392-442.
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

        # Dual BTREE per contract §C2/C3 (USAspending precedent L419-431).
        try:
            ds.create_scalar_index("sam_uei", index_type="BTREE", replace=True)
            logger.info("BTREE on sam_uei: OK")
        except Exception as e:
            logger.error("BTREE on sam_uei FAILED: %s", e)
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
    """Build the SAM CA entities × CA SoS entities Pattern B bridge."""
    parser = argparse.ArgumentParser(
        description="SAM.gov CA entities × CA SoS entities Pattern B bridge generator."
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
        "bridge: %s  method=%s v%s (REUSED from PR #464, 4th REUSER)  normalizer=v%s  apply=%s",
        BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER, NORMALIZER_VERSION, args.apply,
    )
    logger.info("left:  %s", LEFT_LANCE_URI)
    logger.info("right: %s", RIGHT_LANCE_URI)
    logger.info("out:   %s", BRIDGE_LANCE_URI)

    # Idempotent UPSERT on bridge_name — safe to call even on re-runs.
    # REUSER: only register_bridge (no method-definition helpers per L21/p3).
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "SAM.gov CA-state entities × CA SoS entities — legal-name exact match. "
            "Reuses legal_name_state_exact_ca v1.0.0 method registered by PR #464 (SBA x SoS publisher). "
            "Fourth REUSER after PR #466 (UCC CA x CA SoS), PR #482 (CSLB x CA SoS), "
            "PR #481 (USAspending x CA SoS). "
            "Filter: (physical_address_state_normalized='CA') OR (mailing_address_state_or_province='CA'); "
            "~83K CA SAM entities matched against 9.39M CA SoS entities. "
            "Normalizer: _lib.entity_name_normalize applied Python-side to raw legal_business_name "
            "(SAM's pre-supplied column is divergent at ~8.4%); SoS side reads pre-normalized "
            "entity_name_normalized (PR #464 parity confirmed)."
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

"""SBA NY borrowers × SAM NY-physical-address entities — Pattern B REUSER bridge (Lance).

Pattern B exact-match bridge: SBA 7(a)/504 borrowers (legal_name_normalized,
filtered to borrstate='NY') × SAM federal-contractor registry entities
(sam_gov/entities_lance filtered physical_address_state_normalized='NY';
legal_business_name_normalized already pre-normalized upstream — no Python
normalization needed on the RIGHT side).

Method: legal_name_state_exact_ny v1.0.0 (REUSED — registered by PR #513
(SBA × NY State Authority contracts publisher); this script is the SECOND
REUSER, after the SBA NY × USAspending NY bridge in this same cycle).
Per L21, this script ONLY calls register_bridge + start_bridge_run +
complete_bridge_run + fail_bridge_run — the method-definition and
method-version-definition helpers are INTENTIONALLY OMITTED.

Pattern shape mirrors PR #487 (`build_bridge_usaspending_sos_ca_owner_lance.py`).

Bridge version: 1.0.0
COLLISION_THRESHOLD = 50 (symmetric two-sided).
MIN_ROWS_MATCHED = 1_000 (probe: 1,284 distinct SBA NY borrowers matched
SAM NY entities on normalized name; raw bridge rows expected ~2,000+ given
fan-out from name-collision UEIs; 1,000 floor catches catastrophic regression
with ~50% headroom).

Inputs:
    s3://dex-raw-landing-zone/polaris-warehouse/sba/borrowers_lance
        (legal_name_normalized + borrstate; filter borrstate='NY';
        DISTINCT legal_name_normalized post-filter Python-side.)
    s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/entities_lance
        (uei_normalized + legal_business_name_normalized +
        physical_address_state_normalized + cage_code_normalized +
        naics_primary_2digit + physical_address_zip5 + sam_archive_date;
        filter physical_address_state_normalized='NY'.)

Output:
    s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_ny_sam_lance
    (BTREE on sba_legal_name_normalized AND sam_uei_normalized;
    dual-BTREE per PR #466/#482/#487 precedent.)

Match method REUSE (L21):
    register_bridge                          -> ops.bridges                (idempotent)
    start_bridge_run                         -> ops.bridge_generation_runs (status=running)
    write Lance + dual BTREE + tier counts
    complete_bridge_run                      -> status=completed + metrics
    fail_bridge_run (on error or dry-run)    -> status=failed + error
    (Method-definition and method-version-definition helpers are INTENTIONALLY
    NOT IMPORTED — see L21 — REUSER pattern.)

Tier rule (symmetric two-sided per PR #466/#482/#487):
    platinum = BOTH fan_out == 1
    gold     = ONE side fan_out == 1
    silver   = BOTH fan_out <= 50
    rejected = EITHER fan_out >  50 (excluded from output)

Plain Python (NOT Modal):
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      uv run python scripts/build_bridge_sba_ny_sam_lance.py --apply

Dry-run (no Lance write):
    uv run python scripts/build_bridge_sba_ny_sam_lance.py
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# sys.path.insert per PR #481 pattern.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._lib.entity_name_normalize import (
    __version__ as NORMALIZER_VERSION,
)
from scripts._lib.lance_commit_lock import lance_commit_lock
# CRITICAL L21: method + method-version helpers are INTENTIONALLY OMITTED.
# This cycle is the SECOND REUSER of legal_name_state_exact_ny v1.0.0
# (publisher: PR #513). Calling the method-version helper would UPSERT
# over the publisher's input_columns_right config and corrupt provenance.
from scripts._lib.match_method_registry import (
    complete_bridge_run,
    fail_bridge_run,
    register_bridge,
    start_bridge_run,
)

# ---------------------------------------------------------------------------
# Constants (load-bearing — match harness greps)
# ---------------------------------------------------------------------------

BRIDGE_NAME = "sba_ny_sam"            # NAKED — no _lance suffix (ops.bridges convention)
DATASET_SLUG = "sba_ny_sam_lance"     # _lance suffix for R2/Polaris/ops.data_sources
METHOD_NAME = "legal_name_state_exact_ny"  # REUSED — from PR #513
METHOD_SEMVER = "1.0.0"               # REUSED
BRIDGE_VERSION = "1.0.0"

COLLISION_THRESHOLD = 50
# Probe (2026-05-18): 1,284 distinct SBA NY borrowers matched SAM NY entities
# on normalized name. Raw bridge rows expected ~2,000+ given fan-out (same
# name may appear under multiple UEIs in SAM — name-collision artifacts).
# 1,000 floor catches catastrophic regression with ~50% headroom vs 0.5 ×
# estimated yield.
MIN_ROWS_MATCHED = 1_000

SOURCE_LEFT = "sba_borrowers_lance"
SOURCE_RIGHT = "sam_gov_entities_lance"

LEFT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sba/borrowers_lance"
RIGHT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/entities_lance"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_ny_sam_lance"

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
    """Load SBA NY borrowers + SAM NY entities into Arrow tables.

    SBA side: read [legal_name_normalized, borrstate] with push-down filter
    borrstate='NY'; Python-side distinct → ~622,794 distinct names.

    SAM side: read [uei_normalized, legal_business_name_normalized,
    physical_address_state_normalized, cage_code_normalized,
    naics_primary_2digit, physical_address_zip5, sam_archive_date]
    with push-down filter physical_address_state_normalized='NY';
    filter where legal_business_name_normalized is_valid().
    Both sides pre-normalized — no Python normalization needed.
    """
    import lance
    import pyarrow as pa
    import pyarrow.compute as pc

    # ---- SBA side ----
    logger.info("opening sba/borrowers_lance (borrstate=NY filter) ...")
    sba_ds = lance.dataset(LEFT_LANCE_URI, storage_options=storage_options)
    sba_filter = pc.field("borrstate") == "NY"
    sba_raw = sba_ds.scanner(
        columns=["legal_name_normalized", "borrstate"],
        filter=sba_filter,
    ).to_table()
    rows_sba_raw = len(sba_raw)
    logger.info("  sba borrowers_lance (borrstate=NY): %d rows", rows_sba_raw)

    # Python-side distinct (OOM-resistant).
    names = sba_raw.column("legal_name_normalized").to_pylist()
    distinct_sba: set[str] = set()
    for nm in names:
        if not nm:
            continue
        if isinstance(nm, str) and len(nm) >= 2:
            distinct_sba.add(nm)
    del sba_raw, names

    sba_branded_arrow = pa.table({
        "sba_legal_name_normalized": pa.array(
            sorted(distinct_sba), type=pa.string()
        ),
    })
    rows_sba_distinct = len(sba_branded_arrow)
    del distinct_sba
    logger.info(
        "  sba_branded (distinct legal_name_normalized, NY only): %d names",
        rows_sba_distinct,
    )

    # ---- SAM side ----
    logger.info("opening sam_gov/entities_lance (physical_address_state_normalized=NY filter) ...")
    sam_ds = lance.dataset(RIGHT_LANCE_URI, storage_options=storage_options)
    sam_filter = (
        (pc.field("physical_address_state_normalized") == "NY")
        & pc.field("legal_business_name_normalized").is_valid()
    )
    sam_tbl = sam_ds.scanner(
        columns=[
            "uei_normalized",
            "legal_business_name_normalized",
            "physical_address_state_normalized",
            "cage_code_normalized",
            "naics_primary_2digit",
            "physical_address_zip5",
            "sam_archive_date",
        ],
        filter=sam_filter,
    ).to_table()
    rows_sam = len(sam_tbl)
    logger.info("  sam entities_lance (NY phys-addr; valid name): %d rows", rows_sam)

    return sba_branded_arrow, sam_tbl, rows_sba_distinct, rows_sam


def _build_match_table(
    sba_tbl,
    sam_tbl,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
) -> tuple:
    """Run exact-equality JOIN + fan-out tiering in DuckDB.

    Join key: sba_legal_name_normalized = legal_business_name_normalized.
    Both sides pre-normalized via the same canonical normalizer
    (`scripts._lib.entity_name_normalize`) — match is name-state-exact for NY.
    Fan-out: sba_fan_out (always 1 by construction) + sam_fan_out
    (distinct uei_normalized count per normalized name).
    """
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")

    con.register("sba", sba_tbl)
    con.register("sam", sam_tbl)

    rows_sba_reg = con.execute("SELECT COUNT(*) FROM sba").fetchone()[0]
    rows_sam_reg = con.execute("SELECT COUNT(*) FROM sam").fetchone()[0]
    logger.info("  registered: sba=%d  sam=%d", rows_sba_reg, rows_sam_reg)

    # 1. INNER JOIN on normalized name.
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_raw AS
        SELECT
            sba.sba_legal_name_normalized,
            sam.uei_normalized                       AS sam_uei_normalized,
            sam.legal_business_name_normalized       AS sam_legal_business_name_normalized,
            sam.cage_code_normalized                 AS sam_cage_code_normalized,
            sam.naics_primary_2digit                 AS sam_naics_primary_2digit,
            sam.physical_address_zip5                AS sam_physical_address_zip5,
            sam.sam_archive_date                     AS sam_archive_date,
            '{METHOD_NAME}'                          AS match_method,
            sba.sba_legal_name_normalized            AS match_value_normalized,
            '{BRIDGE_VERSION}'                       AS bridge_version,
            '{bridge_run_id}'                        AS bridge_run_id,
            TIMESTAMP '{generated_at_iso}'           AS generated_at
        FROM sba
        JOIN sam
          ON sba.sba_legal_name_normalized = sam.legal_business_name_normalized
        """
    )
    rows_matched_pre = con.execute("SELECT COUNT(*) FROM bridge_raw").fetchone()[0]
    logger.info("  bridge_raw (pre-tier): %d rows", rows_matched_pre)

    # 2. Fan-out counts (symmetric two-sided).
    con.execute(
        """
        CREATE TEMP TABLE sba_fanout AS
        SELECT sba_legal_name_normalized, COUNT(*) AS sba_fan_out
        FROM bridge_raw GROUP BY 1
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE sam_fanout AS
        SELECT sba_legal_name_normalized,
               COUNT(DISTINCT sam_uei_normalized) AS sam_fan_out
        FROM bridge_raw GROUP BY 1
        """
    )

    # 3. Tier rule.
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            b.*,
            sf.sba_fan_out,
            sn.sam_fan_out,
            CASE
                WHEN sf.sba_fan_out > {COLLISION_THRESHOLD}
                  OR sn.sam_fan_out > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN sf.sba_fan_out = 1 AND sn.sam_fan_out = 1
                    THEN 'platinum'
                WHEN sf.sba_fan_out = 1 OR  sn.sam_fan_out = 1
                    THEN 'gold'
                ELSE 'silver'
            END AS confidence_tier
        FROM bridge_raw b
        JOIN sba_fanout sf USING (sba_legal_name_normalized)
        JOIN sam_fanout sn USING (sba_legal_name_normalized)
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
    """Write bridge_match to Lance via Arrow-bridge + dual BTREE."""
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
            ds.create_scalar_index("sba_legal_name_normalized", index_type="BTREE", replace=True)
            logger.info("BTREE on sba_legal_name_normalized: OK")
        except Exception as e:
            logger.error("BTREE on sba_legal_name_normalized FAILED: %s", e)
            raise
        try:
            ds.create_scalar_index("sam_uei_normalized", index_type="BTREE", replace=True)
            logger.info("BTREE on sam_uei_normalized: OK")
        except Exception as e:
            logger.error("BTREE on sam_uei_normalized FAILED: %s", e)
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
    """Build the SBA NY × SAM NY Pattern B REUSER bridge."""
    parser = argparse.ArgumentParser(
        description="SBA NY × SAM NY Pattern B REUSER bridge generator."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Write to Lance. Without this flag runs in dry-run mode.",
    )
    args = parser.parse_args()

    _ensure_db_url()
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)

    storage_options = _storage_options()
    started_at = datetime.now(timezone.utc)
    t0 = time.time()

    logger.info(
        "bridge: %s  method=%s v%s (REUSED from PR #513)  normalizer=v%s  apply=%s",
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
            "SBA NY borrowers x SAM NY-physical-address entities — legal-name exact match. "
            "Reuses legal_name_state_exact_ny v1.0.0 method registered by PR #513 "
            "(SBA x NY State Authority contracts publisher). SECOND REUSER of this method "
            "(after sba_ny_usaspending). Spine: sba/borrowers_lance filtered borrstate='NY' "
            "+ distinct legal_name_normalized (LEFT); sam_gov/entities_lance filtered "
            "physical_address_state_normalized='NY' (RIGHT); both sides pre-normalized "
            "by the same canonical normalizer. Dual BTREE on sba_legal_name_normalized + "
            "sam_uei_normalized."
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
        sba_tbl, sam_tbl, rows_left, rows_right = _materialize_inputs(storage_options)
        con, counts = _build_match_table(
            sba_tbl, sam_tbl,
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
    raise SystemExit(main())

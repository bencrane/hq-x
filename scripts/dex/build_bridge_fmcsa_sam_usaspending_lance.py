#!/usr/bin/env python3
"""Pattern A enriched-cohort emit: FMCSA motor carriers × federal-contract award rollups.

One row per distinct FMCSA carrier in `fmcsa_sam_domain_lance` (63,299 dot_numbers),
with LEFT-JOINed federal award rollup aggregated across all matched UEIs and
carrier descriptive fields from `carrier_essentials_lance`.

Pattern A enriched-cohort emit — NOT a new cross-source identity bridge:
  - Match logic was already settled by `fmcsa_sam_domain_lance` (Pattern B, PR #607).
  - This script does NOT register any new bridge or match method, and does NOT
    INSERT or UPDATE any rows in ops.bridges / ops.match_method_versions.
  - Provenance: per-row `fmcsa_sam_bridge_run_id` inherited from fmcsa_sam_domain_lance
    + this emit's own fresh `bridge_run_id` UUID propagated as a column.

Inputs:
  - bridges/fmcsa_sam_domain_lance  — 101,193 rows (63,299 distinct dot_numbers;
    columns: dot_number, uei, confidence_tier, bridge_run_id, match_value, etc.)
    NOTE: Polaris registration was deferred for this dataset — read directly from R2.
  - usaspending/recipient_grain_lance — 134,153 rows (per-UEI award rollup, keyed
    on recipient_uei; time-windowed obligation and count columns + set membership flags)
  - fmcsa/carrier_essentials_lance  — carrier descriptive fields (legal_name, dba_name,
    phy_state, fleet metrics, etc.) keyed on dot_number

Logic:
  - Spine = fmcsa_sam_domain_lance; grain = dot_number (1 row per distinct carrier).
  - A dot_number may match multiple UEIs (bridge fan-out via silver/gold domains).
    LEFT JOIN each matched UEI → recipient_grain_lance and SUM/MAX/aggregate across
    the carrier's matched UEIs.
  - LEFT JOIN carrier_essentials_lance on dot_number for carrier descriptive fields.
  - Non-award-winners get NULL award columns (LEFT JOIN — full spine is preserved).

Winner anchor: 19,062 dot_numbers resolve to at least one UEI in recipient_grain_lance
  (independently verified in the FMCSA-SAM bridge cycle's spine check). Hard-fail
  floor set at 17,000 (~89% of anchor) to catch a broken UEI join.

Output: polaris-warehouse/bridges/fmcsa_sam_usaspending_lance
  - BTREE scalar index on dot_number.
  - compact_files() + cleanup_old_versions(7 days).

Usage:
    doppler run --project hq-all --config prd -- \\
        uv run --with duckdb --with pylance --with pyarrow python \\
        apps/data-engine-x/scripts/build_bridge_fmcsa_sam_usaspending_lance.py --dry-run

    doppler run --project hq-all --config prd -- \\
        uv run --with duckdb --with pylance --with pyarrow python \\
        apps/data-engine-x/scripts/build_bridge_fmcsa_sam_usaspending_lance.py --apply
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("build_bridge_fmcsa_sam_usaspending_lance")

# Dataset identity -----------------------------------------------------------
DATASET_SLUG = "fmcsa_sam_usaspending_lance"
BRIDGE_VERSION = "1.0.0"

# Input URIs — read directly from R2 (no Polaris catalog dependency) ---------
FMCSA_SAM_BRIDGE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/fmcsa_sam_domain_lance"
)
RECIPIENT_GRAIN_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/recipient_grain_lance"
)
CARRIER_ESSENTIALS_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/fmcsa/carrier_essentials_lance"
)
OUTPUT_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/fmcsa_sam_usaspending_lance"
)

TMP_DIR = "/tmp/lance"

# Hard-fail floors -----------------------------------------------------------
# (a) Total rows >= 90% of distinct dot_number count in bridge (computed at runtime).
# (b) Winners (carriers with >= 1 UEI in recipient_grain) >= 17,000.
#     Anchored on independently-verified 19,062 winners from the FMCSA-SAM spine check.
MIN_ROW_FLOOR_PCT = 0.90
MIN_WINNERS_FLOOR = 17_000


def _lance_storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _materialize_inputs(storage_options: dict) -> tuple:
    """Read all three Lance datasets via PyLance scanner with column projection."""
    import lance
    import pyarrow.compute as pc

    logger.info("opening bridges/fmcsa_sam_domain_lance ...")
    bridge_ds = lance.dataset(FMCSA_SAM_BRIDGE_URI, storage_options=storage_options)
    try:
        versions = bridge_ds.versions()
        if versions:
            latest = versions[-1]
            ts_raw = (
                latest.get("timestamp")
                if isinstance(latest, dict)
                else getattr(latest, "timestamp", None)
            )
            logger.info(
                "  fmcsa_sam_domain_lance latest version=%s timestamp=%s",
                latest, ts_raw,
            )
    except Exception as e:  # noqa: BLE001
        logger.info("  fmcsa_sam_domain_lance freshness probe failed (non-fatal): %s", e)
    bridge_arrow = bridge_ds.scanner(
        columns=["dot_number", "uei", "confidence_tier", "bridge_run_id", "match_value"],
    ).to_table()
    logger.info(
        "  fmcsa_sam_domain_lance: %d rows", bridge_arrow.num_rows,
    )

    logger.info("opening usaspending/recipient_grain_lance ...")
    rg_ds = lance.dataset(RECIPIENT_GRAIN_URI, storage_options=storage_options)
    rg_arrow = rg_ds.scanner(
        columns=[
            "recipient_uei",
            "total_obligation_30d",
            "total_obligation_90d",
            "total_obligation_180d",
            "total_obligation_365d",
            "contract_count_30d",
            "contract_count_90d",
            "contract_count_180d",
            "contract_count_365d",
            "latest_contract_date",
            "earliest_contract_date_365d",
            "top_psc",
            "is_8a",
            "is_hubzone",
            "is_wosb",
            "is_edwosb",
            "is_sdvosb",
            "is_vosb",
            "is_sdb",
            "is_minority_owned",
            "is_nonprofit",
            "is_jv",
        ],
        filter=pc.field("recipient_uei").is_valid(),
    ).to_table()
    logger.info("  recipient_grain_lance: %d rows", rg_arrow.num_rows)

    logger.info("opening fmcsa/carrier_essentials_lance ...")
    ess_ds = lance.dataset(CARRIER_ESSENTIALS_URI, storage_options=storage_options)
    # Project a useful subset of carrier descriptive fields.
    # Use the most recent snapshot per dot_number via dedup in DuckDB.
    ess_arrow = ess_ds.scanner(
        columns=[
            "dot_number",
            "legal_name",
            "dba_name",
            "phy_city",
            "phy_state",
            "phy_zip",
            "carrier_operation",
            "fleet_bucket",
            "power_units_int",
            "total_drivers_int",
            "fleetsize_int",
            "safety_rating",
            "business_org_desc",
            "status_code",
            "snapshot",
        ],
        filter=pc.field("dot_number").is_valid(),
    ).to_table()
    logger.info("  carrier_essentials_lance: %d rows (multi-snapshot)", ess_arrow.num_rows)

    return bridge_arrow, rg_arrow, ess_arrow


def _build_cohort(
    bridge_arrow,
    rg_arrow,
    ess_arrow,
    *,
    emit_bridge_run_id: str,
    generated_at_iso: str,
) -> tuple:
    """Build the enriched cohort table in DuckDB. Returns (con, counts_dict)."""
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET memory_limit='12GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET preserve_insertion_order=false")
    con.register("bridge_raw", bridge_arrow)
    con.register("rg_raw", rg_arrow)
    con.register("ess_raw", ess_arrow)

    # Step 1: Compute bridge-level summary per dot_number.
    # Aggregate UEI fan-out metadata at carrier grain.
    logger.info("step 1: summarising fmcsa_sam_domain_lance at dot_number grain ...")
    con.execute(
        """
        CREATE TEMP TABLE bridge_spine AS
        SELECT
            dot_number,
            COUNT(DISTINCT uei)                                         AS matched_uei_count,
            LIST(DISTINCT uei ORDER BY uei)                             AS matched_ueis,
            -- best_confidence_tier: platinum > gold > silver
            CASE
                WHEN BOOL_OR(confidence_tier = 'platinum') THEN 'platinum'
                WHEN BOOL_OR(confidence_tier = 'gold')     THEN 'gold'
                ELSE 'silver'
            END                                                         AS best_confidence_tier,
            -- inherit bridge_run_id from bridge (first non-null; all rows have same run)
            MAX(bridge_run_id)                                          AS fmcsa_sam_bridge_run_id
        FROM bridge_raw
        GROUP BY dot_number
        """
    )
    distinct_dot_count = con.execute(
        "SELECT COUNT(*) FROM bridge_spine"
    ).fetchone()[0]
    logger.info("  bridge_spine: %d distinct dot_numbers", distinct_dot_count)

    # Step 2: Pre-aggregate recipient_grain at dot_number grain.
    # A dot_number may match multiple UEIs; SUM/MAX across all matched UEIs.
    logger.info("step 2: aggregating recipient_grain across matched UEIs per carrier ...")
    con.execute(
        """
        CREATE TEMP TABLE carrier_award_rollup AS
        SELECT
            sp.dot_number,
            SUM(rg.total_obligation_30d)                         AS total_obligation_30d,
            SUM(rg.total_obligation_90d)                         AS total_obligation_90d,
            SUM(rg.total_obligation_180d)                        AS total_obligation_180d,
            SUM(rg.total_obligation_365d)                        AS total_obligation_365d,
            SUM(rg.contract_count_30d)                           AS contract_count_30d,
            SUM(rg.contract_count_90d)                           AS contract_count_90d,
            SUM(rg.contract_count_180d)                          AS contract_count_180d,
            SUM(rg.contract_count_365d)                          AS contract_count_365d,
            MAX(rg.latest_contract_date)                         AS latest_contract_date,
            MIN(rg.earliest_contract_date_365d)                  AS earliest_contract_date_365d,
            -- top_psc from highest-365d-obligation matched UEI
            FIRST(rg.top_psc ORDER BY COALESCE(rg.total_obligation_365d, 0) DESC) AS top_psc,
            BOOL_OR(rg.is_8a)                                    AS is_8a,
            BOOL_OR(rg.is_hubzone)                               AS is_hubzone,
            BOOL_OR(rg.is_wosb)                                  AS is_wosb,
            BOOL_OR(rg.is_edwosb)                                AS is_edwosb,
            BOOL_OR(rg.is_sdvosb)                                AS is_sdvosb,
            BOOL_OR(rg.is_vosb)                                  AS is_vosb,
            BOOL_OR(rg.is_sdb)                                   AS is_sdb,
            BOOL_OR(rg.is_minority_owned)                        AS is_minority_owned,
            BOOL_OR(rg.is_nonprofit)                             AS is_nonprofit,
            BOOL_OR(rg.is_jv)                                    AS is_jv,
            COUNT(DISTINCT rg.recipient_uei)                     AS award_matched_uei_count
        FROM bridge_raw br
        INNER JOIN bridge_spine sp ON sp.dot_number = br.dot_number
        INNER JOIN rg_raw rg ON rg.recipient_uei = br.uei
        GROUP BY sp.dot_number
        """
    )
    winners = con.execute("SELECT COUNT(*) FROM carrier_award_rollup").fetchone()[0]
    logger.info("  carrier_award_rollup: %d winners (dot_numbers with >= 1 UEI in recipient_grain)", winners)

    # Step 3: Most-recent carrier_essentials snapshot per dot_number.
    logger.info("step 3: deduplicating carrier_essentials to most-recent snapshot ...")
    con.execute(
        """
        CREATE TEMP TABLE carrier_essentials_latest AS
        SELECT *
        FROM (
            SELECT
                dot_number,
                legal_name,
                dba_name,
                phy_city,
                phy_state,
                phy_zip,
                carrier_operation,
                fleet_bucket,
                power_units_int,
                total_drivers_int,
                fleetsize_int,
                safety_rating,
                business_org_desc,
                status_code,
                ROW_NUMBER() OVER (
                    PARTITION BY dot_number
                    ORDER BY snapshot DESC NULLS LAST
                ) AS rn
            FROM ess_raw
        ) t
        WHERE rn = 1
        """
    )
    ess_count = con.execute(
        "SELECT COUNT(*) FROM carrier_essentials_latest"
    ).fetchone()[0]
    logger.info("  carrier_essentials_latest (deduped): %d rows", ess_count)

    # Step 4: Assemble final output at dot_number grain.
    logger.info("step 4: assembling final cohort ...")
    con.execute(
        f"""
        CREATE TEMP TABLE cohort_out AS
        SELECT
            -- carrier identity (from bridge spine)
            sp.dot_number,
            -- carrier descriptive fields (from carrier_essentials)
            ess.legal_name,
            ess.dba_name,
            ess.phy_city,
            ess.phy_state,
            ess.phy_zip,
            ess.carrier_operation,
            ess.fleet_bucket,
            ess.power_units_int,
            ess.total_drivers_int,
            ess.fleetsize_int,
            ess.safety_rating,
            ess.business_org_desc,
            ess.status_code,
            -- bridge match metadata
            sp.matched_uei_count,
            sp.matched_ueis,
            sp.best_confidence_tier,
            -- award rollup (NULL for non-winners)
            ar.total_obligation_30d,
            ar.total_obligation_90d,
            ar.total_obligation_180d,
            ar.total_obligation_365d,
            ar.contract_count_30d,
            ar.contract_count_90d,
            ar.contract_count_180d,
            ar.contract_count_365d,
            ar.latest_contract_date,
            ar.earliest_contract_date_365d,
            ar.top_psc,
            COALESCE(ar.is_8a, FALSE)             AS is_8a,
            COALESCE(ar.is_hubzone, FALSE)         AS is_hubzone,
            COALESCE(ar.is_wosb, FALSE)            AS is_wosb,
            COALESCE(ar.is_edwosb, FALSE)          AS is_edwosb,
            COALESCE(ar.is_sdvosb, FALSE)          AS is_sdvosb,
            COALESCE(ar.is_vosb, FALSE)            AS is_vosb,
            COALESCE(ar.is_sdb, FALSE)             AS is_sdb,
            COALESCE(ar.is_minority_owned, FALSE)  AS is_minority_owned,
            COALESCE(ar.is_nonprofit, FALSE)       AS is_nonprofit,
            COALESCE(ar.is_jv, FALSE)              AS is_jv,
            ar.award_matched_uei_count,
            -- derived winner flag
            (ar.dot_number IS NOT NULL)            AS has_federal_award,
            -- provenance
            sp.fmcsa_sam_bridge_run_id,
            CAST('{emit_bridge_run_id}' AS VARCHAR)   AS bridge_run_id,
            '{BRIDGE_VERSION}'                        AS bridge_version,
            TIMESTAMP '{generated_at_iso}'            AS generated_at
        FROM bridge_spine sp
        LEFT JOIN carrier_award_rollup ar  ON ar.dot_number = sp.dot_number
        LEFT JOIN carrier_essentials_latest ess ON ess.dot_number = sp.dot_number
        """
    )

    forensic = con.execute(
        """
        SELECT
            COUNT(*)                                                  AS total_rows,
            COUNT(*) FILTER (WHERE has_federal_award)                 AS winners,
            COUNT(*) FILTER (WHERE NOT has_federal_award)             AS non_winners,
            COUNT(*) FILTER (WHERE best_confidence_tier = 'platinum') AS tier_platinum,
            COUNT(*) FILTER (WHERE best_confidence_tier = 'gold')     AS tier_gold,
            COUNT(*) FILTER (WHERE best_confidence_tier = 'silver')   AS tier_silver,
            COUNT(*) FILTER (WHERE total_obligation_365d > 0)         AS active_365d,
            COUNT(*) FILTER (WHERE ess.legal_name IS NULL)            AS missing_carrier_ess
        FROM cohort_out
        LEFT JOIN carrier_essentials_latest ess USING (dot_number)
        """
    ).fetchone()
    counts = {
        "total_rows": forensic[0],
        "winners": forensic[1],
        "non_winners": forensic[2],
        "tier_platinum": forensic[3],
        "tier_gold": forensic[4],
        "tier_silver": forensic[5],
        "active_365d": forensic[6],
        "missing_carrier_ess": forensic[7],
        "distinct_dots_in_bridge": distinct_dot_count,
    }
    return con, counts


def _write_lance(con, storage_options: dict) -> int:
    """Write cohort_out to Lance inside commit lock; create BTREE on dot_number."""
    import lance

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = TMP_DIR
    os.environ["LANCE_BYPASS_SPILLING"] = "true"

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        reader = con.from_query("SELECT * FROM cohort_out").to_arrow_reader(
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
        lance_count = ds.count_rows()
        logger.info(
            "wrote %d rows in %.1fs (version=%s)", lance_count, write_dur, ds.version,
        )

        # BTREE scalar index on dot_number for fast lookups.
        try:
            ds.create_scalar_index("dot_number", index_type="BTREE", replace=True)
            logger.info("  BTREE on dot_number: OK")
        except Exception as e:  # noqa: BLE001
            logger.error("BTREE on dot_number FAILED: %s", e)
            raise

        try:
            ds.optimize.compact_files()
            logger.info("  compact_files: OK")
        except Exception as e:  # noqa: BLE001
            logger.warning("compact_files failed (non-fatal): %s", e)
        try:
            ds.cleanup_old_versions(older_than=timedelta(days=7))
            logger.info("  cleanup_old_versions(7d): OK")
        except Exception as e:  # noqa: BLE001
            logger.warning("cleanup_old_versions failed (non-fatal): %s", e)

    return lance_count


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true", help="full Lance write")
    grp.add_argument("--dry-run", action="store_true", help="count + sanity checks only, no write")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: env var {var} not set")

    started_at = datetime.now(tz=timezone.utc)
    t0 = time.time()
    storage_options = _lance_storage_options()

    emit_bridge_run_id = (
        "00000000-0000-0000-0000-000000000000" if args.dry_run else str(uuid.uuid4())
    )

    logger.info("dataset:   %s", DATASET_SLUG)
    logger.info("output:    %s", OUTPUT_LANCE_URI)
    logger.info("run_id:    %s", emit_bridge_run_id)
    logger.info("mode:      %s", "DRY-RUN" if args.dry_run else "APPLY")

    bridge_arrow, rg_arrow, ess_arrow = _materialize_inputs(storage_options)
    con, counts = _build_cohort(
        bridge_arrow,
        rg_arrow,
        ess_arrow,
        emit_bridge_run_id=emit_bridge_run_id,
        generated_at_iso=started_at.isoformat(),
    )

    # ---- HARD-FAIL floors ---- #
    distinct_dots = counts["distinct_dots_in_bridge"]
    min_row_floor = int(distinct_dots * MIN_ROW_FLOOR_PCT)
    logger.info("")
    logger.info("=" * 60)
    logger.info("cohort sanity report:")
    logger.info("  total_rows:          %s  (floor: %s = %.0f%% of %s bridge dots)",
                f"{counts['total_rows']:,}", f"{min_row_floor:,}",
                MIN_ROW_FLOOR_PCT * 100, f"{distinct_dots:,}")
    logger.info("  winners:             %s  (floor: %s — anchor 19,062)",
                f"{counts['winners']:,}", f"{MIN_WINNERS_FLOOR:,}")
    logger.info("  non_winners:         %s", f"{counts['non_winners']:,}")
    logger.info("  tier_platinum:       %s", f"{counts['tier_platinum']:,}")
    logger.info("  tier_gold:           %s", f"{counts['tier_gold']:,}")
    logger.info("  tier_silver:         %s", f"{counts['tier_silver']:,}")
    logger.info("  active_365d:         %s", f"{counts['active_365d']:,}")
    logger.info("  missing_carrier_ess: %s", f"{counts['missing_carrier_ess']:,}")
    logger.info("=" * 60)

    failed = False
    if counts["total_rows"] < min_row_floor:
        logger.error(
            "HARD FAIL: total_rows=%d < floor=%d (%.0f%% of %d distinct bridge dots)",
            counts["total_rows"], min_row_floor, MIN_ROW_FLOOR_PCT * 100, distinct_dots,
        )
        failed = True
    if counts["winners"] < MIN_WINNERS_FLOOR:
        logger.error(
            "HARD FAIL: winners=%d < floor=%d (anchor=19,062) — UEI join is broken",
            counts["winners"], MIN_WINNERS_FLOOR,
        )
        failed = True
    if failed:
        return 1

    logger.info("floors passed — total_rows=%d  winners=%d",
                counts["total_rows"], counts["winners"])

    if args.dry_run:
        logger.info("DRY RUN complete — no Lance writes. duration=%.1fs", time.time() - t0)
        return 0

    # ---- APPLY ---- #
    lance_count = _write_lance(con, storage_options)

    if lance_count != counts["total_rows"]:
        logger.error(
            "FAIL: lance_count=%d != expected=%d", lance_count, counts["total_rows"],
        )
        return 1

    logger.info(
        "OK — emit_bridge_run_id=%s  lance_rows=%d  duration=%.1fs",
        emit_bridge_run_id, lance_count, time.time() - t0,
    )
    logger.info("     output: %s", OUTPUT_LANCE_URI)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

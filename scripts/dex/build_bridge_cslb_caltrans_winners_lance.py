"""CSLB licensees × CalTrans winning contractors bridge (Pattern B).

Pattern B exact-match bridge: CSLB licensees (business_name_normalized
from PR #480) × CalTrans awarded contracts (winning_contractor_name_normalized
from BOTH cc_awarded and PETS Lance datasets, UNION ALL with dedup).

MODIFIED in this cycle (caltrans-hcad-historical-backfill-pre-2017, 2026-05-18)
to UNION ALL across two right-side Lance datasets:
    - caltrans/awards_lance (ServiceNow cc_awarded, 2017-2026)
    - caltrans/awards_pets_lance (PETS payment system, ~2000-2026)
Per p4: dedup on winning_contractor_name_normalized (ROW_NUMBER OVER PARTITION BY,
preferring servicenow_cc_awarded when both datasets have the same contractor name).
Per p3 / L21 REUSER: NO register_match_method or register_match_method_version
calls — the method definition stays in ops.match_method_versions from PR #464.

Method: legal_name_state_exact_ca v1.0.0 (REUSED — the method + version rows
were registered by PR #464's build_bridge_sba_sos_ca_owner_lance.py; this
script ONLY calls register_bridge + start_bridge_run + complete_bridge_run +
fail_bridge_run per L21 — the method-definition and method-version-definition
helpers are INTENTIONALLY OMITTED; calling them would UPSERT over the SBA-shape
input_columns_left config and corrupt the SBA × SoS bridge's provenance trail).

This is the third REUSER of this method (first: PR #466 UCC CA × CA SoS;
second: PR #481 CSLB × CA SoS).

Bridge version: 1.0.0
COLLISION_THRESHOLD = 50
MIN_ROWS_MATCHED = 2800 (validator-calibrated post-probe for combined corpus;
    50% of projection 5,577 = 8.45 rows-per-match × 660 CSLB ∩ combined distinct names).

Inputs (RIGHT side — UNION ALL with dedup):
    s3://dex-raw-landing-zone/polaris-warehouse/caltrans/awards_lance
        (ServiceNow cc_awarded; winning_contractor_name_normalized; data_source='servicenow_cc_awarded')
    s3://dex-raw-landing-zone/polaris-warehouse/caltrans/awards_pets_lance
        (PETS payment data; winning_contractor_name_normalized; data_source='pets_payment')

Left input:
    s3://dex-raw-landing-zone/polaris-warehouse/cslb/licensees_lance
        (business_name_normalized pre-normalized; LicenseNo PK)

Output:
    s3://dex-raw-landing-zone/polaris-warehouse/bridges/cslb_caltrans_winners_lance
    BTREE on cslb_license_no AND winning_contractor_name_normalized.
    data_source column: 'servicenow_cc_awarded' | 'pets_payment' (per-row provenance).

Invocation:
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      uv run python scripts/build_bridge_cslb_caltrans_winners_lance.py --apply
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
    __version__ as NORMALIZER_VERSION,
)
from scripts._lib.lance_commit_lock import lance_commit_lock
# CRITICAL p3 / L21 REUSER: the method-definition and method-version-definition
# helpers are INTENTIONALLY OMITTED — calling register_match_method or
# register_match_method_version would UPSERT over the SBA-shape
# input_columns_left config and corrupt the SBA × SoS bridge's provenance
# trail. This is the third REUSER of legal_name_state_exact_ca v1.0.0.
from scripts._lib.match_method_registry import (
    complete_bridge_run,
    fail_bridge_run,
    register_bridge,
    start_bridge_run,
)

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)

# ---- constants (load-bearing — harness greps match these exact strings) ------

BRIDGE_NAME = "cslb_caltrans_winners"               # naked — ops.bridges convention
DATASET_SLUG = "cslb_caltrans_winners_lance"        # _lance suffix for R2/Polaris/ops.data_sources
METHOD_NAME = "legal_name_state_exact_ca"           # REUSED — registered by PR #464
METHOD_SEMVER = "1.0.0"                             # REUSED
BRIDGE_VERSION = "1.0.0"

COLLISION_THRESHOLD = 50
# Validator-calibrated 2026-05-18 post-probe (UPDATED from 150 to 2800 for combined corpus):
# 50% of projection 5,577 = 8.45 rows-per-match × 660 CSLB ∩ combined distinct names.
# Allows ~25% projection error + ~5% normalizer structural-variant misses (p5).
MIN_ROWS_MATCHED = 2800

SOURCE_LEFT = "cslb_licensees_lance"
# Combined source: UNION ALL of cc_awarded (2017+) and PETS (~2000+)
SOURCE_RIGHT = "caltrans_awards_union"  # logical name for the combined right-side input

LEFT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/cslb/licensees_lance"
# Right side is UNION ALL of both datasets (p4: dedup by winning_contractor_name_normalized)
RIGHT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/caltrans/awards_lance"  # p10: NOT sos/ (cc_awarded)
RIGHT_PETS_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/caltrans/awards_pets_lance"  # p12: verbatim
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/cslb_caltrans_winners_lance"

TMP_DIR = "/tmp/lance"


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
    """Load CSLB licensees + CalTrans awards (UNION ALL cc_awarded + PETS with dedup) into Arrow tables.

    p4 / UNION ALL dedup: The right side is a UNION ALL of two Lance datasets:
        - awards_lance (ServiceNow cc_awarded, 2017-2026): data_source='servicenow_cc_awarded'
        - awards_pets_lance (PETS, ~2000-2026): data_source='pets_payment'
    Dedup on winning_contractor_name_normalized using ROW_NUMBER() OVER (PARTITION BY
    winning_contractor_name_normalized ORDER BY CASE WHEN data_source='servicenow_cc_awarded' THEN 0 ELSE 1 END),
    preferring cc_awarded when both datasets have the same contractor name. This avoids
    double-counting the ~320-name overlap subset (p4 validator prediction).
    """
    import duckdb
    import lance
    import pyarrow.compute as pc

    logger.info("opening cslb/licensees_lance ...")
    cslb_ds = lance.dataset(LEFT_LANCE_URI, storage_options=storage_options)
    cslb_filter = pc.field("business_name_normalized").is_valid()
    cslb_tbl = cslb_ds.scanner(
        columns=[
            "LicenseNo",
            "BusinessName",
            "BusinessType",
            "business_name_normalized",
        ],
        filter=cslb_filter,
    ).to_table()
    logger.info("  cslb licensees_lance (business_name_normalized is_valid): %d rows", len(cslb_tbl))

    logger.info("opening caltrans/awards_lance (cc_awarded) ...")
    awards_cc_ds = lance.dataset(RIGHT_LANCE_URI, storage_options=storage_options)
    awards_cc_filter = pc.field("winning_contractor_name_normalized").is_valid()
    awards_cc_tbl = awards_cc_ds.scanner(
        columns=[
            "contract_number",
            "caltrans_district",
            "winning_contractor_name",
            "winning_contractor_name_normalized",
            "awarded_amount_dollars",
            "award_date",
            "work_description",
            "county_route_pm",
            "type_of_goal",
        ],
        filter=awards_cc_filter,
    ).to_table()
    logger.info("  caltrans awards_lance cc_awarded: %d rows", len(awards_cc_tbl))

    logger.info("opening caltrans/awards_pets_lance (PETS) ...")
    awards_pets_ds = lance.dataset(RIGHT_PETS_LANCE_URI, storage_options=storage_options)
    awards_pets_filter = pc.field("winning_contractor_name_normalized").is_valid()
    awards_pets_tbl = awards_pets_ds.scanner(
        columns=[
            "contract_number",
            "caltrans_district",
            "winning_contractor_name",
            "winning_contractor_name_normalized",
        ],
        filter=awards_pets_filter,
    ).to_table()
    logger.info("  caltrans awards_pets_lance: %d rows", len(awards_pets_tbl))

    # Build UNION ALL with dedup in DuckDB (p4)
    con = duckdb.connect()
    con.register("awards_cc", awards_cc_tbl)
    con.register("awards_pets", awards_pets_tbl)

    # UNION ALL of cc_awarded and PETS, adding data_source column for provenance
    con.execute(
        """
        CREATE TEMP TABLE awards_union AS
        SELECT
            contract_number,
            caltrans_district,
            winning_contractor_name,
            winning_contractor_name_normalized,
            awarded_amount_dollars,
            award_date,
            work_description,
            county_route_pm,
            type_of_goal,
            'servicenow_cc_awarded' AS data_source
        FROM awards_cc
        UNION ALL
        SELECT
            contract_number,
            caltrans_district,
            winning_contractor_name,
            winning_contractor_name_normalized,
            NULL::DOUBLE             AS awarded_amount_dollars,
            NULL::VARCHAR            AS award_date,
            NULL::VARCHAR            AS work_description,
            NULL::VARCHAR            AS county_route_pm,
            NULL::VARCHAR            AS type_of_goal,
            'pets_payment'           AS data_source
        FROM awards_pets
        """
    )

    # p4: Dedup on NORMALIZED contract_number (strip '-', uppercase) — prefer
    # servicenow_cc_awarded when both sources contain the same physical contract.
    # cc_awarded format: "04-1J9614"  PETS format: "041J9614" — same contract.
    # Per-contract dedup (not per-contractor) preserves the per-contract grain
    # the bridge needs for fan-out tiering. ~3,919 contracts overlap between the
    # two sources; ~14,249 unique contracts remain post-dedup.
    con.execute(
        """
        CREATE TEMP TABLE awards_dedup AS
        SELECT contract_number, caltrans_district, winning_contractor_name,
               winning_contractor_name_normalized, awarded_amount_dollars,
               award_date, work_description, county_route_pm, type_of_goal,
               data_source
        FROM (
            SELECT *,
                ROW_NUMBER() OVER (
                    PARTITION BY UPPER(REPLACE(contract_number, '-', ''))
                    ORDER BY CASE WHEN data_source = 'servicenow_cc_awarded' THEN 0 ELSE 1 END
                ) AS rn
            FROM awards_union
        )
        WHERE rn = 1
        """
    )

    dedup_rows = con.execute("SELECT COUNT(*) FROM awards_dedup").fetchone()[0]
    cc_rows_in_dedup = con.execute(
        "SELECT COUNT(*) FROM awards_dedup WHERE data_source = 'servicenow_cc_awarded'"
    ).fetchone()[0]
    pets_rows_in_dedup = con.execute(
        "SELECT COUNT(*) FROM awards_dedup WHERE data_source = 'pets_payment'"
    ).fetchone()[0]
    logger.info(
        "  awards_dedup (UNION ALL after dedup): %d rows (cc=%d pets=%d)",
        dedup_rows, cc_rows_in_dedup, pets_rows_in_dedup,
    )

    caltrans_tbl = con.execute(
        "SELECT contract_number, caltrans_district, winning_contractor_name, "
        "winning_contractor_name_normalized, awarded_amount_dollars, award_date, "
        "work_description, county_route_pm, type_of_goal, data_source FROM awards_dedup"
    ).fetch_arrow_table()
    con.close()

    return cslb_tbl, caltrans_tbl, len(cslb_tbl), caltrans_tbl.num_rows


def _build_match_table(
    cslb_tbl,
    caltrans_tbl,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
) -> tuple:
    """Run exact-equality JOIN + fan-out tiering in DuckDB."""
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET memory_limit='16GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")

    con.register("cslb", cslb_tbl)
    con.register("caltrans", caltrans_tbl)

    logger.info(
        "  registered: cslb=%d  caltrans=%d",
        con.execute("SELECT COUNT(*) FROM cslb").fetchone()[0],
        con.execute("SELECT COUNT(*) FROM caltrans").fetchone()[0],
    )

    # 1. Inner JOIN on normalized name.
    # data_source column carries per-row provenance: 'servicenow_cc_awarded' | 'pets_payment'
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_raw AS
        SELECT
            c."LicenseNo"                             AS cslb_license_no,
            c."BusinessName"                          AS cslb_business_name_raw,
            c."BusinessType"                          AS cslb_business_type,
            c.business_name_normalized,
            ct.contract_number,
            ct.caltrans_district,
            ct.winning_contractor_name,
            ct.winning_contractor_name_normalized,
            ct.awarded_amount_dollars,
            ct.award_date,
            ct.work_description,
            ct.county_route_pm,
            ct.type_of_goal,
            ct.data_source                            AS data_source,
            '{METHOD_NAME}'                           AS match_method,
            c.business_name_normalized                AS match_value_normalized,
            'CA'                                      AS match_state,
            '{BRIDGE_VERSION}'                        AS bridge_version,
            '{bridge_run_id}'                         AS bridge_run_id,
            TIMESTAMP '{generated_at_iso}'            AS generated_at
        FROM cslb c
        JOIN caltrans ct
          ON c.business_name_normalized = ct.winning_contractor_name_normalized
        """
    )
    rows_pre = con.execute("SELECT COUNT(*) FROM bridge_raw").fetchone()[0]
    logger.info("  bridge_raw (pre-tier): %d rows", rows_pre)

    # 2. Fan-out counts (symmetric two-sided per PR #466 pattern).
    con.execute(
        """
        CREATE TEMP TABLE cslb_fanout AS
        SELECT business_name_normalized, COUNT(*) AS cslb_fan_out
        FROM bridge_raw GROUP BY 1
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE caltrans_fanout AS
        SELECT winning_contractor_name_normalized,
               COUNT(DISTINCT contract_number) AS caltrans_fan_out
        FROM bridge_raw GROUP BY 1
        """
    )

    # 3. Tier rule (symmetric two-sided per PR #466 / cslb_sos precedent).
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            b.*,
            cf.cslb_fan_out,
            ctf.caltrans_fan_out,
            CASE
                WHEN cf.cslb_fan_out   > {COLLISION_THRESHOLD}
                  OR ctf.caltrans_fan_out > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN cf.cslb_fan_out = 1 AND ctf.caltrans_fan_out = 1
                    THEN 'platinum'
                WHEN cf.cslb_fan_out = 1 OR  ctf.caltrans_fan_out = 1
                    THEN 'gold'
                ELSE 'silver'
            END AS confidence_tier
        FROM bridge_raw b
        JOIN cslb_fanout cf USING (business_name_normalized)
        JOIN caltrans_fanout ctf ON ctf.winning_contractor_name_normalized = b.winning_contractor_name_normalized
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
    """Write bridge_match to Lance + dual BTREE."""
    import lance

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")
    os.environ["TMPDIR"] = TMP_DIR

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        logger.info("writing bridge Lance at %s ...", BRIDGE_LANCE_URI)
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
        logger.info("wrote %d rows in %.1fs (version=%s)", lance_count, write_dur, ds.version)

        # Dual BTREE per harness verify_s3 checks.
        ds.create_scalar_index("cslb_license_no", index_type="BTREE", replace=True)
        logger.info("BTREE on cslb_license_no: OK")
        ds.create_scalar_index("winning_contractor_name_normalized", index_type="BTREE", replace=True)
        logger.info("BTREE on winning_contractor_name_normalized: OK")

        try:
            ds.optimize.compact_files()
        except Exception as exc:
            logger.warning("compact_files failed (non-fatal): %s", exc)
        try:
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as exc:
            logger.warning("cleanup_old_versions failed (non-fatal): %s", exc)

    return lance_count


def main() -> int:
    parser = argparse.ArgumentParser(
        description="CSLB licensees × CalTrans winning contractors Pattern B bridge."
    )
    parser.add_argument("--apply", action="store_true", default=False)
    args = parser.parse_args()

    _ensure_db_url()
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)

    storage_options = _storage_options()
    started_at = datetime.now(timezone.utc)
    t0 = time.time()

    logger.info(
        "bridge: %s  method=%s v%s (REUSED from PR #464)  normalizer=v%s  apply=%s",
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
            "CSLB licensees × CalTrans winning contractors — legal-name exact match. "
            "Reuses legal_name_state_exact_ca v1.0.0 method (PR #464). "
            "Third REUSER after PR #466 (UCC CA × SoS) and PR #481 (CSLB × SoS). "
            "Joins CA-licensed contractors to state highway construction award history."
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
        cslb_tbl, caltrans_tbl, rows_left, rows_right = _materialize_inputs(storage_options)
        con, counts = _build_match_table(
            cslb_tbl, caltrans_tbl,
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
            "OK — bridge_run_id=%s  lance_rows=%d  duration=%.1fs",
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

#!/usr/bin/env python3
"""DuckDB bridge generator: SAM-registered entity × SBA PPP borrower (Lance).

Companion to `build_bridge_sam_sba_borrower.py`. That bridge joins SAM ×
SBA 7(a)/504 borrowers. This bridge joins SAM × SBA PPP borrowers — the
SBA-program slice that lives in `sba/ppp_borrowers_lance` separately from
the 7(a)/504 universe.

Reads:
  SAM: `polaris-warehouse/sam_gov/entities_lance/`
  PPP: `polaris-warehouse/sba/ppp_borrowers_lance/`

Arrow-bridge pattern (NOT lance-duckdb extension).

CRITICAL: do NOT use SAM Parquet's pre-computed `legal_business_name_normalized`.
Measured 10.9% divergence vs the SBA/PPP-style normalizer. Re-normalize from raw
`legal_business_name` using the same SQL rule used everywhere in this module.

Match method: `name_state_exact` v1.0.0 (reused from PDL × SBA, FEC × SBA,
SAM × SBA 7a/504). Symmetric normalization on both sides.

Fan-out tiering: platinum/gold/silver/rejected (threshold = 50).

Output: `polaris-warehouse/bridges/sam_ppp_borrower_lance/`

Carries SAM authoritative attrs (UEI, entity_url, cage_code, NAICS, bus_type,
govt_bus_poc, entity_structure, state_of_incorporation, registration dates) +
PPP capital-deployment attrs (total_ppp_loans, total_ppp_approval, loanstatus,
franchise_brands_set, naics_codes_set, lender_set, borrower_address_normalized).

Row floor: ≥ 100,000. The PPP universe (~10M borrowers) is much wider than SAM
(~884K), so most PPP rows have no SAM counterpart; the intersection cohort is
proportionally smaller than the 320K seen on SAM × SBA 7(a)/504. Floor sized
conservatively for that asymmetry.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/build_bridge_sam_ppp_borrower_lance.py --apply

  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/build_bridge_sam_ppp_borrower_lance.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from scripts._lib.entity_name_normalize import (  # noqa: E402
    __version__ as NORMALIZER_VERSION,
)
from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402
from scripts._lib.match_method_registry import (  # noqa: E402
    complete_bridge_run,
    fail_bridge_run,
    register_bridge,
    register_match_method,
    register_match_method_version,
    start_bridge_run,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("build_bridge_sam_ppp_borrower_lance")

BRIDGE_NAME = "sam_ppp_borrower"
METHOD_NAME = "name_state_exact"
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "1.0.0"

SOURCE_LEFT = "sam_entities_lance"
SOURCE_RIGHT = "ppp_borrowers_lance"

SAM_ENTITIES_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/entities_lance/"
PPP_BORROWERS_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sba/ppp_borrowers_lance/"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sam_ppp_borrower_lance/"
DATASET_SLUG = "sam_ppp_borrower_lance"

COLLISION_THRESHOLD = 50
MIN_ROWS_MATCHED = 100_000
TMP_DIR = "/tmp/lance"


def _lance_storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _normalize_entity_sql(raw_expr: str) -> str:
    """SQL equivalent of entity_name_normalize.py v1.0.0. Matches the
    SBA emit + SAM × SBA bridge formula exactly so the join key normalization
    is symmetric across the 7(a)/504 and PPP slices of SBA universe.
    """
    suffixes = "incorporated|corporation|company|limited|pllc|llp|lp|llc|inc|ltd|corp|co|pa"
    return f"""
        CASE
          WHEN {raw_expr} IS NULL OR trim({raw_expr}) = '' THEN NULL
          ELSE NULLIF(
            trim(
              regexp_replace(
                regexp_replace(
                  regexp_replace(
                    lower(trim({raw_expr})),
                    '\\b({suffixes})\\b\\.?',
                    ' ',
                    'g'
                  ),
                  '[^\\w\\s]+',
                  ' ',
                  'g'
                ),
                '\\s+',
                ' ',
                'g'
              )
            ),
            ''
          )
        END
    """.strip()


def _materialize_inputs(storage_options: dict) -> tuple:
    """Read Lance datasets via Arrow-bridge."""
    import lance
    import pyarrow.compute as pc

    logger.info("opening SAM entities_lance via pyarrow (Arrow-bridge) ...")
    sam_ds = lance.dataset(SAM_ENTITIES_LANCE_URI, storage_options=storage_options)
    sam_cols = [
        "unique_entity_id", "legal_business_name",
        "physical_address_state_normalized", "physical_address_country_code",
        "dba_name", "entity_url", "cage_code",
        "primary_naics", "naics_code_string",
        "bus_type_string", "sba_business_types_string",
        "govt_bus_poc_first_name", "govt_bus_poc_last_name", "govt_bus_poc_title",
        "entity_structure", "state_of_incorporation",
        "purpose_of_registration",
        "physical_address_city", "physical_address_zip5",
        "registration_expiration_date", "last_update_date",
    ]
    available = {f.name for f in sam_ds.schema}
    scan_cols = [c for c in sam_cols if c in available]
    sam_arrow = sam_ds.scanner(
        columns=scan_cols,
        filter=pc.is_valid(pc.field("legal_business_name")),
    ).to_table()
    rows_left = len(sam_arrow)
    logger.info("  sam_entities_lance: %d rows (after legal_business_name filter)", rows_left)

    logger.info("opening PPP borrowers_lance via pyarrow (Arrow-bridge) ...")
    ppp_ds = lance.dataset(PPP_BORROWERS_LANCE_URI, storage_options=storage_options)
    ppp_cols = [
        "legal_name_normalized", "borrname_sample", "borrstate", "borrzip",
        "total_ppp_loans", "total_ppp_approval",
        "max_approval_date", "min_approval_date",
        "latest_loanstatus", "has_pending_commit",
        "franchise_brands_set", "naics_codes_set", "lender_set",
        "borrower_address_normalized",
    ]
    ppp_arrow = ppp_ds.scanner(columns=ppp_cols).to_table()
    rows_right = len(ppp_arrow)
    logger.info("  ppp_borrowers_lance: %d rows", rows_right)

    return sam_arrow, ppp_arrow, rows_left, rows_right


def _build_match_table(
    sam_arrow,
    ppp_arrow,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
) -> tuple:
    """Fan-out tiers + JOIN. Returns (con, counts_dict)."""
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")

    con.register("sam_raw", sam_arrow)
    con.register("ppp_raw", ppp_arrow)

    norm_expr = _normalize_entity_sql("legal_business_name")

    con.execute(
        f"""
        CREATE TEMP TABLE sam_branded AS
        SELECT
            unique_entity_id                AS sam_uei,
            legal_business_name             AS sam_legal_business_name,
            ({norm_expr})                   AS sam_name_normalized,
            upper(trim(physical_address_state_normalized)) AS sam_state,
            dba_name                        AS sam_dba_name,
            entity_url                      AS sam_entity_url,
            cage_code                       AS sam_cage_code,
            primary_naics                   AS sam_primary_naics,
            naics_code_string               AS sam_naics_code_string,
            bus_type_string                 AS sam_bus_type_string,
            sba_business_types_string       AS sam_sba_business_types_string,
            govt_bus_poc_first_name         AS sam_govt_poc_first,
            govt_bus_poc_last_name          AS sam_govt_poc_last,
            govt_bus_poc_title              AS sam_govt_poc_title,
            entity_structure                AS sam_entity_structure,
            state_of_incorporation          AS sam_state_of_incorporation,
            purpose_of_registration         AS sam_purpose_of_registration,
            physical_address_city           AS sam_city,
            physical_address_zip5           AS sam_zip5,
            registration_expiration_date    AS sam_registration_expiration_date,
            last_update_date                AS sam_last_update_date
        FROM sam_raw
        WHERE ({norm_expr}) IS NOT NULL
          AND physical_address_state_normalized IS NOT NULL
          AND length(trim(physical_address_state_normalized)) = 2
          AND (physical_address_country_code IS NULL
               OR physical_address_country_code = 'USA')
        """
    )
    rows_sam = con.execute("SELECT COUNT(*) FROM sam_branded").fetchone()[0]
    logger.info("  sam_branded (US, name+state non-null): %d", rows_sam)

    con.execute(
        """
        CREATE TEMP TABLE ppp_clean AS
        SELECT
            legal_name_normalized           AS ppp_name_normalized,
            borrname_sample                 AS ppp_borrname_sample,
            borrstate                       AS ppp_borrstate,
            borrzip                         AS ppp_borrzip,
            borrower_address_normalized     AS ppp_borrower_address_normalized,
            total_ppp_loans                 AS ppp_total_loans,
            total_ppp_approval              AS ppp_total_approval,
            max_approval_date               AS ppp_max_approval_date,
            min_approval_date               AS ppp_min_approval_date,
            latest_loanstatus               AS ppp_latest_loanstatus,
            has_pending_commit              AS ppp_has_pending_commit,
            franchise_brands_set            AS ppp_franchise_brands_set,
            naics_codes_set                 AS ppp_naics_codes_set,
            lender_set                      AS ppp_lender_set
        FROM ppp_raw
        WHERE legal_name_normalized IS NOT NULL
          AND borrstate IS NOT NULL
        """
    )

    logger.info("computing fan-out tables ...")
    con.execute(
        """
        CREATE TEMP TABLE sam_fanout AS
        SELECT sam_name_normalized AS norm_name, sam_state AS state,
               COUNT(*) AS sam_entities_at_name_state
        FROM sam_branded
        GROUP BY sam_name_normalized, sam_state
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE ppp_fanout AS
        SELECT ppp_name_normalized AS norm_name, ppp_borrstate AS state,
               COUNT(*) AS ppp_borrowers_at_name_state
        FROM ppp_clean
        GROUP BY ppp_name_normalized, ppp_borrstate
        """
    )

    logger.info("computing tiered JOIN ...")
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            s.sam_name_normalized           AS match_value_normalized,
            s.sam_state                     AS match_state,
            p.ppp_name_normalized,
            p.ppp_borrstate,
            s.sam_uei,
            pf.ppp_borrowers_at_name_state  AS ppp_fan_out,
            samf.sam_entities_at_name_state AS sam_fan_out,
            CASE
                WHEN pf.ppp_borrowers_at_name_state > {COLLISION_THRESHOLD}
                  OR samf.sam_entities_at_name_state > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN pf.ppp_borrowers_at_name_state = 1
                  AND samf.sam_entities_at_name_state = 1
                    THEN 'platinum'
                WHEN pf.ppp_borrowers_at_name_state = 1
                  OR samf.sam_entities_at_name_state = 1
                    THEN 'gold'
                ELSE 'silver'
            END                             AS confidence_tier,
            s.sam_legal_business_name,
            s.sam_dba_name,
            s.sam_entity_url,
            s.sam_cage_code,
            s.sam_primary_naics,
            s.sam_naics_code_string,
            s.sam_bus_type_string,
            s.sam_sba_business_types_string,
            s.sam_govt_poc_first,
            s.sam_govt_poc_last,
            s.sam_govt_poc_title,
            s.sam_entity_structure,
            s.sam_state_of_incorporation,
            s.sam_purpose_of_registration,
            s.sam_city,
            s.sam_zip5,
            s.sam_registration_expiration_date,
            s.sam_last_update_date,
            p.ppp_borrname_sample,
            p.ppp_borrzip,
            p.ppp_borrower_address_normalized,
            p.ppp_total_loans,
            p.ppp_total_approval,
            p.ppp_max_approval_date,
            p.ppp_min_approval_date,
            p.ppp_latest_loanstatus,
            p.ppp_has_pending_commit,
            p.ppp_franchise_brands_set,
            p.ppp_naics_codes_set,
            p.ppp_lender_set,
            TIMESTAMP '{generated_at_iso}'  AS generated_at,
            '{BRIDGE_VERSION}'              AS bridge_version,
            '{bridge_run_id}'               AS bridge_run_id
        FROM sam_branded s
        JOIN ppp_clean p
          ON p.ppp_name_normalized = s.sam_name_normalized
         AND p.ppp_borrstate       = s.sam_state
        JOIN sam_fanout samf
          ON samf.norm_name = s.sam_name_normalized AND samf.state = s.sam_state
        JOIN ppp_fanout pf
          ON pf.norm_name = p.ppp_name_normalized AND pf.state = p.ppp_borrstate
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE bridge_match AS
        SELECT * FROM bridge_all WHERE confidence_tier <> 'rejected'
        """
    )

    row_counts = con.execute(
        """
        SELECT COUNT(*),
               COUNT(*) FILTER (WHERE confidence_tier='platinum'),
               COUNT(*) FILTER (WHERE confidence_tier='gold'),
               COUNT(*) FILTER (WHERE confidence_tier='silver')
        FROM bridge_match
        """
    ).fetchone()
    rejected = con.execute(
        "SELECT COUNT(*) FROM bridge_all WHERE confidence_tier='rejected'"
    ).fetchone()[0]

    counts = {
        "rows_matched": row_counts[0],
        "rows_tier1": row_counts[1],
        "rows_tier2": row_counts[2],
        "rows_tier3": row_counts[3],
        "rows_collision_rejected": rejected,
    }
    return con, counts


def _write_bridge_lance(con, storage_options: dict) -> int:
    import lance

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
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
        logger.info("wrote %d rows in %.1fs (version=%s)", lance_count, write_dur, ds.version)

        os.environ["LANCE_BYPASS_SPILLING"] = "true"
        for col in ("match_value_normalized", "sam_uei", "ppp_name_normalized"):
            try:
                ds.create_scalar_index(col, index_type="BTREE", replace=True)
                logger.info("BTREE index created on %s", col)
            except Exception as e:
                logger.warning("BTREE index on %s failed (non-fatal): %s", col, e)
        try:
            ds.optimize.compact_files()
        except Exception as e:
            logger.warning("compact_files failed (non-fatal): %s", e)
        try:
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as e:
            logger.warning("cleanup_old_versions failed (non-fatal): %s", e)

    return lance_count


def _ensure_registry() -> None:
    register_match_method(
        method_name=METHOD_NAME,
        description=(
            "Exact-equality JOIN on (entity_name_normalized, 2-letter US state). "
            "Applies _lib/entity_name_normalize.py v1.0.0. "
            "CRITICAL: re-normalize from raw name (NOT SAM's pre-computed field)."
        ),
    )
    register_match_method_version(
        method_name=METHOD_NAME,
        semver=METHOD_SEMVER,
        normalizer_module="_lib/entity_name_normalize.py",
        normalizer_version=NORMALIZER_VERSION,
        blacklist_module="_lib/entity_name_normalize.py",
        blacklist_version=NORMALIZER_VERSION,
        tier_rule_description="platinum=1:1; gold=1:N or N:1; silver=N:M ≤50; rejected=>50",
        rejection_rule_description="fan-out >50 on either side → rejected",
        input_columns_left=["legal_business_name", "physical_address_state_normalized"],
        input_columns_right=["legal_name_normalized", "borrstate"],
        output_value_description="normalized name + 2-letter state join key",
    )
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "SAM entities × SBA PPP borrowers (Lance). Sister to "
            "sam_sba_borrower_lance which covers 7(a)/504. This bridge "
            "captures the PPP slice with the same name+state symmetric match. "
            "Carries SAM authoritative attrs (UEI, entity_url, cage_code, "
            "bus_type/8(a)/HUBZone/WOSB, govt_bus_poc) onto every PPP borrower "
            "in the cohort, plus PPP-side capital-deployment attrs "
            "(total_ppp_approval, lender_set, franchise_brands_set, "
            "borrower_address_normalized) onto every SAM-matched row."
        ),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true")
    grp.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")
    if args.apply and not os.environ.get("DEX_DB_URL_DIRECT"):
        raise SystemExit("FAIL: DEX_DB_URL_DIRECT not set (required for registry)")

    started_at = datetime.now(tz=timezone.utc)
    t0 = time.time()
    storage_options = _lance_storage_options()

    logger.info("bridge: %s (method=%s v%s)", BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER)
    logger.info("normalizer: _lib/entity_name_normalize.py v%s", NORMALIZER_VERSION)
    logger.info("inputs: SAM entities Lance + SBA PPP borrowers Lance (Arrow-bridge)")
    logger.info("output: %s", BRIDGE_LANCE_URI)

    if args.dry_run:
        bridge_run_id = "00000000-0000-0000-0000-000000000000"
        run_uuid = None
    else:
        _ensure_registry()
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

    try:
        sam_arrow, ppp_arrow, rows_left, rows_right = _materialize_inputs(storage_options)
        con, counts = _build_match_table(
            sam_arrow, ppp_arrow,
            bridge_run_id=bridge_run_id,
            generated_at_iso=started_at.isoformat(),
        )

        logger.info("-" * 60)
        logger.info("bridge tier distribution:")
        logger.info("  rows_matched:            %d", counts["rows_matched"])
        logger.info("    platinum (1:1):         %d", counts["rows_tier1"])
        logger.info("    gold     (1:N | N:1):   %d", counts["rows_tier2"])
        logger.info("    silver   (N:M ≤%d):     %d", COLLISION_THRESHOLD, counts["rows_tier3"])
        logger.info("  rows_collision_rejected:  %d", counts["rows_collision_rejected"])

        if counts["rows_matched"] < MIN_ROWS_MATCHED:
            msg = (
                f"HARD FAIL: rows_matched={counts['rows_matched']:,} < "
                f"floor={MIN_ROWS_MATCHED:,}"
            )
            logger.error(msg)
            if run_uuid is not None:
                fail_bridge_run(run_uuid, msg)
            return 1

        if args.dry_run:
            logger.info("DRY RUN — no Lance / Postgres writes. duration=%.1fs", time.time() - t0)
            return 0

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
        logger.info("OK — run_id=%s  duration=%.1fs", bridge_run_id, time.time() - t0)
        return 0

    except Exception as exc:
        logger.exception("bridge generation failed")
        if run_uuid is not None:
            try:
                fail_bridge_run(run_uuid, str(exc))
            except Exception:
                logger.exception("also failed to mark run as failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

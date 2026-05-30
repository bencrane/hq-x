#!/usr/bin/env python3
"""Bridge generator: SAM × SBA PPP × Overture — three-axis composite.

Materializes the triple-axis composition of three confirmed identity bridges:

  base:    bridges/sam_ppp_borrower_with_address_lance  (SAM ↔ PPP confirmed
                                                          by name AND address)
  + path A: bridges/ppp_overture_address_lance          (PPP ↔ Overture via
                                                          PPP paperwork address)
  + path B: bridges/sam_overture_address_lance          (SAM ↔ Overture via
                                                          SAM physical address)

Every output row is a (SAM UEI ↔ PPP borrower ↔ Overture place_id) triple
where:
  - SAM ↔ PPP identity is already confirmed by BOTH name+state AND address
    (provenance from sam_ppp_borrower_with_address_lance).
  - The PPP-to-Overture or SAM-to-Overture link is provided by path A or path B
    (tagged on each row via overture_match_provenance).
  - When the same (sam_uei, place_id) appears via BOTH paths, rows are
    deduped and `overture_match_provenance='both'`.

Tier carried per axis:
  - sam_ppp_composite_tier   (from the composite bridge — already worst-of)
  - overture_attach_tier     (best of path A / path B tier when both present)
  - triple_axis_tier         (worst of sam_ppp_composite_tier and overture_attach_tier)

Floor: ≥ 50,000 rows.

Output: `polaris-warehouse/bridges/sam_ppp_overture_lance/`
Audit:  ops.bridge_generation_runs (bridge_name='sam_ppp_overture')

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/build_bridge_sam_ppp_overture_lance.py --apply

  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/build_bridge_sam_ppp_overture_lance.py --dry-run
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

from scripts._lib.address_normalize import (  # noqa: E402
    __version__ as ADDR_NORMALIZER_VERSION,
)
from scripts._lib.entity_name_normalize import (  # noqa: E402
    __version__ as NAME_NORMALIZER_VERSION,
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
logger = logging.getLogger("build_bridge_sam_ppp_overture_lance")

BRIDGE_NAME = "sam_ppp_overture"
METHOD_NAME = "triple_axis_composite"
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "1.0.0"

SOURCE_BASE = "sam_ppp_borrower_with_address_lance"
SOURCE_PATH_A = "ppp_overture_address_lance"
SOURCE_PATH_B = "sam_overture_address_lance"

BASE_URI    = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sam_ppp_borrower_with_address_lance"
PATH_A_URI  = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/ppp_overture_address_lance"
PATH_B_URI  = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sam_overture_address_lance"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sam_ppp_overture_lance"
DATASET_SLUG = "sam_ppp_overture_lance"

MIN_ROWS_MATCHED = 50_000
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


def _materialize_inputs(storage_options: dict) -> tuple:
    """Load all three source bridges as Arrow tables."""
    import lance

    logger.info("opening base bridge: %s ...", BASE_URI)
    base_ds = lance.dataset(BASE_URI, storage_options=storage_options)
    base_arrow = base_ds.to_table()
    logger.info("  %s: %d rows", SOURCE_BASE, len(base_arrow))

    logger.info("opening path A (PPP × Overture): %s ...", PATH_A_URI)
    path_a_ds = lance.dataset(PATH_A_URI, storage_options=storage_options)
    path_a_arrow = path_a_ds.to_table()
    logger.info("  %s: %d rows", SOURCE_PATH_A, len(path_a_arrow))

    logger.info("opening path B (SAM × Overture): %s ...", PATH_B_URI)
    path_b_ds = lance.dataset(PATH_B_URI, storage_options=storage_options)
    path_b_arrow = path_b_ds.to_table()
    logger.info("  %s: %d rows", SOURCE_PATH_B, len(path_b_arrow))

    return base_arrow, path_a_arrow, path_b_arrow


def _build_match_table(base_arrow, path_a_arrow, path_b_arrow, *,
                       bridge_run_id: str, generated_at_iso: str):
    """Compose the three sources; dedup per (sam_uei, place_id)."""
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")

    con.register("base", base_arrow)
    con.register("path_a", path_a_arrow)
    con.register("path_b", path_b_arrow)

    # Helper: tier ordering
    tier_ord_sql = (
        "CASE {col} "
        "WHEN 'platinum' THEN 1 "
        "WHEN 'gold' THEN 2 "
        "WHEN 'silver' THEN 3 "
        "ELSE 4 END"
    )

    # --- Path A: base ⨝ ppp_overture_address (PPP-side address attach) ---
    logger.info("computing path A (base ⨝ PPP × Overture) ...")
    con.execute(
        """
        CREATE TEMP TABLE attach_a AS
        SELECT
            c.sam_uei,
            c.ppp_legal_name_normalized,
            c.ppp_borrstate,
            c.ppp_borrzip,
            p.place_id,
            'via_ppp_address'                        AS overture_match_provenance,
            p.confidence_tier                        AS overture_attach_tier,
            -- Overture payload (path A side)
            p.overture_name_primary,
            p.overture_brand_name_primary,
            p.overture_brand_wikidata,
            p.overture_address_freeform,
            p.overture_address_locality,
            p.overture_categories_primary,
            p.overture_phone_primary,
            p.overture_website_primary,
            p.overture_email_primary,
            p.overture_operating_status,
            p.overture_confidence,
            p.address_base_normalized
        FROM base c
        JOIN path_a p
          ON  p.ppp_legal_name_normalized = c.ppp_legal_name_normalized
          AND p.ppp_borrstate              = c.ppp_borrstate
          AND p.ppp_borrzip                = c.ppp_borrzip
        """
    )
    rows_a = con.execute("SELECT COUNT(*) FROM attach_a").fetchone()[0]
    logger.info("  path A rows: %d", rows_a)

    # --- Path B: base ⨝ sam_overture_address (SAM-side address attach) ---
    logger.info("computing path B (base ⨝ SAM × Overture) ...")
    con.execute(
        """
        CREATE TEMP TABLE attach_b AS
        SELECT
            c.sam_uei,
            c.ppp_legal_name_normalized,
            c.ppp_borrstate,
            c.ppp_borrzip,
            s.place_id,
            'via_sam_address'                        AS overture_match_provenance,
            s.confidence_tier                        AS overture_attach_tier,
            s.overture_name_primary,
            s.overture_brand_name_primary,
            s.overture_brand_wikidata,
            s.overture_address_freeform,
            s.overture_address_locality,
            s.overture_categories_primary,
            s.overture_phone_primary,
            s.overture_website_primary,
            s.overture_email_primary,
            s.overture_operating_status,
            s.overture_confidence,
            s.address_base_normalized
        FROM base c
        JOIN path_b s
          ON s.sam_uei = c.sam_uei
        """
    )
    rows_b = con.execute("SELECT COUNT(*) FROM attach_b").fetchone()[0]
    logger.info("  path B rows: %d", rows_b)

    # UNION both paths
    con.execute(
        """
        CREATE TEMP TABLE attach_union AS
        SELECT * FROM attach_a
        UNION ALL
        SELECT * FROM attach_b
        """
    )

    # Dedup per (sam_uei, place_id): aggregate provenance ('both' if seen via both
    # paths), keep best (lowest-ordinal) overture_attach_tier across them.
    logger.info("deduping per (sam_uei, place_id) ...")
    con.execute(
        f"""
        CREATE TEMP TABLE attach_deduped AS
        SELECT
            sam_uei,
            place_id,
            ANY_VALUE(ppp_legal_name_normalized) AS ppp_legal_name_normalized,
            ANY_VALUE(ppp_borrstate)              AS ppp_borrstate,
            ANY_VALUE(ppp_borrzip)                AS ppp_borrzip,
            STRING_AGG(DISTINCT overture_match_provenance, '|' ORDER BY overture_match_provenance) AS overture_match_provenance,
            CASE
              WHEN MIN({tier_ord_sql.format(col='overture_attach_tier')}) = 1 THEN 'platinum'
              WHEN MIN({tier_ord_sql.format(col='overture_attach_tier')}) = 2 THEN 'gold'
              ELSE 'silver'
            END AS overture_attach_tier,
            ANY_VALUE(overture_name_primary)         AS overture_name_primary,
            ANY_VALUE(overture_brand_name_primary)   AS overture_brand_name_primary,
            ANY_VALUE(overture_brand_wikidata)       AS overture_brand_wikidata,
            ANY_VALUE(overture_address_freeform)     AS overture_address_freeform,
            ANY_VALUE(overture_address_locality)     AS overture_address_locality,
            ANY_VALUE(overture_categories_primary)   AS overture_categories_primary,
            ANY_VALUE(overture_phone_primary)        AS overture_phone_primary,
            ANY_VALUE(overture_website_primary)      AS overture_website_primary,
            ANY_VALUE(overture_email_primary)        AS overture_email_primary,
            ANY_VALUE(overture_operating_status)     AS overture_operating_status,
            ANY_VALUE(overture_confidence)           AS overture_confidence,
            ANY_VALUE(address_base_normalized)       AS address_base_normalized
        FROM attach_union
        GROUP BY sam_uei, place_id
        """
    )
    rows_dedup = con.execute("SELECT COUNT(*) FROM attach_deduped").fetchone()[0]
    logger.info("  deduped rows: %d", rows_dedup)

    # Final: join back to base for the full SAM + PPP payload + composite tier.
    logger.info("attaching base payload + triple_axis_tier ...")
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_match AS
        SELECT
            d.sam_uei,
            d.ppp_legal_name_normalized,
            d.ppp_borrstate,
            d.ppp_borrzip,
            d.place_id,
            d.overture_match_provenance,
            d.overture_attach_tier,
            c.composite_confidence_tier                          AS sam_ppp_composite_tier,
            CASE
              WHEN GREATEST(
                     {tier_ord_sql.format(col='c.composite_confidence_tier')},
                     {tier_ord_sql.format(col='d.overture_attach_tier')}
                   ) = 1 THEN 'platinum'
              WHEN GREATEST(
                     {tier_ord_sql.format(col='c.composite_confidence_tier')},
                     {tier_ord_sql.format(col='d.overture_attach_tier')}
                   ) = 2 THEN 'gold'
              ELSE 'silver'
            END                                                  AS triple_axis_tier,
            c.name_match_value_normalized,
            c.name_match_state,
            c.name_confidence_tier,
            c.name_sam_fan_out,
            c.name_ppp_fan_out,
            c.address_match_paths                                AS sam_ppp_address_match_paths,
            c.address_confidence_tier                            AS sam_ppp_address_confidence_tier,
            c.address_sam_fan_out                                AS sam_ppp_address_sam_fan_out,
            c.address_ppp_fan_out                                AS sam_ppp_address_ppp_fan_out,
            c.address_base_normalized                            AS sam_ppp_address_base_normalized,
            c.address_match_zip5                                 AS sam_ppp_address_match_zip5,
            c.sam_address_line_1_sample,
            c.sam_address_city_sample,
            -- SAM authoritative payload
            c.sam_legal_business_name,
            c.sam_dba_name,
            c.sam_entity_url,
            c.sam_cage_code,
            c.sam_primary_naics,
            c.sam_naics_code_string,
            c.sam_bus_type_string,
            c.sam_sba_business_types_string,
            c.sam_govt_poc_first,
            c.sam_govt_poc_last,
            c.sam_govt_poc_title,
            c.sam_entity_structure,
            c.sam_state_of_incorporation,
            c.sam_purpose_of_registration,
            c.sam_city,
            c.sam_zip5,
            c.sam_registration_expiration_date,
            c.sam_last_update_date,
            -- PPP capital-deployment payload
            c.ppp_borrname_sample,
            c.ppp_total_loans,
            c.ppp_total_approval,
            c.ppp_max_approval_date,
            c.ppp_min_approval_date,
            c.ppp_latest_loanstatus,
            c.ppp_has_pending_commit,
            c.ppp_franchise_brands_set,
            c.ppp_naics_codes_set,
            c.ppp_lender_set,
            -- Overture payload
            d.overture_name_primary,
            d.overture_brand_name_primary,
            d.overture_brand_wikidata,
            d.overture_address_freeform,
            d.overture_address_locality,
            d.overture_categories_primary,
            d.overture_phone_primary,
            d.overture_website_primary,
            d.overture_email_primary,
            d.overture_operating_status,
            d.overture_confidence,
            d.address_base_normalized                            AS overture_address_base_normalized,
            TIMESTAMP '{generated_at_iso}'                       AS generated_at,
            '{BRIDGE_VERSION}'                                   AS bridge_version,
            '{bridge_run_id}'                                    AS bridge_run_id
        FROM attach_deduped d
        JOIN base c
          ON  c.sam_uei                    = d.sam_uei
          AND c.ppp_legal_name_normalized  = d.ppp_legal_name_normalized
          AND c.ppp_borrstate              = d.ppp_borrstate
          AND c.ppp_borrzip                = d.ppp_borrzip
        """
    )

    row_counts = con.execute(
        """
        SELECT
          COUNT(*),
          COUNT(*) FILTER (WHERE triple_axis_tier='platinum'),
          COUNT(*) FILTER (WHERE triple_axis_tier='gold'),
          COUNT(*) FILTER (WHERE triple_axis_tier='silver'),
          COUNT(*) FILTER (WHERE overture_match_provenance='via_ppp_address'),
          COUNT(*) FILTER (WHERE overture_match_provenance='via_sam_address'),
          COUNT(*) FILTER (WHERE overture_match_provenance='via_ppp_address|via_sam_address'),
          COUNT(*) FILTER (WHERE overture_website_primary IS NOT NULL),
          COUNT(DISTINCT sam_uei),
          COUNT(DISTINCT place_id)
        FROM bridge_match
        """
    ).fetchone()

    counts = {
        "rows_matched": row_counts[0],
        "rows_tier1": row_counts[1],
        "rows_tier2": row_counts[2],
        "rows_tier3": row_counts[3],
        "rows_via_ppp_only": row_counts[4],
        "rows_via_sam_only": row_counts[5],
        "rows_via_both": row_counts[6],
        "rows_with_website": row_counts[7],
        "distinct_sam_uei": row_counts[8],
        "distinct_place_id": row_counts[9],
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
        logger.info(
            "wrote %d rows in %.1fs (version=%s)", lance_count, write_dur, ds.version
        )

        os.environ["LANCE_BYPASS_SPILLING"] = "true"
        for col in ("sam_uei", "place_id", "triple_axis_tier"):
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
            "Three-axis composite. Base is sam_ppp_borrower_with_address_lance "
            "(SAM ↔ PPP confirmed by name AND address). Overture attachment via "
            "two paths UNION'd then deduped per (sam_uei, place_id): "
            "(A) ppp_overture_address_lance — joined on PPP identity; "
            "(B) sam_overture_address_lance — joined on sam_uei. "
            "Each row's triple_axis_tier = worst of sam_ppp_composite_tier and "
            "overture_attach_tier."
        ),
    )
    register_match_method_version(
        method_name=METHOD_NAME,
        semver=METHOD_SEMVER,
        normalizer_module="composite (delegates to upstream bridges)",
        normalizer_version=f"name v{NAME_NORMALIZER_VERSION} / addr v{ADDR_NORMALIZER_VERSION}",
        blacklist_module="(same as upstream bridges)",
        blacklist_version=f"name v{NAME_NORMALIZER_VERSION} / addr v{ADDR_NORMALIZER_VERSION}",
        tier_rule_description=(
            "triple_axis_tier = worst of (sam_ppp_composite_tier, overture_attach_tier); "
            "no rejection at this layer — upstream bridges have already applied "
            ">50 fan-out cap"
        ),
        rejection_rule_description="(none; relies on upstream bridges)",
        input_columns_left=[
            "sam_uei", "ppp_legal_name_normalized", "ppp_borrstate", "ppp_borrzip",
        ],
        input_columns_right=[
            "place_id", "overture_match_provenance",
        ],
        output_value_description=(
            "(sam_uei, ppp_borrower, place_id) triple confirmed across SAM, PPP, "
            "and Overture identity axes. Carries SAM authoritative payload, PPP "
            "capital-deployment payload, and Overture commercial payload on every row."
        ),
    )
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_BASE,
        source_right=f"{SOURCE_PATH_A}+{SOURCE_PATH_B}",
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "Three-axis composite: SAM-registered federal entity × SBA PPP "
            "borrower × Overture storefront, where SAM↔PPP is confirmed by "
            "both name and address, and Overture is attached via either the "
            "PPP-paperwork-address path or the SAM-physical-address path "
            "(deduped + tagged with overture_match_provenance). Single-table "
            "view of the full chain — analytical artifact for downstream "
            "composition without re-deriving the three-way JOIN."
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

    logger.info(
        "bridge: %s  method=%s v%s",
        BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER,
    )
    logger.info(
        "inputs: %s + %s + %s",
        SOURCE_BASE, SOURCE_PATH_A, SOURCE_PATH_B,
    )
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
            source_left=SOURCE_BASE,
            source_right=f"{SOURCE_PATH_A}+{SOURCE_PATH_B}",
            match_method=METHOD_NAME,
            r2_output_key=BRIDGE_LANCE_URI,
        )
        bridge_run_id = str(run_uuid)
        logger.info("bridge_run_id=%s", bridge_run_id)

    try:
        base_arrow, path_a_arrow, path_b_arrow = _materialize_inputs(storage_options)
        con, counts = _build_match_table(
            base_arrow, path_a_arrow, path_b_arrow,
            bridge_run_id=bridge_run_id,
            generated_at_iso=started_at.isoformat(),
        )

        logger.info("-" * 60)
        logger.info("triple-axis bridge counts:")
        logger.info("  rows_matched:           %d", counts["rows_matched"])
        logger.info("    triple_axis platinum:  %d", counts["rows_tier1"])
        logger.info("    triple_axis gold:      %d", counts["rows_tier2"])
        logger.info("    triple_axis silver:    %d", counts["rows_tier3"])
        logger.info("  by overture_match_provenance:")
        logger.info("    via_ppp_address only:  %d", counts["rows_via_ppp_only"])
        logger.info("    via_sam_address only:  %d", counts["rows_via_sam_only"])
        logger.info("    via BOTH paths:        %d", counts["rows_via_both"])
        logger.info("  rows with website:       %d", counts["rows_with_website"])
        logger.info("  distinct sam_uei:        %d", counts["distinct_sam_uei"])
        logger.info("  distinct place_id:       %d", counts["distinct_place_id"])

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
            logger.info(
                "DRY RUN OK — no Lance / Postgres writes.  duration=%.1fs",
                time.time() - t0,
            )
            return 0

        lance_count = _write_bridge_lance(con, storage_options)
        complete_bridge_run(
            run_uuid,
            metrics={
                "rows_base": len(base_arrow),
                "rows_path_a": len(path_a_arrow),
                "rows_path_b": len(path_b_arrow),
                "rows_matched": counts["rows_matched"],
                "rows_tier1": counts["rows_tier1"],
                "rows_tier2": counts["rows_tier2"],
                "rows_tier3": counts["rows_tier3"],
                "rows_via_ppp_only": counts["rows_via_ppp_only"],
                "rows_via_sam_only": counts["rows_via_sam_only"],
                "rows_via_both": counts["rows_via_both"],
                "rows_with_website": counts["rows_with_website"],
                "distinct_sam_uei": counts["distinct_sam_uei"],
                "distinct_place_id": counts["distinct_place_id"],
                "lance_rows": lance_count,
            },
        )
        logger.info(
            "OK — run_id=%s  duration=%.1fs", bridge_run_id, time.time() - t0
        )
        return 0

    except Exception as exc:
        logger.exception("bridge build FAILED: %s", exc)
        if run_uuid is not None:
            fail_bridge_run(run_uuid, str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

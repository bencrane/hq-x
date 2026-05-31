#!/usr/bin/env python3
"""Bridge generator: PPP × Overture × USPTO — three-axis identity triangulation.

Composes two confirmed-attribute bridges that converge on the same PPP-borrower
identity:

  base entity (left key): PPP borrower (legal_name_normalized, borrstate, borrzip)
  + attribute A:          bridges/ppp_overture_address_lance  (storefront identity)
  + attribute B:          bridges/ppp_uspto_owner_lance        (registered brand identity)

Every output row is a (PPP borrower ↔ Overture place_id ↔ USPTO serial_no)
triple where the same PPP-borrower paperwork-LLC is linked to both a physical
storefront (Overture) AND a registered trademark (USPTO). This is the
cross-validated identity layer — paperwork-LLC ↔ storefront ↔ registered brand.

Pattern: inner join across both attribute bridges on PPP borrower identity.
Unlike sam_ppp_overture_lance (which OR-unions two paths to the same Overture
attribute), this composite is an AND-conjunction of two distinct attribute
axes. Each attribute brings its own confidence tier; the triple_axis_tier
is the worst-of across both axes.

Tier carried per axis (sourced from each component bridge's tier column):
  - overture_attach_tier   (from ppp_overture_address.confidence_tier)
  - uspto_attach_tier      (from ppp_uspto_owner.confidence_tier)
  - triple_axis_tier       (worst-of overture_attach_tier and uspto_attach_tier)

Expected volume: ~12.2M rows (241,745 borrowers in both bridges × ~7.3 Overture
× ~6.9 USPTO avg fan-out).

Floor: ≥ 1,000,000 rows.

Output: `polaris-warehouse/bridges/ppp_overture_uspto_lance/`
Audit:  ops.bridge_generation_runs (bridge_name='ppp_overture_uspto')

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/build_bridge_ppp_overture_uspto_lance.py --apply

  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/build_bridge_ppp_overture_uspto_lance.py --dry-run
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
logger = logging.getLogger("build_bridge_ppp_overture_uspto_lance")

BRIDGE_NAME = "ppp_overture_uspto"
METHOD_NAME = "borrower_attribute_composite"
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "1.0.0"

SOURCE_LEFT = "ppp_overture_address_lance"
SOURCE_RIGHT = "ppp_uspto_owner_lance"

PATH_A_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/ppp_overture_address_lance"
PATH_B_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/ppp_uspto_owner_lance"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/ppp_overture_uspto_lance"
DATASET_SLUG = "ppp_overture_uspto_lance"

MIN_ROWS_MATCHED = 1_000_000
# Process-unique tmp to avoid DuckDB spill collisions when multiple bridge
# builders run concurrently against the same `/tmp/lance/` shared root.
TMP_DIR = f"/tmp/lance/ppp_overture_uspto_{os.getpid()}"


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
    """Load both attribute bridges as Arrow tables."""
    import lance

    logger.info("opening path A (PPP × Overture address): %s ...", PATH_A_URI)
    over_ds = lance.dataset(PATH_A_URI, storage_options=storage_options)
    over_cols = [
        "ppp_legal_name_normalized",
        "ppp_borrstate",
        "ppp_borrzip",
        "ppp_borrname_sample",
        "ppp_total_loans",
        "ppp_total_approval",
        "ppp_max_approval_date",
        "ppp_min_approval_date",
        "ppp_latest_loanstatus",
        "ppp_has_pending_commit",
        "ppp_franchise_brands_set",
        "ppp_naics_codes_set",
        "ppp_lender_set",
        "place_id",
        "overture_name_primary",
        "overture_brand_name_primary",
        "overture_brand_wikidata",
        "overture_address_freeform",
        "overture_address_locality",
        "overture_categories_primary",
        "overture_phone_primary",
        "overture_website_primary",
        "overture_email_primary",
        "overture_operating_status",
        "overture_confidence",
        "address_base_normalized",
        "confidence_tier",
    ]
    over_arrow = over_ds.scanner(columns=over_cols).to_table()
    rows_over = len(over_arrow)
    logger.info("  ppp_overture_address_lance: %d rows", rows_over)

    logger.info("opening path B (PPP × USPTO owner): %s ...", PATH_B_URI)
    uspto_ds = lance.dataset(PATH_B_URI, storage_options=storage_options)
    uspto_cols = [
        "ppp_legal_name_normalized",
        "ppp_borrstate",
        "ppp_borrzip",
        "uspto_serial_no",
        "uspto_own_name",
        "uspto_owner_name_normalized",
        "uspto_legal_name_normalized",
        "uspto_owner_kind_normalized",
        "uspto_own_entity_cd",
        "uspto_own_addr_1",
        "uspto_own_addr_city",
        "uspto_owner_zip5",
        "uspto_owner_country_normalized",
        "uspto_mark_id_char",
        "uspto_mark_text_normalized",
        "uspto_mark_draw_cd",
        "uspto_trade_mark_in",
        "uspto_serv_mark_in",
        "uspto_std_char_claim_in",
        "uspto_filing_dt",
        "uspto_publication_dt",
        "uspto_registration_dt",
        "uspto_registration_no",
        "uspto_cfh_status_cd",
        "uspto_cfh_status_dt",
        "uspto_case_file_year",
        "confidence_tier",
    ]
    uspto_arrow = uspto_ds.scanner(columns=uspto_cols).to_table()
    rows_uspto = len(uspto_arrow)
    logger.info("  ppp_uspto_owner_lance: %d rows", rows_uspto)

    return over_arrow, uspto_arrow, rows_over, rows_uspto


def _build_match_table(
    over_arrow,
    uspto_arrow,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
):
    """INNER JOIN both attribute bridges on PPP borrower identity + tier composition."""
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")

    con.register("over", over_arrow)
    con.register("uspto", uspto_arrow)

    logger.info(
        "  registered: over=%d  uspto=%d",
        con.execute("SELECT COUNT(*) FROM over").fetchone()[0],
        con.execute("SELECT COUNT(*) FROM uspto").fetchone()[0],
    )

    # Tier ordering helper for worst-of computation
    tier_ord_sql = (
        "CASE {col} "
        "WHEN 'platinum' THEN 1 "
        "WHEN 'gold' THEN 2 "
        "WHEN 'silver' THEN 3 "
        "ELSE 4 END"
    )

    logger.info(
        "INNER JOIN ppp_overture_address × ppp_uspto_owner on PPP borrower ..."
    )
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_match AS
        SELECT
            -- PPP identity (the convergent base entity)
            o.ppp_legal_name_normalized,
            o.ppp_borrstate,
            o.ppp_borrzip,
            o.ppp_borrname_sample,
            o.ppp_total_loans,
            o.ppp_total_approval,
            o.ppp_max_approval_date,
            o.ppp_min_approval_date,
            o.ppp_latest_loanstatus,
            o.ppp_has_pending_commit,
            o.ppp_franchise_brands_set,
            o.ppp_naics_codes_set,
            o.ppp_lender_set,
            -- Overture (storefront) attribute
            o.place_id,
            o.overture_name_primary,
            o.overture_brand_name_primary,
            o.overture_brand_wikidata,
            o.overture_address_freeform,
            o.overture_address_locality,
            o.overture_categories_primary,
            o.overture_phone_primary,
            o.overture_website_primary,
            o.overture_email_primary,
            o.overture_operating_status,
            o.overture_confidence,
            o.address_base_normalized                  AS overture_address_base_normalized,
            o.confidence_tier                          AS overture_attach_tier,
            -- USPTO (registered brand) attribute
            u.uspto_serial_no,
            u.uspto_own_name,
            u.uspto_owner_name_normalized,
            u.uspto_legal_name_normalized,
            u.uspto_owner_kind_normalized,
            u.uspto_own_entity_cd,
            u.uspto_own_addr_1,
            u.uspto_own_addr_city,
            u.uspto_owner_zip5,
            u.uspto_owner_country_normalized,
            u.uspto_mark_id_char,
            u.uspto_mark_text_normalized,
            u.uspto_mark_draw_cd,
            u.uspto_trade_mark_in,
            u.uspto_serv_mark_in,
            u.uspto_std_char_claim_in,
            u.uspto_filing_dt,
            u.uspto_publication_dt,
            u.uspto_registration_dt,
            u.uspto_registration_no,
            u.uspto_cfh_status_cd,
            u.uspto_cfh_status_dt,
            u.uspto_case_file_year,
            u.confidence_tier                          AS uspto_attach_tier,
            -- Triple-axis composite tier (worst-of across both attribute tiers)
            CASE
              WHEN GREATEST(
                     {tier_ord_sql.format(col='o.confidence_tier')},
                     {tier_ord_sql.format(col='u.confidence_tier')}
                   ) = 1 THEN 'platinum'
              WHEN GREATEST(
                     {tier_ord_sql.format(col='o.confidence_tier')},
                     {tier_ord_sql.format(col='u.confidence_tier')}
                   ) = 2 THEN 'gold'
              ELSE 'silver'
            END                                        AS triple_axis_tier,
            TIMESTAMP '{generated_at_iso}'             AS generated_at,
            '{BRIDGE_VERSION}'                         AS bridge_version,
            '{bridge_run_id}'                          AS bridge_run_id
        FROM over o
        JOIN uspto u
          ON  u.ppp_legal_name_normalized = o.ppp_legal_name_normalized
          AND u.ppp_borrstate              = o.ppp_borrstate
          AND u.ppp_borrzip                = o.ppp_borrzip
        """
    )

    row_counts = con.execute(
        """
        SELECT
          COUNT(*),
          COUNT(*) FILTER (WHERE triple_axis_tier='platinum'),
          COUNT(*) FILTER (WHERE triple_axis_tier='gold'),
          COUNT(*) FILTER (WHERE triple_axis_tier='silver'),
          COUNT(DISTINCT (ppp_legal_name_normalized, ppp_borrstate, ppp_borrzip)),
          COUNT(DISTINCT place_id),
          COUNT(DISTINCT uspto_serial_no),
          COUNT(*) FILTER (WHERE overture_attach_tier='platinum' AND uspto_attach_tier='platinum'),
          COUNT(*) FILTER (WHERE uspto_cfh_status_cd LIKE '7%')
        FROM bridge_match
        """
    ).fetchone()

    counts = {
        "rows_matched": row_counts[0],
        "rows_tier1": row_counts[1],
        "rows_tier2": row_counts[2],
        "rows_tier3": row_counts[3],
        "distinct_borrowers": row_counts[4],
        "distinct_places": row_counts[5],
        "distinct_serials": row_counts[6],
        "double_platinum": row_counts[7],
        "rows_active_mark": row_counts[8],
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
        for col in (
            "ppp_legal_name_normalized",
            "place_id",
            "uspto_serial_no",
            "uspto_mark_text_normalized",
            "triple_axis_tier",
        ):
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
            "Inner-join composite across two attribute bridges sharing the "
            "same base entity. Worst-of confidence tier across both axes. "
            "Used for triple-axis identity triangulation when a base entity "
            "(e.g. PPP borrower) has two confirmed-attribute attachments "
            "(e.g. Overture storefront + USPTO trademark)."
        ),
    )
    register_match_method_version(
        method_name=METHOD_NAME,
        semver=METHOD_SEMVER,
        normalizer_module="_lib/entity_name_normalize.py,_lib/address_normalize.py",
        normalizer_version=f"name={NAME_NORMALIZER_VERSION};addr={ADDR_NORMALIZER_VERSION}",
        blacklist_module="(inherited from component bridges)",
        blacklist_version="n/a",
        tier_rule_description=(
            "triple_axis_tier = worst-of(overture_attach_tier, uspto_attach_tier). "
            "Inputs are component-bridge confidence_tier values; no further "
            "fan-out tiering at the composite layer (fan-out is already tiered "
            "in the component bridges)."
        ),
        rejection_rule_description=(
            "rejected rows in component bridges are pre-filtered upstream; "
            "this composite assumes both inputs carry only platinum/gold/silver."
        ),
        input_columns_left=[
            "ppp_legal_name_normalized",
            "ppp_borrstate",
            "ppp_borrzip",
            "confidence_tier",
        ],
        input_columns_right=[
            "ppp_legal_name_normalized",
            "ppp_borrstate",
            "ppp_borrzip",
            "confidence_tier",
        ],
        output_value_description=(
            "(ppp_borrower_identity, place_id, uspto_serial_no) triple — "
            "paperwork-LLC ↔ storefront ↔ registered brand"
        ),
    )
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "PPP × Overture × USPTO triple-axis identity triangulation. "
            "Inner-joins ppp_overture_address (storefront attribute) and "
            "ppp_uspto_owner (registered-brand attribute) on the same PPP "
            "borrower identity (legal_name_normalized + borrstate + borrzip). "
            "Each output row is a cross-validated (paperwork-LLC ↔ storefront "
            "↔ trademark) identity chain — the gold standard for SMB attribution "
            "where the legal-LLC name on PPP paperwork tells you nothing about "
            "what the business actually does."
        ),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true", help="write Lance + ledger row")
    grp.add_argument("--dry-run", action="store_true", help="count only, no writes")
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
        "inputs: ppp_overture_address_lance ⨝ ppp_uspto_owner_lance on PPP borrower"
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
            source_left=SOURCE_LEFT,
            source_right=SOURCE_RIGHT,
            match_method=METHOD_NAME,
            r2_output_key=BRIDGE_LANCE_URI,
        )
        bridge_run_id = str(run_uuid)
        logger.info("bridge_run_id=%s", bridge_run_id)

    try:
        over_arrow, uspto_arrow, rows_left, rows_right = _materialize_inputs(
            storage_options
        )
        con, counts = _build_match_table(
            over_arrow,
            uspto_arrow,
            bridge_run_id=bridge_run_id,
            generated_at_iso=started_at.isoformat(),
        )

        logger.info("-" * 60)
        logger.info("triple-axis composite results:")
        logger.info("  rows_matched:            %d", counts["rows_matched"])
        logger.info("    platinum (worst-of):    %d", counts["rows_tier1"])
        logger.info("    gold     (worst-of):    %d", counts["rows_tier2"])
        logger.info("    silver   (worst-of):    %d", counts["rows_tier3"])
        logger.info("  distinct PPP borrowers:   %d", counts["distinct_borrowers"])
        logger.info("  distinct Overture places: %d", counts["distinct_places"])
        logger.info("  distinct USPTO serials:   %d", counts["distinct_serials"])
        logger.info("  double-platinum rows:     %d", counts["double_platinum"])
        logger.info("  active-mark (status 7xx): %d", counts["rows_active_mark"])

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
                "rows_left": rows_left,
                "rows_right": rows_right,
                "rows_matched": counts["rows_matched"],
                "rows_tier1": counts["rows_tier1"],
                "rows_tier2": counts["rows_tier2"],
                "rows_tier3": counts["rows_tier3"],
                "rows_collision_rejected": 0,  # n/a at composite layer
                "lance_rows": lance_count,
            },
        )
        logger.info(
            "OK — run_id=%s  duration=%.1fs", bridge_run_id, time.time() - t0
        )
        logger.info("     output: %s", BRIDGE_LANCE_URI)
        return 0

    except Exception as exc:
        logger.exception("bridge build FAILED: %s", exc)
        if run_uuid is not None:
            fail_bridge_run(run_uuid, str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

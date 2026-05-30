#!/usr/bin/env python3
"""Bridge generator: SBA 7(a)+504 borrowers × Overture US Places — address-keyed.

Mirror of `build_bridge_ppp_overture_address_lance.py`. Joins SBA loan paperwork
directly to Overture storefronts on a pure address axis. Both sides arrive with
the join token already baked in:

  - SBA:      `borrstreet_normalized` (baked 2026-05-27 in
              `emit_sba_borrowers_lance.py` v1.1.0 via
              `_lib.address_normalize.normalize_address_street` base form)
  - Overture: `address_base_normalized` (96.4% coverage, same normalizer baked
              at emit time in `emit_overture_us_places_lance.py`)

No Python normalize pass at build time — both sides come pre-baked from
their respective emits.

Inputs:
  SBA:      `polaris-warehouse/sba/borrowers_lance`              (~12.0M rows)
  Overture: `polaris-warehouse/overture/us_places_lance`         (~15.95M rows)

Join key (composite, exact equality):
  (address_base_normalized, 2-letter US state, 5-digit zip)

Fan-out tiering:
  platinum = 1:1 on (sba_identity, place_id)
  gold     = 1:N or N:1
  silver   = N:M (fan-out ≤ 50 on each side)
  rejected = any fan-out > 50

Where `sba_identity` is the composite (legal_name_normalized, borrstate,
borrzip) — the natural key of `sba/borrowers_lance`.

Output: `polaris-warehouse/bridges/sba_overture_address_lance/`
Audit:  ops.bridge_generation_runs (bridge_name='sba_overture_address')
Floor:  ≥ 500,000 rows.

Why this bridge:
  The existing `sba_overture_places_lance` joins on (legal_name, state, zip5)
  only. That ceilings out at borrowers whose registered SBA legal name matches
  the Overture place name token. Operating businesses routinely diverge —
  "WILLIAMSBURG HOSPITALITY LLC" on the 7(a) paperwork vs "Holiday Inn Express"
  on the storefront. The address axis bypasses that ceiling for the ~12M-row
  SBA universe just as `ppp_overture_address_lance` did for the PPP universe.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/build_bridge_sba_overture_address_lance.py --apply

  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/build_bridge_sba_overture_address_lance.py --dry-run
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
logger = logging.getLogger("build_bridge_sba_overture_address_lance")

BRIDGE_NAME = "sba_overture_address"
METHOD_NAME = "address_base_state_zip_exact"
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "1.0.0"

SOURCE_LEFT = "sba_borrowers_lance"
SOURCE_RIGHT = "overture_us_places_lance"

SBA_BORROWERS_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sba/borrowers_lance"
OVERTURE_PLACES_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/overture/us_places_lance"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_overture_address_lance"
DATASET_SLUG = "sba_overture_address_lance"

COLLISION_THRESHOLD = 50
MIN_ROWS_MATCHED = 500_000
# Process-unique tmp to avoid DuckDB spill collisions when multiple bridge
# builders run concurrently against the same `/tmp/lance/` shared root.
TMP_DIR = f"/tmp/lance/sba_overture_address_{os.getpid()}"


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
    """Load SBA + Overture Arrow tables; both already pre-normalized."""
    import lance
    import pyarrow.compute as pc

    # ---- SBA side ----
    logger.info("opening sba/borrowers_lance ...")
    sba_ds = lance.dataset(SBA_BORROWERS_LANCE_URI, storage_options=storage_options)
    sba_filter = (
        pc.field("borrstreet_normalized").is_valid()
        & pc.field("borrstate").is_valid()
        & pc.field("borrzip").is_valid()
        & pc.field("legal_name_normalized").is_valid()
    )
    sba_cols = [
        "legal_name_normalized",
        "borrname_sample",
        "borrstate",
        "borrzip",
        "borrstreet_normalized",
        "total_loans",
        "total_gross_approval",
        "max_approval_date",
        "min_approval_date",
        "latest_loanstatus",
        "has_pending_commit",
        "franchise_brands_set",
        "naics_codes_set",
        "lender_set",
    ]
    sba_arrow = sba_ds.scanner(columns=sba_cols, filter=sba_filter).to_table()
    rows_sba = len(sba_arrow)
    logger.info("  sba borrowers_lance (post-filter): %d rows", rows_sba)

    # ---- Overture side ----
    logger.info("opening overture/us_places_lance ...")
    overture_ds = lance.dataset(OVERTURE_PLACES_LANCE_URI, storage_options=storage_options)
    overture_filter = (
        pc.field("address_base_normalized").is_valid()
        & pc.field("address_postcode_5").is_valid()
        & pc.field("address_region").is_valid()
    )
    overture_cols = [
        "place_id",
        "name_primary",
        "address_freeform",
        "address_base_normalized",
        "address_locality",
        "address_postcode_5",
        "address_region",
        "categories_primary",
        "phone_primary",
        "website_primary",
        "email_primary",
        "brand_wikidata",
        "brand_name_primary",
        "operating_status",
        "confidence",
    ]
    overture_arrow = overture_ds.scanner(
        columns=overture_cols, filter=overture_filter
    ).to_table()
    rows_overture = len(overture_arrow)
    logger.info("  overture us_places_lance (post-filter): %d rows", rows_overture)

    return sba_arrow, overture_arrow, rows_sba, rows_overture


def _build_match_table(sba_arrow, overture_arrow, *, bridge_run_id: str, generated_at_iso: str):
    """JOIN on (address_base_normalized, state, zip5) + fan-out tiering.

    Both sides are pre-normalized — no UDF calls during the JOIN. The SBA
    `borrzip` is the raw VARCHAR (ZIP+4 or 5-digit + `.0` cast residue
    possible). We derive a clean 5-digit zip in SQL via the same pattern
    used in `emit_sba_borrowers_lance.py` side-scan.
    """
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")

    con.register("sba", sba_arrow)
    con.register("overture", overture_arrow)

    logger.info(
        "  registered: sba=%d  overture=%d",
        con.execute("SELECT COUNT(*) FROM sba").fetchone()[0],
        con.execute("SELECT COUNT(*) FROM overture").fetchone()[0],
    )

    # Zip5 normalization mirroring emit_sba_borrowers_lance.py side-scan
    zip5_expr = (
        "LPAD(SUBSTR(REGEXP_REPLACE(TRIM(CAST({col} AS VARCHAR)), "
        "'(\\.0+|-\\d+).*$', ''), 1, 5), 5, '0')"
    )

    con.execute(
        f"""
        CREATE TEMP TABLE matched AS
        SELECT
            s.borrstreet_normalized                           AS address_base_normalized,
            UPPER(TRIM(s.borrstate))                          AS match_state,
            {zip5_expr.format(col='s.borrzip')}               AS match_zip5,
            s.legal_name_normalized                           AS sba_legal_name_normalized,
            s.borrname_sample                                 AS sba_borrname_sample,
            s.borrstate                                       AS sba_borrstate,
            s.borrzip                                         AS sba_borrzip,
            s.total_loans                                     AS sba_total_loans,
            s.total_gross_approval                            AS sba_total_gross_approval,
            s.max_approval_date                               AS sba_max_approval_date,
            s.min_approval_date                               AS sba_min_approval_date,
            s.latest_loanstatus                               AS sba_latest_loanstatus,
            s.has_pending_commit                              AS sba_has_pending_commit,
            s.franchise_brands_set                            AS sba_franchise_brands_set,
            s.naics_codes_set                                 AS sba_naics_codes_set,
            s.lender_set                                      AS sba_lender_set,
            o.place_id,
            o.name_primary                                    AS overture_name_primary,
            o.brand_name_primary                              AS overture_brand_name_primary,
            o.brand_wikidata                                  AS overture_brand_wikidata,
            o.address_freeform                                AS overture_address_freeform,
            o.address_locality                                AS overture_address_locality,
            o.categories_primary                              AS overture_categories_primary,
            o.phone_primary                                   AS overture_phone_primary,
            o.website_primary                                 AS overture_website_primary,
            o.email_primary                                   AS overture_email_primary,
            o.operating_status                                AS overture_operating_status,
            o.confidence                                      AS overture_confidence,
            'address_base'                                    AS match_path
        FROM sba s
        JOIN overture o
          ON s.borrstreet_normalized      = o.address_base_normalized
         AND UPPER(TRIM(s.borrstate))      = o.address_region
         AND {zip5_expr.format(col='s.borrzip')} = o.address_postcode_5
        """
    )
    rows_matched = con.execute("SELECT COUNT(*) FROM matched").fetchone()[0]
    logger.info("  matched (pre-tier): %d rows", rows_matched)

    # Fan-out tiering: SBA identity = (legal_name, state, zip), Overture identity = place_id
    con.execute(
        """
        CREATE TEMP TABLE sba_fanout AS
        SELECT sba_legal_name_normalized AS k1,
               sba_borrstate              AS k2,
               sba_borrzip                AS k3,
               COUNT(*)                   AS sba_fan_out
        FROM matched
        GROUP BY 1, 2, 3
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE overture_fanout AS
        SELECT place_id, COUNT(*) AS overture_fan_out
        FROM matched
        GROUP BY place_id
        """
    )
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            m.*,
            sf.sba_fan_out,
            of_.overture_fan_out,
            CASE
                WHEN sf.sba_fan_out > {COLLISION_THRESHOLD}
                  OR of_.overture_fan_out > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN sf.sba_fan_out = 1 AND of_.overture_fan_out = 1
                    THEN 'platinum'
                WHEN sf.sba_fan_out = 1 OR  of_.overture_fan_out = 1
                    THEN 'gold'
                ELSE 'silver'
            END                              AS confidence_tier,
            TIMESTAMP '{generated_at_iso}'   AS generated_at,
            '{BRIDGE_VERSION}'               AS bridge_version,
            '{bridge_run_id}'                AS bridge_run_id
        FROM matched m
        JOIN sba_fanout sf
          ON sf.k1 = m.sba_legal_name_normalized
         AND sf.k2 = m.sba_borrstate
         AND sf.k3 = m.sba_borrzip
        JOIN overture_fanout of_ ON of_.place_id = m.place_id
        """
    )
    con.execute(
        "CREATE TEMP TABLE bridge_match AS "
        "SELECT * FROM bridge_all WHERE confidence_tier <> 'rejected'"
    )

    row_counts = con.execute(
        """
        SELECT
          COUNT(*),
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
        logger.info(
            "wrote %d rows in %.1fs (version=%s)", lance_count, write_dur, ds.version
        )

        os.environ["LANCE_BYPASS_SPILLING"] = "true"
        for col in ("sba_legal_name_normalized", "place_id", "address_base_normalized"):
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
            "Exact-equality JOIN on (address_base_normalized, 2-letter US state, "
            "5-digit zip). Applies _lib/address_normalize.py "
            f"v{ADDR_NORMALIZER_VERSION} (base form: unit-stripped). Both sides "
            "are pre-normalized at emit time — no UDF calls during this JOIN."
        ),
    )
    register_match_method_version(
        method_name=METHOD_NAME,
        semver=METHOD_SEMVER,
        normalizer_module="_lib/address_normalize.py",
        normalizer_version=ADDR_NORMALIZER_VERSION,
        blacklist_module="_lib/address_normalize.py",
        blacklist_version=ADDR_NORMALIZER_VERSION,
        tier_rule_description=(
            "platinum=1:1; gold=1:N or N:1; silver=N:M ≤50; rejected=>50"
        ),
        rejection_rule_description="fan-out >50 on either side → rejected",
        input_columns_left=[
            "borrstreet_normalized",
            "borrstate",
            "borrzip",
        ],
        input_columns_right=[
            "address_base_normalized",
            "address_postcode_5",
            "address_region",
        ],
        output_value_description=(
            "normalized USPS-abbrev street + 2-letter state + 5-digit zip join key"
        ),
    )
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "SBA 7(a)+504 borrowers × Overture US Places — address-keyed exact "
            "match. Bypasses the legal-vs-public-name ceiling that the existing "
            "sba_overture_places (name-axis) bridge hits when SBA paperwork "
            "carries a holding-LLC name and the Overture storefront carries the "
            "operating brand. Sibling of ppp_overture_address_lance for the "
            "~12M-row 7(a)+504 borrower universe."
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
        "bridge: %s  method=%s v%s  normalizer=v%s",
        BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER, ADDR_NORMALIZER_VERSION,
    )
    logger.info(
        "inputs: sba/borrowers_lance + overture/us_places_lance (Arrow-bridge, pre-baked)"
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
        sba_arrow, overture_arrow, rows_left, rows_right = _materialize_inputs(
            storage_options
        )
        con, counts = _build_match_table(
            sba_arrow, overture_arrow,
            bridge_run_id=bridge_run_id,
            generated_at_iso=started_at.isoformat(),
        )

        logger.info("-" * 60)
        logger.info("bridge tier distribution:")
        logger.info("  rows_matched:            %d", counts["rows_matched"])
        logger.info("    platinum (1:1):         %d", counts["rows_tier1"])
        logger.info("    gold     (1:N | N:1):   %d", counts["rows_tier2"])
        logger.info(
            "    silver   (N:M ≤%d):     %d", COLLISION_THRESHOLD, counts["rows_tier3"]
        )
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
                "rows_collision_rejected": counts["rows_collision_rejected"],
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

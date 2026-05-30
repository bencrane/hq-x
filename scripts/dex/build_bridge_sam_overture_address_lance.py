#!/usr/bin/env python3
"""Bridge generator: SAM-registered entity × Overture US Place — address-keyed.

The legal-name bridges (`sba_overture_places_lance`, `sba_franchise_overture_lance`)
hit a structural ceiling: SBA stores the LEGAL entity name ("JCM Investments
LLC"), Overture stores the OPERATING name ("McDonald's"). Franchisees,
holding-companies, DBAs — every entity whose legal name differs from its
storefront name — is unreachable through name-keyed bridges.

This bridge bypasses that ceiling entirely by joining on PHYSICAL ADDRESS.
SAM is the federal entity registry and carries a clean USPS-shape physical
address per registered entity (`physical_address_line_1` + `_line_2`).
Overture carries `address_freeform` for every place. Both sides go through
`scripts._lib.address_normalize.normalize_address_street` (unit-stripped
`base` form) before the join.

Inputs:
  SAM:      `polaris-warehouse/sam_gov/entities_lance`         (~884K rows)
  Overture: `polaris-warehouse/overture/us_places_lance`       (~15.95M rows)

Join key (composite, exact-equality after normalization):
  (address_base_normalized, zip5, 2-letter_state)

Fan-out tiering:
  platinum = 1:1
  gold     = 1:N or N:1
  silver   = N:M (fan-out ≤ 50 on each side)
  rejected = any fan-out > 50

Output: `polaris-warehouse/bridges/sam_overture_address_lance/`
Audit:  ops.bridge_generation_runs (bridge_name='sam_overture_address')
Floor:  ≥ 50,000 rows (sanity — refine post first dry run).

Arrow-bridge pattern (NOT lance-duckdb extension).
Pre-normalize + pre-dedup BOTH sides in Python BEFORE the DuckDB join.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/build_bridge_sam_overture_address_lance.py --apply

  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/build_bridge_sam_overture_address_lance.py --dry-run
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
    normalize_address_street,
    join_sam_line_1_2,
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
logger = logging.getLogger("build_bridge_sam_overture_address_lance")

BRIDGE_NAME = "sam_overture_address"
METHOD_NAME = "address_base_state_zip_exact"
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "1.0.0"

SOURCE_LEFT = "sam_gov_entities_lance"
SOURCE_RIGHT = "overture_us_places_lance"

SAM_ENTITIES_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/entities_lance"
OVERTURE_PLACES_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/overture/us_places_lance"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sam_overture_address_lance"
DATASET_SLUG = "sam_overture_address_lance"

COLLISION_THRESHOLD = 50
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
    """Load SAM entities + Overture US Places; normalize address; pre-dedup."""
    import lance
    import pyarrow as pa
    import pyarrow.compute as pc

    # ---- SAM side ----
    logger.info("opening sam_gov/entities_lance ...")
    sam_ds = lance.dataset(SAM_ENTITIES_LANCE_URI, storage_options=storage_options)
    sam_filter = (
        pc.field("physical_address_line_1").is_valid()
        & pc.field("physical_address_zip5").is_valid()
        & pc.field("physical_address_state_normalized").is_valid()
        & pc.field("unique_entity_id").is_valid()
    )
    sam_cols = [
        "unique_entity_id",
        "legal_business_name",
        "dba_name",
        "entity_url",
        "cage_code",
        "primary_naics",
        "naics_code_string",
        "bus_type_string",
        "physical_address_line_1",
        "physical_address_line_2",
        "physical_address_city",
        "physical_address_zip5",
        "physical_address_state_normalized",
        "entity_structure",
        "registration_expiration_date",
        "last_update_date",
    ]
    sam_raw = sam_ds.scanner(columns=sam_cols, filter=sam_filter).to_table()
    rows_sam_raw = len(sam_raw)
    logger.info("  sam_gov entities_lance (post-filter): %d rows", rows_sam_raw)

    # Build address_base_normalized on SAM side. One row per (UEI, state, zip5,
    # base address) — the same UEI MAY appear under multiple address variants
    # across SAM snapshots; the bridge keeps each variant as its own join token.
    l1 = sam_raw.column("physical_address_line_1").to_pylist()
    l2 = sam_raw.column("physical_address_line_2").to_pylist()
    state = sam_raw.column("physical_address_state_normalized").to_pylist()
    zip5 = sam_raw.column("physical_address_zip5").to_pylist()
    uei = sam_raw.column("unique_entity_id").to_pylist()
    legal = sam_raw.column("legal_business_name").to_pylist()
    dba = sam_raw.column("dba_name").to_pylist()
    url = sam_raw.column("entity_url").to_pylist()
    cage = sam_raw.column("cage_code").to_pylist()
    naics = sam_raw.column("primary_naics").to_pylist()
    naics_s = sam_raw.column("naics_code_string").to_pylist()
    bus_type = sam_raw.column("bus_type_string").to_pylist()
    city = sam_raw.column("physical_address_city").to_pylist()
    struct = sam_raw.column("entity_structure").to_pylist()
    reg_exp = sam_raw.column("registration_expiration_date").to_pylist()
    last_upd = sam_raw.column("last_update_date").to_pylist()

    sam_rows: list[dict] = []
    seen: set = set()
    for i in range(rows_sam_raw):
        st = (state[i] or "").strip().upper()
        if len(st) != 2:
            continue
        z = (zip5[i] or "").strip()
        if len(z) != 5 or not z.isdigit():
            continue
        joined = join_sam_line_1_2(l1[i], l2[i])
        base = normalize_address_street(joined)
        if not base:
            continue
        key = (uei[i], st, z, base)
        if key in seen:
            continue
        seen.add(key)
        sam_rows.append(
            {
                "sam_uei": uei[i],
                "sam_state": st,
                "sam_zip5": z,
                "sam_address_base_normalized": base,
                "sam_legal_business_name": legal[i],
                "sam_dba_name": dba[i],
                "sam_entity_url": url[i],
                "sam_cage_code": cage[i],
                "sam_primary_naics": naics[i],
                "sam_naics_code_string": naics_s[i],
                "sam_bus_type_string": bus_type[i],
                "sam_physical_address_line_1": l1[i],
                "sam_physical_address_line_2": l2[i],
                "sam_physical_address_city": city[i],
                "sam_entity_structure": struct[i],
                "sam_registration_expiration_date": reg_exp[i],
                "sam_last_update_date": last_upd[i],
            }
        )
    del sam_raw, l1, l2, state, zip5, uei, legal, dba, url, cage, naics, naics_s, bus_type, city, struct, reg_exp, last_upd, seen

    logger.info(
        "  sam normalized + deduped: %d distinct (uei, state, zip5, base_address)",
        len(sam_rows),
    )

    sam_arrow = pa.table(
        {k: [r.get(k) for r in sam_rows] for k in sam_rows[0].keys()}
    )
    del sam_rows
    rows_sam = len(sam_arrow)

    # ---- Overture side ----
    logger.info("opening overture/us_places_lance ...")
    overture_ds = lance.dataset(OVERTURE_PLACES_LANCE_URI, storage_options=storage_options)
    overture_filter = (
        pc.field("address_freeform").is_valid()
        & pc.field("address_postcode_5").is_valid()
        & pc.field("address_region").is_valid()
    )
    overture_cols = [
        "place_id",
        "name_primary",
        "name_normalized",
        "address_freeform",
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
    overture_raw = overture_ds.scanner(columns=overture_cols, filter=overture_filter).to_table()
    rows_overture_raw = len(overture_raw)
    logger.info("  overture us_places_lance (post-filter): %d rows", rows_overture_raw)

    # Normalize address on Overture side, keeping every place_id (no dedup —
    # each place is its own identity even at the same street address).
    free = overture_raw.column("address_freeform").to_pylist()
    region = overture_raw.column("address_region").to_pylist()
    zip5_ovt = overture_raw.column("address_postcode_5").to_pylist()

    base_ovt = [normalize_address_street(s) for s in free]
    region_up = [(r or "").strip().upper() for r in region]
    zip5_norm = [(z or "").strip() for z in zip5_ovt]

    # Single mask drives BOTH the row filter and the new-column arrays so
    # lengths stay in lockstep.
    keep_flags = [
        (b is not None) and (len(r) == 2) and (len(z) == 5) and z.isdigit()
        for b, r, z in zip(base_ovt, region_up, zip5_norm)
    ]
    keep_mask = pa.array(keep_flags)
    overture_filtered = overture_raw.filter(keep_mask)
    base_ovt_arr = pa.array(
        [b for b, k in zip(base_ovt, keep_flags) if k], type=pa.string()
    )
    region_up_arr = pa.array(
        [r for r, k in zip(region_up, keep_flags) if k], type=pa.string()
    )

    overture_arrow = overture_filtered.append_column(
        "address_base_normalized", base_ovt_arr
    )
    overture_arrow = overture_arrow.append_column(
        "address_region_normalized", region_up_arr
    )
    rows_overture = len(overture_arrow)
    logger.info("  overture normalized + valid-keyed: %d rows", rows_overture)
    del overture_raw, free, base_ovt, region, region_up, zip5_ovt, zip5_norm, base_ovt_arr, region_up_arr, keep_flags

    return sam_arrow, overture_arrow, rows_sam, rows_overture


def _build_match_table(sam_arrow, overture_arrow, *, bridge_run_id: str, generated_at_iso: str):
    """JOIN on (address_base_normalized, state, zip5) + fan-out tiering."""
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")

    con.register("sam", sam_arrow)
    con.register("overture", overture_arrow)

    logger.info("  registered: sam=%d  overture=%d",
                con.execute("SELECT COUNT(*) FROM sam").fetchone()[0],
                con.execute("SELECT COUNT(*) FROM overture").fetchone()[0])

    con.execute(
        """
        CREATE TEMP TABLE matched AS
        SELECT
            s.sam_address_base_normalized           AS address_base_normalized,
            s.sam_state                             AS match_state,
            s.sam_zip5                              AS match_zip5,
            s.sam_uei,
            s.sam_legal_business_name,
            s.sam_dba_name,
            s.sam_entity_url,
            s.sam_cage_code,
            s.sam_primary_naics,
            s.sam_naics_code_string,
            s.sam_bus_type_string,
            s.sam_physical_address_line_1,
            s.sam_physical_address_line_2,
            s.sam_physical_address_city,
            s.sam_entity_structure,
            s.sam_registration_expiration_date,
            s.sam_last_update_date,
            o.place_id,
            o.name_primary                          AS overture_name_primary,
            o.brand_name_primary                    AS overture_brand_name_primary,
            o.brand_wikidata                        AS overture_brand_wikidata,
            o.address_freeform                      AS overture_address_freeform,
            o.address_locality                      AS overture_address_locality,
            o.categories_primary                    AS overture_categories_primary,
            o.phone_primary                         AS overture_phone_primary,
            o.website_primary                       AS overture_website_primary,
            o.email_primary                         AS overture_email_primary,
            o.operating_status                      AS overture_operating_status,
            o.confidence                            AS overture_confidence,
            'address_base'                          AS match_path
        FROM sam s
        JOIN overture o
          ON s.sam_address_base_normalized = o.address_base_normalized
         AND s.sam_state                   = o.address_region_normalized
         AND s.sam_zip5                    = o.address_postcode_5
        """
    )
    rows_matched = con.execute("SELECT COUNT(*) FROM matched").fetchone()[0]
    logger.info("  matched (pre-tier): %d rows", rows_matched)

    con.execute(
        """
        CREATE TEMP TABLE sam_fanout AS
        SELECT sam_uei, COUNT(*) AS sam_fan_out
        FROM matched
        GROUP BY sam_uei
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
            sf.sam_fan_out,
            of_.overture_fan_out,
            CASE
                WHEN sf.sam_fan_out > {COLLISION_THRESHOLD}
                  OR of_.overture_fan_out > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN sf.sam_fan_out = 1 AND of_.overture_fan_out = 1
                    THEN 'platinum'
                WHEN sf.sam_fan_out = 1 OR  of_.overture_fan_out = 1
                    THEN 'gold'
                ELSE 'silver'
            END                              AS confidence_tier,
            TIMESTAMP '{generated_at_iso}'   AS generated_at,
            '{BRIDGE_VERSION}'               AS bridge_version,
            '{bridge_run_id}'                AS bridge_run_id
        FROM matched m
        JOIN sam_fanout sf ON sf.sam_uei = m.sam_uei
        JOIN overture_fanout of_ ON of_.place_id = m.place_id
        """
    )
    con.execute(
        "CREATE TEMP TABLE bridge_match AS SELECT * FROM bridge_all WHERE confidence_tier <> 'rejected'"
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
        logger.info("wrote %d rows in %.1fs (version=%s)", lance_count, write_dur, ds.version)

        os.environ["LANCE_BYPASS_SPILLING"] = "true"
        for col in ("sam_uei", "place_id", "address_base_normalized"):
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
            f"v{ADDR_NORMALIZER_VERSION} (base form: unit-stripped) on both "
            "sides. SAM line_1+line_2 concatenated before normalization; "
            "Overture address_freeform normalized in-place."
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
            "physical_address_line_1",
            "physical_address_line_2",
            "physical_address_zip5",
            "physical_address_state_normalized",
        ],
        input_columns_right=[
            "address_freeform",
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
            "SAM-registered entities × Overture US Places — address-keyed exact "
            "match. Bypasses legal-vs-public-name divergence by joining on "
            "physical street address. Single-path (match_path='address_base'). "
            "Resolves franchisees, holding-company storefronts, and DBA "
            "operating locations that name-based bridges miss."
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
    logger.info("inputs: sam_gov/entities_lance + overture/us_places_lance (Arrow-bridge)")
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
        sam_arrow, overture_arrow, rows_left, rows_right = _materialize_inputs(storage_options)
        con, counts = _build_match_table(
            sam_arrow, overture_arrow,
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
            logger.info("DRY RUN OK — no Lance / Postgres writes.  duration=%.1fs",
                        time.time() - t0)
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
        logger.info("     output: %s", BRIDGE_LANCE_URI)
        return 0

    except Exception as exc:
        logger.exception("bridge build FAILED: %s", exc)
        if run_uuid is not None:
            fail_bridge_run(run_uuid, str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

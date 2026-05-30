#!/usr/bin/env python3
"""Bridge generator: FL SoS-registered entity × Overture US Place — address-keyed.

Mirrors sos_ca_overture_address (sibling) but anchored on the Florida Division
of Corporations entity registry. Connects every active FL-registered business
to a verified Overture place + website via exact-equality address join.

Inputs:
  FL SoS:    `polaris-warehouse/sos/fl_entities_lance`              (~12.6M rows)
  Overture:  `polaris-warehouse/overture/us_places_lance`           (~15.95M rows)

Filter on FL SoS side: `status = 'A'` (Active; ~3.93M of 12.6M rows).
Address: `address1`+`address2` concatenated (multi-line), with
`mail_address1`+`mail_address2` fallback. Normalized to unit-stripped base
form via `scripts._lib.address_normalize.normalize_address_street`.

Join key (composite, exact-equality after normalization):
  (address_base_normalized, zip5, 2-letter_state)

Fan-out tiering (same convention as sam_overture_address / sos_ca_overture_address):
  platinum = 1:1
  gold     = 1:N or N:1
  silver   = N:M (fan-out ≤ 50 on each side)
  rejected = any fan-out > 50

Output: `polaris-warehouse/bridges/sos_fl_overture_address_lance/`
Audit:  ops.bridge_generation_runs (bridge_name='sos_fl_overture_address')
Method: REUSES `address_base_state_zip_exact` v1.0.0.
Floor:  ≥ 75,000 matched rows.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python apps/data-engine-x/scripts/build_bridge_sos_fl_overture_address_lance.py --apply

  doppler run --project hq-all --config prd -- \\
    uv run python apps/data-engine-x/scripts/build_bridge_sos_fl_overture_address_lance.py --dry-run
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
logger = logging.getLogger("build_bridge_sos_fl_overture_address_lance")

BRIDGE_NAME = "sos_fl_overture_address"
METHOD_NAME = "address_base_state_zip_exact"
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "1.0.0"

SOURCE_LEFT = "sos_fl_entities_lance"
SOURCE_RIGHT = "overture_us_places_lance"

SOS_FL_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sos/fl_entities_lance"
OVERTURE_PLACES_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/overture/us_places_lance"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sos_fl_overture_address_lance"
DATASET_SLUG = "sos_fl_overture_address_lance"

COLLISION_THRESHOLD = 50
MIN_ROWS_MATCHED = 75_000
TMP_DIR = "/tmp/lance"

_FULL_STATE_TO_ABBR = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
    "CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE",
    "DISTRICT OF COLUMBIA": "DC", "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI",
    "IDAHO": "ID", "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA",
    "KANSAS": "KS", "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME",
    "MARYLAND": "MD", "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN",
    "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE",
    "NEVADA": "NV", "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM",
    "NEW YORK": "NY", "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH",
    "OKLAHOMA": "OK", "OREGON": "OR", "PENNSYLVANIA": "PA", "PUERTO RICO": "PR",
    "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD",
    "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT", "VERMONT": "VT",
    "VIRGINIA": "VA", "WASHINGTON": "WA", "WEST VIRGINIA": "WV",
    "WISCONSIN": "WI", "WYOMING": "WY", "GUAM": "GU", "AMERICAN SAMOA": "AS",
    "VIRGIN ISLANDS": "VI", "NORTHERN MARIANA ISLANDS": "MP",
}


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
    """Load FL SoS Active entities + Overture US Places; normalize; pre-dedup."""
    import lance
    import pyarrow as pa
    import pyarrow.compute as pc

    logger.info("opening sos/fl_entities_lance ...")
    sos_ds = lance.dataset(SOS_FL_LANCE_URI, storage_options=storage_options)
    sos_filter = (
        pc.field("entity_num").is_valid()
        & pc.equal(pc.field("status"), "A")
    )
    sos_cols = [
        "entity_num", "entity_name", "entity_name_normalized",
        "status", "filing_type", "file_date",
        "address1", "address2", "city", "state", "zip",
        "mail_address1", "mail_address2", "mail_city", "mail_state", "mail_zip",
    ]
    sos_raw = sos_ds.scanner(columns=sos_cols, filter=sos_filter).to_table()
    rows_sos_raw = len(sos_raw)
    logger.info("  sos/fl_entities_lance (status='A'): %d rows", rows_sos_raw)

    entity_num = sos_raw.column("entity_num").to_pylist()
    entity_name = sos_raw.column("entity_name").to_pylist()
    entity_name_norm = sos_raw.column("entity_name_normalized").to_pylist()
    status_v = sos_raw.column("status").to_pylist()
    filing_type = sos_raw.column("filing_type").to_pylist()
    file_date = sos_raw.column("file_date").to_pylist()
    a1 = sos_raw.column("address1").to_pylist()
    a2 = sos_raw.column("address2").to_pylist()
    city_p = sos_raw.column("city").to_pylist()
    state_p = sos_raw.column("state").to_pylist()
    zip_p = sos_raw.column("zip").to_pylist()
    m1 = sos_raw.column("mail_address1").to_pylist()
    m2 = sos_raw.column("mail_address2").to_pylist()
    city_m = sos_raw.column("mail_city").to_pylist()
    state_m = sos_raw.column("mail_state").to_pylist()
    zip_m = sos_raw.column("mail_zip").to_pylist()

    sos_rows: list[dict] = []
    seen: set = set()
    for i in range(rows_sos_raw):
        # Prefer principal address, fall back to mailing
        p_joined = join_sam_line_1_2(a1[i], a2[i])
        m_joined = join_sam_line_1_2(m1[i], m2[i])
        if p_joined and (a1[i] or "").strip():
            addr_source = "principal"
            joined = p_joined
            city_raw = (city_p[i] or "").strip()
            state_raw = ((state_p[i] or "").strip()).upper()
            postal_raw = (zip_p[i] or "").strip()
        elif m_joined:
            addr_source = "mailing"
            joined = m_joined
            city_raw = (city_m[i] or "").strip()
            state_raw = ((state_m[i] or "").strip()).upper()
            postal_raw = (zip_m[i] or "").strip()
        else:
            continue

        if len(state_raw) != 2:
            state_raw = _FULL_STATE_TO_ABBR.get(state_raw, state_raw)
        if len(state_raw) != 2:
            continue

        z = (postal_raw or "")[:5]
        if len(z) != 5 or not z.isdigit():
            continue

        base = normalize_address_street(joined)
        if not base:
            continue

        key = (entity_num[i], state_raw, z, base)
        if key in seen:
            continue
        seen.add(key)
        sos_rows.append(
            {
                "sos_entity_num": entity_num[i],
                "sos_entity_name": entity_name[i],
                "sos_entity_name_normalized": entity_name_norm[i],
                "sos_status": status_v[i],
                "sos_filing_type": filing_type[i],
                "sos_file_date": file_date[i],
                "sos_address_source": addr_source,
                "sos_address_line_1": (a1[i] if addr_source == "principal" else m1[i]),
                "sos_address_line_2": (a2[i] if addr_source == "principal" else m2[i]),
                "sos_city": city_raw,
                "sos_state": state_raw,
                "sos_zip5": z,
                "sos_address_base_normalized": base,
            }
        )
    del sos_raw, entity_num, entity_name, entity_name_norm, status_v, filing_type
    del file_date, a1, a2, city_p, state_p, zip_p, m1, m2, city_m, state_m, zip_m, seen

    logger.info(
        "  sos normalized + deduped: %d distinct (entity_num, state, zip5, base_address)",
        len(sos_rows),
    )

    sos_arrow = pa.table(
        {k: [r.get(k) for r in sos_rows] for k in sos_rows[0].keys()}
    )
    del sos_rows
    rows_sos = len(sos_arrow)

    logger.info("opening overture/us_places_lance ...")
    overture_ds = lance.dataset(OVERTURE_PLACES_LANCE_URI, storage_options=storage_options)
    overture_filter = (
        pc.field("address_freeform").is_valid()
        & pc.field("address_postcode_5").is_valid()
        & pc.field("address_region").is_valid()
    )
    overture_cols = [
        "place_id", "name_primary", "name_normalized",
        "address_freeform", "address_locality",
        "address_postcode_5", "address_region",
        "categories_primary", "phone_primary",
        "website_primary", "email_primary",
        "brand_wikidata", "brand_name_primary",
        "operating_status", "confidence",
    ]
    overture_raw = overture_ds.scanner(columns=overture_cols, filter=overture_filter).to_table()
    rows_overture_raw = len(overture_raw)
    logger.info("  overture us_places_lance (post-filter): %d rows", rows_overture_raw)

    free = overture_raw.column("address_freeform").to_pylist()
    region = overture_raw.column("address_region").to_pylist()
    zip5_ovt = overture_raw.column("address_postcode_5").to_pylist()

    base_ovt = [normalize_address_street(s) for s in free]
    region_up = [(r or "").strip().upper() for r in region]
    zip5_norm = [(z or "").strip() for z in zip5_ovt]

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
    del overture_raw, free, base_ovt, region, region_up, zip5_ovt, zip5_norm
    del base_ovt_arr, region_up_arr, keep_flags

    return sos_arrow, overture_arrow, rows_sos, rows_overture


def _build_match_table(sos_arrow, overture_arrow, *, bridge_run_id: str, generated_at_iso: str):
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")

    con.register("sos", sos_arrow)
    con.register("overture", overture_arrow)

    logger.info(
        "  registered: sos=%d  overture=%d",
        con.execute("SELECT COUNT(*) FROM sos").fetchone()[0],
        con.execute("SELECT COUNT(*) FROM overture").fetchone()[0],
    )

    con.execute(
        """
        CREATE TEMP TABLE matched AS
        SELECT
            s.sos_address_base_normalized      AS address_base_normalized,
            s.sos_state                         AS match_state,
            s.sos_zip5                          AS match_zip5,
            s.sos_entity_num,
            s.sos_entity_name,
            s.sos_entity_name_normalized,
            s.sos_status,
            s.sos_filing_type,
            s.sos_file_date,
            s.sos_address_source,
            s.sos_address_line_1,
            s.sos_address_line_2,
            s.sos_city,
            o.place_id,
            o.name_primary                      AS overture_name_primary,
            o.name_normalized                   AS overture_name_normalized,
            o.brand_name_primary                AS overture_brand_name_primary,
            o.brand_wikidata                    AS overture_brand_wikidata,
            o.address_freeform                  AS overture_address_freeform,
            o.address_locality                  AS overture_address_locality,
            o.categories_primary                AS overture_categories_primary,
            o.phone_primary                     AS overture_phone_primary,
            o.website_primary                   AS overture_website_primary,
            o.email_primary                     AS overture_email_primary,
            o.operating_status                  AS overture_operating_status,
            o.confidence                        AS overture_confidence,
            'address_base'                      AS match_path
        FROM sos s
        JOIN overture o
          ON s.sos_address_base_normalized = o.address_base_normalized
         AND s.sos_state                   = o.address_region_normalized
         AND s.sos_zip5                    = o.address_postcode_5
        """
    )
    rows_matched = con.execute("SELECT COUNT(*) FROM matched").fetchone()[0]
    logger.info("  matched (pre-tier): %d rows", rows_matched)

    con.execute(
        """
        CREATE TEMP TABLE sos_fanout AS
        SELECT sos_entity_num, COUNT(*) AS sos_fan_out
        FROM matched GROUP BY sos_entity_num
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE overture_fanout AS
        SELECT place_id, COUNT(*) AS overture_fan_out
        FROM matched GROUP BY place_id
        """
    )
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            m.*,
            sf.sos_fan_out,
            of_.overture_fan_out,
            CASE
                WHEN sf.sos_fan_out > {COLLISION_THRESHOLD}
                  OR of_.overture_fan_out > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN sf.sos_fan_out = 1 AND of_.overture_fan_out = 1
                    THEN 'platinum'
                WHEN sf.sos_fan_out = 1 OR  of_.overture_fan_out = 1
                    THEN 'gold'
                ELSE 'silver'
            END                              AS confidence_tier,
            TIMESTAMP '{generated_at_iso}'   AS generated_at,
            '{BRIDGE_VERSION}'               AS bridge_version,
            '{bridge_run_id}'                AS bridge_run_id
        FROM matched m
        JOIN sos_fanout sf ON sf.sos_entity_num = m.sos_entity_num
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
            reader, BRIDGE_LANCE_URI, mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t0
        lance_count = ds.count_rows()
        logger.info("wrote %d rows in %.1fs (version=%s)", lance_count, write_dur, ds.version)

        os.environ["LANCE_BYPASS_SPILLING"] = "true"
        for col in ("sos_entity_num", "place_id", "address_base_normalized"):
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
            "sides. FL SoS uses address1+address2 (multi-line, concatenated) "
            "with mail_address1+mail_address2 fallback."
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
            "address1", "address2", "zip", "state",
            "mail_address1", "mail_address2", "mail_zip", "mail_state",
        ],
        input_columns_right=[
            "address_freeform", "address_postcode_5", "address_region",
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
            "FL Division of Corporations Active entities × Overture US Places — "
            "address-keyed exact match. Anchors on principal address1+address2 "
            "(with mail fallback). Sibling of sos_ca_overture_address."
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
    logger.info("inputs: sos/fl_entities_lance + overture/us_places_lance (Arrow-bridge)")
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
        sos_arrow, overture_arrow, rows_left, rows_right = _materialize_inputs(storage_options)
        con, counts = _build_match_table(
            sos_arrow, overture_arrow,
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

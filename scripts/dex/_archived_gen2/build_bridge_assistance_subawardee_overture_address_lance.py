#!/usr/bin/env python3
"""Bridge generator: USAspending assistance-subawardee × Overture US Place — address-keyed.

Anchored on the subaward filing's own subawardee_* address fields. Solves
the gap for Route D UEIs — assistance subawardees that don't appear in
sam_gov/entities_lance (no SAM entity record). For those UEIs, SAM-anchored
bridges (sam_overture_address) are useless. The only address available is
what the prime filer wrote into FSRS.

Output is keyed on subawardee_uei.

Inputs:
  assistance_subawards: `polaris-warehouse/usaspending/assistance_subawards_lance` (~54K rows, ~22.9K distinct UEIs)
  Overture:             `polaris-warehouse/overture/us_places_lance`              (~15.95M rows)

Address: `subawardee_address_line_1` (no line_2 in this source), `subawardee_city_name`,
`subawardee_state_code`, `subawardee_zip_code`. Per-UEI: pick the most-recent
filing's address as canonical.

Join key (composite, exact-equality after normalization):
  (address_base_normalized, zip5, 2-letter_state)

Tiering: same as siblings (platinum 1:1, gold 1:N|N:1, silver N:M ≤50, rejected >50).

Output: polaris-warehouse/bridges/assistance_subawardee_overture_address_lance
Method: REUSES `address_base_state_zip_exact` v1.0.0.
Floor:  ≥ 3,000 matched rows (conservative — cohort is ~22K UEIs).
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
logger = logging.getLogger("build_bridge_assistance_subawardee_overture_address_lance")

BRIDGE_NAME = "assistance_subawardee_overture_address"
METHOD_NAME = "address_base_state_zip_exact"
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "1.0.0"

SOURCE_LEFT = "usaspending_assistance_subawards_lance"
SOURCE_RIGHT = "overture_us_places_lance"

ASA_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/assistance_subawards_lance"
OVERTURE_PLACES_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/overture/us_places_lance"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/assistance_subawardee_overture_address_lance"
DATASET_SLUG = "assistance_subawardee_overture_address_lance"

COLLISION_THRESHOLD = 50
MIN_ROWS_MATCHED = 3_000
TMP_DIR = "/tmp/lance"
DUCKDB_TMP_DIR = "/Users/benjamincrane/dex-build-tmp"


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
    """Load assistance subawards + Overture; normalize address; pre-dedup per UEI."""
    import lance
    import pyarrow as pa
    import pyarrow.compute as pc

    logger.info("opening usaspending/assistance_subawards_lance ...")
    asa_ds = lance.dataset(ASA_LANCE_URI, storage_options=storage_options)
    asa_cols = [
        "subawardee_uei", "subawardee_name", "subawardee_dba_name",
        "subawardee_address_line_1", "subawardee_city_name",
        "subawardee_state_code", "subawardee_zip_code",
        "subaward_amount", "subaward_action_date",
        "prime_award_awarding_agency_name",
    ]
    asa_tbl = asa_ds.scanner(columns=asa_cols, filter=pc.field("subawardee_uei").is_valid()).to_table()
    logger.info("  assistance_subawards: %d rows", asa_tbl.num_rows)

    # Per-UEI: pick most-recent filing's address
    import duckdb
    con0 = duckdb.connect()
    con0.register("asa", asa_tbl)
    con0.execute("""
      CREATE TEMP TABLE asa_per_uei AS
      SELECT subawardee_uei AS uei,
        arg_max(subawardee_name, subaward_action_date) AS subawardee_name,
        arg_max(subawardee_dba_name, subaward_action_date) AS subawardee_dba_name,
        arg_max(subawardee_address_line_1, subaward_action_date) AS subawardee_address_line_1,
        arg_max(subawardee_city_name, subaward_action_date) AS subawardee_city,
        upper(arg_max(subawardee_state_code, subaward_action_date)) AS subawardee_state,
        arg_max(subawardee_zip_code, subaward_action_date) AS subawardee_zip,
        count(*) AS asa_filing_count,
        sum(coalesce(subaward_amount, 0)) AS asa_total_dollars,
        max(subaward_action_date) AS asa_latest_date,
        arg_max(prime_award_awarding_agency_name, subaward_action_date) AS latest_prime_agency
      FROM asa WHERE subawardee_uei <> '' GROUP BY uei
    """)
    asa_uei_arrow = con0.execute("SELECT * FROM asa_per_uei").fetch_arrow_table()
    logger.info("  distinct assistance subawardees: %d", asa_uei_arrow.num_rows)

    # Python normalize: address_base_normalized
    uei = asa_uei_arrow.column("uei").to_pylist()
    name = asa_uei_arrow.column("subawardee_name").to_pylist()
    dba = asa_uei_arrow.column("subawardee_dba_name").to_pylist()
    a1 = asa_uei_arrow.column("subawardee_address_line_1").to_pylist()
    city = asa_uei_arrow.column("subawardee_city").to_pylist()
    state = asa_uei_arrow.column("subawardee_state").to_pylist()
    zipc = asa_uei_arrow.column("subawardee_zip").to_pylist()
    n_filings = asa_uei_arrow.column("asa_filing_count").to_pylist()
    total_dollars = asa_uei_arrow.column("asa_total_dollars").to_pylist()
    latest_date = asa_uei_arrow.column("asa_latest_date").to_pylist()
    latest_agency = asa_uei_arrow.column("latest_prime_agency").to_pylist()

    rows: list[dict] = []
    seen: set = set()
    for i in range(len(uei)):
        addr_raw = (a1[i] or "").strip()
        if not addr_raw:
            continue
        st = (state[i] or "").strip().upper()
        if len(st) != 2:
            continue
        z = ((zipc[i] or "").strip())[:5]
        if len(z) != 5 or not z.isdigit():
            continue
        base = normalize_address_street(addr_raw)
        if not base:
            continue
        key = (uei[i], st, z, base)
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "subawardee_uei": uei[i],
            "subawardee_name": name[i],
            "subawardee_dba_name": dba[i],
            "subawardee_address_line_1": a1[i],
            "subawardee_city": (city[i] or "").strip(),
            "subawardee_state": st,
            "subawardee_zip5": z,
            "subawardee_address_base_normalized": base,
            "asa_filing_count": n_filings[i],
            "asa_total_dollars": total_dollars[i],
            "asa_latest_date": str(latest_date[i]) if latest_date[i] else None,
            "latest_prime_agency": latest_agency[i],
        })
    logger.info("  normalized + deduped: %d", len(rows))

    asa_arrow = pa.table({k: [r.get(k) for r in rows] for k in rows[0].keys()})
    rows_left = len(asa_arrow)

    # Overture
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
    base_ovt_arr = pa.array([b for b, k in zip(base_ovt, keep_flags) if k], type=pa.string())
    region_up_arr = pa.array([r for r, k in zip(region_up, keep_flags) if k], type=pa.string())

    overture_arrow = overture_filtered.append_column("address_base_normalized", base_ovt_arr)
    overture_arrow = overture_arrow.append_column("address_region_normalized", region_up_arr)
    rows_overture = len(overture_arrow)
    logger.info("  overture normalized + valid-keyed: %d rows", rows_overture)
    del overture_raw, free, base_ovt, region, region_up, zip5_ovt, zip5_norm
    del base_ovt_arr, region_up_arr, keep_flags

    return asa_arrow, overture_arrow, rows_left, rows_overture


def _build_match_table(asa_arrow, overture_arrow, *, bridge_run_id: str, generated_at_iso: str):
    import duckdb
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    Path(DUCKDB_TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET memory_limit='24GB'")
    con.execute(f"SET temp_directory='{DUCKDB_TMP_DIR}'")
    con.execute("SET max_temp_directory_size='240GB'")
    con.execute("SET preserve_insertion_order=false")

    con.register("asa", asa_arrow)
    con.register("overture", overture_arrow)

    logger.info(
        "  registered: asa=%d  overture=%d",
        con.execute("SELECT COUNT(*) FROM asa").fetchone()[0],
        con.execute("SELECT COUNT(*) FROM overture").fetchone()[0],
    )

    con.execute(
        """
        CREATE TEMP TABLE matched AS
        SELECT
            a.subawardee_address_base_normalized AS address_base_normalized,
            a.subawardee_state                     AS match_state,
            a.subawardee_zip5                      AS match_zip5,
            a.subawardee_uei,
            a.subawardee_name,
            a.subawardee_dba_name,
            a.subawardee_address_line_1,
            a.subawardee_city,
            a.asa_filing_count,
            a.asa_total_dollars,
            a.asa_latest_date,
            a.latest_prime_agency,
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
        FROM asa a
        JOIN overture o
          ON a.subawardee_address_base_normalized = o.address_base_normalized
         AND a.subawardee_state                   = o.address_region_normalized
         AND a.subawardee_zip5                    = o.address_postcode_5
        """
    )
    rows_matched = con.execute("SELECT COUNT(*) FROM matched").fetchone()[0]
    logger.info("  matched (pre-tier): %d rows", rows_matched)

    con.execute("CREATE TEMP TABLE asa_fanout AS SELECT subawardee_uei, COUNT(*) AS asa_fan_out FROM matched GROUP BY subawardee_uei")
    con.execute("CREATE TEMP TABLE overture_fanout AS SELECT place_id, COUNT(*) AS overture_fan_out FROM matched GROUP BY place_id")

    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT m.*, af.asa_fan_out, of_.overture_fan_out,
          CASE
            WHEN af.asa_fan_out > {COLLISION_THRESHOLD} OR of_.overture_fan_out > {COLLISION_THRESHOLD} THEN 'rejected'
            WHEN af.asa_fan_out = 1 AND of_.overture_fan_out = 1 THEN 'platinum'
            WHEN af.asa_fan_out = 1 OR  of_.overture_fan_out = 1 THEN 'gold'
            ELSE 'silver'
          END AS confidence_tier,
          TIMESTAMP '{generated_at_iso}' AS generated_at,
          '{BRIDGE_VERSION}' AS bridge_version,
          '{bridge_run_id}' AS bridge_run_id
        FROM matched m
        JOIN asa_fanout af ON af.subawardee_uei = m.subawardee_uei
        JOIN overture_fanout of_ ON of_.place_id = m.place_id
        """
    )
    con.execute("CREATE TEMP TABLE bridge_match AS SELECT * FROM bridge_all WHERE confidence_tier <> 'rejected'")

    row_counts = con.execute("""
      SELECT COUNT(*),
        COUNT(*) FILTER (WHERE confidence_tier='platinum'),
        COUNT(*) FILTER (WHERE confidence_tier='gold'),
        COUNT(*) FILTER (WHERE confidence_tier='silver')
      FROM bridge_match
    """).fetchone()
    rejected = con.execute("SELECT COUNT(*) FROM bridge_all WHERE confidence_tier='rejected'").fetchone()[0]

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

    logger.info("materializing bridge_match to Arrow in memory ...")
    arrow_tbl = con.execute("SELECT * FROM bridge_match").fetch_arrow_table()
    logger.info("  materialized %d rows", arrow_tbl.num_rows)

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        logger.info("writing bridge to Lance at %s ...", BRIDGE_LANCE_URI)
        ds = lance.write_dataset(arrow_tbl, BRIDGE_LANCE_URI, mode="overwrite", storage_options=storage_options)
        write_dur = time.time() - t0
        lance_count = ds.count_rows()
        logger.info("wrote %d rows in %.1fs (version=%s)", lance_count, write_dur, ds.version)

        os.environ["LANCE_BYPASS_SPILLING"] = "true"
        for col in ("subawardee_uei", "place_id", "address_base_normalized"):
            try:
                ds.create_scalar_index(col, index_type="BTREE", replace=True)
                logger.info("BTREE index created on %s", col)
            except Exception as e:
                logger.warning("BTREE index on %s failed (non-fatal): %s", col, e)
        try:
            ds.optimize.compact_files()
        except Exception as e:
            logger.warning("compact_files failed (non-fatal): %s", e)

    return lance_count


def _ensure_registry() -> None:
    register_match_method(
        method_name=METHOD_NAME,
        description=(
            "Exact-equality JOIN on (address_base_normalized, 2-letter US state, 5-digit zip). "
            "Applies _lib/address_normalize.py "
            f"v{ADDR_NORMALIZER_VERSION}. Left-source-agnostic — adapters concatenate multi-line "
            "or use single field per source convention."
        ),
    )
    register_match_method_version(
        method_name=METHOD_NAME,
        semver=METHOD_SEMVER,
        normalizer_module="_lib/address_normalize.py",
        normalizer_version=ADDR_NORMALIZER_VERSION,
        blacklist_module="_lib/address_normalize.py",
        blacklist_version=ADDR_NORMALIZER_VERSION,
        tier_rule_description="platinum=1:1; gold=1:N or N:1; silver=N:M ≤50; rejected=>50",
        rejection_rule_description="fan-out >50 on either side → rejected",
        input_columns_left=["subawardee_address_line_1", "subawardee_zip_code", "subawardee_state_code"],
        input_columns_right=["address_freeform", "address_postcode_5", "address_region"],
        output_value_description="normalized USPS-abbrev street + 2-letter state + 5-digit zip join key",
    )
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "USAspending assistance-subawardee filings × Overture US Places — address-keyed. "
            "Anchors on the subaward filing's subawardee_address_line_1 + state + zip. "
            "Reaches subawardee UEIs that are absent from sam_gov/entities_lance (state/local "
            "gov sub-grantees, small nonprofits, etc.) — i.e. Route D in the assistance-subawardee "
            "routing taxonomy."
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

    logger.info("bridge: %s  method=%s v%s  normalizer=v%s",
                BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER, ADDR_NORMALIZER_VERSION)
    logger.info("output: %s", BRIDGE_LANCE_URI)

    if args.dry_run:
        bridge_run_id = "00000000-0000-0000-0000-000000000000"
        run_uuid = None
    else:
        _ensure_registry()
        run_uuid = start_bridge_run(
            bridge_name=BRIDGE_NAME, method_semver=METHOD_SEMVER, bridge_version=BRIDGE_VERSION,
            source_left=SOURCE_LEFT, source_right=SOURCE_RIGHT,
            match_method=METHOD_NAME, r2_output_key=BRIDGE_LANCE_URI,
        )
        bridge_run_id = str(run_uuid)
        logger.info("bridge_run_id=%s", bridge_run_id)

    try:
        asa_arrow, overture_arrow, rows_left, rows_right = _materialize_inputs(storage_options)
        con, counts = _build_match_table(
            asa_arrow, overture_arrow,
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
            msg = f"HARD FAIL: rows_matched={counts['rows_matched']:,} < floor={MIN_ROWS_MATCHED:,}"
            logger.error(msg)
            if run_uuid is not None:
                fail_bridge_run(run_uuid, msg)
            return 1

        if args.dry_run:
            logger.info("DRY RUN OK — no Lance / Postgres writes. duration=%.1fs", time.time() - t0)
            return 0

        lance_count = _write_bridge_lance(con, storage_options)
        complete_bridge_run(run_uuid, metrics={
            "rows_left": rows_left, "rows_right": rows_right,
            "rows_matched": counts["rows_matched"],
            "rows_tier1": counts["rows_tier1"], "rows_tier2": counts["rows_tier2"],
            "rows_tier3": counts["rows_tier3"],
            "rows_collision_rejected": counts["rows_collision_rejected"],
            "lance_rows": lance_count,
        })
        logger.info("OK — run_id=%s duration=%.1fs", bridge_run_id, time.time() - t0)
        return 0

    except Exception as exc:
        logger.exception("bridge build FAILED: %s", exc)
        if run_uuid is not None:
            fail_bridge_run(run_uuid, str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

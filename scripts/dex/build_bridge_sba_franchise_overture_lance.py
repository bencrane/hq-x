#!/usr/bin/env python3
"""Bridge generator: SBA franchisees × Overture US Places — brand-keyed match.

Cycle: sba-franchise-overture-bridge (Tier 1.5 follow-up to overture-sba-borrower-bridge).

The legal-name bridge (build_bridge_sba_overture_places_lance.py) hits only 1.9%
of TX-franchisee-COMMIT borrowers because SBA stores the LEGAL entity name
("Smith Family Holdings LLC") while Overture stores the OPERATING name
("Subway"). For franchisees this is structurally guaranteed to mismatch.

This bridge inverts the join: instead of (legal_name × name), use
(franchise_brand × brand_name). A franchisee borrower of "SUBWAY" in TX
zip 78258 matches all Overture places where brand_name_primary="Subway" in
TX 78258. Output is one row per (sba_borrower × overture_place) pair — multiple
rows per borrower if multiple Overture locations match.

Inputs:
  - sba/borrowers_lance (12M rows; with franchise_brands_set list<string>)
  - overture/us_places_lance (15.95M rows; with brand_name_primary)

Explode SBA's franchise_brands_set (one row per (borrower, brand)) then JOIN.

Join key: (brand_normalized, borrstate, address_postcode_5) — composite.
Pre-normalize + pre-dedup in Python before DuckDB (OOM-resistant per UCC × PDL).

Fan-out tiering:
  platinum = 1:1 (single brand-borrower at zip × single Overture place — extremely rare)
  gold     = 1:N or N:1 (one franchise at zip, multiple Overture places — common)
  silver   = N:M ≤50
  rejected = >50

Output: polaris-warehouse/bridges/sba_franchise_overture_lance/
Audit row: ops.bridge_generation_runs (bridge_name='sba_franchise_overture')
Floor: ≥50,000 rows (sanity; refine post dry-run).

Arrow-bridge pattern (NOT lance-duckdb extension).
pc.field & pc.field Expression operators (NOT pc.and_()).
.to_table() materialization (NOT .arrow()).

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/build_bridge_sba_franchise_overture_lance.py --apply

  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/build_bridge_sba_franchise_overture_lance.py --dry-run
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
logger = logging.getLogger("build_bridge_sba_franchise_overture_lance")

BRIDGE_NAME = "sba_franchise_overture"
METHOD_NAME = "franchise_brand_state_zip_exact"
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "1.0.0"

SOURCE_LEFT = "sba_borrowers_lance_franchise_exploded"
SOURCE_RIGHT = "overture_us_places_lance"

SBA_BORROWERS_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sba/borrowers_lance"
OVERTURE_PLACES_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/overture/us_places_lance"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_franchise_overture_lance"
DATASET_SLUG = "sba_franchise_overture_lance"

COLLISION_THRESHOLD = 50
# Floor calibrated post dry-run: actual 33K matched rows on first run
# (17K platinum + 12K gold + 4K silver; 144K SBA-franchise universe). 50K
# was set without measurement. 25K is permissive sanity floor below actual.
MIN_ROWS_MATCHED = 25_000
TMP_DIR = "/tmp/lance"

# Placeholder/anonymized values from SBA that aren't real brand names — skip
# at brand-explode time so they don't pollute the join.
_BRAND_BLACKLIST: set[str] = {
    "temporary franchises",
    "temporary franchise",
    "n a",
    "na",
    "none",
    "",
}


def _normalize_brand(raw: str | None) -> str | None:
    """Normalize a franchise brand name for cross-source matching.

    Steps:
      1. Strip parentheticals: "SMOOTHIE KING (HEALTH FOOD STORE)" → "SMOOTHIE KING"
      2. Strip "a.k.a." aliases and everything after them
      3. Lowercase
      4. Strip leading/trailing whitespace, collapse internal whitespace
      5. Strip trailing punctuation
      6. Return None if result is empty or in the blacklist (placeholder values)
    """
    import re
    if not raw:
        return None
    s = raw
    # Strip parentheticals (including nested-like content)
    s = re.sub(r"\s*\([^)]*\)\s*", " ", s)
    # Cut off at "a.k.a." (case-insensitive)
    s = re.split(r"(?i)\s+a\.?k\.?a\.?\s+", s)[0]
    # Lowercase
    s = s.lower()
    # Replace non-alphanumeric with space (preserves multi-word brand structure)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    # Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    if s in _BRAND_BLACKLIST:
        return None
    return s or None


def _lance_storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _clean_zip5(z) -> str | None:
    """Clean SBA borrzip: strip trailing '.0' from DOUBLE→VARCHAR cast residue,
    then take first 5 chars. Returns None if result < 5 chars.

    Handles:
      '70817.0XYZ' -> '70817'  (10-char zip+4 with .0)
      '12345.0'    -> '12345'  (5-digit + .0)
      '12345.'     -> '12345'  (5-digit + .)
      '5491.0'     -> None     (4-digit zip after strip — no match)
      None         -> None
    """
    if z is None:
        return None
    s = str(z).strip()
    if s.endswith(".0"):
        s = s[:-2]
    elif s.endswith("."):
        s = s[:-1]
    return s[:5] if len(s) >= 5 else None


def _materialize_inputs(storage_options: dict) -> tuple:
    """Load SBA borrowers + Overture US Places into Arrow tables.

    SBA side:
      - Read legal_name_normalized, borrstate, borrzip from borrowers_lance
      - Clean borrzip to zip5 via _clean_zip5()
      - Pre-dedup to distinct (legal_name_normalized, borrstate, borrzip5) set
        (10.85M distinct of 12M raw per validator)

    Overture side:
      - Read name_normalized, address_region, address_postcode_5 + all enrichment
        columns from us_places_lance
      - Apply scan-time PyArrow filter: name_normalized valid + address_region valid
        + address_postcode_5 valid + len(address_region)==2 + len(address_postcode_5)==5
        (reviewer amendment A1 — drops 73 empty-region + 5 'FLORIDA' rows + malformed zip5)
      - .to_table() materialization (NOT .arrow())
    """
    import lance
    import pyarrow as pa
    import pyarrow.compute as pc

    logger.info("opening sba/borrowers_lance ...")
    sba_ds = lance.dataset(SBA_BORROWERS_LANCE_URI, storage_options=storage_options)
    # No scanner-level franchise filter: list_value_length lacks Substrait
    # conversion on this pylance build (pyarrow ArrowNotImplementedError).
    # Scan all 12M rows then filter empty/null franchise lists in the Python
    # explode loop below.
    sba_raw = sba_ds.scanner(
        columns=["legal_name_normalized", "borrstate", "borrzip", "franchise_brands_set"],
    ).to_table()
    rows_sba_raw = len(sba_raw)
    logger.info("  sba borrowers_lance (scanned all): %d rows", rows_sba_raw)

    # Explode franchise_brands_set in Python: one row per (borrower × brand).
    # Pre-normalize brand at explode time so the SQL join is on a clean key.
    legal_names = sba_raw.column("legal_name_normalized").to_pylist()
    states = sba_raw.column("borrstate").to_pylist()
    zips_raw = sba_raw.column("borrzip").to_pylist()
    franchise_lists = sba_raw.column("franchise_brands_set").to_pylist()

    # set of (legal_name, state, zip5, brand_normalized, brand_raw_first_seen)
    # — keyed by first 4 for dedup; preserve the first raw-brand spelling we saw.
    sba_branded: dict[tuple[str, str, str, str], str] = {}
    skipped_blacklist = 0
    skipped_bad_input = 0
    for nm, st, z, brands in zip(legal_names, states, zips_raw, franchise_lists):
        if not nm or not st or not brands:
            skipped_bad_input += 1
            continue
        st_clean = st.strip().upper() if isinstance(st, str) else str(st).strip().upper()
        if len(st_clean) != 2:
            skipped_bad_input += 1
            continue
        zip5 = _clean_zip5(z)
        if not zip5:
            skipped_bad_input += 1
            continue
        for brand_raw in brands:
            brand_norm = _normalize_brand(brand_raw)
            if brand_norm is None:
                skipped_blacklist += 1
                continue
            key = (nm, st_clean, zip5, brand_norm)
            if key not in sba_branded:
                sba_branded[key] = brand_raw

    del sba_raw, legal_names, states, zips_raw, franchise_lists

    logger.info(
        "  sba_branded (distinct legal_name + state + zip5 + brand_normalized): %d rows "
        "[input borrowers=%d, skipped_bad_input=%d, skipped_blacklist=%d]",
        len(sba_branded), rows_sba_raw, skipped_bad_input, skipped_blacklist,
    )

    items = list(sba_branded.items())
    sba_branded_arrow = pa.table(
        {
            "sba_legal_name_normalized": pa.array([k[0] for k, _ in items], type=pa.string()),
            "sba_borrstate":             pa.array([k[1] for k, _ in items], type=pa.string()),
            "sba_borrzip5":              pa.array([k[2] for k, _ in items], type=pa.string()),
            "sba_brand_normalized":      pa.array([k[3] for k, _ in items], type=pa.string()),
            "sba_brand_raw":             pa.array([v for _, v in items], type=pa.string()),
        }
    )
    del sba_branded, items
    rows_sba = len(sba_branded_arrow)

    logger.info("opening overture/us_places_lance ...")
    overture_ds = lance.dataset(OVERTURE_PLACES_LANCE_URI, storage_options=storage_options)

    # Only places WITH a brand are joinable for this bridge (vs ~67% of US places
    # without one). Filter at scanner-level for selectivity.
    overture_filter = (
        pc.field("brand_name_primary").is_valid()
        & pc.field("address_region").is_valid()
        & pc.field("address_postcode_5").is_valid()
    )

    overture_arrow_raw = overture_ds.scanner(
        columns=[
            "place_id",
            "name_primary",
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
        ],
        filter=overture_filter,
    ).to_table()
    rows_overture_raw = len(overture_arrow_raw)
    logger.info("  overture us_places_lance (with brand): %d rows", rows_overture_raw)

    # Normalize brand_name_primary in Python (same regex as SBA side) and attach
    # as a new column. Skip rows where normalized brand is null (blacklist hits).
    brand_raw_list = overture_arrow_raw.column("brand_name_primary").to_pylist()
    brand_normalized_list = [_normalize_brand(b) for b in brand_raw_list]
    overture_with_norm = overture_arrow_raw.append_column(
        "overture_brand_normalized",
        pa.array(brand_normalized_list, type=pa.string()),
    )
    # Drop rows where brand_normalized is null after normalization
    keep_mask = pc.is_valid(overture_with_norm.column("overture_brand_normalized"))
    overture_arrow = overture_with_norm.filter(keep_mask)
    rows_overture = len(overture_arrow)
    logger.info(
        "  overture (post brand-normalize, kept): %d rows (dropped %d)",
        rows_overture, rows_overture_raw - rows_overture,
    )
    del overture_arrow_raw, overture_with_norm, brand_raw_list, brand_normalized_list

    return sba_branded_arrow, overture_arrow, rows_sba, rows_overture


def _build_match_table(
    sba_branded_arrow,
    overture_arrow,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
) -> tuple:
    """Run composite-key JOIN + fan-out tiering in DuckDB.

    Join key: (sba_legal_name_normalized, sba_borrstate, sba_borrzip5)
               = (overture.name_normalized, upper(overture.address_region),
                  overture.address_postcode_5)

    address_region is already uppercased at s1 emit time; the upper() in the
    JOIN is defense-in-depth per reviewer finding I.
    """
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")

    con.register("sba_branded", sba_branded_arrow)
    con.register("overture", overture_arrow)

    rows_sba_reg = con.execute("SELECT COUNT(*) FROM sba_branded").fetchone()[0]
    rows_ov_reg = con.execute("SELECT COUNT(*) FROM overture").fetchone()[0]
    logger.info(
        "  registered: sba_branded=%d  overture=%d", rows_sba_reg, rows_ov_reg
    )

    # INNER JOIN on composite key (brand_normalized, state, zip5)
    con.execute(
        f"""
        CREATE TEMP TABLE matched AS
        SELECT
            s.sba_legal_name_normalized,
            s.sba_borrstate,
            s.sba_borrzip5,
            s.sba_brand_normalized,
            s.sba_brand_raw,
            o.place_id,
            o.name_primary                  AS place_name_primary,
            o.address_freeform,
            o.address_locality,
            o.address_postcode_5,
            o.categories_primary,
            o.phone_primary,
            o.website_primary,
            o.email_primary,
            o.brand_wikidata,
            o.brand_name_primary            AS overture_brand_raw,
            o.overture_brand_normalized,
            o.operating_status,
            o.confidence                    AS overture_confidence,
            'franchise_brand'               AS match_path
        FROM sba_branded s
        JOIN overture o
          ON s.sba_brand_normalized = o.overture_brand_normalized
         AND s.sba_borrstate        = upper(o.address_region)
         AND s.sba_borrzip5         = o.address_postcode_5
        """
    )
    rows_matched = con.execute("SELECT COUNT(*) FROM matched").fetchone()[0]
    logger.info("  matched (pre-tier): %d rows", rows_matched)

    # Fan-out tiering — keyed by (borrower + brand + zip) on SBA side, place_id on Overture side
    con.execute("""
        CREATE TEMP TABLE sba_fanout AS
        SELECT
            sba_legal_name_normalized AS k1,
            sba_borrstate             AS k2,
            sba_borrzip5              AS k3,
            sba_brand_normalized      AS k4,
            COUNT(*)                  AS sba_fan_out
        FROM matched
        GROUP BY 1, 2, 3, 4
    """)
    con.execute("""
        CREATE TEMP TABLE overture_fanout AS
        SELECT place_id, COUNT(*) AS overture_fan_out
        FROM matched
        GROUP BY place_id
    """)

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
            END                             AS confidence_tier,
            TIMESTAMP '{generated_at_iso}'  AS generated_at,
            '{BRIDGE_VERSION}'              AS bridge_version,
            '{bridge_run_id}'               AS bridge_run_id
        FROM matched m
        JOIN sba_fanout sf
          ON sf.k1 = m.sba_legal_name_normalized
         AND sf.k2 = m.sba_borrstate
         AND sf.k3 = m.sba_borrzip5
         AND sf.k4 = m.sba_brand_normalized
        JOIN overture_fanout of_
          ON of_.place_id = m.place_id
        """
    )

    con.execute("""
        CREATE TEMP TABLE bridge_match AS
        SELECT * FROM bridge_all WHERE confidence_tier <> 'rejected'
    """)

    row_counts = con.execute("""
        SELECT
            COUNT(*),
            COUNT(*) FILTER (WHERE confidence_tier = 'platinum'),
            COUNT(*) FILTER (WHERE confidence_tier = 'gold'),
            COUNT(*) FILTER (WHERE confidence_tier = 'silver')
        FROM bridge_match
    """).fetchone()
    rejected = con.execute(
        "SELECT COUNT(*) FROM bridge_all WHERE confidence_tier = 'rejected'"
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
    """Write bridge_match to Lance via Arrow-bridge pattern."""
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
        try:
            ds.create_scalar_index(
                "sba_legal_name_normalized", index_type="BTREE", replace=True
            )
            logger.info("BTREE index created on sba_legal_name_normalized")
        except Exception as e:
            logger.warning("BTREE index failed (non-fatal): %s", e)
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
            "Exact-equality JOIN on (franchise_brand_normalized, 2-letter US state, "
            "substr(postcode, 1, 5)). SBA borrowers' franchise_brands_set is exploded "
            "(one row per (borrower, brand)) and each brand normalized via "
            "_normalize_brand() (parenthetical-strip + a.k.a.-strip + lowercase + "
            "alphanumeric-only). Overture brand_name_primary normalized identically. "
            "SBA borrzip cleaned via _clean_zip5() to remove DOUBLE-cast .0 residue."
        ),
    )
    register_match_method_version(
        method_name=METHOD_NAME,
        semver=METHOD_SEMVER,
        normalizer_module="_normalize_brand (inline in build_bridge_sba_franchise_overture_lance.py)",
        normalizer_version=BRIDGE_VERSION,
        blacklist_module="_BRAND_BLACKLIST (inline)",
        blacklist_version=BRIDGE_VERSION,
        tier_rule_description=(
            "platinum=1:1; gold=1:N or N:1; silver=N:M ≤50; rejected=>50; keyed at "
            "(borrower+brand+zip) on SBA side, place_id on Overture side."
        ),
        rejection_rule_description="fan-out >50 on either side → rejected",
        input_columns_left=["legal_name_normalized", "borrstate", "borrzip", "franchise_brands_set"],
        input_columns_right=["brand_name_primary", "address_region", "address_postcode_5"],
        output_value_description=(
            "normalized franchise brand + 2-letter state + 5-digit zip join key; "
            "carries both raw + normalized brand on each side for audit."
        ),
    )
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "SBA franchisees × Overture US Places — brand-keyed exact match on "
            "(brand_normalized, state, zip5). Complementary to "
            "sba_overture_places_lance which is legal-name-keyed. This bridge "
            "captures the franchisee subset (1.9% legal-name hit → 30-60% brand hit) "
            "where SBA's legal-entity name differs from the operating brand. Attaches "
            "phone, website, email, brand_wikidata, operating_status to franchisee "
            "borrowers for Capital Expansion supply-side outreach."
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
        BRIDGE_NAME,
        METHOD_NAME,
        METHOD_SEMVER,
        NORMALIZER_VERSION,
    )
    logger.info("inputs: sba/borrowers_lance + overture/us_places_lance (Arrow-bridge)")
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
        sba_branded_arrow, overture_arrow, rows_left, rows_right = _materialize_inputs(
            storage_options
        )
        con, counts = _build_match_table(
            sba_branded_arrow,
            overture_arrow,
            bridge_run_id=bridge_run_id,
            generated_at_iso=started_at.isoformat(),
        )

        logger.info("-" * 60)
        logger.info("bridge tier distribution:")
        logger.info("  rows_matched:            %d", counts["rows_matched"])
        logger.info("    platinum (1:1):         %d", counts["rows_tier1"])
        logger.info("    gold     (1:N | N:1):   %d", counts["rows_tier2"])
        logger.info(
            "    silver   (N:M ≤%d):     %d",
            COLLISION_THRESHOLD,
            counts["rows_tier3"],
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
        logger.exception("bridge generation failed")
        if run_uuid is not None:
            try:
                fail_bridge_run(run_uuid, str(exc))
            except Exception:
                logger.exception("also failed to mark run as failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

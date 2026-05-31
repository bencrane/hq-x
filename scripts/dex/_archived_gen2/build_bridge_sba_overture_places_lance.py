#!/usr/bin/env python3
"""Bridge generator: SBA borrowers × Overture US Places — composite-key exact match.

Cycle: overture-sba-borrower-bridge (s2).

Inputs:
  - sba/borrowers_lance (12,008,176 rows; 10,846,461 distinct composite keys)
  - overture/us_places_lance (15,952,626 rows; emitted by s1)

Join key: (legal_name_normalized, borrstate, address_postcode_5) — composite
triple for selectivity. Pre-normalize + pre-dedup BOTH sides in Python before
handing off to DuckDB (per UCC × PDL cycle's OOM-resistant pattern).

Fan-out tiering:
  platinum = 1:1
  gold     = 1:N or N:1
  silver   = N:M (fan-out ≤ 50 on each side)
  rejected = any fan-out > 50

Output: polaris-warehouse/bridges/sba_overture_places_lance/
Audit row: ops.bridge_generation_runs (bridge_name='sba_overture_places')
Floor: ≥100,000 rows.

Arrow-bridge pattern (NOT lance-duckdb extension).
Pre-normalize + pre-dedup in Python BEFORE the DuckDB join (OOM-resistant).
pc.field & pc.field Expression operators (NOT pc.and_()).
.to_table() materialization (NOT .arrow()).

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/build_bridge_sba_overture_places_lance.py --apply

  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/build_bridge_sba_overture_places_lance.py --dry-run
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
logger = logging.getLogger("build_bridge_sba_overture_places_lance")

BRIDGE_NAME = "sba_overture_places"
METHOD_NAME = "company_name_state_zip_exact"
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "1.0.0"

SOURCE_LEFT = "sba_borrowers_lance"
SOURCE_RIGHT = "overture_us_places_lance"

SBA_BORROWERS_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sba/borrowers_lance"
OVERTURE_PLACES_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/overture/us_places_lance"
BRIDGE_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_overture_places_lance"
DATASET_SLUG = "sba_overture_places_lance"

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
    sba_raw = sba_ds.scanner(
        columns=["legal_name_normalized", "borrstate", "borrzip"]
    ).to_table()
    rows_sba_raw = len(sba_raw)
    logger.info("  sba borrowers_lance raw: %d rows", rows_sba_raw)

    # Pre-dedup SBA side: distinct (legal_name_normalized, borrstate, borrzip5)
    names = sba_raw.column("legal_name_normalized").to_pylist()
    states = sba_raw.column("borrstate").to_pylist()
    zips_raw = sba_raw.column("borrzip").to_pylist()

    sba_set: set = set()
    for nm, st, z in zip(names, states, zips_raw):
        if not nm or not st:
            continue
        st_clean = st.strip().upper() if isinstance(st, str) else str(st).strip().upper()
        if len(st_clean) != 2:
            continue
        zip5 = _clean_zip5(z)
        if not zip5:
            continue
        sba_set.add((nm, st_clean, zip5))

    del sba_raw, names, states, zips_raw

    logger.info(
        "  sba_branded (distinct name+state+zip5, deduped from %d raw rows): %d rows",
        rows_sba_raw,
        len(sba_set),
    )

    sba_branded_arrow = pa.table(
        {
            "sba_legal_name_normalized": pa.array(
                [r[0] for r in sba_set], type=pa.string()
            ),
            "sba_borrstate": pa.array([r[1] for r in sba_set], type=pa.string()),
            "sba_borrzip5": pa.array([r[2] for r in sba_set], type=pa.string()),
        }
    )
    del sba_set
    rows_sba = len(sba_branded_arrow)

    logger.info("opening overture/us_places_lance ...")
    overture_ds = lance.dataset(OVERTURE_PLACES_LANCE_URI, storage_options=storage_options)

    # Null-only filter at the Lance scanner. `utf8_length` lacks a Substrait
    # conversion on this lance build (pyarrow ArrowNotImplementedError),
    # so the A1 defense-in-depth length checks are applied post-scan via
    # DuckDB WHERE rather than pushed down into the scanner.
    overture_filter = (
        pc.field("name_normalized").is_valid()
        & pc.field("address_region").is_valid()
        & pc.field("address_postcode_5").is_valid()
    )

    overture_arrow = overture_ds.scanner(
        columns=[
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
        ],
        filter=overture_filter,
    ).to_table()
    rows_overture = len(overture_arrow)
    logger.info("  overture us_places_lance (post-filter): %d rows", rows_overture)

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

    # INNER JOIN on composite key
    con.execute(
        f"""
        CREATE TEMP TABLE matched AS
        SELECT
            s.sba_legal_name_normalized,
            s.sba_borrstate,
            s.sba_borrzip5,
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
            o.brand_name_primary,
            o.operating_status,
            o.confidence                    AS sba_overture_confidence,
            'direct'                        AS match_path
        FROM sba_branded s
        JOIN overture o
          ON s.sba_legal_name_normalized = o.name_normalized
         AND s.sba_borrstate             = upper(o.address_region)
         AND s.sba_borrzip5              = o.address_postcode_5
        """
    )
    rows_matched = con.execute("SELECT COUNT(*) FROM matched").fetchone()[0]
    logger.info("  matched (pre-tier): %d rows", rows_matched)

    # Fan-out tiering
    con.execute("""
        CREATE TEMP TABLE sba_fanout AS
        SELECT
            sba_legal_name_normalized AS k1,
            sba_borrstate             AS k2,
            sba_borrzip5              AS k3,
            COUNT(*)                  AS sba_fan_out
        FROM matched
        GROUP BY 1, 2, 3
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
            "Exact-equality JOIN on (entity_name_normalized, 2-letter US state, "
            "substr(postcode, 1, 5)). Applies _lib/entity_name_normalize.py v1.0.0 "
            "on both sides. SBA borrzip cleaned via _clean_zip5() to remove DOUBLE "
            "cast residue (.0 suffix) before substr."
        ),
    )
    register_match_method_version(
        method_name=METHOD_NAME,
        semver=METHOD_SEMVER,
        normalizer_module="_lib/entity_name_normalize.py",
        normalizer_version=NORMALIZER_VERSION,
        blacklist_module="_lib/entity_name_normalize.py",
        blacklist_version=NORMALIZER_VERSION,
        tier_rule_description=(
            "platinum=1:1; gold=1:N or N:1; silver=N:M ≤50; rejected=>50"
        ),
        rejection_rule_description="fan-out >50 on either side → rejected",
        input_columns_left=["legal_name_normalized", "borrstate", "borrzip"],
        input_columns_right=["name_normalized", "address_region", "address_postcode_5"],
        output_value_description=(
            "normalized name + 2-letter state + 5-digit zip join key"
        ),
    )
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "SBA borrowers × Overture US Places — composite-key exact match on "
            "(normalized_name, state, zip5). Single-path bridge (match_path='direct'). "
            "Attaches phone, website, email, brand_wikidata, operating_status to SBA "
            "borrower cohorts for Capital Expansion supply-side outreach."
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

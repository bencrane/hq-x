#!/usr/bin/env python3
"""Lance-emit: Overture US Places — 2026-04-15.0 release.

Reads `s3://dex-raw-landing-zone/overture/release=2026-04-15.0/theme=places/type=place/**/*.parquet`
via DuckDB + R2 httpfs (NO spatial extension — geometry column dropped per
validator finding: coords are scrambled in 2026-04-15.0 release).

Filters `addresses[1].country = 'US'` (DuckDB 1-based list indexing — verified
by validator on live data). Extracts a flat projection: scalar columns only,
no geometry/bbox/sources/taxonomy/categories.alternate/socials/version/release/theme/type.

Normalizes names.primary via the same SQL normalizer used by the SBA emit cycle
so the composite-key bridge join (s2) is symmetric. Also normalizes
addresses[1].freeform via `_lib.address_normalize.normalize_address_street`
(base form, unit-stripped) as the `address_base_normalized` column — pre-bakes
the address join token at emit time so downstream address-keyed bridges skip
the per-build Python pass on 15M+ rows.

Writes Lance to `s3://dex-raw-landing-zone/polaris-warehouse/overture/us_places_lance/`.
Uses `lance_commit_lock` + Arrow-bridge pattern (NOT lance-duckdb extension).

Volume floor: ≥12,000,000 rows (80% of 15,952,626 raw US-filtered rows per validator).

Cycle: overture-sba-borrower-bridge (s1).

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/emit_overture_us_places_lance.py --apply

  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/emit_overture_us_places_lance.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from scripts._lib.address_normalize import (  # noqa: E402
    __version__ as ADDR_NORMALIZER_VERSION,
    normalize_address_street,
    register_address_udf,
)
from scripts._lib.entity_name_normalize import __version__ as NORMALIZER_VERSION  # noqa: E402
from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("emit_overture_us_places_lance")

# Lance output URI
LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/overture/us_places_lance/"
DATASET_SLUG = "overture_us_places_lance"

# Overture release hard-coded for v1 (monthly releases; cron extension is out
# of scope per directive §"Out of scope"; re-emit is operator-manual on new release).
OVERTURE_RELEASE = "2026-04-15.0"
OVERTURE_GLOB = (
    f"r2://dex-raw-landing-zone/overture/release={OVERTURE_RELEASE}"
    "/theme=places/type=place/**/*.parquet"
)

# Row floor per directive §"Volume floors"
ROW_FLOOR = 12_000_000

TMP_DIR = "/tmp/lance"


def _r2_account_id() -> str:
    ep = os.environ["R2_ENDPOINT"]
    return ep.split("//")[-1].split(".")[0]


def _lance_storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _connect_duckdb_to_r2():
    import duckdb

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(
        f"""
        CREATE SECRET (
            TYPE r2,
            KEY_ID '{os.environ["R2_ACCESS_KEY_ID"]}',
            SECRET '{os.environ["R2_SECRET_ACCESS_KEY"]}',
            ACCOUNT_ID '{_r2_account_id()}'
        );
        """
    )
    # Register address normalizer UDF for the projection — bakes the
    # `address_base_normalized` column at emit time so downstream bridges
    # join on a stable token instead of re-running the Python regex per
    # build.  Matches `_lib.address_normalize.normalize_address_street`
    # (base form, unit-stripped).  `null_handling="special"` is REQUIRED:
    # DuckDB's default treats UDFs as not-returning-NULL, which trips on
    # the legitimate None outputs the normalizer emits for empty/generic
    # inputs.
    register_address_udf(con, fn_name="py_normalize_address_street")
    return con


def _normalize_entity_sql(raw_expr: str) -> str:
    """Apply entity_name_normalize.py v1.0.0 rule in SQL.

    MUST match emit_sba_loans_lance.py _normalize_entity_sql exactly
    (same suffix tokens, same regex order) so the s2 bridge JOIN-key
    normalization is symmetric.
    NORMALIZER_VERSION = {NORMALIZER_VERSION}
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


def _build_select_sql() -> str:
    """Build the flat-projection SELECT for the Overture US Places parquet.

    Dropped columns (deliberate, audit-decision):
      - geometry: scrambled coords in 2026-04-15.0 (validator finding); not
        load-bearing for the name+state+zip5 bridge join.
      - bbox: same root cause as geometry.
      - sources: deeply-nested STRUCT[] list, not load-bearing for the bridge.
      - taxonomy: nested struct, not load-bearing.
      - categories.alternate: list type, not load-bearing (keep categories.primary scalar).
      - socials: list, low-coverage, not load-bearing.
      - version / release / theme / type: constant or release-metadata.

    DuckDB 1-based list indexing: addresses[1], phones[1], websites[1], emails[1].
    brand.names.primary: DuckDB struct-field access; PRIMARY is NOT reserved in
    struct-field access context (reviewer A3 finding — verified on real data).
    """
    norm_name_sql = _normalize_entity_sql("names.primary")
    return f"""
SELECT
    id                                                            AS place_id,
    names.primary                                                 AS name_primary,
    ({norm_name_sql})                                             AS name_normalized,
    addresses[1].freeform                                         AS address_freeform,
    py_normalize_address_street(addresses[1].freeform)            AS address_base_normalized,
    addresses[1].locality                                         AS address_locality,
    addresses[1].postcode                                         AS address_postcode,
    substr(addresses[1].postcode, 1, 5)                           AS address_postcode_5,
    upper(addresses[1].region)                                    AS address_region,
    categories.primary                      AS categories_primary,
    phones[1]                               AS phone_primary,
    websites[1]                             AS website_primary,
    emails[1]                               AS email_primary,
    brand.wikidata                          AS brand_wikidata,
    brand.names.primary                     AS brand_name_primary,
    operating_status                        AS operating_status,
    confidence                              AS confidence
FROM read_parquet('{OVERTURE_GLOB}', union_by_name=false, hive_partitioning=true)
WHERE addresses[1].country = 'US'
"""


def _count_rows(con) -> int:
    logger.info("counting US-filtered Overture rows (dry-run) ...")
    sql = f"""
        SELECT COUNT(*)
        FROM read_parquet('{OVERTURE_GLOB}', union_by_name=false, hive_partitioning=true)
        WHERE addresses[1].country = 'US'
    """
    n = con.execute(sql).fetchone()[0]
    logger.info("  US rows: %d", n)
    return n


def _emit_lance(con, storage_options: dict) -> int:
    """Execute the SELECT and write to Lance via Arrow-bridge pattern."""
    import lance

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = TMP_DIR

    select_sql = _build_select_sql()

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        logger.info("reading Overture US rows via DuckDB ...")
        reader = con.from_query(select_sql).to_arrow_reader(batch_size=100_000)

        logger.info("writing to Lance at %s ...", LANCE_URI)
        ds = lance.write_dataset(
            reader,
            LANCE_URI,
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
            ds.create_scalar_index("place_id", index_type="BTREE", replace=True)
            logger.info("BTREE index created on place_id")
        except Exception as e:
            logger.warning("BTREE index failed (non-fatal): %s", e)
        try:
            ds.optimize.compact_files()
            logger.info("compact_files done")
        except Exception as e:
            logger.warning("compact_files failed (non-fatal): %s", e)
        try:
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as e:
            logger.warning("cleanup_old_versions failed (non-fatal): %s", e)

    return lance_count


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true", help="write Lance dataset")
    grp.add_argument("--dry-run", action="store_true", help="count rows only, no writes")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")

    logger.info(
        "emit_overture_us_places_lance  release=%s  normalizer=v%s",
        OVERTURE_RELEASE,
        NORMALIZER_VERSION,
    )
    logger.info("output: %s", LANCE_URI)
    logger.info("row_floor: %d", ROW_FLOOR)

    t0 = time.time()
    con = _connect_duckdb_to_r2()
    storage_options = _lance_storage_options()

    if args.dry_run:
        n = _count_rows(con)
        if n < ROW_FLOOR:
            logger.error(
                "DRY RUN HARD FAIL: row count %d < floor %d", n, ROW_FLOOR
            )
            return 1
        logger.info(
            "DRY RUN OK: %d rows >= floor %d  duration=%.1fs", n, ROW_FLOOR, time.time() - t0
        )
        return 0

    # --apply path
    try:
        lance_count = _emit_lance(con, storage_options)
    except Exception:
        logger.exception("Lance emit failed")
        return 1

    if lance_count < ROW_FLOOR:
        logger.error(
            "HARD FAIL: lance_count=%d < floor=%d", lance_count, ROW_FLOOR
        )
        return 1

    logger.info(
        "OK  lance_rows=%d  duration=%.1fs  output=%s",
        lance_count,
        time.time() - t0,
        LANCE_URI,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

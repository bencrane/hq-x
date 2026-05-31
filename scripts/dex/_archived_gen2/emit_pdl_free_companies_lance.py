#!/usr/bin/env python3
"""Lance-emit: PDL Free Company dataset (GLOBAL — all countries).

ONE-SHOT script — operator runs manually when PDL refreshes the dataset.
NOT in the daily cron (per directive: "PDL is manual-refresh only").

Reads `s3://dex-raw-landing-zone/pdl/free_company_dataset.parquet` (single
~2GB parquet file; ~35.4M rows globally). Computes
`legal_name_normalized` via the v1.0.0 SQL normalizer (same as
build_bridge_pdl_sba_borrower.py L141-L172). Maps PDL `region` (full state
name) → 2-letter state code via the inline CASE in `_STATE_MAP_SQL` —
**resolves to NULL for non-US rows**, which is now allowed (foreign-HQ
contractors registered on SAM.gov can match against their global PDL record
by domain regardless of state). Writes to Lance at
`s3://dex-raw-landing-zone/polaris-warehouse/pdl/free_companies_lance/`.

History: prior version was US-only via `WHERE LOWER(country) = 'united states'
AND region IS NOT NULL AND (_STATE_MAP_SQL) IS NOT NULL`. That dropped DHL
(germany), Siemens (germany), Airbus subs, etc. — every foreign-HQ federal
prime fell to slow lane because the bridge had nothing to match against.
Lifted in 2026-05-27.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance python \\
    apps/data-engine-x/scripts/emit_pdl_free_companies_lance.py --apply

  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance python \\
    apps/data-engine-x/scripts/emit_pdl_free_companies_lance.py --dry-run
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

from scripts._lib.entity_name_normalize import __version__ as NORMALIZER_VERSION  # noqa: E402
from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("emit_pdl_free_companies_lance")

R2_BUCKET = "dex-raw-landing-zone"
PDL_INPUT_KEY = "pdl/free_company_dataset.parquet"
LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/pdl/free_companies_lance/"
DATASET_SLUG = "pdl_free_companies_lance"
TMP_DIR = "/tmp/lance"

# Row floor per directive §"Volume floors". Global PDL is ~35.4M rows
# (US-only was ~9M); 25M floor is a sanity check against pipeline regressions
# while leaving headroom for upstream PDL trimming or filter drift.
ROW_FLOOR = 25_000_000


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
    return con


def _normalize_entity_sql(raw_expr: str) -> str:
    """SQL equivalent of entity_name_normalize.py v1.0.0.

    Matches build_bridge_pdl_sba_borrower.py L141-L172 EXACTLY.
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


# PDL `region` (full state name lowercased) → 2-letter state code.
# Copied verbatim from build_bridge_pdl_sba_borrower.py L178-L200.
_STATE_MAP_SQL = """
CASE LOWER(TRIM(region))
  WHEN 'alabama' THEN 'AL' WHEN 'alaska' THEN 'AK' WHEN 'arizona' THEN 'AZ'
  WHEN 'arkansas' THEN 'AR' WHEN 'california' THEN 'CA' WHEN 'colorado' THEN 'CO'
  WHEN 'connecticut' THEN 'CT' WHEN 'delaware' THEN 'DE' WHEN 'florida' THEN 'FL'
  WHEN 'georgia' THEN 'GA' WHEN 'hawaii' THEN 'HI' WHEN 'idaho' THEN 'ID'
  WHEN 'illinois' THEN 'IL' WHEN 'indiana' THEN 'IN' WHEN 'iowa' THEN 'IA'
  WHEN 'kansas' THEN 'KS' WHEN 'kentucky' THEN 'KY' WHEN 'louisiana' THEN 'LA'
  WHEN 'maine' THEN 'ME' WHEN 'maryland' THEN 'MD' WHEN 'massachusetts' THEN 'MA'
  WHEN 'michigan' THEN 'MI' WHEN 'minnesota' THEN 'MN' WHEN 'mississippi' THEN 'MS'
  WHEN 'missouri' THEN 'MO' WHEN 'montana' THEN 'MT' WHEN 'nebraska' THEN 'NE'
  WHEN 'nevada' THEN 'NV' WHEN 'new hampshire' THEN 'NH' WHEN 'new jersey' THEN 'NJ'
  WHEN 'new mexico' THEN 'NM' WHEN 'new york' THEN 'NY' WHEN 'north carolina' THEN 'NC'
  WHEN 'north dakota' THEN 'ND' WHEN 'ohio' THEN 'OH' WHEN 'oklahoma' THEN 'OK'
  WHEN 'oregon' THEN 'OR' WHEN 'pennsylvania' THEN 'PA' WHEN 'rhode island' THEN 'RI'
  WHEN 'south carolina' THEN 'SC' WHEN 'south dakota' THEN 'SD' WHEN 'tennessee' THEN 'TN'
  WHEN 'texas' THEN 'TX' WHEN 'utah' THEN 'UT' WHEN 'vermont' THEN 'VT'
  WHEN 'virginia' THEN 'VA' WHEN 'washington' THEN 'WA' WHEN 'west virginia' THEN 'WV'
  WHEN 'wisconsin' THEN 'WI' WHEN 'wyoming' THEN 'WY' WHEN 'district of columbia' THEN 'DC'
  WHEN 'puerto rico' THEN 'PR' WHEN 'virgin islands' THEN 'VI' WHEN 'guam' THEN 'GU'
  ELSE NULL
END
"""


def _emit(dry_run: bool) -> int:
    import lance

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = TMP_DIR

    logger.info("=" * 60)
    logger.info("emit_pdl_free_companies_lance — NORMALIZER_VERSION=%s", NORMALIZER_VERSION)
    logger.info("input:  r2://%s/%s", R2_BUCKET, PDL_INPUT_KEY)
    logger.info("output: %s", LANCE_URI)

    con = _connect_duckdb_to_r2()
    pdl_uri = f"r2://{R2_BUCKET}/{PDL_INPUT_KEY}"

    norm_name = _normalize_entity_sql("name")

    # Global filter contract (post-2026-05-27): require only that the
    # record has a non-null name that normalizes to a usable token. Country,
    # region, and state-resolution are NOT filters anymore — non-US rows
    # carry NULL state and that's fine, the SAM↔PDL domain join doesn't
    # depend on state. This is the single change that lets foreign-HQ
    # federal primes (DHL, Siemens, etc.) match through the bridge.
    count_sql = f"""
    SELECT COUNT(*) FROM read_parquet('{pdl_uri}')
    WHERE name IS NOT NULL
      AND ({norm_name}) IS NOT NULL
    """

    logger.info("counting global rows (name+normalized-name non-null) ...")
    total_count = con.execute(count_sql).fetchone()[0]
    logger.info("global rows: %d", total_count)

    if dry_run:
        logger.info("DRY RUN — rows=%d (floor=%d, pass=%s)",
                    total_count, ROW_FLOOR, total_count >= ROW_FLOOR)
        return 0 if total_count >= ROW_FLOOR else 1

    if total_count < ROW_FLOOR:
        logger.error("FAIL: total_count=%d < floor=%d", total_count, ROW_FLOOR)
        return 1

    output_sql = f"""
    SELECT
        id                                  AS pdl_id,
        name                                AS pdl_name,
        ({norm_name})                       AS legal_name_normalized,
        ({_STATE_MAP_SQL})                  AS state,
        website                             AS pdl_website,
        linkedin_url                        AS pdl_linkedin_url,
        industry                            AS pdl_industry,
        size                                AS pdl_size,
        cast(founded AS VARCHAR)            AS pdl_founded,
        locality                            AS pdl_locality,
        country                             AS pdl_country,
        region                              AS pdl_region_raw
    FROM read_parquet('{pdl_uri}')
    WHERE name IS NOT NULL
      AND ({norm_name}) IS NOT NULL
    """

    storage_options = _lance_storage_options()
    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        reader = con.from_query(output_sql).to_arrow_reader(batch_size=100_000)
        logger.info("writing PDL companies to Lance (mode=overwrite) ...")
        ds = lance.write_dataset(
            reader,
            LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t0
        lance_count = ds.count_rows()
        logger.info("wrote %d rows in %.1fs (version=%s)", lance_count, write_dur, ds.version)

        os.environ["LANCE_BYPASS_SPILLING"] = "true"
        try:
            ds.create_scalar_index("legal_name_normalized", index_type="BTREE", replace=True)
        except Exception as e:
            logger.error("BTREE index FAILED: %s", e)
            raise

        try:
            ds.optimize.compact_files()
        except Exception as e:
            logger.warning("compact_files failed (non-fatal): %s", e)
        try:
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as e:
            logger.warning("cleanup_old_versions failed (non-fatal): %s", e)

    logger.info("=" * 60)
    logger.info("OK — PDL companies written: %d", lance_count)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Lance emit: PDL Free Companies (GLOBAL — all countries)")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true")
    grp.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            logger.error("FAIL: %s not set", var)
            return 64

    return _emit(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())

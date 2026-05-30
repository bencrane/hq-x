"""Materialize the SAM-active mid-tier cohort with NO PDL/LinkedIn coverage.

The Parallel.ai enrichment agent consumes this Lance dataset directly — it
contains every SAM-registered UEI that:
  - has 365d (prime + sub) federal $ activity in [$100K, $25M] (the
    addressable mid-tier — drops the noise tail under $100K and the giants
    above $25M)
  - is NOT in bridges/sam_pdl_lance (no LinkedIn URL via the existing
    SAM × PDL domain bridge — needs Blitz / Parallel.ai direct lookup)

Sizing probe (2026-05-27): ~32,227 UEIs / ~$59.2B 365d obligation.

Sources
-------
  - usaspending/recipient_grain_lance     — per-UEI 30/90/180/365d prime totals
  - usaspending/subaward_lance            — full sub-grain transactions
  - sam_gov/entities_lance                — SAM-registered universe + firmographics
  - bridges/sam_pdl_lance                 — UEIs we already have LinkedIn for

Output
------
  s3://dex-raw-landing-zone/polaris-warehouse/cohorts/sam_active_no_pdl_midtier_lance
  BTREE on uei. Polaris-registered under cohorts namespace.

Schema
------
  uei                     string  (BTREE)
  legal_business_name     string
  state                   string  (2-letter)
  city                    string
  entity_url              string  (raw — verbatim from SAM)
  entity_url_normalized   string  (canonical: lower, strip http(s)://, strip
                                   www., strip path/query/fragment — matches
                                   the SQL chain in build_bridge_sam_pdl_domain_lance
                                   so it can be joined directly to PDL's
                                   normalized website downstream)
  primary_naics           string
  bus_type_string         string
  prime_365d              double
  sub_365d                double
  total_365d              double
  role                    string  (prime_only | sub_only | both)
  cohort_version          string
  generated_at            timestamp

Usage
-----
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow python \\
    apps/data-engine-x/scripts/build_cohort_sam_active_no_pdl_midtier_lance.py --apply

  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow python \\
    apps/data-engine-x/scripts/build_cohort_sam_active_no_pdl_midtier_lance.py --dry-run
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

from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402
from scripts._lib.catalog_hooks import register_or_update_polaris  # noqa: E402

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("build_cohort_sam_active_no_pdl_midtier_lance")


COHORT_SLUG = "sam_active_no_pdl_midtier_lance"
COHORT_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/cohorts/sam_active_no_pdl_midtier_lance"
)
COHORT_VERSION = "1.0.0"

RG_URI = "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/recipient_grain_lance"
SUB_URI = "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/subaward_lance"
SAM_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/entities_lance"
SAM_PDL_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sam_pdl_lance"

# Filter band — mid-tier "addressable non-giants" cut. Below $100K is the
# noise tail (small purchase orders, ad-hoc one-shots — low signal for
# LinkedIn enrichment). Above $25M is the giant tier (mega-primes, already
# well-tracked elsewhere — separately addressable).
TOTAL_FLOOR_USD = 100_000
TOTAL_CEILING_USD = 25_000_000

# Window for "active" — 365 days from today. Matches the recipient_grain
# pre-aggregation window; for subawards the same calendar window is applied
# at scan time via sub_action_date pushdown.
ACTIVE_WINDOW_DAYS = 365

# Row-count floor — sizing probe was 32,227. Catch a catastrophically broken
# build while tolerating snapshot drift.
MIN_ROWS = 25_000

TMP_DIR = "/tmp/lance"


def _r2_storage_options() -> dict[str, str]:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _materialize_inputs(storage_options: dict) -> tuple:
    """Read all four Lance sources via Arrow-bridge."""
    import lance
    import pyarrow.compute as pc
    from datetime import date

    cutoff = (date.today() - timedelta(days=ACTIVE_WINDOW_DAYS)).isoformat()

    logger.info("opening %s ...", RG_URI)
    rg = lance.dataset(RG_URI, storage_options=storage_options).to_table(
        columns=["recipient_uei", "total_obligation_365d"]
    )
    logger.info("  recipient_grain rows: %d", rg.num_rows)

    logger.info("opening %s (filter sub_action_date >= %s) ...", SUB_URI, cutoff)
    sub_ds = lance.dataset(SUB_URI, storage_options=storage_options)
    sub = sub_ds.scanner(
        columns=["sub_awardee_or_recipient_uei", "subaward_amount", "sub_action_date"],
        filter=pc.field("sub_action_date") >= cutoff,
    ).to_table()
    logger.info("  subaward rows in window: %d", sub.num_rows)

    logger.info("opening %s ...", SAM_URI)
    sam = lance.dataset(SAM_URI, storage_options=storage_options).to_table(
        columns=[
            "unique_entity_id",
            "legal_business_name",
            "physical_address_state_normalized",
            "physical_address_city",
            "entity_url",
            "primary_naics",
            "bus_type_string",
        ]
    )
    logger.info("  sam rows: %d", sam.num_rows)

    logger.info("opening %s ...", SAM_PDL_URI)
    sam_pdl = lance.dataset(SAM_PDL_URI, storage_options=storage_options).to_table(
        columns=["uei"]
    )
    logger.info("  sam_pdl rows: %d", sam_pdl.num_rows)

    return rg, sub, sam, sam_pdl


def _build_cohort(rg, sub, sam, sam_pdl, generated_at_iso: str):
    """Run the join + filter, return DuckDB relation for cohort rows."""
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=8")
    con.execute("SET memory_limit='16GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET preserve_insertion_order=false")

    con.register("rg", rg)
    con.register("sub", sub)
    con.register("sam_raw", sam)
    con.register("sam_pdl", sam_pdl)

    # Per-UEI prime 365d (typed)
    con.execute(
        """
        CREATE TEMP TABLE prime_by_uei AS
        SELECT recipient_uei AS uei,
               CAST(total_obligation_365d AS DOUBLE) AS prime_365d
        FROM rg
        WHERE recipient_uei IS NOT NULL
          AND CAST(total_obligation_365d AS DOUBLE) > 0
        """
    )

    # Per-UEI sub 365d (filtered + summed)
    con.execute(
        """
        CREATE TEMP TABLE sub_by_uei AS
        SELECT sub_awardee_or_recipient_uei AS uei,
               SUM(TRY_CAST(subaward_amount AS DOUBLE)) AS sub_365d
        FROM sub
        WHERE sub_awardee_or_recipient_uei IS NOT NULL
          AND sub_awardee_or_recipient_uei <> ''
          AND TRY_CAST(subaward_amount AS DOUBLE) > 0
        GROUP BY 1
        """
    )

    # Dedup SAM on uei (one row per UEI, prefer non-null entity_url)
    con.execute(
        """
        CREATE TEMP TABLE sam AS
        SELECT unique_entity_id AS uei,
               legal_business_name,
               physical_address_state_normalized AS state,
               physical_address_city             AS city,
               entity_url,
               primary_naics,
               bus_type_string
        FROM (
          SELECT *,
                 ROW_NUMBER() OVER (
                   PARTITION BY unique_entity_id
                   ORDER BY entity_url IS NOT NULL DESC, entity_url
                 ) AS rn
          FROM sam_raw
          WHERE unique_entity_id IS NOT NULL
        ) WHERE rn = 1
        """
    )

    # Combine prime + sub per UEI, then apply mid-tier filter + SAM join + anti-join sam_pdl
    con.execute(
        f"""
        CREATE TEMP TABLE cohort AS
        WITH totals AS (
          SELECT COALESCE(p.uei, s.uei)                              AS uei,
                 COALESCE(p.prime_365d, 0)                           AS prime_365d,
                 COALESCE(s.sub_365d, 0)                             AS sub_365d,
                 COALESCE(p.prime_365d, 0) + COALESCE(s.sub_365d, 0) AS total_365d
          FROM prime_by_uei p
          FULL OUTER JOIN sub_by_uei s ON s.uei = p.uei
        )
        SELECT
          t.uei,
          sam.legal_business_name,
          sam.state,
          sam.city,
          sam.entity_url,
          -- Canonical domain normalization: lower → strip http(s):// →
          -- strip www. → strip path/query/fragment. Identical SQL chain to
          -- build_bridge_sam_pdl_domain_lance._normalize_domain_sql and
          -- build_cohort_primes_90d_lance, so this column joins directly
          -- to PDL's normalized website without re-normalization drift.
          NULLIF(
            regexp_replace(
              regexp_replace(
                regexp_replace(
                  lower(trim(sam.entity_url)),
                  '^https?://', ''
                ),
                '^www\\.', ''
              ),
              '[/?#].*$', ''
            ),
            ''
          )                                                          AS entity_url_normalized,
          sam.primary_naics,
          sam.bus_type_string,
          t.prime_365d,
          t.sub_365d,
          t.total_365d,
          CASE
            WHEN t.prime_365d > 0 AND t.sub_365d > 0 THEN 'both'
            WHEN t.prime_365d > 0                    THEN 'prime_only'
            ELSE                                          'sub_only'
          END                                                        AS role,
          '{COHORT_VERSION}'                                         AS cohort_version,
          TIMESTAMP '{generated_at_iso}'                             AS generated_at
        FROM totals t
        JOIN sam ON sam.uei = t.uei
        LEFT JOIN sam_pdl sp ON sp.uei = t.uei
        WHERE sp.uei IS NULL
          AND t.total_365d >= {TOTAL_FLOOR_USD}
          AND t.total_365d <= {TOTAL_CEILING_USD}
        """
    )

    n = con.execute("SELECT COUNT(*) FROM cohort").fetchone()[0]
    logger.info("cohort row count: %d", n)
    summary = con.execute(
        """
        SELECT
          COUNT(*)                                             AS n,
          SUM(prime_365d)                                      AS prime_sum,
          SUM(sub_365d)                                        AS sub_sum,
          SUM(total_365d)                                      AS total_sum,
          COUNT(*) FILTER (WHERE role='prime_only')            AS n_prime_only,
          COUNT(*) FILTER (WHERE role='sub_only')              AS n_sub_only,
          COUNT(*) FILTER (WHERE role='both')                  AS n_both,
          COUNT(*) FILTER (WHERE entity_url IS NOT NULL
                                 AND trim(entity_url) <> '')   AS n_with_url_raw,
          COUNT(*) FILTER (WHERE entity_url_normalized IS NOT NULL) AS n_with_url_norm
        FROM cohort
        """
    ).fetchone()
    logger.info("  by role:  prime_only=%d  sub_only=%d  both=%d",
                summary[4], summary[5], summary[6])
    logger.info("  $ totals: prime=$%s  sub=$%s  total=$%s",
                f"{summary[1]:,.0f}", f"{summary[2]:,.0f}", f"{summary[3]:,.0f}")
    logger.info("  rows with non-null entity_url (raw):        %d (%.1f%%)",
                summary[7], 100 * summary[7] / summary[0] if summary[0] else 0)
    logger.info("  rows with non-null entity_url_normalized:   %d (%.1f%%)",
                summary[8], 100 * summary[8] / summary[0] if summary[0] else 0)

    return con, n


def _write_lance(con, storage_options: dict) -> int:
    """Write cohort to Lance + BTREE on uei + compact/cleanup."""
    import lance

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")
    os.environ["TMPDIR"] = TMP_DIR

    t0 = time.time()
    with lance_commit_lock(COHORT_SLUG):
        logger.info("writing cohort to Lance at %s ...", COHORT_URI)
        reader = con.from_query("SELECT * FROM cohort").to_arrow_reader(
            batch_size=50_000
        )
        ds = lance.write_dataset(
            reader,
            COHORT_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t0
        row_count = ds.count_rows()
        logger.info("wrote %d rows in %.1fs (version=%s)",
                    row_count, write_dur, ds.version)

        try:
            ds.create_scalar_index("uei", index_type="BTREE", replace=True)
            logger.info("BTREE on uei: OK")
        except Exception as e:
            logger.error("BTREE on uei FAILED: %s", e)
            raise

        try:
            ds.optimize.compact_files()
        except Exception as e:
            logger.warning("compact_files failed (non-fatal): %s", e)
        try:
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as e:
            logger.warning("cleanup_old_versions failed (non-fatal): %s", e)

    return row_count


def _register_polaris() -> None:
    register_or_update_polaris(
        namespace="cohorts",
        table_name=COHORT_SLUG,
        s3_uri=COHORT_URI.rstrip("/") + "/",
        docstring=(
            "SAM-registered UEIs with 365d federal $ activity in [$100K, $25M] "
            "and NO PDL LinkedIn coverage (anti-joined against sam_pdl_lance). "
            "The Parallel.ai enrichment agent's primary target list — every row "
            "is a federally-active mid-tier contractor we currently can't reach "
            "via PDL domain matching. Sized ~32K UEIs / ~$59B annual obligation."
        ),
    )
    logger.info("Polaris registration: cohorts.%s OK", COHORT_SLUG)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true")
    grp.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")

    started_at = datetime.now(tz=timezone.utc)
    t0 = time.time()
    storage_options = _r2_storage_options()

    logger.info("cohort: %s v%s", COHORT_SLUG, COHORT_VERSION)
    logger.info("filter: total_365d ∈ [$%s, $%s], NOT in sam_pdl_lance",
                f"{TOTAL_FLOOR_USD:,}", f"{TOTAL_CEILING_USD:,}")
    logger.info("output: %s", COHORT_URI)

    rg, sub, sam, sam_pdl = _materialize_inputs(storage_options)
    con, n = _build_cohort(rg, sub, sam, sam_pdl, started_at.isoformat())

    if n < MIN_ROWS:
        msg = f"HARD FAIL: cohort rows={n:,} < floor={MIN_ROWS:,}"
        logger.error(msg)
        return 1

    if args.dry_run:
        logger.info("DRY RUN — no Lance / Polaris writes. duration=%.1fs", time.time() - t0)
        return 0

    row_count = _write_lance(con, storage_options)
    _register_polaris()
    logger.info("OK — duration=%.1fs", time.time() - t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Overture × PDL companies via website domain (Pattern B Lance bridge).

Resolves Overture US Places to PDL company records via exact-equality on the
normalized website domain. Outputs a Lance dataset at
polaris-warehouse/bridges/overture_pdl_domain_lance with PDL firmographics
(name, website, industry, size, locality, LinkedIn URL) keyed on
normalized_domain plus Overture place evidence (ov_us_place_count,
ov_us_state_set).

Use case — second hop of the SAM-rescue chain. SAM UEIs with
entity_url IS NULL get a rescued website via sam_overture_lance (address +
name); this bridge then resolves that website to PDL firmographics:

    sam.entities_lance (entity_url IS NULL)
      → bridges.sam_overture_lance               (rescues ov_website_primary)
        → bridges.overture_pdl_domain_lance      (this bridge)
          → pdl.free_companies_lance             (linkedin_url, industry, size)

Pattern B per inventory/DATA-FACTORY-ARCHITECTURE-PATTERNS.md:
  - inputs: overture/us_places_lance + pdl/free_companies_lance
  - method: domain_exact v1.0.0 (REUSED per L21 — also drives FMCSA-PDL,
    SAM-PDL, SBIC-PDL, SEC-ADV-PDL) → DO NOT call register_match_method or
    register_match_method_version (would clobber other bridges' provenance)
  - Overture is pre-aggregated to (normalized_domain, ov_us_place_count,
    ov_us_state_set) BEFORE the join. This collapses places-per-domain to a
    passenger count, so Overture-side fan-out is always 1 by construction.
    Only PDL-side fan-out matters for collision rejection.
  - free-mail blacklist applied (per L10) as defense — Overture
    website_primary can carry user-input urls including gmail.com etc.
  - tiers (post-pre-agg, since LEFT fan-out ≡ 1):
      platinum = pdl_companies_at_domain = 1
      silver   = pdl_companies_at_domain > 1 AND ≤ COLLISION_THRESHOLD
      rejected = pdl_companies_at_domain > COLLISION_THRESHOLD (50)
    Brand/chain domains (mcdonalds.com etc.) are deliberately dropped — they
    fan out to thousands of Overture places but, more importantly, fan out to
    many PDL parent/subsidiary rows; rejection is correct here. SAM UEIs are
    overwhelmingly independent businesses, not franchise locations.
  - BTREEs on normalized_domain, pdl_company_id, match_value.

Usage:
  cd apps/data-engine-x && doppler run --project hq-all --config prd -- \\
      uv run python scripts/build_bridge_overture_pdl_domain_lance.py --apply

  ... --dry-run for counts only.
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

from scripts._lib.free_mail_domains import FREE_MAIL_DOMAINS  # noqa: E402
from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402
from scripts._lib.match_method_registry import (  # noqa: E402
    complete_bridge_run,
    fail_bridge_run,
    register_bridge,
    start_bridge_run,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("build_bridge_overture_pdl_domain_lance")

# Bridge identity ------------------------------------------------------------
BRIDGE_NAME = "overture_pdl_domain_lance"
METHOD_NAME = "domain_exact"
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "1.0.0"

SOURCE_LEFT = "overture_us_places_lance"
SOURCE_RIGHT = "pdl_free_companies_lance"

# R2 layout ------------------------------------------------------------------
OVERTURE_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/overture/us_places_lance"
)
PDL_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/pdl/free_companies_lance"
)
BRIDGE_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/overture_pdl_domain_lance"
)
DATASET_SLUG = "overture_pdl_domain_lance"

# Tier thresholds + floor ----------------------------------------------------
COLLISION_THRESHOLD = 50
# Floor: validator probe (2026-05-27) — Overture has 13.58M places with a
# website_primary collapsing to 6.57M distinct valid normalized domains;
# PDL has 22.99M rows with pdl_website collapsing to 22.38M distinct valid
# normalized domains. Domain-grain intersection: 2,379,839. PDL-side fan-out
# distribution at the intersection:
#   platinum (1:1):       2,377,988 matched rows (domains: 2,377,988)
#   silver   (1:N ≤50):       8,543 matched rows (domains: 1,664)
#   rejected (1:N >50):     478,613 matched rows (domains:   187 brands)
#   --
#   total matched:        2,386,531 (platinum+silver)
# Floor at 0.63 × measured = 1,500,000. Tightening past this would risk
# false failures on small upstream drift in PDL or Overture refresh cycles.
MIN_ROWS_MATCHED = 1_500_000
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


def _normalize_domain_sql(raw_expr: str) -> str:
    """Domain normalization SQL — IDENTICAL to SAM-PDL / FMCSA-PDL / SBIC-PDL."""
    return (
        f"regexp_replace("
        f"regexp_replace("
        f"regexp_replace("
        f"lower(trim({raw_expr})), '^https?://', ''"
        f"), '^www\\.', ''"
        f"), '/.*$', '')"
    )


def _domain_validation_sql(col: str) -> str:
    return (
        f"{col} ~ '^[a-z0-9]([a-z0-9.-]*[a-z0-9])?\\.[a-z]{{2,}}$' "
        f"AND NOT ({col} ~ '^[0-9.]+$')"
    )


def _materialize_inputs(storage_options: dict) -> tuple:
    """Read Overture + PDL Lance datasets via PyLance scanner with projection."""
    import lance
    import pyarrow.compute as pc

    logger.info("opening overture/us_places_lance ...")
    ov_ds = lance.dataset(OVERTURE_LANCE_URI, storage_options=storage_options)
    ov_arrow = ov_ds.scanner(
        columns=[
            "place_id",
            "website_primary",
            "address_region",
        ],
        filter=pc.field("website_primary").is_valid(),
    ).to_table()
    rows_ov = len(ov_arrow)
    logger.info(
        "  overture places (website_primary IS NOT NULL): %d rows", rows_ov
    )

    logger.info("opening pdl/free_companies_lance ...")
    pdl_ds = lance.dataset(PDL_LANCE_URI, storage_options=storage_options)
    pdl_arrow = pdl_ds.scanner(
        columns=[
            "pdl_id",
            "pdl_name",
            "pdl_website",
            "pdl_industry",
            "pdl_size",
            "pdl_locality",
            "pdl_linkedin_url",
        ],
        filter=pc.field("pdl_website").is_valid(),
    ).to_table()
    rows_pdl = len(pdl_arrow)
    logger.info("  pdl free_companies_lance: %d rows", rows_pdl)

    return ov_arrow, pdl_arrow, rows_ov, rows_pdl


def _build_match_table(
    ov_arrow,
    pdl_arrow,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
) -> tuple:
    """Normalize → pre-agg Overture → tiered join. Returns (con, counts)."""
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET preserve_insertion_order=false")
    con.register("ov_raw", ov_arrow)
    con.register("pdl_raw", pdl_arrow)

    free_mail_csv = ",".join(f"'{d}'" for d in sorted(FREE_MAIL_DOMAINS))
    ov_domain_expr = _normalize_domain_sql("website_primary")
    pdl_domain_expr = _normalize_domain_sql("pdl_website")
    validate_norm = _domain_validation_sql("normalized_domain")

    # Overture LEFT: normalize → blacklist → validate → pre-agg to distinct
    # normalized_domain. Place fan-out becomes a passenger count, not a join
    # source. State set kept as pipe-delimited VARCHAR per Lance 1.5.x
    # definition-buffer-cap convention (CLAUDE.md §Volume-King Lance emit).
    logger.info("materializing overture_branded (pre-aggregated by domain) ...")
    con.execute(
        f"""
        CREATE TEMP TABLE overture_normalized AS
        SELECT
            place_id,
            address_region AS state,
            {ov_domain_expr} AS normalized_domain
        FROM ov_raw
        WHERE website_primary IS NOT NULL
          AND TRIM(website_primary) != ''
        """
    )
    rows_ov_normalized = con.execute(
        "SELECT count(*) FROM overture_normalized WHERE normalized_domain IS NOT NULL"
    ).fetchone()[0]
    logger.info("  overture rows after normalize: %s", f"{rows_ov_normalized:,}")

    con.execute(
        f"""
        CREATE TEMP TABLE overture_branded AS
        SELECT
            normalized_domain,
            count(*)                                                  AS ov_us_place_count,
            string_agg(DISTINCT upper(state), '|' ORDER BY upper(state))
                FILTER (WHERE state IS NOT NULL AND state != '')      AS ov_us_state_set
        FROM overture_normalized
        WHERE normalized_domain IS NOT NULL
          AND NOT (normalized_domain IN ({free_mail_csv}))
          AND {validate_norm}
        GROUP BY normalized_domain
        """
    )
    rows_ov_branded = con.execute(
        "SELECT count(*) FROM overture_branded"
    ).fetchone()[0]
    # domains_blacklisted = distinct domains dropped by free-mail blacklist OR
    # validation predicate. Computed as (pre-filter distinct) − (post-filter
    # distinct). Subtracting from rows_ov_normalized would conflate place
    # fan-out with the filter drop, since overture_branded is GROUP BY.
    ov_pre_filter_distinct = con.execute(
        """
        SELECT count(*) FROM (
            SELECT DISTINCT normalized_domain
            FROM overture_normalized
            WHERE normalized_domain IS NOT NULL
        )
        """
    ).fetchone()[0]
    ov_blacklist = ov_pre_filter_distinct - rows_ov_branded
    logger.info(
        "  overture_branded distinct domains: %s (filtered out: %s of %s pre-filter distinct)",
        f"{rows_ov_branded:,}",
        f"{ov_blacklist:,}",
        f"{ov_pre_filter_distinct:,}",
    )

    # PDL RIGHT: normalize + validate. PDL is already at one-row-per-company
    # grain; multiple companies CAN share a domain (parent/sub/holding co),
    # so collision rejection still applies on this side.
    con.execute(
        f"""
        CREATE TEMP TABLE pdl_validated AS
        WITH pdl AS (
            SELECT
                pdl_id AS pdl_company_id,
                pdl_name,
                pdl_website AS pdl_website_raw,
                {pdl_domain_expr} AS normalized_domain,
                pdl_industry,
                pdl_size AS pdl_size_bucket,
                pdl_locality,
                pdl_linkedin_url
            FROM pdl_raw
            WHERE pdl_website IS NOT NULL
        )
        SELECT *
          FROM pdl
         WHERE normalized_domain IS NOT NULL
           AND {validate_norm}
        """
    )
    rows_pdl_valid = con.execute(
        "SELECT count(*) FROM pdl_validated"
    ).fetchone()[0]
    logger.info("  pdl_validated: %s", f"{rows_pdl_valid:,}")

    logger.info("computing per-domain PDL fan-out + tiered join ...")
    con.execute(
        """
        CREATE TEMP TABLE pdl_fanout AS
        SELECT normalized_domain, count(*) AS pdl_companies_at_domain
        FROM pdl_validated
        GROUP BY normalized_domain
        """
    )
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            o.normalized_domain,
            o.ov_us_place_count,
            o.ov_us_state_set,
            p.pdl_company_id,
            p.pdl_name,
            p.pdl_website_raw                              AS pdl_website,
            p.pdl_industry,
            p.pdl_size_bucket,
            p.pdl_locality,
            p.pdl_linkedin_url,
            '{METHOD_NAME}'                                AS match_method,
            o.normalized_domain                            AS match_value,
            pf.pdl_companies_at_domain,
            CASE
                WHEN pf.pdl_companies_at_domain > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN pf.pdl_companies_at_domain = 1
                    THEN 'platinum'
                ELSE 'silver'
            END                                            AS confidence_tier,
            TIMESTAMP '{generated_at_iso}'                 AS generated_at,
            '{BRIDGE_VERSION}'                             AS bridge_version,
            '{bridge_run_id}'                              AS bridge_run_id
        FROM overture_branded o
        JOIN pdl_validated   p USING (normalized_domain)
        JOIN pdl_fanout     pf USING (normalized_domain)
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE bridge_match AS
        SELECT *
        FROM bridge_all
        WHERE confidence_tier <> 'rejected'
        """
    )

    row_counts = con.execute(
        """
        SELECT
            count(*)                                            AS rows_matched,
            count(*) FILTER (WHERE confidence_tier = 'platinum') AS rows_tier1,
            count(*) FILTER (WHERE confidence_tier = 'silver')   AS rows_tier3
        FROM bridge_match
        """
    ).fetchone()
    rejected = con.execute(
        "SELECT count(*) FROM bridge_all WHERE confidence_tier = 'rejected'"
    ).fetchone()[0]

    counts = {
        "rows_matched": row_counts[0],
        "rows_tier1": row_counts[1],
        "rows_tier2": 0,  # No gold tier — Overture pre-agg makes 1:N|N:1 collapse to platinum/silver.
        "rows_tier3": row_counts[2],
        "rows_collision_rejected": rejected,
        "domains_blacklisted": ov_blacklist,
    }
    return con, counts


def _write_bridge_lance(con, storage_options: dict) -> int:
    import lance

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = TMP_DIR
    os.environ["LANCE_BYPASS_SPILLING"] = "true"

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        logger.info("writing bridge Lance at %s ...", BRIDGE_LANCE_URI)
        reader = con.from_query("SELECT * FROM bridge_match").to_arrow_reader(
            batch_size=10_000
        )
        ds = lance.write_dataset(
            reader,
            BRIDGE_LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        lance_count = ds.count_rows()
        logger.info(
            "wrote %d rows in %.1fs (version=%s)",
            lance_count, time.time() - t0, ds.version,
        )

        for col in ("normalized_domain", "pdl_company_id", "match_value"):
            try:
                ds.create_scalar_index(col, index_type="BTREE", replace=True)
                logger.info("  BTREE on %s: OK", col)
            except Exception as e:
                logger.warning("BTREE on %s non-fatal: %s", col, e)
        try:
            ds.optimize.compact_files()
        except Exception as e:
            logger.warning("compact_files non-fatal: %s", e)
        try:
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as e:
            logger.warning("cleanup_old_versions non-fatal: %s", e)

    return lance_count


def _ensure_registry() -> None:
    """register_bridge ONLY — domain_exact v1.0.0 is shared (L21)."""
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "Overture US Places × PDL free_companies via website domain. "
            "Overture pre-aggregated to one row per normalized_domain "
            "(places-per-domain becomes a passenger count). Resolves a "
            "website to PDL firmographics + LinkedIn URL. Free-mail "
            "blacklist applied as defense. Second hop of the SAM-rescue "
            "chain: sam → sam_overture_lance → overture_pdl_domain_lance → "
            "pdl.free_companies_lance."
        ),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true",
                     help="write Lance + ledger row")
    grp.add_argument("--dry-run", action="store_true",
                     help="count only, no writes")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")
    if args.apply and not os.environ.get("DEX_DB_URL_DIRECT"):
        raise SystemExit("FAIL: DEX_DB_URL_DIRECT not set (required for registry)")

    started_at = datetime.now(tz=timezone.utc)
    t0 = time.time()
    storage_options = _lance_storage_options()

    logger.info("bridge: %s (method=%s v%s)",
                BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER)
    logger.info("inputs: %s + %s", OVERTURE_LANCE_URI, PDL_LANCE_URI)
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
        ov_arrow, pdl_arrow, rows_ov, rows_pdl = _materialize_inputs(
            storage_options
        )
        con, counts = _build_match_table(
            ov_arrow,
            pdl_arrow,
            bridge_run_id=bridge_run_id,
            generated_at_iso=started_at.isoformat(),
        )

        logger.info("-" * 60)
        logger.info("bridge tier distribution:")
        logger.info("  rows_matched:           %s", f"{counts['rows_matched']:,}")
        logger.info("    platinum (1:1):       %s", f"{counts['rows_tier1']:,}")
        logger.info(
            "    silver   (1:N <=%d):  %s",
            COLLISION_THRESHOLD, f"{counts['rows_tier3']:,}",
        )
        logger.info(
            "  rows_collision_rejected: %s",
            f"{counts['rows_collision_rejected']:,}",
        )
        logger.info(
            "  domains_blacklisted (overture): %s",
            f"{counts['domains_blacklisted']:,}",
        )

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
            logger.info("DRY RUN — no writes. duration=%.1fs", time.time() - t0)
            return 0

        lance_count = _write_bridge_lance(con, storage_options)
        complete_bridge_run(
            run_uuid,
            metrics={
                "rows_left": rows_ov,
                "rows_right": rows_pdl,
                "rows_matched": counts["rows_matched"],
                "rows_tier1": counts["rows_tier1"],
                "rows_tier2": counts["rows_tier2"],
                "rows_tier3": counts["rows_tier3"],
                "rows_collision_rejected": counts["rows_collision_rejected"],
                "domains_blacklisted": counts["domains_blacklisted"],
            },
        )
        logger.info(
            "OK — run_id=%s lance_rows=%d duration=%.1fs",
            bridge_run_id, lance_count, time.time() - t0,
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

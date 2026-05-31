#!/usr/bin/env python3
"""DuckDB-on-R2 bridge generator: Entrepreneur.com franchisors × PDL companies
via domain (primary, high-precision) + name (fallback for domain-NULL rows).

Each franchisor in `entities.source_franchisors_entrepreneur` has a brand
name, an Entrepreneur.com URL slug, location count, min-investment-USD, and
~79% of them have an official_domain. PDL Free Company Dataset has ~9M US
companies with website + LinkedIn + industry + size + founded.

Two-pass match strategy:
  1. DOMAIN match (high-precision): normalize(franchisor.official_domain) =
     normalize(pdl.website). Always emits as 'platinum' confidence —
     websites are unique by design even for the largest franchisors.
  2. NAME match (fallback): for franchisors WITHOUT a domain match, JOIN to
     PDL on entity_name_normalized (no state filter — franchisor HQ can be
     anywhere). Tier by fan-out:
        gold     — exactly 1 PDL match
        silver   — 2-50 PDL matches
        rejected — >50 (common-name collisions, suppressed from output)

The output is the canonical "franchisor → PDL company" directory. Used
downstream as a FILTER asset against `bridges/pdl_sba_borrower/`: when a
PDL ID appears in BOTH bridges, the SBA borrower → PDL company match is
likely a franchisee → franchisor match (the franchisor's pdl_size /
pdl_founded describes the parent chain, NOT the franchisee borrower).

Logic:
  1. Register match-method `franchisor_directory_match` v1.0.0 + bridge
     `franchisor_pdl_directory` v1.0.0 in the registry (idempotent UPSERT).
  2. Read franchisors from Postgres (3.5K rows; SELECT-and-materialize-in-DuckDB).
  3. Read PDL from R2 (9M US companies after country filter).
  4. DOMAIN PASS: JOIN normalize(franchisor.official_domain) =
     normalize(pdl.website). All matches → 'platinum'.
  5. NAME PASS: for franchisors not matched in pass 1, JOIN
     normalize(franchisor.name) = normalize(pdl.name). Tier by fan-out.
  6. Write Parquet → bridges/franchisor_pdl_directory/snapshot=YYYY-MM-DD/data.parquet
     with bridge_run_id column embedded per row.

Inputs:
  Franchisors: Postgres entities.source_franchisors_entrepreneur (3,550 rows)
  PDL: r2://dex-raw-landing-zone/pdl/free_company_dataset.parquet

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/build_bridge_franchisor_pdl_directory.py --apply
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/build_bridge_franchisor_pdl_directory.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from scripts._lib.entity_name_normalize import (  # noqa: E402
    __version__ as NORMALIZER_VERSION,
)
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
logger = logging.getLogger("build_bridge_franchisor_pdl_directory")


# Bridge identity ------------------------------------------------------------
BRIDGE_NAME = "franchisor_pdl_directory"
METHOD_NAME = "franchisor_directory_match"
METHOD_SEMVER = "1.0.0"
LEGACY_BRIDGE_VERSION = "1.0.0"

SOURCE_LEFT = "source_franchisors_entrepreneur"
SOURCE_RIGHT = "pdl_companies_free"

# R2 layout ------------------------------------------------------------------
R2_BUCKET = "dex-raw-landing-zone"
PDL_INPUT_KEY = "pdl/free_company_dataset.parquet"
BRIDGE_OUTPUT_PREFIX = "bridges/franchisor_pdl_directory"

# Tier thresholds ------------------------------------------------------------
COLLISION_THRESHOLD = 50  # >50 fan-out on PDL side → name-pass rejected


def _r2_account_id_from_endpoint(endpoint: str) -> str:
    return endpoint.split("//")[-1].split(".")[0]


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
            ACCOUNT_ID '{_r2_account_id_from_endpoint(os.environ["R2_ENDPOINT"])}'
        );
        """
    )
    return con


# DuckDB SQL equivalent of `_lib/entity_name_normalize.py` v1.0.0. Same
# normalizer used by the SBA essentials applier and `pdl_sba_borrower`.
def _normalize_entity_sql(raw_expr: str) -> str:
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


# Domain normalization (lifted from `build_bridge_fmcsa_pdl_domain.py`):
# lower → trim → strip protocol → strip www. → strip path.
def _normalize_domain_sql(raw_expr: str) -> str:
    return (
        f"regexp_replace("
        f"regexp_replace("
        f"regexp_replace("
        f"lower(trim({raw_expr})), '^https?://', ''"
        f"), '^www\\.', ''"
        f"), '/.*$', '')"
    )


def _read_franchisors_from_postgres() -> list[tuple]:
    """SELECT all franchisors from Postgres; return list of typed tuples."""
    import psycopg

    sql = """
        SELECT slug, name, official_domain, primary_category,
               min_investment_usd, units_latest_total, franchise_type
          FROM entities.source_franchisors_entrepreneur
    """
    with psycopg.connect(os.environ["DEX_DB_URL_DIRECT"]) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()


def _materialize_inputs(con) -> tuple[int, int]:
    """Build franchisors + pdl_branded temp tables; return (rows_left, rows_right)."""
    pdl_uri = f"r2://{R2_BUCKET}/{PDL_INPUT_KEY}"

    logger.info("loading franchisors from Postgres …")
    franchisor_rows = _read_franchisors_from_postgres()
    logger.info(f"  fetched {len(franchisor_rows):,} franchisor rows")

    con.execute(
        """
        CREATE TEMP TABLE franchisors (
          franchisor_slug              VARCHAR,
          franchisor_name              VARCHAR,
          franchisor_official_domain   VARCHAR,
          franchisor_primary_category  VARCHAR,
          franchisor_min_investment_usd DOUBLE,
          franchisor_units_latest_total INTEGER,
          franchisor_franchise_type    VARCHAR
        );
        """
    )
    con.executemany(
        "INSERT INTO franchisors VALUES (?, ?, ?, ?, ?, ?, ?)",
        franchisor_rows,
    )

    rows_left = con.execute("SELECT count(*) FROM franchisors").fetchone()[0]
    logger.info(f"  franchisors materialized in DuckDB: {rows_left:,}")

    logger.info("materializing pdl_branded …")
    pdl_name_norm = _normalize_entity_sql("name")
    pdl_domain_norm = _normalize_domain_sql("website")
    con.execute(
        f"""
        CREATE TEMP TABLE pdl_branded AS
        SELECT
          id AS pdl_id,
          name AS pdl_name,
          ({pdl_name_norm}) AS pdl_name_normalized,
          website AS pdl_website,
          ({pdl_domain_norm}) AS pdl_website_normalized,
          linkedin_url AS pdl_linkedin_url,
          industry AS pdl_industry,
          size AS pdl_size,
          cast(founded AS VARCHAR) AS pdl_founded,
          locality AS pdl_locality,
          region AS pdl_region,
          country AS pdl_country
        FROM read_parquet('{pdl_uri}')
        WHERE LOWER(country) = 'united states'
          AND name IS NOT NULL
        """
    )
    rows_right = con.execute("SELECT count(*) FROM pdl_branded").fetchone()[0]
    logger.info(f"  pdl_branded (US, name non-null): {rows_right:,}")
    return rows_left, rows_right


def _build_match_table(
    con,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
) -> dict[str, int]:
    """Two-pass match: domain (platinum) → name (gold/silver/rejected by fan-out)."""
    logger.info("computing domain pass + name fallback …")

    # PASS 1: domain match. franchisor.official_domain → pdl.website.
    # All matches are 'platinum'. Domain match is high-precision (websites
    # are unique by design even for largest franchisors).
    franchisor_domain_norm = _normalize_domain_sql("franchisor_official_domain")
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_domain_matches AS
        SELECT
            'domain' AS match_method,
            'platinum' AS confidence_tier,
            f.franchisor_slug,
            f.franchisor_name,
            f.franchisor_official_domain,
            f.franchisor_primary_category,
            f.franchisor_min_investment_usd,
            f.franchisor_units_latest_total,
            f.franchisor_franchise_type,
            ({franchisor_domain_norm}) AS match_value_normalized,
            cast(NULL AS BIGINT) AS pdl_fan_out,
            p.pdl_id, p.pdl_name, p.pdl_website, p.pdl_linkedin_url,
            p.pdl_industry, p.pdl_size, p.pdl_founded,
            p.pdl_locality, p.pdl_region, p.pdl_country,
            TIMESTAMP '{generated_at_iso}' AS generated_at,
            '{LEGACY_BRIDGE_VERSION}' AS bridge_version,
            '{bridge_run_id}' AS bridge_run_id
        FROM franchisors f
        JOIN pdl_branded p
          ON p.pdl_website_normalized = ({franchisor_domain_norm})
         AND ({franchisor_domain_norm}) IS NOT NULL
         AND length(({franchisor_domain_norm})) > 0
        """
    )
    domain_matches = con.execute(
        "SELECT count(*) FROM bridge_domain_matches"
    ).fetchone()[0]
    domain_franchisors = con.execute(
        "SELECT count(DISTINCT franchisor_slug) FROM bridge_domain_matches"
    ).fetchone()[0]
    logger.info(
        f"  domain pass: {domain_matches:,} matches "
        f"({domain_franchisors:,} distinct franchisors)"
    )

    # PASS 2: name fallback. Apply to franchisors NOT already domain-matched.
    franchisor_name_norm = _normalize_entity_sql("franchisor_name")
    con.execute(
        f"""
        CREATE TEMP TABLE franchisors_unmatched AS
        SELECT *,
               ({franchisor_name_norm}) AS franchisor_name_normalized
          FROM franchisors
         WHERE franchisor_slug NOT IN (SELECT franchisor_slug FROM bridge_domain_matches)
           AND ({franchisor_name_norm}) IS NOT NULL
        """
    )
    unmatched = con.execute(
        "SELECT count(*) FROM franchisors_unmatched"
    ).fetchone()[0]
    logger.info(f"  unmatched after domain pass (name-pass candidates): {unmatched:,}")

    con.execute(
        """
        CREATE TEMP TABLE pdl_name_fanout AS
        SELECT pdl_name_normalized AS norm_name,
               count(*) AS pdl_companies_at_name
          FROM pdl_branded
         WHERE pdl_name_normalized IS NOT NULL
         GROUP BY pdl_name_normalized
        """
    )

    con.execute(
        f"""
        CREATE TEMP TABLE bridge_name_all AS
        SELECT
            'name' AS match_method,
            CASE
                WHEN pf.pdl_companies_at_name > {COLLISION_THRESHOLD} THEN 'rejected'
                WHEN pf.pdl_companies_at_name = 1 THEN 'gold'
                ELSE 'silver'
            END AS confidence_tier,
            f.franchisor_slug,
            f.franchisor_name,
            f.franchisor_official_domain,
            f.franchisor_primary_category,
            f.franchisor_min_investment_usd,
            f.franchisor_units_latest_total,
            f.franchisor_franchise_type,
            f.franchisor_name_normalized AS match_value_normalized,
            pf.pdl_companies_at_name AS pdl_fan_out,
            p.pdl_id, p.pdl_name, p.pdl_website, p.pdl_linkedin_url,
            p.pdl_industry, p.pdl_size, p.pdl_founded,
            p.pdl_locality, p.pdl_region, p.pdl_country,
            TIMESTAMP '{generated_at_iso}' AS generated_at,
            '{LEGACY_BRIDGE_VERSION}' AS bridge_version,
            '{bridge_run_id}' AS bridge_run_id
        FROM franchisors_unmatched f
        JOIN pdl_branded p
          ON p.pdl_name_normalized = f.franchisor_name_normalized
        JOIN pdl_name_fanout pf
          ON pf.norm_name = f.franchisor_name_normalized
        """
    )

    con.execute(
        """
        CREATE TEMP TABLE bridge_name_matches AS
        SELECT * FROM bridge_name_all WHERE confidence_tier <> 'rejected'
        """
    )
    name_total_all = con.execute(
        "SELECT count(*) FROM bridge_name_all"
    ).fetchone()[0]
    name_matches = con.execute(
        "SELECT count(*) FROM bridge_name_matches"
    ).fetchone()[0]
    name_franchisors = con.execute(
        "SELECT count(DISTINCT franchisor_slug) FROM bridge_name_matches"
    ).fetchone()[0]
    name_rejected = name_total_all - name_matches
    logger.info(
        f"  name pass: {name_matches:,} matches kept "
        f"({name_franchisors:,} distinct franchisors); "
        f"{name_rejected:,} rejected (>50 PDL fan-out)"
    )

    # UNION the two passes
    con.execute(
        """
        CREATE TEMP TABLE bridge_match AS
        SELECT * FROM bridge_domain_matches
        UNION ALL
        SELECT * FROM bridge_name_matches
        """
    )

    counts = con.execute(
        """
        SELECT
            count(*) AS rows_matched,
            count(*) FILTER (WHERE confidence_tier = 'platinum') AS rows_tier1,
            count(*) FILTER (WHERE confidence_tier = 'gold')     AS rows_tier2,
            count(*) FILTER (WHERE confidence_tier = 'silver')   AS rows_tier3,
            count(DISTINCT franchisor_slug) AS distinct_franchisors_matched
        FROM bridge_match
        """
    ).fetchone()

    return {
        "rows_matched": counts[0],
        "rows_tier1": counts[1],
        "rows_tier2": counts[2],
        "rows_tier3": counts[3],
        "rows_collision_rejected": name_rejected,
        "distinct_franchisors_matched": counts[4],
    }


def _write_bridge_parquet(con, snapshot: str) -> str:
    output_key = f"{BRIDGE_OUTPUT_PREFIX}/snapshot={snapshot}/data.parquet"
    output_url = f"r2://{R2_BUCKET}/{output_key}"
    logger.info(f"writing bridge → {output_url}")
    con.execute(
        f"""
        COPY bridge_match TO '{output_url}'
        (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)
        """
    )
    return output_key


def _ensure_registry() -> None:
    """Idempotent UPSERTs registering franchisor_directory_match + franchisor_pdl_directory."""
    logger.info("registering match_method + bridge in ops registry …")
    register_match_method(
        method_name=METHOD_NAME,
        description=(
            "Two-pass franchisor → PDL company match: pass 1 normalizes "
            "franchisor.official_domain and pdl.website (lower → strip "
            "protocol/www/path) and JOINs on equality (high-precision; emits "
            "platinum). Pass 2 (fallback for franchisors with no domain "
            "match) normalizes both names via _lib/entity_name_normalize.py "
            "v1.0.0 and JOINs on (name_normalized) without a state filter; "
            "tier by PDL fan-out (1 → gold, 2-50 → silver, >50 → rejected)."
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
            "domain pass → all platinum; name pass → gold (1:1), "
            "silver (1:2-50), rejected (>50)"
        ),
        rejection_rule_description=(
            "name-pass fan-out >50 collapses to rows_collision_rejected; "
            "domain-pass never rejected (websites are unique by design)"
        ),
        input_columns_left=["official_domain", "name"],
        input_columns_right=["website", "name"],
        output_value_description=(
            "domain pass: lower(trim(domain) without protocol/www/path); "
            "name pass: lower(trim(name) after corp-suffix-strip + punct-strip + ws-collapse)"
        ),
    )
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=f"{BRIDGE_OUTPUT_PREFIX}/",
        description=(
            "Entrepreneur.com franchisor brand × PDL Free Companies (US) — "
            "domain match (primary, high-precision) + name match (fallback for "
            "the ~21% domain-NULL franchisors). Used downstream as a FILTER "
            "asset against bridges/pdl_sba_borrower/ to flag SBA franchisee → "
            "PDL franchisor matches whose pdl_size / pdl_founded describes "
            "the parent chain rather than the franchisee borrower."
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write Parquet + ledger row")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="row counts + tier distribution only, no R2 / Postgres writes",
    )
    parser.add_argument(
        "--snapshot",
        default=None,
        help="YYYY-MM-DD output snapshot dir (default: today UTC)",
    )
    args = parser.parse_args()

    if not args.apply and not args.dry_run:
        parser.error("must pass --apply or --dry-run")
    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are mutually exclusive")

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")
    if not os.environ.get("DEX_DB_URL_DIRECT"):
        raise SystemExit("FAIL: DEX_DB_URL_DIRECT not set (required for franchisor read + registry)")

    started_at = datetime.now(tz=timezone.utc)
    snapshot = args.snapshot or started_at.strftime("%Y-%m-%d")
    t0 = time.time()

    logger.info(f"bridge: {BRIDGE_NAME} (method={METHOD_NAME} v{METHOD_SEMVER})")
    logger.info(f"normalizer: _lib/entity_name_normalize.py v{NORMALIZER_VERSION}")
    logger.info(f"snapshot: {snapshot}")

    output_key_planned = (
        f"{BRIDGE_OUTPUT_PREFIX}/snapshot={snapshot}/data.parquet"
    )

    if args.dry_run:
        bridge_run_id = "00000000-0000-0000-0000-000000000000"
        run_uuid = None
    else:
        _ensure_registry()
        run_uuid = start_bridge_run(
            bridge_name=BRIDGE_NAME,
            method_semver=METHOD_SEMVER,
            bridge_version=LEGACY_BRIDGE_VERSION,
            source_left=SOURCE_LEFT,
            source_right=SOURCE_RIGHT,
            match_method=METHOD_NAME,
            r2_output_key=output_key_planned,
        )
        bridge_run_id = str(run_uuid)
        logger.info(f"bridge_run_id={bridge_run_id}")

    con = _connect_duckdb_to_r2()

    try:
        rows_left, rows_right = _materialize_inputs(con)
        counts = _build_match_table(
            con,
            bridge_run_id=bridge_run_id,
            generated_at_iso=started_at.isoformat(),
        )

        logger.info("─" * 60)
        logger.info("bridge tier distribution:")
        logger.info(f"  rows_matched:                 {counts['rows_matched']:,}")
        logger.info(f"    platinum (domain):          {counts['rows_tier1']:,}")
        logger.info(f"    gold     (name 1:1):        {counts['rows_tier2']:,}")
        logger.info(f"    silver   (name 1:2-50):     {counts['rows_tier3']:,}")
        logger.info(f"  rows_collision_rejected:      {counts['rows_collision_rejected']:,}")
        logger.info(f"  distinct_franchisors_matched: {counts['distinct_franchisors_matched']:,} / {rows_left:,}")
        coverage_pct = 100.0 * counts["distinct_franchisors_matched"] / rows_left if rows_left else 0
        logger.info(f"  franchisor coverage:          {coverage_pct:.1f}%")

        if args.dry_run:
            logger.info(f"DRY RUN — no R2 / Postgres writes. duration={time.time()-t0:.1f}s")
            return 0

        r2_output_key = _write_bridge_parquet(con, snapshot)
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
                "domains_blacklisted": 0,
            },
        )
        logger.info(f"OK — run_id={bridge_run_id}  duration={time.time()-t0:.1f}s")
        logger.info(f"     output: r2://{R2_BUCKET}/{r2_output_key}")
        return 0

    except Exception as exc:
        logger.exception("bridge generation failed")
        if args.apply and run_uuid is not None:
            try:
                fail_bridge_run(run_uuid, str(exc))
            except Exception:
                logger.exception("also failed to mark run as failed")
        raise


if __name__ == "__main__":
    raise SystemExit(main())

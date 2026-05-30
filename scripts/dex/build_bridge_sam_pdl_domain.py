#!/usr/bin/env python3
"""DuckDB-on-R2 bridge generator: SAM entity ↔ PDL company via website domain.

Mirror of build_bridge_fmcsa_pdl_domain.py (the FMCSA-PDL pattern from
directive #2 + #2.5), adapted for SAM. Each SAM entity with a populated
entity_url is matched to PDL companies on the same normalized website
domain, with a confidence tier reflecting fan-out.

KEY DIFFERENCE vs. FMCSA-PDL: SAM entity_url is always business-domain
(no free-mail filtering required). SAM-PDL reuses domain_exact v1.0.0
but with no blacklist applied.

    platinum (tier1)  — 1:1 (single SAM entity ↔ single PDL co. on domain)
    gold     (tier2)  — 1:N or N:1 (one side fans out to multiple)
    silver   (tier3)  — N:M with fan-out ≤ 50 on both sides
    rejected          — fan-out > 50 on either side (collision_rejected)

Output:
    Parquet → s3://dex-raw-landing-zone/bridges/sam_pdl_domain/snapshot=<YYYY-MM-DD>/data.parquet
    Audit  → ops.bridge_generation_runs (one row per --apply invocation)

Inputs:
    SAM monthly Parquet at sam-gov/monthly/<YYYY-MM-DD>/part-*.parquet
    PDL companies Parquet at pdl/free_company_dataset.parquet

Domain normalization (must match FMCSA-PDL bridge / domain_exact v1.0.0):
    lower → trim → strip 'https?://' → strip '^www\\.' → strip '/.*$'
    validate via regex ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?\\.[a-z]{2,}$
        AND not numeric-only

Usage:
    doppler run --project hq-all --config prd -- \\
        uv run --with duckdb --with psycopg[binary] python \\
        apps/data-engine-x/scripts/build_bridge_sam_pdl_domain.py --apply

    doppler run --project hq-all --config prd -- \\
        uv run --with duckdb --with psycopg[binary] python \\
        apps/data-engine-x/scripts/build_bridge_sam_pdl_domain.py --dry-run
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

from scripts._lib.match_method_registry import (  # noqa: E402
    complete_bridge_run,
    fail_bridge_run,
    start_bridge_run,
)


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("build_bridge_sam_pdl_domain")


# Bridge identity ------------------------------------------------------------
BRIDGE_NAME = "sam_pdl_domain"
METHOD_NAME = "domain_exact"
METHOD_SEMVER = "1.0.0"
LEGACY_BRIDGE_VERSION = "1.0.0"

SOURCE_LEFT = "source_sam_entities"
SOURCE_RIGHT = "source_pdl_companies"

# R2 layout ------------------------------------------------------------------
R2_BUCKET = "dex-raw-landing-zone"
SAM_INPUT_PREFIX = "sam-gov/monthly"
PDL_INPUT_KEY = "pdl/free_company_dataset.parquet"
BRIDGE_OUTPUT_PREFIX = "bridges/sam_pdl_domain"

# Tier thresholds ------------------------------------------------------------
COLLISION_THRESHOLD = 50  # >50 fan-out on either side → rejected


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


def _detect_latest_sam_snapshot(con) -> str:
    rows = con.execute(
        f"SELECT file FROM glob('r2://{R2_BUCKET}/{SAM_INPUT_PREFIX}/**/*.parquet*')"
    ).fetchall()
    if not rows:
        raise SystemExit("FAIL: no SAM monthly Parquet files found")
    snapshots: set[str] = set()
    for (path,) in rows:
        for p in path.split("/"):
            if len(p) == 10 and p[4] == "-" and p[7] == "-":
                snapshots.add(p)
    if not snapshots:
        raise SystemExit("FAIL: no YYYY-MM-DD snapshot directory found under SAM monthly")
    return max(snapshots)


def _normalize_domain_sql(raw_expr: str) -> str:
    """Domain normalization SQL fragment — must match domain_exact v1.0.0."""
    return (
        f"regexp_replace("
        f"regexp_replace("
        f"regexp_replace("
        f"lower(trim({raw_expr})), '^https?://', ''"
        f"), '^www\\.', ''"
        f"), '/.*$', '')"
    )


def _domain_validation_sql(col: str) -> str:
    """Validation predicate: well-formed DNS-shape AND not numeric-only."""
    return (
        f"{col} ~ '^[a-z0-9]([a-z0-9.-]*[a-z0-9])?\\.[a-z]{{2,}}$' "
        f"AND NOT ({col} ~ '^[0-9.]+$')"
    )


def _materialize_inputs(con, sam_glob: str) -> tuple[int, int]:
    """Build the SAM + PDL candidate temp tables; return (rows_left, rows_right)."""
    sam_domain_expr = _normalize_domain_sql("entity_url")
    pdl_domain_expr = _normalize_domain_sql("website")
    validate_sam = _domain_validation_sql("normalized_domain")
    validate_pdl = _domain_validation_sql("normalized_domain")

    logger.info("materializing sam_branded + pdl_validated …")
    con.execute(
        f"""
        CREATE TEMP TABLE sam_branded AS
        WITH sam AS (
            SELECT
                unique_entity_id AS uei,
                legal_business_name AS sam_legal_name,
                physical_address_state_normalized AS sam_state,
                entity_url AS sam_url_raw,
                {sam_domain_expr} AS normalized_domain
            FROM read_parquet('{sam_glob}')
            WHERE entity_url IS NOT NULL AND entity_url != ''
        )
        SELECT *
          FROM sam
         WHERE normalized_domain IS NOT NULL
           AND {validate_sam}
        """
    )
    con.execute(
        f"""
        CREATE TEMP TABLE pdl_validated AS
        WITH pdl AS (
            SELECT
                id AS pdl_company_id,
                name AS pdl_name,
                website AS pdl_website_raw,
                {pdl_domain_expr} AS normalized_domain,
                industry AS pdl_industry,
                size AS pdl_size_bucket,
                locality AS pdl_locality,
                region AS pdl_region,
                country AS pdl_country,
                linkedin_url AS pdl_linkedin_url
            FROM read_parquet('r2://{R2_BUCKET}/{PDL_INPUT_KEY}')
            WHERE website IS NOT NULL
        )
        SELECT *
          FROM pdl
         WHERE normalized_domain IS NOT NULL
           AND {validate_pdl}
        """
    )

    rows_left = con.execute("SELECT count(*) FROM sam_branded").fetchone()[0]
    rows_right = con.execute("SELECT count(*) FROM pdl_validated").fetchone()[0]
    logger.info(
        f"  sam_branded: {rows_left:,} | pdl_validated: {rows_right:,}"
    )
    return rows_left, rows_right


def _build_match_table(
    con,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
) -> dict[str, int]:
    """Compute per-domain fan-out, JOIN, tier, write to TEMP TABLE bridge_match."""
    logger.info("computing per-domain fan-out + tiered join …")
    con.execute(
        """
        CREATE TEMP TABLE sam_fanout AS
        SELECT normalized_domain, count(*) AS sam_entities_at_domain
        FROM sam_branded
        GROUP BY normalized_domain
        """
    )
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
            s.uei,
            p.pdl_company_id,
            '{METHOD_NAME}' AS match_method,
            s.normalized_domain AS match_value,
            sf.sam_entities_at_domain,
            pf.pdl_companies_at_domain,
            CASE
                WHEN sf.sam_entities_at_domain > {COLLISION_THRESHOLD}
                  OR pf.pdl_companies_at_domain > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN sf.sam_entities_at_domain = 1
                  AND pf.pdl_companies_at_domain = 1
                    THEN 'platinum'
                WHEN sf.sam_entities_at_domain = 1
                  OR pf.pdl_companies_at_domain = 1
                    THEN 'gold'
                ELSE 'silver'
            END AS confidence_tier,
            TIMESTAMP '{generated_at_iso}' AS generated_at,
            '{LEGACY_BRIDGE_VERSION}' AS bridge_version,
            '{bridge_run_id}' AS bridge_run_id
        FROM sam_branded s
        JOIN pdl_validated p USING (normalized_domain)
        JOIN sam_fanout    sf ON sf.normalized_domain = s.normalized_domain
        JOIN pdl_fanout    pf ON pf.normalized_domain = s.normalized_domain
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE bridge_match AS
        SELECT
            uei, pdl_company_id, match_method, match_value,
            confidence_tier, sam_entities_at_domain, pdl_companies_at_domain,
            generated_at, bridge_version, bridge_run_id
        FROM bridge_all
        WHERE confidence_tier <> 'rejected'
        """
    )

    counts = con.execute(
        """
        SELECT
            count(*) AS rows_matched,
            count(*) FILTER (WHERE confidence_tier = 'platinum') AS rows_tier1,
            count(*) FILTER (WHERE confidence_tier = 'gold')     AS rows_tier2,
            count(*) FILTER (WHERE confidence_tier = 'silver')   AS rows_tier3
        FROM bridge_match
        """
    ).fetchone()
    rejected = con.execute(
        "SELECT count(*) FROM bridge_all WHERE confidence_tier = 'rejected'"
    ).fetchone()[0]

    return {
        "rows_matched": counts[0],
        "rows_tier1": counts[1],
        "rows_tier2": counts[2],
        "rows_tier3": counts[3],
        "rows_collision_rejected": rejected,
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
        help="YYYY-MM-DD SAM snapshot dir (default: latest)",
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
        raise SystemExit("FAIL: DEX_DB_URL_DIRECT not set (required for registry)")

    started_at = datetime.now(tz=timezone.utc)
    t0 = time.time()

    con = _connect_duckdb_to_r2()
    snapshot = args.snapshot or _detect_latest_sam_snapshot(con)
    logger.info(f"bridge: {BRIDGE_NAME} (method={METHOD_NAME} v{METHOD_SEMVER})")
    logger.info(f"snapshot: {snapshot}")

    sam_glob = f"r2://{R2_BUCKET}/{SAM_INPUT_PREFIX}/{snapshot}/part-*.parquet"
    output_key_planned = f"{BRIDGE_OUTPUT_PREFIX}/snapshot={snapshot}/data.parquet"

    # Mint a registry-aware bridge_run_id BEFORE materializing inputs so we
    # can embed it per-row in the bridge Parquet (provenance trace key).
    if args.dry_run:
        bridge_run_id = "00000000-0000-0000-0000-000000000000"
        run_uuid = None
    else:
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

    try:
        rows_left, rows_right = _materialize_inputs(con, sam_glob)
        counts = _build_match_table(
            con,
            bridge_run_id=bridge_run_id,
            generated_at_iso=started_at.isoformat(),
        )

        logger.info("─" * 60)
        logger.info("bridge tier distribution:")
        logger.info(f"  rows_matched:            {counts['rows_matched']:,}")
        logger.info(f"    platinum (1:1):        {counts['rows_tier1']:,}")
        logger.info(f"    gold     (1:N | N:1):  {counts['rows_tier2']:,}")
        logger.info(f"    silver   (N:M ≤{COLLISION_THRESHOLD}):    {counts['rows_tier3']:,}")
        logger.info(f"  rows_collision_rejected: {counts['rows_collision_rejected']:,}")

        # Pause-and-surface thresholds (validation gates)
        if counts["rows_matched"] > 0:
            tier1_share = counts["rows_tier1"] / counts["rows_matched"]
            logger.info(f"  tier-1 share:            {tier1_share:.1%}")

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
                "domains_blacklisted": 0,  # SAM entity_url is always business-domain
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

#!/usr/bin/env python3
"""DuckDB-on-R2 bridge generator: FMCSA carrier ↔ PDL company via email domain.

The first cross-source identity bridge in the post-source-wiring-sweep
architecture. Each carrier with a branded (non-free-mail) email domain is
matched to PDL companies on the same normalized website domain, with a
confidence tier reflecting fan-out:

    platinum (tier1)  — 1:1 (single FMCSA carrier ↔ single PDL co. on domain)
    gold     (tier2)  — 1:N or N:1 (one side fans out to multiple)
    silver   (tier3)  — N:M with fan-out ≤ 50 on both sides
    rejected          — fan-out > 50 on either side; logged but NOT emitted
                        (spurious matches: ISP, holding-co, large free-mail-like)

Output:
    Parquet → s3://dex-raw-landing-zone/bridges/fmcsa_pdl_domain/snapshot=<YYYY-MM-DD>/data.parquet
    Audit  → ops.bridge_generation_runs (one row per --apply invocation)

Inputs:
    FMCSA Company Census Parquet at fmcsa/Company Census File/<snapshot>/*.parquet*
    PDL companies Parquet at        pdl/free_company_dataset.parquet

Domain normalization (must match entities.match_domain_etld_plus_one):
    lower → trim → strip 'https?://' → strip '^www\\.' → strip '/.*$'
    validate via regex ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?\\.[a-z]{2,}$ AND not numeric-only

Usage:
    doppler run -p hq-all -c prd -- \\
        uv run --with duckdb --with psycopg[binary] python \\
        apps/data-engine-x/scripts/build_bridge_fmcsa_pdl_domain.py --apply

    doppler run -p hq-all -c prd -- \\
        uv run --with duckdb --with psycopg[binary] python \\
        apps/data-engine-x/scripts/build_bridge_fmcsa_pdl_domain.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from scripts._lib.free_mail_domains import (  # noqa: E402
    BLACKLIST_VERSION,
    FREE_MAIL_DOMAINS,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("build_bridge_fmcsa_pdl_domain")

# Bridge identity ------------------------------------------------------------
BRIDGE_NAME = "fmcsa_pdl_domain"
BRIDGE_VERSION = "1.0.0"  # bump on normalization rule or blacklist change
MATCH_METHOD = "domain_exact"
SOURCE_LEFT = "fmcsa_company_census"
SOURCE_RIGHT = "pdl_companies"

# R2 layout ------------------------------------------------------------------
R2_BUCKET = "dex-raw-landing-zone"
FMCSA_INPUT_PREFIX = "fmcsa/Company Census File"
PDL_INPUT_KEY = "pdl/free_company_dataset.parquet"
BRIDGE_OUTPUT_PREFIX = "bridges/fmcsa_pdl_domain"

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


def _detect_latest_fmcsa_snapshot(con) -> str:
    rows = con.execute(
        f"SELECT file FROM glob('r2://{R2_BUCKET}/{FMCSA_INPUT_PREFIX}/**/*.parquet*')"
    ).fetchall()
    if not rows:
        raise SystemExit("FAIL: no FMCSA Census Parquet files found")
    snapshots: set[str] = set()
    for (path,) in rows:
        for p in path.split("/"):
            if len(p) == 10 and p[4] == "-" and p[7] == "-":
                snapshots.add(p)
    if not snapshots:
        raise SystemExit("FAIL: no YYYY-MM-DD snapshot directory found")
    return max(snapshots)


# Domain normalization SQL fragment generator. Caller passes a SQL expression
# that yields the raw domain (e.g. split_part("EMAIL_ADDRESS", '@', 2) or
# website). Output is a normalized-domain SQL expression: lower+trim, strip
# protocol, strip leading www., strip path. Validation regex is applied
# separately in the WHERE clause.
def _normalize_domain_sql(raw_expr: str) -> str:
    return (
        f"regexp_replace("
        f"regexp_replace("
        f"regexp_replace("
        f"lower(trim({raw_expr})), '^https?://', ''"
        f"), '^www\\.', ''"
        f"), '/.*$', '')"
    )


# Validation predicate: well-formed DNS-shape AND not numeric-only.
def _domain_validation_sql(col: str) -> str:
    return (
        f"{col} ~ '^[a-z0-9]([a-z0-9.-]*[a-z0-9])?\\.[a-z]{{2,}}$' "
        f"AND NOT ({col} ~ '^[0-9.]+$')"
    )


def _build_fmcsa_cte(input_glob: str, blacklist_literal: str) -> str:
    domain_expr = _normalize_domain_sql(
        "split_part(\"EMAIL_ADDRESS\", '@', 2)"
    )
    validate = _domain_validation_sql("normalized_domain")
    return f"""
        fmcsa AS (
            SELECT
                "DOT_NUMBER" AS dot_number,
                "LEGAL_NAME" AS fmcsa_legal_name,
                "PHY_STATE" AS fmcsa_state,
                "STATUS_CODE" AS fmcsa_status_code,
                "EMAIL_ADDRESS" AS fmcsa_email,
                {domain_expr} AS normalized_domain
            FROM read_parquet('{input_glob}')
            WHERE "EMAIL_ADDRESS" LIKE '%@%'
        ),
        fmcsa_branded AS (
            SELECT *
            FROM fmcsa
            WHERE normalized_domain IS NOT NULL
              AND normalized_domain NOT IN ({blacklist_literal})
              AND {validate}
        )
    """


def _build_pdl_cte() -> str:
    domain_expr = _normalize_domain_sql("website")
    validate = _domain_validation_sql("normalized_domain")
    return f"""
        pdl AS (
            SELECT
                id AS pdl_company_id,
                name AS pdl_name,
                website AS pdl_website_raw,
                {domain_expr} AS normalized_domain,
                industry AS pdl_industry,
                size AS pdl_size_bucket,
                locality AS pdl_locality,
                region AS pdl_region,
                country AS pdl_country
            FROM read_parquet('r2://{R2_BUCKET}/{PDL_INPUT_KEY}')
            WHERE website IS NOT NULL
        ),
        pdl_validated AS (
            SELECT *
            FROM pdl
            WHERE normalized_domain IS NOT NULL
              AND {validate}
        )
    """


def _materialize_inputs(con, fmcsa_glob: str) -> tuple[int, int, int]:
    """Build the FMCSA + PDL candidate tables in DuckDB memory; return counts."""
    blacklist_literal = ", ".join(f"'{d}'" for d in sorted(FREE_MAIL_DOMAINS))

    fmcsa_cte = _build_fmcsa_cte(fmcsa_glob, blacklist_literal)
    pdl_cte = _build_pdl_cte()

    logger.info("materializing fmcsa_branded + pdl_validated …")
    con.execute(
        f"CREATE TEMP TABLE fmcsa_branded AS WITH {fmcsa_cte} SELECT * FROM fmcsa_branded"
    )
    con.execute(
        f"CREATE TEMP TABLE pdl_validated AS WITH {pdl_cte} SELECT * FROM pdl_validated"
    )

    rows_left = con.execute("SELECT count(*) FROM fmcsa_branded").fetchone()[0]
    rows_right = con.execute("SELECT count(*) FROM pdl_validated").fetchone()[0]

    # how many FMCSA rows we filtered out as free-mail
    fmcsa_domain_expr = _normalize_domain_sql(
        "split_part(\"EMAIL_ADDRESS\", '@', 2)"
    )
    rows_blacklisted_q = (
        f"SELECT count(*) FROM read_parquet('{fmcsa_glob}') "
        f"WHERE \"EMAIL_ADDRESS\" LIKE '%@%' "
        f"AND {fmcsa_domain_expr} IN ({blacklist_literal})"
    )
    domains_blacklisted = con.execute(rows_blacklisted_q).fetchone()[0]

    logger.info(
        f"  fmcsa_branded: {rows_left:,} | pdl_validated: {rows_right:,} | "
        f"fmcsa rows free-mail (excluded): {domains_blacklisted:,}"
    )
    return rows_left, rows_right, domains_blacklisted


def _build_match_table(con, generated_at_iso: str) -> dict[str, int]:
    """Compute per-domain fan-out, JOIN, tier, write to TEMP TABLE bridge_match.

    Returns dict of row counts (matched + tier1 + tier2 + tier3 + collision_rejected).
    """
    logger.info("computing per-domain fan-out + tiered join …")
    con.execute(
        """
        CREATE TEMP TABLE fmcsa_fanout AS
        SELECT normalized_domain, count(*) AS fmcsa_carriers_at_domain
        FROM fmcsa_branded
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
            f.dot_number,
            p.pdl_company_id,
            '{MATCH_METHOD}' AS match_method,
            f.normalized_domain AS match_value,
            ff.fmcsa_carriers_at_domain,
            pf.pdl_companies_at_domain,
            CASE
                WHEN ff.fmcsa_carriers_at_domain > {COLLISION_THRESHOLD}
                  OR pf.pdl_companies_at_domain   > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN ff.fmcsa_carriers_at_domain = 1
                  AND pf.pdl_companies_at_domain   = 1
                    THEN 'platinum'
                WHEN ff.fmcsa_carriers_at_domain = 1
                  OR pf.pdl_companies_at_domain   = 1
                    THEN 'gold'
                ELSE 'silver'
            END AS confidence_tier,
            TIMESTAMP '{generated_at_iso}' AS generated_at,
            '{BRIDGE_VERSION}' AS bridge_version
        FROM fmcsa_branded f
        JOIN pdl_validated p USING (normalized_domain)
        JOIN fmcsa_fanout ff ON ff.normalized_domain = f.normalized_domain
        JOIN pdl_fanout   pf ON pf.normalized_domain = f.normalized_domain
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE bridge_match AS
        SELECT
            dot_number, pdl_company_id, match_method, match_value,
            confidence_tier, fmcsa_carriers_at_domain, pdl_companies_at_domain,
            generated_at, bridge_version
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


def _write_ledger_row(
    *,
    run_id: str,
    started_at: datetime,
    status: str,
    r2_output_key: str,
    rows_left: int,
    rows_right: int,
    counts: dict[str, int],
    domains_blacklisted: int,
    error_message: str | None = None,
) -> None:
    import psycopg

    finished_at = datetime.now(tz=timezone.utc)
    duration_seconds = (finished_at - started_at).total_seconds()
    with psycopg.connect(os.environ["DEX_DB_URL_DIRECT"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ops.bridge_generation_runs (
                    run_id, bridge_name, bridge_version,
                    source_left, source_right, match_method,
                    r2_output_key, rows_left, rows_right, rows_matched,
                    rows_tier1, rows_tier2, rows_tier3,
                    rows_collision_rejected, domains_blacklisted,
                    started_at, finished_at, duration_seconds, status, error_message
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    run_id,
                    BRIDGE_NAME,
                    BRIDGE_VERSION,
                    SOURCE_LEFT,
                    SOURCE_RIGHT,
                    MATCH_METHOD,
                    r2_output_key,
                    rows_left,
                    rows_right,
                    counts.get("rows_matched"),
                    counts.get("rows_tier1"),
                    counts.get("rows_tier2"),
                    counts.get("rows_tier3"),
                    counts.get("rows_collision_rejected"),
                    domains_blacklisted,
                    started_at,
                    finished_at,
                    duration_seconds,
                    status,
                    error_message,
                ),
            )
        conn.commit()


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
        help="YYYY-MM-DD snapshot dir under FMCSA input prefix (default: latest)",
    )
    args = parser.parse_args()

    if not args.apply and not args.dry_run:
        parser.error("must pass --apply or --dry-run")
    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are mutually exclusive")

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")
    if args.apply and not os.environ.get("DEX_DB_URL_DIRECT"):
        raise SystemExit("FAIL: DEX_DB_URL_DIRECT not set (required for --apply)")

    started_at = datetime.now(tz=timezone.utc)
    run_id = str(uuid.uuid4())
    t0 = time.time()

    con = _connect_duckdb_to_r2()
    snapshot = args.snapshot or _detect_latest_fmcsa_snapshot(con)
    logger.info(f"bridge: {BRIDGE_NAME} v{BRIDGE_VERSION}")
    logger.info(f"snapshot: {snapshot}")
    logger.info(f"blacklist: {BLACKLIST_VERSION} ({len(FREE_MAIL_DOMAINS)} domains)")

    fmcsa_glob = f"r2://{R2_BUCKET}/{FMCSA_INPUT_PREFIX}/{snapshot}/*.parquet*"

    try:
        rows_left, rows_right, domains_blacklisted = _materialize_inputs(con, fmcsa_glob)
        counts = _build_match_table(con, started_at.isoformat())

        logger.info("---- bridge tier distribution ----")
        logger.info(f"  rows_matched:            {counts['rows_matched']:,}")
        logger.info(f"    platinum (1:1):        {counts['rows_tier1']:,}")
        logger.info(f"    gold     (1:N | N:1):  {counts['rows_tier2']:,}")
        logger.info(f"    silver   (N:M ≤{COLLISION_THRESHOLD}):    {counts['rows_tier3']:,}")
        logger.info(f"  rows_collision_rejected: {counts['rows_collision_rejected']:,}")

        if args.dry_run:
            logger.info(f"DRY RUN — no R2 / Postgres writes. duration={time.time()-t0:.1f}s")
            return 0

        r2_output_key = _write_bridge_parquet(con, snapshot)
        _write_ledger_row(
            run_id=run_id,
            started_at=started_at,
            status="completed",
            r2_output_key=r2_output_key,
            rows_left=rows_left,
            rows_right=rows_right,
            counts=counts,
            domains_blacklisted=domains_blacklisted,
        )
        logger.info(f"OK — ledger run_id={run_id}  duration={time.time()-t0:.1f}s")
        return 0

    except Exception as exc:
        logger.exception("bridge generation failed")
        if args.apply:
            try:
                _write_ledger_row(
                    run_id=run_id,
                    started_at=started_at,
                    status="failed",
                    r2_output_key="",
                    rows_left=0,
                    rows_right=0,
                    counts={},
                    domains_blacklisted=0,
                    error_message=str(exc),
                )
            except Exception:
                logger.exception("also failed to write failure ledger row")
        raise


if __name__ == "__main__":
    raise SystemExit(main())

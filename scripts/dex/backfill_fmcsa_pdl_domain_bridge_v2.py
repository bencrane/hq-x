#!/usr/bin/env python3
"""Backfill FMCSA-PDL bridge onto the match-method registry (directive #2.5).

Three steps in one script:

  1. Seed the registry: register_match_method('domain_exact'),
     register_match_method_version('domain_exact', '1.0.0', ...),
     register_bridge('fmcsa_pdl_domain', ...). Idempotent UPSERTs.

  2. Backfill the existing bridge_generation_runs row from PR #258
     (run_id f9966a41-aec8-454f-95e9-5ceb26b2a410): populate the new
     bridge_id + match_method_version_id FK columns. Idempotent UPDATE.

  3. Re-generate the bridge Parquet, this time with `bridge_run_id`
     embedded per row. Mints a fresh run row via start_bridge_run.
     Writes to bridges/fmcsa_pdl_domain/snapshot=YYYY-MM-DD/data.parquet
     where YYYY-MM-DD is today's UTC date (default).

The fresh Parquet supersedes the old one; downstream MVs (mv_fmcsa_pdl_match
in particular) DROP+RECREATE in the next directive step (s5) to pick up
the bridge_run_id column.

Usage:
    doppler run --project hq-all --config prd -- \\
        uv run --with duckdb --with psycopg[binary] python \\
        apps/data-engine-x/scripts/backfill_fmcsa_pdl_domain_bridge_v2.py --apply

    doppler run --project hq-all --config prd -- \\
        uv run --with duckdb --with psycopg[binary] python \\
        apps/data-engine-x/scripts/backfill_fmcsa_pdl_domain_bridge_v2.py --dry-run
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

from scripts._lib.free_mail_domains import (  # noqa: E402
    BLACKLIST_VERSION,
    FREE_MAIL_DOMAINS,
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
logger = logging.getLogger("backfill_fmcsa_pdl_domain_bridge_v2")


# Bridge identity — must align with PR #258's build_bridge_fmcsa_pdl_domain.py.
BRIDGE_NAME = "fmcsa_pdl_domain"
METHOD_NAME = "domain_exact"
METHOD_SEMVER = "1.0.0"
LEGACY_BRIDGE_VERSION = "1.0.0"  # legacy text stamp on bridge_generation_runs

SOURCE_LEFT = "source_fmcsa_census"
SOURCE_RIGHT = "source_pdl_companies"

# R2 layout — matches PR #258.
R2_BUCKET = "dex-raw-landing-zone"
FMCSA_INPUT_PREFIX = "fmcsa/Company Census File"
PDL_INPUT_KEY = "pdl/free_company_dataset.parquet"
BRIDGE_OUTPUT_PREFIX = "bridges/fmcsa_pdl_domain"

COLLISION_THRESHOLD = 50


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


def _normalize_domain_sql(raw_expr: str) -> str:
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


def _materialize_inputs(con, fmcsa_glob: str) -> tuple[int, int, int]:
    blacklist_literal = ", ".join(f"'{d}'" for d in sorted(FREE_MAIL_DOMAINS))

    domain_expr = _normalize_domain_sql("split_part(\"EMAIL_ADDRESS\", '@', 2)")
    pdl_domain_expr = _normalize_domain_sql("website")
    validate_fmcsa = _domain_validation_sql("normalized_domain")
    validate_pdl = _domain_validation_sql("normalized_domain")

    con.execute(
        f"""
        CREATE TEMP TABLE fmcsa_branded AS
        WITH fmcsa AS (
            SELECT
                "DOT_NUMBER" AS dot_number,
                "LEGAL_NAME" AS fmcsa_legal_name,
                "PHY_STATE" AS fmcsa_state,
                "STATUS_CODE" AS fmcsa_status_code,
                "EMAIL_ADDRESS" AS fmcsa_email,
                {domain_expr} AS normalized_domain
            FROM read_parquet('{fmcsa_glob}')
            WHERE "EMAIL_ADDRESS" LIKE '%@%'
        )
        SELECT *
          FROM fmcsa
         WHERE normalized_domain IS NOT NULL
           AND normalized_domain NOT IN ({blacklist_literal})
           AND {validate_fmcsa}
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
                country AS pdl_country
            FROM read_parquet('r2://{R2_BUCKET}/{PDL_INPUT_KEY}')
            WHERE website IS NOT NULL
        )
        SELECT *
          FROM pdl
         WHERE normalized_domain IS NOT NULL
           AND {validate_pdl}
        """
    )

    rows_left = con.execute("SELECT count(*) FROM fmcsa_branded").fetchone()[0]
    rows_right = con.execute("SELECT count(*) FROM pdl_validated").fetchone()[0]

    rows_blacklisted_q = (
        f"SELECT count(*) FROM read_parquet('{fmcsa_glob}') "
        f"WHERE \"EMAIL_ADDRESS\" LIKE '%@%' "
        f"AND {domain_expr} IN ({blacklist_literal})"
    )
    domains_blacklisted = con.execute(rows_blacklisted_q).fetchone()[0]

    logger.info(
        f"  fmcsa_branded: {rows_left:,} | pdl_validated: {rows_right:,} | "
        f"fmcsa rows free-mail (excluded): {domains_blacklisted:,}"
    )
    return rows_left, rows_right, domains_blacklisted


def _build_match_table(con, generated_at_iso: str, bridge_run_id: str) -> dict[str, int]:
    """Build the bridge match TEMP TABLE with bridge_run_id stamped per row."""
    con.execute(
        """
        CREATE TEMP TABLE fmcsa_fanout AS
        SELECT normalized_domain, count(*) AS fmcsa_carriers_at_domain
          FROM fmcsa_branded GROUP BY normalized_domain
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE pdl_fanout AS
        SELECT normalized_domain, count(*) AS pdl_companies_at_domain
          FROM pdl_validated GROUP BY normalized_domain
        """
    )
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            f.dot_number,
            p.pdl_company_id,
            '{METHOD_NAME}' AS match_method,
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
            '{LEGACY_BRIDGE_VERSION}' AS bridge_version,
            '{bridge_run_id}' AS bridge_run_id
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
    logger.info(f"writing bridge -> {output_url}")
    con.execute(
        f"""
        COPY bridge_match TO '{output_url}'
        (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)
        """
    )
    return output_key


def _seed_registry() -> tuple[str, str, str]:
    """Idempotent: register method, version, bridge. Returns the three UUIDs."""
    method_id = register_match_method(
        method_name=METHOD_NAME,
        description=(
            "Exact-equality match on lower(trim(domain)) after protocol/www/path/port "
            "stripping. Free-mail blacklist applied on relevant bridges (e.g. FMCSA-PDL)."
        ),
    )
    version_id = register_match_method_version(
        method_name=METHOD_NAME,
        semver=METHOD_SEMVER,
        normalizer_module="_lib/domain_normalize.py",
        normalizer_version="1.0.0",
        blacklist_module="_lib/free_mail_domains.py",
        blacklist_version=BLACKLIST_VERSION,
        tier_rule_description=(
            "platinum=1:1 (single FMCSA carrier and single PDL company at the domain), "
            "gold=1:N or N:1 (one side fans out), "
            "silver=N:M with fan-out <=50 on both sides"
        ),
        rejection_rule_description=(
            "fan-out >50 on either side -> confidence_tier='rejected', "
            "row excluded from emitted Parquet (logged in rows_collision_rejected)"
        ),
        input_columns_left=["EMAIL_ADDRESS"],
        input_columns_right=["website"],
        output_value_description=(
            "regexp_replace(regexp_replace(regexp_replace("
            "lower(trim(domain)), '^https?://',''), '^www\\.',''), '/.*$','')"
        ),
    )
    bridge_id = register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=f"{BRIDGE_OUTPUT_PREFIX}/",
        description=(
            "FMCSA carrier email-domain x PDL company website-domain via "
            "exact-equality after normalization. Free-mail blacklist applied."
        ),
    )
    return str(method_id), str(version_id), str(bridge_id)


def _backfill_existing_runs(bridge_id: str, version_id: str) -> int:
    """UPDATE existing bridge_generation_runs rows for fmcsa_pdl_domain with FKs."""
    import psycopg

    with psycopg.connect(os.environ["DEX_DB_URL_DIRECT"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE ops.bridge_generation_runs
                   SET bridge_id = %s,
                       match_method_version_id = %s
                 WHERE bridge_name = %s
                   AND (bridge_id IS NULL OR match_method_version_id IS NULL)
                """,
                (bridge_id, version_id, BRIDGE_NAME),
            )
            n = cur.rowcount
        conn.commit()
    return n


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="seed registry, backfill prior run rows, regenerate Parquet")
    parser.add_argument("--dry-run", action="store_true",
                        help="seed registry + backfill prior runs only; skip regen")
    parser.add_argument("--snapshot", default=None,
                        help="YYYY-MM-DD output snapshot dir (default: today UTC)")
    args = parser.parse_args()

    if not args.apply and not args.dry_run:
        parser.error("must pass --apply or --dry-run")
    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are mutually exclusive")

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
                "DEX_DB_URL_DIRECT"):
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")

    started_at = datetime.now(tz=timezone.utc)
    output_snapshot = args.snapshot or started_at.strftime("%Y-%m-%d")
    t0 = time.time()

    # --- Step 1: seed registry --------------------------------------------------
    logger.info("step 1/3: seeding registry (idempotent UPSERTs)")
    method_id, version_id, bridge_id = _seed_registry()
    logger.info(f"  match_method_id        = {method_id}")
    logger.info(f"  match_method_version_id = {version_id}")
    logger.info(f"  bridge_id              = {bridge_id}")

    # --- Step 2: backfill prior run rows ---------------------------------------
    logger.info("step 2/3: backfilling FK columns on existing bridge_generation_runs")
    n_updated = _backfill_existing_runs(bridge_id, version_id)
    logger.info(f"  updated {n_updated} prior run row(s) with bridge_id+version_id")

    if args.dry_run:
        logger.info(f"DRY RUN — skipping Parquet regen. duration={time.time()-t0:.1f}s")
        return 0

    # --- Step 3: regenerate Parquet with bridge_run_id stamp --------------------
    logger.info(
        "step 3/3: regenerating bridge Parquet with bridge_run_id column"
    )

    # Mint a fresh run row via start_bridge_run.
    output_key = (
        f"{BRIDGE_OUTPUT_PREFIX}/snapshot={output_snapshot}/data.parquet"
    )
    bridge_run_id = start_bridge_run(
        bridge_name=BRIDGE_NAME,
        method_semver=METHOD_SEMVER,
        bridge_version=LEGACY_BRIDGE_VERSION,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        match_method=METHOD_NAME,
        r2_output_key=output_key,
    )
    logger.info(f"  bridge_run_id = {bridge_run_id}")

    try:
        con = _connect_duckdb_to_r2()
        fmcsa_snapshot = _detect_latest_fmcsa_snapshot(con)
        fmcsa_glob = (
            f"r2://{R2_BUCKET}/{FMCSA_INPUT_PREFIX}/{fmcsa_snapshot}/*.parquet*"
        )
        logger.info(f"  source FMCSA snapshot = {fmcsa_snapshot}")
        logger.info(f"  output snapshot       = {output_snapshot}")
        logger.info(f"  blacklist             = {BLACKLIST_VERSION} "
                    f"({len(FREE_MAIL_DOMAINS)} domains)")

        rows_left, rows_right, domains_blacklisted = _materialize_inputs(con, fmcsa_glob)
        counts = _build_match_table(
            con, started_at.isoformat(timespec="seconds"), str(bridge_run_id)
        )

        logger.info("---- bridge tier distribution ----")
        logger.info(f"  rows_matched:            {counts['rows_matched']:,}")
        logger.info(f"    platinum (1:1):        {counts['rows_tier1']:,}")
        logger.info(f"    gold     (1:N | N:1):  {counts['rows_tier2']:,}")
        logger.info(f"    silver   (N:M ≤{COLLISION_THRESHOLD}):    {counts['rows_tier3']:,}")
        logger.info(f"  rows_collision_rejected: {counts['rows_collision_rejected']:,}")

        # Pause-and-surface gate per directive: row count >5% drift = surface.
        baseline = 296981
        delta_pct = abs(counts["rows_matched"] - baseline) / baseline * 100
        if delta_pct > 5:
            raise SystemExit(
                f"FAIL row-count drift gate: rows_matched={counts['rows_matched']:,} "
                f"vs baseline={baseline:,} (delta {delta_pct:.1f}% > 5%). "
                f"Likely normalizer/blacklist drift; surface for review."
            )
        logger.info(
            f"  row-count drift gate OK: delta {delta_pct:.2f}% "
            f"(baseline {baseline:,})"
        )

        _write_bridge_parquet(con, output_snapshot)

        complete_bridge_run(
            bridge_run_id,
            metrics={
                "rows_left": rows_left,
                "rows_right": rows_right,
                "rows_matched": counts["rows_matched"],
                "rows_tier1": counts["rows_tier1"],
                "rows_tier2": counts["rows_tier2"],
                "rows_tier3": counts["rows_tier3"],
                "rows_collision_rejected": counts["rows_collision_rejected"],
                "domains_blacklisted": domains_blacklisted,
            },
        )
        logger.info(
            f"OK — registry seeded, run completed. "
            f"bridge_run_id={bridge_run_id}  duration={time.time()-t0:.1f}s"
        )
        return 0

    except Exception as exc:
        logger.exception("regen failed")
        try:
            fail_bridge_run(bridge_run_id, str(exc))
        except Exception:
            logger.exception("also failed to write fail row")
        raise


if __name__ == "__main__":
    raise SystemExit(main())

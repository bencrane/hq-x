#!/usr/bin/env python3
"""Apply the SAM ↔ PDL firmographic MV (directive #3, surface 4).

Builds two RW objects (sections (3) + (4) of risingwave/tier_a_bridges.sql):

    source_bridge_sam_pdl_domain  — R2 source over the bridge Parquet
    mv_sam_pdl_firmographic       — Bridge × SAM POC × PDL refined join

Depends on `source_sam_entities_pocs` having been applied first
(`apply_sam_usaspending_aggregated_rw.py`). Asserts the source exists
in pg_class before applying.

Bridge Parquet must exist at bridges/sam_pdl_domain/snapshot=*/data.parquet
(written by `build_bridge_sam_pdl_domain.py --apply`); the source DDL
fails to scan otherwise.

Registry preflight: asserts sam_pdl_domain bridge + domain_exact method
exist in ops.* — without these the MV's bridge_run_id passthrough has
no provenance trace.

BACKGROUND_DDL=TRUE per L24.

Usage:
    doppler run -p hq-all -c prd -- \\
        uv run --with duckdb --with psycopg[binary] python \\
        apps/data-engine-x/scripts/apply_sam_pdl_firmographic_rw.py --apply

    doppler run -p hq-all -c prd -- \\
        uv run --with duckdb --with psycopg[binary] python \\
        apps/data-engine-x/scripts/apply_sam_pdl_firmographic_rw.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("apply_sam_pdl_firmographic_rw")

REPO_ROOT = Path(__file__).resolve().parents[3]
SQL_FILE = REPO_ROOT / "apps/data-engine-x/risingwave/tier_a_bridges.sql"

R2_BUCKET = "dex-raw-landing-zone"
BRIDGE_KEY = "bridges/sam_pdl_domain/snapshot=*/data.parquet"

EXPECTED_BRIDGE_COLS = {
    "uei", "pdl_company_id", "match_method", "match_value",
    "confidence_tier", "sam_entities_at_domain", "pdl_companies_at_domain",
    "generated_at", "bridge_version", "bridge_run_id",
    "snapshot",  # auto from Hive partition path
}


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


def _verify_bridge_parquet_exists(con) -> None:
    rows = con.execute(
        f"SELECT count(*) FROM glob('r2://{R2_BUCKET}/{BRIDGE_KEY}')"
    ).fetchone()
    if rows[0] == 0:
        raise SystemExit(
            f"FAIL: no bridge Parquet at r2://{R2_BUCKET}/{BRIDGE_KEY}. "
            "Run scripts/build_bridge_sam_pdl_domain.py --apply first."
        )
    logger.info(f"  bridge Parquet OK — {rows[0]} object(s) match {BRIDGE_KEY}")


def _verify_bridge_schema(con) -> None:
    cols = con.execute(
        f"SELECT column_name FROM (DESCRIBE SELECT * FROM read_parquet('r2://{R2_BUCKET}/{BRIDGE_KEY}'))"
    ).fetchall()
    actual = {c[0] for c in cols}
    missing = EXPECTED_BRIDGE_COLS - actual
    if missing:
        raise SystemExit(
            f"FAIL: bridge Parquet missing expected cols: {sorted(missing)}. "
            f"actual={sorted(actual)}"
        )
    extra = actual - EXPECTED_BRIDGE_COLS
    if extra:
        logger.warning(f"bridge Parquet has extra cols not in DDL: {sorted(extra)}")
    logger.info("  bridge Parquet schema OK")


def _verify_registry() -> None:
    """Assert sam_pdl_domain bridge + domain_exact method registered."""
    import psycopg

    with psycopg.connect(os.environ["DEX_DB_URL_DIRECT"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT b.bridge_name, m.method_name, v.semver
                  FROM ops.bridges b
                  JOIN ops.match_methods m USING (match_method_id)
                  JOIN ops.match_method_versions v
                    ON v.match_method_id = m.match_method_id
                 WHERE b.bridge_name = 'sam_pdl_domain'
                """
            )
            rows = cur.fetchall()
    if not rows:
        raise SystemExit(
            "FAIL: sam_pdl_domain bridge not registered. Run "
            "scripts/seed_bridge_registry_tier_a.py --apply first."
        )
    logger.info(
        f"  registry OK — bridge_name=sam_pdl_domain, "
        f"method={rows[0][1]} v{rows[0][2]} ({len(rows)} version(s))"
    )


def _verify_source_sam_entities_pocs() -> None:
    """Assert source_sam_entities_pocs has been applied (s3 must run first)."""
    cmd = [
        "psql",
        "-h", os.environ["RISINGWAVE_HOST"],
        "-p", os.environ["RISINGWAVE_PORT"],
        "-U", os.environ["RISINGWAVE_USER"],
        "-d", os.environ["RISINGWAVE_DATABASE"],
        "--no-psqlrc",
        "-tA",
        "-c",
        "SELECT count(*) FROM pg_class WHERE relname = 'source_sam_entities_pocs';",
    ]
    env = {**os.environ, "PGPASSWORD": os.environ["RISINGWAVE_PASSWORD"]}
    proc = subprocess.run(cmd, env=env, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"psql preflight check failed: {proc.stderr}")
    if proc.stdout.strip() != "1":
        raise SystemExit(
            "FAIL: source_sam_entities_pocs not in pg_class. Run "
            "scripts/apply_sam_usaspending_aggregated_rw.py --apply first."
        )
    logger.info("  source_sam_entities_pocs OK in pg_class")


def _extract_section(sql: str) -> str:
    """Extract sections (3) + (4) from the doc — start at the
    `(3) source_bridge_sam_pdl_domain` section header.
    """
    marker = "-- (3) source_bridge_sam_pdl_domain"
    lines = sql.splitlines(keepends=True)
    section_start = -1
    for i, line in enumerate(lines):
        if marker in line:
            section_start = i
            break
    if section_start == -1:
        raise SystemExit(f"FAIL: marker {marker!r} not found in SQL doc")
    # Walk back to include the leading `-- ───` ruler
    keep_from = section_start
    for j in range(section_start - 1, -1, -1):
        if "─" in lines[j]:
            keep_from = j
            break
    return "".join(lines[keep_from:])


def _substitute_secrets(sql: str) -> str:
    return (
        sql.replace("__R2_ENDPOINT__", os.environ["R2_ENDPOINT"])
        .replace("__R2_ACCESS_KEY_ID__", os.environ["R2_ACCESS_KEY_ID"])
        .replace("__R2_SECRET_ACCESS_KEY__", os.environ["R2_SECRET_ACCESS_KEY"])
    )


def _apply_to_rw(sql: str) -> None:
    cmd = [
        "psql",
        "-h", os.environ["RISINGWAVE_HOST"],
        "-p", os.environ["RISINGWAVE_PORT"],
        "-U", os.environ["RISINGWAVE_USER"],
        "-d", os.environ["RISINGWAVE_DATABASE"],
        "--no-psqlrc",
        "-v", "ON_ERROR_STOP=1",
    ]
    env = {**os.environ, "PGPASSWORD": os.environ["RISINGWAVE_PASSWORD"]}
    proc = subprocess.run(
        cmd, input=sql, env=env, check=False, capture_output=True, text=True
    )
    if proc.stdout:
        logger.info(proc.stdout.rstrip())
    if proc.stderr:
        logger.info(proc.stderr.rstrip())
    if proc.returncode != 0:
        raise SystemExit(f"psql exited {proc.returncode}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="apply DDL")
    parser.add_argument("--dry-run", action="store_true", help="print SQL, no apply")
    args = parser.parse_args()

    if not args.apply and not args.dry_run:
        parser.error("must pass --apply or --dry-run")
    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are mutually exclusive")

    required = (
        "R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
        "RISINGWAVE_HOST", "RISINGWAVE_PORT", "RISINGWAVE_USER",
        "RISINGWAVE_PASSWORD", "RISINGWAVE_DATABASE",
        "DEX_DB_URL_DIRECT",
    )
    for var in required:
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")

    if not SQL_FILE.is_file():
        raise SystemExit(f"FAIL: SQL doc missing: {SQL_FILE}")

    sql = SQL_FILE.read_text()
    section_sql = _extract_section(sql)
    substituted = _substitute_secrets(section_sql)

    _verify_registry()
    _verify_source_sam_entities_pocs()

    con = _connect_duckdb_to_r2()
    _verify_bridge_parquet_exists(con)
    _verify_bridge_schema(con)

    if args.dry_run:
        masked = (
            substituted
            .replace(os.environ["R2_ACCESS_KEY_ID"], "<R2_ACCESS_KEY_ID>")
            .replace(os.environ["R2_SECRET_ACCESS_KEY"], "<R2_SECRET_ACCESS_KEY>")
        )
        print(masked)
        logger.info("DRY RUN — no DDL applied")
        return 0

    t0 = time.time()
    logger.info("applying SAM-PDL DDL to RisingWave …")
    _apply_to_rw(substituted)
    logger.info(f"OK — DDL applied. duration={time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

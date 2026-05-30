#!/usr/bin/env python3
"""Apply the FMCSA↔PDL identity-bridge DDL to RisingWave.

Reads apps/data-engine-x/risingwave/fmcsa_pdl_domain_bridge.sql, substitutes
the R2 credential placeholders, optionally introspects the bridge and
essentials Parquets to assert their schemas match the DDL (post-directive-#2.5
this includes `bridge_run_id`), and runs the substituted SQL via psql against
the RisingWave cluster.

Registry-aware preflight: before applying any DDL, asserts the
fmcsa_pdl_domain bridge has been registered in ops.bridges +
ops.match_methods + ops.match_method_versions via
scripts/_lib/match_method_registry.py. The MV's bridge_run_id passthrough
has no meaning without registry rows.

Creates 4 RW objects:
    source_fmcsa_carrier_essentials_derived  (R2 source over derived essentials Parquet)
    mv_fmcsa_carrier_essentials              (hydrated thin MV)
    source_bridge_fmcsa_pdl_domain           (R2 source over bridge Parquet)
    mv_fmcsa_pdl_match                       (hydrated join MV — the audience MV)

The DDL is DROP+RECREATE on every apply (RW MVs aren't ALTER-friendly).
After directive #2.5, the source + MV carry a `bridge_run_id` column for
provenance trace-back through ops.bridge_generation_runs.

Usage:
    doppler run -p hq-all -c prd -- \\
        uv run --with duckdb --with psycopg[binary] python \\
        apps/data-engine-x/scripts/apply_fmcsa_pdl_match_rw.py --apply

    doppler run -p hq-all -c prd -- \\
        uv run --with duckdb --with psycopg[binary] python \\
        apps/data-engine-x/scripts/apply_fmcsa_pdl_match_rw.py --dry-run
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
logger = logging.getLogger("apply_fmcsa_pdl_match_rw")

REPO_ROOT = Path(__file__).resolve().parents[3]
SQL_FILE = (
    REPO_ROOT / "apps/data-engine-x/risingwave/fmcsa_pdl_domain_bridge.sql"
)

R2_BUCKET = "dex-raw-landing-zone"
ESSENTIALS_KEY = (
    "fmcsa-derived/carrier_essentials/snapshot=*/data.parquet"
)
BRIDGE_KEY = "bridges/fmcsa_pdl_domain/snapshot=*/data.parquet"

EXPECTED_ESSENTIALS_COLS = {
    "dot_number", "dun_bradstreet_no", "legal_name", "dba_name",
    "email_address", "phone", "cell_phone",
    "phy_street", "phy_city", "phy_state", "phy_zip", "phy_country",
    "carrier_mailing_street", "carrier_mailing_city",
    "carrier_mailing_state", "carrier_mailing_zip",
    "business_org_desc", "company_officer_1", "company_officer_2",
    "mcs150_date", "add_date", "status_code", "carrier_operation",
    "fleetsize", "power_units", "total_drivers",
    "mcs150_mileage", "mcs150_mileage_year",
    # Specialty / safety (added 2026-05-10 — see directive
    # 2026-05-10-fmcsa-deferred-audience-mvs-unblock.md)
    "hm_ind", "crgo_passengers", "crgo_liqgas", "crgo_coldfood",
    "crgo_motoveh", "crgo_drivetow", "bus_units", "safety_rating",
    "email_domain_normalized", "is_free_mail_domain",
    "snapshot",  # auto from Hive partition path
}

EXPECTED_BRIDGE_COLS = {
    "dot_number", "pdl_company_id", "match_method", "match_value",
    "confidence_tier", "fmcsa_carriers_at_domain", "pdl_companies_at_domain",
    "generated_at", "bridge_version",
    "bridge_run_id",  # directive #2.5 / L17: provenance trace key
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


def _introspect_parquet_cols(con, glob_path: str) -> set[str]:
    cols = con.execute(
        f"SELECT column_name FROM (DESCRIBE SELECT * FROM read_parquet('r2://{R2_BUCKET}/{glob_path}'))"
    ).fetchall()
    return {c[0] for c in cols}


def _verify_schemas(con) -> None:
    logger.info("introspecting Parquet schemas …")
    actual_essentials = _introspect_parquet_cols(con, ESSENTIALS_KEY)
    missing = EXPECTED_ESSENTIALS_COLS - actual_essentials
    extra = actual_essentials - EXPECTED_ESSENTIALS_COLS
    if missing:
        raise SystemExit(
            f"FAIL: essentials Parquet missing expected cols: {sorted(missing)}"
        )
    if extra:
        logger.warning(f"essentials Parquet has extra cols not in DDL: {sorted(extra)}")

    actual_bridge = _introspect_parquet_cols(con, BRIDGE_KEY)
    missing = EXPECTED_BRIDGE_COLS - actual_bridge
    extra = actual_bridge - EXPECTED_BRIDGE_COLS
    if missing:
        raise SystemExit(
            f"FAIL: bridge Parquet missing expected cols: {sorted(missing)}"
        )
    if extra:
        logger.warning(f"bridge Parquet has extra cols not in DDL: {sorted(extra)}")
    logger.info("  essentials cols OK; bridge cols OK")


def _verify_registry() -> None:
    """Registry-aware preflight (directive #2.5 / L18).

    Assert that the fmcsa_pdl_domain bridge has been registered via
    scripts/_lib/match_method_registry.py before applying the RW DDL.
    Without this, the MV's bridge_run_id column has nothing to trace
    back to.
    """
    import psycopg

    with psycopg.connect(os.environ["DEX_DB_URL_DIRECT"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT b.bridge_id, m.method_name, v.semver
                  FROM ops.bridges b
                  JOIN ops.match_methods m USING (match_method_id)
                  JOIN ops.match_method_versions v
                    ON v.match_method_id = m.match_method_id
                 WHERE b.bridge_name = 'fmcsa_pdl_domain'
                """
            )
            rows = cur.fetchall()
    if not rows:
        raise SystemExit(
            "FAIL: fmcsa_pdl_domain not registered in ops.bridges + "
            "ops.match_methods + ops.match_method_versions. Run "
            "scripts/backfill_fmcsa_pdl_domain_bridge_v2.py --apply first."
        )
    logger.info(
        f"  registry OK — bridge_id={rows[0][0]} method={rows[0][1]} "
        f"v{rows[0][2]} ({len(rows)} version(s))"
    )


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
    parser.add_argument("--apply", action="store_true", help="apply DDL to RW")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print substituted SQL with secrets masked, no apply",
    )
    parser.add_argument(
        "--skip-introspect",
        action="store_true",
        help="skip Parquet schema introspection (default: introspect)",
    )
    args = parser.parse_args()

    if not args.apply and not args.dry_run:
        parser.error("must pass --apply or --dry-run")
    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are mutually exclusive")

    required = (
        "R2_ENDPOINT",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "RISINGWAVE_HOST",
        "RISINGWAVE_PORT",
        "RISINGWAVE_USER",
        "RISINGWAVE_PASSWORD",
        "RISINGWAVE_DATABASE",
        "DEX_DB_URL_DIRECT",  # registry preflight check
    )
    for var in required:
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")

    if not SQL_FILE.is_file():
        raise SystemExit(f"FAIL: SQL doc missing: {SQL_FILE}")

    sql = SQL_FILE.read_text()
    substituted = _substitute_secrets(sql)

    # Registry preflight (directive #2.5 / L18). Always run, even with
    # --skip-introspect, because the MV's bridge_run_id passthrough has
    # no meaning without registry rows.
    _verify_registry()

    if not args.skip_introspect:
        con = _connect_duckdb_to_r2()
        _verify_schemas(con)

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
    logger.info("applying DDL to RisingWave …")
    _apply_to_rw(substituted)
    logger.info(f"OK — DDL applied. duration={time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

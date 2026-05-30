#!/usr/bin/env python3
"""Wire the dol_5500_sba_namestate bridge into RW.

After build_bridge_dol_5500_sba_namestate.py writes Parquet to R2, this
script:

  1. Introspects the bridge Parquet to get column types.
  2. CREATEs `source_bridge_dol_5500_sba_namestate` with typed DDL
     mirroring the Parquet (per the IRS 990 lesson — VARCHAR-only DDL
     silent-NULLs typed columns).
  3. CREATEs `mv_sba_borrower_with_5500_employer` joining bridge to
     mv_sba_borrower_essentials on loan_id.

Pattern lifted from FEC × SBA bridge (apps/data-engine-x/risingwave/
fec_sba_bridges.sql), with typed source DDL added.

Usage:
  doppler run -p hq-all -c prd -- \\
    uv run --with duckdb --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/apply_dol_5500_sba_bridge_rw.py --dry-run
  doppler run -p hq-all -c prd -- \\
    uv run --with duckdb --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/apply_dol_5500_sba_bridge_rw.py --apply
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("apply_dol_5500_sba_bridge_rw")


SOURCE_NAME = "source_bridge_dol_5500_sba_namestate"
MV_NAME = "mv_sba_borrower_with_5500_employer"
BRIDGE_PARQUET_GLOB = "bridges/dol_5500_sba_namestate/snapshot=*/data.parquet"
SAMPLE_KEY = "bridges/dol_5500_sba_namestate/snapshot=2026-05-09/data.parquet"
R2_BUCKET = "dex-raw-landing-zone"


def _required_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise SystemExit(f"FAIL: required env var {name} is not set")
    return v


def _duckdb_type_to_rw(duck_type: str) -> str:
    t = duck_type.upper()
    base = t.split("(")[0]
    mapping = {
        "VARCHAR": "CHARACTER VARYING",
        "TEXT": "CHARACTER VARYING",
        "STRING": "CHARACTER VARYING",
        "BIGINT": "BIGINT",
        "INTEGER": "INTEGER",
        "INT": "INTEGER",
        "SMALLINT": "SMALLINT",
        "TINYINT": "SMALLINT",
        "DOUBLE": "DOUBLE PRECISION",
        "FLOAT": "REAL",
        "REAL": "REAL",
        "DECIMAL": "NUMERIC",
        "NUMERIC": "NUMERIC",
        "BOOLEAN": "BOOLEAN",
        "BOOL": "BOOLEAN",
        "DATE": "DATE",
        "TIMESTAMP": "TIMESTAMP",
        "TIMESTAMPTZ": "TIMESTAMP WITH TIME ZONE",
        "UUID": "CHARACTER VARYING",
    }
    return mapping.get(base, "CHARACTER VARYING")


def _introspect_parquet() -> list[tuple[str, str]]:
    import duckdb

    endpoint_full = _required_env("R2_ENDPOINT")
    endpoint_host = endpoint_full.replace("https://", "").replace("http://", "")

    con = duckdb.connect(":memory:")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(f"SET s3_endpoint='{endpoint_host}';")
    con.execute(f"SET s3_access_key_id='{_required_env('R2_ACCESS_KEY_ID')}';")
    con.execute(f"SET s3_secret_access_key='{_required_env('R2_SECRET_ACCESS_KEY')}';")
    con.execute("SET s3_url_style='path';")
    con.execute("SET s3_region='auto';")

    s3_uri = f"s3://{R2_BUCKET}/{SAMPLE_KEY}"
    rows = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{s3_uri}');").fetchall()
    return [(r[0], r[1]) for r in rows]


def _build_source_ddl(columns: list[tuple[str, str]]) -> str:
    cols_decl = ",\n    ".join(
        f'"{name}" {_duckdb_type_to_rw(t)}' for name, t in columns
    )
    return f"""\
CREATE SOURCE {SOURCE_NAME} (
    {cols_decl}
) WITH (
    connector = 's3',
    s3.region_name = 'us-east-1',
    s3.bucket_name = '{R2_BUCKET}',
    s3.endpoint_url = '{_required_env("R2_ENDPOINT")}',
    s3.credentials.access = '{_required_env("R2_ACCESS_KEY_ID")}',
    s3.credentials.secret = '{_required_env("R2_SECRET_ACCESS_KEY")}',
    match_pattern = '{BRIDGE_PARQUET_GLOB}'
) FORMAT PLAIN ENCODE PARQUET;"""


# Joins bridge to mv_sba_borrower_essentials on loan_id. Pattern lifted
# from mv_sba_borrower_with_fec_owner. Surfaces the EIN + sponsor + plan
# admin payload, plus bridge fan-out diagnostic for downstream provenance.
MV_DDL = f"""
SET BACKGROUND_DDL = TRUE;
DROP MATERIALIZED VIEW IF EXISTS public.{MV_NAME};
CREATE MATERIALIZED VIEW public.{MV_NAME} AS
SELECT
    b.bridge_run_id,
    b.confidence_tier,
    -- SBA side
    s.dataset,
    s.program,
    s.loan_id,
    s.borrower_name,
    s.legal_name_normalized,
    s.borrower_state,
    s.borrower_zip,
    s.gross_approval,
    s.approval_date,
    s.first_disbursement_date,
    s.loanstatus_normalized,
    s.business_age,
    s.naics_code,
    -- Form 5500 sponsor payload (the EIN unlock + corporate identity)
    b.sponsor_ein,
    b.sponsor_name,
    b.sponsor_dba_name,
    b.business_code AS sponsor_business_code,
    b.sponsor_street,
    b.sponsor_city,
    b.sponsor_zip,
    b.sponsor_phone,
    -- Plan administrator payload (often the actual contact for plan
    -- benefits — sometimes the company itself, sometimes a third party
    -- like Fidelity / Empower / Vanguard)
    b.admin_name,
    b.admin_ein,
    b.admin_phone,
    b.admin_state,
    b.admin_city,
    b.admin_zip,
    -- Plan stability signals
    b.max_active_participants,
    b.max_total_participants,
    b.distinct_plan_count,
    b.latest_filing_year,
    b.earliest_filing_year,
    -- Bridge fan-out diagnostic for provenance audit
    b.form_5500_sponsors_at_name_state,
    b.sba_borrowers_at_name_state,
    b.match_value_normalized
FROM public.{SOURCE_NAME} b
JOIN public.mv_sba_borrower_essentials s
  ON s.loan_id = b.loan_id;
"""


def _rw_psql_script(sql: str, *, timeout_s: int = 300) -> None:
    cmd = [
        "psql",
        "-h", _required_env("RISINGWAVE_HOST"),
        "-p", _required_env("RISINGWAVE_PORT"),
        "-U", _required_env("RISINGWAVE_USER"),
        "-d", _required_env("RISINGWAVE_DATABASE"),
        "--no-psqlrc",
        "-v", "ON_ERROR_STOP=1",
    ]
    env = {**os.environ, "PGPASSWORD": _required_env("RISINGWAVE_PASSWORD")}
    proc = subprocess.run(cmd, env=env, input=sql, capture_output=True,
                          text=True, check=False, timeout=timeout_s)
    if proc.returncode != 0:
        raise SystemExit(
            f"RW psql failed (exit {proc.returncode}):\n"
            f"  STDERR: {proc.stderr}\n  STDOUT: {proc.stdout}"
        )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()

    if not (args.dry_run or args.apply):
        p.error("specify --dry-run or --apply")

    logger.info("Introspecting bridge Parquet schema …")
    cols = _introspect_parquet()
    typed_cnt = sum(1 for _, t in cols if t.upper() not in ("VARCHAR", "TEXT", "STRING"))
    logger.info(f"  {len(cols)} columns ({typed_cnt} typed)")

    source_ddl = _build_source_ddl(cols)

    if args.dry_run:
        print("--- CREATE SOURCE ---")
        print(source_ddl)
        print("\n--- CREATE MATERIALIZED VIEW ---")
        print(MV_DDL)
        return 0

    # Drop in reverse-dependency order so re-applies are idempotent.
    logger.info(f"Dropping {MV_NAME} if exists …")
    _rw_psql_script(f"DROP MATERIALIZED VIEW IF EXISTS public.{MV_NAME};")
    logger.info(f"Dropping {SOURCE_NAME} if exists …")
    _rw_psql_script(f"DROP SOURCE IF EXISTS public.{SOURCE_NAME} CASCADE;")

    logger.info(f"Creating {SOURCE_NAME} with typed DDL …")
    _rw_psql_script(source_ddl)
    logger.info(f"  source visible in pg_class: {SOURCE_NAME}")

    logger.info(f"Creating {MV_NAME} (BACKGROUND_DDL) …")
    _rw_psql_script(MV_DDL)
    logger.info(f"  MV DDL admitted; hydration runs async.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

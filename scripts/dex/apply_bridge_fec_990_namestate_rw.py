#!/usr/bin/env python3
"""Wire the fec_990_namestate bridge into RisingWave.

After build_bridge_fec_990_namestate.py writes Parquet to R2, this script:

  1. Introspects the bridge Parquet to get column types via DuckDB-on-R2.
  2. Drops + creates `source_bridge_fec_990_namestate` with typed DDL.
  3. Drops + creates `mv_990_principal_with_fec_giving` (BACKGROUND_DDL)
     filtering to platinum + gold confidence tiers.

Pattern lifted from `apply_dol_5500_sba_bridge_rw.py` — typed-DDL applier
(introspect Parquet → emit explicit-column CREATE SOURCE) per the IRS 990
lesson that VARCHAR-only DDL silent-NULLs typed columns.

Usage:
  doppler run -p hq-all -c prd -- \\
    uv run --with duckdb --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/apply_bridge_fec_990_namestate_rw.py --dry-run
  doppler run -p hq-all -c prd -- \\
    uv run --with duckdb --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/apply_bridge_fec_990_namestate_rw.py --apply
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
logger = logging.getLogger("apply_bridge_fec_990_namestate_rw")


SOURCE_NAME = "source_bridge_fec_990_namestate"
MV_NAME = "mv_990_principal_with_fec_giving"
BRIDGE_PARQUET_GLOB = "bridges/fec_990_namestate/snapshot=*/data.parquet"
R2_BUCKET = "dex-raw-landing-zone"


def _required_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise SystemExit(f"FAIL: required env var {name} is not set")
    return v


def _r2_account_id_from_endpoint(endpoint: str) -> str:
    return endpoint.split("//")[-1].split(".")[0]


def _duckdb_type_to_rw(duck_type: str) -> str:
    t = duck_type.upper()
    base = t.split("(")[0]
    # Array types like VARCHAR[] or BIGINT[].
    if base.endswith("[]"):
        inner = base[:-2]
        return f"{_duckdb_type_to_rw(inner)}[]"
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


def _find_latest_snapshot_key() -> str:
    import boto3

    s3 = boto3.client(
        "s3",
        endpoint_url=_required_env("R2_ENDPOINT"),
        aws_access_key_id=_required_env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=_required_env("R2_SECRET_ACCESS_KEY"),
        region_name="auto",
    )
    r = s3.list_objects_v2(
        Bucket=R2_BUCKET,
        Prefix="bridges/fec_990_namestate/",
    )
    keys = [obj["Key"] for obj in r.get("Contents", []) if obj["Key"].endswith("data.parquet")]
    if not keys:
        raise SystemExit(
            "FAIL: no bridges/fec_990_namestate/snapshot=*/data.parquet found in R2 — "
            "run build_bridge_fec_990_namestate.py first"
        )
    return sorted(keys)[-1]


def _introspect_parquet(sample_key: str) -> list[tuple[str, str]]:
    import duckdb

    con = duckdb.connect(":memory:")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(
        f"""
        CREATE SECRET (
            TYPE r2,
            KEY_ID '{_required_env("R2_ACCESS_KEY_ID")}',
            SECRET '{_required_env("R2_SECRET_ACCESS_KEY")}',
            ACCOUNT_ID '{_r2_account_id_from_endpoint(_required_env("R2_ENDPOINT"))}'
        );
        """
    )

    s3_uri = f"r2://{R2_BUCKET}/{sample_key}"
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


# Pattern C trivial join — bridge Parquet → MV. Filters platinum + gold; silver
# is excluded from the queryable surface (still present in the bridge Parquet
# for forensic re-classification).
MV_DDL = f"""
SET BACKGROUND_DDL = TRUE;
DROP MATERIALIZED VIEW IF EXISTS public.{MV_NAME};
CREATE MATERIALIZED VIEW public.{MV_NAME} AS
SELECT
  bridge_run_id,
  confidence_tier,
  last_normalized,
  first_normalized,
  state,

  -- 990 side enrichment
  person_990_raw_name_sample,
  person_990_role_set,
  person_990_ein_set,
  person_990_org_count,
  person_990_max_compensation,
  person_990_total_org_assets,
  person_990_is_pf_any,

  -- FEC side enrichment (the headline payload)
  fec_donor_employer,
  fec_donor_occupation,
  fec_donor_zip5,
  fec_donor_city,
  fec_donor_lifetime_giving_total,
  fec_donor_lifetime_giving_count,
  fec_donor_first_giving_date,
  fec_donor_latest_giving_date,
  fec_donor_cycles_active,

  -- Bridge fan-out diagnostic for provenance audit
  fec_donor_count_at_key,
  person_990_count_at_key
FROM public.{SOURCE_NAME}
WHERE confidence_tier IN ('platinum', 'gold');
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
    if proc.stdout:
        logger.info(proc.stdout.strip())


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()

    if not (args.dry_run or args.apply):
        p.error("specify --dry-run or --apply")

    logger.info("locating latest bridge Parquet snapshot in R2 …")
    sample_key = _find_latest_snapshot_key()
    logger.info(f"  sample: {sample_key}")

    logger.info("introspecting bridge Parquet schema …")
    cols = _introspect_parquet(sample_key)
    typed_cnt = sum(1 for _, t in cols if t.upper() not in ("VARCHAR", "TEXT", "STRING"))
    logger.info(f"  {len(cols)} columns ({typed_cnt} typed)")
    for name, t in cols:
        logger.info(f"    {name:<40} {t}")

    source_ddl = _build_source_ddl(cols)

    if args.dry_run:
        print("--- CREATE SOURCE ---")
        print(source_ddl)
        print("\n--- CREATE MATERIALIZED VIEW ---")
        print(MV_DDL)
        return 0

    # Drop in reverse-dependency order.
    logger.info(f"dropping {MV_NAME} if exists …")
    _rw_psql_script(f"DROP MATERIALIZED VIEW IF EXISTS public.{MV_NAME};")
    logger.info(f"dropping {SOURCE_NAME} if exists …")
    _rw_psql_script(f"DROP SOURCE IF EXISTS public.{SOURCE_NAME} CASCADE;")

    logger.info(f"creating {SOURCE_NAME} with typed DDL …")
    _rw_psql_script(source_ddl)
    logger.info(f"  source visible in pg_class: {SOURCE_NAME}")

    logger.info(f"creating {MV_NAME} (BACKGROUND_DDL) …")
    _rw_psql_script(MV_DDL)
    logger.info("  MV DDL admitted; hydration runs async.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

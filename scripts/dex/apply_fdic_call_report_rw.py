#!/usr/bin/env python3
"""Apply RisingWave DDL for the FDIC Call Report R2/RW pipeline.

Smart-DDL applier (lessons from PR1/PR2):
  1. Introspect Parquet schemas from R2 via DuckDB httpfs.
  2. Don't drop existing sources with healthy row counts.
  3. (Re)create the MV.
  4. Wait for hydration.
  5. Run validation gate.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/apply_fdic_call_report_rw.py
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/apply_fdic_call_report_rw.py --validate-only
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/apply_fdic_call_report_rw.py --mv-only

The static SQL at risingwave/fdic_call_report.sql is a documentation reference;
this script is the authoritative apply path.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections import OrderedDict

import duckdb
import psycopg


R2_BUCKET = "dex-raw-landing-zone"

INSTITUTIONS_TABLE = "source_fdic_institutions"
FINANCIALS_TABLE = "source_fdic_financials"
MV_NAME = "mv_fdic_performance_trends"

INSTITUTIONS_MATCH_PATTERN = "fdic/institutions/*.parquet"
FINANCIALS_MATCH_PATTERN = "fdic/financials/year=*/*.parquet"

HYDRATION_POLL_INTERVAL_S = 30
HYDRATION_POLL_MAX_MIN = 30

INSTITUTIONS_MIN_ROWS = 20_000
FINANCIALS_MIN_ROWS = 1_000_000
MV_MIN_ROWS = 500_000


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("apply-fdic-call-report-rw")


log = _logger()


def _required_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"{name} is not set in the environment.")
    return v


def _rw_conn() -> psycopg.Connection:
    return psycopg.connect(
        host=_required_env("RISINGWAVE_HOST"),
        port=int(_required_env("RISINGWAVE_PORT")),
        user=_required_env("RISINGWAVE_USER"),
        password=_required_env("RISINGWAVE_PASSWORD"),
        dbname=_required_env("RISINGWAVE_DATABASE"),
        sslmode=_required_env("RISINGWAVE_SSLMODE"),
        connect_timeout=10,
    )


def _duckdb_type_to_rw(t: str) -> str:
    t_upper = t.upper().strip()
    if t_upper in {"VARCHAR", "TEXT", "STRING"}:
        return "VARCHAR"
    if t_upper in {"DOUBLE", "FLOAT", "REAL", "DOUBLE PRECISION"}:
        return "DOUBLE PRECISION"
    if t_upper in {"SMALLINT", "INT2"}:
        return "SMALLINT"
    if t_upper in {"INTEGER", "INT", "INT4"}:
        return "INTEGER"
    if t_upper in {"BIGINT", "INT8"}:
        return "BIGINT"
    if t_upper == "BOOLEAN":
        return "BOOLEAN"
    if t_upper.startswith("DECIMAL") or t_upper.startswith("NUMERIC"):
        return "DECIMAL"
    if t_upper.startswith("TIMESTAMP"):
        return "TIMESTAMP"
    if t_upper == "DATE":
        return "DATE"
    log.warning("Unknown DuckDB type %r — falling back to VARCHAR", t)
    return "VARCHAR"


def _duck_with_r2() -> duckdb.DuckDBPyConnection:
    endpoint_full = _required_env("R2_ENDPOINT")
    endpoint_host = endpoint_full.replace("https://", "").replace("http://", "")
    con = duckdb.connect(":memory:")
    con.execute("INSTALL httpfs;")
    con.execute("LOAD httpfs;")
    con.execute(f"SET s3_endpoint='{endpoint_host}';")
    con.execute(f"SET s3_access_key_id='{_required_env('R2_ACCESS_KEY_ID')}';")
    con.execute(f"SET s3_secret_access_key='{_required_env('R2_SECRET_ACCESS_KEY')}';")
    con.execute("SET s3_url_style='path';")
    con.execute("SET s3_region='auto';")
    return con


def list_r2_objects(prefix: str) -> list[str]:
    import boto3
    s3 = boto3.client(
        "s3",
        endpoint_url=_required_env("R2_ENDPOINT"),
        aws_access_key_id=_required_env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=_required_env("R2_SECRET_ACCESS_KEY"),
        region_name="auto",
    )
    paginator = s3.get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])
    return [f"s3://{R2_BUCKET}/{k}" for k in keys]


def union_parquet_schemas(uris: list[str]) -> list[tuple[str, str]]:
    if not uris:
        raise SystemExit("no Parquet files to introspect")
    con = _duck_with_r2()
    union: "OrderedDict[str, str]" = OrderedDict()
    for uri in uris:
        try:
            rows = con.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{uri}');"
            ).fetchall()
        except Exception as exc:
            log.warning("introspect failed for %s: %s — skipping", uri, exc)
            continue
        for r in rows:
            col, dtype = r[0], _duckdb_type_to_rw(r[1])
            if col in union and union[col] != dtype:
                if union[col] != "VARCHAR" and dtype == "VARCHAR":
                    union[col] = "VARCHAR"
        for r in rows:
            col, dtype = r[0], _duckdb_type_to_rw(r[1])
            if col not in union:
                union[col] = dtype
    con.close()
    return [(c, t) for c, t in union.items()]


def _quote_ident(s: str) -> str:
    return '"' + s.replace('"', '""') + '"'


def make_create_table_ddl(
    table: str,
    schema: list[tuple[str, str]],
    *,
    match_pattern: str,
) -> str:
    cols = ",\n    ".join(f"{_quote_ident(c)} {t}" for c, t in schema)
    return f"""\
CREATE TABLE {table} (
    {cols}
)
WITH (
    connector = 's3',
    s3.bucket_name = '{R2_BUCKET}',
    s3.region_name = 'auto',
    s3.endpoint_url = '{_required_env("R2_ENDPOINT")}',
    s3.credentials.access = '{_required_env("R2_ACCESS_KEY_ID")}',
    s3.credentials.secret = '{_required_env("R2_SECRET_ACCESS_KEY")}',
    match_pattern = '{match_pattern}'
) FORMAT PLAIN ENCODE PARQUET;
"""


def get_existing_tables(conn: psycopg.Connection) -> dict[str, int]:
    found: dict[str, int] = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT relname FROM pg_class c
              JOIN pg_namespace n ON n.oid=c.relnamespace
             WHERE n.nspname='public'
               AND c.relkind IN ('r','v','m','s')
               AND relname IN (%s, %s, %s);
        """, (INSTITUTIONS_TABLE, FINANCIALS_TABLE, MV_NAME))
        names = sorted({r[0] for r in cur.fetchall()})
    for name in names:
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT count(*) FROM {name};")
                found[name] = int(cur.fetchone()[0])
        except Exception as exc:
            log.warning("count failed for %s: %s", name, exc)
            found[name] = -1
    return found


def ensure_source(
    conn: psycopg.Connection,
    *,
    table: str,
    match_pattern: str,
    list_prefix: str,
    existing: dict[str, int],
    expected_min_rows: int,
    sample_count: int = 4,
) -> None:
    if table in existing and existing[table] >= expected_min_rows:
        log.info("[%s] exists with %s rows — keeping as-is",
                 table, f"{existing[table]:,}")
        return

    if table in existing:
        log.warning("[%s] exists with only %s rows (< %s) — dropping + recreating",
                    table, f"{existing[table]:,}", f"{expected_min_rows:,}")
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {table} CASCADE;")

    log.info("[%s] introspecting schema from %s", table, list_prefix)
    uris = [u for u in list_r2_objects(list_prefix) if u.endswith(".parquet")]
    log.info("[%s]   found %d Parquet objects in R2", table, len(uris))
    if not uris:
        raise SystemExit(f"[{table}] no Parquet objects under {list_prefix}")
    sample_uris = sorted(uris)[-sample_count:] if len(uris) > sample_count else uris
    schema = union_parquet_schemas(sample_uris)
    log.info("[%s]   union schema: %d cols (sampled %d files)",
             table, len(schema), len(sample_uris))

    ddl = make_create_table_ddl(table, schema, match_pattern=match_pattern)
    head = ddl.replace("\n", " ")[:140]
    log.info("[%s] applying CREATE: %s ...", table, head)
    with conn.cursor() as cur:
        cur.execute(ddl)


# --------------------------------------------------------------------------- #
# MV: FDIC bank performance trends
# --------------------------------------------------------------------------- #
#
# Locked column contract (17 columns):
#   cert, report_date, report_year, report_quarter,
#   bank_name, state, active,
#   total_assets, total_deposits, equity_capital,
#   loans_net, allowance_loan_losses,
#   net_income, interest_income, interest_expense,
#   equity_ratio, net_income_to_assets.


def _resolve(cols: set[str], *candidates: str) -> str | None:
    for c in candidates:
        if c.lower() in cols:
            return c.lower()
    return None


def make_mv_ddl(
    inst_cols: set[str],
    fin_cols: set[str],
) -> str:
    # Institutions
    cert_inst = _resolve(inst_cols, "cert")
    name_col = _resolve(inst_cols, "name", "bankname")
    active_col = _resolve(inst_cols, "active")

    # Financials
    cert_fin = _resolve(fin_cols, "cert")
    repdte_col = _resolve(fin_cols, "repdte", "report_date")
    state_col = _resolve(fin_cols, "stalp", "state", "statecode")
    asset_col = _resolve(fin_cols, "asset", "total_assets")
    dep_col = _resolve(fin_cols, "dep", "deposits")
    eq_col = _resolve(fin_cols, "eq", "equity")
    lnlsnet_col = _resolve(fin_cols, "lnlsnet", "loans_net")
    lnatres_col = _resolve(fin_cols, "lnatres", "allowance")
    netinc_col = _resolve(fin_cols, "netinc", "net_income")
    intinc_col = _resolve(fin_cols, "intinc", "interest_income")
    intexp_col = _resolve(fin_cols, "eintexp", "intexp", "interest_expense")

    if not cert_fin or not repdte_col:
        raise SystemExit(
            f"required FDIC financials columns missing — "
            f"cert={cert_fin} repdte={repdte_col}"
        )

    def _double(col: str | None, alias: str) -> str:
        if col is None:
            return f"CAST(NULL AS DOUBLE PRECISION) AS {alias}"
        # FDIC fields land as numeric in Parquet; bare CAST is safe.
        return f'CAST(f."{col}" AS DOUBLE PRECISION) AS {alias}'

    asset_expr = _double(asset_col, "total_assets")
    eq_expr = _double(eq_col, "equity_capital")

    # Computed ratios
    if asset_col and eq_col:
        eq_ratio_expr = (
            f'CAST(f."{eq_col}" AS DOUBLE PRECISION) / '
            f'NULLIF(CAST(f."{asset_col}" AS DOUBLE PRECISION), 0)'
        )
    else:
        eq_ratio_expr = "CAST(NULL AS DOUBLE PRECISION)"

    if asset_col and netinc_col:
        ni_ratio_expr = (
            f'CAST(f."{netinc_col}" AS DOUBLE PRECISION) / '
            f'NULLIF(CAST(f."{asset_col}" AS DOUBLE PRECISION), 0)'
        )
    else:
        ni_ratio_expr = "CAST(NULL AS DOUBLE PRECISION)"

    # Year/quarter from REPDTE (YYYYMMDD format from FDIC API)
    repdte_str = f'CAST(f."{repdte_col}" AS VARCHAR)'
    year_expr = f'CAST(SUBSTRING({repdte_str}, 1, 4) AS SMALLINT)'
    # Quarter derived from month (03=Q1, 06=Q2, 09=Q3, 12=Q4)
    quarter_expr = (
        f"CASE SUBSTRING({repdte_str}, 5, 2) "
        f"WHEN '03' THEN 1::SMALLINT "
        f"WHEN '06' THEN 2::SMALLINT "
        f"WHEN '09' THEN 3::SMALLINT "
        f"WHEN '12' THEN 4::SMALLINT "
        f"ELSE NULL END"
    )

    name_expr = (
        f'COALESCE(i."{name_col}", f."name")'
        if name_col and "name" in fin_cols
        else (f'i."{name_col}"' if name_col else 'CAST(NULL AS VARCHAR)')
    )
    active_expr = f'i."{active_col}"' if active_col else 'CAST(NULL AS INTEGER)'
    state_expr = f'UPPER(f."{state_col}")' if state_col else 'CAST(NULL AS VARCHAR)'

    return f"""\
CREATE MATERIALIZED VIEW {MV_NAME} AS
SELECT
    f."{cert_fin}" AS cert,
    {repdte_str} AS report_date,
    {year_expr} AS report_year,
    {quarter_expr} AS report_quarter,
    {name_expr} AS bank_name,
    {state_expr} AS state,
    {active_expr} AS active,
    {asset_expr},
    {_double(dep_col, "total_deposits")},
    {eq_expr},
    {_double(lnlsnet_col, "loans_net")},
    {_double(lnatres_col, "allowance_loan_losses")},
    {_double(netinc_col, "net_income")},
    {_double(intinc_col, "interest_income")},
    {_double(intexp_col, "interest_expense")},
    {eq_ratio_expr} AS equity_ratio,
    {ni_ratio_expr} AS net_income_to_assets
FROM {FINANCIALS_TABLE} f
LEFT JOIN {INSTITUTIONS_TABLE} i
  ON i."{cert_inst}" = f."{cert_fin}"
WHERE f."{cert_fin}" IS NOT NULL AND f."{repdte_col}" IS NOT NULL;
"""


def apply_ddl(conn: psycopg.Connection, ddl: str, *, label: str) -> None:
    head = ddl.replace("\n", " ")[:140]
    log.info("[%s] applying: %s ...", label, head)
    with conn.cursor() as cur:
        cur.execute(ddl)


def wait_for_hydration(table: str, *, expected_min_rows: int) -> int:
    log.info("waiting for %s to hydrate (≥ %s rows)", table, f"{expected_min_rows:,}")
    deadline = time.monotonic() + HYDRATION_POLL_MAX_MIN * 60
    last_rc = -1
    while time.monotonic() < deadline:
        try:
            with _rw_conn() as conn:
                conn.autocommit = True
                with conn.cursor() as cur:
                    cur.execute(f"SELECT count(*) FROM {table};")
                    rc_row = cur.fetchone()
            rc = int(rc_row[0]) if rc_row else 0
        except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
            log.warning("  poll error (%s); will retry", exc)
            time.sleep(HYDRATION_POLL_INTERVAL_S)
            continue
        if rc != last_rc:
            log.info("  %s: %s rows", table, f"{rc:,}")
            last_rc = rc
        if rc >= expected_min_rows:
            return rc
        time.sleep(HYDRATION_POLL_INTERVAL_S)
    log.warning("hydration deadline reached at %s rows", f"{last_rc:,}")
    return last_rc


REQUIRED_MV_COLUMNS = (
    "cert", "report_date", "report_year", "report_quarter",
    "bank_name", "state", "active",
    "total_assets", "total_deposits", "equity_capital",
    "loans_net", "allowance_loan_losses",
    "net_income", "interest_income", "interest_expense",
    "equity_ratio", "net_income_to_assets",
)


def run_validation_gate(conn: psycopg.Connection) -> int:
    failures: list[str] = []
    with conn.cursor() as cur:
        for table, min_rows in (
            (INSTITUTIONS_TABLE, INSTITUTIONS_MIN_ROWS),
            (FINANCIALS_TABLE, FINANCIALS_MIN_ROWS),
            (MV_NAME, MV_MIN_ROWS),
        ):
            cur.execute(f"SELECT count(*) FROM {table};")
            rc = int(cur.fetchone()[0])
            log.info("  %-40s %s rows", table + ":", f"{rc:>12,}")
            if rc < min_rows:
                failures.append(f"{table} rows {rc:,} < {min_rows:,}")

        cur.execute(f"""
            SELECT column_name FROM information_schema.columns
             WHERE table_name = '{MV_NAME}'
             ORDER BY ordinal_position;
        """)
        actual_cols = {r[0].lower() for r in cur.fetchall()}
        missing = [c for c in REQUIRED_MV_COLUMNS if c.lower() not in actual_cols]
        if missing:
            failures.append(f"{MV_NAME} missing columns: {missing}")

    if failures:
        log.error("=== VALIDATION FAILED — %d issue(s) ===", len(failures))
        for f in failures:
            log.error("  - %s", f)
        return 1
    log.info("=== VALIDATION PASSED ===")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--validate-only", action="store_true")
    p.add_argument("--skip-hydration-wait", action="store_true")
    p.add_argument("--mv-only", action="store_true")
    p.add_argument("--force-rebuild-sources", action="store_true")
    args = p.parse_args()

    if args.validate_only:
        with _rw_conn() as conn:
            conn.autocommit = True
            return run_validation_gate(conn)

    with _rw_conn() as conn:
        conn.autocommit = True
        existing = get_existing_tables(conn)
    log.info("existing RW state: %s", existing)

    if args.force_rebuild_sources:
        log.info("--force-rebuild-sources: dropping all FDIC sources")
        with _rw_conn() as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(f"DROP MATERIALIZED VIEW IF EXISTS {MV_NAME};")
                cur.execute(f"DROP TABLE IF EXISTS {FINANCIALS_TABLE} CASCADE;")
                cur.execute(f"DROP TABLE IF EXISTS {INSTITUTIONS_TABLE} CASCADE;")
            existing = {}

    if not args.mv_only:
        with _rw_conn() as conn:
            conn.autocommit = True
            ensure_source(
                conn, table=INSTITUTIONS_TABLE,
                match_pattern=INSTITUTIONS_MATCH_PATTERN,
                list_prefix="fdic/institutions/",
                existing=existing,
                expected_min_rows=INSTITUTIONS_MIN_ROWS,
            )
            ensure_source(
                conn, table=FINANCIALS_TABLE,
                match_pattern=FINANCIALS_MATCH_PATTERN,
                list_prefix="fdic/financials/",
                existing=existing,
                expected_min_rows=FINANCIALS_MIN_ROWS,
            )

    if not args.mv_only and not args.skip_hydration_wait:
        wait_for_hydration(FINANCIALS_TABLE, expected_min_rows=FINANCIALS_MIN_ROWS)

    log.info("introspecting schemas for MV DDL generation")
    with _rw_conn() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT column_name FROM information_schema.columns
                 WHERE table_name = '{INSTITUTIONS_TABLE}'
            """)
            inst_cols = {r[0].lower() for r in cur.fetchall()}
            cur.execute(f"""
                SELECT column_name FROM information_schema.columns
                 WHERE table_name = '{FINANCIALS_TABLE}'
            """)
            fin_cols = {r[0].lower() for r in cur.fetchall()}
    log.info("  INSTITUTIONS=%d cols  FINANCIALS=%d cols",
             len(inst_cols), len(fin_cols))

    if not inst_cols or not fin_cols:
        raise SystemExit("source tables don't exist; re-run without --mv-only")

    ddl_drop_mv = f"DROP MATERIALIZED VIEW IF EXISTS {MV_NAME};"
    ddl_mv = make_mv_ddl(inst_cols, fin_cols)

    with _rw_conn() as conn:
        conn.autocommit = True
        apply_ddl(conn, ddl_drop_mv, label="drop-mv")
        apply_ddl(conn, ddl_mv, label="create-mv")

    if not args.skip_hydration_wait:
        wait_for_hydration(MV_NAME, expected_min_rows=MV_MIN_ROWS)

    with _rw_conn() as conn:
        conn.autocommit = True
        return run_validation_gate(conn)


if __name__ == "__main__":
    sys.exit(main())

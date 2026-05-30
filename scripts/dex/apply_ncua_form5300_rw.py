#!/usr/bin/env python3
"""Apply RisingWave DDL for the NCUA Form 5300 Call Report R2/RW pipeline.

Smart-DDL applier (lessons from PR1):
  1. Don't drop existing sources that the predecessor already hydrated.
  2. Create missing sources (here: source_ncua_fs220a) on demand.
  3. Always (re)create the MV.
  4. Wait for hydration.
  5. Run NCUA-only validation gate.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/apply_ncua_form5300_rw.py
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/apply_ncua_form5300_rw.py --validate-only
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/apply_ncua_form5300_rw.py --mv-only

The static SQL at risingwave/ncua_form5300.sql is a documentation reference;
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

FOICU_TABLE = "source_ncua_foicu"
FS220_TABLE = "source_ncua_fs220"
FS220A_TABLE = "source_ncua_fs220a"
MV_NAME = "mv_ncua_granular_schedules"

FOICU_MATCH_PATTERN = "ncua/year=*/quarter=Q*/foicu.parquet"
FS220_MATCH_PATTERN = "ncua/year=*/quarter=Q*/fs220.parquet"
FS220A_MATCH_PATTERN = "ncua/year=*/quarter=Q*/fs220a.parquet"

HYDRATION_POLL_INTERVAL_S = 30
HYDRATION_POLL_MAX_MIN = 30

NCUA_MIN_FOICU_ROWS = 150_000
NCUA_MIN_FS220_ROWS = 150_000
NCUA_MIN_FS220A_ROWS = 150_000
MV_MIN_ROWS = 100_000

# RW Cloud trial CU is too slow for a 254-col CREATE TABLE on FS220A
# (CREATE blocks at 0% progress for >30 min). Restrict FS220A to the
# columns the MV actually consumes — join keys + a handful of ACCT codes.
FS220A_COLUMN_ALLOWLIST = (
    "cu_number", "cycle_date", "join_number",
    "ncua_year", "ncua_quarter",
    "acct_115", "acct_117", "acct_119",  # income components
    "acct_124", "acct_131", "acct_140",  # expense candidates
    "acct_270", "acct_661a",             # net income candidates
)


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("apply-ncua-form5300-rw")


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


# --------------------------------------------------------------------------- #
# Schema introspection (DuckDB on R2)
# --------------------------------------------------------------------------- #


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


def list_uris_matching(suffix: str) -> list[str]:
    """Return Parquet objects under ncua/ whose path ends with /<suffix>."""
    all_uris = list_r2_objects("ncua/")
    return [u for u in all_uris if u.endswith(suffix)]


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


# --------------------------------------------------------------------------- #
# DDL generation
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# Smart-DDL: check what exists, only create missing
# --------------------------------------------------------------------------- #


def get_existing_tables(conn: psycopg.Connection) -> dict[str, int]:
    """Return {name: row_count} for any existing source_ncua_* tables."""
    found: dict[str, int] = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT relname FROM pg_class c
              JOIN pg_namespace n ON n.oid=c.relnamespace
             WHERE n.nspname='public'
               AND c.relkind IN ('r','v','m','s')
               AND relname IN (%s, %s, %s, %s);
        """, (FOICU_TABLE, FS220_TABLE, FS220A_TABLE, MV_NAME))
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
    list_suffix: str,
    existing: dict[str, int],
    expected_min_rows: int,
    sample_count: int = 4,
    column_allowlist: tuple[str, ...] | None = None,
) -> None:
    """Create the source if missing (or if existing has implausibly low rows).

    If `column_allowlist` is provided, restrict the CREATE TABLE schema to
    only those columns (case-insensitive match on Parquet column names).
    Used to keep DDL fast on RW Cloud trial CU when the upstream Parquet
    has hundreds of columns the MV doesn't need.
    """
    if table in existing and existing[table] >= expected_min_rows:
        log.info("[%s] exists with %s rows — keeping as-is",
                 table, f"{existing[table]:,}")
        return

    if table in existing:
        log.warning("[%s] exists with only %s rows (< %s) — dropping + recreating",
                    table, f"{existing[table]:,}", f"{expected_min_rows:,}")
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {table} CASCADE;")

    log.info("[%s] introspecting schema from %s", table, list_suffix)
    uris = list_uris_matching("/" + list_suffix)
    log.info("[%s]   found %d Parquet objects in R2", table, len(uris))
    if not uris:
        raise SystemExit(f"[{table}] no Parquet objects matching /{list_suffix}")
    sample_uris = sorted(uris)[-sample_count:] if len(uris) > sample_count else uris
    schema = union_parquet_schemas(sample_uris)
    log.info("[%s]   union schema: %d cols (sampled %d files)",
             table, len(schema), len(sample_uris))

    if column_allowlist:
        allow = {c.lower() for c in column_allowlist}
        schema_filtered = [(c, t) for c, t in schema if c.lower() in allow]
        log.info("[%s]   filtered to allowlist: %d cols (from %d)",
                 table, len(schema_filtered), len(schema))
        schema = schema_filtered

    ddl = make_create_table_ddl(table, schema, match_pattern=match_pattern)
    head = ddl.replace("\n", " ")[:140]
    log.info("[%s] applying CREATE: %s ...", table, head)
    with conn.cursor() as cur:
        cur.execute(ddl)


# --------------------------------------------------------------------------- #
# MV: NCUA Form 5300 granular schedules
# --------------------------------------------------------------------------- #
#
# Locked column contract (16 columns):
#   cu_number, cycle_date, ncua_year, ncua_quarter,
#   cu_name, state, city,
#   total_assets, total_loans, loans_60d_delinquent, net_worth,
#   net_worth_ratio, delinquency_ratio_60d,
#   total_income, total_expense, net_income.
#
# Source columns are VARCHAR in Parquet (DuckDB all_varchar=TRUE at ingest);
# the MV explicitly CASTs the financial columns to DOUBLE for arithmetic.
# RW supports plain CAST (not TRY_CAST), so we use a DOUBLE-coerced subquery
# pattern with a defensive NULLIF to guard against bad rows.


def _resolve(cols: set[str], *candidates: str) -> str | None:
    for c in candidates:
        if c.lower() in cols:
            return c.lower()
    return None


def make_mv_ddl(
    foicu_cols: set[str],
    fs220_cols: set[str],
    fs220a_cols: set[str],
) -> str:
    # FOICU join keys + display
    cu_number_foicu = _resolve(foicu_cols, "cu_number", "cunumber")
    cycle_date_foicu = _resolve(foicu_cols, "cycle_date", "cycledate")
    cu_name_col = _resolve(foicu_cols, "cu_name", "cuname")
    state_col = _resolve(foicu_cols, "state", "statecode", "state_code")
    city_col = _resolve(foicu_cols, "city")
    year_col = _resolve(foicu_cols, "ncua_year", "year")
    quarter_col = _resolve(foicu_cols, "ncua_quarter", "quarter")

    # FS220 distress signals
    total_assets_col = _resolve(fs220_cols, "acct_010", "acct010")
    total_loans_col = _resolve(fs220_cols, "acct_025b", "acct_025")
    delq_60d_col = _resolve(fs220_cols, "acct_041b", "acct_041")
    net_worth_col = _resolve(fs220_cols, "acct_997", "acct997")
    fs220_cu_col = _resolve(fs220_cols, "cu_number", "cunumber")
    fs220_cycle_col = _resolve(fs220_cols, "cycle_date", "cycledate")

    # FS220A income statement (best-effort; codes are NCUA Form 5300 Schedule A).
    # If FS220A is unavailable (empty fs220a_cols), all income statement signals
    # ship NULL — the MV column contract is still satisfied.
    has_fs220a = bool(fs220a_cols)
    fs220a_cu_col = _resolve(fs220a_cols, "cu_number", "cunumber") if has_fs220a else None
    fs220a_cycle_col = _resolve(fs220a_cols, "cycle_date", "cycledate") if has_fs220a else None
    income_cols = [
        _resolve(fs220a_cols, "acct_115", "acct115"),
        _resolve(fs220a_cols, "acct_117", "acct117"),
        _resolve(fs220a_cols, "acct_119", "acct119"),
    ] if has_fs220a else [None, None, None]
    expense_col = _resolve(fs220a_cols, "acct_131", "acct131", "acct_124", "acct124") if has_fs220a else None
    net_income_col = _resolve(fs220a_cols, "acct_270", "acct270", "acct_661", "acct661") if has_fs220a else None

    if not cu_number_foicu or not cycle_date_foicu:
        raise SystemExit(
            f"required FOICU columns missing — cu_number={cu_number_foicu} "
            f"cycle_date={cycle_date_foicu}"
        )
    if not fs220_cu_col or not fs220_cycle_col:
        raise SystemExit(
            f"required FS220 columns missing — cu_number={fs220_cu_col} "
            f"cycle_date={fs220_cycle_col}"
        )

    def _double(col: str | None) -> str:
        if col is None:
            return "CAST(NULL AS DOUBLE PRECISION)"
        return f'CAST(NULLIF(f220."{col}", \'\') AS DOUBLE PRECISION)'

    def _double_a(col: str | None) -> str:
        if col is None:
            return "CAST(NULL AS DOUBLE PRECISION)"
        return f'CAST(NULLIF(f220a."{col}", \'\') AS DOUBLE PRECISION)'

    # Total income: sum of present columns; missing columns drop out via COALESCE.
    income_terms = [
        f'COALESCE({_double_a(c)}, 0)' for c in income_cols if c is not None
    ]
    if income_terms:
        total_income_expr = " + ".join(income_terms)
    else:
        total_income_expr = "CAST(NULL AS DOUBLE PRECISION)"

    expense_expr = _double_a(expense_col)
    net_income_expr = _double_a(net_income_col)

    total_assets_expr = _double(total_assets_col)
    total_loans_expr = _double(total_loans_col)
    delq_60d_expr = _double(delq_60d_col)
    net_worth_expr = _double(net_worth_col)

    # Ratios: NULLIF guards divide-by-zero
    nwr_expr = f"({net_worth_expr}) / NULLIF({total_assets_expr}, 0)"
    delq_ratio_expr = f"({delq_60d_expr}) / NULLIF({total_loans_expr}, 0)"

    fs220a_join = ""
    if has_fs220a and fs220a_cu_col and fs220a_cycle_col:
        fs220a_join = (
            f"LEFT JOIN {FS220A_TABLE} f220a\n"
            f"  ON f220a.\"{fs220a_cu_col}\" = f.\"{cu_number_foicu}\"\n"
            f" AND f220a.\"{fs220a_cycle_col}\" = f.\"{cycle_date_foicu}\""
        )

    return f"""\
CREATE MATERIALIZED VIEW {MV_NAME} AS
SELECT
    f."{cu_number_foicu}" AS cu_number,
    f."{cycle_date_foicu}" AS cycle_date,
    {('f.' + _quote_ident(year_col)) if year_col else 'CAST(NULL AS SMALLINT)'} AS ncua_year,
    {('f.' + _quote_ident(quarter_col)) if quarter_col else 'CAST(NULL AS SMALLINT)'} AS ncua_quarter,
    {('f.' + _quote_ident(cu_name_col)) if cu_name_col else "CAST(NULL AS VARCHAR)"} AS cu_name,
    {('UPPER(f.' + _quote_ident(state_col) + ')') if state_col else "CAST(NULL AS VARCHAR)"} AS state,
    {('f.' + _quote_ident(city_col)) if city_col else "CAST(NULL AS VARCHAR)"} AS city,
    {total_assets_expr} AS total_assets,
    {total_loans_expr} AS total_loans,
    {delq_60d_expr} AS loans_60d_delinquent,
    {net_worth_expr} AS net_worth,
    {nwr_expr} AS net_worth_ratio,
    {delq_ratio_expr} AS delinquency_ratio_60d,
    {total_income_expr} AS total_income,
    {expense_expr} AS total_expense,
    {net_income_expr} AS net_income
FROM {FOICU_TABLE} f
LEFT JOIN {FS220_TABLE} f220
  ON f220."{fs220_cu_col}" = f."{cu_number_foicu}"
 AND f220."{fs220_cycle_col}" = f."{cycle_date_foicu}"
{fs220a_join}
WHERE f."{cu_number_foicu}" IS NOT NULL;
"""


# --------------------------------------------------------------------------- #
# Apply / hydrate / validate
# --------------------------------------------------------------------------- #


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
    "cu_number", "cycle_date", "ncua_year", "ncua_quarter",
    "cu_name", "state", "city",
    "total_assets", "total_loans", "loans_60d_delinquent", "net_worth",
    "net_worth_ratio", "delinquency_ratio_60d",
    "total_income", "total_expense", "net_income",
)


def run_validation_gate(conn: psycopg.Connection) -> int:
    failures: list[str] = []
    with conn.cursor() as cur:
        for table, min_rows in (
            (FOICU_TABLE, NCUA_MIN_FOICU_ROWS),
            (FS220_TABLE, NCUA_MIN_FS220_ROWS),
            (FS220A_TABLE, NCUA_MIN_FS220A_ROWS),
            (MV_NAME, MV_MIN_ROWS),
        ):
            cur.execute(f"SELECT count(*) FROM {table};")
            rc = int(cur.fetchone()[0])
            log.info("  %-40s %s rows", table + ":", f"{rc:>12,}")
            if rc < min_rows:
                failures.append(f"{table} rows {rc:,} < {min_rows:,}")

        # Column contract check
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


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--validate-only", action="store_true")
    p.add_argument("--skip-hydration-wait", action="store_true")
    p.add_argument("--mv-only", action="store_true",
                   help="Skip source DDL; rebuild only the MV.")
    p.add_argument("--force-rebuild-sources", action="store_true",
                   help="Drop and recreate all 3 sources unconditionally.")
    p.add_argument("--no-fs220a", action="store_true",
                   help="Skip FS220A source creation (income statement signals "
                        "ship NULL in the MV). Workaround for RW Cloud trial CU "
                        "hydration stalls; FS220A can be added in a follow-up.")
    args = p.parse_args()

    if args.validate_only:
        with _rw_conn() as conn:
            conn.autocommit = True
            return run_validation_gate(conn)

    # 1. Discover existing state in RW
    with _rw_conn() as conn:
        conn.autocommit = True
        existing = get_existing_tables(conn)
    log.info("existing RW state: %s", existing)

    if args.force_rebuild_sources:
        log.info("--force-rebuild-sources: dropping all NCUA sources")
        with _rw_conn() as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(f"DROP MATERIALIZED VIEW IF EXISTS {MV_NAME};")
                cur.execute(f"DROP TABLE IF EXISTS {FS220A_TABLE} CASCADE;")
                cur.execute(f"DROP TABLE IF EXISTS {FS220_TABLE} CASCADE;")
                cur.execute(f"DROP TABLE IF EXISTS {FOICU_TABLE} CASCADE;")
            existing = {}

    # 2. Ensure each source exists with healthy row counts (smart-DDL)
    if not args.mv_only:
        with _rw_conn() as conn:
            conn.autocommit = True
            ensure_source(
                conn, table=FOICU_TABLE, match_pattern=FOICU_MATCH_PATTERN,
                list_suffix="foicu.parquet", existing=existing,
                expected_min_rows=NCUA_MIN_FOICU_ROWS,
            )
            ensure_source(
                conn, table=FS220_TABLE, match_pattern=FS220_MATCH_PATTERN,
                list_suffix="fs220.parquet", existing=existing,
                expected_min_rows=NCUA_MIN_FS220_ROWS,
            )
            if not args.no_fs220a:
                ensure_source(
                    conn, table=FS220A_TABLE, match_pattern=FS220A_MATCH_PATTERN,
                    list_suffix="fs220a.parquet", existing=existing,
                    expected_min_rows=NCUA_MIN_FS220A_ROWS,
                    column_allowlist=FS220A_COLUMN_ALLOWLIST,
                )
            else:
                log.info("--no-fs220a: skipping FS220A source creation")

    # 3. Wait for FS220A hydration (the new source); skip for ones already at full rows
    if not args.mv_only and not args.skip_hydration_wait and not args.no_fs220a:
        wait_for_hydration(FS220A_TABLE, expected_min_rows=NCUA_MIN_FS220A_ROWS)

    # 4. (Re)build the MV
    log.info("introspecting schemas for MV DDL generation")
    with _rw_conn() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT column_name FROM information_schema.columns
                 WHERE table_name = '{FOICU_TABLE}'
            """)
            foicu_cols = {r[0].lower() for r in cur.fetchall()}
            cur.execute(f"""
                SELECT column_name FROM information_schema.columns
                 WHERE table_name = '{FS220_TABLE}'
            """)
            fs220_cols = {r[0].lower() for r in cur.fetchall()}
            cur.execute(f"""
                SELECT column_name FROM information_schema.columns
                 WHERE table_name = '{FS220A_TABLE}'
            """)
            fs220a_cols = {r[0].lower() for r in cur.fetchall()}
    log.info("  FOICU=%d cols  FS220=%d cols  FS220A=%d cols",
             len(foicu_cols), len(fs220_cols), len(fs220a_cols))

    if not foicu_cols or not fs220_cols:
        raise SystemExit(
            "FOICU or FS220 source missing — re-run without --mv-only"
        )
    if not fs220a_cols and not args.no_fs220a:
        raise SystemExit(
            "FS220A source missing — re-run with --no-fs220a or without --mv-only"
        )

    ddl_drop_mv = f"DROP MATERIALIZED VIEW IF EXISTS {MV_NAME};"
    ddl_mv = make_mv_ddl(foicu_cols, fs220_cols, fs220a_cols)

    with _rw_conn() as conn:
        conn.autocommit = True
        apply_ddl(conn, ddl_drop_mv, label="drop-mv")
        apply_ddl(conn, ddl_mv, label="create-mv")

    # 5. Wait for MV hydration
    if not args.skip_hydration_wait:
        wait_for_hydration(MV_NAME, expected_min_rows=MV_MIN_ROWS)

    # 6. Validation gate
    with _rw_conn() as conn:
        conn.autocommit = True
        return run_validation_gate(conn)


if __name__ == "__main__":
    sys.exit(main())

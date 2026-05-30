#!/usr/bin/env python3
"""Apply RisingWave DDL for the SBA + NCUA Time-Machine (R2/RW Fuel Tank).

Sibling to apply_hmda_rw_volume_king.py + apply_dol_5500.sh. Same shape:

  1. Introspect Parquet schemas from R2 via DuckDB httpfs.
  2. Compute union schema across all SBA decade slices (column drift).
  3. Generate explicit-column CREATE TABLE DDL for the s3 connector.
  4. Apply DDL: drop-cascade existing, create tables, create MV.
  5. Wait for hydration.
  6. Run validation gate.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/apply_lending_stability_rw.py
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/apply_lending_stability_rw.py --validate-only
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/apply_lending_stability_rw.py --skip-ddl

The static SQL at risingwave/lending_stability_history.sql is a documentation
reference; this script is the authoritative apply path.
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

SBA_TABLE = "source_sba_historical"
NCUA_FOICU_TABLE = "source_ncua_foicu"
NCUA_FS220_TABLE = "source_ncua_fs220"
MV_NAME = "mv_lending_stability_history"

SBA_MATCH_PATTERN = "sba/program=*/decade=*/*.parquet"
NCUA_FOICU_MATCH_PATTERN = "ncua/year=*/quarter=Q*/foicu.parquet"
NCUA_FS220_MATCH_PATTERN = "ncua/year=*/quarter=Q*/fs220.parquet"

HYDRATION_POLL_INTERVAL_S = 30
HYDRATION_POLL_MAX_MIN = 30


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("apply-lending-stability-rw")


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
        return t_upper.replace("DECIMAL", "NUMERIC")
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
    """Return s3:// URIs for every object under a prefix."""
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
    """Return the union of (col, rw_type) across multiple Parquet files.
    First seen wins for type conflicts; promote to VARCHAR on disagreement."""
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
                    log.warning("col %s type drift %s vs VARCHAR — using VARCHAR",
                                col, union[col])
                    union[col] = "VARCHAR"
                elif union[col] == "VARCHAR" and dtype != "VARCHAR":
                    pass
                else:
                    log.warning("col %s type drift %s vs %s — keeping first (%s)",
                                col, union[col], dtype, union[col])
        for r in rows:
            col, dtype = r[0], _duckdb_type_to_rw(r[1])
            if col not in union:
                union[col] = dtype
    con.close()
    return [(c, t) for c, t in union.items()]


def introspect_one(s3_uri: str) -> list[tuple[str, str]]:
    log.info("introspecting Parquet schema: %s", s3_uri)
    con = _duck_with_r2()
    rows = con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{s3_uri}');"
    ).fetchall()
    con.close()
    return [(r[0], _duckdb_type_to_rw(r[1])) for r in rows]


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


# Distress signals from NCUA FS220 (per Form 5300 instructions; AcctDesc
# table in R2 documents each):
#   net_worth_ratio = ACCT_997 (net worth) / ACCT_010 (total assets)
#   delinquency_ratio_60d = ACCT_041B / ACCT_025B (60+ delq / total loans)


def make_mv_ddl(
    sba_schema: list[tuple[str, str]],
    foicu_schema: list[tuple[str, str]],
    fs220_schema: list[tuple[str, str]],
) -> str:
    sba_cols = {c.lower() for c, _ in sba_schema}
    foicu_cols = {c.lower() for c, _ in foicu_schema}
    fs220_cols = {c.lower() for c, _ in fs220_schema}

    sba_state_col = next(
        (c for c in ("borrstate", "projectstate", "borrowerstate") if c in sba_cols),
        None,
    )
    sba_naics_col = next(
        (c for c in ("naicscode", "naics_code") if c in sba_cols),
        None,
    )
    sba_amount_col = next(
        (c for c in ("grossapproval", "gross_approval", "loanamount") if c in sba_cols),
        None,
    )

    if not sba_state_col or not sba_amount_col:
        raise SystemExit(
            f"required SBA columns missing — state={sba_state_col} "
            f"amount={sba_amount_col} in schema {list(sba_cols)[:20]}"
        )

    # naicscode lands as DOUBLE PRECISION (the ingest TRY_CAST'd it). Cast to
    # BIGINT then VARCHAR so LEFT() works without scientific-notation noise.
    naics_clause = (
        f"LEFT(CAST(CAST(\"{sba_naics_col}\" AS BIGINT) AS VARCHAR), 2)"
        if sba_naics_col else "CAST(NULL AS VARCHAR)"
    )

    net_worth_col = next((c for c in ("acct_997", "acct997") if c in fs220_cols), None)
    total_assets_col = next((c for c in ("acct_010", "acct010") if c in fs220_cols), None)
    delq_60d_col = next((c for c in ("acct_041b", "acct_041") if c in fs220_cols), None)
    total_loans_col = next((c for c in ("acct_025b", "acct_025") if c in fs220_cols), None)

    # NCUA acct_* columns are VARCHAR in the Parquet (all_varchar=TRUE in the
    # NCUA ingest preserves leading zeros + sentinels). RisingWave 2.8 has no
    # TRY_CAST — gate on a numeric regex via CASE WHEN before ::DOUBLE PRECISION.
    def _safe_cast(col: str) -> str:
        return (
            f"CASE WHEN \"{col}\" ~ '^-?[0-9]+(\\.[0-9]+)?$' "
            f"THEN \"{col}\"::DOUBLE PRECISION ELSE NULL END"
        )

    nwr_expr = (
        f"AVG({_safe_cast(net_worth_col)} "
        f"/ NULLIF({_safe_cast(total_assets_col)}, 0))"
        if net_worth_col and total_assets_col
        else "CAST(NULL AS DOUBLE PRECISION)"
    )
    delq_expr = (
        f"AVG({_safe_cast(delq_60d_col)} "
        f"/ NULLIF({_safe_cast(total_loans_col)}, 0))"
        if delq_60d_col and total_loans_col
        else "CAST(NULL AS DOUBLE PRECISION)"
    )

    return f"""\
CREATE MATERIALIZED VIEW {MV_NAME} AS
WITH sba_agg AS (
    SELECT
        sba_program AS program,
        sba_decade AS decade_start_year,
        UPPER("{sba_state_col}") AS state,
        {naics_clause} AS naics_2digit,
        count(*) AS loan_count,
        SUM("{sba_amount_col}") AS loan_amount_total
    FROM {SBA_TABLE}
    WHERE "{sba_state_col}" IS NOT NULL AND "{sba_state_col}" <> ''
    GROUP BY 1, 2, 3, 4
),
ncua_distress AS (
    SELECT
        UPPER(f.state) AS state,
        f.ncua_year AS year,
        f.ncua_quarter AS quarter,
        count(*) AS cu_count,
        {nwr_expr} AS avg_net_worth_ratio,
        {delq_expr} AS avg_delinquency_ratio_60d
    FROM {NCUA_FOICU_TABLE} f
    LEFT JOIN {NCUA_FS220_TABLE} fs
      ON fs.cu_number = f.cu_number
     AND fs.ncua_year = f.ncua_year
     AND fs.ncua_quarter = f.ncua_quarter
    WHERE f.state IS NOT NULL AND f.state <> ''
    GROUP BY 1, 2, 3
),
ncua_decade_agg AS (
    SELECT
        state,
        (year / 10) * 10 AS decade_start_year,
        AVG(cu_count) AS cu_count_quarter_avg,
        AVG(avg_net_worth_ratio) AS avg_net_worth_ratio,
        AVG(avg_delinquency_ratio_60d) AS avg_delinquency_ratio_60d,
        count(DISTINCT year * 10 + quarter) AS quarters_observed
    FROM ncua_distress
    GROUP BY 1, 2
)
SELECT
    s.program,
    s.decade_start_year,
    s.state,
    s.naics_2digit,
    s.loan_count,
    s.loan_amount_total,
    n.cu_count_quarter_avg,
    n.avg_net_worth_ratio,
    n.avg_delinquency_ratio_60d,
    n.quarters_observed
FROM sba_agg s
LEFT JOIN ncua_decade_agg n
  ON n.state = s.state
 AND n.decade_start_year = s.decade_start_year;
"""


# --------------------------------------------------------------------------- #
# Apply / hydrate / validate
# --------------------------------------------------------------------------- #


def apply_ddl(conn: psycopg.Connection, ddl: str, *, label: str) -> None:
    head = ddl.replace("\n", " ")[:120]
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


def run_validation_gate(conn: psycopg.Connection) -> int:
    failures: list[str] = []
    with conn.cursor() as cur:
        for table in (SBA_TABLE, NCUA_FOICU_TABLE, NCUA_FS220_TABLE, MV_NAME):
            cur.execute(f"SELECT count(*) FROM {table};")
            rc = int(cur.fetchone()[0])
            log.info("  %-32s %s rows", table + ":", f"{rc:>12,}")

        cur.execute(f"SELECT count(*) FROM {SBA_TABLE};")
        sba_rows = int(cur.fetchone()[0])
        cur.execute(f"SELECT count(*) FROM {NCUA_FOICU_TABLE};")
        foicu_rows = int(cur.fetchone()[0])
        cur.execute(f"SELECT count(*) FROM {MV_NAME};")
        mv_rows = int(cur.fetchone()[0])

        if sba_rows < 1_500_000:
            failures.append(
                f"SBA row count {sba_rows} < 1.5M — directive lower bound "
                "for the historical 35-year ingest."
            )
        if foicu_rows < 100_000:
            failures.append(
                f"NCUA FOICU row count {foicu_rows} < 100K — expected ~5K rows "
                "× 39 quarters ≈ 195K. Hydration may not be complete."
            )
        if mv_rows < 50:
            failures.append(
                f"{MV_NAME} row count {mv_rows} < 50 — expected ≥ 50 (state × decade × program)."
            )

        cur.execute(f"""
            SELECT count(DISTINCT (program, decade_start_year, state))
              FROM {MV_NAME};
        """)
        distinct_keys = int(cur.fetchone()[0])
        log.info("  distinct (program, decade, state):  %s", f"{distinct_keys:>12,}")

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
    p.add_argument("--skip-ddl", action="store_true")
    args = p.parse_args()

    if args.validate_only:
        with _rw_conn() as conn:
            conn.autocommit = True
            return run_validation_gate(conn)

    if args.skip_ddl:
        log.info("--skip-ddl: skipping DDL apply, polling existing tables.")
        wait_for_hydration(NCUA_FOICU_TABLE, expected_min_rows=100_000)
        wait_for_hydration(SBA_TABLE, expected_min_rows=1_500_000)
        with _rw_conn() as conn:
            conn.autocommit = True
            return run_validation_gate(conn)

    sba_uris = list_r2_objects("sba/program=")
    sba_uris = [u for u in sba_uris if u.endswith(".parquet") and "/decade=" in u]
    log.info("SBA Parquet objects in R2: %d", len(sba_uris))
    if not sba_uris:
        raise SystemExit(
            "no SBA Parquet objects in R2 — run "
            "scripts/run_sba_historical_r2_ingest.py --all first"
        )

    foicu_uris = list_r2_objects("ncua/year=")
    foicu_uris = [u for u in foicu_uris if u.endswith("/foicu.parquet")]
    log.info("NCUA foicu Parquet objects in R2: %d", len(foicu_uris))
    if not foicu_uris:
        raise SystemExit(
            "no NCUA foicu Parquet objects in R2 — run "
            "scripts/run_ncua_call_report_r2_ingest.py --all first"
        )

    fs220_uris = list_r2_objects("ncua/year=")
    fs220_uris = [u for u in fs220_uris if u.endswith("/fs220.parquet")]
    log.info("NCUA fs220 Parquet objects in R2: %d", len(fs220_uris))
    if not fs220_uris:
        raise SystemExit("no NCUA fs220 Parquet objects in R2")

    log.info("computing SBA union schema across %d files", len(sba_uris))
    sba_schema = union_parquet_schemas(sba_uris)
    log.info("  SBA union: %d cols", len(sba_schema))

    foicu_schema = introspect_one(sorted(foicu_uris)[-1])
    log.info("  FOICU schema: %d cols", len(foicu_schema))
    fs220_schema = union_parquet_schemas(sorted(fs220_uris)[-4:])
    log.info("  FS220 union (last 4 quarters): %d cols", len(fs220_schema))

    ddl_drop_mv = f"DROP MATERIALIZED VIEW IF EXISTS {MV_NAME};"
    ddl_drop_sba = f"DROP TABLE IF EXISTS {SBA_TABLE} CASCADE;"
    ddl_drop_foicu = f"DROP TABLE IF EXISTS {NCUA_FOICU_TABLE} CASCADE;"
    ddl_drop_fs220 = f"DROP TABLE IF EXISTS {NCUA_FS220_TABLE} CASCADE;"

    ddl_sba = make_create_table_ddl(
        SBA_TABLE, sba_schema, match_pattern=SBA_MATCH_PATTERN,
    )
    ddl_foicu = make_create_table_ddl(
        NCUA_FOICU_TABLE, foicu_schema, match_pattern=NCUA_FOICU_MATCH_PATTERN,
    )
    ddl_fs220 = make_create_table_ddl(
        NCUA_FS220_TABLE, fs220_schema, match_pattern=NCUA_FS220_MATCH_PATTERN,
    )
    ddl_mv = make_mv_ddl(sba_schema, foicu_schema, fs220_schema)

    with _rw_conn() as conn:
        conn.autocommit = True
        apply_ddl(conn, ddl_drop_mv, label="drop-mv")
        apply_ddl(conn, ddl_drop_sba, label="drop-sba")
        apply_ddl(conn, ddl_drop_foicu, label="drop-foicu")
        apply_ddl(conn, ddl_drop_fs220, label="drop-fs220")
        apply_ddl(conn, ddl_sba, label="create-sba")
        apply_ddl(conn, ddl_foicu, label="create-foicu")
        apply_ddl(conn, ddl_fs220, label="create-fs220")
        apply_ddl(conn, ddl_mv, label="create-mv")
        log.info("DDL applied.")

    if not args.skip_hydration_wait:
        wait_for_hydration(NCUA_FOICU_TABLE, expected_min_rows=100_000)
        wait_for_hydration(SBA_TABLE, expected_min_rows=1_500_000)

    with _rw_conn() as conn:
        conn.autocommit = True
        return run_validation_gate(conn)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Apply RisingWave DDL for the SBA 7(a) + 504 FOIA historical R2/RW pipeline.

Adapted from the predecessor combined SBA+NCUA applier. SBA-only:

  1. Introspect Parquet schemas from R2 via DuckDB httpfs.
  2. Compute union schema across all 6 SBA decade slices (column drift).
  3. Generate explicit-column CREATE TABLE DDL for the s3 connector.
  4. Apply DDL: drop-cascade existing, create source, create MV.
  5. Wait for hydration.
  6. Run SBA-only validation gate (≥1.5M rows + MV column contract).

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/apply_sba_historical_rw.py
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/apply_sba_historical_rw.py --validate-only
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/apply_sba_historical_rw.py --skip-ddl

The static SQL at risingwave/sba_historical.sql is a documentation
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
MV_NAME = "mv_sba_historical_survivability"

SBA_MATCH_PATTERN = "sba/program=*/decade=*/*.parquet"

# RW Cloud trial: hydration runs at ~tens-of-thousands of rows/sec under
# trial CU cap. SBA is ~1.5M rows total across 6 Parquet files.
HYDRATION_POLL_INTERVAL_S = 30
HYDRATION_POLL_MAX_MIN = 30
SBA_MIN_ROWS = 1_500_000


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("apply-sba-historical-rw")


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
        # RisingWave does not support parameterized NUMERIC(p,s); use bare DECIMAL.
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
    First seen wins for type, with VARCHAR promotion on type drift."""
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
                    pass  # keep VARCHAR
                else:
                    log.warning("col %s type drift %s vs %s — keeping first (%s)",
                                col, union[col], dtype, union[col])
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
# MV: SBA historical survivability projection
# --------------------------------------------------------------------------- #
#
# Locked column contract (per directive verification check #11):
#   loan_id, program, approval_fiscal_year, decade, borrower_name,
#   borrower_state, borrower_zip, naics_code,
#   business_age_years_at_approval, gross_approval,
#   loan_status_canonical, charge_off_amount.
#
# Defensive column-name resolution: SBA schema drifts across decades.
# `borrname`/`borrowername`, `naicscode`/`naics_code`, etc.


def _resolve(cols: set[str], *candidates: str) -> str | None:
    for c in candidates:
        if c.lower() in cols:
            return c.lower()
    return None


def make_mv_ddl(sba_schema: list[tuple[str, str]]) -> str:
    cols = {c.lower() for c, _ in sba_schema}

    state_col = _resolve(cols, "borrstate", "projectstate", "borrowerstate")
    name_col = _resolve(cols, "borrname", "borrowername")
    zip_col = _resolve(cols, "borrzip", "borrowerzip", "zipcode")
    naics_col = _resolve(cols, "naicscode", "naics_code")
    amount_col = _resolve(cols, "grossapproval", "gross_approval", "loanamount")
    fy_col = _resolve(cols, "approvalfiscalyear", "approvalfy", "fiscal_year")
    bus_age_col = _resolve(cols, "businessage", "business_age")
    status_col = _resolve(cols, "loanstatus", "loan_status")
    co_col = _resolve(cols, "chargeoffamount", "grosschargeoffamount", "grosschargeoffamt")

    if not state_col or not amount_col:
        raise SystemExit(
            f"required SBA columns missing — state={state_col} "
            f"amount={amount_col} in schema {sorted(cols)[:20]}"
        )

    # RisingWave does not support TRY_CAST. Source columns are already typed
    # correctly via DuckDB's TRY_CAST at ingest time (numeric cols → DOUBLE,
    # date cols → DATE, text cols → VARCHAR). Bare references in the MV are
    # type-safe; no further coercion needed for numeric cols.
    def _expr(col: str | None, *, varchar: bool = False) -> str:
        if col is None:
            return "CAST(NULL AS VARCHAR)" if varchar else "CAST(NULL AS DOUBLE PRECISION)"
        return f'"{col}"'

    name_expr = _expr(name_col, varchar=True)
    state_expr = f'UPPER("{state_col}")'
    zip_expr = _expr(zip_col, varchar=True)
    if zip_col:
        # zip may be DOUBLE-cast — coerce back to VARCHAR for downstream join.
        zip_expr = f'CAST("{zip_col}" AS VARCHAR)'
    naics_expr = _expr(naics_col, varchar=True)
    if naics_col:
        naics_expr = f'CAST("{naics_col}" AS VARCHAR)'
    amount_expr = f'"{amount_col}"'  # already DOUBLE in source
    fy_expr = _expr(fy_col, varchar=True)
    if fy_col:
        fy_expr = f'CAST("{fy_col}" AS VARCHAR)'
    bus_age_expr = (
        f'"{bus_age_col}"'  # already DOUBLE in source
        if bus_age_col else "CAST(NULL AS DOUBLE PRECISION)"
    )
    status_expr = _expr(status_col, varchar=True)
    co_expr = (
        f'"{co_col}"'  # already DOUBLE in source
        if co_col else "CAST(NULL AS DOUBLE PRECISION)"
    )

    # Canonicalize loan_status:
    #   PIF/Paid in Full       -> 'paid_in_full'
    #   CHGOFF/Charged Off     -> 'charged_off'
    #   CANCLD/Cancelled       -> 'cancelled'
    #   EXEMPT                 -> 'exempt'
    #   COMMIT/Committed       -> 'committed'
    #   else                   -> raw lowercased status
    if status_col:
        canonical_expr = f"""
        CASE
          WHEN UPPER("{status_col}") IN ('PIF','PAID IN FULL','PAIDINFULL') THEN 'paid_in_full'
          WHEN UPPER("{status_col}") IN ('CHGOFF','CHARGED OFF','CHARGEDOFF') THEN 'charged_off'
          WHEN UPPER("{status_col}") IN ('CANCLD','CANCELLED','CANCELED') THEN 'cancelled'
          WHEN UPPER("{status_col}") = 'EXEMPT' THEN 'exempt'
          WHEN UPPER("{status_col}") IN ('COMMIT','COMMITTED') THEN 'committed'
          ELSE LOWER("{status_col}")
        END
        """.strip()
    else:
        canonical_expr = "CAST(NULL AS VARCHAR)"

    # loan_id: SBA FOIA CSVs have no stable per-row identifier. Synthesize a
    # composite from (program, decade, borrower-name, approval_date hash). We
    # use a stable hash for join-key purposes; collisions across programs
    # are vanishingly unlikely given the inputs.
    approval_date_col = _resolve(cols, "approvaldate", "approval_date")
    loan_id_inputs = [f'"sba_program"', f'CAST("sba_decade" AS VARCHAR)']
    if name_col:
        loan_id_inputs.append(f'COALESCE("{name_col}",\'\')')
    if approval_date_col:
        loan_id_inputs.append(f'COALESCE(CAST("{approval_date_col}" AS VARCHAR),\'\')')
    if amount_col:
        loan_id_inputs.append(f'COALESCE(CAST("{amount_col}" AS VARCHAR),\'\')')
    loan_id_concat = " || '|' || ".join(loan_id_inputs)
    # md5 hash → BIGINT-shaped key as VARCHAR to keep MV column type stable.
    loan_id_expr = f"md5({loan_id_concat})"

    return f"""\
CREATE MATERIALIZED VIEW {MV_NAME} AS
SELECT
    {loan_id_expr} AS loan_id,
    "sba_program"::VARCHAR AS program,
    {fy_expr} AS approval_fiscal_year,
    "sba_decade"::SMALLINT AS decade,
    {name_expr} AS borrower_name,
    {state_expr} AS borrower_state,
    {zip_expr} AS borrower_zip,
    {naics_expr} AS naics_code,
    {bus_age_expr} AS business_age_years_at_approval,
    {amount_expr} AS gross_approval,
    {canonical_expr} AS loan_status_canonical,
    {co_expr} AS charge_off_amount
FROM {SBA_TABLE}
WHERE "{state_col}" IS NOT NULL AND "{state_col}" <> '';
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


REQUIRED_MV_COLUMNS = (
    "loan_id", "program", "approval_fiscal_year", "decade",
    "borrower_name", "borrower_state", "borrower_zip", "naics_code",
    "business_age_years_at_approval", "gross_approval",
    "loan_status_canonical", "charge_off_amount",
)


def run_validation_gate(conn: psycopg.Connection) -> int:
    failures: list[str] = []
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {SBA_TABLE};")
        sba_rows = int(cur.fetchone()[0])
        log.info("  %-40s %s rows", SBA_TABLE + ":", f"{sba_rows:>12,}")

        cur.execute(f"SELECT count(*) FROM {MV_NAME};")
        mv_rows = int(cur.fetchone()[0])
        log.info("  %-40s %s rows", MV_NAME + ":", f"{mv_rows:>12,}")

        if sba_rows < SBA_MIN_ROWS:
            failures.append(
                f"{SBA_TABLE} row count {sba_rows:,} < {SBA_MIN_ROWS:,} — "
                "directive lower bound for the historical 35-year ingest."
            )
        if mv_rows < SBA_MIN_ROWS // 2:
            failures.append(
                f"{MV_NAME} row count {mv_rows:,} < {SBA_MIN_ROWS // 2:,} — "
                "MV should be roughly the same cardinality as the source "
                "(state-filter drops <50%)."
            )

        # Distinct (program, decade, borrower_state) sanity check
        cur.execute(f"""
            SELECT count(DISTINCT (program, decade, borrower_state))
              FROM {MV_NAME};
        """)
        distinct_keys = int(cur.fetchone()[0])
        log.info("  distinct (program, decade, state):       %s",
                 f"{distinct_keys:>12,}")

        # Column contract: every required MV column must exist.
        cur.execute(f"""
            SELECT column_name FROM information_schema.columns
             WHERE table_name = '{MV_NAME}'
             ORDER BY ordinal_position;
        """)
        actual_cols = {r[0].lower() for r in cur.fetchall()}
        missing = [c for c in REQUIRED_MV_COLUMNS if c.lower() not in actual_cols]
        if missing:
            failures.append(
                f"{MV_NAME} is missing required columns: {missing}"
            )

        # Spine columns (slot 20260520000001) must be present on raw_entity_records.
        # We can only check this on Postgres, but RW connection is fine for a
        # cross-DB sanity log; the full spine check happens in the benchmark.

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
    p.add_argument("--skip-ddl", action="store_true")
    args = p.parse_args()

    if args.validate_only:
        with _rw_conn() as conn:
            conn.autocommit = True
            return run_validation_gate(conn)

    if args.skip_ddl:
        log.info("--skip-ddl: skipping DDL apply, polling existing source.")
        wait_for_hydration(SBA_TABLE, expected_min_rows=SBA_MIN_ROWS)
        with _rw_conn() as conn:
            conn.autocommit = True
            return run_validation_gate(conn)

    # 1. Discover R2 objects + introspect schemas.
    # Filter to historical 7(a) + 504 paths only — `sba/program=ppp/segment=*`
    # objects coexist in the same prefix but have a different schema and are
    # NOT part of this directive.
    all_uris = list_r2_objects("sba/program=")
    sba_uris = [
        u for u in all_uris
        if u.endswith(".parquet")
        and ("/program=7a/decade=" in u or "/program=504/decade=" in u)
    ]
    log.info("SBA historical Parquet objects in R2: %d (of %d total under sba/)",
             len(sba_uris), len(all_uris))
    if not sba_uris:
        raise SystemExit(
            "no SBA historical Parquet objects in R2 — "
            "run scripts/run_sba_historical_r2_ingest.py --all first"
        )

    log.info("computing SBA union schema across %d files", len(sba_uris))
    sba_schema = union_parquet_schemas(sba_uris)
    log.info("  SBA union: %d cols", len(sba_schema))

    # 2. Generate DDL
    ddl_drop_mv = f"DROP MATERIALIZED VIEW IF EXISTS {MV_NAME};"
    ddl_drop_sba = f"DROP TABLE IF EXISTS {SBA_TABLE} CASCADE;"
    ddl_sba = make_create_table_ddl(
        SBA_TABLE, sba_schema, match_pattern=SBA_MATCH_PATTERN,
    )
    ddl_mv = make_mv_ddl(sba_schema)

    # 3. Apply
    with _rw_conn() as conn:
        conn.autocommit = True
        apply_ddl(conn, ddl_drop_mv, label="drop-mv")
        apply_ddl(conn, ddl_drop_sba, label="drop-sba")
        apply_ddl(conn, ddl_sba, label="create-sba")
        apply_ddl(conn, ddl_mv, label="create-mv")
        log.info("DDL applied.")

    if not args.skip_hydration_wait:
        wait_for_hydration(SBA_TABLE, expected_min_rows=SBA_MIN_ROWS)

    with _rw_conn() as conn:
        conn.autocommit = True
        return run_validation_gate(conn)


if __name__ == "__main__":
    sys.exit(main())

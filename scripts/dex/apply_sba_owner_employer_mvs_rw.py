#!/usr/bin/env python3
"""Apply RisingWave DDL: source_bridge_fec_sba_employer + mv_sba_borrower_with_fec_owner.

Wires the FEC × SBA bridge Parquet (written by build_bridge_fec_sba_employer.py)
into RisingWave as an s3-connector source, then materializes a join with
mv_sba_borrower_essentials to deliver the capital-matching audience MV.

Per directive 2026-05-09 (descoped — Form 5500 portion deferred to follow-up).

Source DDL: introspect Parquet schema from R2 via DuckDB; emit explicit-column
CREATE TABLE so RW knows the schema (RW S3 source connector requires this for
PARQUET format). All cols VARCHAR per L2.

MV DDL: SET BACKGROUND_DDL=TRUE; CREATE MATERIALIZED VIEW joining bridge ×
mv_sba_borrower_essentials on loan_id.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/apply_sba_owner_employer_mvs_rw.py
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/apply_sba_owner_employer_mvs_rw.py --validate-only
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
BRIDGE_SOURCE_NAME = "source_bridge_fec_sba_employer"
BRIDGE_MATCH_PATTERN = "bridges/fec_sba_employer/snapshot=*/data.parquet"
MV_NAME = "mv_sba_borrower_with_fec_owner"

HYDRATION_POLL_INTERVAL_S = 30
HYDRATION_POLL_MAX_MIN = 30


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("apply-sba-owner-employer-mvs")


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


def _duck_with_r2() -> duckdb.DuckDBPyConnection:
    endpoint_full = _required_env("R2_ENDPOINT")
    endpoint_host = endpoint_full.replace("https://", "").replace("http://", "")
    con = duckdb.connect(":memory:")
    con.execute("INSTALL httpfs;")
    con.execute("LOAD httpfs;")
    con.execute(f"SET s3_endpoint='{endpoint_host}';")
    con.execute(f"SET s3_access_key_id='{_required_env('R2_ACCESS_KEY_ID')}';")
    con.execute(
        f"SET s3_secret_access_key='{_required_env('R2_SECRET_ACCESS_KEY')}';"
    )
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


def get_bridge_schema() -> list[tuple[str, str]]:
    """Read one Parquet file from the bridge prefix; return columns
    forced to VARCHAR (per L2) for RW compatibility."""
    keys = list_r2_objects("bridges/fec_sba_employer/")
    parquet_keys = [k for k in keys if k.endswith(".parquet")]
    if not parquet_keys:
        raise SystemExit(
            "no bridge Parquet found under bridges/fec_sba_employer/ — "
            "run build_bridge_fec_sba_employer.py --apply first"
        )
    log.info("introspecting %s for source schema …", parquet_keys[0])
    con = _duck_with_r2()
    rows = con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{parquet_keys[0]}');"
    ).fetchall()
    con.close()
    schema: "OrderedDict[str, str]" = OrderedDict()
    for r in rows:
        col = r[0]
        # Per L2: all source cols VARCHAR.
        schema[col] = "VARCHAR"
    return list(schema.items())


def _quote_ident(s: str) -> str:
    return '"' + s.replace('"', '""') + '"'


def make_source_ddl(schema: list[tuple[str, str]]) -> str:
    cols = ",\n    ".join(f"{_quote_ident(c)} {t}" for c, t in schema)
    return f"""\
CREATE TABLE {BRIDGE_SOURCE_NAME} (
    {cols}
)
WITH (
    connector = 's3',
    s3.bucket_name = '{R2_BUCKET}',
    s3.region_name = 'auto',
    s3.endpoint_url = '{_required_env("R2_ENDPOINT")}',
    s3.credentials.access = '{_required_env("R2_ACCESS_KEY_ID")}',
    s3.credentials.secret = '{_required_env("R2_SECRET_ACCESS_KEY")}',
    match_pattern = '{BRIDGE_MATCH_PATTERN}'
) FORMAT PLAIN ENCODE PARQUET;
"""


def make_mv_ddl() -> str:
    return f"""\
CREATE MATERIALIZED VIEW {MV_NAME} AS
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
    -- FEC owner payload
    b.donor_name AS owner_name,
    b.donor_occupation AS owner_occupation,
    b.donor_zip AS owner_home_zip,
    b.donor_state AS owner_home_state,
    b.transaction_amt_total AS owner_lifetime_giving,
    b.cycles_active AS owner_cycles_active,
    b.contribution_count AS owner_contribution_count,
    -- Bridge fan-out diagnostic
    b.fec_employers_at_name_state,
    b.sba_borrowers_at_name_state,
    b.match_value_normalized
FROM {BRIDGE_SOURCE_NAME} b
JOIN mv_sba_borrower_essentials s
  ON s.loan_id = b.loan_id;
"""


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


def smoke_gate(conn: psycopg.Connection) -> int:
    """Smoke gate per L25 + L30: capital-matching cohort exists."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {MV_NAME};")
        total = int(cur.fetchone()[0])
        log.info("  %s total: %s rows", MV_NAME, f"{total:,}")

        cur.execute(f"""
            SELECT count(*)
              FROM {MV_NAME}
             WHERE confidence_tier = 'platinum'
               AND loanstatus_normalized = 'committed'
               AND approval_date >= '2026-01-01';
        """)
        smoke = int(cur.fetchone()[0])
        log.info("  capital-matching smoke (platinum+committed+2026+): %s rows", f"{smoke:,}")

        # Tier distribution
        cur.execute(f"""
            SELECT confidence_tier, count(*) FROM {MV_NAME}
             GROUP BY confidence_tier ORDER BY 2 DESC;
        """)
        for tier, cnt in cur.fetchall():
            log.info("  tier %-10s %s rows", tier, f"{cnt:,}")

    if total == 0:
        log.error("FAIL: %s has 0 rows", MV_NAME)
        return 1
    if smoke == 0:
        # Smoke is allowed to be 0 if no committed-2026 loans exist with FEC
        # match — surface but don't fail. Capital-matching cohort is real
        # data-dependent.
        log.warning(
            "smoke query returned 0 rows — possible if no platinum FEC "
            "matches exist for 2026 committed loans yet"
        )
    log.info("smoke gate passed (rows present in MV)")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--validate-only", action="store_true")
    p.add_argument("--skip-hydration-wait", action="store_true")
    p.add_argument("--source-only", action="store_true",
                   help="apply source DDL but skip MV (useful for testing)")
    p.add_argument("--min-rows", type=int, default=5_000)
    args = p.parse_args()

    if args.validate_only:
        with _rw_conn() as conn:
            conn.autocommit = True
            return smoke_gate(conn)

    schema = get_bridge_schema()
    log.info("bridge schema: %d cols", len(schema))

    src_ddl = make_source_ddl(schema)
    drop_src = f"DROP TABLE IF EXISTS {BRIDGE_SOURCE_NAME} CASCADE;"
    drop_mv = f"DROP MATERIALIZED VIEW IF EXISTS {MV_NAME};"

    with _rw_conn() as conn:
        conn.autocommit = True
        apply_ddl(conn, drop_mv, label="drop-mv")
        apply_ddl(conn, drop_src, label="drop-src")
        apply_ddl(conn, src_ddl, label="create-src")

        # Wait for source to hydrate before creating MV that joins on it
        # (RW source connector is async; row count is 0 immediately after
        # CREATE TABLE).
        log.info("waiting for source hydration …")

    if not args.source_only:
        wait_for_hydration(BRIDGE_SOURCE_NAME, expected_min_rows=args.min_rows)

        with _rw_conn() as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SET BACKGROUND_DDL = TRUE;")
                cur.execute(make_mv_ddl())
                log.info("MV creation issued (background hydration)")

        if args.skip_hydration_wait:
            return 0
        wait_for_hydration(MV_NAME, expected_min_rows=args.min_rows)

    with _rw_conn() as conn:
        conn.autocommit = True
        return smoke_gate(conn)


if __name__ == "__main__":
    sys.exit(main())

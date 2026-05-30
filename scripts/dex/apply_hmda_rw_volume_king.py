#!/usr/bin/env python3
"""Apply RisingWave DDL for the HMDA Volume King (R2/RW Fuel Tank pattern).

Pipeline:
  1. Introspect Parquet schema from R2 via DuckDB httpfs (LAR + Panel).
  2. Generate CREATE TABLE DDL with explicit columns + s3 connector.
     RisingWave 2.8.x rejects both bare CREATE SOURCE without columns AND
     CREATE TABLE (*) for Parquet — explicit schema is required.
  3. Apply DDL: drop-cascade existing, create tables, create MV.
  4. Wait for hydration (Parquet → RW storage; ~minutes for 12M rows).
  5. Run validation gate (row counts, NULL rates, LEI-join cardinality).

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/apply_hmda_rw_volume_king.py

  # Validate-only against existing objects, skip DDL:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/apply_hmda_rw_volume_king.py --validate-only

The static SQL at risingwave/hmda_volume_king.sql is a documentation
reference; this script is the authoritative apply path.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

import duckdb
import psycopg


R2_BUCKET = "dex-raw-landing-zone"
LAR_S3_URI = f"s3://{R2_BUCKET}/hmda/lar/year=2024/lar_2024.parquet"
PANEL_S3_URI = f"s3://{R2_BUCKET}/hmda/panel/year=2023/panel_2023.parquet"

LAR_TABLE = "source_hmda_lar_2024_r2"
PANEL_TABLE = "source_hmda_panel_2023_r2"
MV_NAME = "mv_hmda_analysis"

# Hydration polling: RW pulls Parquet from R2 after CREATE TABLE; the row
# count climbs from 0 to final over a few minutes for ~12M rows.
HYDRATION_POLL_INTERVAL_S = 30
HYDRATION_POLL_MAX_MIN = 30


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("apply-hmda-rw")


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
    """Map DuckDB DESCRIBE types to RisingWave types. Both speak Postgres
    protocol so most types align verbatim."""
    t_upper = t.upper().strip()
    # Common direct matches
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
    # Fallback: keep VARCHAR; safer than guessing wrong.
    log.warning("Unknown DuckDB type %r — falling back to VARCHAR", t)
    return "VARCHAR"


def introspect_parquet_schema(s3_uri: str) -> list[tuple[str, str]]:
    """Returns [(col_name, rw_type)] read from the Parquet at s3_uri."""
    log.info("introspecting Parquet schema: %s", s3_uri)
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

    rows = con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{s3_uri}');"
    ).fetchall()
    con.close()
    out = [(r[0], _duckdb_type_to_rw(r[1])) for r in rows]
    log.info("  %d columns in %s", len(out), s3_uri.split("/")[-1])
    return out


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


# Hot column set kept narrow — secondary applicant demographics, denial
# reasons, AUS, and tract percentiles intentionally omitted (consume from
# source_hmda_lar_2024_r2 directly if needed).
MV_HOT_LAR_COLS = (
    "lei",
    "activity_year",
    "dataset_year",
    "state_code",
    "county_code",
    "census_tract",
    "action_taken",
    "loan_type",
    "loan_purpose",
    "lien_status",
    "occupancy_type",
    "loan_amount",
    "property_value",
    "income",
    "interest_rate",
    "rate_spread",
    "combined_loan_to_value_ratio",
    "loan_term",
    "derived_loan_product_type",
    "derived_dwelling_category",
    "derived_msa_md",
)
MV_HOT_PANEL_COLS = (
    ("respondent_name", "lender_name"),
    ("respondent_state", "lender_state"),
    ("respondent_city", "lender_city"),
    ("assets", "lender_assets_thousands"),
    ("parent_name", "lender_parent_name"),
    ("topholder_name", "lender_topholder_name"),
)


def make_mv_ddl(
    lar_schema: list[tuple[str, str]],
    panel_schema: list[tuple[str, str]],
) -> str:
    lar_col_set = {c for c, _ in lar_schema}
    panel_col_set = {c for c, _ in panel_schema}

    lar_select_parts: list[str] = []
    for c in MV_HOT_LAR_COLS:
        if c in lar_col_set:
            lar_select_parts.append(f"l.{_quote_ident(c)}")
        else:
            log.warning("MV hot col %r not in LAR schema — omitting", c)

    panel_select_parts: list[str] = []
    for src_col, alias in MV_HOT_PANEL_COLS:
        if src_col in panel_col_set:
            panel_select_parts.append(
                f"p.{_quote_ident(src_col)} AS {_quote_ident(alias)}"
            )
        else:
            log.warning("MV hot Panel col %r not in Panel schema — omitting", src_col)

    select_list = ",\n    ".join(lar_select_parts + panel_select_parts)
    return f"""\
CREATE MATERIALIZED VIEW {MV_NAME} AS
SELECT
    {select_list}
FROM {LAR_TABLE} l
LEFT JOIN {PANEL_TABLE} p ON p.{_quote_ident("lei")} = l.{_quote_ident("lei")};
"""


# --------------------------------------------------------------------------- #
# Apply / hydrate / validate
# --------------------------------------------------------------------------- #


def apply_ddl(conn: psycopg.Connection, ddl: str, *, label: str) -> None:
    head = ddl.replace("\n", " ")[:120]
    log.info("[%s] applying: %s ...", label, head)
    with conn.cursor() as cur:
        cur.execute(ddl)


def wait_for_hydration(
    table: str, *, expected_min_rows: int,
) -> int:
    """Polls a fresh connection per cycle. RW's frontend kills long-idle TLS
    connections mid-poll (SSL error: unexpected eof) — re-opening avoids it."""
    log.info(
        "waiting for %s to hydrate (expecting ≥ %s rows)",
        table, f"{expected_min_rows:,}",
    )
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
        cur.execute(f"SELECT count(*) FROM {LAR_TABLE};")
        lar_rows = int(cur.fetchone()[0])
        cur.execute(f"SELECT count(*) FROM {PANEL_TABLE};")
        panel_rows = int(cur.fetchone()[0])
        cur.execute(f"SELECT count(*) FROM {MV_NAME};")
        mv_rows = int(cur.fetchone()[0])

        log.info("=== validation gate ===")
        log.info("  %-32s %s rows", LAR_TABLE + ":", f"{lar_rows:>12,}")
        log.info("  %-32s %s rows", PANEL_TABLE + ":", f"{panel_rows:>12,}")
        log.info("  %-32s %s rows", MV_NAME + ":", f"{mv_rows:>12,}")

        if lar_rows < 10_000_000:
            failures.append(
                f"LAR row count {lar_rows} < 10M — HMDA 2024 LAR has ~12.2M rows; "
                "expected ≥ 10M as a sanity floor."
            )
        if panel_rows < 4_000 or panel_rows > 8_000:
            failures.append(
                f"Panel row count {panel_rows} outside expected 4K-8K range "
                "(2023 Panel typically ~5K rows)."
            )
        if mv_rows != lar_rows:
            failures.append(
                f"{MV_NAME} row count {mv_rows} != LAR row count {lar_rows} "
                "(LEFT JOIN should preserve LAR cardinality)."
            )

        # Null-rate sanity checks
        cur.execute(f"""
            SELECT
              count(*) FILTER (WHERE census_tract IS NULL OR census_tract = ''),
              count(*) FILTER (WHERE loan_amount IS NULL),
              count(*) FILTER (WHERE lei IS NULL OR lei = '')
              FROM {LAR_TABLE};
        """)
        ct_null, la_null, lei_null = cur.fetchone()
        ct_pct = 100.0 * ct_null / max(lar_rows, 1)
        la_pct = 100.0 * la_null / max(lar_rows, 1)
        lei_pct = 100.0 * lei_null / max(lar_rows, 1)
        log.info("  census_tract null:               %s (%.2f%%)",
                 f"{ct_null:>12,}", ct_pct)
        log.info("  loan_amount null:                %s (%.2f%%)",
                 f"{la_null:>12,}", la_pct)
        log.info("  lei null/empty:                  %s (%.2f%%)",
                 f"{lei_null:>12,}", lei_pct)
        if ct_pct > 5.0:
            failures.append(
                f"census_tract NULL rate {ct_pct:.2f}% > 5% — likely decoder issue."
            )
        if la_pct > 1.0:
            failures.append(
                f"loan_amount NULL rate {la_pct:.2f}% > 1% — TRY_CAST may have "
                "rejected too many rows; inspect the 'Exempt' sentinel handling."
            )
        if lei_pct > 0.5:
            failures.append(
                f"lei NULL/empty rate {lei_pct:.2f}% > 0.5%."
            )

        # Lender match cardinality
        cur.execute(f"""
            SELECT
              count(DISTINCT lei) AS lar_distinct_lei,
              count(DISTINCT lei) FILTER (WHERE lender_name IS NOT NULL) AS matched_to_panel
              FROM {MV_NAME};
        """)
        d_lei, matched = cur.fetchone()
        match_pct = 100.0 * (matched or 0) / max(d_lei or 1, 1)
        log.info(
            "  distinct LAR LEIs:               %s (%s matched to Panel = %.1f%%)",
            f"{d_lei:>12,}", f"{matched:,}", match_pct,
        )
        if match_pct < 50.0:
            failures.append(
                f"LAR↔Panel LEI match rate {match_pct:.1f}% < 50% — column-name "
                "mismatch (lei vs LEI casing) or wrong Panel year."
            )

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
    p.add_argument("--validate-only", action="store_true",
                   help="Skip DDL + hydration polling; run validation gate only.")
    p.add_argument("--skip-hydration-wait", action="store_true",
                   help="Apply DDL but don't poll for hydration.")
    p.add_argument("--skip-ddl", action="store_true",
                   help="Skip DDL apply (tables already exist); just poll "
                        "hydration + validate. Used to resume after a "
                        "transient connection drop mid-poll.")
    args = p.parse_args()

    if args.validate_only:
        with _rw_conn() as conn:
            conn.autocommit = True
            return run_validation_gate(conn)

    if args.skip_ddl:
        log.info("--skip-ddl: skipping DDL apply, polling existing tables.")
        wait_for_hydration(PANEL_TABLE, expected_min_rows=4000)
        wait_for_hydration(LAR_TABLE, expected_min_rows=10_000_000)
        with _rw_conn() as conn:
            conn.autocommit = True
            return run_validation_gate(conn)

    # 1. Introspect schemas
    lar_schema = introspect_parquet_schema(LAR_S3_URI)
    panel_schema = introspect_parquet_schema(PANEL_S3_URI)

    # 2. Generate DDL
    ddl_drop_lar = f"DROP TABLE IF EXISTS {LAR_TABLE} CASCADE;"
    ddl_drop_panel = f"DROP TABLE IF EXISTS {PANEL_TABLE} CASCADE;"
    ddl_drop_mv = f"DROP MATERIALIZED VIEW IF EXISTS {MV_NAME};"
    ddl_lar = make_create_table_ddl(
        LAR_TABLE, lar_schema,
        match_pattern="hmda/lar/year=2024/lar_2024.parquet",
    )
    ddl_panel = make_create_table_ddl(
        PANEL_TABLE, panel_schema,
        match_pattern="hmda/panel/year=2023/panel_2023.parquet",
    )
    ddl_mv = make_mv_ddl(lar_schema, panel_schema)

    # 3. Apply
    with _rw_conn() as conn:
        conn.autocommit = True
        # Drop in reverse dep order: MV first, then tables.
        apply_ddl(conn, ddl_drop_mv, label="drop-mv")
        apply_ddl(conn, ddl_drop_lar, label="drop-lar")
        apply_ddl(conn, ddl_drop_panel, label="drop-panel")
        apply_ddl(conn, ddl_panel, label="create-panel")
        apply_ddl(conn, ddl_lar, label="create-lar")
        apply_ddl(conn, ddl_mv, label="create-mv")
        log.info("DDL applied.")

    # Hydration polling uses fresh connections per cycle (RW kills idle TLS).
    if not args.skip_hydration_wait:
        wait_for_hydration(PANEL_TABLE, expected_min_rows=4000)
        wait_for_hydration(LAR_TABLE, expected_min_rows=10_000_000)

    with _rw_conn() as conn:
        conn.autocommit = True
        return run_validation_gate(conn)


if __name__ == "__main__":
    sys.exit(main())

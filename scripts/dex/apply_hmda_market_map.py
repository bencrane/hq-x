#!/usr/bin/env python3
"""Apply RisingWave DDL for the HMDA Market Map (multi-year LAR + geographic MVs).

Stacks on apply_hmda_rw_volume_king.py (the predecessor 2024-only ingest).
Independent: this script's relations do not touch source_hmda_lar_2024_r2
or mv_hmda_analysis.

Pipeline:
  1. Introspect Parquet schema from R2 via DuckDB httpfs (single year — LAR
     schema is stable since 2018 per CFPB).
  2. Generate CREATE TABLE DDL for source_hmda_lar_r2 with glob
     match_pattern across all years.
  3. Apply DDL: drop-cascade existing, create table, create MVs, create
     indexes.
  4. Wait for hydration (Parquet → RW storage; cap-bound on RW Cloud trial
     tier).
  5. Run validation gate.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/apply_hmda_market_map.py

  # Validate-only against existing relations:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/apply_hmda_market_map.py --validate-only

  # Apply DDL only, no hydration wait:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/apply_hmda_market_map.py --skip-hydration-wait

The static SQL at risingwave/hmda_market_map.sql is a documentation
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

# Schema-introspection source. LAR's column set is stable since 2018; we pick
# 2024 because it's known to be in R2 (predecessor's land target). If multi-
# year schema drift ever arrives, the per-year cast/projection in
# run_hmda_r2_ingest.py absorbs it before the file hits R2.
INTROSPECTION_S3_URI = f"s3://{R2_BUCKET}/hmda/lar/year=2024/lar_2024.parquet"

LAR_TABLE = "source_hmda_lar_r2"  # multi-year unified
MV_CREDIT_SUPPLY = "mv_market_map_credit_supply"
MV_LENDER_CONCENTRATION = "mv_market_map_lender_concentration"

# Glob across years. RW's s3 connector match_pattern supports glob.
LAR_MATCH_PATTERN = "hmda/lar/year=*/lar_*.parquet"

# Hydration polling: RW pulls Parquet from R2 incrementally.
HYDRATION_POLL_INTERVAL_S = 30
HYDRATION_POLL_MAX_MIN = 30


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("apply-hmda-market-map")


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
    """Map DuckDB DESCRIBE types to RisingWave types."""
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


def make_credit_supply_mv_ddl(lar_schema: list[tuple[str, str]]) -> str:
    """Per-tract per-year aggregation MV. action_taken codes per HMDA spec:
      1 = Loan originated
      3 = Application denied
      4 = Application withdrawn by applicant
      5 = File closed for incompleteness
    """
    lar_cols = {c for c, _ in lar_schema}
    # Tract-population columns may be absent in older years (pre-2018 used
    # different column names). Only project what's present.
    pop_col = "tract_population" if "tract_population" in lar_cols else None
    minority_col = (
        "tract_minority_population_percent"
        if "tract_minority_population_percent" in lar_cols
        else None
    )
    income_pct_col = (
        "tract_to_msa_income_percentage"
        if "tract_to_msa_income_percentage" in lar_cols
        else None
    )

    extras: list[str] = []
    if pop_col:
        extras.append(f"    max({pop_col}) AS tract_population")
    if minority_col:
        extras.append(f"    max({minority_col}) AS tract_minority_population_percent")
    if income_pct_col:
        extras.append(f"    max({income_pct_col}) AS tract_to_msa_income_percentage")

    # Credit-desert indicator: originated loans per 1K tract residents.
    if pop_col:
        extras.append(
            "    CASE\n"
            f"        WHEN max({pop_col}) > 0\n"
            "        THEN count(*) FILTER (WHERE action_taken = '1')::DOUBLE PRECISION\n"
            f"             / (max({pop_col}) / 1000.0)\n"
            "        ELSE NULL\n"
            "    END AS originations_per_1k_residents"
        )

    extras_sql = (",\n" + ",\n".join(extras)) if extras else ""

    return f"""\
CREATE MATERIALIZED VIEW {MV_CREDIT_SUPPLY} AS
SELECT
    state_code,
    county_code,
    census_tract,
    dataset_year,
    count(*) AS total_applications,
    count(*) FILTER (WHERE action_taken = '1') AS originated_count,
    count(*) FILTER (WHERE action_taken = '3') AS denied_count,
    count(*) FILTER (WHERE action_taken = '4') AS withdrawn_count,
    count(*) FILTER (WHERE action_taken = '5') AS closed_incomplete_count,
    sum(loan_amount) FILTER (WHERE action_taken = '1')
        AS originated_total_amount,
    avg(loan_amount) FILTER (WHERE action_taken = '1')
        AS originated_avg_amount,
    avg(interest_rate) FILTER (WHERE action_taken = '1' AND interest_rate IS NOT NULL)
        AS originated_avg_interest_rate,
    avg(rate_spread) FILTER (WHERE action_taken = '1' AND rate_spread IS NOT NULL)
        AS originated_avg_rate_spread,
    count(DISTINCT lei) AS distinct_lender_count,
    count(DISTINCT lei) FILTER (WHERE action_taken = '1')
        AS distinct_originating_lender_count{extras_sql}
FROM {LAR_TABLE}
WHERE state_code IS NOT NULL
  AND state_code != ''
  AND census_tract IS NOT NULL
  AND census_tract != ''
GROUP BY state_code, county_code, census_tract, dataset_year;
"""


def make_lender_concentration_mv_ddl() -> str:
    """Per-tract per-lender per-year aggregation MV. Used to surface the
    top-N lenders winning in specific neighborhoods.

    Cardinality bound: ~75K national tracts × ~5 years × ~50 active lenders
    per tract per year ≈ 18M rows worst case. In practice much smaller — most
    tracts have <10 active lenders per year.
    """
    return f"""\
CREATE MATERIALIZED VIEW {MV_LENDER_CONCENTRATION} AS
SELECT
    state_code,
    county_code,
    census_tract,
    dataset_year,
    lei,
    count(*) AS application_count,
    count(*) FILTER (WHERE action_taken = '1') AS originated_count,
    count(*) FILTER (WHERE action_taken = '3') AS denied_count,
    sum(loan_amount) FILTER (WHERE action_taken = '1') AS originated_total_amount
FROM {LAR_TABLE}
WHERE state_code IS NOT NULL
  AND state_code != ''
  AND census_tract IS NOT NULL
  AND census_tract != ''
  AND lei IS NOT NULL
  AND lei != ''
GROUP BY state_code, county_code, census_tract, dataset_year, lei;
"""


def make_indexes_ddl() -> list[str]:
    return [
        # Geographic lookup on the credit-supply MV
        f"CREATE INDEX idx_{MV_CREDIT_SUPPLY}_geo ON {MV_CREDIT_SUPPLY} "
        "(state_code, county_code, census_tract, dataset_year);",
        # Per-LEI lookup on lender concentration (drill-down: "where does lender X lend?")
        f"CREATE INDEX idx_{MV_LENDER_CONCENTRATION}_lei ON {MV_LENDER_CONCENTRATION} "
        "(lei, dataset_year);",
        # Geographic lookup on lender concentration (drill-down: "who lends in tract X?")
        f"CREATE INDEX idx_{MV_LENDER_CONCENTRATION}_geo ON {MV_LENDER_CONCENTRATION} "
        "(state_code, county_code, census_tract, dataset_year);",
    ]


def apply_ddl(conn: psycopg.Connection, ddl: str, *, label: str) -> None:
    head = ddl.replace("\n", " ")[:120]
    log.info("[%s] applying: %s ...", label, head)
    with conn.cursor() as cur:
        cur.execute(ddl)


def wait_for_hydration(table: str, *, expected_min_rows: int) -> int:
    """Polls a fresh connection per cycle. RW's frontend kills long-idle TLS
    connections mid-poll — re-opening avoids the SSL EOF error."""
    log.info(
        "waiting for %s to hydrate (expecting ≥ %s rows)",
        table, f"{expected_min_rows:,}",
    )
    deadline = time.monotonic() + HYDRATION_POLL_MAX_MIN * 60
    last_rc = -1
    plateau_streak = 0
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
            plateau_streak = 0
        else:
            plateau_streak += 1
        if rc >= expected_min_rows:
            return rc
        # Plateau detection: 5 consecutive identical reads (=2.5 min) and we're
        # past the initial ramp — likely cap-bound on RW Cloud trial tier.
        if plateau_streak >= 5 and rc > 0:
            log.warning(
                "  plateau detected at %s rows (%d consecutive identical polls) — "
                "likely RW capacity cap; proceeding to validation",
                f"{rc:,}", plateau_streak,
            )
            return rc
        time.sleep(HYDRATION_POLL_INTERVAL_S)
    log.warning("hydration deadline reached at %s rows", f"{last_rc:,}")
    return last_rc


def run_validation_gate(conn: psycopg.Connection) -> int:
    """Validation gate per directive §"Verification harness". Tolerant of
    partial hydration (RW Cloud trial cap) — flags row-count floors as
    warnings, not hard failures, when shape constraints are met."""
    failures: list[str] = []
    warnings: list[str] = []
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {LAR_TABLE};")
        lar_rows = int(cur.fetchone()[0])
        cur.execute(
            f"SELECT count(*), count(distinct dataset_year), "
            f"min(dataset_year), max(dataset_year) FROM {LAR_TABLE};"
        )
        _, distinct_years, min_yr, max_yr = cur.fetchone()
        cur.execute(f"SELECT count(*) FROM {MV_CREDIT_SUPPLY};")
        mv_supply_rows = int(cur.fetchone()[0])
        cur.execute(f"SELECT count(*) FROM {MV_LENDER_CONCENTRATION};")
        mv_lender_rows = int(cur.fetchone()[0])

        log.info("=== validation gate ===")
        log.info("  %-40s %s rows", LAR_TABLE + ":", f"{lar_rows:>14,}")
        log.info("  %-40s %s distinct (min=%s max=%s)",
                 "  distinct dataset_year:",
                 distinct_years, min_yr, max_yr)
        log.info("  %-40s %s rows", MV_CREDIT_SUPPLY + ":", f"{mv_supply_rows:>14,}")
        log.info("  %-40s %s rows", MV_LENDER_CONCENTRATION + ":", f"{mv_lender_rows:>14,}")

        # NULL-rate sanity (per directive's §Data Governance: lei strict-typed)
        cur.execute(f"""
            SELECT
              count(*) FILTER (WHERE lei IS NULL OR lei = ''),
              count(*) FILTER (WHERE census_tract IS NULL OR census_tract = ''),
              count(*) FILTER (WHERE action_taken = '1' AND
                              (census_tract IS NULL OR census_tract = ''))
              FROM {LAR_TABLE};
        """)
        lei_null, ct_null, ct_null_originated = cur.fetchone()
        denom = max(lar_rows, 1)
        lei_pct = 100.0 * lei_null / denom
        ct_pct = 100.0 * ct_null / denom
        log.info("  %-40s %s (%.2f%%)", "lei null/empty:",
                 f"{lei_null:>14,}", lei_pct)
        log.info("  %-40s %s (%.2f%%)", "census_tract null/empty (all):",
                 f"{ct_null:>14,}", ct_pct)
        log.info("  %-40s %s",
                 "census_tract null on originated loans:",
                 f"{ct_null_originated:>14,}")

        if lar_rows == 0:
            failures.append(f"{LAR_TABLE} has 0 rows — hydration not started?")

        if lei_pct > 0.5 and lar_rows > 0:
            failures.append(
                f"lei NULL/empty rate {lei_pct:.2f}% > 0.5% — HMDA mandates LEI on "
                "every row; investigate column mapping."
            )

        # Geographic coverage: per directive, >95% coverage of census_tract for
        # originated loans
        cur.execute(f"""
            SELECT count(*) FILTER (WHERE action_taken = '1') AS originated_total,
                   count(*) FILTER (WHERE action_taken = '1' AND
                                    census_tract IS NOT NULL AND census_tract != '')
                       AS originated_with_tract
            FROM {LAR_TABLE};
        """)
        orig_total, orig_with_tract = cur.fetchone()
        if orig_total and orig_total > 0:
            geo_coverage_pct = 100.0 * orig_with_tract / orig_total
            log.info("  %-40s %.2f%% (%s of %s)",
                     "originated loan census_tract coverage:",
                     geo_coverage_pct,
                     f"{orig_with_tract:,}", f"{orig_total:,}")
            if geo_coverage_pct < 95.0:
                failures.append(
                    f"census_tract coverage on originated loans "
                    f"{geo_coverage_pct:.2f}% < 95% — directive sanity floor."
                )

        # MV cardinality sanity
        if mv_supply_rows > 400_000:
            warnings.append(
                f"{MV_CREDIT_SUPPLY} cardinality {mv_supply_rows:,} > 400K — "
                "national tract count is ~74K; expected ≤ ~370K across 5 years."
            )
        if mv_supply_rows == 0 and lar_rows > 0:
            failures.append(
                f"{MV_CREDIT_SUPPLY} has 0 rows but {LAR_TABLE} has {lar_rows:,} — "
                "MV not hydrated or aggregation predicate dropped all rows."
            )

        # Top originators sanity (informational)
        if mv_lender_rows > 0:
            cur.execute(f"""
                SELECT lei, sum(originated_count) AS total_orig
                FROM {MV_LENDER_CONCENTRATION}
                GROUP BY lei
                ORDER BY total_orig DESC NULLS LAST
                LIMIT 5;
            """)
            log.info("  top-5 LEIs by originated count (informational):")
            for r in cur.fetchall():
                log.info("    %s  originated=%s", r[0], f"{r[1]:,}")

        # Hydration completeness (informational, not a failure — RW capacity cap)
        if lar_rows > 0:
            expected_full = 12_000_000 + 14_000_000 + 18_000_000 + 19_000_000 + 11_000_000
            # 2020-2024 LAR row counts per CFPB: roughly 19M, 18M, 14M, 11M, 12M
            # — exact totals depend on which years actually completed ingest.
            log.info(
                "  hydration completeness (heuristic): %s of ~%s rows expected (%.1f%%)",
                f"{lar_rows:,}", f"{expected_full:,}",
                100.0 * lar_rows / expected_full,
            )

    if warnings:
        log.warning("=== %d warning(s) (informational) ===", len(warnings))
        for w in warnings:
            log.warning("  - %s", w)
    if failures:
        log.error("=== VALIDATION FAILED — %d issue(s) ===", len(failures))
        for f in failures:
            log.error("  - %s", f)
        return 1
    log.info("=== VALIDATION PASSED ===")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--validate-only", action="store_true",
                   help="Skip DDL + hydration polling; run validation gate only.")
    p.add_argument("--skip-hydration-wait", action="store_true",
                   help="Apply DDL but don't poll for hydration.")
    p.add_argument("--skip-ddl", action="store_true",
                   help="Skip DDL apply (relations already exist); poll "
                        "hydration + validate.")
    args = p.parse_args()

    # Phase 0c atomic ingest: wrap _main_impl in atomic_ingest_run.
    # Ledger-only mode (RW DDL applier doesn't write to R2 — RW reads from
    # an existing R2 prefix populated by separate ingests). atomic_ingest
    # provides advisory-lock serialization (so two concurrent applier runs
    # against the same RW source serialize), idempotency check (replays
    # with --skip-ddl + same run_id exit 'skipped'), transactional ledger
    # commit, and automatic 'failed' write on uncaught exceptions.
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

    # Generate a stable idempotency_run_id from this invocation's mode flags
    # so successive `--validate-only` runs all share the same key (cheap
    # repeats), but a real `--skip-ddl=False` run gets a fresh key.
    import uuid as _uuid
    args_signature = f"{int(args.validate_only)}|{int(args.skip_hydration_wait)}|{int(args.skip_ddl)}"
    # Date-bucket the key so a re-run on the same day with same args is
    # idempotent, but a next-day refresh is a fresh key.
    from datetime import date as _date
    today = _date.today().isoformat()
    namespace = _uuid.NAMESPACE_OID
    idem_key = str(_uuid.uuid5(namespace, f"hmda_market_map/{today}/{args_signature}"))

    captured_exit: dict[str, int] = {}

    def _finalize() -> int:
        ec = _main_impl(args)
        captured_exit["code"] = ec
        if ec != 0:
            raise RuntimeError(f"_main_impl exited with code {ec}")
        return ec

    try:
        from app.services import atomic_ingest  # type: ignore[import]
        atomic_result = atomic_ingest.atomic_ingest_run(
            source_display_name="hmda_market_map",
            idempotency_run_id=idem_key,
            format="rw_mv",
            finalize_callable=_finalize,
            finalize_kwargs={},
            dest_bucket=None,
            dest_key=None,
            ledger_metadata={
                "writer": "apply_hmda_market_map",
                "args": args_signature,
                "date": today,
            },
            storage_uri="risingwave://prod/public/source_hmda_lar_r2",
        )
    except Exception as exc:
        log.error("atomic_ingest_run failed: %s", exc)
        return captured_exit.get("code", 1) or 1

    if atomic_result["status"] == "skipped":
        log.info("atomic_ingest skipped — already succeeded today with same args (existing_run_id=%s)",
                 atomic_result.get("existing_run_id"))
        return 0

    return captured_exit.get("code", 0)


def _main_impl(args: argparse.Namespace) -> int:
    if args.validate_only:
        with _rw_conn() as conn:
            conn.autocommit = True
            return run_validation_gate(conn)

    if args.skip_ddl:
        log.info("--skip-ddl: skipping DDL apply, polling existing tables.")
        wait_for_hydration(LAR_TABLE, expected_min_rows=10_000_000)
        with _rw_conn() as conn:
            conn.autocommit = True
            return run_validation_gate(conn)

    # 1. Introspect schema (one year — LAR schema stable since 2018)
    lar_schema = introspect_parquet_schema(INTROSPECTION_S3_URI)

    # 2. Generate DDL
    ddl_drop_lender_mv = f"DROP MATERIALIZED VIEW IF EXISTS {MV_LENDER_CONCENTRATION};"
    ddl_drop_supply_mv = f"DROP MATERIALIZED VIEW IF EXISTS {MV_CREDIT_SUPPLY};"
    ddl_drop_table = f"DROP TABLE IF EXISTS {LAR_TABLE} CASCADE;"
    ddl_table = make_create_table_ddl(
        LAR_TABLE, lar_schema, match_pattern=LAR_MATCH_PATTERN,
    )
    ddl_supply_mv = make_credit_supply_mv_ddl(lar_schema)
    ddl_lender_mv = make_lender_concentration_mv_ddl()
    ddl_indexes = make_indexes_ddl()

    # 3. Apply
    with _rw_conn() as conn:
        conn.autocommit = True
        # Drop in reverse dep order: MVs first, then table.
        apply_ddl(conn, ddl_drop_lender_mv, label="drop-lender-mv")
        apply_ddl(conn, ddl_drop_supply_mv, label="drop-supply-mv")
        apply_ddl(conn, ddl_drop_table, label="drop-table")
        apply_ddl(conn, ddl_table, label="create-table")
        apply_ddl(conn, ddl_supply_mv, label="create-supply-mv")
        apply_ddl(conn, ddl_lender_mv, label="create-lender-mv")
        for ix_ddl in ddl_indexes:
            apply_ddl(conn, ix_ddl, label="create-index")
        log.info("DDL applied.")

    if not args.skip_hydration_wait:
        # Min rows = 10M is the predecessor's threshold for 2024 alone. Multi-
        # year theoretical floor is much higher, but plateau detection short-
        # circuits when hydration caps out.
        wait_for_hydration(LAR_TABLE, expected_min_rows=10_000_000)

    with _rw_conn() as conn:
        conn.autocommit = True
        return run_validation_gate(conn)


if __name__ == "__main__":
    sys.exit(main())

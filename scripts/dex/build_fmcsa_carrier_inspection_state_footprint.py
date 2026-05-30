#!/usr/bin/env python3
"""DuckDB-on-R2 derived-Parquet builder: FMCSA carrier inspection state
footprint — per-(dot_number, report_state) inspection rollup.

Reads `fmcsa-derived/vehicle_inspection_essentials/snapshot=*/data.parquet`
(8.2M inspection rows produced by build_fmcsa_vehicle_inspection_essentials.py)
and aggregates to per-(dot_number, report_state) grain. Powers the demand-side
intro-call "where do they run?" lane-color signal — for any carrier, a
queryable per-state inspection footprint (count + first/last date + recent-365d
count + violations + OOS sums).

Output: s3://dex-raw-landing-zone/fmcsa-derived/carrier_inspection_state_footprint/
        snapshot=<YYYY-MM-DD>/data.parquet

12 cols: dot_number, report_state, inspection_count, first_inspection_date,
last_inspection_date, inspection_count_last_365d, viol_total_sum, oos_total_sum,
driver_viol_sum, vehicle_viol_sum, hazmat_viol_sum, snapshot_date.

Architecture (Pattern B per ~/Desktop/hq/inventory/DATA-FACTORY-ARCHITECTURE-PATTERNS.md):
  1. DuckDB-on-R2 reads vehicle_inspection_essentials Parquet directly.
  2. Single SQL aggregation pass: GROUP BY (dot_number, report_state) with
     count + min/max(insp_date) + recency count + violation sums.
  3. Conservation HARD gate: SUM(inspection_count) over output must equal
     non-null-key input row count (catches double-counting + row loss).
  4. Write Parquet via DuckDB COPY → upload to R2.
  5. Ledger row in ops.fmcsa_derived_carrier_inspection_state_footprint_r2_ingest_runs.

Per directive ~/Desktop/hq/directives/2026-05-10-fmcsa-carrier-inspection-state-footprint.md.
Predecessor: build_fmcsa_carrier_officers_normalized.py (PR #314) — same
DuckDB-on-R2 + boto3 upload + ledger architecture, simpler aggregation
shape (pure SQL, no Python normalization layer).

Lessons applied:
  L0 worktree path discipline / L1 Doppler shell expansion / L2 all-VARCHAR
  carry-through with seven typed BIGINT count/sum cols matching DDL /
  L9 read upstream Parquet, output via DuckDB COPY / L29 no producer-side
  date casting (insp_date stays VARCHAR YYYYMMDD) / L37 MV is pure
  pass-through (no self-referential aggregating subquery) / L42 plain
  .parquet extension, no Content-Encoding / L45 snapshot-stamped output
  keys.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with "psycopg[binary]" --with boto3 \\
    python apps/data-engine-x/scripts/build_fmcsa_carrier_inspection_state_footprint.py \\
      --dry-run

  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with "psycopg[binary]" --with boto3 \\
    python apps/data-engine-x/scripts/build_fmcsa_carrier_inspection_state_footprint.py \\
      --apply

  # Idempotent re-run: if target snapshot key already exists, skip rebuild.
  # NOTE per L45: same-snapshot re-runs against an existing key DO NOT
  # propagate to the downstream RW source (S3_V2 connector marks the key
  # as already-consumed). To force a re-build that propagates, pass
  # --snapshot <next-day>.
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with "psycopg[binary]" --with boto3 \\
    python apps/data-engine-x/scripts/build_fmcsa_carrier_inspection_state_footprint.py \\
      --apply --skip-if-unchanged
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("build_fmcsa_carrier_inspection_state_footprint")

R2_BUCKET = "dex-raw-landing-zone"
INPUT_PREFIX = "fmcsa-derived/vehicle_inspection_essentials"
OUTPUT_PREFIX = "fmcsa-derived/carrier_inspection_state_footprint"
AUDIT_TABLE = "ops.fmcsa_derived_carrier_inspection_state_footprint_r2_ingest_runs"


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


def _detect_latest_snapshot(s3, bucket: str, prefix: str) -> str:
    """List under prefix; return lexicographically-greatest snapshot=YYYY-MM-DD."""
    paginator = s3.get_paginator("list_objects_v2")
    snapshots: set[str] = set()
    for page in paginator.paginate(Bucket=bucket, Prefix=f"{prefix}/snapshot="):
        for obj in page.get("Contents", []):
            for part in obj["Key"].split("/"):
                if part.startswith("snapshot=") and len(part) == len("snapshot=YYYY-MM-DD"):
                    snapshots.add(part.split("=", 1)[1])
    if not snapshots:
        raise SystemExit(
            f"FAIL: no snapshot=YYYY-MM-DD under r2://{bucket}/{prefix}/"
        )
    return max(snapshots)


def _audit_open(snapshot: str, input_url: str, output_url: str) -> str:
    import psycopg

    with psycopg.connect(os.environ["DEX_DB_URL_DIRECT"], autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {AUDIT_TABLE}
                  (snapshot_date, upstream_input_prefix, output_r2_prefix,
                   status, triggered_by)
                VALUES (%s, %s, %s, 'running',
                        'manual:build_fmcsa_carrier_inspection_state_footprint')
                RETURNING run_id;
                """,
                (snapshot, input_url, output_url),
            )
            return str(cur.fetchone()[0])


def _audit_close(
    run_id: str,
    *,
    status: str,
    input_inspection_row_count: int | None = None,
    output_footprint_row_count: int | None = None,
    distinct_dot_numbers_count: int | None = None,
    distinct_report_state_count: int | None = None,
    dropped_null_key_row_count: int | None = None,
    conservation_sum_check_passed: bool | None = None,
    duration_s: float | None = None,
    error: str | None = None,
    upstream_keys: list[str] | None = None,
    upstream_total_bytes: int | None = None,
    output_total_bytes: int | None = None,
    notes: dict | None = None,
) -> None:
    import psycopg

    with psycopg.connect(os.environ["DEX_DB_URL_DIRECT"], autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {AUDIT_TABLE}
                   SET status = %s,
                       input_inspection_row_count = %s,
                       output_footprint_row_count = %s,
                       distinct_dot_numbers_count = %s,
                       distinct_report_state_count = %s,
                       dropped_null_key_row_count = %s,
                       conservation_sum_check_passed = %s,
                       duration_seconds = %s,
                       error_message = %s,
                       upstream_object_keys = %s,
                       upstream_total_bytes = %s,
                       output_object_count = 1,
                       output_total_bytes = %s,
                       notes = %s,
                       ended_at = now()
                 WHERE run_id = %s;
                """,
                (
                    status,
                    input_inspection_row_count,
                    output_footprint_row_count,
                    distinct_dot_numbers_count,
                    distinct_report_state_count,
                    dropped_null_key_row_count,
                    conservation_sum_check_passed,
                    duration_s,
                    error,
                    upstream_keys,
                    upstream_total_bytes,
                    output_total_bytes,
                    json.dumps(notes) if notes else None,
                    run_id,
                ),
            )


def _audit_no_change(snapshot: str, input_url: str, output_url: str) -> None:
    import psycopg

    with psycopg.connect(os.environ["DEX_DB_URL_DIRECT"], autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {AUDIT_TABLE}
                  (snapshot_date, upstream_input_prefix, output_r2_prefix,
                   status, triggered_by, ended_at)
                VALUES (%s, %s, %s, 'no_change',
                        'manual:build_fmcsa_carrier_inspection_state_footprint',
                        now());
                """,
                (snapshot, input_url, output_url),
            )


def _build_footprint(con, *, input_url: str, snapshot: str) -> dict:
    """Materialize the per-(dot, state) rollup as a TEMP TABLE and return
    metrics + a HARD-fail conservation check result."""
    logger.info("reading upstream inspections …")
    con.execute(
        f"""
        CREATE TEMP TABLE inspections AS
        SELECT * FROM read_parquet('{input_url}')
        """
    )
    total_input = con.execute("SELECT count(*) FROM inspections").fetchone()[0]
    logger.info(f"  inspections rows: {total_input:,}")

    # Count rows that will drop due to null/empty join keys before aggregation.
    dropped = con.execute(
        """
        SELECT count(*) FROM inspections
         WHERE dot_number IS NULL OR TRIM(dot_number) = ''
            OR report_state IS NULL OR TRIM(report_state) = ''
        """
    ).fetchone()[0]
    non_null_keys = total_input - dropped
    logger.info(
        f"  rows with both keys non-null/non-empty: {non_null_keys:,} "
        f"(dropped: {dropped:,})"
    )

    logger.info("aggregating per (dot_number, report_state) …")
    # L29: insp_date stays VARCHAR YYYYMMDD in input AND output. The recency
    # filter casts at aggregation time only.
    con.execute(
        f"""
        CREATE TEMP TABLE footprint AS
        SELECT
          dot_number,
          report_state,
          count(*)::BIGINT AS inspection_count,
          min(insp_date) AS first_inspection_date,
          max(insp_date) AS last_inspection_date,
          count(*) FILTER (
            WHERE strptime(insp_date, '%Y%m%d') >=
                  strptime('{snapshot}', '%Y-%m-%d') - INTERVAL 365 DAY
          )::BIGINT AS inspection_count_last_365d,
          COALESCE(SUM(TRY_CAST(viol_total          AS BIGINT)), 0)::BIGINT AS viol_total_sum,
          COALESCE(SUM(TRY_CAST(oos_total           AS BIGINT)), 0)::BIGINT AS oos_total_sum,
          COALESCE(SUM(TRY_CAST(driver_viol_total   AS BIGINT)), 0)::BIGINT AS driver_viol_sum,
          COALESCE(SUM(TRY_CAST(vehicle_viol_total  AS BIGINT)), 0)::BIGINT AS vehicle_viol_sum,
          COALESCE(SUM(TRY_CAST(hazmat_viol_total   AS BIGINT)), 0)::BIGINT AS hazmat_viol_sum
        FROM inspections
        WHERE dot_number IS NOT NULL
          AND TRIM(dot_number) <> ''
          AND report_state IS NOT NULL
          AND TRIM(report_state) <> ''
        GROUP BY dot_number, report_state
        """
    )
    out_count = con.execute("SELECT count(*) FROM footprint").fetchone()[0]
    logger.info(f"  footprint rows: {out_count:,}")

    distinct_dots = con.execute(
        "SELECT count(DISTINCT dot_number) FROM footprint"
    ).fetchone()[0]
    distinct_states = con.execute(
        "SELECT count(DISTINCT report_state) FROM footprint"
    ).fetchone()[0]
    logger.info(
        f"  distinct dot_numbers: {distinct_dots:,}   "
        f"distinct report_states: {distinct_states}"
    )

    # HARD-FAIL conservation gate: SUM(inspection_count) must equal the
    # non-null-key input row count exactly. Catches double-counting + row
    # loss in the GROUP BY.
    conserved_sum = con.execute(
        "SELECT SUM(inspection_count) FROM footprint"
    ).fetchone()[0]
    conservation_passed = (conserved_sum == non_null_keys)
    logger.info(
        f"  conservation: sum(inspection_count)={conserved_sum:,} "
        f"non_null_keys={non_null_keys:,} "
        f"passed={conservation_passed}"
    )

    # Sanity diagnostics for ledger notes.
    top10_rows = con.execute(
        """
        SELECT report_state, count(DISTINCT dot_number) AS distinct_carriers
          FROM footprint
         GROUP BY report_state
         ORDER BY distinct_carriers DESC
         LIMIT 10
        """
    ).fetchall()
    top10 = [(state, int(n)) for state, n in top10_rows]
    logger.info("  top-10 report_state by distinct dot_number:")
    for state, n in top10:
        logger.info(f"    {state}  {n:,}")

    return {
        "input_inspection_row_count": int(total_input),
        "output_footprint_row_count": int(out_count),
        "distinct_dot_numbers_count": int(distinct_dots),
        "distinct_report_state_count": int(distinct_states),
        "dropped_null_key_row_count": int(dropped),
        "conservation_sum_check_passed": conservation_passed,
        "conserved_sum": int(conserved_sum),
        "non_null_key_input_row_count": int(non_null_keys),
        "top_10_states_by_distinct_carriers": top10,
    }


def _write_output_parquet(con, snapshot: str) -> str:
    """Write footprint TEMP TABLE to R2 via DuckDB COPY. All carry-through cols
    stay VARCHAR; aggregation count/sum cols are BIGINT typed exceptions per
    the L2 footprint table schema declaration."""
    output_key = f"{OUTPUT_PREFIX}/snapshot={snapshot}/data.parquet"
    output_url = f"r2://{R2_BUCKET}/{output_key}"
    logger.info(f"writing footprint → {output_url}")
    con.execute(
        f"""
        COPY (
          SELECT
            dot_number::VARCHAR              AS dot_number,
            report_state::VARCHAR            AS report_state,
            inspection_count,
            first_inspection_date::VARCHAR   AS first_inspection_date,
            last_inspection_date::VARCHAR    AS last_inspection_date,
            inspection_count_last_365d,
            viol_total_sum,
            oos_total_sum,
            driver_viol_sum,
            vehicle_viol_sum,
            hazmat_viol_sum,
            '{snapshot}'::VARCHAR            AS snapshot_date
            FROM footprint
        )
        TO '{output_url}'
        (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)
        """
    )
    return output_key


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="build + write Parquet to R2 + record ledger row")
    parser.add_argument("--dry-run", action="store_true",
                        help="row counts + conservation gate only, no R2 / Postgres writes")
    parser.add_argument("--snapshot", default=None,
                        help="YYYY-MM-DD snapshot label (default: latest "
                             "fmcsa-derived/vehicle_inspection_essentials snapshot)")
    parser.add_argument("--skip-if-unchanged", action="store_true",
                        help="if a derived Parquet already exists at the target "
                             "snapshot key, skip rebuild (logs no_change ledger row)")
    parser.add_argument("--workdir", default="/tmp/fmcsa_carrier_inspection_state_footprint",
                        help="local working dir (unused — DuckDB COPY direct to R2)")
    args = parser.parse_args()

    if not args.apply and not args.dry_run:
        parser.error("must pass --apply or --dry-run")
    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are mutually exclusive")

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
                "DEX_DB_URL_DIRECT"):
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")

    t0 = time.time()

    import boto3

    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )

    snapshot = args.snapshot or _detect_latest_snapshot(s3, R2_BUCKET, INPUT_PREFIX)
    logger.info(f"snapshot: {snapshot}")

    input_key = f"{INPUT_PREFIX}/snapshot={snapshot}/data.parquet"
    input_url = f"r2://{R2_BUCKET}/{input_key}"
    output_key = f"{OUTPUT_PREFIX}/snapshot={snapshot}/data.parquet"
    output_url = f"r2://{R2_BUCKET}/{output_key}"
    logger.info(f"input:  {input_url}")
    logger.info(f"output: {output_url}")

    try:
        head = s3.head_object(Bucket=R2_BUCKET, Key=input_key)
        upstream_total_bytes = head["ContentLength"]
        upstream_keys = [input_key]
    except Exception as exc:
        raise SystemExit(f"FAIL: input Parquet missing — {exc}")
    logger.info(f"upstream object size: {upstream_total_bytes/1e6:.1f} MB")

    # Idempotency check: uses r2_object_is_landed so a 0-byte object is treated
    # as nonexistent and does NOT trigger the skip (poison-file defense-in-depth).
    if args.skip_if_unchanged:
        from scripts._lib.r2_keys import r2_object_is_landed

        if r2_object_is_landed(s3, bucket=R2_BUCKET, key=output_key):
            logger.info(f"output key already present at {output_url}; skip-if-unchanged")
            if args.apply:
                _audit_no_change(snapshot, input_url, output_url)
            return 0

    con = _connect_duckdb_to_r2()

    if args.dry_run:
        try:
            metrics = _build_footprint(con, input_url=input_url, snapshot=snapshot)
        except Exception:
            logger.exception("dry-run build failed")
            raise
        logger.info("─" * 60)
        logger.info("DRY RUN summary:")
        logger.info(f"  input_inspection_row_count:     {metrics['input_inspection_row_count']:,}")
        logger.info(f"  output_footprint_row_count:     {metrics['output_footprint_row_count']:,}")
        logger.info(f"  distinct_dot_numbers_count:     {metrics['distinct_dot_numbers_count']:,}")
        logger.info(f"  distinct_report_state_count:    {metrics['distinct_report_state_count']}")
        logger.info(f"  dropped_null_key_row_count:     {metrics['dropped_null_key_row_count']:,}")
        logger.info(f"  conservation_sum_check_passed:  {metrics['conservation_sum_check_passed']}")
        logger.info(f"  duration: {time.time()-t0:.1f}s")
        if not metrics["conservation_sum_check_passed"]:
            logger.error(
                "FAIL gate: conservation sum check failed "
                f"(sum={metrics['conserved_sum']:,} "
                f"non_null_keys={metrics['non_null_key_input_row_count']:,})"
            )
            return 1
        logger.info("DRY RUN — no R2 / Postgres writes.")
        return 0

    run_id = _audit_open(snapshot, input_url, output_url)
    logger.info(f"audit run_id: {run_id}")

    try:
        metrics = _build_footprint(con, input_url=input_url, snapshot=snapshot)

        # HARD-FAIL conservation gate before writing.
        if not metrics["conservation_sum_check_passed"]:
            err = (
                f"conservation sum check failed: "
                f"sum(inspection_count)={metrics['conserved_sum']:,} "
                f"non_null_keys={metrics['non_null_key_input_row_count']:,}"
            )
            _audit_close(
                run_id,
                status="failed",
                input_inspection_row_count=metrics["input_inspection_row_count"],
                output_footprint_row_count=metrics["output_footprint_row_count"],
                distinct_dot_numbers_count=metrics["distinct_dot_numbers_count"],
                distinct_report_state_count=metrics["distinct_report_state_count"],
                dropped_null_key_row_count=metrics["dropped_null_key_row_count"],
                conservation_sum_check_passed=False,
                duration_s=time.time() - t0,
                error=err,
                upstream_keys=upstream_keys,
                upstream_total_bytes=upstream_total_bytes,
            )
            logger.error(f"HARD FAIL: {err}")
            return 1

        _ = _write_output_parquet(con, snapshot)

        # Verify R2 readability post-upload — row count + conservation.
        verify_count = con.execute(
            f"SELECT count(*) FROM read_parquet('{output_url}')"
        ).fetchone()[0]
        if verify_count != metrics["output_footprint_row_count"]:
            err = (
                f"post-upload row count mismatch: wrote "
                f"{metrics['output_footprint_row_count']:,}, "
                f"R2 readback {verify_count:,}"
            )
            _audit_close(
                run_id, status="failed", duration_s=time.time() - t0,
                error=err, upstream_keys=upstream_keys,
                upstream_total_bytes=upstream_total_bytes,
            )
            logger.error(f"HARD FAIL: {err}")
            return 1

        verify_sum = con.execute(
            f"SELECT SUM(inspection_count) FROM read_parquet('{output_url}')"
        ).fetchone()[0]
        if verify_sum != metrics["non_null_key_input_row_count"]:
            err = (
                f"post-upload conservation check failed: "
                f"sum={verify_sum:,} expected={metrics['non_null_key_input_row_count']:,}"
            )
            _audit_close(
                run_id, status="failed", duration_s=time.time() - t0,
                error=err, upstream_keys=upstream_keys,
                upstream_total_bytes=upstream_total_bytes,
            )
            logger.error(f"HARD FAIL: {err}")
            return 1

        head = s3.head_object(Bucket=R2_BUCKET, Key=output_key)
        output_total_bytes = head["ContentLength"]

        duration = time.time() - t0
        notes = {
            "conserved_sum": metrics["conserved_sum"],
            "non_null_key_input_row_count": metrics["non_null_key_input_row_count"],
            "top_10_states_by_distinct_carriers": metrics["top_10_states_by_distinct_carriers"],
        }
        _audit_close(
            run_id,
            status="completed",
            input_inspection_row_count=metrics["input_inspection_row_count"],
            output_footprint_row_count=metrics["output_footprint_row_count"],
            distinct_dot_numbers_count=metrics["distinct_dot_numbers_count"],
            distinct_report_state_count=metrics["distinct_report_state_count"],
            dropped_null_key_row_count=metrics["dropped_null_key_row_count"],
            conservation_sum_check_passed=True,
            duration_s=duration,
            upstream_keys=upstream_keys,
            upstream_total_bytes=upstream_total_bytes,
            output_total_bytes=output_total_bytes,
            notes=notes,
        )
        logger.info("─" * 60)
        logger.info(f"OK — run_id={run_id}  duration={duration:.1f}s")
        logger.info(f"     output: {output_url}  ({output_total_bytes/1e6:.1f} MB)")
        logger.info(
            f"     rows={metrics['output_footprint_row_count']:,} "
            f"distinct_dots={metrics['distinct_dot_numbers_count']:,} "
            f"states={metrics['distinct_report_state_count']}"
        )
        return 0

    except Exception as exc:
        logger.exception("build failed")
        try:
            _audit_close(
                run_id, status="failed", duration_s=time.time() - t0,
                error=str(exc)[:500],
                upstream_keys=upstream_keys,
                upstream_total_bytes=upstream_total_bytes,
            )
        except Exception:
            logger.exception("also failed to mark run as failed")
        raise


if __name__ == "__main__":
    raise SystemExit(main())

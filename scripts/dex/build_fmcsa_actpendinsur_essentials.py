#!/usr/bin/env python3
"""Project FMCSA Active+Pending Insurance Parquet → derived essentials Parquet on R2.

Reads `fmcsa/ActPendInsur - All With History/<snapshot>/*.parquet.zst` via
DuckDB-on-R2, projects to snake_case column names (upstream uses spaces +
ampersands — "USDOT Number", "BI&PD Maximum Limit", etc.), writes plain
`.parquet` (internal column ZSTD only) to:

    s3://dex-raw-landing-zone/fmcsa-derived/actpendinsur_essentials/snapshot=<YYYY-MM-DD>/data.parquet

The "USDOT Number" upstream column is renamed to `dot_number` to match the
canonical join-key name across the FMCSA spine.

Source path note: reads from `<Feed> - All With History/` (the full-state
variant) rather than `<Feed>/` (daily-delta variant, ~5K rows/day) — the
full-state path is the only source with audience-MV-grade row counts
(~470K rows). Daily-delta path declared in `risingwave/source_wiring_sweep_
fmcsa_event_streams.sql` is unreadable by RW (.parquet.zst whole-file ZSTD)
and would only carry one day of activity even if it were readable.

Per-run audit row written to ops.fmcsa_derived_actpendinsur_r2_ingest_runs.

L40 closure for FMCSA ActPendInsur feed (predecessor directive
`2026-05-10-fmcsa-insurance-shopper-audience-mvs.md`).

See ~/Desktop/hq/directives/2026-05-10-fmcsa-insurance-feeds-derived-parquet-unblock.md.

Usage:
    doppler run -p hq-all -c prd -- \\
        uv run --with duckdb --with psycopg[binary] --with boto3 python \\
        apps/data-engine-x/scripts/build_fmcsa_actpendinsur_essentials.py --apply
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("build_fmcsa_actpendinsur_essentials")

R2_BUCKET = "dex-raw-landing-zone"
INPUT_PREFIX = "fmcsa/ActPendInsur - All With History"
OUTPUT_PREFIX = "fmcsa-derived/actpendinsur_essentials"
AUDIT_TABLE = "ops.fmcsa_derived_actpendinsur_r2_ingest_runs"

# (upstream_quoted_name, output_snake_case_name).
COL_PROJECTIONS = [
    ('"USDOT Number"',                 "dot_number"),
    ('"Docket Number"',                "docket_number"),
    ('"Form Code"',                    "form_code"),
    ('"Insurance Type Description"',   "insurance_type_description"),
    ('"Insurance Company Name"',       "insurance_company_name"),
    ('"Policy Number"',                "policy_number"),
    ('"Posted Date"',                  "posted_date"),
    ('"BI&PD Underlying Limit"',       "bipd_underlying_limit"),
    ('"BI&PD Maximum Limit"',          "bipd_maximum_limit"),
    ('"Effective Date"',               "effective_date"),
    ('"Cancel Effective Date"',        "cancel_effective_date"),
]


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


def _detect_latest_snapshot(con) -> str:
    rows = con.execute(
        f"SELECT file FROM glob('r2://{R2_BUCKET}/{INPUT_PREFIX}/**/*.parquet*')"
    ).fetchall()
    if not rows:
        raise SystemExit(f"FAIL: no Parquet under r2://{R2_BUCKET}/{INPUT_PREFIX}/")
    snapshots: set[str] = set()
    for (path,) in rows:
        for p in path.split("/"):
            if len(p) == 10 and p[4] == "-" and p[7] == "-":
                snapshots.add(p)
    if not snapshots:
        raise SystemExit("FAIL: no YYYY-MM-DD snapshot directory found")
    return max(snapshots)


def _build_select_sql(input_glob: str) -> str:
    quoted = ", ".join(f"{src}::VARCHAR AS {dst}" for src, dst in COL_PROJECTIONS)
    return f"SELECT {quoted} FROM read_parquet('{input_glob}')"


def _audit_open(snapshot: str, input_glob: str, output_url: str) -> str:
    import psycopg

    with psycopg.connect(os.environ["DEX_DB_URL_DIRECT"], autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {AUDIT_TABLE}
                  (snapshot_date, upstream_input_prefix, output_r2_prefix,
                   status, triggered_by)
                VALUES (%s, %s, %s, 'running', 'manual:build_fmcsa_actpendinsur_essentials')
                RETURNING run_id;
                """,
                (snapshot, input_glob, output_url),
            )
            return str(cur.fetchone()[0])


def _audit_close(
    run_id: str,
    *,
    status: str,
    row_count: int | None = None,
    duration_s: float | None = None,
    error: str | None = None,
    upstream_keys: list[str] | None = None,
    upstream_total_bytes: int | None = None,
    output_total_bytes: int | None = None,
) -> None:
    import psycopg

    with psycopg.connect(os.environ["DEX_DB_URL_DIRECT"], autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {AUDIT_TABLE}
                   SET status = %s,
                       parquet_row_count = %s,
                       duration_seconds = %s,
                       error_message = %s,
                       upstream_object_keys = %s,
                       upstream_total_bytes = %s,
                       output_object_count = 1,
                       output_total_bytes = %s,
                       ended_at = now()
                 WHERE run_id = %s;
                """,
                (
                    status, row_count, duration_s, error,
                    upstream_keys, upstream_total_bytes, output_total_bytes,
                    run_id,
                ),
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--snapshot", default=None)
    args = parser.parse_args()
    if not args.apply and not args.dry_run:
        parser.error("must pass --apply or --dry-run")

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "DEX_DB_URL_DIRECT"):
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")

    t0 = time.time()
    con = _connect_duckdb_to_r2()
    snapshot = args.snapshot or _detect_latest_snapshot(con)
    logger.info(f"snapshot: {snapshot}")

    input_glob = f"r2://{R2_BUCKET}/{INPUT_PREFIX}/{snapshot}/*.parquet*"
    output_key = f"{OUTPUT_PREFIX}/snapshot={snapshot}/data.parquet"
    output_url = f"r2://{R2_BUCKET}/{output_key}"

    import boto3

    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )
    upstream_resp = s3.list_objects_v2(
        Bucket=R2_BUCKET, Prefix=f"{INPUT_PREFIX}/{snapshot}/"
    )
    upstream_items = upstream_resp.get("Contents", [])
    upstream_keys = [it["Key"] for it in upstream_items]
    upstream_total_bytes = sum(it["Size"] for it in upstream_items)
    logger.info(
        f"upstream: {len(upstream_keys)} objects, {upstream_total_bytes:,} bytes total"
    )

    select_sql = _build_select_sql(input_glob)
    cnt = con.execute(f"SELECT count(*) FROM ({select_sql})").fetchone()[0]
    logger.info(f"projected row count: {cnt:,}")

    if args.dry_run:
        logger.info(f"DRY RUN — no writes. duration={time.time()-t0:.1f}s")
        return 0

    run_id = _audit_open(snapshot, input_glob, output_url)
    try:
        copy_sql = f"""
            COPY ({select_sql})
            TO '{output_url}'
            (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)
        """
        con.execute(copy_sql)

        head = s3.head_object(Bucket=R2_BUCKET, Key=output_key)
        output_total_bytes = head["ContentLength"]

        duration = time.time() - t0
        _audit_close(
            run_id,
            status="completed",
            row_count=cnt,
            duration_s=duration,
            upstream_keys=upstream_keys,
            upstream_total_bytes=upstream_total_bytes,
            output_total_bytes=output_total_bytes,
        )
        logger.info(f"OK — wrote {cnt:,} rows to {output_url}")
        logger.info(f"duration={duration:.1f}s")
        return 0
    except Exception as exc:
        _audit_close(run_id, status="failed", error=str(exc)[:500])
        raise


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""SEC EDGAR Form N-PORT (DERA bulk) → R2 Parquet ingest.

For each (year, quarter) in the configured span:
  1. HEAD the DERA quarterly ZIP (Last-Modified is the freshness signal).
  2. Compare to the most-recent ``ops.sec_edgar_form_n_port_r2_ingest_runs.source_last_modified``
     for that (year, quarter); if --skip-if-unchanged and matches → write
     ``no_change`` rows for raw_zip + every prior-known table, skip download.
  3. Download the ZIP into ``--workdir`` (default /tmp/sec_edgar_form_n_port_workdir).
  4. Upload the raw ZIP to s3://dex-raw-landing-zone/sec-edgar/form-n-port/raw/{Y}q{N}_nport.zip
     (one ledger row, table_name='raw_zip').
  5. Unzip in-place; **discover-by-namelist** — for every TSV in the ZIP:
       - DuckDB COPY (SELECT *  + provenance cols FROM read_csv(tsv, all_varchar=TRUE))
         TO 'parquet' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)
       - Upload Parquet to s3://dex-raw-landing-zone/sec-edgar/form-n-port/year={Y}/quarter=Q{N}/{table}/data.parquet
       - Validate row-count parity (TSV → Parquet)
       - Record the run row.
  6. Cleanup workdir (in finally).

Key differences vs. Form 4 ingest:
  - No pinned schema lib — table set is discovered per-quarter from the ZIP
    namelist. Drift vs. prior quarter is logged in the discovery row's notes.
  - R2 partition convention is `quarter=Q{N}` (Q-prefix) per directive.
  - Uppercase TSV filenames map to lowercase R2 dir names
    (FUND_REPORTED_HOLDING.tsv → fund_reported_holding/).

Idempotency: per (year, quarter) HEAD-Last-Modified vs prior raw_zip ledger row.
Audit: 1 discovery + 1 raw_zip + ~29-30 table rows per (year, quarter).

Usage:
  doppler run --project hq-all --config prd --command \\
    'uv run python3 scripts/run_sec_edgar_form_n_port_r2_ingest.py --year 2024 --quarter 1'
  doppler run --project hq-all --config prd --command \\
    'uv run python3 scripts/run_sec_edgar_form_n_port_r2_ingest.py --years 2019-2026'
  doppler run --project hq-all --config prd --command \\
    'uv run python3 scripts/run_sec_edgar_form_n_port_r2_ingest.py --year 2019 --quarter 4 --max-rows 1000'
  doppler run --project hq-all --config prd --command \\
    'uv run python3 scripts/run_sec_edgar_form_n_port_r2_ingest.py --years 2019-2026 --skip-if-unchanged'
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import boto3
import duckdb
import httpx
import psycopg
from psycopg.types.json import Jsonb


# ------------------------------------------------------------------ #
# Constants
# ------------------------------------------------------------------ #

R2_BUCKET_DEFAULT = "dex-raw-landing-zone"
R2_PREFIX_DEFAULT = "sec-edgar/form-n-port"
PROVIDER = "sec_edgar_form_n_port_dera"

ZIP_URL_TEMPLATE = (
    "https://www.sec.gov/files/dera/data/form-n-port-data-sets/"
    "{year}q{q}_nport.zip"
)

USER_AGENT = (
    "data-engine-x/sec-edgar-form-n-port-ingest "
    "tools@substrate.build "
    "(operational research)"
)

# N-PORT first published: 2019-Q4. Earlier quarters do not exist.
EARLIEST_YEAR = 2019
LATEST_YEAR_DEFAULT = datetime.now(timezone.utc).year + 1


# ------------------------------------------------------------------ #
# Logging
# ------------------------------------------------------------------ #

def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("sec-edgar-form-n-port-ingest")


log = _logger()


# ------------------------------------------------------------------ #
# Env helpers
# ------------------------------------------------------------------ #

def _required_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"{name} is not set in the environment.")
    return v


def _r2_client():
    return boto3.client(
        "s3",
        endpoint_url=_required_env("R2_ENDPOINT"),
        aws_access_key_id=_required_env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=_required_env("R2_SECRET_ACCESS_KEY"),
        region_name="auto",
    )


def _database_url() -> str | None:
    return os.environ.get("DEX_DB_URL_POOLED") or os.environ.get("DEX_DB_URL_DIRECT")


# ------------------------------------------------------------------ #
# HTTP helpers
# ------------------------------------------------------------------ #

def _httpx_client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"},
        timeout=httpx.Timeout(connect=20.0, read=900.0, write=60.0, pool=20.0),
        follow_redirects=True,
    )


def head_zip(client: httpx.Client, url: str) -> tuple[int, datetime | None, int | None]:
    r = client.head(url)
    lm: datetime | None = None
    cl: int | None = None
    if "Last-Modified" in r.headers:
        try:
            lm = parsedate_to_datetime(r.headers["Last-Modified"])
        except (TypeError, ValueError):
            lm = None
    if "Content-Length" in r.headers:
        try:
            cl = int(r.headers["Content-Length"])
        except ValueError:
            cl = None
    return r.status_code, lm, cl


def download_zip(client: httpx.Client, url: str, dest: Path) -> int:
    last_exc: Exception | None = None
    for attempt in range(1, 5):
        try:
            with client.stream("GET", url) as r:
                r.raise_for_status()
                bytes_written = 0
                with dest.open("wb") as f:
                    for chunk in r.iter_bytes(chunk_size=1 << 20):
                        f.write(chunk)
                        bytes_written += len(chunk)
                return bytes_written
        except (httpx.TimeoutException, httpx.RequestError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            wait = min(2 ** attempt, 30)
            log.warning("download %s failed (%s); retry in %ss", url, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"download {url} exhausted retries: {last_exc}")


# ------------------------------------------------------------------ #
# Naming helpers
# ------------------------------------------------------------------ #

def _logical_name_for(tsv_filename: str) -> str:
    """Map ZIP-namelist TSV filename to lowercase R2 dir name.

    e.g. 'FUND_REPORTED_HOLDING.tsv' -> 'fund_reported_holding'
    """
    base = tsv_filename
    if base.lower().endswith(".tsv"):
        base = base[: -len(".tsv")]
    return base.lower()


def _enumerate_tsv_members(zip_path: Path) -> list[str]:
    """Return TSV member names in stable sort order."""
    with zipfile.ZipFile(zip_path) as zf:
        members = [n for n in zf.namelist() if n.lower().endswith(".tsv")]
    return sorted(members)


# ------------------------------------------------------------------ #
# DuckDB transform
# ------------------------------------------------------------------ #

def _read_tsv_header(path: Path) -> tuple[str, ...]:
    with path.open("r", encoding="utf-8", errors="strict") as f:
        header = f.readline().rstrip("\r\n")
    return tuple(header.split("\t"))


def _count_tsv_rows(path: Path) -> int:
    n = 0
    with path.open("rb") as f:
        for _ in f:
            n += 1
    return n - 1  # subtract header


def transform_tsv_to_parquet(
    con: duckdb.DuckDBPyConnection,
    *,
    tsv_path: Path,
    parquet_path: Path,
    year: int,
    quarter: int,
    source_url: str,
    source_last_modified: datetime | None,
    max_rows: int | None,
) -> tuple[int, tuple[str, ...]]:
    """Run DuckDB COPY on a single TSV. Returns (parquet_row_count, observed_columns)."""
    observed_columns = _read_tsv_header(tsv_path)
    proj = ", ".join(f'"{c}"' for c in observed_columns)

    src_obs_iso = (
        source_last_modified.astimezone(timezone.utc).isoformat()
        if source_last_modified else ""
    )

    provenance = (
        f", {year}::SMALLINT AS dataset_year"
        f", {quarter}::SMALLINT AS dataset_quarter"
        f", '{PROVIDER}' AS source_provider"
        f", '{source_url}' AS source_download_url"
        f", '{src_obs_iso}' AS source_observed_at"
    )

    limit_clause = f"LIMIT {int(max_rows)}" if max_rows is not None else ""

    sql = f"""
    COPY (
      SELECT {proj}{provenance}
      FROM read_csv(
        '{tsv_path}',
        delim='\\t', header=TRUE, all_varchar=TRUE,
        quote='', escape='', strict_mode=FALSE
      )
      {limit_clause}
    ) TO '{parquet_path}'
    (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000);
    """
    con.execute(sql)

    parquet_row_count = con.execute(
        f"SELECT count(*) FROM read_parquet('{parquet_path}')"
    ).fetchone()[0]
    return int(parquet_row_count), observed_columns


# ------------------------------------------------------------------ #
# R2 upload
# ------------------------------------------------------------------ #

def upload_file_to_r2(
    s3, *, bucket: str, key: str, local_path: Path,
    content_type: str = "application/x-parquet",
) -> int:
    """Upload local file to R2; return uploaded bytes.

    Per L42: never set ContentEncoding=zstd — Parquet's column-chunk
    compression is internal; R2 must serve as opaque bytes.
    """
    s3.upload_file(
        str(local_path), bucket, key,
        ExtraArgs={"ContentType": content_type},
    )
    return local_path.stat().st_size


# ------------------------------------------------------------------ #
# Audit-ledger helpers
# ------------------------------------------------------------------ #

def _connect(db_url: str) -> psycopg.Connection:
    return psycopg.connect(db_url)


def fetch_prior_last_modified(
    db_url: str, *, year: int, quarter: int,
) -> datetime | None:
    sql = """
    SELECT source_last_modified
      FROM ops.sec_edgar_form_n_port_r2_ingest_runs
     WHERE dataset_year = %s
       AND dataset_quarter = %s
       AND table_name = 'raw_zip'
       AND status = 'completed'
     ORDER BY started_at DESC
     LIMIT 1;
    """
    with _connect(db_url) as conn, conn.cursor() as cur:
        cur.execute(sql, (year, quarter))
        row = cur.fetchone()
    return row[0] if row else None


def fetch_prior_table_names(
    db_url: str, *, year: int, quarter: int,
) -> list[str]:
    """Tables ingested in the most-recent completed run for a (year, quarter).

    Used to write ``no_change`` rows for the same set on a skip-if-unchanged
    refresh.
    """
    sql = """
    SELECT DISTINCT table_name
      FROM ops.sec_edgar_form_n_port_r2_ingest_runs
     WHERE dataset_year = %s
       AND dataset_quarter = %s
       AND status = 'completed'
       AND table_name NOT IN ('discovery', 'raw_zip');
    """
    with _connect(db_url) as conn, conn.cursor() as cur:
        cur.execute(sql, (year, quarter))
        return [r[0] for r in cur.fetchall()]


def fetch_prior_quarter_tables(
    db_url: str, *, year: int, quarter: int,
) -> set[str]:
    """The table set ingested in the most-recent prior quarter (any year).

    Used to detect schema drift across quarters.
    """
    sql = """
    SELECT DISTINCT table_name
      FROM ops.sec_edgar_form_n_port_r2_ingest_runs
     WHERE (dataset_year, dataset_quarter) < (%s, %s)
       AND status = 'completed'
       AND table_name NOT IN ('discovery', 'raw_zip')
       AND (dataset_year, dataset_quarter) = (
         SELECT dataset_year, dataset_quarter
           FROM ops.sec_edgar_form_n_port_r2_ingest_runs
          WHERE (dataset_year, dataset_quarter) < (%s, %s)
            AND status = 'completed'
            AND table_name NOT IN ('discovery', 'raw_zip')
          ORDER BY dataset_year DESC, dataset_quarter DESC
          LIMIT 1
       );
    """
    with _connect(db_url) as conn, conn.cursor() as cur:
        cur.execute(sql, (year, quarter, year, quarter))
        return {r[0] for r in cur.fetchall()}


def insert_run_row(
    conn: psycopg.Connection, *,
    year: int, quarter: int, table_name: str,
    source_url: str | None = None,
    source_last_modified: datetime | None = None,
    prior_source_last_modified: datetime | None = None,
) -> str:
    sql = """
    INSERT INTO ops.sec_edgar_form_n_port_r2_ingest_runs
      (dataset_year, dataset_quarter, table_name, status,
       source_url, source_last_modified, prior_source_last_modified)
    VALUES (%s, %s, %s, 'running', %s, %s, %s)
    RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            year, quarter, table_name,
            source_url, source_last_modified, prior_source_last_modified,
        ))
        row_id = cur.fetchone()[0]
    conn.commit()
    return str(row_id)


def finalize_run_row(
    conn: psycopg.Connection, run_id: str, *,
    status: str,
    started_at: float,
    zip_bytes_downloaded: int | None = None,
    tsv_bytes_uncompressed: int | None = None,
    tsv_row_count: int | None = None,
    parquet_row_count: int | None = None,
    parquet_bytes_written: int | None = None,
    parquet_column_count: int | None = None,
    r2_bucket: str | None = None,
    r2_prefix: str | None = None,
    r2_object_key: str | None = None,
    r2_total_bytes: int | None = None,
    error_message: str | None = None,
    notes: dict[str, Any] | None = None,
) -> None:
    duration = round(time.monotonic() - started_at, 3)
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE ops.sec_edgar_form_n_port_r2_ingest_runs
               SET status = %s,
                   zip_bytes_downloaded = %s,
                   tsv_bytes_uncompressed = %s,
                   tsv_row_count = %s,
                   parquet_row_count = %s,
                   parquet_bytes_written = %s,
                   parquet_column_count = %s,
                   r2_bucket = %s,
                   r2_prefix = %s,
                   r2_object_key = %s,
                   r2_total_bytes = %s,
                   finished_at = now(),
                   duration_seconds = %s,
                   error_message = %s,
                   notes = %s
             WHERE id = %s;
        """, (
            status,
            zip_bytes_downloaded, tsv_bytes_uncompressed, tsv_row_count,
            parquet_row_count, parquet_bytes_written, parquet_column_count,
            r2_bucket, r2_prefix, r2_object_key, r2_total_bytes,
            duration, error_message,
            Jsonb(notes) if notes else None,
            run_id,
        ))
    conn.commit()


def write_no_change_rows(
    db_url: str, *, year: int, quarter: int,
    source_url: str, source_last_modified: datetime | None,
) -> None:
    """Write a 'no_change' ledger row for raw_zip + every table from the prior run."""
    prior_tables = fetch_prior_table_names(db_url, year=year, quarter=quarter)
    table_names: list[str] = ["raw_zip", *prior_tables]
    for tn in table_names:
        with _connect(db_url) as conn:
            run_id = insert_run_row(
                conn, year=year, quarter=quarter, table_name=tn,
                source_url=source_url,
                source_last_modified=source_last_modified,
                prior_source_last_modified=source_last_modified,
            )
            finalize_run_row(
                conn, run_id, status="no_change", started_at=time.monotonic(),
                notes={"skipped_via": "skip_if_unchanged"},
            )


# ------------------------------------------------------------------ #
# Per-quarter orchestrator
# ------------------------------------------------------------------ #


@dataclass
class QuarterArgs:
    year: int
    quarter: int
    workdir: Path
    r2_bucket: str
    r2_prefix: str
    include_raw_zip: bool
    skip_if_unchanged: bool
    max_rows: int | None
    db_url: str | None


def process_quarter(args: QuarterArgs) -> int:  # noqa: C901
    """Single (year, quarter) pipeline. Returns 0 on success, non-zero on failure."""
    yq = f"{args.year}q{args.quarter}"
    url = ZIP_URL_TEMPLATE.format(year=args.year, q=args.quarter)
    log.info("=" * 70)
    log.info("=== %s :: %s ===", yq, url)
    log.info("=" * 70)

    s3 = _r2_client()
    quarter_workdir = args.workdir / yq
    quarter_workdir.mkdir(parents=True, exist_ok=True)
    zip_path = quarter_workdir / f"{yq}_nport.zip"

    # Discovery row.
    disc_id: str | None = None
    if args.db_url:
        with _connect(args.db_url) as conn:
            disc_id = insert_run_row(
                conn, year=args.year, quarter=args.quarter,
                table_name="discovery", source_url=url,
            )

    try:
        with _httpx_client() as client:
            # 1. HEAD probe.
            status_code, lm, content_length = head_zip(client, url)
            if status_code == 404:
                log.warning("%s: HEAD 404 — quarter not yet published", yq)
                if args.db_url and disc_id:
                    with _connect(args.db_url) as conn:
                        finalize_run_row(
                            conn, disc_id, status="skipped",
                            started_at=time.monotonic(),
                            notes={"reason": "HEAD 404 — quarter not published"},
                        )
                return 0
            if status_code != 200:
                raise RuntimeError(f"HEAD {url} returned {status_code}")

            # 2. Idempotency gate.
            prior_lm: datetime | None = None
            if args.db_url:
                prior_lm = fetch_prior_last_modified(
                    args.db_url, year=args.year, quarter=args.quarter,
                )
            if (
                args.skip_if_unchanged
                and prior_lm is not None
                and lm is not None
                and prior_lm == lm
            ):
                log.info("%s: unchanged (Last-Modified=%s) — skipping", yq, lm)
                if args.db_url and disc_id:
                    with _connect(args.db_url) as conn:
                        finalize_run_row(
                            conn, disc_id, status="no_change",
                            started_at=time.monotonic(),
                            notes={"skipped_via": "skip_if_unchanged"},
                        )
                    write_no_change_rows(
                        args.db_url, year=args.year, quarter=args.quarter,
                        source_url=url, source_last_modified=lm,
                    )
                return 0

            # 3. Discovery row — completed (HEAD probe).
            if args.db_url and disc_id:
                with _connect(args.db_url) as conn:
                    finalize_run_row(
                        conn, disc_id, status="completed",
                        started_at=time.monotonic(),
                        notes={
                            "head_status": status_code,
                            "content_length": content_length,
                            "last_modified": lm.isoformat() if lm else None,
                        },
                    )

            # 4. Download ZIP.
            log.info("%s: downloading ZIP (%d bytes expected)",
                     yq, content_length or -1)
            zip_started = time.monotonic()
            zip_bytes = download_zip(client, url, zip_path)
            log.info("%s: downloaded %.2f MB in %.1fs",
                     yq, zip_bytes / (1 << 20), time.monotonic() - zip_started)

            # 5. Optionally upload raw ZIP.
            raw_run_id: str | None = None
            if args.include_raw_zip:
                if args.db_url:
                    with _connect(args.db_url) as conn:
                        raw_run_id = insert_run_row(
                            conn, year=args.year, quarter=args.quarter,
                            table_name="raw_zip",
                            source_url=url,
                            source_last_modified=lm,
                            prior_source_last_modified=prior_lm,
                        )
                raw_key = f"{args.r2_prefix}/raw/{yq}_nport.zip"
                raw_started = time.monotonic()
                try:
                    raw_bytes = upload_file_to_r2(
                        s3, bucket=args.r2_bucket, key=raw_key,
                        local_path=zip_path,
                        content_type="application/zip",
                    )
                    log.info("%s: raw ZIP uploaded → s3://%s/%s (%d bytes)",
                             yq, args.r2_bucket, raw_key, raw_bytes)
                    if args.db_url and raw_run_id is not None:
                        with _connect(args.db_url) as conn:
                            finalize_run_row(
                                conn, raw_run_id, status="completed",
                                started_at=raw_started,
                                zip_bytes_downloaded=zip_bytes,
                                r2_bucket=args.r2_bucket,
                                r2_prefix=f"{args.r2_prefix}/raw",
                                r2_object_key=raw_key,
                                r2_total_bytes=raw_bytes,
                            )
                except Exception as exc:
                    log.exception("%s: raw ZIP upload failed", yq)
                    if args.db_url and raw_run_id is not None:
                        with _connect(args.db_url) as conn:
                            finalize_run_row(
                                conn, raw_run_id, status="failed",
                                started_at=raw_started,
                                error_message=str(exc)[:1000],
                            )

        # 6. Discover table set + unzip.
        tsv_members = _enumerate_tsv_members(zip_path)
        log.info("%s: discovered %d TSVs in ZIP", yq, len(tsv_members))

        # Schema-drift surface: compare to prior quarter's table set.
        if args.db_url:
            prior_qtables = fetch_prior_quarter_tables(
                args.db_url, year=args.year, quarter=args.quarter,
            )
            current_qtables = {_logical_name_for(m) for m in tsv_members}
            new_streams = sorted(current_qtables - prior_qtables) if prior_qtables else []
            missing_streams = sorted(prior_qtables - current_qtables) if prior_qtables else []
            if new_streams or missing_streams:
                log.warning(
                    "%s: schema drift vs prior quarter — new=%s missing=%s",
                    yq, new_streams, missing_streams,
                )

        log.info("%s: unzipping...", yq)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(quarter_workdir)

        # 7. Per-table transform → upload → ledger.
        rc = 0
        con = duckdb.connect()
        for tsv_filename in tsv_members:
            logical_name = _logical_name_for(tsv_filename)
            tsv_path = quarter_workdir / tsv_filename
            if not tsv_path.exists():
                log.warning("%s: %s missing — skipping", yq, tsv_filename)
                continue

            t_started = time.monotonic()
            run_id: str | None = None
            if args.db_url:
                with _connect(args.db_url) as conn:
                    run_id = insert_run_row(
                        conn, year=args.year, quarter=args.quarter,
                        table_name=logical_name, source_url=url,
                        source_last_modified=lm,
                        prior_source_last_modified=prior_lm,
                    )

            try:
                tsv_row_count = _count_tsv_rows(tsv_path)
                tsv_bytes = tsv_path.stat().st_size
                parquet_path = quarter_workdir / f"{logical_name}.parquet"

                parquet_row_count, observed_columns = transform_tsv_to_parquet(
                    con,
                    tsv_path=tsv_path,
                    parquet_path=parquet_path,
                    year=args.year,
                    quarter=args.quarter,
                    source_url=url,
                    source_last_modified=lm,
                    max_rows=args.max_rows,
                )

                # Row-count parity check.
                expected_parquet = (
                    min(tsv_row_count, args.max_rows)
                    if args.max_rows is not None else tsv_row_count
                )
                parity_ok = parquet_row_count == expected_parquet

                # Upload Parquet (note: Q-prefix per directive §5).
                r2_key = (
                    f"{args.r2_prefix}/year={args.year}/quarter=Q{args.quarter}/"
                    f"{logical_name}/data.parquet"
                )
                parquet_bytes = upload_file_to_r2(
                    s3, bucket=args.r2_bucket, key=r2_key,
                    local_path=parquet_path,
                    content_type="application/x-parquet",
                )

                parquet_columns = con.execute(
                    f"DESCRIBE SELECT * FROM read_parquet('{parquet_path}')"
                ).fetchall()
                parquet_column_count = len(parquet_columns)

                log.info(
                    "%s/%s: tsv=%d rows (%.2f MB) → parquet=%d rows (%.2f MB) → s3://%s/%s",
                    yq, logical_name, tsv_row_count, tsv_bytes / (1 << 20),
                    parquet_row_count, parquet_bytes / (1 << 20),
                    args.r2_bucket, r2_key,
                )

                if not parity_ok:
                    log.error(
                        "%s/%s: ROW-COUNT PARITY FAIL: tsv=%d, parquet=%d, expected=%d",
                        yq, logical_name, tsv_row_count, parquet_row_count, expected_parquet,
                    )

                notes: dict[str, Any] = {
                    "observed_columns": list(observed_columns),
                    "first_3_columns": list(observed_columns[:3]),
                    "last_3_columns": list(observed_columns[-3:]),
                    "tsv_filename": tsv_filename,
                }
                if not parity_ok:
                    notes["parity_fail"] = {
                        "tsv_rows": tsv_row_count,
                        "parquet_rows": parquet_row_count,
                        "expected": expected_parquet,
                    }
                if args.max_rows is not None:
                    notes["max_rows"] = args.max_rows

                if args.db_url and run_id is not None:
                    with _connect(args.db_url) as conn:
                        finalize_run_row(
                            conn, run_id,
                            status="completed" if parity_ok else "failed",
                            started_at=t_started,
                            zip_bytes_downloaded=zip_bytes,
                            tsv_bytes_uncompressed=tsv_bytes,
                            tsv_row_count=tsv_row_count,
                            parquet_row_count=parquet_row_count,
                            parquet_bytes_written=parquet_bytes,
                            parquet_column_count=parquet_column_count,
                            r2_bucket=args.r2_bucket,
                            r2_prefix=(
                                f"{args.r2_prefix}/year={args.year}/"
                                f"quarter=Q{args.quarter}"
                            ),
                            r2_object_key=r2_key,
                            r2_total_bytes=parquet_bytes,
                            error_message=None if parity_ok else "row_count_parity",
                            notes=notes,
                        )
                if not parity_ok:
                    rc = 1

                # Cleanup per-table TSV + Parquet immediately to bound disk
                # use across the next quarter's (~1.5 GB) dataset.
                try:
                    tsv_path.unlink()
                    parquet_path.unlink()
                except OSError:
                    pass
            except Exception as exc:
                log.exception("%s/%s: transform/upload FAILED", yq, logical_name)
                rc = 1
                if args.db_url and run_id is not None:
                    with _connect(args.db_url) as conn:
                        finalize_run_row(
                            conn, run_id, status="failed",
                            started_at=t_started,
                            error_message=str(exc)[:1000],
                        )
        con.close()
        return rc
    finally:
        try:
            shutil.rmtree(quarter_workdir, ignore_errors=True)
        except Exception:
            pass


# ------------------------------------------------------------------ #
# CLI
# ------------------------------------------------------------------ #

def _parse_year_range(s: str) -> list[int]:
    if "-" in s:
        a, b = s.split("-", 1)
        return list(range(int(a), int(b) + 1))
    return [int(s)]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--year", type=int, default=None,
                   help="Single year (use with --quarter for one quarter).")
    p.add_argument("--quarter", type=int, default=None,
                   help="Single quarter 1-4.")
    p.add_argument("--years", default=None,
                   help=f"Year range, e.g. {EARLIEST_YEAR}-{LATEST_YEAR_DEFAULT}.")
    p.add_argument("--max-rows", type=int, default=None,
                   help="Per-table cap on output rows (smoke).")
    p.add_argument("--skip-if-unchanged", action="store_true",
                   help="Skip quarters whose Last-Modified matches a "
                        "prior completed run.")
    p.add_argument("--workdir", type=Path,
                   default=Path("/tmp/sec_edgar_form_n_port_workdir"),
                   help="Per-quarter scratch dir.")
    p.add_argument("--no-include-raw-zip", action="store_true",
                   help="Skip raw ZIP preservation upload.")
    p.add_argument("--r2-bucket", default=R2_BUCKET_DEFAULT)
    p.add_argument("--r2-prefix-override", default=None,
                   help="Override the R2 prefix (default sec-edgar/form-n-port).")
    p.add_argument("--no-audit", action="store_true",
                   help="Skip ops.sec_edgar_form_n_port_r2_ingest_runs writes.")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    pairs: list[tuple[int, int]] = []
    if args.years:
        years: list[int] = _parse_year_range(args.years)
        for y in years:
            for q in (1, 2, 3, 4):
                pairs.append((y, q))
    elif args.year is not None:
        if args.quarter is not None:
            pairs.append((args.year, args.quarter))
        else:
            for q in (1, 2, 3, 4):
                pairs.append((args.year, q))
    else:
        log.error("must pass --year [+ --quarter] or --years YYYY-YYYY")
        return 2

    args.workdir.mkdir(parents=True, exist_ok=True)
    db_url = None if args.no_audit else _database_url()
    if not args.no_audit and not db_url:
        log.warning("no DB URL set; audit ledger writes will be skipped")
    r2_prefix = args.r2_prefix_override or R2_PREFIX_DEFAULT
    log.info("R2 target: s3://%s/%s/", args.r2_bucket, r2_prefix)
    log.info("processing %d quarters", len(pairs))

    rc = 0
    for (y, q) in pairs:
        try:
            qa = QuarterArgs(
                year=y, quarter=q,
                workdir=args.workdir,
                r2_bucket=args.r2_bucket,
                r2_prefix=r2_prefix,
                include_raw_zip=not args.no_include_raw_zip,
                skip_if_unchanged=args.skip_if_unchanged,
                max_rows=args.max_rows,
                db_url=db_url,
            )
            r = process_quarter(qa)
            if r != 0:
                rc = r
        except Exception:
            log.exception("quarter %d-Q%d failed", y, q)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())

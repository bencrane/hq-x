#!/usr/bin/env python3
"""SBA 7(a) + 504 FOIA historical → R2/RW Fuel Tank ingest.

Mirrors the HMDA Volume King pattern (run_hmda_r2_ingest.py): stream a public
CSV → DuckDB transform → ZSTD Parquet → boto3 upload to R2 → audit row.

The 6 historical FOIA snapshots span 1991-Present:
  7(a) 1991-1999, 2000-2009, 2010-2019, 2020-Present
  504  1991-2009, 2010-Present

Each invocation processes ONE (program, decade) slice; total expected
cardinality across all six is ~1.5M+ rows.

Audit ledger: ops.sba_r2_ingest_runs.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_sba_historical_r2_ingest.py 7a 2020
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_sba_historical_r2_ingest.py 504 1991 --dry-run
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_sba_historical_r2_ingest.py 7a 2020 --max-rows 50000

Special form:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_sba_historical_r2_ingest.py --all
  Iterates all 6 (program, decade) slices sequentially.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
import duckdb
import httpx
import psycopg
from psycopg.types.json import Jsonb


R2_BUCKET = "dex-raw-landing-zone"
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 5

# SBA 7(a)/504 FOIA columns that should be cast to numeric. Reading
# all_varchar=TRUE preserves leading zeros and any sentinels; TRY_CAST
# in the projection cleanly handles malformed historical data.
SBA_NUMERIC_COLS: tuple[str, ...] = (
    "grossapproval",
    "sbaguaranteedapproval",
    "approvaleff",
    "thirdpartydollars",
    "termmonths",
    "termmonthsto",
    "initialinterestrate",
    "jobssupported",
    "jobsretained",
    "naicscode",
    "borrzip",
    "borrzip4",
    "projectstateunsigned",
    "currentapprovalamount",
    "businessage",
    "annualintimerate",
    "averagermsbalance",
    "amountoutstanding",
    "chargeoffamount",
    "grosschargeoffamount",
    "grosschargeoffamt",
)
# These are "sometimes numeric" — TRY_CAST keeps them as DOUBLE if convertible,
# NULL otherwise. Schema varies across decades.
# NOTE (2026-05-08): deliverymethod, subprogramdescription, fixedoradjustablerate,
# revolverstatus, and loanstatus were intentionally REMOVED from this list —
# they hold text codes ("PLP", "GUARANTY", "PIF", "CHGOFF") that TRY_CAST
# silently nulled out, destroying the loan_status_canonical signal. Keep
# this list to actually-numeric columns only; text columns flow through
# unchanged via the all_varchar=TRUE projection.

SBA_DATE_COLS: tuple[str, ...] = (
    "approvaldate",
    "firstdisbursementdate",
    "lastdisbursementdate",
    "fullymatureddate",
    "paidinfulldate",
    "chargeoffdate",
)


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("sba-r2-ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# Per-(program, decade) configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Slice:
    program: str       # '7a' | '504'
    decade: int        # start year of the slice (e.g., 2020 for FY2020-Present)
    decade_label: str  # e.g., 'fy2020-present', 'fy1991-fy1999'
    url: str           # data.sba.gov CKAN download URL
    parquet_filename: str  # e.g., 'sba_7a_fy2020-present.parquet'

    @property
    def r2_prefix(self) -> str:
        return f"sba/program={self.program}/decade={self.decade}/"

    @property
    def r2_key(self) -> str:
        return self.r2_prefix + self.parquet_filename


SBA_SLICES: tuple[Slice, ...] = (
    Slice(
        program="7a", decade=1991, decade_label="fy1991-fy1999",
        url="https://data.sba.gov/en/dataset/0ff8e8e9-b967-4f4e-987c-6ac78c575087/resource/182e9421-ccee-4562-acb3-93b34fb695f2/download/foia-7a-fy1991-fy1999-asof-260331.csv",
        parquet_filename="sba_7a_fy1991_fy1999.parquet",
    ),
    Slice(
        program="7a", decade=2000, decade_label="fy2000-fy2009",
        url="https://data.sba.gov/en/dataset/0ff8e8e9-b967-4f4e-987c-6ac78c575087/resource/186eb176-b53e-4cbe-ab93-e5c4fb50197d/download/foia-7a-fy2000-fy2009-asof-260331.csv",
        parquet_filename="sba_7a_fy2000_fy2009.parquet",
    ),
    Slice(
        program="7a", decade=2010, decade_label="fy2010-fy2019",
        url="https://data.sba.gov/en/dataset/0ff8e8e9-b967-4f4e-987c-6ac78c575087/resource/3f838176-6060-44db-9c91-b4acafbcb28c/download/foia-7a-fy2010-fy2019-asof-260331.csv",
        parquet_filename="sba_7a_fy2010_fy2019.parquet",
    ),
    Slice(
        program="7a", decade=2020, decade_label="fy2020-present",
        url="https://data.sba.gov/en/dataset/0ff8e8e9-b967-4f4e-987c-6ac78c575087/resource/d67d3ccb-2002-4134-a288-481b51cd3479/download/foia-7a-fy2020-present-asof-260331.csv",
        parquet_filename="sba_7a_fy2020_present.parquet",
    ),
    Slice(
        program="504", decade=1991, decade_label="fy1991-fy2009",
        url="https://data.sba.gov/en/dataset/0ff8e8e9-b967-4f4e-987c-6ac78c575087/resource/8854d636-599d-463f-a961-7dbdb3bab152/download/foia-504-fy1991-fy2009-asof-260331.csv",
        parquet_filename="sba_504_fy1991_fy2009.parquet",
    ),
    Slice(
        program="504", decade=2010, decade_label="fy2010-present",
        url="https://data.sba.gov/en/dataset/0ff8e8e9-b967-4f4e-987c-6ac78c575087/resource/4ad7f0f1-9da6-4d90-8bdb-89a6f821a1a9/download/foia-504-fy2010-present-asof-260331.csv",
        parquet_filename="sba_504_fy2010_present.parquet",
    ),
)


def _slice_lookup(program: str, decade: int) -> Slice:
    for s in SBA_SLICES:
        if s.program == program and s.decade == decade:
            return s
    raise SystemExit(
        f"no slice for program={program} decade={decade}; "
        f"valid: {[(s.program, s.decade) for s in SBA_SLICES]}"
    )


# --------------------------------------------------------------------------- #
# Env helpers
# --------------------------------------------------------------------------- #


def _required_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"{name} is not set in the environment.")
    return v


def _r2_client() -> "boto3.client":
    return boto3.client(
        "s3",
        endpoint_url=_required_env("R2_ENDPOINT"),
        aws_access_key_id=_required_env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=_required_env("R2_SECRET_ACCESS_KEY"),
        region_name="auto",
    )


def _database_url() -> str:
    return _required_env("DEX_DB_URL_POOLED")


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #


def head_url(client: httpx.Client, url: str) -> tuple[int | None, datetime | None, int]:
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = client.head(url, follow_redirects=True, timeout=30.0)
            if r.status_code == 404:
                return None, None, 404
            if r.status_code in RETRY_STATUSES:
                wait = min(2 ** attempt, 30)
                log.warning("HEAD %s HTTP %s; retry in %ss", url, r.status_code, wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            cl = int(r.headers.get("content-length", 0)) or None
            lm_raw = r.headers.get("last-modified")
            lm: datetime | None = None
            if lm_raw:
                try:
                    lm = datetime.strptime(
                        lm_raw, "%a, %d %b %Y %H:%M:%S %Z"
                    ).replace(tzinfo=timezone.utc)
                except ValueError:
                    lm = None
            return cl, lm, r.status_code
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            wait = min(2 ** attempt, 30)
            log.warning("HEAD %s error (%s); retry in %ss", url, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"HEAD failed: {last_exc}")


def download_csv(client: httpx.Client, url: str, dest: Path) -> int:
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            written = 0
            with client.stream("GET", url, follow_redirects=True, timeout=1800.0) as r:
                if r.status_code in RETRY_STATUSES:
                    wait = min(2 ** attempt, 30)
                    log.warning("GET %s HTTP %s; retry in %ss", url, r.status_code, wait)
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                with dest.open("wb") as f:
                    last_log = time.monotonic()
                    for chunk in r.iter_bytes(chunk_size=1 << 20):
                        f.write(chunk)
                        written += len(chunk)
                        now = time.monotonic()
                        if now - last_log >= 10.0:
                            log.info(
                                "  download progress: %.1f MB written",
                                written / (1 << 20),
                            )
                            last_log = now
            return written
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            wait = min(2 ** attempt, 30)
            log.warning("GET %s error (%s); retry in %ss", url, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"download failed: {last_exc}")


# --------------------------------------------------------------------------- #
# DuckDB transform
# --------------------------------------------------------------------------- #


def _normalize(c: str) -> str:
    """Normalize column names: lowercase + replace hyphens/spaces with underscore."""
    return c.lower().replace("-", "_").replace(" ", "_")


def duckdb_csv_to_parquet(
    csv_path: Path,
    parquet_path: Path,
    *,
    sl: Slice,
    log_prefix: str,
    max_rows: int | None,
) -> tuple[int, int]:
    """Read CSV (all VARCHAR), TRY_CAST numeric/date columns, write ZSTD Parquet.
    Returns (rows_in_csv, rows_in_parquet).
    """
    con = duckdb.connect(":memory:")
    con.execute("PRAGMA threads=4;")
    con.execute("PRAGMA memory_limit='8GB';")

    # Discover the schema. all_varchar=TRUE preserves leading zeros (zip codes,
    # NAICS codes) and avoids inference on legacy schema quirks.
    con.execute(f"""
        CREATE VIEW raw AS
        SELECT * FROM read_csv_auto('{csv_path}', all_varchar=TRUE,
                                    sample_size=-1, header=TRUE,
                                    ignore_errors=TRUE);
    """)
    cols_info = con.execute("DESCRIBE raw;").fetchall()
    csv_cols = [c[0] for c in cols_info]
    log.info("%s discovered %d columns in CSV", log_prefix, len(csv_cols))

    # Normalize: strip BOM, lower, replace hyphens/spaces with underscore.
    normalized = [_normalize(c.lstrip("﻿")) for c in csv_cols]
    if any(s != n for s, n in zip(csv_cols, normalized)):
        n_renames = sum(1 for s, n in zip(csv_cols, normalized) if s != n)
        log.info("%s normalizing %d column name(s)", log_prefix, n_renames)

    rows_in_csv_row = con.execute("SELECT count(*) FROM raw;").fetchone()
    rows_in_csv = int(rows_in_csv_row[0]) if rows_in_csv_row else 0
    log.info("%s CSV row count: %d", log_prefix, rows_in_csv)

    # Pick which columns to numeric-cast / date-cast.
    numeric_present = [c for c in SBA_NUMERIC_COLS if c in normalized]
    date_present = [c for c in SBA_DATE_COLS if c in normalized]
    if numeric_present:
        log.info("%s casting %d numeric col(s) to DOUBLE: %s",
                 log_prefix, len(numeric_present), numeric_present[:6])
    if date_present:
        log.info("%s casting %d date col(s) to DATE: %s",
                 log_prefix, len(date_present), date_present[:6])

    select_parts: list[str] = []
    for src_col, dst_col in zip(csv_cols, normalized):
        src_q = f'"{src_col}"'
        dst_q = f'"{dst_col}"'
        if dst_col in numeric_present:
            select_parts.append(f'TRY_CAST({src_q} AS DOUBLE) AS {dst_q}')
        elif dst_col in date_present:
            # SBA dates are MM/DD/YYYY in older slices and YYYY-MM-DD in newer.
            # Try strptime with both; fall back to NULL.
            select_parts.append(
                f"COALESCE("
                f"TRY_CAST({src_q} AS DATE), "
                f"TRY_STRPTIME({src_q}, '%m/%d/%Y')::DATE"
                f") AS {dst_q}"
            )
        elif src_col != dst_col:
            select_parts.append(f"{src_q} AS {dst_q}")
        else:
            select_parts.append(src_q)

    # Add stable partition metadata.
    select_parts.append(f"CAST('{sl.program}' AS VARCHAR) AS sba_program")
    select_parts.append(f"CAST({sl.decade} AS SMALLINT) AS sba_decade")
    select_parts.append(f"CAST('{sl.decade_label}' AS VARCHAR) AS sba_decade_label")

    limit_clause = f"LIMIT {max_rows}" if max_rows is not None else ""
    select_sql = (
        f"SELECT {', '.join(select_parts)} FROM raw {limit_clause}"
    )

    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    log.info("%s writing Parquet → %s (ZSTD)", log_prefix, parquet_path)
    t0 = time.monotonic()
    con.execute(f"""
        COPY ({select_sql}) TO '{parquet_path}'
        (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000);
    """)
    log.info("%s Parquet write done in %.1fs", log_prefix, time.monotonic() - t0)

    rows_in_pq_row = con.execute(
        f"SELECT count(*) FROM read_parquet('{parquet_path}');"
    ).fetchone()
    rows_in_parquet = int(rows_in_pq_row[0]) if rows_in_pq_row else 0
    con.close()
    return rows_in_csv, rows_in_parquet


# --------------------------------------------------------------------------- #
# R2 upload
# --------------------------------------------------------------------------- #


def upload_to_r2(
    parquet_path: Path,
    *,
    bucket: str,
    key: str,
    log_prefix: str,
) -> int:
    s3 = _r2_client()
    file_bytes = parquet_path.stat().st_size
    log.info("%s uploading %s (%.1f MB) → s3://%s/%s",
             log_prefix, parquet_path, file_bytes / (1 << 20), bucket, key)

    last_progress: dict[str, float] = {"sent": 0.0, "ts": time.monotonic()}

    def _progress(n: int) -> None:
        last_progress["sent"] += n
        now = time.monotonic()
        if now - last_progress["ts"] >= 10.0:
            pct = 100.0 * last_progress["sent"] / max(file_bytes, 1)
            log.info("  upload progress: %.1f MB (%.1f%%)",
                     last_progress["sent"] / (1 << 20), pct)
            last_progress["ts"] = now

    s3.upload_file(
        str(parquet_path), bucket, key,
        ExtraArgs={"ContentType": "application/x-parquet"},
        Callback=_progress,
    )
    log.info("%s upload done", log_prefix)
    return file_bytes


# --------------------------------------------------------------------------- #
# Audit-row helpers
# --------------------------------------------------------------------------- #


def insert_run_row(
    conn: psycopg.Connection,
    sl: Slice,
    *,
    source_last_modified: datetime | None,
    prior_source_last_modified: datetime | None,
) -> str:
    sql = """
    INSERT INTO ops.sba_r2_ingest_runs (
        program, decade, decade_label, status, source_url,
        source_last_modified, prior_source_last_modified
    ) VALUES (%s, %s, %s, 'running', %s, %s, %s)
    RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            sl.program, sl.decade, sl.decade_label, sl.url,
            source_last_modified, prior_source_last_modified,
        ))
        row_id = cur.fetchone()[0]
    conn.commit()
    return str(row_id)


def get_prior_source_last_modified(
    conn: psycopg.Connection, sl: Slice,
) -> datetime | None:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT source_last_modified
              FROM ops.sba_r2_ingest_runs
             WHERE program = %s AND decade = %s AND status = 'completed'
             ORDER BY started_at DESC LIMIT 1
            """,
            (sl.program, sl.decade),
        )
        row = cur.fetchone()
    return row[0] if row else None


def write_no_change_run(
    conn: psycopg.Connection,
    sl: Slice,
    *,
    source_last_modified: datetime | None,
    prior_source_last_modified: datetime | None,
) -> None:
    started = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ops.sba_r2_ingest_runs (
                program, decade, decade_label, status, source_url,
                source_last_modified, prior_source_last_modified,
                started_at, finished_at, duration_seconds, notes
            ) VALUES (%s, %s, %s, 'no_change', %s, %s, %s, %s, %s, 0, %s);
            """,
            (
                sl.program, sl.decade, sl.decade_label, sl.url,
                source_last_modified, prior_source_last_modified,
                started, started,
                Jsonb({"reason": "source_last_modified unchanged"}),
            ),
        )
    conn.commit()


def finalize_run_row(
    conn: psycopg.Connection,
    run_id: str,
    *,
    status: str,
    csv_bytes: int,
    rows_in_csv: int,
    parquet_bytes_written: int,
    parquet_row_count: int,
    parquet_part_count: int,
    r2_bucket: str | None,
    r2_prefix: str | None,
    r2_object_count: int,
    r2_total_bytes: int,
    started_at: float,
    error_message: str | None,
    notes: dict[str, Any] | None,
) -> None:
    duration = round(time.monotonic() - started_at, 3)
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE ops.sba_r2_ingest_runs
               SET status = %s,
                   csv_bytes_downloaded = %s,
                   rows_in_csv = %s,
                   parquet_bytes_written = %s, parquet_row_count = %s,
                   parquet_part_count = %s,
                   r2_bucket = %s, r2_prefix = %s,
                   r2_object_count = %s, r2_total_bytes = %s,
                   finished_at = now(), duration_seconds = %s,
                   error_message = %s, notes = %s
             WHERE id = %s;
            """, (
            status, csv_bytes, rows_in_csv,
            parquet_bytes_written, parquet_row_count, parquet_part_count,
            r2_bucket, r2_prefix, r2_object_count, r2_total_bytes,
            duration, error_message,
            Jsonb(notes) if notes else None, run_id,
        ))
    conn.commit()


# --------------------------------------------------------------------------- #
# Per-slice main
# --------------------------------------------------------------------------- #


def ingest_slice(
    sl: Slice,
    *,
    skip_if_unchanged: bool,
    dry_run: bool,
    workdir: Path,
    max_rows: int | None,
    r2_prefix_override: str | None,
) -> int:
    log_prefix = f"[{sl.program} {sl.decade_label}]"
    started_wall = time.monotonic()
    log.info("%s start url=%s", log_prefix, sl.url)

    with httpx.Client(headers={"User-Agent": "data-engine-x/sba-r2-ingest"}) as client:
        try:
            content_length, source_last_modified, status_code = head_url(client, sl.url)
        except Exception:
            log.exception("%s HEAD failed", log_prefix)
            return 1
        if status_code == 404:
            log.error("%s HEAD 404 — source URL not published", log_prefix)
            return 1
        log.info("%s HEAD content_length=%s last_modified=%s",
                 log_prefix, content_length, source_last_modified)
        if dry_run:
            log.info("%s DRY RUN — exiting after HEAD", log_prefix)
            return 0

        with psycopg.connect(_database_url()) as conn:
            prior = get_prior_source_last_modified(conn, sl)
            log.info("%s prior source_last_modified: %s", log_prefix, prior)
            if (
                skip_if_unchanged
                and prior is not None
                and source_last_modified is not None
                and source_last_modified <= prior
            ):
                log.info("%s source unchanged — recording no_change", log_prefix)
                write_no_change_run(
                    conn, sl,
                    source_last_modified=source_last_modified,
                    prior_source_last_modified=prior,
                )
                return 0

            run_id = insert_run_row(
                conn, sl,
                source_last_modified=source_last_modified,
                prior_source_last_modified=prior,
            )
            log.info("%s run id: %s", log_prefix, run_id)

            csv_path = workdir / f"sba_{sl.program}_{sl.decade}.csv"
            parquet_path = workdir / sl.parquet_filename

            try:
                csv_bytes = download_csv(client, sl.url, csv_path)
                log.info("%s downloaded %d bytes -> %s", log_prefix, csv_bytes, csv_path)

                rows_in_csv, parquet_row_count = duckdb_csv_to_parquet(
                    csv_path, parquet_path,
                    sl=sl, log_prefix=log_prefix, max_rows=max_rows,
                )
                parquet_bytes = parquet_path.stat().st_size
                log.info(
                    "%s parquet: %d rows, %.1f MB (%.2f bytes/row)",
                    log_prefix, parquet_row_count,
                    parquet_bytes / (1 << 20),
                    parquet_bytes / max(parquet_row_count, 1),
                )

                r2_prefix = r2_prefix_override or sl.r2_prefix
                r2_key = r2_prefix + sl.parquet_filename
                uploaded_bytes = upload_to_r2(
                    parquet_path, bucket=R2_BUCKET, key=r2_key,
                    log_prefix=log_prefix,
                )

                finalize_run_row(
                    conn, run_id, status="completed",
                    csv_bytes=csv_bytes, rows_in_csv=rows_in_csv,
                    parquet_bytes_written=parquet_bytes,
                    parquet_row_count=parquet_row_count,
                    parquet_part_count=1,
                    r2_bucket=R2_BUCKET,
                    r2_prefix=r2_prefix,
                    r2_object_count=1,
                    r2_total_bytes=uploaded_bytes,
                    started_at=started_wall, error_message=None,
                    notes={"r2_key": r2_key, "max_rows": max_rows},
                )
                log.info(
                    "%s DONE rows=%d parquet=%.1f MB upload=%.1f MB wall=%.1fs",
                    log_prefix, parquet_row_count,
                    parquet_bytes / (1 << 20), uploaded_bytes / (1 << 20),
                    time.monotonic() - started_wall,
                )
                return 0

            except Exception as exc:
                log.exception("%s ingest failed", log_prefix)
                finalize_run_row(
                    conn, run_id, status="failed",
                    csv_bytes=0, rows_in_csv=0,
                    parquet_bytes_written=0, parquet_row_count=0,
                    parquet_part_count=0,
                    r2_bucket=None, r2_prefix=None,
                    r2_object_count=0, r2_total_bytes=0,
                    started_at=started_wall,
                    error_message=str(exc), notes=None,
                )
                return 1

            finally:
                for p in (csv_path, parquet_path):
                    try:
                        p.unlink(missing_ok=True)
                    except Exception:
                        pass


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("program", nargs="?", choices=["7a", "504"],
                   help="SBA program (7a | 504). Required unless --all.")
    p.add_argument("decade", nargs="?", type=int,
                   help="Decade start year (1991 | 2000 | 2010 | 2020). "
                        "504 supports 1991 (FY1991-2009) + 2010 (FY2010-Present).")
    p.add_argument("--all", action="store_true",
                   help="Ingest every (program, decade) slice sequentially.")
    p.add_argument("--skip-if-unchanged", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max-rows", type=int, default=None)
    p.add_argument("--workdir", default=None)
    p.add_argument("--r2-prefix-override", default=None)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    workdir = Path(args.workdir or "/tmp/sba_r2_ingest")
    workdir.mkdir(parents=True, exist_ok=True)

    if args.all:
        slices = list(SBA_SLICES)
    else:
        if not args.program or args.decade is None:
            log.error("must pass program + decade (or use --all)")
            return 2
        slices = [_slice_lookup(args.program, args.decade)]

    rc = 0
    for sl in slices:
        log.info("=" * 70)
        log.info("=== INGEST: program=%s decade=%s ===", sl.program, sl.decade_label)
        log.info("=" * 70)
        rc_one = ingest_slice(
            sl,
            skip_if_unchanged=args.skip_if_unchanged,
            dry_run=args.dry_run,
            workdir=workdir,
            max_rows=args.max_rows,
            r2_prefix_override=args.r2_prefix_override,
        )
        if rc_one != 0:
            rc = rc_one
            log.error("slice failed; continuing with remaining slices")
    return rc


if __name__ == "__main__":
    sys.exit(main())

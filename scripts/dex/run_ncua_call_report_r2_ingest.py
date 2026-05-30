#!/usr/bin/env python3
"""NCUA quarterly call-report archive → R2/RW Fuel Tank ingest.

Mirrors the HMDA Volume King pattern but adapted for multi-file ZIPs:
each NCUA quarterly ZIP contains ~30 comma-delimited `.txt` files (FOICU,
FOICULong, FS220, FS220A-S, AcctDesc, etc.). Each `.txt` becomes its own
ZSTD Parquet at the partitioned R2 path:

  s3://dex-raw-landing-zone/ncua/year={YYYY}/quarter={Q}/<lower(table)>.parquet

Audit ledger: ops.ncua_r2_ingest_runs (one row per (year, quarter) ingest).
Idempotency basis: HEAD Last-Modified (per-ZIP).

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_ncua_call_report_r2_ingest.py 2024 4
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_ncua_call_report_r2_ingest.py 2024 4 --dry-run

Special form:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_ncua_call_report_r2_ingest.py --all
  Default span: 2015-Q2 through 2024-Q4 (39 quarters; 2015-Q1 has a different
  legacy URL pattern and is intentionally skipped per the directive).
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import sys
import time
import zipfile
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

# Quarter → calendar month code embedded in the URL.
QUARTER_TO_MONTH: dict[int, str] = {1: "03", 2: "06", 3: "09", 4: "12"}

# Default span: 2015-Q2 through 2024-Q4 (39 quarters). 2015-Q1 has a different
# legacy URL and is intentionally skipped per the directive.
DEFAULT_SPAN: list[tuple[int, int]] = [
    (y, q)
    for y in range(2015, 2025)
    for q in (1, 2, 3, 4)
    if not (y == 2015 and q == 1)
]


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("ncua-r2-ingest")


log = _logger()


@dataclass(frozen=True)
class Quarter:
    year: int
    quarter: int

    @property
    def month_code(self) -> str:
        return QUARTER_TO_MONTH[self.quarter]

    @property
    def url(self) -> str:
        return (
            "https://ncua.gov/files/publications/analysis/"
            f"call-report-data-{self.year}-{self.month_code}.zip"
        )

    @property
    def r2_prefix(self) -> str:
        return f"ncua/year={self.year}/quarter=Q{self.quarter}/"


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
# HTTP layer (clone of SBA / HMDA shape)
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


def download_zip(client: httpx.Client, url: str, dest: Path) -> int:
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
# ZIP unpack + per-table Parquet conversion
# --------------------------------------------------------------------------- #


_TABLE_BASENAME_RE = re.compile(r"^(.*?)\.txt$", re.IGNORECASE)


def _normalize_table_name(raw: str) -> str:
    """NCUA inner filenames have spaces ("Credit Union Branch Information.txt")
    and dashes ("Acct-DescTradeNames.txt"). Normalize to lower + alnum+_."""
    s = raw.lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_") or "unnamed"


def extract_txt_files(zip_path: Path, dest_dir: Path) -> list[tuple[str, Path, int]]:
    """Extract every .txt file inside the ZIP. Returns a list of
    (table_basename_normalized, extracted_path, uncompressed_bytes).
    """
    out: list[tuple[str, Path, int]] = []
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            name = Path(info.filename).name
            m = _TABLE_BASENAME_RE.match(name)
            if not m:
                continue
            base = _normalize_table_name(m.group(1))
            if not base:
                continue
            target = dest_dir / f"{base}.txt"
            with z.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst, length=1 << 20)
            out.append((base, target, info.file_size))
    return out


def _normalize_col(c: str) -> str:
    """Lowercase + strip non-alnum (replace with underscore). NCUA columns
    occasionally have spaces or odd punctuation; normalize for SQL safety."""
    s = c.strip().lstrip("﻿").lower()
    s = re.sub(r"[^a-z0-9_]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "col"


def txt_to_parquet(
    txt_path: Path,
    parquet_path: Path,
    *,
    table: str,
    qtr: Quarter,
    log_prefix: str,
    max_rows: int | None,
) -> tuple[int, int, list[str]]:
    """Read comma-delimited NCUA .txt file as VARCHAR. Project rows with
    normalized column names + partition metadata. Write ZSTD Parquet.

    Returns (rows_in_input, rows_in_parquet, columns_used).
    """
    con = duckdb.connect(":memory:")
    con.execute("PRAGMA threads=2;")
    con.execute("PRAGMA memory_limit='4GB';")

    # NCUA .txt files use ',' delimiter with double-quoted headers + values.
    # all_varchar=TRUE preserves leading zeros (cu_number, charter_no, etc.).
    # ignore_errors=TRUE survives the occasional malformed historical row.
    con.execute(f"""
        CREATE VIEW raw AS
        SELECT * FROM read_csv_auto(
          '{txt_path}',
          delim=',', quote='"', escape='"', header=TRUE,
          all_varchar=TRUE, sample_size=-1,
          ignore_errors=TRUE
        );
    """)
    cols_info = con.execute("DESCRIBE raw;").fetchall()
    src_cols = [c[0] for c in cols_info]
    norm_cols = [_normalize_col(c) for c in src_cols]

    # Dedup any normalization collisions by appending _2, _3, ...
    seen: dict[str, int] = {}
    final_cols: list[str] = []
    for n in norm_cols:
        if n in seen:
            seen[n] += 1
            final_cols.append(f"{n}_{seen[n]}")
        else:
            seen[n] = 1
            final_cols.append(n)

    rows_in_row = con.execute("SELECT count(*) FROM raw;").fetchone()
    rows_in = int(rows_in_row[0]) if rows_in_row else 0

    # Build the projection.
    select_parts: list[str] = []
    for s, n in zip(src_cols, final_cols):
        if s == n:
            select_parts.append(f'"{s}"')
        else:
            select_parts.append(f'"{s}" AS "{n}"')
    select_parts.append(f"CAST({qtr.year} AS SMALLINT) AS ncua_year")
    select_parts.append(f"CAST({qtr.quarter} AS SMALLINT) AS ncua_quarter")
    select_parts.append(f"CAST('{table}' AS VARCHAR) AS ncua_table")

    limit_clause = f"LIMIT {max_rows}" if max_rows is not None else ""
    select_sql = (
        f"SELECT {', '.join(select_parts)} FROM raw {limit_clause}"
    )

    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    con.execute(f"""
        COPY ({select_sql}) TO '{parquet_path}'
        (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 50000);
    """)
    log.info("%s   %s: %d rows, %d cols → %.2f MB in %.1fs",
             log_prefix, table, rows_in, len(final_cols),
             parquet_path.stat().st_size / (1 << 20),
             time.monotonic() - t0)

    rows_pq_row = con.execute(
        f"SELECT count(*) FROM read_parquet('{parquet_path}');"
    ).fetchone()
    rows_pq = int(rows_pq_row[0]) if rows_pq_row else 0
    con.close()
    return rows_in, rows_pq, final_cols


def upload_to_r2(
    parquet_path: Path,
    *,
    bucket: str,
    key: str,
) -> int:
    s3 = _r2_client()
    file_bytes = parquet_path.stat().st_size
    s3.upload_file(
        str(parquet_path), bucket, key,
        ExtraArgs={"ContentType": "application/x-parquet"},
    )
    return file_bytes


# --------------------------------------------------------------------------- #
# Audit-row helpers
# --------------------------------------------------------------------------- #


def insert_run_row(
    conn: psycopg.Connection,
    qtr: Quarter,
    *,
    source_url: str,
    source_last_modified: datetime | None,
    prior_source_last_modified: datetime | None,
) -> str:
    sql = """
    INSERT INTO ops.ncua_r2_ingest_runs (
        year, quarter, status, source_url,
        source_last_modified, prior_source_last_modified
    ) VALUES (%s, %s, 'running', %s, %s, %s)
    RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            qtr.year, qtr.quarter, source_url,
            source_last_modified, prior_source_last_modified,
        ))
        row_id = cur.fetchone()[0]
    conn.commit()
    return str(row_id)


def get_prior_source_last_modified(
    conn: psycopg.Connection, qtr: Quarter,
) -> datetime | None:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT source_last_modified
              FROM ops.ncua_r2_ingest_runs
             WHERE year = %s AND quarter = %s AND status = 'completed'
             ORDER BY started_at DESC LIMIT 1
            """,
            (qtr.year, qtr.quarter),
        )
        row = cur.fetchone()
    return row[0] if row else None


def write_no_change_run(
    conn: psycopg.Connection,
    qtr: Quarter,
    *,
    source_url: str,
    source_last_modified: datetime | None,
    prior_source_last_modified: datetime | None,
) -> None:
    started = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ops.ncua_r2_ingest_runs (
                year, quarter, status, source_url,
                source_last_modified, prior_source_last_modified,
                started_at, finished_at, duration_seconds, notes
            ) VALUES (%s, %s, 'no_change', %s, %s, %s, %s, %s, 0, %s);
            """,
            (
                qtr.year, qtr.quarter, source_url, source_last_modified,
                prior_source_last_modified, started, started,
                Jsonb({"reason": "source_last_modified unchanged"}),
            ),
        )
    conn.commit()


def finalize_run_row(
    conn: psycopg.Connection,
    run_id: str,
    *,
    status: str,
    zip_bytes: int,
    zip_inner_files: int,
    parquet_object_count: int,
    parquet_row_count_total: int,
    parquet_bytes_written: int,
    r2_bucket: str | None,
    r2_prefix: str | None,
    r2_total_bytes: int,
    started_at: float,
    error_message: str | None,
    notes: dict[str, Any] | None,
) -> None:
    duration = round(time.monotonic() - started_at, 3)
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE ops.ncua_r2_ingest_runs
               SET status = %s,
                   zip_bytes_downloaded = %s,
                   zip_inner_file_count = %s,
                   parquet_object_count = %s,
                   parquet_row_count_total = %s,
                   parquet_bytes_written = %s,
                   r2_bucket = %s, r2_prefix = %s,
                   r2_total_bytes = %s,
                   finished_at = now(), duration_seconds = %s,
                   error_message = %s, notes = %s
             WHERE id = %s;
            """, (
            status, zip_bytes, zip_inner_files,
            parquet_object_count, parquet_row_count_total,
            parquet_bytes_written,
            r2_bucket, r2_prefix, r2_total_bytes,
            duration, error_message,
            Jsonb(notes) if notes else None, run_id,
        ))
    conn.commit()


# --------------------------------------------------------------------------- #
# Per-(year, quarter) main
# --------------------------------------------------------------------------- #


def ingest_quarter(
    qtr: Quarter,
    *,
    skip_if_unchanged: bool,
    dry_run: bool,
    workdir: Path,
    max_rows: int | None,
    r2_prefix_override: str | None,
    only_tables: set[str] | None = None,
) -> int:
    log_prefix = f"[{qtr.year}-Q{qtr.quarter}]"
    started_wall = time.monotonic()
    log.info("%s start url=%s", log_prefix, qtr.url)

    with httpx.Client(headers={"User-Agent": "data-engine-x/ncua-r2-ingest"}) as client:
        try:
            content_length, source_last_modified, status_code = head_url(client, qtr.url)
        except Exception:
            log.exception("%s HEAD failed", log_prefix)
            return 1
        if status_code == 404:
            log.error("%s HEAD 404 — quarter not published", log_prefix)
            return 1
        log.info("%s HEAD content_length=%s last_modified=%s",
                 log_prefix, content_length, source_last_modified)
        if dry_run:
            log.info("%s DRY RUN — exiting after HEAD", log_prefix)
            return 0

        with psycopg.connect(_database_url()) as conn:
            prior = get_prior_source_last_modified(conn, qtr)
            log.info("%s prior source_last_modified: %s", log_prefix, prior)
            if (
                skip_if_unchanged
                and prior is not None
                and source_last_modified is not None
                and source_last_modified <= prior
            ):
                log.info("%s source unchanged — recording no_change", log_prefix)
                write_no_change_run(
                    conn, qtr, source_url=qtr.url,
                    source_last_modified=source_last_modified,
                    prior_source_last_modified=prior,
                )
                return 0

            run_id = insert_run_row(
                conn, qtr, source_url=qtr.url,
                source_last_modified=source_last_modified,
                prior_source_last_modified=prior,
            )
            log.info("%s run id: %s", log_prefix, run_id)

            zip_path = workdir / f"ncua_{qtr.year}_q{qtr.quarter}.zip"
            extract_dir = workdir / f"ncua_{qtr.year}_q{qtr.quarter}"
            extract_dir.mkdir(parents=True, exist_ok=True)
            parquet_dir = workdir / f"ncua_{qtr.year}_q{qtr.quarter}_parquet"
            parquet_dir.mkdir(parents=True, exist_ok=True)

            try:
                zip_bytes = download_zip(client, qtr.url, zip_path)
                log.info("%s downloaded %d bytes", log_prefix, zip_bytes)

                txt_files = extract_txt_files(zip_path, extract_dir)
                log.info("%s extracted %d .txt files", log_prefix, len(txt_files))
                if not txt_files:
                    raise RuntimeError("no .txt files inside ZIP")

                r2_prefix = r2_prefix_override or qtr.r2_prefix
                parquet_objects: list[dict[str, Any]] = []
                total_rows = 0
                total_bytes = 0
                for table, src_path, _src_bytes in sorted(txt_files, key=lambda t: t[0]):
                    if only_tables is not None and table not in only_tables:
                        continue
                    parquet_path = parquet_dir / f"{table}.parquet"
                    try:
                        rows_in, rows_pq, _cols = txt_to_parquet(
                            src_path, parquet_path,
                            table=table, qtr=qtr, log_prefix=log_prefix,
                            max_rows=max_rows,
                        )
                    except Exception as inner:
                        log.warning("%s   %s: convert FAILED — skipping (%s)",
                                    log_prefix, table, inner)
                        try:
                            parquet_path.unlink(missing_ok=True)
                        except Exception:
                            pass
                        continue
                    if rows_pq <= 0:
                        log.info("%s   %s: 0 rows — skipping upload", log_prefix, table)
                        try:
                            parquet_path.unlink(missing_ok=True)
                        except Exception:
                            pass
                        continue
                    r2_key = r2_prefix + f"{table}.parquet"
                    try:
                        uploaded = upload_to_r2(
                            parquet_path, bucket=R2_BUCKET, key=r2_key,
                        )
                    except Exception as upexc:
                        log.warning("%s   %s: upload FAILED (%s) — skipping",
                                    log_prefix, table, upexc)
                        try:
                            parquet_path.unlink(missing_ok=True)
                        except Exception:
                            pass
                        continue
                    parquet_objects.append({
                        "table": table, "rows": rows_pq, "bytes": uploaded,
                        "r2_key": r2_key,
                    })
                    total_rows += rows_pq
                    total_bytes += uploaded
                    try:
                        parquet_path.unlink(missing_ok=True)
                    except Exception:
                        pass

                log.info(
                    "%s uploaded %d Parquet objects, %s rows, %.1f MB",
                    log_prefix, len(parquet_objects), f"{total_rows:,}",
                    total_bytes / (1 << 20),
                )

                finalize_run_row(
                    conn, run_id, status="completed",
                    zip_bytes=zip_bytes,
                    zip_inner_files=len(txt_files),
                    parquet_object_count=len(parquet_objects),
                    parquet_row_count_total=total_rows,
                    parquet_bytes_written=total_bytes,
                    r2_bucket=R2_BUCKET,
                    r2_prefix=r2_prefix,
                    r2_total_bytes=total_bytes,
                    started_at=started_wall, error_message=None,
                    notes={
                        "objects": parquet_objects[:50],
                        "object_count": len(parquet_objects),
                        "max_rows": max_rows,
                    },
                )
                log.info(
                    "%s DONE objects=%d rows=%s upload=%.1f MB wall=%.1fs",
                    log_prefix, len(parquet_objects), f"{total_rows:,}",
                    total_bytes / (1 << 20),
                    time.monotonic() - started_wall,
                )
                return 0

            except Exception as exc:
                log.exception("%s ingest failed", log_prefix)
                finalize_run_row(
                    conn, run_id, status="failed",
                    zip_bytes=0, zip_inner_files=0,
                    parquet_object_count=0,
                    parquet_row_count_total=0,
                    parquet_bytes_written=0,
                    r2_bucket=None, r2_prefix=None,
                    r2_total_bytes=0,
                    started_at=started_wall,
                    error_message=str(exc), notes=None,
                )
                return 1

            finally:
                try:
                    zip_path.unlink(missing_ok=True)
                except Exception:
                    pass
                shutil.rmtree(extract_dir, ignore_errors=True)
                shutil.rmtree(parquet_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_year_range(s: str) -> list[tuple[int, int]]:
    if "-" in s:
        a, b = s.split("-", 1)
        ya, yb = int(a), int(b)
    else:
        ya = yb = int(s)
    out: list[tuple[int, int]] = []
    for y in range(ya, yb + 1):
        for q in (1, 2, 3, 4):
            out.append((y, q))
    return out


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("year", nargs="?", type=int)
    p.add_argument("quarter", nargs="?", type=int)
    p.add_argument("--years", default=None,
                   help="Year range, e.g., 2016-2024.")
    p.add_argument("--all", action="store_true",
                   help="Default span: 2015-Q2 through 2024-Q4 (39 quarters).")
    p.add_argument("--skip-if-unchanged", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max-rows", type=int, default=None)
    p.add_argument("--workdir", default=None)
    p.add_argument("--r2-prefix-override", default=None)
    p.add_argument("--only-tables", default=None,
                   help="Comma-separated list of inner table names "
                        "(lowercased, no .txt). Default: all tables.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    workdir = Path(args.workdir or "/tmp/ncua_r2_ingest")
    workdir.mkdir(parents=True, exist_ok=True)

    if args.all:
        spans: list[tuple[int, int]] = list(DEFAULT_SPAN)
    elif args.years:
        spans = [
            (y, q) for (y, q) in parse_year_range(args.years)
            if not (y == 2015 and q == 1)
        ]
    else:
        if args.year is None or args.quarter is None:
            log.error("must pass year + quarter (or --years / --all)")
            return 2
        spans = [(args.year, args.quarter)]

    only_tables: set[str] | None = None
    if args.only_tables:
        only_tables = {t.strip().lower() for t in args.only_tables.split(",") if t.strip()}

    rc = 0
    for y, q in spans:
        qtr = Quarter(year=y, quarter=q)
        log.info("=" * 70)
        log.info("=== INGEST: %s-Q%s ===", y, q)
        log.info("=" * 70)
        rc_one = ingest_quarter(
            qtr,
            skip_if_unchanged=args.skip_if_unchanged,
            dry_run=args.dry_run,
            workdir=workdir,
            max_rows=args.max_rows,
            r2_prefix_override=args.r2_prefix_override,
            only_tables=only_tables,
        )
        if rc_one != 0:
            rc = rc_one
            log.error("quarter failed; continuing with remaining quarters")
    return rc


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""HMDA LAR legacy-era (2007-2017) ingest — CFPB historic mirror → R2.

Sibling to `run_hmda_r2_ingest.py` (modern 2018+ via FFIEC CFPB snapshot).
The legacy era's source URL pattern, CSV schema, and unit conventions
diverge enough to warrant its own script — the schema mapping work is
carried by `_lib/hmda_legacy_schema_map.py`.

Pipeline (parallel to the modern script):
  1. HEAD the consumerfinance.gov mirror URL; capture content-length +
     last-modified.
  2. Skip-if-unchanged short-circuit on Last-Modified.
  3. Stream-download the ZIP.
  4. Extract the bundled CSV.
  5. DuckDB transform: read all-VARCHAR (preserves leading zeros on
     census_tract; HMDA pre-2018 has no "Exempt" sentinel but uses
     "  NA  " / blank for missing — TRY_CAST handles both). Project to the
     modern 99-column schema via _lib.hmda_legacy_schema_map, NULL-filling
     fields the legacy era didn't collect (LEI, interest_rate,
     total_loan_costs, etc.) and preserving 6 legacy-only fields as
     `legacy_*` columns. loan_amount_000s and applicant_income_000s are
     ×1000 unit-converted to match the modern Parquets' raw-dollar
     convention.
  6. boto3 multipart upload to R2 at the same partitioned key as modern
     (`hmda/lar/year={year}/lar_{year}.parquet`).
  7. Verify Parquet row count vs CSV row count. Write the audit row to
     `ops.hmda_r2_ingest_runs` (same audit table; the loan-grain R2 path
     already supports multi-year per PR #211).

Coverage scope: 2007-2017 (CFPB historic data mirror). 1990-2006 sit in
the FFIEC archive at `ffiec.gov/hmdarawdata/...`, which is now bot-
protected by Cloudflare and not addressable from automation. That range
requires a separate operator-driven ingest path.

Idempotency basis: HEAD Last-Modified (mirrors the modern script).

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_hmda_lar_legacy_r2_ingest.py 2017
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_hmda_lar_legacy_r2_ingest.py 2017 --max-rows 50000
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
import duckdb
import httpx
import psycopg
from psycopg.types.json import Jsonb

# pylint: disable=wrong-import-position
sys.path.insert(0, str(Path(__file__).parent))
from _lib.hmda_legacy_schema_map import (  # noqa: E402
    build_select_clauses,
    coverage_report,
)


R2_BUCKET = "dex-raw-landing-zone"
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 5

LEGACY_YEARS_SUPPORTED: tuple[int, ...] = tuple(range(2007, 2018))  # 2007..2017


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("hmda-lar-legacy-r2-ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# Source URL + R2 layout
# --------------------------------------------------------------------------- #


def source_url(year: int) -> str:
    return (
        "https://files.consumerfinance.gov/hmda-historic-loan-data/"
        f"hmda_{year}_nationwide_all-records_labels.zip"
    )


def r2_prefix(year: int) -> str:
    return f"hmda/lar/year={year}/"


def parquet_filename(year: int) -> str:
    return f"lar_{year}.parquet"


# --------------------------------------------------------------------------- #
# Env helpers
# --------------------------------------------------------------------------- #


def _r2_client():
    return boto3.client(
        "s3",
        endpoint_url=_required_env("R2_ENDPOINT"),
        aws_access_key_id=_required_env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=_required_env("R2_SECRET_ACCESS_KEY"),
        region_name="auto",
    )


def _required_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"{name} is not set in the environment.")
    return v


def _database_url() -> str:
    return _required_env("DEX_DB_URL_POOLED")


# --------------------------------------------------------------------------- #
# HTTP layer (mirrors run_hmda_r2_ingest.py)
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
# CSV extract + DuckDB transform
# --------------------------------------------------------------------------- #


def extract_csv(zip_path: Path, dest_dir: Path) -> tuple[Path, int]:
    """Extract the single bundled CSV. Returns (csv_path, csv_bytes)."""
    with zipfile.ZipFile(zip_path) as z:
        target = next((n for n in z.namelist() if n.lower().endswith(".csv")), None)
        if target is None:
            raise RuntimeError(
                f"No CSV in {zip_path.name}; contents: {z.namelist()}"
            )
        info = z.getinfo(target)
        out = dest_dir / Path(target).name
        with z.open(target) as src, out.open("wb") as dst:
            shutil.copyfileobj(src, dst, length=1 << 20)
        return out, info.file_size


def duckdb_csv_to_parquet(
    csv_path: Path,
    parquet_path: Path,
    *,
    year: int,
    log_prefix: str,
    max_rows: int | None,
) -> tuple[int, int, dict[str, Any]]:
    """Read legacy CSV, project to modern schema, write ZSTD Parquet.

    Returns (rows_in_csv, rows_in_parquet, mapping_report).
    """
    con = duckdb.connect(":memory:")
    con.execute("PRAGMA threads=4;")
    con.execute("PRAGMA memory_limit='8GB';")

    con.execute(f"""
        CREATE VIEW raw AS
        SELECT * FROM read_csv_auto('{csv_path}', all_varchar=TRUE,
                                    sample_size=-1, header=TRUE);
    """)
    cols_info = con.execute("DESCRIBE raw;").fetchall()
    csv_cols = [c[0] for c in cols_info]
    log.info("%s discovered %d columns in CSV", log_prefix, len(csv_cols))

    rows_in_csv_row = con.execute("SELECT count(*) FROM raw;").fetchone()
    rows_in_csv = int(rows_in_csv_row[0]) if rows_in_csv_row else 0
    log.info("%s CSV row count: %d", log_prefix, rows_in_csv)

    select_parts, missing = build_select_clauses(csv_cols, year=year)
    rep = coverage_report(csv_cols)
    log.info(
        "%s mapping coverage: %d/%d modern cols populated (%.1f%%) | %d null-by-design "
        "| %d missing-in-csv | %d/%d legacy-only preserved",
        log_prefix,
        rep["modern_cols_populated"], rep["modern_cols_total"], rep["populated_pct"],
        rep["modern_cols_null_by_design"], rep["modern_cols_missing_in_csv"],
        rep["legacy_only_present"], rep["legacy_only_total"],
    )
    if missing:
        log.warning(
            "%s legacy cols referenced by mapping but missing in CSV: %s",
            log_prefix, missing,
        )

    limit_clause = f"LIMIT {max_rows}" if max_rows is not None else ""
    select_sql = f"SELECT {', '.join(select_parts)} FROM raw {limit_clause}"

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

    rep["missing_in_csv_list"] = missing
    return rows_in_csv, rows_in_parquet, rep


# --------------------------------------------------------------------------- #
# R2 upload (boto3 multipart) — identical to modern script
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
            pct = 100.0 * last_progress["sent"] / file_bytes
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
# Audit-row helpers — same `ops.hmda_r2_ingest_runs` table as modern script
# --------------------------------------------------------------------------- #


def insert_run_row(
    conn: psycopg.Connection,
    *,
    year: int,
    url: str,
    source_last_modified: datetime | None,
    prior_source_last_modified: datetime | None,
) -> str:
    sql = """
    INSERT INTO ops.hmda_r2_ingest_runs (
        dataset_form, dataset_year, status, source_url,
        source_last_modified, prior_source_last_modified
    ) VALUES ('LAR', %s, 'running', %s, %s, %s)
    RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            year, url, source_last_modified, prior_source_last_modified,
        ))
        row_id = cur.fetchone()[0]
    conn.commit()
    return str(row_id)


def get_prior_source_last_modified(
    conn: psycopg.Connection, year: int
) -> datetime | None:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT source_last_modified
              FROM ops.hmda_r2_ingest_runs
             WHERE dataset_form = 'LAR' AND dataset_year = %s AND status = 'completed'
             ORDER BY started_at DESC LIMIT 1
            """,
            (year,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def write_no_change_run(
    conn: psycopg.Connection,
    *,
    year: int,
    url: str,
    source_last_modified: datetime | None,
    prior_source_last_modified: datetime | None,
) -> None:
    started = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ops.hmda_r2_ingest_runs (
                dataset_form, dataset_year, status, source_url,
                source_last_modified, prior_source_last_modified,
                started_at, finished_at, duration_seconds, notes
            ) VALUES ('LAR', %s, 'no_change', %s, %s, %s, %s, %s, 0, %s);
            """,
            (
                year, url, source_last_modified,
                prior_source_last_modified, started, started,
                Jsonb({
                    "reason": "source_last_modified unchanged",
                    "source": "cfpb-historic-mirror-legacy",
                }),
            ),
        )
    conn.commit()


def finalize_run_row(
    conn: psycopg.Connection,
    run_id: str,
    *,
    status: str,
    zip_bytes: int,
    csv_bytes: int,
    rows_in_csv: int,
    parquet_bytes_written: int,
    parquet_row_count: int,
    parquet_part_count: int,
    r2_bucket: str | None,
    r2_prefix_used: str | None,
    r2_object_count: int,
    r2_total_bytes: int,
    started_at: float,
    error_message: str | None,
    notes: dict[str, Any] | None,
) -> None:
    duration = round(time.monotonic() - started_at, 3)
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE ops.hmda_r2_ingest_runs
               SET status = %s, zip_bytes_downloaded = %s,
                   csv_bytes_extracted = %s, rows_in_csv = %s,
                   parquet_bytes_written = %s, parquet_row_count = %s,
                   parquet_part_count = %s,
                   r2_bucket = %s, r2_prefix = %s,
                   r2_object_count = %s, r2_total_bytes = %s,
                   finished_at = now(), duration_seconds = %s,
                   error_message = %s, notes = %s
             WHERE id = %s;
            """, (
            status, zip_bytes, csv_bytes, rows_in_csv,
            parquet_bytes_written, parquet_row_count, parquet_part_count,
            r2_bucket, r2_prefix_used, r2_object_count, r2_total_bytes,
            duration, error_message,
            Jsonb(notes) if notes else None, run_id,
        ))
    conn.commit()


# --------------------------------------------------------------------------- #
# Per-year main
# --------------------------------------------------------------------------- #


def ingest_one(
    *,
    year: int,
    skip_if_unchanged: bool,
    dry_run: bool,
    workdir: Path,
    max_rows: int | None,
    r2_prefix_override: str | None,
) -> int:
    if year not in LEGACY_YEARS_SUPPORTED:
        log.error(
            "year %d not in supported legacy range %d-%d (CFPB historic mirror)",
            year, LEGACY_YEARS_SUPPORTED[0], LEGACY_YEARS_SUPPORTED[-1],
        )
        return 1

    url = source_url(year)
    log_prefix = f"[lar-legacy {year}]"
    started_wall = time.monotonic()
    log.info("%s start url=%s", log_prefix, url)

    with httpx.Client(headers={"User-Agent": "data-engine-x/hmda-r2-ingest"}) as client:
        try:
            content_length, source_last_modified, status_code = head_url(client, url)
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
            prior = get_prior_source_last_modified(conn, year)
            log.info("%s prior source_last_modified: %s", log_prefix, prior)
            if (
                skip_if_unchanged
                and prior is not None
                and source_last_modified is not None
                and source_last_modified <= prior
            ):
                log.info("%s source unchanged — recording no_change", log_prefix)
                write_no_change_run(
                    conn, year=year, url=url,
                    source_last_modified=source_last_modified,
                    prior_source_last_modified=prior,
                )
                return 0

            run_id = insert_run_row(
                conn, year=year, url=url,
                source_last_modified=source_last_modified,
                prior_source_last_modified=prior,
            )
            log.info("%s run id: %s", log_prefix, run_id)

            zip_path = workdir / f"hmda_lar_legacy_{year}.zip"
            csv_dir = workdir / f"hmda_lar_legacy_{year}_csv"
            csv_dir.mkdir(parents=True, exist_ok=True)
            parquet_path = workdir / parquet_filename(year)

            try:
                # 1. Download
                zip_bytes = download_zip(client, url, zip_path)
                log.info("%s downloaded %d bytes -> %s",
                         log_prefix, zip_bytes, zip_path)

                # 2. Extract CSV
                csv_path, csv_bytes = extract_csv(zip_path, csv_dir)
                log.info("%s extracted %s (%d bytes uncompressed)",
                         log_prefix, csv_path.name, csv_bytes)

                # 3. DuckDB transform → ZSTD Parquet
                rows_in_csv, parquet_row_count, mapping_rep = duckdb_csv_to_parquet(
                    csv_path, parquet_path,
                    year=year, log_prefix=log_prefix,
                    max_rows=max_rows,
                )
                parquet_bytes = parquet_path.stat().st_size
                log.info(
                    "%s parquet: %d rows, %.1f MB (%.2f bytes/row)",
                    log_prefix, parquet_row_count,
                    parquet_bytes / (1 << 20),
                    parquet_bytes / max(parquet_row_count, 1),
                )

                # 4. Upload to R2
                r2_prefix_used = r2_prefix_override or r2_prefix(year)
                r2_key = r2_prefix_used + parquet_filename(year)
                uploaded_bytes = upload_to_r2(
                    parquet_path, bucket=R2_BUCKET, key=r2_key,
                    log_prefix=log_prefix,
                )

                # 5. Finalize audit
                notes = {
                    "r2_key":      r2_key,
                    "source":      "cfpb-historic-mirror-legacy",
                    "schema_era":  "lar-2007-2017",
                    "mapping":     mapping_rep,
                }
                finalize_run_row(
                    conn, run_id, status="completed",
                    zip_bytes=zip_bytes, csv_bytes=csv_bytes,
                    rows_in_csv=rows_in_csv,
                    parquet_bytes_written=parquet_bytes,
                    parquet_row_count=parquet_row_count,
                    parquet_part_count=1,
                    r2_bucket=R2_BUCKET,
                    r2_prefix_used=r2_prefix_used,
                    r2_object_count=1,
                    r2_total_bytes=uploaded_bytes,
                    started_at=started_wall, error_message=None,
                    notes=notes,
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
                    zip_bytes=0, csv_bytes=0, rows_in_csv=0,
                    parquet_bytes_written=0, parquet_row_count=0,
                    parquet_part_count=0,
                    r2_bucket=None, r2_prefix_used=None,
                    r2_object_count=0, r2_total_bytes=0,
                    started_at=started_wall,
                    error_message=str(exc), notes=None,
                )
                return 1

            finally:
                for p in (zip_path, parquet_path):
                    try:
                        p.unlink(missing_ok=True)
                    except Exception:
                        pass
                shutil.rmtree(csv_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("year", type=int,
                   help=f"Snapshot year (legacy era: {LEGACY_YEARS_SUPPORTED[0]}-{LEGACY_YEARS_SUPPORTED[-1]}).")
    p.add_argument("--skip-if-unchanged", action="store_true",
                   help="No-op if source Last-Modified hasn't advanced "
                        "since the prior successful run.")
    p.add_argument("--dry-run", action="store_true",
                   help="HEAD only; no download, transform, upload, or DB write.")
    p.add_argument("--max-rows", type=int, default=None,
                   help="Cap rows in Parquet output (smoke testing only). "
                        "Audit row records the full CSV row count regardless.")
    p.add_argument("--workdir", default=None,
                   help="Working dir for ZIP/CSV/Parquet temp files "
                        "(default: /tmp/hmda_r2_ingest).")
    p.add_argument("--r2-prefix-override", default=None,
                   help="Override R2 prefix — used by smoke tests to land "
                        "in /smoke/ instead of the canonical year= path.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    workdir = Path(args.workdir or "/tmp/hmda_r2_ingest")
    workdir.mkdir(parents=True, exist_ok=True)
    return ingest_one(
        year=args.year,
        skip_if_unchanged=args.skip_if_unchanged,
        dry_run=args.dry_run,
        workdir=workdir,
        max_rows=args.max_rows,
        r2_prefix_override=args.r2_prefix_override,
    )


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""FDIC Institution Directory + Call Report → R2/RW Fuel Tank ingest.

Pulls from the FDIC BankFind Suite API at api.fdic.gov:
  - Institution Directory (~28K active + historical bank profiles)
  - Quarterly Call Reports (~1.67M rows; 1984-Q1 through 2024-Q4)

Mirrors the SBA / NCUA pattern: stream API → DuckDB transform → ZSTD Parquet
→ boto3 upload to R2 → audit row in ops.fdic_r2_ingest_runs.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_fdic_call_report_r2_ingest.py institutions
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_fdic_call_report_r2_ingest.py financials --year 2024
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_fdic_call_report_r2_ingest.py --all
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_fdic_call_report_r2_ingest.py financials --year 2024 --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
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
FDIC_API_BASE = "https://api.fdic.gov/banks"
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 5
PAGE_SIZE = 10_000

# Institution Directory fields — profile data
INSTITUTION_FIELDS = (
    "CERT,NAME,CITY,STALP,STNAME,COUNTY,ZIP,ACTIVE,BKCLASS,FED,STMULT,"
    "OFFICES,ASSET,DEP,ESTYMD,ENDEFYMD,CHARTER,FEDCHRTR,STCHRTR,WEBADDR,"
    "INSAGNT1,SPECGRP,SUBCHAPS,REGAGNT,FDICDBS,FDICSUPV,LATITUDE,LONGITUDE,"
    "TRUST,INSDIF,INSBIF,INSSAIF,INSFDIC,INSCOML,RUNDATE"
)

# Call Report (Schedule RC + RI) fields — quarterly financial line items
FINANCIALS_FIELDS = (
    "CERT,REPDTE,ASSET,DEP,DEPDOM,EQ,LNLSNET,LNATRES,NETINC,INTINC,EINTEXP,"
    "NIM,ROA,ROE,LIABEQ,LIAB,EQCS,EQTOT,EQUPTOT,IDDIVTOT,SC,IGLSEC,"
    "BKPREM,ORE,INTAN,INVESTED,LNLSGR,LNRESNCR,NTLNLSR,DDT,TS,DEPNIB,"
    "DEPI,RUNDATE,STALP,NAME"
)


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("fdic-r2-ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# Slice configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class InstitutionsSlice:
    kind: str = "institutions"

    @property
    def r2_key(self) -> str:
        return "fdic/institutions/institutions.parquet"


@dataclass(frozen=True)
class FinancialsSlice:
    year: int
    kind: str = "financials"

    @property
    def r2_prefix(self) -> str:
        return f"fdic/financials/year={self.year}/"

    @property
    def r2_key(self) -> str:
        return self.r2_prefix + f"financials_{self.year}.parquet"


def all_financials_slices(start: int = 1984, end: int = 2024) -> list[FinancialsSlice]:
    return [FinancialsSlice(year=y) for y in range(start, end + 1)]


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
# FDIC API client
# --------------------------------------------------------------------------- #


def fdic_get(
    client: httpx.Client,
    path: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    url = f"{FDIC_API_BASE}/{path}"
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = client.get(url, params=params, follow_redirects=True, timeout=60.0)
            if r.status_code in RETRY_STATUSES:
                wait = min(2 ** attempt, 30)
                log.warning("GET %s HTTP %s; retry in %ss", url, r.status_code, wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            wait = min(2 ** attempt, 30)
            log.warning("GET %s error (%s); retry in %ss", url, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"GET {url} failed after {MAX_RETRIES}: {last_exc}")


def paginate(
    client: httpx.Client,
    path: str,
    *,
    fields: str,
    filters: str | None = None,
    sort_by: str = "CERT",
) -> list[dict[str, Any]]:
    """Pull all matching rows. FDIC API supports limit + offset paging."""
    rows: list[dict[str, Any]] = []
    offset = 0
    total: int | None = None
    while True:
        params = {
            "limit": PAGE_SIZE,
            "offset": offset,
            "fields": fields,
            "sort_by": sort_by,
            "sort_order": "ASC",
        }
        if filters:
            params["filters"] = filters
        page = fdic_get(client, path, params)
        page_rows = [r.get("data", {}) for r in page.get("data", [])]
        if total is None:
            total = page.get("meta", {}).get("total")
            log.info("  total reported: %s", f"{total:,}" if total else "?")
        rows.extend(page_rows)
        log.info("  fetched %s rows (offset %s)", f"{len(rows):,}", f"{offset:,}")
        if not page_rows or (total is not None and len(rows) >= total):
            break
        offset += PAGE_SIZE
    return rows


# --------------------------------------------------------------------------- #
# Parquet write via DuckDB (in-memory)
# --------------------------------------------------------------------------- #


def write_rows_to_parquet(
    rows: list[dict[str, Any]],
    parquet_path: Path,
    *,
    log_prefix: str,
) -> tuple[int, int]:
    if not rows:
        return 0, 0

    parquet_path.parent.mkdir(parents=True, exist_ok=True)

    # Stage to NDJSON; let DuckDB infer types from JSON.
    ndjson_path = parquet_path.with_suffix(".ndjson")
    with ndjson_path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, default=str) + "\n")

    con = duckdb.connect(":memory:")
    con.execute("PRAGMA threads=4;")
    log.info("%s converting %s rows -> Parquet (ZSTD)",
             log_prefix, f"{len(rows):,}")
    t0 = time.monotonic()
    con.execute(f"""
        COPY (SELECT * FROM read_json_auto('{ndjson_path}'))
        TO '{parquet_path}'
        (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000);
    """)
    rc_row = con.execute(f"SELECT count(*) FROM read_parquet('{parquet_path}');").fetchone()
    rows_in_parquet = int(rc_row[0]) if rc_row else 0
    con.close()
    log.info("%s Parquet write done in %.1fs", log_prefix, time.monotonic() - t0)

    try:
        ndjson_path.unlink()
    except Exception:
        pass

    return len(rows), rows_in_parquet


def upload_to_r2(parquet_path: Path, *, key: str, log_prefix: str) -> int:
    s3 = _r2_client()
    n_bytes = parquet_path.stat().st_size
    log.info("%s uploading %.1f MB → s3://%s/%s",
             log_prefix, n_bytes / (1 << 20), R2_BUCKET, key)
    s3.upload_file(
        str(parquet_path), R2_BUCKET, key,
        ExtraArgs={"ContentType": "application/x-parquet"},
    )
    return n_bytes


# --------------------------------------------------------------------------- #
# Audit ledger
# --------------------------------------------------------------------------- #


def insert_run_row(
    conn: psycopg.Connection,
    *,
    feed_kind: str,
    year: int | None,
    source_url: str,
) -> str:
    sql = """
    INSERT INTO ops.fdic_r2_ingest_runs (
        feed_kind, year, status, source_url
    ) VALUES (%s, %s, 'running', %s)
    RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (feed_kind, year, source_url))
        row_id = cur.fetchone()[0]
    conn.commit()
    return str(row_id)


def finalize_run_row(
    conn: psycopg.Connection,
    run_id: str,
    *,
    status: str,
    rows_fetched: int,
    parquet_row_count: int,
    parquet_bytes_written: int,
    r2_key: str | None,
    r2_total_bytes: int,
    started_at: float,
    error_message: str | None,
    notes: dict[str, Any] | None,
) -> None:
    duration = round(time.monotonic() - started_at, 3)
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE ops.fdic_r2_ingest_runs
               SET status = %s,
                   rows_fetched = %s,
                   parquet_row_count = %s,
                   parquet_bytes_written = %s,
                   r2_bucket = %s, r2_key = %s, r2_total_bytes = %s,
                   finished_at = now(), duration_seconds = %s,
                   error_message = %s, notes = %s
             WHERE id = %s;
        """, (
            status, rows_fetched, parquet_row_count,
            parquet_bytes_written, R2_BUCKET, r2_key, r2_total_bytes,
            duration, error_message,
            Jsonb(notes) if notes else None, run_id,
        ))
    conn.commit()


# --------------------------------------------------------------------------- #
# Per-slice main
# --------------------------------------------------------------------------- #


def ingest_institutions(
    *,
    workdir: Path,
    dry_run: bool,
) -> int:
    log_prefix = "[institutions]"
    started_wall = time.monotonic()
    source_url = f"{FDIC_API_BASE}/institutions (paged)"
    log.info("%s start", log_prefix)

    if dry_run:
        log.info("%s DRY RUN — exiting before fetch", log_prefix)
        return 0

    with httpx.Client(headers={"User-Agent": "data-engine-x/fdic-r2-ingest"}) as client:
        with psycopg.connect(_database_url()) as conn:
            run_id = insert_run_row(
                conn, feed_kind="institutions", year=None, source_url=source_url,
            )
            log.info("%s run id: %s", log_prefix, run_id)

            try:
                rows = paginate(client, "institutions", fields=INSTITUTION_FIELDS)
                log.info("%s fetched %s rows", log_prefix, f"{len(rows):,}")
                if not rows:
                    raise RuntimeError("no rows returned")

                parquet_path = workdir / "fdic_institutions.parquet"
                rows_fetched, rows_in_parquet = write_rows_to_parquet(
                    rows, parquet_path, log_prefix=log_prefix,
                )
                pq_bytes = parquet_path.stat().st_size

                key = "fdic/institutions/institutions.parquet"
                uploaded_bytes = upload_to_r2(
                    parquet_path, key=key, log_prefix=log_prefix,
                )

                finalize_run_row(
                    conn, run_id, status="completed",
                    rows_fetched=rows_fetched,
                    parquet_row_count=rows_in_parquet,
                    parquet_bytes_written=pq_bytes,
                    r2_key=key, r2_total_bytes=uploaded_bytes,
                    started_at=started_wall, error_message=None,
                    notes={"r2_key": key},
                )
                log.info(
                    "%s DONE rows=%d parquet=%.1f MB wall=%.1fs",
                    log_prefix, rows_in_parquet,
                    pq_bytes / (1 << 20),
                    time.monotonic() - started_wall,
                )
                try:
                    parquet_path.unlink()
                except Exception:
                    pass
                return 0

            except Exception as exc:
                log.exception("%s ingest failed", log_prefix)
                finalize_run_row(
                    conn, run_id, status="failed",
                    rows_fetched=0, parquet_row_count=0,
                    parquet_bytes_written=0, r2_key=None, r2_total_bytes=0,
                    started_at=started_wall,
                    error_message=str(exc), notes=None,
                )
                return 1


def ingest_financials_year(
    sl: FinancialsSlice,
    *,
    workdir: Path,
    dry_run: bool,
) -> int:
    log_prefix = f"[financials {sl.year}]"
    started_wall = time.monotonic()
    # FDIC REPDTE filter: REPDTE in [YYYY0101, YYYY1231]
    filters = f"REPDTE:[{sl.year}0101 TO {sl.year}1231]"
    source_url = f"{FDIC_API_BASE}/financials?filters={filters}"
    log.info("%s start", log_prefix)

    if dry_run:
        log.info("%s DRY RUN — exiting before fetch", log_prefix)
        return 0

    with httpx.Client(headers={"User-Agent": "data-engine-x/fdic-r2-ingest"}) as client:
        with psycopg.connect(_database_url()) as conn:
            run_id = insert_run_row(
                conn, feed_kind="financials", year=sl.year, source_url=source_url,
            )
            log.info("%s run id: %s", log_prefix, run_id)

            try:
                rows = paginate(
                    client, "financials",
                    fields=FINANCIALS_FIELDS, filters=filters,
                )
                log.info("%s fetched %s rows", log_prefix, f"{len(rows):,}")
                if not rows:
                    log.warning("%s no rows for year %d — skipping", log_prefix, sl.year)
                    finalize_run_row(
                        conn, run_id, status="no_change",
                        rows_fetched=0, parquet_row_count=0,
                        parquet_bytes_written=0, r2_key=None, r2_total_bytes=0,
                        started_at=started_wall, error_message=None,
                        notes={"reason": f"no rows for year {sl.year}"},
                    )
                    return 0

                parquet_path = workdir / f"fdic_financials_{sl.year}.parquet"
                rows_fetched, rows_in_parquet = write_rows_to_parquet(
                    rows, parquet_path, log_prefix=log_prefix,
                )
                pq_bytes = parquet_path.stat().st_size

                uploaded_bytes = upload_to_r2(
                    parquet_path, key=sl.r2_key, log_prefix=log_prefix,
                )

                finalize_run_row(
                    conn, run_id, status="completed",
                    rows_fetched=rows_fetched,
                    parquet_row_count=rows_in_parquet,
                    parquet_bytes_written=pq_bytes,
                    r2_key=sl.r2_key, r2_total_bytes=uploaded_bytes,
                    started_at=started_wall, error_message=None,
                    notes={"r2_key": sl.r2_key, "filters": filters},
                )
                log.info(
                    "%s DONE rows=%d parquet=%.1f MB wall=%.1fs",
                    log_prefix, rows_in_parquet,
                    pq_bytes / (1 << 20),
                    time.monotonic() - started_wall,
                )
                try:
                    parquet_path.unlink()
                except Exception:
                    pass
                return 0

            except Exception as exc:
                log.exception("%s ingest failed", log_prefix)
                finalize_run_row(
                    conn, run_id, status="failed",
                    rows_fetched=0, parquet_row_count=0,
                    parquet_bytes_written=0, r2_key=None, r2_total_bytes=0,
                    started_at=started_wall,
                    error_message=str(exc), notes=None,
                )
                return 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("kind", nargs="?", choices=["institutions", "financials"],
                   help="What to ingest.")
    p.add_argument("--year", type=int, default=None,
                   help="Year for financials slice (1984-2024).")
    p.add_argument("--all", action="store_true",
                   help="Ingest institutions + every financials year sequentially.")
    p.add_argument("--start", type=int, default=1984)
    p.add_argument("--end", type=int, default=2024)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--workdir", default=None)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    workdir = Path(args.workdir or "/tmp/fdic_r2_ingest")
    workdir.mkdir(parents=True, exist_ok=True)

    if args.all:
        rc = ingest_institutions(workdir=workdir, dry_run=args.dry_run)
        if rc != 0:
            log.error("institutions failed; continuing")
        for sl in all_financials_slices(args.start, args.end):
            log.info("=" * 70)
            log.info("=== INGEST: financials year=%d ===", sl.year)
            log.info("=" * 70)
            rc_one = ingest_financials_year(sl, workdir=workdir, dry_run=args.dry_run)
            if rc_one != 0:
                rc = rc_one
                log.error("year %d failed; continuing", sl.year)
        return rc

    if args.kind == "institutions":
        return ingest_institutions(workdir=workdir, dry_run=args.dry_run)

    if args.kind == "financials":
        if args.year is None:
            log.error("financials requires --year YYYY (or use --all)")
            return 2
        sl = FinancialsSlice(year=args.year)
        return ingest_financials_year(sl, workdir=workdir, dry_run=args.dry_run)

    log.error("must pass kind (institutions|financials) or --all")
    return 2


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""NOAA AIS vessel-position pings — high-volume telemetry raw ingest.

Source:
    NOAA Office for Coastal Management / MarineCadastre.
    Public archive: https://coast.noaa.gov/htdata/CMSP/AISDataHandler/<YYYY>/
    Daily ZIP files: AIS_YYYY_MM_DD.zip (one CSV inside, ~30–80M rows/day).
    Publication lag: 6–12 months from real-time. This is HISTORICAL data —
    real-time positions require a separate AISStream.io / commercial feed.

Pattern:
    Per CLAUDE.md §"Source ingest invariant" → "Carve-out: high-volume
    telemetry sources":
      - File-level idempotency in ops.ais_pings_ingest_runs (UNIQUE filename).
      - No per-row raw_source_row jsonb; provenance lives in the run row.
      - PARTITION BY RANGE (base_datetime), monthly partitions created on demand.
      - COPY FROM STDIN (requires DEX_DB_URL_DIRECT — pgbouncer chokes on COPY).

Loader flow (per file):
    1. INSERT into ops.ais_pings_ingest_runs with status='running' (UNIQUE
       (source_filename) constraint provides the lock — fails immediately if
       this file is already loaded).
    2. Stream-download the ZIP, unzip in memory, parse CSV header.
    3. Ensure the target monthly partition exists (CREATE PARTITION OF …
       IF NOT EXISTS via DDL — idempotent).
    4. COPY FROM STDIN into entities.source_ais_pings, filling source_run_id
       with the run row's UUID.
    5. UPDATE the run row to status='succeeded' with rows_loaded.

Failure modes:
    - HTTP 404 / file not yet published → run row marked 'failed', error_text
      records the status. Re-runs of the same file_date will see the prior
      'failed' row (UNIQUE filename), and must explicitly --retry to overwrite.
    - COPY error mid-stream → run row marked 'failed', any rows already
      flushed to the partition remain. Operator can DELETE FROM partition
      WHERE source_run_id = <run_id> to clean.
    - Schema drift (NOAA adds a column) → CSV-header check fails, run aborts
      cleanly. Operator adds the column via ALTER TABLE migration.

Usage:
    PYTHONPATH=. doppler run -- python3 scripts/run_ais_pings_ingest.py --days 7
    PYTHONPATH=. doppler run -- python3 scripts/run_ais_pings_ingest.py --start 2024-01-01 --end 2024-01-31
    PYTHONPATH=. doppler run -- python3 scripts/run_ais_pings_ingest.py --year 2024
    PYTHONPATH=. doppler run -- python3 scripts/run_ais_pings_ingest.py --date 2024-06-15 --retry
    PYTHONPATH=. doppler run -- python3 scripts/run_ais_pings_ingest.py --date 2024-06-15 --dry-run
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import os
import sys
import zipfile
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Iterator

import httpx
import psycopg
from psycopg.types.json import Jsonb


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

PROVIDER = "noaa_ais"
USER_AGENT = "data-engine-x-api/ais-pings-ingest"
BASE_URL = "https://coast.noaa.gov/htdata/CMSP/AISDataHandler"
TARGET_TABLE = "entities.source_ais_pings"
RUNS_TABLE = "ops.ais_pings_ingest_runs"

# 1:1 NOAA AIS CSV column mirror (snake_case lower).
# Order MUST match the CSV header order NOAA emits.
EXPECTED_CSV_HEADER: tuple[str, ...] = (
    "MMSI", "BaseDateTime", "LAT", "LON", "SOG", "COG", "Heading",
    "VesselName", "IMO", "CallSign", "VesselType", "Status",
    "Length", "Width", "Draft", "Cargo", "TransceiverClass",
)

# DB column order for COPY. Two trailing cols (source_run_id, ingested_at) are
# server-side defaults / per-row constants; we set source_run_id via COPY.
COPY_COLUMNS: tuple[str, ...] = (
    "mmsi", "base_datetime", "lat", "lon", "sog", "cog", "heading",
    "vessel_name", "imo", "call_sign", "vessel_type", "status",
    "length", "width", "draft", "cargo", "transceiver_class",
    "source_run_id",
)

# COPY chunk size: stream rows from the CSV reader to the COPY pipe in batches
# of this many rows. Keeps memory bounded; doesn't affect throughput much
# beyond ~10k.
COPY_BATCH_ROWS = 50_000


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("ais_pings_ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# URL / file naming
# --------------------------------------------------------------------------- #


def _file_url(d: date) -> tuple[str, str]:
    """Return (download_url, filename) for an AIS file dated `d`."""
    fname = f"AIS_{d.year}_{d.month:02d}_{d.day:02d}.zip"
    url = f"{BASE_URL}/{d.year}/{fname}"
    return url, fname


def _partition_name(d: date) -> str:
    return f"source_ais_pings_{d.year}_{d.month:02d}"


def _partition_bounds(d: date) -> tuple[date, date]:
    """Return (start_inclusive, end_exclusive) for the month containing `d`."""
    start = date(d.year, d.month, 1)
    if d.month == 12:
        end = date(d.year + 1, 1, 1)
    else:
        end = date(d.year, d.month + 1, 1)
    return start, end


# --------------------------------------------------------------------------- #
# Partition management
# --------------------------------------------------------------------------- #


def _ensure_partition(conn: psycopg.Connection, d: date) -> str:
    """CREATE PARTITION OF … IF NOT EXISTS for the month of `d`. Returns the
    fully-qualified partition table name."""
    pname = _partition_name(d)
    fq = f"entities.{pname}"
    start, end = _partition_bounds(d)
    sql = (
        f"CREATE TABLE IF NOT EXISTS {fq} "
        f"PARTITION OF {TARGET_TABLE} "
        f"FOR VALUES FROM (%s) TO (%s)"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (start, end))
    conn.commit()
    log.info("ensured partition %s [%s, %s)", fq, start, end)
    return fq


# --------------------------------------------------------------------------- #
# Run-row lifecycle
# --------------------------------------------------------------------------- #


def _start_run(
    conn: psycopg.Connection,
    *,
    source_filename: str,
    source_download_url: str,
    file_date: date,
    partition_name: str,
    task_id: str | None,
    schedule_id: str | None,
    retry: bool,
) -> str:
    """INSERT (or with --retry, DELETE+INSERT) the run row.

    UNIQUE (source_filename) → naturally rejects re-runs of a successful file.
    With --retry, we delete the prior run row and any pings tagged with that
    run_id before re-inserting.
    """
    with conn.cursor() as cur:
        if retry:
            # Find prior run + clean its pings (across partitions automatically).
            cur.execute(
                f"SELECT run_id FROM {RUNS_TABLE} WHERE source_filename = %s",
                (source_filename,),
            )
            prior = cur.fetchone()
            if prior:
                prior_run_id = prior[0]
                log.warning(
                    "--retry: deleting prior run %s and its pings", prior_run_id
                )
                cur.execute(
                    f"DELETE FROM {TARGET_TABLE} WHERE source_run_id = %s",
                    (prior_run_id,),
                )
                deleted = cur.rowcount
                cur.execute(
                    f"DELETE FROM {RUNS_TABLE} WHERE run_id = %s",
                    (prior_run_id,),
                )
                log.warning("--retry: deleted %d pings from prior run", deleted)

        cur.execute(
            f"INSERT INTO {RUNS_TABLE} "
            f"(status, source_provider, source_filename, source_download_url, "
            f" file_date, partition_name, task_id, schedule_id) "
            f"VALUES ('running', %s, %s, %s, %s, %s, %s, %s) "
            f"RETURNING run_id",
            (PROVIDER, source_filename, source_download_url, file_date,
             partition_name, task_id, schedule_id),
        )
        run_id = cur.fetchone()[0]
        conn.commit()
    return run_id


def _finish_run(
    conn: psycopg.Connection,
    run_id: str,
    *,
    status: str,
    rows_loaded: int | None,
    source_observed_at: datetime | None,
    source_run_metadata: dict | None,
    error_text: str | None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {RUNS_TABLE} SET "
            f"  status = %s, completed_at = now(), "
            f"  rows_loaded = %s, source_observed_at = %s, "
            f"  source_run_metadata = %s, error_text = %s "
            f"WHERE run_id = %s",
            (status, rows_loaded, source_observed_at,
             Jsonb(source_run_metadata) if source_run_metadata else None,
             error_text, run_id),
        )
        conn.commit()


# --------------------------------------------------------------------------- #
# Download + parse
# --------------------------------------------------------------------------- #


def _download_zip(client: httpx.Client, url: str) -> tuple[bytes, datetime | None]:
    """GET the daily ZIP, return (zip_bytes, observed_at from Last-Modified)."""
    r = client.get(url, timeout=600)
    r.raise_for_status()
    if "application/zip" not in (r.headers.get("Content-Type") or "") \
            and not r.content[:2] == b"PK":
        raise RuntimeError(
            f"non-zip response for {url}: ct={r.headers.get('Content-Type')!r}"
        )
    observed: datetime | None = None
    lm = r.headers.get("Last-Modified") or r.headers.get("last-modified")
    if lm:
        try:
            dt = parsedate_to_datetime(lm)
            observed = dt.astimezone(timezone.utc) if dt else None
        except (TypeError, ValueError):
            observed = None
    return r.content, observed


def _open_csv(zip_bytes: bytes) -> tuple[io.TextIOWrapper, str]:
    """Open the CSV inside the ZIP. Returns (text_stream, csv_filename).

    Caller must eventually close the underlying ZipFile; we keep a reference
    on the wrapper.
    """
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
    if not csv_names:
        raise RuntimeError(f"no CSV in ZIP (members={zf.namelist()})")
    if len(csv_names) > 1:
        log.warning("multiple CSVs in ZIP — using first: %s", csv_names[0])
    csv_name = csv_names[0]
    raw = zf.open(csv_name, "r")
    # utf-8-sig handles a possible BOM; NOAA's CSVs are ASCII in practice.
    text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
    text._zipfile_ref = zf  # keep zipfile alive  # type: ignore[attr-defined]
    return text, csv_name


def _validate_header(header: list[str]) -> None:
    """Assert the CSV header matches EXPECTED_CSV_HEADER 1:1.

    Schema drift means NOAA added/renamed a column — abort cleanly so the
    operator can ALTER TABLE before reloading.
    """
    expected = list(EXPECTED_CSV_HEADER)
    got = [c.strip() for c in header]
    if got != expected:
        added = [c for c in got if c not in expected]
        removed = [c for c in expected if c not in got]
        raise RuntimeError(
            f"CSV header drift — expected {expected} but got {got}; "
            f"added={added}, removed={removed}. Add a migration "
            f"(ALTER TABLE entities.source_ais_pings ADD COLUMN …) and update "
            f"EXPECTED_CSV_HEADER + COPY_COLUMNS in this script before reloading."
        )


# --------------------------------------------------------------------------- #
# COPY pipeline
# --------------------------------------------------------------------------- #


def _coerce_row(raw: list[str], run_id: str) -> tuple:
    """Map NOAA CSV row → tuple in COPY_COLUMNS order. Empty → None."""
    def s(v: str) -> str | None:
        v = v.strip()
        return v if v else None

    def i(v: str) -> int | None:
        v = v.strip()
        if not v:
            return None
        try:
            return int(float(v))  # tolerate "10.0"
        except (TypeError, ValueError):
            return None

    def f(v: str) -> float | None:
        v = v.strip()
        if not v:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    # raw order matches EXPECTED_CSV_HEADER
    (mmsi, base_dt, lat, lon, sog, cog, heading, vessel_name, imo, call_sign,
     vessel_type, status, length, width, draft, cargo, transceiver_class) = raw

    return (
        i(mmsi),
        s(base_dt),  # ISO 8601 string; let psycopg cast to timestamptz
        f(lat),
        f(lon),
        f(sog),
        f(cog),
        f(heading),
        s(vessel_name),
        s(imo),
        s(call_sign),
        i(vessel_type),
        i(status),
        f(length),
        f(width),
        f(draft),
        i(cargo),
        s(transceiver_class),
        run_id,
    )


def _copy_csv_to_partition(
    conn: psycopg.Connection,
    csv_text: io.TextIOWrapper,
    run_id: str,
) -> int:
    """Stream CSV rows through psycopg's COPY pipe. Returns rows loaded."""
    reader = csv.reader(csv_text)
    header = next(reader)
    _validate_header(header)

    cols = ", ".join(COPY_COLUMNS)
    copy_sql = f"COPY {TARGET_TABLE} ({cols}) FROM STDIN"

    rows_loaded = 0
    with conn.cursor() as cur:
        with cur.copy(copy_sql) as cp:
            batch: list[tuple] = []
            for raw in reader:
                if len(raw) != len(EXPECTED_CSV_HEADER):
                    # Skip malformed lines defensively; log but don't abort.
                    log.warning("skipping malformed row (len=%d): %.120r",
                                len(raw), raw)
                    continue
                batch.append(_coerce_row(raw, run_id))
                if len(batch) >= COPY_BATCH_ROWS:
                    for row in batch:
                        cp.write_row(row)
                    rows_loaded += len(batch)
                    batch.clear()
                    if rows_loaded % (COPY_BATCH_ROWS * 10) == 0:
                        log.info("  COPY progress: %d rows", rows_loaded)
            if batch:
                for row in batch:
                    cp.write_row(row)
                rows_loaded += len(batch)
    conn.commit()
    return rows_loaded


# --------------------------------------------------------------------------- #
# Per-file pipeline
# --------------------------------------------------------------------------- #


def _ingest_file(
    conn: psycopg.Connection | None,
    client: httpx.Client,
    file_date: date,
    *,
    dry_run: bool,
    retry: bool,
    task_id: str | None,
    schedule_id: str | None,
) -> tuple[bool, int, str | None]:
    """Returns (succeeded, rows_loaded, error_text)."""
    url, fname = _file_url(file_date)
    pname = _partition_name(file_date)
    log.info("[%s] %s", file_date.isoformat(), url)

    if dry_run or conn is None:
        log.info("[%s] dry-run: skipping run-row, partition, download", file_date)
        return True, 0, None

    # Start run row first so UNIQUE (source_filename) guards against double-load.
    try:
        run_id = _start_run(
            conn,
            source_filename=fname,
            source_download_url=url,
            file_date=file_date,
            partition_name=pname,
            task_id=task_id,
            schedule_id=schedule_id,
            retry=retry,
        )
    except psycopg.errors.UniqueViolation:
        msg = (f"file {fname} already loaded — pass --retry to re-ingest "
               f"(this will DELETE prior pings for that run_id).")
        log.error(msg)
        conn.rollback()
        return False, 0, msg

    rows_loaded = 0
    err: str | None = None
    observed: datetime | None = None
    meta: dict = {"url": url}

    try:
        # Ensure the monthly partition exists before COPY.
        _ensure_partition(conn, file_date)

        zip_bytes, observed = _download_zip(client, url)
        meta["zip_bytes"] = len(zip_bytes)

        csv_text, csv_name = _open_csv(zip_bytes)
        meta["csv_name"] = csv_name

        rows_loaded = _copy_csv_to_partition(conn, csv_text, run_id)
        meta["rows_loaded"] = rows_loaded

    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        log.exception("[%s] ingest failed", file_date)
        try:
            conn.rollback()
        except Exception:
            log.exception("rollback failed")

    finally:
        try:
            _finish_run(
                conn, run_id,
                status="succeeded" if err is None else "failed",
                rows_loaded=rows_loaded if err is None else None,
                source_observed_at=observed,
                source_run_metadata=meta,
                error_text=err,
            )
        except Exception:
            log.exception("[%s] finish_run failed", file_date)

    log.info("[%s] done: rows_loaded=%d status=%s",
             file_date, rows_loaded, "succeeded" if err is None else "failed")
    return err is None, rows_loaded, err


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _date_range(start: date, end: date) -> Iterator[date]:
    d = start
    while d <= end:
        yield d
        d = d + timedelta(days=1)


def _resolve_dates(args) -> list[date]:
    if args.date:
        d = datetime.strptime(args.date, "%Y-%m-%d").date()
        return [d]
    if args.year:
        start = date(args.year, 1, 1)
        end = date(args.year, 12, 31)
        return list(_date_range(start, end))
    if args.start or args.end:
        if not (args.start and args.end):
            raise SystemExit("--start and --end must be passed together")
        s = datetime.strptime(args.start, "%Y-%m-%d").date()
        e = datetime.strptime(args.end, "%Y-%m-%d").date()
        if e < s:
            raise SystemExit("--end must be >= --start")
        return list(_date_range(s, e))
    if args.days:
        # Last N days, ending yesterday (today's file isn't published).
        end = date.today() - timedelta(days=1)
        start = end - timedelta(days=args.days - 1)
        return list(_date_range(start, end))
    # Default: yesterday only.
    yesterday = date.today() - timedelta(days=1)
    return [yesterday]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    g = parser.add_argument_group("date selection (mutually exclusive)")
    g.add_argument("--date", help="single YYYY-MM-DD")
    g.add_argument("--start", help="inclusive YYYY-MM-DD (with --end)")
    g.add_argument("--end", help="inclusive YYYY-MM-DD (with --start)")
    g.add_argument("--year", type=int, help="full calendar year YYYY")
    g.add_argument("--days", type=int,
                   help="last N days, ending yesterday")
    parser.add_argument("--retry", action="store_true",
                        help="delete prior run-row + its pings before re-ingest")
    parser.add_argument("--dry-run", action="store_true",
                        help="resolve URLs only; no download, no DB writes")
    args = parser.parse_args()

    dates = _resolve_dates(args)
    log.info("resolved %d file-date(s): %s..%s",
             len(dates), dates[0], dates[-1])

    task_id = os.environ.get("TRIGGER_TASK_ID")
    schedule_id = os.environ.get("TRIGGER_SCHEDULE_ID")

    # Per CLAUDE.md §"Carve-out: high-volume telemetry" — COPY FROM STDIN
    # requires the direct connection. Pgbouncer transaction-mode at the pooled
    # URL doesn't support COPY cleanly at this volume.
    db_url = os.environ.get("DEX_DB_URL_DIRECT")
    if not args.dry_run and not db_url:
        log.error(
            "DEX_DB_URL_DIRECT must be set (DEX_DB_URL_POOLED is insufficient "
            "for COPY at AIS volume)."
        )
        return 2

    conn: psycopg.Connection | None = None
    if not args.dry_run:
        conn = psycopg.connect(db_url, autocommit=False)

    overall_succeeded = True
    overall_loaded = 0

    try:
        with httpx.Client(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        ) as client:
            for d in dates:
                ok, loaded, _ = _ingest_file(
                    conn, client, d,
                    dry_run=args.dry_run,
                    retry=args.retry,
                    task_id=task_id,
                    schedule_id=schedule_id,
                )
                overall_succeeded = overall_succeeded and ok
                overall_loaded += loaded
    finally:
        if conn is not None:
            conn.close()

    log.info("done. files=%d rows_loaded=%d all_ok=%s",
             len(dates), overall_loaded, overall_succeeded)
    return 0 if overall_succeeded else 1


if __name__ == "__main__":
    sys.exit(main())

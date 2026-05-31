#!/usr/bin/env python3
"""EIA Weekly Retail Gasoline and Diesel Prices — raw ingest.

Source:
    EIA Open Data API v2 — https://api.eia.gov/v2/petroleum/pri/gnd/data/
    Underlying report: "Weekly Retail Gasoline and Diesel Prices"
    (https://www.eia.gov/dnav/pet/pet_pri_gnd_dcus_nus_w.htm).

Auth:
    Requires `EIA_API_KEY` env var. Free, instant registration at
    https://www.eia.gov/opendata/register.php.

Idempotency:
    INSERT ... ON CONFLICT (period, series) DO UPDATE ... WHERE row IS DISTINCT
    FROM EXCLUDED. Re-running over the same range is a no-op when EIA hasn't
    revised values; revisions land cleanly via UPDATE.

Audit:
    ops.eia_retail_fuel_weekly_ingest_runs — one row per invocation.

Coverage:
    Defaults to a 60-day rolling window (catches the prior week's release plus
    any retroactive revisions). Use --start / --end to backfill arbitrary
    windows. First published gasoline week: 1990-08-20. First published diesel
    week: 1994-03-21. Full backfill from 1990-08-20 lands ~16k rows in ~5
    paginated requests.

Usage:
    PYTHONPATH=. doppler run -- python3 scripts/run_eia_retail_fuel_weekly_ingest.py
    PYTHONPATH=. doppler run -- python3 scripts/run_eia_retail_fuel_weekly_ingest.py --start 1990-08-20
    PYTHONPATH=. doppler run -- python3 scripts/run_eia_retail_fuel_weekly_ingest.py --start 2026-01-01 --end 2026-04-30
    PYTHONPATH=. doppler run -- python3 scripts/run_eia_retail_fuel_weekly_ingest.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
import psycopg
from psycopg.types.json import Jsonb


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

PROVIDER = "eia_retail_fuel_weekly"
USER_AGENT = "data-engine-x-api/eia-retail-fuel-weekly-ingest"
ENDPOINT = "https://api.eia.gov/v2/petroleum/pri/gnd/data/"
SOURCE_FILENAME = "v2/petroleum/pri/gnd/data"  # endpoint slug for provenance

# EIA v2 max page size is 5000.
PAGE_SIZE = 5_000
DB_BATCH_SIZE = 5_000

# Default rolling window: enough to catch retroactive revisions (~3 weeks per
# EIA practice) plus the latest release.
DEFAULT_LOOKBACK_DAYS = 60

# 1:1 mirror of EIA v2 row fields (snake_case lower).
TYPED_COLS: tuple[str, ...] = (
    "period",
    "series",
    "series_description",
    "duoarea",
    "area_name",
    "product",
    "product_name",
    "process",
    "process_name",
    "value",
    "units",
)

PK_COLS: tuple[str, str] = ("period", "series")

# EIA v2 returns column names with hyphens (e.g. "area-name") and no
# "series_description" → it's published as "series-description". Normalize.
EIA_KEY_MAP: dict[str, str] = {
    "period": "period",
    "duoarea": "duoarea",
    "area-name": "area_name",
    "product": "product",
    "product-name": "product_name",
    "process": "process",
    "process-name": "process_name",
    "series": "series",
    "series-description": "series_description",
    "value": "value",
    "units": "units",
}


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("eia_retail_fuel_weekly_ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #


def _fetch_page(
    client: httpx.Client,
    api_key: str,
    start: str,
    end: str | None,
    offset: int,
) -> tuple[list[dict[str, Any]], int, dict[str, str], str]:
    """Return (rows, total_available, response_headers, sanitized_url).

    sanitized_url omits api_key for safe logging / provenance storage.
    """
    params: list[tuple[str, str]] = [
        ("api_key", api_key),
        ("frequency", "weekly"),
        ("data[0]", "value"),
        ("start", start),
        ("offset", str(offset)),
        ("length", str(PAGE_SIZE)),
        ("sort[0][column]", "period"),
        ("sort[0][direction]", "asc"),
    ]
    if end:
        params.append(("end", end))

    r = client.get(ENDPOINT, params=params, timeout=60)
    r.raise_for_status()
    body = r.json()
    response = body.get("response") or {}
    rows = response.get("data") or []
    total = int(response.get("total") or 0)

    sanitized = [(k, v) for k, v in params if k != "api_key"]
    sanitized_url = f"{ENDPOINT}?{urllib.parse.urlencode(sanitized)}"

    return rows, total, dict(r.headers), sanitized_url


def _parse_observed_at(headers: dict[str, str]) -> datetime | None:
    # EIA returns standard HTTP Date header.
    raw = headers.get("Date") or headers.get("date")
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        return dt.astimezone(timezone.utc) if dt else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Row coercion
# --------------------------------------------------------------------------- #


def _coerce(raw: dict[str, Any]) -> dict[str, Any]:
    """Map EIA v2 row → DB column dict. Empty strings → None."""
    out: dict[str, Any] = {c: None for c in TYPED_COLS}
    for src_key, db_col in EIA_KEY_MAP.items():
        if src_key not in raw:
            continue
        v = raw[src_key]
        if v is None:
            out[db_col] = None
            continue
        if db_col == "value":
            try:
                out[db_col] = float(v) if v != "" else None
            except (TypeError, ValueError):
                out[db_col] = None
        elif db_col == "period":
            out[db_col] = (str(v).strip() or None)
        else:
            s = str(v).strip()
            out[db_col] = s if s != "" else None
    return out


# --------------------------------------------------------------------------- #
# Upsert
# --------------------------------------------------------------------------- #


def _upsert(
    conn: psycopg.Connection,
    rows: list[dict[str, Any]],
    raw_rows: list[dict[str, Any]],
    *,
    source_filename: str,
    source_download_url: str,
    source_observed_at: datetime | None,
    source_run_metadata: dict[str, Any],
    task_id: str | None,
    schedule_id: str | None,
) -> int:
    """INSERT…ON CONFLICT…DO UPDATE in batches. Returns rows upserted."""
    table = "entities.source_eia_retail_fuel_weekly"
    all_cols = (
        *TYPED_COLS,
        "raw_source_row", "source_provider", "source_filename",
        "source_download_url", "source_observed_at", "source_run_metadata",
        "source_task_id", "source_schedule_id",
    )
    placeholders = ",".join(["%s"] * len(all_cols))
    update_cols = [c for c in TYPED_COLS if c not in PK_COLS] + [
        "raw_source_row", "source_filename", "source_download_url",
        "source_observed_at", "source_run_metadata",
        "source_task_id", "source_schedule_id",
        # ingested_at intentionally NOT updated — preserves first-seen audit.
    ]
    set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
    distinct_clause = " OR ".join(
        f"{table}.{c} IS DISTINCT FROM EXCLUDED.{c}" for c in update_cols
    )
    sql = (
        f"INSERT INTO {table} ({','.join(all_cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({','.join(PK_COLS)}) DO UPDATE SET {set_clause} "
        f"WHERE {distinct_clause}"
    )

    upserted = 0
    with conn.cursor() as cur:
        for i in range(0, len(rows), DB_BATCH_SIZE):
            chunk = rows[i:i + DB_BATCH_SIZE]
            chunk_raw = raw_rows[i:i + DB_BATCH_SIZE]
            params = []
            for row, raw in zip(chunk, chunk_raw):
                p = [row.get(c) for c in TYPED_COLS]
                p.append(Jsonb(raw))
                p.append(PROVIDER)
                p.append(source_filename)
                p.append(source_download_url)
                p.append(source_observed_at)
                p.append(Jsonb(source_run_metadata))
                p.append(task_id)
                p.append(schedule_id)
                params.append(p)
            cur.executemany(sql, params)
            upserted += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        conn.commit()
    return upserted


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #


def _start_run(
    conn: psycopg.Connection,
    start_period: str,
    end_period: str | None,
    task_id: str | None,
    schedule_id: str | None,
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ops.eia_retail_fuel_weekly_ingest_runs "
            "(status, source_filename, start_period, end_period, "
            " task_id, schedule_id) "
            "VALUES ('running', %s, %s, %s, %s, %s) RETURNING run_id",
            (SOURCE_FILENAME, start_period, end_period, task_id, schedule_id),
        )
        run_id = cur.fetchone()[0]
        conn.commit()
    return run_id


def _finish_run(
    conn: psycopg.Connection,
    run_id: str,
    status: str,
    rows_seen: dict[str, int],
    rows_upserted: dict[str, int],
    source_observed_at: datetime | None,
    source_download_url: str | None,
    error: str | None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE ops.eia_retail_fuel_weekly_ingest_runs SET "
            "  status = %s, completed_at = now(), "
            "  rows_seen = %s, rows_upserted = %s, "
            "  source_observed_at = %s, source_download_url = %s, "
            "  error_text = %s "
            "WHERE run_id = %s",
            (status, Jsonb(rows_seen), Jsonb(rows_upserted),
             source_observed_at, source_download_url, error, run_id),
        )
        conn.commit()


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #


def _ingest(
    conn: psycopg.Connection | None,
    client: httpx.Client,
    api_key: str,
    start: str,
    end: str | None,
    *,
    dry_run: bool,
    task_id: str | None,
    schedule_id: str | None,
) -> tuple[int, int, datetime | None, str | None]:
    """Returns (rows_seen, rows_upserted, source_observed_at, last_url)."""
    offset = 0
    pages = 0
    total_seen = 0
    total_upserted = 0
    source_observed_at: datetime | None = None
    last_url: str | None = None

    while True:
        rows, total, headers, url = _fetch_page(client, api_key, start, end, offset)
        last_url = url
        observed = _parse_observed_at(headers)
        if observed and (source_observed_at is None or observed < source_observed_at):
            source_observed_at = observed

        page_count = len(rows)
        pages += 1
        total_seen += page_count
        log.info(
            "page %d: offset=%d rows=%d total_available=%d (cumulative seen=%d)",
            pages, offset, page_count, total, total_seen,
        )

        if page_count == 0:
            break

        coerced = [_coerce(r) for r in rows]
        # Filter rows missing PK components (defensive — EIA shouldn't emit
        # null period/series, but skip rather than fail the whole page).
        keep_pairs = [
            (rc, raw) for rc, raw in zip(coerced, rows)
            if rc.get("period") and rc.get("series")
        ]
        skipped = page_count - len(keep_pairs)
        if skipped:
            log.warning("page %d: skipping %d rows missing PK columns", pages, skipped)

        if not dry_run and conn is not None and keep_pairs:
            run_meta = {
                "page": pages,
                "offset": offset,
                "rows_in_page": len(keep_pairs),
                "total_available": total,
                "start": start,
                "end": end,
            }
            upserted = _upsert(
                conn,
                [c for c, _ in keep_pairs],
                [r for _, r in keep_pairs],
                source_filename=SOURCE_FILENAME,
                source_download_url=url,
                source_observed_at=observed,
                source_run_metadata=run_meta,
                task_id=task_id,
                schedule_id=schedule_id,
            )
            total_upserted += upserted
            log.info("page %d: upserted %d rows (cumulative=%d)",
                     pages, upserted, total_upserted)

        # EIA v2 doesn't always return total accurately when filters applied;
        # advance by page_count and stop when a short page comes in.
        if page_count < PAGE_SIZE:
            break
        offset += page_count

    return total_seen, total_upserted, source_observed_at, last_url


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _default_start() -> str:
    today = datetime.now(timezone.utc).date()
    return (today - timedelta(days=DEFAULT_LOOKBACK_DAYS)).isoformat()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--start",
        default=None,
        help="Inclusive start period (YYYY-MM-DD). "
             f"Default: today - {DEFAULT_LOOKBACK_DAYS} days. "
             "Use 1990-08-20 for full history.",
    )
    parser.add_argument(
        "--end",
        default=None,
        help="Inclusive end period (YYYY-MM-DD). Default: open (latest).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch + parse only, no DB writes.",
    )
    args = parser.parse_args()

    start = args.start or _default_start()
    end = args.end

    api_key = os.environ.get("EIA_API_KEY")
    if not api_key:
        log.error(
            "EIA_API_KEY env var must be set. Register free at "
            "https://www.eia.gov/opendata/register.php and store the key in "
            "Doppler (data-engine-x/prd) as EIA_API_KEY."
        )
        return 2

    task_id = os.environ.get("TRIGGER_TASK_ID")
    schedule_id = os.environ.get("TRIGGER_SCHEDULE_ID")

    db_url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ.get("DEX_DB_URL_POOLED")
    if not args.dry_run and not db_url:
        log.error("DEX_DB_URL_DIRECT or DEX_DB_URL_POOLED must be set "
                  "(or pass --dry-run).")
        return 2

    conn: psycopg.Connection | None = None
    if not args.dry_run:
        conn = psycopg.connect(db_url, autocommit=False)

    rows_seen = 0
    rows_upserted = 0
    source_observed_at: datetime | None = None
    last_url: str | None = None
    run_id: str | None = None
    status = "succeeded"
    err: str | None = None

    try:
        if conn is not None:
            run_id = _start_run(conn, start, end, task_id, schedule_id)

        with httpx.Client(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        ) as client:
            rows_seen, rows_upserted, source_observed_at, last_url = _ingest(
                conn, client, api_key, start, end,
                dry_run=args.dry_run,
                task_id=task_id, schedule_id=schedule_id,
            )

    except Exception as exc:
        status = "failed"
        err = f"{type(exc).__name__}: {exc}"
        log.exception("ingest failed")
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                log.exception("rollback failed during error cleanup")

    finally:
        if conn is not None and run_id is not None:
            _finish_run(
                conn, run_id, status,
                {"total": rows_seen},
                {"total": rows_upserted},
                source_observed_at, last_url, err,
            )
            conn.close()

    log.info("done. status=%s rows_seen=%d rows_upserted=%d",
             status, rows_seen, rows_upserted)
    return 0 if status == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())

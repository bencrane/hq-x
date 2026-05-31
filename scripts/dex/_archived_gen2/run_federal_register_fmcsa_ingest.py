#!/usr/bin/env python3
"""Federal Register articles filtered to FMCSA — regulatory-change ingest.

Source:
    Federal Register Public API v1 — https://www.federalregister.gov/api/v1/articles
    Filter: conditions[agencies][]=federal-motor-carrier-safety-administration
    Pagination: per_page up to 1000; we use 200 for safety.
    No auth.

Idempotency:
    INSERT ... ON CONFLICT (document_number) DO UPDATE WHERE row IS DISTINCT
    FROM EXCLUDED.

Audit:
    ops.federal_register_fmcsa_ingest_runs — one row per invocation.

Coverage:
    Default: rolling --days 365 (publication_date >= today - 365d). The FR
    API supports unbounded back-fills but historical FMCSA corpus is
    only a few thousand articles; --start-date 1994-01-01 backfills the
    full archive in ~10 paginated requests.

Usage:
    PYTHONPATH=. doppler run -- python3 scripts/run_federal_register_fmcsa_ingest.py
    PYTHONPATH=. doppler run -- python3 scripts/run_federal_register_fmcsa_ingest.py --start-date 1994-01-01
    PYTHONPATH=. doppler run -- python3 scripts/run_federal_register_fmcsa_ingest.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
import psycopg
from psycopg.types.json import Jsonb


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

PROVIDER = "federal_register_fmcsa"
USER_AGENT = "data-engine-x-api/federal-register-fmcsa-ingest"
ENDPOINT = "https://www.federalregister.gov/api/v1/articles"
SOURCE_FILENAME = "api/v1/articles"
AGENCY_SLUG = "federal-motor-carrier-safety-administration"

PAGE_SIZE = 200
DB_BATCH_SIZE = 500
DEFAULT_LOOKBACK_DAYS = 365

# 1:1 typed column mirror; ALTER TABLE to extend.
TYPED_COLS: tuple[str, ...] = (
    "document_number", "title", "type", "subtype", "abstract", "action",
    "excerpts", "dates", "effective_on", "comments_close_on",
    "publication_date", "citation", "volume", "start_page", "end_page",
    "html_url", "pdf_url", "raw_text_url", "body_html_url",
    "public_inspection_pdf_url",
    "agencies", "topics", "dockets", "regulations_dot_gov_info",
)

JSONB_COLS: frozenset[str] = frozenset({
    "agencies", "topics", "dockets", "regulations_dot_gov_info",
})
DATE_COLS: frozenset[str] = frozenset({
    "effective_on", "comments_close_on", "publication_date",
})
INT_COLS: frozenset[str] = frozenset({
    "volume", "start_page", "end_page",
})

PK_COLS: tuple[str] = ("document_number",)

# Subset of fields requested from the FR API. The API returns ~30 fields by
# default; we explicitly request the ones we plan to keep typed + a few
# extras the API surfaces only with `fields[]=`.
FR_FIELDS: tuple[str, ...] = (
    "document_number", "title", "type", "subtype", "abstract", "action",
    "excerpts", "dates", "effective_on", "comments_close_on",
    "publication_date", "citation", "volume", "start_page", "end_page",
    "html_url", "pdf_url", "raw_text_url", "body_html_url",
    "public_inspection_pdf_url",
    "agencies", "topics", "dockets", "regulations_dot_gov_info",
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
    return logging.getLogger("federal_register_fmcsa_ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #


def _fetch_page(
    client: httpx.Client,
    start_date: str,
    end_date: str | None,
    page: int,
) -> tuple[list[dict[str, Any]], int, dict[str, str], str]:
    """Returns (results, total_pages, response_headers, sanitized_url)."""
    params: list[tuple[str, str]] = [
        ("conditions[agencies][]", AGENCY_SLUG),
        ("conditions[publication_date][gte]", start_date),
        ("per_page", str(PAGE_SIZE)),
        ("page", str(page)),
        ("order", "newest"),
    ]
    if end_date:
        params.append(("conditions[publication_date][lte]", end_date))
    for f in FR_FIELDS:
        params.append(("fields[]", f))

    r = client.get(ENDPOINT, params=params, timeout=60)
    r.raise_for_status()
    body = r.json()
    results = body.get("results") or []
    total_pages = int(body.get("total_pages") or 1)
    sanitized_url = f"{ENDPOINT}?{urllib.parse.urlencode(params)}"
    return results, total_pages, dict(r.headers), sanitized_url


def _response_date(headers: dict[str, str]) -> datetime | None:
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
    out: dict[str, Any] = {c: None for c in TYPED_COLS}
    for col in TYPED_COLS:
        v = raw.get(col)
        if v is None:
            out[col] = None
        elif col in JSONB_COLS:
            out[col] = v
        elif col in DATE_COLS:
            out[col] = _to_date(v)
        elif col in INT_COLS:
            try:
                out[col] = int(v) if v != "" else None
            except (TypeError, ValueError):
                out[col] = None
        else:
            s = str(v).strip()
            out[col] = s if s else None
    return out


def _to_date(v: Any) -> date | None:
    if not v:
        return None
    if isinstance(v, date):
        return v
    s = str(v).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


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
    table = "entities.source_federal_register_fmcsa"
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
                p = []
                for c in TYPED_COLS:
                    v = row.get(c)
                    if c in JSONB_COLS and v is not None:
                        v = Jsonb(v)
                    p.append(v)
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
    start_date: str,
    end_date: str | None,
    task_id: str | None,
    schedule_id: str | None,
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ops.federal_register_fmcsa_ingest_runs "
            "(status, source_filename, source_download_url, "
            " start_date, end_date, task_id, schedule_id) "
            "VALUES ('running', %s, %s, %s, %s, %s, %s) RETURNING run_id",
            (SOURCE_FILENAME, ENDPOINT, start_date, end_date,
             task_id, schedule_id),
        )
        run_id = cur.fetchone()[0]
        conn.commit()
    return run_id


def _finish_run(
    conn: psycopg.Connection,
    run_id: str,
    status: str,
    pages: int,
    rows_seen: int,
    rows_upserted: int,
    source_observed_at: datetime | None,
    error: str | None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE ops.federal_register_fmcsa_ingest_runs SET "
            "  status = %s, completed_at = now(), "
            "  pages_fetched = %s, rows_seen = %s, rows_upserted = %s, "
            "  source_observed_at = %s, error_text = %s "
            "WHERE run_id = %s",
            (status, pages, rows_seen, rows_upserted,
             source_observed_at, error, run_id),
        )
        conn.commit()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    today = date.today()
    parser.add_argument(
        "--start-date",
        default=(today - timedelta(days=DEFAULT_LOOKBACK_DAYS)).isoformat(),
        help="Inclusive start (YYYY-MM-DD). Default: today - %d days." % DEFAULT_LOOKBACK_DAYS,
    )
    parser.add_argument("--end-date", default=None,
                        help="Inclusive end (YYYY-MM-DD). Default: open.")
    parser.add_argument("--days", type=int, default=None,
                        help="Override start-date as today - N days.")
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch + parse only, no DB writes.")
    args = parser.parse_args()

    if args.days is not None:
        args.start_date = (today - timedelta(days=args.days)).isoformat()

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

    pages = 0
    rows_seen = 0
    rows_upserted = 0
    source_observed_at: datetime | None = None
    run_id: str | None = None
    status = "succeeded"
    err: str | None = None

    try:
        if conn is not None:
            run_id = _start_run(conn, args.start_date, args.end_date,
                                task_id, schedule_id)

        with httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        ) as client:
            page = 1
            while True:
                results, total_pages, headers, url = _fetch_page(
                    client, args.start_date, args.end_date, page
                )
                pages += 1
                obs = _response_date(headers)
                if obs and (source_observed_at is None or obs < source_observed_at):
                    source_observed_at = obs
                page_count = len(results)
                rows_seen += page_count
                log.info("page %d/%d: %d rows (cumulative=%d)",
                         page, total_pages, page_count, rows_seen)

                if args.dry_run or conn is None:
                    if page >= total_pages or page_count == 0:
                        break
                    page += 1
                    continue

                if results:
                    coerced: list[dict[str, Any]] = []
                    raw_rows: list[dict[str, Any]] = []
                    for raw in results:
                        rc = _coerce(raw)
                        if not rc.get("document_number"):
                            log.warning("skipping article with no document_number: %r",
                                        {k: raw.get(k) for k in ("title", "type")})
                            continue
                        coerced.append(rc)
                        raw_rows.append(raw)
                    if coerced:
                        meta = {"page": page, "rows_in_page": len(coerced),
                                "total_pages": total_pages}
                        ups = _upsert(
                            conn, coerced, raw_rows,
                            source_filename=SOURCE_FILENAME,
                            source_download_url=url,
                            source_observed_at=obs,
                            source_run_metadata=meta,
                            task_id=task_id,
                            schedule_id=schedule_id,
                        )
                        rows_upserted += ups
                        log.info("page %d: %d upserted", page, ups)

                if page >= total_pages or page_count == 0:
                    break
                page += 1

    except Exception as exc:
        status = "failed"
        err = f"{type(exc).__name__}: {exc}"
        log.exception("ingest failed")
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                log.exception("rollback failed")

    finally:
        if conn is not None and run_id is not None:
            _finish_run(conn, run_id, status, pages, rows_seen, rows_upserted,
                        source_observed_at, err)
            conn.close()

    log.info("done. status=%s pages=%d rows_seen=%d rows_upserted=%d",
             status, pages, rows_seen, rows_upserted)
    return 0 if status == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())

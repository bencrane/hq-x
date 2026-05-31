#!/usr/bin/env python3
"""Trade.gov Consolidated Screening List — federal aggregator of 12 sanctions/restriction lists.

Source:
    International Trade Administration (ITA).
    Default mode (--bulk, the default): canonical bulk JSON file at
        https://data.trade.gov/downloadable_consolidated_screening_list/v1/consolidated.json
    Single ~32 MB request, no auth, daily-refreshed by ITA. Returns the entire
    25k+ row corpus across all 12 source lists in one shot.

    Fallback mode (--api): per-source paginated calls to the search API at
        https://data.trade.gov/consolidated_screening_list/v1/search
    Auth via TRADE_GOV_API_KEY (?subscription-key=). Only useful for ad-hoc
    lookups against a specific source/filter — the search API caps `offset` at
    1000, so any source with >1000 entries (SDN ~19k, EL ~3.4k, DPL ~1.6k) is
    silently truncated. Do NOT use --api for full-corpus ingest.

Idempotency:
    INSERT ... ON CONFLICT (csl_id, source) DO UPDATE WHERE row IS DISTINCT
    FROM EXCLUDED. last_observed_at is set to the response Date header on
    every run; downstream MVs filter currently-listed entries by recency
    window.

Audit:
    ops.trade_gov_csl_ingest_runs — one row per invocation. rows_by_source
    breaks down counts per source list.

Usage:
    PYTHONPATH=. doppler run -- python3 scripts/run_trade_gov_csl_ingest.py
    PYTHONPATH=. doppler run -- python3 scripts/run_trade_gov_csl_ingest.py --dry-run
    PYTHONPATH=. doppler run -- python3 scripts/run_trade_gov_csl_ingest.py --api      # fallback
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
import psycopg
from psycopg.types.json import Jsonb


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

PROVIDER = "trade_gov_csl"
USER_AGENT = "data-engine-x-api/trade-gov-csl-ingest"

# Bulk path — canonical full-corpus source. No auth, daily refresh.
BULK_URL = "https://data.trade.gov/downloadable_consolidated_screening_list/v1/consolidated.json"
BULK_FILENAME = "downloadable_consolidated_screening_list/v1/consolidated.json"

# Search-API path — fallback only. Subject to offset<=1000 cap per source.
API_ENDPOINT = "https://data.trade.gov/consolidated_screening_list/v1/search"
API_FILENAME = "consolidated_screening_list/v1/search"

# Source codes for the search-API fallback. Short codes correspond to the
# `sources` query param. SDN intentionally excluded from the API path: the
# offset<=1000 cap silently truncates ~19k entries down to 1000.
API_SOURCE_CODES: tuple[str, ...] = (
    "DPL", "EL", "UVL", "PLC", "FSE", "ISN", "DTC", "CAP",
)

PAGE_SIZE = 50  # ITA caps size at 50; >50 silently downgrades to 10
DB_BATCH_SIZE = 1_000
PAGE_DELAY_SEC = 1.0  # 429 after ~17 fast requests; 1s/page is safe

# Field mapping: bulk-JSON / API key → DB column.
SCALAR_FIELDS: dict[str, str] = {
    "id": "csl_id",
    "source": "source",
    "entity_number": "entity_number",
    "name": "name",
    "type": "type",
    "federal_register_notice": "federal_register_notice",
    "start_date": "start_date",
    "end_date": "end_date",
    "date_of_listing": "date_of_listing",
    "source_information_url": "source_information_url",
    "source_list_url": "source_list_url",
    "standard_order": "standard_order",
    "license_requirement": "license_requirement",
    "license_policy": "license_policy",
    "call_sign": "call_sign",
    "vessel_type": "vessel_type",
    "gross_tonnage": "gross_tonnage",
    "gross_registered_tonnage": "gross_registered_tonnage",
    "vessel_flag": "vessel_flag",
    "vessel_owner": "vessel_owner",
    "title": "title",
    "remarks": "remarks",
    "country": "country",
}
JSONB_FIELDS: dict[str, str] = {
    "programs": "programs",
    "alt_names": "alt_names",
    "ids": "ids",
    "nationalities": "nationalities",
    "citizenships": "citizenships",
    "dates_of_birth": "dates_of_birth",
    "places_of_birth": "places_of_birth",
    "addresses": "addresses",
}
DATE_COLS: frozenset[str] = frozenset({"start_date", "end_date", "date_of_listing"})

TYPED_COLS: tuple[str, ...] = (
    tuple(SCALAR_FIELDS.values()) + tuple(JSONB_FIELDS.values()) + ("last_observed_at",)
)
PK_COLS: tuple[str, str] = ("csl_id", "source")


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("trade_gov_csl_ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# Fetch (bulk path)
# --------------------------------------------------------------------------- #


def _fetch_bulk(
    client: httpx.Client,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Returns (results, response_headers) from the bulk JSON file.

    The payload shape is {"results": [...], "search_performed_at": ..., ...}.
    """
    r = client.get(BULK_URL, timeout=180)
    r.raise_for_status()
    body = r.json()
    results = body.get("results") or []
    if not isinstance(results, list):
        raise RuntimeError(f"unexpected bulk payload shape: top-level keys={list(body.keys())}")
    return results, dict(r.headers)


# --------------------------------------------------------------------------- #
# Fetch (search-API fallback)
# --------------------------------------------------------------------------- #


def _fetch_api_page(
    client: httpx.Client,
    api_key: str,
    source_code: str,
    offset: int,
) -> tuple[list[dict[str, Any]], int, dict[str, str], str]:
    """Returns (results, total, response_headers, sanitized_url) for one source page."""
    params: list[tuple[str, str]] = [
        ("offset", str(offset)),
        ("size", str(PAGE_SIZE)),
        ("sources", source_code),
        ("subscription-key", api_key),
    ]
    r = client.get(API_ENDPOINT, params=params, timeout=60)
    r.raise_for_status()
    body = r.json()
    results = body.get("results") or []
    total = int(body.get("total") or 0)
    sanitized = [(k, v) for k, v in params if k != "subscription-key"]
    sanitized_url = f"{API_ENDPOINT}?{urllib.parse.urlencode(sanitized)}"
    return results, total, dict(r.headers), sanitized_url


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


def _to_date(v: Any) -> Any:
    if not v:
        return None
    s = str(v).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _coerce(raw: dict[str, Any], last_observed_at: datetime) -> dict[str, Any]:
    out: dict[str, Any] = {c: None for c in TYPED_COLS}
    for src_key, db_col in SCALAR_FIELDS.items():
        v = raw.get(src_key)
        if v is None:
            continue
        if db_col in DATE_COLS:
            out[db_col] = _to_date(v)
        else:
            s = str(v).strip()
            out[db_col] = s if s else None
    for src_key, db_col in JSONB_FIELDS.items():
        v = raw.get(src_key)
        if v is not None:
            out[db_col] = v
    out["last_observed_at"] = last_observed_at
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
    table = "entities.source_trade_gov_csl"
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
    jsonb_set = set(JSONB_FIELDS.values())
    with conn.cursor() as cur:
        for i in range(0, len(rows), DB_BATCH_SIZE):
            chunk = rows[i:i + DB_BATCH_SIZE]
            chunk_raw = raw_rows[i:i + DB_BATCH_SIZE]
            params = []
            for row, raw in zip(chunk, chunk_raw):
                p = []
                for c in TYPED_COLS:
                    v = row.get(c)
                    if c in jsonb_set and v is not None:
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
    source_filename: str,
    source_download_url: str,
    task_id: str | None,
    schedule_id: str | None,
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ops.trade_gov_csl_ingest_runs "
            "(status, source_filename, source_download_url, "
            " task_id, schedule_id) "
            "VALUES ('running', %s, %s, %s, %s) RETURNING run_id",
            (source_filename, source_download_url, task_id, schedule_id),
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
    rows_by_source: dict[str, int],
    source_observed_at: datetime | None,
    error: str | None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE ops.trade_gov_csl_ingest_runs SET "
            "  status = %s, completed_at = now(), "
            "  pages_fetched = %s, rows_seen = %s, rows_upserted = %s, "
            "  rows_by_source = %s, source_observed_at = %s, error_text = %s "
            "WHERE run_id = %s",
            (status, pages, rows_seen, rows_upserted,
             Jsonb(rows_by_source), source_observed_at, error, run_id),
        )
        conn.commit()


# --------------------------------------------------------------------------- #
# Pipeline — bulk
# --------------------------------------------------------------------------- #


def _run_bulk(
    conn: psycopg.Connection | None,
    args,
    task_id: str | None,
    schedule_id: str | None,
) -> tuple[str, str, int, int, int, dict[str, int], datetime | None]:
    """Returns (status, err_or_empty, pages, rows_seen, rows_upserted,
    rows_by_source, source_observed_at). pages is always 1 for bulk mode."""
    rows_by_source: dict[str, int] = {}
    rows_seen = 0
    rows_upserted = 0
    source_observed_at: datetime | None = None

    with httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
    ) as client:
        log.info("fetching bulk file: %s", BULK_URL)
        results, headers = _fetch_bulk(client)
        rows_seen = len(results)
        source_observed_at = _response_date(headers)
        log.info("bulk fetch returned %d rows (response_date=%s)",
                 rows_seen, source_observed_at)

        last_obs = source_observed_at or datetime.now(timezone.utc)
        coerced: list[dict[str, Any]] = []
        raw_rows: list[dict[str, Any]] = []
        for raw in results:
            rc = _coerce(raw, last_obs)
            if not rc.get("csl_id") or not rc.get("source"):
                log.warning("skipping CSL entry missing PK: %r",
                            {k: raw.get(k) for k in ("id", "source", "name")})
                continue
            coerced.append(rc)
            raw_rows.append(raw)
            src = rc["source"]
            rows_by_source[src] = rows_by_source.get(src, 0) + 1

        if args.dry_run or conn is None:
            log.info("dry-run: would upsert %d rows across %d sources",
                     len(coerced), len(rows_by_source))
            return "succeeded", "", 1, rows_seen, 0, rows_by_source, source_observed_at

        meta = {
            "mode": "bulk",
            "rows_in_payload": len(results),
            "rows_ingestable": len(coerced),
            "distinct_sources": len(rows_by_source),
        }
        rows_upserted = _upsert(
            conn, coerced, raw_rows,
            source_filename=BULK_FILENAME,
            source_download_url=BULK_URL,
            source_observed_at=source_observed_at,
            source_run_metadata=meta,
            task_id=task_id,
            schedule_id=schedule_id,
        )
        log.info("bulk upserted %d rows across %d sources",
                 rows_upserted, len(rows_by_source))

    return "succeeded", "", 1, rows_seen, rows_upserted, rows_by_source, source_observed_at


# --------------------------------------------------------------------------- #
# Pipeline — search-API fallback
# --------------------------------------------------------------------------- #


def _run_api(
    conn: psycopg.Connection | None,
    args,
    api_key: str,
    task_id: str | None,
    schedule_id: str | None,
) -> tuple[str, str, int, int, int, dict[str, int], datetime | None]:
    pages = 0
    rows_seen = 0
    rows_upserted = 0
    rows_by_source: dict[str, int] = {}
    source_observed_at: datetime | None = None

    with httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
    ) as client:
        for source_code in API_SOURCE_CODES:
            offset = 0
            source_pages = 0
            source_rows = 0
            log.info("=== source: %s ===", source_code)
            while True:
                results, total, headers, url = _fetch_api_page(
                    client, api_key, source_code, offset,
                )
                pages += 1
                source_pages += 1
                obs = _response_date(headers)
                if obs and (source_observed_at is None or obs > source_observed_at):
                    source_observed_at = obs
                page_count = len(results)
                rows_seen += page_count
                source_rows += page_count
                log.info("[%s] page %d offset=%d: %d rows (source=%d/%d, cum=%d)",
                         source_code, source_pages, offset, page_count,
                         source_rows, total, rows_seen)

                if page_count == 0:
                    break

                last_obs = source_observed_at or datetime.now(timezone.utc)
                coerced: list[dict[str, Any]] = []
                raw_rows: list[dict[str, Any]] = []
                for raw in results:
                    rc = _coerce(raw, last_obs)
                    if not rc.get("csl_id") or not rc.get("source"):
                        log.warning("skipping CSL entry missing PK: %r",
                                    {k: raw.get(k) for k in ("id", "source", "name")})
                        continue
                    coerced.append(rc)
                    raw_rows.append(raw)
                    src = rc["source"]
                    rows_by_source[src] = rows_by_source.get(src, 0) + 1

                if not args.dry_run and conn is not None and coerced:
                    meta = {"mode": "api",
                            "source_code": source_code,
                            "page": source_pages, "offset": offset,
                            "rows_in_page": len(coerced), "total": total}
                    ups = _upsert(
                        conn, coerced, raw_rows,
                        source_filename=API_FILENAME,
                        source_download_url=url,
                        source_observed_at=source_observed_at,
                        source_run_metadata=meta,
                        task_id=task_id,
                        schedule_id=schedule_id,
                    )
                    rows_upserted += ups
                    log.info("[%s] page %d: upserted %d",
                             source_code, source_pages, ups)

                if page_count < PAGE_SIZE:
                    break
                offset += page_count
                if offset >= 1000:
                    log.warning("[%s] reached offset cap 1000 — partial coverage; "
                                "use bulk mode (default) for full corpus",
                                source_code)
                    break
                time.sleep(PAGE_DELAY_SEC)

    return "succeeded", "", pages, rows_seen, rows_upserted, rows_by_source, source_observed_at


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--api",
        action="store_true",
        help="Use the paginated search API instead of the bulk JSON file. "
             "Subject to a hard offset<=1000 cap per source — silently "
             "truncates SDN/EL/DPL. For ad-hoc lookups only, never full ingest.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch + parse only, no DB writes.",
    )
    args = parser.parse_args()

    api_key: str | None = None
    if args.api:
        api_key = os.environ.get("TRADE_GOV_API_KEY")
        if not api_key:
            log.error(
                "--api mode requires TRADE_GOV_API_KEY env var. Free key at "
                "https://api.trade.gov/v3/key_signup; store in Doppler."
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

    run_id: str | None = None
    status = "succeeded"
    err: str | None = None
    pages = 0
    rows_seen = 0
    rows_upserted = 0
    rows_by_source: dict[str, int] = {}
    source_observed_at: datetime | None = None

    src_filename = API_FILENAME if args.api else BULK_FILENAME
    src_url = API_ENDPOINT if args.api else BULK_URL

    try:
        if conn is not None:
            run_id = _start_run(conn, src_filename, src_url, task_id, schedule_id)

        if args.api:
            status, err, pages, rows_seen, rows_upserted, rows_by_source, source_observed_at = _run_api(
                conn, args, api_key, task_id, schedule_id,
            )
        else:
            status, err, pages, rows_seen, rows_upserted, rows_by_source, source_observed_at = _run_bulk(
                conn, args, task_id, schedule_id,
            )

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
            _finish_run(
                conn, run_id, status, pages, rows_seen, rows_upserted,
                rows_by_source, source_observed_at, err,
            )
            conn.close()

    log.info("done. mode=%s status=%s pages=%d rows_seen=%d rows_upserted=%d sources=%s",
             "api" if args.api else "bulk",
             status, pages, rows_seen, rows_upserted,
             dict(sorted(rows_by_source.items())))
    return 0 if status == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""BLS PPI for freight transportation modes — monthly raw ingest.

Two source modes:

  Default (bulk):
    BLS publishes the entire PPI dataset as tab-delimited flat files at
    https://download.bls.gov/pub/time.series/pc/. No API key required;
    no quota. Files are tiny (~3–4 MB total for all freight industries).
    This is the canonical path for full-corpus and routine refreshes.

  --api:
    BLS Public Data API v2 — https://api.bls.gov/publicAPI/v2/timeseries/data/
    POST JSON, narrow to specific series IDs and year ranges. Useful for
    one-off lookups, ad-hoc deltas, or when you only need a handful of
    series. Requires `BLS_API_KEY` for reasonable throughput
    (https://data.bls.gov/registrationEngine/).

Idempotency:
    INSERT ... ON CONFLICT (series_id, year, period) DO UPDATE ... WHERE
    row IS DISTINCT FROM EXCLUDED. Handles (a) BLS revisions and (b) the
    `latest` flag flipping when a newer month is published.

Audit:
    ops.bls_ppi_freight_monthly_ingest_runs — one row per invocation.

Usage:
    # Bulk (default, no key required)
    PYTHONPATH=. doppler run -- python3 scripts/run_bls_ppi_freight_monthly_ingest.py
    PYTHONPATH=. doppler run -- python3 scripts/run_bls_ppi_freight_monthly_ingest.py --dry-run

    # API mode (one-off / delta)
    PYTHONPATH=. doppler run -- python3 scripts/run_bls_ppi_freight_monthly_ingest.py --api
    PYTHONPATH=. doppler run -- python3 scripts/run_bls_ppi_freight_monthly_ingest.py --api --start-year 2024 --end-year 2026
    PYTHONPATH=. doppler run -- python3 scripts/run_bls_ppi_freight_monthly_ingest.py --api --series PCU484121484121
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
import psycopg
from psycopg.types.json import Jsonb


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

PROVIDER = "bls_ppi_freight_monthly"
USER_AGENT = (
    "data-engine-x-api/bls-ppi-freight-monthly-ingest "
    "(contact: tools@substrate.build)"
)

# Bulk source — flat files, no key required.
BULK_BASE_URL = "https://download.bls.gov/pub/time.series/pc/"
SERIES_CATALOG_FILE = "pc.series"
PERIOD_CATALOG_FILE = "pc.period"

# Curated set of bulk industry files covering freight modes. BLS publishes
# one .data file per ~2-digit NAICS industry.
FREIGHT_BULK_FILES: tuple[str, ...] = (
    "pc.data.36.AirTransportation",
    "pc.data.37.RailTransportation",
    "pc.data.38.WaterTransportation",
    "pc.data.39.TruckTransportation",
    "pc.data.40.PipelineTransportation",
    "pc.data.42.TransportationSupport",
    "pc.data.43.PostalService",
    "pc.data.44.CouriersAndMessengers",
    "pc.data.45.WarehousingStorage",
)

# API source — fallback / delta mode.
API_ENDPOINT = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
API_SOURCE_FILENAME = "publicAPI/v2/timeseries/data"

# Curated API series list — used only when --api is set without --series.
FREIGHT_PPI_SERIES_IDS: tuple[str, ...] = (
    "PCU484121484121",  # Long-distance truckload trucking
    "PCU484122484122",  # Long-distance LTL trucking
    "PCU484110484110",  # Local general freight trucking
    "PCU482111482111",  # Line-haul railroads
    "PCU481112481112",  # Scheduled freight air transportation
    "PCU483111483111",  # Deep sea freight transportation
    "PCU483211483211",  # Inland water freight transportation
    "PCU492110492110",  # Couriers and express delivery services
    "PCU493110493110",  # General warehousing and storage
)

MAX_YEARS_PER_REQUEST_WITH_KEY = 20
MAX_YEARS_PER_REQUEST_NO_KEY = 10
MAX_SERIES_PER_REQUEST_WITH_KEY = 50
MAX_SERIES_PER_REQUEST_NO_KEY = 25

DEFAULT_API_LOOKBACK_YEARS = 5
DB_BATCH_SIZE = 5_000

TYPED_COLS: tuple[str, ...] = (
    "series_id", "year", "period",
    "period_name", "value", "latest", "footnotes",
    "series_title", "survey_name", "survey_abbreviation",
    "seasonal_adjustment", "measure_data_type", "industry_code",
)

PK_COLS: tuple[str, ...] = ("series_id", "year", "period")

# Hardcoded survey identity for the PPI Industry Data file family.
SURVEY_NAME = "Producer Price Index"
SURVEY_ABBREVIATION = "PC"
MEASURE_DATA_TYPE = "Index"

# Period code → human name. BLS uses M01–M12 (months), Q01–Q04 (quarters
# rare for PPI), A01 (annual).
PERIOD_NAME_MAP: dict[str, str] = {
    f"M{m:02d}": ["January", "February", "March", "April", "May", "June",
                   "July", "August", "September", "October", "November",
                   "December"][m - 1]
    for m in range(1, 13)
}
PERIOD_NAME_MAP.update({
    "Q01": "1st Quarter", "Q02": "2nd Quarter",
    "Q03": "3rd Quarter", "Q04": "4th Quarter",
    "A01": "Annual",
})


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("bls_ppi_freight_monthly_ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# Bulk: fetch + parse
# --------------------------------------------------------------------------- #


def _fetch_text(client: httpx.Client, path: str) -> tuple[str, datetime | None]:
    """GET a flat file from BLS bulk path. Returns (text, last_modified UTC)."""
    url = BULK_BASE_URL + path
    r = client.get(url, timeout=120)
    r.raise_for_status()
    text = r.text
    lm = r.headers.get("Last-Modified") or r.headers.get("last-modified")
    observed: datetime | None = None
    if lm:
        try:
            dt = parsedate_to_datetime(lm)
            observed = dt.astimezone(timezone.utc) if dt else None
        except (TypeError, ValueError):
            observed = None
    return text, observed


def _parse_tab_table(text: str) -> tuple[list[str], list[list[str]]]:
    """Parse a BLS flat tab-delimited file. Returns (header, rows)."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return [], []
    header = [c.strip() for c in lines[0].split("\t")]
    rows: list[list[str]] = []
    for ln in lines[1:]:
        cells = [c.strip() for c in ln.split("\t")]
        # Pad short rows to header length.
        if len(cells) < len(header):
            cells = cells + [""] * (len(header) - len(cells))
        rows.append(cells)
    return header, rows


def _load_series_catalog(client: httpx.Client) -> dict[str, dict[str, Any]]:
    """Download pc.series and return {series_id: {field: value}} map."""
    log.info("loading pc.series catalog")
    text, _ = _fetch_text(client, SERIES_CATALOG_FILE)
    header, rows = _parse_tab_table(text)
    catalog: dict[str, dict[str, Any]] = {}
    for cells in rows:
        d = dict(zip(header, cells))
        sid = (d.get("series_id") or "").strip()
        if not sid:
            continue
        catalog[sid] = d
    log.info("catalog: %d series", len(catalog))
    return catalog


def _series_industry_code(series_id: str) -> str | None:
    """Extract 6-digit NAICS embedded in BLS PCU series IDs."""
    m = re.match(r"^PCU(\d{6})\d+$", series_id)
    return m.group(1) if m else None


def _seasonal_label(code: str | None) -> str | None:
    if code is None:
        return None
    c = code.strip()
    if c == "S":
        return "Seasonally Adjusted"
    if c == "U":
        return "Not Seasonally Adjusted"
    return c if c else None


def _coerce_bulk_row(
    bulk_row: dict[str, str],
    catalog_row: dict[str, Any] | None,
    industry_code: str | None,
    latest_year: int,
    latest_period: str,
) -> dict[str, Any]:
    """Map a pc.data.XX row + catalog metadata → DB column dict."""
    sid = (bulk_row.get("series_id") or "").strip()
    period = (bulk_row.get("period") or "").strip()
    year_s = (bulk_row.get("year") or "").strip()
    try:
        year = int(year_s) if year_s else None
    except (TypeError, ValueError):
        year = None
    value_s = (bulk_row.get("value") or "").strip()
    try:
        value = float(value_s) if value_s else None
    except (TypeError, ValueError):
        value = None

    fcode = (bulk_row.get("footnote_codes") or "").strip()
    footnotes = [{"code": fcode}] if fcode else None

    is_latest = (year == latest_year and period == latest_period)

    catalog_row = catalog_row or {}
    return {
        "series_id": sid,
        "year": year,
        "period": period,
        "period_name": PERIOD_NAME_MAP.get(period),
        "value": value,
        "latest": is_latest,
        "footnotes": footnotes,
        "series_title": (catalog_row.get("series_title") or "").strip() or None,
        "survey_name": SURVEY_NAME,
        "survey_abbreviation": SURVEY_ABBREVIATION,
        "seasonal_adjustment": _seasonal_label(catalog_row.get("seasonal")),
        "measure_data_type": MEASURE_DATA_TYPE,
        "industry_code": industry_code,
    }


# --------------------------------------------------------------------------- #
# API: fetch (delta / one-off mode)
# --------------------------------------------------------------------------- #


def _api_post(
    client: httpx.Client,
    series_ids: list[str],
    start_year: int,
    end_year: int,
    api_key: str | None,
) -> tuple[dict[str, Any], dict[str, str]]:
    body: dict[str, Any] = {
        "seriesid": series_ids,
        "startyear": str(start_year),
        "endyear": str(end_year),
        "catalog": True,
    }
    if api_key:
        body["registrationkey"] = api_key
    r = client.post(
        API_ENDPOINT, json=body,
        headers={"Content-Type": "application/json"},
        timeout=120,
    )
    r.raise_for_status()
    body_json = r.json()
    if body_json.get("status") != "REQUEST_SUCCEEDED":
        raise RuntimeError(
            f"BLS API status={body_json.get('status')!r} "
            f"messages={body_json.get('message') or []!r}"
        )
    return body_json, dict(r.headers)


def _api_extract_catalog(series: dict[str, Any]) -> dict[str, Any]:
    cat = series.get("catalog") or {}
    return {
        "series_title": cat.get("series_title"),
        "survey_name": cat.get("survey_name"),
        "survey_abbreviation": cat.get("survey_abbreviation"),
        "seasonal_adjustment": cat.get("seasonal_adjustment"),
        "measure_data_type": cat.get("measure_data_type"),
    }


def _coerce_api_row(
    series_id: str,
    catalog: dict[str, Any],
    industry_code: str | None,
    raw: dict[str, Any],
) -> dict[str, Any]:
    year_raw = raw.get("year")
    try:
        year = int(year_raw) if year_raw is not None else None
    except (TypeError, ValueError):
        year = None
    value_raw = raw.get("value")
    try:
        value = float(value_raw) if value_raw not in (None, "") else None
    except (TypeError, ValueError):
        value = None
    latest_raw = raw.get("latest")
    if isinstance(latest_raw, bool):
        latest = latest_raw
    elif isinstance(latest_raw, str):
        latest = latest_raw.lower() == "true"
    else:
        latest = None
    period = raw.get("period")
    return {
        "series_id": series_id,
        "year": year,
        "period": str(period).strip() if period is not None else None,
        "period_name": (raw.get("periodName") or "").strip() or None,
        "value": value,
        "latest": latest,
        "footnotes": raw.get("footnotes"),
        "series_title": catalog.get("series_title"),
        "survey_name": catalog.get("survey_name"),
        "survey_abbreviation": catalog.get("survey_abbreviation"),
        "seasonal_adjustment": catalog.get("seasonal_adjustment"),
        "measure_data_type": catalog.get("measure_data_type"),
        "industry_code": industry_code,
    }


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
    table = "entities.source_bls_ppi_freight_monthly"
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
                    if c == "footnotes" and v is not None:
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
    series_ids: list[str] | None,
    start_year: int | None,
    end_year: int | None,
    source_filename: str,
    task_id: str | None,
    schedule_id: str | None,
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ops.bls_ppi_freight_monthly_ingest_runs "
            "(status, source_filename, series_ids, start_year, end_year, "
            " task_id, schedule_id) "
            "VALUES ('running', %s, %s, %s, %s, %s, %s) RETURNING run_id",
            (source_filename, series_ids, start_year, end_year,
             task_id, schedule_id),
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
            "UPDATE ops.bls_ppi_freight_monthly_ingest_runs SET "
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
# Bulk pipeline
# --------------------------------------------------------------------------- #


def _ingest_bulk(
    conn: psycopg.Connection | None,
    client: httpx.Client,
    *,
    dry_run: bool,
    task_id: str | None,
    schedule_id: str | None,
) -> tuple[dict[str, int], dict[str, int], datetime | None, str | None]:
    catalog = _load_series_catalog(client)
    rows_seen: dict[str, int] = {}
    rows_upserted: dict[str, int] = {}
    source_observed_at: datetime | None = None
    last_url: str | None = None

    for fname in FREIGHT_BULK_FILES:
        log.info("=== %s ===", fname)
        text, observed = _fetch_text(client, fname)
        last_url = BULK_BASE_URL + fname
        if observed and (source_observed_at is None or observed < source_observed_at):
            source_observed_at = observed

        header, rows = _parse_tab_table(text)
        log.info("[%s] %d rows", fname, len(rows))
        if not rows:
            continue

        # Determine the latest (year, period) seen in this file → set
        # `latest=true` flag on those rows.
        latest_year, latest_period = 0, ""
        for cells in rows:
            d = dict(zip(header, cells))
            try:
                y = int((d.get("year") or "0").strip())
            except (TypeError, ValueError):
                y = 0
            p = (d.get("period") or "").strip()
            if (y, p) > (latest_year, latest_period):
                latest_year, latest_period = y, p

        coerced: list[dict[str, Any]] = []
        raw_rows: list[dict[str, Any]] = []
        for cells in rows:
            d = dict(zip(header, cells))
            sid = (d.get("series_id") or "").strip()
            if not sid:
                continue
            cat_row = catalog.get(sid)
            industry = _series_industry_code(sid)
            rc = _coerce_bulk_row(d, cat_row, industry, latest_year, latest_period)
            if rc.get("year") is None or not rc.get("period") or not rc.get("series_id"):
                continue
            coerced.append(rc)
            # raw_source_row: combine bulk row + catalog row into one jsonb blob
            raw_rows.append({
                "bulk_data": d,
                "catalog": cat_row,
                "bulk_file": fname,
            })
            rows_seen[sid] = rows_seen.get(sid, 0) + 1

        log.info("[%s] ingestable: %d", fname, len(coerced))

        if dry_run or conn is None:
            continue

        meta = {
            "bulk_file": fname,
            "rows_in_file": len(rows),
            "rows_ingestable": len(coerced),
            "latest_year": latest_year,
            "latest_period": latest_period,
        }
        ups = _upsert(
            conn, coerced, raw_rows,
            source_filename=fname,
            source_download_url=BULK_BASE_URL + fname,
            source_observed_at=observed,
            source_run_metadata=meta,
            task_id=task_id,
            schedule_id=schedule_id,
        )
        # Distribute upsert count proportionally across series in this file.
        # rows_upserted ends up tracking per-series totals best-effort.
        for sid, n in rows_seen.items():
            if sid.startswith("PCU"):
                rows_upserted[sid] = rows_upserted.get(sid, 0)
        # Bulk upsert is per-file; record file-level total under a sentinel.
        rows_upserted[fname] = ups
        log.info("[%s] upserted %d", fname, ups)

    return rows_seen, rows_upserted, source_observed_at, last_url


# --------------------------------------------------------------------------- #
# API pipeline
# --------------------------------------------------------------------------- #


def _ingest_api(
    conn: psycopg.Connection | None,
    client: httpx.Client,
    api_key: str | None,
    series_ids: list[str],
    start_year: int,
    end_year: int,
    *,
    dry_run: bool,
    task_id: str | None,
    schedule_id: str | None,
) -> tuple[dict[str, int], dict[str, int], datetime | None, str | None]:
    rows_seen: dict[str, int] = {}
    rows_upserted: dict[str, int] = {}
    source_observed_at: datetime | None = None
    last_url: str | None = None

    max_years = (MAX_YEARS_PER_REQUEST_WITH_KEY if api_key
                 else MAX_YEARS_PER_REQUEST_NO_KEY)
    max_series = (MAX_SERIES_PER_REQUEST_WITH_KEY if api_key
                  else MAX_SERIES_PER_REQUEST_NO_KEY)

    year_chunks: list[tuple[int, int]] = []
    y = start_year
    while y <= end_year:
        ye = min(y + max_years - 1, end_year)
        year_chunks.append((y, ye))
        y = ye + 1

    for s_offset in range(0, len(series_ids), max_series):
        s_chunk = series_ids[s_offset:s_offset + max_series]
        for y_start, y_end in year_chunks:
            log.info("BLS API request: %d series, years %d–%d",
                     len(s_chunk), y_start, y_end)
            body, headers = _api_post(client, s_chunk, y_start, y_end, api_key)
            obs_raw = headers.get("Date") or headers.get("date")
            obs: datetime | None = None
            if obs_raw:
                try:
                    dt = parsedate_to_datetime(obs_raw)
                    obs = dt.astimezone(timezone.utc) if dt else None
                except (TypeError, ValueError):
                    obs = None
            if obs and (source_observed_at is None or obs < source_observed_at):
                source_observed_at = obs

            results = (body.get("Results") or {}).get("series") or []
            for series in results:
                sid = series.get("seriesID")
                if not sid:
                    continue
                catalog = _api_extract_catalog(series)
                industry = _series_industry_code(sid)
                data_points = series.get("data") or []
                rows_seen[sid] = rows_seen.get(sid, 0) + len(data_points)

                if dry_run or conn is None:
                    continue

                coerced: list[dict[str, Any]] = []
                raw_rows: list[dict[str, Any]] = []
                for raw in data_points:
                    rc = _coerce_api_row(sid, catalog, industry, raw)
                    if rc.get("year") is None or not rc.get("period"):
                        continue
                    coerced.append(rc)
                    raw_rows.append(raw)

                if not coerced:
                    continue
                sanitized_url = (
                    f"{API_ENDPOINT}?series={sid}&start={y_start}&end={y_end}"
                )
                last_url = sanitized_url
                meta = {
                    "series_id": sid, "year_start": y_start, "year_end": y_end,
                    "data_points": len(coerced), "catalog": catalog,
                }
                ups = _upsert(
                    conn, coerced, raw_rows,
                    source_filename=API_SOURCE_FILENAME,
                    source_download_url=sanitized_url,
                    source_observed_at=obs,
                    source_run_metadata=meta,
                    task_id=task_id,
                    schedule_id=schedule_id,
                )
                rows_upserted[sid] = rows_upserted.get(sid, 0) + ups
                log.info("[%s] %d → %d upserted", sid, len(coerced), ups)

    return rows_seen, rows_upserted, source_observed_at, last_url


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    today = datetime.now(timezone.utc).date()
    parser.add_argument(
        "--api", action="store_true",
        help="Use BLS Public Data API instead of bulk flat files. "
             "Useful for one-off / delta queries against specific series.",
    )
    parser.add_argument(
        "--start-year", type=int, default=today.year - DEFAULT_API_LOOKBACK_YEARS,
        help="Inclusive start year (API mode only).",
    )
    parser.add_argument(
        "--end-year", type=int, default=today.year,
        help="Inclusive end year (API mode only).",
    )
    parser.add_argument(
        "--series", nargs="+", default=list(FREIGHT_PPI_SERIES_IDS),
        help="Override series list (API mode only).",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch + parse only, no DB writes.")
    args = parser.parse_args()

    task_id = os.environ.get("TRIGGER_TASK_ID")
    schedule_id = os.environ.get("TRIGGER_SCHEDULE_ID")

    db_url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ.get("DEX_DB_URL_POOLED")
    if not args.dry_run and not db_url:
        log.error("DEX_DB_URL_DIRECT or DEX_DB_URL_POOLED must be set "
                  "(or pass --dry-run).")
        return 2

    api_key: str | None = None
    if args.api:
        api_key = os.environ.get("BLS_API_KEY") or None
        if not api_key:
            log.warning(
                "BLS_API_KEY not set; API mode will run with reduced limits."
            )

    conn: psycopg.Connection | None = None
    if not args.dry_run:
        conn = psycopg.connect(db_url, autocommit=False)

    rows_seen: dict[str, int] = {}
    rows_upserted: dict[str, int] = {}
    source_observed_at: datetime | None = None
    last_url: str | None = None
    run_id: str | None = None
    status = "succeeded"
    err: str | None = None

    try:
        if conn is not None:
            run_id = _start_run(
                conn,
                series_ids=list(args.series) if args.api else None,
                start_year=args.start_year if args.api else None,
                end_year=args.end_year if args.api else None,
                source_filename=API_SOURCE_FILENAME if args.api else "bulk",
                task_id=task_id, schedule_id=schedule_id,
            )

        with httpx.Client(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        ) as client:
            if args.api:
                if args.start_year > args.end_year:
                    log.error("--start-year must be <= --end-year")
                    return 2
                rows_seen, rows_upserted, source_observed_at, last_url = _ingest_api(
                    conn, client, api_key,
                    list(args.series), args.start_year, args.end_year,
                    dry_run=args.dry_run,
                    task_id=task_id, schedule_id=schedule_id,
                )
            else:
                rows_seen, rows_upserted, source_observed_at, last_url = _ingest_bulk(
                    conn, client,
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
                log.exception("rollback failed")

    finally:
        if conn is not None and run_id is not None:
            seen_payload = {"by_series": rows_seen, "total": sum(rows_seen.values())}
            ups_payload = {"by_series": rows_upserted,
                           "total": sum(rows_upserted.values())}
            _finish_run(
                conn, run_id, status,
                seen_payload, ups_payload,
                source_observed_at, last_url, err,
            )
            conn.close()

    log.info("done. status=%s mode=%s rows_seen=%d",
             status, "api" if args.api else "bulk", sum(rows_seen.values()))
    return 0 if status == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())

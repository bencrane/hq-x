#!/usr/bin/env python3
"""Census USA Trade Online — exports by port and HS commodity, monthly raw ingest.

Source:
    U.S. Census Bureau International Trade API.
    Endpoint: https://api.census.gov/data/timeseries/intltrade/exports/porths
    Format: JSON 2D array (header row + data rows).
    Frequency: monthly; Census publishes at ~6–7 week lag from end-of-month.
    Variables: 28 total at this endpoint (verified via /variables.json).
                We request 23 — all data-bearing fields except CTY_CODE /
                CTY_NAME / time. Country dimension intentionally omitted —
                see companion imports script for the rationale.

Companion to scripts/run_census_intltrade_imports_porths_ingest.py. Same
shape, exports-side variable taxonomy:
  - E_COMMODITY* instead of I_COMMODITY*
  - ALL_VAL_MO/YR replaces GEN_VAL_MO/YR (no consumption distinction)
  - No DUTY (no U.S. export duty), no CON_VAL (consumption is imports-only)
  - No quantity columns at this endpoint (GEN_QY*/ALL_QY* live on /exports/hs)

Auth:
    `CENSUS_API_KEY` env var. Free, instant registration at
    https://api.census.gov/data/key_signup.html.

Idempotency:
    INSERT ... ON CONFLICT (port, e_commodity, year, month) DO UPDATE WHERE
    row IS DISTINCT FROM EXCLUDED. Census revises the most-recent ~3 months
    of data on each release; revisions land cleanly via UPDATE.

Audit:
    ops.census_intltrade_exports_porths_ingest_runs — one row per invocation.

Coverage / volume:
    Default grain: HS6, ~250k rows/month nationwide (no CTY).
    Default rolling window: --months 24.

Usage:
    PYTHONPATH=. doppler run -- python3 scripts/run_census_intltrade_exports_porths_ingest.py
    PYTHONPATH=. doppler run -- python3 scripts/run_census_intltrade_exports_porths_ingest.py --start-year 2020 --start-month 1
    PYTHONPATH=. doppler run -- python3 scripts/run_census_intltrade_exports_porths_ingest.py --comm-lvl HS2 --months 60
    PYTHONPATH=. doppler run -- python3 scripts/run_census_intltrade_exports_porths_ingest.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import urllib.parse
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable

import httpx
import psycopg
from psycopg.types.json import Jsonb


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

PROVIDER = "census_intltrade_exports_porths"
USER_AGENT = "data-engine-x-api/census-intltrade-exports-porths-ingest"
ENDPOINT = "https://api.census.gov/data/timeseries/intltrade/exports/porths"
SOURCE_FILENAME = "data/timeseries/intltrade/exports/porths"

DEFAULT_COMM_LVL = "HS6"
DEFAULT_LOOKBACK_MONTHS = 24
DEFAULT_END_LAG_DAYS = 75
DB_BATCH_SIZE = 5_000

# All confirmed-exposed at exports/porths per
# https://api.census.gov/data/timeseries/intltrade/exports/porths/variables.json
# (28 total — we request 23, omitting CTY_CODE/CTY_NAME for port-rollup
# grain and `time` which is the filter param).
CENSUS_VARS: tuple[str, ...] = (
    "PORT", "PORT_NAME",
    "E_COMMODITY", "E_COMMODITY_LDESC", "E_COMMODITY_SDESC",
    "COMM_LVL", "SUMMARY_LVL", "SUMMARY_LVL2", "LAST_UPDATE",
    "ALL_VAL_MO", "ALL_VAL_YR",
    "AIR_VAL_MO", "AIR_VAL_YR",
    "VES_VAL_MO", "VES_VAL_YR",
    "CNT_VAL_MO", "CNT_VAL_YR",
    "AIR_WGT_MO", "AIR_WGT_YR",
    "VES_WGT_MO", "VES_WGT_YR",
    "CNT_WGT_MO", "CNT_WGT_YR",
)

CENSUS_TO_DB: dict[str, str] = {v: v.lower() for v in CENSUS_VARS}

NUMERIC_COLS: frozenset[str] = frozenset({
    "all_val_mo", "all_val_yr",
    "air_val_mo", "air_val_yr",
    "ves_val_mo", "ves_val_yr",
    "cnt_val_mo", "cnt_val_yr",
    "air_wgt_mo", "air_wgt_yr",
    "ves_wgt_mo", "ves_wgt_yr",
    "cnt_wgt_mo", "cnt_wgt_yr",
})

TYPED_COLS: tuple[str, ...] = (
    "port", "e_commodity", "year", "month",
    "port_name",
    "e_commodity_ldesc", "e_commodity_sdesc",
    "comm_lvl", "summary_lvl", "summary_lvl2", "last_update",
    "all_val_mo", "all_val_yr",
    "air_val_mo", "air_val_yr",
    "ves_val_mo", "ves_val_yr",
    "cnt_val_mo", "cnt_val_yr",
    "air_wgt_mo", "air_wgt_yr",
    "ves_wgt_mo", "ves_wgt_yr",
    "cnt_wgt_mo", "cnt_wgt_yr",
)

PK_COLS: tuple[str, ...] = ("port", "e_commodity", "year", "month")


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("census_intltrade_exports_porths_ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #


def _fetch_month(
    client: httpx.Client,
    api_key: str,
    year: int,
    month: int,
    comm_lvl: str,
) -> tuple[list[list[str]] | None, dict[str, str], str]:
    """One request → entire month's port × commodity rollup.

    Returns (body, headers, sanitized_url). body is None when Census returns
    HTTP 204 (month not yet published).
    """
    params: list[tuple[str, str]] = [
        ("get", ",".join(CENSUS_VARS)),
        ("time", f"{year}-{month:02d}"),
        ("COMM_LVL", comm_lvl),
        ("key", api_key),
    ]
    sanitized = [(k, v) for k, v in params if k != "key"]
    sanitized_url = f"{ENDPOINT}?{urllib.parse.urlencode(sanitized)}"

    r = client.get(ENDPOINT, params=params, timeout=300)
    if r.status_code == 204:
        return None, dict(r.headers), sanitized_url
    r.raise_for_status()
    body = r.json()
    if not isinstance(body, list) or not body:
        raise RuntimeError(f"unexpected Census response shape: {type(body).__name__}")
    return body, dict(r.headers), sanitized_url


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


def _coerce_row(
    header: list[str],
    raw: list[str],
    year: int,
    month: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    raw_dict = dict(zip(header, raw))
    out: dict[str, Any] = {c: None for c in TYPED_COLS}
    out["year"] = year
    out["month"] = month
    for src_var, db_col in CENSUS_TO_DB.items():
        v = raw_dict.get(src_var)
        if v is None:
            continue
        s = str(v).strip()
        if s == "":
            continue
        if db_col in NUMERIC_COLS:
            try:
                out[db_col] = float(s)
            except (TypeError, ValueError):
                out[db_col] = None
        else:
            out[db_col] = s
    return out, raw_dict


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
    table = "entities.source_census_intltrade_exports_porths"
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
    comm_lvl: str,
    start_y: int,
    start_m: int,
    end_y: int,
    end_m: int,
    task_id: str | None,
    schedule_id: str | None,
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ops.census_intltrade_exports_porths_ingest_runs "
            "(status, source_filename, comm_lvl, "
            " start_year, start_month, end_year, end_month, "
            " task_id, schedule_id) "
            "VALUES ('running', %s, %s, %s, %s, %s, %s, %s, %s) RETURNING run_id",
            (SOURCE_FILENAME, comm_lvl, start_y, start_m, end_y, end_m,
             task_id, schedule_id),
        )
        run_id = cur.fetchone()[0]
        conn.commit()
    return run_id


def _finish_run(
    conn: psycopg.Connection,
    run_id: str,
    status: str,
    months_fetched: int,
    rows_seen: int,
    rows_upserted: int,
    source_observed_at: datetime | None,
    source_download_url: str | None,
    error: str | None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE ops.census_intltrade_exports_porths_ingest_runs SET "
            "  status = %s, completed_at = now(), "
            "  months_fetched = %s, rows_seen = %s, rows_upserted = %s, "
            "  source_observed_at = %s, source_download_url = %s, "
            "  error_text = %s "
            "WHERE run_id = %s",
            (status, months_fetched, rows_seen, rows_upserted,
             source_observed_at, source_download_url, error, run_id),
        )
        conn.commit()


# --------------------------------------------------------------------------- #
# Window resolution
# --------------------------------------------------------------------------- #


def _months_in_range(
    start_y: int, start_m: int, end_y: int, end_m: int,
) -> Iterable[tuple[int, int]]:
    y, m = start_y, start_m
    while (y, m) <= (end_y, end_m):
        yield y, m
        m += 1
        if m > 12:
            y += 1
            m = 1


def _approx_latest_published(today: date, lag_days: int) -> tuple[int, int]:
    target = date.fromordinal(today.toordinal() - lag_days)
    return target.year, target.month


def _resolve_window(args) -> tuple[int, int, int, int]:
    today = date.today()
    if args.end_year and args.end_month:
        end_y, end_m = args.end_year, args.end_month
    else:
        end_y, end_m = _approx_latest_published(today, DEFAULT_END_LAG_DAYS)

    if args.start_year and args.start_month:
        start_y, start_m = args.start_year, args.start_month
    else:
        n = args.months or DEFAULT_LOOKBACK_MONTHS
        start_y, start_m = end_y, end_m
        for _ in range(n - 1):
            start_m -= 1
            if start_m <= 0:
                start_y -= 1
                start_m += 12

    return start_y, start_m, end_y, end_m


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--start-year", type=int)
    parser.add_argument("--start-month", type=int, choices=range(1, 13))
    parser.add_argument("--end-year", type=int)
    parser.add_argument("--end-month", type=int, choices=range(1, 13))
    parser.add_argument(
        "--months", type=int, default=None,
        help=f"Rolling-window length when --start/--end aren't both given. "
             f"Default {DEFAULT_LOOKBACK_MONTHS}.",
    )
    parser.add_argument(
        "--comm-lvl", default=DEFAULT_COMM_LVL,
        choices=("HS2", "HS4", "HS6", "HS10"),
        help=f"Census COMM_LVL filter. Default {DEFAULT_COMM_LVL}.",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch + parse only, no DB writes.")
    args = parser.parse_args()

    start_y, start_m, end_y, end_m = _resolve_window(args)
    log.info("window: %d-%02d → %d-%02d, comm_lvl=%s",
             start_y, start_m, end_y, end_m, args.comm_lvl)

    api_key = os.environ.get("CENSUS_API_KEY")
    if not api_key:
        log.error(
            "CENSUS_API_KEY env var must be set. Register free at "
            "https://api.census.gov/data/key_signup.html and store in Doppler."
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

    months_fetched = 0
    rows_seen = 0
    rows_upserted = 0
    source_observed_at: datetime | None = None
    last_url: str | None = None
    run_id: str | None = None
    status = "succeeded"
    err: str | None = None

    try:
        if conn is not None:
            run_id = _start_run(conn, args.comm_lvl,
                                start_y, start_m, end_y, end_m,
                                task_id, schedule_id)

        with httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        ) as client:
            for y, m in _months_in_range(start_y, start_m, end_y, end_m):
                body, headers, url = _fetch_month(
                    client, api_key, y, m, args.comm_lvl,
                )
                last_url = url
                obs = _response_date(headers)
                if obs and (source_observed_at is None or obs < source_observed_at):
                    source_observed_at = obs

                if body is None:
                    log.info("[%d-%02d] HTTP 204 — not yet published, skipping",
                             y, m)
                    continue
                if len(body) < 2:
                    log.warning("[%d-%02d] empty response body — skipping", y, m)
                    continue

                header = body[0]
                data_rows = body[1:]
                months_fetched += 1
                rows_seen += len(data_rows)
                log.info("[%d-%02d] %d rows", y, m, len(data_rows))

                if args.dry_run or conn is None:
                    continue

                coerced: list[dict[str, Any]] = []
                raw_rows: list[dict[str, Any]] = []
                for raw in data_rows:
                    typed, raw_dict = _coerce_row(header, raw, y, m)
                    if not typed.get("port") or not typed.get("e_commodity"):
                        log.warning("[%d-%02d] skipping row missing PK: %r",
                                    y, m, raw_dict)
                        continue
                    coerced.append(typed)
                    raw_rows.append(raw_dict)

                if not coerced:
                    continue

                meta = {
                    "year": y, "month": m,
                    "comm_lvl": args.comm_lvl,
                    "rows_in_response": len(data_rows),
                    "rows_ingestable": len(coerced),
                }
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
                log.info("[%d-%02d] upserted %d", y, m, ups)

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
                conn, run_id, status,
                months_fetched, rows_seen, rows_upserted,
                source_observed_at, last_url, err,
            )
            conn.close()

    log.info("done. status=%s months=%d rows_seen=%d rows_upserted=%d",
             status, months_fetched, rows_seen, rows_upserted)
    return 0 if status == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())

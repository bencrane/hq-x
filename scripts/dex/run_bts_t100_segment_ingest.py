#!/usr/bin/env python3
"""BTS T-100 Segment (All Carriers) — raw CSV ingest.

Source:
    https://www.transtats.bts.gov/DL_SelectFields.aspx
      T-100 Segment (All Carriers): gnoyr_VQ=FMG

    The download endpoint is ASP.NET WebForms — there is no static prezip URL.
    Each (year, period=All) request must:
      1. GET DL_SelectFields.aspx?gnoyr_VQ=FMG to seed __VIEWSTATE,
         __VIEWSTATEGENERATOR, __EVENTVALIDATION + cookies.
      2. POST back the form with cboYear / cboPeriod set + every column
         checkbox set + chkDownloadZip=on + btnDownload=Download.
      3. Server replies with application/zip (Content-Disposition:
         T_T100_SEGMENT_ALL_CARRIER_<ts>.zip), one CSV per response.

Idempotency:
    INSERT ... ON CONFLICT (year, month, airline_id, unique_carrier_entity,
    origin_airport_id, dest_airport_id, aircraft_type, aircraft_config, class,
    data_source) DO UPDATE ... WHERE row IS DISTINCT FROM EXCLUDED.

Audit:
    ops.bts_t100_ingest_runs — one row per invocation. rows_seen /
    rows_upserted are jsonb objects keyed by year.

Coverage:
    Years 1990–most-recent-released (BTS publishes monthly with ~4-6 month lag).
    Script defaults to "all available years" but takes --years to narrow.

Usage:
    PYTHONPATH=. doppler run -- python3 scripts/run_bts_t100_segment_ingest.py
    PYTHONPATH=. doppler run -- python3 scripts/run_bts_t100_segment_ingest.py --years 2024-2026
    PYTHONPATH=. doppler run -- python3 scripts/run_bts_t100_segment_ingest.py --years 2026
    PYTHONPATH=. doppler run -- python3 scripts/run_bts_t100_segment_ingest.py --dry-run
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import os
import re
import sys
import urllib.parse
import zipfile
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
import psycopg
from psycopg.types.json import Jsonb


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

PROVIDER = "bts_t100_segment"
DATASET = "segment_all_carriers"
USER_AGENT = "data-engine-x-api/bts-t100-ingest"
BASE_URL = "https://www.transtats.bts.gov/DL_SelectFields.aspx"
GNOYR_VQ = "FMG"
TABLE = "entities.source_bts_t100_segment"
BATCH_SIZE = 5_000

DEFAULT_YEAR_RANGE = (1990, 2030)  # narrowed at runtime by available years.
PERIOD = "All"  # one zip = all 12 months

# CSV column names from BTS DL_SelectFields.aspx (gnoyr_VQ=FMG), 2026-05-04.
# Order matters for both the form POST (each column must be checked) and for
# row_coercion (must match migration column order).
FORM_FIELD_NAMES: tuple[str, ...] = (
    "DEPARTURES_SCHEDULED", "DEPARTURES_PERFORMED",
    "PAYLOAD", "SEATS", "PASSENGERS", "FREIGHT", "MAIL",
    "DISTANCE", "RAMP_TO_RAMP", "AIR_TIME",
    "UNIQUE_CARRIER", "AIRLINE_ID", "UNIQUE_CARRIER_NAME",
    "UNIQUE_CARRIER_ENTITY", "REGION",
    "CARRIER", "CARRIER_NAME", "CARRIER_GROUP", "CARRIER_GROUP_NEW",
    "ORIGIN_AIRPORT_ID", "ORIGIN_AIRPORT_SEQ_ID", "ORIGIN_CITY_MARKET_ID",
    "ORIGIN", "ORIGIN_CITY_NAME", "ORIGIN_STATE_ABR", "ORIGIN_STATE_FIPS",
    "ORIGIN_STATE_NM", "ORIGIN_COUNTRY", "ORIGIN_COUNTRY_NAME", "ORIGIN_WAC",
    "DEST_AIRPORT_ID", "DEST_AIRPORT_SEQ_ID", "DEST_CITY_MARKET_ID",
    "DEST", "DEST_CITY_NAME", "DEST_STATE_ABR", "DEST_STATE_FIPS",
    "DEST_STATE_NM", "DEST_COUNTRY", "DEST_COUNTRY_NAME", "DEST_WAC",
    "AIRCRAFT_GROUP", "AIRCRAFT_TYPE", "AIRCRAFT_CONFIG",
    "YEAR", "QUARTER", "MONTH", "DISTANCE_GROUP",
    "CLASS", "DATA_SOURCE",
)

# Typed column order MUST match migration (excluding provenance).
TYPED_COLS: tuple[str, ...] = (
    "departures_scheduled", "departures_performed",
    "payload", "seats", "passengers", "freight", "mail",
    "distance", "ramp_to_ramp", "air_time",
    "unique_carrier", "airline_id", "unique_carrier_name",
    "unique_carrier_entity", "region",
    "carrier", "carrier_name", "carrier_group", "carrier_group_new",
    "origin_airport_id", "origin_airport_seq_id", "origin_city_market_id",
    "origin", "origin_city_name", "origin_state_abr", "origin_state_fips",
    "origin_state_nm", "origin_country", "origin_country_name", "origin_wac",
    "dest_airport_id", "dest_airport_seq_id", "dest_city_market_id",
    "dest", "dest_city_name", "dest_state_abr", "dest_state_fips",
    "dest_state_nm", "dest_country", "dest_country_name", "dest_wac",
    "aircraft_group", "aircraft_type", "aircraft_config",
    "year", "quarter", "month", "distance_group",
    "class", "data_source",
)

PK_COLS: tuple[str, ...] = (
    "year", "month", "airline_id", "unique_carrier_entity",
    "origin_airport_id", "dest_airport_id",
    "aircraft_type", "aircraft_config", "class", "data_source",
)

INT_COLS: frozenset[str] = frozenset({
    "airline_id", "carrier_group", "carrier_group_new",
    "origin_airport_id", "origin_airport_seq_id", "origin_city_market_id",
    "origin_state_fips", "origin_wac",
    "dest_airport_id", "dest_airport_seq_id", "dest_city_market_id",
    "dest_state_fips", "dest_wac",
    "aircraft_group", "aircraft_type", "aircraft_config",
    "year", "quarter", "month", "distance_group",
})

NUMERIC_COLS: frozenset[str] = frozenset({
    "departures_scheduled", "departures_performed",
    "payload", "seats", "passengers", "freight", "mail",
    "distance", "ramp_to_ramp", "air_time",
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
    return logging.getLogger("bts_t100_segment_ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# Download (ASP.NET WebForms POST flow)
# --------------------------------------------------------------------------- #


_VS_RE = {
    name: re.compile(r'name="%s"[^>]*value="([^"]*)"' % re.escape(name))
    for name in ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION")
}


def _fetch_form(client: httpx.Client) -> tuple[dict[str, str], list[int]]:
    """GET the form, return (viewstate dict, available years)."""
    url = f"{BASE_URL}?gnoyr_VQ={GNOYR_VQ}&QO_fu146_anzr="
    r = client.get(url, timeout=30)
    r.raise_for_status()
    html = r.text
    vs = {k: m.search(html).group(1) for k, m in _VS_RE.items() if m.search(html)}
    if "__VIEWSTATE" not in vs:
        raise RuntimeError(f"viewstate missing from {url}")
    m = re.search(r'<select[^>]*name="cboYear"[^>]*>(.*?)</select>', html, re.S)
    years: list[int] = []
    if m:
        years = sorted({int(v) for v in re.findall(r'value="(\d{4})"', m.group(1))})
    return vs, years


def _download_year(
    client: httpx.Client, year: int,
) -> tuple[bytes, dict[str, str]]:
    """POST the form for one year (period=All) and return (csv_bytes, headers).

    Re-seeds __VIEWSTATE on every call (TranStats invalidates after one POST).
    """
    vs, _ = _fetch_form(client)
    payload: list[tuple[str, str]] = [
        ("__EVENTTARGET", ""),
        ("__EVENTARGUMENT", ""),
        ("__VIEWSTATE", vs["__VIEWSTATE"]),
        ("__VIEWSTATEGENERATOR", vs["__VIEWSTATEGENERATOR"]),
        ("__EVENTVALIDATION", vs["__EVENTVALIDATION"]),
        ("cboYear", str(year)),
        ("cboPeriod", PERIOD),
        ("chkDownloadZip", "on"),
        ("chkAllVars", "on"),
        ("btnDownload", "Download"),
    ]
    payload.extend((f, "on") for f in FORM_FIELD_NAMES)

    url = f"{BASE_URL}?gnoyr_VQ={GNOYR_VQ}&QO_fu146_anzr="
    r = client.post(
        url,
        data=urllib.parse.urlencode(payload),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": url,
        },
        timeout=300,
    )
    r.raise_for_status()
    if "application/zip" not in (r.headers.get("Content-Type") or ""):
        raise RuntimeError(
            f"non-zip response for year={year}: "
            f"ct={r.headers.get('Content-Type')!r} body[:200]={r.content[:200]!r}"
        )
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        # Zip contains the data CSV plus a Documentation.csv (field descriptions).
        # Filter to the data file by prefix.
        names = [n for n in zf.namelist() if n.startswith("T_T100_")]
        if not names:
            raise RuntimeError(f"no T_T100_*.csv in zip for year={year}: names={zf.namelist()}")
        csv_bytes = zf.read(names[0])
    return csv_bytes, dict(r.headers)


def _parse_observed_at(headers: dict[str, str]) -> datetime | None:
    lm = headers.get("Last-Modified") or headers.get("last-modified")
    if not lm:
        return None
    try:
        dt = parsedate_to_datetime(lm)
        return dt.astimezone(timezone.utc) if dt else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Row coercion
# --------------------------------------------------------------------------- #


def _csv_to_db(header: str) -> str:
    return header.strip().lower()


def _coerce(raw: dict[str, str]) -> dict[str, Any]:
    """Map CSV header → db col, coerce types. Empty strings → None."""
    out: dict[str, Any] = {}
    for header, value in raw.items():
        col = _csv_to_db(header)
        v = (value or "").strip()
        if v == "":
            out[col] = None
        elif col in INT_COLS:
            try:
                out[col] = int(float(v))     # tolerate "10.0" style
            except (TypeError, ValueError):
                out[col] = None
        elif col in NUMERIC_COLS:
            try:
                out[col] = float(v)
            except (TypeError, ValueError):
                out[col] = None
        else:
            out[col] = v
    return out


# --------------------------------------------------------------------------- #
# Upsert
# --------------------------------------------------------------------------- #


def _upsert(
    conn: psycopg.Connection,
    rows: list[dict[str, Any]],
    raw_rows: list[dict[str, str]],
    *,
    source_filename: str,
    source_download_url: str,
    source_observed_at: datetime | None,
    source_run_metadata: dict[str, Any],
    task_id: str | None,
    schedule_id: str | None,
) -> int:
    """INSERT…ON CONFLICT…DO UPDATE in batches of BATCH_SIZE. Returns rows upserted."""
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
        f"{TABLE}.{c} IS DISTINCT FROM EXCLUDED.{c}" for c in update_cols
    )
    sql = (
        f"INSERT INTO {TABLE} ({','.join(all_cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({','.join(PK_COLS)}) DO UPDATE SET {set_clause} "
        f"WHERE {distinct_clause}"
    )

    upserted = 0
    with conn.cursor() as cur:
        for i in range(0, len(rows), BATCH_SIZE):
            chunk = rows[i:i + BATCH_SIZE]
            chunk_raw = raw_rows[i:i + BATCH_SIZE]
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
    years: tuple[int, int] | None,
    task_id: str | None,
    schedule_id: str | None,
) -> int:
    yrange = f"[{years[0]},{years[1] + 1})" if years else None
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ops.bts_t100_ingest_runs "
            "(status, dataset, years_requested, task_id, schedule_id) "
            "VALUES ('running', %s, %s::int4range, %s, %s) RETURNING id",
            (DATASET, yrange, task_id, schedule_id),
        )
        run_id = cur.fetchone()[0]
        conn.commit()
    return run_id


def _finish_run(
    conn: psycopg.Connection,
    run_id: int,
    status: str,
    rows_seen: dict[str, int],
    rows_upserted: dict[str, int],
    source_observed_at: datetime | None,
    error: str | None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE ops.bts_t100_ingest_runs SET "
            "  status = %s, finished_at = now(), "
            "  rows_seen = %s, rows_upserted = %s, "
            "  source_observed_at = %s, error_message = %s "
            "WHERE id = %s",
            (status, Jsonb(rows_seen), Jsonb(rows_upserted),
             source_observed_at, error, run_id),
        )
        conn.commit()


# --------------------------------------------------------------------------- #
# Per-year pipeline
# --------------------------------------------------------------------------- #


def _ingest_year(
    conn: psycopg.Connection | None,
    client: httpx.Client,
    year: int,
    *,
    dry_run: bool,
    max_rows: int | None,
    task_id: str | None,
    schedule_id: str | None,
) -> tuple[int, int, datetime | None]:
    """Returns (rows_seen, rows_upserted, source_observed_at_for_year)."""
    log.info("[t100 %d] downloading…", year)
    csv_bytes, headers = _download_year(client, year)
    observed = _parse_observed_at(headers)

    text = csv_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    raw_rows = list(reader)
    if max_rows is not None:
        raw_rows = raw_rows[:max_rows]
    coerced = [_coerce(r) for r in raw_rows]
    rows_in_year = len(raw_rows)

    # Filter rows missing any PK column (rare in T-100 but possible at the
    # 1990-era boundaries where some carriers report partial dimensions).
    keep_pairs = [
        (rc, raw)
        for rc, raw in zip(coerced, raw_rows)
        if all(rc.get(c) is not None for c in PK_COLS)
    ]
    skipped = rows_in_year - len(keep_pairs)
    if skipped:
        log.warning("[t100 %d] skipping %d rows missing PK columns %s",
                    year, skipped, PK_COLS)
    coerced = [c for c, _ in keep_pairs]
    raw_rows = [r for _, r in keep_pairs]
    log.info("[t100 %d] parsed %d rows (%d ingestable)",
             year, rows_in_year, len(coerced))

    if dry_run or conn is None:
        log.info("[t100 %d] dry-run: skipping upsert", year)
        return rows_in_year, 0, observed

    filename = headers.get("Content-Disposition", "")
    if "filename=" in filename:
        filename = filename.split("filename=", 1)[1].strip().strip('"')
    else:
        filename = f"T_T100_SEGMENT_ALL_CARRIER_{year}.csv"
    download_url = (
        f"{BASE_URL}?gnoyr_VQ={GNOYR_VQ}&cboYear={year}&cboPeriod={PERIOD}"
    )
    run_meta = {
        "year": year,
        "period": PERIOD,
        "csv_bytes": len(csv_bytes),
        "rows_in_csv": len(raw_rows),
        "tranStats_filename": filename,
    }
    upserted = _upsert(
        conn, coerced, raw_rows,
        source_filename=filename,
        source_download_url=download_url,
        source_observed_at=observed,
        source_run_metadata=run_meta,
        task_id=task_id,
        schedule_id=schedule_id,
    )
    log.info("[t100 %d] upserted %d rows", year, upserted)
    return rows_in_year, upserted, observed


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _parse_years(raw: str | None) -> tuple[int, int] | None:
    if not raw:
        return None
    if "-" in raw:
        a, b = raw.split("-", 1)
        return int(a), int(b)
    y = int(raw)
    return y, y


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--years", help="YYYY or YYYY-YYYY (inclusive). Default: all available.")
    parser.add_argument("--dry-run", action="store_true",
                        help="parse only, no DB writes")
    parser.add_argument("--max-rows", type=int, default=None,
                        help="stop after this many rows per year (smoke test)")
    args = parser.parse_args(argv)

    task_id = os.environ.get("TRIGGER_TASK_ID") or os.environ.get("MODAL_TASK_ID")
    schedule_id = os.environ.get("TRIGGER_SCHEDULE_ID") or os.environ.get("MODAL_SCHEDULE_ID")

    db_url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ.get("DEX_DB_URL_POOLED")
    if not args.dry_run and not db_url:
        log.error("DEX_DB_URL_DIRECT or DEX_DB_URL_POOLED must be set "
                  "(or pass --dry-run).")
        return {"status": "error", "error": "no DB URL"}

    conn: psycopg.Connection | None = None
    if not args.dry_run:
        conn = psycopg.connect(db_url, autocommit=False)

    rows_seen: dict[str, int] = {}
    rows_upserted: dict[str, int] = {}
    earliest_observed: datetime | None = None
    run_id: int | None = None
    status = "success"
    err: str | None = None

    try:
        with httpx.Client(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        ) as client:
            _, available = _fetch_form(client)
            log.info("[t100] available years on TranStats: %s", available)

            yrange = _parse_years(args.years)
            if yrange:
                target_years = [y for y in available if yrange[0] <= y <= yrange[1]]
            else:
                target_years = list(available)
            if not target_years:
                log.warning("[t100] no years matched range %s — nothing to do", yrange)
                return {"status": "success", "rows_seen": {}, "rows_upserted": {}}

            if conn is not None:
                run_id = _start_run(
                    conn,
                    (target_years[0], target_years[-1]),
                    task_id, schedule_id,
                )

            for year in target_years:
                seen, upserted, obs = _ingest_year(
                    conn, client, year,
                    dry_run=args.dry_run,
                    max_rows=args.max_rows,
                    task_id=task_id, schedule_id=schedule_id,
                )
                rows_seen[str(year)] = seen
                rows_upserted[str(year)] = upserted
                if obs and (earliest_observed is None or obs < earliest_observed):
                    earliest_observed = obs

    except Exception as exc:
        status = "error"
        err = f"{type(exc).__name__}: {exc}"
        log.exception("ingest failed")
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                log.exception("rollback failed during error cleanup")

    finally:
        if conn is not None and run_id is not None:
            _finish_run(conn, run_id, status, rows_seen, rows_upserted,
                        earliest_observed, err)
            conn.close()

    log.info("done. status=%s rows_seen=%s rows_upserted=%s",
             status, rows_seen, rows_upserted)
    return {
        "status": status,
        "rows_seen": rows_seen,
        "rows_upserted": rows_upserted,
        "error": err,
    }


if __name__ == "__main__":
    result = main()
    sys.exit(0 if result.get("status") == "success" else 1)

#!/usr/bin/env python3
"""NHC active tropical cyclones — advisory-grain raw ingest.

Source:
    NOAA / NWS National Hurricane Center.
    Endpoint: https://www.nhc.noaa.gov/CurrentStorms.json
    No auth (User-Agent header included).

Idempotency:
    INSERT ... ON CONFLICT (storm_id, last_update) DO UPDATE WHERE row IS
    DISTINCT FROM EXCLUDED. Re-pulls between advisories absorb cleanly;
    each new advisory cycle (every ~6 hours during active storms) creates
    a new row.

Audit:
    ops.nhc_active_storms_ingest_runs — one row per invocation. Empty
    `activeStorms[]` during quiet periods is the steady state — run row
    records storms_seen=0.

Usage:
    PYTHONPATH=. doppler run -- python3 scripts/run_nhc_active_storms_ingest.py
    PYTHONPATH=. doppler run -- python3 scripts/run_nhc_active_storms_ingest.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
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

PROVIDER = "nhc_active_storms"
USER_AGENT = (
    "data-engine-x-api/nhc-active-storms-ingest "
    "(contact: tools@substrate.build)"
)
ENDPOINT = "https://www.nhc.noaa.gov/CurrentStorms.json"
SOURCE_FILENAME = "CurrentStorms.json"

DB_BATCH_SIZE = 100  # at most a few dozen storms ever active simultaneously

# NHC field → DB column. Explicit mapping makes schema drift loud.
NHC_FIELD_MAP: dict[str, str] = {
    "id": "storm_id",
    "binNumber": "bin_number",
    "name": "name",
    "classification": "classification",
    "intensity": "intensity_mph",
    "pressure": "pressure_mbar",
    "latitude": "latitude_text",
    "longitude": "longitude_text",
    "latitudeNumeric": "latitude",
    "longitudeNumeric": "longitude",
    "movementDir": "movement_dir",
    "movementSpeed": "movement_speed_mph",
    "lastUpdate": "last_update",
    "publicAdvisory": "public_advisory",
    "forecastAdvisory": "forecast_advisory",
    "forecastTrack": "forecast_track",
    "forecastCone": "forecast_cone",
    "initialWindExtent": "initial_wind_extent",
    "bestTrack": "best_track",
    "windWatchesWarnings": "wind_watches_warnings",
}

INT_COLS: frozenset[str] = frozenset({
    "intensity_mph", "pressure_mbar", "movement_speed_mph",
})
FLOAT_COLS: frozenset[str] = frozenset({"latitude", "longitude"})
TIMESTAMP_COLS: frozenset[str] = frozenset({"last_update"})
JSONB_COLS: frozenset[str] = frozenset({
    "public_advisory", "forecast_advisory", "forecast_track",
    "forecast_cone", "initial_wind_extent", "best_track",
    "wind_watches_warnings",
})

TYPED_COLS: tuple[str, ...] = tuple(NHC_FIELD_MAP.values())
PK_COLS: tuple[str, str] = ("storm_id", "last_update")


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("nhc_active_storms_ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #


def _fetch(client: httpx.Client) -> tuple[list[dict[str, Any]], dict[str, str]]:
    r = client.get(ENDPOINT, timeout=60)
    r.raise_for_status()
    body = r.json()
    storms = body.get("activeStorms") or []
    return storms, dict(r.headers)


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


def _to_ts(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc) if v.tzinfo else v.replace(tzinfo=timezone.utc)
    s = str(v).strip()
    if not s:
        return None
    # NHC formats: ISO 8601 with Z or offset, or sometimes 'YYYY-MM-DDTHH:MM:SS'.
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%a, %d %b %Y %H:%M:%S %Z"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    log.warning("could not parse timestamp: %r", v)
    return None


def _to_int(v: Any) -> int | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return int(float(s))  # tolerate '125.0'
    except (TypeError, ValueError):
        return None


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _coerce(storm: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {c: None for c in TYPED_COLS}
    for src_key, db_col in NHC_FIELD_MAP.items():
        v = storm.get(src_key)
        if v is None:
            continue
        if db_col in JSONB_COLS:
            out[db_col] = v
        elif db_col in TIMESTAMP_COLS:
            out[db_col] = _to_ts(v)
        elif db_col in INT_COLS:
            out[db_col] = _to_int(v)
        elif db_col in FLOAT_COLS:
            out[db_col] = _to_float(v)
        else:
            s = str(v).strip()
            out[db_col] = s if s else None
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
    table = "entities.source_nhc_active_storms"
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
    task_id: str | None,
    schedule_id: str | None,
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ops.nhc_active_storms_ingest_runs "
            "(status, source_filename, source_download_url, "
            " task_id, schedule_id) "
            "VALUES ('running', %s, %s, %s, %s) RETURNING run_id",
            (SOURCE_FILENAME, ENDPOINT, task_id, schedule_id),
        )
        run_id = cur.fetchone()[0]
        conn.commit()
    return run_id


def _finish_run(
    conn: psycopg.Connection,
    run_id: str,
    status: str,
    storms_seen: int,
    rows_seen: int,
    rows_upserted: int,
    source_observed_at: datetime | None,
    error: str | None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE ops.nhc_active_storms_ingest_runs SET "
            "  status = %s, completed_at = now(), "
            "  storms_seen = %s, rows_seen = %s, rows_upserted = %s, "
            "  source_observed_at = %s, error_text = %s "
            "WHERE run_id = %s",
            (status, storms_seen, rows_seen, rows_upserted,
             source_observed_at, error, run_id),
        )
        conn.commit()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch + parse only, no DB writes.",
    )
    args = parser.parse_args()

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

    storms_seen = 0
    rows_seen = 0
    rows_upserted = 0
    source_observed_at: datetime | None = None
    run_id: str | None = None
    status = "succeeded"
    err: str | None = None

    try:
        if conn is not None:
            run_id = _start_run(conn, task_id, schedule_id)

        with httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        ) as client:
            storms, headers = _fetch(client)
            source_observed_at = _response_date(headers)
            storms_seen = len(storms)
            log.info("NHC returned %d active storms", storms_seen)

            coerced: list[dict[str, Any]] = []
            raw_rows: list[dict[str, Any]] = []
            for s in storms:
                rc = _coerce(s)
                if not rc.get("storm_id") or rc.get("last_update") is None:
                    log.warning("skipping storm missing PK: %r",
                                {k: s.get(k) for k in ("id", "name", "lastUpdate")})
                    continue
                coerced.append(rc)
                raw_rows.append(s)

            rows_seen = len(coerced)
            log.info("ingestable: %d (skipped %d)",
                     rows_seen, storms_seen - rows_seen)

            if args.dry_run or conn is None:
                log.info("dry-run: skipping upsert")
            elif coerced:
                meta = {"storms_in_response": storms_seen}
                rows_upserted = _upsert(
                    conn, coerced, raw_rows,
                    source_filename=SOURCE_FILENAME,
                    source_download_url=ENDPOINT,
                    source_observed_at=source_observed_at,
                    source_run_metadata=meta,
                    task_id=task_id,
                    schedule_id=schedule_id,
                )
                log.info("upserted %d rows", rows_upserted)

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
                conn, run_id, status, storms_seen, rows_seen, rows_upserted,
                source_observed_at, err,
            )
            conn.close()

    log.info("done. status=%s storms=%d rows_upserted=%d",
             status, storms_seen, rows_upserted)
    return 0 if status == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())

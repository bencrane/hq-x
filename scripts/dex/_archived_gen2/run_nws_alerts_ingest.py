#!/usr/bin/env python3
"""NOAA NWS active weather alerts — near-real-time freight-disruption ingest.

Source:
    NWS public API — https://api.weather.gov/alerts/active
    Format: GeoJSON FeatureCollection of active alerts.
    No auth (a User-Agent header is requested).

Idempotency:
    INSERT ... ON CONFLICT (alert_id) DO UPDATE WHERE row IS DISTINCT FROM
    EXCLUDED. Re-pulls within an alert's lifetime overwrite if anything
    changed; cancellations / updates appear as new alerts (new URN) that
    reference the prior alert in the `references` jsonb.

Audit:
    ops.nws_alerts_ingest_runs — one row per invocation.

Usage:
    PYTHONPATH=. doppler run -- python3 scripts/run_nws_alerts_ingest.py
    PYTHONPATH=. doppler run -- python3 scripts/run_nws_alerts_ingest.py --dry-run
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

PROVIDER = "nws_alerts"
USER_AGENT = (
    "data-engine-x-api/nws-alerts-ingest "
    "(contact: tools@substrate.build)"
)
ENDPOINT = "https://api.weather.gov/alerts/active"
SOURCE_FILENAME = "alerts/active"

DB_BATCH_SIZE = 500

# 1:1 mapping NWS API field → DB column. Surface high-value CAP fields as
# typed columns; the full feature stays in raw_source_row jsonb.
PROP_MAP: dict[str, str] = {
    "id": "alert_id",
    "event": "event",
    "severity": "severity",
    "certainty": "certainty",
    "urgency": "urgency",
    "category": "category",
    "status": "status",
    "messageType": "message_type",
    "response": "response",
    "headline": "headline",
    "description": "description",
    "instruction": "instruction",
    "areaDesc": "area_desc",
    "sender": "sender",
    "senderName": "sender_name",
    "sent": "sent",
    "effective": "effective",
    "onset": "onset",
    "expires": "expires",
    "ends": "ends",
    "affectedZones": "affected_zones",
    "geocode": "geocode",
    "parameters": "parameters",
    "references": "references",
}

TIMESTAMP_COLS: frozenset[str] = frozenset({
    "sent", "effective", "onset", "expires", "ends",
})
JSONB_COLS: frozenset[str] = frozenset({
    "affected_zones", "geocode", "parameters", "references", "geometry",
})

TYPED_COLS: tuple[str, ...] = tuple(PROP_MAP.values()) + ("geometry",)
PK_COLS: tuple[str] = ("alert_id",)


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("nws_alerts_ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #


def _fetch(client: httpx.Client) -> tuple[list[dict[str, Any]], dict[str, str]]:
    r = client.get(ENDPOINT, timeout=120)
    r.raise_for_status()
    body = r.json()
    features = body.get("features") or []
    return features, dict(r.headers)


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
    if not v:
        return None
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc) if v.tzinfo else v.replace(tzinfo=timezone.utc)
    s = str(v).strip()
    if not s:
        return None
    # NWS emits ISO 8601 with offset, e.g. '2026-05-05T12:34:00-05:00'.
    # datetime.fromisoformat handles this in modern Python.
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _coerce(feature: dict[str, Any]) -> dict[str, Any]:
    props = feature.get("properties") or {}
    out: dict[str, Any] = {c: None for c in TYPED_COLS}
    for src_key, db_col in PROP_MAP.items():
        v = props.get(src_key)
        if v is None:
            out[db_col] = None
        elif db_col in TIMESTAMP_COLS:
            out[db_col] = _to_ts(v)
        elif db_col in JSONB_COLS:
            out[db_col] = v
        else:
            s = str(v).strip()
            out[db_col] = s if s else None
    out["geometry"] = feature.get("geometry")
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
    table = "entities.source_nws_alerts"
    # `references` is a reserved word; quote it.
    typed_cols_quoted = tuple(f'"{c}"' if c == "references" else c for c in TYPED_COLS)
    all_cols = (
        *typed_cols_quoted,
        "raw_source_row", "source_provider", "source_filename",
        "source_download_url", "source_observed_at", "source_run_metadata",
        "source_task_id", "source_schedule_id",
    )
    placeholders = ",".join(["%s"] * len(all_cols))
    update_cols = [c for c in typed_cols_quoted
                   if c.strip('"') not in PK_COLS] + [
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
                p.append(ENDPOINT)
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
            "INSERT INTO ops.nws_alerts_ingest_runs "
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
    rows_seen: int,
    rows_upserted: int,
    source_observed_at: datetime | None,
    error: str | None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE ops.nws_alerts_ingest_runs SET "
            "  status = %s, completed_at = now(), "
            "  rows_seen = %s, rows_upserted = %s, "
            "  source_observed_at = %s, error_text = %s "
            "WHERE run_id = %s",
            (status, rows_seen, rows_upserted, source_observed_at,
             error, run_id),
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
            headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"},
            follow_redirects=True,
        ) as client:
            features, headers = _fetch(client)
            source_observed_at = _response_date(headers)
            rows_seen = len(features)
            log.info("NWS returned %d active alerts", rows_seen)

            coerced: list[dict[str, Any]] = []
            raw_rows: list[dict[str, Any]] = []
            for f in features:
                rc = _coerce(f)
                if not rc.get("alert_id"):
                    log.warning("skipping feature with no properties.id: %r",
                                f.get("id"))
                    continue
                coerced.append(rc)
                raw_rows.append(f)

            log.info("ingestable: %d (skipped %d)",
                     len(coerced), rows_seen - len(coerced))

            if args.dry_run or conn is None:
                log.info("dry-run: skipping upsert")
            elif coerced:
                meta = {"alerts_in_response": rows_seen}
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
                conn, run_id, status, rows_seen, rows_upserted,
                source_observed_at, err,
            )
            conn.close()

    log.info("done. status=%s alerts=%d rows_upserted=%d",
             status, rows_seen, rows_upserted)
    return 0 if status == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())

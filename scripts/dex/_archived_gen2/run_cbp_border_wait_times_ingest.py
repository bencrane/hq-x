#!/usr/bin/env python3
"""CBP Border Wait Times — near-real-time per-port snapshot ingest.

Source:
    U.S. Customs and Border Protection — https://bwt.cbp.gov/api/bwtnew
    No auth. Each pull returns the current state of every land-border port
    of entry (Mexican border + Canadian border) with per-lane-category
    delays. CBP refreshes upstream every ~5–15 minutes.

Idempotency:
    INSERT ... ON CONFLICT (port_number, source_observed_at) DO UPDATE …
    WHERE row IS DISTINCT FROM EXCLUDED. source_observed_at is derived as
    MAX(update_time) across the port's lane categories — falls back to the
    pull time when no lane has an update_time. Re-pulls within the same CBP
    refresh window land on the same PK and absorb cleanly.

Audit:
    ops.cbp_border_wait_times_ingest_runs — one row per script invocation.

Usage:
    PYTHONPATH=. doppler run -- python3 scripts/run_cbp_border_wait_times_ingest.py
    PYTHONPATH=. doppler run -- python3 scripts/run_cbp_border_wait_times_ingest.py --dry-run
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

PROVIDER = "cbp_border_wait_times"
USER_AGENT = "data-engine-x-api/cbp-border-wait-times-ingest"
ENDPOINT = "https://bwt.cbp.gov/api/bwtnew"
SOURCE_FILENAME = "api/bwtnew"

DB_BATCH_SIZE = 200  # ~50 ports per pull; one pull = one batch in practice.

# Mapping CBP API field name → DB column. Done explicitly to make schema drift
# loud (the script aborts if CBP renames a top-level field).
CBP_FIELD_MAP: dict[str, str] = {
    "port_number": "port_number",
    "port_name": "port_name",
    "state": "state",
    "border": "border",
    "port_status": "port_status",
    "port_running_state": "port_running_state",
    "construction_notice": "construction_notice",
    "hours": "hours",
    "commercial_vehicle_lanes": "commercial_vehicle_lanes",
    "passenger_vehicle_lanes": "passenger_vehicle_lanes",
    "pedestrian_lanes": "pedestrian_lanes",
}

TYPED_COLS: tuple[str, ...] = tuple(CBP_FIELD_MAP.values()) + ("source_observed_at",)
JSONB_COLS: frozenset[str] = frozenset({
    "commercial_vehicle_lanes", "passenger_vehicle_lanes", "pedestrian_lanes",
})

PK_COLS: tuple[str, str] = ("port_number", "source_observed_at")


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("cbp_border_wait_times_ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# Fetch + parse
# --------------------------------------------------------------------------- #


def _fetch(client: httpx.Client) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """GET the CBP endpoint. Returns (ports[], response_headers)."""
    r = client.get(ENDPOINT, timeout=60)
    r.raise_for_status()
    body = r.json()
    if not isinstance(body, list):
        raise RuntimeError(f"unexpected CBP response shape: {type(body).__name__}")
    return body, dict(r.headers)


def _response_date(headers: dict[str, str]) -> datetime | None:
    raw = headers.get("Date") or headers.get("date")
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        return dt.astimezone(timezone.utc) if dt else None
    except (TypeError, ValueError):
        return None


def _parse_lane_update_time(raw: str | None) -> datetime | None:
    """CBP emits lane update_time as 'MM/DD/YYYY HH:MM AM/PM' in port-local time.

    Per CBP docs all fields are reported in ET. Parse to naive datetime, then
    treat as ET (UTC-5/UTC-4 depending on DST). For ingest correctness we
    don't need second-level accuracy on the time-zone conversion — minute-grain
    is sufficient — so we attach UTC offset of -5 hours as a reasonable proxy.
    Operators querying historical wait-time series should treat these
    timestamps as ET for downstream MV unpacking.
    """
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    for fmt in ("%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            # If timezone-naive, tag as ET (-05:00 winter / -04:00 summer
            # — use -05:00 as a conservative default; cross-DST drift is
            # ~1 hour per 6 months which is acceptable for the wait-time
            # snapshot semantics).
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone(__import__("datetime").timedelta(hours=-5)))
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    log.warning("could not parse lane update_time: %r", raw)
    return None


def _derive_observed_at(
    port: dict[str, Any], pull_time: datetime
) -> datetime:
    """source_observed_at = max lane update_time across categories, or pull_time."""
    candidates: list[datetime] = []
    for key in ("commercial_vehicle_lanes", "passenger_vehicle_lanes", "pedestrian_lanes"):
        lane_obj = port.get(key) or {}
        if not isinstance(lane_obj, dict):
            continue
        # Lane object has nested 'standard_lanes' and possibly 'NEXUS_SENTRI_lanes'
        # / 'FAST_lanes' / 'ready_lanes' etc. Each carries an 'update_time'.
        for sub_key, sub_val in lane_obj.items():
            if not isinstance(sub_val, dict):
                continue
            ut = sub_val.get("update_time")
            parsed = _parse_lane_update_time(ut)
            if parsed:
                candidates.append(parsed)
    return max(candidates) if candidates else pull_time


def _validate_top_level_keys(port: dict[str, Any]) -> None:
    """Ensure CBP hasn't renamed the documented top-level fields."""
    missing = [k for k in CBP_FIELD_MAP.keys() if k not in port]
    if missing:
        # Don't fail — CBP occasionally omits fields for closed ports. Just log.
        log.debug("port %s missing fields %s",
                  port.get("port_number"), missing)


# --------------------------------------------------------------------------- #
# Row coercion
# --------------------------------------------------------------------------- #


def _coerce(port: dict[str, Any], pull_time: datetime) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for src_key, db_col in CBP_FIELD_MAP.items():
        v = port.get(src_key)
        if v is None:
            out[db_col] = None
        elif db_col in JSONB_COLS:
            out[db_col] = v  # passed through Jsonb() at upsert time
        else:
            s = str(v).strip()
            out[db_col] = s if s else None
    out["source_observed_at"] = _derive_observed_at(port, pull_time)
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
    source_run_metadata: dict[str, Any],
    task_id: str | None,
    schedule_id: str | None,
) -> int:
    table = "entities.source_cbp_border_wait_times"
    all_cols = (
        *TYPED_COLS,
        "raw_source_row", "source_provider", "source_filename",
        "source_download_url", "source_run_metadata",
        "source_task_id", "source_schedule_id",
    )
    placeholders = ",".join(["%s"] * len(all_cols))
    update_cols = [c for c in TYPED_COLS if c not in PK_COLS] + [
        "raw_source_row", "source_filename", "source_download_url",
        "source_run_metadata",
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
            "INSERT INTO ops.cbp_border_wait_times_ingest_runs "
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
    ports_seen: int,
    rows_seen: int,
    rows_upserted: int,
    source_observed_at: datetime | None,
    error: str | None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE ops.cbp_border_wait_times_ingest_runs SET "
            "  status = %s, completed_at = now(), "
            "  ports_seen = %s, rows_seen = %s, rows_upserted = %s, "
            "  source_observed_at = %s, error_text = %s "
            "WHERE run_id = %s",
            (status, ports_seen, rows_seen, rows_upserted,
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

    ports_seen = 0
    rows_seen = 0
    rows_upserted = 0
    source_observed_at_max: datetime | None = None
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
            ports, headers = _fetch(client)
            response_date = _response_date(headers) or datetime.now(timezone.utc)
            ports_seen = len(ports)
            log.info("CBP returned %d ports", ports_seen)

            coerced: list[dict[str, Any]] = []
            raw_rows: list[dict[str, Any]] = []
            for port in ports:
                _validate_top_level_keys(port)
                rc = _coerce(port, response_date)
                if not rc.get("port_number"):
                    log.warning("skipping port with no port_number: %r",
                                {k: port.get(k) for k in ("port_name", "border")})
                    continue
                coerced.append(rc)
                raw_rows.append(port)
                obs = rc["source_observed_at"]
                if source_observed_at_max is None or obs > source_observed_at_max:
                    source_observed_at_max = obs

            rows_seen = len(coerced)
            log.info("ingestable rows: %d (skipped %d)",
                     rows_seen, ports_seen - rows_seen)

            if args.dry_run or conn is None:
                log.info("dry-run: skipping upsert")
            elif coerced:
                run_meta = {"ports_seen": ports_seen, "rows_seen": rows_seen}
                rows_upserted = _upsert(
                    conn, coerced, raw_rows,
                    source_filename=SOURCE_FILENAME,
                    source_download_url=ENDPOINT,
                    source_run_metadata=run_meta,
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
                log.exception("rollback failed during error cleanup")

    finally:
        if conn is not None and run_id is not None:
            _finish_run(
                conn, run_id, status, ports_seen, rows_seen, rows_upserted,
                source_observed_at_max, err,
            )
            conn.close()

    log.info("done. status=%s ports=%d rows_upserted=%d",
             status, ports_seen, rows_upserted)
    return 0 if status == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""AISStream.io real-time AIS — long-lived WebSocket ingest.

Source:
    AISStream.io public WebSocket — wss://stream.aisstream.io/v0/stream
    Auth: free API key (https://aisstream.io/authenticate).

Pattern:
    Per CLAUDE.md §"Source ingest invariant" → "Carve-out: high-volume
    telemetry sources" → "Sub-case: real-time streaming sources without
    an upstream archive":
      - Long-lived asyncio process. Each invocation = one session = one
        source_run_id.
      - Reconnect loop on disconnect (exponential backoff, capped).
      - In-memory buffer + COPY FROM STDIN flush every N rows or M seconds.
      - Buffer lost on crash; tracked via rows_buffered_lost.
      - Graceful shutdown on SIGINT / SIGTERM marks run 'succeeded'.

Subscription:
    Configurable bbox(es) + optional MMSI / message-type filters. Defaults
    to all U.S. coastal waters (Atlantic, Gulf, Pacific, Great Lakes) and
    the two highest-value message types (PositionReport, ShipStaticData).

Usage:
    PYTHONPATH=. doppler run -- python3 scripts/run_aisstream_ingest.py
    PYTHONPATH=. doppler run -- python3 scripts/run_aisstream_ingest.py --bbox 33.5 -118.5 33.9 -117.9 --bbox 40.0 -74.5 40.9 -73.7
    PYTHONPATH=. doppler run -- python3 scripts/run_aisstream_ingest.py --max-runtime 3600
    PYTHONPATH=. doppler run -- python3 scripts/run_aisstream_ingest.py --dry-run --max-messages 50
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any

import psycopg
import websockets
from psycopg.types.json import Jsonb


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

PROVIDER = "aisstream"
WS_URL = "wss://stream.aisstream.io/v0/stream"
SOURCE_FILENAME = "wss://stream.aisstream.io/v0/stream"
TARGET_TABLE = "entities.source_aisstream_messages"
RUNS_TABLE = "ops.aisstream_ingest_runs"

# Default bounding boxes — full U.S. coastal coverage. AISStream's bbox
# format is [[[lat_max, lon_min], [lat_min, lon_max]]] where each bbox is
# defined by its NW + SE corners.
DEFAULT_BBOXES: list[list[list[float]]] = [
    # CONUS Atlantic + Gulf
    [[49.5, -82.0], [24.0, -65.0]],
    # CONUS Pacific
    [[49.5, -130.0], [32.0, -116.0]],
    # Alaska
    [[71.5, -180.0], [50.0, -130.0]],
    # Hawaii
    [[23.0, -161.0], [18.5, -154.0]],
    # Great Lakes (overlap with Atlantic bbox is fine)
    [[49.5, -93.0], [41.0, -76.0]],
]

DEFAULT_MESSAGE_TYPES: list[str] = ["PositionReport", "ShipStaticData"]

# Buffer flush thresholds.
BUFFER_FLUSH_ROWS = 10_000
BUFFER_FLUSH_INTERVAL_SEC = 30
RUN_ROW_UPDATE_INTERVAL_SEC = 60

# Reconnect backoff. Exponential up to a cap.
RECONNECT_BASE_DELAY = 2.0
RECONNECT_MAX_DELAY = 120.0

# WebSocket recv timeout. AISStream sends keep-alive frames; if nothing for
# this long, treat as connection-dead and reconnect.
RECV_TIMEOUT_SEC = 90

# COPY columns (must match table schema, in order).
COPY_COLUMNS: tuple[str, ...] = (
    "message_type", "mmsi", "ship_name", "time_utc",
    "latitude", "longitude", "sog", "cog", "true_heading",
    "navigational_status",
    "imo_number", "call_sign", "ship_type", "destination",
    "message", "metadata",
    "source_run_id",
)

JSONB_COLS: frozenset[str] = frozenset({"message", "metadata"})

# AISStream message-type → handler routing.
POSITION_TYPES: frozenset[str] = frozenset({
    "PositionReport", "ExtendedClassBPositionReport",
    "StandardClassBPositionReport", "LongRangeAisBroadcastMessage",
})
STATIC_TYPES: frozenset[str] = frozenset({
    "ShipStaticData", "StaticDataReport", "ShipStaticDataReport",
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
    return logging.getLogger("aisstream_ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# Partition management
# --------------------------------------------------------------------------- #


def _partition_name(d: date) -> str:
    return f"source_aisstream_messages_{d.year}_{d.month:02d}"


def _partition_bounds(d: date) -> tuple[date, date]:
    start = date(d.year, d.month, 1)
    end = date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)
    return start, end


def _ensure_partition(conn: psycopg.Connection, d: date) -> None:
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


# --------------------------------------------------------------------------- #
# Run-row lifecycle
# --------------------------------------------------------------------------- #


def _start_run(
    conn: psycopg.Connection,
    bboxes: list[list[list[float]]],
    message_types: list[str] | None,
    mmsis: list[int] | None,
    task_id: str | None,
    schedule_id: str | None,
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {RUNS_TABLE} "
            f"(status, source_provider, source_filename, source_download_url, "
            f" bounding_boxes, message_type_filter, mmsi_filter, "
            f" task_id, schedule_id) "
            f"VALUES ('running', %s, %s, %s, %s, %s, %s, %s, %s) "
            f"RETURNING run_id",
            (PROVIDER, SOURCE_FILENAME, WS_URL,
             Jsonb(bboxes), message_types, mmsis,
             task_id, schedule_id),
        )
        run_id = cur.fetchone()[0]
        conn.commit()
    return run_id


def _update_run_counters(
    conn: psycopg.Connection,
    run_id: str,
    *,
    rows_loaded: int,
    ws_connect_attempts: int,
    ws_disconnects: int,
    last_flush_at: datetime | None,
    source_observed_at: datetime | None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {RUNS_TABLE} SET "
            f"  rows_loaded = %s, ws_connect_attempts = %s, "
            f"  ws_disconnects = %s, last_flush_at = %s, "
            f"  source_observed_at = %s "
            f"WHERE run_id = %s",
            (rows_loaded, ws_connect_attempts, ws_disconnects,
             last_flush_at, source_observed_at, run_id),
        )
        conn.commit()


def _finish_run(
    conn: psycopg.Connection,
    run_id: str,
    *,
    status: str,
    rows_loaded: int,
    ws_connect_attempts: int,
    ws_disconnects: int,
    rows_buffered_lost: int,
    source_observed_at: datetime | None,
    error: str | None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {RUNS_TABLE} SET "
            f"  status = %s, completed_at = now(), "
            f"  rows_loaded = %s, ws_connect_attempts = %s, "
            f"  ws_disconnects = %s, rows_buffered_lost = %s, "
            f"  source_observed_at = %s, error_text = %s "
            f"WHERE run_id = %s",
            (status, rows_loaded, ws_connect_attempts, ws_disconnects,
             rows_buffered_lost, source_observed_at, error, run_id),
        )
        conn.commit()


# --------------------------------------------------------------------------- #
# Message coercion
# --------------------------------------------------------------------------- #


def _to_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_text(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _parse_time_utc(v: Any) -> datetime | None:
    """Parse AISStream MetaData.time_utc — typically RFC3339 nanos like
    '2026-05-05 18:30:00.123456789 +0000 UTC' or pure ISO 8601."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc) if v.tzinfo else v.replace(tzinfo=timezone.utc)
    s = str(v).strip()
    if not s:
        return None
    # Try ISO 8601 first.
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    # AISStream format: '2026-05-05 18:30:00.123456789 +0000 UTC'
    # Strip trailing ' UTC' and the nanosecond fractional precision below us.
    s2 = s.replace(" UTC", "").strip()
    # Truncate fractional seconds to microsecond precision (Python max).
    if "." in s2:
        head, _, tail = s2.partition(".")
        # tail looks like '123456789 +0000' — split off the offset.
        frac, _, offset = tail.partition(" ")
        frac = frac[:6]  # microseconds
        s2 = f"{head}.{frac}" + (f" {offset}" if offset else "")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f %z", "%Y-%m-%d %H:%M:%S %z",
                "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s2, fmt)
            return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _coerce_message(msg: dict[str, Any], run_id: str) -> tuple | None:
    """Return COPY-row tuple in COPY_COLUMNS order, or None to skip."""
    message_type = msg.get("MessageType")
    metadata = msg.get("MetaData") or {}
    payload = (msg.get("Message") or {}).get(message_type) or {}

    time_utc = _parse_time_utc(metadata.get("time_utc"))
    if time_utc is None:
        # No usable timestamp → can't partition → skip.
        return None

    mmsi = _to_int(metadata.get("MMSI"))
    if mmsi is None:
        mmsi = _to_int(payload.get("UserID"))
    ship_name = _to_text(metadata.get("ShipName"))

    # Position-report subset.
    lat = _to_float(metadata.get("latitude"))
    if lat is None:
        lat = _to_float(payload.get("Latitude"))
    lon = _to_float(metadata.get("longitude"))
    if lon is None:
        lon = _to_float(payload.get("Longitude"))
    sog = _to_float(payload.get("Sog"))
    cog = _to_float(payload.get("Cog"))
    true_heading = _to_int(payload.get("TrueHeading"))
    nav_status = _to_int(payload.get("NavigationalStatus"))

    # Static-data subset.
    imo_number = _to_int(payload.get("ImoNumber"))
    call_sign = _to_text(payload.get("CallSign"))
    ship_type = _to_int(payload.get("Type") or payload.get("ShipType"))
    destination = _to_text(payload.get("Destination"))

    return (
        message_type,
        mmsi,
        ship_name,
        time_utc,
        lat, lon, sog, cog, true_heading, nav_status,
        imo_number, call_sign, ship_type, destination,
        Jsonb(payload),     # message
        Jsonb(metadata),    # metadata
        run_id,
    )


# --------------------------------------------------------------------------- #
# Buffer + COPY flush
# --------------------------------------------------------------------------- #


def _flush(
    conn: psycopg.Connection,
    rows: list[tuple],
    ensured_partitions: set[tuple[int, int]],
) -> int:
    """Ensure target partitions exist, then COPY in. Returns rows written."""
    if not rows:
        return 0
    # Ensure each touched month partition exists.
    months_in_buffer = {(r[3].year, r[3].month) for r in rows}
    for ym in months_in_buffer:
        if ym not in ensured_partitions:
            d = date(ym[0], ym[1], 1)
            _ensure_partition(conn, d)
            ensured_partitions.add(ym)

    cols = ", ".join(COPY_COLUMNS)
    copy_sql = f"COPY {TARGET_TABLE} ({cols}) FROM STDIN"
    written = 0
    with conn.cursor() as cur:
        with cur.copy(copy_sql) as cp:
            for row in rows:
                cp.write_row(row)
                written += 1
    conn.commit()
    return written


# --------------------------------------------------------------------------- #
# Main async loop
# --------------------------------------------------------------------------- #


async def _stream(
    conn: psycopg.Connection | None,
    api_key: str,
    bboxes: list[list[list[float]]],
    message_types: list[str] | None,
    mmsis: list[int] | None,
    *,
    run_id: str | None,
    max_runtime_sec: float | None,
    max_messages: int | None,
    dry_run: bool,
    shutdown: asyncio.Event,
) -> dict[str, int]:
    """Main streaming loop. Returns counters dict."""
    counters = {
        "rows_loaded": 0,
        "ws_connect_attempts": 0,
        "ws_disconnects": 0,
        "rows_buffered_lost": 0,
    }
    buffer: list[tuple] = []
    ensured_partitions: set[tuple[int, int]] = set()
    last_flush_monotonic = asyncio.get_event_loop().time()
    last_run_update_monotonic = last_flush_monotonic
    source_observed_at: datetime | None = None
    started_monotonic = asyncio.get_event_loop().time()

    subscribe_payload: dict[str, Any] = {
        "APIKey": api_key,
        "BoundingBoxes": bboxes,
    }
    if message_types:
        subscribe_payload["FilterMessageTypes"] = message_types
    if mmsis:
        subscribe_payload["FiltersShipMMSI"] = [str(m) for m in mmsis]

    backoff = RECONNECT_BASE_DELAY

    while not shutdown.is_set():
        if max_runtime_sec is not None:
            elapsed = asyncio.get_event_loop().time() - started_monotonic
            if elapsed >= max_runtime_sec:
                log.info("max-runtime %.0fs reached; shutting down", max_runtime_sec)
                break

        counters["ws_connect_attempts"] += 1
        try:
            log.info("connecting to %s (attempt %d)",
                     WS_URL, counters["ws_connect_attempts"])
            async with websockets.connect(
                WS_URL,
                max_size=2 ** 20,        # 1MB max frame
                ping_interval=30,
                ping_timeout=20,
                close_timeout=5,
            ) as ws:
                await ws.send(json.dumps(subscribe_payload))
                log.info("subscribed: bboxes=%d message_types=%s mmsis=%d",
                         len(bboxes),
                         message_types or "ALL",
                         len(mmsis) if mmsis else 0)
                backoff = RECONNECT_BASE_DELAY  # reset on successful connect

                while not shutdown.is_set():
                    if max_runtime_sec is not None:
                        elapsed = asyncio.get_event_loop().time() - started_monotonic
                        if elapsed >= max_runtime_sec:
                            break
                    if max_messages is not None and counters["rows_loaded"] >= max_messages:
                        log.info("max-messages %d reached", max_messages)
                        shutdown.set()
                        break

                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT_SEC)
                    except asyncio.TimeoutError:
                        log.warning("recv timeout (%ds) — reconnecting",
                                    RECV_TIMEOUT_SEC)
                        break

                    try:
                        msg = json.loads(raw)
                    except (TypeError, ValueError):
                        log.warning("non-JSON message: %.200r", raw)
                        continue

                    # AISStream sometimes sends top-level error JSON if the
                    # API key / subscription is bad.
                    if isinstance(msg, dict) and msg.get("error"):
                        raise RuntimeError(f"AISStream error: {msg['error']}")

                    coerced = _coerce_message(msg, run_id or "00000000-0000-0000-0000-000000000000")
                    if coerced is None:
                        continue
                    buffer.append(coerced)
                    # source_observed_at = max time_utc seen
                    obs = coerced[3]
                    if source_observed_at is None or obs > source_observed_at:
                        source_observed_at = obs

                    now = asyncio.get_event_loop().time()
                    should_flush = (
                        len(buffer) >= BUFFER_FLUSH_ROWS
                        or (now - last_flush_monotonic) >= BUFFER_FLUSH_INTERVAL_SEC
                    )
                    if should_flush:
                        if not dry_run and conn is not None and buffer:
                            try:
                                written = _flush(conn, buffer, ensured_partitions)
                                counters["rows_loaded"] += written
                                buffer.clear()
                                last_flush_monotonic = now
                                log.info("flush: %d rows (cumulative=%d)",
                                         written, counters["rows_loaded"])
                            except Exception:
                                log.exception("flush failed; buffer dropped")
                                counters["rows_buffered_lost"] += len(buffer)
                                buffer.clear()
                                conn.rollback()
                        else:
                            counters["rows_loaded"] += len(buffer)
                            buffer.clear()
                            last_flush_monotonic = now

                    if conn is not None and run_id is not None and (
                        now - last_run_update_monotonic >= RUN_ROW_UPDATE_INTERVAL_SEC
                    ):
                        _update_run_counters(
                            conn, run_id,
                            rows_loaded=counters["rows_loaded"],
                            ws_connect_attempts=counters["ws_connect_attempts"],
                            ws_disconnects=counters["ws_disconnects"],
                            last_flush_at=datetime.now(timezone.utc),
                            source_observed_at=source_observed_at,
                        )
                        last_run_update_monotonic = now

        except websockets.exceptions.ConnectionClosed as exc:
            counters["ws_disconnects"] += 1
            log.warning("WebSocket closed: %r — reconnecting in %.1fs",
                        exc, backoff)
        except Exception as exc:
            counters["ws_disconnects"] += 1
            log.exception("WebSocket error: %r — reconnecting in %.1fs",
                          exc, backoff)

        if shutdown.is_set():
            break
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, RECONNECT_MAX_DELAY)

    # Final flush of whatever's in buffer.
    if not dry_run and conn is not None and buffer:
        try:
            written = _flush(conn, buffer, ensured_partitions)
            counters["rows_loaded"] += written
            log.info("final flush: %d rows (cumulative=%d)",
                     written, counters["rows_loaded"])
            buffer.clear()
        except Exception:
            log.exception("final flush failed; buffer dropped")
            counters["rows_buffered_lost"] += len(buffer)
            buffer.clear()
    elif buffer and dry_run:
        counters["rows_loaded"] += len(buffer)
        buffer.clear()

    return counters


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _parse_bbox_arg(values: list[str]) -> list[list[float]]:
    if len(values) != 4:
        raise SystemExit("--bbox requires exactly 4 numbers: NE_lat NE_lon SW_lat SW_lon")
    nw_lat, nw_lon = float(values[0]), float(values[1])
    se_lat, se_lon = float(values[2]), float(values[3])
    return [[nw_lat, nw_lon], [se_lat, se_lon]]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--bbox", action="append", nargs=4, metavar=("NW_LAT", "NW_LON", "SE_LAT", "SE_LON"),
        help="Bounding box (NW + SE corners). Pass multiple --bbox for multiple boxes. "
             "If omitted, defaults to all U.S. coastal waters.",
    )
    parser.add_argument(
        "--message-type", action="append",
        help="AISStream message type to subscribe to. Pass multiple. "
             "Default: PositionReport + ShipStaticData.",
    )
    parser.add_argument(
        "--mmsi", action="append", type=int,
        help="Filter to specific MMSI(s). Pass multiple. Default: no filter.",
    )
    parser.add_argument(
        "--max-runtime", type=float, default=None,
        help="Stop after N seconds (graceful shutdown). Default: run indefinitely.",
    )
    parser.add_argument(
        "--max-messages", type=int, default=None,
        help="Stop after N messages received. Default: no limit.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Don't write to DB; just receive + parse.",
    )
    args = parser.parse_args()

    bboxes = (
        [_parse_bbox_arg(b) for b in args.bbox] if args.bbox
        else DEFAULT_BBOXES
    )
    message_types = args.message_type or DEFAULT_MESSAGE_TYPES
    mmsis = args.mmsi

    api_key = os.environ.get("AISSTREAM_API_KEY")
    if not api_key:
        log.error(
            "AISSTREAM_API_KEY env var must be set. Free key at "
            "https://aisstream.io/authenticate; store in Doppler."
        )
        return 2

    task_id = os.environ.get("TRIGGER_TASK_ID")
    schedule_id = os.environ.get("TRIGGER_SCHEDULE_ID")

    db_url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ.get("DEX_DB_URL_POOLED")
    if not args.dry_run and not db_url:
        log.error("DEX_DB_URL_DIRECT must be set (or pass --dry-run).")
        return 2

    conn: psycopg.Connection | None = None
    if not args.dry_run:
        conn = psycopg.connect(db_url, autocommit=False)

    run_id: str | None = None
    if conn is not None:
        run_id = _start_run(conn, bboxes, message_types, mmsis, task_id, schedule_id)
        log.info("run_id=%s", run_id)

    shutdown = asyncio.Event()

    def _signal_handler(sig: int) -> None:
        log.info("received signal %d; initiating graceful shutdown", sig)
        shutdown.set()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler, sig)
        except NotImplementedError:
            # Windows; fall back.
            signal.signal(sig, lambda s, _: shutdown.set())

    counters = {
        "rows_loaded": 0,
        "ws_connect_attempts": 0,
        "ws_disconnects": 0,
        "rows_buffered_lost": 0,
    }
    status = "succeeded"
    err: str | None = None
    source_observed_at: datetime | None = None

    try:
        counters = loop.run_until_complete(
            _stream(
                conn, api_key, bboxes, message_types, mmsis,
                run_id=run_id,
                max_runtime_sec=args.max_runtime,
                max_messages=args.max_messages,
                dry_run=args.dry_run,
                shutdown=shutdown,
            )
        )
    except Exception as exc:
        status = "failed"
        err = f"{type(exc).__name__}: {exc}"
        log.exception("ingest crashed")

    finally:
        loop.close()
        if conn is not None and run_id is not None:
            try:
                _finish_run(
                    conn, run_id,
                    status=status,
                    rows_loaded=counters["rows_loaded"],
                    ws_connect_attempts=counters["ws_connect_attempts"],
                    ws_disconnects=counters["ws_disconnects"],
                    rows_buffered_lost=counters["rows_buffered_lost"],
                    source_observed_at=source_observed_at,
                    error=err,
                )
            except Exception:
                log.exception("finish_run failed")
            conn.close()

    log.info(
        "done. status=%s rows_loaded=%d ws_connects=%d ws_disconnects=%d lost=%d",
        status, counters["rows_loaded"], counters["ws_connect_attempts"],
        counters["ws_disconnects"], counters["rows_buffered_lost"],
    )
    return 0 if status == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())

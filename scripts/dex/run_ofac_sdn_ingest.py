#!/usr/bin/env python3
"""OFAC Specially Designated Nationals (SDN) — sanctions screening raw ingest.

Source:
    U.S. Treasury Office of Foreign Assets Control public bulk CSVs:
      https://www.treasury.gov/ofac/downloads/sdn.csv (entity grain)
      https://www.treasury.gov/ofac/downloads/alt.csv (aliases by ent_num)
      https://www.treasury.gov/ofac/downloads/add.csv (addresses by ent_num)
    No auth. ~30s end-to-end; bandwidth is ~5–10 MB total.

Idempotency:
    INSERT ... ON CONFLICT (ent_num) DO UPDATE WHERE row IS DISTINCT FROM
    EXCLUDED. last_observed_at is set to the max Last-Modified across the
    three files on every run; downstream MVs filter currently-listed
    entities by recency window.

Audit:
    ops.ofac_sdn_ingest_runs — one row per invocation.

Usage:
    PYTHONPATH=. doppler run -- python3 scripts/run_ofac_sdn_ingest.py
    PYTHONPATH=. doppler run -- python3 scripts/run_ofac_sdn_ingest.py --dry-run
"""

from __future__ import annotations

import argparse
import csv
import io
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

PROVIDER = "ofac_sdn"
USER_AGENT = "data-engine-x-api/ofac-sdn-ingest"
BASE_URL = "https://www.treasury.gov/ofac/downloads"
SDN_URL = f"{BASE_URL}/sdn.csv"
ALT_URL = f"{BASE_URL}/alt.csv"
ADD_URL = f"{BASE_URL}/add.csv"
SOURCE_FILENAME = "sdn.csv + alt.csv + add.csv"

DB_BATCH_SIZE = 5_000

# OFAC's "no value" sentinel — historically a literal "-0-" string.
OFAC_NULL_SENTINEL = "-0-"

# sdn.csv column order (12 columns; positional, no header row in source).
SDN_COLS: tuple[str, ...] = (
    "ent_num", "sdn_name", "sdn_type", "programs", "title",
    "call_sign", "vess_type", "tonnage", "grt", "vess_flag",
    "vess_owner", "remarks",
)

# alt.csv columns (5; positional, no header).
ALT_COLS: tuple[str, ...] = (
    "ent_num", "alt_num", "alt_type", "alt_name", "alt_remarks",
)

# add.csv columns (6; positional, no header).
ADD_COLS: tuple[str, ...] = (
    "ent_num", "add_num", "address", "city_state_postal",
    "country", "add_remarks",
)

# DB columns surfaced as typed.
TYPED_COLS: tuple[str, ...] = (
    "ent_num", "sdn_name", "sdn_type", "programs", "title",
    "call_sign", "vess_type", "tonnage", "grt", "vess_flag",
    "vess_owner", "remarks", "aliases", "addresses", "last_observed_at",
)

JSONB_COLS: frozenset[str] = frozenset({"aliases", "addresses"})

PK_COLS: tuple[str] = ("ent_num",)


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("ofac_sdn_ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #


def _fetch_csv(client: httpx.Client, url: str) -> tuple[str, datetime | None]:
    """GET an OFAC CSV. Returns (text, last_modified UTC)."""
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


def _parse_csv(text: str, cols: tuple[str, ...]) -> list[dict[str, Any]]:
    """Parse OFAC CSV (no header, positional columns).

    Substitutes OFAC's '-0-' sentinel with None. Strips whitespace.
    """
    rows: list[dict[str, Any]] = []
    reader = csv.reader(io.StringIO(text), quotechar='"', skipinitialspace=True)
    for raw in reader:
        if len(raw) < len(cols):
            # Pad short rows with None — defensive.
            raw = raw + [""] * (len(cols) - len(raw))
        elif len(raw) > len(cols):
            log.warning("row has %d cols, expected %d: %r", len(raw), len(cols), raw[:5])
        out: dict[str, Any] = {}
        for i, col in enumerate(cols):
            v = (raw[i] or "").strip()
            if v == OFAC_NULL_SENTINEL or v == "":
                out[col] = None
            else:
                out[col] = v
        rows.append(out)
    return rows


def _coerce_ent_num(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Join + transform
# --------------------------------------------------------------------------- #


def _build_entities(
    sdn_rows: list[dict[str, Any]],
    alt_rows: list[dict[str, Any]],
    add_rows: list[dict[str, Any]],
    last_observed_at: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Join sdn + alt + add by ent_num. Returns (typed_rows, raw_rows)."""
    aliases_by_ent: dict[int, list[dict[str, Any]]] = {}
    for a in alt_rows:
        en = _coerce_ent_num(a.get("ent_num"))
        if en is None:
            continue
        aliases_by_ent.setdefault(en, []).append({
            "alt_num": _coerce_ent_num(a.get("alt_num")),
            "alt_type": a.get("alt_type"),
            "alt_name": a.get("alt_name"),
            "alt_remarks": a.get("alt_remarks"),
        })

    addresses_by_ent: dict[int, list[dict[str, Any]]] = {}
    for a in add_rows:
        en = _coerce_ent_num(a.get("ent_num"))
        if en is None:
            continue
        addresses_by_ent.setdefault(en, []).append({
            "add_num": _coerce_ent_num(a.get("add_num")),
            "address": a.get("address"),
            "city_state_postal": a.get("city_state_postal"),
            "country": a.get("country"),
            "add_remarks": a.get("add_remarks"),
        })

    typed: list[dict[str, Any]] = []
    raw: list[dict[str, Any]] = []
    for s in sdn_rows:
        en = _coerce_ent_num(s.get("ent_num"))
        if en is None:
            log.warning("skipping sdn row with non-integer ent_num: %r",
                        s.get("ent_num"))
            continue
        aliases = aliases_by_ent.get(en, [])
        addresses = addresses_by_ent.get(en, [])
        typed.append({
            "ent_num": en,
            "sdn_name": s.get("sdn_name"),
            "sdn_type": s.get("sdn_type"),
            "programs": s.get("programs"),
            "title": s.get("title"),
            "call_sign": s.get("call_sign"),
            "vess_type": s.get("vess_type"),
            "tonnage": s.get("tonnage"),
            "grt": s.get("grt"),
            "vess_flag": s.get("vess_flag"),
            "vess_owner": s.get("vess_owner"),
            "remarks": s.get("remarks"),
            "aliases": aliases or None,
            "addresses": addresses or None,
            "last_observed_at": last_observed_at,
        })
        raw.append({
            "sdn": s,
            "aliases": aliases,
            "addresses": addresses,
        })

    return typed, raw


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
    table = "entities.source_ofac_sdn"
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
            "INSERT INTO ops.ofac_sdn_ingest_runs "
            "(status, source_filename, source_download_url, "
            " task_id, schedule_id) "
            "VALUES ('running', %s, %s, %s, %s) RETURNING run_id",
            (SOURCE_FILENAME, BASE_URL, task_id, schedule_id),
        )
        run_id = cur.fetchone()[0]
        conn.commit()
    return run_id


def _finish_run(
    conn: psycopg.Connection,
    run_id: str,
    status: str,
    sdn_rows: int,
    alt_rows: int,
    add_rows: int,
    entities_upserted: int,
    source_observed_at: datetime | None,
    error: str | None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE ops.ofac_sdn_ingest_runs SET "
            "  status = %s, completed_at = now(), "
            "  sdn_rows_seen = %s, alt_rows_seen = %s, add_rows_seen = %s, "
            "  entities_upserted = %s, source_observed_at = %s, "
            "  error_text = %s "
            "WHERE run_id = %s",
            (status, sdn_rows, alt_rows, add_rows, entities_upserted,
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

    sdn_count = 0
    alt_count = 0
    add_count = 0
    entities_upserted = 0
    source_observed_at: datetime | None = None
    run_id: str | None = None
    status = "succeeded"
    err: str | None = None

    try:
        if conn is not None:
            run_id = _start_run(conn, task_id, schedule_id)

        with httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "text/csv"},
            follow_redirects=True,
        ) as client:
            log.info("fetching sdn.csv")
            sdn_text, sdn_obs = _fetch_csv(client, SDN_URL)
            log.info("fetching alt.csv")
            alt_text, alt_obs = _fetch_csv(client, ALT_URL)
            log.info("fetching add.csv")
            add_text, add_obs = _fetch_csv(client, ADD_URL)

            # source_observed_at = max Last-Modified across the three files
            # (clamped to now if all are None).
            observed_candidates = [t for t in (sdn_obs, alt_obs, add_obs) if t]
            source_observed_at = (
                max(observed_candidates) if observed_candidates
                else datetime.now(timezone.utc)
            )

            sdn_rows = _parse_csv(sdn_text, SDN_COLS)
            alt_rows = _parse_csv(alt_text, ALT_COLS)
            add_rows = _parse_csv(add_text, ADD_COLS)
            sdn_count = len(sdn_rows)
            alt_count = len(alt_rows)
            add_count = len(add_rows)
            log.info("parsed: sdn=%d alt=%d add=%d",
                     sdn_count, alt_count, add_count)

            typed, raw = _build_entities(
                sdn_rows, alt_rows, add_rows, source_observed_at,
            )
            log.info("joined into %d entities", len(typed))

            if args.dry_run or conn is None:
                log.info("dry-run: skipping upsert")
            elif typed:
                meta = {
                    "sdn_rows": sdn_count,
                    "alt_rows": alt_count,
                    "add_rows": add_count,
                    "entities": len(typed),
                }
                entities_upserted = _upsert(
                    conn, typed, raw,
                    source_filename=SOURCE_FILENAME,
                    source_download_url=BASE_URL,
                    source_observed_at=source_observed_at,
                    source_run_metadata=meta,
                    task_id=task_id,
                    schedule_id=schedule_id,
                )
                log.info("upserted %d entities", entities_upserted)

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
                sdn_count, alt_count, add_count, entities_upserted,
                source_observed_at, err,
            )
            conn.close()

    log.info("done. status=%s sdn=%d alt=%d add=%d entities_upserted=%d",
             status, sdn_count, alt_count, add_count, entities_upserted)
    return 0 if status == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())

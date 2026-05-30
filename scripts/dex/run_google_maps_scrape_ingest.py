#!/usr/bin/env python3
"""Google Maps US businesses — raw scrape ingest into entities.source_google_maps_scrape.

Source:
    github.com/growthenginenowoslawski/coldoutboundskills
      Common Outbound Lists/Google Maps Scrape - 12M US Businesses/
      google-maps-scrape-part{1..13}.zip  (each contains one CSV)

PK strategy (see migration 20260504222651_create_source_google_maps_scrape.sql):
    row_digest = sha256_hex(
      coalesce(title,'') || U+001F || coalesce(link,'') || U+001F ||
      coalesce(phone,'') || U+001F || coalesce(category_titles,'') || U+001F ||
      coalesce(zip_code,'') || U+001F || coalesce(normalized_display_link,'')
    )
    Computed verbatim from CSV cells (no trim) so re-ingest of the same file is a no-op.
    INSERT … ON CONFLICT (row_digest) DO NOTHING.

Idempotency:
    Re-running on the same parts is a no-op. Cross-part overlap dedupes naturally.

Audit:
    ops.google_maps_scrape_ingest_runs — one row per invocation, with
    {by_part: {...}, total: N} counters.

Usage:
    PYTHONPATH=. doppler run -- bash -c \\
      'python3 scripts/run_google_maps_scrape_ingest.py --parts 13'
    PYTHONPATH=. doppler run -- bash -c \\
      'python3 scripts/run_google_maps_scrape_ingest.py --parts 1-13'
    PYTHONPATH=. doppler run -- bash -c \\
      'python3 scripts/run_google_maps_scrape_ingest.py --parts 13 --dry-run'

Connects via $DEX_DB_URL_DIRECT (pooled URL is pgbouncer transaction-mode and
is unreliable for multi-statement COPY loads of this scale).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import logging
import os
import sys
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

PROVIDER = "google_maps_scrape"
USER_AGENT = "data-engine-x-api/google-maps-scrape-ingest"
TARGET_TABLE = "entities.source_google_maps_scrape"
RUNS_TABLE = "ops.google_maps_scrape_ingest_runs"

REPO_OWNER = "growthenginenowoslawski"
REPO_NAME = "coldoutboundskills"
REPO_DIR = "Common Outbound Lists/Google Maps Scrape - 12M US Businesses"
REPO_TREE_URL = (
    f"https://github.com/{REPO_OWNER}/{REPO_NAME}/tree/main/"
    "Common%20Outbound%20Lists/Google%20Maps%20Scrape%20-%2012M%20US%20Businesses"
)

# Order MUST match the upstream CSV header order and the typed columns in the
# migration. Used both to validate the header and to extract values.
CSV_COLS: tuple[str, ...] = (
    "title",
    "link",
    "phone",
    "category_titles",
    "zip_code",
    "normalized_display_link",
)

# CSV cell separator used inside the digest preimage. ASCII Unit Separator
# avoids "Smith,John" vs "Smith,,John" collision class of plain concat.
DIGEST_SEP = "\x1f"

# Per-part chunk for COPY into staging.
COPY_CHUNK_ROWS = 50_000


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("google_maps_scrape_ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #


def _zip_url(part: int) -> str:
    return (
        f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/main/"
        "Common%20Outbound%20Lists/Google%20Maps%20Scrape%20-%2012M%20US%20Businesses/"
        f"google-maps-scrape-part{part}.zip"
    )


def _parse_observed_at(headers: dict[str, str]) -> datetime | None:
    lm = headers.get("Last-Modified") or headers.get("last-modified")
    if not lm:
        return None
    try:
        dt = parsedate_to_datetime(lm)
        return dt.astimezone(timezone.utc) if dt else None
    except (TypeError, ValueError):
        return None


def _download_part(client: httpx.Client, part: int) -> tuple[bytes, dict[str, str]]:
    url = _zip_url(part)
    log.info("[part%d] downloading %s", part, url)
    r = client.get(url, timeout=300, follow_redirects=True)
    r.raise_for_status()
    log.info("[part%d] downloaded %.1f MB", part, len(r.content) / 1_048_576)
    return r.content, dict(r.headers)


def _extract_csv(zip_bytes: bytes, part: int) -> tuple[str, bytes]:
    """Return (csv_filename, csv_bytes) from the zip."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise RuntimeError(
                f"no .csv in part{part} zip: names={zf.namelist()}"
            )
        if len(names) > 1:
            log.warning(
                "[part%d] zip has %d CSVs; using first: %s",
                part, len(names), names[0],
            )
        return names[0], zf.read(names[0])


# --------------------------------------------------------------------------- #
# CSV → staging via COPY, then upsert into target
# --------------------------------------------------------------------------- #


def _digest(values: tuple[str, str, str, str, str, str]) -> str:
    """sha256 hex of the 6 verbatim CSV cells joined by U+001F.

    Values here are already strings (csv.reader returns strings); empty cells
    are empty strings, NOT None.
    """
    preimage = DIGEST_SEP.join(values).encode("utf-8")
    return hashlib.sha256(preimage).hexdigest()


def _stream_rows(csv_bytes: bytes):
    """Yield (row_digest, title, link, phone, category_titles, zip_code,
    normalized_display_link, raw_source_row_json) tuples.

    Empty cells become None for the typed columns (so '' → NULL in pg) but the
    digest is computed from the verbatim string cells, and raw_source_row also
    holds the verbatim strings (including empty strings).
    """
    text = csv_bytes.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if header is None:
        raise RuntimeError("empty CSV (no header row)")
    header_norm = tuple(h.strip().lower() for h in header)
    if header_norm != CSV_COLS:
        raise RuntimeError(
            f"unexpected CSV header. got={header_norm} expected={CSV_COLS}"
        )

    for row in reader:
        # Pad short rows / truncate long rows to exactly 6 cells.
        if len(row) < 6:
            row = row + [""] * (6 - len(row))
        elif len(row) > 6:
            row = row[:6]

        title, link, phone, cats, zipc, ndl = row
        digest = _digest((title, link, phone, cats, zipc, ndl))
        raw = {
            "title": title, "link": link, "phone": phone,
            "category_titles": cats, "zip_code": zipc,
            "normalized_display_link": ndl,
        }
        yield (
            digest,
            title or None,
            link or None,
            phone or None,
            cats or None,
            zipc or None,
            ndl or None,
            raw,
        )


def _ingest_part(
    conn: psycopg.Connection,
    *,
    csv_filename: str,
    csv_bytes: bytes,
    download_url: str,
    observed_at: datetime | None,
    run_metadata: dict[str, Any],
    task_id: str | None,
    schedule_id: str | None,
) -> tuple[int, int]:
    """Returns (rows_seen, rows_upserted) for this part.

    Strategy: per-part TEMP table for staging, then INSERT … SELECT …
    ON CONFLICT (row_digest) DO NOTHING. The TEMP table is dropped at
    transaction end (ON COMMIT DROP).
    """
    rows_seen = 0
    rows_upserted = 0

    with conn.cursor() as cur:
        # Staging has NO PK — the source CSVs contain intra-file duplicate
        # rows (same business scraped twice), and we dedupe at upsert time
        # via SELECT DISTINCT ON (row_digest).
        cur.execute("""
            CREATE TEMP TABLE _stage_gmaps (
              row_digest              text NOT NULL,
              title                   text,
              link                    text,
              phone                   text,
              category_titles         text,
              zip_code                text,
              normalized_display_link text,
              raw_source_row          jsonb NOT NULL
            ) ON COMMIT DROP
        """)

        # Stream rows in via COPY in chunks. psycopg3 .copy() with text format
        # auto-handles escaping. We push raw_source_row as JSON text.
        copy_sql = (
            "COPY _stage_gmaps "
            "(row_digest, title, link, phone, category_titles, zip_code, "
            "normalized_display_link, raw_source_row) FROM STDIN"
        )
        with cur.copy(copy_sql) as cp:
            for parsed in _stream_rows(csv_bytes):
                rows_seen += 1
                # write_row with explicit types — psycopg adapts dict->jsonb via Jsonb wrapper.
                digest, title, link, phone, cats, zipc, ndl, raw = parsed
                cp.write_row((digest, title, link, phone, cats, zipc, ndl, Jsonb(raw)))
                if rows_seen % 100_000 == 0:
                    log.info("  copied %d rows into staging…", rows_seen)

        log.info("  staged %d rows; upserting…", rows_seen)

        cur.execute(
            f"""
            INSERT INTO {TARGET_TABLE} (
              row_digest, title, link, phone, category_titles, zip_code,
              normalized_display_link, raw_source_row, source_provider,
              source_filename, source_download_url, source_observed_at,
              source_run_metadata, source_task_id, source_schedule_id
            )
            SELECT DISTINCT ON (row_digest)
              row_digest, title, link, phone, category_titles, zip_code,
              normalized_display_link, raw_source_row, %s,
              %s, %s, %s, %s, %s, %s
            FROM _stage_gmaps
            ON CONFLICT (row_digest) DO NOTHING
            """,
            (
                PROVIDER,
                csv_filename,
                download_url,
                observed_at,
                Jsonb(run_metadata),
                task_id,
                schedule_id,
            ),
        )
        rows_upserted = cur.rowcount or 0

    conn.commit()
    return rows_seen, rows_upserted


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #


def _start_run(
    conn: psycopg.Connection,
    parts: list[int],
    task_id: str | None,
    schedule_id: str | None,
) -> str:
    parts_arr = [f"part{p}" for p in parts]
    files_csv = ",".join(f"google-maps-scrape-part{p}.zip" for p in parts)
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {RUNS_TABLE} "
            "(status, parts_requested, source_filename, source_download_url, "
            " source_task_id, source_schedule_id) "
            "VALUES ('running', %s, %s, %s, %s, %s) RETURNING run_id",
            (parts_arr, files_csv, REPO_TREE_URL, task_id, schedule_id),
        )
        run_id = cur.fetchone()[0]
        conn.commit()
    return str(run_id)


def _finish_run(
    conn: psycopg.Connection,
    run_id: str,
    status: str,
    rows_seen_by_part: dict[str, int],
    rows_upserted_by_part: dict[str, int],
    earliest_observed: datetime | None,
    error: str | None,
) -> None:
    rows_seen = {
        "by_part": rows_seen_by_part,
        "total": sum(rows_seen_by_part.values()),
    }
    rows_upserted = {
        "by_part": rows_upserted_by_part,
        "total": sum(rows_upserted_by_part.values()),
    }
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {RUNS_TABLE} SET "
            "  status = %s, completed_at = now(), "
            "  rows_seen = %s, rows_upserted = %s, "
            "  source_observed_at = %s, error_text = %s "
            "WHERE run_id = %s",
            (
                status, Jsonb(rows_seen), Jsonb(rows_upserted),
                earliest_observed, error, run_id,
            ),
        )
        conn.commit()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _parse_parts(raw: str) -> list[int]:
    """'13' → [13]; '1-3' → [1,2,3]; '1,4,9' → [1,4,9]."""
    if not raw:
        raise SystemExit("--parts is required (e.g. 13, 1-13, 1,4,9)")
    out: list[int] = []
    for piece in raw.split(","):
        piece = piece.strip()
        if "-" in piece:
            a, b = piece.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(piece))
    if not all(1 <= p <= 13 for p in out):
        raise SystemExit(f"all parts must be in [1, 13], got {out}")
    return out


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--parts", required=True,
        help="Which zip parts to ingest. Examples: 13 | 1-13 | 1,4,9",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Download + parse only, no DB writes.",
    )
    parser.add_argument(
        "--max-rows-per-part", type=int, default=None,
        help="Stop streaming each part after N rows (smoke test).",
    )
    args = parser.parse_args(argv)

    parts = _parse_parts(args.parts)
    log.info("parts to ingest: %s", parts)

    task_id = os.environ.get("TRIGGER_TASK_ID") or os.environ.get("MODAL_TASK_ID")
    schedule_id = (
        os.environ.get("TRIGGER_SCHEDULE_ID") or os.environ.get("MODAL_SCHEDULE_ID")
    )

    db_url = os.environ.get("DEX_DB_URL_DIRECT")
    if not args.dry_run and not db_url:
        log.error("DEX_DB_URL_DIRECT must be set (run via `doppler run -- bash -c …`)")
        return {"status": "error", "error": "no DEX_DB_URL_DIRECT"}

    conn: psycopg.Connection | None = None
    if not args.dry_run:
        conn = psycopg.connect(db_url, autocommit=False)
        # Supabase prod defaults to statement_timeout='2min'; the per-part
        # COPY of large CSVs (part1 = 132 MB / ~1M rows) blows past that.
        # Disable the timeout for this session — single-purpose ingest, no
        # risk of accidental long-running selects.
        with conn.cursor() as _cur:
            _cur.execute("SET statement_timeout = 0")
        conn.commit()

    rows_seen_by_part: dict[str, int] = {}
    rows_upserted_by_part: dict[str, int] = {}
    earliest_observed: datetime | None = None
    run_id: str | None = None
    status = "succeeded"
    err: str | None = None

    try:
        if conn is not None:
            run_id = _start_run(conn, parts, task_id, schedule_id)
            log.info("audit run_id=%s", run_id)

        with httpx.Client(headers={"User-Agent": USER_AGENT}) as client:
            for part in parts:
                zip_bytes, headers = _download_part(client, part)
                observed = _parse_observed_at(headers)
                csv_name, csv_bytes = _extract_csv(zip_bytes, part)
                log.info("[part%d] csv: %s (%.1f MB)",
                         part, csv_name, len(csv_bytes) / 1_048_576)

                key = f"part{part}"

                if args.dry_run or conn is None:
                    # Drain the iterator to exercise parsing.
                    seen = 0
                    for _ in _stream_rows(csv_bytes):
                        seen += 1
                        if args.max_rows_per_part and seen >= args.max_rows_per_part:
                            break
                    log.info("[part%d] dry-run: parsed %d rows", part, seen)
                    rows_seen_by_part[key] = seen
                    rows_upserted_by_part[key] = 0
                    continue

                if args.max_rows_per_part is not None:
                    head = io.StringIO()
                    head.write(csv_bytes.decode("utf-8-sig").split("\n", 1)[0] + "\n")
                    body_lines = csv_bytes.decode("utf-8-sig").split("\n")[1:]
                    head.write("\n".join(body_lines[: args.max_rows_per_part]))
                    csv_bytes_to_use = head.getvalue().encode("utf-8")
                else:
                    csv_bytes_to_use = csv_bytes

                run_meta = {
                    "part": part,
                    "csv_filename": csv_name,
                    "csv_bytes": len(csv_bytes_to_use),
                    "zip_bytes": len(zip_bytes),
                    "max_rows_per_part": args.max_rows_per_part,
                }
                seen, upserted = _ingest_part(
                    conn,
                    csv_filename=csv_name,
                    csv_bytes=csv_bytes_to_use,
                    download_url=_zip_url(part),
                    observed_at=observed,
                    run_metadata=run_meta,
                    task_id=task_id,
                    schedule_id=schedule_id,
                )
                rows_seen_by_part[key] = seen
                rows_upserted_by_part[key] = upserted
                if observed and (
                    earliest_observed is None or observed < earliest_observed
                ):
                    earliest_observed = observed
                log.info(
                    "[part%d] done. seen=%d upserted=%d", part, seen, upserted,
                )

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
                conn, run_id, status,
                rows_seen_by_part, rows_upserted_by_part,
                earliest_observed, err,
            )
            conn.close()

    log.info(
        "done. status=%s rows_seen=%s rows_upserted=%s",
        status, rows_seen_by_part, rows_upserted_by_part,
    )
    return {
        "status": status,
        "rows_seen": rows_seen_by_part,
        "rows_upserted": rows_upserted_by_part,
        "error": err,
    }


if __name__ == "__main__":
    result = main()
    sys.exit(0 if result.get("status") == "succeeded" else 1)

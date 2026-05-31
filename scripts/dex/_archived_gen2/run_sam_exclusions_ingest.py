#!/usr/bin/env python3
"""SAM.gov public exclusions — bulk-CSV daily ingest.

Source:
    U.S. General Services Administration, System for Award Management.
    Lookup endpoint: https://api.sam.gov/data-services/v1/extracts
        ?fileType=EXCLUSION&sensitivity=PUBLIC&frequency=DAILY&date=MM/DD/YYYY
    Returns HTTP 302 → presigned S3 URL → ~12 MB ZIP → ~78 MB CSV.
    Lookup uses 1 API quota call per run; the actual ZIP download is
    unauthenticated (presigned S3, 1-hour expiry on the URL).

Auth:
    `SAM_API_KEY` env var. Free key from sam.gov/data-services. The 1-call/
    day quota gates the lookup only; bulk download is no-auth.

Idempotency:
    INSERT ... ON CONFLICT (sam_number) DO UPDATE WHERE row IS DISTINCT FROM
    EXCLUDED. SAM publishes a full snapshot of currently-listed exclusions
    daily; re-ingesting the same extract is a no-op.

Audit:
    ops.sam_exclusions_ingest_runs — one row per invocation.

Usage:
    PYTHONPATH=. doppler run -- python3 scripts/run_sam_exclusions_ingest.py
    PYTHONPATH=. doppler run -- python3 scripts/run_sam_exclusions_ingest.py --date 05/04/2026
    PYTHONPATH=. doppler run -- python3 scripts/run_sam_exclusions_ingest.py --dry-run
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import shutil
import sys
import tempfile
import zipfile
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
import psycopg
from psycopg.types.json import Jsonb


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

PROVIDER = "sam_exclusions"
USER_AGENT = "data-engine-x-api/sam-exclusions-ingest"
LOOKUP_ENDPOINT = "https://api.sam.gov/data-services/v1/extracts"
DB_BATCH_SIZE = 1_000

# CSV header → DB column mapping (verbatim from the bulk extract header).
CSV_TO_DB: dict[str, str] = {
    "Classification": "classification",
    "Name": "name",
    "Prefix": "prefix",
    "First": "first_name",
    "Middle": "middle_name",
    "Last": "last_name",
    "Suffix": "suffix",
    "Address 1": "address_1",
    "Address 2": "address_2",
    "Address 3": "address_3",
    "Address 4": "address_4",
    "City": "city",
    "State / Province": "state_province",
    "Country": "country",
    "Zip Code": "zip_code",
    "Open Data Flag": "open_data_flag",
    "Blank (Deprecated)": "blank_deprecated",
    "Unique Entity ID": "unique_entity_id",
    "Exclusion Program": "exclusion_program",
    "Excluding Agency": "excluding_agency",
    "CT Code": "ct_code",
    "Exclusion Type": "exclusion_type",
    "Additional Comments": "additional_comments",
    "Active Date": "active_date",
    "Termination Date": "termination_date",
    "Record Status": "record_status",
    "Cross-Reference": "cross_reference",
    "SAM Number": "sam_number",
    "CAGE": "cage",
    "NPI": "npi",
    "Creation_Date": "creation_date",
}

DATE_COLS: frozenset[str] = frozenset({"active_date", "termination_date", "creation_date"})

TYPED_COLS: tuple[str, ...] = tuple(CSV_TO_DB.values())
PK_COLS: tuple[str, ...] = ("sam_number",)


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("sam_exclusions_ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# Lookup + download
# --------------------------------------------------------------------------- #


def _resolve_extract_url(
    client: httpx.Client,
    api_key: str,
    extract_date_mmddyyyy: str,
) -> tuple[str, dict[str, str], str]:
    """Hit the SAM lookup endpoint, follow 302 manually, return
    (presigned_s3_url, response_headers, sanitized_lookup_url).
    """
    params = {
        "api_key": api_key,
        "fileType": "EXCLUSION",
        "sensitivity": "PUBLIC",
        "frequency": "DAILY",
        "date": extract_date_mmddyyyy,
    }
    sanitized = {k: v for k, v in params.items() if k != "api_key"}
    sanitized_url = (
        f"{LOOKUP_ENDPOINT}?"
        + "&".join(f"{k}={v}" for k, v in sanitized.items())
    )
    r = client.get(LOOKUP_ENDPOINT, params=params, follow_redirects=False, timeout=60)
    if r.status_code == 302:
        location = r.headers.get("location") or r.headers.get("Location")
        if not location:
            raise RuntimeError("302 with no Location header")
        return location, dict(r.headers), sanitized_url
    if r.status_code == 200:
        body = r.json()
        download_url = (
            body.get("downloadUrl")
            or body.get("url")
            or (body.get("links", [{}])[0].get("href") if body.get("links") else None)
        )
        if not download_url:
            raise RuntimeError(f"200 OK but no download URL in body: {str(body)[:200]}")
        return download_url, dict(r.headers), sanitized_url
    raise RuntimeError(
        f"unexpected lookup status {r.status_code}: {r.text[:300]}"
    )


def _download_zip(client: httpx.Client, url: str, dst_path: str) -> int:
    """Stream the presigned S3 ZIP to disk. Returns byte count."""
    total = 0
    with client.stream("GET", url, timeout=300) as r:
        r.raise_for_status()
        with open(dst_path, "wb") as f:
            for chunk in r.iter_bytes(chunk_size=1 << 20):
                f.write(chunk)
                total += len(chunk)
    return total


def _extract_csv(zip_path: str, dst_dir: str) -> str:
    """Unzip and return the path to the .CSV file inside."""
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if n.upper().endswith(".CSV")]
        if not names:
            raise RuntimeError(f"no .CSV in ZIP: {zf.namelist()}")
        if len(names) > 1:
            log.warning("multiple .CSV in ZIP — using first: %s", names)
        csv_name = names[0]
        zf.extract(csv_name, dst_dir)
        return os.path.join(dst_dir, csv_name)


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


def _to_date(s: str) -> Any:
    if not s:
        return None
    s = s.strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _coerce_row(raw: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Map CSV row → typed DB row + verbatim raw dict.

    Returns None if the row has no sam_number (PK constraint can't be
    satisfied — log + skip).
    """
    out: dict[str, Any] = {c: None for c in TYPED_COLS}
    for csv_key, db_col in CSV_TO_DB.items():
        v = raw.get(csv_key)
        if v is None:
            continue
        s = v.strip() if isinstance(v, str) else str(v).strip()
        if s == "":
            continue
        if db_col in DATE_COLS:
            out[db_col] = _to_date(s)
        else:
            out[db_col] = s
    if not out.get("sam_number"):
        return None
    return out, raw


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
    table = "entities.source_sam_exclusions"
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
    extract_date: str,
    source_download_url: str,
    task_id: str | None,
    schedule_id: str | None,
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ops.sam_exclusions_ingest_runs "
            "(status, source_filename, source_download_url, "
            " task_id, schedule_id) "
            "VALUES ('running', %s, %s, %s, %s) RETURNING run_id",
            (f"DAILY:{extract_date}", source_download_url, task_id, schedule_id),
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
    source_filename: str | None,
    source_download_url: str | None,
    error: str | None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE ops.sam_exclusions_ingest_runs SET "
            "  status = %s, completed_at = now(), "
            "  rows_seen = %s, rows_upserted = %s, "
            "  source_filename = %s, source_observed_at = %s, "
            "  source_download_url = %s, error_text = %s "
            "WHERE run_id = %s",
            (status, rows_seen, rows_upserted,
             source_filename, source_observed_at,
             source_download_url, error, run_id),
        )
        conn.commit()


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #


def _default_extract_date() -> str:
    """Yesterday in MM/DD/YYYY (SAM publishes overnight; today's may not be ready)."""
    y = date.today() - timedelta(days=1)
    return f"{y.month:02d}/{y.day:02d}/{y.year}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--date",
        default=None,
        help="Extract date in MM/DD/YYYY format. Default = yesterday.",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Download + parse only; no DB writes.")
    args = parser.parse_args()

    extract_date = args.date or _default_extract_date()

    api_key = os.environ.get("SAM_API_KEY")
    if not api_key:
        log.error(
            "SAM_API_KEY env var must be set. Free key at sam.gov/data-services."
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
        # 167k upserts with 11 indexes blows past Supabase's default
        # statement_timeout. Disable for this session.
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = 0")
        conn.commit()

    workdir = tempfile.mkdtemp(prefix="sam-excl-")
    zip_path = os.path.join(workdir, "extract.zip")

    rows_seen = 0
    rows_upserted = 0
    zip_bytes = 0
    source_observed_at: datetime | None = None
    source_filename: str | None = None
    presigned_url: str | None = None
    run_id: str | None = None
    status = "succeeded"
    err: str | None = None

    try:
        with httpx.Client(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=False,
        ) as client:
            log.info("resolving extract URL for %s ...", extract_date)
            presigned_url, lookup_headers, lookup_url = _resolve_extract_url(
                client, api_key, extract_date,
            )
            source_observed_at = _response_date(lookup_headers)
            log.info("resolved (lookup HTTP=302). downloading ZIP ...")

            if conn is not None:
                run_id = _start_run(conn, extract_date, presigned_url,
                                    task_id, schedule_id)

            zip_bytes = _download_zip(client, presigned_url, zip_path)
            log.info("downloaded %.1f MB ZIP", zip_bytes / (1 << 20))

            csv_path = _extract_csv(zip_path, workdir)
            source_filename = os.path.basename(csv_path)
            log.info("extracted %s (%.1f MB)",
                     source_filename, os.path.getsize(csv_path) / (1 << 20))

        coerced: list[dict[str, Any]] = []
        raw_rows: list[dict[str, Any]] = []
        skipped_no_pk = 0
        with open(csv_path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for raw in reader:
                rows_seen += 1
                result = _coerce_row(raw)
                if result is None:
                    skipped_no_pk += 1
                    continue
                typed, raw_dict = result
                coerced.append(typed)
                raw_rows.append(raw_dict)

        log.info("parsed %d rows (%d skipped — missing sam_number)",
                 len(coerced), skipped_no_pk)

        if args.dry_run or conn is None:
            log.info("dry-run: would upsert %d rows", len(coerced))
        else:
            meta = {
                "extract_date": extract_date,
                "frequency": "DAILY",
                "rows_in_csv": rows_seen,
                "rows_ingestable": len(coerced),
                "rows_skipped_no_pk": skipped_no_pk,
                "zip_bytes": zip_bytes,
            }
            rows_upserted = _upsert(
                conn, coerced, raw_rows,
                source_filename=source_filename,
                source_download_url=presigned_url,
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
                source_observed_at, source_filename, presigned_url, err,
            )
            conn.close()
        try:
            shutil.rmtree(workdir, ignore_errors=True)
        except Exception:
            pass

    log.info("done. status=%s rows_seen=%d rows_upserted=%d",
             status, rows_seen, rows_upserted)
    return 0 if status == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())

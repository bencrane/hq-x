#!/usr/bin/env python3
"""FAA Releasable Airmen Database — CSV ingest.

Source:
    https://registry.faa.gov/database/CS{MMYYYY}.zip — public Akamai CDN.
    ZIP contains 4 CSVs: PILOT_BASIC.csv, PILOT_CERT.csv,
    NONPILOT_BASIC.csv, NONPILOT_CERT.csv.

Freshness probe:
    Probes current month's ZIP first (CS{MMYYYY}.zip); falls back to prior
    month on 404. Records the actual file used in ops.faa_airmen_ingest_runs.
    Note: registry.faa.gov is behind Akamai and may 503 from local IPs;
    Modal/AWS egress IPs have no such block.

Idempotency:
    COPY rows into a temp staging table, then INSERT ... ON CONFLICT
    (pk_cols) DO UPDATE ... WHERE row IS DISTINCT FROM EXCLUDED.
    Basic tables: PK (unique_id). Cert tables: PK (unique_id,
    certificate_type, certificate_level).

Audit:
    One row per invocation in ops.faa_airmen_ingest_runs.
    rows_seen / rows_upserted are jsonb objects keyed by csv basename
    (pilot_basic, pilot_cert, nonpilot_basic, nonpilot_cert).

Usage:
    DEX_DB_URL_POOLED=<url> python3 scripts/run_faa_airmen_ingest.py
    DEX_DB_URL_POOLED=<url> python3 scripts/run_faa_airmen_ingest.py --dry-run
    DEX_DB_URL_POOLED=<url> python3 scripts/run_faa_airmen_ingest.py --month 042026
    DEX_DB_URL_POOLED=<url> python3 scripts/run_faa_airmen_ingest.py --max-rows 1000
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx
import psycopg
from psycopg.types.json import Jsonb


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

BASE_URL = "https://registry.faa.gov/database/"
PROVIDER = "faa_airmen"
USER_AGENT = "data-engine-x-api/faa-airmen-ingest"
BATCH_SIZE = 10_000

# Column shape for PILOT_BASIC and NONPILOT_BASIC CSVs.
# Actual CS052026.zip headers (leading spaces stripped, snake_cased):
# UNIQUE ID, FIRST NAME, LAST NAME, STREET 1, STREET 2, CITY, STATE,
# ZIP CODE, COUNTRY, REGION, MED CLASS, MED DATE, MED EXP DATE,
# BASIC MED COURSE DATE, BASIC MED CMEC DATE
BASIC_COLS = (
    "unique_id",
    "first_name",
    "last_name",
    "street_1",
    "street_2",
    "city",
    "state",
    "zip_code",
    "country",
    "region",
    "med_class",
    "med_date",
    "med_exp_date",
    "basic_med_course_date",
    "basic_med_cmec_date",
)

# PILOT_CERT.csv actual headers: UNIQUE ID, FIRST NAME, LAST NAME, TYPE,
# LEVEL, EXPIRE DATE, RATING1..11, TYPERATING1..99
PILOT_CERT_COLS = (
    "unique_id",
    "first_name",
    "last_name",
    "certificate_type",          # TYPE column
    "certificate_level",         # LEVEL column
    "certificate_expire_date",   # EXPIRE DATE column
    "rating1", "rating2", "rating3", "rating4", "rating5",
    "rating6", "rating7", "rating8", "rating9", "rating10", "rating11",
    "typerating1", "typerating2", "typerating3", "typerating4", "typerating5",
    "typerating6", "typerating7", "typerating8", "typerating9", "typerating10",
    "typerating11", "typerating12", "typerating13", "typerating14", "typerating15",
    "typerating16", "typerating17", "typerating18", "typerating19", "typerating20",
    "typerating21", "typerating22", "typerating23", "typerating24", "typerating25",
    "typerating26", "typerating27", "typerating28", "typerating29", "typerating30",
    "typerating31", "typerating32", "typerating33", "typerating34", "typerating35",
    "typerating36", "typerating37", "typerating38", "typerating39", "typerating40",
    "typerating41", "typerating42", "typerating43", "typerating44", "typerating45",
    "typerating46", "typerating47", "typerating48", "typerating49", "typerating50",
    "typerating51", "typerating52", "typerating53", "typerating54", "typerating55",
    "typerating56", "typerating57", "typerating58", "typerating59", "typerating60",
    "typerating61", "typerating62", "typerating63", "typerating64", "typerating65",
    "typerating66", "typerating67", "typerating68", "typerating69", "typerating70",
    "typerating71", "typerating72", "typerating73", "typerating74", "typerating75",
    "typerating76", "typerating77", "typerating78", "typerating79", "typerating80",
    "typerating81", "typerating82", "typerating83", "typerating84", "typerating85",
    "typerating86", "typerating87", "typerating88", "typerating89", "typerating90",
    "typerating91", "typerating92", "typerating93", "typerating94", "typerating95",
    "typerating96", "typerating97", "typerating98", "typerating99",
)

# NONPILOT_CERT.csv actual headers: UNIQUE ID, FIRST NAME, LAST NAME, TYPE,
# LEVEL, EXPIRE DATE, RATING1..11 (no TYPERATING columns)
NONPILOT_CERT_COLS = (
    "unique_id",
    "first_name",
    "last_name",
    "certificate_type",          # TYPE column
    "certificate_level",         # LEVEL column
    "certificate_expire_date",   # EXPIRE DATE column
    "rating1", "rating2", "rating3", "rating4", "rating5",
    "rating6", "rating7", "rating8", "rating9", "rating10", "rating11",
)

# CSV filename → (target table, pk_cols tuple, typed_cols tuple)
CSV_TABLES: dict[str, tuple[str, tuple[str, ...], tuple[str, ...]]] = {
    "PILOT_BASIC.csv":    (
        "entities.source_faa_airmen_pilot_basic",
        ("unique_id",),
        BASIC_COLS,
    ),
    "PILOT_CERT.csv":     (
        "entities.source_faa_airmen_pilot_cert",
        ("unique_id", "certificate_type", "certificate_level"),
        PILOT_CERT_COLS,
    ),
    "NONPILOT_BASIC.csv": (
        "entities.source_faa_airmen_nonpilot_basic",
        ("unique_id",),
        BASIC_COLS,
    ),
    "NONPILOT_CERT.csv":  (
        "entities.source_faa_airmen_nonpilot_cert",
        ("unique_id", "certificate_type", "certificate_level"),
        NONPILOT_CERT_COLS,
    ),
}

# FAA CSV header → snake_case column name mapping.
# Actual headers observed in CS052026.zip (2026-05-01). Most headers after
# the first column have a leading space; .strip() handles that. Unknown
# headers are silently skipped (raw_source_row captures them verbatim).
HEADER_TO_COLUMN: dict[str, str] = {
    # Shared (all CSVs)
    "UNIQUE ID": "unique_id",
    "FIRST NAME": "first_name",
    "LAST NAME": "last_name",
    # Basic CSVs (pilot + nonpilot)
    "STREET 1": "street_1",
    "STREET 2": "street_2",
    "CITY": "city",
    "STATE": "state",
    "ZIP CODE": "zip_code",
    "COUNTRY": "country",
    "REGION": "region",
    "MED CLASS": "med_class",
    "MED DATE": "med_date",
    "MED EXP DATE": "med_exp_date",
    "BASIC MED COURSE DATE": "basic_med_course_date",
    "BASIC MED CMEC DATE": "basic_med_cmec_date",
    # Cert CSVs (pilot + nonpilot)
    "TYPE": "certificate_type",
    "LEVEL": "certificate_level",
    "EXPIRE DATE": "certificate_expire_date",
    "RATING1": "rating1", "RATING2": "rating2", "RATING3": "rating3",
    "RATING4": "rating4", "RATING5": "rating5", "RATING6": "rating6",
    "RATING7": "rating7", "RATING8": "rating8", "RATING9": "rating9",
    "RATING10": "rating10", "RATING11": "rating11",
    # Pilot cert only (TYPERATING1..99)
    **{f"TYPERATING{i}": f"typerating{i}" for i in range(1, 100)},
}

# Provenance columns appended to every row (not from the CSV).
PROVENANCE_COLS = (
    "raw_source_row",
    "source_provider",
    "source_filename",
    "source_download_url",
    "source_observed_at",
    "source_run_metadata",
    "source_task_id",
    "source_schedule_id",
    "ingested_at",
)


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("faa-airmen-ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# DB URL
# --------------------------------------------------------------------------- #

def _database_url() -> str:
    """Prefer DEX_DB_URL_POOLED; fall back to DEX_DB_URL_DIRECT."""
    url = os.environ.get("DEX_DB_URL_POOLED") or os.environ.get("DEX_DB_URL_DIRECT")
    if not url:
        raise RuntimeError(
            "neither DEX_DB_URL_POOLED nor DEX_DB_URL_DIRECT is set — "
            "are you running under `doppler run` or inside a Modal function?"
        )
    return url


# --------------------------------------------------------------------------- #
# ZIP URL probe
# --------------------------------------------------------------------------- #

def _month_str(year: int, month: int) -> str:
    """Return MMYYYY string, e.g. month=5, year=2026 → '052026'."""
    return f"{month:02d}{year}"


def _resolve_zip_url(
    today: datetime,
    override_month: str | None = None,
) -> tuple[str, str, datetime | None]:
    """Probe for the current month's ZIP; fall back to prior month.

    Returns (url, filename, last_modified_dt).
    last_modified_dt may be None if the server omits Last-Modified.
    """
    if override_month:
        # Caller passed --month MMYYYY (e.g. '042026')
        candidates = [override_month]
    else:
        # Current month first, prior month as fallback.
        year, month = today.year, today.month
        current = _month_str(year, month)
        prior_month = month - 1 if month > 1 else 12
        prior_year = year if month > 1 else year - 1
        prior = _month_str(prior_year, prior_month)
        candidates = [current, prior]

    with httpx.Client(follow_redirects=True, timeout=30) as client:
        for mmyyyy in candidates:
            filename = f"CS{mmyyyy}.zip"
            url = BASE_URL + filename
            log.info("probing %s", url)
            try:
                # Use GET with Range: bytes=0-0 instead of HEAD.
                # Akamai on registry.faa.gov blocks HEAD requests (503/403)
                # but accepts range GETs. This fetches only the first byte.
                resp = client.get(
                    url,
                    headers={"User-Agent": USER_AGENT, "Range": "bytes=0-0"},
                )
            except httpx.HTTPError as exc:
                log.warning("probe GET %s failed: %s — trying next candidate", url, exc)
                continue
            # 200 (no range support) or 206 (partial content) both mean the file exists.
            if resp.status_code in (200, 206):
                last_modified: datetime | None = None
                lm_header = resp.headers.get("last-modified")
                if lm_header:
                    try:
                        last_modified = parsedate_to_datetime(lm_header)
                    except Exception:
                        log.warning("could not parse Last-Modified: %r", lm_header)
                log.info("resolved: %s (Last-Modified: %s)", url, last_modified)
                return url, filename, last_modified
            log.info("HTTP %s for %s — trying next candidate", resp.status_code, url)

    raise RuntimeError(
        f"could not resolve a valid FAA ZIP URL; tried: "
        + ", ".join(BASE_URL + f"CS{m}.zip" for m in candidates)
    )


# --------------------------------------------------------------------------- #
# ZIP download
# --------------------------------------------------------------------------- #

def _download_zip(url: str) -> Path:
    """Stream the ZIP to a temp file; return the path.

    FAA ZIP is ~150-300 MB; streaming to /tmp avoids OOM in a 4 GB container.
    """
    log.info("downloading %s", url)
    with httpx.stream("GET", url, headers={"User-Agent": USER_AGENT}, timeout=300, follow_redirects=True) as resp:
        resp.raise_for_status()
        content_length = resp.headers.get("content-length")
        if content_length:
            log.info("content-length: %s bytes", content_length)
        tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        bytes_written = 0
        for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
            tmp.write(chunk)
            bytes_written += len(chunk)
        tmp.flush()
        tmp.close()
    log.info("downloaded %d bytes to %s", bytes_written, tmp.name)
    return Path(tmp.name)


# --------------------------------------------------------------------------- #
# ops.faa_airmen_ingest_runs helpers
# --------------------------------------------------------------------------- #

def insert_run(
    conn: psycopg.Connection,
    filename: str,
    url: str,
    observed_at: datetime | None,
) -> str:
    """INSERT a 'running' row; return run_id (UUID str)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.faa_airmen_ingest_runs
              (status, source_filename, source_download_url, source_observed_at)
            VALUES ('running', %s, %s, %s)
            RETURNING run_id;
            """,
            (filename, url, observed_at),
        )
        run_id = str(cur.fetchone()[0])
    conn.commit()
    log.info("audit run_id=%s", run_id)
    return run_id


def finalize_run(
    conn: psycopg.Connection,
    run_id: str,
    *,
    status: str,
    rows_seen: dict[str, int],
    rows_upserted: dict[str, int],
    error_text: str | None = None,
) -> None:
    """UPDATE the run row with terminal status + counters."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ops.faa_airmen_ingest_runs SET
              status       = %s,
              rows_seen    = %s,
              rows_upserted = %s,
              completed_at = now(),
              error_text   = %s
            WHERE run_id = %s;
            """,
            (
                status,
                Jsonb(rows_seen),
                Jsonb(rows_upserted),
                error_text,
                run_id,
            ),
        )
    conn.commit()


# --------------------------------------------------------------------------- #
# Per-CSV processing
# --------------------------------------------------------------------------- #

def _build_stage_ddl(stage_table: str, typed_cols: tuple[str, ...]) -> str:
    """Build CREATE TEMP TABLE DDL for a staging table."""
    col_defs = "\n  ".join(f"{c} text," for c in typed_cols)
    prov_defs = (
        "  raw_source_row jsonb,\n"
        "  source_provider text,\n"
        "  source_filename text,\n"
        "  source_download_url text,\n"
        "  source_observed_at timestamptz,\n"
        "  source_run_metadata jsonb,\n"
        "  source_task_id text,\n"
        "  source_schedule_id text,\n"
        "  ingested_at timestamptz\n"
    )
    return (
        f"CREATE TEMP TABLE {stage_table} (\n"
        f"  {col_defs}\n"
        f"{prov_defs}"
        f") ON COMMIT DROP;"
    )


def process_csv(
    conn: psycopg.Connection,
    csv_basename: str,
    csv_fileobj: io.TextIOWrapper,
    *,
    source_filename: str,
    source_download_url: str,
    source_observed_at: datetime | None,
    run_id: str,
    max_rows: int | None = None,
) -> tuple[int, int]:
    """Process one CSV file. Returns (rows_seen, rows_upserted)."""
    table_name, pk_cols, typed_cols = CSV_TABLES[csv_basename]
    stage_table = f"_stage_faa_{csv_basename.lower().replace('.csv', '').replace('_', '')}"
    key = csv_basename.replace(".csv", "").lower()  # e.g. 'pilot_basic'

    log.info("processing %s → %s (pk=%s)", csv_basename, table_name, pk_cols)

    # Build provenance fields constant across all rows in this CSV.
    now_ts = datetime.now(timezone.utc)
    run_meta = {
        "run_id": run_id,
        "csv_basename": csv_basename,
    }
    task_id = os.environ.get("MODAL_TASK_ID")
    schedule_id = os.environ.get("MODAL_SCHEDULE_ID")

    all_copy_cols = tuple(typed_cols) + PROVENANCE_COLS

    reader = csv.DictReader(csv_fileobj)

    # Log the actual headers seen in this CSV so drift is auditable.
    log.info("%s actual headers: %r", csv_basename, reader.fieldnames)

    rows_seen = 0
    rows_upserted = 0

    # Stream in batches of BATCH_SIZE.
    batch: list[tuple] = []

    def _flush_batch(batch: list[tuple]) -> int:
        if not batch:
            return 0
        with conn.cursor() as cur:
            # Create/truncate temp staging table.
            cur.execute(f"CREATE TEMP TABLE IF NOT EXISTS {stage_table} AS SELECT * FROM {table_name} WHERE FALSE;")
            cur.execute(f"TRUNCATE {stage_table};")

            # COPY batch into staging.
            copy_cols_str = ", ".join(all_copy_cols)
            with cur.copy(f"COPY {stage_table} ({copy_cols_str}) FROM STDIN") as copy:
                for row_tuple in batch:
                    copy.write_row(row_tuple)

            # Upsert from staging into target.
            non_pk_typed = [c for c in typed_cols if c not in pk_cols]
            update_cols = non_pk_typed + [
                "raw_source_row", "source_provider", "source_filename",
                "source_download_url", "source_observed_at", "source_run_metadata",
                "source_task_id", "source_schedule_id",
            ]
            set_clause = ",\n                ".join(
                f"{c} = EXCLUDED.{c}" for c in update_cols
            )
            set_clause += ",\n                ingested_at = now()"

            conflict_target = ", ".join(pk_cols)

            # Build WHERE clause for IS DISTINCT FROM check (all typed + provenance data cols).
            check_cols = non_pk_typed + [
                "raw_source_row", "source_filename", "source_download_url",
            ]
            if check_cols:
                distinct_clauses = " OR ".join(
                    f"(t.{c} IS DISTINCT FROM EXCLUDED.{c})" for c in check_cols
                )
                where_clause = f"WHERE {distinct_clauses}"
            else:
                where_clause = ""

            cur.execute(f"""
                WITH ins AS (
                  INSERT INTO {table_name} AS t ({copy_cols_str})
                  SELECT {copy_cols_str} FROM {stage_table}
                  ON CONFLICT ({conflict_target}) DO UPDATE SET
                    {set_clause}
                  {where_clause}
                  RETURNING (xmax = 0) AS inserted
                )
                SELECT COUNT(*) FILTER (WHERE inserted),
                       COUNT(*) FROM ins;
            """)
            inserted, total = cur.fetchone()
        conn.commit()
        return int(inserted)

    for raw_row in reader:
        if max_rows is not None and rows_seen >= max_rows:
            break

        # Snake-case the CSV headers; skip unknown headers.
        # Strip whitespace AND BOM chars (U+FEFF) from headers — the FAA cert
        # CSVs may be UTF-8 BOM encoded while being read as latin-1, leaving
        # a stray \xef\xbb\xbf prefix on the first column.
        typed_vals: dict[str, str | None] = {}
        for header, value in raw_row.items():
            if header is None:
                continue
            normalized = header.strip().lstrip("﻿").strip()
            col = HEADER_TO_COLUMN.get(normalized)
            if col and col in typed_cols:
                typed_vals[col] = value.strip() if value else None

        # Build the row tuple in all_copy_cols order.
        row_tuple: tuple = (
            # typed columns (in typed_cols order)
            *tuple(typed_vals.get(c) for c in typed_cols),
            # provenance
            Jsonb(dict(raw_row)),       # raw_source_row
            PROVIDER,                   # source_provider
            source_filename,            # source_filename
            source_download_url,        # source_download_url
            source_observed_at,         # source_observed_at
            Jsonb(run_meta),            # source_run_metadata
            task_id,                    # source_task_id
            schedule_id,                # source_schedule_id
            now_ts,                     # ingested_at
        )

        batch.append(row_tuple)
        rows_seen += 1

        if len(batch) >= BATCH_SIZE:
            rows_upserted += _flush_batch(batch)
            batch = []
            log.info("  %s: %d rows seen so far", csv_basename, rows_seen)

    # Flush remaining rows.
    rows_upserted += _flush_batch(batch)

    log.info(
        "%s done: rows_seen=%d rows_upserted=%d",
        csv_basename, rows_seen, rows_upserted,
    )
    return rows_seen, rows_upserted


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> dict[str, Any]:
    """Entry point. Returns a dict with run_id, rows_seen, rows_upserted."""
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument(
        "--month",
        default=None,
        metavar="MMYYYY",
        help="Override ZIP month probe, e.g. '042026'",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Download and parse but do not write to DB",
    )
    p.add_argument(
        "--max-rows",
        type=int,
        default=None,
        metavar="N",
        help="Stop after N rows per CSV (smoke-test limit)",
    )
    args = p.parse_args(argv)

    today = datetime.now(timezone.utc)

    # Resolve URL.
    zip_url, zip_filename, observed_at = _resolve_zip_url(today, override_month=args.month)

    if args.dry_run:
        log.info("DRY RUN — resolved %s (Last-Modified: %s) — no DB writes", zip_url, observed_at)
        zip_path = _download_zip(zip_url)
        try:
            with zipfile.ZipFile(zip_path) as zf:
                names = zf.namelist()
                log.info("ZIP contents: %s", names)
                for basename in CSV_TABLES:
                    if basename in names:
                        with zf.open(basename) as f:
                            reader = csv.DictReader(io.TextIOWrapper(f, encoding="latin-1"))
                            first = next(iter(reader), None)
                            log.info("  %s headers: %s", basename, list(first.keys()) if first else "(empty)")
        finally:
            Path(zip_path).unlink(missing_ok=True)
        return {"dry_run": True, "url": zip_url}

    # Download.
    zip_path = _download_zip(zip_url)

    db_url = _database_url()
    rows_seen: dict[str, int] = {}
    rows_upserted: dict[str, int] = {}

    try:
        with psycopg.connect(db_url, autocommit=False) as conn:
            run_id = insert_run(conn, zip_filename, zip_url, observed_at)

            try:
                with zipfile.ZipFile(zip_path) as zf:
                    available = set(zf.namelist())
                    log.info("ZIP contains: %s", sorted(available))
                    for csv_basename in CSV_TABLES:
                        if csv_basename not in available:
                            log.warning("expected CSV not found in ZIP: %s — skipping", csv_basename)
                            key = csv_basename.replace(".csv", "").lower()
                            rows_seen[key] = 0
                            rows_upserted[key] = 0
                            continue
                        key = csv_basename.replace(".csv", "").lower()
                        with zf.open(csv_basename) as raw_f:
                            text_f = io.TextIOWrapper(raw_f, encoding="latin-1")
                            seen, upserted = process_csv(
                                conn,
                                csv_basename,
                                text_f,
                                source_filename=zip_filename,
                                source_download_url=zip_url,
                                source_observed_at=observed_at,
                                run_id=run_id,
                                max_rows=args.max_rows,
                            )
                            rows_seen[key] = seen
                            rows_upserted[key] = upserted

                finalize_run(
                    conn,
                    run_id,
                    status="succeeded",
                    rows_seen=rows_seen,
                    rows_upserted=rows_upserted,
                )

            except Exception as exc:
                log.exception("ingest failed: %s", exc)
                try:
                    conn.rollback()
                except Exception:
                    pass
                finalize_run(
                    conn,
                    run_id,
                    status="failed",
                    rows_seen=rows_seen,
                    rows_upserted=rows_upserted,
                    error_text=str(exc),
                )
                raise

    finally:
        Path(zip_path).unlink(missing_ok=True)

    log.info(
        "DONE — rows_seen=%s rows_upserted=%s",
        json.dumps(rows_seen),
        json.dumps(rows_upserted),
    )
    return {
        "run_id": run_id,
        "rows_seen": rows_seen,
        "rows_upserted": rows_upserted,
    }


if __name__ == "__main__":
    result = main()
    print(json.dumps(result, indent=2))
    sys.exit(0)

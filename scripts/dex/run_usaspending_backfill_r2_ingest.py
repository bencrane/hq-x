#!/usr/bin/env python3
"""USAspending historical → R2 Fuel Tank ingest.

Sources directly from the public USAspending bulk archive (S3 bucket
dti-usaspending-monthly-downloads, exposed at
https://files.usaspending.gov/award_data_archive/) — NOT from the existing
entities.usaspending_* Postgres tables. R2 is the canonical archive of
public-source bulk data; Postgres is a sibling destination.

Per-FY ZIP at
  https://files.usaspending.gov/award_data_archive/FY{YYYY}_All_{Stream}_Full_{YYYYMMDD}.zip
contains 1-N comma-delimited CSV chunks (~5-8 GB total uncompressed for
modern prime contracts). The publication date suffix rotates quarterly as
Treasury republishes each FY with corrections; the archive index is read at
runtime, latest publication wins.

Each invocation writes ONE ZSTD Parquet per (fiscal_year, stream) at
  s3://dex-raw-landing-zone/usaspending/{stream}/year=YYYY/data.parquet

The Parquet carries the 1:1 raw column mirror (all VARCHAR — see the
"Source ingest invariant" carve-out in apps/data-engine-x/CLAUDE.md), with
typed casts on a small set of hot columns (federal_action_obligation,
total_obligated_amount, action_date, etc.) and 7 normalized identity-spine
columns:

    recipient_name_normalized
    recipient_uei_normalized
    recipient_duns_normalized
    recipient_zip5
    recipient_state_normalized
    naics_2digit
    funding_agency_normalized

RisingWave wiring is DEFERRED — PR #213's existing source uses
match_pattern='usaspending/contracts/year=*/...' and absorbs the new years
automatically. Future RW directives will add cross-source recipient-bridge
MVs.

Streams supported:
  - contracts            (FY*_All_Contracts_Full_*.zip)
  - assistance           (FY*_All_Assistance_Full_*.zip)

Sub-awards (contract_subawards, assistance_subawards) are NOT in the public
bulk archive — they are only accessible via the per-day bulk-download API
and are out of scope for this directive.

Audit ledger: ops.usaspending_r2_ingest_runs.
Idempotency basis: HEAD Last-Modified per source_url.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_usaspending_backfill_r2_ingest.py \\
    --year 2023 --stream contracts
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_usaspending_backfill_r2_ingest.py \\
    --year 2023 --stream contracts --max-rows 50000
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_usaspending_backfill_r2_ingest.py \\
    --year 2008-2024 --stream all --skip-if-unchanged

See directive
~/Desktop/hq/directives/2026-05-08-usaspending-historical-backfill.md.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import sys
import time
import urllib.parse
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
import duckdb
import httpx
import psycopg
from psycopg.types.json import Jsonb

R2_BUCKET = "dex-raw-landing-zone"
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 5

ARCHIVE_INDEX_BASE = "https://files.usaspending.gov/award_data_archive/"

# Streams that exist as FY*_All_*_Full_*.zip in the public bulk archive.
SUPPORTED_STREAMS = ("contracts", "assistance")

# Default span — FFATA-era reporting starts FY2008. PR #213's daily-drip
# handles FY2025+ via bulk-download API.
DEFAULT_FY_START = 2008
DEFAULT_FY_END = 2024


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("usaspending-backfill")


log = _logger()


# --------------------------------------------------------------------------- #
# S3-bucket index discovery
# --------------------------------------------------------------------------- #


def discover_archive_index() -> dict[tuple[int, str], tuple[str, str]]:
    """Page through the bulk-archive S3 listing; return per (fy, stream) the
    LATEST publication's full URL plus the publication-date YYYYMMDD suffix.

    Returns:
        { (fiscal_year, stream): (full_url, publication_date_yyyymmdd) }
    """
    pattern = re.compile(
        r"^FY(\d{4})_All_(Contracts|Assistance)_Full_(\d{8})\.zip$"
    )

    all_keys: list[str] = []
    last_marker = ""
    with httpx.Client(headers={"User-Agent": "data-engine-x/usaspending-backfill"}) as client:
        while True:
            url = ARCHIVE_INDEX_BASE
            if last_marker:
                url = f"{url}?marker={urllib.parse.quote(last_marker)}"
            r = client.get(url, timeout=60.0)
            r.raise_for_status()
            xml = r.text
            keys = re.findall(r"<Key>([^<]+)</Key>", xml)
            if not keys:
                break
            all_keys.extend(keys)
            last_marker = keys[-1]
            if "<IsTruncated>true</IsTruncated>" not in xml:
                break

    # Group by (fy, stream); keep latest publication.
    grouped: dict[tuple[int, str], list[tuple[str, str]]] = {}
    for k in all_keys:
        m = pattern.match(k)
        if not m:
            continue
        fy = int(m.group(1))
        stream = m.group(2).lower()
        date = m.group(3)
        grouped.setdefault((fy, stream), []).append((date, k))

    out: dict[tuple[int, str], tuple[str, str]] = {}
    for key, entries in grouped.items():
        entries.sort(key=lambda x: x[0], reverse=True)
        date, fname = entries[0]
        out[key] = (ARCHIVE_INDEX_BASE + fname, date)

    return out


# --------------------------------------------------------------------------- #
# Job descriptor
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Job:
    fiscal_year: int
    stream: str  # "contracts" | "assistance"
    source_url: str
    publication_date: str

    @property
    def r2_prefix(self) -> str:
        return f"usaspending/{self.stream}/year={self.fiscal_year:04d}/"

    @property
    def r2_object_key(self) -> str:
        return self.r2_prefix + "data.parquet"


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #


def _required_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"{name} is not set in the environment.")
    return v


def _r2_client():
    return boto3.client(
        "s3",
        endpoint_url=_required_env("R2_ENDPOINT"),
        aws_access_key_id=_required_env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=_required_env("R2_SECRET_ACCESS_KEY"),
        region_name="auto",
    )


def _database_url() -> str:
    return _required_env("DEX_DB_URL_POOLED")


def head_url(client: httpx.Client, url: str) -> tuple[int | None, datetime | None, int]:
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = client.head(url, follow_redirects=True, timeout=30.0)
            if r.status_code == 404:
                return None, None, 404
            if r.status_code in RETRY_STATUSES:
                wait = min(2 ** attempt, 30)
                log.warning("HEAD %s HTTP %s; retry in %ss", url, r.status_code, wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            cl = int(r.headers.get("content-length", 0)) or None
            lm_raw = r.headers.get("last-modified")
            lm: datetime | None = None
            if lm_raw:
                try:
                    lm = datetime.strptime(
                        lm_raw, "%a, %d %b %Y %H:%M:%S %Z"
                    ).replace(tzinfo=timezone.utc)
                except ValueError:
                    lm = None
            return cl, lm, r.status_code
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            wait = min(2 ** attempt, 30)
            log.warning("HEAD %s error (%s); retry in %ss", url, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"HEAD failed: {last_exc}")


def download_zip(client: httpx.Client, url: str, dest: Path) -> int:
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            written = 0
            with client.stream("GET", url, follow_redirects=True, timeout=3600.0) as r:
                if r.status_code in RETRY_STATUSES:
                    wait = min(2 ** attempt, 30)
                    log.warning("GET %s HTTP %s; retry in %ss", url, r.status_code, wait)
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                with dest.open("wb") as f:
                    last_log = time.monotonic()
                    for chunk in r.iter_bytes(chunk_size=1 << 20):
                        f.write(chunk)
                        written += len(chunk)
                        now = time.monotonic()
                        if now - last_log >= 30.0:
                            log.info(
                                "  download progress: %.1f MB written",
                                written / (1 << 20),
                            )
                            last_log = now
            return written
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            wait = min(2 ** attempt, 30)
            log.warning("GET %s error (%s); retry in %ss", url, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"download failed: {last_exc}")


def extract_csvs(zip_path: Path, dest_dir: Path) -> tuple[list[Path], int]:
    """Extract every inner CSV from a ZIP. Returns (paths, total_uncompressed_bytes).

    USAspending FY ZIPs split into N chunks (e.g., FY2008 contracts has 5
    chunks of ~1.7GB each). Each chunk shares the same header.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    total_bytes = 0
    with zipfile.ZipFile(zip_path) as z:
        names = sorted(n for n in z.namelist() if n.lower().endswith(".csv"))
        if not names:
            raise RuntimeError(f"no CSVs in {zip_path.name}")
        for n in names:
            info = z.getinfo(n)
            target = dest_dir / Path(n).name
            with z.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst, length=1 << 20)
            paths.append(target)
            total_bytes += info.file_size
    return paths, total_bytes


# --------------------------------------------------------------------------- #
# DuckDB transform
# --------------------------------------------------------------------------- #


# DuckDB SQL macros mirroring scripts/_lib/usaspending_normalize.py. SQL
# applies the macros vector-at-a-time during the COPY ... TO Parquet step;
# the Python module is the reference + tested implementation. Both are
# expected to remain in lockstep — schema-drift surfaces as failed parquet-
# null-rate checks if they diverge.
_NORMALIZE_MACROS_SQL = r"""
CREATE MACRO usaspending_normalize_recipient_name(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(raw) = '' THEN NULL
    ELSE NULLIF(
      (
        WITH parts AS (
          SELECT string_split(
            trim(regexp_replace(
              regexp_replace(lower(raw), '[.,&]+', ' ', 'g'),
              '\s+', ' ', 'g'
            )),
            ' '
          ) AS p
        )
        SELECT CASE
          WHEN length(p) >= 2 AND p[length(p)] IN
               ('llc','inc','corp','corporation','ltd','limited',
                'lp','llp','pc','pa','pllc','co','company',
                'holdings','group','associates')
          THEN array_to_string(p[1:length(p)-1], ' ')
          ELSE array_to_string(p, ' ')
        END FROM parts
      ),
      ''
    )
  END
);

CREATE MACRO usaspending_normalize_uei(raw) AS (
  CASE
    WHEN raw IS NULL THEN NULL
    WHEN length(regexp_replace(raw, '[^A-Za-z0-9]', '', 'g')) <> 12 THEN NULL
    ELSE upper(regexp_replace(raw, '[^A-Za-z0-9]', '', 'g'))
  END
);

CREATE MACRO usaspending_normalize_duns(raw) AS (
  CASE
    WHEN raw IS NULL OR regexp_replace(raw, '\D', '', 'g') = '' THEN NULL
    WHEN length(regexp_replace(raw, '\D', '', 'g')) > 9 THEN NULL
    ELSE lpad(regexp_replace(raw, '\D', '', 'g'), 9, '0')
  END
);

CREATE MACRO usaspending_recipient_zip5(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(raw) = '' THEN NULL
    WHEN length(regexp_replace(raw, '\D', '', 'g')) < 5 THEN NULL
    ELSE substr(regexp_replace(raw, '\D', '', 'g'), 1, 5)
  END
);

CREATE MACRO usaspending_normalize_state(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(raw) = '' THEN NULL
    ELSE upper(trim(raw))
  END
);

CREATE MACRO usaspending_naics_2digit(raw) AS (
  CASE
    WHEN raw IS NULL OR length(trim(raw)) < 2 THEN NULL
    ELSE substr(trim(raw), 1, 2)
  END
);

CREATE MACRO usaspending_normalize_funding_agency(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(raw) = '' THEN NULL
    ELSE NULLIF(
      regexp_replace(upper(trim(raw)), '\s+', ' ', 'g'),
      ''
    )
  END
);
"""


def _register_normalizers(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(_NORMALIZE_MACROS_SQL)


def _typed_overrides() -> dict[str, str]:
    """Hot columns we promote out of all_varchar=TRUE.

    Only the subset that's both (a) common to BOTH contracts + assistance and
    (b) likely to be predicate-pushed by downstream MVs.
    """
    return {
        "federal_action_obligation": (
            "TRY_CAST(NULLIF(\"federal_action_obligation\", '') AS DOUBLE) "
            "AS federal_action_obligation"
        ),
        "action_date": (
            "TRY_CAST(TRY_STRPTIME("
            "  NULLIF(\"action_date\", ''), '%Y-%m-%d'"
            ") AS DATE) AS action_date"
        ),
        "period_of_performance_start_date": (
            "TRY_CAST(TRY_STRPTIME("
            "  NULLIF(\"period_of_performance_start_date\", ''), '%Y-%m-%d'"
            ") AS DATE) AS period_of_performance_start_date"
        ),
        "period_of_performance_current_end_date": (
            "TRY_CAST(TRY_STRPTIME("
            "  NULLIF(\"period_of_performance_current_end_date\", ''), '%Y-%m-%d'"
            ") AS DATE) AS period_of_performance_current_end_date"
        ),
        "last_modified_date": (
            "TRY_CAST("
            "  NULLIF(\"last_modified_date\", '') AS TIMESTAMP"
            ") AS last_modified_date"
        ),
    }


def csvs_to_parquet(
    csv_paths: list[Path],
    parquet_path: Path,
    *,
    job: Job,
    log_prefix: str,
    max_rows: int | None,
) -> tuple[int, int, int, dict[str, float]]:
    """Read all CSV chunks for the (year, stream); typed-cast hot columns;
    add normalized columns; write ZSTD Parquet.

    Returns (csv_chunk_count, rows_in_input, rows_in_parquet, null_rates).
    """
    con = duckdb.connect(":memory:")
    con.execute("PRAGMA threads=4;")
    con.execute("PRAGMA memory_limit='8GB';")
    _register_normalizers(con)

    # Build a multi-file glob in DuckDB-friendly form.
    # read_csv supports a list literal of paths; union_by_name handles any
    # cross-chunk schema drift defensively (chunks within one ZIP share a
    # schema in practice, but the option makes it forgiving).
    csv_list_sql = "[" + ", ".join(f"'{p}'" for p in csv_paths) + "]"
    con.execute(f"""
        CREATE VIEW raw AS
        SELECT * FROM read_csv(
          {csv_list_sql},
          delim=',', header=TRUE, quote='"',
          all_varchar=TRUE,
          ignore_errors=TRUE,
          union_by_name=TRUE,
          parallel=TRUE
        );
    """)

    rows_in_row = con.execute("SELECT count(*) FROM raw;").fetchone()
    rows_in = int(rows_in_row[0]) if rows_in_row else 0
    log.info("%s   raw rows across %d chunks: %s",
             log_prefix, len(csv_paths), f"{rows_in:,}")

    # Discover the column set DuckDB landed on.
    desc = con.execute("DESCRIBE SELECT * FROM raw;").fetchall()
    raw_cols = [row[0] for row in desc]

    overrides = _typed_overrides()
    select_parts: list[str] = []
    for col in raw_cols:
        # Skip columns we'll promote with a typed cast; emit the typed form.
        if col in overrides:
            continue
        # Quote the column name to survive any unusual chars.
        select_parts.append(f'"{col}"')

    for col, expr in overrides.items():
        if col in raw_cols:
            select_parts.append(expr)

    # Normalized identity-spine columns. Track which are emitted so the
    # post-write null-rate check can skip absent ones (assistance awards lack
    # naics_code; their schema differs from contracts).
    emitted_normalized: set[str] = set()
    if "recipient_name" in raw_cols:
        select_parts.append(
            "usaspending_normalize_recipient_name(\"recipient_name\") "
            "AS recipient_name_normalized"
        )
        emitted_normalized.add("recipient_name_normalized")
    if "recipient_uei" in raw_cols:
        select_parts.append(
            "usaspending_normalize_uei(\"recipient_uei\") AS recipient_uei_normalized"
        )
        emitted_normalized.add("recipient_uei_normalized")
    if "recipient_duns" in raw_cols:
        select_parts.append(
            "usaspending_normalize_duns(\"recipient_duns\") AS recipient_duns_normalized"
        )
        emitted_normalized.add("recipient_duns_normalized")
    if "recipient_zip_4_code" in raw_cols:
        select_parts.append(
            "usaspending_recipient_zip5(\"recipient_zip_4_code\") AS recipient_zip5"
        )
        emitted_normalized.add("recipient_zip5")
    elif "recipient_zip_code" in raw_cols:
        select_parts.append(
            "usaspending_recipient_zip5(\"recipient_zip_code\") AS recipient_zip5"
        )
        emitted_normalized.add("recipient_zip5")
    if "recipient_state_code" in raw_cols:
        select_parts.append(
            "usaspending_normalize_state(\"recipient_state_code\") "
            "AS recipient_state_normalized"
        )
        emitted_normalized.add("recipient_state_normalized")
    if "naics_code" in raw_cols:
        select_parts.append(
            "usaspending_naics_2digit(\"naics_code\") AS naics_2digit"
        )
        emitted_normalized.add("naics_2digit")
    if "funding_agency_name" in raw_cols:
        select_parts.append(
            "usaspending_normalize_funding_agency(\"funding_agency_name\") "
            "AS funding_agency_normalized"
        )
        emitted_normalized.add("funding_agency_normalized")

    # Partition metadata
    select_parts.append(
        f"CAST({job.fiscal_year} AS SMALLINT) AS usaspending_fy"
    )

    limit_clause = f"LIMIT {max_rows}" if max_rows is not None else ""
    select_sql = f"SELECT {', '.join(select_parts)} FROM raw {limit_clause}"

    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    con.execute(f"""
        COPY ({select_sql}) TO '{parquet_path}'
        (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000);
    """)
    log.info(
        "%s   parquet write: %.1f MB in %.1fs",
        log_prefix,
        parquet_path.stat().st_size / (1 << 20),
        time.monotonic() - t0,
    )

    # Per-job normalization null-rate sanity check (directive §5.4). Only
    # check columns we actually emitted — assistance awards lack naics_code,
    # for example.
    rate_check_columns = [
        "recipient_name_normalized",
        "recipient_uei_normalized",
        "recipient_duns_normalized",
        "recipient_zip5",
        "naics_2digit",
    ]
    null_filter_parts = ["count(*) AS total"]
    rate_keys: list[str] = []
    for col in rate_check_columns:
        if col in emitted_normalized:
            null_filter_parts.append(
                f"count(*) FILTER (WHERE {col} IS NULL) AS {col}_null"
            )
            rate_keys.append(col)
    rates_sql = (
        "SELECT " + ", ".join(null_filter_parts)
        + f" FROM read_parquet('{parquet_path}');"
    )
    rates_row = con.execute(rates_sql).fetchone()

    total = int(rates_row[0]) if rates_row else 0
    rows_pq = total
    rates: dict[str, float] = {}
    for i, col in enumerate(rate_keys, start=1):
        if total > 0:
            rates[f"{col}_null_pct"] = round(100.0 * int(rates_row[i]) / total, 4)
        else:
            rates[f"{col}_null_pct"] = 0.0
    log.info(
        "%s   parquet rows: %s; null-rates %s",
        log_prefix, f"{rows_pq:,}",
        ", ".join(f"{k}={v:.2f}%" for k, v in rates.items()),
    )
    parquet_columns = len(select_parts)
    con.close()
    return len(csv_paths), rows_in, rows_pq, rates, parquet_columns


def upload_to_r2(parquet_path: Path, *, bucket: str, key: str) -> int:
    s3 = _r2_client()
    file_bytes = parquet_path.stat().st_size
    s3.upload_file(
        str(parquet_path), bucket, key,
        ExtraArgs={"ContentType": "application/x-parquet"},
    )
    return file_bytes


# --------------------------------------------------------------------------- #
# Audit-row helpers
# --------------------------------------------------------------------------- #


def get_prior_source_last_modified(
    conn: psycopg.Connection, job: Job,
) -> datetime | None:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT source_last_modified
              FROM ops.usaspending_r2_ingest_runs
             WHERE fiscal_year = %s AND stream = %s AND status = 'completed'
             ORDER BY started_at DESC LIMIT 1
            """,
            (job.fiscal_year, job.stream),
        )
        row = cur.fetchone()
    return row[0] if row else None


def insert_run_row(
    conn: psycopg.Connection,
    job: Job,
    *,
    source_last_modified: datetime | None,
    prior_source_last_modified: datetime | None,
) -> str:
    sql = """
    INSERT INTO ops.usaspending_r2_ingest_runs (
        fiscal_year, stream, status, source_url,
        source_last_modified, prior_source_last_modified
    ) VALUES (%s, %s, 'running', %s, %s, %s)
    RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            job.fiscal_year, job.stream, job.source_url,
            source_last_modified, prior_source_last_modified,
        ))
        row_id = cur.fetchone()[0]
    conn.commit()
    return str(row_id)


def write_no_change_run(
    conn: psycopg.Connection,
    job: Job,
    *,
    source_last_modified: datetime | None,
    prior_source_last_modified: datetime | None,
) -> None:
    started = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ops.usaspending_r2_ingest_runs (
                fiscal_year, stream, status, source_url,
                source_last_modified, prior_source_last_modified,
                started_at, finished_at, duration_seconds, notes
            ) VALUES (%s, %s, 'no_change', %s, %s, %s, %s, %s, 0, %s);
            """,
            (
                job.fiscal_year, job.stream, job.source_url,
                source_last_modified, prior_source_last_modified,
                started, started,
                Jsonb({"reason": "source_last_modified unchanged"}),
            ),
        )
    conn.commit()


def finalize_run_row(
    conn: psycopg.Connection,
    run_id: str,
    *,
    status: str,
    zip_bytes: int,
    csv_bytes: int,
    csv_rows: int,
    csv_chunks: int,
    parquet_rows: int,
    parquet_bytes: int,
    parquet_columns: int,
    r2_bucket: str | None,
    r2_prefix: str | None,
    r2_object_key: str | None,
    r2_total_bytes: int,
    null_rates: dict[str, float] | None,
    started_at: float,
    error_message: str | None,
    notes: dict[str, Any] | None,
) -> None:
    duration = round(time.monotonic() - started_at, 3)
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE ops.usaspending_r2_ingest_runs
               SET status = %s,
                   zip_bytes_downloaded = %s,
                   csv_bytes_uncompressed = %s,
                   csv_row_count = %s,
                   csv_chunk_count = %s,
                   parquet_row_count = %s,
                   parquet_bytes_written = %s,
                   parquet_column_count = %s,
                   r2_bucket = %s, r2_prefix = %s, r2_object_key = %s,
                   r2_total_bytes = %s,
                   recipient_name_normalized_null_pct = %s,
                   recipient_uei_normalized_null_pct = %s,
                   recipient_duns_normalized_null_pct = %s,
                   recipient_zip5_null_pct = %s,
                   naics_2digit_null_pct = %s,
                   finished_at = now(), duration_seconds = %s,
                   error_message = %s, notes = %s
             WHERE id = %s;
            """, (
            status, zip_bytes, csv_bytes, csv_rows, csv_chunks,
            parquet_rows, parquet_bytes, parquet_columns,
            r2_bucket, r2_prefix, r2_object_key, r2_total_bytes,
            (null_rates or {}).get("recipient_name_normalized_null_pct"),
            (null_rates or {}).get("recipient_uei_normalized_null_pct"),
            (null_rates or {}).get("recipient_duns_normalized_null_pct"),
            (null_rates or {}).get("recipient_zip5_null_pct"),
            (null_rates or {}).get("naics_2digit_null_pct"),
            duration, error_message,
            Jsonb(notes) if notes else None, run_id,
        ))
    conn.commit()


# --------------------------------------------------------------------------- #
# Per-job main
# --------------------------------------------------------------------------- #


def ingest_job(
    job: Job,
    *,
    skip_if_unchanged: bool,
    dry_run: bool,
    workdir: Path,
    max_rows: int | None,
    r2_prefix_override: str | None,
) -> int:
    log_prefix = f"[fy={job.fiscal_year} stream={job.stream}]"
    started_wall = time.monotonic()
    log.info("%s start url=%s", log_prefix, job.source_url)

    with httpx.Client(headers={"User-Agent": "data-engine-x/usaspending-backfill"}) as client:
        try:
            content_length, source_last_modified, status_code = head_url(
                client, job.source_url,
            )
        except Exception:
            log.exception("%s HEAD failed", log_prefix)
            return 1
        if status_code == 404:
            log.error("%s HEAD 404 — file not published", log_prefix)
            return 1
        log.info(
            "%s HEAD content_length=%s last_modified=%s",
            log_prefix, content_length, source_last_modified,
        )
        if dry_run:
            log.info("%s DRY RUN — exiting after HEAD", log_prefix)
            return 0

        with psycopg.connect(_database_url()) as conn:
            prior = get_prior_source_last_modified(conn, job)
            log.info("%s prior source_last_modified: %s", log_prefix, prior)
            if (
                skip_if_unchanged
                and prior is not None
                and source_last_modified is not None
                and source_last_modified <= prior
            ):
                log.info("%s source unchanged — recording no_change", log_prefix)
                write_no_change_run(
                    conn, job,
                    source_last_modified=source_last_modified,
                    prior_source_last_modified=prior,
                )
                return 0

            run_id = insert_run_row(
                conn, job,
                source_last_modified=source_last_modified,
                prior_source_last_modified=prior,
            )
            log.info("%s run id: %s", log_prefix, run_id)

            zip_path = workdir / f"usaspending_{job.stream}_{job.fiscal_year}.zip"
            extract_dir = workdir / f"usaspending_{job.stream}_{job.fiscal_year}"
            parquet_path = workdir / f"usaspending_{job.stream}_{job.fiscal_year}.parquet"

            try:
                zip_bytes = download_zip(client, job.source_url, zip_path)
                log.info("%s downloaded %d bytes", log_prefix, zip_bytes)

                csv_paths, csv_bytes = extract_csvs(zip_path, extract_dir)
                log.info(
                    "%s extracted %d CSV chunks (%.1f MB uncompressed)",
                    log_prefix, len(csv_paths), csv_bytes / (1 << 20),
                )

                csv_chunks, rows_in, rows_pq, null_rates, parquet_columns = \
                    csvs_to_parquet(
                        csv_paths, parquet_path,
                        job=job, log_prefix=log_prefix, max_rows=max_rows,
                    )
                # Validation gate: row-count parity.
                if max_rows is None and rows_in > 0:
                    variance = abs(rows_pq - rows_in) / rows_in
                    if variance > 0.001:
                        raise RuntimeError(
                            f"row-count variance {variance:.4%} > 0.1% "
                            f"(in={rows_in:,} pq={rows_pq:,})"
                        )

                target_prefix = r2_prefix_override or job.r2_prefix
                target_key = (
                    target_prefix.rstrip("/") + "/data.parquet"
                    if r2_prefix_override
                    else job.r2_object_key
                )
                uploaded = upload_to_r2(
                    parquet_path, bucket=R2_BUCKET, key=target_key,
                )
                log.info(
                    "%s uploaded → s3://%s/%s (%.1f MB)",
                    log_prefix, R2_BUCKET, target_key, uploaded / (1 << 20),
                )

                finalize_run_row(
                    conn, run_id, status="completed",
                    zip_bytes=zip_bytes,
                    csv_bytes=csv_bytes, csv_rows=rows_in, csv_chunks=csv_chunks,
                    parquet_rows=rows_pq, parquet_bytes=uploaded,
                    parquet_columns=parquet_columns,
                    r2_bucket=R2_BUCKET,
                    r2_prefix=target_prefix,
                    r2_object_key=target_key,
                    r2_total_bytes=uploaded,
                    null_rates=null_rates,
                    started_at=started_wall, error_message=None,
                    notes={
                        "max_rows": max_rows,
                        "r2_prefix_override": r2_prefix_override,
                        "publication_date": job.publication_date,
                        "csv_chunk_filenames": [p.name for p in csv_paths],
                    },
                )
                log.info(
                    "%s DONE rows=%s upload=%.1f MB wall=%.1fs",
                    log_prefix, f"{rows_pq:,}",
                    uploaded / (1 << 20),
                    time.monotonic() - started_wall,
                )
                return 0

            except Exception as exc:
                log.exception("%s ingest failed", log_prefix)
                finalize_run_row(
                    conn, run_id, status="failed",
                    zip_bytes=0, csv_bytes=0, csv_rows=0, csv_chunks=0,
                    parquet_rows=0, parquet_bytes=0, parquet_columns=0,
                    r2_bucket=None, r2_prefix=None, r2_object_key=None,
                    r2_total_bytes=0,
                    null_rates=None,
                    started_at=started_wall,
                    error_message=str(exc), notes=None,
                )
                return 1

            finally:
                try:
                    zip_path.unlink(missing_ok=True)
                except Exception:
                    pass
                try:
                    parquet_path.unlink(missing_ok=True)
                except Exception:
                    pass
                shutil.rmtree(extract_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_year_arg(s: str) -> list[int]:
    if "-" in s:
        a, b = s.split("-", 1)
        ya, yb = int(a), int(b)
    else:
        ya = yb = int(s)
    return [y for y in range(ya, yb + 1)
            if DEFAULT_FY_START <= y <= DEFAULT_FY_END]


def parse_streams(s: str) -> list[str]:
    if s == "all":
        return list(SUPPORTED_STREAMS)
    streams = [x.strip() for x in s.split(",") if x.strip()]
    for x in streams:
        if x not in SUPPORTED_STREAMS:
            raise SystemExit(
                f"stream={x!r} not supported; valid: {SUPPORTED_STREAMS}"
            )
    return streams


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--year", required=True,
                   help="FY (e.g., 2023) or range (e.g., 2008-2024).")
    p.add_argument("--stream", required=True,
                   help="contracts | assistance | all | comma-separated subset.")
    p.add_argument("--skip-if-unchanged", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max-rows", type=int, default=None)
    p.add_argument("--workdir", default=None)
    p.add_argument("--r2-prefix-override", default=None,
                   help="Replace canonical usaspending/{stream}/year=YYYY/ "
                        "prefix (smoke-test use).")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    workdir = Path(args.workdir or "/tmp/usaspending_r2_ingest")
    workdir.mkdir(parents=True, exist_ok=True)

    years = parse_year_arg(args.year)
    streams = parse_streams(args.stream)
    if not years:
        log.error("year arg %r yielded no FYs in supported span %d-%d",
                  args.year, DEFAULT_FY_START, DEFAULT_FY_END)
        return 2

    log.info("discovering archive index ...")
    archive_index = discover_archive_index()
    log.info("archive_index: %d (fy, stream) entries", len(archive_index))

    rc = 0
    for y in years:
        for s in streams:
            entry = archive_index.get((y, s))
            if entry is None:
                log.error("(fy=%d, stream=%s) not in archive index — skipping", y, s)
                rc = 1
                continue
            url, pub = entry
            job = Job(fiscal_year=y, stream=s, source_url=url, publication_date=pub)
            log.info("=" * 70)
            log.info("=== INGEST: fy=%d stream=%s pub=%s ===", y, s, pub)
            log.info("=" * 70)
            rc_one = ingest_job(
                job,
                skip_if_unchanged=args.skip_if_unchanged,
                dry_run=args.dry_run,
                workdir=workdir,
                max_rows=args.max_rows,
                r2_prefix_override=args.r2_prefix_override,
            )
            if rc_one != 0:
                rc = rc_one
                log.error("(fy=%d, stream=%s) failed; continuing", y, s)
    return rc


if __name__ == "__main__":
    sys.exit(main())

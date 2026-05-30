#!/usr/bin/env python3
"""FEC Individual Contributions historical → R2 Fuel Tank ingest.

The largest public US name→address corpus: every itemized contribution >$200
since the 1979-1980 election cycle (FECA §30104). 24 two-year cycles
(1980-2026), ~300-500M rows total.

Per-cycle ZIP at `https://www.fec.gov/files/bulk-downloads/{YYYY}/indiv{YY}.zip`
contains a single pipe-delimited `itcont.txt` with NO header. Headers come
from the separate published CSV
`https://www.fec.gov/files/bulk-downloads/data_dictionaries/indiv_header_file.csv`
(21 columns, stable since 1980).

Each invocation writes ONE ZSTD Parquet per cycle at
`s3://dex-raw-landing-zone/fec/cycle=YYYY/indiv.parquet`.

The Parquet carries 21 raw columns (preserved as VARCHAR — leading zeros on
ZIP codes and sentinel strings like "INFORMATION REQUESTED" stay intact),
typed `transaction_amt` (DOUBLE), `transaction_dt` (DATE via
TRY_STRPTIME(`%m%d%Y`)), `sub_id` (BIGINT, natural PK), four normalized
identity-spine columns (name_normalized, employer_normalized, zip5,
occupation_normalized), and `cycle_year` (SMALLINT partition metadata).

RisingWave wiring is DEFERRED to a follow-up directive — this script lands
canonical R2 Parquet only.

Audit ledger: `ops.fec_r2_ingest_runs`. Idempotency basis: HEAD Last-Modified.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_fec_individual_contributions_r2_ingest.py 2024
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_fec_individual_contributions_r2_ingest.py 2024 --dry-run
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_fec_individual_contributions_r2_ingest.py 2024 --max-rows 50000

Special form:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_fec_individual_contributions_r2_ingest.py --all
  Default span: 1980 through 2026 — all 24 even-numbered election cycles.

See directive
~/Desktop/hq/directives/2026-05-08-fec-individual-contributions-r2-ingest.md.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import time
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

HEADER_URL = (
    "https://www.fec.gov/files/bulk-downloads/data_dictionaries/"
    "indiv_header_file.csv"
)

# 21 published columns per FEC's indiv_header_file.csv. Pinned here because
# the published CSV has been stable since 1980; if it ever drifts, the
# script's HEAD-of-header check will surface the divergence.
EXPECTED_COLUMNS: tuple[str, ...] = (
    "CMTE_ID", "AMNDT_IND", "RPT_TP", "TRANSACTION_PGI", "IMAGE_NUM",
    "TRANSACTION_TP", "ENTITY_TP", "NAME", "CITY", "STATE",
    "ZIP_CODE", "EMPLOYER", "OCCUPATION", "TRANSACTION_DT",
    "TRANSACTION_AMT", "OTHER_ID", "TRAN_ID", "FILE_NUM", "MEMO_CD",
    "MEMO_TEXT", "SUB_ID",
)

# All 24 even-numbered election cycles 1980-2026.
DEFAULT_SPAN: tuple[int, ...] = tuple(range(1980, 2027, 2))


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("fec-r2-ingest")


log = _logger()


@dataclass(frozen=True)
class Cycle:
    year: int

    @property
    def yy(self) -> str:
        return f"{self.year % 100:02d}"

    @property
    def url(self) -> str:
        return (
            f"https://www.fec.gov/files/bulk-downloads/{self.year}/"
            f"indiv{self.yy}.zip"
        )

    @property
    def r2_prefix(self) -> str:
        return f"fec/cycle={self.year}/"

    @property
    def r2_object_key(self) -> str:
        return self.r2_prefix + "indiv.parquet"


def _required_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"{name} is not set in the environment.")
    return v


def _r2_client() -> "boto3.client":
    return boto3.client(
        "s3",
        endpoint_url=_required_env("R2_ENDPOINT"),
        aws_access_key_id=_required_env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=_required_env("R2_SECRET_ACCESS_KEY"),
        region_name="auto",
    )


def _database_url() -> str:
    return _required_env("DEX_DB_URL_POOLED")


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #


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
                        if now - last_log >= 15.0:
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


def fetch_header_columns(client: httpx.Client) -> tuple[str, ...]:
    """Download the published header CSV and return its columns. Falls back
    to EXPECTED_COLUMNS if fetch fails (the schema has been stable for 40+
    years; the network call is a sanity check, not a hard dependency).
    """
    try:
        r = client.get(HEADER_URL, follow_redirects=True, timeout=30.0)
        r.raise_for_status()
        line = r.text.splitlines()[0]
        cols = tuple(c.strip() for c in line.split(",") if c.strip())
        if cols != EXPECTED_COLUMNS:
            log.warning(
                "header drift: published=%s, pinned=%s — using published",
                cols, EXPECTED_COLUMNS,
            )
        return cols
    except Exception as exc:
        log.warning("header fetch failed (%s) — using pinned EXPECTED_COLUMNS", exc)
        return EXPECTED_COLUMNS


def extract_itcont(zip_path: Path, dest_dir: Path) -> tuple[Path, int]:
    """Extract the single inner `itcont.txt` from a cycle ZIP. Returns
    (path_to_itcont, uncompressed_bytes)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        names = [n for n in z.namelist() if n.lower().endswith("itcont.txt")]
        if not names:
            raise RuntimeError(f"no itcont.txt inside {zip_path.name}")
        if len(names) > 1:
            log.warning("multiple itcont.txt entries in ZIP — using %s", names[0])
        info = z.getinfo(names[0])
        target = dest_dir / "itcont.txt"
        with z.open(info) as src, target.open("wb") as dst:
            shutil.copyfileobj(src, dst, length=1 << 20)
        return target, info.file_size


# --------------------------------------------------------------------------- #
# DuckDB transform
# --------------------------------------------------------------------------- #


_NORMALIZE_MACROS_SQL = r"""
CREATE MACRO fec_normalize_name(raw) AS (
  NULLIF(
    trim(regexp_replace(
      regexp_replace(
        lower(
          CASE
            WHEN raw IS NULL OR trim(raw) = '' THEN ''
            WHEN strpos(trim(raw), ',') > 0 THEN
              ltrim(substr(trim(raw), strpos(trim(raw), ',') + 1)) || ' ' ||
              rtrim(substr(trim(raw), 1, strpos(trim(raw), ',') - 1))
            ELSE trim(raw)
          END
        ),
        '[,.]+', ' ', 'g'
      ),
      '\s+', ' ', 'g'
    )),
    ''
  )
);

CREATE MACRO fec_zip5(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(raw) = '' THEN NULL
    WHEN length(regexp_replace(raw, '\D', '', 'g')) < 5 THEN NULL
    ELSE substr(regexp_replace(raw, '\D', '', 'g'), 1, 5)
  END
);

CREATE MACRO fec_normalize_occupation(raw) AS (
  NULLIF(
    trim(regexp_replace(lower(coalesce(raw, '')), '\s+', ' ', 'g')),
    ''
  )
);

-- Strips ONE trailing org suffix word — list mirrors
-- scripts/_lib/fec_normalize.py:_EMPLOYER_SUFFIXES exactly. Tested via
-- tests/scripts/test_fec_normalize_sql_parity.py (SQL ↔ Python).
CREATE MACRO fec_normalize_employer(raw) AS (
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
                'lp','llp','pc','pa','pllc','co','company')
          THEN array_to_string(p[1:length(p)-1], ' ')
          ELSE array_to_string(p, ' ')
        END FROM parts
      ),
      ''
    )
  END
);
"""


def _register_normalizers(con: duckdb.DuckDBPyConnection) -> None:
    """Register the four normalization macros at the connection scope.

    Pure-SQL via CREATE MACRO: vectorized at plan time, no Python UDF
    overhead, no numpy dep. The Python module
    `scripts/_lib/fec_normalize.py` is the reference implementation; the
    SQL macros track it via tests/scripts/test_fec_normalize_sql_parity.py.
    """
    con.execute(_NORMALIZE_MACROS_SQL)


def itcont_to_parquet(
    txt_path: Path,
    parquet_path: Path,
    *,
    cycle: Cycle,
    columns: tuple[str, ...],
    log_prefix: str,
    max_rows: int | None,
) -> tuple[int, int, dict[str, float]]:
    """Read pipe-delimited itcont.txt as VARCHAR, project + normalize, write
    ZSTD Parquet.

    Returns (rows_in_input, rows_in_parquet, normalization_null_rates).
    """
    con = duckdb.connect(":memory:")
    con.execute("PRAGMA threads=4;")
    con.execute("PRAGMA memory_limit='6GB';")
    _register_normalizers(con)

    # FEC bulk: pipe-delimited, NO header, no quoting. all_varchar=TRUE
    # preserves leading zeros on ZIP codes + sentinels in EMPLOYER/OCCUPATION.
    # null_padding=TRUE handles short rows in older cycles. ignore_errors
    # survives malformed historical rows.
    column_list = ", ".join(f"'{c}'" for c in columns)
    con.execute(f"""
        CREATE VIEW raw AS
        SELECT * FROM read_csv(
          '{txt_path}',
          delim='|', header=FALSE, quote='',
          all_varchar=TRUE,
          ignore_errors=TRUE, null_padding=TRUE,
          names=[{column_list}]
        );
    """)

    rows_in_row = con.execute("SELECT count(*) FROM raw;").fetchone()
    rows_in = int(rows_in_row[0]) if rows_in_row else 0
    log.info("%s   itcont raw rows: %s", log_prefix, f"{rows_in:,}")

    # Build the projection.
    raw_select = ", ".join(f'"{c}" AS "{c.lower()}"' for c in columns)

    # Override typed casts on three columns at the SELECT level (the raw
    # VARCHAR copies are also kept under the original lowercase names; we
    # replace those three with their typed forms and append the normalized
    # / partition columns).
    typed_overrides = {
        "transaction_amt": "TRY_CAST(NULLIF(\"TRANSACTION_AMT\", '') AS DOUBLE) AS transaction_amt",
        "transaction_dt": (
            "TRY_CAST(TRY_STRPTIME(NULLIF(\"TRANSACTION_DT\", ''), '%m%d%Y') AS DATE) "
            "AS transaction_dt"
        ),
        "sub_id": "TRY_CAST(NULLIF(\"SUB_ID\", '') AS BIGINT) AS sub_id",
    }
    select_parts: list[str] = []
    for c in columns:
        lc = c.lower()
        if lc in typed_overrides:
            select_parts.append(typed_overrides[lc])
        else:
            select_parts.append(f'"{c}" AS "{lc}"')

    # Normalized identity-spine columns.
    select_parts.extend([
        'fec_normalize_name("NAME") AS name_normalized',
        'fec_normalize_employer("EMPLOYER") AS employer_normalized',
        'fec_zip5("ZIP_CODE") AS zip5',
        'fec_normalize_occupation("OCCUPATION") AS occupation_normalized',
        f"CAST({cycle.year} AS SMALLINT) AS cycle_year",
    ])

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

    # Per-cycle normalization null-rate sanity check (directive §4.7).
    rates_row = con.execute(f"""
        SELECT
          count(*) AS total,
          count(*) FILTER (WHERE name_normalized IS NULL) AS name_null,
          count(*) FILTER (WHERE zip5 IS NULL) AS zip5_null,
          count(*) FILTER (WHERE employer_normalized IS NULL) AS emp_null
        FROM read_parquet('{parquet_path}');
    """).fetchone()
    total = int(rates_row[0]) if rates_row else 0
    rows_pq = total
    rates: dict[str, float]
    if total > 0:
        rates = {
            "name_normalized_null_pct": round(100.0 * int(rates_row[1]) / total, 4),
            "zip5_null_pct": round(100.0 * int(rates_row[2]) / total, 4),
            "employer_normalized_null_pct": round(100.0 * int(rates_row[3]) / total, 4),
        }
    else:
        rates = {
            "name_normalized_null_pct": 0.0,
            "zip5_null_pct": 0.0,
            "employer_normalized_null_pct": 0.0,
        }
    log.info(
        "%s   parquet rows: %s; null-rate name=%.2f%% zip5=%.2f%% employer=%.2f%%",
        log_prefix, f"{rows_pq:,}",
        rates["name_normalized_null_pct"], rates["zip5_null_pct"],
        rates["employer_normalized_null_pct"],
    )
    con.close()
    return rows_in, rows_pq, rates


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


def insert_run_row(
    conn: psycopg.Connection,
    cycle: Cycle,
    *,
    source_url: str,
    source_last_modified: datetime | None,
    prior_source_last_modified: datetime | None,
) -> str:
    sql = """
    INSERT INTO ops.fec_r2_ingest_runs (
        cycle_year, status, source_url,
        source_last_modified, prior_source_last_modified
    ) VALUES (%s, 'running', %s, %s, %s)
    RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            cycle.year, source_url,
            source_last_modified, prior_source_last_modified,
        ))
        row_id = cur.fetchone()[0]
    conn.commit()
    return str(row_id)


def get_prior_source_last_modified(
    conn: psycopg.Connection, cycle: Cycle,
) -> datetime | None:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT source_last_modified
              FROM ops.fec_r2_ingest_runs
             WHERE cycle_year = %s AND status = 'completed'
             ORDER BY started_at DESC LIMIT 1
            """,
            (cycle.year,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def write_no_change_run(
    conn: psycopg.Connection,
    cycle: Cycle,
    *,
    source_url: str,
    source_last_modified: datetime | None,
    prior_source_last_modified: datetime | None,
) -> None:
    started = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ops.fec_r2_ingest_runs (
                cycle_year, status, source_url,
                source_last_modified, prior_source_last_modified,
                started_at, finished_at, duration_seconds, notes
            ) VALUES (%s, 'no_change', %s, %s, %s, %s, %s, 0, %s);
            """,
            (
                cycle.year, source_url, source_last_modified,
                prior_source_last_modified, started, started,
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
    itcont_bytes: int,
    itcont_rows: int,
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
            UPDATE ops.fec_r2_ingest_runs
               SET status = %s,
                   zip_bytes_downloaded = %s,
                   itcont_bytes_uncompressed = %s,
                   itcont_row_count = %s,
                   parquet_row_count = %s,
                   parquet_bytes_written = %s,
                   parquet_column_count = %s,
                   r2_bucket = %s, r2_prefix = %s, r2_object_key = %s,
                   r2_total_bytes = %s,
                   name_normalized_null_pct = %s,
                   zip5_null_pct = %s,
                   employer_normalized_null_pct = %s,
                   finished_at = now(), duration_seconds = %s,
                   error_message = %s, notes = %s
             WHERE id = %s;
            """, (
            status, zip_bytes, itcont_bytes, itcont_rows,
            parquet_rows, parquet_bytes, parquet_columns,
            r2_bucket, r2_prefix, r2_object_key, r2_total_bytes,
            (null_rates or {}).get("name_normalized_null_pct"),
            (null_rates or {}).get("zip5_null_pct"),
            (null_rates or {}).get("employer_normalized_null_pct"),
            duration, error_message,
            Jsonb(notes) if notes else None, run_id,
        ))
    conn.commit()


# --------------------------------------------------------------------------- #
# Per-cycle main
# --------------------------------------------------------------------------- #


def ingest_cycle(
    cycle: Cycle,
    *,
    skip_if_unchanged: bool,
    dry_run: bool,
    workdir: Path,
    max_rows: int | None,
    r2_prefix_override: str | None,
    columns: tuple[str, ...],
) -> int:
    log_prefix = f"[cycle={cycle.year}]"
    started_wall = time.monotonic()
    log.info("%s start url=%s", log_prefix, cycle.url)

    with httpx.Client(headers={"User-Agent": "data-engine-x/fec-r2-ingest"}) as client:
        try:
            content_length, source_last_modified, status_code = head_url(
                client, cycle.url,
            )
        except Exception:
            log.exception("%s HEAD failed", log_prefix)
            return 1
        if status_code == 404:
            log.error("%s HEAD 404 — cycle ZIP not published", log_prefix)
            return 1
        log.info("%s HEAD content_length=%s last_modified=%s",
                 log_prefix, content_length, source_last_modified)
        if dry_run:
            log.info("%s DRY RUN — exiting after HEAD", log_prefix)
            return 0

        with psycopg.connect(_database_url()) as conn:
            prior = get_prior_source_last_modified(conn, cycle)
            log.info("%s prior source_last_modified: %s", log_prefix, prior)
            if (
                skip_if_unchanged
                and prior is not None
                and source_last_modified is not None
                and source_last_modified <= prior
            ):
                log.info("%s source unchanged — recording no_change", log_prefix)
                write_no_change_run(
                    conn, cycle, source_url=cycle.url,
                    source_last_modified=source_last_modified,
                    prior_source_last_modified=prior,
                )
                return 0

            run_id = insert_run_row(
                conn, cycle, source_url=cycle.url,
                source_last_modified=source_last_modified,
                prior_source_last_modified=prior,
            )
            log.info("%s run id: %s", log_prefix, run_id)

            zip_path = workdir / f"fec_indiv_{cycle.year}.zip"
            extract_dir = workdir / f"fec_indiv_{cycle.year}"
            parquet_path = workdir / f"fec_indiv_{cycle.year}.parquet"

            try:
                zip_bytes = download_zip(client, cycle.url, zip_path)
                log.info("%s downloaded %d bytes", log_prefix, zip_bytes)

                txt_path, itcont_bytes = extract_itcont(zip_path, extract_dir)
                log.info(
                    "%s extracted itcont.txt (%.1f MB uncompressed)",
                    log_prefix, itcont_bytes / (1 << 20),
                )

                rows_in, rows_pq, null_rates = itcont_to_parquet(
                    txt_path, parquet_path,
                    cycle=cycle, columns=columns,
                    log_prefix=log_prefix, max_rows=max_rows,
                )
                # Validation gate: row-count parity (max_rows path skips this).
                if max_rows is None and rows_in > 0:
                    variance = abs(rows_pq - rows_in) / rows_in
                    if variance > 0.001:
                        raise RuntimeError(
                            f"row-count variance {variance:.4%} > 0.1% "
                            f"(in={rows_in:,} pq={rows_pq:,})"
                        )

                target_prefix = r2_prefix_override or cycle.r2_prefix
                target_key = (
                    target_prefix.rstrip("/") + "/indiv.parquet"
                    if r2_prefix_override
                    else cycle.r2_object_key
                )
                uploaded = upload_to_r2(
                    parquet_path, bucket=R2_BUCKET, key=target_key,
                )
                log.info(
                    "%s uploaded → s3://%s/%s (%.1f MB)",
                    log_prefix, R2_BUCKET, target_key, uploaded / (1 << 20),
                )

                # Column count = 21 raw + 4 normalized + cycle_year = 26.
                column_count = len(columns) + 5
                finalize_run_row(
                    conn, run_id, status="completed",
                    zip_bytes=zip_bytes,
                    itcont_bytes=itcont_bytes,
                    itcont_rows=rows_in,
                    parquet_rows=rows_pq,
                    parquet_bytes=uploaded,
                    parquet_columns=column_count,
                    r2_bucket=R2_BUCKET,
                    r2_prefix=target_prefix,
                    r2_object_key=target_key,
                    r2_total_bytes=uploaded,
                    null_rates=null_rates,
                    started_at=started_wall, error_message=None,
                    notes={
                        "max_rows": max_rows,
                        "r2_prefix_override": r2_prefix_override,
                        "raw_columns": list(columns),
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
                    zip_bytes=0, itcont_bytes=0, itcont_rows=0,
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


def parse_year_range(s: str) -> list[int]:
    if "-" in s:
        a, b = s.split("-", 1)
        ya, yb = int(a), int(b)
    else:
        ya = yb = int(s)
    out: list[int] = []
    for y in range(ya, yb + 1):
        if y in DEFAULT_SPAN:
            out.append(y)
    return out


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("cycle", nargs="?", type=int,
                   help="Single 4-digit even election year (1980, 1982, ..., 2024).")
    p.add_argument("--years", default=None,
                   help="Year range, e.g., 2010-2024.")
    p.add_argument("--all", action="store_true",
                   help="All 23 cycles (1980-2024).")
    p.add_argument("--skip-if-unchanged", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max-rows", type=int, default=None)
    p.add_argument("--workdir", default=None)
    p.add_argument("--r2-prefix-override", default=None,
                   help="Replace canonical fec/cycle=YYYY/ prefix (smoke-test use).")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    workdir = Path(args.workdir or "/tmp/fec_r2_ingest")
    workdir.mkdir(parents=True, exist_ok=True)

    if args.all:
        years: list[int] = list(DEFAULT_SPAN)
    elif args.years:
        years = parse_year_range(args.years)
    else:
        if args.cycle is None:
            log.error("must pass cycle (or --years / --all)")
            return 2
        if args.cycle not in DEFAULT_SPAN:
            log.error("cycle %s not in supported span %s-%s (even years only)",
                      args.cycle, DEFAULT_SPAN[0], DEFAULT_SPAN[-1])
            return 2
        years = [args.cycle]

    # Single header fetch for the whole batch.
    with httpx.Client(headers={"User-Agent": "data-engine-x/fec-r2-ingest"}) as client:
        columns = fetch_header_columns(client)
    log.info("header columns (%d): %s", len(columns), ", ".join(columns))

    rc = 0
    for y in years:
        cycle = Cycle(year=y)
        log.info("=" * 70)
        log.info("=== INGEST: cycle=%s ===", y)
        log.info("=" * 70)
        rc_one = ingest_cycle(
            cycle,
            skip_if_unchanged=args.skip_if_unchanged,
            dry_run=args.dry_run,
            workdir=workdir,
            max_rows=args.max_rows,
            r2_prefix_override=args.r2_prefix_override,
            columns=columns,
        )
        if rc_one != 0:
            rc = rc_one
            log.error("cycle %s failed; continuing with remaining cycles", y)
    return rc


if __name__ == "__main__":
    sys.exit(main())

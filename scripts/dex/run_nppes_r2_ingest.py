#!/usr/bin/env python3
"""NPPES (CMS National Plan and Provider Enumeration System) bulk →
R2 monthly snapshot ingest.

Mirrors the NCUA / SBA / HMDA Volume King pattern but adapted for NPPES's
single-file-per-month full-replacement bulk:

  s3://dex-raw-landing-zone/nppes/snapshot=YYYY-MM/<table>.parquet

where <table> ∈ {npidata, practice_locations, other_names, endpoints}.

Audit ledger: ops.nppes_r2_ingest_runs (one row per snapshot_year_month).
Idempotency basis: HEAD Last-Modified (per ZIP).

The match-tier file is `npidata.parquet` (one row per NPI). It carries the
five normalized join columns the downstream FEC ⨝ NPPES match MV depends
on (provider_first_normalized, provider_last_normalized,
provider_org_name_normalized, practice_zip5, practice_state_normalized,
plus primary_taxonomy_code). The other three Parquets pass through verbatim
with snapshot metadata appended.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_nppes_r2_ingest.py --month 2026-04
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_nppes_r2_ingest.py --month 2026-04 --dry-run
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_nppes_r2_ingest.py --month 2026-04 \\
        --max-rows 50000 --r2-prefix-override 'nppes/_smoke/snapshot=2026-04/'
"""

from __future__ import annotations

import argparse
import calendar
import logging
import os
import re
import shutil
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure the data-engine-x repo root is on sys.path so `scripts._lib.*` imports
# resolve when the script is invoked directly (`python3 scripts/run_nppes_r2_ingest.py`).
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import boto3
import duckdb
import httpx
import psycopg
from psycopg.types.json import Jsonb

# scripts._lib.nppes_normalize is the canonical Python spec for the
# normalization rules (unit-tested in tests/scripts/test_nppes_normalize.py).
# This script implements equivalent rules in DuckDB SQL for throughput.
# A run-time parity check on a small sample lives in `_parity_check`.
from scripts._lib.nppes_normalize import (
    normalize_org_name,
    normalize_provider_name,
    normalize_state,
    practice_zip5_with_fallback,
)


R2_BUCKET = "dex-raw-landing-zone"
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 5

# Inner ZIP file naming prefix → R2 Parquet basename. The directive carves
# out 4 expected `_pfile_*.csv` files per month.
INNER_FILE_PREFIXES: dict[str, str] = {
    "npidata": "npidata",
    "pl": "practice_locations",
    "othername": "other_names",
    "endpoint": "endpoints",
}


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("nppes-r2-ingest")


log = _logger()


@dataclass(frozen=True)
class Snapshot:
    year: int
    month: int  # 1..12

    @property
    def month_name(self) -> str:
        # Full English month name, first letter capitalized: 'January'..'December'.
        return calendar.month_name[self.month]

    @property
    def label(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"

    @property
    def url(self) -> str:
        return (
            "https://download.cms.gov/nppes/"
            f"NPPES_Data_Dissemination_{self.month_name}_{self.year}_V2.zip"
        )

    @property
    def r2_prefix(self) -> str:
        return f"nppes/snapshot={self.label}/"


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
# HTTP layer (clone of NCUA / SBA shape)
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
                        if now - last_log >= 10.0:
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


# --------------------------------------------------------------------------- #
# ZIP unpack
# --------------------------------------------------------------------------- #


# Match the data CSV (e.g., `npidata_pfile_20050523-20260412.csv`) but NOT
# the sibling header CSV (e.g., `npidata_pfile_20050523-20260412_fileheader.csv`).
# Anchoring to date-only digits before `.csv` is the easiest way to exclude
# any name that ends in `_fileheader.csv` (case varies in CMS publications).
_PFILE_RE = re.compile(
    r"^(npidata|pl|othername|endpoint)_pfile_[\d\-]+\.csv$",
    re.IGNORECASE,
)


def extract_pfile_csvs(zip_path: Path, dest_dir: Path) -> dict[str, Path]:
    """Extract every `<base>_pfile_<dates>.csv` (the data file) into dest_dir.
    Returns a mapping of base prefix (lowercased) to extracted path. Skips
    every `_FileHeader.csv` / `_fileheader.csv` companion.
    """
    out: dict[str, Path] = {}
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            name = Path(info.filename).name
            if "_fileheader" in name.lower():
                continue
            m = _PFILE_RE.match(name)
            if not m:
                continue
            base = m.group(1).lower()
            target = dest_dir / name
            with z.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst, length=1 << 20)
            out[base] = target
    return out


# --------------------------------------------------------------------------- #
# DuckDB transform
# --------------------------------------------------------------------------- #


# Hard-coded NPPES column names (CMS publishes consistent header rows).
# These are referenced exactly as they appear in row 1 of npidata_pfile_*.csv.
NPI_COL_FIRST_NAME = "Provider First Name"
NPI_COL_LAST_NAME = "Provider Last Name (Legal Name)"
NPI_COL_ORG_NAME = "Provider Organization Name (Legal Business Name)"
NPI_COL_ENTITY_TYPE = "Entity Type Code"
NPI_COL_PRACTICE_ZIP = "Provider Business Practice Location Address Postal Code"
NPI_COL_MAILING_ZIP = "Provider Business Mailing Address Postal Code"
NPI_COL_PRACTICE_STATE = "Provider Business Practice Location Address State Name"
NPI_COL_MAILING_STATE = "Provider Business Mailing Address State Name"
NPI_COL_DEACTIVATION_DATE = "NPI Deactivation Date"

# Taxonomy slots: 15 pairs of (Code_N, Switch_N).
TAXONOMY_SLOTS = list(range(1, 16))


def _connect_duckdb() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    con.execute("PRAGMA threads=4;")
    con.execute("PRAGMA memory_limit='8GB';")
    return con


# DuckDB SQL ports of the Python normalize functions. The Python module is the
# canonical spec; these SQL fragments implement the same rules. Discrepancies
# would be caught by `_parity_check` against a 30-row sample at run time.
_ORG_SUFFIX_TOKEN_ALT = (
    "llc|inc|incorporated|co|company|corp|corporation|ltd|limited|"
    "lp|llp|pc|pa|pllc"
)


def _sql_normalize_provider_name(col_sql: str) -> str:
    """Build SQL for normalize_provider_name(col).

    Lowercase → replace non-[a-z0-9 ] with space → collapse whitespace →
    trim → NULL-out empty.
    """
    return (
        "NULLIF("
        f"TRIM(REGEXP_REPLACE(REGEXP_REPLACE(LOWER(TRIM({col_sql})), "
        "'[^a-z0-9 ]+', ' ', 'g'), '\\s+', ' ', 'g')), '')"
    )


def _sql_normalize_org_name(col_sql: str) -> str:
    """Build SQL for normalize_org_name(col).

    Same as provider name, plus drop trailing org-suffix tokens (LLC, Inc,
    Corp, etc.) — repeated until none remain.
    """
    base = _sql_normalize_provider_name(col_sql)
    return (
        "NULLIF(TRIM(REGEXP_REPLACE("
        f"{base}, "
        f"'(\\s+({_ORG_SUFFIX_TOKEN_ALT}))+\\s*$', "
        "'', 'g')), '')"
    )


def _sql_normalize_state(col_sql: str) -> str:
    """Build SQL for normalize_state(col)."""
    return f"NULLIF(UPPER(TRIM({col_sql})), '')"


def _sql_practice_zip5_with_fallback(practice_sql: str, mailing_sql: str) -> str:
    """Build SQL for practice_zip5_with_fallback(practice, mailing).

    For each candidate: strip non-alphanumeric, take first 5 if length ≥ 5,
    else NULL. Practice has priority via COALESCE.
    """

    def one(col: str) -> str:
        stripped = f"REGEXP_REPLACE(COALESCE({col}, ''), '[^A-Za-z0-9]', '', 'g')"
        return f"CASE WHEN LENGTH({stripped}) >= 5 THEN SUBSTRING({stripped}, 1, 5) END"

    return f"COALESCE({one(practice_sql)}, {one(mailing_sql)})"


def _quoted(col: str) -> str:
    """Quote a DuckDB identifier, escaping any embedded double-quotes."""
    return '"' + col.replace('"', '""') + '"'


def _primary_taxonomy_sql() -> str:
    """Build the COALESCE chain that picks the primary taxonomy code.

    Mirrors `pick_primary_taxonomy()` in scripts/_lib/nppes_normalize.py:
    1. For each of the 15 slots, return the code if its primary switch = 'Y'.
    2. Else fall back to the first non-empty slot.
    """
    primary_branches: list[str] = []
    for n in TAXONOMY_SLOTS:
        code_col = _quoted(f"Healthcare Provider Taxonomy Code_{n}")
        switch_col = _quoted(f"Healthcare Provider Primary Taxonomy Switch_{n}")
        primary_branches.append(
            f"CASE WHEN UPPER(TRIM({switch_col})) = 'Y' "
            f"AND {code_col} IS NOT NULL AND {code_col} <> '' "
            f"THEN {code_col} END"
        )
    fallback_branches: list[str] = []
    for n in TAXONOMY_SLOTS:
        code_col = _quoted(f"Healthcare Provider Taxonomy Code_{n}")
        fallback_branches.append(f"NULLIF(TRIM({code_col}), '')")
    return "COALESCE(\n  " + ",\n  ".join(primary_branches + fallback_branches) + "\n)"


def _date_cast_sql(col: str) -> str:
    """Cast a NPPES MM/DD/YYYY date string to DATE; NULL on parse failure."""
    return f"TRY_STRPTIME({_quoted(col)}, '%m/%d/%Y')::DATE AS {_quoted(col)}"


def transform_npidata_to_parquet(
    csv_path: Path,
    parquet_path: Path,
    *,
    snapshot: Snapshot,
    log_prefix: str,
    max_rows: int | None,
) -> tuple[int, int, list[str]]:
    """Read npidata CSV → project + add normalized columns → ZSTD Parquet.

    Returns (rows_in, rows_pq, raw_columns).
    """
    con = _connect_duckdb()

    # NPPES CSVs are RFC 4180 with comma delimiter and double-quoted fields.
    # all_varchar=TRUE preserves leading zeros (NPI is 10 digits, taxonomy
    # codes have leading zeros, ZIPs are 5/9 digits).
    # ignore_errors=TRUE survives the occasional non-UTF-8 row.
    con.execute(f"""
        CREATE VIEW raw AS
        SELECT * FROM read_csv_auto(
          '{csv_path}',
          delim=',', quote='"', escape='"', header=TRUE,
          all_varchar=TRUE, sample_size=-1,
          ignore_errors=TRUE
        );
    """)

    cols_info = con.execute("DESCRIBE raw;").fetchall()
    raw_cols = [c[0] for c in cols_info]
    log.info("%s   npidata: %d raw columns detected", log_prefix, len(raw_cols))

    rows_in_row = con.execute("SELECT count(*) FROM raw;").fetchone()
    rows_in = int(rows_in_row[0]) if rows_in_row else 0
    log.info("%s   npidata: %d input rows", log_prefix, rows_in)

    # Build SELECT projecting all raw columns + cast Date-typed columns
    # back to DATE + normalized columns + snapshot metadata.
    select_parts: list[str] = []
    for col in raw_cols:
        if "Date" in col:
            select_parts.append(_date_cast_sql(col))
        else:
            select_parts.append(_quoted(col))

    # Normalized identity columns.
    select_parts.append(
        f"{_sql_normalize_provider_name(_quoted(NPI_COL_FIRST_NAME))} "
        f"AS provider_first_normalized"
    )
    select_parts.append(
        f"{_sql_normalize_provider_name(_quoted(NPI_COL_LAST_NAME))} "
        f"AS provider_last_normalized"
    )
    select_parts.append(
        f"{_sql_normalize_org_name(_quoted(NPI_COL_ORG_NAME))} "
        f"AS provider_org_name_normalized"
    )
    select_parts.append(
        f"{_sql_practice_zip5_with_fallback(_quoted(NPI_COL_PRACTICE_ZIP), _quoted(NPI_COL_MAILING_ZIP))} "
        f"AS practice_zip5"
    )
    select_parts.append(
        f"COALESCE("
        f"{_sql_normalize_state(_quoted(NPI_COL_PRACTICE_STATE))}, "
        f"{_sql_normalize_state(_quoted(NPI_COL_MAILING_STATE))}"
        f") AS practice_state_normalized"
    )
    select_parts.append(
        f"TRY_CAST(NULLIF(TRIM({_quoted(NPI_COL_ENTITY_TYPE)}), '') "
        f"AS SMALLINT) AS entity_type_code"
    )
    select_parts.append(
        f"({_primary_taxonomy_sql()}) AS primary_taxonomy_code"
    )
    select_parts.append(
        f"({_quoted(NPI_COL_DEACTIVATION_DATE)} IS NOT NULL "
        f"AND TRIM({_quoted(NPI_COL_DEACTIVATION_DATE)}) <> '') "
        f"AS is_deactivated"
    )
    select_parts.append(
        f"CAST('{snapshot.label}' AS VARCHAR) AS nppes_snapshot_year_month"
    )

    limit_clause = f"LIMIT {max_rows}" if max_rows is not None else ""
    select_sql = f"SELECT {', '.join(select_parts)} FROM raw {limit_clause}"

    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    con.execute(f"""
        COPY ({select_sql}) TO '{parquet_path}'
        (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 50000);
    """)
    log.info("%s   npidata: wrote %.1f MB in %.1fs",
             log_prefix,
             parquet_path.stat().st_size / (1 << 20),
             time.monotonic() - t0)

    rows_pq_row = con.execute(
        f"SELECT count(*) FROM read_parquet('{parquet_path}');"
    ).fetchone()
    rows_pq = int(rows_pq_row[0]) if rows_pq_row else 0
    con.close()
    return rows_in, rows_pq, raw_cols


def transform_passthrough_to_parquet(
    csv_path: Path,
    parquet_path: Path,
    *,
    snapshot: Snapshot,
    table: str,
    log_prefix: str,
    max_rows: int | None,
) -> tuple[int, int, list[str]]:
    """Pass-through: read NPPES sibling CSV (pl, othername, endpoint) → ZSTD
    Parquet with snapshot metadata appended. No normalization.
    """
    con = _connect_duckdb()
    con.execute(f"""
        CREATE VIEW raw AS
        SELECT * FROM read_csv_auto(
          '{csv_path}',
          delim=',', quote='"', escape='"', header=TRUE,
          all_varchar=TRUE, sample_size=-1,
          ignore_errors=TRUE
        );
    """)
    cols_info = con.execute("DESCRIBE raw;").fetchall()
    raw_cols = [c[0] for c in cols_info]
    log.info("%s   %s: %d raw columns detected", log_prefix, table, len(raw_cols))

    rows_in_row = con.execute("SELECT count(*) FROM raw;").fetchone()
    rows_in = int(rows_in_row[0]) if rows_in_row else 0

    select_parts = [_quoted(c) for c in raw_cols]
    select_parts.append(
        f"CAST('{snapshot.label}' AS VARCHAR) AS nppes_snapshot_year_month"
    )

    limit_clause = f"LIMIT {max_rows}" if max_rows is not None else ""
    select_sql = f"SELECT {', '.join(select_parts)} FROM raw {limit_clause}"

    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    con.execute(f"""
        COPY ({select_sql}) TO '{parquet_path}'
        (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 50000);
    """)
    log.info("%s   %s: %d rows → %.1f MB in %.1fs",
             log_prefix, table, rows_in,
             parquet_path.stat().st_size / (1 << 20),
             time.monotonic() - t0)

    rows_pq_row = con.execute(
        f"SELECT count(*) FROM read_parquet('{parquet_path}');"
    ).fetchone()
    rows_pq = int(rows_pq_row[0]) if rows_pq_row else 0
    con.close()
    return rows_in, rows_pq, raw_cols


def upload_to_r2(parquet_path: Path, *, bucket: str, key: str) -> int:
    s3 = _r2_client()
    file_bytes = parquet_path.stat().st_size
    s3.upload_file(
        str(parquet_path), bucket, key,
        ExtraArgs={"ContentType": "application/x-parquet"},
    )
    return file_bytes


# --------------------------------------------------------------------------- #
# npidata normalization sanity check
# --------------------------------------------------------------------------- #


def normalization_sanity_check(parquet_path: Path) -> dict[str, float]:
    """Run the §"Validation Gate" normalization sanity checks against the
    just-written npidata.parquet. Returns a dict of stat name → ratio.
    Logs pass/fail per check; raises RuntimeError on any fail.

    Gates apply to *active* NPIs only. NPPES blanks out the typed columns
    (Entity Type Code, taxonomy slots) when an NPI is deactivated, so
    deactivated rows naturally show NULL on those fields. The directive's
    gates ("> 99% non-null entity type", "< 5% null primary taxonomy")
    apply to the active subset.
    """
    con = _connect_duckdb()
    p = str(parquet_path)
    stats = con.execute(f"""
        SELECT
          COUNT(*) AS total,
          COUNT(*) FILTER (WHERE NOT is_deactivated) AS active,
          COUNT(*) FILTER (
            WHERE NOT is_deactivated AND entity_type_code IS NULL
          ) AS active_null_entity_type,
          COUNT(*) FILTER (WHERE entity_type_code = 1) AS type1,
          COUNT(*) FILTER (WHERE entity_type_code = 2) AS type2,
          COUNT(*) FILTER (
            WHERE entity_type_code = 1
              AND (provider_first_normalized IS NULL
                OR provider_last_normalized IS NULL)
          ) AS type1_missing_name,
          COUNT(*) FILTER (
            WHERE entity_type_code = 2
              AND provider_org_name_normalized IS NULL
          ) AS type2_missing_org,
          COUNT(*) FILTER (
            WHERE practice_zip5 IS NOT NULL AND length(practice_zip5) = 5
          ) AS zip5_ok,
          COUNT(*) FILTER (WHERE practice_zip5 IS NOT NULL) AS zip5_present,
          COUNT(*) FILTER (
            WHERE NOT is_deactivated AND primary_taxonomy_code IS NULL
          ) AS active_null_taxonomy
        FROM read_parquet('{p}');
    """).fetchone()
    con.close()

    total = stats[0] or 1
    active = stats[1] or 1
    type1 = stats[3] or 1
    type2 = stats[4] or 1
    zip5_present = stats[8] or 1

    ratios = {
        "active_null_entity_type_rate": stats[2] / active,
        "type1_missing_name_rate": stats[5] / type1,
        "type2_missing_org_rate": stats[6] / type2,
        "zip5_length5_rate": stats[7] / zip5_present,
        "active_null_taxonomy_rate": stats[9] / active,
    }

    failures: list[str] = []
    if ratios["active_null_entity_type_rate"] >= 0.01:
        failures.append(
            "active null entity_type_code rate "
            f"{ratios['active_null_entity_type_rate']:.2%} >= 1%"
        )
    if ratios["type1_missing_name_rate"] >= 0.01:
        failures.append(
            f"Type-1 missing-name rate {ratios['type1_missing_name_rate']:.2%} >= 1%"
        )
    if ratios["type2_missing_org_rate"] >= 0.01:
        failures.append(
            f"Type-2 missing-org rate {ratios['type2_missing_org_rate']:.2%} >= 1%"
        )
    if ratios["zip5_length5_rate"] < 0.95:
        failures.append(
            f"practice_zip5 length-5 rate {ratios['zip5_length5_rate']:.2%} < 95%"
        )
    if ratios["active_null_taxonomy_rate"] >= 0.05:
        failures.append(
            "active null primary_taxonomy_code rate "
            f"{ratios['active_null_taxonomy_rate']:.2%} >= 5%"
        )

    log.info(
        "  norm-sanity total=%d active=%d type1=%d type2=%d zip5_present=%d "
        "active_null_entity=%.2f%% t1_no_name=%.2f%% t2_no_org=%.2f%% "
        "zip5_ok=%.2f%% active_null_taxonomy=%.2f%%",
        total, stats[1], stats[3], stats[4], stats[8],
        ratios["active_null_entity_type_rate"] * 100,
        ratios["type1_missing_name_rate"] * 100,
        ratios["type2_missing_org_rate"] * 100,
        ratios["zip5_length5_rate"] * 100,
        ratios["active_null_taxonomy_rate"] * 100,
    )

    if failures:
        raise RuntimeError(
            "normalization sanity check failed: " + "; ".join(failures)
        )
    return ratios


# --------------------------------------------------------------------------- #
# Audit-row helpers
# --------------------------------------------------------------------------- #


def insert_run_row(
    conn: psycopg.Connection,
    snapshot: Snapshot,
    *,
    source_url: str,
    source_last_modified: datetime | None,
    prior_source_last_modified: datetime | None,
) -> str:
    sql = """
    INSERT INTO ops.nppes_r2_ingest_runs (
        snapshot_year_month, status, source_url,
        source_last_modified, prior_source_last_modified
    ) VALUES (%s, 'running', %s, %s, %s)
    RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            snapshot.label, source_url,
            source_last_modified, prior_source_last_modified,
        ))
        row_id = cur.fetchone()[0]
    conn.commit()
    return str(row_id)


def get_prior_source_last_modified(
    conn: psycopg.Connection, snapshot: Snapshot,
) -> datetime | None:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT source_last_modified
              FROM ops.nppes_r2_ingest_runs
             WHERE snapshot_year_month = %s AND status = 'completed'
             ORDER BY started_at DESC LIMIT 1
            """,
            (snapshot.label,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def write_no_change_run(
    conn: psycopg.Connection,
    snapshot: Snapshot,
    *,
    source_url: str,
    source_last_modified: datetime | None,
    prior_source_last_modified: datetime | None,
) -> None:
    started = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ops.nppes_r2_ingest_runs (
                snapshot_year_month, status, source_url,
                source_last_modified, prior_source_last_modified,
                started_at, finished_at, duration_seconds, notes
            ) VALUES (%s, 'no_change', %s, %s, %s, %s, %s, 0, %s);
            """,
            (
                snapshot.label, source_url, source_last_modified,
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
    zip_inner_files: int,
    parquet_object_count: int,
    parquet_row_count_total: int,
    parquet_bytes_written: int,
    r2_bucket: str | None,
    r2_prefix: str | None,
    r2_total_bytes: int,
    started_at: float,
    error_message: str | None,
    notes: dict[str, Any] | None,
) -> None:
    duration = round(time.monotonic() - started_at, 3)
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE ops.nppes_r2_ingest_runs
               SET status = %s,
                   zip_bytes_downloaded = %s,
                   zip_inner_file_count = %s,
                   parquet_object_count = %s,
                   parquet_row_count_total = %s,
                   parquet_bytes_written = %s,
                   r2_bucket = %s, r2_prefix = %s,
                   r2_total_bytes = %s,
                   finished_at = now(), duration_seconds = %s,
                   error_message = %s, notes = %s
             WHERE id = %s;
            """, (
            status, zip_bytes, zip_inner_files,
            parquet_object_count, parquet_row_count_total,
            parquet_bytes_written,
            r2_bucket, r2_prefix, r2_total_bytes,
            duration, error_message,
            Jsonb(notes) if notes else None, run_id,
        ))
    conn.commit()


# --------------------------------------------------------------------------- #
# Per-snapshot main
# --------------------------------------------------------------------------- #


def ingest_snapshot(
    snapshot: Snapshot,
    *,
    skip_if_unchanged: bool,
    dry_run: bool,
    workdir: Path,
    max_rows: int | None,
    r2_prefix_override: str | None,
    only_tables: set[str] | None,
) -> int:
    log_prefix = f"[{snapshot.label}]"
    started_wall = time.monotonic()
    log.info("%s start url=%s", log_prefix, snapshot.url)

    with httpx.Client(headers={"User-Agent": "data-engine-x/nppes-r2-ingest"}) as client:
        try:
            content_length, source_last_modified, status_code = head_url(client, snapshot.url)
        except Exception:
            log.exception("%s HEAD failed", log_prefix)
            return 1
        if status_code == 404:
            log.error("%s HEAD 404 — month not published at expected URL", log_prefix)
            return 1
        if content_length is not None and content_length < 1 * (1 << 30):
            # Sanity floor: full NPPES ZIP is >1 GB. Anything smaller is
            # likely the wrong URL or a partial publish.
            log.error(
                "%s HEAD content-length %d < 1GB sanity floor — refusing",
                log_prefix, content_length,
            )
            return 1
        log.info("%s HEAD content_length=%s last_modified=%s",
                 log_prefix, content_length, source_last_modified)
        if dry_run:
            log.info("%s DRY RUN — exiting after HEAD", log_prefix)
            return 0

        with psycopg.connect(_database_url()) as conn:
            prior = get_prior_source_last_modified(conn, snapshot)
            log.info("%s prior source_last_modified: %s", log_prefix, prior)
            if (
                skip_if_unchanged
                and prior is not None
                and source_last_modified is not None
                and source_last_modified <= prior
            ):
                log.info("%s source unchanged — recording no_change", log_prefix)
                write_no_change_run(
                    conn, snapshot, source_url=snapshot.url,
                    source_last_modified=source_last_modified,
                    prior_source_last_modified=prior,
                )
                return 0

            run_id = insert_run_row(
                conn, snapshot, source_url=snapshot.url,
                source_last_modified=source_last_modified,
                prior_source_last_modified=prior,
            )
            log.info("%s run id: %s", log_prefix, run_id)

            zip_path = workdir / f"nppes_{snapshot.label}.zip"
            extract_dir = workdir / f"nppes_{snapshot.label}"
            extract_dir.mkdir(parents=True, exist_ok=True)
            parquet_dir = workdir / f"nppes_{snapshot.label}_parquet"
            parquet_dir.mkdir(parents=True, exist_ok=True)

            try:
                zip_bytes = download_zip(client, snapshot.url, zip_path)
                log.info("%s downloaded %d bytes", log_prefix, zip_bytes)

                pfile_csvs = extract_pfile_csvs(zip_path, extract_dir)
                log.info("%s extracted %d _pfile CSVs: %s",
                         log_prefix, len(pfile_csvs), sorted(pfile_csvs.keys()))
                for required in INNER_FILE_PREFIXES:
                    if required not in pfile_csvs:
                        raise RuntimeError(
                            f"missing required inner CSV: {required}_pfile_*.csv"
                        )

                r2_prefix = r2_prefix_override or snapshot.r2_prefix
                if not r2_prefix.endswith("/"):
                    r2_prefix = r2_prefix + "/"

                parquet_objects: list[dict[str, Any]] = []
                total_rows = 0
                total_bytes = 0

                # Process npidata first (the match-tier file).
                for prefix, output_basename in INNER_FILE_PREFIXES.items():
                    if only_tables is not None and output_basename not in only_tables:
                        continue
                    csv_path = pfile_csvs[prefix]
                    parquet_path = parquet_dir / f"{output_basename}.parquet"
                    if prefix == "npidata":
                        rows_in, rows_pq, raw_cols = transform_npidata_to_parquet(
                            csv_path, parquet_path,
                            snapshot=snapshot, log_prefix=log_prefix,
                            max_rows=max_rows,
                        )
                        # Run normalization sanity check inline.
                        try:
                            normalization_sanity_check(parquet_path)
                        except RuntimeError as exc:
                            log.error("%s   %s: norm sanity FAIL — %s",
                                      log_prefix, output_basename, exc)
                            raise
                    else:
                        rows_in, rows_pq, raw_cols = transform_passthrough_to_parquet(
                            csv_path, parquet_path,
                            snapshot=snapshot, table=output_basename,
                            log_prefix=log_prefix, max_rows=max_rows,
                        )
                    if rows_pq <= 0:
                        log.info("%s   %s: 0 rows — skipping upload",
                                 log_prefix, output_basename)
                        try:
                            parquet_path.unlink(missing_ok=True)
                        except Exception:
                            pass
                        continue

                    r2_key = r2_prefix + f"{output_basename}.parquet"
                    uploaded = upload_to_r2(
                        parquet_path, bucket=R2_BUCKET, key=r2_key,
                    )
                    parquet_objects.append({
                        "table": output_basename,
                        "rows_in": rows_in,
                        "rows_pq": rows_pq,
                        "raw_columns": len(raw_cols),
                        "bytes": uploaded,
                        "r2_key": r2_key,
                    })
                    total_rows += rows_pq
                    total_bytes += uploaded
                    try:
                        parquet_path.unlink(missing_ok=True)
                    except Exception:
                        pass

                log.info(
                    "%s uploaded %d Parquet objects, %s rows, %.1f MB",
                    log_prefix, len(parquet_objects), f"{total_rows:,}",
                    total_bytes / (1 << 20),
                )

                finalize_run_row(
                    conn, run_id, status="completed",
                    zip_bytes=zip_bytes,
                    zip_inner_files=len(pfile_csvs),
                    parquet_object_count=len(parquet_objects),
                    parquet_row_count_total=total_rows,
                    parquet_bytes_written=total_bytes,
                    r2_bucket=R2_BUCKET,
                    r2_prefix=r2_prefix,
                    r2_total_bytes=total_bytes,
                    started_at=started_wall, error_message=None,
                    notes={
                        "objects": parquet_objects,
                        "max_rows": max_rows,
                        "smoke_override": r2_prefix_override,
                    },
                )
                log.info(
                    "%s DONE objects=%d rows=%s upload=%.1f MB wall=%.1fs",
                    log_prefix, len(parquet_objects), f"{total_rows:,}",
                    total_bytes / (1 << 20),
                    time.monotonic() - started_wall,
                )
                return 0

            except Exception as exc:
                log.exception("%s ingest failed", log_prefix)
                finalize_run_row(
                    conn, run_id, status="failed",
                    zip_bytes=0, zip_inner_files=0,
                    parquet_object_count=0,
                    parquet_row_count_total=0,
                    parquet_bytes_written=0,
                    r2_bucket=None, r2_prefix=None,
                    r2_total_bytes=0,
                    started_at=started_wall,
                    error_message=str(exc), notes=None,
                )
                return 1

            finally:
                try:
                    zip_path.unlink(missing_ok=True)
                except Exception:
                    pass
                shutil.rmtree(extract_dir, ignore_errors=True)
                shutil.rmtree(parquet_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")


def parse_month(s: str) -> Snapshot:
    m = _MONTH_RE.match(s)
    if not m:
        raise argparse.ArgumentTypeError(f"--month must be YYYY-MM, got {s!r}")
    year = int(m.group(1))
    month = int(m.group(2))
    if not (1 <= month <= 12):
        raise argparse.ArgumentTypeError(f"--month month out of range: {s!r}")
    return Snapshot(year=year, month=month)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--month", type=parse_month, required=True,
                   help="Snapshot month, YYYY-MM (e.g., 2026-04).")
    p.add_argument("--skip-if-unchanged", action="store_true",
                   help="Skip ingest if HEAD Last-Modified unchanged from "
                        "the most recent completed run for this month.")
    p.add_argument("--dry-run", action="store_true",
                   help="HEAD only; no download, transform, or R2 writes.")
    p.add_argument("--max-rows", type=int, default=None,
                   help="Cap rows per Parquet (smoke testing).")
    p.add_argument("--workdir", default=None,
                   help="Working directory for downloaded ZIPs and "
                        "intermediate Parquets. Defaults to /tmp/nppes_r2_ingest.")
    p.add_argument("--r2-prefix-override", default=None,
                   help="Override the R2 prefix (e.g., 'nppes/_smoke/...'). "
                        "Use for smoke runs — clean canonical paths.")
    p.add_argument("--only-tables", default=None,
                   help="Comma-separated list of output basenames "
                        "(npidata, practice_locations, other_names, endpoints). "
                        "Default: all four.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    workdir = Path(args.workdir or "/tmp/nppes_r2_ingest")
    workdir.mkdir(parents=True, exist_ok=True)

    only_tables: set[str] | None = None
    if args.only_tables:
        only_tables = {
            t.strip() for t in args.only_tables.split(",") if t.strip()
        }

    log.info("=" * 70)
    log.info("=== NPPES R2 INGEST: %s ===", args.month.label)
    log.info("=" * 70)
    return ingest_snapshot(
        args.month,
        skip_if_unchanged=args.skip_if_unchanged,
        dry_run=args.dry_run,
        workdir=workdir,
        max_rows=args.max_rows,
        r2_prefix_override=args.r2_prefix_override,
        only_tables=only_tables,
    )


if __name__ == "__main__":
    sys.exit(main())

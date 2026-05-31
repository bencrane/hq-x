#!/usr/bin/env python3
"""California UCC Bulk Ingest — operator-supplied CSV → R2 ZSTD Parquet.

California Secretary of State (bizfile Online) publishes UCC filing data in
two products:

  1. **Master Unload of Data** — paid, full historical dump (operator buys
     once; this is the `--mode initial-dump` path).
  2. **Weekly Data & Images** — free weekly delta in CSV/zip format
     (this is the `--mode weekly-delta` path).

This script handles both via the same code path: read CSV (auto-extract from
zip if needed) → DuckDB-normalize identity-spine columns → write ZSTD parquet
to R2 → record run in `ops.ucc_r2_ingest_runs` (file-level audit) AND
`ops.data_source_ingest_runs` (Phase 0a observability ledger).

Pattern context:
  - Sibling to `scripts/run_ucc_r2_ingest.py` (CO/CT/OR Socrata-based ingest).
    Same R2 layout (`ucc/state=ST/stream=NAME/snapshot=YYYY-MM-DD/`), same
    audit ledger (`ops.ucc_r2_ingest_runs`), same normalization-spine columns
    via the shared `_lib/ucc_normalize.py` SQL macros.
  - California has no Socrata-style open-data portal for UCC data, so the
    fetch path is operator-provided file (local or `s3://`) instead of HTTP
    pagination.
  - Master Unload is expected to contain millions of historical filings —
    Volume King carve-out applies (file-level provenance via `source_run_id`
    in `ops.ucc_r2_ingest_runs`; no per-row jsonb).

R2 layout:
    s3://dex-raw-landing-zone/ucc/state=CA/stream=initial-dump/snapshot=YYYY-MM-DD/data.parquet.zst
    s3://dex-raw-landing-zone/ucc/state=CA/stream=weekly-delta/snapshot=YYYY-MM-DD/data.parquet.zst
    s3://dex-raw-landing-zone/ucc/state=CA/stream=snapshot/snapshot=YYYY-MM-DD/data.parquet.zst

Usage (operator drops the bulk file, then runs):
    cd ~/hq-all/apps/data-engine-x
    doppler run --project hq-all --config prd -- \\
      uv run python scripts/run_ucc_ca_ingest.py \\
        --mode initial-dump \\
        --input-file s3://dex-raw-landing-zone/ucc/state=CA/initial-dump-incoming/<file>.zip \\
        --snapshot-date 2026-05-12

Local file works too:
    doppler run --project hq-all --config prd -- \\
      uv run python scripts/run_ucc_ca_ingest.py \\
        --mode weekly-delta \\
        --input-file ~/Downloads/ca-ucc-weekly-2026-05-12.zip

Test (dry-run + no-R2 skip):
    uv run python scripts/run_ucc_ca_ingest.py \\
      --mode initial-dump \\
      --input-file ./tests/scripts/fixtures/ucc_ca_synthetic.csv \\
      --snapshot-date 2026-05-12 \\
      --r2-prefix-override 'ucc-test/state=CA/stream=initial-dump/snapshot=2026-05-12/'

See directive `~/Desktop/hq/directives/2026-05-12-hq-all-ucc-ca-ingest-scaffold.md`.
"""
from __future__ import annotations

import argparse
import io
import logging
import os
import shutil
import sys
import tempfile
import time
import urllib.parse
import uuid
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import boto3
import duckdb
import psycopg
from psycopg.types.json import Jsonb


R2_BUCKET = "dex-raw-landing-zone"
SOURCE_DISPLAY_NAME = "ucc_ca_filings"
SOURCE_PROVIDER = "ca_sos_bizfile"

VALID_MODES = ("initial-dump", "weekly-delta", "snapshot")


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("ucc-ca-ingest")


log = _logger()


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
    return os.environ.get("DEX_DB_URL_POOLED") or _required_env("DEX_DB_URL_DIRECT")


# --------------------------------------------------------------------------- #
# Input file resolution — local path or s3:// URI
# --------------------------------------------------------------------------- #


def _is_s3_uri(uri: str) -> bool:
    return uri.startswith("s3://")


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    """Return (bucket, key) from an s3://bucket/key URI."""
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme != "s3":
        raise ValueError(f"not an s3:// URI: {uri!r}")
    return parsed.netloc, parsed.path.lstrip("/")


def fetch_input_to_local(input_uri: str, workdir: Path) -> Path:
    """Resolve --input-file to a local path. Downloads from R2 if s3://.

    Returns the path on local disk. The file may be .csv, .zip, or .csv.zip —
    extraction happens in the next step.
    """
    if _is_s3_uri(input_uri):
        bucket, key = _parse_s3_uri(input_uri)
        filename = Path(key).name or "input.bin"
        local_path = workdir / filename
        log.info("downloading from R2: s3://%s/%s → %s", bucket, key, local_path)
        s3 = _r2_client()
        s3.download_file(bucket, key, str(local_path))
        return local_path
    local_path = Path(input_uri).expanduser().resolve()
    if not local_path.exists():
        raise FileNotFoundError(f"input file not found: {local_path}")
    return local_path


def extract_csv_from_input(input_path: Path, workdir: Path) -> list[Path]:
    """Extract one or more CSV files from the input.

    If input is .zip: extracts every *.csv inside.
    If input is .csv: returns [input_path] unchanged.
    Other extensions are accepted and treated as csv.
    """
    suffix = input_path.suffix.lower()
    if suffix == ".zip":
        extract_dir = workdir / "extracted"
        extract_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(input_path) as zf:
            csv_members = [
                m for m in zf.namelist()
                if m.lower().endswith(".csv") and not m.endswith("/")
            ]
            if not csv_members:
                raise RuntimeError(
                    f"zip {input_path} contains no .csv members; "
                    f"saw: {zf.namelist()[:10]}"
                )
            log.info("extracting %d CSV(s) from zip: %s", len(csv_members), csv_members)
            zf.extractall(extract_dir, members=csv_members)
        return sorted([extract_dir / m for m in csv_members])
    return [input_path]


# --------------------------------------------------------------------------- #
# DuckDB transform — CSV → typed/normalized columns → ZSTD Parquet
# --------------------------------------------------------------------------- #
#
# The normalization SQL macros mirror scripts/_lib/ucc_normalize.py and are a
# verbatim copy of the macros defined in scripts/run_ucc_r2_ingest.py
# (`_NORMALIZE_MACROS_SQL`). We duplicate the macros here rather than import
# them because run_ucc_r2_ingest.py is a script (not a module-shaped package)
# and importing across script files is fragile under uv-managed Modal images.
# Rule changes must update both halves; the SQL form below is canonical for
# this CA path. See the parent script for the full doc-block.

_NORMALIZE_MACROS_SQL = r"""
CREATE MACRO ucc_normalize_party(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(raw) = '' THEN NULL
    ELSE NULLIF(
      (
        WITH s0 AS (
          SELECT lower(raw) AS s
        ), s1 AS (
          SELECT regexp_replace(
            regexp_replace(s, '\bn\.a\.?\b', 'na', 'g'),
            '\bn\s*\.\s*a\s*\.?\b', 'na', 'g'
          ) AS s FROM s0
        ), s2 AS (
          SELECT
            CASE
              WHEN strpos(s, ',') > 0
                AND length(string_split(rtrim(substr(s, 1, strpos(s, ',') - 1)), ' ')) = 1
                AND lower(replace(regexp_replace(
                      ltrim(substr(s, strpos(s, ',') + 1)),
                      '^\W*(\w+).*$', '\1', 'g'
                    ), '.', '')) NOT IN (
                  'llc','inc','incorporated','corp','corporation','ltd','limited',
                  'lp','llp','pc','pa','pllc','co','company','na','fsb','fa'
                )
              THEN ltrim(substr(s, strpos(s, ',') + 1)) || ' '
                   || rtrim(substr(s, 1, strpos(s, ',') - 1))
              ELSE s
            END AS s FROM s1
        ), s3 AS (
          SELECT regexp_replace(s, '[,.&''\"]+', ' ', 'g') AS s FROM s2
        ), s4 AS (
          SELECT trim(regexp_replace(s, '\s+', ' ', 'g')) AS s FROM s3
        ), parts AS (
          SELECT s, string_split(s, ' ') AS p FROM s4
        )
        SELECT CASE
          WHEN length(p) >= 2 AND p[length(p)] IN
               ('llc','inc','incorporated','corp','corporation','ltd','limited',
                'lp','llp','pc','pa','pllc','co','company','na','fsb','fa')
          THEN array_to_string(p[1:length(p)-1], ' ')
          ELSE s
        END FROM parts
      ),
      ''
    )
  END
);

CREATE MACRO ucc_zip5(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(raw) = '' THEN NULL
    WHEN length(regexp_replace(raw, '\D', '', 'g')) < 5 THEN NULL
    ELSE substr(regexp_replace(raw, '\D', '', 'g'), 1, 5)
  END
);

CREATE MACRO ucc_state_norm(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(raw) = '' THEN NULL
    WHEN length(trim(raw)) <> 2 THEN NULL
    WHEN upper(trim(raw)) IN (
      'AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA',
      'HI','ID','IL','IN','IA','KS','KY','LA','ME','MD',
      'MA','MI','MN','MS','MO','MT','NE','NV','NH','NJ',
      'NM','NY','NC','ND','OH','OK','OR','PA','RI','SC',
      'SD','TN','TX','UT','VT','VA','WA','WV','WI','WY',
      'DC','PR','VI','GU','AS','MP'
    ) THEN upper(trim(raw))
    ELSE NULL
  END
);
"""


# Canonical CA SOS UCC bulk column hints. The exact column names depend on
# the operator's bulk file (CA SOS publishes both "Master Unload" with
# detailed columns and "Weekly Data & Images" with a slightly different
# schema; the operator's specific extract may use ALL_CAPS or quoted
# identifiers). The script does case-insensitive matching across these
# common variants and stamps NULL for the spine column when no match is
# found — the operator can iterate the column hints once they have the
# real file in hand.
#
# Order in each tuple = preference order (first match wins).
DEBTOR_NAME_COL_CANDIDATES = (
    "DEBTOR_NAME", "DebtorName", "debtor_name",
    "DEBTOR_ORG_NAME", "DebtorOrgName", "debtor_org_name",
    "ORGANIZATION_NAME", "OrganizationName", "organization_name",
    "DEBTOR_LAST_NAME", "DebtorLastName", "debtor_last_name",
    "BUSINESS_NAME", "BusinessName", "business_name",
)
DEBTOR_ZIP_COL_CANDIDATES = (
    "DEBTOR_ZIP", "DebtorZip", "debtor_zip",
    "DEBTOR_POSTAL_CODE", "DebtorPostalCode", "debtor_postal_code",
    "POSTAL_CODE", "PostalCode", "postal_code",
    "ZIP", "Zip", "zip",
)
DEBTOR_STATE_COL_CANDIDATES = (
    "DEBTOR_STATE", "DebtorState", "debtor_state",
    "STATE", "State", "state",
)
SECURED_PARTY_NAME_COL_CANDIDATES = (
    "SECURED_PARTY_NAME", "SecuredPartyName", "secured_party_name",
    "SECURED_PARTY_ORG_NAME", "SecuredPartyOrgName", "secured_party_org_name",
    "SECURED_PARTY", "SecuredParty", "secured_party",
)
SECURED_PARTY_ZIP_COL_CANDIDATES = (
    "SECURED_PARTY_ZIP", "SecuredPartyZip", "secured_party_zip",
    "SECURED_PARTY_POSTAL_CODE", "SecuredPartyPostalCode", "secured_party_postal_code",
)
SECURED_PARTY_STATE_COL_CANDIDATES = (
    "SECURED_PARTY_STATE", "SecuredPartyState", "secured_party_state",
)
FILING_DATE_COL_CANDIDATES = (
    "FILING_DATE", "FilingDate", "filing_date",
    "DATE_FILED", "DateFiled", "date_filed",
    "LAPSE_DATE", "LapseDate", "lapse_date",
)
FILE_NUMBER_COL_CANDIDATES = (
    "FILE_NUMBER", "FileNumber", "file_number",
    "FILING_NUMBER", "FilingNumber", "filing_number",
    "INITIAL_FILING_NUMBER", "InitialFilingNumber", "initial_filing_number",
)


def _first_present_col(raw_cols: list[str], candidates: tuple[str, ...]) -> str | None:
    """Case-insensitive lookup: first candidate present in raw_cols (case
    fold)."""
    lc_map = {c.lower(): c for c in raw_cols}
    for cand in candidates:
        if cand.lower() in lc_map:
            return lc_map[cand.lower()]
    return None


def csv_to_parquet(
    csv_paths: list[Path],
    parquet_path: Path,
    *,
    snapshot_date: date,
    stream: str,
    source_run_id: uuid.UUID,
    log_prefix: str,
    max_rows: int | None,
) -> tuple[int, int, dict[str, float], int]:
    """Read CSV(s), normalize, write ZSTD parquet.

    Returns (rows_in, rows_pq, null_rates, column_count).
    """
    con = duckdb.connect(":memory:")
    con.execute("PRAGMA threads=4;")
    con.execute("PRAGMA memory_limit='6GB';")
    con.execute(_NORMALIZE_MACROS_SQL)

    # Build read_csv_auto over ALL CSV files. DuckDB infers the schema from
    # the first file; union_by_name=true reconciles minor drift.
    csv_globs = ", ".join(f"'{p.as_posix()}'" for p in csv_paths)
    con.execute(f"""
        CREATE VIEW raw AS
        SELECT * FROM read_csv_auto(
          [{csv_globs}],
          header=true,
          ignore_errors=false,
          union_by_name=true,
          all_varchar=true
        );
    """)

    rows_in_row = con.execute("SELECT count(*) FROM raw;").fetchone()
    rows_in = int(rows_in_row[0]) if rows_in_row else 0
    log.info("%s   csv raw rows: %s", log_prefix, f"{rows_in:,}")

    raw_describe = con.execute("DESCRIBE raw;").fetchall()
    raw_cols = [r[0] for r in raw_describe]
    log.info("%s   csv columns (%d): %s",
             log_prefix, len(raw_cols),
             ", ".join(raw_cols[:8]) + (", …" if len(raw_cols) > 8 else ""))

    # Resolve the identity-spine source columns by case-insensitive lookup.
    debtor_name_col = _first_present_col(raw_cols, DEBTOR_NAME_COL_CANDIDATES)
    debtor_zip_col = _first_present_col(raw_cols, DEBTOR_ZIP_COL_CANDIDATES)
    debtor_state_col = _first_present_col(raw_cols, DEBTOR_STATE_COL_CANDIDATES)
    sp_name_col = _first_present_col(raw_cols, SECURED_PARTY_NAME_COL_CANDIDATES)
    sp_zip_col = _first_present_col(raw_cols, SECURED_PARTY_ZIP_COL_CANDIDATES)
    sp_state_col = _first_present_col(raw_cols, SECURED_PARTY_STATE_COL_CANDIDATES)
    filing_date_col = _first_present_col(raw_cols, FILING_DATE_COL_CANDIDATES)
    file_number_col = _first_present_col(raw_cols, FILE_NUMBER_COL_CANDIDATES)

    log.info(
        "%s   spine cols: debtor=%s zip=%s state=%s sp=%s filing_date=%s file_no=%s",
        log_prefix, debtor_name_col, debtor_zip_col, debtor_state_col,
        sp_name_col, filing_date_col, file_number_col,
    )

    # Build the projection: lowercased raw columns + typed date casts +
    # normalization spine + partition metadata + provenance.
    #
    # The typed projections below for filing_date and file_number reserve
    # those output names — we skip raw projection of the source columns
    # they come from to avoid duplicate-column-name collisions.
    reserved_typed_outputs = {"filing_date", "file_number"}
    skip_source_cols = {filing_date_col, file_number_col} - {None}
    select_parts: list[str] = []
    raw_lc_set: set[str] = set(reserved_typed_outputs)
    for col in raw_cols:
        if col in skip_source_cols:
            # The typed projection below covers this column under a
            # canonical name; raw VARCHAR not also retained to avoid
            # duplicate output columns.
            continue
        lc = col.lower().replace(" ", "_").replace("-", "_")
        candidate = lc
        i = 2
        while candidate in raw_lc_set:
            candidate = f"{lc}_{i}"
            i += 1
        raw_lc_set.add(candidate)
        # All columns come through as VARCHAR per all_varchar=true.
        select_parts.append(f'"{col}" AS {candidate}')

    # Normalization spine — debtor side
    if debtor_name_col:
        select_parts.append(
            f'ucc_normalize_party("{debtor_name_col}") AS debtor_name_normalized'
        )
    else:
        select_parts.append("CAST(NULL AS VARCHAR) AS debtor_name_normalized")
    if debtor_zip_col:
        select_parts.append(f'ucc_zip5("{debtor_zip_col}") AS debtor_zip5')
    else:
        select_parts.append("CAST(NULL AS VARCHAR) AS debtor_zip5")
    if debtor_state_col:
        select_parts.append(
            f'ucc_state_norm("{debtor_state_col}") AS debtor_state_normalized'
        )
    else:
        select_parts.append("CAST(NULL AS VARCHAR) AS debtor_state_normalized")
    # Secured party side
    if sp_name_col:
        select_parts.append(
            f'ucc_normalize_party("{sp_name_col}") AS secured_party_name_normalized'
        )
    else:
        select_parts.append("CAST(NULL AS VARCHAR) AS secured_party_name_normalized")
    if sp_zip_col:
        select_parts.append(
            f'ucc_zip5("{sp_zip_col}") AS secured_party_zip5'
        )
    else:
        select_parts.append("CAST(NULL AS VARCHAR) AS secured_party_zip5")
    if sp_state_col:
        select_parts.append(
            f'ucc_state_norm("{sp_state_col}") AS secured_party_state_normalized'
        )
    else:
        select_parts.append(
            "CAST(NULL AS VARCHAR) AS secured_party_state_normalized"
        )

    # Typed filing_date and file_number aliases (raw kept under original name).
    if filing_date_col:
        select_parts.append(
            f'TRY_CAST("{filing_date_col}" AS DATE) AS filing_date'
        )
    else:
        select_parts.append("CAST(NULL AS DATE) AS filing_date")
    if file_number_col:
        select_parts.append(f'"{file_number_col}" AS file_number')
    else:
        select_parts.append("CAST(NULL AS VARCHAR) AS file_number")

    # Partition metadata + provenance (file-level per Volume King carve-out).
    select_parts.extend([
        "CAST('CA' AS VARCHAR) AS ucc_state",
        f"CAST('{stream}' AS VARCHAR) AS ucc_stream",
        f"CAST('{snapshot_date.isoformat()}' AS DATE) AS ucc_snapshot_date",
        f"CAST('{source_run_id}' AS VARCHAR) AS source_run_id",
        f"CAST('{SOURCE_PROVIDER}' AS VARCHAR) AS source_provider",
    ])

    select_sql = (
        "SELECT " + ", ".join(select_parts) + " FROM raw"
        + (f" LIMIT {max_rows}" if max_rows is not None else "")
    )

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

    # Spine null-rate sanity check.
    rates: dict[str, float] = {}
    rates_row = con.execute(f"""
        SELECT
          count(*) AS total,
          count(*) FILTER (WHERE debtor_name_normalized IS NULL) AS d_name_null,
          count(*) FILTER (WHERE debtor_zip5 IS NULL) AS d_zip_null,
          count(*) FILTER (WHERE secured_party_name_normalized IS NULL) AS sp_name_null,
          count(*) FILTER (WHERE secured_party_zip5 IS NULL) AS sp_zip_null
        FROM read_parquet('{parquet_path}');
    """).fetchone()
    total = int(rates_row[0]) if rates_row else 0
    if total > 0 and rates_row is not None:
        rates = {
            "debtor_name_normalized_null_pct":
                round(100.0 * int(rates_row[1]) / total, 4),
            "debtor_zip5_null_pct":
                round(100.0 * int(rates_row[2]) / total, 4),
            "secured_party_name_normalized_null_pct":
                round(100.0 * int(rates_row[3]) / total, 4),
            "secured_party_zip5_null_pct":
                round(100.0 * int(rates_row[4]) / total, 4),
        }

    rows_pq_row = con.execute(
        f"SELECT count(*) FROM read_parquet('{parquet_path}');"
    ).fetchone()
    rows_pq = int(rows_pq_row[0]) if rows_pq_row else 0

    column_count_row = con.execute(
        f"SELECT count(*) FROM (DESCRIBE SELECT * FROM read_parquet('{parquet_path}'));"
    ).fetchone()
    column_count = int(column_count_row[0]) if column_count_row else 0

    if rates:
        rates_str = ", ".join(
            f"{k.replace('_null_pct','')}={v:.2f}%" for k, v in rates.items()
        )
        log.info("%s   parquet rows: %s; null-rate %s",
                 log_prefix, f"{rows_pq:,}", rates_str)
    else:
        log.info("%s   parquet rows: %s", log_prefix, f"{rows_pq:,}")
    con.close()
    return rows_in, rows_pq, rates, column_count


def upload_to_r2(parquet_path: Path, *, bucket: str, key: str) -> int:
    s3 = _r2_client()
    file_bytes = parquet_path.stat().st_size
    s3.upload_file(
        str(parquet_path), bucket, key,
        ExtraArgs={"ContentType": "application/x-parquet"},
    )
    return file_bytes


def head_check_r2(*, bucket: str, key: str) -> dict[str, Any] | None:
    """Return HeadObject response dict, or None if key doesn't exist."""
    s3 = _r2_client()
    try:
        return s3.head_object(Bucket=bucket, Key=key)
    except s3.exceptions.ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


# --------------------------------------------------------------------------- #
# Audit-row helpers — ops.ucc_r2_ingest_runs (file-level audit, existing)
# --------------------------------------------------------------------------- #


def insert_run_row(
    conn: psycopg.Connection,
    *,
    stream: str,
    snapshot_date: date,
    source_url: str,
) -> str:
    """Insert a 'running' row into ops.ucc_r2_ingest_runs and return its id."""
    sql = """
    INSERT INTO ops.ucc_r2_ingest_runs (
        ucc_state, ucc_stream, ucc_snapshot_date, status,
        source_url, socrata_dataset_id, stream_kind
    ) VALUES (%s, %s, %s, 'running', %s, %s, %s)
    RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(
            sql,
            (
                "CA", stream, snapshot_date,
                source_url,
                "n/a — bulk file (CA SOS)",  # socrata_dataset_id is NOT NULL
                "denormalized",  # CA bulk is one row per filing
            ),
        )
        row_id = cur.fetchone()[0]
    conn.commit()
    return str(row_id)


def finalize_run_row(
    conn: psycopg.Connection,
    run_id: str,
    *,
    status: str,
    csv_bytes: int,
    csv_rows: int,
    parquet_rows: int,
    parquet_bytes: int,
    parquet_columns: int,
    r2_bucket: str | None,
    r2_prefix: str | None,
    r2_object_key: str | None,
    r2_total_bytes: int,
    null_rates: dict[str, float] | None,
    started_at_wall: float,
    error_message: str | None,
    notes: dict[str, Any] | None,
) -> None:
    duration = round(time.monotonic() - started_at_wall, 3)
    rates = null_rates or {}
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ops.ucc_r2_ingest_runs
               SET status = %s,
                   csv_bytes_downloaded = %s,
                   csv_row_count = %s,
                   parquet_row_count = %s,
                   parquet_bytes_written = %s,
                   parquet_column_count = %s,
                   r2_bucket = %s, r2_prefix = %s, r2_object_key = %s,
                   r2_total_bytes = %s,
                   debtor_name_normalized_null_pct = %s,
                   debtor_zip5_null_pct = %s,
                   secured_party_name_normalized_null_pct = %s,
                   secured_party_zip5_null_pct = %s,
                   finished_at = now(), duration_seconds = %s,
                   error_message = %s, notes = %s
             WHERE id = %s;
            """,
            (
                status, csv_bytes, csv_rows,
                parquet_rows, parquet_bytes, parquet_columns,
                r2_bucket, r2_prefix, r2_object_key, r2_total_bytes,
                rates.get("debtor_name_normalized_null_pct"),
                rates.get("debtor_zip5_null_pct"),
                rates.get("secured_party_name_normalized_null_pct"),
                rates.get("secured_party_zip5_null_pct"),
                duration, error_message,
                Jsonb(notes) if notes else None, run_id,
            ),
        )
    conn.commit()


# --------------------------------------------------------------------------- #
# Phase 0a observability ledger — ops.data_source_ingest_runs
# --------------------------------------------------------------------------- #


def get_or_create_data_source_id(conn: psycopg.Connection) -> str | None:
    """Look up the source_id for `ucc_ca_filings` in ops.data_sources.

    Returns None if the seed hasn't been run yet (the script will skip Phase
    0a ledger writes in that case rather than crash). The seed is shipped in
    the same directive (s5) and runs once against prod after merge.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_id FROM ops.data_sources WHERE display_name = %s",
            (SOURCE_DISPLAY_NAME,),
        )
        row = cur.fetchone()
    return str(row[0]) if row else None


def record_obs_run_started(
    conn: psycopg.Connection,
    source_id: str,
    run_metadata: dict[str, Any],
) -> str:
    """Insert a 'running' row into ops.data_source_ingest_runs."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.data_source_ingest_runs
                (source_id, status, run_metadata)
            VALUES (%s, 'running'::data_source_run_status, %s)
            RETURNING run_id
            """,
            (source_id, Jsonb(run_metadata)),
        )
        run_id = cur.fetchone()[0]
    conn.commit()
    return str(run_id)


def record_obs_run_completed(
    conn: psycopg.Connection,
    run_id: str,
    *,
    status: str,
    rows_ingested: int | None,
    bytes_written: int | None,
    error_message: str | None,
) -> None:
    """Update the obs-ledger run row to terminal status."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ops.data_source_ingest_runs
            SET
                status         = %s::data_source_run_status,
                completed_at   = NOW(),
                rows_ingested  = %s,
                bytes_written  = %s,
                error_message  = %s
            WHERE run_id = %s
            """,
            (status, rows_ingested, bytes_written, error_message, run_id),
        )
    conn.commit()


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def ingest(
    *,
    mode: str,
    input_uri: str,
    snapshot_date: date,
    workdir: Path,
    max_rows: int | None,
    r2_prefix_override: str | None,
    skip_if_exists: bool,
) -> int:
    log_prefix = f"[CA/{mode}/{snapshot_date}]"
    started_wall = time.monotonic()
    log.info("%s start input=%s", log_prefix, input_uri)

    source_run_id = uuid.uuid4()

    target_prefix = r2_prefix_override or (
        f"ucc/state=CA/stream={mode}/snapshot={snapshot_date.isoformat()}/"
    )
    target_key = target_prefix.rstrip("/") + "/data.parquet.zst"

    if skip_if_exists:
        head = head_check_r2(bucket=R2_BUCKET, key=target_key)
        if head is not None:
            log.info(
                "%s R2 destination already exists (%d bytes) — skipping",
                log_prefix, int(head.get("ContentLength", 0)),
            )
            return 0

    with psycopg.connect(_database_url()) as conn:
        # Insert running rows in BOTH ledgers.
        ucc_run_id = insert_run_row(
            conn,
            stream=mode,
            snapshot_date=snapshot_date,
            source_url=input_uri,
        )
        log.info("%s ucc_r2_ingest_runs id=%s", log_prefix, ucc_run_id)

        obs_source_id = get_or_create_data_source_id(conn)
        obs_run_id: str | None = None
        if obs_source_id:
            obs_run_id = record_obs_run_started(
                conn,
                obs_source_id,
                run_metadata={
                    "writer": "run_ucc_ca_ingest",
                    "mode": mode,
                    "snapshot_date": snapshot_date.isoformat(),
                    "input_uri": input_uri,
                    "source_run_id": str(source_run_id),
                    "ucc_r2_run_id": ucc_run_id,
                },
            )
            log.info("%s data_source_ingest_runs run_id=%s",
                     log_prefix, obs_run_id)
        else:
            log.warning(
                "%s ops.data_sources has no row for display_name=%s — "
                "skipping Phase 0a ledger write. Run seed_observability_sources.py.",
                log_prefix, SOURCE_DISPLAY_NAME,
            )

        try:
            local_input = fetch_input_to_local(input_uri, workdir)
            log.info("%s local input: %s (%d bytes)",
                     log_prefix, local_input,
                     local_input.stat().st_size)

            csv_paths = extract_csv_from_input(local_input, workdir)
            csv_total_bytes = sum(p.stat().st_size for p in csv_paths)
            log.info("%s   %d CSV file(s), %.1f MB total",
                     log_prefix, len(csv_paths),
                     csv_total_bytes / (1 << 20))

            parquet_path = workdir / f"ucc_ca_{mode}_{snapshot_date.isoformat()}.parquet"

            rows_in, rows_pq, null_rates, column_count = csv_to_parquet(
                csv_paths, parquet_path,
                snapshot_date=snapshot_date,
                stream=mode,
                source_run_id=source_run_id,
                log_prefix=log_prefix,
                max_rows=max_rows,
            )

            # Row-count parity check (skipped on max_rows path).
            if max_rows is None and rows_in > 0:
                variance = abs(rows_pq - rows_in) / rows_in
                if variance > 0.001:
                    raise RuntimeError(
                        f"row-count variance {variance:.4%} > 0.1% "
                        f"(in={rows_in:,} pq={rows_pq:,})"
                    )

            uploaded = upload_to_r2(
                parquet_path, bucket=R2_BUCKET, key=target_key,
            )
            log.info(
                "%s uploaded → s3://%s/%s (%.1f MB)",
                log_prefix, R2_BUCKET, target_key, uploaded / (1 << 20),
            )

            notes = {
                "mode": mode,
                "input_uri": input_uri,
                "csv_file_count": len(csv_paths),
                "source_run_id": str(source_run_id),
                "max_rows": max_rows,
                "r2_prefix_override": r2_prefix_override,
                "source_provider": SOURCE_PROVIDER,
            }
            finalize_run_row(
                conn, ucc_run_id, status="completed",
                csv_bytes=csv_total_bytes, csv_rows=rows_in,
                parquet_rows=rows_pq, parquet_bytes=uploaded,
                parquet_columns=column_count,
                r2_bucket=R2_BUCKET, r2_prefix=target_prefix,
                r2_object_key=target_key, r2_total_bytes=uploaded,
                null_rates=null_rates,
                started_at_wall=started_wall, error_message=None,
                notes=notes,
            )
            if obs_run_id:
                record_obs_run_completed(
                    conn, obs_run_id, status="succeeded",
                    rows_ingested=rows_pq, bytes_written=uploaded,
                    error_message=None,
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
                conn, ucc_run_id, status="failed",
                csv_bytes=0, csv_rows=0,
                parquet_rows=0, parquet_bytes=0, parquet_columns=0,
                r2_bucket=None, r2_prefix=None, r2_object_key=None,
                r2_total_bytes=0,
                null_rates=None,
                started_at_wall=started_wall,
                error_message=str(exc), notes=None,
            )
            if obs_run_id:
                record_obs_run_completed(
                    conn, obs_run_id, status="failed",
                    rows_ingested=None, bytes_written=None,
                    error_message=str(exc)[:4000],
                )
            return 1


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--mode", required=True, choices=VALID_MODES,
        help="Ingest mode: initial-dump (full historical), weekly-delta (free "
             "weekly delta from CA SOS), or snapshot (operator-supplied "
             "non-recurring full re-pull).",
    )
    p.add_argument(
        "--input-file", required=True,
        help="Path to the operator-supplied bulk file. Local path or s3:// URI. "
             "Accepts .csv or .zip (auto-extract).",
    )
    p.add_argument(
        "--snapshot-date", default=None,
        help="Snapshot partition date (YYYY-MM-DD). Default: today (UTC).",
    )
    p.add_argument("--max-rows", type=int, default=None,
                   help="Cap rows for smoke testing.")
    p.add_argument(
        "--workdir", default=None,
        help="Working directory for downloads + parquet staging. "
             "Default: a fresh tempdir under the OS temp.",
    )
    p.add_argument(
        "--r2-prefix-override", default=None,
        help="Replace canonical ucc/state=CA/stream=…/snapshot=… prefix "
             "(use for smoke tests against a non-prod R2 path).",
    )
    p.add_argument(
        "--skip-if-exists", action="store_true",
        help="HEAD-check the destination R2 key; skip if already present.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    snapshot_date = (
        date.fromisoformat(args.snapshot_date)
        if args.snapshot_date else datetime.now(timezone.utc).date()
    )

    if args.workdir:
        workdir = Path(args.workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        cleanup_workdir = False
    else:
        workdir = Path(tempfile.mkdtemp(prefix="ucc_ca_ingest_"))
        cleanup_workdir = True

    log.info("CA UCC ingest — mode=%s snapshot=%s input=%s",
             args.mode, snapshot_date, args.input_file)

    try:
        rc = ingest(
            mode=args.mode,
            input_uri=args.input_file,
            snapshot_date=snapshot_date,
            workdir=workdir,
            max_rows=args.max_rows,
            r2_prefix_override=args.r2_prefix_override,
            skip_if_exists=args.skip_if_exists,
        )
    finally:
        if cleanup_workdir:
            try:
                shutil.rmtree(workdir, ignore_errors=True)
            except Exception:
                pass
    return rc


if __name__ == "__main__":
    sys.exit(main())

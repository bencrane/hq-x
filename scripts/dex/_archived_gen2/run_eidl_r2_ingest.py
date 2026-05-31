#!/usr/bin/env python3
"""SBA EIDL (COVID-19 Economic Injury Disaster Loans) → R2 Fuel Tank ingest.

Mirrors data.sba.gov's COVID-19 EIDL bulk publications into Cloudflare R2 as
ZSTD-compressed Parquet, snapshot-partitioned. The companion identity-rich
COVID-era small-business capture to PPP — adds borrower name + full address
+ DUNS + amount on ~8-9M loan/advance records.

Source: data.sba.gov CKAN (federal DATA Act / FFATA schema CSVs).

  /dataset/covid-19-eidl                COVID EIDL primary loans (1 outer ZIP,
                                        5 inner CSVs, ~3-4M rows through 2020-11-15)
  /dataset/covid-19-eidl-advance        COVID EIDL Advance grants (1 outer ZIP
                                        with 7 nested ZIPs, each one CSV inside,
                                        ~5M rows through 2020-11-15)

The EIDL Targeted Advance + Supplemental Targeted Advance programs (the
post-Dec-2020 enhancements) are NOT bulk-published on data.sba.gov; the
script logs these as 'unavailable' but does not fail the run. More-recent
loan-level data lives on USAspending.gov (a separate ingest concern).

R2 layout (sibling to existing sba/program=ppp/...):

  sba/program=eidl/
    covid_eidl_loans/snapshot=2020-12-01/data.parquet
    covid_eidl_advances/snapshot=2020-12-01/data.parquet

Each Parquet preserves all 43 (loans) / 44 (advances) source columns as
VARCHAR (DATA Act schema is stable; sentinels + leading-zero ZIPs survive
intact), and adds typed:

  loan_action_date       DATE       (TRY_STRPTIME of YYYYMMDD)
  federal_action_amount  DOUBLE     (TRY_CAST of FEDERALACTIONOBLIGATION)
  loan_face_value        DOUBLE     (TRY_CAST of FACEVALUEOFDIRECTLOANORLOANGUARANTEE)
  loan_subsidy_cost      DOUBLE     (TRY_CAST of ORIGINALLOANSUBSIDYCOST)

Plus normalized identity-spine columns (downstream MV join keys):

  borrower_name              raw legal entity name
  borrower_name_normalized   stripped suffixes / collapsed whitespace
  borrower_first_normalized  parsed sole-prop first
  borrower_last_normalized   parsed sole-prop last
  borrower_zip5              first-5-digit ZIP
  borrower_state_normalized  uppercased 2-letter
  duns_normalized            9-digit DUNS (NULL when malformed)
  borrower_kind_normalized   sole_prop|small_business|nonprofit|partnership|unknown
  eidl_program_normalized    'covid_eidl_loan' | 'covid_eidl_advance'
  eidl_snapshot_date         DATE partition value

Audit ledger: ops.eidl_r2_ingest_runs (stream, partition_value).
Idempotency basis: HEAD Last-Modified per stream.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_eidl_r2_ingest.py covid_eidl_loans
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_eidl_r2_ingest.py covid_eidl_advances --dry-run
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_eidl_r2_ingest.py covid_eidl_advances --max-rows 50000

Special form:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_eidl_r2_ingest.py --all
  Ingests both streams sequentially.

See directive ~/Desktop/hq/directives/2026-05-08-sba-eidl-r2-ingest.md.
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

# Snapshot date: data.sba.gov publishes "as of 12-01-20" for both COVID
# streams. The actual data window through 2020-11-15 (per inner CSV
# filenames). We use the publication-date snapshot per the directive's
# `snapshot={YYYY-MM-DD}` partition convention.
SNAPSHOT_DATE = "2020-12-01"


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("eidl-r2-ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# Per-stream configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Stream:
    name: str  # 'covid_eidl_loans' | 'covid_eidl_advances'
    program_label: str  # 'covid_eidl_loan' | 'covid_eidl_advance'
    url: str
    name_column_in_csv: str  # AWARDEEORRECIPIENTLEGALENTITYNAME[ANDDOINGBUSINESSAS]
    nested_zip: bool  # outer ZIP contains inner ZIPs (advances), not raw CSVs

    @property
    def partition_value(self) -> str:
        return f"snapshot={SNAPSHOT_DATE}"

    @property
    def r2_prefix(self) -> str:
        return f"sba/program=eidl/{self.name}/{self.partition_value}/"

    @property
    def r2_object_key(self) -> str:
        return self.r2_prefix + "data.parquet"


STREAMS: tuple[Stream, ...] = (
    Stream(
        name="covid_eidl_loans",
        program_label="covid_eidl_loan",
        url=(
            "https://data.sba.gov/dataset/d158e867-cf27-49dd-b6c8-fa8df098e394/"
            "resource/28563b11-99a1-40a2-aa80-c446a181e231/download/"
            "april-2021-delivery-of-eidl-data-through-november-2020.zip"
        ),
        name_column_in_csv="AWARDEEORRECIPIENTLEGALENTITYNAME",
        nested_zip=False,
    ),
    Stream(
        name="covid_eidl_advances",
        program_label="covid_eidl_advance",
        url=(
            "https://data.sba.gov/dataset/f0ce346d-ac6d-4a7f-b502-1c95cbf668b8/"
            "resource/a282de7a-c6d6-4685-ab7f-e36c5374e58c/download/"
            "12-01-20-eidl-advance-data.zip"
        ),
        name_column_in_csv="AWARDEEORRECIPIENTLEGALENTITYNAMEANDDOINGBUSINESSAS",
        nested_zip=True,
    ),
)

# Streams the directive listed but data.sba.gov does NOT publish. The script
# stamps an 'unavailable' run row for each on every invocation so the audit
# ledger reflects "we checked, source had nothing."
UNAVAILABLE_STREAMS: tuple[tuple[str, str, str], ...] = (
    (
        "covid_eidl_targeted_advances",
        f"snapshot={SNAPSHOT_DATE}",
        "Not bulk-published on data.sba.gov as of 2026-05-08. The Targeted "
        "Advance + Supplemental Targeted Advance programs launched after "
        "2020-12-01 (the cutoff of the bulk publications) and remain "
        "available only through USAspending.gov, which is a separate "
        "ingest pipeline.",
    ),
    (
        "disaster_loans",
        f"snapshot={SNAPSHOT_DATE}",
        "data.sba.gov publishes pre/post-COVID disaster loans by fiscal "
        "year (sba_disaster_loan_data_fy{YY}.xlsx, FY2000-FY2022) but the "
        "XLSX rows are aggregated by damaged-property location (city, ZIP, "
        "county, state) — there is NO per-borrower row. Useless for the "
        "directive's identity-spine objective; excluded from this ingest.",
    ),
)


def _stream_lookup(name: str) -> Stream:
    for s in STREAMS:
        if s.name == name:
            return s
    raise SystemExit(
        f"unknown stream {name!r}; valid: {[s.name for s in STREAMS]}"
    )


# --------------------------------------------------------------------------- #
# Env / clients
# --------------------------------------------------------------------------- #


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
# HTTP layer (lifted from run_sba_historical_r2_ingest.py)
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
                log.warning("HEAD %s HTTP %s; retry in %ss",
                            url, r.status_code, wait)
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
            with client.stream(
                "GET", url, follow_redirects=True, timeout=3600.0,
            ) as r:
                if r.status_code in RETRY_STATUSES:
                    wait = min(2 ** attempt, 30)
                    log.warning("GET %s HTTP %s; retry in %ss",
                                url, r.status_code, wait)
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


# --------------------------------------------------------------------------- #
# ZIP unpacking — flat (loans) vs nested (advances)
# --------------------------------------------------------------------------- #


def extract_csvs(
    zip_path: Path, dest_dir: Path, *, nested: bool, log_prefix: str,
) -> tuple[list[Path], int]:
    """Extract every *.csv from the outer ZIP. If nested=True, also unwrap
    inner ZIPs (each containing one CSV) before yielding the path list.

    Returns (sorted_csv_paths, total_uncompressed_bytes).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    csvs: list[Path] = []
    total_bytes = 0
    with zipfile.ZipFile(zip_path) as outer:
        names = outer.namelist()
        if nested:
            inner_names = [n for n in names if n.lower().endswith(".zip")]
            log.info("%s outer ZIP contains %d inner ZIPs",
                     log_prefix, len(inner_names))
            inner_dir = dest_dir / "inner"
            inner_dir.mkdir(parents=True, exist_ok=True)
            for n in inner_names:
                local_inner = inner_dir / Path(n).name
                with outer.open(n) as src, local_inner.open("wb") as dst:
                    shutil.copyfileobj(src, dst, length=1 << 20)
                with zipfile.ZipFile(local_inner) as inner_zip:
                    csv_names = [
                        m for m in inner_zip.namelist()
                        if m.lower().endswith(".csv")
                    ]
                    if not csv_names:
                        log.warning("%s no CSV inside %s", log_prefix, n)
                        continue
                    if len(csv_names) > 1:
                        log.warning(
                            "%s multiple CSVs in inner zip %s — using all",
                            log_prefix, n,
                        )
                    for cn in csv_names:
                        info = inner_zip.getinfo(cn)
                        out_path = dest_dir / Path(cn).name
                        with inner_zip.open(info) as src, \
                                out_path.open("wb") as dst:
                            shutil.copyfileobj(src, dst, length=1 << 20)
                        csvs.append(out_path)
                        total_bytes += info.file_size
                local_inner.unlink(missing_ok=True)
        else:
            csv_names = [n for n in names if n.lower().endswith(".csv")]
            log.info("%s outer ZIP contains %d CSVs", log_prefix, len(csv_names))
            for cn in csv_names:
                info = outer.getinfo(cn)
                out_path = dest_dir / Path(cn).name
                with outer.open(info) as src, out_path.open("wb") as dst:
                    shutil.copyfileobj(src, dst, length=1 << 20)
                csvs.append(out_path)
                total_bytes += info.file_size
    if not csvs:
        raise RuntimeError(f"no CSVs found in {zip_path}")
    csvs.sort()
    log.info(
        "%s extracted %d CSVs (%.1f MB total uncompressed)",
        log_prefix, len(csvs), total_bytes / (1 << 20),
    )
    return csvs, total_bytes


# --------------------------------------------------------------------------- #
# DuckDB transform — pure-SQL normalizer macros (no numpy dep)
# --------------------------------------------------------------------------- #

# Pure-SQL macros for the EIDL normalizers. Pattern matches FEC's approach
# (scripts/run_fec_individual_contributions_r2_ingest.py) — DuckDB CREATE
# MACRO is vectorized at plan time, no per-row Python call overhead, no
# numpy dep. The Python lib `scripts/_lib/eidl_normalize.py` remains the
# reference implementation; downstream parity is verified by
# tests/scripts/test_eidl_normalize_sql_parity.py.
#
# Trailing 'g' on regexp_replace is the DuckDB syntax for global replace.
# Suffix list mirrors _lib/eidl_normalize.py:_BORROWER_SUFFIX_TOKENS.

_NORMALIZE_MACROS_SQL = r"""
CREATE MACRO eidl_normalize_name(raw) AS (
  NULLIF(
    trim(regexp_replace(
      regexp_replace(
        regexp_replace(
          lower(coalesce(raw, '')),
          '\b(incorporated|corporation|company|limited|pllc|llp|lp|llc|inc|ltd|corp|co|pa|holdings|group|associates)\b\.?',
          ' ',
          'g'
        ),
        '[^\w\s]+', ' ', 'g'
      ),
      '\s+', ' ', 'g'
    )),
    ''
  )
);

-- Canonical "First [Middle] Last" reshape from the raw EIDL borrower name.
-- Three input patterns; identifies which by checking whether the head before
-- the first comma is whitespace-free:
--   "Last, First [Middle]"   →  head before comma has NO whitespace
--   "First Last, Tail"       →  head before comma HAS whitespace
--   "First Last [Middle]"    →  no comma
-- Output for each: tail + ' ' + head, head, raw.
-- Mirrors `_lib/eidl_normalize.parse_sole_prop_first_last` decision tree.
CREATE MACRO eidl_canonical_name(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(raw) = '' THEN NULL
    WHEN strpos(raw, ',') > 0
         AND length(trim(substr(raw, 1, strpos(raw, ',') - 1))) > 0
         AND NOT regexp_matches(trim(substr(raw, 1, strpos(raw, ',') - 1)), '\s')
    THEN
      ltrim(substr(raw, strpos(raw, ',') + 1))
      || ' ' || rtrim(substr(raw, 1, strpos(raw, ',') - 1))
    WHEN strpos(raw, ',') > 0 THEN
      trim(substr(raw, 1, strpos(raw, ',') - 1))
    ELSE trim(raw)
  END
);

-- Sole-prop first-name. Returns NULL for org-shaped names (corp-form token
-- present) or single-token names (can't disambiguate first vs last).
CREATE MACRO eidl_first_normalized(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(raw) = '' THEN NULL
    WHEN regexp_matches(
      lower(raw),
      '\b(llc|inc|corp|corporation|ltd|limited|llp|lp|pllc|pa|incorporated|company|co|holdings|group|associates)\b'
    ) THEN NULL
    WHEN NOT regexp_matches(trim(eidl_canonical_name(raw)), '\s') THEN NULL
    ELSE NULLIF(
      lower(regexp_replace(
        regexp_extract(trim(eidl_canonical_name(raw)), '^([^\s.,]+)', 1),
        '[^\w]', '', 'g'
      )),
      ''
    )
  END
);

-- Sole-prop last-name. Same disqualifiers; takes the last whitespace-delimited
-- token of the canonical-shape name, stripped of trailing post-nominal punct.
CREATE MACRO eidl_last_normalized(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(raw) = '' THEN NULL
    WHEN regexp_matches(
      lower(raw),
      '\b(llc|inc|corp|corporation|ltd|limited|llp|lp|pllc|pa|incorporated|company|co|holdings|group|associates)\b'
    ) THEN NULL
    WHEN NOT regexp_matches(trim(eidl_canonical_name(raw)), '\s') THEN NULL
    ELSE NULLIF(
      lower(regexp_replace(
        regexp_extract(
          regexp_replace(trim(eidl_canonical_name(raw)), '[.,]+$', '', 'g'),
          '([^\s.,]+)\s*$', 1
        ),
        '[^\w]', '', 'g'
      )),
      ''
    )
  END
);

CREATE MACRO eidl_zip5(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(raw) = '' THEN NULL
    WHEN length(regexp_replace(raw, '\D', '', 'g')) < 5 THEN NULL
    ELSE substr(regexp_replace(raw, '\D', '', 'g'), 1, 5)
  END
);

CREATE MACRO eidl_normalize_state(raw) AS (
  CASE
    WHEN raw IS NULL THEN NULL
    WHEN length(trim(raw)) <> 2 THEN NULL
    WHEN NOT regexp_matches(trim(raw), '^[A-Za-z]{2}$') THEN NULL
    ELSE upper(trim(raw))
  END
);

CREATE MACRO eidl_normalize_duns(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(raw) = '' THEN NULL
    WHEN length(regexp_replace(raw, '\D', '', 'g')) <> 9 THEN NULL
    ELSE regexp_replace(raw, '\D', '', 'g')
  END
);

-- Classify into the 6-bin enum. Decision order:
--   1. BUSINESSTYPES code (split on whitespace/comma/slash, pick first known).
--   2. Name pattern fallback (partnership, nonprofit hints, then corp-form).
--   3. 'unknown'.
-- Map: PR/I=sole_prop, N/O=nonprofit, P=partnership, R/Q/23=small_business.
CREATE MACRO eidl_classify_kind(bt, raw_name) AS (
  CASE
    WHEN bt IS NOT NULL THEN
      COALESCE(
        list_filter(
          string_split_regex(upper(trim(bt)), '[,\s/;]+'),
          x -> x IN ('PR','I','N','O','P','R','Q','23')
        )[1],
        ''
      )
    ELSE NULL
  END
);

-- Lookup wrapper: takes the classify_kind output (a code or '') and the name,
-- returns the directive's enum string.
CREATE MACRO eidl_kind_resolved(bt, raw_name) AS (
  CASE
    -- Code-driven first
    WHEN eidl_classify_kind(bt, raw_name) IN ('PR','I') THEN 'sole_prop'
    WHEN eidl_classify_kind(bt, raw_name) IN ('N','O')   THEN 'nonprofit'
    WHEN eidl_classify_kind(bt, raw_name) = 'P'          THEN 'partnership'
    WHEN eidl_classify_kind(bt, raw_name) IN ('R','Q','23') THEN 'small_business'
    -- Name-pattern fallback (only when no usable BUSINESSTYPES)
    WHEN raw_name IS NOT NULL AND regexp_matches(lower(raw_name),
                                                 '\b(llp|lp)\b')         THEN 'partnership'
    WHEN raw_name IS NOT NULL AND regexp_matches(lower(raw_name),
                                                 '\b(foundation|trust|ministry|ministries|church|charity|fund)\b')
                                                                          THEN 'nonprofit'
    WHEN raw_name IS NOT NULL AND regexp_matches(lower(raw_name),
                                                 '\b(llc|inc|corp|corporation)\b')
                                                                          THEN 'small_business'
    ELSE 'unknown'
  END
);
"""


def _register_normalizers(con: duckdb.DuckDBPyConnection) -> None:
    """Register the EIDL normalizer macros at the connection scope."""
    con.execute(_NORMALIZE_MACROS_SQL)


def _normalize_col(c: str) -> str:
    """Normalize CSV column names: lowercase + strip non-alphanumeric.

    DATA Act schema includes one hyphenated column (`PRIMPLACEOFPERFORMANCEZIP+4`)
    that DuckDB SQL can't quote cleanly without escape gymnastics. We rewrite
    `+` → `_plus_` so the column survives projection.
    """
    return (
        c.lower()
        .replace("+", "_plus_")
        .replace("-", "_")
        .replace(" ", "_")
        .lstrip("﻿")
    )


def csvs_to_parquet(
    csv_paths: list[Path],
    parquet_path: Path,
    *,
    stream: Stream,
    log_prefix: str,
    max_rows: int | None,
) -> tuple[int, int, int, dict[str, float]]:
    """Read pipe-of-CSVs as VARCHAR, project + normalize, write ZSTD Parquet.

    Returns (rows_in_csvs, rows_in_parquet, parquet_column_count, null_rates).
    """
    con = duckdb.connect(":memory:")
    con.execute("PRAGMA threads=4;")
    con.execute("PRAGMA memory_limit='8GB';")
    _register_normalizers(con)

    # Explicit file list (DuckDB list literal) — avoids globbing surprises and
    # works the same across all 5 (loans) / 7 (advances) inner CSVs.
    file_list = ", ".join(f"'{p}'" for p in csv_paths)
    con.execute(f"""
        CREATE VIEW raw AS
        SELECT * FROM read_csv(
          [{file_list}],
          delim=',',
          header=TRUE,
          quote='"',
          escape='"',
          all_varchar=TRUE,
          ignore_errors=TRUE,
          union_by_name=TRUE,
          strict_mode=FALSE
        );
    """)
    cols_info = con.execute("DESCRIBE raw;").fetchall()
    src_cols = [c[0] for c in cols_info]
    log.info("%s discovered %d columns in CSVs", log_prefix, len(src_cols))

    rows_in_row = con.execute("SELECT count(*) FROM raw;").fetchone()
    rows_in = int(rows_in_row[0]) if rows_in_row else 0
    log.info("%s   raw row count: %s", log_prefix, f"{rows_in:,}")

    # Project: every source column → lowercase normalized name as VARCHAR.
    select_parts: list[str] = []
    for src in src_cols:
        dst = _normalize_col(src)
        if src != dst:
            select_parts.append(f'"{src}" AS "{dst}"')
        else:
            select_parts.append(f'"{src}"')

    # Typed casts for DATA Act numeric / date columns.
    # ACTIONDATE is YYYYMMDD per FFATA. The TRY_STRPTIME catches the
    # standard form; rows with malformed dates flow through as NULL.
    typed_appends = [
        ("loan_action_date",
         "TRY_CAST(TRY_STRPTIME(NULLIF(\"ACTIONDATE\", ''), '%Y%m%d') AS DATE) "
         "AS loan_action_date"),
        ("federal_action_amount",
         "TRY_CAST(NULLIF(\"FEDERALACTIONOBLIGATION\", '') AS DOUBLE) "
         "AS federal_action_amount"),
        ("loan_face_value",
         "TRY_CAST(NULLIF(\"FACEVALUEOFDIRECTLOANORLOANGUARANTEE\", '') AS DOUBLE) "
         "AS loan_face_value"),
        ("loan_subsidy_cost",
         "TRY_CAST(NULLIF(\"ORIGINALLOANSUBSIDYCOST\", '') AS DOUBLE) "
         "AS loan_subsidy_cost"),
        ("period_of_performance_start_date",
         "TRY_CAST(TRY_STRPTIME(NULLIF(\"PERIODOFPERFORMANCESTARTDATE\", ''), '%Y%m%d') AS DATE) "
         "AS period_of_performance_start_date"),
        ("period_of_performance_end_date",
         "TRY_CAST(TRY_STRPTIME(NULLIF(\"PERIODOFPERFORMANCECURRENTENDDATE\", ''), '%Y%m%d') AS DATE) "
         "AS period_of_performance_end_date"),
    ]
    for _, sql in typed_appends:
        select_parts.append(sql)

    # Identity-spine normalizations.
    name_col = stream.name_column_in_csv
    select_parts.extend([
        f'"{name_col}" AS borrower_name',
        f'eidl_normalize_name("{name_col}") AS borrower_name_normalized',
        f'eidl_first_normalized("{name_col}") AS borrower_first_normalized',
        f'eidl_last_normalized("{name_col}") AS borrower_last_normalized',
        'eidl_zip5("LEGALENTITYZIP5") AS borrower_zip5',
        'eidl_normalize_state("LEGALENTITYSTATECD") AS borrower_state_normalized',
        'eidl_normalize_duns("AWARDEEORRECIPIENTUNIQUEIDENTIFIER") AS duns_normalized',
        'eidl_kind_resolved('
        '"BUSINESSTYPES", '
        f'"{name_col}"'
        ') AS borrower_kind_normalized',
        f"CAST('{stream.program_label}' AS VARCHAR) AS eidl_program_normalized",
        f"CAST('{SNAPSHOT_DATE}' AS DATE) AS eidl_snapshot_date",
    ])

    limit_clause = f"LIMIT {max_rows}" if max_rows is not None else ""
    select_sql = f"SELECT {', '.join(select_parts)} FROM raw {limit_clause}"

    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    log.info("%s   writing Parquet → %s (ZSTD)", log_prefix, parquet_path)
    t0 = time.monotonic()
    con.execute(f"""
        COPY ({select_sql}) TO '{parquet_path}'
        (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000);
    """)
    log.info("%s   parquet write done in %.1fs",
             log_prefix, time.monotonic() - t0)

    # Per-stream normalization null-rate sanity (directive §5.4).
    rates_row = con.execute(f"""
        SELECT
          count(*) AS total,
          count(*) FILTER (WHERE borrower_name_normalized IS NULL) AS name_null,
          count(*) FILTER (WHERE borrower_zip5 IS NULL) AS zip5_null,
          count(*) FILTER (WHERE duns_normalized IS NULL) AS duns_null,
          count(*) AS rows_pq
        FROM read_parquet('{parquet_path}');
    """).fetchone()
    total = int(rates_row[0]) if rates_row else 0
    rows_pq = int(rates_row[4]) if rates_row else 0
    if total > 0:
        rates: dict[str, float] = {
            "borrower_name_null_pct":
                round(100.0 * int(rates_row[1]) / total, 4),
            "borrower_zip5_null_pct":
                round(100.0 * int(rates_row[2]) / total, 4),
            "duns_null_pct":
                round(100.0 * int(rates_row[3]) / total, 4),
        }
    else:
        rates = {
            "borrower_name_null_pct": 0.0,
            "borrower_zip5_null_pct": 0.0,
            "duns_null_pct": 0.0,
        }
    log.info(
        "%s   parquet rows=%s null-rate name=%.2f%% zip5=%.2f%% duns=%.2f%%",
        log_prefix, f"{rows_pq:,}",
        rates["borrower_name_null_pct"],
        rates["borrower_zip5_null_pct"],
        rates["duns_null_pct"],
    )

    column_count_row = con.execute(
        f"SELECT count(*) FROM ("
        f"  SELECT * FROM read_parquet('{parquet_path}') LIMIT 0"
        f");"
    )
    # Use DESCRIBE to count columns
    pq_cols = con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{parquet_path}');"
    ).fetchall()
    parquet_columns = len(pq_cols)
    con.close()
    return rows_in, rows_pq, parquet_columns, rates


# --------------------------------------------------------------------------- #
# R2 upload
# --------------------------------------------------------------------------- #


def upload_to_r2(
    parquet_path: Path,
    *,
    bucket: str,
    key: str,
    log_prefix: str,
) -> int:
    s3 = _r2_client()
    file_bytes = parquet_path.stat().st_size
    log.info(
        "%s uploading %s (%.1f MB) → s3://%s/%s",
        log_prefix, parquet_path, file_bytes / (1 << 20), bucket, key,
    )
    last_progress: dict[str, float] = {"sent": 0.0, "ts": time.monotonic()}

    def _progress(n: int) -> None:
        last_progress["sent"] += n
        now = time.monotonic()
        if now - last_progress["ts"] >= 10.0:
            pct = 100.0 * last_progress["sent"] / max(file_bytes, 1)
            log.info(
                "  upload progress: %.1f MB (%.1f%%)",
                last_progress["sent"] / (1 << 20), pct,
            )
            last_progress["ts"] = now

    s3.upload_file(
        str(parquet_path), bucket, key,
        ExtraArgs={"ContentType": "application/x-parquet"},
        Callback=_progress,
    )
    log.info("%s upload done", log_prefix)
    return file_bytes


# --------------------------------------------------------------------------- #
# Audit-row helpers
# --------------------------------------------------------------------------- #


def insert_run_row(
    conn: psycopg.Connection,
    stream: Stream,
    *,
    source_last_modified: datetime | None,
    prior_source_last_modified: datetime | None,
) -> str:
    sql = """
    INSERT INTO ops.eidl_r2_ingest_runs (
        stream, partition_value, status, source_url,
        source_last_modified, prior_source_last_modified
    ) VALUES (%s, %s, 'running', %s, %s, %s)
    RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            stream.name, stream.partition_value, stream.url,
            source_last_modified, prior_source_last_modified,
        ))
        row_id = cur.fetchone()[0]
    conn.commit()
    return str(row_id)


def get_prior_source_last_modified(
    conn: psycopg.Connection, stream: Stream,
) -> datetime | None:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT source_last_modified
              FROM ops.eidl_r2_ingest_runs
             WHERE stream = %s AND partition_value = %s AND status = 'completed'
             ORDER BY started_at DESC LIMIT 1
            """, (stream.name, stream.partition_value))
        row = cur.fetchone()
    return row[0] if row else None


def write_no_change_run(
    conn: psycopg.Connection,
    stream: Stream,
    *,
    source_last_modified: datetime | None,
    prior_source_last_modified: datetime | None,
) -> None:
    started = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ops.eidl_r2_ingest_runs (
                stream, partition_value, status, source_url,
                source_last_modified, prior_source_last_modified,
                started_at, finished_at, duration_seconds, notes
            ) VALUES (%s, %s, 'no_change', %s, %s, %s, %s, %s, 0, %s);
            """,
            (
                stream.name, stream.partition_value, stream.url,
                source_last_modified, prior_source_last_modified,
                started, started,
                Jsonb({"reason": "source_last_modified unchanged"}),
            ),
        )
    conn.commit()


def write_unavailable_run(
    conn: psycopg.Connection,
    *,
    stream_name: str,
    partition_value: str,
    reason: str,
) -> None:
    """Stamp an 'unavailable' row for streams the directive listed but
    data.sba.gov does not publish. Idempotent at (stream, partition,
    status='unavailable'): only writes if there's no prior unavailable row.
    """
    started = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT 1 FROM ops.eidl_r2_ingest_runs
             WHERE stream = %s AND partition_value = %s
               AND status = 'unavailable'
             LIMIT 1;
            """, (stream_name, partition_value))
        if cur.fetchone():
            return
        cur.execute("""
            INSERT INTO ops.eidl_r2_ingest_runs (
                stream, partition_value, status, source_url,
                started_at, finished_at, duration_seconds, notes
            ) VALUES (%s, %s, 'unavailable', %s, %s, %s, 0, %s);
            """,
            (
                stream_name, partition_value,
                "https://data.sba.gov/  (not published)",
                started, started,
                Jsonb({"reason": reason}),
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
    csv_part_count: int,
    rows_in_csv: int,
    parquet_row_count: int,
    parquet_bytes_written: int,
    parquet_column_count: int,
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
            UPDATE ops.eidl_r2_ingest_runs
               SET status = %s,
                   zip_bytes_downloaded = %s,
                   csv_bytes_uncompressed = %s,
                   csv_part_count = %s,
                   rows_in_csv = %s,
                   parquet_row_count = %s,
                   parquet_bytes_written = %s,
                   parquet_column_count = %s,
                   r2_bucket = %s, r2_prefix = %s, r2_object_key = %s,
                   r2_total_bytes = %s,
                   borrower_name_null_pct = %s,
                   borrower_zip5_null_pct = %s,
                   duns_null_pct = %s,
                   finished_at = now(), duration_seconds = %s,
                   error_message = %s, notes = %s
             WHERE id = %s;
            """, (
            status, zip_bytes, csv_bytes, csv_part_count, rows_in_csv,
            parquet_row_count, parquet_bytes_written, parquet_column_count,
            r2_bucket, r2_prefix, r2_object_key, r2_total_bytes,
            (null_rates or {}).get("borrower_name_null_pct"),
            (null_rates or {}).get("borrower_zip5_null_pct"),
            (null_rates or {}).get("duns_null_pct"),
            duration, error_message,
            Jsonb(notes) if notes else None, run_id,
        ))
    conn.commit()


# --------------------------------------------------------------------------- #
# Per-stream main
# --------------------------------------------------------------------------- #


def ingest_stream(
    stream: Stream,
    *,
    skip_if_unchanged: bool,
    dry_run: bool,
    workdir: Path,
    max_rows: int | None,
    r2_prefix_override: str | None,
) -> int:
    log_prefix = f"[{stream.name}]"
    started_wall = time.monotonic()
    log.info("%s start url=%s", log_prefix, stream.url)

    with httpx.Client(headers={"User-Agent": "data-engine-x/eidl-r2-ingest"}) as client:
        try:
            content_length, source_last_modified, status_code = head_url(
                client, stream.url,
            )
        except Exception:
            log.exception("%s HEAD failed", log_prefix)
            return 1
        if status_code == 404:
            log.error("%s HEAD 404 — source URL not published", log_prefix)
            return 1
        log.info(
            "%s HEAD content_length=%s last_modified=%s",
            log_prefix, content_length, source_last_modified,
        )
        if dry_run:
            log.info("%s DRY RUN — exiting after HEAD", log_prefix)
            return 0

        with psycopg.connect(_database_url()) as conn:
            prior = get_prior_source_last_modified(conn, stream)
            log.info("%s prior source_last_modified: %s", log_prefix, prior)
            if (
                skip_if_unchanged
                and prior is not None
                and source_last_modified is not None
                and source_last_modified <= prior
            ):
                log.info("%s source unchanged — recording no_change", log_prefix)
                write_no_change_run(
                    conn, stream,
                    source_last_modified=source_last_modified,
                    prior_source_last_modified=prior,
                )
                return 0

            run_id = insert_run_row(
                conn, stream,
                source_last_modified=source_last_modified,
                prior_source_last_modified=prior,
            )
            log.info("%s run id: %s", log_prefix, run_id)

            stream_dir = workdir / stream.name
            stream_dir.mkdir(parents=True, exist_ok=True)
            zip_path = stream_dir / "outer.zip"
            csv_dir = stream_dir / "csvs"
            parquet_path = stream_dir / "data.parquet"

            try:
                # ---- Download outer ZIP ----
                if csv_dir.exists():
                    shutil.rmtree(csv_dir)
                csv_dir.mkdir(parents=True, exist_ok=True)
                zip_bytes = download_zip(client, stream.url, zip_path)
                log.info("%s downloaded %d bytes -> %s",
                         log_prefix, zip_bytes, zip_path)

                # ---- Extract CSVs ----
                csv_paths, csv_bytes_uncompressed = extract_csvs(
                    zip_path, csv_dir,
                    nested=stream.nested_zip, log_prefix=log_prefix,
                )

                # ---- DuckDB transform → Parquet ----
                rows_in_csv, parquet_row_count, parquet_columns, null_rates = (
                    csvs_to_parquet(
                        csv_paths, parquet_path,
                        stream=stream, log_prefix=log_prefix,
                        max_rows=max_rows,
                    )
                )
                parquet_bytes = parquet_path.stat().st_size
                log.info(
                    "%s parquet: %d rows, %.1f MB (%.2f bytes/row), %d cols",
                    log_prefix, parquet_row_count,
                    parquet_bytes / (1 << 20),
                    parquet_bytes / max(parquet_row_count, 1),
                    parquet_columns,
                )

                # ---- R2 upload ----
                if r2_prefix_override:
                    r2_prefix = r2_prefix_override
                    r2_key = r2_prefix.rstrip("/") + "/data.parquet"
                else:
                    r2_prefix = stream.r2_prefix
                    r2_key = stream.r2_object_key
                uploaded_bytes = upload_to_r2(
                    parquet_path, bucket=R2_BUCKET, key=r2_key,
                    log_prefix=log_prefix,
                )

                # ---- Finalize audit ----
                finalize_run_row(
                    conn, run_id, status="completed",
                    zip_bytes=zip_bytes,
                    csv_bytes=csv_bytes_uncompressed,
                    csv_part_count=len(csv_paths),
                    rows_in_csv=rows_in_csv,
                    parquet_row_count=parquet_row_count,
                    parquet_bytes_written=parquet_bytes,
                    parquet_column_count=parquet_columns,
                    r2_bucket=R2_BUCKET,
                    r2_prefix=r2_prefix,
                    r2_object_key=r2_key,
                    r2_total_bytes=uploaded_bytes,
                    null_rates=null_rates,
                    started_at=started_wall, error_message=None,
                    notes={
                        "max_rows": max_rows,
                        "r2_prefix_override": r2_prefix_override,
                        "csv_filenames": [p.name for p in csv_paths],
                        "snapshot_date": SNAPSHOT_DATE,
                    },
                )
                log.info(
                    "%s DONE rows=%s parquet=%.1f MB upload=%.1f MB wall=%.1fs",
                    log_prefix, f"{parquet_row_count:,}",
                    parquet_bytes / (1 << 20), uploaded_bytes / (1 << 20),
                    time.monotonic() - started_wall,
                )
                return 0

            except Exception as exc:
                log.exception("%s ingest failed", log_prefix)
                finalize_run_row(
                    conn, run_id, status="failed",
                    zip_bytes=0, csv_bytes=0, csv_part_count=0,
                    rows_in_csv=0,
                    parquet_row_count=0, parquet_bytes_written=0,
                    parquet_column_count=0,
                    r2_bucket=None, r2_prefix=None, r2_object_key=None,
                    r2_total_bytes=0,
                    null_rates=None,
                    started_at=started_wall,
                    error_message=str(exc), notes=None,
                )
                return 1

            finally:
                # Cleanup local artifacts.
                try:
                    zip_path.unlink(missing_ok=True)
                except Exception:
                    pass
                shutil.rmtree(csv_dir, ignore_errors=True)
                try:
                    parquet_path.unlink(missing_ok=True)
                except Exception:
                    pass


def stamp_unavailable_streams() -> None:
    """Record an 'unavailable' row for each directive-listed stream that
    data.sba.gov does not publish. Idempotent — safe to call on every run."""
    with psycopg.connect(_database_url()) as conn:
        for name, partition_value, reason in UNAVAILABLE_STREAMS:
            log.info("[unavailable] stamping %s %s", name, partition_value)
            write_unavailable_run(
                conn,
                stream_name=name,
                partition_value=partition_value,
                reason=reason,
            )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "stream", nargs="?",
        choices=[s.name for s in STREAMS],
        help="Which EIDL stream to ingest. Required unless --all.",
    )
    p.add_argument(
        "--all", action="store_true",
        help="Ingest every COVID EIDL stream sequentially.",
    )
    p.add_argument("--skip-if-unchanged", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max-rows", type=int, default=None)
    p.add_argument("--workdir", default=None)
    p.add_argument("--r2-prefix-override", default=None,
                   help="Replace canonical sba/program=eidl/{stream}/snapshot=.../ "
                        "prefix (smoke testing).")
    p.add_argument(
        "--skip-unavailable-stamp", action="store_true",
        help="Skip writing 'unavailable' audit rows for non-published streams.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    workdir = Path(args.workdir or "/tmp/eidl_r2_ingest")
    workdir.mkdir(parents=True, exist_ok=True)

    if not args.skip_unavailable_stamp:
        stamp_unavailable_streams()

    if args.all:
        streams: list[Stream] = list(STREAMS)
    else:
        if not args.stream:
            log.error("must pass stream (or use --all)")
            return 2
        streams = [_stream_lookup(args.stream)]

    rc = 0
    for s in streams:
        log.info("=" * 70)
        log.info("=== INGEST: stream=%s ===", s.name)
        log.info("=" * 70)
        rc_one = ingest_stream(
            s,
            skip_if_unchanged=args.skip_if_unchanged,
            dry_run=args.dry_run,
            workdir=workdir,
            max_rows=args.max_rows,
            r2_prefix_override=args.r2_prefix_override,
        )
        if rc_one != 0:
            rc = rc_one
            log.error("stream failed; continuing with remaining streams")
    return rc


if __name__ == "__main__":
    sys.exit(main())

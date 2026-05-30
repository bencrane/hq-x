#!/usr/bin/env python3
"""OSHA Inspection Data → R2 Fuel Tank ingest.

Mirrors the DOL Open Data Portal's OSHA enforcement endpoints into Cloudflare
R2 as ZSTD-compressed Parquet, year-stratified by inspection / violation /
injury event date. The directive's establishments stream is derived as a
DISTINCT projection over inspection — there is no separate establishment
endpoint on the new portal.

Source replaces the deprecated enforcedata.dol.gov bulk archive (301-redirects
to a SPA-fronted authenticated API). The new path requires an X-API-KEY query
parameter (NOT a header — the API gateway only honors it as a query param)
issued by free registration at https://dataportal.dol.gov/.

  /v4/get/osha/inspection/csv         ~5M+ historical inspection events
  /v4/get/osha/violation/csv          ~10M+ citations  (FK activity_nr)
  /v4/get/osha/accident_injury/csv    ~500K+ per-victim injury rows
                                       (FK activity_nr)
  establishments                       DERIVED from inspection DISTINCT

R2 layout (sibling to existing fec/, sba/, hmda/, ucc/, cms-pecos/, etc.):

  osha/inspection/year=YYYY/data.parquet
  osha/violation/year=YYYY/data.parquet
  osha/accident_injury/year=YYYY/data.parquet
  osha/establishments/snapshot=YYYY-MM-DD/data.parquet

Each Parquet preserves all source columns as VARCHAR (dates + numerics get
typed-cast columns alongside) and adds normalized identity-spine columns:

  establishment_name              raw `estab_name`
  establishment_name_normalized   suffix-stripped + collapsed
  establishment_address_zip5      first 5 digits of `site_zip`
  establishment_address_state_normalized  uppercased 2-letter
  naics_code_normalized           6-digit zero-padded `naics_code`
  naics_2digit                    sector
  is_construction_naics           BOOLEAN — derived flag for downstream MV
  legacy_sic_code                 raw `sic_code` (pre-2003 era)

  inspection_type_normalized      enum from `insp_type` (inspection only)
  violation_severity_normalized   enum (violation only)
  osha_standard_normalized        uppercased `standard` (violation only)
  accident_outcome_normalized     enum from `degree_of_inj` (accident_injury only)

Year partition basis:
  inspection       open_date
  violation        issuance_date
  accident_injury  event_date

Audit ledger: ops.osha_r2_ingest_runs (stream, partition_value).
Idempotency basis: full re-fetch (the API doesn't expose a Last-Modified
header per row; a smarter incremental layer is a follow-up directive).

Rate-limit strategy: the apiprod.dol.gov gateway throttles aggressively per
key (observed: bursts of ~25 calls return persistent 429 ForbiddenException
for tens of minutes). The script paces with a configurable inter-call delay
(default 2s) and backs off exponentially on 429 starting at 60s up to 480s.
The default tone is conservative; --burst-mode tightens the gap for
well-behaved key tiers.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_osha_r2_ingest.py inspection
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_osha_r2_ingest.py inspection --max-rows 50000
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_osha_r2_ingest.py --all
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_osha_r2_ingest.py establishments
    # (re-derives from latest inspection parquet on R2 — no API call needed)

See directive ~/Desktop/hq/directives/2026-05-08-osha-inspection-data-r2-ingest.md.

API mechanics, measured quota tier (~20/period), cursor guidance (use open_date,
NOT load_dt), per-stream schemas, volumes, and credit sizing — all probe-captured
to avoid re-spending quota — live in docs/OSHA_DOL_API_CANONICAL_REFERENCE.md.
READ IT before making any OSHA API call.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import time
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
DOL_API_BASE = "https://apiprod.dol.gov/v4"
PAGE_LIMIT = 10000  # API hard cap per call (CSV format)
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 8

# Today's UTC date stamps the establishments snapshot. Set once at module
# load so a single run produces a single snapshot key even if the run spans
# midnight UTC.
SNAPSHOT_DATE = datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("osha-r2-ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# Per-stream configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Stream:
    name: str  # 'inspection' | 'violation' | 'accident_injury' | 'establishments'
    endpoint_slug: str | None  # OSHA endpoint slug; None for derived streams
    date_column: str | None  # source-column basis for year stratification
    partition_kind: str  # 'year' | 'snapshot'
    description: str

    @property
    def r2_prefix_root(self) -> str:
        return f"osha/{self.name}/"


STREAMS: tuple[Stream, ...] = (
    Stream(
        name="inspection",
        endpoint_slug="inspection",
        date_column="open_date",
        partition_kind="year",
        description="OSHA inspection events (~5M+ historical, year-stratified)",
    ),
    Stream(
        name="violation",
        endpoint_slug="violation",
        date_column="issuance_date",
        partition_kind="year",
        description="OSHA citations (~10M+, FK activity_nr to inspection)",
    ),
    Stream(
        name="accident_injury",
        endpoint_slug="accident_injury",
        date_column="event_date",
        partition_kind="year",
        description="OSHA accident-injury per-victim records (~500K+)",
    ),
    Stream(
        name="establishments",
        endpoint_slug=None,  # derived from inspection
        date_column=None,
        partition_kind="snapshot",
        description="DISTINCT establishments — derived from inspection DISTINCT",
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


def _api_key() -> str:
    return _required_env("DOL_API_KEY")


# --------------------------------------------------------------------------- #
# HTTP layer — paginated CSV fetcher with rate-limit-aware backoff
# --------------------------------------------------------------------------- #


def _build_url(endpoint: str, *, limit: int, offset: int) -> str:
    """Build a /v4/get/osha/{endpoint}/csv URL with pagination params.

    The X-API-KEY MUST be a query parameter (uppercase). The gateway
    rejects header-form auth despite documentation hints; verified
    empirically 2026-05-09.
    """
    return (
        f"{DOL_API_BASE}/get/osha/{endpoint}/csv"
        f"?limit={limit}&offset={offset}&X-API-KEY={_api_key()}"
    )


def fetch_page(
    client: httpx.Client,
    *,
    endpoint: str,
    offset: int,
    log_prefix: str,
    inter_call_delay: float,
) -> tuple[bytes, int]:
    """Fetch one CSV page; return (csv_bytes, row_count_in_page).

    Row count excludes the header row (which the API includes on every
    response, even at offset>0). Caller is responsible for stripping the
    header from chunks beyond offset=0 before concatenating.

    Rate-limit handling: 429 ForbiddenException backs off exponentially
    starting at 60s up to 480s; other 5xx use shorter retry. The
    inter_call_delay applies between successful calls.
    """
    url = _build_url(endpoint, limit=PAGE_LIMIT, offset=offset)
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = client.get(url, timeout=120.0, follow_redirects=True)
            if r.status_code == 429:
                wait = min(60 * (2 ** (attempt - 1)), 480)
                log.warning(
                    "%s GET offset=%d HTTP 429; backoff %ds (attempt %d/%d)",
                    log_prefix, offset, wait, attempt, MAX_RETRIES,
                )
                time.sleep(wait)
                continue
            if r.status_code in RETRY_STATUSES:
                wait = min(2 ** attempt, 30)
                log.warning(
                    "%s GET offset=%d HTTP %s; retry in %ss",
                    log_prefix, offset, r.status_code, wait,
                )
                time.sleep(wait)
                continue
            r.raise_for_status()
            content = r.content
            # Row count = # newlines - 1 (subtract header)
            # CSV may end with no trailing newline; count records
            # by counting "\n" then -1 if leading content exists.
            if not content:
                return b"", 0
            row_count = content.count(b"\n") - 1
            if row_count < 0:
                row_count = 0
            # Pace successful calls
            if inter_call_delay > 0:
                time.sleep(inter_call_delay)
            return content, row_count
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            wait = min(2 ** attempt, 30)
            log.warning(
                "%s GET offset=%d error (%s); retry in %ss",
                log_prefix, offset, exc, wait,
            )
            time.sleep(wait)
    raise RuntimeError(f"page fetch failed after {MAX_RETRIES} retries: {last_exc}")


def stream_paginated_csv(
    *,
    stream: Stream,
    out_dir: Path,
    log_prefix: str,
    max_rows: int | None,
    inter_call_delay: float,
) -> tuple[list[Path], int, int, float]:
    """Pull every page sequentially; write each as a numbered CSV part.

    The first page keeps its header; subsequent pages have headers stripped
    so DuckDB's read_csv across a file list works without `union_by_name`
    schema heuristics getting confused.

    Returns (csv_part_paths, total_rows, api_calls_made, api_total_seconds).
    Stops when:
      - a page returns 0 rows (end-of-stream), OR
      - max_rows cap is reached (smoke testing).
    """
    if stream.endpoint_slug is None:
        raise ValueError(
            f"stream {stream.name} is derived (no endpoint); "
            "do not call stream_paginated_csv on it"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []
    total_rows = 0
    api_calls = 0
    api_seconds = 0.0
    offset = 0
    page_index = 0
    started_at = time.monotonic()
    last_progress_log = started_at
    with httpx.Client(headers={"User-Agent": "data-engine-x/osha-r2-ingest"}) as client:
        while True:
            t0 = time.monotonic()
            content, row_count = fetch_page(
                client,
                endpoint=stream.endpoint_slug,
                offset=offset,
                log_prefix=log_prefix,
                inter_call_delay=inter_call_delay,
            )
            elapsed = time.monotonic() - t0
            api_seconds += elapsed
            api_calls += 1
            if row_count <= 0:
                log.info("%s end-of-stream at offset=%d", log_prefix, offset)
                break
            # Strip header on pages > 0 so DuckDB read_csv with file list
            # (header=TRUE on first part only via union_by_name=TRUE) works
            # cleanly. Easier: ALL parts include the header, and we tell
            # DuckDB header=TRUE; union_by_name=TRUE handles it.
            part_path = out_dir / f"part-{page_index:05d}.csv"
            part_path.write_bytes(content)
            parts.append(part_path)
            total_rows += row_count
            page_index += 1
            offset += PAGE_LIMIT
            now = time.monotonic()
            if now - last_progress_log >= 30.0:
                rate = total_rows / max(now - started_at, 1.0)
                log.info(
                    "%s   progress: %d pages, %d rows, %.1f rows/s, %.1fs avg per page",
                    log_prefix, page_index, total_rows, rate,
                    api_seconds / max(api_calls, 1),
                )
                last_progress_log = now
            if max_rows is not None and total_rows >= max_rows:
                log.info(
                    "%s reached max_rows cap (%d), stopping pagination",
                    log_prefix, max_rows,
                )
                break
    log.info(
        "%s pagination done: %d pages, %d rows, %d API calls, %.1fs total API time",
        log_prefix, len(parts), total_rows, api_calls, api_seconds,
    )
    return parts, total_rows, api_calls, api_seconds


# --------------------------------------------------------------------------- #
# DuckDB normalizer macros — pure-SQL, vectorized at plan time
# --------------------------------------------------------------------------- #
#
# Mirrors `_lib/osha_normalize.py` for SQL parity. Trailing 'g' on
# regexp_replace is the DuckDB syntax for global replace. Suffix list
# matches the Python module's `_ESTAB_SUFFIX_TOKENS`.

_NORMALIZE_MACROS_SQL = r"""
CREATE MACRO osha_normalize_estab_name(raw) AS (
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

-- 6-digit zero-padded NAICS, NULL on sentinel '0' / '000000'.
-- Drops non-digits before zero-pad. Returns NULL for codes longer than 6.
CREATE MACRO osha_normalize_naics(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(raw) = '' THEN NULL
    WHEN length(regexp_replace(raw, '\D', '', 'g')) = 0 THEN NULL
    WHEN TRY_CAST(regexp_replace(raw, '\D', '', 'g') AS BIGINT) = 0 THEN NULL
    WHEN length(regexp_replace(raw, '\D', '', 'g')) > 6 THEN NULL
    ELSE rpad(regexp_replace(raw, '\D', '', 'g'), 6, '0')
  END
);

CREATE MACRO osha_naics_2digit(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(raw) = '' THEN NULL
    WHEN length(regexp_replace(raw, '\D', '', 'g')) = 0 THEN NULL
    WHEN TRY_CAST(regexp_replace(raw, '\D', '', 'g') AS BIGINT) = 0 THEN NULL
    WHEN length(regexp_replace(raw, '\D', '', 'g')) < 2 THEN NULL
    ELSE substr(regexp_replace(raw, '\D', '', 'g'), 1, 2)
  END
);

CREATE MACRO osha_is_construction_naics(raw) AS (
  osha_naics_2digit(raw) = '23'
);

CREATE MACRO osha_zip5(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(raw) = '' THEN NULL
    WHEN length(regexp_replace(raw, '\D', '', 'g')) < 5 THEN NULL
    ELSE substr(regexp_replace(raw, '\D', '', 'g'), 1, 5)
  END
);

CREATE MACRO osha_normalize_state(raw) AS (
  CASE
    WHEN raw IS NULL THEN NULL
    WHEN length(trim(raw)) <> 2 THEN NULL
    WHEN NOT regexp_matches(trim(raw), '^[A-Za-z]{2}$') THEN NULL
    ELSE upper(trim(raw))
  END
);

-- inspection.insp_type single-letter → 6-bin enum.
-- Default to 'OTHER' for unknown letters (matches Python module behavior).
CREATE MACRO osha_classify_insp_type(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(raw) = '' THEN NULL
    WHEN upper(trim(raw)) = 'A' THEN 'ACCIDENT'
    WHEN upper(trim(raw)) = 'B' THEN 'COMPLAINT'
    WHEN upper(trim(raw)) = 'C' THEN 'REFERRAL'
    WHEN upper(trim(raw)) = 'F' THEN 'FOLLOW_UP'
    WHEN upper(trim(raw)) IN ('H','I','J','N') THEN 'PROGRAMMED'
    WHEN upper(trim(raw)) = 'M' THEN 'ACCIDENT'
    ELSE 'OTHER'
  END
);

-- violation severity → 5-bin enum. Source field varies across legacy and
-- modern data; accept both single-letter and full-word codes.
CREATE MACRO osha_classify_violation_severity(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(raw) = '' THEN NULL
    WHEN upper(trim(raw)) IN ('S', 'SERIOUS') THEN 'SERIOUS'
    WHEN upper(trim(raw)) IN ('W', 'WILLFUL') THEN 'WILLFUL'
    WHEN upper(trim(raw)) IN ('R', 'REPEAT') THEN 'REPEAT'
    WHEN upper(trim(raw)) IN ('O', 'OTHER', 'OTHER-THAN-SERIOUS') THEN 'OTHER'
    WHEN upper(trim(raw)) IN ('U', 'UNCLASSIFIED') THEN 'UNCLASSIFIED'
    ELSE 'UNCLASSIFIED'
  END
);

CREATE MACRO osha_normalize_standard(raw) AS (
  NULLIF(
    upper(trim(regexp_replace(coalesce(raw, ''), '\s+', ' ', 'g'))),
    ''
  )
);

-- accident_injury degree_of_inj → 4-bin outcome enum.
CREATE MACRO osha_classify_outcome(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(raw) = '' THEN NULL
    WHEN upper(replace(trim(raw), ' ', '')) IN ('1', 'FATALITY', 'FATAL', 'DEATH')
      THEN 'FATALITY'
    WHEN upper(replace(trim(raw), ' ', '')) IN ('2', 'HOSPITALIZATION', 'HOSPITALIZED')
      THEN 'HOSPITALIZATION'
    WHEN upper(replace(trim(raw), ' ', '')) IN
      ('3', 'INJURY', 'NONHOSPITALIZED', 'NON-HOSPITALIZED') THEN 'INJURY'
    ELSE 'OTHER'
  END
);
"""


def _register_normalizers(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(_NORMALIZE_MACROS_SQL)


# --------------------------------------------------------------------------- #
# DuckDB transform — concatenated CSV pages → year-partitioned Parquet
# --------------------------------------------------------------------------- #


def _normalize_col(c: str) -> str:
    """Lowercase + strip BOM. The DOL API returns lowercase already, but
    a stray BOM on the first column on some responses is possible."""
    return c.lower().lstrip("﻿")


def _select_for_inspection() -> list[str]:
    """Build the SELECT projection for inspection rows.

    All raw columns survive as VARCHAR (their default DuckDB infer would
    pick up types but we want the "raw 1:1 mirror" contract); typed +
    normalized columns get appended.
    """
    return [
        # Pass-through raw columns (DuckDB will project anything in *)
        '*',
        # Typed casts
        'TRY_CAST("nr_in_estab" AS DOUBLE) AS nr_in_estab_typed',
        'TRY_CAST("open_date" AS DATE) AS open_date_typed',
        'TRY_CAST("close_case_date" AS DATE) AS close_case_date_typed',
        'TRY_CAST("case_mod_date" AS DATE) AS case_mod_date_typed',
        'TRY_CAST("close_conf_date" AS DATE) AS close_conf_date_typed',
        # Identity-spine normalizations
        '"estab_name" AS establishment_name',
        'osha_normalize_estab_name("estab_name") AS establishment_name_normalized',
        'osha_zip5("site_zip") AS establishment_address_zip5',
        'osha_normalize_state("site_state") AS establishment_address_state_normalized',
        'osha_normalize_naics("naics_code") AS naics_code_normalized',
        'osha_naics_2digit("naics_code") AS naics_2digit',
        'osha_is_construction_naics("naics_code") AS is_construction_naics',
        '"sic_code" AS legacy_sic_code',
        'osha_classify_insp_type("insp_type") AS inspection_type_normalized',
        # Year for partitioning
        'EXTRACT(YEAR FROM TRY_CAST("open_date" AS DATE))::SMALLINT AS osha_inspection_year',
    ]


def _select_for_violation() -> list[str]:
    """SELECT projection for violation rows.

    Violation table grain: one row per citation. FK `activity_nr` joins to
    inspection. Date basis: `issuance_date` for partition.
    """
    return [
        '*',
        'TRY_CAST("initial_penalty" AS DOUBLE) AS initial_penalty_typed',
        'TRY_CAST("current_penalty" AS DOUBLE) AS current_penalty_typed',
        'TRY_CAST("nr_exposed" AS DOUBLE) AS nr_exposed_typed',
        'TRY_CAST("issuance_date" AS DATE) AS issuance_date_typed',
        'TRY_CAST("abate_date" AS DATE) AS abate_date_typed',
        'osha_classify_violation_severity("viol_type") AS violation_severity_normalized',
        'osha_normalize_standard("standard") AS osha_standard_normalized',
        'EXTRACT(YEAR FROM TRY_CAST("issuance_date" AS DATE))::SMALLINT AS osha_violation_year',
    ]


def _select_for_accident_injury() -> list[str]:
    """SELECT projection for accident_injury rows."""
    return [
        '*',
        'TRY_CAST("event_date" AS DATE) AS event_date_typed',
        'osha_classify_outcome("degree_of_inj") AS accident_outcome_normalized',
        'EXTRACT(YEAR FROM TRY_CAST("event_date" AS DATE))::SMALLINT AS osha_accident_year',
    ]


_STREAM_PROJECTIONS = {
    "inspection": (_select_for_inspection, "osha_inspection_year"),
    "violation": (_select_for_violation, "osha_violation_year"),
    "accident_injury": (_select_for_accident_injury, "osha_accident_year"),
}


def csv_parts_to_year_partitioned_parquet(
    csv_paths: list[Path],
    out_dir: Path,
    *,
    stream: Stream,
    log_prefix: str,
) -> tuple[int, int, int, dict[int, int], dict[str, float]]:
    """Concatenate CSV parts, transform, write year-partitioned Parquet.

    Returns (rows_in_csv, rows_in_parquet, parquet_columns,
             rows_per_year_dict, null_rates_dict).
    Output: out_dir/year=YYYY/data.parquet (one Parquet per year).
    """
    if stream.name not in _STREAM_PROJECTIONS:
        raise ValueError(
            f"stream {stream.name} has no transform projection registered"
        )
    select_fn, year_col = _STREAM_PROJECTIONS[stream.name]
    select_parts = select_fn()

    con = duckdb.connect(":memory:")
    con.execute("PRAGMA threads=4;")
    con.execute("PRAGMA memory_limit='8GB';")
    _register_normalizers(con)

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

    rows_in_row = con.execute("SELECT count(*) FROM raw;").fetchone()
    rows_in = int(rows_in_row[0]) if rows_in_row else 0
    log.info("%s   raw row count from CSV: %s", log_prefix, f"{rows_in:,}")

    select_sql = ", ".join(select_parts)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Build the per-year COPY using PARTITION_BY. DuckDB writes
    # out_dir/<year_col>=<value>/data.parquet automatically.
    #
    # NOTE: PARTITION_BY needs the year column to exist in the SELECT
    # output. The `year_col` we registered is added by the projection.
    log.info("%s   writing year-partitioned Parquet → %s", log_prefix, out_dir)
    t0 = time.monotonic()
    con.execute(f"""
        COPY (
          SELECT {select_sql}, {year_col} AS partition_year
          FROM raw
          WHERE {year_col} IS NOT NULL
        ) TO '{out_dir}'
        (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000,
         PARTITION_BY (partition_year), OVERWRITE_OR_IGNORE);
    """)
    log.info("%s   parquet write done in %.1fs",
             log_prefix, time.monotonic() - t0)

    # Re-rename `partition_year=YYYY` directories to `year=YYYY` to match
    # the directive's R2 layout (`osha/{stream}/year={YYYY}/data.parquet`).
    # DuckDB's COPY ... PARTITION_BY uses the column name in the directory.
    for d in sorted(out_dir.iterdir()):
        if d.is_dir() and d.name.startswith("partition_year="):
            new_name = d.name.replace("partition_year=", "year=", 1)
            d.rename(d.parent / new_name)

    # Also write the rows-with-null-year to a separate "year=unknown"
    # partition so we don't silently drop rows with malformed dates.
    null_year_count_row = con.execute(f"""
        SELECT count(*) FROM raw WHERE {year_col} IS NULL;
    """).fetchone()
    null_year_count = int(null_year_count_row[0]) if null_year_count_row else 0
    if null_year_count > 0:
        log.info(
            "%s   %d rows with NULL %s — writing to year=unknown",
            log_prefix, null_year_count, year_col,
        )
        unknown_dir = out_dir / "year=unknown"
        unknown_dir.mkdir(parents=True, exist_ok=True)
        con.execute(f"""
            COPY (
              SELECT {select_sql}
              FROM raw
              WHERE {year_col} IS NULL
            ) TO '{unknown_dir}/data.parquet'
            (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000);
        """)

    # Tally rows + bytes per year partition.
    rows_per_year: dict[int, int] = {}
    parquet_total_rows = 0
    for year_dir in sorted(out_dir.iterdir()):
        if not year_dir.is_dir():
            continue
        year_label = year_dir.name.replace("year=", "")
        # COPY ... PARTITION_BY emits a single 'data.parquet' OR multiple
        # 'data_0.parquet', 'data_1.parquet', etc. Sum across all.
        for pq in year_dir.glob("*.parquet"):
            row_count = con.execute(
                f"SELECT count(*) FROM read_parquet('{pq}');"
            ).fetchone()[0]
            parquet_total_rows += row_count
            rows_per_year[year_label] = (
                rows_per_year.get(year_label, 0) + int(row_count)
            )

    log.info(
        "%s   total parquet rows: %d across %d year partitions",
        log_prefix, parquet_total_rows, len(rows_per_year),
    )

    # Compute normalization sanity (directive §5.4). Apply to inspection
    # only; violation/accident_injury have different normalized columns.
    null_rates: dict[str, float] = {}
    if stream.name == "inspection" and parquet_total_rows > 0:
        # Read all year partitions back through DuckDB for a single pass.
        glob = str(out_dir / "year=*/data.parquet")
        rates_row = con.execute(f"""
            SELECT
              count(*) AS total,
              count(*) FILTER (WHERE establishment_name_normalized IS NULL) AS name_null,
              count(*) FILTER (WHERE
                naics_code_normalized IS NOT NULL AND length(naics_code_normalized) = 6
              ) AS naics_6digit,
              count(*) FILTER (WHERE
                establishment_address_zip5 IS NOT NULL AND length(establishment_address_zip5) = 5
              ) AS zip5_ok,
              count(*) FILTER (WHERE inspection_type_normalized IS NOT NULL) AS insp_type_ok,
              count(*) FILTER (WHERE is_construction_naics) AS is_constr_yes
            FROM read_parquet('{glob}');
        """).fetchone()
        if rates_row and int(rates_row[0]) > 0:
            total = int(rates_row[0])
            null_rates = {
                "establishment_name_null_pct":
                    round(100.0 * int(rates_row[1]) / total, 4),
                "naics_code_normalized_6digit_pct":
                    round(100.0 * int(rates_row[2]) / total, 4),
                "zip5_length_5_pct":
                    round(100.0 * int(rates_row[3]) / total, 4),
                "inspection_type_non_null_pct":
                    round(100.0 * int(rates_row[4]) / total, 4),
                "is_construction_naics_pct":
                    round(100.0 * int(rates_row[5]) / total, 4),
            }
            log.info(
                "%s   normalization: name_null=%.2f%% naics6=%.2f%% zip5=%.2f%% "
                "insp_type=%.2f%% construction=%.2f%%",
                log_prefix, null_rates["establishment_name_null_pct"],
                null_rates["naics_code_normalized_6digit_pct"],
                null_rates["zip5_length_5_pct"],
                null_rates["inspection_type_non_null_pct"],
                null_rates["is_construction_naics_pct"],
            )

    # Column count from the first parquet found.
    parquet_columns = 0
    for year_dir in out_dir.iterdir():
        if not year_dir.is_dir():
            continue
        for pq in year_dir.glob("*.parquet"):
            cols = con.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{pq}');"
            ).fetchall()
            parquet_columns = len(cols)
            break
        if parquet_columns:
            break

    con.close()
    return rows_in, parquet_total_rows, parquet_columns, rows_per_year, null_rates


# --------------------------------------------------------------------------- #
# Establishments stream — DISTINCT projection over inspection on R2
# --------------------------------------------------------------------------- #


def derive_establishments_from_inspection_r2(
    out_dir: Path,
    *,
    log_prefix: str,
) -> tuple[int, int, int, dict[str, float]]:
    """Materialize the establishments stream from inspection parquet on R2.

    Reads `s3://dex-raw-landing-zone/osha/inspection/year=*/data.parquet` via
    DuckDB httpfs, projects DISTINCT (establishment_name_normalized,
    establishment_address_zip5, establishment_address_state_normalized,
    naics_code_normalized) plus most-recent inspection metadata, writes a
    single snapshot Parquet locally.

    Returns (rows_in, rows_pq, parquet_columns, null_rates).
    """
    con = duckdb.connect(":memory:")
    con.execute("PRAGMA threads=4;")
    con.execute("PRAGMA memory_limit='8GB';")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    # Map R2 access via DuckDB's S3 secret.
    con.execute(f"""
        CREATE OR REPLACE SECRET r2 (
          TYPE S3,
          KEY_ID '{_required_env("R2_ACCESS_KEY_ID")}',
          SECRET '{_required_env("R2_SECRET_ACCESS_KEY")}',
          ENDPOINT '{_required_env("R2_ENDPOINT").replace("https://", "")}',
          URL_STYLE 'path',
          REGION 'auto'
        );
    """)
    inspection_glob = (
        f"s3://{R2_BUCKET}/osha/inspection/year=*/data.parquet"
    )
    log.info("%s reading inspection parquet glob: %s", log_prefix, inspection_glob)

    # Sanity: row count
    rows_in_row = con.execute(f"""
        SELECT count(*) FROM read_parquet('{inspection_glob}', union_by_name=TRUE);
    """).fetchone()
    rows_in = int(rows_in_row[0]) if rows_in_row else 0
    log.info("%s   inspection input rows: %s", log_prefix, f"{rows_in:,}")

    out_path = out_dir / "data.parquet"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    log.info("%s   computing DISTINCT establishments + writing parquet", log_prefix)
    t0 = time.monotonic()
    con.execute(f"""
        COPY (
          WITH inspections AS (
            SELECT * FROM read_parquet('{inspection_glob}', union_by_name=TRUE)
          ),
          establishment_grain AS (
            SELECT
              establishment_name,
              establishment_name_normalized,
              site_address,
              site_city,
              site_state,
              establishment_address_state_normalized,
              site_zip,
              establishment_address_zip5,
              naics_code,
              naics_code_normalized,
              naics_2digit,
              is_construction_naics,
              legacy_sic_code,
              owner_type,
              owner_code,
              union_status,
              count(*) AS lifetime_inspection_count,
              max(open_date_typed) AS most_recent_inspection_date,
              min(open_date_typed) AS earliest_inspection_date
            FROM inspections
            WHERE establishment_name_normalized IS NOT NULL
            GROUP BY ALL
          )
          SELECT *,
                 CAST('{SNAPSHOT_DATE}' AS DATE) AS osha_establishments_snapshot_date
          FROM establishment_grain
        ) TO '{out_path}'
        (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000);
    """)
    log.info("%s   parquet write done in %.1fs",
             log_prefix, time.monotonic() - t0)

    rows_pq_row = con.execute(
        f"SELECT count(*) FROM read_parquet('{out_path}');"
    ).fetchone()
    rows_pq = int(rows_pq_row[0]) if rows_pq_row else 0

    cols = con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{out_path}');"
    ).fetchall()
    parquet_columns = len(cols)

    null_rates: dict[str, float] = {}
    if rows_pq > 0:
        rates_row = con.execute(f"""
            SELECT
              count(*) AS total,
              count(*) FILTER (WHERE establishment_name_normalized IS NULL) AS name_null,
              count(*) FILTER (WHERE
                naics_code_normalized IS NOT NULL AND length(naics_code_normalized) = 6
              ) AS naics_6digit,
              count(*) FILTER (WHERE
                establishment_address_zip5 IS NOT NULL AND length(establishment_address_zip5) = 5
              ) AS zip5_ok,
              count(*) FILTER (WHERE is_construction_naics) AS is_constr_yes
            FROM read_parquet('{out_path}');
        """).fetchone()
        total = int(rates_row[0])
        null_rates = {
            "establishment_name_null_pct":
                round(100.0 * int(rates_row[1]) / total, 4),
            "naics_code_normalized_6digit_pct":
                round(100.0 * int(rates_row[2]) / total, 4),
            "zip5_length_5_pct":
                round(100.0 * int(rates_row[3]) / total, 4),
            "is_construction_naics_pct":
                round(100.0 * int(rates_row[4]) / total, 4),
        }
        log.info(
            "%s   normalization: name_null=%.2f%% naics6=%.2f%% zip5=%.2f%% construction=%.2f%%",
            log_prefix, null_rates["establishment_name_null_pct"],
            null_rates["naics_code_normalized_6digit_pct"],
            null_rates["zip5_length_5_pct"],
            null_rates["is_construction_naics_pct"],
        )
    con.close()
    return rows_in, rows_pq, parquet_columns, null_rates


# --------------------------------------------------------------------------- #
# R2 upload — per-year for time-stratified streams, single object for snapshot
# --------------------------------------------------------------------------- #


def upload_year_partitioned_to_r2(
    out_dir: Path,
    *,
    stream: Stream,
    log_prefix: str,
) -> tuple[int, list[str]]:
    """Upload every year=YYYY/*.parquet under out_dir to R2.

    Returns (total_bytes, list_of_r2_keys).
    """
    s3 = _r2_client()
    total_bytes = 0
    keys: list[str] = []
    for year_dir in sorted(out_dir.iterdir()):
        if not year_dir.is_dir():
            continue
        for pq in sorted(year_dir.glob("*.parquet")):
            # Year partition key uses a single canonical 'data.parquet' filename
            # per directive — coalesce multi-part output to a single key when
            # there's only one partition file. When there's more than one,
            # preserve their numbered filenames to avoid clobbering.
            files_in_year = list(year_dir.glob("*.parquet"))
            if len(files_in_year) == 1:
                r2_key = (
                    f"{stream.r2_prefix_root}{year_dir.name}/data.parquet"
                )
            else:
                r2_key = f"{stream.r2_prefix_root}{year_dir.name}/{pq.name}"
            file_bytes = pq.stat().st_size
            log.info(
                "%s   uploading %s (%.1f MB) → s3://%s/%s",
                log_prefix, pq.name, file_bytes / (1 << 20),
                R2_BUCKET, r2_key,
            )
            s3.upload_file(
                str(pq), R2_BUCKET, r2_key,
                ExtraArgs={"ContentType": "application/x-parquet"},
            )
            total_bytes += file_bytes
            keys.append(r2_key)
    return total_bytes, keys


def upload_snapshot_to_r2(
    parquet_path: Path,
    *,
    stream: Stream,
    log_prefix: str,
) -> tuple[int, str]:
    """Upload a single snapshot Parquet (establishments) to R2.

    Returns (bytes_uploaded, r2_key).
    """
    s3 = _r2_client()
    r2_key = (
        f"{stream.r2_prefix_root}snapshot={SNAPSHOT_DATE}/data.parquet"
    )
    file_bytes = parquet_path.stat().st_size
    log.info(
        "%s   uploading %s (%.1f MB) → s3://%s/%s",
        log_prefix, parquet_path.name, file_bytes / (1 << 20),
        R2_BUCKET, r2_key,
    )
    s3.upload_file(
        str(parquet_path), R2_BUCKET, r2_key,
        ExtraArgs={"ContentType": "application/x-parquet"},
    )
    return file_bytes, r2_key


# --------------------------------------------------------------------------- #
# Audit-row helpers
# --------------------------------------------------------------------------- #


def _insert_run_row(
    conn: psycopg.Connection,
    stream: Stream,
    *,
    partition_value: str,
    source_url: str,
) -> str:
    sql = """
    INSERT INTO ops.osha_r2_ingest_runs (
        stream, partition_value, status, source_url
    ) VALUES (%s, %s, 'running', %s)
    RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (stream.name, partition_value, source_url))
        row_id = cur.fetchone()[0]
    conn.commit()
    return str(row_id)


def _finalize_run_row(
    conn: psycopg.Connection,
    run_id: str,
    *,
    status: str,
    api_calls_made: int,
    api_total_seconds: float,
    rows_fetched: int,
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
    nr = null_rates or {}
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE ops.osha_r2_ingest_runs
               SET status = %s,
                   api_calls_made = %s,
                   api_total_seconds = %s,
                   rows_fetched = %s,
                   parquet_row_count = %s,
                   parquet_bytes_written = %s,
                   parquet_column_count = %s,
                   r2_bucket = %s, r2_prefix = %s, r2_object_key = %s,
                   r2_total_bytes = %s,
                   establishment_name_null_pct = %s,
                   naics_code_normalized_6digit_pct = %s,
                   zip5_length_5_pct = %s,
                   inspection_type_non_null_pct = %s,
                   is_construction_naics_pct = %s,
                   finished_at = now(), duration_seconds = %s,
                   error_message = %s, notes = %s
             WHERE id = %s;
            """, (
            status, api_calls_made, api_total_seconds, rows_fetched,
            parquet_row_count, parquet_bytes_written, parquet_column_count,
            r2_bucket, r2_prefix, r2_object_key, r2_total_bytes,
            nr.get("establishment_name_null_pct"),
            nr.get("naics_code_normalized_6digit_pct"),
            nr.get("zip5_length_5_pct"),
            nr.get("inspection_type_non_null_pct"),
            nr.get("is_construction_naics_pct"),
            duration, error_message,
            Jsonb(notes) if notes else None, run_id,
        ))
    conn.commit()


# --------------------------------------------------------------------------- #
# Per-stream ingest entry points
# --------------------------------------------------------------------------- #


def ingest_paginated_stream(
    stream: Stream,
    *,
    workdir: Path,
    max_rows: int | None,
    inter_call_delay: float,
    dry_run: bool,
) -> int:
    """Ingest a paginated /v4 endpoint stream (inspection / violation /
    accident_injury). Year-partitioned Parquet → R2 → audit row."""
    log_prefix = f"[{stream.name}]"
    started_wall = time.monotonic()

    # The partition_value for paginated streams is "all-years" — the script
    # writes per-year files in a single run; a more granular "(stream,
    # year)" partition_value is doable as a future enhancement but
    # complicates idempotency for the v1 run.
    partition_value = "all-years"
    source_url = f"{DOL_API_BASE}/get/osha/{stream.endpoint_slug}/csv"

    if dry_run:
        log.info("%s DRY RUN — would ingest %s", log_prefix, source_url)
        return 0

    stream_dir = workdir / stream.name
    if stream_dir.exists():
        shutil.rmtree(stream_dir)
    stream_dir.mkdir(parents=True, exist_ok=True)
    csv_dir = stream_dir / "csvs"
    pq_dir = stream_dir / "parquet"

    with psycopg.connect(_database_url()) as conn:
        run_id = _insert_run_row(
            conn, stream,
            partition_value=partition_value,
            source_url=source_url,
        )
        log.info("%s run id=%s", log_prefix, run_id)

        try:
            csv_paths, total_rows, api_calls, api_seconds = (
                stream_paginated_csv(
                    stream=stream,
                    out_dir=csv_dir,
                    log_prefix=log_prefix,
                    max_rows=max_rows,
                    inter_call_delay=inter_call_delay,
                )
            )
            (rows_in, parquet_rows, parquet_columns, rows_per_year,
             null_rates) = csv_parts_to_year_partitioned_parquet(
                csv_paths, pq_dir, stream=stream, log_prefix=log_prefix,
            )
            uploaded_bytes, r2_keys = upload_year_partitioned_to_r2(
                pq_dir, stream=stream, log_prefix=log_prefix,
            )
            parquet_bytes = sum(
                pq.stat().st_size
                for year_dir in pq_dir.iterdir() if year_dir.is_dir()
                for pq in year_dir.glob("*.parquet")
            )

            _finalize_run_row(
                conn, run_id, status="completed",
                api_calls_made=api_calls,
                api_total_seconds=round(api_seconds, 3),
                rows_fetched=total_rows,
                parquet_row_count=parquet_rows,
                parquet_bytes_written=parquet_bytes,
                parquet_column_count=parquet_columns,
                r2_bucket=R2_BUCKET,
                r2_prefix=stream.r2_prefix_root,
                r2_object_key=",".join(r2_keys[:5]) + (
                    f" (+{len(r2_keys) - 5} more)" if len(r2_keys) > 5 else ""
                ),
                r2_total_bytes=uploaded_bytes,
                null_rates=null_rates,
                started_at=started_wall, error_message=None,
                notes={
                    "max_rows": max_rows,
                    "inter_call_delay_s": inter_call_delay,
                    "rows_per_year": rows_per_year,
                    "r2_keys": r2_keys,
                },
            )
            log.info(
                "%s DONE rows=%d parquet=%.1f MB r2=%.1f MB wall=%.1fs",
                log_prefix, parquet_rows,
                parquet_bytes / (1 << 20),
                uploaded_bytes / (1 << 20),
                time.monotonic() - started_wall,
            )
            return 0

        except Exception as exc:
            log.exception("%s ingest failed", log_prefix)
            _finalize_run_row(
                conn, run_id, status="failed",
                api_calls_made=0, api_total_seconds=0,
                rows_fetched=0,
                parquet_row_count=0, parquet_bytes_written=0,
                parquet_column_count=0,
                r2_bucket=None, r2_prefix=None, r2_object_key=None,
                r2_total_bytes=0, null_rates=None,
                started_at=started_wall,
                error_message=str(exc), notes=None,
            )
            return 1
        finally:
            shutil.rmtree(csv_dir, ignore_errors=True)
            shutil.rmtree(pq_dir, ignore_errors=True)


def ingest_establishments_derived(
    *,
    workdir: Path,
    dry_run: bool,
) -> int:
    """Re-derive the establishments snapshot from inspection on R2."""
    stream = _stream_lookup("establishments")
    log_prefix = f"[{stream.name}]"
    started_wall = time.monotonic()
    partition_value = f"snapshot={SNAPSHOT_DATE}"
    source_url = f"derived from {R2_BUCKET}/osha/inspection/"

    if dry_run:
        log.info("%s DRY RUN — would derive from %s", log_prefix, source_url)
        return 0

    stream_dir = workdir / stream.name
    if stream_dir.exists():
        shutil.rmtree(stream_dir)
    stream_dir.mkdir(parents=True, exist_ok=True)

    with psycopg.connect(_database_url()) as conn:
        run_id = _insert_run_row(
            conn, stream,
            partition_value=partition_value,
            source_url=source_url,
        )
        log.info("%s run id=%s", log_prefix, run_id)

        try:
            rows_in, rows_pq, parquet_columns, null_rates = (
                derive_establishments_from_inspection_r2(
                    stream_dir, log_prefix=log_prefix,
                )
            )
            parquet_path = stream_dir / "data.parquet"
            uploaded_bytes, r2_key = upload_snapshot_to_r2(
                parquet_path, stream=stream, log_prefix=log_prefix,
            )

            _finalize_run_row(
                conn, run_id, status="completed",
                api_calls_made=0, api_total_seconds=0,
                rows_fetched=rows_in,
                parquet_row_count=rows_pq,
                parquet_bytes_written=parquet_path.stat().st_size,
                parquet_column_count=parquet_columns,
                r2_bucket=R2_BUCKET,
                r2_prefix=stream.r2_prefix_root,
                r2_object_key=r2_key,
                r2_total_bytes=uploaded_bytes,
                null_rates=null_rates,
                started_at=started_wall, error_message=None,
                notes={
                    "snapshot_date": SNAPSHOT_DATE,
                    "r2_key": r2_key,
                    "derivation_basis": "DISTINCT projection over inspection",
                },
            )
            log.info(
                "%s DONE rows_in=%d rows_pq=%d r2=%.1f MB wall=%.1fs",
                log_prefix, rows_in, rows_pq,
                uploaded_bytes / (1 << 20),
                time.monotonic() - started_wall,
            )
            return 0
        except Exception as exc:
            log.exception("%s ingest failed", log_prefix)
            _finalize_run_row(
                conn, run_id, status="failed",
                api_calls_made=0, api_total_seconds=0,
                rows_fetched=0,
                parquet_row_count=0, parquet_bytes_written=0,
                parquet_column_count=0,
                r2_bucket=None, r2_prefix=None, r2_object_key=None,
                r2_total_bytes=0, null_rates=None,
                started_at=started_wall,
                error_message=str(exc), notes=None,
            )
            return 1
        finally:
            shutil.rmtree(stream_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "stream", nargs="?",
        choices=[s.name for s in STREAMS],
        help="Which OSHA stream to ingest. Required unless --all.",
    )
    p.add_argument(
        "--all", action="store_true",
        help="Ingest every stream sequentially (paginated streams first, "
             "then establishments DISTINCT-derived).",
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max-rows", type=int, default=None,
                   help="Cap rows fetched (smoke testing).")
    p.add_argument("--inter-call-delay", type=float, default=2.0,
                   help="Seconds between consecutive API calls "
                        "(default 2.0; 0.5 for burst-friendly tiers).")
    p.add_argument("--workdir", default=None)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    workdir = Path(args.workdir or "/tmp/osha_r2_ingest")
    workdir.mkdir(parents=True, exist_ok=True)

    if args.all:
        # Paginated streams first; establishments must run AFTER inspection
        # since it reads inspection's R2 output.
        streams = list(STREAMS)
    else:
        if not args.stream:
            log.error("must pass stream name (or use --all); valid: %s",
                      [s.name for s in STREAMS])
            return 2
        streams = [_stream_lookup(args.stream)]

    rc = 0
    for s in streams:
        log.info("=" * 70)
        log.info("=== INGEST: stream=%s — %s ===", s.name, s.description)
        log.info("=" * 70)
        if s.name == "establishments":
            rc_one = ingest_establishments_derived(
                workdir=workdir, dry_run=args.dry_run,
            )
        else:
            rc_one = ingest_paginated_stream(
                s,
                workdir=workdir,
                max_rows=args.max_rows,
                inter_call_delay=args.inter_call_delay,
                dry_run=args.dry_run,
            )
        if rc_one != 0:
            rc = rc_one
            log.error("stream %s failed (rc=%d); continuing", s.name, rc_one)
    return rc


if __name__ == "__main__":
    sys.exit(main())

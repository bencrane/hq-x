#!/usr/bin/env python3
"""CMS Open Payments historical backfill + Ownership feed → R2 Fuel Tank.

Sunshine Act §6002 mandates pharma/medical-device payments to physicians and
teaching hospitals be reported annually since 2013. This script backfills
historical years AND adds the previously-missing Ownership feed (physician
equity / investment interests) for every year.

**Year span (deviation from directive):** 2018-2024.

The directive 2026-05-08-cms-open-payments-backfill-r2-ingest.md scoped
2013-2023 but **CMS retired pre-2018 data** from its bulk download portal.
Direct URL probes against PGYR2013-PGYR2017 patterns return 404, and the
DKAN metastore (https://openpaymentsdata.cms.gov/api/1/metastore/...)
exhaustively lists 2018-2024 only. This script ingests every year CMS
currently publishes; a follow-up directive can chase the archived 2013-2017
data via the National Archives or CMS FOIA if the operator still wants it.

**(year, feed) job count:**

| feed       | year span    | jobs | note                                    |
|------------|--------------|------|-----------------------------------------|
| general    | 2018-2023    | 6    | 2024 already exists (out of scope)      |
| research   | 2018-2023    | 6    | 2024 already exists (out of scope)      |
| ownership  | 2018-2024    | 7    | NEW feed for every year                 |
| **total**  |              | **19** |                                       |

**R2 layout:**

    cms-open-payments/year=<YYYY>/feed=<general|research|ownership>/data.parquet

Single-Parquet-per-(year, feed), mirroring the FEC + HMDA precedents (the
existing year=2024 General+Research uses multi-part `part-NNNNN.parquet`
naming — schema-divergent but glob-compatible with the RisingWave source's
`match_pattern='cms-open-payments/year=*/feed=*/*.parquet'`).

**Schema convention (HMDA-style flat, vs existing CMS narrow-hot+raw_json):**

Per the directive's identity-spine requirement and CLAUDE.md §"Source
ingest invariant" sub-case for Volume King + the Data Factory Protocol §5
(identity-spine indexes), this Parquet ships:
  - Every CMS source CSV column as VARCHAR (true 1:1 mirror).
  - `total_amount_of_payment_usdollars` cast to DOUBLE (General + Research).
  - `dollar_amount_invested` cast to DOUBLE (Ownership).
  - `date_of_payment` (G+R) / `interest_held_from_date` (Ownership) cast
    to DATE via TRY_STRPTIME('%m/%d/%Y').
  - Identity-spine VARCHAR columns: `physician_first_normalized`,
    `physician_last_normalized`, `physician_npi_normalized`,
    `physician_zip5`, `physician_state_normalized`,
    `manufacturer_name_normalized`, `manufacturer_ein_normalized`.
  - Partition metadata: `cms_op_year` (SMALLINT), `cms_op_feed` (VARCHAR).

Schema-divergence with the existing year=2024 General+Research partitions
is intentional: the RW source declares its own ~13-column projection and
silently ignores the wider columns; future RW MV work can extend the source
to consume the new identity-spine columns. The new Parquet is the canonical
path forward; year=2024 G+R can be re-ingested through this script later
if the operator wants schema parity (out-of-scope per directive).

**Idempotency:** HEAD `Last-Modified` per (year, feed). If unchanged since
prior `completed` audit row, write `no_change` row and skip. CMS rotates
publication dates within the URL when republishing; any republish bumps
Last-Modified.

**Audit ledger:** `ops.cms_open_payments_r2_ingest_runs` (one row per
(year, feed)). Migration `20260508212352_…_ownership_feed.sql` relaxes
the `feed_name` CHECK constraint to accept 'ownership'.

Usage:

    # smoke test (writes locally, no R2 upload, no DB row):
    doppler run -p hq-all -c prd -- \\
      python3 apps/data-engine-x/scripts/run_cms_open_payments_backfill_r2_ingest.py \\
        --year 2023 --feed ownership --max-rows 50000 --no-upload

    # single (year, feed) full ingest:
    doppler run -p hq-all -c prd -- \\
      python3 apps/data-engine-x/scripts/run_cms_open_payments_backfill_r2_ingest.py \\
        --year 2023 --feed ownership

    # full backfill — all 19 (year, feed) jobs:
    doppler run -p hq-all -c prd -- \\
      python3 apps/data-engine-x/scripts/run_cms_open_payments_backfill_r2_ingest.py \\
        --all

    # constrained backfill (e.g., 2020-2022 ownership only):
    doppler run -p hq-all -c prd -- \\
      python3 apps/data-engine-x/scripts/run_cms_open_payments_backfill_r2_ingest.py \\
        --years 2020-2022 --feed ownership

See directive ~/Desktop/hq/directives/2026-05-08-cms-open-payments-backfill-r2-ingest.md.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import boto3
import duckdb
import httpx
import psycopg

# Identity-spine normalization is implemented as DuckDB SQL macros below
# (see _NORMALIZE_MACROS_SQL); the canonical Python reference lives in
# scripts/_lib/cms_op_normalize.py and is exercised by
# tests/scripts/test_cms_op_normalize.py + the SQL-parity test.

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

R2_BUCKET = "dex-raw-landing-zone"
R2_PREFIX = "cms-open-payments"
METASTORE_URL = (
    "https://openpaymentsdata.cms.gov/api/1/metastore/schemas/dataset/items"
)
USER_AGENT = "data-engine-x/cms-op-r2-backfill"

VALID_FEEDS = ("general", "research", "ownership")

# Year span CMS currently publishes via DKAN (verified 2026-05-08).
# Pre-2018 returned 0 hits across both /metastore and direct URL probes.
GENERAL_RESEARCH_YEARS: tuple[int, ...] = tuple(range(2018, 2024))  # 2018..2023
OWNERSHIP_YEARS: tuple[int, ...] = tuple(range(2018, 2025))          # 2018..2024

RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 5

# Per-feed canonical date column for transformation (CMS publishes "MM/DD/YYYY").
# Verified against year=2023 CSV smoke output for all three feeds.
DATE_COLUMNS = {
    "general": "date_of_payment",
    "research": "date_of_payment",
    "ownership": "payment_publication_date",
}

# Per-feed canonical numeric column. Ownership feed uses
# "Total_Amount_Invested_USDollars" rather than the General/Research
# "Total_Amount_of_Payment_USDollars" — different terminology because
# Ownership rows describe equity stakes, not single payments.
NUMERIC_COLUMNS = {
    "general": "total_amount_of_payment_usdollars",
    "research": "total_amount_of_payment_usdollars",
    "ownership": "total_amount_invested_usdollars",
}


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
    )
    return logging.getLogger("cms-op-backfill")


log = _logger()


# --------------------------------------------------------------------------- #
# DKAN URL discovery
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DiscoveredCsv:
    title: str
    download_url: str
    filename: str
    modified: datetime


def _feed_title(feed: str) -> str:
    # CMS title pattern: "<YYYY> <General|Research|Ownership> Payment Data".
    return feed.capitalize()


def discover_csv_url(client: httpx.Client, year: int, feed: str) -> DiscoveredCsv:
    """Resolve the canonical CSV URL for (year, feed) via DKAN metastore.

    CMS rotates publication-date stamps in the URL on every republish; we
    always pin the latest stamp by querying the metastore at runtime rather
    than computing URLs algorithmically.
    """
    title_re = re.compile(rf"^{year} {_feed_title(feed)} Payment Data$")
    r = client.get(METASTORE_URL, params={"limit": 500})
    r.raise_for_status()
    items = r.json()
    for item in items:
        title = item.get("title", "")
        if not title_re.match(title):
            continue
        distributions = item.get("distribution", []) or []
        if not distributions:
            raise RuntimeError(f"metastore item {title!r} has no distributions")
        url = distributions[0].get("downloadURL") or distributions[0].get("accessURL")
        if not url:
            raise RuntimeError(f"metastore item {title!r} missing downloadURL")
        modified_str = item.get("modified", "") or ""
        try:
            modified = datetime.fromisoformat(
                modified_str + "T00:00:00+00:00"
            )
        except (ValueError, TypeError):
            modified = datetime.now(timezone.utc)
        filename = url.rsplit("/", 1)[-1]
        log.info(
            "discovered year=%d feed=%s url=%s modified=%s",
            year, feed, url, modified_str,
        )
        return DiscoveredCsv(
            title=title, download_url=url, filename=filename, modified=modified,
        )
    raise RuntimeError(
        f"no metastore item matched '{year} {_feed_title(feed)} Payment Data'"
    )


# --------------------------------------------------------------------------- #
# HTTP layer (HEAD + streamed GET)
# --------------------------------------------------------------------------- #


def head_url(
    client: httpx.Client, url: str,
) -> tuple[int | None, datetime | None, int]:
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


def stream_download(
    client: httpx.Client, url: str, dest: Path,
) -> int:
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            written = 0
            with client.stream(
                "GET", url, follow_redirects=True, timeout=3600.0,
            ) as r:
                if r.status_code in RETRY_STATUSES:
                    wait = min(2 ** attempt, 30)
                    log.warning(
                        "GET %s HTTP %s; retry in %ss",
                        url, r.status_code, wait,
                    )
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
# DuckDB transform
# --------------------------------------------------------------------------- #


# SQL-macro mirror of scripts/_lib/cms_op_normalize.py. Pure SQL avoids the
# numpy dep that DuckDB Python UDFs require. Tested for parity with the
# Python module via tests/scripts/test_cms_op_normalize_sql_parity.py — if
# you change suffix lists or normalization rules in either place, update
# both AND re-run that parity test.
_NORMALIZE_MACROS_SQL = r"""
CREATE OR REPLACE MACRO cms_op_norm_name(raw) AS (
  NULLIF(
    trim(regexp_replace(
      regexp_replace(
        regexp_replace(lower(coalesce(raw, '')), '[''’]', '', 'g'),
        '[^a-z0-9 ]+', ' ', 'g'
      ),
      '\s+', ' ', 'g'
    )),
    ''
  )
);

CREATE OR REPLACE MACRO cms_op_norm_npi(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(raw) = '' THEN NULL
    WHEN length(regexp_replace(raw, '\D', '', 'g')) = 10
      THEN regexp_replace(raw, '\D', '', 'g')
    WHEN length(regexp_replace(raw, '\D', '', 'g')) BETWEEN 1 AND 9
      THEN lpad(regexp_replace(raw, '\D', '', 'g'), 10, '0')
    ELSE NULL
  END
);

CREATE OR REPLACE MACRO cms_op_zip5(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(raw) = '' THEN NULL
    WHEN length(regexp_replace(raw, '\D', '', 'g')) < 5 THEN NULL
    ELSE substr(regexp_replace(raw, '\D', '', 'g'), 1, 5)
  END
);

CREATE OR REPLACE MACRO cms_op_norm_state(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(raw) = '' THEN NULL
    WHEN length(regexp_replace(upper(raw), '[^A-Z]', '', 'g')) = 2
      THEN regexp_replace(upper(raw), '[^A-Z]', '', 'g')
    ELSE NULL
  END
);

CREATE OR REPLACE MACRO cms_op_norm_ein(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(raw) = '' THEN NULL
    WHEN length(regexp_replace(raw, '\D', '', 'g')) = 9
      THEN regexp_replace(raw, '\D', '', 'g')
    ELSE NULL
  END
);

-- Manufacturer name normalization. The terminal-suffix strip uses a single
-- regex `(\s+(suffix))+\s*$` which greedily eats one or more terminal
-- suffix words — equivalent to the Python loop's repeated terminal-pop.
-- Suffix list is the alternation in the regex; mirrors
-- scripts/_lib/cms_op_normalize.py:_MANUFACTURER_SUFFIXES character-for-character.
CREATE OR REPLACE MACRO cms_op_norm_manufacturer(raw) AS (
  NULLIF(
    trim(
      regexp_replace(
        trim(regexp_replace(
          regexp_replace(lower(coalesce(raw, '')), '[^a-z0-9 ]+', ' ', 'g'),
          '\s+', ' ', 'g'
        )),
        '(\s+(inc|incorporated|llc|ltd|limited|corp|corporation|company|co|gmbh|ag|sa|spa|plc|usa|us|na|holdings|group|pharmaceuticals|pharmaceutical|pharma|biosciences|bioscience|biotech|biotechnology|medical|medicals|devices|device|therapeutics|diagnostics|healthcare|health|labs|laboratories|laboratory))+\s*$',
        '',
        'g'
      )
    ),
    ''
  )
);
"""


def _register_normalizers(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(_NORMALIZE_MACROS_SQL)


@dataclass
class TransformResult:
    rows_in: int
    rows_out: int
    parquet_bytes: int
    column_count: int
    null_rates: dict[str, float]


def csv_to_parquet(
    csv_path: Path,
    parquet_path: Path,
    *,
    year: int,
    feed: str,
    log_prefix: str,
    max_rows: int | None,
    threads: int = 4,
    memory_limit: str = "6GB",
) -> TransformResult:
    """Read CSV → DuckDB → ZSTD Parquet with normalization.

    Strategy:
      1. read_csv(all_varchar=TRUE, ignore_errors=TRUE) preserves leading
         zeros + sentinels; survives malformed rows.
      2. Project ALL upstream columns lowercased (true 1:1 mirror).
      3. Override the canonical numeric + date columns with typed casts.
      4. Append identity-spine normalized columns + partition metadata.
      5. COPY TO Parquet (ZSTD, ROW_GROUP_SIZE 100000).

    Returns row counts + Parquet bytes + null-rate sanity metrics.
    """
    con = duckdb.connect(":memory:")
    con.execute(f"PRAGMA threads={threads};")
    con.execute(f"PRAGMA memory_limit='{memory_limit}';")
    _register_normalizers(con)

    # Phase 1: build a CSV view over raw rows (no projection yet).
    # parallel=FALSE is required when null_padding=TRUE coexists with
    # quoted newlines — CMS General Payments CSVs have both (some
    # `Name_of_Drug_or_Biological_or_Device_or_Medical_Supply_*` values
    # contain embedded newlines inside quoted strings, and older years
    # publish fewer columns than newer ones). The single-threaded scan
    # adds 30-60s on a 12M-row General CSV but is correct.
    # ignore_errors=TRUE lets DuckDB skip rows with quoting / encoding issues.
    con.execute(f"""
        CREATE VIEW raw AS
        SELECT * FROM read_csv(
          '{csv_path}',
          delim=',',
          header=TRUE,
          quote='"',
          escape='"',
          all_varchar=TRUE,
          ignore_errors=TRUE,
          null_padding=TRUE,
          parallel=FALSE
        );
    """)

    columns = [r[0] for r in con.execute("DESCRIBE raw;").fetchall()]
    log.info("%s   csv columns: %d", log_prefix, len(columns))

    # Skip the explicit count when max_rows is set — count(*) over a 7GB
    # CSV is wasted work for smoke tests, and the variance check only
    # fires for full (non-capped) runs.
    if max_rows is not None:
        rows_in = -1  # sentinel: not measured
        log.info("%s   csv raw rows: not measured (--max-rows set)", log_prefix)
    else:
        rows_in_row = con.execute("SELECT count(*) FROM raw;").fetchone()
        rows_in = int(rows_in_row[0]) if rows_in_row else 0
        log.info("%s   csv raw rows: %s", log_prefix, f"{rows_in:,}")

    # Phase 2: build the projection.
    # - Lowercased column names everywhere (Postgres convention; RW source
    #   declares lowercase too).
    # - Numeric override on the canonical USD column for this feed.
    # - Date override on the canonical date column for this feed.
    # - All other columns kept as VARCHAR.
    numeric_col = NUMERIC_COLUMNS[feed]
    date_col = DATE_COLUMNS[feed]

    select_parts: list[str] = []
    for c in columns:
        lc = c.lower()
        if lc == numeric_col:
            select_parts.append(
                f'TRY_CAST(NULLIF(trim("{c}"), \'\') AS DOUBLE) AS "{lc}"'
            )
        elif lc == date_col:
            select_parts.append(
                f"TRY_CAST(TRY_STRPTIME(NULLIF(trim(\"{c}\"), ''), "
                f"'%m/%d/%Y') AS DATE) AS \"{lc}\""
            )
        else:
            select_parts.append(f'"{c}" AS "{lc}"')

    # Phase 3: identity-spine normalized columns. Each CMS feed publishes
    # physician identity columns under slightly different names; we use
    # COALESCE over a fallback list and tolerate missing columns by
    # checking the columns set inline.
    def _coalesce_first_match(candidates: tuple[str, ...]) -> str:
        present = [c for c in candidates if c.lower() in {x.lower() for x in columns}]
        if not present:
            return "NULL"
        if len(present) == 1:
            return f'"{present[0]}"'
        return "COALESCE(" + ", ".join(f'"{c}"' for c in present) + ")"

    # Per-feed candidate column lists. Research feed's top-level Recipient
    # is typically a teaching hospital (NOT a physician); the actual
    # physician identity lives in `Principal_Investigator_1_*` columns
    # (CMS Research feed lists up to 5 PIs per row — we keep PI 1 as the
    # canonical physician for identity-spine purposes; PI 2-5 stay
    # recoverable via the wide flat-mirror Parquet schema). General +
    # Ownership populate the top-level Physician/Covered_Recipient
    # columns directly, so PI fallback is harmless there (COALESCE picks
    # the first non-NULL).
    physician_first_src = _coalesce_first_match((
        "Physician_First_Name",
        "Covered_Recipient_First_Name",
        "Recipient_First_Name",
        "Principal_Investigator_1_First_Name",
    ))
    physician_last_src = _coalesce_first_match((
        "Physician_Last_Name",
        "Covered_Recipient_Last_Name",
        "Recipient_Last_Name",
        "Principal_Investigator_1_Last_Name",
    ))
    physician_npi_src = _coalesce_first_match((
        "Physician_NPI",
        "Covered_Recipient_NPI",
        "Recipient_NPI",
        "Principal_Investigator_1_NPI",
    ))
    physician_zip_src = _coalesce_first_match((
        "Recipient_Zip_Code",
        "Recipient_Postal_Code",
        "Recipient_ZIP_Code",
        "Principal_Investigator_1_Zip_Code",
        "Principal_Investigator_1_Postal_Code",
    ))
    physician_state_src = _coalesce_first_match((
        "Recipient_State",
        "Principal_Investigator_1_State",
    ))
    manufacturer_name_src = _coalesce_first_match((
        "Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_Name",
        "Submitting_Applicable_Manufacturer_or_Applicable_GPO_Name",
    ))
    manufacturer_ein_src = _coalesce_first_match((
        "Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_EIN",
        "Submitting_Applicable_Manufacturer_or_Applicable_GPO_EIN",
    ))

    select_parts.extend([
        f"cms_op_norm_name({physician_first_src})  AS physician_first_normalized",
        f"cms_op_norm_name({physician_last_src})   AS physician_last_normalized",
        f"cms_op_norm_npi({physician_npi_src})     AS physician_npi_normalized",
        f"cms_op_zip5({physician_zip_src})         AS physician_zip5",
        f"cms_op_norm_state({physician_state_src}) AS physician_state_normalized",
        f"cms_op_norm_manufacturer({manufacturer_name_src}) AS manufacturer_name_normalized",
        f"cms_op_norm_ein({manufacturer_ein_src})  AS manufacturer_ein_normalized",
        f"CAST({year} AS SMALLINT)                 AS cms_op_year",
        f"CAST('{feed}' AS VARCHAR)                AS cms_op_feed",
    ])

    limit_clause = f"LIMIT {max_rows}" if max_rows is not None else ""
    select_sql = f"SELECT {', '.join(select_parts)} FROM raw {limit_clause}"

    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    con.execute(f"""
        COPY ({select_sql}) TO '{parquet_path}'
        (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000);
    """)
    parquet_bytes = parquet_path.stat().st_size
    log.info(
        "%s   parquet write: %.1f MB in %.1fs",
        log_prefix, parquet_bytes / (1 << 20), time.monotonic() - t0,
    )

    # Phase 4: null-rate sanity check.
    rates_row = con.execute(f"""
        SELECT
          count(*) AS total,
          count(*) FILTER (WHERE physician_npi_normalized IS NOT NULL) AS npi_present,
          count(*) FILTER (WHERE physician_first_normalized IS NULL
                              AND physician_last_normalized IS NULL) AS name_both_null,
          count(*) FILTER (WHERE manufacturer_name_normalized IS NULL) AS mfr_null
        FROM read_parquet('{parquet_path}');
    """).fetchone()
    total = int(rates_row[0]) if rates_row else 0
    rows_pq = total
    rates: dict[str, float]
    if total > 0:
        rates = {
            "physician_npi_present_pct": round(100.0 * int(rates_row[1]) / total, 4),
            "physician_name_both_null_pct": round(100.0 * int(rates_row[2]) / total, 4),
            "manufacturer_name_null_pct": round(100.0 * int(rates_row[3]) / total, 4),
        }
    else:
        rates = {
            "physician_npi_present_pct": 0.0,
            "physician_name_both_null_pct": 0.0,
            "manufacturer_name_null_pct": 0.0,
        }

    column_count_row = con.execute(
        f"SELECT count(*) FROM (DESCRIBE SELECT * FROM read_parquet('{parquet_path}'));"
    ).fetchone()
    column_count = int(column_count_row[0]) if column_count_row else 0

    log.info(
        "%s   parquet rows: %s; cols: %d; npi_present=%.2f%% name_null=%.2f%% mfr_null=%.2f%%",
        log_prefix, f"{rows_pq:,}", column_count,
        rates["physician_npi_present_pct"],
        rates["physician_name_both_null_pct"],
        rates["manufacturer_name_null_pct"],
    )

    con.close()
    return TransformResult(
        rows_in=rows_in, rows_out=rows_pq,
        parquet_bytes=parquet_bytes, column_count=column_count,
        null_rates=rates,
    )


# --------------------------------------------------------------------------- #
# R2 + audit ledger
# --------------------------------------------------------------------------- #


def _required_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"{name} is not set in the environment.")
    return v


def make_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=_required_env("R2_ENDPOINT"),
        aws_access_key_id=_required_env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=_required_env("R2_SECRET_ACCESS_KEY"),
        region_name="auto",
    )


def upload_to_r2(
    s3, parquet_path: Path, *, bucket: str, key: str,
) -> int:
    file_bytes = parquet_path.stat().st_size
    s3.upload_file(
        str(parquet_path), bucket, key,
        ExtraArgs={"ContentType": "application/x-parquet"},
    )
    return file_bytes


def db_url() -> str:
    return _required_env("DEX_DB_URL_DIRECT")


def insert_run_row(
    conn: psycopg.Connection,
    *,
    feed: str, year: int,
    source_url: str, source_filename: str,
    source_last_modified: datetime | None,
    invoked_by: str | None,
) -> uuid.UUID:
    sql = """
    INSERT INTO ops.cms_open_payments_r2_ingest_runs (
        run_id, feed_name, program_year,
        source_url, source_filename, source_last_modified,
        status, started_at, invoked_by
    ) VALUES (
        gen_random_uuid(), %s, %s,
        %s, %s, %s,
        'running', now(), %s
    ) RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            feed, year,
            source_url, source_filename, source_last_modified,
            invoked_by,
        ))
        row_id = cur.fetchone()[0]
    conn.commit()
    return row_id


def get_prior_completed_lm(
    conn: psycopg.Connection, year: int, feed: str,
) -> datetime | None:
    sql = """
    SELECT source_last_modified
      FROM ops.cms_open_payments_r2_ingest_runs
     WHERE feed_name = %s AND program_year = %s
       AND status = 'completed'
     ORDER BY started_at DESC LIMIT 1
    """
    with conn.cursor() as cur:
        cur.execute(sql, (feed, year))
        row = cur.fetchone()
    return row[0] if row else None


def write_no_change_run(
    conn: psycopg.Connection,
    *,
    feed: str, year: int,
    source_url: str, source_filename: str,
    source_last_modified: datetime | None,
    invoked_by: str | None,
) -> None:
    sql = """
    INSERT INTO ops.cms_open_payments_r2_ingest_runs (
        run_id, feed_name, program_year,
        source_url, source_filename, source_last_modified,
        status, started_at, completed_at, duration_seconds, invoked_by
    ) VALUES (
        gen_random_uuid(), %s, %s,
        %s, %s, %s,
        'no_change', now(), now(), 0, %s
    );
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            feed, year,
            source_url, source_filename, source_last_modified,
            invoked_by,
        ))
    conn.commit()


def finalize_run_row(
    conn: psycopg.Connection,
    row_id: uuid.UUID,
    *,
    status: str,
    r2_prefix: str | None = None,
    r2_object_count: int | None = None,
    r2_total_bytes: int | None = None,
    parquet_row_count: int | None = None,
    source_byte_size: int | None = None,
    duration_seconds: float | None = None,
    error_class: str | None = None,
    error_message: str | None = None,
) -> None:
    sql = """
    UPDATE ops.cms_open_payments_r2_ingest_runs
       SET status = %s,
           completed_at = now(),
           r2_prefix = COALESCE(%s, r2_prefix),
           r2_object_count = COALESCE(%s, r2_object_count),
           r2_total_bytes = COALESCE(%s, r2_total_bytes),
           parquet_row_count = COALESCE(%s, parquet_row_count),
           source_byte_size = COALESCE(%s, source_byte_size),
           duration_seconds = COALESCE(%s, duration_seconds),
           error_class = COALESCE(%s, error_class),
           error_message = COALESCE(%s, error_message)
     WHERE id = %s;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            status, r2_prefix, r2_object_count, r2_total_bytes,
            parquet_row_count, source_byte_size,
            duration_seconds, error_class, error_message,
            row_id,
        ))
    conn.commit()


# --------------------------------------------------------------------------- #
# Per-(year, feed) main
# --------------------------------------------------------------------------- #


def ingest_one(
    *,
    year: int, feed: str,
    skip_if_unchanged: bool,
    dry_run: bool,
    workdir: Path,
    upload: bool,
    max_rows: int | None,
    invoked_by: str | None,
    r2_prefix_override: str | None,
) -> int:
    log_prefix = f"[year={year} feed={feed}]"
    started_wall = time.monotonic()
    log.info("%s start", log_prefix)

    with httpx.Client(headers={"User-Agent": USER_AGENT}) as client:
        try:
            discovered = discover_csv_url(client, year, feed)
        except Exception:
            log.exception("%s metastore discovery failed", log_prefix)
            return 1

        try:
            content_length, source_last_modified, status_code = head_url(
                client, discovered.download_url,
            )
        except Exception:
            log.exception("%s HEAD failed", log_prefix)
            return 1
        if status_code == 404:
            log.error("%s HEAD 404 — CSV not published", log_prefix)
            return 1
        log.info(
            "%s HEAD content_length=%s last_modified=%s",
            log_prefix, content_length, source_last_modified,
        )
        if dry_run:
            log.info("%s DRY RUN — exiting after HEAD", log_prefix)
            return 0

        # Audit row + idempotency
        audit_conn: psycopg.Connection | None = None
        run_id: uuid.UUID | None = None
        if upload:
            audit_conn = psycopg.connect(db_url(), autocommit=False)
            prior = get_prior_completed_lm(audit_conn, year, feed)
            log.info("%s prior source_last_modified: %s", log_prefix, prior)
            if (
                skip_if_unchanged
                and prior is not None
                and source_last_modified is not None
                and source_last_modified <= prior
            ):
                log.info("%s source unchanged — recording no_change", log_prefix)
                write_no_change_run(
                    audit_conn,
                    feed=feed, year=year,
                    source_url=discovered.download_url,
                    source_filename=discovered.filename,
                    source_last_modified=source_last_modified,
                    invoked_by=invoked_by,
                )
                audit_conn.close()
                return 0

            run_id = insert_run_row(
                audit_conn,
                feed=feed, year=year,
                source_url=discovered.download_url,
                source_filename=discovered.filename,
                source_last_modified=source_last_modified,
                invoked_by=invoked_by,
            )
            log.info("%s audit run id: %s", log_prefix, run_id)

        # Download → transform → upload
        csv_path = workdir / discovered.filename
        parquet_path = workdir / f"{year}_{feed}.parquet"
        try:
            if csv_path.exists() and csv_path.stat().st_size > 1024:
                csv_bytes = csv_path.stat().st_size
                log.info(
                    "%s csv cached (%.1f MB) at %s",
                    log_prefix, csv_bytes / (1 << 20), csv_path,
                )
            else:
                csv_bytes = stream_download(
                    client, discovered.download_url, csv_path,
                )
                log.info(
                    "%s downloaded %.1f MB",
                    log_prefix, csv_bytes / (1 << 20),
                )

            t = csv_to_parquet(
                csv_path, parquet_path,
                year=year, feed=feed,
                log_prefix=log_prefix, max_rows=max_rows,
            )

            if max_rows is None and t.rows_in > 0:
                variance = abs(t.rows_out - t.rows_in) / t.rows_in
                if variance > 0.001:
                    raise RuntimeError(
                        f"row-count variance {variance:.4%} > 0.1% "
                        f"(in={t.rows_in:,} pq={t.rows_out:,})"
                    )

            target_prefix = (
                r2_prefix_override
                or f"{R2_PREFIX}/year={year}/feed={feed}/"
            )
            target_key = target_prefix.rstrip("/") + "/data.parquet"

            if upload:
                s3 = make_s3_client()
                uploaded = upload_to_r2(
                    s3, parquet_path, bucket=R2_BUCKET, key=target_key,
                )
                log.info(
                    "%s uploaded → s3://%s/%s (%.1f MB)",
                    log_prefix, R2_BUCKET, target_key, uploaded / (1 << 20),
                )
            else:
                uploaded = t.parquet_bytes
                log.info(
                    "%s --no-upload — local parquet at %s (%.1f MB)",
                    log_prefix, parquet_path, uploaded / (1 << 20),
                )

            if audit_conn is not None and run_id is not None:
                finalize_run_row(
                    audit_conn, run_id,
                    status="completed",
                    r2_prefix=target_prefix,
                    r2_object_count=1,
                    r2_total_bytes=uploaded,
                    parquet_row_count=t.rows_out,
                    source_byte_size=csv_bytes,
                    duration_seconds=time.monotonic() - started_wall,
                )

            log.info(
                "%s DONE rows=%s upload=%.1f MB cols=%d wall=%.1fs",
                log_prefix, f"{t.rows_out:,}",
                uploaded / (1 << 20), t.column_count,
                time.monotonic() - started_wall,
            )
            return 0

        except Exception as exc:
            log.exception("%s ingest failed", log_prefix)
            if audit_conn is not None and run_id is not None:
                try:
                    finalize_run_row(
                        audit_conn, run_id,
                        status="failed",
                        duration_seconds=time.monotonic() - started_wall,
                        error_class="parse_failure",
                        error_message=str(exc)[:1000],
                    )
                except Exception:
                    log.exception("failed to write failure audit row")
            return 1

        finally:
            # In --no-upload mode the local CSV + Parquet are the
            # deliverable (smoke-test inspection / iteration); leave
            # them on disk so re-runs can skip the multi-GB download.
            if upload:
                try:
                    csv_path.unlink(missing_ok=True)
                except Exception:
                    pass
                try:
                    parquet_path.unlink(missing_ok=True)
                except Exception:
                    pass
            if audit_conn is not None:
                audit_conn.close()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_year_range(s: str, allowed: Iterable[int]) -> list[int]:
    allowed_set = set(allowed)
    if "-" in s:
        a, b = s.split("-", 1)
        ya, yb = int(a), int(b)
    else:
        ya = yb = int(s)
    return [y for y in range(ya, yb + 1) if y in allowed_set]


def jobs_for_args(args: argparse.Namespace) -> list[tuple[int, str]]:
    """Return ordered list of (year, feed) jobs."""
    jobs: list[tuple[int, str]] = []
    if args.all:
        for y in GENERAL_RESEARCH_YEARS:
            jobs.append((y, "general"))
            jobs.append((y, "research"))
        for y in OWNERSHIP_YEARS:
            jobs.append((y, "ownership"))
        return jobs

    feeds: list[str]
    if args.feed == "all":
        feeds = list(VALID_FEEDS)
    else:
        feeds = [args.feed] if args.feed else list(VALID_FEEDS)

    years: list[int]
    if args.years:
        # Use the union of all-feed allowed years for parsing; per-feed
        # validity is enforced inside the loop below.
        all_years = set(GENERAL_RESEARCH_YEARS) | set(OWNERSHIP_YEARS)
        years = parse_year_range(args.years, all_years)
    elif args.year is not None:
        years = [args.year]
    else:
        log.error("must pass --year, --years, or --all")
        sys.exit(2)

    for f in feeds:
        allowed = (
            OWNERSHIP_YEARS if f == "ownership" else GENERAL_RESEARCH_YEARS
        )
        for y in years:
            if y not in allowed:
                log.warning(
                    "skipping (year=%d, feed=%s) — not in allowed span %s",
                    y, f, allowed,
                )
                continue
            jobs.append((y, f))

    return jobs


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--year", type=int, default=None,
                   help="Single program year (e.g., 2023).")
    p.add_argument("--years", default=None,
                   help="Year range (e.g., 2020-2023).")
    p.add_argument("--feed", choices=list(VALID_FEEDS) + ["all"], default=None,
                   help="One of general / research / ownership / all.")
    p.add_argument("--all", action="store_true",
                   help="Run all 19 (year, feed) jobs (full backfill).")
    p.add_argument("--skip-if-unchanged", action="store_true",
                   help="Skip if Last-Modified ≤ prior completed run's.")
    p.add_argument("--dry-run", action="store_true",
                   help="Do everything up to HEAD; do not download/transform.")
    p.add_argument("--no-upload", action="store_true",
                   help="Write Parquet locally only; skip R2 upload + audit row.")
    p.add_argument("--max-rows", type=int, default=None,
                   help="Cap output rows (smoke testing).")
    p.add_argument("--workdir", default=None,
                   help="Local cache directory (default: /tmp/cms_op_backfill).")
    p.add_argument("--r2-prefix-override", default=None,
                   help="Replace canonical cms-open-payments/year=YYYY/feed=F/ prefix.")
    p.add_argument("--invoked-by", default=os.environ.get("USER", "manual"))
    return p.parse_args()


def main() -> int:
    args = parse_args()
    workdir = Path(args.workdir or "/tmp/cms_op_backfill")
    workdir.mkdir(parents=True, exist_ok=True)

    jobs = jobs_for_args(args)
    if not jobs:
        log.error("no jobs to run; check --year / --years / --feed / --all")
        return 2

    log.info(
        "planning %d (year, feed) job(s): %s",
        len(jobs), ", ".join(f"{y}/{f}" for y, f in jobs),
    )

    rc = 0
    for i, (year, feed) in enumerate(jobs, start=1):
        log.info("=" * 70)
        log.info("=== JOB %d/%d: year=%d feed=%s ===", i, len(jobs), year, feed)
        log.info("=" * 70)
        rc_one = ingest_one(
            year=year, feed=feed,
            skip_if_unchanged=args.skip_if_unchanged,
            dry_run=args.dry_run,
            workdir=workdir,
            upload=not args.no_upload,
            max_rows=args.max_rows,
            invoked_by=args.invoked_by,
            r2_prefix_override=args.r2_prefix_override,
        )
        if rc_one != 0:
            rc = rc_one
            log.error(
                "job (year=%d, feed=%s) failed; continuing with remaining",
                year, feed,
            )
    # Best-effort cleanup of workdir if empty.
    try:
        if not any(workdir.iterdir()):
            shutil.rmtree(workdir, ignore_errors=True)
    except Exception:
        pass

    return rc


if __name__ == "__main__":
    sys.exit(main())

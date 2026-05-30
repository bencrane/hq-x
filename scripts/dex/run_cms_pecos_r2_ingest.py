#!/usr/bin/env python3
"""CMS PECOS → R2 Fuel Tank ingest (5 streams).

Mirrors the CMS Provider Enrollment, Chain, and Ownership System (PECOS)
public bulk extracts — ~3M Medicare-enrolled provider/supplier records —
into Cloudflare R2 as ZSTD-compressed Parquet, snapshot-partitioned by
ingest date.

Source: data.cms.gov public open-data catalog
  - PPEF Enrollment Extract (2.98M rows; practitioners + organizations
    + DMEPOS suppliers in one CSV, distinguished by PROVIDER_TYPE_DESC)
  - Order and Referring (1-2M rows; eligibility flags per NPI)
  - Revalidation Clinic Group Practice Reassignment (3-5M
    practitioner↔org reassignment links)
  - Revalidation Due Date List (joined onto practitioners + orgs to
    surface the 3-5 year revalidation cycle as a forward-looking
    compliance event — the GTM payoff)

Five streams written per snapshot:

  R2 layout:
    cms-pecos/
      practitioners/snapshot=YYYY-MM-DD/data.parquet
      organizations/snapshot=YYYY-MM-DD/data.parquet
      dmepos_suppliers/snapshot=YYYY-MM-DD/data.parquet
      reassignment_of_benefits/snapshot=YYYY-MM-DD/data.parquet
      order_refer/snapshot=YYYY-MM-DD/data.parquet

Audit ledger: ops.cms_pecos_r2_ingest_runs.

The directive originally specified surety-bond columns on the
dmepos_suppliers stream — those fields are NOT in the public CMS PECOS
extract (DMEPOS surety bond data is held in a separate gated CMS
system). The dmepos_suppliers Parquet ships enrollment metadata only;
surety / accreditation columns will be populated when (if) CMS exposes
them via the Provider Data Catalog.

Identity-spine join keys at output:
  practitioners + dmepos_suppliers + order_refer:
    provider_npi_normalized (10-digit, leading-zero left-padded)
  organizations:
    org_npi_normalized
  reassignment_of_benefits:
    practitioner_npi_normalized + group_pac_id

Idempotency: per-stream HEAD Last-Modified check. If the upstream
artifact's Last-Modified matches a prior `completed` run for the same
(snapshot_date, stream), the script writes a `no_change` audit row and
skips the download.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_cms_pecos_r2_ingest.py --all
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_cms_pecos_r2_ingest.py \\
      --streams practitioners,order_refer
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_cms_pecos_r2_ingest.py \\
      --streams practitioners --max-rows 50000 \\
      --r2-prefix-override 'cms-pecos/_smoke/practitioners_50k'

See directive ~/Desktop/hq/directives/2026-05-08-cms-pecos-r2-ingest.md.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import boto3
import duckdb
import httpx
import psycopg
from psycopg.types.json import Jsonb


R2_BUCKET = "dex-raw-landing-zone"
USER_AGENT = "data-engine-x/cms-pecos-r2-ingest"
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 5

CMS_DATA_JSON_URL = "https://data.cms.gov/data.json"

# Per-stream catalog discovery. CMS dataset slugs occasionally rotate;
# we discover the latest CSV URL per stream from data.cms.gov/data.json
# at runtime rather than hardcoding URLs.
@dataclass(frozen=True)
class StreamSpec:
    name: str
    catalog_title: str          # exact data.cms.gov dataset title to match
    fallback_url: str | None    # used only if catalog discovery fails


STREAM_SPECS: tuple[StreamSpec, ...] = (
    # practitioners + organizations + dmepos_suppliers all derive from
    # the same PPEF source. Three streams, one upstream file.
    StreamSpec(
        name="practitioners",
        catalog_title="Medicare Fee-For-Service  Public Provider Enrollment",
        fallback_url=None,
    ),
    StreamSpec(
        name="organizations",
        catalog_title="Medicare Fee-For-Service  Public Provider Enrollment",
        fallback_url=None,
    ),
    StreamSpec(
        name="dmepos_suppliers",
        catalog_title="Medicare Fee-For-Service  Public Provider Enrollment",
        fallback_url=None,
    ),
    StreamSpec(
        name="reassignment_of_benefits",
        catalog_title="Revalidation Clinic Group Practice Reassignment",
        fallback_url=None,
    ),
    StreamSpec(
        name="order_refer",
        catalog_title="Order and Referring",
        fallback_url=None,
    ),
)

# Companion file (not a stream of its own) — joined onto practitioners +
# organizations to surface revalidation_due_date.
REVALIDATION_BASE_TITLE = "Revalidation Due Date List"


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("cms-pecos-r2-ingest")


log = _logger()


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
# Catalog discovery + HTTP layer
# --------------------------------------------------------------------------- #


def fetch_data_json(client: httpx.Client) -> dict[str, Any]:
    """Download the CMS Open Data Catalog JSON. Cached to /tmp for the
    process lifetime — every stream needs the same file."""
    r = client.get(CMS_DATA_JSON_URL, follow_redirects=True, timeout=120.0)
    r.raise_for_status()
    return r.json()


def discover_stream_url(
    catalog: dict[str, Any], dataset_title: str,
) -> str | None:
    """Find the most-recent CSV download URL for a dataset by exact title.

    CMS publishes the latest distribution first. We scan distributions
    for the FIRST entry that has mediaType=text/csv (or a .csv URL).
    Returns None if the title isn't found or no CSV exists.
    """
    for ds in catalog.get("dataset", []):
        if ds.get("title") != dataset_title:
            continue
        for dist in ds.get("distribution", []) or []:
            mt = dist.get("mediaType")
            url = dist.get("downloadURL") or dist.get("accessURL") or ""
            if (mt == "text/csv" or url.lower().endswith(".csv")) and url:
                return url
        return None
    return None


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


def download_csv(
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
# DuckDB normalize macros (parity with scripts/_lib/cms_pecos_normalize.py)
# --------------------------------------------------------------------------- #


_NORMALIZE_MACROS_SQL = r"""
-- 10-digit left-pad NPI normalizer.
CREATE MACRO pecos_normalize_npi(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(CAST(raw AS VARCHAR)) = '' THEN NULL
    WHEN length(regexp_replace(CAST(raw AS VARCHAR), '\D', '', 'g')) = 0 THEN NULL
    WHEN length(regexp_replace(CAST(raw AS VARCHAR), '\D', '', 'g')) > 10 THEN NULL
    ELSE lpad(regexp_replace(CAST(raw AS VARCHAR), '\D', '', 'g'), 10, '0')
  END
);

-- Provider name part normalizer: lowercase, strip [.,'-], collapse ws.
CREATE MACRO pecos_normalize_provider_name(raw) AS (
  NULLIF(
    trim(regexp_replace(
      regexp_replace(lower(coalesce(raw, '')), '[.,''\-]', ' ', 'g'),
      '\s+', ' ', 'g'
    )),
    ''
  )
);

-- Org name normalizer: lowercase, strip [.,&], collapse ws, then iterative
-- strip ONE trailing org suffix word per pass. DuckDB doesn't support
-- recursion in macros, so we unroll up to 3 passes (covers all observed
-- "ACME MEDICAL GROUP LLC" → "ACME MEDICAL GROUP" → "ACME MEDICAL"
-- iterative strips). _ORG_SUFFIXES list mirrors the Python ref impl.
CREATE MACRO _pecos_strip_one_org_suffix(s) AS (
  CASE
    WHEN s IS NULL OR s = '' THEN NULL
    ELSE (
      WITH _stripp AS (SELECT string_split(s, ' ') AS toks)
      SELECT CASE
        WHEN length(toks) >= 2 AND toks[length(toks)] IN
             ('llc','inc','incorporated','corp','corporation',
              'co','company','ltd','limited','lp','llp',
              'pa','pc','pllc','group','associates',
              'association','practice')
        THEN array_to_string(toks[1:length(toks)-1], ' ')
        ELSE s
      END FROM _stripp
    )
  END
);

CREATE MACRO pecos_normalize_org_name(raw) AS (
  NULLIF(
    _pecos_strip_one_org_suffix(
      _pecos_strip_one_org_suffix(
        _pecos_strip_one_org_suffix(
          NULLIF(
            trim(regexp_replace(
              regexp_replace(lower(coalesce(raw, '')), '[.,&]', ' ', 'g'),
              '\s+', ' ', 'g'
            )),
            ''
          )
        )
      )
    ),
    ''
  )
);

-- ZIP5 extractor.
CREATE MACRO pecos_zip5(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(CAST(raw AS VARCHAR)) = '' THEN NULL
    WHEN length(regexp_replace(CAST(raw AS VARCHAR), '\D', '', 'g')) < 5 THEN NULL
    ELSE substr(regexp_replace(CAST(raw AS VARCHAR), '\D', '', 'g'), 1, 5)
  END
);

-- 2-letter state normalizer.
CREATE MACRO pecos_normalize_state(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(raw) = '' THEN NULL
    WHEN length(trim(raw)) != 2 THEN NULL
    WHEN regexp_matches(trim(upper(raw)), '^[A-Z]{2}$') THEN trim(upper(raw))
    ELSE NULL
  END
);

-- Specialty normalizer: lowercase, collapse whitespace.
CREATE MACRO pecos_normalize_specialty(raw) AS (
  NULLIF(
    trim(regexp_replace(lower(coalesce(raw, '')), '\s+', ' ', 'g')),
    ''
  )
);

-- Org-type bucketer: maps PROVIDER_TYPE_DESC prefixes to a canonical
-- bucket. Mirrors scripts/_lib/cms_pecos_normalize.derive_org_type.
CREATE MACRO pecos_derive_org_type(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(raw) = '' THEN NULL
    WHEN starts_with(upper(raw), 'PART A PROVIDER - HOSPITAL')
      THEN 'HOSPITAL'
    WHEN starts_with(upper(raw), 'PART A PROVIDER - CRITICAL ACCESS HOSPITAL')
      THEN 'HOSPITAL'
    WHEN starts_with(upper(raw), 'PART A PROVIDER - SKILLED NURSING FACILITY')
      THEN 'SNF'
    WHEN starts_with(upper(raw), 'PART A PROVIDER - HOME HEALTH AGENCY')
      THEN 'HOME_HEALTH_AGENCY'
    WHEN starts_with(upper(raw), 'PART A PROVIDER - HOSPICE')
      THEN 'HOSPICE'
    WHEN starts_with(upper(raw),
                     'PART A PROVIDER - FEDERALLY QUALIFIED HEALTH CENTER')
      THEN 'FQHC'
    WHEN starts_with(upper(raw), 'PART A PROVIDER - RURAL HEALTH CLINIC')
      THEN 'RURAL_HEALTH_CLINIC'
    WHEN starts_with(upper(raw),
                     'PART A PROVIDER - END-STAGE RENAL DISEASE FACILITY')
      THEN 'ESRD_FACILITY'
    WHEN starts_with(upper(raw),
                     'PART B SUPPLIER - CLINIC/GROUP PRACTICE')
      THEN 'GROUP_PRACTICE'
    WHEN starts_with(upper(raw),
                     'PART B SUPPLIER - AMBULATORY SURGICAL CENTER')
      THEN 'ASC'
    WHEN starts_with(upper(raw),
              'PART B SUPPLIER - INDEPENDENT DIAGNOSTIC TESTING FACILITY')
      THEN 'IDTF'
    WHEN starts_with(upper(raw),
                'PART B SUPPLIER - INDEPENDENT CLINICAL LABORATORY')
      THEN 'CLINICAL_LAB'
    WHEN starts_with(upper(raw), 'PART B SUPPLIER - AMBULANCE')
      THEN 'AMBULANCE'
    WHEN starts_with(upper(raw), 'PART B SUPPLIER - PHARMACY')
      THEN 'PHARMACY'
    WHEN starts_with(upper(raw), 'DME SUPPLIER')
      THEN 'DME_SUPPLIER'
    WHEN starts_with(upper(raw), 'PART A PROVIDER')
      THEN 'OTHER_PART_A'
    WHEN starts_with(upper(raw), 'PART B SUPPLIER')
      THEN 'OTHER_PART_B'
    ELSE NULL
  END
);

-- Y/N → boolean.
CREATE MACRO pecos_yn_to_bool(raw) AS (
  CASE
    WHEN raw IS NULL THEN NULL
    WHEN upper(trim(raw)) = 'Y' THEN TRUE
    WHEN upper(trim(raw)) = 'N' THEN FALSE
    ELSE NULL
  END
);
"""


def _register_normalizers(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(_NORMALIZE_MACROS_SQL)


# --------------------------------------------------------------------------- #
# Stream transforms
# --------------------------------------------------------------------------- #


def transform_practitioners(
    *,
    ppef_csv: Path, revalidation_csv: Path | None,
    parquet_path: Path, snapshot_date: date,
    max_rows: int | None,
) -> tuple[int, int, dict[str, float]]:
    """PPEF → practitioners.parquet.

    Filters PPEF rows where ORG_NAME is empty (= individual-grain).
    LEFT JOIN onto Revalidation Due Date List by ENRLMT_ID to surface
    revalidation_due_date.
    """
    con = duckdb.connect(":memory:")
    con.execute("PRAGMA threads=4;")
    con.execute("PRAGMA memory_limit='6GB';")
    _register_normalizers(con)

    con.execute(f"""
        CREATE VIEW raw_ppef AS
        SELECT * FROM read_csv(
          '{ppef_csv}',
          delim=',', header=TRUE,
          all_varchar=TRUE,
          ignore_errors=TRUE, null_padding=TRUE
        );
    """)

    if revalidation_csv is not None and revalidation_csv.exists():
        con.execute(f"""
            CREATE VIEW raw_revalidation AS
            SELECT * FROM read_csv(
              '{revalidation_csv}',
              delim=',', header=TRUE,
              all_varchar=TRUE,
              ignore_errors=TRUE, null_padding=TRUE
            );
        """)
    else:
        # Empty placeholder — LEFT JOIN yields all NULLs.
        con.execute("""
            CREATE VIEW raw_revalidation AS
            SELECT
              CAST(NULL AS VARCHAR) AS "Enrollment ID",
              CAST(NULL AS VARCHAR) AS "Revalidation Due Date",
              CAST(NULL AS VARCHAR) AS "Adjusted Due Date";
        """)

    rows_in_row = con.execute("SELECT count(*) FROM raw_ppef;").fetchone()
    rows_in = int(rows_in_row[0]) if rows_in_row else 0

    limit_clause = f"LIMIT {max_rows}" if max_rows is not None else ""

    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"""
        COPY (
          SELECT
            -- Raw PPEF columns preserved as VARCHAR.
            "NPI"                          AS npi,
            "MULTIPLE_NPI_FLAG"            AS multiple_npi_flag,
            "PECOS_ASCT_CNTL_ID"           AS pecos_asct_cntl_id,
            "ENRLMT_ID"                    AS enrollment_id,
            "PROVIDER_TYPE_CD"             AS provider_type_code,
            "PROVIDER_TYPE_DESC"           AS provider_type_desc,
            "STATE_CD"                     AS practice_state_raw,
            "FIRST_NAME"                   AS first_name,
            "MDL_NAME"                     AS middle_name,
            "LAST_NAME"                    AS last_name,
            "ORG_NAME"                     AS org_name,
            -- Joined columns from Revalidation Due Date List.
            r."Revalidation Due Date"      AS revalidation_due_date_raw,
            r."Adjusted Due Date"          AS revalidation_adjusted_due_date_raw,
            CAST(TRY_STRPTIME(NULLIF(r."Revalidation Due Date", ''),
                              '%m/%d/%Y') AS DATE)
                                            AS revalidation_due_date,
            CAST(TRY_STRPTIME(NULLIF(r."Adjusted Due Date", ''),
                              '%m/%d/%Y') AS DATE)
                                            AS revalidation_adjusted_due_date,
            -- Identity-spine normalized columns.
            pecos_normalize_npi("NPI")            AS provider_npi_normalized,
            pecos_normalize_provider_name("FIRST_NAME")
                                                  AS provider_first_normalized,
            pecos_normalize_provider_name("LAST_NAME")
                                                  AS provider_last_normalized,
            pecos_normalize_state("STATE_CD")     AS practice_state_normalized,
            pecos_normalize_specialty("PROVIDER_TYPE_DESC")
                                                  AS primary_specialty_normalized,
            -- PPEF only contains active enrollments.
            'ACTIVE'                              AS enrollment_status_normalized,
            -- Surety / malpractice carrier data not in public PECOS.
            FALSE                                 AS is_malpractice_carrier_present,
            CAST('{snapshot_date.isoformat()}' AS DATE) AS cms_pecos_snapshot_date
          FROM raw_ppef p
          LEFT JOIN raw_revalidation r
            ON p."ENRLMT_ID" = r."Enrollment ID"
          WHERE coalesce(trim(p."ORG_NAME"), '') = ''
            AND coalesce(trim(p."FIRST_NAME"), '') <> ''
          {limit_clause}
        ) TO '{parquet_path}'
        (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000);
    """)

    rates_row = con.execute(f"""
        SELECT
          count(*) AS total,
          count(*) FILTER (WHERE provider_npi_normalized IS NULL) AS id_null,
          count(*) FILTER (WHERE provider_first_normalized IS NULL
                           AND provider_last_normalized IS NULL) AS name_null,
          count(*) FILTER (WHERE practice_state_normalized IS NULL) AS state_null
        FROM read_parquet('{parquet_path}');
    """).fetchone()
    total = int(rates_row[0]) if rates_row else 0
    rates = (
        {
            "primary_id_null_pct": round(100.0 * int(rates_row[1]) / total, 4),
            "primary_name_null_pct": round(100.0 * int(rates_row[2]) / total, 4),
            "state_null_pct": round(100.0 * int(rates_row[3]) / total, 4),
        }
        if total > 0
        else {"primary_id_null_pct": 0.0, "primary_name_null_pct": 0.0,
              "state_null_pct": 0.0}
    )
    con.close()
    return rows_in, total, rates


def transform_organizations(
    *,
    ppef_csv: Path, revalidation_csv: Path | None,
    parquet_path: Path, snapshot_date: date,
    max_rows: int | None,
) -> tuple[int, int, dict[str, float]]:
    """PPEF → organizations.parquet (org-grain only, DMEPOS suppliers
    EXCLUDED — they get their own stream)."""
    con = duckdb.connect(":memory:")
    con.execute("PRAGMA threads=4;")
    con.execute("PRAGMA memory_limit='6GB';")
    _register_normalizers(con)

    con.execute(f"""
        CREATE VIEW raw_ppef AS
        SELECT * FROM read_csv(
          '{ppef_csv}',
          delim=',', header=TRUE,
          all_varchar=TRUE,
          ignore_errors=TRUE, null_padding=TRUE
        );
    """)
    if revalidation_csv is not None and revalidation_csv.exists():
        con.execute(f"""
            CREATE VIEW raw_revalidation AS
            SELECT * FROM read_csv(
              '{revalidation_csv}',
              delim=',', header=TRUE,
              all_varchar=TRUE,
              ignore_errors=TRUE, null_padding=TRUE
            );
        """)
    else:
        con.execute("""
            CREATE VIEW raw_revalidation AS
            SELECT
              CAST(NULL AS VARCHAR) AS "Enrollment ID",
              CAST(NULL AS VARCHAR) AS "Revalidation Due Date",
              CAST(NULL AS VARCHAR) AS "Adjusted Due Date";
        """)

    rows_in_row = con.execute("SELECT count(*) FROM raw_ppef;").fetchone()
    rows_in = int(rows_in_row[0]) if rows_in_row else 0

    limit_clause = f"LIMIT {max_rows}" if max_rows is not None else ""

    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"""
        COPY (
          SELECT
            "NPI"                          AS org_npi,
            "MULTIPLE_NPI_FLAG"            AS multiple_npi_flag,
            "PECOS_ASCT_CNTL_ID"           AS pecos_asct_cntl_id,
            "ENRLMT_ID"                    AS enrollment_id,
            "PROVIDER_TYPE_CD"             AS provider_type_code,
            "PROVIDER_TYPE_DESC"           AS provider_type_desc,
            "STATE_CD"                     AS org_state_raw,
            "ORG_NAME"                     AS org_name,
            r."Revalidation Due Date"      AS revalidation_due_date_raw,
            r."Adjusted Due Date"          AS revalidation_adjusted_due_date_raw,
            CAST(TRY_STRPTIME(NULLIF(r."Revalidation Due Date", ''),
                              '%m/%d/%Y') AS DATE)
                                            AS revalidation_due_date,
            CAST(TRY_STRPTIME(NULLIF(r."Adjusted Due Date", ''),
                              '%m/%d/%Y') AS DATE)
                                            AS revalidation_adjusted_due_date,
            pecos_normalize_npi("NPI")            AS org_npi_normalized,
            pecos_normalize_org_name("ORG_NAME")  AS org_name_normalized,
            pecos_normalize_state("STATE_CD")     AS org_state_normalized,
            pecos_derive_org_type("PROVIDER_TYPE_DESC")
                                                  AS org_type_normalized,
            'ACTIVE'                              AS enrollment_status_normalized,
            CAST('{snapshot_date.isoformat()}' AS DATE) AS cms_pecos_snapshot_date
          FROM raw_ppef p
          LEFT JOIN raw_revalidation r
            ON p."ENRLMT_ID" = r."Enrollment ID"
          WHERE coalesce(trim(p."ORG_NAME"), '') <> ''
            AND NOT starts_with(upper(p."PROVIDER_TYPE_DESC"), 'DME SUPPLIER')
          {limit_clause}
        ) TO '{parquet_path}'
        (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000);
    """)

    rates_row = con.execute(f"""
        SELECT
          count(*) AS total,
          count(*) FILTER (WHERE org_npi_normalized IS NULL) AS id_null,
          count(*) FILTER (WHERE org_name_normalized IS NULL) AS name_null,
          count(*) FILTER (WHERE org_state_normalized IS NULL) AS state_null
        FROM read_parquet('{parquet_path}');
    """).fetchone()
    total = int(rates_row[0]) if rates_row else 0
    rates = (
        {
            "primary_id_null_pct": round(100.0 * int(rates_row[1]) / total, 4),
            "primary_name_null_pct": round(100.0 * int(rates_row[2]) / total, 4),
            "state_null_pct": round(100.0 * int(rates_row[3]) / total, 4),
        }
        if total > 0
        else {"primary_id_null_pct": 0.0, "primary_name_null_pct": 0.0,
              "state_null_pct": 0.0}
    )
    con.close()
    return rows_in, total, rates


def transform_dmepos_suppliers(
    *,
    ppef_csv: Path, revalidation_csv: Path | None,
    parquet_path: Path, snapshot_date: date,
    max_rows: int | None,
) -> tuple[int, int, dict[str, float]]:
    """PPEF → dmepos_suppliers.parquet (DME SUPPLIER prefix only).

    Surety bond / accreditation columns are NOT in the public PECOS
    extract — they're carved off into a sibling future-stream when CMS
    publishes them. Currently the stream ships enrollment metadata
    only; surety_bond_carrier_normalized / surety_bond_amount_dollars /
    surety_bond_effective_date / surety_bond_termination_date columns
    are present but always NULL.
    """
    con = duckdb.connect(":memory:")
    con.execute("PRAGMA threads=4;")
    con.execute("PRAGMA memory_limit='6GB';")
    _register_normalizers(con)

    con.execute(f"""
        CREATE VIEW raw_ppef AS
        SELECT * FROM read_csv(
          '{ppef_csv}',
          delim=',', header=TRUE,
          all_varchar=TRUE,
          ignore_errors=TRUE, null_padding=TRUE
        );
    """)
    if revalidation_csv is not None and revalidation_csv.exists():
        con.execute(f"""
            CREATE VIEW raw_revalidation AS
            SELECT * FROM read_csv(
              '{revalidation_csv}',
              delim=',', header=TRUE,
              all_varchar=TRUE,
              ignore_errors=TRUE, null_padding=TRUE
            );
        """)
    else:
        con.execute("""
            CREATE VIEW raw_revalidation AS
            SELECT
              CAST(NULL AS VARCHAR) AS "Enrollment ID",
              CAST(NULL AS VARCHAR) AS "Revalidation Due Date",
              CAST(NULL AS VARCHAR) AS "Adjusted Due Date";
        """)

    rows_in_row = con.execute("SELECT count(*) FROM raw_ppef;").fetchone()
    rows_in = int(rows_in_row[0]) if rows_in_row else 0

    limit_clause = f"LIMIT {max_rows}" if max_rows is not None else ""

    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"""
        COPY (
          SELECT
            "NPI"                          AS supplier_npi,
            "MULTIPLE_NPI_FLAG"            AS multiple_npi_flag,
            "PECOS_ASCT_CNTL_ID"           AS pecos_asct_cntl_id,
            "ENRLMT_ID"                    AS enrollment_id,
            "PROVIDER_TYPE_CD"             AS provider_type_code,
            "PROVIDER_TYPE_DESC"           AS provider_type_desc,
            "STATE_CD"                     AS supplier_state_raw,
            "FIRST_NAME"                   AS first_name,
            "MDL_NAME"                     AS middle_name,
            "LAST_NAME"                    AS last_name,
            "ORG_NAME"                     AS supplier_business_name,
            r."Revalidation Due Date"      AS revalidation_due_date_raw,
            r."Adjusted Due Date"          AS revalidation_adjusted_due_date_raw,
            CAST(TRY_STRPTIME(NULLIF(r."Revalidation Due Date", ''),
                              '%m/%d/%Y') AS DATE)
                                            AS revalidation_due_date,
            CAST(TRY_STRPTIME(NULLIF(r."Adjusted Due Date", ''),
                              '%m/%d/%Y') AS DATE)
                                            AS revalidation_adjusted_due_date,
            pecos_normalize_npi("NPI")            AS supplier_npi_normalized,
            pecos_normalize_org_name(
                coalesce(NULLIF("ORG_NAME", ''),
                         "FIRST_NAME" || ' ' || "LAST_NAME")
            )                                     AS supplier_name_normalized,
            pecos_normalize_state("STATE_CD")     AS supplier_state_normalized,
            -- Surety bond / accreditation columns — placeholders only.
            CAST(NULL AS VARCHAR)                 AS surety_bond_carrier_normalized,
            CAST(NULL AS DOUBLE)                  AS surety_bond_amount_dollars,
            CAST(NULL AS DATE)                    AS surety_bond_effective_date,
            CAST(NULL AS DATE)                    AS surety_bond_termination_date,
            CAST(NULL AS VARCHAR)                 AS accreditation_organization_normalized,
            CAST(NULL AS DATE)                    AS accreditation_termination_date,
            'ACTIVE'                              AS enrollment_status_normalized,
            CAST('{snapshot_date.isoformat()}' AS DATE) AS cms_pecos_snapshot_date
          FROM raw_ppef p
          LEFT JOIN raw_revalidation r
            ON p."ENRLMT_ID" = r."Enrollment ID"
          WHERE starts_with(upper(p."PROVIDER_TYPE_DESC"), 'DME SUPPLIER')
          {limit_clause}
        ) TO '{parquet_path}'
        (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000);
    """)

    rates_row = con.execute(f"""
        SELECT
          count(*) AS total,
          count(*) FILTER (WHERE supplier_npi_normalized IS NULL) AS id_null,
          count(*) FILTER (WHERE supplier_name_normalized IS NULL) AS name_null,
          count(*) FILTER (WHERE supplier_state_normalized IS NULL) AS state_null
        FROM read_parquet('{parquet_path}');
    """).fetchone()
    total = int(rates_row[0]) if rates_row else 0
    rates = (
        {
            "primary_id_null_pct": round(100.0 * int(rates_row[1]) / total, 4),
            "primary_name_null_pct": round(100.0 * int(rates_row[2]) / total, 4),
            "state_null_pct": round(100.0 * int(rates_row[3]) / total, 4),
        }
        if total > 0
        else {"primary_id_null_pct": 0.0, "primary_name_null_pct": 0.0,
              "state_null_pct": 0.0}
    )
    con.close()
    return rows_in, total, rates


def transform_reassignment(
    *,
    reassignment_csv: Path, parquet_path: Path, snapshot_date: date,
    max_rows: int | None,
) -> tuple[int, int, dict[str, float]]:
    """Revalidation Clinic Group Practice Reassignment → Parquet."""
    con = duckdb.connect(":memory:")
    con.execute("PRAGMA threads=4;")
    con.execute("PRAGMA memory_limit='6GB';")
    _register_normalizers(con)

    con.execute(f"""
        CREATE VIEW raw_reassignment AS
        SELECT * FROM read_csv(
          '{reassignment_csv}',
          delim=',', header=TRUE,
          all_varchar=TRUE,
          ignore_errors=TRUE, null_padding=TRUE,
          quote='"'
        );
    """)

    rows_in_row = con.execute(
        "SELECT count(*) FROM raw_reassignment;"
    ).fetchone()
    rows_in = int(rows_in_row[0]) if rows_in_row else 0

    limit_clause = f"LIMIT {max_rows}" if max_rows is not None else ""

    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"""
        COPY (
          SELECT
            "Group PAC ID"                              AS group_pac_id,
            "Group Enrollment ID"                       AS group_enrollment_id,
            "Group Legal Business Name"                 AS group_legal_business_name,
            "Group State Code"                          AS group_state_raw,
            "Group Due Date"                            AS group_revalidation_due_date_raw,
            CAST(TRY_STRPTIME(NULLIF("Group Due Date", ''),
                              '%m/%d/%Y') AS DATE)     AS group_revalidation_due_date,
            "Group Reassignments and Physician Assistants"
                                                        AS group_reassignments_count_raw,
            TRY_CAST(NULLIF("Group Reassignments and Physician Assistants", '')
                     AS BIGINT)
                                                        AS group_reassignments_count,
            "Record Type"                               AS record_type,
            "Individual Enrollment ID"                  AS individual_enrollment_id,
            "Individual NPI"                            AS individual_npi,
            "Individual First Name"                     AS individual_first_name,
            "Individual Last Name"                      AS individual_last_name,
            "Individual State Code"                     AS individual_state_raw,
            "Individual Specialty Description"          AS individual_specialty,
            "Individual Due Date"                       AS individual_revalidation_due_date_raw,
            CAST(TRY_STRPTIME(NULLIF("Individual Due Date", ''),
                              '%m/%d/%Y') AS DATE)     AS individual_revalidation_due_date,
            "Individual Total Employer Associations"   AS individual_total_employer_associations_raw,
            TRY_CAST(NULLIF("Individual Total Employer Associations", '')
                     AS BIGINT)
                                                        AS individual_total_employer_associations,
            -- Identity-spine normalized columns.
            pecos_normalize_npi("Individual NPI")
                                                        AS practitioner_npi_normalized,
            pecos_normalize_provider_name("Individual First Name")
                                                        AS practitioner_first_normalized,
            pecos_normalize_provider_name("Individual Last Name")
                                                        AS practitioner_last_normalized,
            pecos_normalize_state("Individual State Code")
                                                        AS individual_state_normalized,
            pecos_normalize_org_name("Group Legal Business Name")
                                                        AS group_legal_business_name_normalized,
            pecos_normalize_state("Group State Code")
                                                        AS group_state_normalized,
            pecos_normalize_specialty("Individual Specialty Description")
                                                        AS individual_specialty_normalized,
            CAST('{snapshot_date.isoformat()}' AS DATE) AS cms_pecos_snapshot_date
          FROM raw_reassignment
          {limit_clause}
        ) TO '{parquet_path}'
        (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000);
    """)

    rates_row = con.execute(f"""
        SELECT
          count(*) AS total,
          count(*) FILTER (WHERE practitioner_npi_normalized IS NULL) AS id_null,
          count(*) FILTER (WHERE group_legal_business_name_normalized IS NULL)
                                                                      AS name_null,
          count(*) FILTER (WHERE individual_state_normalized IS NULL) AS state_null
        FROM read_parquet('{parquet_path}');
    """).fetchone()
    total = int(rates_row[0]) if rates_row else 0
    rates = (
        {
            "primary_id_null_pct": round(100.0 * int(rates_row[1]) / total, 4),
            "primary_name_null_pct": round(100.0 * int(rates_row[2]) / total, 4),
            "state_null_pct": round(100.0 * int(rates_row[3]) / total, 4),
        }
        if total > 0
        else {"primary_id_null_pct": 0.0, "primary_name_null_pct": 0.0,
              "state_null_pct": 0.0}
    )
    con.close()
    return rows_in, total, rates


def transform_order_refer(
    *,
    order_refer_csv: Path, parquet_path: Path, snapshot_date: date,
    max_rows: int | None,
) -> tuple[int, int, dict[str, float]]:
    """OrderReferring.csv → Parquet."""
    con = duckdb.connect(":memory:")
    con.execute("PRAGMA threads=4;")
    con.execute("PRAGMA memory_limit='6GB';")
    _register_normalizers(con)

    con.execute(f"""
        CREATE VIEW raw_or AS
        SELECT * FROM read_csv(
          '{order_refer_csv}',
          delim=',', header=TRUE,
          all_varchar=TRUE,
          ignore_errors=TRUE, null_padding=TRUE
        );
    """)
    rows_in_row = con.execute("SELECT count(*) FROM raw_or;").fetchone()
    rows_in = int(rows_in_row[0]) if rows_in_row else 0

    limit_clause = f"LIMIT {max_rows}" if max_rows is not None else ""

    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"""
        COPY (
          SELECT
            "NPI"                          AS npi,
            "LAST_NAME"                    AS last_name,
            "FIRST_NAME"                   AS first_name,
            "PARTB"                        AS eligible_part_b_raw,
            "DME"                          AS eligible_dme_raw,
            "HHA"                          AS eligible_hha_raw,
            "PMD"                          AS eligible_pmd_raw,
            "HOSPICE"                      AS eligible_hospice_raw,
            pecos_yn_to_bool("PARTB")      AS eligible_part_b,
            pecos_yn_to_bool("DME")        AS eligible_dme,
            pecos_yn_to_bool("HHA")        AS eligible_hha,
            pecos_yn_to_bool("PMD")        AS eligible_pmd,
            pecos_yn_to_bool("HOSPICE")    AS eligible_hospice,
            pecos_normalize_npi("NPI")     AS provider_npi_normalized,
            pecos_normalize_provider_name("FIRST_NAME")
                                            AS provider_first_normalized,
            pecos_normalize_provider_name("LAST_NAME")
                                            AS provider_last_normalized,
            CAST('{snapshot_date.isoformat()}' AS DATE) AS cms_pecos_snapshot_date
          FROM raw_or
          {limit_clause}
        ) TO '{parquet_path}'
        (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000);
    """)

    rates_row = con.execute(f"""
        SELECT
          count(*) AS total,
          count(*) FILTER (WHERE provider_npi_normalized IS NULL) AS id_null,
          count(*) FILTER (WHERE provider_first_normalized IS NULL
                           AND provider_last_normalized IS NULL) AS name_null
        FROM read_parquet('{parquet_path}');
    """).fetchone()
    total = int(rates_row[0]) if rates_row else 0
    rates = (
        {
            "primary_id_null_pct": round(100.0 * int(rates_row[1]) / total, 4),
            "primary_name_null_pct": round(100.0 * int(rates_row[2]) / total, 4),
            "state_null_pct": None,  # not present in source.
        }
        if total > 0
        else {"primary_id_null_pct": 0.0, "primary_name_null_pct": 0.0,
              "state_null_pct": None}
    )
    con.close()
    return rows_in, total, rates


# --------------------------------------------------------------------------- #
# R2 + audit helpers
# --------------------------------------------------------------------------- #


def upload_to_r2(parquet_path: Path, *, bucket: str, key: str) -> int:
    s3 = _r2_client()
    file_bytes = parquet_path.stat().st_size
    s3.upload_file(
        str(parquet_path), bucket, key,
        ExtraArgs={"ContentType": "application/x-parquet"},
    )
    return file_bytes


def insert_run_row(
    conn: psycopg.Connection,
    *,
    snapshot_date: date, stream: str,
    source_url: str | None,
    source_last_modified: datetime | None,
    prior_source_last_modified: datetime | None,
) -> str:
    sql = """
    INSERT INTO ops.cms_pecos_r2_ingest_runs (
        snapshot_date, stream, status,
        source_url, source_last_modified, prior_source_last_modified
    ) VALUES (%s, %s, 'running', %s, %s, %s)
    RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            snapshot_date, stream,
            source_url, source_last_modified, prior_source_last_modified,
        ))
        row_id = cur.fetchone()[0]
    conn.commit()
    return str(row_id)


def get_prior_source_last_modified(
    conn: psycopg.Connection, snapshot_date: date, stream: str,
) -> datetime | None:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT source_last_modified
              FROM ops.cms_pecos_r2_ingest_runs
             WHERE stream = %s AND status = 'completed'
             ORDER BY started_at DESC LIMIT 1
        """, (stream,))
        row = cur.fetchone()
    return row[0] if row else None


def write_no_change_run(
    conn: psycopg.Connection,
    *,
    snapshot_date: date, stream: str,
    source_url: str | None,
    source_last_modified: datetime | None,
    prior_source_last_modified: datetime | None,
) -> None:
    started = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ops.cms_pecos_r2_ingest_runs (
                snapshot_date, stream, status,
                source_url, source_last_modified, prior_source_last_modified,
                started_at, finished_at, duration_seconds, notes
            ) VALUES (%s, %s, 'no_change', %s, %s, %s, %s, %s, 0, %s);
        """, (
            snapshot_date, stream, source_url,
            source_last_modified, prior_source_last_modified,
            started, started,
            Jsonb({"reason": "source_last_modified unchanged"}),
        ))
    conn.commit()


def finalize_run_row(
    conn: psycopg.Connection, run_id: str,
    *,
    status: str,
    started_wall: float,
    source_bytes: int | None,
    source_rows: int | None,
    parquet_rows: int | None,
    parquet_bytes: int | None,
    parquet_columns: int | None,
    r2_bucket: str | None,
    r2_prefix: str | None,
    r2_object_key: str | None,
    r2_total_bytes: int | None,
    null_rates: dict[str, float | None] | None,
    error_message: str | None,
    notes: dict[str, Any] | None,
) -> None:
    duration = round(time.monotonic() - started_wall, 3)
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE ops.cms_pecos_r2_ingest_runs
               SET status = %s,
                   source_bytes_downloaded = %s,
                   source_row_count = %s,
                   parquet_row_count = %s,
                   parquet_bytes_written = %s,
                   parquet_column_count = %s,
                   r2_bucket = %s,
                   r2_prefix = %s,
                   r2_object_key = %s,
                   r2_total_bytes = %s,
                   primary_id_null_pct = %s,
                   primary_name_null_pct = %s,
                   state_null_pct = %s,
                   finished_at = now(),
                   duration_seconds = %s,
                   error_message = %s,
                   notes = %s
             WHERE id = %s;
        """, (
            status, source_bytes, source_rows,
            parquet_rows, parquet_bytes, parquet_columns,
            r2_bucket, r2_prefix, r2_object_key, r2_total_bytes,
            (null_rates or {}).get("primary_id_null_pct"),
            (null_rates or {}).get("primary_name_null_pct"),
            (null_rates or {}).get("state_null_pct"),
            duration, error_message,
            Jsonb(notes) if notes else None, run_id,
        ))
    conn.commit()


# --------------------------------------------------------------------------- #
# Per-stream orchestration
# --------------------------------------------------------------------------- #


@dataclass
class DiscoveredSources:
    """URLs + Last-Modified for the three CMS source files (PPEF,
    Reassignment, OrderReferring) plus the companion Revalidation Due
    Date List. Discovered once at process start; the practitioners /
    organizations / dmepos_suppliers streams all share the same PPEF."""
    ppef_url: str
    ppef_last_modified: datetime | None
    reassignment_url: str
    reassignment_last_modified: datetime | None
    order_refer_url: str
    order_refer_last_modified: datetime | None
    revalidation_url: str | None
    revalidation_last_modified: datetime | None


def discover_sources(client: httpx.Client) -> DiscoveredSources:
    log.info("discovering CMS dataset URLs from %s", CMS_DATA_JSON_URL)
    catalog = fetch_data_json(client)

    def _disc(title: str, required: bool = True) -> tuple[str | None, datetime | None]:
        url = discover_stream_url(catalog, title)
        if url is None:
            if required:
                raise RuntimeError(
                    f"could not find dataset {title!r} in CMS catalog"
                )
            return None, None
        _, lm, _ = head_url(client, url)
        log.info("  %s → %s (last_modified=%s)", title, url, lm)
        return url, lm

    ppef_url, ppef_lm = _disc(
        "Medicare Fee-For-Service  Public Provider Enrollment"
    )
    reass_url, reass_lm = _disc("Revalidation Clinic Group Practice Reassignment")
    or_url, or_lm = _disc("Order and Referring")
    rev_url, rev_lm = _disc(REVALIDATION_BASE_TITLE, required=False)

    return DiscoveredSources(
        ppef_url=ppef_url, ppef_last_modified=ppef_lm,
        reassignment_url=reass_url,
        reassignment_last_modified=reass_lm,
        order_refer_url=or_url,
        order_refer_last_modified=or_lm,
        revalidation_url=rev_url,
        revalidation_last_modified=rev_lm,
    )


@dataclass(frozen=True)
class StreamCtx:
    """Per-stream runtime context."""
    spec: StreamSpec
    source_url: str
    source_last_modified: datetime | None


def stream_ctx_for(spec: StreamSpec, sources: DiscoveredSources) -> StreamCtx:
    if spec.name in ("practitioners", "organizations", "dmepos_suppliers"):
        return StreamCtx(spec, sources.ppef_url, sources.ppef_last_modified)
    if spec.name == "reassignment_of_benefits":
        return StreamCtx(spec, sources.reassignment_url,
                         sources.reassignment_last_modified)
    if spec.name == "order_refer":
        return StreamCtx(spec, sources.order_refer_url,
                         sources.order_refer_last_modified)
    raise ValueError(f"unknown stream {spec.name!r}")


def run_one_stream(
    spec: StreamSpec,
    *,
    client: httpx.Client,
    sources: DiscoveredSources,
    snapshot_date: date,
    workdir: Path,
    max_rows: int | None,
    r2_prefix_override: str | None,
    skip_if_unchanged: bool,
    dry_run: bool,
    cached_files: dict[str, Path],
) -> int:
    started_wall = time.monotonic()
    log.info("=" * 70)
    log.info("=== INGEST: stream=%s snapshot=%s ===",
             spec.name, snapshot_date.isoformat())
    log.info("=" * 70)

    ctx = stream_ctx_for(spec, sources)
    log.info("[%s] source_url=%s last_modified=%s",
             spec.name, ctx.source_url, ctx.source_last_modified)

    if dry_run:
        log.info("[%s] DRY RUN — exiting after URL discovery", spec.name)
        return 0

    # Streams with no source URL can't proceed.
    if not ctx.source_url:
        log.error("[%s] source_url unresolved", spec.name)
        return 1

    parquet_path = workdir / f"cms_pecos_{spec.name}.parquet"

    with psycopg.connect(_database_url()) as conn:
        prior = get_prior_source_last_modified(conn, snapshot_date, spec.name)
        log.info("[%s] prior source_last_modified: %s", spec.name, prior)
        if (
            skip_if_unchanged
            and prior is not None
            and ctx.source_last_modified is not None
            and ctx.source_last_modified <= prior
        ):
            log.info("[%s] source unchanged — recording no_change", spec.name)
            write_no_change_run(
                conn,
                snapshot_date=snapshot_date, stream=spec.name,
                source_url=ctx.source_url,
                source_last_modified=ctx.source_last_modified,
                prior_source_last_modified=prior,
            )
            return 0

        run_id = insert_run_row(
            conn,
            snapshot_date=snapshot_date, stream=spec.name,
            source_url=ctx.source_url,
            source_last_modified=ctx.source_last_modified,
            prior_source_last_modified=prior,
        )
        log.info("[%s] run id=%s", spec.name, run_id)

        try:
            # Cached download per upstream file (PPEF is shared across
            # 3 streams).
            csv_path = cached_files.get(ctx.source_url)
            if csv_path is None:
                csv_path = workdir / (
                    re.sub(r"[^a-zA-Z0-9_.-]+", "_",
                           ctx.source_url.rsplit("/", 1)[-1])
                )
                src_bytes = download_csv(client, ctx.source_url, csv_path)
                cached_files[ctx.source_url] = csv_path
                log.info("[%s] downloaded %d bytes → %s",
                         spec.name, src_bytes, csv_path)
            else:
                src_bytes = csv_path.stat().st_size
                log.info("[%s] reusing cached download %s (%d bytes)",
                         spec.name, csv_path, src_bytes)

            # Companion file: Revalidation Due Date List, joined onto
            # PPEF-derived streams.
            revalidation_csv: Path | None = None
            if sources.revalidation_url and spec.name in (
                "practitioners", "organizations", "dmepos_suppliers",
            ):
                revalidation_csv = cached_files.get(sources.revalidation_url)
                if revalidation_csv is None:
                    revalidation_csv = workdir / (
                        re.sub(r"[^a-zA-Z0-9_.-]+", "_",
                               sources.revalidation_url.rsplit("/", 1)[-1])
                    )
                    download_csv(client, sources.revalidation_url,
                                 revalidation_csv)
                    cached_files[sources.revalidation_url] = revalidation_csv
                    log.info("[%s] downloaded revalidation file → %s",
                             spec.name, revalidation_csv)

            # Per-stream transform.
            if spec.name == "practitioners":
                rows_in, rows_pq, rates = transform_practitioners(
                    ppef_csv=csv_path, revalidation_csv=revalidation_csv,
                    parquet_path=parquet_path, snapshot_date=snapshot_date,
                    max_rows=max_rows,
                )
            elif spec.name == "organizations":
                rows_in, rows_pq, rates = transform_organizations(
                    ppef_csv=csv_path, revalidation_csv=revalidation_csv,
                    parquet_path=parquet_path, snapshot_date=snapshot_date,
                    max_rows=max_rows,
                )
            elif spec.name == "dmepos_suppliers":
                rows_in, rows_pq, rates = transform_dmepos_suppliers(
                    ppef_csv=csv_path, revalidation_csv=revalidation_csv,
                    parquet_path=parquet_path, snapshot_date=snapshot_date,
                    max_rows=max_rows,
                )
            elif spec.name == "reassignment_of_benefits":
                rows_in, rows_pq, rates = transform_reassignment(
                    reassignment_csv=csv_path, parquet_path=parquet_path,
                    snapshot_date=snapshot_date, max_rows=max_rows,
                )
            elif spec.name == "order_refer":
                rows_in, rows_pq, rates = transform_order_refer(
                    order_refer_csv=csv_path, parquet_path=parquet_path,
                    snapshot_date=snapshot_date, max_rows=max_rows,
                )
            else:
                raise ValueError(f"unsupported stream {spec.name!r}")

            log.info(
                "[%s] transform done: source_rows=%s parquet_rows=%s "
                "null id=%.2f%% name=%.2f%% state=%s",
                spec.name, f"{rows_in:,}", f"{rows_pq:,}",
                rates.get("primary_id_null_pct") or 0.0,
                rates.get("primary_name_null_pct") or 0.0,
                f"{rates.get('state_null_pct'):.2f}%"
                  if rates.get("state_null_pct") is not None else "n/a",
            )

            # Compute parquet column count.
            con = duckdb.connect(":memory:")
            cols_row = con.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{parquet_path}');"
            ).fetchall()
            con.close()
            parquet_columns = len(cols_row)

            target_prefix = (
                r2_prefix_override
                or f"cms-pecos/{spec.name}/snapshot={snapshot_date.isoformat()}"
            )
            target_key = target_prefix.rstrip("/") + "/data.parquet"
            uploaded = upload_to_r2(
                parquet_path, bucket=R2_BUCKET, key=target_key,
            )
            log.info(
                "[%s] uploaded → s3://%s/%s (%.1f MB)",
                spec.name, R2_BUCKET, target_key, uploaded / (1 << 20),
            )

            finalize_run_row(
                conn, run_id, status="completed",
                started_wall=started_wall,
                source_bytes=src_bytes,
                source_rows=rows_in,
                parquet_rows=rows_pq,
                parquet_bytes=uploaded,
                parquet_columns=parquet_columns,
                r2_bucket=R2_BUCKET,
                r2_prefix=target_prefix.rstrip("/") + "/",
                r2_object_key=target_key,
                r2_total_bytes=uploaded,
                null_rates=rates,
                error_message=None,
                notes={
                    "max_rows": max_rows,
                    "r2_prefix_override": r2_prefix_override,
                    "source_url": ctx.source_url,
                    "revalidation_url": sources.revalidation_url,
                },
            )
            log.info(
                "[%s] DONE rows=%s upload=%.1f MB wall=%.1fs",
                spec.name, f"{rows_pq:,}",
                uploaded / (1 << 20),
                time.monotonic() - started_wall,
            )

            try:
                parquet_path.unlink(missing_ok=True)
            except Exception:
                pass

            return 0

        except Exception as exc:
            log.exception("[%s] ingest failed", spec.name)
            try:
                finalize_run_row(
                    conn, run_id, status="failed",
                    started_wall=started_wall,
                    source_bytes=None, source_rows=None,
                    parquet_rows=None, parquet_bytes=None,
                    parquet_columns=None,
                    r2_bucket=None, r2_prefix=None, r2_object_key=None,
                    r2_total_bytes=None,
                    null_rates=None,
                    error_message=str(exc), notes=None,
                )
            except Exception:
                log.exception("[%s] failed to finalize audit row on error",
                              spec.name)
            return 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--streams", default=None,
        help="Comma-separated stream names. Default: all 5. "
             "Choices: practitioners,organizations,dmepos_suppliers,"
             "reassignment_of_benefits,order_refer.",
    )
    p.add_argument("--all", action="store_true",
                   help="Run all 5 streams (default behavior; flag is "
                        "redundant but supported for parity with FEC CLI).")
    p.add_argument("--snapshot-date", default=None,
                   help="ISO date YYYY-MM-DD for the snapshot partition. "
                        "Default: today UTC.")
    p.add_argument("--workdir", default=None,
                   help="Staging directory. Default /tmp/cms_pecos_r2_ingest.")
    p.add_argument("--r2-prefix-override", default=None,
                   help="Replace canonical cms-pecos/{stream}/snapshot=*/ "
                        "prefix (smoke testing).")
    p.add_argument("--max-rows", type=int, default=None,
                   help="Smoke testing: cap rows in parquet output.")
    p.add_argument("--skip-if-unchanged", action="store_true",
                   help="Short-circuit if upstream Last-Modified matches "
                        "prior completed run for the same stream.")
    p.add_argument("--dry-run", action="store_true",
                   help="HEAD/probe only; no DB or R2 writes.")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if args.snapshot_date:
        snapshot = datetime.strptime(args.snapshot_date, "%Y-%m-%d").date()
    else:
        snapshot = datetime.now(timezone.utc).date()

    workdir = Path(args.workdir or "/tmp/cms_pecos_r2_ingest")
    workdir.mkdir(parents=True, exist_ok=True)

    # Resolve stream selection.
    if args.streams:
        wanted_names = [s.strip() for s in args.streams.split(",") if s.strip()]
        specs = [s for s in STREAM_SPECS if s.name in wanted_names]
        unknown = set(wanted_names) - {s.name for s in STREAM_SPECS}
        if unknown:
            log.error("unknown stream(s): %s", sorted(unknown))
            return 2
    else:
        specs = list(STREAM_SPECS)

    log.info("streams to run: %s", [s.name for s in specs])

    with httpx.Client(headers={"User-Agent": USER_AGENT}) as client:
        sources = discover_sources(client)
        rc = 0
        cached_files: dict[str, Path] = {}
        for spec in specs:
            rc_one = run_one_stream(
                spec,
                client=client, sources=sources,
                snapshot_date=snapshot, workdir=workdir,
                max_rows=args.max_rows,
                r2_prefix_override=args.r2_prefix_override,
                skip_if_unchanged=args.skip_if_unchanged,
                dry_run=args.dry_run,
                cached_files=cached_files,
            )
            if rc_one != 0:
                rc = rc_one
                log.error("[%s] failed; continuing with remaining streams",
                          spec.name)

    # Cleanup cached downloads.
    try:
        shutil.rmtree(workdir, ignore_errors=True)
    except Exception:
        pass

    return rc


if __name__ == "__main__":
    sys.exit(main())

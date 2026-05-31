#!/usr/bin/env python3
"""EPA NPDES Construction General Permit (CGP) → R2 Fuel Tank ingest.

Mirrors the construction-site subset of the EPA ICIS-NPDES bulk
download into Cloudflare R2 as ZSTD-compressed Parquet, snapshot-
partitioned by ingest date. ~990K-1.6M records spanning operator-side
identity + permit-component metadata + SIC/NAICS classifications.

Source: https://echo.epa.gov/files/echodownloads/npdes_downloads.zip
  one ~300MB ZIP containing 10 CSVs; we use 5 of them.

CGP filter: rows are retained when EXTERNAL_PERMIT_NMBR is in the SWC
subset of NPDES_PERM_COMPONENTS (COMPONENT_TYPE_CODE='SWC' = "Storm
Water Construction"). The script does a single pre-pass to build the
SWC permit set in DuckDB, then joins every stream against it.

Four streams written per snapshot:

  R2 layout:
    epa-npdes-cgp/
      cgp_permits/snapshot=YYYY-MM-DD/data.parquet
      cgp_perm_components_swc/snapshot=YYYY-MM-DD/data.parquet
      cgp_sic_codes/snapshot=YYYY-MM-DD/data.parquet
      cgp_naics_codes/snapshot=YYYY-MM-DD/data.parquet

`cgp_permits` is a JOIN of ICIS_PERMITS + ICIS_FACILITIES on permit
number, kept at MAX(VERSION_NMBR) per permit so downstream MVs see
the current registry state without per-version deduping. The other
3 streams are 1:1 mirrors of their EPA bulk CSVs, filtered to SWC.

The directive specified a `disturbed_acres` numeric column on
`cgp_permits` — that field is NOT in the public ECHO ICIS-NPDES bulk
(it lives in the eNOI submission system, which has no bulk
publication). The Parquet ships TOTAL_DESIGN_FLOW_NMBR /
ACTUAL_AVERAGE_FLOW_NMBR (the numeric columns the bulk DOES expose)
and omits disturbed_acres. Future re-introduction would require a
separate eNOI scrape directive.

Audit ledger: ops.epa_npdes_cgp_r2_ingest_runs.

Identity-spine join keys at output:
  cgp_permits:
    permit_number_normalized + site_address_zip5 + site_state_normalized
    + naics_code_normalized (primary NAICS) + operator_name_normalized
  cgp_perm_components_swc:
    permit_number_normalized
  cgp_sic_codes / cgp_naics_codes:
    permit_number_normalized + sic_code_normalized / naics_code_normalized

Idempotency: bundle HEAD Last-Modified per stream. If the upstream
artifact's Last-Modified matches a prior `completed` run for the same
(snapshot_date, stream), the script writes a `no_change` audit row and
skips that stream's transform. The bundle download itself is shared
across streams within one process invocation.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_epa_npdes_cgp_r2_ingest.py --all
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_epa_npdes_cgp_r2_ingest.py \\
      --streams cgp_permits --max-rows 50000 \\
      --r2-prefix-override 'epa-npdes-cgp/_smoke/cgp_permits_50k'

See directive ~/Desktop/hq/directives/2026-05-08-epa-npdes-cgp-r2-ingest.md.
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
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import boto3
import duckdb
import httpx
import psycopg
from psycopg.types.json import Jsonb


R2_BUCKET = "dex-raw-landing-zone"
USER_AGENT = "data-engine-x/epa-npdes-cgp-r2-ingest"
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 5

BUNDLE_URL = "https://echo.epa.gov/files/echodownloads/npdes_downloads.zip"


@dataclass(frozen=True)
class StreamSpec:
    name: str
    csv_member: str          # primary CSV inside the bundle ZIP
    extra_csv_member: str    # secondary CSV joined in (only cgp_permits uses)


STREAM_SPECS: tuple[StreamSpec, ...] = (
    StreamSpec(
        name="cgp_permits",
        csv_member="ICIS_PERMITS.csv",
        extra_csv_member="ICIS_FACILITIES.csv",
    ),
    StreamSpec(
        name="cgp_perm_components_swc",
        csv_member="NPDES_PERM_COMPONENTS.csv",
        extra_csv_member="",
    ),
    StreamSpec(
        name="cgp_sic_codes",
        csv_member="NPDES_SICS.csv",
        extra_csv_member="",
    ),
    StreamSpec(
        name="cgp_naics_codes",
        csv_member="NPDES_NAICS.csv",
        extra_csv_member="",
    ),
)


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("epa-npdes-cgp-r2-ingest")


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
# HTTP layer
# --------------------------------------------------------------------------- #


def head_url(
    client: httpx.Client, url: str,
) -> tuple[int | None, datetime | None]:
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = client.head(url, follow_redirects=True, timeout=30.0)
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
            return cl, lm
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


def extract_zip(zip_path: Path, dest_dir: Path, members: list[str]) -> None:
    """Extract specific members of a ZIP archive to a directory."""
    with zipfile.ZipFile(zip_path) as z:
        names = set(z.namelist())
        for member in members:
            if member not in names:
                raise RuntimeError(
                    f"member {member!r} missing from {zip_path}; "
                    f"contents include: {sorted(names)[:15]}"
                )
            z.extract(member, dest_dir)


# --------------------------------------------------------------------------- #
# DuckDB normalize macros (parity with _lib/epa_npdes_cgp_normalize.py)
# --------------------------------------------------------------------------- #


_NORMALIZE_MACROS_SQL = r"""
-- Operator-name normalizer: lowercase, strip [.,&], collapse whitespace,
-- iteratively strip ONE trailing org suffix per pass (unrolled 3x —
-- DuckDB macros don't recurse).
CREATE MACRO _epa_strip_one_org_suffix(s) AS (
  CASE
    WHEN s IS NULL OR s = '' THEN NULL
    ELSE (
      WITH _stripp AS (SELECT string_split(s, ' ') AS toks)
      SELECT CASE
        WHEN length(toks) >= 2 AND toks[length(toks)] IN
             ('llc','inc','incorporated','corp','corporation',
              'co','company','ltd','limited','lp','llp',
              'pa','pc','pllc','group','associates',
              'association','partners')
        THEN array_to_string(toks[1:length(toks)-1], ' ')
        ELSE s
      END FROM _stripp
    )
  END
);

CREATE MACRO epa_normalize_operator_name(raw) AS (
  NULLIF(
    _epa_strip_one_org_suffix(
      _epa_strip_one_org_suffix(
        _epa_strip_one_org_suffix(
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

-- Permit number normalizer: uppercase + strip non-alphanumeric.
CREATE MACRO epa_normalize_permit_number(raw) AS (
  NULLIF(
    regexp_replace(upper(coalesce(CAST(raw AS VARCHAR), '')), '[^A-Z0-9]', '', 'g'),
    ''
  )
);

-- Permit-status classifier: ICIS code → canonical bucket.
CREATE MACRO epa_classify_permit_status(raw) AS (
  CASE upper(trim(coalesce(CAST(raw AS VARCHAR), '')))
    WHEN 'EFF' THEN 'EFFECTIVE'
    WHEN 'EFFECTIVE' THEN 'EFFECTIVE'
    WHEN 'ADC' THEN 'EFFECTIVE'
    WHEN 'TRM' THEN 'TERMINATED'
    WHEN 'TERMINATED' THEN 'TERMINATED'
    WHEN 'EXP' THEN 'EXPIRED'
    WHEN 'EXPIRED' THEN 'EXPIRED'
    WHEN 'RET' THEN 'EXPIRED'
    WHEN 'PND' THEN 'PENDING'
    WHEN 'PENDING' THEN 'PENDING'
    WHEN 'NON' THEN 'PENDING'
    ELSE NULL
  END
);

-- ZIP5 extractor.
CREATE MACRO epa_zip5(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(CAST(raw AS VARCHAR)) = '' THEN NULL
    WHEN length(regexp_replace(CAST(raw AS VARCHAR), '\D', '', 'g')) < 5 THEN NULL
    ELSE substr(regexp_replace(CAST(raw AS VARCHAR), '\D', '', 'g'), 1, 5)
  END
);

-- 2-letter state normalizer.
CREATE MACRO epa_normalize_state(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(CAST(raw AS VARCHAR)) = '' THEN NULL
    WHEN length(trim(CAST(raw AS VARCHAR))) != 2 THEN NULL
    WHEN regexp_matches(trim(upper(CAST(raw AS VARCHAR))), '^[A-Z]{2}$')
      THEN trim(upper(CAST(raw AS VARCHAR)))
    ELSE NULL
  END
);

-- NAICS 6-digit normalizer with left-pad to 6.
CREATE MACRO epa_normalize_naics(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(CAST(raw AS VARCHAR)) = '' THEN NULL
    WHEN length(regexp_replace(CAST(raw AS VARCHAR), '\D', '', 'g')) = 0 THEN NULL
    WHEN length(regexp_replace(CAST(raw AS VARCHAR), '\D', '', 'g')) > 6 THEN NULL
    ELSE lpad(regexp_replace(CAST(raw AS VARCHAR), '\D', '', 'g'), 6, '0')
  END
);
"""


def _register_normalizers(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(_NORMALIZE_MACROS_SQL)


# --------------------------------------------------------------------------- #
# SWC permit set (CGP filter basis)
# --------------------------------------------------------------------------- #


def build_swc_permit_set(
    *, components_csv: Path, swc_set_parquet: Path,
) -> int:
    """One-shot pre-pass: filter NPDES_PERM_COMPONENTS to SWC component
    type and emit a single-column Parquet of unique permit numbers.

    Other streams join against this set to enforce the CGP filter.
    """
    con = duckdb.connect(":memory:")
    con.execute("PRAGMA threads=4;")
    con.execute("PRAGMA memory_limit='6GB';")
    swc_set_parquet.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"""
        COPY (
          SELECT DISTINCT "EXTERNAL_PERMIT_NMBR" AS external_permit_nmbr
            FROM read_csv(
              '{components_csv}',
              delim=',', header=TRUE,
              all_varchar=TRUE,
              ignore_errors=TRUE, null_padding=TRUE
            )
           WHERE upper(trim(coalesce("COMPONENT_TYPE_CODE", ''))) = 'SWC'
             AND coalesce(trim("EXTERNAL_PERMIT_NMBR"), '') <> ''
        ) TO '{swc_set_parquet}'
        (FORMAT PARQUET, COMPRESSION ZSTD);
    """)
    cnt_row = con.execute(
        f"SELECT count(*) FROM read_parquet('{swc_set_parquet}');"
    ).fetchone()
    con.close()
    return int(cnt_row[0]) if cnt_row else 0


# --------------------------------------------------------------------------- #
# Stream transforms
# --------------------------------------------------------------------------- #


def transform_cgp_permits(
    *,
    permits_csv: Path, facilities_csv: Path, swc_set_parquet: Path,
    parquet_path: Path, snapshot_date: date,
    max_rows: int | None,
) -> tuple[int, int, dict[str, float]]:
    """ICIS_PERMITS ⨝ ICIS_FACILITIES → cgp_permits.parquet.

    QUALIFY ROW_NUMBER() OVER (PARTITION BY EXTERNAL_PERMIT_NMBR
    ORDER BY VERSION_NMBR DESC) = 1 keeps only the latest version of
    each permit so the parquet has one row per current-state permit.
    """
    con = duckdb.connect(":memory:")
    con.execute("PRAGMA threads=4;")
    con.execute("PRAGMA memory_limit='6GB';")
    _register_normalizers(con)

    con.execute(f"""
        CREATE VIEW raw_permits AS
        SELECT * FROM read_csv(
          '{permits_csv}',
          delim=',', header=TRUE,
          all_varchar=TRUE,
          ignore_errors=TRUE, null_padding=TRUE
        );
        CREATE VIEW raw_facilities AS
        SELECT * FROM read_csv(
          '{facilities_csv}',
          delim=',', header=TRUE,
          all_varchar=TRUE,
          ignore_errors=TRUE, null_padding=TRUE
        );
        CREATE VIEW swc_set AS
        SELECT external_permit_nmbr FROM read_parquet('{swc_set_parquet}');
    """)

    rows_in_row = con.execute("""
        SELECT count(*) FROM raw_permits p
        WHERE p."EXTERNAL_PERMIT_NMBR" IN
              (SELECT external_permit_nmbr FROM swc_set);
    """).fetchone()
    rows_in = int(rows_in_row[0]) if rows_in_row else 0

    limit_clause = f"LIMIT {max_rows}" if max_rows is not None else ""

    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"""
        COPY (
          WITH permit_latest AS (
            SELECT *
              FROM raw_permits
             WHERE "EXTERNAL_PERMIT_NMBR" IN
                   (SELECT external_permit_nmbr FROM swc_set)
             QUALIFY ROW_NUMBER() OVER (
                       PARTITION BY "EXTERNAL_PERMIT_NMBR"
                       ORDER BY TRY_CAST("VERSION_NMBR" AS INTEGER) DESC
                                NULLS LAST,
                                "VERSION_NMBR" DESC
                     ) = 1
          )
          SELECT
            -- Raw ICIS_PERMITS columns preserved as VARCHAR.
            p."ACTIVITY_ID"                  AS activity_id,
            p."EXTERNAL_PERMIT_NMBR"         AS external_permit_nmbr,
            p."VERSION_NMBR"                 AS version_nmbr,
            p."FACILITY_TYPE_INDICATOR"      AS facility_type_indicator,
            p."PERMIT_TYPE_CODE"             AS permit_type_code,
            p."MAJOR_MINOR_STATUS_FLAG"      AS major_minor_status_flag,
            p."PERMIT_STATUS_CODE"           AS permit_status_code,
            p."TOTAL_DESIGN_FLOW_NMBR"       AS total_design_flow_raw,
            p."ACTUAL_AVERAGE_FLOW_NMBR"     AS actual_average_flow_raw,
            p."STATE_WATER_BODY"             AS state_water_body,
            p."STATE_WATER_BODY_NAME"        AS state_water_body_name,
            p."PERMIT_NAME"                  AS permit_name,
            p."AGENCY_TYPE_CODE"             AS agency_type_code,
            p."ORIGINAL_ISSUE_DATE"          AS original_issue_date_raw,
            p."ISSUE_DATE"                   AS issue_date_raw,
            p."ISSUING_AGENCY"               AS issuing_agency,
            p."EFFECTIVE_DATE"               AS effective_date_raw,
            p."EXPIRATION_DATE"              AS expiration_date_raw,
            p."RETIREMENT_DATE"              AS retirement_date_raw,
            p."TERMINATION_DATE"             AS termination_date_raw,
            p."PERMIT_COMP_STATUS_FLAG"      AS permit_comp_status_flag,
            p."DMR_NON_RECEIPT_FLAG"         AS dmr_non_receipt_flag,
            p."RNC_TRACKING_FLAG"            AS rnc_tracking_flag,
            p."MASTER_EXTERNAL_PERMIT_NMBR"  AS master_external_permit_nmbr,
            p."TMDL_INTERFACE_FLAG"          AS tmdl_interface_flag,
            p."EDMR_AUTHORIZATION_FLAG"      AS edmr_authorization_flag,
            p."PRETREATMENT_INDICATOR_CODE"  AS pretreatment_indicator_code,
            p."RAD_WBD_HUC12S"               AS rad_wbd_huc12s,
            -- Raw ICIS_FACILITIES columns preserved as VARCHAR.
            f."ICIS_FACILITY_INTEREST_ID"    AS icis_facility_interest_id,
            f."NPDES_ID"                     AS npdes_id,
            f."FACILITY_UIN"                 AS facility_uin,
            f."FACILITY_TYPE_CODE"           AS facility_type_code,
            f."FACILITY_NAME"                AS facility_name,
            f."LOCATION_ADDRESS"             AS site_address,
            f."SUPPLEMENTAL_ADDRESS_TEXT"    AS site_supplemental_address,
            f."CITY"                         AS site_city,
            f."COUNTY_CODE"                  AS site_county_code,
            f."STATE_CODE"                   AS site_state_code,
            f."ZIP"                          AS site_zip_raw,
            f."GEOCODE_LATITUDE"             AS site_latitude_raw,
            f."GEOCODE_LONGITUDE"            AS site_longitude_raw,
            f."IMPAIRED_WATERS"              AS impaired_waters,
            -- Typed casts.
            CAST(TRY_STRPTIME(NULLIF(p."ORIGINAL_ISSUE_DATE", ''),
                              ['%m/%d/%Y', '%Y-%m-%d']) AS DATE)
                                              AS permit_original_issue_date,
            CAST(TRY_STRPTIME(NULLIF(p."ISSUE_DATE", ''),
                              ['%m/%d/%Y', '%Y-%m-%d']) AS DATE)
                                              AS permit_issue_date,
            CAST(TRY_STRPTIME(NULLIF(p."EFFECTIVE_DATE", ''),
                              ['%m/%d/%Y', '%Y-%m-%d']) AS DATE)
                                              AS permit_effective_date,
            CAST(TRY_STRPTIME(NULLIF(p."EXPIRATION_DATE", ''),
                              ['%m/%d/%Y', '%Y-%m-%d']) AS DATE)
                                              AS permit_expiration_date,
            CAST(TRY_STRPTIME(NULLIF(p."RETIREMENT_DATE", ''),
                              ['%m/%d/%Y', '%Y-%m-%d']) AS DATE)
                                              AS permit_retirement_date,
            CAST(TRY_STRPTIME(NULLIF(p."TERMINATION_DATE", ''),
                              ['%m/%d/%Y', '%Y-%m-%d']) AS DATE)
                                              AS permit_termination_date,
            TRY_CAST(p."TOTAL_DESIGN_FLOW_NMBR" AS DOUBLE)
                                              AS total_design_flow_mgd,
            TRY_CAST(p."ACTUAL_AVERAGE_FLOW_NMBR" AS DOUBLE)
                                              AS actual_average_flow_mgd,
            TRY_CAST(f."GEOCODE_LATITUDE" AS DOUBLE)
                                              AS site_latitude,
            TRY_CAST(f."GEOCODE_LONGITUDE" AS DOUBLE)
                                              AS site_longitude,
            -- Identity-spine normalized columns.
            epa_normalize_permit_number(p."EXTERNAL_PERMIT_NMBR")
                                              AS permit_number_normalized,
            epa_classify_permit_status(p."PERMIT_STATUS_CODE")
                                              AS permit_status_normalized,
            epa_normalize_operator_name(p."PERMIT_NAME")
                                              AS operator_name_normalized,
            epa_zip5(f."ZIP")                 AS site_address_zip5,
            epa_normalize_state(f."STATE_CODE")
                                              AS site_state_normalized,
            CAST('{snapshot_date.isoformat()}' AS DATE) AS epa_npdes_cgp_snapshot_date
          FROM permit_latest p
          LEFT JOIN raw_facilities f
            ON p."EXTERNAL_PERMIT_NMBR" = f."NPDES_ID"
          {limit_clause}
        ) TO '{parquet_path}'
        (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000);
    """)

    rates_row = con.execute(f"""
        SELECT
          count(*) AS total,
          count(*) FILTER (WHERE permit_number_normalized IS NULL) AS id_null,
          count(*) FILTER (WHERE operator_name_normalized IS NULL) AS name_null,
          count(*) FILTER (WHERE site_state_normalized IS NULL) AS state_null
        FROM read_parquet('{parquet_path}');
    """).fetchone()
    total = int(rates_row[0]) if rates_row else 0
    rates: dict[str, float | None] = (
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


def transform_cgp_perm_components_swc(
    *,
    components_csv: Path, parquet_path: Path,
    snapshot_date: date, max_rows: int | None,
) -> tuple[int, int, dict[str, float]]:
    """NPDES_PERM_COMPONENTS filtered to COMPONENT_TYPE_CODE='SWC'."""
    con = duckdb.connect(":memory:")
    con.execute("PRAGMA threads=4;")
    con.execute("PRAGMA memory_limit='6GB';")
    _register_normalizers(con)

    con.execute(f"""
        CREATE VIEW raw_components AS
        SELECT * FROM read_csv(
          '{components_csv}',
          delim=',', header=TRUE,
          all_varchar=TRUE,
          ignore_errors=TRUE, null_padding=TRUE
        );
    """)

    rows_in_row = con.execute("""
        SELECT count(*) FROM raw_components
         WHERE upper(trim(coalesce("COMPONENT_TYPE_CODE", ''))) = 'SWC';
    """).fetchone()
    rows_in = int(rows_in_row[0]) if rows_in_row else 0

    limit_clause = f"LIMIT {max_rows}" if max_rows is not None else ""

    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"""
        COPY (
          SELECT
            "EXTERNAL_PERMIT_NMBR"           AS external_permit_nmbr,
            "COMPONENT_TYPE_CODE"            AS component_type_code,
            "COMPONENT_TYPE_DESC"            AS component_type_desc,
            epa_normalize_permit_number("EXTERNAL_PERMIT_NMBR")
                                              AS permit_number_normalized,
            upper(trim(coalesce("COMPONENT_TYPE_DESC", '')))
                                              AS component_kind_normalized,
            CAST('{snapshot_date.isoformat()}' AS DATE) AS epa_npdes_cgp_snapshot_date
          FROM raw_components
          WHERE upper(trim(coalesce("COMPONENT_TYPE_CODE", ''))) = 'SWC'
          {limit_clause}
        ) TO '{parquet_path}'
        (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000);
    """)

    rates_row = con.execute(f"""
        SELECT
          count(*) AS total,
          count(*) FILTER (WHERE permit_number_normalized IS NULL) AS id_null
        FROM read_parquet('{parquet_path}');
    """).fetchone()
    total = int(rates_row[0]) if rates_row else 0
    rates: dict[str, float | None] = (
        {
            "primary_id_null_pct": round(100.0 * int(rates_row[1]) / total, 4),
            "primary_name_null_pct": None,
            "state_null_pct": None,
        }
        if total > 0
        else {"primary_id_null_pct": 0.0, "primary_name_null_pct": None,
              "state_null_pct": None}
    )
    con.close()
    return rows_in, total, rates


def transform_cgp_codes(
    *,
    codes_csv: Path, swc_set_parquet: Path,
    parquet_path: Path, snapshot_date: date,
    code_kind: str,            # "sic" or "naics"
    max_rows: int | None,
) -> tuple[int, int, dict[str, float]]:
    """NPDES_SICS or NPDES_NAICS filtered to SWC permits.

    NAICS rows additionally normalize the code to a 6-digit form;
    SIC codes are 4-digit so they ship raw + a permit_number_normalized
    join key only.
    """
    assert code_kind in ("sic", "naics"), code_kind
    con = duckdb.connect(":memory:")
    con.execute("PRAGMA threads=4;")
    con.execute("PRAGMA memory_limit='6GB';")
    _register_normalizers(con)

    con.execute(f"""
        CREATE VIEW raw_codes AS
        SELECT * FROM read_csv(
          '{codes_csv}',
          delim=',', header=TRUE,
          all_varchar=TRUE,
          ignore_errors=TRUE, null_padding=TRUE
        );
        CREATE VIEW swc_set AS
        SELECT external_permit_nmbr FROM read_parquet('{swc_set_parquet}');
    """)

    rows_in_row = con.execute("""
        SELECT count(*) FROM raw_codes
         WHERE "NPDES_ID" IN (SELECT external_permit_nmbr FROM swc_set);
    """).fetchone()
    rows_in = int(rows_in_row[0]) if rows_in_row else 0

    limit_clause = f"LIMIT {max_rows}" if max_rows is not None else ""

    code_col_csv = "NAICS_CODE" if code_kind == "naics" else "SIC_CODE"
    desc_col_csv = "NAICS_DESC" if code_kind == "naics" else "SIC_DESC"
    code_col_out = f"{code_kind}_code"
    desc_col_out = f"{code_kind}_desc"

    code_normalize_expr = (
        f'epa_normalize_naics("{code_col_csv}")'
        if code_kind == "naics"
        else f'NULLIF(trim(coalesce("{code_col_csv}", \'\')), \'\')'
    )

    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"""
        COPY (
          SELECT
            "NPDES_ID"                       AS external_permit_nmbr,
            "{code_col_csv}"                 AS {code_col_out},
            "{desc_col_csv}"                 AS {desc_col_out},
            "PRIMARY_INDICATOR_FLAG"         AS primary_indicator_flag,
            epa_normalize_permit_number("NPDES_ID")
                                              AS permit_number_normalized,
            {code_normalize_expr}             AS {code_kind}_code_normalized,
            CAST('{snapshot_date.isoformat()}' AS DATE) AS epa_npdes_cgp_snapshot_date
          FROM raw_codes
          WHERE "NPDES_ID" IN (SELECT external_permit_nmbr FROM swc_set)
          {limit_clause}
        ) TO '{parquet_path}'
        (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000);
    """)

    rates_row = con.execute(f"""
        SELECT
          count(*) AS total,
          count(*) FILTER (WHERE permit_number_normalized IS NULL) AS id_null
        FROM read_parquet('{parquet_path}');
    """).fetchone()
    total = int(rates_row[0]) if rates_row else 0
    rates: dict[str, float | None] = (
        {
            "primary_id_null_pct": round(100.0 * int(rates_row[1]) / total, 4),
            "primary_name_null_pct": None,
            "state_null_pct": None,
        }
        if total > 0
        else {"primary_id_null_pct": 0.0, "primary_name_null_pct": None,
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
    INSERT INTO ops.epa_npdes_cgp_r2_ingest_runs (
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
              FROM ops.epa_npdes_cgp_r2_ingest_runs
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
            INSERT INTO ops.epa_npdes_cgp_r2_ingest_runs (
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
            UPDATE ops.epa_npdes_cgp_r2_ingest_runs
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
class BundleState:
    """Lazy-downloaded bundle state. Shared across streams within
    one process invocation."""
    url: str
    last_modified: datetime | None
    bytes_downloaded: int | None
    zip_path: Path | None
    extract_dir: Path | None
    swc_set_parquet: Path | None
    swc_permit_count: int | None


def ensure_bundle_downloaded(
    state: BundleState, *, client: httpx.Client, workdir: Path,
) -> None:
    """Download + unzip + build SWC set if not already done."""
    if state.zip_path is not None and state.extract_dir is not None \
       and state.swc_set_parquet is not None:
        return

    state.zip_path = workdir / "npdes_downloads.zip"
    if not state.zip_path.exists():
        log.info("downloading bundle %s ...", state.url)
        state.bytes_downloaded = download_zip(client, state.url, state.zip_path)
        log.info("  → %s (%.1f MB)", state.zip_path,
                 state.bytes_downloaded / (1 << 20))
    else:
        state.bytes_downloaded = state.zip_path.stat().st_size
        log.info("reusing cached bundle %s (%.1f MB)",
                 state.zip_path, state.bytes_downloaded / (1 << 20))

    state.extract_dir = workdir / "extracted"
    state.extract_dir.mkdir(parents=True, exist_ok=True)
    members = sorted({s.csv_member for s in STREAM_SPECS}
                      | {s.extra_csv_member for s in STREAM_SPECS if s.extra_csv_member})
    log.info("extracting %d members → %s", len(members), state.extract_dir)
    extract_zip(state.zip_path, state.extract_dir, members)

    state.swc_set_parquet = workdir / "swc_permit_set.parquet"
    components_csv = state.extract_dir / "NPDES_PERM_COMPONENTS.csv"
    log.info("building SWC permit set from %s", components_csv.name)
    state.swc_permit_count = build_swc_permit_set(
        components_csv=components_csv,
        swc_set_parquet=state.swc_set_parquet,
    )
    log.info("  SWC permits: %s", f"{state.swc_permit_count:,}")


def run_one_stream(
    spec: StreamSpec,
    *,
    bundle_state: BundleState,
    client: httpx.Client,
    snapshot_date: date,
    workdir: Path,
    max_rows: int | None,
    r2_prefix_override: str | None,
    skip_if_unchanged: bool,
    dry_run: bool,
) -> int:
    started_wall = time.monotonic()
    log.info("=" * 70)
    log.info("=== INGEST: stream=%s snapshot=%s ===",
             spec.name, snapshot_date.isoformat())
    log.info("=" * 70)
    log.info("[%s] source_url=%s last_modified=%s",
             spec.name, bundle_state.url, bundle_state.last_modified)

    if dry_run:
        log.info("[%s] DRY RUN — exiting after URL discovery", spec.name)
        return 0

    # Smoke runs (custom R2 prefix) skip the audit ledger entirely. The
    # ledger's partial-unique index is sized to the canonical R2 slot
    # (one completed row per (snapshot_date, stream)); a smoke run that
    # writes to a non-canonical R2 prefix would steal that slot and
    # cause the next real ingest's UPDATE to violate the constraint.
    is_smoke = r2_prefix_override is not None
    parquet_path = workdir / f"epa_npdes_{spec.name}.parquet"

    with psycopg.connect(_database_url()) as conn:
        if is_smoke:
            log.info(
                "[%s] SMOKE run (r2_prefix_override set) — audit ledger skipped",
                spec.name,
            )
            run_id = None
        else:
            prior = get_prior_source_last_modified(conn, snapshot_date, spec.name)
            log.info("[%s] prior source_last_modified: %s", spec.name, prior)
            if (
                skip_if_unchanged
                and prior is not None
                and bundle_state.last_modified is not None
                and bundle_state.last_modified <= prior
            ):
                log.info("[%s] source unchanged — recording no_change",
                         spec.name)
                write_no_change_run(
                    conn,
                    snapshot_date=snapshot_date, stream=spec.name,
                    source_url=bundle_state.url,
                    source_last_modified=bundle_state.last_modified,
                    prior_source_last_modified=prior,
                )
                return 0

            run_id = insert_run_row(
                conn,
                snapshot_date=snapshot_date, stream=spec.name,
                source_url=bundle_state.url,
                source_last_modified=bundle_state.last_modified,
                prior_source_last_modified=prior,
            )
            log.info("[%s] run id=%s", spec.name, run_id)

        try:
            ensure_bundle_downloaded(
                bundle_state, client=client, workdir=workdir,
            )
            assert bundle_state.extract_dir is not None
            assert bundle_state.swc_set_parquet is not None

            primary_csv = bundle_state.extract_dir / spec.csv_member

            if spec.name == "cgp_permits":
                facilities_csv = (
                    bundle_state.extract_dir / spec.extra_csv_member
                )
                rows_in, rows_pq, rates = transform_cgp_permits(
                    permits_csv=primary_csv,
                    facilities_csv=facilities_csv,
                    swc_set_parquet=bundle_state.swc_set_parquet,
                    parquet_path=parquet_path,
                    snapshot_date=snapshot_date,
                    max_rows=max_rows,
                )
            elif spec.name == "cgp_perm_components_swc":
                rows_in, rows_pq, rates = transform_cgp_perm_components_swc(
                    components_csv=primary_csv,
                    parquet_path=parquet_path,
                    snapshot_date=snapshot_date,
                    max_rows=max_rows,
                )
            elif spec.name == "cgp_sic_codes":
                rows_in, rows_pq, rates = transform_cgp_codes(
                    codes_csv=primary_csv,
                    swc_set_parquet=bundle_state.swc_set_parquet,
                    parquet_path=parquet_path,
                    snapshot_date=snapshot_date,
                    code_kind="sic",
                    max_rows=max_rows,
                )
            elif spec.name == "cgp_naics_codes":
                rows_in, rows_pq, rates = transform_cgp_codes(
                    codes_csv=primary_csv,
                    swc_set_parquet=bundle_state.swc_set_parquet,
                    parquet_path=parquet_path,
                    snapshot_date=snapshot_date,
                    code_kind="naics",
                    max_rows=max_rows,
                )
            else:
                raise ValueError(f"unsupported stream {spec.name!r}")

            log.info(
                "[%s] transform done: source_rows=%s parquet_rows=%s "
                "null id=%.2f%% name=%s state=%s",
                spec.name, f"{rows_in:,}", f"{rows_pq:,}",
                rates.get("primary_id_null_pct") or 0.0,
                f"{rates.get('primary_name_null_pct'):.2f}%"
                  if rates.get("primary_name_null_pct") is not None
                  else "n/a",
                f"{rates.get('state_null_pct'):.2f}%"
                  if rates.get("state_null_pct") is not None else "n/a",
            )

            con = duckdb.connect(":memory:")
            cols_row = con.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{parquet_path}');"
            ).fetchall()
            con.close()
            parquet_columns = len(cols_row)

            target_prefix = (
                r2_prefix_override
                or f"epa-npdes-cgp/{spec.name}/snapshot={snapshot_date.isoformat()}"
            )
            target_key = target_prefix.rstrip("/") + "/data.parquet"
            uploaded = upload_to_r2(
                parquet_path, bucket=R2_BUCKET, key=target_key,
            )
            log.info(
                "[%s] uploaded → s3://%s/%s (%.1f MB)",
                spec.name, R2_BUCKET, target_key, uploaded / (1 << 20),
            )

            if run_id is not None:
                finalize_run_row(
                    conn, run_id, status="completed",
                    started_wall=started_wall,
                    source_bytes=bundle_state.bytes_downloaded,
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
                        "bundle_url": bundle_state.url,
                        "swc_permit_count": bundle_state.swc_permit_count,
                        "csv_member": spec.csv_member,
                        "extra_csv_member": spec.extra_csv_member or None,
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
            if run_id is not None:
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
        help="Comma-separated stream names. Default: all 4. "
             "Choices: cgp_permits,cgp_perm_components_swc,"
             "cgp_sic_codes,cgp_naics_codes.",
    )
    p.add_argument("--all", action="store_true",
                   help="Run all 4 streams (default behavior).")
    p.add_argument("--snapshot-date", default=None,
                   help="ISO date YYYY-MM-DD for the snapshot partition. "
                        "Default: today UTC.")
    p.add_argument("--workdir", default=None,
                   help="Staging directory. Default /tmp/epa_npdes_cgp_r2_ingest.")
    p.add_argument("--r2-prefix-override", default=None,
                   help="Replace canonical epa-npdes-cgp/{stream}/snapshot=*/ "
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

    workdir = Path(args.workdir or "/tmp/epa_npdes_cgp_r2_ingest")
    workdir.mkdir(parents=True, exist_ok=True)

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
        cl, lm = head_url(client, BUNDLE_URL)
        log.info("bundle %s: content-length=%s last_modified=%s",
                 BUNDLE_URL, cl, lm)
        bundle_state = BundleState(
            url=BUNDLE_URL,
            last_modified=lm,
            bytes_downloaded=None,
            zip_path=None,
            extract_dir=None,
            swc_set_parquet=None,
            swc_permit_count=None,
        )

        rc = 0
        for spec in specs:
            rc_one = run_one_stream(
                spec,
                bundle_state=bundle_state,
                client=client,
                snapshot_date=snapshot, workdir=workdir,
                max_rows=args.max_rows,
                r2_prefix_override=args.r2_prefix_override,
                skip_if_unchanged=args.skip_if_unchanged,
                dry_run=args.dry_run,
            )
            if rc_one != 0:
                rc = rc_one
                log.error("[%s] failed; continuing with remaining streams",
                          spec.name)

    try:
        shutil.rmtree(workdir, ignore_errors=True)
    except Exception:
        pass

    return rc


if __name__ == "__main__":
    sys.exit(main())

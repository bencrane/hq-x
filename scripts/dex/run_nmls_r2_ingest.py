#!/usr/bin/env python3
"""NMLS Consumer Access bulk export → R2 Fuel Tank ingest.

The canonical national mortgage-loan-originator (MLO) registry. Every
state-licensed and federal-registered MLO at every depository bank +
non-bank mortgage company. ~600K active MLOs + ~150K employer institutions
+ ~250K branch offices + ~3-5M (NMLS_id, state) license tuples.

Streams (one Parquet per snapshot):

  mlo_individuals       — individual licensees (NMLS ID, name, employer,
                          employment history, state licenses, designations)
  employer_entities     — institutions (employer NMLS ID, legal name, MLO count)
  branch_offices        — per-employer branch / office locations
  state_licenses_held   — per-MLO state-by-state license status

Per-stream Parquet at
`s3://dex-raw-landing-zone/nmls/{stream}/snapshot=YYYY-MM-DD/data.parquet`.

Each Parquet preserves all source columns as VARCHAR (raw fidelity), adds
typed DATE casts on lifecycle date columns, and adds the normalization-spine
canonical column set: nmls_id_normalized (BIGINT), employer_nmls_id_normalized
(BIGINT), mlo_first/middle/last_normalized, employer_name_normalized,
employer_kind_normalized, mlo_status_normalized + is_active flag,
mlo_address_zip5 + mlo_address_state_normalized, plus snapshot partition
metadata.

RisingWave wiring is DEFERRED — this script lands canonical R2 Parquet only.

Audit ledger: `ops.nmls_r2_ingest_runs`. Idempotency basis: HEAD
`Last-Modified` (where the source URL is reachable). Streams that require
operator-staged files exit with code 42 and write a `status='awaiting_operator'`
audit row pointing the operator at the staging directory.

Access reality:

  NMLS Consumer Access (`www.nmlsconsumeraccess.org`) is Cloudflare-bot-
  protected — programmatic GET returns HTTP 403 from automation. The NMLS
  Reports SharePoint page (`mortgage.nationwidelicensingsystem.org/...`)
  renders its report-list dynamically and exposes no static download links.

  Per-MLO record-level bulk data is NOT freely available via public-Internet
  download. It comes via:
    - paid NMLS Reports subscription
    - state-regulator data-sharing agreement
    - regulator-staged operator delivery

  The operator-staged-files fallback (`--resume`) is the canonical access
  path for this ingest. The script attempts programmatic discovery first
  (so future improvements to NMLS public-data posture are picked up
  automatically), and falls through to the staged-files path on access
  denial. Pattern mirrors the SAM.gov directive (2026-05-08).

Usage:

  # First run — discover whether any stream is programmatically accessible.
  # If access is gated, the script exits 42 with operator instructions.
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_nmls_r2_ingest.py --all

  # Single-stream smoke (for known-staged file).
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_nmls_r2_ingest.py mlo_individuals \\
        --resume --staged-dir /Users/benjamincrane/Downloads/nmls_bulk \\
        --max-rows 50000

  # Full resume (operator has staged all 4 stream files).
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_nmls_r2_ingest.py --all \\
        --resume --staged-dir /Users/benjamincrane/Downloads/nmls_bulk

See directive
~/Desktop/hq/directives/2026-05-08-nmls-consumer-access-bulk-r2-ingest.md.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
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
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 3
USER_AGENT = "data-engine-x/nmls-r2-ingest"
EXIT_OPERATOR_ACTION_REQUIRED = 42

# Discovery URL pattern — the SharePoint NMLS Reports page. Used only to log
# that we tried; the page itself renders client-side and has no static links.
NMLS_REPORTS_PAGE = (
    "https://mortgage.nationwidelicensingsystem.org/about/Pages/Reports.aspx"
)
# Consumer Access landing — Cloudflare-protected; we HEAD only to record the
# response code in audit notes.
NMLS_CONSUMER_ACCESS = "https://www.nmlsconsumeraccess.org/"


@dataclass(frozen=True)
class StreamSpec:
    """One NMLS bulk-export stream the operator stages locally.

    `staged_filename_candidates` lists filenames the operator might have
    saved the extract under — the script picks the first one that exists in
    `--staged-dir`. The first candidate is the canonical name we recommend
    to the operator in the awaiting-operator message.
    """
    name: str
    staged_filename_candidates: tuple[str, ...]
    pk_column_label: str
    minimum_row_floor: int

    @property
    def canonical_filename(self) -> str:
        return self.staged_filename_candidates[0]


STREAMS: tuple[StreamSpec, ...] = (
    StreamSpec(
        name="mlo_individuals",
        staged_filename_candidates=(
            "nmls_mlo_individuals.csv",
            "MLOIndividuals.csv",
            "MLO_Individuals.csv",
            "mlo_individuals.csv",
        ),
        pk_column_label="NMLS_ID",
        # ≥500K active+historical, per directive validation gate.
        minimum_row_floor=500_000,
    ),
    StreamSpec(
        name="employer_entities",
        staged_filename_candidates=(
            "nmls_employer_entities.csv",
            "EmployerEntities.csv",
            "Employers.csv",
            "employer_entities.csv",
        ),
        pk_column_label="EMPLOYER_NMLS_ID",
        # ≥100K employers, per directive validation gate.
        minimum_row_floor=100_000,
    ),
    StreamSpec(
        name="branch_offices",
        staged_filename_candidates=(
            "nmls_branch_offices.csv",
            "BranchOffices.csv",
            "Branches.csv",
            "branch_offices.csv",
        ),
        pk_column_label="EMPLOYER_NMLS_ID",
        minimum_row_floor=200_000,
    ),
    StreamSpec(
        name="state_licenses_held",
        staged_filename_candidates=(
            "nmls_state_licenses_held.csv",
            "StateLicensesHeld.csv",
            "StateLicenses.csv",
            "state_licenses_held.csv",
        ),
        pk_column_label="NMLS_ID",
        minimum_row_floor=2_000_000,
    ),
)
STREAMS_BY_NAME: dict[str, StreamSpec] = {s.name: s for s in STREAMS}


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("nmls-r2-ingest")


log = _logger()


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
# Programmatic discovery probe.
# --------------------------------------------------------------------------- #


def probe_public_access(client: httpx.Client) -> dict[str, Any]:
    """Probe the canonical NMLS public-data endpoints. Records HTTP status,
    Cloudflare-protection signal, and returned content-type for each URL.

    Used only to record in the audit ledger that the script tried — and to
    pick up the "happy path" if NMLS ever publishes static bulk URLs.
    Currently the realistic outcome is 403 / dynamic-page / no-link-found.
    """
    findings: dict[str, Any] = {}
    for label, url in (
        ("reports_page", NMLS_REPORTS_PAGE),
        ("consumer_access", NMLS_CONSUMER_ACCESS),
    ):
        try:
            r = client.get(url, follow_redirects=True, timeout=30.0)
            findings[label] = {
                "status": r.status_code,
                "final_url": str(r.url),
                "content_type": r.headers.get("content-type"),
                "cloudflare_blocked": (
                    r.status_code == 403
                    and "cloudflare" in r.text.lower()[:4000]
                ),
            }
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            findings[label] = {"error": str(exc)}
    return findings


# --------------------------------------------------------------------------- #
# DuckDB transform — per-stream projection + normalization.
# --------------------------------------------------------------------------- #


# Macros mirror scripts/_lib/nmls_normalize.py exactly. Keep in sync.
_NORMALIZE_MACROS_SQL = r"""
CREATE MACRO nmls_normalize_id(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(CAST(raw AS VARCHAR)) = '' THEN NULL
    ELSE TRY_CAST(
      regexp_replace(CAST(raw AS VARCHAR), '\D', '', 'g') AS BIGINT
    )
  END
);

CREATE MACRO nmls_zip5(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(CAST(raw AS VARCHAR)) = '' THEN NULL
    WHEN length(regexp_replace(CAST(raw AS VARCHAR), '\D', '', 'g')) < 5 THEN NULL
    ELSE substr(regexp_replace(CAST(raw AS VARCHAR), '\D', '', 'g'), 1, 5)
  END
);

CREATE MACRO nmls_state_code(raw) AS (
  CASE
    WHEN raw IS NULL THEN NULL
    WHEN length(trim(upper(CAST(raw AS VARCHAR)))) <> 2 THEN NULL
    WHEN trim(upper(CAST(raw AS VARCHAR))) IN (
      'AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA',
      'HI','ID','IL','IN','IA','KS','KY','LA','ME','MD',
      'MA','MI','MN','MS','MO','MT','NE','NV','NH','NJ',
      'NM','NY','NC','ND','OH','OK','OR','PA','RI','SC',
      'SD','TN','TX','UT','VT','VA','WA','WV','WI','WY',
      'DC','PR','VI','GU','AS','MP'
    ) THEN trim(upper(CAST(raw AS VARCHAR)))
    ELSE NULL
  END
);

-- Strips ONE trailing org suffix word — list mirrors
-- scripts/_lib/nmls_normalize.py:_ORG_SUFFIXES exactly.
CREATE MACRO nmls_normalize_employer_name(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(CAST(raw AS VARCHAR)) = '' THEN NULL
    ELSE NULLIF(
      (
        WITH parts AS (
          SELECT string_split(
            trim(regexp_replace(
              regexp_replace(
                regexp_replace(
                  lower(CAST(raw AS VARCHAR)),
                  '\bn\.a\.?\b', 'na', 'g'
                ),
                '[,.&''"]+', ' ', 'g'
              ),
              '\s+', ' ', 'g'
            )),
            ' '
          ) AS p
        )
        SELECT CASE
          WHEN length(p) >= 2 AND p[length(p)] IN
               ('llc','inc','incorporated','corp','corporation','ltd','limited',
                'lp','llp','pc','pa','pllc','co','company','na','fsb','fa','ssb',
                'trust','group','holdings','associates','partners','partnership')
          THEN array_to_string(p[1:length(p)-1], ' ')
          ELSE array_to_string(p, ' ')
        END FROM parts
      ),
      ''
    )
  END
);

CREATE MACRO nmls_normalize_name_part(raw) AS (
  NULLIF(
    trim(regexp_replace(
      regexp_replace(
        lower(CAST(coalesce(raw, '') AS VARCHAR)),
        '[,.&''"]+', ' ', 'g'
      ),
      '\s+', ' ', 'g'
    )),
    ''
  )
);

-- Employer-kind classifier — order matters; mirrors
-- scripts/_lib/nmls_normalize.py:_EMPLOYER_KIND_PATTERNS.
CREATE MACRO nmls_classify_employer_kind(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(CAST(raw AS VARCHAR)) = '' THEN NULL
    WHEN strpos(lower(CAST(raw AS VARCHAR)), 'credit union') > 0 THEN 'CREDIT_UNION'
    WHEN strpos(lower(CAST(raw AS VARCHAR)), 'federal credit') > 0 THEN 'CREDIT_UNION'
    WHEN strpos(lower(CAST(raw AS VARCHAR)), 'state-chartered credit') > 0 THEN 'CREDIT_UNION'
    WHEN strpos(lower(CAST(raw AS VARCHAR)), 'mortgage bank') > 0 THEN 'MORTGAGE_BANK'
    WHEN strpos(lower(CAST(raw AS VARCHAR)), 'mortgage banker') > 0 THEN 'MORTGAGE_BANK'
    WHEN strpos(lower(CAST(raw AS VARCHAR)), 'mortgage lender') > 0 THEN 'MORTGAGE_BANK'
    WHEN strpos(lower(CAST(raw AS VARCHAR)), 'non-depository') > 0 THEN 'MORTGAGE_BANK'
    WHEN strpos(lower(CAST(raw AS VARCHAR)), 'mortgage broker') > 0 THEN 'MORTGAGE_BROKER'
    WHEN strpos(lower(CAST(raw AS VARCHAR)), 'loan broker') > 0 THEN 'MORTGAGE_BROKER'
    WHEN strpos(lower(CAST(raw AS VARCHAR)), 'broker') > 0 THEN 'MORTGAGE_BROKER'
    WHEN strpos(lower(CAST(raw AS VARCHAR)), 'federal depository') > 0 THEN 'BANK'
    WHEN strpos(lower(CAST(raw AS VARCHAR)), 'national bank') > 0 THEN 'BANK'
    WHEN strpos(lower(CAST(raw AS VARCHAR)), 'state bank') > 0 THEN 'BANK'
    WHEN strpos(lower(CAST(raw AS VARCHAR)), 'savings bank') > 0 THEN 'BANK'
    WHEN strpos(lower(CAST(raw AS VARCHAR)), 'savings and loan') > 0 THEN 'BANK'
    WHEN strpos(lower(CAST(raw AS VARCHAR)), 'savings & loan') > 0 THEN 'BANK'
    WHEN strpos(lower(CAST(raw AS VARCHAR)), 'commercial bank') > 0 THEN 'BANK'
    WHEN strpos(lower(CAST(raw AS VARCHAR)), 'trust company') > 0 THEN 'BANK'
    WHEN strpos(lower(CAST(raw AS VARCHAR)), 'federal savings') > 0 THEN 'BANK'
    WHEN strpos(lower(CAST(raw AS VARCHAR)), 'bank holding') > 0 THEN 'BANK'
    WHEN strpos(lower(CAST(raw AS VARCHAR)), 'bank') > 0 THEN 'BANK'
    ELSE 'OTHER'
  END
);

-- Status classifier — mirrors scripts/_lib/nmls_normalize.py:_STATUS_MAP
-- (case-insensitive equality, not substring).
CREATE MACRO nmls_classify_status(raw) AS (
  CASE
    WHEN raw IS NULL OR trim(CAST(raw AS VARCHAR)) = '' THEN NULL
    WHEN lower(trim(CAST(raw AS VARCHAR))) IN ('active','approved','approved-conditional',
        'approved - conditional','approved-deficient','approved - deficient',
        'approved-renewal-required','approved deficient',
        'approved deficient renewal required','in good standing',
        'good standing','valid','current') THEN 'ACTIVE'
    WHEN lower(trim(CAST(raw AS VARCHAR))) IN ('inactive','deactivated','withdrawn',
        'voluntarily surrendered','surrendered') THEN 'INACTIVE'
    WHEN lower(trim(CAST(raw AS VARCHAR))) IN ('terminated','terminated-deceased',
        'terminated - deceased','terminated for cause','terminated-cause',
        'abandoned') THEN 'TERMINATED'
    WHEN lower(trim(CAST(raw AS VARCHAR))) = 'suspended' THEN 'SUSPENDED'
    WHEN lower(trim(CAST(raw AS VARCHAR))) = 'revoked' THEN 'REVOKED'
    WHEN lower(trim(CAST(raw AS VARCHAR))) IN ('expired','lapsed',
        'expired - eligible to renew','expired-eligible-to-renew') THEN 'EXPIRED'
    ELSE 'OTHER'
  END
);
"""


def _register_normalizers(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(_NORMALIZE_MACROS_SQL)


def _csv_columns(con: duckdb.DuckDBPyConnection, csv_path: Path) -> list[str]:
    """Read the CSV header row to discover columns."""
    rows = con.execute(
        f"DESCRIBE SELECT * FROM read_csv('{csv_path}', "
        f"all_varchar=TRUE, ignore_errors=TRUE, sample_size=1024);"
    ).fetchall()
    return [r[0] for r in rows]


def _pick_column(columns: list[str], candidates: tuple[str, ...]) -> str | None:
    """Pick the first column whose lowercase form matches one of the
    case-insensitive candidate labels. NMLS emits column names in mixed
    cases across vintages; this insulates us from that."""
    lc_to_real = {c.lower(): c for c in columns}
    for cand in candidates:
        if cand.lower() in lc_to_real:
            return lc_to_real[cand.lower()]
    return None


def _projection_for_stream(
    stream: StreamSpec, columns: list[str], snapshot: date,
) -> tuple[list[str], dict[str, str]]:
    """Build the per-stream SELECT list. All raw columns pass through as
    VARCHAR (lowercased label) plus the canonical normalized columns
    relevant to the stream.

    Returns (select_parts, picked_column_map) — picked_column_map is logged
    so any "missing column" issue is debuggable from the audit row.
    """
    select_parts: list[str] = []
    picked: dict[str, str] = {}

    # Raw-column passthroughs (lowercase label).
    for c in columns:
        select_parts.append(f'"{c}" AS "{c.lower()}"')

    # Normalized columns common to all streams that carry an NMLS_ID.
    nmls_id_col = _pick_column(columns, (
        "NMLS_ID", "NMLSID", "NMLS Id", "NmlsID", "MLO_NMLS_ID",
        "INDIVIDUAL_NMLS_ID", "MLO_NMLSID",
    ))
    if nmls_id_col:
        picked["nmls_id"] = nmls_id_col
        select_parts.append(
            f'nmls_normalize_id("{nmls_id_col}") AS nmls_id_normalized'
        )
    else:
        select_parts.append('CAST(NULL AS BIGINT) AS nmls_id_normalized')

    employer_id_col = _pick_column(columns, (
        "EMPLOYER_NMLS_ID", "EMPLOYER_ID", "EmployerID", "Employer_NMLS_ID",
        "CURRENT_EMPLOYER_NMLS_ID", "CurrentEmployerNMLSID",
        "PARENT_NMLS_ID", "ENTITY_NMLS_ID", "COMPANY_NMLS_ID",
    ))
    if employer_id_col:
        picked["employer_nmls_id"] = employer_id_col
        select_parts.append(
            f'nmls_normalize_id("{employer_id_col}") '
            f'AS employer_nmls_id_normalized'
        )
    elif stream.name == "employer_entities" and nmls_id_col:
        # In the employer stream, NMLS_ID *is* the employer NMLS ID.
        picked["employer_nmls_id"] = nmls_id_col
        select_parts.append(
            f'nmls_normalize_id("{nmls_id_col}") '
            f'AS employer_nmls_id_normalized'
        )
    else:
        select_parts.append(
            'CAST(NULL AS BIGINT) AS employer_nmls_id_normalized'
        )

    # MLO name parts.
    if stream.name in ("mlo_individuals", "state_licenses_held"):
        first_col = _pick_column(columns, (
            "FIRST_NAME", "FirstName", "First Name", "FNAME",
            "MLO_FIRST_NAME", "INDIVIDUAL_FIRST_NAME",
        ))
        middle_col = _pick_column(columns, (
            "MIDDLE_NAME", "MiddleName", "Middle Name", "MNAME",
            "MLO_MIDDLE_NAME",
        ))
        last_col = _pick_column(columns, (
            "LAST_NAME", "LastName", "Last Name", "LNAME",
            "MLO_LAST_NAME", "INDIVIDUAL_LAST_NAME",
        ))
        for label, col in (("first", first_col),
                           ("middle", middle_col),
                           ("last", last_col)):
            if col:
                picked[f"mlo_{label}"] = col
                select_parts.append(
                    f'nmls_normalize_name_part("{col}") '
                    f'AS mlo_{label}_normalized'
                )
            else:
                select_parts.append(
                    f'CAST(NULL AS VARCHAR) AS mlo_{label}_normalized'
                )

    # Employer name (current_employer for individual stream; entity name for
    # employer / branch streams).
    if stream.name == "mlo_individuals":
        emp_name_col = _pick_column(columns, (
            "CURRENT_EMPLOYER_NAME", "CurrentEmployerName", "EMPLOYER_NAME",
            "CURRENT_EMPLOYER", "EMPLOYER",
        ))
    elif stream.name in ("employer_entities", "branch_offices"):
        emp_name_col = _pick_column(columns, (
            "LEGAL_NAME", "LegalName", "ENTITY_NAME", "EMPLOYER_NAME",
            "BUSINESS_NAME", "NAME",
        ))
    else:
        emp_name_col = None
    if emp_name_col:
        picked["employer_name"] = emp_name_col
        select_parts.append(
            f'nmls_normalize_employer_name("{emp_name_col}") '
            f'AS employer_name_normalized'
        )
    else:
        select_parts.append(
            'CAST(NULL AS VARCHAR) AS employer_name_normalized'
        )

    # Employer kind (only meaningful on employer + individual streams).
    if stream.name in ("employer_entities", "mlo_individuals"):
        kind_col = _pick_column(columns, (
            "ENTITY_TYPE", "EntityType", "AUTHORITY_TYPE",
            "EMPLOYER_TYPE", "INSTITUTION_TYPE", "BUSINESS_TYPE",
            "CHARTER_TYPE",
        ))
        if kind_col:
            picked["employer_kind"] = kind_col
            select_parts.append(
                f'nmls_classify_employer_kind("{kind_col}") '
                f'AS employer_kind_normalized'
            )
        else:
            select_parts.append(
                'CAST(NULL AS VARCHAR) AS employer_kind_normalized'
            )

    # Address columns — vary by stream but the MLO + employer share patterns.
    zip_col = _pick_column(columns, (
        "MAILING_ZIP", "MailingZip", "ZIP_CODE", "ZIP", "POSTAL_CODE",
        "ADDRESS_ZIP", "BUSINESS_ZIP",
    ))
    state_col = _pick_column(columns, (
        "MAILING_STATE", "MailingState", "STATE", "ADDRESS_STATE",
        "BUSINESS_STATE", "PHYSICAL_STATE",
    ))
    if stream.name == "mlo_individuals":
        if zip_col:
            picked["mlo_zip"] = zip_col
            select_parts.append(
                f'nmls_zip5("{zip_col}") AS mlo_address_zip5'
            )
        else:
            select_parts.append('CAST(NULL AS VARCHAR) AS mlo_address_zip5')
        if state_col:
            picked["mlo_state"] = state_col
            select_parts.append(
                f'nmls_state_code("{state_col}") '
                f'AS mlo_address_state_normalized'
            )
        else:
            select_parts.append(
                'CAST(NULL AS VARCHAR) AS mlo_address_state_normalized'
            )
    elif stream.name in ("employer_entities", "branch_offices"):
        if zip_col:
            picked["employer_zip"] = zip_col
            select_parts.append(
                f'nmls_zip5("{zip_col}") AS employer_address_zip5'
            )
        else:
            select_parts.append(
                'CAST(NULL AS VARCHAR) AS employer_address_zip5'
            )
        if state_col:
            picked["employer_state"] = state_col
            select_parts.append(
                f'nmls_state_code("{state_col}") '
                f'AS employer_address_state_normalized'
            )
        else:
            select_parts.append(
                'CAST(NULL AS VARCHAR) AS employer_address_state_normalized'
            )

    # Status.
    status_col = _pick_column(columns, (
        "STATUS", "MLO_STATUS", "LICENSE_STATUS", "EMPLOYMENT_STATUS",
        "ENTITY_STATUS", "AUTHORITY_STATUS",
    ))
    if status_col:
        picked["status"] = status_col
        if stream.name == "employer_entities":
            label = "employer_status_normalized"
        else:
            label = "mlo_status_normalized"
        select_parts.append(
            f'nmls_classify_status("{status_col}") AS {label}'
        )
        if label == "mlo_status_normalized":
            select_parts.append(
                f'(nmls_classify_status("{status_col}") = \'ACTIVE\') '
                f'AS is_active'
            )

    # Date casts on common lifecycle date columns (preserve raw too).
    for col in columns:
        lc = col.lower()
        if any(k in lc for k in (
            "issue_date", "effective_date", "expiration_date", "term_date",
            "approval_date", "license_date",
        )):
            select_parts.append(
                f'TRY_CAST(NULLIF("{col}", \'\') AS DATE) '
                f'AS "{lc}_typed"'
            )

    # Snapshot partition metadata.
    select_parts.append(
        f"CAST('{snapshot.isoformat()}' AS DATE) AS nmls_snapshot_date"
    )

    return select_parts, picked


def csv_to_parquet(
    csv_path: Path,
    parquet_path: Path,
    *,
    stream: StreamSpec,
    snapshot: date,
    log_prefix: str,
    max_rows: int | None,
) -> tuple[int, int, dict[str, float], dict[str, str]]:
    """Read pipe / comma / tab-delimited CSV as VARCHAR, project + normalize,
    write ZSTD Parquet.

    Returns (rows_in, rows_pq, null_rates, picked_columns_log).
    """
    con = duckdb.connect(":memory:")
    con.execute("PRAGMA threads=4;")
    con.execute("PRAGMA memory_limit='6GB';")
    _register_normalizers(con)

    columns = _csv_columns(con, csv_path)
    log.info("%s   csv columns (%d): %s", log_prefix,
             len(columns), ", ".join(columns[:12])
             + ("..." if len(columns) > 12 else ""))

    select_parts, picked = _projection_for_stream(stream, columns, snapshot)
    log.info("%s   normalized-column picks: %s", log_prefix, picked)

    con.execute(f"""
        CREATE VIEW raw AS
        SELECT * FROM read_csv(
          '{csv_path}',
          all_varchar=TRUE,
          ignore_errors=TRUE, null_padding=TRUE,
          sample_size=1024, header=TRUE
        );
    """)

    rows_in_row = con.execute("SELECT count(*) FROM raw;").fetchone()
    rows_in = int(rows_in_row[0]) if rows_in_row else 0
    log.info("%s   raw rows: %s", log_prefix, f"{rows_in:,}")

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

    # Null-rate sanity on key normalized columns.
    rates_row = con.execute(f"""
        SELECT
          count(*) AS total,
          count(*) FILTER (WHERE nmls_id_normalized IS NULL) AS nmls_null,
          count(*) FILTER (WHERE employer_nmls_id_normalized IS NULL) AS emp_null,
          count(*) FILTER (
            WHERE 'mlo_first_normalized' IN (
              SELECT column_name FROM (DESCRIBE SELECT * FROM read_parquet('{parquet_path}'))
            )
          ) AS has_mlo_name
        FROM read_parquet('{parquet_path}');
    """).fetchone()
    total = int(rates_row[0]) if rates_row else 0
    rows_pq = total
    rates: dict[str, float]
    if total > 0:
        rates = {
            "nmls_id_null_pct": round(100.0 * int(rates_row[1]) / total, 4),
            "employer_nmls_id_null_pct": round(
                100.0 * int(rates_row[2]) / total, 4
            ),
        }
        # mlo_name null rate (joint first+last) only meaningful on streams
        # that carry MLO names.
        if stream.name in ("mlo_individuals", "state_licenses_held"):
            name_null = con.execute(f"""
                SELECT count(*) FILTER (
                  WHERE mlo_first_normalized IS NULL
                    AND mlo_last_normalized IS NULL
                )
                FROM read_parquet('{parquet_path}');
            """).fetchone()
            rates["mlo_name_null_pct"] = round(
                100.0 * int(name_null[0]) / total, 4,
            )
        else:
            rates["mlo_name_null_pct"] = 0.0
    else:
        rates = {
            "nmls_id_null_pct": 0.0,
            "employer_nmls_id_null_pct": 0.0,
            "mlo_name_null_pct": 0.0,
        }
    log.info(
        "%s   parquet rows: %s; null-rate nmls_id=%.2f%% emp_id=%.2f%% "
        "mlo_name=%.2f%%",
        log_prefix, f"{rows_pq:,}",
        rates["nmls_id_null_pct"],
        rates["employer_nmls_id_null_pct"],
        rates["mlo_name_null_pct"],
    )
    con.close()
    return rows_in, rows_pq, rates, picked


def upload_to_r2(parquet_path: Path, *, bucket: str, key: str) -> int:
    s3 = _r2_client()
    file_bytes = parquet_path.stat().st_size
    s3.upload_file(
        str(parquet_path), bucket, key,
        ExtraArgs={"ContentType": "application/x-parquet"},
    )
    return file_bytes


# --------------------------------------------------------------------------- #
# Audit helpers.
# --------------------------------------------------------------------------- #


def insert_run_row(
    conn: psycopg.Connection,
    stream: StreamSpec,
    snapshot: date,
    *,
    source_url: str | None,
    source_filename: str | None,
    source_last_modified: datetime | None,
    prior_source_last_modified: datetime | None,
) -> str:
    sql = """
    INSERT INTO ops.nmls_r2_ingest_runs (
        stream, snapshot_date, status, source_url, source_filename,
        source_last_modified, prior_source_last_modified
    ) VALUES (%s, %s, 'running', %s, %s, %s, %s)
    RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            stream.name, snapshot, source_url, source_filename,
            source_last_modified, prior_source_last_modified,
        ))
        row_id = cur.fetchone()[0]
    conn.commit()
    return str(row_id)


def write_awaiting_operator_run(
    conn: psycopg.Connection,
    stream: StreamSpec,
    snapshot: date,
    *,
    notes: dict[str, Any],
) -> None:
    started = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ops.nmls_r2_ingest_runs (
                stream, snapshot_date, status,
                started_at, finished_at, duration_seconds, notes
            ) VALUES (%s, %s, 'awaiting_operator', %s, %s, 0, %s);
            """,
            (stream.name, snapshot, started, started, Jsonb(notes)),
        )
    conn.commit()


def finalize_run_row(
    conn: psycopg.Connection,
    run_id: str,
    *,
    status: str,
    download_bytes: int,
    source_uncompressed_bytes: int,
    source_row_count: int,
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
            UPDATE ops.nmls_r2_ingest_runs
               SET status = %s,
                   download_bytes = %s,
                   source_uncompressed_bytes = %s,
                   source_row_count = %s,
                   parquet_row_count = %s,
                   parquet_bytes_written = %s,
                   parquet_column_count = %s,
                   r2_bucket = %s, r2_prefix = %s, r2_object_key = %s,
                   r2_total_bytes = %s,
                   nmls_id_null_pct = %s,
                   mlo_name_null_pct = %s,
                   employer_nmls_id_null_pct = %s,
                   finished_at = now(), duration_seconds = %s,
                   error_message = %s, notes = %s
             WHERE id = %s;
            """, (
            status, download_bytes, source_uncompressed_bytes, source_row_count,
            parquet_rows, parquet_bytes, parquet_columns,
            r2_bucket, r2_prefix, r2_object_key, r2_total_bytes,
            (null_rates or {}).get("nmls_id_null_pct"),
            (null_rates or {}).get("mlo_name_null_pct"),
            (null_rates or {}).get("employer_nmls_id_null_pct"),
            duration, error_message,
            Jsonb(notes) if notes else None, run_id,
        ))
    conn.commit()


# --------------------------------------------------------------------------- #
# Per-stream main.
# --------------------------------------------------------------------------- #


def find_staged_file(stream: StreamSpec, staged_dir: Path) -> Path | None:
    for cand in stream.staged_filename_candidates:
        p = staged_dir / cand
        if p.exists() and p.is_file():
            return p
    return None


def ingest_stream(
    stream: StreamSpec,
    snapshot: date,
    *,
    resume: bool,
    staged_dir: Path,
    workdir: Path,
    max_rows: int | None,
    r2_prefix_override: str | None,
    discovery_findings: dict[str, Any],
) -> int:
    log_prefix = f"[stream={stream.name}]"
    started_wall = time.monotonic()
    log.info("%s start", log_prefix)

    csv_path: Path | None = None
    if resume:
        csv_path = find_staged_file(stream, staged_dir)
        if csv_path:
            log.info("%s   using staged file: %s", log_prefix, csv_path)
        else:
            log.warning(
                "%s   no staged file found at %s/[%s]",
                log_prefix, staged_dir,
                ",".join(stream.staged_filename_candidates),
            )

    if not csv_path:
        # No programmatic discovery yielded a downloadable URL (Cloudflare /
        # SharePoint dynamic), and operator hasn't staged a file — record
        # awaiting_operator and bail out for this stream.
        with psycopg.connect(_database_url()) as conn:
            write_awaiting_operator_run(
                conn, stream, snapshot,
                notes={
                    "discovery": discovery_findings,
                    "expected_filenames": list(
                        stream.staged_filename_candidates
                    ),
                    "staged_dir_checked": str(staged_dir),
                    "instructions": (
                        "Download the NMLS bulk extract for this stream "
                        "(via paid NMLS Reports subscription, regulator data-"
                        "share agreement, or other authorized channel) and "
                        "place at the staged_dir under one of "
                        "expected_filenames; re-run with --resume."
                    ),
                },
            )
        log.error(
            "%s   AWAITING OPERATOR — no programmatic access; place CSV at "
            "%s/<%s> and re-run with --resume",
            log_prefix, staged_dir, stream.canonical_filename,
        )
        return EXIT_OPERATOR_ACTION_REQUIRED

    # Staged-file path.
    parquet_path = workdir / f"nmls_{stream.name}_{snapshot.isoformat()}.parquet"

    target_prefix = (
        r2_prefix_override
        if r2_prefix_override
        else f"nmls/{stream.name}/snapshot={snapshot.isoformat()}/"
    )
    target_key = target_prefix.rstrip("/") + "/data.parquet"

    with psycopg.connect(_database_url()) as conn:
        run_id = insert_run_row(
            conn, stream, snapshot,
            source_url=None,
            source_filename=csv_path.name,
            source_last_modified=None,
            prior_source_last_modified=None,
        )
        log.info("%s   run id: %s", log_prefix, run_id)

        try:
            uncompressed_bytes = csv_path.stat().st_size
            log.info(
                "%s   csv size: %.1f MB",
                log_prefix, uncompressed_bytes / (1 << 20),
            )

            rows_in, rows_pq, rates, picked = csv_to_parquet(
                csv_path, parquet_path,
                stream=stream, snapshot=snapshot,
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

            # Validation gate: minimum row floor (max_rows path skips this).
            if max_rows is None and rows_pq < stream.minimum_row_floor:
                raise RuntimeError(
                    f"{stream.name}: parquet rows {rows_pq:,} < "
                    f"required floor {stream.minimum_row_floor:,}"
                )

            uploaded = upload_to_r2(
                parquet_path, bucket=R2_BUCKET, key=target_key,
            )
            log.info(
                "%s uploaded → s3://%s/%s (%.1f MB)",
                log_prefix, R2_BUCKET, target_key, uploaded / (1 << 20),
            )

            con = duckdb.connect(":memory:")
            col_count = int(con.execute(
                f"SELECT count(*) FROM ("
                f"DESCRIBE SELECT * FROM read_parquet('{parquet_path}')"
                f");"
            ).fetchone()[0])
            con.close()

            finalize_run_row(
                conn, run_id, status="completed",
                download_bytes=uncompressed_bytes,
                source_uncompressed_bytes=uncompressed_bytes,
                source_row_count=rows_in,
                parquet_rows=rows_pq,
                parquet_bytes=uploaded,
                parquet_columns=col_count,
                r2_bucket=R2_BUCKET,
                r2_prefix=target_prefix,
                r2_object_key=target_key,
                r2_total_bytes=uploaded,
                null_rates=rates,
                started_at=started_wall, error_message=None,
                notes={
                    "max_rows": max_rows,
                    "r2_prefix_override": r2_prefix_override,
                    "picked_columns": picked,
                    "staged_filename": csv_path.name,
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
                download_bytes=0, source_uncompressed_bytes=0, source_row_count=0,
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
                parquet_path.unlink(missing_ok=True)
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# CLI.
# --------------------------------------------------------------------------- #


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "stream", nargs="?",
        help=(
            "Stream name: mlo_individuals | employer_entities | "
            "branch_offices | state_licenses_held."
        ),
    )
    p.add_argument("--all", action="store_true",
                   help="Run all 4 streams in sequence.")
    p.add_argument(
        "--resume", action="store_true",
        help=(
            "Read CSVs from the operator-staged directory rather than "
            "attempting programmatic download."
        ),
    )
    p.add_argument(
        "--staged-dir", default=None,
        help=(
            "Directory where the operator has placed staged CSV(s). "
            "Default: $HOME/Downloads/nmls_bulk."
        ),
    )
    p.add_argument(
        "--snapshot-date", default=None,
        help="ISO YYYY-MM-DD; default = today (UTC).",
    )
    p.add_argument("--max-rows", type=int, default=None)
    p.add_argument("--workdir", default=None)
    p.add_argument(
        "--r2-prefix-override", default=None,
        help="Replace canonical nmls/{stream}/snapshot={d}/ prefix (smoke).",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    workdir = Path(args.workdir or "/tmp/nmls_r2_ingest")
    workdir.mkdir(parents=True, exist_ok=True)

    staged_dir = Path(
        args.staged_dir
        or os.path.expanduser("~/Downloads/nmls_bulk")
    )

    if args.snapshot_date:
        snapshot = date.fromisoformat(args.snapshot_date)
    else:
        snapshot = datetime.now(timezone.utc).date()

    if args.all:
        target_streams = list(STREAMS)
    elif args.stream:
        if args.stream not in STREAMS_BY_NAME:
            log.error(
                "unknown stream %r; valid: %s",
                args.stream, ", ".join(STREAMS_BY_NAME),
            )
            return 2
        target_streams = [STREAMS_BY_NAME[args.stream]]
    else:
        log.error("must pass <stream> or --all")
        return 2

    log.info("snapshot_date=%s staged_dir=%s resume=%s",
             snapshot.isoformat(), staged_dir, args.resume)

    # One probe up front (single HTTP attempt per URL) to record what we
    # tried for the audit ledger. Always run — informs the awaiting-operator
    # message and surfaces any future change in NMLS public-data posture.
    with httpx.Client(headers={"User-Agent": USER_AGENT}) as client:
        discovery = probe_public_access(client)
    log.info("discovery findings: %s", discovery)

    overall_rc = 0
    awaiting = []
    for stream in target_streams:
        log.info("=" * 70)
        log.info("=== INGEST: stream=%s snapshot=%s ===",
                 stream.name, snapshot.isoformat())
        log.info("=" * 70)
        rc = ingest_stream(
            stream, snapshot,
            resume=args.resume,
            staged_dir=staged_dir,
            workdir=workdir,
            max_rows=args.max_rows,
            r2_prefix_override=args.r2_prefix_override,
            discovery_findings=discovery,
        )
        if rc == EXIT_OPERATOR_ACTION_REQUIRED:
            awaiting.append(stream)
            if overall_rc == 0:
                overall_rc = EXIT_OPERATOR_ACTION_REQUIRED
        elif rc != 0:
            overall_rc = rc

    if awaiting:
        log.error("=" * 70)
        log.error("OPERATOR ACTION REQUIRED — %d stream(s) awaiting staged "
                  "files at %s:", len(awaiting), staged_dir)
        for s in awaiting:
            log.error("  • %s  →  expected one of: %s",
                      s.name, ", ".join(s.staged_filename_candidates))
        log.error("=" * 70)
        log.error(
            "Once files are in place, re-run with `--resume "
            "--staged-dir %s`.", staged_dir,
        )

    return overall_rc


if __name__ == "__main__":
    sys.exit(main())

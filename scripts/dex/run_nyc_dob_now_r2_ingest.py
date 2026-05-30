#!/usr/bin/env python3
"""NYC DOB Now R2 ingest — 4 streams × snapshot-date partition (no RisingWave).

Mirrors three NYC DOB Now SODA datasets into R2 as ZSTD-compressed Parquet,
partitioned by snapshot date, then computes a derived `active_firms` rollup
in DuckDB against the freshly-written approved-permits + job-application
parquets.

  stream                       | dataset 4x4   | rows (~) | identity column
  -----------------------------+---------------+----------+----------------------
  approved_permits             | rbx6-tga4     | 930K     | applicant_business_name_normalized
  job_application_filings      | w9ak-ipjd     | 900K     | applicant_business_name_normalized
  stalled_construction_sites   | i296-73x5     | 1.4M     | bin (no block/lot in feed)
  active_firms (derived)       | —             | 30-50K   | (business_name, license_number)

Layout per snapshot:

  nyc-dob-now/approved_permits/snapshot=YYYY-MM-DD/data.parquet
  nyc-dob-now/job_application_filings/snapshot=YYYY-MM-DD/data.parquet
  nyc-dob-now/stalled_construction_sites/snapshot=YYYY-MM-DD/data.parquet
  nyc-dob-now/active_firms/snapshot=YYYY-MM-DD/data.parquet

Audit ledger: ops.nyc_dob_now_r2_ingest_runs (one row per (snapshot_date, stream)).

Schema-reality divergence from directive 2026-05-08-nyc-dob-now-r2-ingest.md:

  * Stalled Construction Sites (i296-73x5) is a complaint-grain feed with
    only `bin` — no `block`, `lot`, or owner_name. The directive expected
    bbl_normalized + owner_name_normalized on this stream; in reality only
    `borough_normalized` (derived from `borough_name`) is computable. BBL
    coverage on this stream is intentionally NULL.
  * Job Application Filings (w9ak-ipjd) carries the firm under
    `owner_s_business_name`, NOT `applicant_business_name` — the
    `applicant_*` columns on this feed are individual filers, not firms.
    The active_firms aggregation accepts both column conventions.
  * Approved Permits (rbx6-tga4) ships `permittee_s_license_type` rather
    than a simple `license_type` field; classify_license_kind() handles
    the underscore-suffixed Socrata-rewritten name.
  * Directive sanity check "applicant_business_name_normalized NULL rate
    < 1% on permit / application streams" holds for approved_permits
    (~0.5% NULL) but is structurally violated by job_application_filings
    (~6.3% NULL): many job applications are filed by individual homeowners
    or sole proprietors whose owner_s_business_name is legitimately empty.
    This is real-world data shape, not a normalization bug — the existing
    Postgres ingest for the same dataset stores NULL in those rows too.

NO RisingWave wiring — that's a follow-up directive after R2 is populated.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_nyc_dob_now_r2_ingest.py
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_nyc_dob_now_r2_ingest.py --max-rows 50000 \\
                                                          --r2-prefix-override 'nyc-dob-now/_smoke/'
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_nyc_dob_now_r2_ingest.py --stream approved_permits --dry-run
"""

from __future__ import annotations

import argparse
import json
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
import pyarrow as pa
import pyarrow.parquet as pq
from psycopg.types.json import Jsonb

from scripts._lib.nyc_dob_now_normalize import (
    classify_license_kind,
    classify_work_type,
    compute_bbl,
    normalize_borough_code,
    normalize_business_name,
    normalize_owner_name,
)


R2_BUCKET = "dex-raw-landing-zone"
SOCRATA_HOST = "https://data.cityofnewyork.us"

DEFAULT_PAGE_SIZE = 50_000
DEFAULT_PAGE_SLEEP = 0.25
HTTP_TIMEOUT_SECONDS = 120.0
HTTP_RETRY_LIMIT = 5
HTTP_RETRY_BASE_BACKOFF_SECONDS = 2.0


# --------------------------------------------------------------------------- #
# Stream registry — ordered SODA-streams-first so that the derived
# active_firms aggregation has its inputs on disk by the time it runs.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class StreamSpec:
    name: str
    socrata_id: str | None       # None for the derived 'active_firms' stream.
    dataset_name: str

    def soda_url(self) -> str | None:
        return (
            f"{SOCRATA_HOST}/resource/{self.socrata_id}.json"
            if self.socrata_id else None
        )

    def metadata_url(self) -> str | None:
        return (
            f"{SOCRATA_HOST}/api/views/{self.socrata_id}.json"
            if self.socrata_id else None
        )


SODA_STREAMS: tuple[StreamSpec, ...] = (
    StreamSpec(
        name="approved_permits",
        socrata_id="rbx6-tga4",
        dataset_name="DOB NOW: Build – Approved Permits",
    ),
    StreamSpec(
        name="job_application_filings",
        socrata_id="w9ak-ipjd",
        dataset_name="DOB NOW: Build – Job Application Filings",
    ),
    StreamSpec(
        name="stalled_construction_sites",
        socrata_id="i296-73x5",
        dataset_name="DOB Stalled Construction Sites",
    ),
)

DERIVED_STREAM = StreamSpec(
    name="active_firms",
    socrata_id=None,
    dataset_name="NYC DOB Now active firms (derived)",
)

ALL_STREAMS: tuple[StreamSpec, ...] = SODA_STREAMS + (DERIVED_STREAM,)


# --------------------------------------------------------------------------- #
# Logging.
# --------------------------------------------------------------------------- #


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("nyc-dob-now-r2-ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# Env helpers.
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
    url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ.get("DEX_DB_URL_POOLED")
    if not url:
        raise RuntimeError(
            "neither DEX_DB_URL_DIRECT nor DEX_DB_URL_POOLED is set — "
            "are you running under `doppler run`?"
        )
    return url


def _socrata_app_token() -> str | None:
    return os.environ.get("NYC_OPEN_DATA_APP_TOKEN") or None


# --------------------------------------------------------------------------- #
# Audit-row helpers.
# --------------------------------------------------------------------------- #


@dataclass
class RunRow:
    run_id: str
    snapshot_date: date
    stream: str
    started_monotonic: float


def insert_run_row(
    conn: psycopg.Connection,
    *,
    snapshot_date: date,
    stream: str,
    source_url: str | None,
    socrata_dataset_id: str | None,
    source_last_modified: datetime | None,
    num_found_at_run_start: int | None,
    started_monotonic: float,
) -> RunRow:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.nyc_dob_now_r2_ingest_runs (
                snapshot_date, stream, status,
                source_url, socrata_dataset_id, source_last_modified,
                num_found_at_run_start
            ) VALUES (%s, %s, 'running', %s, %s, %s, %s)
            RETURNING id;
            """,
            (snapshot_date, stream, source_url, socrata_dataset_id,
             source_last_modified, num_found_at_run_start),
        )
        row_id = cur.fetchone()[0]
    conn.commit()
    return RunRow(
        run_id=str(row_id),
        snapshot_date=snapshot_date,
        stream=stream,
        started_monotonic=started_monotonic,
    )


def finalize_run_row(
    conn: psycopg.Connection,
    rr: RunRow,
    *,
    status: str,
    pages_fetched: int | None,
    bytes_downloaded: int | None,
    parquet_bytes_written: int | None,
    parquet_row_count: int | None,
    r2_bucket: str | None,
    r2_prefix: str | None,
    r2_key: str | None,
    r2_object_bytes: int | None,
    num_found_at_run_end: int | None,
    error_message: str | None,
    error_class: str | None,
    notes: dict[str, Any] | None,
) -> None:
    duration = round(time.monotonic() - rr.started_monotonic, 3)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ops.nyc_dob_now_r2_ingest_runs SET
                status = %s,
                pages_fetched = %s, bytes_downloaded = %s,
                parquet_bytes_written = %s, parquet_row_count = %s,
                r2_bucket = %s, r2_prefix = %s, r2_key = %s, r2_object_bytes = %s,
                num_found_at_run_end = %s,
                finished_at = now(), duration_seconds = %s,
                error_message = %s, error_class = %s, notes = %s
              WHERE id = %s;
            """,
            (
                status, pages_fetched, bytes_downloaded,
                parquet_bytes_written, parquet_row_count,
                r2_bucket, r2_prefix, r2_key, r2_object_bytes,
                num_found_at_run_end,
                duration, error_message, error_class,
                Jsonb(notes) if notes else None, rr.run_id,
            ),
        )
    conn.commit()


# --------------------------------------------------------------------------- #
# SODA HTTP helpers.
# --------------------------------------------------------------------------- #


def _http_headers() -> dict[str, str]:
    h = {
        "User-Agent": "data-engine-x nyc-dob-now-r2-ingest",
        "Accept": "application/json",
    }
    tok = _socrata_app_token()
    if tok:
        h["X-App-Token"] = tok
    return h


def _retry_get(
    client: httpx.Client, url: str, *, params: dict[str, str] | None,
) -> httpx.Response:
    last_exc: Exception | None = None
    for attempt in range(HTTP_RETRY_LIMIT):
        try:
            r = client.get(
                url, params=params, headers=_http_headers(),
                timeout=HTTP_TIMEOUT_SECONDS,
            )
            if r.status_code == 429 or 500 <= r.status_code < 600:
                raise httpx.HTTPStatusError(
                    f"retryable status={r.status_code}", request=r.request, response=r,
                )
            r.raise_for_status()
            return r
        except (httpx.HTTPError, OSError) as exc:
            last_exc = exc
            backoff = HTTP_RETRY_BASE_BACKOFF_SECONDS * (2 ** attempt)
            log.warning(
                "GET %s attempt %d/%d failed: %s — retrying in %.1fs",
                url, attempt + 1, HTTP_RETRY_LIMIT, exc, backoff,
            )
            time.sleep(backoff)
    raise RuntimeError(f"GET {url} failed after {HTTP_RETRY_LIMIT} attempts: {last_exc}")


def fetch_dataset_metadata(client: httpx.Client, stream: StreamSpec) -> dict[str, Any]:
    assert stream.metadata_url() is not None
    r = _retry_get(client, stream.metadata_url(), params=None)
    return r.json()


def fetch_count(client: httpx.Client, stream: StreamSpec) -> int:
    assert stream.soda_url() is not None
    r = _retry_get(
        client, stream.soda_url(),
        params={"$query": "SELECT count(*) AS c"},
    )
    body = r.json()
    if not body or "c" not in body[0]:
        return 0
    return int(body[0]["c"])


def fetch_page(
    client: httpx.Client, stream: StreamSpec,
    *, limit: int, offset: int,
) -> tuple[list[dict[str, Any]], int]:
    assert stream.soda_url() is not None
    r = _retry_get(
        client, stream.soda_url(),
        params={"$limit": str(limit), "$offset": str(offset), "$order": ":id"},
    )
    return r.json(), len(r.content)


# --------------------------------------------------------------------------- #
# SODA pagination.
# --------------------------------------------------------------------------- #


@dataclass
class SodaPull:
    rows: list[dict[str, Any]]
    pages_fetched: int
    bytes_downloaded: int
    num_found_at_run_start: int
    num_found_at_run_end: int
    source_last_modified: datetime | None


def pull_dataset(
    stream: StreamSpec, *,
    page_size: int, page_sleep: float, max_rows: int | None,
) -> SodaPull:
    rows: list[dict[str, Any]] = []
    pages_fetched = 0
    bytes_downloaded = 0

    with httpx.Client(follow_redirects=True) as client:
        meta = fetch_dataset_metadata(client, stream)
        last_mod_unix = meta.get("rowsUpdatedAt") or meta.get("dataUpdatedAt")
        last_mod = (
            datetime.fromtimestamp(int(last_mod_unix), tz=timezone.utc)
            if last_mod_unix is not None else None
        )
        num_start = fetch_count(client, stream)
        log.info(
            "[%s] dataset_name=%s rowsUpdatedAt=%s rowCount=%s",
            stream.name, stream.dataset_name, last_mod, num_start,
        )

        offset = 0
        while True:
            limit = page_size
            if max_rows is not None:
                remaining = max_rows - len(rows)
                if remaining <= 0:
                    break
                limit = min(limit, remaining)

            page, nbytes = fetch_page(
                client, stream, limit=limit, offset=offset,
            )
            pages_fetched += 1
            bytes_downloaded += nbytes
            rows.extend(page)
            log.info(
                "[%s] page %d offset=%d got=%d total=%d (%.1f KB)",
                stream.name, pages_fetched, offset, len(page), len(rows),
                nbytes / 1024,
            )
            if len(page) < limit:
                break
            offset += limit
            if max_rows is not None and len(rows) >= max_rows:
                break
            time.sleep(page_sleep)

        num_end = fetch_count(client, stream)
        if num_end != num_start:
            log.warning(
                "[%s] count drifted mid-run: start=%s end=%s",
                stream.name, num_start, num_end,
            )

    return SodaPull(
        rows=rows, pages_fetched=pages_fetched, bytes_downloaded=bytes_downloaded,
        num_found_at_run_start=num_start, num_found_at_run_end=num_end,
        source_last_modified=last_mod,
    )


# --------------------------------------------------------------------------- #
# Per-stream row projection.
#
# Each projector takes the raw Socrata dict and returns a dict whose keys
# match the stream's pyarrow schema. Raw source fields are preserved as
# strings; normalized fields are computed from them.
# --------------------------------------------------------------------------- #


def _str(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        return s if s else None
    return str(v)


def _zip5(v: Any) -> str | None:
    s = _str(v)
    if s is None:
        return None
    if "-" in s:
        s = s.split("-", 1)[0]
    s = s.strip()
    if len(s) >= 5 and s[:5].isdigit():
        return s[:5]
    return s if s else None


def _project_approved_permits(row: dict[str, Any]) -> dict[str, Any]:
    """rbx6-tga4 — approved DOB Now permits.

    Identity-bearing columns:
      - applicant_business_name → applicant_business_name_normalized (firm)
      - applicant_license      → applicant_license_number_normalized
      - permittee_s_license_type → applicant_license_kind_normalized
      - owner_business_name + owner_name → owner_name_normalized
        (owner_business_name takes precedence; falls back to owner_name)
      - work_type → work_type_normalized
      - borough + block + lot → bbl_normalized
    """
    borough_raw = row.get("borough")
    block_raw = row.get("block")
    lot_raw = row.get("lot")
    applicant_business_name = row.get("applicant_business_name")
    applicant_license = row.get("applicant_license")
    permittee_license_type = row.get("permittee_s_license_type")
    owner_business_name = row.get("owner_business_name")
    owner_name = row.get("owner_name")
    work_type = row.get("work_type")

    # Preserve the full upstream payload as raw_<col> so any field not
    # surfaced in the hot-column set remains queryable. Drop SODA's `:@*`
    # system metadata.
    out = {f"raw_{k}": _str(v) for k, v in row.items() if not k.startswith(":@")}
    out.update({
        # Hot columns surfaced for predicate pushdown.
        "job_filing_number":              _str(row.get("job_filing_number")),
        "work_permit":                    _str(row.get("work_permit")),
        "filing_reason":                  _str(row.get("filing_reason")),
        "house_no":                       _str(row.get("house_no")),
        "street_name":                    _str(row.get("street_name")),
        "borough_raw":                    _str(borough_raw),
        "block":                          _str(block_raw),
        "lot":                            _str(lot_raw),
        "bin":                            _str(row.get("bin")),
        "bbl_socrata":                    _str(row.get("bbl")),
        "work_type":                      _str(work_type),
        "permittee_s_license_type":       _str(permittee_license_type),
        "applicant_license":              _str(applicant_license),
        "applicant_business_name":        _str(applicant_business_name),
        "filing_representative_business_name": _str(row.get("filing_representative_business_name")),
        "owner_business_name":            _str(owner_business_name),
        "owner_name":                     _str(owner_name),
        "permit_status":                  _str(row.get("permit_status")),
        "approved_date":                  _str(row.get("approved_date")),
        "issued_date":                    _str(row.get("issued_date")),
        "expired_date":                   _str(row.get("expired_date")),
        "estimated_job_costs":            _str(row.get("estimated_job_costs")),
        "zip_code":                       _str(row.get("zip_code")),
        "latitude":                       _str(row.get("latitude")),
        "longitude":                      _str(row.get("longitude")),
        # Normalized columns.
        "borough_normalized":             normalize_borough_code(borough_raw),
        "bbl_normalized":                 compute_bbl(borough_raw, block_raw, lot_raw),
        "property_zip5":                  _zip5(row.get("zip_code")),
        "applicant_business_name_normalized": normalize_business_name(_str(applicant_business_name)),
        "applicant_license_number_normalized": _str(applicant_license),
        "applicant_license_kind_normalized":   classify_license_kind(_str(permittee_license_type)),
        "owner_name_normalized":          normalize_owner_name(
            _str(owner_business_name) or _str(owner_name)
        ),
        "work_type_normalized":           classify_work_type(_str(work_type)),
    })
    return out


def _project_job_application_filings(row: dict[str, Any]) -> dict[str, Any]:
    """w9ak-ipjd — submitted job applications (proposed work, may not yet be approved).

    Identity-bearing columns:
      - owner_s_business_name → applicant_business_name_normalized (the firm)
        AND owner_name_normalized (for joins to owner-keyed signals)
      - applicant_license → applicant_license_number_normalized
      - applicant_professional_title → applicant_license_kind_normalized
      - filing_status → application_status_normalized
      - borough + block + lot → bbl_normalized
    """
    borough_raw = row.get("borough")
    block_raw = row.get("block")
    lot_raw = row.get("lot")
    owner_business = row.get("owner_s_business_name")
    applicant_license = row.get("applicant_license")
    applicant_professional_title = row.get("applicant_professional_title")
    filing_status = row.get("filing_status")

    out = {f"raw_{k}": _str(v) for k, v in row.items() if not k.startswith(":@")}
    out.update({
        # Hot columns.
        "job_filing_number":              _str(row.get("job_filing_number")),
        "filing_status":                  _str(filing_status),
        "house_no":                       _str(row.get("house_no")),
        "street_name":                    _str(row.get("street_name")),
        "borough_raw":                    _str(borough_raw),
        "block":                          _str(block_raw),
        "lot":                            _str(lot_raw),
        "bin":                            _str(row.get("bin")),
        "bbl_socrata":                    _str(row.get("bbl")),
        "applicant_professional_title":   _str(applicant_professional_title),
        "applicant_license":              _str(applicant_license),
        "owner_s_business_name":          _str(owner_business),
        "filing_representative_business_name": _str(row.get("filing_representative_business_name")),
        "filing_date":                    _str(row.get("filing_date")),
        "current_status_date":            _str(row.get("current_status_date")),
        "first_permit_date":              _str(row.get("first_permit_date")),
        "approved_date":                  _str(row.get("approved_date")),
        "signoff_date":                   _str(row.get("signoff_date")),
        "initial_cost":                   _str(row.get("initial_cost")),
        "total_construction_floor_area":  _str(row.get("total_construction_floor_area")),
        "proposed_height":                _str(row.get("proposed_height")),
        "job_type":                       _str(row.get("job_type")),
        "zip":                            _str(row.get("zip")),
        "postcode":                       _str(row.get("postcode")),
        "latitude":                       _str(row.get("latitude")),
        "longitude":                      _str(row.get("longitude")),
        # Normalized columns.
        "borough_normalized":             normalize_borough_code(borough_raw),
        "bbl_normalized":                 compute_bbl(borough_raw, block_raw, lot_raw),
        "property_zip5":                  _zip5(row.get("zip") or row.get("postcode")),
        "applicant_business_name_normalized": normalize_business_name(_str(owner_business)),
        "applicant_license_number_normalized": _str(applicant_license),
        "applicant_license_kind_normalized":   classify_license_kind(_str(applicant_professional_title)),
        "owner_name_normalized":          normalize_owner_name(_str(owner_business)),
        "application_status_normalized":  (
            _str(filing_status).upper() if _str(filing_status) else None
        ),
    })
    return out


def _project_stalled_construction_sites(row: dict[str, Any]) -> dict[str, Any]:
    """i296-73x5 — stalled construction sites (complaint snapshot).

    The dataset carries `bin`, `borough_name`, `house_number`, `street_name`,
    `complaint_number`, `dobrundate`, `date_complaint_received` — but NO
    block/lot, NO owner_name. BBL is therefore NOT computable from this
    feed; bbl_normalized will be NULL, and an owner-name-keyed join must
    fall back to bin → PLUTO bridge in downstream MVs.
    """
    borough_name = row.get("borough_name")
    out = {f"raw_{k}": _str(v) for k, v in row.items() if not k.startswith(":@")}
    out.update({
        # Hot columns.
        "complaint_number":               _str(row.get("complaint_number")),
        "dobrundate":                     _str(row.get("dobrundate")),
        "borough_name":                   _str(borough_name),
        "community_board":                _str(row.get("community_board")),
        "bin":                            _str(row.get("bin")),
        "house_number":                   _str(row.get("house_number")),
        "street_name":                    _str(row.get("street_name")),
        "date_complaint_received":        _str(row.get("date_complaint_received")),
        # Normalized columns.
        "borough_normalized":             normalize_borough_code(borough_name),
        "bbl_normalized":                 None,
        "property_zip5":                  None,
        "owner_name_normalized":          None,
        "stall_reason_normalized":        (
            _str(row.get("reason_for_stalled_status")).upper()
            if _str(row.get("reason_for_stalled_status")) else None
        ),
    })
    return out


PROJECTORS = {
    "approved_permits": _project_approved_permits,
    "job_application_filings": _project_job_application_filings,
    "stalled_construction_sites": _project_stalled_construction_sites,
}


# --------------------------------------------------------------------------- #
# Parquet writer.
# --------------------------------------------------------------------------- #


def project_rows(
    stream: StreamSpec, raw_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    proj = PROJECTORS[stream.name]
    return [proj(r) for r in raw_rows]


def _build_table(
    rows: list[dict[str, Any]], snapshot_date: date, stream_name: str,
) -> pa.Table:
    """Build a pyarrow Table with a stable column union across rows.

    `borough_normalized` is typed int8; everything else is string.
    `snapshot_date` and `nyc_dob_now_stream` are appended to every row.
    """
    if not rows:
        return pa.table({
            "snapshot_date": pa.array([], type=pa.date32()),
            "nyc_dob_now_stream": pa.array([], type=pa.string()),
        })

    seen: dict[str, None] = {}
    for r in rows:
        for k in r.keys():
            seen.setdefault(k, None)
    columns = list(seen.keys())

    arrays: list[pa.Array] = []
    field_types: list[pa.Field] = []
    for col in columns:
        values = [r.get(col) for r in rows]
        if col == "borough_normalized":
            arr = pa.array(values, type=pa.int8())
        else:
            arr = pa.array(
                [None if v is None else str(v) for v in values],
                type=pa.string(),
            )
        arrays.append(arr)
        field_types.append(pa.field(col, arr.type))

    arrays.append(pa.array([snapshot_date] * len(rows), type=pa.date32()))
    field_types.append(pa.field("snapshot_date", pa.date32()))
    arrays.append(pa.array([stream_name] * len(rows), type=pa.string()))
    field_types.append(pa.field("nyc_dob_now_stream", pa.string()))

    return pa.Table.from_arrays(arrays, schema=pa.schema(field_types))


def write_parquet(
    rows: list[dict[str, Any]], *,
    snapshot_date: date, stream_name: str, out_path: Path,
) -> tuple[int, int]:
    table = _build_table(rows, snapshot_date, stream_name)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        table, out_path,
        compression="zstd", compression_level=9,
        row_group_size=50_000,
    )
    return table.num_rows, out_path.stat().st_size


# --------------------------------------------------------------------------- #
# R2 upload.
# --------------------------------------------------------------------------- #


def upload_to_r2(
    parquet_path: Path, *, bucket: str, key: str, log_prefix: str,
) -> int:
    s3 = _r2_client()
    file_bytes = parquet_path.stat().st_size
    log.info(
        "%s uploading %s (%.1f MB) → s3://%s/%s",
        log_prefix, parquet_path, file_bytes / (1 << 20), bucket, key,
    )
    s3.upload_file(
        str(parquet_path), bucket, key,
        ExtraArgs={"ContentType": "application/x-parquet"},
    )
    log.info("%s upload done", log_prefix)
    return file_bytes


# --------------------------------------------------------------------------- #
# Derived `active_firms` aggregation — DuckDB GROUP BY against the freshly-
# written approved_permits + job_application_filings parquets.
# --------------------------------------------------------------------------- #


def build_active_firms_parquet(
    *,
    approved_permits_path: Path,
    job_application_filings_path: Path,
    out_path: Path,
    snapshot_date: date,
) -> tuple[int, int]:
    """Compute the per-firm rollup and write to Parquet.

    Output schema (per directive):
      - applicant_business_name_normalized (PK)
      - applicant_license_number_normalized
      - applicant_license_kind_normalized
      - count_approved_permits_lifetime
      - count_job_applications_lifetime
      - latest_filing_date
      - earliest_filing_date
      - nyc_dob_now_snapshot_date
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(":memory:")
    try:
        # The two source parquets carry different filing-date columns.
        # approved_permits → issued_date (or approved_date as fallback)
        # job_application_filings → filing_date
        # We TRY_CAST those to DATE for safe MIN/MAX.
        con.execute(
            f"""
            CREATE OR REPLACE VIEW approved_permits_v AS
              SELECT
                applicant_business_name_normalized AS firm,
                applicant_license_number_normalized AS license_number,
                applicant_license_kind_normalized AS license_kind,
                TRY_CAST(COALESCE(issued_date, approved_date) AS DATE) AS filing_date
              FROM read_parquet('{approved_permits_path.as_posix()}')
              WHERE applicant_business_name_normalized IS NOT NULL;

            CREATE OR REPLACE VIEW job_applications_v AS
              SELECT
                applicant_business_name_normalized AS firm,
                applicant_license_number_normalized AS license_number,
                applicant_license_kind_normalized AS license_kind,
                TRY_CAST(filing_date AS DATE) AS filing_date
              FROM read_parquet('{job_application_filings_path.as_posix()}')
              WHERE applicant_business_name_normalized IS NOT NULL;
            """
        )

        # Group by (firm, license_number) to deduplicate; preserve any non-
        # null license_kind. NB: ANY_VALUE for license_kind is acceptable
        # because for a given (firm, license_number) the kind should be
        # stable per filing.
        con.execute(
            f"""
            CREATE OR REPLACE TABLE active_firms_t AS
              WITH unioned AS (
                SELECT firm, license_number, license_kind, filing_date,
                       'approved'::TEXT AS source FROM approved_permits_v
                UNION ALL
                SELECT firm, license_number, license_kind, filing_date,
                       'application'::TEXT AS source FROM job_applications_v
              )
              SELECT
                firm AS applicant_business_name_normalized,
                license_number AS applicant_license_number_normalized,
                ANY_VALUE(license_kind) AS applicant_license_kind_normalized,
                SUM(CASE WHEN source = 'approved' THEN 1 ELSE 0 END) AS count_approved_permits_lifetime,
                SUM(CASE WHEN source = 'application' THEN 1 ELSE 0 END) AS count_job_applications_lifetime,
                MAX(filing_date) AS latest_filing_date,
                MIN(filing_date) AS earliest_filing_date,
                CAST('{snapshot_date.isoformat()}' AS DATE) AS nyc_dob_now_snapshot_date
              FROM unioned
              GROUP BY firm, license_number;
            """
        )

        # Write Parquet with ZSTD compression. DuckDB handles row-group
        # sizing internally; default 122,880 → adequate for ~50K firms.
        con.execute(
            f"""
            COPY active_firms_t TO '{out_path.as_posix()}'
              (FORMAT 'parquet', COMPRESSION 'zstd', ROW_GROUP_SIZE 50000);
            """
        )
        rows_written = con.execute(
            "SELECT COUNT(*) FROM active_firms_t"
        ).fetchone()[0]
    finally:
        con.close()
    return int(rows_written), out_path.stat().st_size


# --------------------------------------------------------------------------- #
# Per-stream orchestration.
# --------------------------------------------------------------------------- #


def _classify_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPError):
        return "download_failure"
    if isinstance(exc, psycopg.Error):
        return "db_failure"
    if isinstance(exc, (TimeoutError, OSError)):
        return "timeout"
    return "unknown"


def _validation_floor(stream_name: str) -> int:
    return {
        "approved_permits":             800_000,
        "job_application_filings":      800_000,
        "stalled_construction_sites": 1_200_000,
        "active_firms":                  30_000,
    }[stream_name]


@dataclass
class StreamResult:
    stream: str
    rows: int
    parquet_bytes: int
    parquet_local_path: Path | None
    r2_key: str | None
    duration_s: float
    status: str
    error: str | None


def run_one_soda_stream(
    stream: StreamSpec, *,
    snapshot_date: date,
    r2_prefix: str,
    workdir: Path,
    page_size: int,
    page_sleep: float,
    max_rows: int | None,
    dry_run: bool,
    skip_db: bool,
    skip_upload: bool,
) -> StreamResult:
    started = time.monotonic()
    log.info(
        "[%s] starting (snapshot=%s dataset=%s)",
        stream.name, snapshot_date, stream.socrata_id,
    )
    pull = pull_dataset(
        stream, page_size=page_size, page_sleep=page_sleep, max_rows=max_rows,
    )
    log.info(
        "[%s] SODA pull complete — pages=%d rows=%d bytes=%.1f MB",
        stream.name, pull.pages_fetched, len(pull.rows),
        pull.bytes_downloaded / (1 << 20),
    )

    if dry_run:
        log.info("[%s] DRY RUN — sample row: %s", stream.name,
                 pull.rows[0] if pull.rows else "(none)")
        return StreamResult(
            stream=stream.name, rows=len(pull.rows), parquet_bytes=0,
            parquet_local_path=None, r2_key=None,
            duration_s=time.monotonic() - started,
            status="dry_run", error=None,
        )

    projected = project_rows(stream, pull.rows)
    parquet_path = workdir / f"{stream.name}.parquet"
    rows_written, parquet_bytes = write_parquet(
        projected, snapshot_date=snapshot_date,
        stream_name=stream.name, out_path=parquet_path,
    )
    log.info(
        "[%s] wrote %s — rows=%d bytes=%.1f MB",
        stream.name, parquet_path, rows_written, parquet_bytes / (1 << 20),
    )

    r2_key = r2_prefix + f"{stream.name}/snapshot={snapshot_date.isoformat()}/data.parquet"
    if skip_upload:
        uploaded = 0
        log.info("[%s] --skip-upload — no R2 write", stream.name)
    else:
        uploaded = upload_to_r2(
            parquet_path, bucket=R2_BUCKET, key=r2_key,
            log_prefix=f"[{stream.name}]",
        )

    if not skip_db:
        with psycopg.connect(_database_url()) as conn:
            rr = insert_run_row(
                conn,
                snapshot_date=snapshot_date, stream=stream.name,
                source_url=stream.soda_url(),
                socrata_dataset_id=stream.socrata_id,
                source_last_modified=pull.source_last_modified,
                num_found_at_run_start=pull.num_found_at_run_start,
                started_monotonic=started,
            )
            try:
                finalize_run_row(
                    conn, rr, status="completed",
                    pages_fetched=pull.pages_fetched,
                    bytes_downloaded=pull.bytes_downloaded,
                    parquet_bytes_written=parquet_bytes,
                    parquet_row_count=rows_written,
                    r2_bucket=R2_BUCKET, r2_prefix=r2_prefix, r2_key=r2_key,
                    r2_object_bytes=uploaded,
                    num_found_at_run_end=pull.num_found_at_run_end,
                    error_message=None, error_class=None,
                    notes={
                        "dataset_name": stream.dataset_name,
                        "max_rows": max_rows,
                        "page_size": page_size,
                    },
                )
            except Exception as exc:
                log.exception("[%s] failed to finalize run row", stream.name)
                finalize_run_row(
                    conn, rr, status="failed",
                    pages_fetched=pull.pages_fetched,
                    bytes_downloaded=pull.bytes_downloaded,
                    parquet_bytes_written=parquet_bytes,
                    parquet_row_count=rows_written,
                    r2_bucket=R2_BUCKET, r2_prefix=r2_prefix, r2_key=r2_key,
                    r2_object_bytes=uploaded,
                    num_found_at_run_end=pull.num_found_at_run_end,
                    error_message=str(exc), error_class=_classify_error(exc),
                    notes=None,
                )
                raise

    return StreamResult(
        stream=stream.name, rows=rows_written, parquet_bytes=parquet_bytes,
        parquet_local_path=parquet_path,
        r2_key=r2_key,
        duration_s=time.monotonic() - started,
        status="completed", error=None,
    )


def run_active_firms_stream(
    *,
    snapshot_date: date,
    r2_prefix: str,
    workdir: Path,
    approved_permits_result: StreamResult,
    job_application_filings_result: StreamResult,
    skip_db: bool,
    skip_upload: bool,
) -> StreamResult:
    started = time.monotonic()
    log.info("[active_firms] starting derived aggregation")

    if (approved_permits_result.parquet_local_path is None or
            job_application_filings_result.parquet_local_path is None):
        msg = (
            "active_firms requires both approved_permits and "
            "job_application_filings parquets on disk — got "
            f"approved={approved_permits_result.parquet_local_path}, "
            f"job_app={job_application_filings_result.parquet_local_path}"
        )
        log.error("[active_firms] %s", msg)
        return StreamResult(
            stream="active_firms", rows=0, parquet_bytes=0,
            parquet_local_path=None, r2_key=None,
            duration_s=time.monotonic() - started,
            status="failed", error=msg,
        )

    parquet_path = workdir / "active_firms.parquet"
    rows_written, parquet_bytes = build_active_firms_parquet(
        approved_permits_path=approved_permits_result.parquet_local_path,
        job_application_filings_path=job_application_filings_result.parquet_local_path,
        out_path=parquet_path,
        snapshot_date=snapshot_date,
    )
    log.info(
        "[active_firms] wrote %s — rows=%d bytes=%.1f MB",
        parquet_path, rows_written, parquet_bytes / (1 << 20),
    )

    r2_key = r2_prefix + f"active_firms/snapshot={snapshot_date.isoformat()}/data.parquet"
    if skip_upload:
        uploaded = 0
        log.info("[active_firms] --skip-upload — no R2 write")
    else:
        uploaded = upload_to_r2(
            parquet_path, bucket=R2_BUCKET, key=r2_key,
            log_prefix="[active_firms]",
        )

    if not skip_db:
        with psycopg.connect(_database_url()) as conn:
            rr = insert_run_row(
                conn,
                snapshot_date=snapshot_date, stream="active_firms",
                source_url=None, socrata_dataset_id=None,
                source_last_modified=None,
                num_found_at_run_start=None,
                started_monotonic=started,
            )
            finalize_run_row(
                conn, rr, status="completed",
                pages_fetched=None, bytes_downloaded=None,
                parquet_bytes_written=parquet_bytes,
                parquet_row_count=rows_written,
                r2_bucket=R2_BUCKET, r2_prefix=r2_prefix, r2_key=r2_key,
                r2_object_bytes=uploaded,
                num_found_at_run_end=None,
                error_message=None, error_class=None,
                notes={
                    "derived_from": [
                        "approved_permits",
                        "job_application_filings",
                    ],
                    "approved_permits_rows": approved_permits_result.rows,
                    "job_application_filings_rows": job_application_filings_result.rows,
                },
            )

    return StreamResult(
        stream="active_firms", rows=rows_written, parquet_bytes=parquet_bytes,
        parquet_local_path=parquet_path,
        r2_key=r2_key,
        duration_s=time.monotonic() - started,
        status="completed", error=None,
    )


# --------------------------------------------------------------------------- #
# Validation gate.
# --------------------------------------------------------------------------- #


def validate_results(
    results: list[StreamResult], *, max_rows: int | None,
) -> tuple[bool, list[str]]:
    failures: list[str] = []
    if max_rows is not None:
        return True, failures
    for r in results:
        if r.status != "completed":
            failures.append(f"{r.stream}: status={r.status} error={r.error}")
            continue
        floor = _validation_floor(r.stream)
        if r.rows < floor:
            failures.append(
                f"{r.stream}: row_count={r.rows} below floor={floor}",
            )
    return len(failures) == 0, failures


# --------------------------------------------------------------------------- #
# Main.
# --------------------------------------------------------------------------- #


@dataclass
class IngestArgs:
    page_size: int
    page_sleep: float
    max_rows: int | None
    dry_run: bool
    workdir: Path
    r2_prefix_override: str | None
    streams: tuple[StreamSpec, ...]
    skip_db: bool
    skip_upload: bool


def run_ingest(args: IngestArgs) -> int:
    snapshot_date = datetime.now(timezone.utc).date()
    r2_prefix = args.r2_prefix_override or "nyc-dob-now/"
    if not r2_prefix.endswith("/"):
        r2_prefix += "/"
    log.info(
        "starting NYC DOB Now R2 ingest snapshot=%s prefix=%s streams=%s dry_run=%s",
        snapshot_date, r2_prefix, [s.name for s in args.streams], args.dry_run,
    )

    results: dict[str, StreamResult] = {}
    rc = 0

    # Phase 1 — SODA streams (skip the derived 'active_firms' here; it
    # depends on the approved_permits + job_application_filings parquets).
    for stream in args.streams:
        if stream.name == "active_firms":
            continue
        try:
            res = run_one_soda_stream(
                stream,
                snapshot_date=snapshot_date,
                r2_prefix=r2_prefix,
                workdir=args.workdir,
                page_size=args.page_size,
                page_sleep=args.page_sleep,
                max_rows=args.max_rows,
                dry_run=args.dry_run,
                skip_db=args.skip_db,
                skip_upload=args.skip_upload,
            )
            results[stream.name] = res
            log.info(
                "[%s] DONE rows=%d bytes=%.1f MB duration=%.1fs",
                res.stream, res.rows, res.parquet_bytes / (1 << 20), res.duration_s,
            )
        except Exception as exc:
            log.exception("[%s] failed", stream.name)
            results[stream.name] = StreamResult(
                stream=stream.name, rows=0, parquet_bytes=0,
                parquet_local_path=None, r2_key=None,
                duration_s=0.0, status="failed", error=str(exc),
            )
            rc = 1

    # Phase 2 — derived active_firms aggregation. Only run if both source
    # parquets exist on disk (i.e. their respective SODA pulls succeeded
    # AND the user asked for active_firms in the stream filter).
    if any(s.name == "active_firms" for s in args.streams) and not args.dry_run:
        ap = results.get("approved_permits")
        ja = results.get("job_application_filings")
        if (ap is not None and ja is not None and
                ap.status == "completed" and ja.status == "completed"):
            try:
                af_res = run_active_firms_stream(
                    snapshot_date=snapshot_date,
                    r2_prefix=r2_prefix,
                    workdir=args.workdir,
                    approved_permits_result=ap,
                    job_application_filings_result=ja,
                    skip_db=args.skip_db,
                    skip_upload=args.skip_upload,
                )
                results["active_firms"] = af_res
                log.info(
                    "[active_firms] DONE rows=%d bytes=%.1f MB duration=%.1fs",
                    af_res.rows, af_res.parquet_bytes / (1 << 20),
                    af_res.duration_s,
                )
            except Exception as exc:
                log.exception("[active_firms] failed")
                results["active_firms"] = StreamResult(
                    stream="active_firms", rows=0, parquet_bytes=0,
                    parquet_local_path=None, r2_key=None,
                    duration_s=0.0, status="failed", error=str(exc),
                )
                rc = 1
        else:
            log.warning(
                "[active_firms] skipping — approved_permits or "
                "job_application_filings did not complete"
            )
            results["active_firms"] = StreamResult(
                stream="active_firms", rows=0, parquet_bytes=0,
                parquet_local_path=None, r2_key=None,
                duration_s=0.0, status="failed",
                error="upstream stream(s) did not complete",
            )
            rc = 1

    # Cleanup local parquets.
    for r in results.values():
        if r.parquet_local_path is not None:
            try:
                r.parquet_local_path.unlink(missing_ok=True)
            except Exception:
                pass

    if args.dry_run:
        log.info("DRY RUN — skipping validation gate")
        return rc

    selected_results = [
        results[s.name] for s in args.streams if s.name in results
    ]
    passed, failures = validate_results(selected_results, max_rows=args.max_rows)
    if passed:
        log.info("validation gate PASSED — %d streams completed", len(selected_results))
    else:
        log.error("validation gate FAILED:")
        for f in failures:
            log.error("  - %s", f)
        rc = max(rc, 1)

    log.info(
        "INGEST SUMMARY snapshot=%s prefix=%s rc=%d\n%s",
        snapshot_date, r2_prefix, rc,
        json.dumps(
            {r.stream: {
                "rows": r.rows,
                "bytes": r.parquet_bytes,
                "status": r.status,
                "duration_s": round(r.duration_s, 2),
                "r2_key": r.r2_key,
            } for r in selected_results},
            indent=2,
        ),
    )
    return rc


def parse_args(argv: list[str] | None = None) -> IngestArgs:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    p.add_argument("--page-sleep", type=float, default=DEFAULT_PAGE_SLEEP)
    p.add_argument(
        "--max-rows", type=int, default=None,
        help="Cap rows pulled per SODA stream — useful for smoke tests",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Pull SODA, project, but write no Parquet / no R2 / no DB rows",
    )
    p.add_argument(
        "--workdir", default=None,
        help="Local Parquet scratch dir (default: /tmp/nyc_dob_now_r2)",
    )
    p.add_argument(
        "--r2-prefix-override", default=None,
        help="Override the nyc-dob-now/ prefix (e.g. for smoke runs)",
    )
    p.add_argument(
        "--stream", action="append", choices=[s.name for s in ALL_STREAMS],
        help="Only ingest the named stream(s). Repeatable. Default: all 4.",
    )
    p.add_argument(
        "--skip-db", action="store_true",
        help="Skip audit ledger writes (smoke / dev only)",
    )
    p.add_argument(
        "--skip-upload", action="store_true",
        help="Skip R2 uploads (local-only smoke runs)",
    )
    a = p.parse_args(argv)
    workdir = Path(a.workdir or "/tmp/nyc_dob_now_r2")
    workdir.mkdir(parents=True, exist_ok=True)
    if a.stream:
        streams = tuple(s for s in ALL_STREAMS if s.name in a.stream)
    else:
        streams = ALL_STREAMS
    return IngestArgs(
        page_size=a.page_size, page_sleep=a.page_sleep,
        max_rows=a.max_rows, dry_run=a.dry_run,
        workdir=workdir, r2_prefix_override=a.r2_prefix_override,
        streams=streams, skip_db=a.skip_db, skip_upload=a.skip_upload,
    )


def main(argv: list[str] | None = None) -> int:
    return run_ingest(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())

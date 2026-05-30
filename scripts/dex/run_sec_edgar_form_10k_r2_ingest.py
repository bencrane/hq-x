#!/usr/bin/env python3
"""SEC EDGAR Form 10-K → R2 Parquet ingest.

For each year in the configured span:
  1. Pull the EDGAR full-index ``form.idx`` for the year and filter to the
     six Form 10-K variants
     (10-K, 10-K/A, 10-KSB, 10-KSB/A, 10-K405, 10-K405/A).
  2. Concurrently fetch + parse each filing's primary HTML doc
     (asyncio + 2 RPS rate-limit when running parallel with sibling EDGAR
     ingests, ~16 concurrent workers).
  3. Buffer parsed records into 7 streams (filings, officers_directors,
     executive_compensation, security_ownership, properties,
     legal_proceedings, risk_factors) and emit ZSTD Parquet at:
        s3://dex-raw-landing-zone/sec-edgar/form-10k/year={Y}/{stream}/data.parquet
  4. Optionally also upload raw filing HTML at:
        s3://dex-raw-landing-zone/sec-edgar/form-10k/raw/{cik}/{accession}/primary.html

Idempotency: per (year, accession) — the orchestrator keeps a JSON checkpoint
file at ``--state-file`` so a network blip or session timeout doesn't restart
from zero. Re-running with the same state file resumes where it left off.

Audit: one row per (year, stream) in ops.sec_edgar_form_10k_r2_ingest_runs.

⚠ Rate-limit: this script may run concurrently with the six sibling SEC
EDGAR per-filing ingests (Schedule 13D/G + Form 144 + Form 13F + DEF 14A
+ Form ABS-15G + Form 8-K). When all seven scripts share an egress IP,
SEC fair-use caps aggregate request rate at ≤10 RPS across every
*.sec.gov URL. The default TARGET_RPS=1 keeps the 7-script aggregate at
~7 RPS with margin (set per parent /scope cycle
``~/Desktop/hq/directives/2026-05-12-sec-edgar-credit-facility-feeds.md``).
Override to 5 with ``--target-rps 5`` only when running solo OR from a
Modal container with an independent egress IP (the Modal app
``modal/sec_edgar_form_10k_app.py`` sets ``SEC_EDGAR_TARGET_RPS=5`` for
this reason).

Usage:
  doppler run --project hq-all --config prd --command 'bash -c "
    uv run python3 scripts/run_sec_edgar_form_10k_r2_ingest.py \\
      2024 --max-filings 1000
  "'
  doppler run --project hq-all --config prd --command 'bash -c "
    uv run python3 scripts/run_sec_edgar_form_10k_r2_ingest.py \\
      --years 1994-2026
  "'
  doppler run --project hq-all --config prd --command 'bash -c "
    uv run python3 scripts/run_sec_edgar_form_10k_r2_ingest.py \\
      --years 1994-2026 --include-raw-html
  "'
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import os
import re
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
import httpx
import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).parent))
from _lib.sec_edgar_form_10k_parser import FilingHeader, parse_filing  # noqa: E402


# ------------------------------------------------------------------ #
# Constants
# ------------------------------------------------------------------ #

R2_BUCKET = "dex-raw-landing-zone"
PROVIDER = "sec_edgar_form_10k"

EDGAR_HOST = "https://www.sec.gov"
FORM_IDX_URL = (
    "https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{q}/form.idx"
)

# Default TARGET_RPS=1 because this script may coexist with up to six sibling
# SEC EDGAR per-filing ingests (13D/G, 144, 13F, DEF 14A, ABS-15G, 8-K). 7
# scripts × 1 RPS = 7 RPS aggregate, comfortably under the SEC fair-use ≤10
# RPS cap. Override via --target-rps 5 when running solo or from a Modal
# container with an independent egress IP (see modal/sec_edgar_form_10k_app.py
# which sets SEC_EDGAR_TARGET_RPS=5 for that reason).
# Set per parent /scope cycle 2026-05-12-sec-edgar-credit-facility-feeds.md.
DEFAULT_TARGET_RPS = 1
HTTP_CONCURRENCY = 16

RECORDS_LOG_EVERY = 250

USER_AGENT = (
    "data-engine-x/sec-edgar-form-10k-ingest "
    "tools@substrate.build "
    "(operational research)"
)

# Six Form 10-K variants per directive (modern + amendment + small-business +
# pre-2003 Section-16-delinquency variants).
FORM_TYPES_10K: frozenset[str] = frozenset({
    "10-K",
    "10-K/A",
    "10-KSB",
    "10-KSB/A",
    "10-K405",
    "10-K405/A",
})

STREAMS: tuple[str, ...] = (
    "filings",
    "officers_directors",
    "executive_compensation",
    "security_ownership",
    "properties",
    "legal_proceedings",
    "risk_factors",
)


# ------------------------------------------------------------------ #
# Logging
# ------------------------------------------------------------------ #

def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("sec-edgar-form-10k-ingest")


log = _logger()


# ------------------------------------------------------------------ #
# Env helpers
# ------------------------------------------------------------------ #

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


def _database_url() -> str | None:
    return os.environ.get("DEX_DB_URL_POOLED") or os.environ.get("DEX_DB_URL_DIRECT")


# ------------------------------------------------------------------ #
# Form index parser
# ------------------------------------------------------------------ #


@dataclass(frozen=True)
class FormIdxRow:
    """Single row from EDGAR form.idx — fixed-width text format."""

    form_type: str
    company_name: str
    cik: str
    date_filed: str
    filename: str

    @property
    def primary_url(self) -> str:
        return f"{EDGAR_HOST}/Archives/{self.filename}"

    @property
    def accession(self) -> str:
        m = re.search(r"(\d{10}-\d{2}-\d{6})", self.filename)
        return m.group(1) if m else ""


def parse_form_idx(
    text: str, target_forms: frozenset[str] = FORM_TYPES_10K,
) -> list[FormIdxRow]:
    """Parse form.idx body into typed rows for any of ``target_forms``.

    Filters by *exact-match* against ``target_forms`` so the substring
    overlap of e.g. ``10-K`` against ``10-KSB`` doesn't pull in unrelated
    rows when the caller passes only ``{"10-K"}``.
    """
    out: list[FormIdxRow] = []
    seen_dash_line = False
    for line in text.splitlines():
        if not seen_dash_line:
            if line.startswith("---"):
                seen_dash_line = True
            continue
        if not line.strip():
            continue
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) < 5:
            continue
        form_type = parts[0].strip()
        if form_type not in target_forms:
            continue
        if len(parts) > 5:
            company = "  ".join(parts[1:-3]).strip()
            cik = parts[-3].strip()
            date_filed = parts[-2].strip()
            filename = parts[-1].strip()
        else:
            company, cik, date_filed, filename = (
                parts[1].strip(), parts[2].strip(),
                parts[3].strip(), parts[4].strip(),
            )
        out.append(FormIdxRow(
            form_type=form_type, company_name=company,
            cik=cik, date_filed=date_filed, filename=filename,
        ))
    return out


# ------------------------------------------------------------------ #
# HTTP rate-limited fetcher
# ------------------------------------------------------------------ #


class RpsLimiter:
    """Simple token-bucket: limit to ``rps`` calls per second."""

    def __init__(self, rps: int):
        self.rps = rps
        self.min_interval = 1.0 / rps if rps > 0 else 0.0
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._last + self.min_interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()


async def fetch_text(
    client: httpx.AsyncClient, url: str, *, limiter: RpsLimiter,
    timeout: float = 60.0, retries: int = 6,
) -> tuple[int, bytes]:
    """Fetch a URL with token-bucket pacing + exp-backoff retry on 429/5xx.

    Default ``retries=6`` chosen so total backoff (2+4+8+16+30+30s ≈ 90s)
    exceeds the typical SEC fair-use throttle burst window when running
    parallel with sibling EDGAR ingests.
    """
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        await limiter.wait()
        try:
            r = await client.get(url, timeout=timeout, follow_redirects=True)
            if r.status_code in (429, 500, 502, 503, 504):
                wait = min(2 ** attempt, 30)
                log.warning("HTTP %s %s; retry in %ss",
                            r.status_code, url, wait)
                await asyncio.sleep(wait)
                continue
            return r.status_code, r.content
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            last_exc = exc
            wait = min(2 ** attempt, 30)
            log.warning("fetch %s failed (%s); retry in %ss",
                        url, exc, wait)
            await asyncio.sleep(wait)
    raise RuntimeError(f"fetch {url} exhausted retries: {last_exc}")


# ------------------------------------------------------------------ #
# Per-filing pipeline (resolve primary HTML → parse)
# ------------------------------------------------------------------ #


async def fetch_filing_index_json(
    client: httpx.AsyncClient, idx_row: FormIdxRow, *, limiter: RpsLimiter,
) -> dict[str, Any] | None:
    cik_int = idx_row.cik.lstrip("0") or "0"
    acc = idx_row.accession.replace("-", "")
    if not acc:
        return None
    url = f"{EDGAR_HOST}/Archives/edgar/data/{cik_int}/{acc}/index.json"
    try:
        status, body = await fetch_text(client, url, limiter=limiter)
    except RuntimeError:
        return None
    if status != 200:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def _select_primary_doc(
    idx_json: dict[str, Any], target_forms: frozenset[str] = FORM_TYPES_10K,
) -> str | None:
    """From an EDGAR index.json choose the primary HTML doc.

    Heuristic:
      1. Prefer items where ``type`` is one of the target form types.
      2. Of those, choose the one with .htm/.html extension.
      3. Fallback: largest .htm/.html in the directory.
    """
    items = idx_json.get("directory", {}).get("item", [])
    if not items:
        return None
    candidates: list[tuple[int, str]] = []
    target_upper = {t.upper() for t in target_forms}
    for it in items:
        name = it.get("name", "")
        size = int(it.get("size") or 0)
        type_ = (it.get("type") or "").upper()
        if not name.lower().endswith((".htm", ".html")):
            continue
        if type_ in target_upper:
            return name
        candidates.append((size, name))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


async def fetch_filing_primary_html(
    client: httpx.AsyncClient, idx_row: FormIdxRow, *, limiter: RpsLimiter,
) -> tuple[str | None, bytes | None]:
    idx_json = await fetch_filing_index_json(client, idx_row, limiter=limiter)
    if idx_json is None:
        return None, None
    primary_name = _select_primary_doc(idx_json)
    if primary_name is None:
        return None, None
    cik_int = idx_row.cik.lstrip("0") or "0"
    acc = idx_row.accession.replace("-", "")
    primary_url = (
        f"{EDGAR_HOST}/Archives/edgar/data/{cik_int}/{acc}/{primary_name}"
    )
    try:
        status, body = await fetch_text(
            client, primary_url, limiter=limiter, timeout=180.0,
        )
    except RuntimeError:
        return primary_url, None
    if status != 200:
        return primary_url, None
    return primary_url, body


# ------------------------------------------------------------------ #
# pyarrow schemas
# ------------------------------------------------------------------ #


_FILINGS_SCHEMA = pa.schema([
    pa.field("accession_number", pa.string()),
    pa.field("cik_normalized", pa.string()),
    pa.field("filer_legal_name_normalized", pa.string()),
    pa.field("filer_legal_name_raw", pa.string()),
    pa.field("filer_lei_normalized", pa.string()),
    pa.field("filer_ein_normalized", pa.string()),
    pa.field("form_type", pa.string()),
    pa.field("is_amendment", pa.bool_()),
    pa.field("original_accession_number", pa.string()),
    pa.field("filing_date", pa.string()),
    pa.field("period_of_report", pa.string()),
    pa.field("fiscal_year_end", pa.string()),
    pa.field("form_10k_year", pa.int16()),
    pa.field("primary_doc_url", pa.string()),
    pa.field("raw_html_r2_uri", pa.string()),
])

_OFFICERS_DIRECTORS_SCHEMA = pa.schema([
    pa.field("accession_number", pa.string()),
    pa.field("cik_normalized", pa.string()),
    pa.field("person_seq", pa.int32()),
    pa.field("person_first_name", pa.string()),
    pa.field("person_last_name", pa.string()),
    pa.field("person_first_normalized", pa.string()),
    pa.field("person_last_normalized", pa.string()),
    pa.field("person_title_raw", pa.string()),
    pa.field("person_title_normalized", pa.string()),
    pa.field("person_age", pa.int16()),
    pa.field("person_role_type", pa.string()),
    pa.field("is_independent_director", pa.bool_()),
    pa.field("biography_text_raw", pa.string()),
    pa.field("form_10k_year", pa.int16()),
])

_EXECUTIVE_COMPENSATION_SCHEMA = pa.schema([
    pa.field("accession_number", pa.string()),
    pa.field("cik_normalized", pa.string()),
    pa.field("person_first_normalized", pa.string()),
    pa.field("person_last_normalized", pa.string()),
    pa.field("compensation_year", pa.int16()),
    pa.field("comp_base_salary", pa.float64()),
    pa.field("comp_bonus", pa.float64()),
    pa.field("comp_stock_awards", pa.float64()),
    pa.field("comp_option_awards", pa.float64()),
    pa.field("comp_non_equity_incentive", pa.float64()),
    pa.field("comp_pension_change", pa.float64()),
    pa.field("comp_all_other", pa.float64()),
    pa.field("comp_total", pa.float64()),
    pa.field("form_10k_year", pa.int16()),
])

_SECURITY_OWNERSHIP_SCHEMA = pa.schema([
    pa.field("accession_number", pa.string()),
    pa.field("cik_normalized", pa.string()),
    pa.field("holder_name_raw", pa.string()),
    pa.field("holder_name_normalized", pa.string()),
    pa.field("holder_type", pa.string()),
    pa.field("person_first_normalized", pa.string()),
    pa.field("person_last_normalized", pa.string()),
    pa.field("security_class", pa.string()),
    pa.field("shares_beneficially_owned", pa.int64()),
    pa.field("percent_of_class", pa.float64()),
    pa.field("form_10k_year", pa.int16()),
])

_PROPERTIES_SCHEMA = pa.schema([
    pa.field("accession_number", pa.string()),
    pa.field("cik_normalized", pa.string()),
    pa.field("property_seq", pa.int32()),
    pa.field("property_description_raw", pa.string()),
    pa.field("property_city", pa.string()),
    pa.field("property_state", pa.string()),
    pa.field("property_country", pa.string()),
    pa.field("property_size_sqft", pa.int64()),
    pa.field("property_owned_or_leased", pa.string()),
    pa.field("property_use", pa.string()),
    pa.field("form_10k_year", pa.int16()),
])

_LEGAL_PROCEEDINGS_SCHEMA = pa.schema([
    pa.field("accession_number", pa.string()),
    pa.field("cik_normalized", pa.string()),
    pa.field("proceeding_seq", pa.int32()),
    pa.field("proceeding_text_raw", pa.string()),
    pa.field("proceeding_caption", pa.string()),
    pa.field("proceeding_jurisdiction", pa.string()),
    pa.field("proceeding_filed_date", pa.string()),
    pa.field("proceeding_status", pa.string()),
    pa.field("proceeding_named_persons", pa.string()),
    pa.field("form_10k_year", pa.int16()),
])

_RISK_FACTORS_SCHEMA = pa.schema([
    pa.field("accession_number", pa.string()),
    pa.field("cik_normalized", pa.string()),
    pa.field("risk_factor_seq", pa.int32()),
    pa.field("risk_factor_heading", pa.string()),
    pa.field("risk_factor_text_raw", pa.string()),
    pa.field("form_10k_year", pa.int16()),
])

_STREAM_SCHEMAS: dict[str, pa.Schema] = {
    "filings": _FILINGS_SCHEMA,
    "officers_directors": _OFFICERS_DIRECTORS_SCHEMA,
    "executive_compensation": _EXECUTIVE_COMPENSATION_SCHEMA,
    "security_ownership": _SECURITY_OWNERSHIP_SCHEMA,
    "properties": _PROPERTIES_SCHEMA,
    "legal_proceedings": _LEGAL_PROCEEDINGS_SCHEMA,
    "risk_factors": _RISK_FACTORS_SCHEMA,
}


def _records_to_table(records: list[dict[str, Any]], schema: pa.Schema) -> pa.Table:
    cols: dict[str, list[Any]] = {f.name: [] for f in schema}
    for rec in records:
        for f in schema:
            cols[f.name].append(rec.get(f.name))
    arrays = []
    for f in schema:
        arrays.append(pa.array(cols[f.name], type=f.type))
    return pa.Table.from_arrays(arrays, schema=schema)


def write_parquet_zstd(records: list[dict[str, Any]], schema: pa.Schema) -> bytes:
    tbl = _records_to_table(records, schema)
    buf = io.BytesIO()
    pq.write_table(tbl, buf, compression="zstd", compression_level=3)
    return buf.getvalue()


# ------------------------------------------------------------------ #
# R2 upload
# ------------------------------------------------------------------ #


def upload_bytes_to_r2(
    s3, *, bucket: str, key: str, body: bytes,
    content_type: str = "application/x-parquet",
) -> int:
    s3.put_object(
        Bucket=bucket, Key=key, Body=body, ContentType=content_type,
    )
    return len(body)


# ------------------------------------------------------------------ #
# Audit-row helpers
# ------------------------------------------------------------------ #


def insert_run_row(
    conn: psycopg.Connection, *,
    year: int, stream: str, source_url: str | None = None,
) -> str:
    sql = """
    INSERT INTO ops.sec_edgar_form_10k_r2_ingest_runs
      (year, stream, status, source_url)
    VALUES (%s, %s, 'running', %s)
    RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (year, stream, source_url))
        row = cur.fetchone()[0]
    conn.commit()
    return str(row)


def finalize_run_row(
    conn: psycopg.Connection, run_id: str, *,
    status: str,
    parquet_row_count: int = 0,
    parquet_bytes_written: int = 0,
    r2_bucket: str | None = None, r2_key: str | None = None,
    filings_indexed_count: int | None = None,
    filings_fetched_count: int | None = None,
    filings_parsed_ok_count: int | None = None,
    filings_parsed_failed_count: int | None = None,
    started_at: float, error_message: str | None = None,
    notes: dict[str, Any] | None = None,
) -> None:
    duration = round(time.monotonic() - started_at, 3)
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE ops.sec_edgar_form_10k_r2_ingest_runs
               SET status = %s,
                   parquet_row_count = %s,
                   parquet_bytes_written = %s,
                   r2_bucket = %s, r2_key = %s,
                   filings_indexed_count = %s,
                   filings_fetched_count = %s,
                   filings_parsed_ok_count = %s,
                   filings_parsed_failed_count = %s,
                   finished_at = now(), duration_seconds = %s,
                   error_message = %s, notes = %s
             WHERE id = %s;
            """, (
            status, parquet_row_count, parquet_bytes_written,
            r2_bucket, r2_key,
            filings_indexed_count, filings_fetched_count,
            filings_parsed_ok_count, filings_parsed_failed_count,
            duration, error_message,
            Jsonb(notes) if notes else None, run_id,
        ))
    conn.commit()


# ------------------------------------------------------------------ #
# Per-year orchestrator
# ------------------------------------------------------------------ #


@dataclass
class YearAccumulator:
    year: int
    filings: list[dict[str, Any]] = field(default_factory=list)
    officers_directors: list[dict[str, Any]] = field(default_factory=list)
    executive_compensation: list[dict[str, Any]] = field(default_factory=list)
    security_ownership: list[dict[str, Any]] = field(default_factory=list)
    properties: list[dict[str, Any]] = field(default_factory=list)
    legal_proceedings: list[dict[str, Any]] = field(default_factory=list)
    risk_factors: list[dict[str, Any]] = field(default_factory=list)
    parsed_ok: int = 0
    parsed_failed: int = 0
    incorp_by_ref_11: int = 0
    incorp_by_ref_12: int = 0

    def stream(self, name: str) -> list[dict[str, Any]]:
        return getattr(self, name)


async def discover_year(
    client: httpx.AsyncClient, year: int, *, limiter: RpsLimiter,
) -> list[FormIdxRow]:
    out: list[FormIdxRow] = []
    for q in (1, 2, 3, 4):
        url = FORM_IDX_URL.format(year=year, q=q)
        try:
            status, body = await fetch_text(
                client, url, limiter=limiter, timeout=120.0,
            )
        except RuntimeError as exc:
            log.warning("[%d] QTR%d form.idx fetch failed: %s", year, q, exc)
            continue
        if status != 200:
            log.warning("[%d] QTR%d form.idx HTTP %s", year, q, status)
            continue
        try:
            text = body.decode("latin-1")
        except UnicodeDecodeError:
            text = body.decode("utf-8", "ignore")
        rows = parse_form_idx(text, target_forms=FORM_TYPES_10K)
        log.info("[%d] QTR%d form.idx → %d Form 10-K rows", year, q, len(rows))
        out.extend(rows)
    return out


async def ingest_one_filing(
    client: httpx.AsyncClient, idx_row: FormIdxRow, *,
    limiter: RpsLimiter, include_raw_html: bool, s3,
) -> tuple[bool, dict[str, list[dict[str, Any]]] | None, int]:
    primary_url, html = await fetch_filing_primary_html(
        client, idx_row, limiter=limiter,
    )
    if html is None:
        return False, None, 0

    raw_uri: str | None = None
    if include_raw_html:
        cik_padded = idx_row.cik.zfill(10)
        acc = idx_row.accession or "unknown"
        raw_key = f"sec-edgar/form-10k/raw/{cik_padded}/{acc}/primary.html"
        try:
            upload_bytes_to_r2(
                s3, bucket=R2_BUCKET, key=raw_key, body=html,
                content_type="text/html",
            )
            raw_uri = f"s3://{R2_BUCKET}/{raw_key}"
        except Exception as exc:
            log.warning("raw HTML upload failed for %s: %s",
                        idx_row.accession, exc)

    header = FilingHeader(
        cik_raw=idx_row.cik,
        filer_name_raw=idx_row.company_name,
        accession_raw=idx_row.accession,
        form_type=idx_row.form_type,
        filing_date=idx_row.date_filed,
        primary_doc_url=primary_url or "",
        raw_html_r2_uri=raw_uri,
    )
    try:
        parsed = parse_filing(header, html)
    except Exception as exc:
        log.warning("parse_filing %s/%s threw %s",
                    idx_row.cik, idx_row.accession, exc)
        return False, None, len(html)

    return True, parsed, len(html)


async def process_year(
    year: int, *,
    state_path: Path,
    include_raw_html: bool,
    max_filings: int | None,
    db_url: str | None,
    target_rps: int,
) -> int:
    started_wall = time.monotonic()
    log.info("=" * 70)
    log.info("=== YEAR %d  (target_rps=%d) ===", year, target_rps)
    log.info("=" * 70)

    limiter = RpsLimiter(target_rps)
    s3 = _r2_client()

    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}
    timeout = httpx.Timeout(180.0, connect=30.0)
    async with httpx.AsyncClient(headers=headers, http2=False, timeout=timeout) as client:
        accumulator = YearAccumulator(year=year)
        idx_rows = await discover_year(client, year, limiter=limiter)
        log.info("[%d] total Form 10-K indexed: %d", year, len(idx_rows))

        discovery_run_id: str | None = None
        if db_url:
            with psycopg.connect(db_url) as conn:
                discovery_run_id = insert_run_row(
                    conn, year=year, stream="discovery",
                    source_url=FORM_IDX_URL.format(year=year, q="*"),
                )
                finalize_run_row(
                    conn, discovery_run_id, status="completed",
                    filings_indexed_count=len(idx_rows),
                    started_at=started_wall,
                )

        if not idx_rows:
            log.warning("[%d] no Form 10-K rows discovered", year)
            return 0

        if max_filings is not None and len(idx_rows) > max_filings:
            log.info("[%d] limiting to first %d filings (smoke)",
                     year, max_filings)
            idx_rows = idx_rows[:max_filings]

        # Resume support.
        completed: set[str] = set()
        state: dict[str, Any] = {}
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text())
            except (json.JSONDecodeError, OSError):
                state = {}
        year_key = str(year)
        completed = set(state.get(year_key, {}).get("completed", []))
        if completed:
            log.info("[%d] state file: %d accessions already done",
                     year, len(completed))
            idx_rows = [r for r in idx_rows if r.accession not in completed]
            log.info("[%d] remaining: %d", year, len(idx_rows))

        sem = asyncio.Semaphore(HTTP_CONCURRENCY)
        results_lock = asyncio.Lock()

        async def _worker(idx_row: FormIdxRow) -> None:
            async with sem:
                ok, parsed, _bytes = await ingest_one_filing(
                    client, idx_row, limiter=limiter,
                    include_raw_html=include_raw_html, s3=s3,
                )
                async with results_lock:
                    if ok and parsed is not None:
                        for stream_name in STREAMS:
                            accumulator.stream(stream_name).extend(
                                parsed.get(stream_name, [])
                            )
                        # Track incorporation-by-reference rate based on
                        # parser's empty-stream signal.
                        if not parsed.get("executive_compensation"):
                            accumulator.incorp_by_ref_11 += 1
                        if not parsed.get("security_ownership"):
                            accumulator.incorp_by_ref_12 += 1
                        accumulator.parsed_ok += 1
                        completed.add(idx_row.accession)
                    else:
                        accumulator.parsed_failed += 1
                    seen = accumulator.parsed_ok + accumulator.parsed_failed
                    if seen % RECORDS_LOG_EVERY == 0:
                        log.info(
                            "[%d] progress: %d/%d (ok=%d, failed=%d) "
                            "filings=%d od=%d ec=%d so=%d prop=%d lp=%d rf=%d",
                            year, seen, len(idx_rows),
                            accumulator.parsed_ok, accumulator.parsed_failed,
                            len(accumulator.filings),
                            len(accumulator.officers_directors),
                            len(accumulator.executive_compensation),
                            len(accumulator.security_ownership),
                            len(accumulator.properties),
                            len(accumulator.legal_proceedings),
                            len(accumulator.risk_factors),
                        )

        stop_event = asyncio.Event()

        async def _checkpointer() -> None:
            while not stop_event.is_set():
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=120.0)
                except asyncio.TimeoutError:
                    pass
                async with results_lock:
                    state[year_key] = {
                        "completed": sorted(completed),
                        "ts": datetime.now(timezone.utc).isoformat(),
                    }
                    try:
                        state_path.write_text(json.dumps(state))
                    except OSError as exc:
                        log.warning("state write failed: %s", exc)

        cp_task = asyncio.create_task(_checkpointer())
        try:
            await asyncio.gather(*[_worker(r) for r in idx_rows])
        finally:
            stop_event.set()
            await cp_task

        async with results_lock:
            state[year_key] = {
                "completed": sorted(completed),
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            try:
                state_path.write_text(json.dumps(state))
            except OSError as exc:
                log.warning("state write failed: %s", exc)

        # Per-stream Parquet emit + R2 upload + audit.
        rc = 0
        run_notes_common = {
            "incorporation_by_reference_item_11_count": accumulator.incorp_by_ref_11,
            "incorporation_by_reference_item_12_count": accumulator.incorp_by_ref_12,
        }
        for stream_name in STREAMS:
            recs = accumulator.stream(stream_name)
            schema = _STREAM_SCHEMAS[stream_name]
            r2_key = f"sec-edgar/form-10k/year={year}/{stream_name}/data.parquet"
            run_id: str | None = None
            stream_started = time.monotonic()
            if db_url:
                conn = psycopg.connect(db_url)
                try:
                    run_id = insert_run_row(
                        conn, year=year, stream=stream_name,
                        source_url=f"s3://{R2_BUCKET}/{r2_key}",
                    )
                finally:
                    conn.close()

            if not recs:
                log.info("[%d/%s] no records — skipping upload",
                         year, stream_name)
                if db_url and run_id is not None:
                    conn = psycopg.connect(db_url)
                    try:
                        finalize_run_row(
                            conn, run_id, status="completed",
                            parquet_row_count=0, parquet_bytes_written=0,
                            r2_bucket=R2_BUCKET, r2_key=r2_key,
                            filings_indexed_count=len(idx_rows),
                            filings_fetched_count=accumulator.parsed_ok + accumulator.parsed_failed,
                            filings_parsed_ok_count=accumulator.parsed_ok,
                            filings_parsed_failed_count=accumulator.parsed_failed,
                            started_at=stream_started,
                            notes={**run_notes_common, "empty": True},
                        )
                    finally:
                        conn.close()
                continue

            try:
                parquet_bytes = write_parquet_zstd(recs, schema)
                uploaded = upload_bytes_to_r2(
                    s3, bucket=R2_BUCKET, key=r2_key, body=parquet_bytes,
                )
                log.info(
                    "[%d/%s] uploaded %d rows, %.2f MB → s3://%s/%s",
                    year, stream_name, len(recs), uploaded / (1 << 20),
                    R2_BUCKET, r2_key,
                )
                if db_url and run_id is not None:
                    conn = psycopg.connect(db_url)
                    try:
                        finalize_run_row(
                            conn, run_id, status="completed",
                            parquet_row_count=len(recs),
                            parquet_bytes_written=uploaded,
                            r2_bucket=R2_BUCKET, r2_key=r2_key,
                            filings_indexed_count=len(idx_rows),
                            filings_fetched_count=accumulator.parsed_ok + accumulator.parsed_failed,
                            filings_parsed_ok_count=accumulator.parsed_ok,
                            filings_parsed_failed_count=accumulator.parsed_failed,
                            started_at=stream_started,
                            notes=run_notes_common,
                        )
                    finally:
                        conn.close()
            except Exception as exc:
                log.exception("[%d/%s] write/upload FAILED", year, stream_name)
                rc = 1
                if db_url and run_id is not None:
                    conn = psycopg.connect(db_url)
                    try:
                        finalize_run_row(
                            conn, run_id, status="failed",
                            started_at=stream_started,
                            error_message=str(exc)[:1000],
                        )
                    finally:
                        conn.close()

        log.info(
            "[%d] DONE parsed_ok=%d failed=%d "
            "incorp_by_ref(item11=%d, item12=%d) wall=%.1fs",
            year, accumulator.parsed_ok, accumulator.parsed_failed,
            accumulator.incorp_by_ref_11, accumulator.incorp_by_ref_12,
            time.monotonic() - started_wall,
        )
        return rc


# ------------------------------------------------------------------ #
# CLI
# ------------------------------------------------------------------ #


def _parse_year_range(s: str) -> list[int]:
    if "-" in s:
        a, b = s.split("-", 1)
        return list(range(int(a), int(b) + 1))
    return [int(s)]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("year", nargs="?", type=int)
    p.add_argument("--years", default=None,
                   help="Year range e.g. 1994-2026.")
    p.add_argument("--max-filings", type=int, default=None,
                   help="Per-year cap (smoke).")
    p.add_argument("--include-raw-html", action="store_true",
                   help="Also upload raw HTML for each parsed filing. "
                        "Default OFF — large preservation cost (~1.8-9 TB "
                        "for full ingest); confirm with operator before "
                        "enabling.")
    p.add_argument("--state-file",
                   default="/tmp/sec_edgar_form_10k_state.json",
                   help="Resume checkpoint file.")
    p.add_argument("--no-audit", action="store_true",
                   help="Skip ops.sec_edgar_form_10k_r2_ingest_runs writes "
                        "(useful for smoke when DB is unavailable).")
    # Env-var fallthrough so the Modal app's `env["SEC_EDGAR_TARGET_RPS"]="5"`
    # actually takes effect. Without this, the CLI default below was always
    # winning (root-caused 2026-05-13 during sec-10k-backfill-to-floor cycle:
    # the Modal app set the env var but the script never read it, so 5h
    # containers were processing at RPS=1 — too slow to complete a year before
    # Modal kill). Explicit --target-rps still wins over env var.
    env_rps = os.environ.get("SEC_EDGAR_TARGET_RPS")
    cli_default_rps = int(env_rps) if env_rps and env_rps.isdigit() else DEFAULT_TARGET_RPS
    p.add_argument("--target-rps", type=int, default=cli_default_rps,
                   help=f"Per-script SEC fetch RPS. Default from "
                        f"SEC_EDGAR_TARGET_RPS env var or "
                        f"{DEFAULT_TARGET_RPS} for parallel-with-siblings; "
                        f"set 5 when running solo.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.years:
        years: Iterable[int] = _parse_year_range(args.years)
    elif args.year is not None:
        years = [args.year]
    else:
        log.error("must pass year or --years")
        return 2

    state_path = Path(args.state_file)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    db_url = None if args.no_audit else _database_url()
    if not args.no_audit and not db_url:
        log.warning("no DB URL set; audit ledger writes will be skipped")

    rc = 0
    for y in years:
        try:
            rc_one = asyncio.run(process_year(
                year=y, state_path=state_path,
                include_raw_html=args.include_raw_html,
                max_filings=args.max_filings,
                db_url=db_url,
                target_rps=args.target_rps,
            ))
            if rc_one != 0:
                rc = rc_one
        except Exception:
            log.exception("year %d failed", y)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())

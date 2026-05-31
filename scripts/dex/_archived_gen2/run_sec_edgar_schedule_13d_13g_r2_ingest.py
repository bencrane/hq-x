#!/usr/bin/env python3
"""SEC EDGAR Schedule 13D + 13G → R2 Parquet ingest.

For each year in the configured span:
  1. Pull the EDGAR full-index ``form.idx`` for the year and filter to
     SC 13D / SC 13G / SC 13D/A / SC 13G/A filings.
  2. Concurrently fetch + parse each filing's primary HTML doc
     (asyncio + 2 RPS rate-limit, ~16 concurrent workers).
  3. Buffer parsed records into 5 streams
     (filings, reporting_persons, share_amounts, items, subject_company)
     and emit ZSTD Parquet at:
        s3://dex-raw-landing-zone/sec-edgar/schedule-13d-13g/year={Y}/{stream}/data.parquet
  4. Optionally also upload raw filing HTML at:
        s3://dex-raw-landing-zone/sec-edgar/schedule-13d-13g/raw/{cik}/{accession}/primary.html

Idempotency: per (year, accession) — JSON checkpoint file at ``--state-file``.

Audit: one row per (year, stream) in
``ops.sec_edgar_schedule_13d_13g_r2_ingest_runs``.

Parallel-execution constraint: this script runs concurrently with two
other SEC EDGAR per-filing ingests (Form 144 + Form 13F). SEC fair-use
is 10 RPS aggregate per egress IP; ``TARGET_RPS = 2`` keeps 3-way
parallel + concurrent Form 4 DERA-bulk under the cap. Do NOT raise.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_sec_edgar_schedule_13d_13g_r2_ingest.py 2024 --max-filings 500
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_sec_edgar_schedule_13d_13g_r2_ingest.py --years 2010-2026
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
from _lib.sec_edgar_schedule_13d_13g_parser import FilingHeader, parse_filing  # noqa: E402


# ------------------------------------------------------------------ #
# Constants
# ------------------------------------------------------------------ #

R2_BUCKET = "dex-raw-landing-zone"
PROVIDER = "sec_edgar_schedule_13d_13g"

EDGAR_HOST = "https://www.sec.gov"
FORM_IDX_URL = (
    "https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{q}/form.idx"
)

# Default TARGET_RPS=2 reflects local-machine parallel-execution constraint
# (3 SEC EDGAR per-filing scripts × 2 RPS = 6 RPS aggregate < 10 RPS cap).
# Overridable via SEC_EDGAR_TARGET_RPS env var. Modal-deployed instances each
# have an independent egress IP, so the per-IP 10 RPS cap applies per Modal
# app — Modal sets SEC_EDGAR_TARGET_RPS=5 to run faster.
# See directive §"Parallel-execution constraint".
TARGET_RPS = int(os.environ.get("SEC_EDGAR_TARGET_RPS", "2"))
HTTP_CONCURRENCY = int(os.environ.get("SEC_EDGAR_HTTP_CONCURRENCY", "16"))

RECORDS_LOG_EVERY = 500

USER_AGENT = (
    "data-engine-x/sec-edgar-schedule-13d-13g-ingest "
    "tools@substrate.build "
    "(operational research)"
)

TARGET_FORM_TYPES: frozenset[str] = frozenset({
    "SC 13D", "SC 13G", "SC 13D/A", "SC 13G/A",
})

STREAMS: tuple[str, ...] = (
    "filings",
    "reporting_persons",
    "share_amounts",
    "items",
    "subject_company",
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
    return logging.getLogger("sec-edgar-schedule-13d-13g-ingest")


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


def parse_form_idx(text: str, target_forms: frozenset[str]) -> list[FormIdxRow]:
    """Parse a form.idx body into typed rows for any of ``target_forms``."""
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
        self.min_interval = 1.0 / rps
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
    timeout: float = 60.0, retries: int = 3,
) -> tuple[int, bytes]:
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


def _select_primary_doc(idx_json: dict[str, Any], form_type: str) -> str | None:
    """From an EDGAR index.json choose the primary HTML doc.

    Heuristic:
      1. Prefer items where ``type`` equals the form type ('SC 13D',
         'SC 13G', etc.).
      2. Of those, choose the one with .htm/.html extension (not .txt).
      3. Fallback: largest .htm/.html in the directory.
    """
    items = idx_json.get("directory", {}).get("item", [])
    if not items:
        return None
    candidates: list[tuple[int, str]] = []
    target_type = form_type.upper()
    for it in items:
        name = it.get("name", "")
        size = int(it.get("size") or 0)
        type_ = (it.get("type") or "").upper()
        if not name.lower().endswith((".htm", ".html")):
            continue
        if type_ == target_type:
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
    primary_name = _select_primary_doc(idx_json, idx_row.form_type)
    if primary_name is None:
        return None, None
    cik_int = idx_row.cik.lstrip("0") or "0"
    acc = idx_row.accession.replace("-", "")
    primary_url = (
        f"{EDGAR_HOST}/Archives/edgar/data/{cik_int}/{acc}/{primary_name}"
    )
    try:
        status, body = await fetch_text(
            client, primary_url, limiter=limiter, timeout=120.0,
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
    pa.field("filer_cik_normalized", pa.string()),
    pa.field("filer_legal_name_normalized", pa.string()),
    pa.field("filer_legal_name_raw", pa.string()),
    pa.field("form_type", pa.string()),
    pa.field("is_amendment", pa.bool_()),
    pa.field("original_accession_number", pa.string()),
    pa.field("filing_date", pa.string()),
    pa.field("event_date", pa.string()),
    pa.field("schedule_year", pa.int16()),
    pa.field("primary_doc_url", pa.string()),
    pa.field("raw_html_r2_uri", pa.string()),
])

_REPORTING_PERSONS_SCHEMA = pa.schema([
    pa.field("accession_number", pa.string()),
    pa.field("reporting_person_seq", pa.int32()),
    pa.field("reporting_person_name_raw", pa.string()),
    pa.field("reporting_person_name_normalized", pa.string()),
    pa.field("reporting_person_type", pa.string()),
    pa.field("reporting_person_first_normalized", pa.string()),
    pa.field("reporting_person_last_normalized", pa.string()),
    pa.field("reporting_person_lei_normalized", pa.string()),
    pa.field("reporting_person_ein_normalized", pa.string()),
    pa.field("address_street", pa.string()),
    pa.field("address_city", pa.string()),
    pa.field("address_state", pa.string()),
    pa.field("address_zip5", pa.string()),
    pa.field("address_country", pa.string()),
    pa.field("jurisdiction_of_organization", pa.string()),
    pa.field("citizenship", pa.string()),
    pa.field("rp_type_code", pa.string()),
    pa.field("schedule_year", pa.int16()),
])

_SHARE_AMOUNTS_SCHEMA = pa.schema([
    pa.field("accession_number", pa.string()),
    pa.field("reporting_person_seq", pa.int32()),
    pa.field("cusip", pa.string()),
    pa.field("shares_beneficially_owned", pa.int64()),
    pa.field("sole_voting_power", pa.int64()),
    pa.field("shared_voting_power", pa.int64()),
    pa.field("sole_dispositive_power", pa.int64()),
    pa.field("shared_dispositive_power", pa.int64()),
    pa.field("percent_of_class", pa.float64()),
    pa.field("schedule_year", pa.int16()),
])

_ITEMS_SCHEMA = pa.schema([
    pa.field("accession_number", pa.string()),
    pa.field("item_number", pa.int16()),
    pa.field("item_label", pa.string()),
    pa.field("item_text_raw", pa.string()),
    pa.field("schedule_year", pa.int16()),
])

_SUBJECT_COMPANY_SCHEMA = pa.schema([
    pa.field("accession_number", pa.string()),
    pa.field("subject_company_cik_normalized", pa.string()),
    pa.field("subject_company_name_raw", pa.string()),
    pa.field("subject_company_name_normalized", pa.string()),
    pa.field("subject_company_cusip", pa.string()),
    pa.field("subject_company_ticker", pa.string()),
    pa.field("subject_company_title_of_class", pa.string()),
    pa.field("subject_company_principal_office_street", pa.string()),
    pa.field("subject_company_principal_office_city", pa.string()),
    pa.field("subject_company_principal_office_state", pa.string()),
    pa.field("subject_company_principal_office_zip5", pa.string()),
    pa.field("schedule_year", pa.int16()),
])

_STREAM_SCHEMAS: dict[str, pa.Schema] = {
    "filings": _FILINGS_SCHEMA,
    "reporting_persons": _REPORTING_PERSONS_SCHEMA,
    "share_amounts": _SHARE_AMOUNTS_SCHEMA,
    "items": _ITEMS_SCHEMA,
    "subject_company": _SUBJECT_COMPANY_SCHEMA,
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
    pq.write_table(
        tbl, buf,
        compression="zstd",
        compression_level=3,
    )
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
    INSERT INTO ops.sec_edgar_schedule_13d_13g_r2_ingest_runs
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
            UPDATE ops.sec_edgar_schedule_13d_13g_r2_ingest_runs
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
    reporting_persons: list[dict[str, Any]] = field(default_factory=list)
    share_amounts: list[dict[str, Any]] = field(default_factory=list)
    items: list[dict[str, Any]] = field(default_factory=list)
    subject_company: list[dict[str, Any]] = field(default_factory=list)
    parsed_ok: int = 0
    parsed_failed: int = 0

    def stream(self, name: str) -> list[dict[str, Any]]:
        return getattr(self, name)


async def discover_year(
    client: httpx.AsyncClient, year: int, *, limiter: RpsLimiter,
) -> list[FormIdxRow]:
    """Pull all four quarterly form.idx files for the year, filter to the
    Schedule 13D/G form-type set, return concatenated list — deduped by
    accession.

    Multi-filer Schedule 13D/Gs appear once per FILER in form.idx, all
    referencing the same accession + filename. We dedup by accession to
    fetch + parse each filing exactly once; the per-filing HTML cover
    page enumerates every reporting person.
    """
    out: list[FormIdxRow] = []
    seen_accessions: set[str] = set()
    for q in (1, 2, 3, 4):
        url = FORM_IDX_URL.format(year=year, q=q)
        try:
            status, body = await fetch_text(
                client, url, limiter=limiter, timeout=60.0,
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
        rows = parse_form_idx(text, target_forms=TARGET_FORM_TYPES)
        deduped = []
        for r in rows:
            if not r.accession or r.accession in seen_accessions:
                continue
            seen_accessions.add(r.accession)
            deduped.append(r)
        log.info(
            "[%d] QTR%d form.idx → %d rows (%d unique accessions)",
            year, q, len(rows), len(deduped),
        )
        out.extend(deduped)
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
        raw_key = f"sec-edgar/schedule-13d-13g/raw/{cik_padded}/{acc}/primary.html"
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
        filer_cik_raw=idx_row.cik,
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
) -> int:
    started_wall = time.monotonic()
    log.info("=" * 70)
    log.info("=== YEAR %d ===", year)
    log.info("=" * 70)

    limiter = RpsLimiter(TARGET_RPS)
    s3 = _r2_client()

    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}
    async with httpx.AsyncClient(headers=headers, http2=False) as client:
        accumulator = YearAccumulator(year=year)
        idx_rows = await discover_year(client, year, limiter=limiter)
        log.info("[%d] total Schedule 13D/G indexed: %d", year, len(idx_rows))

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
            log.warning("[%d] no Schedule 13D/G rows discovered", year)
            return 0

        if max_filings is not None and len(idx_rows) > max_filings:
            log.info("[%d] limiting to first %d filings (smoke)",
                     year, max_filings)
            idx_rows = idx_rows[:max_filings]

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
                        accumulator.parsed_ok += 1
                        completed.add(idx_row.accession)
                    else:
                        accumulator.parsed_failed += 1
                    seen = accumulator.parsed_ok + accumulator.parsed_failed
                    if seen % RECORDS_LOG_EVERY == 0:
                        log.info(
                            "[%d] progress: %d/%d (ok=%d, failed=%d) "
                            "filings=%d rps=%d sa=%d items=%d sub=%d",
                            year, seen, len(idx_rows),
                            accumulator.parsed_ok, accumulator.parsed_failed,
                            len(accumulator.filings),
                            len(accumulator.reporting_persons),
                            len(accumulator.share_amounts),
                            len(accumulator.items),
                            len(accumulator.subject_company),
                        )

        stop_event = asyncio.Event()

        async def _checkpointer() -> None:
            while not stop_event.is_set():
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=60.0)
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

        rc = 0
        for stream_name in STREAMS:
            recs = accumulator.stream(stream_name)
            schema = _STREAM_SCHEMAS[stream_name]
            r2_key = f"sec-edgar/schedule-13d-13g/year={year}/{stream_name}/data.parquet"
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
                            notes={"empty": True},
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
                        )
                    finally:
                        conn.close()
            except Exception as exc:
                log.exception("[%d/%s] write/upload FAILED",
                              year, stream_name)
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
            "[%d] DONE parsed_ok=%d failed=%d wall=%.1fs",
            year, accumulator.parsed_ok, accumulator.parsed_failed,
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
                   help="Year range e.g. 2010-2026.")
    p.add_argument("--max-filings", type=int, default=None,
                   help="Per-year cap (smoke).")
    p.add_argument("--include-raw-html", action="store_true",
                   help="Also upload raw HTML for each parsed filing.")
    p.add_argument(
        "--state-file",
        default="/tmp/sec_edgar_schedule_13d_13g_state.json",
        help="Resume checkpoint file.",
    )
    p.add_argument(
        "--no-audit", action="store_true",
        help="Skip ops.sec_edgar_schedule_13d_13g_r2_ingest_runs writes.",
    )
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
            ))
            if rc_one != 0:
                rc = rc_one
        except Exception:
            log.exception("year %d failed", y)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())

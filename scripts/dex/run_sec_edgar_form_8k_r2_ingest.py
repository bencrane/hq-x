#!/usr/bin/env python3
"""SEC EDGAR Form 8-K (current report — material events) → R2 Parquet ingest.

For each (year, quarter) in the configured span:
  1. Pull the EDGAR full-index ``form.idx`` for the quarter and filter to
     filings of form_type ``8-K`` or ``8-K/A``.
  2. Concurrently fetch + parse each filing's primary HTML doc (asyncio +
     TARGET_RPS rate-limit, 16 concurrent workers).
  3. Buffer parsed records into 8 streams. Per-quarter buffer flush for
     high-volume streams (filings, items_index, item_8_01_other_events);
     per-year flush for Item-specific lower-volume streams
     (item_5_02_officer_changes, item_1_01_material_agreement,
     item_2_01_acquisition_disposition, item_2_03_direct_financial_obligation,
     item_5_01_change_in_control).
  4. Emit ZSTD Parquet at:
        s3://dex-raw-landing-zone/sec-edgar/form-8k/year={Y}/quarter={Q}/{stream}/data.parquet
        s3://dex-raw-landing-zone/sec-edgar/form-8k/year={Y}/{item-stream}/data.parquet

Idempotency: per (year, quarter, accession). The orchestrator keeps a JSON
checkpoint at ``--state-file`` so a network blip / session timeout doesn't
restart from zero. Re-running with the same state file resumes where it left
off.

Audit: rows in ops.sec_edgar_form_8k_r2_ingest_runs — per (year, quarter,
stream) for the high-volume streams; per (year, stream) for the per-year
Item-specific streams.

PHASING (per parent SPLIT): Phase 1 (2020-2026), Phase 2 (2010-2019). Pre-2010
is out of scope for this cycle (Item 2.03 corpus pre-2010 is thin).

Parallel-execution constraint (inherited from parent SPLIT
p1-rps-budget-exceeded): TARGET_RPS=1 default. Aggregate SEC EDGAR fair-use
cap is 10 RPS; 7 existing scripts + 3 new × 1 RPS = 10 RPS at full overlap.
Override via SEC_EDGAR_TARGET_RPS=5 env or ``--target-rps 5`` for solo runs.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_sec_edgar_form_8k_r2_ingest.py \\
        --years 2024 --quarters 1 --max-filings 1000  # smoke
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_sec_edgar_form_8k_r2_ingest.py \\
        --years 2010-2026  # full Phase 1+2 backfill
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
from _lib.sec_edgar_form_8k_parser import FilingHeader, parse_filing  # noqa: E402


# ------------------------------------------------------------------ #
# Constants
# ------------------------------------------------------------------ #

R2_BUCKET = "dex-raw-landing-zone"
PROVIDER = "sec_edgar_form_8k"

EDGAR_HOST = "https://www.sec.gov"
FORM_IDX_URL = (
    "https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{q}/form.idx"
)

# Per parent SPLIT inherited constraint p1-rps-budget-exceeded: TARGET_RPS=1
# default (not 2). Aggregate SEC EDGAR fair-use cap is 10 RPS across all
# concurrent crons; 7 existing scripts (13f/def-14a/13d-13g + 4 operator-
# invoked) + 3 new from parent SPLIT × 1 RPS = 10 RPS aggregate at full
# overlap. Override via SEC_EDGAR_TARGET_RPS=5 env var or --target-rps for
# solo runs once siblings complete. See directive 2026-05-12-sec-8k-item-203-
# extension.md §"Validator notes" §"TARGET_RPS=1 (inherited from parent SPLIT)".
TARGET_RPS = int(os.environ.get("SEC_EDGAR_TARGET_RPS", "1"))
HTTP_CONCURRENCY = int(os.environ.get("SEC_EDGAR_HTTP_CONCURRENCY", "16"))

RECORDS_LOG_EVERY = 500

USER_AGENT = (
    "data-engine-x/sec-edgar-form-8k-ingest "
    "tools@substrate.build "
    "(operational research)"
)

PER_QUARTER_STREAMS: tuple[str, ...] = (
    "filings",
    "items_index",
    "item_8_01_other_events",
)
PER_YEAR_STREAMS: tuple[str, ...] = (
    "item_5_02_officer_changes",
    "item_1_01_material_agreement",
    "item_2_01_acquisition_disposition",
    "item_2_03_direct_financial_obligation",
    "item_5_01_change_in_control",
)
ALL_STREAMS: tuple[str, ...] = PER_QUARTER_STREAMS + PER_YEAR_STREAMS


# ------------------------------------------------------------------ #
# Logging
# ------------------------------------------------------------------ #

def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("sec-edgar-form-8k-ingest")


log = _logger()


# ------------------------------------------------------------------ #
# Env helpers
# ------------------------------------------------------------------ #

def _required_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"{name} is not set in the environment.")
    return v


def _r2_client():  # type: ignore[no-untyped-def]
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
# Form index parser (lifted from DEF 14A predecessor)
# ------------------------------------------------------------------ #


@dataclass(frozen=True)
class FormIdxRow:
    form_type: str
    company_name: str
    cik: str
    date_filed: str
    filename: str

    @property
    def accession(self) -> str:
        m = re.search(r"(\d{10}-\d{2}-\d{6})", self.filename)
        return m.group(1) if m else ""


def parse_form_idx(
    text: str, target_forms: frozenset[str] = frozenset({"8-K", "8-K/A"}),
) -> list[FormIdxRow]:
    """Parse form.idx → typed rows for ``target_forms``."""
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
# Per-filing pipeline
# ------------------------------------------------------------------ #


_AUX_FILE_RE = re.compile(
    r"(?:^|/)("
    r"(?:.*-index)|"
    r"(?:.*\.(?:xsd|xml|zip|gif|png|jpg|jpeg|xlsx|css|js))|"
    r"(?:R\d+\.htm)|"
    r"(?:Financial_Report\.xlsx)|"
    r"(?:FilingSummary\.xml)"
    r")$",
    re.I,
)


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


def _select_primary_doc(idx_json: dict[str, Any]) -> str | None:
    """From an EDGAR index.json choose the primary 8-K HTML doc.

    For 8-K, ``type`` in index.json is just ``"text.gif"`` for all items —
    not a form-type discriminator. Heuristic:
      1. Skip aux files (xsd/xml/zip/gif/Excel/index/R-numbered exhibits).
      2. Prefer .htm/.html files whose name contains "8k" or "8-k".
      3. Fallback: largest .htm/.html that's not an exhibit (no leading ex_).
      4. Last resort: largest .htm/.html in the directory.
    """
    items = idx_json.get("directory", {}).get("item", [])
    if not items:
        return None
    eight_k_named: list[tuple[int, str]] = []
    main_html: list[tuple[int, str]] = []
    exhibit_html: list[tuple[int, str]] = []
    for it in items:
        name = it.get("name", "")
        if not name.lower().endswith((".htm", ".html")):
            continue
        if _AUX_FILE_RE.search(name):
            continue
        try:
            size = int(it.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        lname = name.lower()
        if "8k" in lname or "8-k" in lname or "form8" in lname or "current_report" in lname:
            eight_k_named.append((size, name))
        elif lname.startswith("ex"):
            exhibit_html.append((size, name))
        else:
            main_html.append((size, name))
    for bucket in (eight_k_named, main_html, exhibit_html):
        if bucket:
            bucket.sort(reverse=True)
            return bucket[0][1]
    return None


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
            client, primary_url, limiter=limiter, timeout=120.0,
        )
    except RuntimeError:
        return primary_url, None
    if status != 200:
        return primary_url, None
    return primary_url, body


_TXT_DOCUMENT_RE = re.compile(
    rb"<DOCUMENT>\s*<TYPE>(?P<type>[^<\n]+?)\s*"
    rb"(?:<SEQUENCE>[^<]*)?"
    rb"(?:<FILENAME>[^<]*)?"
    rb"(?:<DESCRIPTION>[^<]*)?"
    rb"<TEXT>(?P<text>.*?)</TEXT>\s*</DOCUMENT>",
    re.S,
)


async def fetch_filing_txt_8k_html(
    client: httpx.AsyncClient, idx_row: FormIdxRow, *, limiter: RpsLimiter,
) -> bytes | None:
    """Fetch the SEC complete-submission .txt and extract the first
    <DOCUMENT><TYPE>8-K (or 8-K/A) inner <TEXT>.

    Used as a fallback when the primary-HTML parse yields zero Items —
    typically because the index.json picked an exhibit (press release) instead
    of the cover page.
    """
    cik_int = idx_row.cik.lstrip("0") or "0"
    acc_no_dashes = idx_row.accession.replace("-", "")
    if not acc_no_dashes:
        return None
    url = (
        f"{EDGAR_HOST}/Archives/edgar/data/{cik_int}/"
        f"{acc_no_dashes}/{idx_row.accession}.txt"
    )
    try:
        status, body = await fetch_text(
            client, url, limiter=limiter, timeout=120.0,
        )
    except RuntimeError:
        return None
    if status != 200 or not body:
        return None
    target_types = {b"8-K", b"8-K/A"}
    for m in _TXT_DOCUMENT_RE.finditer(body):
        doc_type = m.group("type").strip()
        if doc_type in target_types:
            return m.group("text")
    return None


# ------------------------------------------------------------------ #
# pyarrow schemas
# ------------------------------------------------------------------ #


_FILINGS_SCHEMA = pa.schema([
    pa.field("accession_number", pa.string()),
    pa.field("cik_normalized", pa.string()),
    pa.field("form_type", pa.string()),
    pa.field("is_amendment", pa.bool_()),
    pa.field("original_accession_number", pa.string()),
    pa.field("company_name_raw", pa.string()),
    pa.field("company_name_normalized", pa.string()),
    pa.field("filing_date", pa.string()),
    pa.field("period_of_report", pa.string()),
    pa.field("items_list", pa.string()),
    pa.field("report_year", pa.int16()),
    pa.field("report_quarter", pa.int16()),
])

_ITEMS_INDEX_SCHEMA = pa.schema([
    pa.field("accession_number", pa.string()),
    pa.field("cik_normalized", pa.string()),
    pa.field("item_no", pa.string()),
    pa.field("item_label", pa.string()),
    pa.field("item_seq", pa.int16()),
    pa.field("has_body_parse", pa.bool_()),
    pa.field("report_year", pa.int16()),
    pa.field("report_quarter", pa.int16()),
])

_ITEM_5_02_SCHEMA = pa.schema([
    pa.field("accession_number", pa.string()),
    pa.field("cik_normalized", pa.string()),
    pa.field("officer_event_seq", pa.int16()),
    pa.field("officer_name_raw", pa.string()),
    pa.field("officer_name_normalized", pa.string()),
    pa.field("officer_first_normalized", pa.string()),
    pa.field("officer_last_normalized", pa.string()),
    pa.field("role", pa.string()),
    pa.field("role_normalized", pa.string()),
    pa.field("event_type", pa.string()),
    pa.field("effective_date", pa.string()),
    pa.field("comp_arrangement_summary", pa.string()),
    pa.field("item_text_raw", pa.string()),
    pa.field("report_year", pa.int16()),
])

_ITEM_1_01_SCHEMA = pa.schema([
    pa.field("accession_number", pa.string()),
    pa.field("cik_normalized", pa.string()),
    pa.field("agreement_event_seq", pa.int16()),
    pa.field("counterparty_name_raw", pa.string()),
    pa.field("counterparty_name_normalized", pa.string()),
    pa.field("agreement_type", pa.string()),
    pa.field("effective_date", pa.string()),
    pa.field("item_text_raw", pa.string()),
    pa.field("report_year", pa.int16()),
])

_ITEM_2_01_SCHEMA = pa.schema([
    pa.field("accession_number", pa.string()),
    pa.field("cik_normalized", pa.string()),
    pa.field("event_type", pa.string()),
    pa.field("acquirer_name_raw", pa.string()),
    pa.field("acquirer_name_normalized", pa.string()),
    pa.field("target_name_raw", pa.string()),
    pa.field("target_name_normalized", pa.string()),
    pa.field("consideration_summary", pa.string()),
    pa.field("consideration_value_usd", pa.float64()),
    pa.field("effective_date", pa.string()),
    pa.field("item_text_raw", pa.string()),
    pa.field("report_year", pa.int16()),
])

_ITEM_2_03_SCHEMA = pa.schema([
    pa.field("accession_number", pa.string()),
    pa.field("cik_normalized", pa.string()),
    pa.field("obligation_event_seq", pa.int16()),
    pa.field("obligation_type", pa.string()),
    pa.field("creditor_name_raw", pa.string()),
    pa.field("creditor_name_normalized", pa.string()),
    pa.field("obligation_amount_usd", pa.float64()),
    pa.field("effective_date", pa.string()),
    pa.field("item_text_raw", pa.string()),
    pa.field("report_year", pa.int16()),
])

_ITEM_5_01_SCHEMA = pa.schema([
    pa.field("accession_number", pa.string()),
    pa.field("cik_normalized", pa.string()),
    pa.field("acquirer_name_raw", pa.string()),
    pa.field("acquirer_name_normalized", pa.string()),
    pa.field("effective_date", pa.string()),
    pa.field("consideration_summary", pa.string()),
    pa.field("item_text_raw", pa.string()),
    pa.field("report_year", pa.int16()),
])

_ITEM_8_01_SCHEMA = pa.schema([
    pa.field("accession_number", pa.string()),
    pa.field("cik_normalized", pa.string()),
    pa.field("item_text_raw", pa.string()),
    pa.field("report_year", pa.int16()),
    pa.field("report_quarter", pa.int16()),
])

_STREAM_SCHEMAS: dict[str, pa.Schema] = {
    "filings": _FILINGS_SCHEMA,
    "items_index": _ITEMS_INDEX_SCHEMA,
    "item_5_02_officer_changes": _ITEM_5_02_SCHEMA,
    "item_1_01_material_agreement": _ITEM_1_01_SCHEMA,
    "item_2_01_acquisition_disposition": _ITEM_2_01_SCHEMA,
    "item_2_03_direct_financial_obligation": _ITEM_2_03_SCHEMA,
    "item_5_01_change_in_control": _ITEM_5_01_SCHEMA,
    "item_8_01_other_events": _ITEM_8_01_SCHEMA,
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
    year: int, quarter: int | None, stream: str,
    source_url: str | None = None,
) -> str:
    sql = """
    INSERT INTO ops.sec_edgar_form_8k_r2_ingest_runs
      (year, quarter, stream, status, source_url)
    VALUES (%s, %s, %s, 'running', %s)
    RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (year, quarter, stream, source_url))
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
    filings_skipped_count: int | None = None,
    parser_skipped_reason: str | None = None,
    started_at: float, error_message: str | None = None,
    notes: dict[str, Any] | None = None,
) -> None:
    duration = round(time.monotonic() - started_at, 3)
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE ops.sec_edgar_form_8k_r2_ingest_runs
               SET status = %s,
                   parquet_row_count = %s,
                   parquet_bytes_written = %s,
                   r2_bucket = %s, r2_key = %s,
                   filings_indexed_count = %s,
                   filings_fetched_count = %s,
                   filings_parsed_ok_count = %s,
                   filings_parsed_failed_count = %s,
                   filings_skipped_count = %s,
                   parser_skipped_reason = %s,
                   finished_at = now(), duration_seconds = %s,
                   error_message = %s, notes = %s
             WHERE id = %s;
            """, (
            status, parquet_row_count, parquet_bytes_written,
            r2_bucket, r2_key,
            filings_indexed_count, filings_fetched_count,
            filings_parsed_ok_count, filings_parsed_failed_count,
            filings_skipped_count, parser_skipped_reason,
            duration, error_message,
            Jsonb(notes) if notes else None, run_id,
        ))
    conn.commit()


# ------------------------------------------------------------------ #
# Per-quarter / per-year orchestration
# ------------------------------------------------------------------ #


@dataclass
class QuarterAccumulator:
    """High-volume per-quarter streams accumulate here; flushed at end of
    quarter."""
    year: int
    quarter: int
    filings: list[dict[str, Any]] = field(default_factory=list)
    items_index: list[dict[str, Any]] = field(default_factory=list)
    item_8_01_other_events: list[dict[str, Any]] = field(default_factory=list)
    parsed_ok: int = 0
    parsed_failed: int = 0
    skipped_pre_2004: int = 0
    indexed: int = 0

    def stream(self, name: str) -> list[dict[str, Any]]:
        return getattr(self, name)


@dataclass
class YearAccumulator:
    """Lower-volume per-year Item-specific streams accumulate here; flushed
    at end of year."""
    year: int
    item_5_02_officer_changes: list[dict[str, Any]] = field(default_factory=list)
    item_1_01_material_agreement: list[dict[str, Any]] = field(default_factory=list)
    item_2_01_acquisition_disposition: list[dict[str, Any]] = field(default_factory=list)
    item_2_03_direct_financial_obligation: list[dict[str, Any]] = field(default_factory=list)
    item_5_01_change_in_control: list[dict[str, Any]] = field(default_factory=list)

    def stream(self, name: str) -> list[dict[str, Any]]:
        return getattr(self, name)


async def discover_quarter(
    client: httpx.AsyncClient, year: int, quarter: int, *,
    limiter: RpsLimiter,
) -> list[FormIdxRow]:
    url = FORM_IDX_URL.format(year=year, q=quarter)
    # form.idx is the entry point of the entire quarter — if it fails, no
    # filings get processed, so retries are higher + slower than the
    # per-filing default.
    try:
        status, body = await fetch_text(
            client, url, limiter=limiter, timeout=120.0, retries=8,
        )
    except RuntimeError as exc:
        log.warning("[%d Q%d] form.idx fetch failed: %s", year, quarter, exc)
        return []
    if status != 200:
        log.warning("[%d Q%d] form.idx HTTP %s", year, quarter, status)
        return []
    try:
        text = body.decode("latin-1")
    except UnicodeDecodeError:
        text = body.decode("utf-8", "ignore")
    rows = parse_form_idx(text)
    log.info("[%d Q%d] form.idx → %d 8-K + 8-K/A rows", year, quarter, len(rows))
    return rows


async def ingest_one_filing(
    client: httpx.AsyncClient, idx_row: FormIdxRow, *,
    limiter: RpsLimiter, include_raw_html: bool, s3,
) -> tuple[bool, dict[str, list[dict[str, Any]]] | None, int, str | None]:
    """Returns ``(ok, parsed_streams, html_bytes, skip_reason)``.

    ``skip_reason='pre_2004_item_taxonomy'`` when the parser returns None due
    to pre-Aug-2004 filing detection (counted separately from parse failures).
    """
    primary_url, html = await fetch_filing_primary_html(
        client, idx_row, limiter=limiter,
    )
    if html is None:
        return False, None, 0, None

    raw_uri: str | None = None
    if include_raw_html:
        cik_padded = idx_row.cik.zfill(10)
        acc = idx_row.accession or "unknown"
        raw_key = f"sec-edgar/form-8k/raw/{cik_padded}/{acc}/8-k.html"
        try:
            upload_bytes_to_r2(
                s3, bucket=R2_BUCKET, key=raw_key, body=html,
                content_type="text/html",
            )
            raw_uri = f"s3://{R2_BUCKET}/{raw_key}"
            _ = raw_uri  # not stored on filings row in v1; kept for future
        except Exception as exc:
            log.warning("raw HTML upload failed for %s: %s",
                        idx_row.accession, exc)

    header = FilingHeader(
        cik_raw=idx_row.cik,
        company_name_raw=idx_row.company_name,
        accession_raw=idx_row.accession,
        form_type=idx_row.form_type,
        filing_date=idx_row.date_filed,
        primary_doc_url=primary_url or "",
    )
    try:
        parsed = parse_filing(header, html)
    except Exception as exc:
        log.warning("parse_filing %s/%s threw %s",
                    idx_row.cik, idx_row.accession, exc)
        return False, None, len(html), None
    if parsed is None:
        return False, None, len(html), "pre_2004_item_taxonomy"
    # Fallback: if the primary HTML parse produced zero Items (typical when
    # index.json picked an exhibit press-release instead of the cover page),
    # fetch the SEC .txt complete submission and re-parse the inner 8-K
    # <DOCUMENT> block. Cover page + Items always live there.
    if not parsed.get("items_index"):
        txt_html = await fetch_filing_txt_8k_html(
            client, idx_row, limiter=limiter,
        )
        if txt_html:
            try:
                fallback = parse_filing(header, txt_html)
            except Exception as exc:
                log.warning("txt fallback parse %s/%s threw %s",
                            idx_row.cik, idx_row.accession, exc)
                fallback = None
            if fallback and fallback.get("items_index"):
                parsed = fallback
    return True, parsed, len(html), None


async def process_quarter(
    *,
    year: int, quarter: int,
    client: httpx.AsyncClient,
    limiter: RpsLimiter,
    s3,
    db_url: str | None,
    state: dict[str, Any], state_path: Path,
    include_raw_html: bool, max_filings: int | None,
    year_acc: YearAccumulator,
) -> int:
    """Run discovery + concurrent fetch+parse for one (year, quarter).
    Flushes high-volume per-quarter streams at the end. Item-specific records
    accumulate into the supplied ``year_acc`` for end-of-year flush."""
    started_wall = time.monotonic()
    log.info("=" * 70)
    log.info("=== YEAR %d Q%d ===", year, quarter)
    log.info("=" * 70)

    quarter_acc = QuarterAccumulator(year=year, quarter=quarter)
    idx_rows = await discover_quarter(client, year, quarter, limiter=limiter)
    quarter_acc.indexed = len(idx_rows)

    # Discovery audit row.
    if db_url:
        with psycopg.connect(db_url) as conn:
            d_run = insert_run_row(
                conn, year=year, quarter=quarter, stream="discovery",
                source_url=FORM_IDX_URL.format(year=year, q=quarter),
            )
            finalize_run_row(
                conn, d_run, status="completed",
                filings_indexed_count=len(idx_rows),
                started_at=started_wall,
            )

    if not idx_rows:
        log.warning("[%d Q%d] no 8-K rows discovered", year, quarter)
        return 0

    if max_filings is not None and len(idx_rows) > max_filings:
        log.info("[%d Q%d] limiting to first %d filings (smoke)",
                 year, quarter, max_filings)
        idx_rows = idx_rows[:max_filings]

    # Resume support — state is keyed per (year, quarter).
    state_key = f"{year}-Q{quarter}"
    completed: set[str] = set(state.get(state_key, {}).get("completed", []))
    if completed:
        log.info("[%d Q%d] state file: %d accessions already done",
                 year, quarter, len(completed))
        idx_rows = [r for r in idx_rows if r.accession not in completed]
        log.info("[%d Q%d] remaining: %d", year, quarter, len(idx_rows))

    # Concurrent fetch + parse.
    sem = asyncio.Semaphore(HTTP_CONCURRENCY)
    results_lock = asyncio.Lock()

    async def _worker(idx_row: FormIdxRow) -> None:
        async with sem:
            ok, parsed, _bytes, skip_reason = await ingest_one_filing(
                client, idx_row, limiter=limiter,
                include_raw_html=include_raw_html, s3=s3,
            )
            async with results_lock:
                if ok and parsed is not None:
                    for stream_name in PER_QUARTER_STREAMS:
                        quarter_acc.stream(stream_name).extend(
                            parsed.get(stream_name, [])
                        )
                    for stream_name in PER_YEAR_STREAMS:
                        year_acc.stream(stream_name).extend(
                            parsed.get(stream_name, [])
                        )
                    quarter_acc.parsed_ok += 1
                    completed.add(idx_row.accession)
                else:
                    if skip_reason == "pre_2004_item_taxonomy":
                        quarter_acc.skipped_pre_2004 += 1
                        completed.add(idx_row.accession)
                    else:
                        quarter_acc.parsed_failed += 1
                seen = (
                    quarter_acc.parsed_ok
                    + quarter_acc.parsed_failed
                    + quarter_acc.skipped_pre_2004
                )
                if seen % RECORDS_LOG_EVERY == 0:
                    log.info(
                        "[%d Q%d] progress: %d/%d (ok=%d failed=%d skip=%d) "
                        "filings=%d items=%d 8.01=%d "
                        "5.02_year=%d 1.01_year=%d 2.01_year=%d "
                        "2.03_year=%d 5.01_year=%d",
                        year, quarter, seen, len(idx_rows),
                        quarter_acc.parsed_ok, quarter_acc.parsed_failed,
                        quarter_acc.skipped_pre_2004,
                        len(quarter_acc.filings),
                        len(quarter_acc.items_index),
                        len(quarter_acc.item_8_01_other_events),
                        len(year_acc.item_5_02_officer_changes),
                        len(year_acc.item_1_01_material_agreement),
                        len(year_acc.item_2_01_acquisition_disposition),
                        len(year_acc.item_2_03_direct_financial_obligation),
                        len(year_acc.item_5_01_change_in_control),
                    )

    # Periodic checkpoint task.
    stop_event = asyncio.Event()

    async def _checkpointer() -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=60.0)
            except asyncio.TimeoutError:
                pass
            async with results_lock:
                state[state_key] = {
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

    # Final state-file write.
    async with results_lock:
        state[state_key] = {
            "completed": sorted(completed),
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        try:
            state_path.write_text(json.dumps(state))
        except OSError as exc:
            log.warning("state write failed: %s", exc)

    # Flush high-volume per-quarter streams.
    rc = 0
    for stream_name in PER_QUARTER_STREAMS:
        recs = quarter_acc.stream(stream_name)
        schema = _STREAM_SCHEMAS[stream_name]
        r2_key = (
            f"sec-edgar/form-8k/year={year}/quarter={quarter}/"
            f"{stream_name}/data.parquet"
        )
        run_id: str | None = None
        stream_started = time.monotonic()
        if db_url:
            conn = psycopg.connect(db_url)
            try:
                run_id = insert_run_row(
                    conn, year=year, quarter=quarter, stream=stream_name,
                    source_url=f"s3://{R2_BUCKET}/{r2_key}",
                )
            finally:
                conn.close()

        if not recs:
            log.info("[%d Q%d/%s] no records — skipping upload",
                     year, quarter, stream_name)
            if db_url and run_id is not None:
                conn = psycopg.connect(db_url)
                try:
                    finalize_run_row(
                        conn, run_id, status="completed",
                        parquet_row_count=0, parquet_bytes_written=0,
                        r2_bucket=R2_BUCKET, r2_key=r2_key,
                        filings_indexed_count=quarter_acc.indexed,
                        filings_fetched_count=(
                            quarter_acc.parsed_ok
                            + quarter_acc.parsed_failed
                            + quarter_acc.skipped_pre_2004
                        ),
                        filings_parsed_ok_count=quarter_acc.parsed_ok,
                        filings_parsed_failed_count=quarter_acc.parsed_failed,
                        filings_skipped_count=quarter_acc.skipped_pre_2004,
                        parser_skipped_reason=(
                            "pre_2004_item_taxonomy"
                            if quarter_acc.skipped_pre_2004 > 0 else None
                        ),
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
                "[%d Q%d/%s] uploaded %d rows, %.2f MB → s3://%s/%s",
                year, quarter, stream_name, len(recs),
                uploaded / (1 << 20), R2_BUCKET, r2_key,
            )
            if db_url and run_id is not None:
                conn = psycopg.connect(db_url)
                try:
                    finalize_run_row(
                        conn, run_id, status="completed",
                        parquet_row_count=len(recs),
                        parquet_bytes_written=uploaded,
                        r2_bucket=R2_BUCKET, r2_key=r2_key,
                        filings_indexed_count=quarter_acc.indexed,
                        filings_fetched_count=(
                            quarter_acc.parsed_ok
                            + quarter_acc.parsed_failed
                            + quarter_acc.skipped_pre_2004
                        ),
                        filings_parsed_ok_count=quarter_acc.parsed_ok,
                        filings_parsed_failed_count=quarter_acc.parsed_failed,
                        filings_skipped_count=quarter_acc.skipped_pre_2004,
                        parser_skipped_reason=(
                            "pre_2004_item_taxonomy"
                            if quarter_acc.skipped_pre_2004 > 0 else None
                        ),
                        started_at=stream_started,
                    )
                finally:
                    conn.close()
        except Exception as exc:
            log.exception("[%d Q%d/%s] write/upload FAILED",
                          year, quarter, stream_name)
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
        "[%d Q%d] DONE parsed_ok=%d failed=%d skipped=%d wall=%.1fs",
        year, quarter, quarter_acc.parsed_ok, quarter_acc.parsed_failed,
        quarter_acc.skipped_pre_2004, time.monotonic() - started_wall,
    )
    return rc


def flush_year_streams(
    *, year: int, year_acc: YearAccumulator, s3, db_url: str | None,
) -> int:
    """Emit per-year ZSTD Parquet for the 4 lower-volume Item-specific
    streams. Called once per year after all 4 quarters complete.
    """
    rc = 0
    for stream_name in PER_YEAR_STREAMS:
        recs = year_acc.stream(stream_name)
        schema = _STREAM_SCHEMAS[stream_name]
        r2_key = (
            f"sec-edgar/form-8k/year={year}/"
            f"{stream_name}/data.parquet"
        )
        run_id: str | None = None
        stream_started = time.monotonic()
        if db_url:
            conn = psycopg.connect(db_url)
            try:
                run_id = insert_run_row(
                    conn, year=year, quarter=None, stream=stream_name,
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
                year, stream_name, len(recs),
                uploaded / (1 << 20), R2_BUCKET, r2_key,
            )
            if db_url and run_id is not None:
                conn = psycopg.connect(db_url)
                try:
                    finalize_run_row(
                        conn, run_id, status="completed",
                        parquet_row_count=len(recs),
                        parquet_bytes_written=uploaded,
                        r2_bucket=R2_BUCKET, r2_key=r2_key,
                        started_at=stream_started,
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
    return rc


# ------------------------------------------------------------------ #
# Top-level orchestration: years × quarters
# ------------------------------------------------------------------ #


async def process_year_all_quarters(
    *,
    year: int, quarters: list[int],
    state_path: Path,
    include_raw_html: bool,
    max_filings: int | None,
    db_url: str | None,
    target_rps: int,
) -> int:
    rc = 0
    state: dict[str, Any] = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
        except (json.JSONDecodeError, OSError):
            state = {}

    limiter = RpsLimiter(target_rps)
    s3 = _r2_client()
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}
    year_acc = YearAccumulator(year=year)

    async with httpx.AsyncClient(headers=headers, http2=False) as client:
        for q in quarters:
            try:
                rc_one = await process_quarter(
                    year=year, quarter=q, client=client,
                    limiter=limiter, s3=s3,
                    db_url=db_url, state=state, state_path=state_path,
                    include_raw_html=include_raw_html,
                    max_filings=max_filings, year_acc=year_acc,
                )
                if rc_one != 0:
                    rc = rc_one
            except Exception:
                log.exception("year %d Q%d failed", year, q)
                rc = 1

    rc_year = flush_year_streams(
        year=year, year_acc=year_acc, s3=s3, db_url=db_url,
    )
    if rc_year != 0:
        rc = rc_year
    return rc


# ------------------------------------------------------------------ #
# CLI
# ------------------------------------------------------------------ #


def _parse_year_range(s: str) -> list[int]:
    if "-" in s:
        a, b = s.split("-", 1)
        return list(range(int(a), int(b) + 1))
    return [int(s)]


def _parse_quarter_range(s: str) -> list[int]:
    if "-" in s:
        a, b = s.split("-", 1)
        out = list(range(int(a), int(b) + 1))
    elif "," in s:
        out = [int(x.strip()) for x in s.split(",")]
    else:
        out = [int(s)]
    bad = [q for q in out if q < 1 or q > 4]
    if bad:
        raise ValueError(f"Quarters must be 1-4: got {bad}")
    return out


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--years", required=True,
                   help="Year range e.g. 2020-2026 (REQUIRED). "
                        "Reject if any year < 2003.")
    p.add_argument("--quarters", default="1-4",
                   help="Quarters to ingest, e.g. '1-4', '1', '1,2,3'. "
                        "Default: 1-4 (all quarters).")
    p.add_argument("--max-filings", type=int, default=None,
                   help="Per-quarter cap (smoke).")
    p.add_argument("--target-rps", type=int, default=TARGET_RPS,
                   help=f"Override TARGET_RPS (default {TARGET_RPS} — set via "
                        f"SEC_EDGAR_TARGET_RPS env). Use 5 for solo runs once "
                        f"sibling SEC EDGAR crons complete.")
    p.add_argument("--include-raw-html", action="store_true",
                   help="Also upload raw HTML for each parsed filing.")
    p.add_argument("--state-file", default="/tmp/sec_edgar_form_8k_state.json",
                   help="Resume checkpoint file.")
    p.add_argument("--no-audit", action="store_true",
                   help="Skip ops.sec_edgar_form_8k_r2_ingest_runs writes "
                        "(useful for smoke when DB is unavailable).")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    years: Iterable[int] = _parse_year_range(args.years)
    bad = [y for y in years if y < 2003]
    if bad:
        log.error("years before 2003 are out of scope per directive: %s", bad)
        return 2
    quarters = _parse_quarter_range(args.quarters)

    state_path = Path(args.state_file)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    db_url = None if args.no_audit else _database_url()
    if not args.no_audit and not db_url:
        log.warning("no DB URL set; audit ledger writes will be skipped")

    rc = 0
    for y in years:
        try:
            rc_one = asyncio.run(process_year_all_quarters(
                year=y, quarters=quarters,
                state_path=state_path,
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

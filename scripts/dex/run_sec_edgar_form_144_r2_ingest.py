#!/usr/bin/env python3
"""SEC EDGAR Form 144 / 144/A → R2 Parquet ingest.

For each year in the configured span:
  1. Pull EDGAR full-index ``form.idx`` for QTR1–QTR4, filter to Form Types
     ``144`` and ``144/A``.
  2. Concurrently fetch + parse each filing's primary doc — XML-first
     (``primary_doc.xml`` etc.) with HTML fallback for legacy filings
     (asyncio + 2 RPS rate-limit per parallel-execution constraint,
     16 concurrent workers).
  3. Buffer parsed records into 4 streams (filings, securities_to_be_sold,
     securities_sold_past_3_months, acquisition_info) and emit ZSTD Parquet at:
        s3://dex-raw-landing-zone/sec-edgar/form-144/year={Y}/{stream}/data.parquet
  4. Optionally also upload raw primary-doc bytes at:
        s3://dex-raw-landing-zone/sec-edgar/form-144/raw/{cik}/{accession}/primary.{xml,html}

Idempotency: per (year, accession) — the orchestrator keeps a JSON checkpoint
file at ``--state-file`` so a network blip / session timeout doesn't restart
from zero. Re-running with the same state file resumes where it left off.

Audit: one row per (year, stream) in ops.sec_edgar_form_144_r2_ingest_runs.

Parallel-execution constraint: TARGET_RPS=2 because three SEC EDGAR per-filing
ingests run concurrently (Schedule 13D/G, Form 144, Form 13F) sharing the
same egress IP under SEC's 10 RPS aggregate cap. See directive
~/Desktop/hq/directives/2026-05-09-sec-edgar-form-144-r2-ingest.md
§"Parallel-execution constraint".

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_sec_edgar_form_144_r2_ingest.py 2024 --max-filings 1000
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_sec_edgar_form_144_r2_ingest.py --years 2010-2024
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_sec_edgar_form_144_r2_ingest.py --years 2010-2024 \\
        --include-raw
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
from _lib.sec_edgar_form_144_parser import FilingHeader, parse_filing  # noqa: E402


# ------------------------------------------------------------------ #
# Constants
# ------------------------------------------------------------------ #

R2_BUCKET = "dex-raw-landing-zone"
PROVIDER = "sec_edgar_form_144"

EDGAR_HOST = "https://www.sec.gov"
FORM_IDX_URL = (
    "https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{q}/form.idx"
)

# Parallel with up to 4 other SEC EDGAR per-filing ingests sharing the same
# egress IP under SEC's 10 RPS aggregate cap. Empirically, 2 RPS produced a
# ~50% loss rate on 2012 (massive 429 storm with retries exhausted) when
# concurrent harnesses also pushed near the cap; lowering to 1 RPS leaves
# more headroom for retries. Per the directive's parallel-execution
# constraint: do NOT raise this — the global IP-level limit is shared.
TARGET_RPS = 1
HTTP_CONCURRENCY = 16

RECORDS_LOG_EVERY = 200

USER_AGENT = (
    "data-engine-x/sec-edgar-form-144-ingest "
    "tools@substrate.build "
    "(operational research)"
)

STREAMS: tuple[str, ...] = (
    "filings",
    "securities_to_be_sold",
    "securities_sold_past_3_months",
    "acquisition_info",
)

TARGET_FORM_TYPES: frozenset[str] = frozenset({"144", "144/A"})


# ------------------------------------------------------------------ #
# Logging
# ------------------------------------------------------------------ #

def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("sec-edgar-form-144-ingest")


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


def parse_form_idx(text: str) -> list[FormIdxRow]:
    """Parse a form.idx body into typed rows for Form 144 + 144/A.

    form.idx is fixed-width with a header block, dashes line, then rows. We
    split each row on whitespace runs of 2+ chars; assemble the 5 fields.
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
        if form_type not in TARGET_FORM_TYPES:
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
    """Token-bucket: limit to ``rps`` calls per second."""

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
    timeout: float = 60.0, retries: int = 6,
) -> tuple[int, bytes]:
    """Fetch URL with retry. Returns (status_code, body)."""
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
# Per-filing pipeline (resolve primary doc → parse)
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


_XML_NAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^primary_doc\.xml$", re.I),
    re.compile(r"^xslf144x.*\.xml$", re.I),
    re.compile(r"^form_?144.*\.xml$", re.I),
    re.compile(r"^.*\.xml$", re.I),
)


def _select_primary_doc(idx_json: dict[str, Any]) -> tuple[str, str] | None:
    """Choose the primary doc name + format ('xml' | 'html').

    XML preferred (modern e-filing). Falls back to the largest .htm/.html.
    Returns (filename, format) or None.
    """
    items = idx_json.get("directory", {}).get("item", [])
    if not items:
        return None
    xml_candidates: list[tuple[int, str, int]] = []
    html_candidates: list[tuple[int, str]] = []
    for it in items:
        name = it.get("name", "")
        if not name:
            continue
        size = int(it.get("size") or 0)
        lower = name.lower()
        if lower.endswith(".xml"):
            for rank, pat in enumerate(_XML_NAME_PATTERNS):
                if pat.match(lower):
                    xml_candidates.append((rank, name, size))
                    break
        elif lower.endswith((".htm", ".html")):
            if "index" in lower or "header" in lower:
                continue
            html_candidates.append((size, name))

    if xml_candidates:
        xml_candidates.sort(key=lambda t: (t[0], -t[2]))
        return (xml_candidates[0][1], "xml")
    if html_candidates:
        html_candidates.sort(reverse=True)
        return (html_candidates[0][1], "html")
    return None


async def fetch_filing_primary_doc(
    client: httpx.AsyncClient, idx_row: FormIdxRow, *, limiter: RpsLimiter,
) -> tuple[str | None, str | None, bytes | None]:
    """Resolve + fetch the primary doc for a 144/144/A accession.

    Returns ``(primary_doc_url, format, doc_bytes)`` or all-None on failure.
    """
    idx_json = await fetch_filing_index_json(client, idx_row, limiter=limiter)
    if idx_json is None:
        return None, None, None
    pick = _select_primary_doc(idx_json)
    if pick is None:
        return None, None, None
    primary_name, fmt = pick
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
        return primary_url, fmt, None
    if status != 200:
        return primary_url, fmt, None
    return primary_url, fmt, body


# ------------------------------------------------------------------ #
# pyarrow schemas
# ------------------------------------------------------------------ #


_FILINGS_SCHEMA = pa.schema([
    pa.field("accession_number", pa.string()),
    pa.field("cik_normalized", pa.string()),
    pa.field("form_type", pa.string()),
    pa.field("amendment_number", pa.int16()),
    pa.field("issuer_legal_name_normalized", pa.string()),
    pa.field("issuer_lei_normalized", pa.string()),
    pa.field("filer_legal_name_normalized", pa.string()),
    pa.field("person_signing_name", pa.string()),
    pa.field("person_first_normalized", pa.string()),
    pa.field("person_last_normalized", pa.string()),
    pa.field("person_relationship_to_issuer", pa.string()),
    pa.field("person_relationship_to_issuer_normalized", pa.string()),
    pa.field("filing_date", pa.string()),
    pa.field("period_of_report", pa.string()),
    pa.field("form_144_year", pa.int16()),
    pa.field("xml_or_html", pa.string()),
    pa.field("raw_doc_r2_uri", pa.string()),
])

_SEC_TBS_SCHEMA = pa.schema([
    pa.field("accession_number", pa.string()),
    pa.field("cik_normalized", pa.string()),
    pa.field("person_signing_name", pa.string()),
    pa.field("person_first_normalized", pa.string()),
    pa.field("person_last_normalized", pa.string()),
    pa.field("class_of_securities", pa.string()),
    pa.field("aggregate_market_value", pa.float64()),
    pa.field("number_of_shares", pa.float64()),
    pa.field("name_of_broker", pa.string()),
    pa.field("broker_normalized", pa.string()),
    pa.field("approximate_sale_date", pa.string()),
    pa.field("form_144_year", pa.int16()),
])

_SEC_PAST_3M_SCHEMA = pa.schema([
    pa.field("accession_number", pa.string()),
    pa.field("cik_normalized", pa.string()),
    pa.field("person_signing_name", pa.string()),
    pa.field("person_first_normalized", pa.string()),
    pa.field("person_last_normalized", pa.string()),
    pa.field("seller_name", pa.string()),
    pa.field("seller_normalized", pa.string()),
    pa.field("class_of_securities", pa.string()),
    pa.field("sale_date", pa.string()),
    pa.field("shares_sold", pa.float64()),
    pa.field("gross_proceeds", pa.float64()),
    pa.field("form_144_year", pa.int16()),
])

_ACQ_SCHEMA = pa.schema([
    pa.field("accession_number", pa.string()),
    pa.field("cik_normalized", pa.string()),
    pa.field("person_signing_name", pa.string()),
    pa.field("date_acquired", pa.string()),
    pa.field("nature_of_acquisition", pa.string()),
    pa.field("nature_normalized", pa.string()),
    pa.field("payor_identity", pa.string()),
    pa.field("payor_normalized", pa.string()),
    pa.field("cost_basis_per_share", pa.float64()),
    pa.field("form_144_year", pa.int16()),
])

_STREAM_SCHEMAS: dict[str, pa.Schema] = {
    "filings": _FILINGS_SCHEMA,
    "securities_to_be_sold": _SEC_TBS_SCHEMA,
    "securities_sold_past_3_months": _SEC_PAST_3M_SCHEMA,
    "acquisition_info": _ACQ_SCHEMA,
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
    INSERT INTO ops.sec_edgar_form_144_r2_ingest_runs
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
            UPDATE ops.sec_edgar_form_144_r2_ingest_runs
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
    securities_to_be_sold: list[dict[str, Any]] = field(default_factory=list)
    securities_sold_past_3_months: list[dict[str, Any]] = field(default_factory=list)
    acquisition_info: list[dict[str, Any]] = field(default_factory=list)
    parsed_ok: int = 0
    parsed_failed: int = 0
    fmt_xml: int = 0
    fmt_html: int = 0

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
        rows = parse_form_idx(text)
        log.info("[%d] QTR%d form.idx → %d 144/144/A rows", year, q, len(rows))
        out.extend(rows)
    return out


async def ingest_one_filing(
    client: httpx.AsyncClient, idx_row: FormIdxRow, *,
    limiter: RpsLimiter, include_raw: bool, s3,
) -> tuple[bool, dict[str, list[dict[str, Any]]] | None, str | None]:
    """Fetch + parse one filing.

    Returns ``(success, parsed_streams, format)`` — format is 'xml' or 'html'
    when the doc was fetched, else None. ``success`` is True iff the doc was
    fetched and the parser returned the canonical filings row (always 1).
    """
    primary_url, fmt, body = await fetch_filing_primary_doc(
        client, idx_row, limiter=limiter,
    )
    if body is None or fmt is None:
        return False, None, fmt

    raw_uri: str | None = None
    if include_raw:
        cik_padded = idx_row.cik.zfill(10)
        acc = idx_row.accession or "unknown"
        ext = "xml" if fmt == "xml" else "html"
        raw_key = f"sec-edgar/form-144/raw/{cik_padded}/{acc}/primary.{ext}"
        try:
            upload_bytes_to_r2(
                s3, bucket=R2_BUCKET, key=raw_key, body=body,
                content_type=("application/xml" if fmt == "xml" else "text/html"),
            )
            raw_uri = f"s3://{R2_BUCKET}/{raw_key}"
        except Exception as exc:
            log.warning("raw upload failed for %s: %s",
                        idx_row.accession, exc)

    header = FilingHeader(
        cik_raw=idx_row.cik,
        filer_name_raw=idx_row.company_name,
        accession_raw=idx_row.accession,
        form_type=idx_row.form_type,
        filing_date=idx_row.date_filed,
        primary_doc_url=primary_url or "",
        primary_doc_format=fmt,
        raw_doc_r2_uri=raw_uri,
    )
    try:
        parsed = parse_filing(header, body)
    except Exception as exc:
        log.warning("parse_filing %s/%s threw %s",
                    idx_row.cik, idx_row.accession, exc)
        return False, None, fmt

    return True, parsed, fmt


async def process_year(
    year: int, *,
    state_path: Path,
    include_raw: bool,
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
        log.info("[%d] total 144/144/A indexed: %d", year, len(idx_rows))

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
            log.warning("[%d] no 144/144/A rows discovered", year)
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
                ok, parsed, fmt = await ingest_one_filing(
                    client, idx_row, limiter=limiter,
                    include_raw=include_raw, s3=s3,
                )
                async with results_lock:
                    if ok and parsed is not None:
                        for stream_name in STREAMS:
                            accumulator.stream(stream_name).extend(
                                parsed.get(stream_name, [])
                            )
                        accumulator.parsed_ok += 1
                        if fmt == "xml":
                            accumulator.fmt_xml += 1
                        elif fmt == "html":
                            accumulator.fmt_html += 1
                        completed.add(idx_row.accession)
                    else:
                        accumulator.parsed_failed += 1
                    seen = accumulator.parsed_ok + accumulator.parsed_failed
                    if seen % RECORDS_LOG_EVERY == 0:
                        log.info(
                            "[%d] progress: %d/%d (ok=%d, failed=%d, xml=%d, html=%d) "
                            "filings=%d tbs=%d past3m=%d acq=%d",
                            year, seen, len(idx_rows),
                            accumulator.parsed_ok, accumulator.parsed_failed,
                            accumulator.fmt_xml, accumulator.fmt_html,
                            len(accumulator.filings),
                            len(accumulator.securities_to_be_sold),
                            len(accumulator.securities_sold_past_3_months),
                            len(accumulator.acquisition_info),
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
            r2_key = f"sec-edgar/form-144/year={year}/{stream_name}/data.parquet"
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

            stream_notes = {
                "xml_count": accumulator.fmt_xml,
                "html_count": accumulator.fmt_html,
            }

            if not recs:
                log.info("[%d/%s] no records — skipping upload", year, stream_name)
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
                            notes={**stream_notes, "empty": True},
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
                            notes=stream_notes,
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
                            notes=stream_notes,
                        )
                    finally:
                        conn.close()

        log.info(
            "[%d] DONE parsed_ok=%d failed=%d xml=%d html=%d wall=%.1fs",
            year, accumulator.parsed_ok, accumulator.parsed_failed,
            accumulator.fmt_xml, accumulator.fmt_html,
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
                   help="Year range e.g. 2010-2024.")
    p.add_argument("--max-filings", type=int, default=None,
                   help="Per-year cap (smoke).")
    p.add_argument("--include-raw", action="store_true",
                   help="Also upload raw primary doc for each parsed filing.")
    p.add_argument("--state-file", default="/tmp/sec_edgar_form_144_state.json",
                   help="Resume checkpoint file.")
    p.add_argument("--no-audit", action="store_true",
                   help="Skip ops.sec_edgar_form_144_r2_ingest_runs writes.")
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
                include_raw=args.include_raw,
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

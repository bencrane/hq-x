#!/usr/bin/env python3
"""SEC EDGAR Form ABS-15G → R2 Parquet ingest.

For each (year, quarter) in the configured span:
  1. Pull the EDGAR full-index ``form.idx`` for the quarter and filter to
     Form ABS-15G rows (ABS-15G | ABS-15G/A).
  2. Concurrently fetch + parse each filing's primary_doc.xml + Exhibit 99.
     asyncio + 1 RPS rate-limit (validator p1 inherited constraint — SEC
     fair-use aggregate cap across 6 SEC EDGAR crons), 16 concurrent workers.
  3. Buffer parsed records into 2 streams (filings, repurchase_summary) and
     emit ZSTD Parquet to R2:
        sec-edgar/form-abs-15g/year={Y}/filings/data.parquet              (per-year)
        sec-edgar/form-abs-15g/year={Y}/quarter={Q}/repurchase_summary/data.parquet  (per-quarter)
  4. Optionally also upload raw primary_doc.xml + Exhibit 99 when
     --include-raw-xml is set (off by default per directive).

Idempotency: per (year, quarter, accession) — the orchestrator keeps a
JSON checkpoint at ``--state-file`` so a network blip / session timeout
doesn't restart from zero.

Audit: rows per (year, quarter, stream) in ops.sec_edgar_form_abs_15g_r2_ingest_runs.

Parallel-execution constraint: TARGET_RPS=1 default (validator p1) because
this script will run on a Modal cron alongside 5 other SEC EDGAR crons
sharing aggregate SEC fair-use budget (10 RPS cap). The Modal wrapper may
override per-tick via SEC_EDGAR_TARGET_RPS env var when scheduled in an
exclusive 6h window.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_sec_edgar_form_abs_15g_r2_ingest.py \\
        --years 2024 --quarters 1 --max-filings 200
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_sec_edgar_form_abs_15g_r2_ingest.py \\
        --years 2012-2026
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
from _lib.sec_edgar_form_abs_15g_parser import FilingHeader, parse_filing  # noqa: E402


R2_BUCKET = "dex-raw-landing-zone"
PROVIDER = "sec_edgar_form_abs_15g"

EDGAR_HOST = "https://www.sec.gov"
FORM_IDX_URL = (
    "https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{q}/form.idx"
)

# TARGET_RPS=1 default per validator p1 (inherited from parent's SEC EDGAR
# aggregate fair-use cap constraint). Overridable via SEC_EDGAR_TARGET_RPS
# env var. Modal-deployed instances each have an independent egress IP, so
# the per-IP cap applies per Modal app — the Modal wrapper may override to
# 5 (matching form-13f's wrapper). The bare-script default stays at 1 for
# local-machine / multi-script-shared-egress safety.
TARGET_RPS = int(os.environ.get("SEC_EDGAR_TARGET_RPS", "1"))
HTTP_CONCURRENCY = int(os.environ.get("SEC_EDGAR_HTTP_CONCURRENCY", "16"))

RECORDS_LOG_EVERY = 100

USER_AGENT = (
    "data-engine-x/sec-edgar-form-abs-15g-ingest "
    "tools@substrate.build "
    "(operational research)"
)

TARGET_FORMS: frozenset[str] = frozenset({"ABS-15G", "ABS-15G/A"})


# ------------------------------------------------------------------ #
# Logging
# ------------------------------------------------------------------ #

def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("sec-edgar-form-abs-15g-ingest")


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
# Form index parser (shared shape with form-13f)
# ------------------------------------------------------------------ #


@dataclass(frozen=True)
class FormIdxRow:
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
    """Parse a form.idx body into typed rows for any form_type in
    ``target_forms``."""
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
# Rate-limited fetcher
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


async def fetch_bytes(
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


async def fetch_filing_index_json(
    client: httpx.AsyncClient, idx_row: FormIdxRow, *, limiter: RpsLimiter,
) -> tuple[dict[str, Any] | None, str | None]:
    cik_int = idx_row.cik.lstrip("0") or "0"
    acc = idx_row.accession.replace("-", "")
    if not acc:
        return None, None
    base = f"{EDGAR_HOST}/Archives/edgar/data/{cik_int}/{acc}"
    url = f"{base}/index.json"
    try:
        status, body = await fetch_bytes(client, url, limiter=limiter)
    except RuntimeError:
        return None, base
    if status != 200:
        return None, base
    try:
        return json.loads(body), base
    except json.JSONDecodeError:
        return None, base


def _select_exhibit_files(idx_json: dict[str, Any]) -> tuple[str | None, str | None]:
    """From an EDGAR index.json choose the primary ABS-15G doc + Exhibit 99.

    ABS-15G filename reality (2012-2024 sample investigation, 2026-05-13):
      - NO filing uses a literal ``primary_doc.xml``. That convention is form-13f
        / NPORT / etc.; ABS-15G never adopted it.
      - The "primary document" is a single ``.htm``/``.html`` file whose name
        either contains ``abs15g`` / ``abs-15g`` (case-insensitive) OR is
        named after the issuer (e.g. ``n4035-x1_3650calbridge.htm``,
        ``anthracite-abs-15g_0621.htm``).
      - Exhibit 99 (rep-and-warranty schedule) when present ships as
        ``ex99-N.htm`` / ``e<accession>ex99_N.htm`` / ``d<accession>dex99N.htm``.
      - Many filings (Rule 15Ga-1(c) "no activity" annual notices) have ONLY
        the primary HTM; no separate Exhibit 99.

    Heuristic (3-tier, in order):
      1. Legacy fallback: literal ``primary_doc.xml`` if present.
      2. ABS-15G filename pattern: any ``.htm``/``.html`` whose lowercased
         basename contains ``abs15g`` or ``abs-15g``.
      3. Largest non-{index,header,ex99,image,jpg,gif,png} HTM/HTML file.
         Captures issuer-named primaries (``n4035-x1_3650calbridge.htm``).
    """
    items = idx_json.get("directory", {}).get("item", [])
    if not items:
        return None, None

    primary: str | None = None
    abs15g_candidates: list[tuple[int, str]] = []
    generic_html_candidates: list[tuple[int, str]] = []
    exhibit_candidates: list[tuple[int, str]] = []

    for it in items:
        name = it.get("name", "")
        try:
            size = int(it.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        lname = name.lower()

        if lname == "primary_doc.xml":
            primary = name
            continue

        if re.match(r"^(ex|exhibit)[-_]?99", lname) or re.search(r"d?ex99[_\-]?\d*", lname):
            exhibit_candidates.append((size, name))
            continue

        # Skip EDGAR-internal navigation/header artifacts.
        if (
            "-index" in lname
            or "-headers" in lname
            or lname.endswith((".jpg", ".gif", ".png", ".jpeg"))
            or lname.endswith(".txt")  # the .txt is the full SGML wrapper, not the form
        ):
            continue

        if lname.endswith((".htm", ".html")):
            if "abs15g" in lname or "abs-15g" in lname:
                abs15g_candidates.append((size, name))
            else:
                generic_html_candidates.append((size, name))

    if primary is None:
        if abs15g_candidates:
            abs15g_candidates.sort(reverse=True)
            primary = abs15g_candidates[0][1]
        elif generic_html_candidates:
            generic_html_candidates.sort(reverse=True)
            primary = generic_html_candidates[0][1]

    exhibit: str | None = None
    if exhibit_candidates:
        exhibit_candidates.sort(reverse=True)
        exhibit = exhibit_candidates[0][1]
    return primary, exhibit


async def fetch_filing_artifacts(
    client: httpx.AsyncClient, idx_row: FormIdxRow, *, limiter: RpsLimiter,
) -> tuple[str | None, bytes | None, str | None, bytes | None]:
    """Resolve + fetch primary_doc.xml + Exhibit 99 bytes."""
    idx_json, base = await fetch_filing_index_json(
        client, idx_row, limiter=limiter,
    )
    primary_name: str | None = None
    exhibit_name: str | None = None
    if idx_json is not None:
        primary_name, exhibit_name = _select_exhibit_files(idx_json)

    if base is None:
        return None, None, None, None

    primary_url = f"{base}/{primary_name}" if primary_name else None
    exhibit_url = f"{base}/{exhibit_name}" if exhibit_name else None

    primary_bytes: bytes | None = None
    exhibit_bytes: bytes | None = None
    if primary_url is not None:
        try:
            status, body = await fetch_bytes(
                client, primary_url, limiter=limiter, timeout=120.0,
            )
            if status == 200:
                primary_bytes = body
        except RuntimeError:
            pass

    if exhibit_url is not None:
        try:
            status, body = await fetch_bytes(
                client, exhibit_url, limiter=limiter, timeout=180.0,
            )
            if status == 200:
                exhibit_bytes = body
        except RuntimeError:
            pass

    return primary_url, primary_bytes, exhibit_url, exhibit_bytes


# ------------------------------------------------------------------ #
# pyarrow schemas
# ------------------------------------------------------------------ #


_FILINGS_SCHEMA = pa.schema([
    pa.field("accession_number", pa.string()),
    pa.field("cik_normalized", pa.string()),
    pa.field("filer_name_raw", pa.string()),
    pa.field("filer_name_normalized", pa.string()),
    pa.field("depositor_name_raw", pa.string()),
    pa.field("depositor_name_normalized", pa.string()),
    pa.field("sponsor_name_raw", pa.string()),
    pa.field("sponsor_name_normalized", pa.string()),
    pa.field("trustee_name_raw", pa.string()),
    pa.field("trustee_name_normalized", pa.string()),
    pa.field("filer_lei_normalized", pa.string()),
    pa.field("form_type", pa.string()),
    pa.field("filing_date", pa.string()),
    pa.field("period_of_report", pa.string()),
    pa.field("report_year", pa.int16()),
    pa.field("report_quarter", pa.int16()),
    pa.field("asset_class_count", pa.int32()),
    pa.field("total_demand_count", pa.int64()),
    pa.field("total_repurchase_count", pa.int64()),
    pa.field("total_replacement_count", pa.int64()),
    pa.field("total_dollar_amount", pa.int64()),
    pa.field("primary_doc_url", pa.string()),
    pa.field("exhibit_url", pa.string()),
    pa.field("raw_xml_r2_uri", pa.string()),
    pa.field("exhibit_format", pa.string()),
])

_REPURCHASE_SUMMARY_SCHEMA = pa.schema([
    pa.field("accession_number", pa.string()),
    pa.field("cik_normalized", pa.string()),
    pa.field("asset_class", pa.string()),
    pa.field("asset_class_raw", pa.string()),
    pa.field("reporting_period", pa.string()),
    pa.field("demand_count", pa.int64()),
    pa.field("repurchase_count", pa.int64()),
    pa.field("replacement_count", pa.int64()),
    pa.field("dollar_amount", pa.int64()),
    pa.field("report_year", pa.int16()),
    pa.field("report_quarter", pa.int16()),
])

_STREAM_SCHEMAS: dict[str, pa.Schema] = {
    "filings": _FILINGS_SCHEMA,
    "repurchase_summary": _REPURCHASE_SUMMARY_SCHEMA,
}


def _records_to_table(records: list[dict[str, Any]], schema: pa.Schema) -> pa.Table:
    cols: dict[str, list[Any]] = {f.name: [] for f in schema}
    for rec in records:
        for f in schema:
            cols[f.name].append(rec.get(f.name))
    arrays = [pa.array(cols[f.name], type=f.type) for f in schema]
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
    source_observed_at: datetime | None = None,
) -> str:
    sql = """
    INSERT INTO ops.sec_edgar_form_abs_15g_r2_ingest_runs
      (year, quarter, stream, status, source_url, source_observed_at)
    VALUES (%s, %s, %s, 'running', %s, %s)
    RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (year, quarter, stream, source_url, source_observed_at))
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
            UPDATE ops.sec_edgar_form_abs_15g_r2_ingest_runs
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
# Per-quarter accumulator + orchestrator
# ------------------------------------------------------------------ #


@dataclass
class QuarterAccumulator:
    year: int
    quarter: int
    filings: list[dict[str, Any]] = field(default_factory=list)
    repurchase_summary: list[dict[str, Any]] = field(default_factory=list)
    parsed_ok: int = 0
    parsed_failed: int = 0


async def discover_quarter(
    client: httpx.AsyncClient, year: int, quarter: int, *, limiter: RpsLimiter,
) -> list[FormIdxRow]:
    url = FORM_IDX_URL.format(year=year, q=quarter)
    try:
        status, body = await fetch_bytes(
            client, url, limiter=limiter, timeout=60.0,
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
    rows = parse_form_idx(text, target_forms=TARGET_FORMS)
    log.info("[%d Q%d] form.idx → %d Form ABS-15G rows", year, quarter, len(rows))
    return rows


async def ingest_one_filing(
    client: httpx.AsyncClient, idx_row: FormIdxRow, *,
    limiter: RpsLimiter, include_raw_xml: bool, s3,
) -> tuple[bool, dict[str, Any] | None]:
    primary_url, primary_bytes, exhibit_url, exhibit_bytes = await fetch_filing_artifacts(
        client, idx_row, limiter=limiter,
    )
    if primary_bytes is None and exhibit_bytes is None:
        return False, None

    raw_uri: str | None = None
    if include_raw_xml:
        cik_padded = idx_row.cik.zfill(10)
        acc = idx_row.accession or "unknown"
        try:
            if primary_bytes is not None:
                primary_key = f"sec-edgar/form-abs-15g/raw/{cik_padded}/{acc}/primary_doc.xml"
                upload_bytes_to_r2(
                    s3, bucket=R2_BUCKET, key=primary_key, body=primary_bytes,
                    content_type="application/xml",
                )
                raw_uri = f"s3://{R2_BUCKET}/{primary_key}"
            if exhibit_bytes is not None:
                ext = "xml" if exhibit_url and exhibit_url.lower().endswith(".xml") else (
                    "pdf" if exhibit_url and exhibit_url.lower().endswith(".pdf") else "html"
                )
                ex_key = f"sec-edgar/form-abs-15g/raw/{cik_padded}/{acc}/exhibit99.{ext}"
                upload_bytes_to_r2(
                    s3, bucket=R2_BUCKET, key=ex_key, body=exhibit_bytes,
                    content_type="application/octet-stream",
                )
        except Exception as exc:
            log.warning("raw upload failed for %s: %s",
                        idx_row.accession, exc)

    header = FilingHeader(
        cik_raw=idx_row.cik,
        filer_name_raw=idx_row.company_name,
        accession_raw=idx_row.accession,
        filing_date=idx_row.date_filed,
        form_type=idx_row.form_type,
        primary_doc_url=primary_url or "",
        exhibit_url=exhibit_url,
        raw_xml_r2_uri=raw_uri,
    )
    try:
        parsed = parse_filing(header, primary_bytes, exhibit_bytes)
    except Exception as exc:
        log.warning("parse_filing %s/%s threw %s",
                    idx_row.cik, idx_row.accession, exc)
        return False, None
    if not parsed["filings"]:
        return False, None
    return True, parsed


async def process_quarter(
    *, year: int, quarter: int,
    state_path: Path,
    include_raw_xml: bool,
    max_filings: int | None,
    db_url: str | None,
    accumulator: QuarterAccumulator,
    limiter: RpsLimiter,
    s3,
) -> int:
    started_wall = time.monotonic()
    log.info("=" * 70)
    log.info("=== %d Q%d ===", year, quarter)
    log.info("=" * 70)

    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}
    async with httpx.AsyncClient(headers=headers, http2=False) as client:
        idx_rows = await discover_quarter(client, year, quarter, limiter=limiter)
        log.info("[%d Q%d] total Form ABS-15G indexed: %d",
                 year, quarter, len(idx_rows))

        discovery_run_id: str | None = None
        if db_url:
            with psycopg.connect(db_url) as conn:
                discovery_run_id = insert_run_row(
                    conn, year=year, quarter=quarter, stream="discovery",
                    source_url=FORM_IDX_URL.format(year=year, q=quarter),
                )
                finalize_run_row(
                    conn, discovery_run_id, status="completed",
                    filings_indexed_count=len(idx_rows),
                    started_at=started_wall,
                )

        if not idx_rows:
            return 0

        if max_filings is not None and len(idx_rows) > max_filings:
            log.info("[%d Q%d] limiting to first %d filings (smoke)",
                     year, quarter, max_filings)
            idx_rows = idx_rows[:max_filings]

        completed: set[str] = set()
        state: dict[str, Any] = {}
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text())
            except (json.JSONDecodeError, OSError):
                state = {}
        quarter_key = f"{year}Q{quarter}"
        completed = set(state.get(quarter_key, {}).get("completed", []))
        if completed:
            log.info("[%d Q%d] state: %d accessions already done",
                     year, quarter, len(completed))
            idx_rows = [r for r in idx_rows if r.accession not in completed]
            log.info("[%d Q%d] remaining: %d", year, quarter, len(idx_rows))

        sem = asyncio.Semaphore(HTTP_CONCURRENCY)
        results_lock = asyncio.Lock()

        async def _worker(idx_row: FormIdxRow) -> None:
            async with sem:
                ok, parsed = await ingest_one_filing(
                    client, idx_row, limiter=limiter,
                    include_raw_xml=include_raw_xml, s3=s3,
                )
                async with results_lock:
                    if ok and parsed is not None:
                        accumulator.filings.extend(parsed["filings"])
                        accumulator.repurchase_summary.extend(parsed["repurchase_summary"])
                        accumulator.parsed_ok += 1
                        completed.add(idx_row.accession)
                    else:
                        accumulator.parsed_failed += 1
                    seen = accumulator.parsed_ok + accumulator.parsed_failed
                    if seen % RECORDS_LOG_EVERY == 0:
                        log.info(
                            "[%d Q%d] progress: %d/%d (ok=%d, failed=%d) "
                            "filings=%d summary=%d",
                            year, quarter, seen, len(idx_rows),
                            accumulator.parsed_ok, accumulator.parsed_failed,
                            len(accumulator.filings),
                            len(accumulator.repurchase_summary),
                        )

        stop_event = asyncio.Event()

        async def _checkpointer() -> None:
            while not stop_event.is_set():
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=60.0)
                except asyncio.TimeoutError:
                    pass
                async with results_lock:
                    state[quarter_key] = {
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
            state[quarter_key] = {
                "completed": sorted(completed),
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            try:
                state_path.write_text(json.dumps(state))
            except OSError as exc:
                log.warning("state write failed: %s", exc)

        # Per-quarter repurchase_summary stream emit + R2 upload + audit.
        rc = _emit_repurchase_summary_for_quarter(
            year=year, quarter=quarter,
            accumulator=accumulator,
            db_url=db_url, s3=s3,
            stream_started=started_wall,
        )
        log.info(
            "[%d Q%d] DONE parsed_ok=%d failed=%d wall=%.1fs",
            year, quarter, accumulator.parsed_ok, accumulator.parsed_failed,
            time.monotonic() - started_wall,
        )
        return rc


def _emit_repurchase_summary_for_quarter(
    *, year: int, quarter: int,
    accumulator: QuarterAccumulator,
    db_url: str | None, s3,
    stream_started: float,
) -> int:
    quarter_rows = accumulator.repurchase_summary
    r2_key = f"sec-edgar/form-abs-15g/year={year}/quarter={quarter}/repurchase_summary/data.parquet"
    run_id: str | None = None
    if db_url:
        conn = psycopg.connect(db_url)
        try:
            run_id = insert_run_row(
                conn, year=year, quarter=quarter, stream="repurchase_summary",
                source_url=f"s3://{R2_BUCKET}/{r2_key}",
            )
        finally:
            conn.close()
    if not quarter_rows:
        log.info("[%d Q%d/repurchase_summary] no records — skipping upload",
                 year, quarter)
        if db_url and run_id is not None:
            conn = psycopg.connect(db_url)
            try:
                finalize_run_row(
                    conn, run_id, status="completed",
                    parquet_row_count=0, parquet_bytes_written=0,
                    r2_bucket=R2_BUCKET, r2_key=r2_key,
                    filings_fetched_count=accumulator.parsed_ok + accumulator.parsed_failed,
                    filings_parsed_ok_count=accumulator.parsed_ok,
                    filings_parsed_failed_count=accumulator.parsed_failed,
                    started_at=stream_started,
                    notes={"empty": True},
                )
            finally:
                conn.close()
        return 0
    try:
        pq_bytes = write_parquet_zstd(quarter_rows, _REPURCHASE_SUMMARY_SCHEMA)
        uploaded = upload_bytes_to_r2(
            s3, bucket=R2_BUCKET, key=r2_key, body=pq_bytes,
        )
        log.info(
            "[%d Q%d/repurchase_summary] uploaded %d rows, %.2f MB → s3://%s/%s",
            year, quarter, len(quarter_rows), uploaded / (1 << 20),
            R2_BUCKET, r2_key,
        )
        if db_url and run_id is not None:
            conn = psycopg.connect(db_url)
            try:
                finalize_run_row(
                    conn, run_id, status="completed",
                    parquet_row_count=len(quarter_rows),
                    parquet_bytes_written=uploaded,
                    r2_bucket=R2_BUCKET, r2_key=r2_key,
                    filings_fetched_count=accumulator.parsed_ok + accumulator.parsed_failed,
                    filings_parsed_ok_count=accumulator.parsed_ok,
                    filings_parsed_failed_count=accumulator.parsed_failed,
                    started_at=stream_started,
                )
            finally:
                conn.close()
        return 0
    except Exception as exc:
        log.exception("[%d Q%d/repurchase_summary] write/upload FAILED", year, quarter)
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
        return 1


def _emit_per_year_filings(
    *, year: int,
    filings_records: list[dict[str, Any]],
    parsed_ok: int, parsed_failed: int,
    db_url: str | None, s3, year_started: float,
) -> int:
    rc = 0
    r2_key = f"sec-edgar/form-abs-15g/year={year}/filings/data.parquet"
    run_id: str | None = None
    if db_url:
        conn = psycopg.connect(db_url)
        try:
            run_id = insert_run_row(
                conn, year=year, quarter=None, stream="filings",
                source_url=f"s3://{R2_BUCKET}/{r2_key}",
            )
        finally:
            conn.close()
    if not filings_records:
        log.info("[%d/filings] no records — skipping upload", year)
        if db_url and run_id is not None:
            conn = psycopg.connect(db_url)
            try:
                finalize_run_row(
                    conn, run_id, status="completed",
                    parquet_row_count=0, parquet_bytes_written=0,
                    r2_bucket=R2_BUCKET, r2_key=r2_key,
                    filings_parsed_ok_count=parsed_ok,
                    filings_parsed_failed_count=parsed_failed,
                    started_at=year_started,
                    notes={"empty": True},
                )
            finally:
                conn.close()
        return 0
    try:
        pq_bytes = write_parquet_zstd(filings_records, _FILINGS_SCHEMA)
        uploaded = upload_bytes_to_r2(
            s3, bucket=R2_BUCKET, key=r2_key, body=pq_bytes,
        )
        log.info(
            "[%d/filings] uploaded %d rows, %.2f MB → s3://%s/%s",
            year, len(filings_records), uploaded / (1 << 20),
            R2_BUCKET, r2_key,
        )
        if db_url and run_id is not None:
            conn = psycopg.connect(db_url)
            try:
                finalize_run_row(
                    conn, run_id, status="completed",
                    parquet_row_count=len(filings_records),
                    parquet_bytes_written=uploaded,
                    r2_bucket=R2_BUCKET, r2_key=r2_key,
                    filings_parsed_ok_count=parsed_ok,
                    filings_parsed_failed_count=parsed_failed,
                    started_at=year_started,
                )
            finally:
                conn.close()
    except Exception as exc:
        log.exception("[%d/filings] write/upload FAILED", year)
        rc = 1
        if db_url and run_id is not None:
            conn = psycopg.connect(db_url)
            try:
                finalize_run_row(
                    conn, run_id, status="failed",
                    started_at=year_started,
                    error_message=str(exc)[:1000],
                )
            finally:
                conn.close()
    return rc


async def process_year(
    *, year: int,
    quarters: list[int],
    state_path: Path,
    include_raw_xml: bool,
    max_filings: int | None,
    db_url: str | None,
    target_rps: int,
) -> int:
    year_started = time.monotonic()
    rc = 0
    s3 = _r2_client()
    limiter = RpsLimiter(target_rps)

    year_filings: list[dict[str, Any]] = []
    year_parsed_ok = 0
    year_parsed_failed = 0

    for q in quarters:
        accumulator = QuarterAccumulator(year=year, quarter=q)
        rc_q = await process_quarter(
            year=year, quarter=q,
            state_path=state_path,
            include_raw_xml=include_raw_xml,
            max_filings=max_filings,
            db_url=db_url,
            accumulator=accumulator,
            limiter=limiter,
            s3=s3,
        )
        if rc_q != 0:
            rc = rc_q
        year_filings.extend(accumulator.filings)
        year_parsed_ok += accumulator.parsed_ok
        year_parsed_failed += accumulator.parsed_failed

    # Per-year filings stream.
    rc |= _emit_per_year_filings(
        year=year,
        filings_records=year_filings,
        parsed_ok=year_parsed_ok,
        parsed_failed=year_parsed_failed,
        db_url=db_url, s3=s3, year_started=year_started,
    )
    log.info(
        "[%d] YEAR DONE parsed_ok=%d failed=%d filings_rows=%d wall=%.1fs",
        year, year_parsed_ok, year_parsed_failed,
        len(year_filings),
        time.monotonic() - year_started,
    )
    return rc


def _parse_year_range(s: str) -> list[int]:
    if "-" in s:
        a, b = s.split("-", 1)
        return list(range(int(a), int(b) + 1))
    return [int(s)]


def _parse_quarter_range(s: str) -> list[int]:
    if "-" in s:
        a, b = s.split("-", 1)
        return list(range(int(a), int(b) + 1))
    return [int(s)]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--years", default=None,
                   help="Year range e.g. 2012-2026 or single 2024.")
    p.add_argument("--quarters", default="1-4",
                   help="Quarter range e.g. 1-4 or single 1. Default 1-4.")
    p.add_argument("--max-filings", type=int, default=None,
                   help="Per-quarter cap (smoke).")
    p.add_argument("--include-raw-xml", action="store_true",
                   help="Also upload raw primary_doc.xml + Exhibit 99. "
                        "Default OFF per directive.")
    p.add_argument("--state-file", default="/tmp/sec_edgar_form_abs_15g_state.json",
                   help="Resume checkpoint file.")
    p.add_argument("--no-audit", action="store_true",
                   help="Skip ops.sec_edgar_form_abs_15g_r2_ingest_runs writes.")
    p.add_argument("--target-rps", type=int, default=TARGET_RPS,
                   help="RPS rate-limit (default %(default)s — TARGET_RPS=1 per "
                        "validator p1; SEC fair-use aggregate cap is 10 RPS).")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if not args.years:
        log.error("must pass --years")
        return 2
    years: Iterable[int] = _parse_year_range(args.years)
    quarters: list[int] = _parse_quarter_range(args.quarters)
    if not all(1 <= q <= 4 for q in quarters):
        log.error("quarters must be in 1..4")
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
                year=y, quarters=quarters,
                state_path=state_path,
                include_raw_xml=args.include_raw_xml,
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

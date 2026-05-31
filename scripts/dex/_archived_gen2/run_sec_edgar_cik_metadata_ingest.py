#!/usr/bin/env python3
"""SEC EDGAR submissions-JSON CIK metadata → R2 Parquet ingest.

Per-CIK fetch of ``https://data.sec.gov/submissions/CIK{CIK}.json`` for the
union of (a) SEC's published company-tickers index and (b) distinct CIKs
already present in DEF 14A / Form 4 / Schedule 13D-G / Form 13F R2 corpora.
ZSTD Parquet to R2 + RW source DDL (separate apply script).

Strategic purpose: this ingest produces the canonical CIK→state lookup that
unblocks the four pending public-company × FEC bridges (DEF 14A, Form 4,
Schedule 13D/G, Form 144). See directive
~/Desktop/hq/directives/2026-05-10-sec-edgar-cik-metadata-submissions-json-ingest.md.

Phase A: enumerate CIK union from SEC's published index + R2 corpora.
Phase B: per-CIK JSON fetch, parse, buffer, flush every 5,000 to R2 Parquet.
Phase C: RW source wiring is in a separate apply script.

Idempotency: per CIK — the orchestrator keeps a JSON checkpoint file at
``--state-file`` so a network blip or session timeout doesn't restart from
zero. Re-running with the same state file resumes where it left off.

Audit: one row per snapshot run in
``ops.sec_edgar_cik_metadata_ingest_runs``.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with httpx --with boto3 --with psycopg --with pyarrow \\
    --with duckdb python3 scripts/run_sec_edgar_cik_metadata_ingest.py --max-ciks 100
  doppler run --project hq-all --config prd -- \\
    uv run --with httpx --with boto3 --with psycopg --with pyarrow \\
    --with duckdb python3 scripts/run_sec_edgar_cik_metadata_ingest.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
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
import pyarrow as pa
import pyarrow.parquet as pq
from psycopg.types.json import Jsonb


# ------------------------------------------------------------------ #
# Constants
# ------------------------------------------------------------------ #

R2_BUCKET = "dex-raw-landing-zone"
R2_PREFIX_BASE = "sec-edgar/cik-metadata"

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL_TMPL = "https://data.sec.gov/submissions/CIK{cik10}.json"

# SEC fair-use: ≤10 RPS aggregate per egress IP. Default 5 mirrors
# run_sec_edgar_def_14a_r2_ingest.py.
TARGET_RPS = int(os.environ.get("SEC_EDGAR_TARGET_RPS", "5"))
HTTP_CONCURRENCY = int(os.environ.get("SEC_EDGAR_HTTP_CONCURRENCY", "16"))

# Buffer threshold — flush every N records to a Parquet part.
FLUSH_EVERY_N = int(os.environ.get("CIK_METADATA_FLUSH_EVERY", "5000"))

# Failure circuit breakers (per directive §7).
MAX_RETRY_EXHAUSTED = 100
MAX_OTHER_FAILURE = 100
MAX_PARSE_FAILED_PCT = 10  # of total target list

USER_AGENT = (
    "data-engine-x/sec-edgar-cik-metadata-ingest "
    "tools@substrate.build "
    "(operational research)"
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
    return logging.getLogger("sec-edgar-cik-metadata-ingest")


log = _logger()


# ------------------------------------------------------------------ #
# Env helpers
# ------------------------------------------------------------------ #

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


def _database_url() -> str | None:
    return os.environ.get("DEX_DB_URL_POOLED") or os.environ.get("DEX_DB_URL_DIRECT")


def _duck_with_r2() -> duckdb.DuckDBPyConnection:
    endpoint_full = _required_env("R2_ENDPOINT")
    endpoint_host = endpoint_full.replace("https://", "").replace("http://", "")
    con = duckdb.connect(":memory:")
    con.execute("INSTALL httpfs;")
    con.execute("LOAD httpfs;")
    con.execute(f"SET s3_endpoint='{endpoint_host}';")
    con.execute(f"SET s3_access_key_id='{_required_env('R2_ACCESS_KEY_ID')}';")
    con.execute(f"SET s3_secret_access_key='{_required_env('R2_SECRET_ACCESS_KEY')}';")
    con.execute("SET s3_url_style='path';")
    con.execute("SET s3_region='auto';")
    return con


# ------------------------------------------------------------------ #
# pyarrow schema
# ------------------------------------------------------------------ #


CIK_METADATA_SCHEMA = pa.schema([
    ("cik", pa.string()),
    ("name", pa.string()),
    ("name_normalized", pa.string()),
    ("tickers", pa.list_(pa.string())),
    ("former_names", pa.list_(pa.string())),
    ("sic", pa.string()),
    ("sic_description", pa.string()),
    ("address_business_state", pa.string()),
    ("address_business_city", pa.string()),
    ("address_business_zip", pa.string()),
    ("address_mailing_state", pa.string()),
    ("address_mailing_city", pa.string()),
    ("address_mailing_zip", pa.string()),
    ("entity_type", pa.string()),
    ("phone", pa.string()),
    ("ein", pa.string()),
    ("category", pa.string()),
    ("fiscal_year_end", pa.string()),
    ("state_of_incorporation", pa.string()),
    ("filing_count_recent", pa.int64()),
    ("most_recent_filing_date", pa.date32()),
    ("most_recent_form_type", pa.string()),
    ("snapshot_date", pa.date32()),
    ("source_url", pa.string()),
    ("ingested_at", pa.timestamp("us")),
])


_NORM_RE = re.compile(r"[^a-z0-9]")


def normalize_name(s: str | None) -> str | None:
    if not s:
        return None
    return _NORM_RE.sub("", s.lower()) or None


def _parse_filing_date(s: Any) -> date | None:
    """SEC publishes dates as 'YYYY-MM-DD' strings."""
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _coerce_list(v: Any) -> list[str]:
    """Coerce upstream value to list of strings; tolerate string-instead-of-list."""
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        return [str(x) for x in v if x is not None]
    return []


def _str_or_none(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _state_or_none(v: Any) -> str | None:
    """Extract bare 2-letter state code; trim + uppercase. Pass through any
    non-2-letter values verbatim (FEC bridge will not match them, which is
    the correct outcome for foreign-country filers)."""
    if v is None:
        return None
    s = str(v).strip().upper()
    return s or None


def parse_submissions_json(
    cik10: str, data: dict[str, Any], snapshot_date: date, source_url: str,
) -> dict[str, Any]:
    """Map upstream submissions-JSON → CIK_METADATA_SCHEMA row dict.

    Defensive against missing keys (L13). Counts / extracts:
      - tickers: list[str]
      - former_names: list[str] from formerNames[].name
      - business + mailing address: state/city/zip
      - filings.recent: count + most-recent date + most-recent form type
    """
    name = _str_or_none(data.get("name"))
    addresses = data.get("addresses") or {}
    business = addresses.get("business") or {}
    mailing = addresses.get("mailing") or {}

    former_names_raw = data.get("formerNames") or []
    former_names: list[str] = []
    for item in former_names_raw:
        if isinstance(item, dict):
            n = item.get("name")
            if n:
                former_names.append(str(n))
        elif isinstance(item, str):
            former_names.append(item)

    filings_recent = (data.get("filings") or {}).get("recent") or {}
    accession_numbers = filings_recent.get("accessionNumber") or []
    filing_dates = filings_recent.get("filingDate") or []
    forms = filings_recent.get("form") or []
    most_recent_date: date | None = None
    most_recent_form: str | None = None
    if isinstance(filing_dates, list) and filing_dates:
        most_recent_date = _parse_filing_date(filing_dates[0])
    if isinstance(forms, list) and forms:
        most_recent_form = _str_or_none(forms[0])

    return {
        "cik": cik10,
        "name": name,
        "name_normalized": normalize_name(name),
        "tickers": _coerce_list(data.get("tickers")),
        "former_names": former_names,
        "sic": _str_or_none(data.get("sic")),
        "sic_description": _str_or_none(data.get("sicDescription")),
        "address_business_state": _state_or_none(business.get("stateOrCountry")),
        "address_business_city": _str_or_none(business.get("city")),
        "address_business_zip": _str_or_none(business.get("zipCode")),
        "address_mailing_state": _state_or_none(mailing.get("stateOrCountry")),
        "address_mailing_city": _str_or_none(mailing.get("city")),
        "address_mailing_zip": _str_or_none(mailing.get("zipCode")),
        "entity_type": _str_or_none(data.get("entityType")),
        "phone": _str_or_none(data.get("phone")),
        "ein": _str_or_none(data.get("ein")),
        "category": _str_or_none(data.get("category")),
        "fiscal_year_end": _str_or_none(data.get("fiscalYearEnd")),
        "state_of_incorporation": _str_or_none(data.get("stateOfIncorporation")),
        "filing_count_recent": (
            len(accession_numbers) if isinstance(accession_numbers, list) else 0
        ),
        "most_recent_filing_date": most_recent_date,
        "most_recent_form_type": most_recent_form,
        "snapshot_date": snapshot_date,
        "source_url": source_url,
        "ingested_at": datetime.now(timezone.utc).replace(tzinfo=None),
    }


# ------------------------------------------------------------------ #
# Phase A — CIK enumeration
# ------------------------------------------------------------------ #


def _r2_prefix_has_objects(s3, prefix: str) -> bool:
    r = s3.list_objects_v2(Bucket=R2_BUCKET, Prefix=prefix, MaxKeys=1)
    return r.get("KeyCount", 0) > 0


@dataclass
class EnumerationCounts:
    company_tickers: int = 0
    def_14a: int = 0
    form_4_issuer: int = 0
    form_4_owner: int = 0
    schedule_13d_13g: int = 0
    form_13f: int = 0
    total: int = 0


async def fetch_company_tickers(
    client: httpx.AsyncClient, *, limiter: "RpsLimiter",
) -> set[str]:
    """Fetch SEC's published index of active publicly-traded filers.

    Schema: ``{ "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ... }``
    Returns set of 10-digit zero-padded CIKs.
    """
    log.info("Phase A: fetching %s", COMPANY_TICKERS_URL)
    status, body = await fetch_text(client, COMPANY_TICKERS_URL, limiter=limiter)
    if status != 200:
        raise RuntimeError(
            f"company_tickers.json returned HTTP {status}; "
            "directive §7 says STOP — surface the response to operator"
        )
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"company_tickers.json malformed: {exc}")
    out: set[str] = set()
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, dict) and "cik_str" in v:
                cik_int = v["cik_str"]
                if isinstance(cik_int, int):
                    out.add(str(cik_int).zfill(10))
                elif isinstance(cik_int, str) and cik_int.isdigit():
                    out.add(cik_int.zfill(10))
    log.info("Phase A: company-tickers index → %d distinct CIKs", len(out))
    return out


def enumerate_r2_corpus_ciks(
    duck: duckdb.DuckDBPyConnection, s3, *, include_form_4_owner: bool = False,
) -> dict[str, set[str]]:
    """Run DuckDB-on-R2 SELECT DISTINCT for each pre-ingested SEC R2 corpus.

    Tolerates missing prefixes (L13): if a prefix is empty (e.g. Schedule 13D/G
    backfill not landed yet), skip its query without failing. Returns dict
    keyed by source-bucket name (matches the audit-ledger column suffixes).

    By default, form_4 REPORTING-OWNER CIKs are NOT enumerated (~267K
    individual-CIK count would blow past directive gate 3's 30K cap and would
    push wall-clock from ~17 min to ~16 h with most fetches yielding
    individual-mailing-only metadata — not useful for a company-state lookup).
    The four pending bridges all use ISSUER state as the proxy for executive
    state (per directive §0). Pass ``include_form_4_owner=True`` to override
    if a v2 use case needs individual-CIK metadata.
    """
    # NOTE: every cik_normalized column we read is already typed VARCHAR in
    # the source Parquet, so we don't pass all_varchar=TRUE here (which this
    # DuckDB version doesn't support as a read_parquet parameter anyway —
    # only DESCRIBE and the CSV reader accept it).
    queries: list[tuple[str, str, str]] = [
        (
            "def_14a",
            "sec-edgar/def-14a/",
            "SELECT DISTINCT cik_normalized AS cik "
            "FROM read_parquet('s3://dex-raw-landing-zone/sec-edgar/def-14a/year=*/filings/data.parquet') "
            "WHERE cik_normalized IS NOT NULL",
        ),
        (
            "form_4_issuer",
            "sec-edgar/form-4/",
            "SELECT DISTINCT issuer_cik_normalized AS cik "
            "FROM read_parquet('s3://dex-raw-landing-zone/sec-edgar/form-4/year=*/quarter=*/submission/data.parquet') "
            "WHERE issuer_cik_normalized IS NOT NULL",
        ),
        (
            "form_4_owner",
            "sec-edgar/form-4/",
            "SELECT DISTINCT reporting_owner_cik_normalized AS cik "
            "FROM read_parquet('s3://dex-raw-landing-zone/sec-edgar/form-4/year=*/quarter=*/reporting_owner/data.parquet') "
            "WHERE reporting_owner_cik_normalized IS NOT NULL",
        ),
        (
            "schedule_13d_13g",
            "sec-edgar/schedule-13d-13g/",
            "SELECT DISTINCT filer_cik_normalized AS cik "
            "FROM read_parquet('s3://dex-raw-landing-zone/sec-edgar/schedule-13d-13g/year=*/filings/data.parquet') "
            "WHERE filer_cik_normalized IS NOT NULL",
        ),
        (
            "form_13f",
            "sec-edgar/form-13f/",
            "SELECT DISTINCT cik_normalized AS cik "
            "FROM read_parquet('s3://dex-raw-landing-zone/sec-edgar/form-13f/year=*/filings/data.parquet') "
            "WHERE cik_normalized IS NOT NULL",
        ),
    ]
    out: dict[str, set[str]] = {}
    for label, probe_prefix, sql in queries:
        if label == "form_4_owner" and not include_form_4_owner:
            log.info(
                "Phase A: skipping form_4_owner (~267K individual CIKs); "
                "issuer state covers v1 bridge needs",
            )
            out[label] = set()
            continue
        if not _r2_prefix_has_objects(s3, probe_prefix):
            log.info("Phase A: %s prefix %s empty — skipping", label, probe_prefix)
            out[label] = set()
            continue
        log.info("Phase A: enumerating %s …", label)
        try:
            rows = duck.execute(sql).fetchall()
        except Exception as exc:
            log.warning("Phase A: %s query failed (%s) — skipping", label, exc)
            out[label] = set()
            continue
        ciks = {r[0].zfill(10) for r in rows if r[0] and r[0].isdigit()}
        out[label] = ciks
        log.info("Phase A: %s → %d distinct CIKs", label, len(ciks))
    return out


def union_ciks(
    company_tickers: set[str], r2_corpus: dict[str, set[str]],
) -> tuple[set[str], EnumerationCounts]:
    counts = EnumerationCounts(
        company_tickers=len(company_tickers),
        def_14a=len(r2_corpus.get("def_14a", set())),
        form_4_issuer=len(r2_corpus.get("form_4_issuer", set())),
        form_4_owner=len(r2_corpus.get("form_4_owner", set())),
        schedule_13d_13g=len(r2_corpus.get("schedule_13d_13g", set())),
        form_13f=len(r2_corpus.get("form_13f", set())),
    )
    union = set(company_tickers)
    for s in r2_corpus.values():
        union |= s
    # Drop anything that isn't a 10-digit numeric (defensive).
    union = {c for c in union if c.isdigit() and len(c) == 10}
    counts.total = len(union)
    log.info(
        "Phase A: union → %d distinct CIKs "
        "(company_tickers=%d def_14a=%d form_4_issuer=%d form_4_owner=%d "
        "schedule_13d_13g=%d form_13f=%d)",
        counts.total, counts.company_tickers, counts.def_14a,
        counts.form_4_issuer, counts.form_4_owner,
        counts.schedule_13d_13g, counts.form_13f,
    )
    return union, counts


# ------------------------------------------------------------------ #
# HTTP rate-limiter + fetch
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
    timeout: float = 60.0, retries: int = 3,
) -> tuple[int, bytes]:
    """Fetch URL with retry. Returns (status_code, body).

    For per-CIK fetches the caller treats 404 as terminal and 429/5xx as
    retryable. This function only retries 429/5xx; 404 and other 4xx pass
    through immediately so the caller can branch.
    """
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        await limiter.wait()
        try:
            r = await client.get(url, timeout=timeout, follow_redirects=True)
            if r.status_code in (429, 500, 502, 503, 504):
                wait = min(2 ** attempt, 30)
                log.warning(
                    "HTTP %s %s; retry in %ss (attempt %d/%d)",
                    r.status_code, url, wait, attempt, retries,
                )
                await asyncio.sleep(wait)
                continue
            return r.status_code, r.content
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            last_exc = exc
            wait = min(2 ** attempt, 30)
            log.warning(
                "fetch %s failed (%s); retry in %ss (attempt %d/%d)",
                url, exc, wait, attempt, retries,
            )
            await asyncio.sleep(wait)
    raise RuntimeError(f"fetch {url} exhausted retries: {last_exc}")


# ------------------------------------------------------------------ #
# R2 upload
# ------------------------------------------------------------------ #


def _records_to_table(records: list[dict[str, Any]]) -> pa.Table:
    cols: dict[str, list[Any]] = {f.name: [] for f in CIK_METADATA_SCHEMA}
    for rec in records:
        for f in CIK_METADATA_SCHEMA:
            cols[f.name].append(rec.get(f.name))
    arrays = []
    for f in CIK_METADATA_SCHEMA:
        arrays.append(pa.array(cols[f.name], type=f.type))
    return pa.Table.from_arrays(arrays, schema=CIK_METADATA_SCHEMA)


def write_part_to_r2(
    s3, *, snapshot_date: date, part_idx: int, records: list[dict[str, Any]],
) -> tuple[str, int, int]:
    """Serialize records → ZSTD Parquet → upload to R2.

    Returns ``(r2_key, bytes_uploaded, row_count)``.

    Per L42: ``upload_file`` would set ``ContentEncoding`` from extension —
    we use ``put_object`` with explicit ``ContentType=application/x-parquet``
    and never set ContentEncoding. The file extension is plain ``.parquet``.
    """
    table = _records_to_table(records)
    local_path = Path(f"/tmp/cik_metadata_part_{snapshot_date}_{part_idx:05d}.parquet")
    pq.write_table(
        table, local_path,
        compression="zstd",
        compression_level=9,
        row_group_size=5000,
    )
    body = local_path.read_bytes()
    snapshot_iso = snapshot_date.isoformat()
    r2_key = f"{R2_PREFIX_BASE}/snapshot={snapshot_iso}/part-{part_idx:05d}.parquet"
    s3.put_object(
        Bucket=R2_BUCKET, Key=r2_key, Body=body,
        ContentType="application/x-parquet",
    )
    try:
        local_path.unlink()
    except OSError:
        pass
    return r2_key, len(body), len(records)


# ------------------------------------------------------------------ #
# Audit-row helpers
# ------------------------------------------------------------------ #


def insert_run_row(
    conn: psycopg.Connection, *, snapshot_date: date,
) -> str:
    sql = """
    INSERT INTO ops.sec_edgar_cik_metadata_ingest_runs
      (snapshot_date, status)
    VALUES (%s, 'running')
    RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (snapshot_date,))
        row = cur.fetchone()[0]
    conn.commit()
    return str(row)


def update_run_enumeration(
    conn: psycopg.Connection, run_id: str, counts: EnumerationCounts,
) -> None:
    sql = """
    UPDATE ops.sec_edgar_cik_metadata_ingest_runs
       SET ciks_from_company_tickers = %s,
           ciks_from_def_14a = %s,
           ciks_from_form_4_issuer = %s,
           ciks_from_form_4_owner = %s,
           ciks_from_schedule_13d_13g = %s,
           ciks_from_form_13f = %s,
           ciks_total = %s
     WHERE id = %s;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            counts.company_tickers, counts.def_14a,
            counts.form_4_issuer, counts.form_4_owner,
            counts.schedule_13d_13g, counts.form_13f,
            counts.total, run_id,
        ))
    conn.commit()


def finalize_run_row(
    conn: psycopg.Connection, run_id: str, *,
    status: str,
    ciks_succeeded: int, ciks_404: int,
    ciks_retry_exhausted: int, ciks_json_parse_failed: int,
    ciks_other_failure: int,
    ciks_with_business_state: int, ciks_with_mailing_state: int,
    ciks_with_no_state: int,
    parquet_part_count: int, parquet_row_count: int,
    parquet_total_bytes: int,
    r2_bucket: str | None, r2_prefix: str | None,
    started_at: float, error_message: str | None = None,
    notes: dict[str, Any] | None = None,
) -> None:
    duration = round(time.monotonic() - started_at, 3)
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE ops.sec_edgar_cik_metadata_ingest_runs
               SET status = %s,
                   ciks_succeeded = %s,
                   ciks_404 = %s,
                   ciks_retry_exhausted = %s,
                   ciks_json_parse_failed = %s,
                   ciks_other_failure = %s,
                   ciks_with_business_state = %s,
                   ciks_with_mailing_state = %s,
                   ciks_with_no_state = %s,
                   parquet_part_count = %s,
                   parquet_row_count = %s,
                   parquet_total_bytes = %s,
                   r2_bucket = %s, r2_prefix = %s,
                   finished_at = now(), duration_seconds = %s,
                   error_message = %s, notes = %s
             WHERE id = %s;
            """, (
            status,
            ciks_succeeded, ciks_404,
            ciks_retry_exhausted, ciks_json_parse_failed,
            ciks_other_failure,
            ciks_with_business_state, ciks_with_mailing_state,
            ciks_with_no_state,
            parquet_part_count, parquet_row_count, parquet_total_bytes,
            r2_bucket, r2_prefix,
            duration, error_message,
            Jsonb(notes) if notes else None, run_id,
        ))
    conn.commit()


# ------------------------------------------------------------------ #
# Phase B — per-CIK fetch + flush orchestrator
# ------------------------------------------------------------------ #


@dataclass
class FetchAccumulator:
    succeeded: int = 0
    not_found: int = 0
    retry_exhausted: int = 0
    parse_failed: int = 0
    other_failure: int = 0
    with_business_state: int = 0
    with_mailing_state: int = 0
    with_no_state: int = 0
    parts_uploaded: int = 0
    rows_uploaded: int = 0
    bytes_uploaded: int = 0
    parse_drift_no_addresses: int = 0


async def ingest_one_cik(
    client: httpx.AsyncClient, cik10: str, *,
    limiter: RpsLimiter, snapshot_date: date,
) -> tuple[str, dict[str, Any] | None]:
    """Fetch + parse one CIK's submissions JSON.

    Returns ``(outcome, record_or_none)`` where outcome is one of:
      ok, not_found, retry_exhausted, parse_failed, other
    """
    url = SUBMISSIONS_URL_TMPL.format(cik10=cik10)
    try:
        status, body = await fetch_text(client, url, limiter=limiter)
    except RuntimeError as exc:
        log.warning("CIK %s retry exhausted: %s", cik10, exc)
        return "retry_exhausted", None

    if status == 404:
        return "not_found", None
    if status != 200:
        log.warning("CIK %s HTTP %s", cik10, status)
        return "other", None

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        log.warning("CIK %s JSON parse failed: %s", cik10, exc)
        return "parse_failed", None

    if not isinstance(data, dict):
        log.warning("CIK %s JSON not a dict (got %s)", cik10, type(data).__name__)
        return "parse_failed", None

    record = parse_submissions_json(cik10, data, snapshot_date, url)
    return "ok", record


async def process_run(
    *, snapshot_date: date,
    state_path: Path,
    max_ciks: int | None,
    db_url: str | None,
    include_form_4_owner: bool = False,
) -> int:
    started_wall = time.monotonic()
    log.info("=" * 70)
    log.info("=== SEC EDGAR CIK METADATA INGEST  snapshot=%s ===", snapshot_date)
    log.info("=" * 70)

    limiter = RpsLimiter(TARGET_RPS)
    s3 = _r2_client()
    duck = _duck_with_r2()

    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}
    async with httpx.AsyncClient(headers=headers, http2=False) as client:
        # Phase A.
        company_tickers = await fetch_company_tickers(client, limiter=limiter)
        r2_corpus = enumerate_r2_corpus_ciks(
            duck, s3, include_form_4_owner=include_form_4_owner,
        )
        target_ciks_set, counts = union_ciks(company_tickers, r2_corpus)

        run_id: str | None = None
        if db_url:
            with psycopg.connect(db_url) as conn:
                run_id = insert_run_row(conn, snapshot_date=snapshot_date)
                update_run_enumeration(conn, run_id, counts)
                log.info("audit-ledger run_id=%s (running)", run_id)

        # Sort for stability.
        target_ciks = sorted(target_ciks_set)

        if max_ciks is not None and len(target_ciks) > max_ciks:
            log.info("Phase B: limiting to first %d CIKs (smoke)", max_ciks)
            target_ciks = target_ciks[:max_ciks]

        # Resume support.
        completed: set[str] = set()
        state: dict[str, Any] = {}
        snapshot_key = snapshot_date.isoformat()
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text())
            except (json.JSONDecodeError, OSError):
                state = {}
        completed = set(state.get(snapshot_key, {}).get("completed", []))
        if completed:
            log.info("state file: %d CIKs already done", len(completed))
            target_ciks = [c for c in target_ciks if c not in completed]
            log.info("remaining: %d CIKs to fetch", len(target_ciks))

        # Phase B.
        sem = asyncio.Semaphore(HTTP_CONCURRENCY)
        results_lock = asyncio.Lock()
        accumulator = FetchAccumulator()
        # We accumulate already-completed CIK count too (for ledger totals
        # spanning pre-resume + post-resume). Set initial succeeded count
        # to len(completed) so re-runs that resume hit the gate criteria.
        accumulator.succeeded = 0  # only counts newly-fetched in this run
        pending_records: list[dict[str, Any]] = []
        part_idx = 1

        # Total pool for parse-failure circuit breaker.
        target_total_for_circuit = max(len(target_ciks), 1)

        async def _flush_locked() -> None:
            """Flush pending_records to R2. Caller holds results_lock."""
            nonlocal part_idx
            if not pending_records:
                return
            recs = pending_records[:]
            pending_records.clear()
            r2_key, body_bytes, row_count = write_part_to_r2(
                s3, snapshot_date=snapshot_date,
                part_idx=part_idx, records=recs,
            )
            accumulator.parts_uploaded += 1
            accumulator.rows_uploaded += row_count
            accumulator.bytes_uploaded += body_bytes
            log.info(
                "Phase B: flushed part %05d → s3://%s/%s "
                "(%d rows, %.2f MB)",
                part_idx, R2_BUCKET, r2_key,
                row_count, body_bytes / (1 << 20),
            )
            part_idx += 1

        async def _worker(cik10: str) -> None:
            outcome, record = await ingest_one_cik(
                client, cik10, limiter=limiter, snapshot_date=snapshot_date,
            )
            async with results_lock:
                if outcome == "ok" and record is not None:
                    accumulator.succeeded += 1
                    completed.add(cik10)
                    pending_records.append(record)
                    bs = record.get("address_business_state")
                    ms = record.get("address_mailing_state")
                    if bs:
                        accumulator.with_business_state += 1
                    if ms:
                        accumulator.with_mailing_state += 1
                    if not bs and not ms:
                        accumulator.with_no_state += 1
                    if not record.get("address_business_city") and not bs:
                        accumulator.parse_drift_no_addresses += 1
                    if len(pending_records) >= FLUSH_EVERY_N:
                        await _flush_locked()
                elif outcome == "not_found":
                    accumulator.not_found += 1
                elif outcome == "retry_exhausted":
                    accumulator.retry_exhausted += 1
                elif outcome == "parse_failed":
                    accumulator.parse_failed += 1
                else:
                    accumulator.other_failure += 1

                seen = (
                    accumulator.succeeded + accumulator.not_found
                    + accumulator.retry_exhausted + accumulator.parse_failed
                    + accumulator.other_failure
                )
                if seen % 500 == 0:
                    log.info(
                        "Phase B progress: %d/%d "
                        "(ok=%d 404=%d retry_x=%d parse_x=%d other=%d) "
                        "with_state=%d",
                        seen, len(target_ciks),
                        accumulator.succeeded, accumulator.not_found,
                        accumulator.retry_exhausted, accumulator.parse_failed,
                        accumulator.other_failure,
                        accumulator.with_business_state,
                    )

                # Circuit breakers.
                if accumulator.retry_exhausted > MAX_RETRY_EXHAUSTED:
                    raise RuntimeError(
                        f"circuit_breaker: retry_exhausted={accumulator.retry_exhausted} > "
                        f"{MAX_RETRY_EXHAUSTED}; SEC throttling — STOP per directive §7"
                    )
                if accumulator.other_failure > MAX_OTHER_FAILURE:
                    raise RuntimeError(
                        f"circuit_breaker: other_failure={accumulator.other_failure} > "
                        f"{MAX_OTHER_FAILURE}; STOP per directive §7"
                    )
                pct_parse_failed = (
                    100.0 * accumulator.parse_failed / target_total_for_circuit
                )
                if pct_parse_failed > MAX_PARSE_FAILED_PCT:
                    raise RuntimeError(
                        f"circuit_breaker: parse_failed_pct={pct_parse_failed:.1f}% > "
                        f"{MAX_PARSE_FAILED_PCT}%; SEC schema drift — STOP per directive §7"
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
                    state[snapshot_key] = {
                        "completed": sorted(completed),
                        "ts": datetime.now(timezone.utc).isoformat(),
                    }
                    try:
                        state_path.write_text(json.dumps(state))
                    except OSError as exc:
                        log.warning("state write failed: %s", exc)

        async def _bounded(cik10: str) -> None:
            async with sem:
                await _worker(cik10)

        cp_task = asyncio.create_task(_checkpointer())
        rc = 0
        try:
            await asyncio.gather(*[_bounded(c) for c in target_ciks])
        except Exception as exc:
            log.exception("Phase B halted: %s", exc)
            rc = 1
        finally:
            stop_event.set()
            await cp_task

        # Final flush.
        async with results_lock:
            await _flush_locked()
            state[snapshot_key] = {
                "completed": sorted(completed),
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            try:
                state_path.write_text(json.dumps(state))
            except OSError as exc:
                log.warning("state write failed: %s", exc)

        # Audit-ledger finalize.
        if db_url and run_id is not None:
            status = "completed" if rc == 0 else "failed"
            r2_prefix = f"{R2_PREFIX_BASE}/snapshot={snapshot_key}/"
            notes = {
                "target_rps": TARGET_RPS,
                "http_concurrency": HTTP_CONCURRENCY,
                "flush_every_n": FLUSH_EVERY_N,
                "max_ciks_arg": max_ciks,
                "parse_drift_no_addresses": accumulator.parse_drift_no_addresses,
                "resumed_from_state_file_count": len(completed) - accumulator.succeeded,
            }
            with psycopg.connect(db_url) as conn:
                finalize_run_row(
                    conn, run_id, status=status,
                    ciks_succeeded=accumulator.succeeded,
                    ciks_404=accumulator.not_found,
                    ciks_retry_exhausted=accumulator.retry_exhausted,
                    ciks_json_parse_failed=accumulator.parse_failed,
                    ciks_other_failure=accumulator.other_failure,
                    ciks_with_business_state=accumulator.with_business_state,
                    ciks_with_mailing_state=accumulator.with_mailing_state,
                    ciks_with_no_state=accumulator.with_no_state,
                    parquet_part_count=accumulator.parts_uploaded,
                    parquet_row_count=accumulator.rows_uploaded,
                    parquet_total_bytes=accumulator.bytes_uploaded,
                    r2_bucket=R2_BUCKET, r2_prefix=r2_prefix,
                    started_at=started_wall,
                    error_message=None if rc == 0 else "circuit_breaker_or_phase_b_halted",
                    notes=notes,
                )

        log.info(
            "DONE snapshot=%s succeeded=%d 404=%d retry_x=%d parse_x=%d "
            "other=%d with_state=%d parts=%d rows=%d bytes=%.1fMB wall=%.1fs",
            snapshot_date, accumulator.succeeded, accumulator.not_found,
            accumulator.retry_exhausted, accumulator.parse_failed,
            accumulator.other_failure, accumulator.with_business_state,
            accumulator.parts_uploaded, accumulator.rows_uploaded,
            accumulator.bytes_uploaded / (1 << 20),
            time.monotonic() - started_wall,
        )
        return rc


# ------------------------------------------------------------------ #
# CLI
# ------------------------------------------------------------------ #


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--snapshot-date", default=None,
                   help="YYYY-MM-DD; defaults to today UTC.")
    p.add_argument("--max-ciks", type=int, default=None,
                   help="Cap (smoke).")
    p.add_argument("--state-file", default="/tmp/sec_edgar_cik_metadata_state.json",
                   help="Resume checkpoint file.")
    p.add_argument("--no-audit", action="store_true",
                   help="Skip ops.sec_edgar_cik_metadata_ingest_runs writes.")
    p.add_argument("--include-form-4-owner", action="store_true",
                   help="Also enumerate ~267K Form 4 reporting-owner CIKs "
                        "(individuals). Default off — issuer state covers "
                        "v1 bridge needs at much lower wall-clock cost.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.snapshot_date:
        try:
            snapshot_date = datetime.strptime(args.snapshot_date, "%Y-%m-%d").date()
        except ValueError as exc:
            log.error("--snapshot-date must be YYYY-MM-DD: %s", exc)
            return 2
    else:
        snapshot_date = datetime.now(timezone.utc).date()

    state_path = Path(args.state_file)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    db_url = None if args.no_audit else _database_url()
    if not args.no_audit and not db_url:
        log.warning("no DB URL set; audit ledger writes will be skipped")

    try:
        return asyncio.run(process_run(
            snapshot_date=snapshot_date,
            state_path=state_path,
            max_ciks=args.max_ciks,
            db_url=db_url,
            include_form_4_owner=args.include_form_4_owner,
        ))
    except Exception:
        log.exception("ingest failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())

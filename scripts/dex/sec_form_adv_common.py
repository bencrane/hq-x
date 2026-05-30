"""Shared helpers for SEC Form ADV ingest loaders.

Used by:
    scripts/run_sec_form_adv_part1_ingest.py    (Part 1 + Schedule D)
    scripts/run_sec_form_adv_pdfs_ingest.py     (Part 2 + Part 3 PDFs)
    scripts/run_sec_form_adv_w_ingest.py        (ADV-W)

Politeness toward sec.gov:
    SEC's EDGAR-adjacent endpoints expect a descriptive User-Agent with an
    operator contact. We send the same UA used during reconnaissance.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import httpx
import psycopg

logger = logging.getLogger(__name__)

USER_AGENT = "data-engine-x ingest (operator: benjaminjcrane@gmail.com)"

STORAGE_BUCKET = "sec-form-adv-pdfs"

# Source URLs for the 2000-2024 historical compilations + most recent monthly
# delta. The loader CLIs accept --source-url to override; this is just the
# default that recon found.
DEFAULT_PART1_URLS = [
    "https://www.sec.gov/files/adv-filing-data-20111105-20241231-part1.zip",
    "https://www.sec.gov/files/adv-filing-data-20111105-20241231-part2.zip",
    "https://www.sec.gov/files/adv-filing-data-20001019-20111104.zip",
]
DEFAULT_ADV_W_FULL_HISTORICAL_URL = (
    "https://www.sec.gov/files/advw-20001019-20241231.zip"
)


@dataclass
class IngestRunHandle:
    """Represents a row in ops.sec_form_adv_ingest_runs.

    Created in 'pending' status by start_run(); flipped to 'running' →
    'completed'/'failed' by the loader. Use the context manager
    `ingest_run` to ensure the row is always finalized.
    """

    run_id: uuid.UUID
    feed_name: str
    row_id: uuid.UUID
    started_monotonic: float


def _get_db_url() -> str:
    """Return the direct (non-pooled) Postgres URL.

    Doppler pre-injects DEX_DB_URL_DIRECT into the script's environment
    when invoked under `doppler run --`. We use the direct connection
    because DDL-adjacent operations (like CREATE EXTENSION lookups in
    other scripts) and bulk inserts both work cleanly there.
    """
    url = os.environ.get("DEX_DB_URL_DIRECT")
    if not url:
        raise RuntimeError(
            "DEX_DB_URL_DIRECT not set — invoke this script under "
            "`doppler run --` from a worktree with Doppler pinned to "
            "data-engine-x/prd."
        )
    return url


@contextmanager
def db_connection():
    """Yield a psycopg connection that auto-commits on exit (or rolls back on error)."""
    conn = psycopg.connect(_get_db_url())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def start_run(
    *,
    run_id: uuid.UUID,
    feed_name: str,
    source_url: str,
    source_filename: str | None,
    compilation_date: str | None,
    invoked_by: str = "cli",
) -> IngestRunHandle:
    """Insert a 'pending' row into ops.sec_form_adv_ingest_runs and flip to 'running'."""
    with db_connection() as conn:
        with conn.cursor() as cur:
            row_id = uuid.uuid4()
            cur.execute(
                """
                INSERT INTO ops.sec_form_adv_ingest_runs
                  (id, run_id, feed_name, compilation_date, source_url,
                   source_filename, status, attempt, started_at, invoked_by)
                VALUES (%s, %s, %s, %s, %s, %s, 'running', 1, NOW(), %s)
                """,
                (
                    row_id,
                    run_id,
                    feed_name,
                    compilation_date,
                    source_url,
                    source_filename,
                    invoked_by,
                ),
            )
    return IngestRunHandle(
        run_id=run_id,
        feed_name=feed_name,
        row_id=row_id,
        started_monotonic=time.monotonic(),
    )


def finish_run(
    handle: IngestRunHandle,
    *,
    status: str,
    rows_loaded: int | None = None,
    rows_skipped_idempotent: int | None = None,
    pdfs_uploaded: int | None = None,
    pdfs_skipped_idempotent: int | None = None,
    bytes_downloaded: int | None = None,
    source_sha256: str | None = None,
    source_byte_size: int | None = None,
    error_message: str | None = None,
    error_class: str | None = None,
) -> None:
    """Update the ingest_runs row to its terminal state."""
    duration = time.monotonic() - handle.started_monotonic
    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE ops.sec_form_adv_ingest_runs
                SET status = %s,
                    completed_at = NOW(),
                    duration_seconds = %s,
                    rows_loaded = COALESCE(%s, rows_loaded),
                    rows_skipped_idempotent = COALESCE(%s, rows_skipped_idempotent),
                    pdfs_uploaded = COALESCE(%s, pdfs_uploaded),
                    pdfs_skipped_idempotent = COALESCE(%s, pdfs_skipped_idempotent),
                    bytes_downloaded = COALESCE(%s, bytes_downloaded),
                    source_sha256 = COALESCE(%s, source_sha256),
                    source_byte_size = COALESCE(%s, source_byte_size),
                    error_message = %s,
                    error_class = %s
                WHERE id = %s
                """,
                (
                    status,
                    duration,
                    rows_loaded,
                    rows_skipped_idempotent,
                    pdfs_uploaded,
                    pdfs_skipped_idempotent,
                    bytes_downloaded,
                    source_sha256,
                    source_byte_size,
                    error_message,
                    error_class,
                    handle.row_id,
                ),
            )


def classify_error(exc: BaseException) -> str:
    """Coarse exception classifier matching the error_class CHECK enum."""
    exc_text = repr(exc).lower()
    if isinstance(exc, (httpx.HTTPError, httpx.RequestError)):
        return "download_failure"
    if isinstance(exc, psycopg.Error):
        return "db_failure"
    if isinstance(exc, (UnicodeDecodeError, ValueError)) or "csv" in exc_text:
        return "parse_failure"
    if "timeout" in exc_text or "timed out" in exc_text:
        return "timeout"
    if "storage" in exc_text or "bucket" in exc_text or "supabase" in exc_text:
        return "storage_failure"
    return "unknown"


def stream_download(url: str, dest: Path, *, log_every_mb: int = 50) -> tuple[int, str]:
    """Download `url` to `dest`, returning (byte_count, sha256_hex).

    Streams the response so we don't blow memory on >1 GB ZIPs. Computes
    SHA-256 incrementally.
    """
    sha = hashlib.sha256()
    bytes_total = 0
    next_log = log_every_mb * 1024 * 1024
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}

    with httpx.stream("GET", url, headers=headers, timeout=300.0, follow_redirects=True) as resp:
        resp.raise_for_status()
        with dest.open("wb") as f:
            for chunk in resp.iter_bytes(chunk_size=1 << 20):  # 1 MiB
                f.write(chunk)
                sha.update(chunk)
                bytes_total += len(chunk)
                if bytes_total >= next_log:
                    logger.info(
                        "download_progress",
                        extra={
                            "url": url,
                            "bytes_downloaded": bytes_total,
                            "mb_downloaded": bytes_total / (1024 * 1024),
                        },
                    )
                    next_log += log_every_mb * 1024 * 1024
    return bytes_total, sha.hexdigest()


def chunked(iterable: Iterator, n: int) -> Iterator[list]:
    """Yield successive lists of length <= n from iterable."""
    buf: list = []
    for x in iterable:
        buf.append(x)
        if len(buf) >= n:
            yield buf
            buf = []
    if buf:
        yield buf


def parse_iso_date(s: str | None) -> datetime | None:
    """Best-effort parse of date/datetime values from SEC CSVs.

    Accepts: '12/05/2024 12:00:13 PM', '03/27/2024', '2024-10-24 16:29:15',
    '2000', '05/2003'.
    """
    if not s:
        return None
    s = s.strip().strip('"')
    if not s:
        return None
    formats = [
        "%m/%d/%Y %I:%M:%S %p",
        "%m/%d/%Y",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
        "%m/%Y",
        "%Y",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def parse_int(s: str | None) -> int | None:
    if s is None:
        return None
    s = str(s).strip().strip('"').replace(",", "")
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        try:
            return int(float(s))
        except ValueError:
            return None


def parse_numeric(s: str | None) -> float | None:
    if s is None:
        return None
    s = str(s).strip().strip('"').replace(",", "").replace("$", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_yn_flag(s: str | None) -> bool | None:
    """Parse Y/N → True/False; everything else → None."""
    if s is None:
        return None
    v = str(s).strip().strip('"').upper()
    if v == "Y":
        return True
    if v == "N":
        return False
    return None


# ---------------------------------------------------------------------------
# Item-code lookup helpers for the Form ADV Part 1 raw_jsonb projection.
# The pre-2011 archive's IA_ADV_Base_A header uses Item-numbered columns
# (e.g. '1A', '1B1', '1D'). Post-2011 datasets may use explicit names. The
# loader tries multiple candidate keys.
# ---------------------------------------------------------------------------

LEGAL_NAME_KEYS = ("1A", "Legal Name", "FullLegalName", "Full Legal Name", "LegalName")
PRIMARY_BUSINESS_NAME_KEYS = (
    "1B1",
    "Primary Business Name",
    "PrimaryBusinessName",
    "BusinessName",
)
CRD_KEYS = ("CRDNumber", "CRD Number", "CRD", "1E1")  # 1D is SEC file number, not CRD
SEC_NUMBER_KEYS = ("SECNumber", "SEC Number", "SEC#", "SEC File Number")


def first_present(row: dict, keys: tuple[str, ...]) -> str | None:
    for k in keys:
        v = row.get(k)
        if v is not None and str(v).strip() not in ("", '""'):
            return str(v).strip().strip('"')
    return None

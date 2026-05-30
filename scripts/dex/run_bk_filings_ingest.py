#!/usr/bin/env python3
"""Epiq11 bankruptcy full-docket PDF ingest — multi-case parameterized.

Queries entities.source_epiq_dockets for every docket entry that has at least
one attached PDF (jsonb_array_length(docket_documents) > 0), then fetches each
PDF from Epiq's getdocumentbycode endpoint, uploads to Supabase Storage bucket
`bk-epiq-filings` under a content-addressable key, and writes one manifest row
per PDF to entities.source_bk_filings.

Stage 2 (parsing PDFs into table rows) is explicitly deferred. This script is
acquisition-only.

Idempotency:
    If a row already exists for (project_code, document_id) with matching
    pdf_sha256, the upload and DB write are both skipped (content-addressable
    key means the storage object is also already present). If pdf_sha256
    differs, re-upload and update.

Audit:
    One row per (invocation, project_code) in ops.bk_filings_ingest_runs.

Usage:
    PYTHONPATH=. doppler run --project hq-all --config prd -- \\
        python3 scripts/run_bk_filings_ingest.py
    PYTHONPATH=. doppler run --project hq-all --config prd -- \\
        python3 scripts/run_bk_filings_ingest.py --case spirit
    PYTHONPATH=. doppler run --project hq-all --config prd -- \\
        python3 scripts/run_bk_filings_ingest.py --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import os
import random
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
import psycopg
from psycopg.types.json import Jsonb
from supabase import create_client


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

PROVIDER = "epiq11"
STORAGE_BUCKET = "bk-epiq-filings"
EPIQ_DOC_URL_TEMPLATE = (
    "https://document.epiq11.com/document/getdocumentbycode"
    "?docId={doc_id}&projectCode={project_code}&source=DM"
)

# Multi-case config: slug -> upstream projectCode (Epiq-internal short code).
# Both fields come from source_epiq_dockets.upstream_project_code and
# project_code; sourced here from existing Epiq ingest scripts pattern.
_ENABLED_CASES = ("spirit",)

# Polite fetch constants — mirror run_epiq_dockets_ingest.py
REQUEST_DELAY_SEC = 0.5
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_0) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
_RETRY_STATUSES = {403, 429, 500, 502, 503, 504}


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Supabase client
# --------------------------------------------------------------------------- #

def _get_supabase_client():
    """Build Supabase client from Doppler-injected env vars."""
    url = os.environ.get("DEX_SUPABASE_URL")
    key = os.environ.get("DEX_SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise RuntimeError(
            "DEX_SUPABASE_URL and DEX_SUPABASE_SERVICE_ROLE_KEY must be set — "
            "invoke under `doppler run --project hq-all --config prd -- ...`"
        )
    return create_client(url, key)


# --------------------------------------------------------------------------- #
# Storage helpers — mirror run_sec_form_adv_pdfs_ingest.py
# --------------------------------------------------------------------------- #

def ensure_bucket(supabase) -> None:
    """Create the storage bucket if it doesn't exist (idempotent)."""
    try:
        existing = supabase.storage.list_buckets()
        names = {b.name if hasattr(b, "name") else b.get("name") for b in existing}
        if STORAGE_BUCKET in names:
            log.info("bucket_exists bucket=%s", STORAGE_BUCKET)
            return
        supabase.storage.create_bucket(
            STORAGE_BUCKET,
            options={"public": False},
        )
        log.info("bucket_created bucket=%s", STORAGE_BUCKET)
    except Exception as exc:
        msg = repr(exc).lower()
        if "already exists" in msg or "duplicate" in msg or "exists" in msg:
            log.info("bucket_already_exists bucket=%s", STORAGE_BUCKET)
            return
        raise


def upload_pdf(supabase, *, storage_key: str, pdf_bytes: bytes) -> None:
    """Upload PDF bytes to Supabase Storage (upsert=true)."""
    try:
        supabase.storage.from_(STORAGE_BUCKET).upload(
            path=storage_key,
            file=pdf_bytes,
            file_options={"content-type": "application/pdf", "upsert": "true"},
        )
    except Exception as exc:
        raise RuntimeError(
            f"storage upload failed for {storage_key}: {exc}"
        ) from exc


# --------------------------------------------------------------------------- #
# HTTP fetch — mirror run_epiq_dockets_ingest.py retry/backoff pattern
# --------------------------------------------------------------------------- #

def _fetch_pdf(url: str) -> tuple[bytes, str | None]:
    """GET a PDF URL with retry/backoff.

    Returns (pdf_bytes, filename_from_content_disposition | None).
    """
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/pdf,*/*",
        "Referer": "https://dm.epiq11.com/",
    }

    last_exc: Exception | None = None
    for attempt in range(1, 5):
        time.sleep(REQUEST_DELAY_SEC)
        try:
            with httpx.Client(headers=headers, follow_redirects=True) as c:
                resp = c.get(url, timeout=120.0)
            if resp.status_code in _RETRY_STATUSES:
                resp.raise_for_status()
            resp.raise_for_status()
            pdf_bytes = resp.content

            # Parse Content-Disposition for filename
            filename: str | None = None
            cd = resp.headers.get("content-disposition", "")
            if cd:
                for part in cd.split(";"):
                    part = part.strip()
                    if part.lower().startswith("filename="):
                        filename = part[9:].strip().strip('"').strip("'") or None
                    elif part.lower().startswith("filename*="):
                        # RFC 5987 encoded — strip encoding prefix
                        raw = part[10:].strip()
                        if "''" in raw:
                            filename = raw.split("''", 1)[1] or None
                        else:
                            filename = raw or None

            return pdf_bytes, filename

        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            last_exc = exc
            if attempt >= 4:
                raise
            backoff = (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            log.warning(
                "pdf_fetch_retry url=%s attempt=%d exc=%s sleeping=%.2fs",
                url, attempt, exc, backoff,
            )
            time.sleep(backoff)
    raise last_exc  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# PDF page count — optional, via pdfplumber
# --------------------------------------------------------------------------- #

def _page_count(pdf_bytes: bytes) -> int | None:
    """Return page count if pdfplumber is available; else None."""
    try:
        import pdfplumber  # noqa: PLC0415
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            return len(pdf.pages)
    except Exception as exc:
        log.debug("page_count_unavailable exc=%s", exc)
        return None


# --------------------------------------------------------------------------- #
# DB helpers
# --------------------------------------------------------------------------- #

_UPSERT_SQL = """
INSERT INTO entities.source_bk_filings (
    project_code,
    document_id,
    docket_source_notice_id,
    pdf_url,
    storage_bucket,
    storage_key,
    pdf_sha256,
    pdf_byte_size,
    page_count,
    raw_source_row,
    source_provider,
    source_filename,
    source_download_url,
    source_observed_at,
    source_run_metadata,
    source_task_id,
    source_schedule_id
) VALUES (
    %(project_code)s,
    %(document_id)s,
    %(docket_source_notice_id)s,
    %(pdf_url)s,
    %(storage_bucket)s,
    %(storage_key)s,
    %(pdf_sha256)s,
    %(pdf_byte_size)s,
    %(page_count)s,
    %(raw_source_row)s,
    %(source_provider)s,
    %(source_filename)s,
    %(source_download_url)s,
    %(source_observed_at)s,
    %(source_run_metadata)s,
    %(source_task_id)s,
    %(source_schedule_id)s
)
ON CONFLICT (project_code, document_id) DO UPDATE SET
    docket_source_notice_id = EXCLUDED.docket_source_notice_id,
    pdf_url                 = EXCLUDED.pdf_url,
    storage_bucket          = EXCLUDED.storage_bucket,
    storage_key             = EXCLUDED.storage_key,
    pdf_sha256              = EXCLUDED.pdf_sha256,
    pdf_byte_size           = EXCLUDED.pdf_byte_size,
    page_count              = COALESCE(EXCLUDED.page_count, source_bk_filings.page_count),
    raw_source_row          = EXCLUDED.raw_source_row,
    source_provider         = EXCLUDED.source_provider,
    source_filename         = EXCLUDED.source_filename,
    source_download_url     = EXCLUDED.source_download_url,
    source_observed_at      = EXCLUDED.source_observed_at,
    source_run_metadata     = EXCLUDED.source_run_metadata,
    ingested_at             = now()
WHERE
    entities.source_bk_filings.pdf_sha256 IS DISTINCT FROM EXCLUDED.pdf_sha256
"""


def _fetch_docket_rows(conn: psycopg.Connection, case_slug: str) -> list[dict]:
    """Query source_epiq_dockets for all docket entries with at least one PDF."""
    sql = """
        SELECT
            source_notice_id,
            docket_name,
            docket_documents,
            upstream_project_code,
            source_observed_at
        FROM entities.source_epiq_dockets
        WHERE project_code = %s
          AND docket_documents IS NOT NULL
          AND jsonb_array_length(docket_documents) > 0
        ORDER BY source_notice_id
    """
    with conn.cursor() as cur:
        cur.execute(sql, (case_slug,))
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _fetch_existing_sha(conn: psycopg.Connection, project_code: str, document_id: str) -> str | None:
    """Return the stored pdf_sha256 for (project_code, document_id), or None."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pdf_sha256 FROM entities.source_bk_filings "
            "WHERE project_code = %s AND document_id = %s",
            (project_code, document_id),
        )
        row = cur.fetchone()
        return row[0] if row else None


# --------------------------------------------------------------------------- #
# Per-case ingest
# --------------------------------------------------------------------------- #

def ingest_case(
    conn: psycopg.Connection,
    supabase,
    case_slug: str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Full PDF fetch + manifest upsert for one Epiq case slug.

    Returns summary dict.
    """
    observed = datetime.now(timezone.utc)
    run_id: str | None = None

    if not dry_run:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ops.bk_filings_ingest_runs
                    (project_code, status, source_observed_at)
                VALUES (%s, 'running', now())
                RETURNING run_id::text
                """,
                (case_slug,),
            )
            run_id = cur.fetchone()[0]
        conn.commit()
        log.info("ops.bk_filings_ingest_runs case=%s run_id=%s", case_slug, run_id)

    dockets_scanned = 0
    filings_seen = 0
    filings_upserted = 0
    filings_skipped = 0
    filings_failed = 0
    pdfs_uploaded = 0

    try:
        docket_rows = _fetch_docket_rows(conn, case_slug)
        dockets_scanned = len(docket_rows)
        log.info(
            "bk_filings case=%s dockets_found=%d (full docket — has-documents filter)",
            case_slug, dockets_scanned,
        )

        for docket in docket_rows:
            source_notice_id: str = docket["source_notice_id"]
            docket_documents = docket["docket_documents"]
            upstream_project_code: str = docket["upstream_project_code"] or case_slug.upper()

            # docket_documents is a list of dicts; each has a documentId field.
            if not isinstance(docket_documents, list):
                log.warning(
                    "bk_filings case=%s notice=%s docket_documents not a list — skipping",
                    case_slug, source_notice_id,
                )
                continue

            for doc_elem in docket_documents:
                if not isinstance(doc_elem, dict):
                    continue

                doc_id_raw = doc_elem.get("documentId")
                if doc_id_raw is None:
                    log.warning(
                        "bk_filings case=%s notice=%s missing documentId in element — skipping",
                        case_slug, source_notice_id,
                    )
                    continue

                document_id = str(doc_id_raw)
                filings_seen += 1

                pdf_url = EPIQ_DOC_URL_TEMPLATE.format(
                    doc_id=document_id,
                    project_code=upstream_project_code,
                )

                if dry_run:
                    log.info(
                        "DRY_RUN bk_filings case=%s notice=%s doc_id=%s url=%s",
                        case_slug, source_notice_id, document_id, pdf_url,
                    )
                    continue

                # Progress heartbeat every 50 filings
                if filings_seen % 50 == 0:
                    log.info(
                        "bk_filings progress case=%s seen=%d upserted=%d "
                        "skipped=%d failed=%d uploaded=%d",
                        case_slug, filings_seen, filings_upserted,
                        filings_skipped, filings_failed, pdfs_uploaded,
                    )

                try:
                    # Idempotency fast-path: check existing sha256
                    existing_sha = _fetch_existing_sha(conn, case_slug, document_id)

                    # Fetch PDF bytes
                    log.info(
                        "bk_filings case=%s notice=%s doc_id=%s fetching url=%s",
                        case_slug, source_notice_id, document_id, pdf_url,
                    )
                    pdf_bytes, content_disposition_filename = _fetch_pdf(pdf_url)
                    pdf_sha256 = hashlib.sha256(pdf_bytes).hexdigest()
                    pdf_byte_size = len(pdf_bytes)

                    if existing_sha == pdf_sha256:
                        log.info(
                            "bk_filings case=%s doc_id=%s skipped — sha256 unchanged (%s)",
                            case_slug, document_id, pdf_sha256[:16],
                        )
                        filings_skipped += 1
                        continue

                    # Content-addressable storage key (mirror Form-ADV Part 2 pattern)
                    storage_key = f"{case_slug}/{pdf_sha256}.pdf"

                    # Upload to Supabase Storage
                    upload_pdf(supabase, storage_key=storage_key, pdf_bytes=pdf_bytes)
                    pdfs_uploaded += 1
                    log.info(
                        "bk_filings case=%s doc_id=%s uploaded storage_key=%s bytes=%d",
                        case_slug, document_id, storage_key, pdf_byte_size,
                    )

                    # Page count (optional)
                    pages = _page_count(pdf_bytes)

                    # Determine source_filename: prefer Content-Disposition, fall back to
                    # documentDownloadName from the docket_documents element
                    source_filename = content_disposition_filename or doc_elem.get("documentDownloadName")

                    row_params = {
                        "project_code":             case_slug,
                        "document_id":              document_id,
                        "docket_source_notice_id":  source_notice_id,
                        "pdf_url":                  pdf_url,
                        "storage_bucket":           STORAGE_BUCKET,
                        "storage_key":              storage_key,
                        "pdf_sha256":               pdf_sha256,
                        "pdf_byte_size":            pdf_byte_size,
                        "page_count":               pages,
                        "raw_source_row":           Jsonb(doc_elem),
                        "source_provider":          PROVIDER,
                        "source_filename":          source_filename,
                        "source_download_url":      pdf_url,
                        "source_observed_at":       observed,
                        "source_run_metadata":      Jsonb({
                            "run_id": run_id,
                            "case": case_slug,
                            "docket_source_notice_id": source_notice_id,
                        }),
                        "source_task_id":           None,
                        "source_schedule_id":       None,
                    }

                    with conn.cursor() as cur:
                        cur.execute(_UPSERT_SQL, row_params)
                        upserted = cur.rowcount
                    conn.commit()

                    if upserted > 0:
                        filings_upserted += 1
                        log.info(
                            "bk_filings case=%s doc_id=%s upserted sha=%s pages=%s",
                            case_slug, document_id, pdf_sha256[:16], pages,
                        )
                    else:
                        # ON CONFLICT matched but sha was not distinct (race condition
                        # — shouldn't happen since we checked above, but handle safely)
                        filings_skipped += 1
                        log.info(
                            "bk_filings case=%s doc_id=%s no-op (sha unchanged after fetch)",
                            case_slug, document_id,
                        )

                except Exception as pdf_exc:
                    # Single-PDF failure: log and continue; don't abort the whole run.
                    filings_failed += 1
                    log.warning(
                        "bk_filings case=%s notice=%s doc_id=%s FAILED (skipping) exc=%s",
                        case_slug, source_notice_id, document_id, pdf_exc,
                    )
                    try:
                        conn.rollback()
                    except Exception:
                        pass

        # Close run row
        if not dry_run and run_id:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE ops.bk_filings_ingest_runs
                    SET status='succeeded',
                        completed_at=now(),
                        dockets_scanned=%s,
                        filings_seen=%s,
                        filings_upserted=%s,
                        filings_skipped=%s,
                        pdfs_uploaded=%s
                    WHERE run_id=%s
                    """,
                    (
                        dockets_scanned, filings_seen, filings_upserted,
                        filings_skipped, pdfs_uploaded, run_id,
                    ),
                )
            conn.commit()

        return {
            "case": case_slug,
            "status": "succeeded",
            "dockets_scanned": dockets_scanned,
            "filings_seen": filings_seen,
            "filings_upserted": filings_upserted,
            "filings_skipped": filings_skipped,
            "filings_failed": filings_failed,
            "pdfs_uploaded": pdfs_uploaded,
            "run_id": run_id,
        }

    except Exception as exc:
        log.exception("bk_filings ingest case=%s failed: %s", case_slug, exc)
        if not dry_run and run_id:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE ops.bk_filings_ingest_runs
                    SET status='failed', completed_at=now(), error_text=%s
                    WHERE run_id=%s
                    """,
                    (str(exc)[:1000], run_id),
                )
            conn.commit()
        raise


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case",
        choices=list(_ENABLED_CASES),
        default=None,
        help="Case slug (e.g. spirit). Default: all enabled cases.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Query DB + print URLs but do not fetch PDFs or write to DB.",
    )
    args = parser.parse_args(argv)

    cases = (args.case,) if args.case else _ENABLED_CASES

    db_url = os.environ.get("DEX_DB_URL_POOLED")
    if not db_url:
        log.error("DEX_DB_URL_POOLED is not set — invoke under doppler run")
        sys.exit(1)

    if args.dry_run:
        log.info("DRY RUN — no DB writes, no PDF fetches")

    supabase = _get_supabase_client()
    if not args.dry_run:
        ensure_bucket(supabase)

    conn = psycopg.connect(db_url)
    try:
        summaries: dict[str, Any] = {}
        for case in cases:
            summaries[case] = ingest_case(
                conn, supabase, case, dry_run=args.dry_run,
            )

        result = {"status": "succeeded", "cases": summaries}
        log.info("Done: %s", json.dumps(result, default=str))
        return result

    except Exception as exc:
        log.exception("bk_filings multi-case ingest failed: %s", exc)
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Stage 2: raw PDF table extraction for Epiq11 bankruptcy filings.

Reads entities.source_bk_filings for unparsed filings (parsed_at IS NULL),
downloads each PDF from Supabase Storage bucket `bk-epiq-filings`, opens
with pdfplumber, walks every page, extracts every table, and writes one row
per table-row to entities.source_bk_filing_table_rows.

Also detects schedule-section headers via regex on page text (Schedule A/B,
D, E/F, F, G, H; Statement of Financial Affairs with SOFA-N part tags).
The last matched section on a page carries forward to subsequent pages
until a new section header is found.

This is RAW extraction — no classification, no normalization, no joins.
Downstream LLM classification, name normalization, fuzzy-match to
source_epiq_claims, and MV computation are explicitly deferred.

Idempotency:
    Skips filings where parsed_at IS NOT NULL (already parsed).
    --reparse flag re-parses all filings regardless of parsed_at (deletes
    prior rows in same transaction, then reinserts).

Usage:
    PYTHONPATH=apps/data-engine-x doppler run --project hq-all --config prd -- \\
        python3 apps/data-engine-x/scripts/run_bk_filing_table_rows_parse.py \\
        --case spirit

    # Single filing (debug):
    PYTHONPATH=apps/data-engine-x doppler run --project hq-all --config prd -- \\
        python3 apps/data-engine-x/scripts/run_bk_filing_table_rows_parse.py \\
        --case spirit --document-id 4530894

    # Force re-parse (clears prior rows):
    PYTHONPATH=apps/data-engine-x doppler run --project hq-all --config prd -- \\
        python3 apps/data-engine-x/scripts/run_bk_filing_table_rows_parse.py \\
        --case spirit --reparse
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any

import httpx
import psycopg
from psycopg.types.json import Jsonb

try:
    import pdfplumber  # noqa: F401
    _PDFPLUMBER_VERSION = pdfplumber.__version__
except ImportError as _err:
    raise ImportError(
        "pdfplumber is required — install via: uv add pdfplumber  or  pip install pdfplumber"
    ) from _err


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

PROVIDER = "pdfplumber"
STORAGE_BUCKET = "bk-epiq-filings"

_ENABLED_CASES = ("spirit",)


# --------------------------------------------------------------------------- #
# Schedule-section regex patterns
# --------------------------------------------------------------------------- #
# We scan the full page text and collect ALL matches; the last one on the
# page is used as the current section (section headers appear at page top,
# but tables sometimes span sections within a single page).
#
# Patterns (case-insensitive):
#   SOFA part N:   "Statement of Financial Affairs" ... "Part N" (1-99)
#   Schedules:     "Schedule A/B", "Schedule D", "Schedule E/F", "Schedule E",
#                  "Schedule F", "Schedule G", "Schedule H"
#
# SOFA parts map to SOFA-N tags (e.g., Part 3 -> SOFA-3).

# SOFA part pattern: captures the part number (group 1)
_SOFA_PART_RE = re.compile(
    r"Statement\s+of\s+Financial\s+Affairs.*?Part\s+(\d{1,2})",
    re.IGNORECASE | re.DOTALL,
)

# Plain SOFA (no part number found)
_SOFA_PLAIN_RE = re.compile(
    r"Statement\s+of\s+Financial\s+Affairs",
    re.IGNORECASE,
)

# Schedule letter patterns (order: longest/most-specific first)
_SCHEDULE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"Schedule\s+A\s*/\s*B", re.IGNORECASE), "A/B"),
    (re.compile(r"Schedule\s+E\s*/\s*F", re.IGNORECASE), "E/F"),
    (re.compile(r"Schedule\s+A\b", re.IGNORECASE), "A"),
    (re.compile(r"Schedule\s+B\b", re.IGNORECASE), "B"),
    (re.compile(r"Schedule\s+D\b", re.IGNORECASE), "D"),
    (re.compile(r"Schedule\s+E\b", re.IGNORECASE), "E"),
    (re.compile(r"Schedule\s+F\b", re.IGNORECASE), "F"),
    (re.compile(r"Schedule\s+G\b", re.IGNORECASE), "G"),
    (re.compile(r"Schedule\s+H\b", re.IGNORECASE), "H"),
    (re.compile(r"Schedule\s+I\b", re.IGNORECASE), "I"),
    (re.compile(r"Schedule\s+J\b", re.IGNORECASE), "J"),
]


def _detect_section_from_text(page_text: str) -> str | None:
    """Return the last schedule-section label found in page_text, or None.

    Returns tags like 'G', 'F', 'A/B', 'E/F', 'SOFA-3', 'SOFA'.
    Precedence: later-in-text match wins (header at top, tables below).
    """
    if not page_text:
        return None

    matches: list[tuple[int, str]] = []  # (match_start_pos, tag)

    # SOFA with part number
    for m in _SOFA_PART_RE.finditer(page_text):
        part_num = m.group(1)
        matches.append((m.start(), f"SOFA-{part_num}"))

    # Plain SOFA (only if no SOFA-with-part already at this position)
    for m in _SOFA_PLAIN_RE.finditer(page_text):
        matches.append((m.start(), "SOFA"))

    # Schedule letters
    for pattern, tag in _SCHEDULE_PATTERNS:
        for m in pattern.finditer(page_text):
            matches.append((m.start(), tag))

    if not matches:
        return None

    # Return the tag from the last match by position
    return max(matches, key=lambda x: x[0])[1]


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Supabase / storage download
# --------------------------------------------------------------------------- #

def _get_supabase_creds() -> tuple[str, str]:
    """Return (supabase_url, service_role_key) from env."""
    url = os.environ.get("DEX_SUPABASE_URL")
    key = os.environ.get("DEX_SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise RuntimeError(
            "DEX_SUPABASE_URL and DEX_SUPABASE_SERVICE_ROLE_KEY must be set — "
            "invoke under `doppler run --project hq-all --config prd -- ...`"
        )
    return url, key


def _download_from_bucket(supabase_url: str, service_key: str, storage_key: str) -> bytes:
    """Download PDF bytes from Supabase Storage using direct HTTP.

    Mirrors apps/data-engine-x/app/services/fmcsa_artifact_ingest.py
    download_artifact_from_storage pattern — avoids buffering issues with
    the Supabase Python client for large PDFs.
    """
    url = f"{supabase_url}/storage/v1/object/{STORAGE_BUCKET}/{storage_key}"
    headers = {
        "Authorization": f"Bearer {service_key}",
        "apikey": service_key,
    }
    resp = httpx.get(url, headers=headers, timeout=600.0)
    if resp.status_code != 200:
        raise RuntimeError(
            f"Storage download failed for {storage_key}: "
            f"HTTP {resp.status_code} — {resp.text[:500]}"
        )
    return resp.content


# --------------------------------------------------------------------------- #
# DB helpers
# --------------------------------------------------------------------------- #

def _fetch_filings(
    conn: psycopg.Connection,
    project_code: str,
    *,
    reparse: bool,
    document_id: str | None,
) -> list[dict]:
    """Query source_bk_filings for filings to parse."""
    conditions = ["project_code = %s"]
    params: list[Any] = [project_code]

    if document_id:
        conditions.append("document_id = %s")
        params.append(document_id)

    if not reparse:
        conditions.append("parsed_at IS NULL")

    where = " AND ".join(conditions)
    sql = f"""
        SELECT
            project_code,
            document_id,
            storage_key,
            storage_bucket,
            source_filename,
            page_count
        FROM entities.source_bk_filings
        WHERE {where}
        ORDER BY document_id
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


_UPSERT_SQL = """
INSERT INTO entities.source_bk_filing_table_rows (
    project_code,
    document_id,
    page_number,
    table_index_on_page,
    row_index_in_table,
    schedule_section,
    columns_array,
    row_data,
    is_header_row,
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
    %(page_number)s,
    %(table_index_on_page)s,
    %(row_index_in_table)s,
    %(schedule_section)s,
    %(columns_array)s,
    %(row_data)s,
    %(is_header_row)s,
    %(raw_source_row)s,
    %(source_provider)s,
    %(source_filename)s,
    %(source_download_url)s,
    %(source_observed_at)s,
    %(source_run_metadata)s,
    %(source_task_id)s,
    %(source_schedule_id)s
)
ON CONFLICT (project_code, document_id, page_number, table_index_on_page, row_index_in_table)
DO UPDATE SET
    schedule_section     = EXCLUDED.schedule_section,
    columns_array        = EXCLUDED.columns_array,
    row_data             = EXCLUDED.row_data,
    is_header_row        = EXCLUDED.is_header_row,
    raw_source_row       = EXCLUDED.raw_source_row,
    source_provider      = EXCLUDED.source_provider,
    source_filename      = EXCLUDED.source_filename,
    source_download_url  = EXCLUDED.source_download_url,
    source_observed_at   = EXCLUDED.source_observed_at,
    source_run_metadata  = EXCLUDED.source_run_metadata,
    ingested_at          = now()
"""


# --------------------------------------------------------------------------- #
# Cell normalization
# --------------------------------------------------------------------------- #

def _normalize_cell(cell: Any) -> Any:
    """Normalize a pdfplumber cell value for JSONB storage.

    - None stays None (serializes as JSONB null).
    - Strings: strip leading/trailing whitespace; preserve empty strings.
    - Non-string scalars: convert to string.
    """
    if cell is None:
        return None
    if isinstance(cell, str):
        return cell.strip()
    return str(cell)


# --------------------------------------------------------------------------- #
# Per-filing parse
# --------------------------------------------------------------------------- #

def parse_filing(
    conn: psycopg.Connection,
    *,
    project_code: str,
    document_id: str,
    storage_key: str,
    source_filename: str | None,
    supabase_url: str,
    service_key: str,
    observed_at: datetime,
    run_metadata: dict,
    reparse: bool,
) -> dict[str, Any]:
    """Download + parse one PDF filing; bulk-insert table rows.

    Returns summary dict.
    """
    import pdfplumber

    log.info("parse_filing project=%s doc=%s storage_key=%s", project_code, document_id, storage_key)

    # Download PDF bytes from bucket
    pdf_bytes = _download_from_bucket(supabase_url, service_key, storage_key)
    log.info(
        "parse_filing doc=%s downloaded bytes=%d", document_id, len(pdf_bytes)
    )

    # If --reparse, delete prior rows in this transaction first
    if reparse:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM entities.source_bk_filing_table_rows "
                "WHERE project_code = %s AND document_id = %s",
                (project_code, document_id),
            )
            deleted = cur.rowcount
        if deleted:
            log.info("parse_filing doc=%s reparse: deleted %d prior rows", document_id, deleted)

    rows_to_insert: list[dict[str, Any]] = []

    pages_processed = 0
    tables_found = 0
    section_counts: dict[str, int] = {}

    current_section: str | None = None

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            total_pages = len(pdf.pages)
            log.info("parse_filing doc=%s pages=%d", document_id, total_pages)

            for page_idx, page in enumerate(pdf.pages):
                page_num = page_idx + 1  # 1-indexed

                # Detect schedule section from page text
                try:
                    page_text = page.extract_text() or ""
                    section_on_page = _detect_section_from_text(page_text)
                    if section_on_page is not None:
                        current_section = section_on_page
                except Exception as exc:
                    log.debug(
                        "parse_filing doc=%s page=%d text extract failed: %s",
                        document_id, page_num, exc,
                    )
                    page_text = ""

                # Extract tables
                try:
                    tables = page.extract_tables()
                except Exception as exc:
                    log.warning(
                        "parse_filing doc=%s page=%d extract_tables failed: %s — skipping page",
                        document_id, page_num, exc,
                    )
                    pages_processed += 1
                    continue

                if not tables:
                    log.debug("parse_filing doc=%s page=%d no tables", document_id, page_num)
                    pages_processed += 1
                    continue

                for table_idx, table in enumerate(tables):
                    if not table:
                        continue

                    tables_found += 1
                    if current_section:
                        section_counts[current_section] = section_counts.get(current_section, 0) + len(table)

                    # columns_array = normalized cells of row 0
                    header_row_raw = table[0] if table else []
                    columns_array = [_normalize_cell(c) for c in header_row_raw] if header_row_raw else None

                    for row_idx, raw_row in enumerate(table):
                        if raw_row is None:
                            continue

                        normalized_cells = [_normalize_cell(c) for c in raw_row]
                        is_header = row_idx == 0

                        # raw_source_row: full pdfplumber row as-is, plus context
                        raw_source = {
                            "row": raw_row,   # raw list-of-cells from pdfplumber
                            "page_number": page_num,
                            "table_index_on_page": table_idx,
                            "row_index_in_table": row_idx,
                            "page_width": float(page.width) if page.width else None,
                            "page_height": float(page.height) if page.height else None,
                        }

                        rows_to_insert.append({
                            "project_code":         project_code,
                            "document_id":          document_id,
                            "page_number":          page_num,
                            "table_index_on_page":  table_idx,
                            "row_index_in_table":   row_idx,
                            "schedule_section":     current_section,
                            "columns_array":        Jsonb(columns_array),
                            "row_data":             Jsonb(normalized_cells),
                            "is_header_row":        is_header,
                            "raw_source_row":       Jsonb(raw_source),
                            "source_provider":      PROVIDER,
                            "source_filename":      source_filename or storage_key,
                            "source_download_url":  None,
                            "source_observed_at":   observed_at,
                            "source_run_metadata":  Jsonb(run_metadata),
                            "source_task_id":       None,
                            "source_schedule_id":   None,
                        })

                pages_processed += 1

    except Exception as exc:
        log.error(
            "parse_filing doc=%s FAILED to open/parse PDF: %s", document_id, exc
        )
        return {
            "document_id": document_id,
            "status": "failed",
            "error": str(exc),
            "rows_inserted": 0,
        }

    if not rows_to_insert:
        log.warning("parse_filing doc=%s yielded 0 rows (no tables found)", document_id)
        # Still mark parsed_at so we don't retry indefinitely on empty PDFs
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE entities.source_bk_filings SET parsed_at = now() "
                "WHERE project_code = %s AND document_id = %s",
                (project_code, document_id),
            )
        conn.commit()
        return {
            "document_id": document_id,
            "status": "succeeded",
            "pages_processed": pages_processed,
            "tables_found": tables_found,
            "rows_inserted": 0,
            "section_counts": section_counts,
        }

    # Bulk upsert + mark parsed_at in a single transaction
    BATCH_SIZE = 500
    total_inserted = 0
    with conn.cursor() as cur:
        for i in range(0, len(rows_to_insert), BATCH_SIZE):
            batch = rows_to_insert[i : i + BATCH_SIZE]
            cur.executemany(_UPSERT_SQL, batch)
            total_inserted += len(batch)

        # Mark parsed_at on source_bk_filings
        cur.execute(
            "UPDATE entities.source_bk_filings SET parsed_at = now() "
            "WHERE project_code = %s AND document_id = %s",
            (project_code, document_id),
        )
    conn.commit()

    log.info(
        "parsed: %s/%s — %d pages, %d tables, %d table rows, sections: %s",
        project_code,
        document_id,
        pages_processed,
        tables_found,
        total_inserted,
        json.dumps(section_counts),
    )

    return {
        "document_id": document_id,
        "status": "succeeded",
        "pages_processed": pages_processed,
        "tables_found": tables_found,
        "rows_inserted": total_inserted,
        "section_counts": section_counts,
    }


# --------------------------------------------------------------------------- #
# Per-case runner
# --------------------------------------------------------------------------- #

def run_case(
    conn: psycopg.Connection,
    *,
    case_slug: str,
    supabase_url: str,
    service_key: str,
    reparse: bool,
    document_id: str | None,
) -> dict[str, Any]:
    """Parse all unparsed filings for one case slug."""
    observed_at = datetime.now(timezone.utc)

    run_metadata = {
        "pdfplumber_version": _PDFPLUMBER_VERSION,
        "case": case_slug,
        "reparse": reparse,
        "section_regexes": [
            "Schedule A/B", "Schedule E/F", "Schedule A", "Schedule B",
            "Schedule D", "Schedule E", "Schedule F", "Schedule G",
            "Schedule H", "Schedule I", "Schedule J",
            "Statement of Financial Affairs (with Part N -> SOFA-N)",
        ],
    }

    filings = _fetch_filings(
        conn,
        case_slug,
        reparse=reparse,
        document_id=document_id,
    )

    log.info(
        "run_case case=%s filings_to_parse=%d reparse=%s doc_filter=%s",
        case_slug, len(filings), reparse, document_id,
    )

    if not filings:
        log.info("run_case case=%s — no filings to parse (all have parsed_at set)", case_slug)
        return {"case": case_slug, "status": "skipped", "filings": []}

    summaries = []
    failure_count = 0

    for filing in filings:
        try:
            summary = parse_filing(
                conn,
                project_code=filing["project_code"],
                document_id=filing["document_id"],
                storage_key=filing["storage_key"],
                source_filename=filing.get("source_filename"),
                supabase_url=supabase_url,
                service_key=service_key,
                observed_at=observed_at,
                run_metadata=run_metadata,
                reparse=reparse,
            )
            summaries.append(summary)
            if summary.get("status") == "failed":
                failure_count += 1
        except Exception as exc:
            log.exception(
                "run_case case=%s doc=%s unexpected error: %s",
                case_slug, filing["document_id"], exc,
            )
            summaries.append({
                "document_id": filing["document_id"],
                "status": "failed",
                "error": str(exc),
                "rows_inserted": 0,
            })
            failure_count += 1

    total_rows = sum(s.get("rows_inserted", 0) for s in summaries)
    log.info(
        "run_case case=%s complete — filings=%d failures=%d total_rows=%d",
        case_slug, len(filings), failure_count, total_rows,
    )

    return {
        "case": case_slug,
        "status": "succeeded" if failure_count == 0 else "partial",
        "filings_attempted": len(filings),
        "failures": failure_count,
        "total_rows_inserted": total_rows,
        "summaries": summaries,
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case",
        choices=list(_ENABLED_CASES),
        default=None,
        help="Case slug (e.g. spirit). Default: all enabled cases.",
    )
    parser.add_argument(
        "--document-id",
        default=None,
        help="Parse a single filing by document_id (debug aid).",
    )
    parser.add_argument(
        "--reparse",
        action="store_true",
        help="Re-parse all filings, ignoring parsed_at. Deletes prior rows.",
    )
    args = parser.parse_args(argv)

    cases = (args.case,) if args.case else _ENABLED_CASES

    db_url = os.environ.get("DEX_DB_URL_POOLED")
    if not db_url:
        log.error("DEX_DB_URL_POOLED is not set — invoke under doppler run")
        sys.exit(1)

    supabase_url, service_key = _get_supabase_creds()

    conn = psycopg.connect(db_url)
    try:
        results: dict[str, Any] = {}
        for case in cases:
            results[case] = run_case(
                conn,
                case_slug=case,
                supabase_url=supabase_url,
                service_key=service_key,
                reparse=args.reparse,
                document_id=args.document_id,
            )

        log.info("Done: %s", json.dumps(results, default=str))

    except Exception as exc:
        log.exception("bk_filing_table_rows_parse failed: %s", exc)
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()

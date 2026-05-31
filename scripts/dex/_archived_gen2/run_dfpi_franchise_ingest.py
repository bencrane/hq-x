#!/usr/bin/env python3
"""DFPI Franchise Registry — SearchStax/Solr ingest.

Source:
    DFPI public franchise filing index, served by SearchStax-hosted Solr at
    https://searchcloud-1-us-west-2.searchstax.com/29847/dfpiprod-1839/emselect
    behind a token scraped from
    https://dfpi.ca.gov/regulated-industries/regulated-entities-list/

Auth:
    No Doppler secret. The Solr `select_auth_token` is embedded in the public
    page HTML and rotates without notice. We re-scrape it on every run; on
    auth failure mid-run we re-scrape and retry once before failing.

Pagination:
    start + rows + `sort=id asc`. Stops when a page returns < rows. Politeness
    sleep between pages because the dataset is small (~54k docs / ~54 pages
    at rows=1000) and the SearchStax tier we're hitting is shared.

Idempotency:
    PK on filing_guid (Solr `id`); ON CONFLICT DO UPDATE. Documents have PK
    on document_id with the same pattern.

Audit:
    One row per invocation in ops.dfpi_ingest_runs.

Usage:
    PYTHONPATH=. doppler run -- python3 scripts/run_dfpi_franchise_ingest.py
    PYTHONPATH=. doppler run -- python3 scripts/run_dfpi_franchise_ingest.py --dry-run --max-pages 1
    PYTHONPATH=. doppler run -- python3 scripts/run_dfpi_franchise_ingest.py --skip-if-unchanged
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

import httpx
import psycopg
from psycopg.types.json import Jsonb


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

DFPI_PAGE_URL = "https://dfpi.ca.gov/regulated-industries/regulated-entities-list/"
SOLR_BASE = "https://searchcloud-1-us-west-2.searchstax.com/29847/dfpiprod-1839/emselect"
SOLR_COLLECTION = "dfpiprod-1839"
USER_AGENT = "data-engine-x-api/dfpi-franchise-ingest"

DEFAULT_PAGE_SIZE = 1000
DEFAULT_PAGE_SLEEP = 0.5
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 5

SOLR_FQ = ['ss_content_type_s:"Regulated Entity"', "ss_industry_s:Franchises"]

# Fields requested per doc. Listed explicitly so a server-side index change
# that adds a new field does not silently bloat raw_payload.
SOLR_FL = (
    "id,App_ID,Org_Legal_Name,Formerly_Known_As,"
    "Application_Notice_Type,Filing_Type,subFilingCategory_s,"
    "app_notice_status_s,app_notice_sub_status_s,"
    "ENFCASES,PublicActions,"
    "Date_Filed,Effective_Date,Status_Date,exif_date,"
    "_version_,ImportedFromIntermediateDB,"
    "Documents,uri"
)

# Token scraping. The page ships two `select_auth_token: '<hex>'` blocks; the
# first is prod (paired with dfpiprod-1839), the second is dev. Match the
# first occurrence.
TOKEN_RE = re.compile(r"select_auth_token:\s*'([a-f0-9]+)'")


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("dfpi-franchise-ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# Field unwrapping / coercion
# --------------------------------------------------------------------------- #

def unwrap(value: Any) -> Any:
    """Solr returns most user fields as single-element arrays. Pop the
    element if so; pass scalars through; return None for empty arrays."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def coerce_text(value: Any) -> str | None:
    v = unwrap(value)
    if v is None:
        return None
    s = str(v)
    if not s.strip():
        return None
    # Formerly_Known_As ships a literal NUL byte when unset; treat as null.
    if all(ord(c) <= 0x20 or c == "\x00" for c in s):
        return None
    # Postgres rejects U+0000 in text and jsonb — scrub.
    return s.replace("\x00", "")


def scrub_nul_bytes(obj: Any) -> Any:
    """Recursively replace U+0000 in strings — Postgres jsonb cannot store
    NUL bytes. The DFPI source ships them in Formerly_Known_As when unset."""
    if isinstance(obj, str):
        return obj.replace("\x00", "") if "\x00" in obj else obj
    if isinstance(obj, list):
        return [scrub_nul_bytes(x) for x in obj]
    if isinstance(obj, dict):
        return {k: scrub_nul_bytes(v) for k, v in obj.items()}
    return obj


def coerce_yes_no_bool(value: Any) -> bool | None:
    v = unwrap(value)
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("yes", "y", "true", "t", "1"):
        return True
    if s in ("no", "n", "false", "f", "0"):
        return False
    return None


def coerce_solr_bool(value: Any) -> bool | None:
    v = unwrap(value)
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("true", "t", "1"):
        return True
    if s in ("false", "f", "0"):
        return False
    return None


def coerce_int(value: Any) -> int | None:
    v = unwrap(value)
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def coerce_tstz(value: Any) -> datetime | None:
    """ISO 8601 timestamp -> datetime. exif_date is the only proper datetime
    in the source — the *_date_text fields are M/D/YYYY strings and stay
    as text (we don't parse them at ingest)."""
    v = unwrap(value)
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    cleaned = s.replace("Z", "+00:00") if s.endswith("Z") else s
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Solr/HTTP helpers
# --------------------------------------------------------------------------- #

@dataclass
class SolrToken:
    value: str
    fetched_at: datetime


def fetch_solr_token(client: httpx.Client) -> SolrToken:
    """Re-scrape the bearer token from the public DFPI page."""
    log.info("scraping Solr token from %s", DFPI_PAGE_URL)
    resp = client.get(DFPI_PAGE_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    matches = TOKEN_RE.findall(resp.text)
    if not matches:
        raise RuntimeError("no select_auth_token found in DFPI page HTML")
    token = matches[0]
    if len(matches) > 1:
        log.info("token scraped (prod=%s..., %d candidates seen)", token[:10], len(matches))
    return SolrToken(value=token, fetched_at=datetime.now(timezone.utc))


def solr_get(
    client: httpx.Client,
    token: SolrToken,
    *,
    rows: int,
    start: int = 0,
    fl: str | None = None,
) -> tuple[dict[str, Any], int]:
    params: list[tuple[str, str]] = [
        ("q", "*:*"),
        *[("fq", v) for v in SOLR_FQ],
        ("rows", str(rows)),
        ("start", str(start)),
        ("wt", "json"),
        ("sort", "id asc"),
        ("fl", fl or SOLR_FL),
    ]
    headers = {
        "Authorization": f"Token {token.value}",
        "Origin": "https://dfpi.ca.gov",
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = client.get(SOLR_BASE, params=params, headers=headers, timeout=60)
            if resp.status_code == 401 or resp.status_code == 403:
                # Auth failure — caller decides whether to refresh token.
                raise SolrAuthError(f"Solr auth failed: HTTP {resp.status_code}")
            if resp.status_code in RETRY_STATUSES:
                wait = min(2 ** attempt, 30)
                log.warning(
                    "Solr %s — backing off %ss (attempt %d/%d)",
                    resp.status_code, wait, attempt + 1, MAX_RETRIES,
                )
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json(), len(resp.content)
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            last_exc = exc
            wait = min(2 ** attempt, 30)
            log.warning("Solr request error: %s — retry in %ss", exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"Solr request failed after {MAX_RETRIES} attempts: {last_exc}")


class SolrAuthError(Exception):
    """Raised on 401/403 from Solr so callers can refresh the token."""


# --------------------------------------------------------------------------- #
# Per-doc -> table rows
# --------------------------------------------------------------------------- #

@dataclass
class FilingRow:
    filing_guid: str
    app_id: str | None
    app_id_kind: str | None
    org_legal_name: str
    formerly_known_as: str | None
    application_notice_type: str | None
    filing_type: str | None
    sub_filing_category: str | None
    app_notice_status: str | None
    app_notice_sub_status: str | None
    enf_cases: bool | None
    public_actions: bool | None
    date_filed_text: str | None
    effective_date_text: str | None
    status_date_text: str | None
    exif_date: datetime | None
    solr_version: int | None
    imported_from_intermediate_db: bool | None
    uri: str | None
    raw_payload: dict[str, Any]


@dataclass
class DocumentRow:
    document_id: str
    filing_guid: str
    document_title: str | None
    file_link: str
    ord: int


def parse_app_id_kind(app_id: str | None) -> str | None:
    if not app_id:
        return None
    if "-" not in app_id:
        return None
    return app_id.split("-", 1)[0].lower() or None


def parse_documents(raw: Any, filing_guid: str) -> list[DocumentRow]:
    """Solr ships Documents as a single-element array containing a JSON-
    stringified list of {DocumentID, DocumentTitle, FileLink}."""
    inner = unwrap(raw)
    if inner is None:
        return []
    if isinstance(inner, str):
        try:
            decoded = json.loads(inner)
        except json.JSONDecodeError:
            log.warning("filing %s: Documents not parseable JSON — skipping", filing_guid)
            return []
    elif isinstance(inner, list):
        decoded = inner
    else:
        return []

    rows: list[DocumentRow] = []
    seen_ids: set[str] = set()
    for idx, entry in enumerate(decoded):
        if not isinstance(entry, dict):
            continue
        doc_id = entry.get("DocumentID")
        file_link = entry.get("FileLink")
        if not doc_id or not file_link:
            continue
        doc_id = str(doc_id)
        if doc_id in seen_ids:
            # Defensive — observed in the wild but rare; keep first.
            continue
        seen_ids.add(doc_id)
        rows.append(DocumentRow(
            document_id=doc_id,
            filing_guid=filing_guid,
            document_title=entry.get("DocumentTitle"),
            file_link=str(file_link),
            ord=idx,
        ))
    return rows


def parse_doc(raw: dict[str, Any]) -> tuple[FilingRow, list[DocumentRow]]:
    filing_guid = raw.get("id")
    if not filing_guid:
        raise ValueError(f"Solr doc missing id: {raw!r}")
    org_name = coerce_text(raw.get("Org_Legal_Name"))
    if not org_name:
        raise ValueError(f"Solr doc {filing_guid} missing Org_Legal_Name")
    app_id = coerce_text(raw.get("App_ID"))
    filing = FilingRow(
        filing_guid=filing_guid,
        app_id=app_id,
        app_id_kind=parse_app_id_kind(app_id),
        org_legal_name=org_name,
        formerly_known_as=coerce_text(raw.get("Formerly_Known_As")),
        application_notice_type=coerce_text(raw.get("Application_Notice_Type")),
        filing_type=coerce_text(raw.get("Filing_Type")),
        sub_filing_category=coerce_text(raw.get("subFilingCategory_s")),
        app_notice_status=coerce_text(raw.get("app_notice_status_s")),
        app_notice_sub_status=coerce_text(raw.get("app_notice_sub_status_s")),
        enf_cases=coerce_yes_no_bool(raw.get("ENFCASES")),
        public_actions=coerce_yes_no_bool(raw.get("PublicActions")),
        date_filed_text=coerce_text(raw.get("Date_Filed")),
        effective_date_text=coerce_text(raw.get("Effective_Date")),
        status_date_text=coerce_text(raw.get("Status_Date")),
        exif_date=coerce_tstz(raw.get("exif_date")),
        solr_version=coerce_int(raw.get("_version_")),
        imported_from_intermediate_db=coerce_solr_bool(raw.get("ImportedFromIntermediateDB")),
        uri=coerce_text(raw.get("uri")),
        raw_payload=scrub_nul_bytes(raw),
    )
    docs = parse_documents(raw.get("Documents"), filing_guid)
    return filing, docs


# --------------------------------------------------------------------------- #
# DB I/O — staging table + COPY + upsert per page
# --------------------------------------------------------------------------- #

FILING_COPY_COLS = (
    "filing_guid",
    "app_id",
    "app_id_kind",
    "org_legal_name",
    "formerly_known_as",
    "application_notice_type",
    "filing_type",
    "sub_filing_category",
    "app_notice_status",
    "app_notice_sub_status",
    "enf_cases",
    "public_actions",
    "date_filed_text",
    "effective_date_text",
    "status_date_text",
    "exif_date",
    "solr_version",
    "imported_from_intermediate_db",
    "uri",
    "raw_payload",
)

DOCUMENT_COPY_COLS = (
    "document_id",
    "filing_guid",
    "document_title",
    "file_link",
    "ord",
)


def ensure_stage_tables(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TEMP TABLE IF NOT EXISTS _stage_dfpi_filings (
              filing_guid                       text PRIMARY KEY,
              app_id                            text,
              app_id_kind                       text,
              org_legal_name                    text,
              formerly_known_as                 text,
              application_notice_type           text,
              filing_type                       text,
              sub_filing_category               text,
              app_notice_status                 text,
              app_notice_sub_status             text,
              enf_cases                         boolean,
              public_actions                    boolean,
              date_filed_text                   text,
              effective_date_text               text,
              status_date_text                  text,
              exif_date                         timestamptz,
              solr_version                      bigint,
              imported_from_intermediate_db     boolean,
              uri                               text,
              raw_payload                       jsonb
            );
            CREATE TEMP TABLE IF NOT EXISTS _stage_dfpi_documents (
              document_id      text PRIMARY KEY,
              filing_guid      text,
              document_title   text,
              file_link        text,
              ord              int
            );
        """)
    conn.commit()


def truncate_stage(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute("TRUNCATE _stage_dfpi_filings; TRUNCATE _stage_dfpi_documents;")


def filing_to_tuple(f: FilingRow) -> tuple:
    return (
        f.filing_guid, f.app_id, f.app_id_kind, f.org_legal_name,
        f.formerly_known_as, f.application_notice_type, f.filing_type,
        f.sub_filing_category, f.app_notice_status, f.app_notice_sub_status,
        f.enf_cases, f.public_actions,
        f.date_filed_text, f.effective_date_text, f.status_date_text,
        f.exif_date, f.solr_version, f.imported_from_intermediate_db,
        f.uri, Jsonb(f.raw_payload),
    )


def document_to_tuple(d: DocumentRow) -> tuple:
    return (d.document_id, d.filing_guid, d.document_title, d.file_link, d.ord)


def upsert_page(
    conn: psycopg.Connection,
    filings: list[FilingRow],
    documents: list[DocumentRow],
) -> tuple[int, int, int, int]:
    """Returns (filings_inserted, filings_updated, docs_inserted, docs_updated)."""
    if not filings:
        return 0, 0, 0, 0
    with conn.cursor() as cur:
        truncate_stage(conn)
        # Stage filings via COPY
        with cur.copy(
            f"COPY _stage_dfpi_filings ({', '.join(FILING_COPY_COLS)}) FROM STDIN"
        ) as copy:
            for f in filings:
                copy.write_row(filing_to_tuple(f))
        # Stage documents via COPY (deduped — child PK collisions drop)
        seen: set[str] = set()
        unique_docs = []
        for d in documents:
            if d.document_id in seen:
                continue
            seen.add(d.document_id)
            unique_docs.append(d)
        if unique_docs:
            with cur.copy(
                f"COPY _stage_dfpi_documents ({', '.join(DOCUMENT_COPY_COLS)}) FROM STDIN"
            ) as copy:
                for d in unique_docs:
                    copy.write_row(document_to_tuple(d))

        # Upsert filings (parent first — documents FK depends on filings).
        cur.execute(f"""
            WITH ins AS (
              INSERT INTO entities.dfpi_franchise_filings AS t (
                filing_guid, app_id, app_id_kind, org_legal_name,
                formerly_known_as, application_notice_type, filing_type,
                sub_filing_category, app_notice_status, app_notice_sub_status,
                enf_cases, public_actions,
                date_filed_text, effective_date_text, status_date_text,
                exif_date, solr_version, imported_from_intermediate_db,
                uri, raw_payload
              )
              SELECT {', '.join(FILING_COPY_COLS)} FROM _stage_dfpi_filings
              ON CONFLICT (filing_guid) DO UPDATE SET
                app_id                        = EXCLUDED.app_id,
                app_id_kind                   = EXCLUDED.app_id_kind,
                org_legal_name                = EXCLUDED.org_legal_name,
                formerly_known_as             = EXCLUDED.formerly_known_as,
                application_notice_type       = EXCLUDED.application_notice_type,
                filing_type                   = EXCLUDED.filing_type,
                sub_filing_category           = EXCLUDED.sub_filing_category,
                app_notice_status             = EXCLUDED.app_notice_status,
                app_notice_sub_status         = EXCLUDED.app_notice_sub_status,
                enf_cases                     = EXCLUDED.enf_cases,
                public_actions                = EXCLUDED.public_actions,
                date_filed_text               = EXCLUDED.date_filed_text,
                effective_date_text           = EXCLUDED.effective_date_text,
                status_date_text              = EXCLUDED.status_date_text,
                exif_date                     = EXCLUDED.exif_date,
                solr_version                  = EXCLUDED.solr_version,
                imported_from_intermediate_db = EXCLUDED.imported_from_intermediate_db,
                uri                           = EXCLUDED.uri,
                raw_payload                   = EXCLUDED.raw_payload,
                updated_at                    = now()
              RETURNING (xmax = 0) AS inserted
            )
            SELECT COUNT(*) FILTER (WHERE inserted) AS ins,
                   COUNT(*) FILTER (WHERE NOT inserted) AS upd FROM ins;
        """)
        f_ins, f_upd = cur.fetchone()

        # Upsert documents — and CRITICALLY, delete sibling docs that no
        # longer appear in the latest Solr response for these filings,
        # so has_fdd_document stays accurate. Keep within the transaction.
        if unique_docs:
            cur.execute(f"""
                WITH ins AS (
                  INSERT INTO entities.dfpi_franchise_filing_documents AS t (
                    document_id, filing_guid, document_title, file_link, ord
                  )
                  SELECT {', '.join(DOCUMENT_COPY_COLS)} FROM _stage_dfpi_documents
                  ON CONFLICT (document_id) DO UPDATE SET
                    filing_guid    = EXCLUDED.filing_guid,
                    document_title = EXCLUDED.document_title,
                    file_link      = EXCLUDED.file_link,
                    ord            = EXCLUDED.ord,
                    updated_at     = now()
                  RETURNING (xmax = 0) AS inserted
                )
                SELECT COUNT(*) FILTER (WHERE inserted),
                       COUNT(*) FILTER (WHERE NOT inserted) FROM ins;
            """)
            d_ins, d_upd = cur.fetchone()
        else:
            d_ins, d_upd = 0, 0

        # Drop documents that vanished from Solr for filings in THIS page.
        # Use the staged filing GUIDs as the scope so we don't touch unrelated rows.
        cur.execute("""
            DELETE FROM entities.dfpi_franchise_filing_documents d
            WHERE d.filing_guid IN (SELECT filing_guid FROM _stage_dfpi_filings)
              AND d.document_id NOT IN (
                SELECT document_id FROM _stage_dfpi_documents
              );
        """)

    conn.commit()
    return int(f_ins), int(f_upd), int(d_ins), int(d_upd)


# --------------------------------------------------------------------------- #
# Audit helpers
# --------------------------------------------------------------------------- #

def insert_run_row(
    conn: psycopg.Connection,
    *,
    page_size: int,
    num_found: int | None,
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.dfpi_ingest_runs (
              status, source_url, solr_collection, page_size, num_found_at_run_start
            ) VALUES ('running', %s, %s, %s, %s)
            RETURNING id;
            """,
            (SOLR_BASE, SOLR_COLLECTION, page_size, num_found),
        )
        run_id = cur.fetchone()[0]
    conn.commit()
    return str(run_id)


def finalize_run_row(
    conn: psycopg.Connection,
    run_id: str,
    *,
    status: str,
    pages_fetched: int,
    rows_upserted: int,
    documents_persisted: int,
    bytes_downloaded: int,
    token_refreshes: int,
    num_found_at_run_end: int | None,
    started_monotonic: float,
    error_message: str | None,
    error_class: str | None,
    notes: dict[str, Any] | None,
) -> None:
    duration = round(time.monotonic() - started_monotonic, 3)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ops.dfpi_ingest_runs SET
              status = %s,
              pages_fetched = %s,
              rows_upserted = %s,
              documents_persisted = %s,
              bytes_downloaded = %s,
              token_refreshes = %s,
              num_found_at_run_end = %s,
              finished_at = now(),
              duration_seconds = %s,
              error_message = %s,
              error_class = %s,
              notes = %s
            WHERE id = %s;
            """,
            (
                status, pages_fetched, rows_upserted, documents_persisted,
                bytes_downloaded, token_refreshes, num_found_at_run_end,
                duration, error_message, error_class,
                Jsonb(notes) if notes else None, run_id,
            ),
        )
    conn.commit()


def get_prior_num_found(conn: psycopg.Connection) -> int | None:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT num_found_at_run_end
              FROM ops.dfpi_ingest_runs
             WHERE status = 'completed'
             ORDER BY started_at DESC
             LIMIT 1
        """)
        row = cur.fetchone()
    return row[0] if row else None


def write_no_change_run(
    conn: psycopg.Connection,
    *,
    page_size: int,
    num_found: int,
    prior_num_found: int,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.dfpi_ingest_runs (
              status, source_url, solr_collection, page_size,
              num_found_at_run_start, num_found_at_run_end,
              started_at, finished_at, duration_seconds, notes
            ) VALUES (
              'no_change', %s, %s, %s, %s, %s, now(), now(), 0, %s
            );
            """,
            (
                SOLR_BASE, SOLR_COLLECTION, page_size, num_found, num_found,
                Jsonb({
                    "reason": "numFound unchanged since prior successful run",
                    "prior_num_found": prior_num_found,
                }),
            ),
        )
    conn.commit()


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #

def _database_url() -> str:
    # Prefer DIRECT for the ingest write path (long-running connection).
    url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ.get("DEX_DB_URL_POOLED")
    if not url:
        raise RuntimeError(
            "neither DEX_DB_URL_DIRECT nor DEX_DB_URL_POOLED is set — "
            "are you running under `doppler run`?"
        )
    return url


def run_ingest(
    *,
    page_size: int,
    page_sleep: float,
    max_pages: int | None,
    skip_if_unchanged: bool,
    dry_run: bool,
) -> int:
    started_monotonic = time.monotonic()
    log.info("starting DFPI franchise ingest (page_size=%s, dry_run=%s)", page_size, dry_run)

    with httpx.Client(follow_redirects=True) as client:
        token = fetch_solr_token(client)
        token_refreshes = 0

        # Probe to capture num_found at run start
        probe, _ = solr_get(client, token, rows=1, start=0)
        num_found_start = probe["response"]["numFound"]
        log.info("Solr numFound at run start: %s", num_found_start)

        if dry_run:
            log.info("DRY RUN — fetching first %d page(s), no DB writes", max_pages or 1)
            offset = 0
            total = 0
            for page_idx in range(max_pages or 1):
                page, nbytes = solr_get(client, token, rows=page_size, start=offset)
                docs = page["response"]["docs"]
                total += len(docs)
                log.info("  page %d: %d docs, %d bytes", page_idx, len(docs), nbytes)
                if docs:
                    sample_filing, sample_docs = parse_doc(docs[0])
                    log.info("  sample filing: guid=%s name=%r docs=%d",
                             sample_filing.filing_guid,
                             sample_filing.org_legal_name,
                             len(sample_docs))
                if len(docs) < page_size:
                    break
                offset += page_size
                time.sleep(page_sleep)
            log.info("DRY RUN done — fetched %d total docs", total)
            return 0

        with psycopg.connect(_database_url()) as conn:
            prior_num_found = get_prior_num_found(conn)
            if (
                skip_if_unchanged
                and prior_num_found is not None
                and num_found_start == prior_num_found
            ):
                log.info(
                    "numFound unchanged since prior successful run (%s) — recording no_change",
                    prior_num_found,
                )
                write_no_change_run(
                    conn,
                    page_size=page_size,
                    num_found=num_found_start,
                    prior_num_found=prior_num_found,
                )
                return 0

            run_id = insert_run_row(conn, page_size=page_size, num_found=num_found_start)
            log.info("audit run id=%s", run_id)

            ensure_stage_tables(conn)

            offset = 0
            pages_fetched = 0
            total_filings_inserted = 0
            total_filings_updated = 0
            total_documents_inserted = 0
            total_documents_updated = 0
            total_bytes = 0
            error_message: str | None = None
            error_class: str | None = None
            status = "completed"

            try:
                while True:
                    if max_pages is not None and pages_fetched >= max_pages:
                        log.info("max_pages=%d reached — stopping", max_pages)
                        break

                    try:
                        page, nbytes = solr_get(
                            client, token, rows=page_size, start=offset,
                        )
                    except SolrAuthError as exc:
                        log.warning("auth error: %s — refreshing token", exc)
                        token = fetch_solr_token(client)
                        token_refreshes += 1
                        page, nbytes = solr_get(
                            client, token, rows=page_size, start=offset,
                        )

                    docs = page["response"]["docs"]
                    total_bytes += nbytes
                    pages_fetched += 1

                    filings: list[FilingRow] = []
                    documents: list[DocumentRow] = []
                    for raw in docs:
                        try:
                            f, ds = parse_doc(raw)
                        except Exception as exc:
                            log.warning(
                                "parse failure on doc %s: %s — skipping",
                                raw.get("id"), exc,
                            )
                            continue
                        filings.append(f)
                        documents.extend(ds)

                    f_ins, f_upd, d_ins, d_upd = upsert_page(conn, filings, documents)
                    total_filings_inserted += f_ins
                    total_filings_updated += f_upd
                    total_documents_inserted += d_ins
                    total_documents_updated += d_upd

                    log.info(
                        "page %d (start=%d): %d docs, filings ins=%d upd=%d, docs ins=%d upd=%d, %.1f KB",
                        pages_fetched, offset, len(docs), f_ins, f_upd,
                        d_ins, d_upd, nbytes / 1024,
                    )

                    if len(docs) < page_size:
                        break
                    offset += page_size
                    time.sleep(page_sleep)

                # End-of-run probe (detect mid-run publisher delta).
                end_probe, _ = solr_get(client, token, rows=1, start=0)
                num_found_end = end_probe["response"]["numFound"]
                if num_found_end != num_found_start:
                    log.warning(
                        "numFound shifted mid-run: start=%s end=%s (publisher updated during ingest)",
                        num_found_start, num_found_end,
                    )

            except Exception as exc:
                status = "failed"
                error_message = str(exc)
                if isinstance(exc, SolrAuthError):
                    error_class = "auth_failure"
                elif isinstance(exc, httpx.HTTPError):
                    error_class = "download_failure"
                elif isinstance(exc, psycopg.Error):
                    error_class = "db_failure"
                else:
                    error_class = "unknown"
                log.exception("ingest failed: %s", exc)
                # Roll back any aborted transaction so finalize_run_row can write.
                try:
                    conn.rollback()
                except Exception:
                    pass
                num_found_end = num_found_start
            else:
                num_found_end = end_probe["response"]["numFound"]

            finalize_run_row(
                conn, run_id,
                status=status,
                pages_fetched=pages_fetched,
                rows_upserted=total_filings_inserted + total_filings_updated,
                documents_persisted=total_documents_inserted + total_documents_updated,
                bytes_downloaded=total_bytes,
                token_refreshes=token_refreshes,
                num_found_at_run_end=num_found_end,
                started_monotonic=started_monotonic,
                error_message=error_message,
                error_class=error_class,
                notes={
                    "filings_inserted": total_filings_inserted,
                    "filings_updated": total_filings_updated,
                    "documents_inserted": total_documents_inserted,
                    "documents_updated": total_documents_updated,
                },
            )

            if status != "completed":
                return 1
            log.info(
                "DONE — pages=%d filings_ins=%d filings_upd=%d docs_ins=%d docs_upd=%d bytes=%d",
                pages_fetched, total_filings_inserted, total_filings_updated,
                total_documents_inserted, total_documents_updated, total_bytes,
            )
            return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    p.add_argument("--page-sleep", type=float, default=DEFAULT_PAGE_SLEEP)
    p.add_argument("--max-pages", type=int, default=None)
    p.add_argument(
        "--skip-if-unchanged",
        action="store_true",
        help="No-op if Solr numFound has not advanced since prior successful run",
    )
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    return run_ingest(
        page_size=args.page_size,
        page_sleep=args.page_sleep,
        max_pages=args.max_pages,
        skip_if_unchanged=args.skip_if_unchanged,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())

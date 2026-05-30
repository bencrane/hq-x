#!/usr/bin/env python3
"""DFPI Franchise Filings → R2 snapshot ingest (no RisingWave wiring).

Mirrors the upstream California DFPI public franchise filing index (Solr) into
R2 as ZSTD-compressed Parquet, partitioned by snapshot date. Lifts the
HTTP / Solr / token-scrape layer from scripts/run_dfpi_franchise_ingest.py;
adds Parquet writes, R2 upload, snapshot-date partitioning, and the Python
ports of entities.normalize_franchise_name / classify_dfpi_document_kind.

Three Parquet objects per snapshot:

  dfpi/snapshot=YYYY-MM-DD/franchise_filings.parquet           (~53K rows)
  dfpi/snapshot=YYYY-MM-DD/franchise_filing_documents.parquet  (~237K rows)
  dfpi/snapshot=YYYY-MM-DD/franchisors.parquet                 (~5K rows)

Audit ledger: ops.dfpi_r2_ingest_runs (one row per (snapshot_date, table_name)).

NO RisingWave wiring — that's a follow-up directive.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_dfpi_franchise_r2_snapshot_ingest.py
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_dfpi_franchise_r2_snapshot_ingest.py --dry-run --max-pages 1
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_dfpi_franchise_r2_snapshot_ingest.py --r2-prefix-override 'dfpi/_smoke/2026-05-08/'
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import boto3
import duckdb
import httpx
import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
from psycopg.types.json import Jsonb

from scripts._lib.dfpi_normalize import (
    classify_document_kind,
    normalize_franchise_name,
)
from scripts.run_dfpi_franchise_ingest import (
    DFPI_PAGE_URL,
    DocumentRow,
    FilingRow,
    SOLR_BASE,
    SOLR_COLLECTION,
    SolrAuthError,
    SolrToken,
    fetch_solr_token,
    parse_doc,
    solr_get,
)


R2_BUCKET = "dex-raw-landing-zone"
DEFAULT_PAGE_SIZE = 1000
DEFAULT_PAGE_SLEEP = 0.5

TABLE_FILINGS = "franchise_filings"
TABLE_DOCUMENTS = "franchise_filing_documents"
TABLE_FRANCHISORS = "franchisors"
ALL_TABLES: tuple[str, ...] = (TABLE_FILINGS, TABLE_DOCUMENTS, TABLE_FRANCHISORS)


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("dfpi-r2-snapshot-ingest")


log = _logger()


# --------------------------------------------------------------------------- #
# Env helpers
# --------------------------------------------------------------------------- #


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


def _database_url() -> str:
    url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ.get("DEX_DB_URL_POOLED")
    if not url:
        raise RuntimeError(
            "neither DEX_DB_URL_DIRECT nor DEX_DB_URL_POOLED is set — "
            "are you running under `doppler run`?"
        )
    return url


# --------------------------------------------------------------------------- #
# Per-(snapshot_date, table_name) audit-row helpers
# --------------------------------------------------------------------------- #


@dataclass
class RunRow:
    run_id: str
    snapshot_date: date
    table_name: str
    started_monotonic: float


def insert_run_row(
    conn: psycopg.Connection,
    *,
    snapshot_date: date,
    table_name: str,
    pages_fetched: int | None,
    num_found_at_run_start: int | None,
    bytes_downloaded: int | None,
    token_refreshes: int,
) -> RunRow:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.dfpi_r2_ingest_runs (
                snapshot_date, table_name, status, source_url, solr_collection,
                pages_fetched, num_found_at_run_start,
                bytes_downloaded, token_refreshes
            ) VALUES (%s, %s, 'running', %s, %s, %s, %s, %s, %s)
            RETURNING id;
            """,
            (
                snapshot_date, table_name, SOLR_BASE, SOLR_COLLECTION,
                pages_fetched, num_found_at_run_start,
                bytes_downloaded, token_refreshes,
            ),
        )
        row_id = cur.fetchone()[0]
    conn.commit()
    return RunRow(
        run_id=str(row_id),
        snapshot_date=snapshot_date,
        table_name=table_name,
        started_monotonic=time.monotonic(),
    )


def finalize_run_row(
    conn: psycopg.Connection,
    rr: RunRow,
    *,
    status: str,
    parquet_bytes_written: int | None,
    parquet_row_count: int | None,
    r2_bucket: str | None,
    r2_prefix: str | None,
    r2_key: str | None,
    r2_object_bytes: int | None,
    num_found_at_run_end: int | None,
    error_message: str | None,
    error_class: str | None,
    notes: dict[str, Any] | None,
) -> None:
    duration = round(time.monotonic() - rr.started_monotonic, 3)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ops.dfpi_r2_ingest_runs SET
                status = %s,
                parquet_bytes_written = %s, parquet_row_count = %s,
                r2_bucket = %s, r2_prefix = %s, r2_key = %s, r2_object_bytes = %s,
                num_found_at_run_end = %s,
                finished_at = now(), duration_seconds = %s,
                error_message = %s, error_class = %s, notes = %s
              WHERE id = %s;
            """,
            (
                status,
                parquet_bytes_written, parquet_row_count,
                r2_bucket, r2_prefix, r2_key, r2_object_bytes,
                num_found_at_run_end,
                duration, error_message, error_class,
                Jsonb(notes) if notes else None, rr.run_id,
            ),
        )
    conn.commit()


# --------------------------------------------------------------------------- #
# Solr → in-memory accumulator
# --------------------------------------------------------------------------- #


@dataclass
class SolrPull:
    filings: list[FilingRow]
    documents: list[DocumentRow]
    raw_docs: list[dict[str, Any]]
    pages_fetched: int
    bytes_downloaded: int
    token_refreshes: int
    num_found_at_run_start: int
    num_found_at_run_end: int


def pull_solr(
    *,
    page_size: int,
    page_sleep: float,
    max_pages: int | None,
) -> SolrPull:
    filings: list[FilingRow] = []
    documents: list[DocumentRow] = []
    raw_docs: list[dict[str, Any]] = []
    pages_fetched = 0
    bytes_downloaded = 0
    token_refreshes = 0

    with httpx.Client(follow_redirects=True) as client:
        token: SolrToken = fetch_solr_token(client)

        # Probe — capture numFound at run start.
        probe, _ = solr_get(client, token, rows=1, start=0)
        num_found_start = probe["response"]["numFound"]
        log.info("Solr numFound at run start: %s", num_found_start)

        offset = 0
        while True:
            if max_pages is not None and pages_fetched >= max_pages:
                log.info("max_pages=%d reached — stopping pagination", max_pages)
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
            bytes_downloaded += nbytes
            pages_fetched += 1

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
                raw_docs.append(raw)

            log.info(
                "page %d (start=%d): %d docs, %.1f KB",
                pages_fetched, offset, len(docs), nbytes / 1024,
            )

            if len(docs) < page_size:
                break
            offset += page_size
            time.sleep(page_sleep)

        # End-of-run probe.
        end_probe, _ = solr_get(client, token, rows=1, start=0)
        num_found_end = end_probe["response"]["numFound"]
        if num_found_end != num_found_start:
            log.warning(
                "numFound shifted mid-run: start=%s end=%s",
                num_found_start, num_found_end,
            )

    return SolrPull(
        filings=filings,
        documents=documents,
        raw_docs=raw_docs,
        pages_fetched=pages_fetched,
        bytes_downloaded=bytes_downloaded,
        token_refreshes=token_refreshes,
        num_found_at_run_start=num_found_start,
        num_found_at_run_end=num_found_end,
    )


# --------------------------------------------------------------------------- #
# Parquet writers
# --------------------------------------------------------------------------- #


def _file_name_from_link(link: str | None) -> str | None:
    if not link:
        return None
    try:
        parsed = urlparse(link)
        last = parsed.path.rsplit("/", 1)[-1] if parsed.path else ""
        return unquote(last) or None
    except Exception:
        return None


def _file_extension_from_name(name: str | None) -> str | None:
    if not name:
        return None
    if "." not in name:
        return None
    ext = name.rsplit(".", 1)[-1].lower()
    return ext or None


def _exif_to_pyarrow(value: datetime | None) -> int | None:
    """pyarrow timestamp('us', tz='UTC') wants microseconds since epoch."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1_000_000)


FILINGS_SCHEMA = pa.schema([
    ("filing_guid",                       pa.string()),
    ("app_id",                            pa.string()),
    ("app_id_kind",                       pa.string()),
    ("org_legal_name",                    pa.string()),
    ("org_legal_name_normalized",         pa.string()),
    ("formerly_known_as",                 pa.string()),
    ("application_notice_type",           pa.string()),
    ("filing_type",                       pa.string()),
    ("sub_filing_category",               pa.string()),
    ("app_notice_status",                 pa.string()),
    ("app_notice_sub_status",             pa.string()),
    ("enf_cases",                         pa.bool_()),
    ("public_actions",                    pa.bool_()),
    ("date_filed_text",                   pa.string()),
    ("effective_date_text",               pa.string()),
    ("status_date_text",                  pa.string()),
    ("exif_date",                         pa.timestamp("us", tz="UTC")),
    ("solr_version",                      pa.int64()),
    ("imported_from_intermediate_db",     pa.bool_()),
    ("uri",                               pa.string()),
    ("has_fdd_document",                  pa.bool_()),
    ("raw_solr_doc",                      pa.string()),
    ("dfpi_snapshot_date",                pa.date32()),
])


DOCUMENTS_SCHEMA = pa.schema([
    ("document_id",                       pa.string()),
    ("filing_guid",                       pa.string()),
    ("ord",                               pa.int32()),
    ("document_title",                    pa.string()),
    ("document_kind",                     pa.string()),
    ("file_link",                         pa.string()),
    ("file_name",                         pa.string()),
    ("file_extension",                    pa.string()),
    ("dfpi_snapshot_date",                pa.date32()),
])


def write_filings_parquet(
    filings: list[FilingRow],
    raw_docs: list[dict[str, Any]],
    documents: list[DocumentRow],
    *,
    snapshot_date: date,
    out_path: Path,
) -> tuple[int, int]:
    """Write franchise_filings.parquet. Returns (rows_written, bytes_written)."""

    # Compute has_fdd_document from documents in a single pass.
    has_fdd: dict[str, bool] = {}
    for d in documents:
        if d.document_kind_cached == "fdd":
            has_fdd[d.filing_guid] = True

    # Map raw_docs by filing_guid for the raw_solr_doc column. parse_doc
    # already validated f.filing_guid == raw['id'], so we can index by that.
    raw_by_guid: dict[str, dict[str, Any]] = {}
    for raw in raw_docs:
        guid = raw.get("id")
        if guid:
            raw_by_guid[guid] = raw

    cols: dict[str, list[Any]] = {name: [] for name in FILINGS_SCHEMA.names}
    for f in filings:
        cols["filing_guid"].append(f.filing_guid)
        cols["app_id"].append(f.app_id)
        cols["app_id_kind"].append(f.app_id_kind)
        cols["org_legal_name"].append(f.org_legal_name)
        cols["org_legal_name_normalized"].append(
            normalize_franchise_name(f.org_legal_name)
        )
        cols["formerly_known_as"].append(f.formerly_known_as)
        cols["application_notice_type"].append(f.application_notice_type)
        cols["filing_type"].append(f.filing_type)
        cols["sub_filing_category"].append(f.sub_filing_category)
        cols["app_notice_status"].append(f.app_notice_status)
        cols["app_notice_sub_status"].append(f.app_notice_sub_status)
        cols["enf_cases"].append(f.enf_cases)
        cols["public_actions"].append(f.public_actions)
        cols["date_filed_text"].append(f.date_filed_text)
        cols["effective_date_text"].append(f.effective_date_text)
        cols["status_date_text"].append(f.status_date_text)
        cols["exif_date"].append(_exif_to_pyarrow(f.exif_date))
        cols["solr_version"].append(f.solr_version)
        cols["imported_from_intermediate_db"].append(f.imported_from_intermediate_db)
        cols["uri"].append(f.uri)
        cols["has_fdd_document"].append(bool(has_fdd.get(f.filing_guid, False)))
        # raw_solr_doc as compact JSON string. f.raw_payload is the scrubbed copy.
        cols["raw_solr_doc"].append(
            json.dumps(f.raw_payload, separators=(",", ":"), default=str)
        )
        cols["dfpi_snapshot_date"].append(snapshot_date)

    # Cast exif_date to timestamp[us, UTC] via pa.array with explicit type.
    arrays: list[pa.Array] = []
    for field in FILINGS_SCHEMA:
        arr = pa.array(cols[field.name], type=field.type)
        arrays.append(arr)
    table = pa.Table.from_arrays(arrays, schema=FILINGS_SCHEMA)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        table, out_path, compression="zstd", compression_level=9,
    )
    return table.num_rows, out_path.stat().st_size


def write_documents_parquet(
    documents: list[DocumentRow],
    *,
    snapshot_date: date,
    out_path: Path,
) -> tuple[int, int]:
    cols: dict[str, list[Any]] = {name: [] for name in DOCUMENTS_SCHEMA.names}
    for d in documents:
        kind = d.document_kind_cached
        file_name = _file_name_from_link(d.file_link)
        cols["document_id"].append(d.document_id)
        cols["filing_guid"].append(d.filing_guid)
        cols["ord"].append(d.ord)
        cols["document_title"].append(d.document_title)
        cols["document_kind"].append(kind)
        cols["file_link"].append(d.file_link)
        cols["file_name"].append(file_name)
        cols["file_extension"].append(_file_extension_from_name(file_name))
        cols["dfpi_snapshot_date"].append(snapshot_date)

    arrays = [pa.array(cols[f.name], type=f.type) for f in DOCUMENTS_SCHEMA]
    table = pa.Table.from_arrays(arrays, schema=DOCUMENTS_SCHEMA)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        table, out_path, compression="zstd", compression_level=9,
    )
    return table.num_rows, out_path.stat().st_size


def write_franchisors_parquet(
    filings_parquet: Path,
    *,
    snapshot_date: date,
    out_path: Path,
) -> tuple[int, int]:
    """DuckDB GROUP BY on the local filings.parquet → franchisors.parquet."""
    con = duckdb.connect(":memory:")
    try:
        con.execute("PRAGMA threads=4;")
        con.execute(
            f"""
            COPY (
              SELECT
                org_legal_name_normalized       AS franchisor_name_normalized,
                COUNT(*)                        AS count_of_filings,
                CAST(MAX(exif_date) AS DATE)    AS latest_filing_date,
                CAST(MIN(exif_date) AS DATE)    AS earliest_filing_date,
                DATE '{snapshot_date.isoformat()}' AS dfpi_snapshot_date
              FROM read_parquet('{filings_parquet}')
              WHERE org_legal_name_normalized IS NOT NULL
                AND org_legal_name_normalized <> ''
              GROUP BY org_legal_name_normalized
            ) TO '{out_path}'
            (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 50000);
            """
        )
        rows = con.execute(
            f"SELECT count(*) FROM read_parquet('{out_path}');"
        ).fetchone()[0]
    finally:
        con.close()
    return int(rows), out_path.stat().st_size


# --------------------------------------------------------------------------- #
# R2 upload
# --------------------------------------------------------------------------- #


def upload_to_r2(
    parquet_path: Path,
    *,
    bucket: str,
    key: str,
    log_prefix: str,
) -> int:
    s3 = _r2_client()
    file_bytes = parquet_path.stat().st_size
    log.info(
        "%s uploading %s (%.1f MB) → s3://%s/%s",
        log_prefix, parquet_path, file_bytes / (1 << 20), bucket, key,
    )
    s3.upload_file(
        str(parquet_path), bucket, key,
        ExtraArgs={"ContentType": "application/x-parquet"},
    )
    log.info("%s upload done", log_prefix)
    return file_bytes


# --------------------------------------------------------------------------- #
# Inject document_kind onto DocumentRow at parse time.
#
# parse_doc returns DocumentRow with no kind cached; we need it both for
# computing has_fdd (filings) and for the documents Parquet column. Compute
# once on the dataclass via a setattr trick to avoid re-parsing.
# --------------------------------------------------------------------------- #


def _attach_document_kinds(documents: list[DocumentRow]) -> None:
    for d in documents:
        # Stamp a cached kind onto the existing DocumentRow instance. This is
        # a side-channel attribute, not on the imported dataclass def — both
        # call sites (write_filings_parquet, write_documents_parquet) read it.
        d.document_kind_cached = classify_document_kind(d.document_title)  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


@dataclass
class IngestArgs:
    page_size: int
    page_sleep: float
    max_pages: int | None
    dry_run: bool
    workdir: Path
    r2_prefix_override: str | None


def run_ingest(args: IngestArgs) -> int:
    snapshot_date = datetime.now(timezone.utc).date()
    snapshot_label = f"snapshot={snapshot_date.isoformat()}"
    r2_prefix = args.r2_prefix_override or f"dfpi/{snapshot_label}/"
    log.info("starting DFPI R2 snapshot ingest — %s (dry_run=%s)",
             snapshot_label, args.dry_run)

    pull = pull_solr(
        page_size=args.page_size,
        page_sleep=args.page_sleep,
        max_pages=args.max_pages,
    )
    log.info(
        "Solr pull complete — %d filings, %d documents, %d pages, %d bytes, "
        "%d token refreshes, numFound start=%s end=%s",
        len(pull.filings), len(pull.documents), pull.pages_fetched,
        pull.bytes_downloaded, pull.token_refreshes,
        pull.num_found_at_run_start, pull.num_found_at_run_end,
    )

    _attach_document_kinds(pull.documents)

    if args.dry_run:
        log.info("DRY RUN — sample filing[0]: %s", pull.filings[0] if pull.filings else "(none)")
        log.info("DRY RUN — exiting before Parquet write / R2 upload / DB writes")
        return 0

    filings_path = args.workdir / "franchise_filings.parquet"
    documents_path = args.workdir / "franchise_filing_documents.parquet"
    franchisors_path = args.workdir / "franchisors.parquet"

    rc = 0
    with psycopg.connect(_database_url()) as conn:
        # ---------------- filings ----------------
        rr_filings = insert_run_row(
            conn,
            snapshot_date=snapshot_date,
            table_name=TABLE_FILINGS,
            pages_fetched=pull.pages_fetched,
            num_found_at_run_start=pull.num_found_at_run_start,
            bytes_downloaded=pull.bytes_downloaded,
            token_refreshes=pull.token_refreshes,
        )
        try:
            log.info("[%s] writing %s", TABLE_FILINGS, filings_path)
            rows, parquet_bytes = write_filings_parquet(
                pull.filings, pull.raw_docs, pull.documents,
                snapshot_date=snapshot_date, out_path=filings_path,
            )
            r2_key = r2_prefix + filings_path.name
            uploaded = upload_to_r2(
                filings_path, bucket=R2_BUCKET, key=r2_key,
                log_prefix=f"[{TABLE_FILINGS}]",
            )
            finalize_run_row(
                conn, rr_filings, status="completed",
                parquet_bytes_written=parquet_bytes,
                parquet_row_count=rows,
                r2_bucket=R2_BUCKET, r2_prefix=r2_prefix, r2_key=r2_key,
                r2_object_bytes=uploaded,
                num_found_at_run_end=pull.num_found_at_run_end,
                error_message=None, error_class=None,
                notes={"max_pages": args.max_pages},
            )
            log.info("[%s] DONE rows=%d parquet=%.1f KB",
                     TABLE_FILINGS, rows, parquet_bytes / 1024)
        except Exception as exc:
            log.exception("[%s] failed", TABLE_FILINGS)
            finalize_run_row(
                conn, rr_filings, status="failed",
                parquet_bytes_written=None, parquet_row_count=None,
                r2_bucket=None, r2_prefix=None, r2_key=None, r2_object_bytes=None,
                num_found_at_run_end=pull.num_found_at_run_end,
                error_message=str(exc), error_class=_classify_error(exc),
                notes=None,
            )
            return 1

        # ---------------- documents ----------------
        rr_docs = insert_run_row(
            conn,
            snapshot_date=snapshot_date,
            table_name=TABLE_DOCUMENTS,
            pages_fetched=pull.pages_fetched,
            num_found_at_run_start=pull.num_found_at_run_start,
            bytes_downloaded=None,
            token_refreshes=0,
        )
        try:
            log.info("[%s] writing %s", TABLE_DOCUMENTS, documents_path)
            rows, parquet_bytes = write_documents_parquet(
                pull.documents,
                snapshot_date=snapshot_date, out_path=documents_path,
            )
            r2_key = r2_prefix + documents_path.name
            uploaded = upload_to_r2(
                documents_path, bucket=R2_BUCKET, key=r2_key,
                log_prefix=f"[{TABLE_DOCUMENTS}]",
            )
            finalize_run_row(
                conn, rr_docs, status="completed",
                parquet_bytes_written=parquet_bytes,
                parquet_row_count=rows,
                r2_bucket=R2_BUCKET, r2_prefix=r2_prefix, r2_key=r2_key,
                r2_object_bytes=uploaded,
                num_found_at_run_end=pull.num_found_at_run_end,
                error_message=None, error_class=None,
                notes={},
            )
            log.info("[%s] DONE rows=%d parquet=%.1f KB",
                     TABLE_DOCUMENTS, rows, parquet_bytes / 1024)
        except Exception as exc:
            log.exception("[%s] failed", TABLE_DOCUMENTS)
            finalize_run_row(
                conn, rr_docs, status="failed",
                parquet_bytes_written=None, parquet_row_count=None,
                r2_bucket=None, r2_prefix=None, r2_key=None, r2_object_bytes=None,
                num_found_at_run_end=pull.num_found_at_run_end,
                error_message=str(exc), error_class=_classify_error(exc),
                notes=None,
            )
            rc = 1

        # ---------------- franchisors ----------------
        rr_fr = insert_run_row(
            conn,
            snapshot_date=snapshot_date,
            table_name=TABLE_FRANCHISORS,
            pages_fetched=None,
            num_found_at_run_start=None,
            bytes_downloaded=None,
            token_refreshes=0,
        )
        try:
            log.info("[%s] writing %s (DuckDB GROUP BY on filings.parquet)",
                     TABLE_FRANCHISORS, franchisors_path)
            rows, parquet_bytes = write_franchisors_parquet(
                filings_path,
                snapshot_date=snapshot_date, out_path=franchisors_path,
            )
            r2_key = r2_prefix + franchisors_path.name
            uploaded = upload_to_r2(
                franchisors_path, bucket=R2_BUCKET, key=r2_key,
                log_prefix=f"[{TABLE_FRANCHISORS}]",
            )
            finalize_run_row(
                conn, rr_fr, status="completed",
                parquet_bytes_written=parquet_bytes,
                parquet_row_count=rows,
                r2_bucket=R2_BUCKET, r2_prefix=r2_prefix, r2_key=r2_key,
                r2_object_bytes=uploaded,
                num_found_at_run_end=None,
                error_message=None, error_class=None,
                notes={"derived_from": "franchise_filings.parquet"},
            )
            log.info("[%s] DONE rows=%d parquet=%.1f KB",
                     TABLE_FRANCHISORS, rows, parquet_bytes / 1024)
        except Exception as exc:
            log.exception("[%s] failed", TABLE_FRANCHISORS)
            finalize_run_row(
                conn, rr_fr, status="failed",
                parquet_bytes_written=None, parquet_row_count=None,
                r2_bucket=None, r2_prefix=None, r2_key=None, r2_object_bytes=None,
                num_found_at_run_end=None,
                error_message=str(exc), error_class=_classify_error(exc),
                notes=None,
            )
            rc = 1

    # Cleanup local Parquet artefacts.
    for p in (filings_path, documents_path, franchisors_path):
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass

    return rc


def _classify_error(exc: Exception) -> str:
    if isinstance(exc, SolrAuthError):
        return "auth_failure"
    if isinstance(exc, httpx.HTTPError):
        return "download_failure"
    if isinstance(exc, psycopg.Error):
        return "db_failure"
    if isinstance(exc, (TimeoutError, OSError)):
        return "timeout"
    return "unknown"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str] | None = None) -> IngestArgs:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    p.add_argument("--page-sleep", type=float, default=DEFAULT_PAGE_SLEEP)
    p.add_argument("--max-pages", type=int, default=None,
                   help="Cap pagination — useful for smoke tests")
    p.add_argument("--dry-run", action="store_true",
                   help="Pull Solr and parse, but write no Parquet / no R2 / no DB rows")
    p.add_argument("--workdir", default=None,
                   help="Local Parquet scratch dir (default: /tmp/dfpi_r2_snapshot)")
    p.add_argument("--r2-prefix-override", default=None,
                   help="Override the dfpi/snapshot=<date>/ prefix (e.g. for smoke runs)")
    a = p.parse_args(argv)
    workdir = Path(a.workdir or "/tmp/dfpi_r2_snapshot")
    workdir.mkdir(parents=True, exist_ok=True)
    return IngestArgs(
        page_size=a.page_size,
        page_sleep=a.page_sleep,
        max_pages=a.max_pages,
        dry_run=a.dry_run,
        workdir=workdir,
        r2_prefix_override=a.r2_prefix_override,
    )


def main(argv: list[str] | None = None) -> int:
    return run_ingest(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())

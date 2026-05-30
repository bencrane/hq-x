#!/usr/bin/env python3
"""Stream CMS Open Payments (Sunshine Act) bulk CSVs into ZSTD-Parquet on R2.

Output layout in `dex-raw-landing-zone`:
    cms-open-payments/year=<YYYY>/feed=<general|research>/part-NNNNN.parquet

Two feeds: General Payments (~14.7M rows/year) and Research Payments (~1.45M
rows/year). Source CSV URLs are discovered at runtime via the openpaymentsdata.cms.gov
DKAN metastore API — CMS rotates publish/processing date stamps in the URL on
each refresh; the date-stamps are pinned in the audit-run row at completion.

Schema convention (matches sec_adv.sql precedent, satisfies CLAUDE.md
§"Source ingest invariant" rule 1 at parquet level): narrow hot columns +
raw_json VARCHAR carrying the full source CSV row encoded as compact JSON.

Identity-spine contract: every Parquet row carries record_id (PK),
program_year, hot columns including `total_amount_of_payment_usdollars`
(DECIMAL(18,2)), `date_of_payment` (DATE), and a computed
`manufacturer_name_normalized` (lowercased + suffix-stripped form of
`Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_Name`). The
manufacturer_name_normalized field is the join spine for downstream PDL /
SEC ADV company resolution.

Predecessor: scripts/run_cms_open_payments_ingest.py (Postgres-target
ingest, kept untouched per directive — see §"Out of scope").

See directive: ~/Desktop/hq/directives/2026-05-07-pharma-nexus-cms-open-payments-r2-rw-ingest.md

Usage:
    doppler run -p hq-all -c prd -- \\
        python3 apps/data-engine-x/scripts/run_cms_open_payments_r2_ingest.py \\
            --feed general --program-year 2024

    # smoke test (writes to /tmp, skips R2 upload):
    doppler run -p hq-all -c prd -- \\
        python3 apps/data-engine-x/scripts/run_cms_open_payments_r2_ingest.py \\
            --feed general --program-year 2024 --no-upload --max-rows 50000
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import re
import sys
import time
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

import boto3
import httpx
import psycopg
import pyarrow as pa
import pyarrow.parquet as pq

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("run_cms_open_payments_r2_ingest")

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

METASTORE_URL = "https://openpaymentsdata.cms.gov/api/1/metastore/schemas/dataset/items"
USER_AGENT = "data-engine-x/cms-op-r2-ingest"
R2_BUCKET = "dex-raw-landing-zone"
R2_PREFIX = "cms-open-payments"
CHUNK_ROWS = 50_000

# Legal-form suffixes stripped from manufacturer names. Same set as enrich /
# resolve normalization elsewhere; kept inline for self-contained transform.
_SUFFIX_RE = re.compile(
    r"\b(?:inc|incorporated|llc|ltd|limited|corp|corporation|company|co|"
    r"gmbh|ag|s\s?a|s\s?p\s?a|plc|usa|us|na|n\s?a|holdings|group)\b\.?",
    re.IGNORECASE,
)
_PUNCT_RE = re.compile(r"[^a-z0-9 ]+")
_WS_RE = re.compile(r"\s+")


def _normalize_manufacturer(name: str | None) -> str | None:
    if not name:
        return None
    s = name.lower().strip()
    s = _PUNCT_RE.sub(" ", s)
    s = _SUFFIX_RE.sub("", s)
    s = _WS_RE.sub(" ", s).strip()
    return s or None


def _strip(s: str | None) -> str | None:
    if s is None:
        return None
    s = str(s).strip().strip('"')
    return s if s else None


def _parse_decimal(s: str | None) -> str | None:
    """Return amount as string for pyarrow decimal — let arrow do precision math."""
    s = _strip(s)
    if s is None:
        return None
    s = s.replace(",", "").replace("$", "")
    if not s:
        return None
    try:
        # Validate; we keep the original string form (with sign + 2dp) for downstream.
        f = float(s)
        if f != f or f in (float("inf"), float("-inf")):
            return None
        # round to 2dp deterministically
        return f"{f:.2f}"
    except ValueError:
        return None


def _parse_date(s: str | None) -> str | None:
    """Return ISO date string for pyarrow date32; CMS dates are MM/DD/YYYY."""
    s = _strip(s)
    if s is None:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------- #
# DKAN URL discovery (lifted from run_cms_open_payments_ingest.py)
# --------------------------------------------------------------------------- #


def resolve_metastore_url(feed: str, program_year: int) -> tuple[str, str, datetime]:
    """Return (download_url, source_filename, modified) for a CMS Open Payments feed.

    Title pattern: '^{year} {Feed} Payment Data$'. Feed ∈ {general, research}.
    """
    feed_title = feed.capitalize()
    title_re = re.compile(rf"^{program_year} {feed_title} Payment Data$")
    with httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=30.0,
        follow_redirects=True,
    ) as client:
        r = client.get(METASTORE_URL, params={"limit": 300})
        r.raise_for_status()
        items = r.json()
    for item in items:
        title = item.get("title", "")
        if title_re.match(title):
            distributions = item.get("distribution", []) or []
            if not distributions:
                raise RuntimeError(f"Metastore item {title!r} has no distributions")
            dist = distributions[0]
            url = dist.get("downloadURL") or dist.get("accessURL")
            if not url:
                raise RuntimeError(f"Metastore item {title!r} missing downloadURL")
            modified_str = item.get("modified", "") or ""
            try:
                modified = datetime.fromisoformat(modified_str + "T00:00:00+00:00")
            except (ValueError, TypeError):
                modified = datetime.now(timezone.utc)
            filename = url.rsplit("/", 1)[-1]
            logger.info("metastore resolved feed=%s year=%d url=%s modified=%s",
                        feed, program_year, url, modified_str)
            return url, filename, modified
    raise RuntimeError(
        f"No metastore item matched '{program_year} {feed_title} Payment Data'"
    )


# --------------------------------------------------------------------------- #
# Pyarrow schemas + per-feed row transforms
# --------------------------------------------------------------------------- #


_HOT_SCHEMA = pa.schema(
    [
        ("record_id", pa.string()),
        ("program_year", pa.int16()),
        ("total_amount_of_payment_usdollars", pa.decimal128(18, 2)),
        ("date_of_payment", pa.date32()),
        ("applicable_manufacturer_or_applicable_gpo_making_payment_name", pa.string()),
        ("applicable_manufacturer_or_applicable_gpo_making_payment_id", pa.string()),
        ("name_of_drug_or_biological_or_device_or_medical_supply_1", pa.string()),
        ("covered_recipient_npi", pa.string()),
        ("covered_recipient_type", pa.string()),
        ("nature_of_payment_or_transfer_of_value", pa.string()),
        ("dispute_status_for_publication", pa.string()),
        ("manufacturer_name_normalized", pa.string()),
        ("raw_json", pa.string()),
    ]
)


# CMS column-name keys (CSV headers). Both feeds share these for the hot-col
# subset the directive specifies; research has no Nature_of_Payment_or_Transfer_of_Value
# column, so the transform substitutes the constant "research_payment".
_RECORD_ID_KEYS = ("Record_ID", "record_id")
_PROGRAM_YEAR_KEYS = ("Program_Year", "program_year")
_AMOUNT_KEYS = ("Total_Amount_of_Payment_USDollars", "total_amount_of_payment_usdollars")
_DATE_KEYS = ("Date_of_Payment", "date_of_payment")
_MFR_NAME_KEYS = (
    "Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_Name",
    "applicable_manufacturer_or_applicable_gpo_making_payment_name",
)
_MFR_ID_KEYS = (
    "Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_ID",
    "applicable_manufacturer_or_applicable_gpo_making_payment_id",
)
_DRUG_NAME_KEYS = (
    "Name_of_Drug_or_Biological_or_Device_or_Medical_Supply_1",
    "name_of_drug_or_biological_or_device_or_medical_supply_1",
)
_NPI_KEYS = ("Covered_Recipient_NPI", "covered_recipient_npi")
_RECIP_TYPE_KEYS = ("Covered_Recipient_Type", "covered_recipient_type")
_NATURE_KEYS = (
    "Nature_of_Payment_or_Transfer_of_Value",
    "nature_of_payment_or_transfer_of_value",
)
_DISPUTE_KEYS = (
    "Dispute_Status_for_Publication",
    "dispute_status_for_publication",
)


def _first(row: dict, keys: tuple[str, ...]) -> str | None:
    for k in keys:
        v = row.get(k)
        if v is not None and v != "":
            return v
    return None


def _t_general(row: dict) -> dict:
    mfr_name = _first(row, _MFR_NAME_KEYS)
    return {
        "record_id": _strip(_first(row, _RECORD_ID_KEYS)),
        "program_year": _to_int(_first(row, _PROGRAM_YEAR_KEYS)),
        "total_amount_of_payment_usdollars": _parse_decimal(_first(row, _AMOUNT_KEYS)),
        "date_of_payment": _parse_date(_first(row, _DATE_KEYS)),
        "applicable_manufacturer_or_applicable_gpo_making_payment_name": _strip(mfr_name),
        "applicable_manufacturer_or_applicable_gpo_making_payment_id": _strip(_first(row, _MFR_ID_KEYS)),
        "name_of_drug_or_biological_or_device_or_medical_supply_1": _strip(_first(row, _DRUG_NAME_KEYS)),
        "covered_recipient_npi": _strip(_first(row, _NPI_KEYS)),
        "covered_recipient_type": _strip(_first(row, _RECIP_TYPE_KEYS)),
        "nature_of_payment_or_transfer_of_value": _strip(_first(row, _NATURE_KEYS)),
        "dispute_status_for_publication": _strip(_first(row, _DISPUTE_KEYS)),
        "manufacturer_name_normalized": _normalize_manufacturer(mfr_name),
        "raw_json": json.dumps(row, separators=(",", ":")),
    }


def _t_research(row: dict) -> dict:
    # Research feed has no Nature_of_Payment_or_Transfer_of_Value column;
    # we substitute the constant 'research_payment' to make the master MV's
    # union schema-compatible with general.
    mfr_name = _first(row, _MFR_NAME_KEYS)
    return {
        "record_id": _strip(_first(row, _RECORD_ID_KEYS)),
        "program_year": _to_int(_first(row, _PROGRAM_YEAR_KEYS)),
        "total_amount_of_payment_usdollars": _parse_decimal(_first(row, _AMOUNT_KEYS)),
        "date_of_payment": _parse_date(_first(row, _DATE_KEYS)),
        "applicable_manufacturer_or_applicable_gpo_making_payment_name": _strip(mfr_name),
        "applicable_manufacturer_or_applicable_gpo_making_payment_id": _strip(_first(row, _MFR_ID_KEYS)),
        "name_of_drug_or_biological_or_device_or_medical_supply_1": _strip(_first(row, _DRUG_NAME_KEYS)),
        "covered_recipient_npi": _strip(_first(row, _NPI_KEYS)),
        "covered_recipient_type": _strip(_first(row, _RECIP_TYPE_KEYS)),
        "nature_of_payment_or_transfer_of_value": "research_payment",
        "dispute_status_for_publication": _strip(_first(row, _DISPUTE_KEYS)),
        "manufacturer_name_normalized": _normalize_manufacturer(mfr_name),
        "raw_json": json.dumps(row, separators=(",", ":")),
    }


def _to_int(s: str | None) -> int | None:
    s = _strip(s)
    if s is None:
        return None
    try:
        return int(s)
    except ValueError:
        return None


@dataclass(frozen=True)
class FeedSpec:
    name: str
    title_word: str  # "General" or "Research"
    transform: Callable[[dict], dict]


FEED_SPECS: dict[str, FeedSpec] = {
    "general": FeedSpec("general", "General", _t_general),
    "research": FeedSpec("research", "Research", _t_research),
}


# --------------------------------------------------------------------------- #
# I/O helpers
# --------------------------------------------------------------------------- #


def stream_download(url: str, dest: Path) -> int:
    """Stream HTTPS response to dest; returns total bytes."""
    bytes_total = 0
    next_log = 100 * 1024 * 1024
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    with httpx.stream("GET", url, headers=headers, timeout=600.0, follow_redirects=True) as resp:
        resp.raise_for_status()
        with dest.open("wb") as f:
            for chunk in resp.iter_bytes(chunk_size=1 << 20):
                f.write(chunk)
                bytes_total += len(chunk)
                if bytes_total >= next_log:
                    logger.info("download_progress mb=%d url=%s",
                                bytes_total // (1 << 20), url)
                    next_log += 100 * 1024 * 1024
    return bytes_total


def ensure_csv(url: str, cache_dir: Path, filename: str) -> tuple[Path, int]:
    """Cache CSV from url under cache_dir/filename; returns (path, size_bytes).

    Re-uses cache if file is already present and >1 KB. Caller is responsible
    for cache invalidation (delete the file to force re-download).
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    p = cache_dir / filename
    if p.exists() and p.stat().st_size > 1024:
        logger.info("csv_cached path=%s bytes=%d", p, p.stat().st_size)
        return p, p.stat().st_size
    logger.info("csv_download url=%s -> %s", url, p)
    n = stream_download(url, p)
    logger.info("csv_downloaded bytes=%d", n)
    return p, n


def iter_csv_rows(csv_path: Path, max_rows: int | None) -> Iterator[dict]:
    """Yield CSV rows as dicts. CMS files use UTF-8 with occasional latin-1
    fallback on non-ASCII manufacturer names; csv module handles either
    transparently when we open with errors='replace'."""
    with csv_path.open("r", encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            if max_rows is not None and i >= max_rows:
                return
            yield row


# --------------------------------------------------------------------------- #
# R2 client
# --------------------------------------------------------------------------- #


def make_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


# --------------------------------------------------------------------------- #
# Audit row helpers — ops.cms_open_payments_r2_ingest_runs
# --------------------------------------------------------------------------- #


def insert_audit_row(
    conn: psycopg.Connection,
    *,
    feed: str,
    program_year: int,
    source_url: str,
    source_filename: str,
    source_last_modified: datetime,
    invoked_by: str | None,
) -> uuid.UUID:
    sql = """
    INSERT INTO ops.cms_open_payments_r2_ingest_runs (
        run_id, feed_name, program_year,
        source_url, source_filename, source_last_modified,
        status, started_at, invoked_by
    ) VALUES (
        gen_random_uuid(), %s, %s,
        %s, %s, %s,
        'running', now(), %s
    ) RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            feed, program_year,
            source_url, source_filename, source_last_modified,
            invoked_by,
        ))
        row_id = cur.fetchone()[0]
    conn.commit()
    return row_id


def finalize_audit_row(
    conn: psycopg.Connection,
    row_id: uuid.UUID,
    *,
    status: str,
    r2_prefix: str | None = None,
    r2_object_count: int | None = None,
    r2_total_bytes: int | None = None,
    parquet_row_count: int | None = None,
    source_byte_size: int | None = None,
    duration_seconds: float | None = None,
    error_class: str | None = None,
    error_message: str | None = None,
) -> None:
    sql = """
    UPDATE ops.cms_open_payments_r2_ingest_runs
       SET status = %s,
           completed_at = now(),
           r2_prefix = COALESCE(%s, r2_prefix),
           r2_object_count = COALESCE(%s, r2_object_count),
           r2_total_bytes = COALESCE(%s, r2_total_bytes),
           parquet_row_count = COALESCE(%s, parquet_row_count),
           source_byte_size = COALESCE(%s, source_byte_size),
           duration_seconds = COALESCE(%s, duration_seconds),
           error_class = COALESCE(%s, error_class),
           error_message = COALESCE(%s, error_message)
     WHERE id = %s;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            status,
            r2_prefix, r2_object_count, r2_total_bytes,
            parquet_row_count, source_byte_size,
            duration_seconds, error_class, error_message,
            row_id,
        ))
    conn.commit()


# --------------------------------------------------------------------------- #
# Per-feed writer
# --------------------------------------------------------------------------- #


@dataclass
class WriteResult:
    feed: str
    rows_in: int = 0
    rows_out: int = 0
    parts: int = 0
    bytes_written: int = 0
    elapsed_seconds: float = 0.0
    r2_keys: list[str] = field(default_factory=list)
    r2_prefix: str = ""


def write_feed(
    spec: FeedSpec,
    *,
    csv_path: Path,
    out_dir: Path,
    s3,
    program_year: int,
    upload_r2: bool,
    rows_per_part: int,
    max_rows: int | None,
) -> WriteResult:
    out_dir.mkdir(parents=True, exist_ok=True)
    result = WriteResult(feed=spec.name,
                          r2_prefix=f"{R2_PREFIX}/year={program_year}/feed={spec.name}/")
    t0 = time.monotonic()

    buf: list[dict] = []
    part_idx = 0

    def flush() -> None:
        nonlocal part_idx
        if not buf:
            return
        cols: dict[str, list] = {f.name: [] for f in _HOT_SCHEMA}
        for r in buf:
            for f in _HOT_SCHEMA:
                cols[f.name].append(r.get(f.name))
        # Decimal column needs explicit conversion from string → Decimal.
        from decimal import Decimal
        cols["total_amount_of_payment_usdollars"] = [
            Decimal(v) if v is not None else None
            for v in cols["total_amount_of_payment_usdollars"]
        ]
        # date32 wants datetime.date objects, not strings.
        cols["date_of_payment"] = [
            datetime.fromisoformat(v).date() if v is not None else None
            for v in cols["date_of_payment"]
        ]
        tbl = pa.table(cols, schema=_HOT_SCHEMA)
        local_path = out_dir / f"part-{part_idx:05d}.parquet"
        pq.write_table(tbl, local_path, compression="zstd", compression_level=9)
        size = local_path.stat().st_size
        r2_key = f"{result.r2_prefix}part-{part_idx:05d}.parquet"
        if upload_r2:
            s3.upload_file(str(local_path), R2_BUCKET, r2_key)
            local_path.unlink(missing_ok=True)
        else:
            r2_key = f"(local) {local_path}"
        result.parts += 1
        result.bytes_written += size
        result.r2_keys.append(r2_key)
        logger.info("part_written feed=%s part=%d rows=%d bytes=%d r2_key=%s",
                    spec.name, part_idx, len(buf), size, r2_key)
        part_idx += 1
        buf.clear()

    for row in iter_csv_rows(csv_path, max_rows):
        result.rows_in += 1
        try:
            transformed = spec.transform(row)
        except Exception as exc:
            logger.warning("row_transform_error feed=%s row_in=%d err=%s",
                           spec.name, result.rows_in, exc)
            continue
        if not transformed.get("record_id"):
            continue
        buf.append(transformed)
        result.rows_out += 1
        if len(buf) >= rows_per_part:
            flush()
        if result.rows_in % 250_000 == 0:
            logger.info("read_progress feed=%s rows_in=%d rows_out=%d parts=%d",
                        spec.name, result.rows_in, result.rows_out, result.parts)

    flush()
    result.elapsed_seconds = time.monotonic() - t0
    logger.info("feed_done feed=%s rows_in=%d rows_out=%d parts=%d bytes=%d elapsed=%.1fs",
                spec.name, result.rows_in, result.rows_out, result.parts,
                result.bytes_written, result.elapsed_seconds)
    return result


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--feed", choices=sorted(FEED_SPECS.keys()), required=True)
    ap.add_argument("--program-year", type=int, default=2024)
    ap.add_argument("--cache-dir", type=Path, default=Path("/tmp/cms_op_csvs"))
    ap.add_argument("--out-dir", type=Path, default=Path("/tmp/cms_op_parquet"))
    ap.add_argument("--no-upload", action="store_true",
                    help="Write Parquet locally only; skip R2 upload + audit row.")
    ap.add_argument("--max-rows", type=int, default=None,
                    help="Cap CSV → Parquet output rows (smoke testing).")
    ap.add_argument("--rows-per-part", type=int, default=CHUNK_ROWS)
    ap.add_argument("--invoked-by", default=os.environ.get("USER", "manual"))
    args = ap.parse_args()

    spec = FEED_SPECS[args.feed]
    upload_r2 = not args.no_upload
    s3 = make_s3_client() if upload_r2 else None

    started_at = time.time()
    started_dt = datetime.now(timezone.utc)

    # 1. URL discovery via DKAN metastore
    url, source_filename, source_last_modified = resolve_metastore_url(
        args.feed, args.program_year,
    )

    # 2. Cache CSV locally
    csv_path, csv_bytes = ensure_csv(url, args.cache_dir, source_filename)

    # 3. Audit row (skip in --no-upload mode — no DB side-effect for smoke tests)
    audit_conn: psycopg.Connection | None = None
    audit_row_id: uuid.UUID | None = None
    if upload_r2:
        db_url = os.environ["DEX_DB_URL_DIRECT"]
        audit_conn = psycopg.connect(db_url, autocommit=False)
        audit_row_id = insert_audit_row(
            audit_conn,
            feed=args.feed,
            program_year=args.program_year,
            source_url=url,
            source_filename=source_filename,
            source_last_modified=source_last_modified,
            invoked_by=args.invoked_by,
        )
        logger.info("audit_row inserted id=%s feed=%s year=%d",
                    audit_row_id, args.feed, args.program_year)

    # 4. CSV → chunked Parquet → R2
    out_sub = args.out_dir / args.feed
    try:
        result = write_feed(
            spec,
            csv_path=csv_path,
            out_dir=out_sub,
            s3=s3,
            program_year=args.program_year,
            upload_r2=upload_r2,
            rows_per_part=args.rows_per_part,
            max_rows=args.max_rows,
        )
    except Exception as exc:
        logger.exception("feed_write_failed feed=%s err=%s", args.feed, exc)
        if audit_conn is not None and audit_row_id is not None:
            try:
                finalize_audit_row(
                    audit_conn, audit_row_id,
                    status="failed",
                    duration_seconds=time.time() - started_at,
                    error_class="parse_failure",
                    error_message=str(exc)[:1000],
                    source_byte_size=csv_bytes,
                )
            except Exception:
                logger.exception("failed to write failure audit row")
        if audit_conn is not None:
            audit_conn.close()
        return 1

    # 5. Finalize audit
    if audit_conn is not None and audit_row_id is not None:
        finalize_audit_row(
            audit_conn, audit_row_id,
            status="completed",
            r2_prefix=result.r2_prefix,
            r2_object_count=result.parts,
            r2_total_bytes=result.bytes_written,
            parquet_row_count=result.rows_out,
            source_byte_size=csv_bytes,
            duration_seconds=time.time() - started_at,
        )
        audit_conn.close()

    # 6. JSON summary to stdout (for harness consumption)
    summary = {
        "started_at": started_dt.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "feed": args.feed,
        "program_year": args.program_year,
        "source_url": url,
        "source_byte_size": csv_bytes,
        "uploaded_to_r2": upload_r2,
        "r2_bucket": R2_BUCKET if upload_r2 else None,
        "r2_prefix": result.r2_prefix if upload_r2 else None,
        "audit_row_id": str(audit_row_id) if audit_row_id else None,
        "rows_in": result.rows_in,
        "rows_out": result.rows_out,
        "parts": result.parts,
        "bytes_written": result.bytes_written,
        "elapsed_seconds": round(result.elapsed_seconds, 2),
        "r2_keys_sample": result.r2_keys[:3]
            + ([f"... ({len(result.r2_keys) - 3} more)"] if len(result.r2_keys) > 3 else []),
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

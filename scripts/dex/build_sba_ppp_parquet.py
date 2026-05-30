#!/usr/bin/env python3
"""Stream SBA PPP FOIA CSVs into ZSTD-Parquet on Cloudflare R2.

Output layout in `dex-raw-landing-zone`:
    sba/program=ppp/segment=<N>/part-NNNNN.parquet

Source: DKAN dataset `ppp-foia` at https://data.sba.gov/dataset/ppp-foia.
13 CSV resources (last refresh 2024-09-30, suffix `_240930`):

    segment=0      public_150k_plus_240930.csv          (~600K loans ≥ $150K)
    segment=1..12  public_up_to_150k_<N>_240930.csv     (~10.9M loans < $150K)

Total: ~11.5M rows, ~5.2 GB CSV uncompressed.

Identity-key contract — every Parquet row carries:
    loan_number (int64), date_approved (timestamp, UTC),
    borrower_name (string), borrower_city, borrower_state (2-char USPS),
    borrower_zip (raw), borrower_zip5 (computed, 5-char), borrower_name_normalized,
    initial_approval_amount/current_approval_amount/forgiveness_amount/undisbursed_amount
    (decimal(18,2)), naics_code, business_type, jobs_reported, raw_json.

Usage (under uv-managed venv with httpx, pyarrow, boto3, psycopg):

    doppler run -p hq-all -c prd -- \\
        uv run --project apps/data-engine-x \\
        python apps/data-engine-x/scripts/build_sba_ppp_parquet.py --all

    # Smoke test (segment 1, capped at 50K rows):
    doppler run -p hq-all -c prd -- \\
        uv run --project apps/data-engine-x \\
        python apps/data-engine-x/scripts/build_sba_ppp_parquet.py \\
        --segments 1 --max-rows 50000

    # Local-only (skip R2 upload):
    python apps/data-engine-x/scripts/build_sba_ppp_parquet.py \\
        --all --no-upload --no-audit

See directive: ~/Desktop/hq/directives/2026-05-07-sba-ppp-ingest.md.
"""

from __future__ import annotations

import argparse
import csv
import decimal
import io
import json
import logging
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import boto3
import httpx
import pyarrow as pa
import pyarrow.parquet as pq

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("build_sba_ppp_parquet")

# ---------------------------------------------------------------------------
# Source: DKAN ppp-foia dataset
# ---------------------------------------------------------------------------

DKAN_PACKAGE_URL = "https://data.sba.gov/api/3/action/package_show?id=ppp-foia"
USER_AGENT = "data-engine-x ingest (operator: tools@substrate.build)"

R2_BUCKET = "dex-raw-landing-zone"
R2_PREFIX = "sba/program=ppp"

CHUNK_ROWS = 50_000
SEGMENT_RANGE = range(0, 13)  # 0 = 150k_plus, 1..12 = up_to_150k_<N>

# Empty-string sentinels in PPP source data (and CSV-quoted ""s).
_EMPTY_SENTINELS = {"", "Unknown", "Unknown/NotStated", "Unanswered", "N/A", "NA"}

# Suffix tokens stripped during borrower_name_normalized. Keep ordered by
# length descending so longer suffixes match before their substrings.
_SUFFIX_TOKENS = [
    "incorporated", "corporation", "company", "limited",
    "pllc", "llp", "lp", "llc", "inc", "ltd", "corp", "co", "pa",
]
_SUFFIX_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(t) for t in _SUFFIX_TOKENS) + r")\b\.?",
    re.IGNORECASE,
)
_PUNCT_RE = re.compile(r"[^\w\s]+")
_WS_RE = re.compile(r"\s+")
_ZIP5_RE = re.compile(r"^\s*(\d{5})")


def _strip(s: str | None) -> str | None:
    if s is None:
        return None
    s = str(s).strip()
    if s in _EMPTY_SENTINELS:
        return None
    return s


def _normalize_borrower_name(name: str | None) -> str | None:
    s = _strip(name)
    if s is None:
        return None
    s = s.lower()
    s = _SUFFIX_RE.sub(" ", s)
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s or None


def _zip5(z: str | None) -> str | None:
    s = _strip(z)
    if s is None:
        return None
    m = _ZIP5_RE.match(s)
    return m.group(1) if m else None


def _state(s: str | None) -> str | None:
    v = _strip(s)
    if v is None:
        return None
    v = v.upper()
    return v if len(v) == 2 and v.isalpha() else None


def _parse_int(s: str | None) -> int | None:
    s = _strip(s)
    if s is None:
        return None
    s = s.replace(",", "")
    try:
        return int(s)
    except ValueError:
        try:
            return int(float(s))
        except ValueError:
            return None


def _parse_decimal(s: str | None) -> decimal.Decimal | None:
    """Returns a Decimal quantized to 2 places, suitable for decimal128(18,2)."""
    s = _strip(s)
    if s is None:
        return None
    s = s.replace(",", "").replace("$", "")
    try:
        d = decimal.Decimal(s)
        # Quantize to 2 places — guards against rare scientific-notation rows
        # producing unstable scale across Parquet parts.
        return d.quantize(decimal.Decimal("0.01"), rounding=decimal.ROUND_HALF_UP)
    except (decimal.InvalidOperation, ValueError):
        return None


def _parse_date_us(s: str | None) -> datetime | None:
    s = _strip(s)
    if s is None:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _ts_to_pa(dt: datetime | None) -> int | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.astimezone(timezone.utc).timestamp() * 1_000_000)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

PPP_SCHEMA = pa.schema(
    [
        # Identity
        ("loan_number", pa.int64()),
        ("date_approved", pa.timestamp("us", tz="UTC")),
        ("sba_office_code", pa.string()),
        ("processing_method", pa.string()),
        # Borrower (raw + normalized)
        ("borrower_name", pa.string()),
        ("borrower_address", pa.string()),
        ("borrower_city", pa.string()),
        ("borrower_state", pa.string()),
        ("borrower_zip", pa.string()),
        ("borrower_name_normalized", pa.string()),
        ("borrower_zip5", pa.string()),
        # Loan lifecycle
        ("loan_status_date", pa.timestamp("us", tz="UTC")),
        ("loan_status", pa.string()),
        ("term", pa.int32()),
        ("sba_guaranty_percentage", pa.int32()),
        ("initial_approval_amount", pa.decimal128(18, 2)),
        ("current_approval_amount", pa.decimal128(18, 2)),
        ("undisbursed_amount", pa.decimal128(18, 2)),
        ("forgiveness_amount", pa.decimal128(18, 2)),
        ("forgiveness_date", pa.timestamp("us", tz="UTC")),
        ("franchise_name", pa.string()),
        # Lender
        ("servicing_lender_location_id", pa.string()),
        ("servicing_lender_name", pa.string()),
        ("servicing_lender_address", pa.string()),
        ("servicing_lender_city", pa.string()),
        ("servicing_lender_state", pa.string()),
        ("servicing_lender_zip", pa.string()),
        ("originating_lender_location_id", pa.string()),
        ("originating_lender", pa.string()),
        ("originating_lender_city", pa.string()),
        ("originating_lender_state", pa.string()),
        # Geo flags
        ("rural_urban_indicator", pa.string()),
        ("hubzone_indicator", pa.string()),
        ("lmi_indicator", pa.string()),
        ("business_age_description", pa.string()),
        # Project geo
        ("project_city", pa.string()),
        ("project_county_name", pa.string()),
        ("project_state", pa.string()),
        ("project_zip", pa.string()),
        ("cd", pa.string()),
        # Classification
        ("jobs_reported", pa.int32()),
        ("naics_code", pa.string()),
        ("race", pa.string()),
        ("ethnicity", pa.string()),
        ("business_type", pa.string()),
        ("gender", pa.string()),
        ("veteran", pa.string()),
        ("non_profit", pa.string()),
        # Loan-use proceed buckets (decimal — sum reconciles to InitialApprovalAmount)
        ("utilities_proceed", pa.decimal128(18, 2)),
        ("payroll_proceed", pa.decimal128(18, 2)),
        ("mortgage_interest_proceed", pa.decimal128(18, 2)),
        ("rent_proceed", pa.decimal128(18, 2)),
        ("refinance_eidl_proceed", pa.decimal128(18, 2)),
        ("health_care_proceed", pa.decimal128(18, 2)),
        ("debt_interest_proceed", pa.decimal128(18, 2)),
        # Verbatim source row for forward-compat
        ("raw_json", pa.string()),
    ]
)


def _row_transform(row: dict) -> dict:
    """Map a single CSV DictReader row → typed dict matching PPP_SCHEMA."""
    borrower_state = _state(row.get("BorrowerState")) or _state(row.get("ProjectState"))
    return {
        "loan_number":            _parse_int(row.get("LoanNumber")),
        "date_approved":          _ts_to_pa(_parse_date_us(row.get("DateApproved"))),
        "sba_office_code":        _strip(row.get("SBAOfficeCode")),
        "processing_method":      _strip(row.get("ProcessingMethod")),

        "borrower_name":          _strip(row.get("BorrowerName")),
        "borrower_address":       _strip(row.get("BorrowerAddress")),
        "borrower_city":          _strip(row.get("BorrowerCity")),
        "borrower_state":         borrower_state,
        "borrower_zip":           _strip(row.get("BorrowerZip")),
        "borrower_name_normalized": _normalize_borrower_name(row.get("BorrowerName")),
        "borrower_zip5":          _zip5(row.get("BorrowerZip")) or _zip5(row.get("ProjectZip")),

        "loan_status_date":       _ts_to_pa(_parse_date_us(row.get("LoanStatusDate"))),
        "loan_status":            _strip(row.get("LoanStatus")),
        "term":                   _parse_int(row.get("Term")),
        "sba_guaranty_percentage": _parse_int(row.get("SBAGuarantyPercentage")),
        "initial_approval_amount": _parse_decimal(row.get("InitialApprovalAmount")),
        "current_approval_amount": _parse_decimal(row.get("CurrentApprovalAmount")),
        "undisbursed_amount":     _parse_decimal(row.get("UndisbursedAmount")),
        "forgiveness_amount":     _parse_decimal(row.get("ForgivenessAmount")),
        "forgiveness_date":       _ts_to_pa(_parse_date_us(row.get("ForgivenessDate"))),
        "franchise_name":         _strip(row.get("FranchiseName")),

        "servicing_lender_location_id": _strip(row.get("ServicingLenderLocationID")),
        "servicing_lender_name":   _strip(row.get("ServicingLenderName")),
        "servicing_lender_address": _strip(row.get("ServicingLenderAddress")),
        "servicing_lender_city":  _strip(row.get("ServicingLenderCity")),
        "servicing_lender_state": _state(row.get("ServicingLenderState")),
        "servicing_lender_zip":   _strip(row.get("ServicingLenderZip")),
        "originating_lender_location_id": _strip(row.get("OriginatingLenderLocationID")),
        "originating_lender":     _strip(row.get("OriginatingLender")),
        "originating_lender_city": _strip(row.get("OriginatingLenderCity")),
        "originating_lender_state": _state(row.get("OriginatingLenderState")),

        "rural_urban_indicator":  _strip(row.get("RuralUrbanIndicator")),
        "hubzone_indicator":      _strip(row.get("HubzoneIndicator")),
        "lmi_indicator":          _strip(row.get("LMIIndicator")),
        "business_age_description": _strip(row.get("BusinessAgeDescription")),

        "project_city":           _strip(row.get("ProjectCity")),
        "project_county_name":    _strip(row.get("ProjectCountyName")),
        "project_state":          _state(row.get("ProjectState")),
        "project_zip":            _strip(row.get("ProjectZip")),
        "cd":                     _strip(row.get("CD")),

        "jobs_reported":          _parse_int(row.get("JobsReported")),
        "naics_code":             _strip(row.get("NAICSCode")),
        "race":                   _strip(row.get("Race")),
        "ethnicity":              _strip(row.get("Ethnicity")),
        "business_type":          _strip(row.get("BusinessType")),
        "gender":                 _strip(row.get("Gender")),
        "veteran":                _strip(row.get("Veteran")),
        "non_profit":             _strip(row.get("NonProfit")),

        "utilities_proceed":         _parse_decimal(row.get("UTILITIES_PROCEED")),
        "payroll_proceed":           _parse_decimal(row.get("PAYROLL_PROCEED")),
        "mortgage_interest_proceed": _parse_decimal(row.get("MORTGAGE_INTEREST_PROCEED")),
        "rent_proceed":              _parse_decimal(row.get("RENT_PROCEED")),
        "refinance_eidl_proceed":    _parse_decimal(row.get("REFINANCE_EIDL_PROCEED")),
        "health_care_proceed":       _parse_decimal(row.get("HEALTH_CARE_PROCEED")),
        "debt_interest_proceed":     _parse_decimal(row.get("DEBT_INTEREST_PROCEED")),

        "raw_json": json.dumps(row, separators=(",", ":")),
    }


# ---------------------------------------------------------------------------
# DKAN discovery + downloads
# ---------------------------------------------------------------------------


@dataclass
class SegmentResource:
    segment: int
    dkan_resource_id: str
    filename: str
    url: str
    size_bytes: int
    last_modified_iso: str | None


def discover_segments() -> list[SegmentResource]:
    """Hit DKAN package_show, return the 13 segments ordered 0..12."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    resp = httpx.get(DKAN_PACKAGE_URL, headers=headers, timeout=60.0)
    resp.raise_for_status()
    payload = resp.json()
    out: dict[int, SegmentResource] = {}
    for r in payload["result"]["resources"]:
        name = r.get("name", "")
        if name.startswith("public_150k_plus"):
            seg = 0
        elif name.startswith("public_up_to_150k_"):
            try:
                seg = int(name.removeprefix("public_up_to_150k_").split("_", 1)[0])
            except ValueError:
                continue
        else:
            continue
        out[seg] = SegmentResource(
            segment=seg,
            dkan_resource_id=r.get("id", ""),
            filename=name,
            url=r.get("url", ""),
            size_bytes=int(r.get("size") or 0),
            last_modified_iso=r.get("last_modified"),
        )
    return [out[s] for s in sorted(out)]


def stream_csv(url: str, dest: Path) -> tuple[int, str | None]:
    """Stream-download CSV to local cache. Returns (bytes, last_modified)."""
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    next_log = 100 * 1024 * 1024
    bytes_total = 0
    last_modified: str | None = None
    with httpx.stream("GET", url, headers=headers, timeout=600.0, follow_redirects=True) as resp:
        resp.raise_for_status()
        last_modified = resp.headers.get("last-modified")
        with dest.open("wb") as f:
            for chunk in resp.iter_bytes(chunk_size=1 << 20):
                f.write(chunk)
                bytes_total += len(chunk)
                if bytes_total >= next_log:
                    logger.info("download_progress mb=%d url=%s",
                                bytes_total // (1 << 20), url)
                    next_log += 100 * 1024 * 1024
    return bytes_total, last_modified


def ensure_csv(seg: SegmentResource, cache_dir: Path) -> tuple[Path, int, str | None]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    p = cache_dir / seg.filename
    if p.exists() and p.stat().st_size > 1024 and (
        seg.size_bytes == 0 or abs(p.stat().st_size - seg.size_bytes) <= seg.size_bytes // 20
    ):
        logger.info("csv_cached seg=%d path=%s bytes=%d", seg.segment, p, p.stat().st_size)
        return p, p.stat().st_size, None
    logger.info("csv_download seg=%d url=%s -> %s", seg.segment, seg.url, p)
    bytes_total, last_modified = stream_csv(seg.url, p)
    logger.info("csv_downloaded seg=%d bytes=%d last_modified=%s",
                seg.segment, bytes_total, last_modified)
    return p, bytes_total, last_modified


def iter_csv_rows(csv_path: Path) -> Iterator[dict]:
    # PPP CSVs are Latin-1 in places (legacy data). Use latin-1 → never raises
    # on stray bytes, faithful pass-through into raw_json.
    with csv_path.open("rb") as raw:
        txt = io.TextIOWrapper(raw, encoding="latin-1", newline="")
        yield from csv.DictReader(txt)


# ---------------------------------------------------------------------------
# R2 + Postgres helpers
# ---------------------------------------------------------------------------


def make_s3_client():
    endpoint = os.environ["R2_ENDPOINT"]
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def _audit_dsn() -> str | None:
    return (
        os.environ.get("DEX_DB_URL_DIRECT")
        or os.environ.get("SUPABASE_DB_URL_DIRECT")
        or os.environ.get("DEX_DB_URL_POOLED")
    )


def _segment_label(seg: SegmentResource) -> str:
    """segment_label is NOT NULL in the schema. Derive a human-readable label
    from the source filename: 'public_150k_plus' or 'public_up_to_150k_<N>'."""
    name = seg.filename.removesuffix(".csv")
    # Strip the date suffix '_240930' if present.
    return re.sub(r"_\d{6}$", "", name)


def _audit_insert_pending(dsn: str, seg: SegmentResource) -> str:
    """Insert pending row, return its id."""
    import psycopg
    run_id = str(uuid.uuid4())
    r2_prefix = f"{R2_PREFIX}/segment={seg.segment}/"
    last_modified = _parse_date_iso(seg.last_modified_iso)
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ops.sba_ppp_ingest_runs
                  (id, segment, segment_label, source_resource_id, source_url,
                   source_last_modified, status, started_at,
                   r2_bucket, r2_prefix, invoked_by)
                VALUES (%s, %s, %s, %s, %s, %s, 'running', now(), %s, %s, 'cli')
                """,
                (run_id, seg.segment, _segment_label(seg), seg.dkan_resource_id, seg.url,
                 last_modified, R2_BUCKET, r2_prefix),
            )
        conn.commit()
    return run_id


def _audit_finish(dsn: str, run_id: str, *, status: str,
                  csv_bytes: int | None, csv_rows_in: int | None,
                  parquet_part_count: int | None, parquet_row_count: int | None,
                  parquet_bytes: int | None, r2_object_count: int | None,
                  r2_total_bytes: int | None,
                  rows_with_borrower_name: int | None,
                  rows_with_borrower_state: int | None,
                  rows_with_borrower_zip: int | None,
                  rows_with_naics: int | None,
                  rows_with_initial_amount: int | None,
                  rows_with_normalized_name: int | None,
                  duration_seconds: float | None,
                  error_message: str | None = None,
                  error_class: str | None = None,
                  notes: dict | None = None) -> None:
    import psycopg
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE ops.sba_ppp_ingest_runs SET
                  status                    = %s,
                  finished_at               = now(),
                  duration_seconds          = %s,
                  csv_bytes_downloaded      = %s,
                  rows_in_csv               = %s,
                  parquet_part_count        = %s,
                  parquet_row_count         = %s,
                  parquet_bytes_written     = %s,
                  r2_object_count           = %s,
                  r2_total_bytes            = %s,
                  rows_with_borrower_name   = %s,
                  rows_with_borrower_state  = %s,
                  rows_with_borrower_zip    = %s,
                  rows_with_naics           = %s,
                  rows_with_initial_amount  = %s,
                  rows_with_normalized_name = %s,
                  error_message             = %s,
                  error_class               = %s,
                  notes                     = %s
                WHERE id = %s
                """,
                (status, duration_seconds, csv_bytes, csv_rows_in,
                 parquet_part_count, parquet_row_count, parquet_bytes,
                 r2_object_count, r2_total_bytes,
                 rows_with_borrower_name, rows_with_borrower_state,
                 rows_with_borrower_zip, rows_with_naics,
                 rows_with_initial_amount, rows_with_normalized_name,
                 error_message, error_class,
                 json.dumps(notes) if notes is not None else None,
                 run_id),
            )
        conn.commit()


def _parse_date_iso(s: str | None) -> datetime | None:
    """DKAN's last_modified field is ISO-8601-ish, not US."""
    if not s:
        return None
    try:
        if "T" in s:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Per-segment writer
# ---------------------------------------------------------------------------


@dataclass
class SegmentResult:
    segment: int
    rows_in: int = 0
    rows_out: int = 0
    parts: int = 0
    bytes_written: int = 0
    elapsed_seconds: float = 0.0
    r2_keys: list[str] = field(default_factory=list)
    rows_with_borrower_name: int = 0
    rows_with_borrower_state: int = 0
    rows_with_borrower_zip: int = 0
    rows_with_naics: int = 0
    rows_with_initial_amount: int = 0
    rows_with_normalized_name: int = 0


def write_segment(
    seg: SegmentResource,
    csv_path: Path,
    *,
    out_dir: Path,
    s3,
    upload_r2: bool,
    rows_per_part: int,
    max_rows: int | None,
) -> SegmentResult:
    out_dir.mkdir(parents=True, exist_ok=True)
    result = SegmentResult(segment=seg.segment)
    t0 = time.monotonic()

    buf: list[dict] = []
    part_idx = 0
    n_in = 0
    n_out = 0

    def flush() -> None:
        nonlocal part_idx
        if not buf:
            return
        cols: dict[str, list] = {f.name: [] for f in PPP_SCHEMA}
        for r in buf:
            for f in PPP_SCHEMA:
                cols[f.name].append(r.get(f.name))
        tbl = pa.table(cols, schema=PPP_SCHEMA)
        local_path = out_dir / f"part-{part_idx:05d}.parquet"
        # ZSTD level 3 (default) — empirically ~3x faster than level 9 with
        # only ~10-15% larger files. Trade-off chosen for the 11.5M-row PPP
        # ingest where part-write time was the bottleneck on segment 0 (≈63s
        # per 50K-row part at level 9; level 3 brings this to ≈20s).
        pq.write_table(tbl, local_path, compression="zstd", compression_level=3)
        size = local_path.stat().st_size
        r2_key = f"{R2_PREFIX}/segment={seg.segment}/part-{part_idx:05d}.parquet"
        if upload_r2:
            s3.upload_file(str(local_path), R2_BUCKET, r2_key)
            local_path.unlink(missing_ok=True)
        else:
            r2_key = f"(local) {local_path}"
        result.parts += 1
        result.bytes_written += size
        result.r2_keys.append(r2_key)
        logger.info("part_written seg=%d part=%d rows=%d bytes=%d r2_key=%s",
                    seg.segment, part_idx, len(buf), size, r2_key)
        part_idx += 1
        buf.clear()

    for row in iter_csv_rows(csv_path):
        n_in += 1
        try:
            transformed = _row_transform(row)
        except Exception as exc:
            logger.warning("row_transform_error seg=%d row_in=%d err=%s",
                           seg.segment, n_in, exc)
            continue

        if transformed["loan_number"] is None:
            # PPP rows without LoanNumber are unusable — no natural PK, no
            # downstream join possible. Drop and count.
            continue

        # Coverage stats — bookkeeping for the audit ledger.
        if transformed["borrower_name"] is not None:        result.rows_with_borrower_name += 1
        if transformed["borrower_state"] is not None:       result.rows_with_borrower_state += 1
        if transformed["borrower_zip"] is not None:         result.rows_with_borrower_zip += 1
        if transformed["naics_code"] is not None:           result.rows_with_naics += 1
        if transformed["initial_approval_amount"] is not None:
                                                            result.rows_with_initial_amount += 1
        if transformed["borrower_name_normalized"] is not None:
                                                            result.rows_with_normalized_name += 1

        buf.append(transformed)
        n_out += 1
        if len(buf) >= rows_per_part:
            flush()
        if n_in % 250_000 == 0:
            logger.info("read_progress seg=%d rows_in=%d rows_out=%d",
                        seg.segment, n_in, n_out)
        if max_rows is not None and n_out >= max_rows:
            logger.info("max_rows_hit seg=%d max_rows=%d — stopping early",
                        seg.segment, max_rows)
            break

    flush()
    result.rows_in = n_in
    result.rows_out = n_out
    result.elapsed_seconds = time.monotonic() - t0
    logger.info("segment_done seg=%d rows_in=%d rows_out=%d parts=%d bytes=%d elapsed=%.1fs",
                seg.segment, n_in, n_out, result.parts, result.bytes_written,
                result.elapsed_seconds)
    return result


def list_r2_segment_objects(s3, segment: int) -> tuple[int, int]:
    """Return (object_count, total_bytes) for a segment prefix."""
    prefix = f"{R2_PREFIX}/segment={segment}/"
    paginator = s3.get_paginator("list_objects_v2")
    n = 0
    total = 0
    for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []) or []:
            n += 1
            total += obj["Size"]
    return n, total


def clear_r2_segment(s3, segment: int) -> int:
    """Delete all objects under sba/program=ppp/segment=<N>/. Returns count."""
    prefix = f"{R2_PREFIX}/segment={segment}/"
    paginator = s3.get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []) or []:
            keys.append(obj["Key"])
    for i in range(0, len(keys), 1000):
        batch = [{"Key": k} for k in keys[i:i + 1000]]
        s3.delete_objects(Bucket=R2_BUCKET, Delete={"Objects": batch, "Quiet": True})
    if keys:
        logger.info("r2_cleared seg=%d objects=%d", segment, len(keys))
    return len(keys)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--all", action="store_true",
                     help="Run all 13 segments (0..12).")
    grp.add_argument("--segments",
                     help="Comma-separated subset, e.g. '0,1,2' or '5'.")
    ap.add_argument("--cache-dir", type=Path, default=Path("/tmp/sba_ppp_csvs"))
    ap.add_argument("--out-dir", type=Path, default=Path("/tmp/sba_ppp_parquet"))
    ap.add_argument("--no-upload", action="store_true",
                    help="Write Parquet locally only, skip R2 upload.")
    ap.add_argument("--no-audit", action="store_true",
                    help="Skip the ops.sba_ppp_ingest_runs audit row.")
    ap.add_argument("--rows-per-part", type=int, default=CHUNK_ROWS)
    ap.add_argument("--max-rows", type=int,
                    help="Cap rows per segment (smoke-test mode).")
    ap.add_argument("--keep-csv", action="store_true",
                    help="Don't delete CSV cache after a successful segment.")
    ap.add_argument("--clear-r2-segment", action="store_true",
                    help="Before writing, delete all existing R2 objects under "
                         "sba/program=ppp/segment=<N>/. Use when re-running a "
                         "previously-partial segment to avoid mixed part-NNNNN files.")
    args = ap.parse_args()

    if args.all:
        wanted = list(SEGMENT_RANGE)
    else:
        try:
            wanted = sorted({int(s) for s in args.segments.split(",") if s.strip()})
        except ValueError:
            ap.error(f"--segments must be comma-separated ints; got {args.segments!r}")
        bad = [s for s in wanted if s not in SEGMENT_RANGE]
        if bad:
            ap.error(f"unknown segments: {bad}; valid={list(SEGMENT_RANGE)}")

    upload_r2 = not args.no_upload
    s3 = make_s3_client() if upload_r2 else None
    dsn = None if args.no_audit else _audit_dsn()
    if not args.no_audit and dsn is None:
        logger.warning("audit_disabled reason=no_db_url_in_env "
                       "(set DEX_DB_URL_DIRECT to enable ops.sba_ppp_ingest_runs writes)")

    args.cache_dir.mkdir(parents=True, exist_ok=True)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    logger.info("dkan_discover url=%s", DKAN_PACKAGE_URL)
    all_segments = discover_segments()
    if len(all_segments) != 13:
        logger.warning("dkan_discover_unexpected count=%d expected=13", len(all_segments))
    by_seg = {s.segment: s for s in all_segments}

    run_started = datetime.now(timezone.utc)
    results: list[SegmentResult] = []
    failures: list[tuple[int, Exception]] = []

    for seg_num in wanted:
        seg = by_seg.get(seg_num)
        if seg is None:
            logger.error("segment_missing_in_dkan seg=%d — skipping", seg_num)
            continue

        run_id = None
        if dsn is not None:
            try:
                run_id = _audit_insert_pending(dsn, seg)
                logger.info("audit_started seg=%d run_id=%s", seg.segment, run_id)
            except Exception as exc:
                logger.warning("audit_insert_failed seg=%d err=%s — proceeding without ledger",
                               seg.segment, exc)

        try:
            if upload_r2 and args.clear_r2_segment:
                clear_r2_segment(s3, seg.segment)

            csv_path, csv_bytes, _last_modified = ensure_csv(seg, args.cache_dir)
            out_sub = args.out_dir / f"segment={seg.segment}"
            result = write_segment(
                seg, csv_path,
                out_dir=out_sub,
                s3=s3, upload_r2=upload_r2,
                rows_per_part=args.rows_per_part,
                max_rows=args.max_rows,
            )
            results.append(result)

            r2_n, r2_b = (0, 0)
            if upload_r2:
                r2_n, r2_b = list_r2_segment_objects(s3, seg.segment)

            if run_id is not None and dsn is not None:
                _audit_finish(
                    dsn, run_id, status="completed",
                    csv_bytes=csv_bytes, csv_rows_in=result.rows_in,
                    parquet_part_count=result.parts,
                    parquet_row_count=result.rows_out,
                    parquet_bytes=result.bytes_written,
                    r2_object_count=r2_n if upload_r2 else None,
                    r2_total_bytes=r2_b if upload_r2 else None,
                    rows_with_borrower_name=result.rows_with_borrower_name,
                    rows_with_borrower_state=result.rows_with_borrower_state,
                    rows_with_borrower_zip=result.rows_with_borrower_zip,
                    rows_with_naics=result.rows_with_naics,
                    rows_with_initial_amount=result.rows_with_initial_amount,
                    rows_with_normalized_name=result.rows_with_normalized_name,
                    duration_seconds=round(result.elapsed_seconds, 2),
                    notes={"max_rows": args.max_rows} if args.max_rows else None,
                )
                logger.info("audit_completed seg=%d run_id=%s", seg.segment, run_id)

            if not args.keep_csv:
                csv_path.unlink(missing_ok=True)

        except Exception as exc:
            failures.append((seg.segment, exc))
            logger.exception("segment_failed seg=%d", seg.segment)
            if run_id is not None and dsn is not None:
                try:
                    _audit_finish(
                        dsn, run_id, status="failed",
                        csv_bytes=None, csv_rows_in=None,
                        parquet_part_count=None, parquet_row_count=None,
                        parquet_bytes=None,
                        r2_object_count=None, r2_total_bytes=None,
                        rows_with_borrower_name=None, rows_with_borrower_state=None,
                        rows_with_borrower_zip=None, rows_with_naics=None,
                        rows_with_initial_amount=None, rows_with_normalized_name=None,
                        duration_seconds=None,
                        error_message=str(exc)[:2000],
                        error_class=_classify_error(exc),
                    )
                except Exception:
                    logger.exception("audit_finish_failed seg=%d", seg.segment)

    total_rows = sum(r.rows_out for r in results)
    total_bytes = sum(r.bytes_written for r in results)
    total_elapsed = sum(r.elapsed_seconds for r in results)
    logger.info("BUILD COMPLETE segments=%d total_rows=%d total_bytes=%d total_elapsed=%.1fs failures=%d",
                len(results), total_rows, total_bytes, total_elapsed, len(failures))

    summary = {
        "started_at": run_started.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "uploaded_to_r2": upload_r2,
        "bucket": R2_BUCKET if upload_r2 else None,
        "segments": [
            {
                "segment": r.segment,
                "rows_in": r.rows_in,
                "rows_out": r.rows_out,
                "parts": r.parts,
                "bytes": r.bytes_written,
                "elapsed_seconds": round(r.elapsed_seconds, 2),
                "rows_with_borrower_name": r.rows_with_borrower_name,
                "rows_with_borrower_state": r.rows_with_borrower_state,
                "rows_with_borrower_zip": r.rows_with_borrower_zip,
                "rows_with_naics": r.rows_with_naics,
                "rows_with_initial_amount": r.rows_with_initial_amount,
                "rows_with_normalized_name": r.rows_with_normalized_name,
            } for r in results
        ],
        "failures": [{"segment": s, "error": str(e)[:200]} for s, e in failures],
    }
    print(json.dumps(summary, indent=2))
    return 1 if failures else 0


def _classify_error(exc: Exception) -> str:
    name = type(exc).__name__.lower()
    if any(k in name for k in ("connect", "timeout", "http")):
        return "download_failure"
    if "csv" in name or "decode" in name or "parse" in name:
        return "parse_failure"
    if "boto" in name or "client" in name or "endpoint" in name:
        return "r2_upload_failure"
    if "psycopg" in name or "operationalerror" in name or "interface" in name:
        return "db_failure"
    return "unknown"


if __name__ == "__main__":
    sys.exit(main())

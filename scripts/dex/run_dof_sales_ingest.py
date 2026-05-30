#!/usr/bin/env python3
"""NYC DOF Property Sales — Annualized + Rolling — XLSX/XLS ingest.

Lands per-borough per-year DOF property-sales files into
`entities.dof_annualized_sales` with `ops.dof_sales_ingest_runs` audit.

Subcommands:

    # Full historical backfill: walk every (year × borough) annualized file
    # we know about and ingest anything not already done.
    PYTHONPATH=. doppler run -- python scripts/run_dof_sales_ingest.py annualized-backfill

    # Single-file ingest (manual download or re-run).
    PYTHONPATH=. doppler run -- python scripts/run_dof_sales_ingest.py annualized \\
        --year 2024 --borough manhattan [--xlsx-path /path/to/file.xlsx]

    # Monthly rolling refresh — fetches all 5 borough rolling files.
    PYTHONPATH=. doppler run -- python scripts/run_dof_sales_ingest.py rolling-refresh

    # Single rolling file:
    PYTHONPATH=. doppler run -- python scripts/run_dof_sales_ingest.py rolling \\
        --borough manhattan [--xlsx-path /path/to/file.xlsx]

Notes
-----
* `.xlsx` (2018+) parsed with openpyxl read_only mode.
* `.xls` (2003–2017) parsed with xlrd 1.2 (the only version that still supports
  legacy BIFF). Pinned to <2.0 in requirements.
* Pre-2009 history lives in a single Open Data ZIP at uzf5-f8n2; the
  `annualized-backfill` subcommand auto-fetches and unpacks it on demand.
* Idempotent — re-running a file inserts zero new rows (ON CONFLICT DO NOTHING
  on the unique key).
* Pre-2003 data does not exist in DOF's public corpus.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import logging
import os
import re
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional

import httpx
import openpyxl
import psycopg
import xlrd
from psycopg import sql

LOG = logging.getLogger("dof_sales_ingest")

# ---------------------------------------------------------------------------
# Source-URL conventions

USER_AGENT = "data-engine-x dof-sales-ingest"

# Direct annualized URLs — keyed by (year, borough_code) → (url, ext).
# borough_code: 1=MN, 2=BX, 3=BK, 4=QN, 5=SI.
BOROUGH_NAME = {1: "manhattan", 2: "bronx", 3: "brooklyn", 4: "queens", 5: "staten_island"}
BOROUGH_CODE = {v: k for k, v in BOROUGH_NAME.items()}
BOROUGH_CODE.update({"si": 5, "statenisland": 5, "staten island": 5})

ANNUALIZED_DIRECT_BASE = "https://www.nyc.gov/assets/finance/downloads/pdf/rolling_sales/annualized-sales"
ROLLING_BASE = "https://www.nyc.gov/assets/finance/downloads/pdf/rolling_sales"

# Direct annualized coverage by URL pattern (verified 2026-04-29):
#   2009–2017  → .xls,  SI slug = statenisland
#   2018–2019  → .xlsx, SI slug = statenisland
#   2020–2025  → .xlsx, SI slug = staten_island
DIRECT_ANNUALIZED_YEARS = list(range(2009, 2026))

ARCHIVE_ZIP_URL = (
    "https://data.cityofnewyork.us/api/views/uzf5-f8n2/files/"
    "8d9ba1d7-9b7a-43de-b741-bd9a35839e03"
    "?download=true&filename=Annualized%20Rolling%20Sales%20Update.zip"
)
# Years available only via the archive ZIP.
ARCHIVE_ONLY_YEARS = [2003, 2004, 2005, 2006, 2007, 2008]


def annualized_borough_slug(year: int, code: int) -> str:
    """The borough slug used in the URL for a given year, accounting for SI flips."""
    if code != 5:
        return BOROUGH_NAME[code]
    if year >= 2020:
        return "staten_island"
    return "statenisland"


def annualized_url_and_ext(year: int, code: int) -> tuple[str, str]:
    slug = annualized_borough_slug(year, code)
    ext = "xlsx" if year >= 2018 else "xls"
    url = f"{ANNUALIZED_DIRECT_BASE}/{year}/{year}_{slug}.{ext}"
    return url, ext


def rolling_url(code: int) -> str:
    # All 5 boroughs present at this base; SI uses the no-underscore slug.
    slug = "statenisland" if code == 5 else BOROUGH_NAME[code]
    return f"{ROLLING_BASE}/rollingsales_{slug}.xlsx"


def archive_filename_for(year: int, code: int) -> str:
    """Filename inside the Open Data archive ZIP for a given year/borough.

    Naming inside the ZIP varies by era:
      2003–2006:  sales_<boro>_<2digit>.xls,  SI = "si"
      2007–2008:  sales_<year>_<boro>.xls,    SI = "statenisland"
      2009–2015:  <year>_<boro>.xls,          SI = "statenisland"
    """
    if code == 5:
        if year <= 2006:
            return f"sales_si_{str(year)[2:]}.xls"
        return f"sales_{year}_statenisland.xls" if year <= 2008 else f"{year}_statenisland.xls"
    name = BOROUGH_NAME[code]
    if year <= 2006:
        return f"sales_{name}_{str(year)[2:]}.xls"
    if year <= 2008:
        return f"sales_{year}_{name}.xls"
    return f"{year}_{name}.xls"


# ---------------------------------------------------------------------------
# Parsing

CANONICAL_COLUMNS = [
    "borough",
    "neighborhood",
    "building_class_category_raw",
    "tax_class_at_present",
    "block",
    "lot",
    "easement",
    "building_class_at_present",
    "address",
    "apartment_number",
    "zip5",
    "residential_units",
    "commercial_units",
    "total_units",
    "land_square_feet",
    "gross_square_feet",
    "year_built",
    "tax_class_at_time_of_sale",
    "building_class_at_time_of_sale",
    "sale_price",
    "sale_date",
]
assert len(CANONICAL_COLUMNS) == 21


def _norm_header(s: str) -> str:
    """Collapse a raw header label to a comparison key."""
    return re.sub(r"\s+", " ", str(s).strip().upper())


# Maps every header-label variant observed across 2003–2025 + rolling to its
# canonical-column position (0-indexed). Order in source files is stable, but
# we still match by label name to be defensive against silent column reorders.
HEADER_LABEL_MAP: dict[str, str] = {
    "BOROUGH": "borough",
    "NEIGHBORHOOD": "neighborhood",
    "BUILDING CLASS CATEGORY": "building_class_category_raw",
    "TAX CLASS AT PRESENT": "tax_class_at_present",
    # 2010–2023 used "AS OF FINAL ROLL <YY/YY>" — match the prefix.
    "TAX CLASS AS OF FINAL ROLL": "tax_class_at_present",
    "BLOCK": "block",
    "LOT": "lot",
    "EASE-MENT": "easement",
    "EASEMENT": "easement",
    "BUILDING CLASS AT PRESENT": "building_class_at_present",
    "BUILDING CLASS AS OF FINAL ROLL": "building_class_at_present",
    "ADDRESS": "address",
    "APARTMENT NUMBER": "apartment_number",
    "ZIP CODE": "zip5",
    "RESIDENTIAL UNITS": "residential_units",
    "COMMERCIAL UNITS": "commercial_units",
    "TOTAL UNITS": "total_units",
    "LAND SQUARE FEET": "land_square_feet",
    "GROSS SQUARE FEET": "gross_square_feet",
    "YEAR BUILT": "year_built",
    "TAX CLASS AT TIME OF SALE": "tax_class_at_time_of_sale",
    "BUILDING CLASS AT TIME OF SALE": "building_class_at_time_of_sale",
    "SALE PRICE": "sale_price",
    "SALE DATE": "sale_date",
}


def _resolve_header(label: str) -> Optional[str]:
    n = _norm_header(label)
    if n in HEADER_LABEL_MAP:
        return HEADER_LABEL_MAP[n]
    # Prefix-match for "AS OF FINAL ROLL <YY/YY>" variants
    for prefix, col in HEADER_LABEL_MAP.items():
        if n.startswith(prefix):
            return col
    return None


@dataclass
class ParsedFile:
    """In-memory representation of a parsed DOF sales workbook."""

    rows: list[dict]
    source_published_at: dt.date
    detected_columns: dict[int, str]


_BANNER_DATE_RE = re.compile(r"(?:as of|All Sales From)\s+([A-Za-z0-9/\-, ]+)", re.I)
_DOWNLOAD_DATE_FALLBACK = dt.date.today  # captured at call time


def _parse_banner_date(banner_text: str) -> Optional[dt.date]:
    m = _BANNER_DATE_RE.search(banner_text)
    if not m:
        return None
    raw = m.group(1).strip().rstrip(".")
    for fmt in ("%m/%d/%Y", "%m-%d-%Y", "%B %Y", "%B %d, %Y"):
        try:
            return dt.datetime.strptime(raw[: len(fmt) + 4], fmt).date()
        except ValueError:
            continue
    return None


def _coerce_int(v) -> Optional[int]:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int,)):
        return int(v)
    if isinstance(v, float):
        if v != v:  # NaN
            return None
        return int(v)
    s = str(v).strip()
    if not s:
        return None
    # ".0" tail from xls float-coercion; strip
    if s.endswith(".0"):
        s = s[:-2]
    try:
        return int(s)
    except ValueError:
        try:
            return int(float(s))
        except ValueError:
            return None


def _coerce_smallint(v) -> Optional[int]:
    return _coerce_int(v)


def _coerce_numeric(v) -> Optional[str]:
    """Return canonical numeric form as string (preserves precision in sha)."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        if isinstance(v, float) and v != v:
            return None
        # Format: integer if integral, else strip trailing zeros after decimal
        if float(v).is_integer():
            return str(int(v))
        return f"{v:.6f}".rstrip("0").rstrip(".")
    s = str(v).strip()
    if not s:
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    if f.is_integer():
        return str(int(f))
    return f"{f:.6f}".rstrip("0").rstrip(".")


def _coerce_text(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _coerce_tax_class(v) -> Optional[str]:
    """Tax-class tokens come through xlrd as '2.0' (float-coerced) for plain
    numeric classes, and as '2A' / '2B' strings for sub-classes. Strip the
    trailing '.0' so '2.0' → '2' but '2B' stays '2B'."""
    s = _coerce_text(v)
    if s is None:
        return None
    if re.fullmatch(r"\d+\.0+", s):
        return s.split(".")[0]
    return s


def _coerce_zip5(v) -> Optional[str]:
    s = _coerce_text(v)
    if s is None:
        return None
    if s.endswith(".0"):
        s = s[:-2]
    s = re.sub(r"[^0-9]", "", s)
    if not s:
        return None
    if len(s) > 5:
        s = s[:5]
    return s.zfill(5) if len(s) >= 4 else None


def _coerce_date_xlsx(v) -> Optional[dt.date]:
    if v is None or v == "":
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    s = str(v).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(s.split(".")[0], fmt).date()
        except ValueError:
            continue
    return None


def _coerce_date_xls(v, datemode: int) -> Optional[dt.date]:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        try:
            return xlrd.xldate.xldate_as_datetime(float(v), datemode).date()
        except (xlrd.xldate.XLDateError, ValueError):
            return None
    return _coerce_date_xlsx(v)


# ---------------------------------------------------------------------------

INT_COLS = {"borough", "block", "lot", "residential_units", "commercial_units",
            "total_units", "year_built"}
NUM_COLS = {"land_square_feet", "gross_square_feet", "sale_price"}
TAX_CLASS_COLS = {"tax_class_at_present", "tax_class_at_time_of_sale"}
TXT_COLS = {"neighborhood", "building_class_category_raw",
            "easement", "building_class_at_present", "address", "apartment_number",
            "building_class_at_time_of_sale"}


def _coerce_row(raw: dict, *, is_xls: bool, datemode: int = 0) -> dict:
    out = {}
    for col, raw_v in raw.items():
        if col == "zip5":
            out[col] = _coerce_zip5(raw_v)
        elif col == "sale_date":
            out[col] = _coerce_date_xls(raw_v, datemode) if is_xls else _coerce_date_xlsx(raw_v)
        elif col in INT_COLS:
            out[col] = _coerce_int(raw_v)
        elif col in NUM_COLS:
            out[col] = _coerce_numeric(raw_v)
        elif col in TAX_CLASS_COLS:
            out[col] = _coerce_tax_class(raw_v)
        elif col in TXT_COLS:
            out[col] = _coerce_text(raw_v)
        else:
            out[col] = raw_v
    return out


def _row_sha256(row: dict) -> str:
    """SHA-256 over canonicalized (borough, block, lot, sale_date, sale_price,
    address, apartment_number, building_class_at_time_of_sale)."""
    parts = [
        "" if row.get("borough") is None else str(row["borough"]),
        "" if row.get("block") is None else str(row["block"]),
        "" if row.get("lot") is None else str(row["lot"]),
        row["sale_date"].isoformat() if row.get("sale_date") else "",
        row.get("sale_price") or "",
        row.get("address") or "",
        row.get("apartment_number") or "",
        row.get("building_class_at_time_of_sale") or "",
    ]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def _detect_header_row(rows: Iterable[list], max_scan: int = 12) -> Optional[int]:
    for i, r in enumerate(rows):
        if i >= max_scan:
            break
        if not r:
            continue
        first = r[0]
        if first is None:
            continue
        if _norm_header(first) == "BOROUGH":
            return i
    return None


def _build_column_index(header_row: list) -> dict[int, str]:
    out = {}
    for idx, label in enumerate(header_row):
        if label is None:
            continue
        col = _resolve_header(label)
        if col is not None:
            out[idx] = col
    return out


def _row_to_dict(row: list, col_idx: dict[int, str]) -> Optional[dict]:
    raw = {}
    for idx, col in col_idx.items():
        raw[col] = row[idx] if idx < len(row) else None
    # Skip obvious blank rows (no borough, no block, no address)
    if not any(v not in (None, "", " ") for v in (raw.get("borough"), raw.get("block"),
                                                   raw.get("address"))):
        return None
    return raw


def parse_xlsx(path: Path) -> ParsedFile:
    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    try:
        ws = wb.active
        all_rows: list[list] = []
        # Header detection scans the first 12 rows; we need them in memory for that
        scan_buf = []
        row_iter = ws.iter_rows(values_only=True)
        for i, row in enumerate(row_iter):
            scan_buf.append(list(row))
            if i >= 11:
                break
        header_idx = _detect_header_row(scan_buf)
        if header_idx is None:
            raise ValueError(f"could not locate BOROUGH header in {path}")
        # Banner text = rows above the header concatenated
        banner = " | ".join(
            " ".join(str(c) for c in r if c is not None) for r in scan_buf[:header_idx]
        )
        published = _parse_banner_date(banner) or dt.date.today()
        col_idx = _build_column_index(scan_buf[header_idx])
        # Continue iterating where we left off; first replay any buffered post-header
        rows: list[dict] = []
        for r in scan_buf[header_idx + 1:]:
            d = _row_to_dict(r, col_idx)
            if d is None:
                continue
            rows.append(_coerce_row(d, is_xls=False))
        for r in row_iter:
            d = _row_to_dict(list(r), col_idx)
            if d is None:
                continue
            rows.append(_coerce_row(d, is_xls=False))
        return ParsedFile(rows=rows, source_published_at=published, detected_columns=col_idx)
    finally:
        wb.close()


def parse_xls(path: Path) -> ParsedFile:
    wb = xlrd.open_workbook(str(path), on_demand=True)
    try:
        ws = wb.sheet_by_index(0)
        scan_buf = [ws.row_values(i) for i in range(min(12, ws.nrows))]
        header_idx = _detect_header_row(scan_buf)
        if header_idx is None:
            raise ValueError(f"could not locate BOROUGH header in {path}")
        banner = " | ".join(
            " ".join(str(c) for c in r if c not in (None, "")) for r in scan_buf[:header_idx]
        )
        published = _parse_banner_date(banner) or dt.date.today()
        col_idx = _build_column_index(scan_buf[header_idx])
        rows: list[dict] = []
        for r_i in range(header_idx + 1, ws.nrows):
            r = ws.row_values(r_i)
            d = _row_to_dict(r, col_idx)
            if d is None:
                continue
            rows.append(_coerce_row(d, is_xls=True, datemode=wb.datemode))
        return ParsedFile(rows=rows, source_published_at=published, detected_columns=col_idx)
    finally:
        wb.release_resources()


def parse_file(path: Path) -> ParsedFile:
    if path.suffix.lower() == ".xlsx":
        return parse_xlsx(path)
    return parse_xls(path)


# ---------------------------------------------------------------------------
# Persistence

INSERT_COLUMNS = [
    "source_file_type", "source_year", "source_borough_code",
    "source_filename", "source_url", "source_published_at",
    "borough", "block", "lot",
    "neighborhood", "building_class_category_raw", "tax_class_at_present",
    "building_class_at_present", "address", "apartment_number", "zip5",
    "residential_units", "commercial_units", "total_units",
    "land_square_feet", "gross_square_feet", "year_built",
    "tax_class_at_time_of_sale", "building_class_at_time_of_sale",
    "sale_price", "sale_date", "easement",
    "row_sha256",
]


def insert_rows(
    conn: psycopg.Connection,
    *,
    source_file_type: str,
    source_year: Optional[int],
    source_borough_code: int,
    source_filename: str,
    source_url: Optional[str],
    source_published_at: dt.date,
    parsed: ParsedFile,
    chunk_size: int = 1000,
) -> tuple[int, int]:
    """Insert parsed rows. Returns (rows_seen, rows_inserted)."""
    rows_seen = 0
    rows_inserted = 0
    placeholders = "(" + ",".join(["%s"] * len(INSERT_COLUMNS)) + ")"
    base_stmt = (
        f"INSERT INTO entities.dof_annualized_sales ({', '.join(INSERT_COLUMNS)}) "
        f"VALUES %s ON CONFLICT (source_file_type, source_year, source_borough_code, row_sha256) DO NOTHING"
    )
    # psycopg3 lacks execute_values; we batch with executemany + RETURNING-less path
    with conn.cursor() as cur:
        buffer: list[tuple] = []
        for r in parsed.rows:
            rows_seen += 1
            sha = _row_sha256(r)
            if r.get("borough") is None or r.get("block") is None or r.get("lot") is None:
                # Required columns missing; skip silently — these are blank-ish
                # rows DOF leaves at the bottom of some files.
                continue
            buffer.append((
                source_file_type,
                source_year,
                source_borough_code,
                source_filename,
                source_url,
                source_published_at,
                r["borough"], r["block"], r["lot"],
                r.get("neighborhood"),
                r.get("building_class_category_raw"),
                r.get("tax_class_at_present"),
                r.get("building_class_at_present"),
                r.get("address"),
                r.get("apartment_number"),
                r.get("zip5"),
                r.get("residential_units"),
                r.get("commercial_units"),
                r.get("total_units"),
                r.get("land_square_feet"),
                r.get("gross_square_feet"),
                r.get("year_built"),
                r.get("tax_class_at_time_of_sale"),
                r.get("building_class_at_time_of_sale"),
                r.get("sale_price"),
                r.get("sale_date"),
                r.get("easement"),
                sha,
            ))
            if len(buffer) >= chunk_size:
                rows_inserted += _flush(cur, buffer)
                buffer.clear()
        if buffer:
            rows_inserted += _flush(cur, buffer)
    return rows_seen, rows_inserted


def _flush(cur: psycopg.Cursor, buffer: list[tuple]) -> int:
    """Multi-row INSERT ... ON CONFLICT DO NOTHING. Returns rows actually inserted."""
    placeholder = "(" + ",".join(["%s"] * len(INSERT_COLUMNS)) + ")"
    values_sql = ",".join([placeholder] * len(buffer))
    flat: list = []
    for row in buffer:
        flat.extend(row)
    stmt = (
        f"INSERT INTO entities.dof_annualized_sales ({', '.join(INSERT_COLUMNS)}) "
        f"VALUES {values_sql} ON CONFLICT (source_file_type, source_year, source_borough_code, row_sha256) DO NOTHING"
    )
    cur.execute(stmt, flat)
    # rowcount reflects rows actually inserted (skipped conflicts not counted)
    return cur.rowcount or 0


# ---------------------------------------------------------------------------
# Audit-table helpers

def audit_start(conn: psycopg.Connection, **kw) -> str:
    """Insert an ingest-run row in `running` state. Returns the run_id (uuid str).

    On unique-key conflict (rerun of an existing attempt), bumps `attempt`
    until a free slot is found.
    """
    stmt = """
    INSERT INTO ops.dof_sales_ingest_runs
      (source_file_type, source_year, source_borough_code, source_filename,
       source_url, attempt, status, started_at)
    VALUES (%(source_file_type)s, %(source_year)s, %(source_borough_code)s,
            %(source_filename)s, %(source_url)s, %(attempt)s, 'running', now())
    RETURNING id::text
    """
    with conn.cursor() as cur:
        kw = dict(kw)
        kw.setdefault("attempt", 1)
        for _ in range(20):
            try:
                cur.execute(stmt, kw)
                row = cur.fetchone()
                conn.commit()
                return row[0]
            except psycopg.errors.UniqueViolation:
                conn.rollback()
                kw["attempt"] += 1
        raise RuntimeError("could not allocate audit-run attempt slot")


def audit_finish(conn: psycopg.Connection, run_id: str, *, status: str,
                 rows_seen: int = 0, rows_inserted: int = 0,
                 bytes_downloaded: Optional[int] = None,
                 source_etag: Optional[str] = None,
                 source_last_modified: Optional[str] = None,
                 error_class: Optional[str] = None,
                 error_message: Optional[str] = None) -> None:
    stmt = """
    UPDATE ops.dof_sales_ingest_runs
       SET status = %(status)s,
           completed_at = now(),
           duration_seconds = EXTRACT(EPOCH FROM (now() - started_at)),
           rows_seen = %(rows_seen)s,
           rows_inserted = %(rows_inserted)s,
           rows_skipped = %(rows_seen)s - %(rows_inserted)s,
           bytes_downloaded = %(bytes_downloaded)s,
           source_etag = COALESCE(%(source_etag)s, source_etag),
           source_last_modified = COALESCE(%(source_last_modified)s, source_last_modified),
           error_class = %(error_class)s,
           error_message = %(error_message)s
     WHERE id = %(run_id)s
    """
    with conn.cursor() as cur:
        cur.execute(stmt, dict(
            run_id=run_id, status=status, rows_seen=rows_seen, rows_inserted=rows_inserted,
            bytes_downloaded=bytes_downloaded, source_etag=source_etag,
            source_last_modified=source_last_modified, error_class=error_class,
            error_message=(error_message or "")[:4000],
        ))
        conn.commit()


def already_completed(conn: psycopg.Connection, *, source_file_type: str,
                      source_year: Optional[int], source_borough_code: int) -> bool:
    stmt = """
    SELECT 1 FROM ops.dof_sales_ingest_runs
     WHERE source_file_type = %s
       AND source_year IS NOT DISTINCT FROM %s
       AND source_borough_code = %s
       AND status = 'completed'
     LIMIT 1
    """
    with conn.cursor() as cur:
        cur.execute(stmt, (source_file_type, source_year, source_borough_code))
        return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# Download

def download_file(url: str, dest: Path) -> tuple[int, dict]:
    headers = {"User-Agent": USER_AGENT}
    with httpx.stream("GET", url, headers=headers, follow_redirects=True, timeout=120.0) as r:
        r.raise_for_status()
        total = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes(chunk_size=1 << 16):
                if chunk:
                    f.write(chunk)
                    total += len(chunk)
        meta = {
            "etag": r.headers.get("etag"),
            "last_modified": r.headers.get("last-modified"),
        }
    return total, meta


# ---------------------------------------------------------------------------
# Orchestration

def db_connect() -> psycopg.Connection:
    url = os.environ.get("DEX_DB_URL_POOLED") or os.environ.get("DEX_DB_URL_DIRECT")
    if not url:
        raise RuntimeError("DEX_DB_URL_POOLED / DEX_DB_URL_DIRECT not set")
    return psycopg.connect(url, autocommit=False)


def ingest_one(
    conn: psycopg.Connection,
    *,
    file_path: Path,
    source_file_type: str,
    source_year: Optional[int],
    source_borough_code: int,
    source_filename: str,
    source_url: Optional[str],
    bytes_downloaded: Optional[int] = None,
    source_etag: Optional[str] = None,
    source_last_modified: Optional[str] = None,
) -> tuple[int, int]:
    run_id = audit_start(conn,
                         source_file_type=source_file_type,
                         source_year=source_year,
                         source_borough_code=source_borough_code,
                         source_filename=source_filename,
                         source_url=source_url)
    try:
        parsed = parse_file(file_path)
        rows_seen, rows_inserted = insert_rows(
            conn,
            source_file_type=source_file_type,
            source_year=source_year,
            source_borough_code=source_borough_code,
            source_filename=source_filename,
            source_url=source_url,
            source_published_at=parsed.source_published_at,
            parsed=parsed,
        )
        conn.commit()
        audit_finish(conn, run_id, status="completed",
                     rows_seen=rows_seen, rows_inserted=rows_inserted,
                     bytes_downloaded=bytes_downloaded,
                     source_etag=source_etag,
                     source_last_modified=source_last_modified)
        LOG.info("ingest_completed",
                 extra={"file": source_filename, "rows_seen": rows_seen,
                        "rows_inserted": rows_inserted})
        return rows_seen, rows_inserted
    except Exception as e:
        conn.rollback()
        audit_finish(conn, run_id, status="failed",
                     error_class="parse_failure" if isinstance(e, ValueError) else "db_failure",
                     error_message=repr(e))
        raise


def cmd_annualized_single(args):
    code = BOROUGH_CODE[args.borough.lower()]
    url, ext = annualized_url_and_ext(args.year, code)
    fname = f"{args.year}_{annualized_borough_slug(args.year, code)}.{ext}"
    if args.xlsx_path:
        path = Path(args.xlsx_path)
        bytes_downloaded = path.stat().st_size
        meta = {}
    else:
        with tempfile.NamedTemporaryFile(prefix=f"dof_{args.year}_", suffix=f".{ext}", delete=False) as tf:
            path = Path(tf.name)
        bytes_downloaded, meta = download_file(url, path)
    try:
        with db_connect() as conn:
            ingest_one(conn,
                       file_path=path,
                       source_file_type="annualized",
                       source_year=args.year,
                       source_borough_code=code,
                       source_filename=fname,
                       source_url=url,
                       bytes_downloaded=bytes_downloaded,
                       source_etag=meta.get("etag"),
                       source_last_modified=meta.get("last_modified"))
    finally:
        if not args.xlsx_path and path.exists():
            path.unlink()


def cmd_rolling_single(args):
    code = BOROUGH_CODE[args.borough.lower()]
    url = rolling_url(code)
    slug = "statenisland" if code == 5 else BOROUGH_NAME[code]
    fname = f"rollingsales_{slug}.xlsx"
    if args.xlsx_path:
        path = Path(args.xlsx_path)
        bytes_downloaded = path.stat().st_size
        meta = {}
    else:
        with tempfile.NamedTemporaryFile(prefix="dof_rolling_", suffix=".xlsx", delete=False) as tf:
            path = Path(tf.name)
        bytes_downloaded, meta = download_file(url, path)
    try:
        with db_connect() as conn:
            ingest_one(conn,
                       file_path=path,
                       source_file_type="rolling",
                       source_year=None,
                       source_borough_code=code,
                       source_filename=fname,
                       source_url=url,
                       bytes_downloaded=bytes_downloaded,
                       source_etag=meta.get("etag"),
                       source_last_modified=meta.get("last_modified"))
    finally:
        if not args.xlsx_path and path.exists():
            path.unlink()


def cmd_rolling_refresh(args):
    for code in (1, 2, 3, 4, 5):
        url = rolling_url(code)
        slug = "statenisland" if code == 5 else BOROUGH_NAME[code]
        fname = f"rollingsales_{slug}.xlsx"
        with tempfile.NamedTemporaryFile(prefix="dof_rolling_", suffix=".xlsx", delete=False) as tf:
            path = Path(tf.name)
        try:
            try:
                bytes_downloaded, meta = download_file(url, path)
            except httpx.HTTPError as e:
                LOG.error("rolling_download_failed", extra={"borough": slug, "url": url, "err": str(e)})
                continue
            with db_connect() as conn:
                try:
                    ingest_one(conn,
                               file_path=path,
                               source_file_type="rolling",
                               source_year=None,
                               source_borough_code=code,
                               source_filename=fname,
                               source_url=url,
                               bytes_downloaded=bytes_downloaded,
                               source_etag=meta.get("etag"),
                               source_last_modified=meta.get("last_modified"))
                except Exception as e:
                    LOG.error("rolling_ingest_failed",
                              extra={"borough": slug, "err": repr(e)})
        finally:
            if path.exists():
                path.unlink()


def _ensure_archive_zip(cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / "dof_pre_2009_archive.zip"
    if dest.exists() and dest.stat().st_size > 50_000_000:
        return dest
    LOG.info("archive_download_start", extra={"url": ARCHIVE_ZIP_URL})
    download_file(ARCHIVE_ZIP_URL, dest)
    return dest


def cmd_annualized_backfill(args):
    cache = Path(args.cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    archive_zip = None  # lazy

    with db_connect() as conn:
        # 1) Direct-URL years (2009–2025)
        for year in DIRECT_ANNUALIZED_YEARS:
            for code in (1, 2, 3, 4, 5):
                if already_completed(conn, source_file_type="annualized",
                                     source_year=year, source_borough_code=code):
                    LOG.info("backfill_skip_completed",
                             extra={"year": year, "borough": BOROUGH_NAME[code]})
                    continue
                url, ext = annualized_url_and_ext(year, code)
                fname = f"{year}_{annualized_borough_slug(year, code)}.{ext}"
                local = cache / fname
                from_archive = False
                try:
                    bytes_downloaded, meta = download_file(url, local)
                except httpx.HTTPError as e:
                    # Direct URL 404s for some borough×year combinations
                    # (notably 2009 BX/BK/QN/SI). Fall back to the archive ZIP
                    # for years it covers (≤2015).
                    if year <= 2015:
                        LOG.info("backfill_archive_fallback",
                                 extra={"year": year, "borough": BOROUGH_NAME[code],
                                        "direct_url": url, "err": str(e)})
                        if archive_zip is None:
                            archive_zip = _ensure_archive_zip(cache)
                        inner_name = archive_filename_for(year, code)
                        try:
                            with zipfile.ZipFile(archive_zip) as zf:
                                if inner_name not in zf.namelist():
                                    LOG.error("archive_member_missing",
                                              extra={"year": year,
                                                     "borough": BOROUGH_NAME[code],
                                                     "name": inner_name})
                                    continue
                                with zf.open(inner_name) as src, open(local, "wb") as dst:
                                    dst.write(src.read())
                            fname = inner_name
                            url = ARCHIVE_ZIP_URL
                            bytes_downloaded = local.stat().st_size
                            meta = {}
                            from_archive = True
                        except Exception as e2:
                            LOG.error("archive_fallback_failed",
                                      extra={"year": year, "borough": BOROUGH_NAME[code],
                                             "err": repr(e2)})
                            continue
                    else:
                        LOG.error("backfill_download_failed",
                                  extra={"year": year, "borough": BOROUGH_NAME[code],
                                         "url": url, "err": str(e)})
                        continue
                try:
                    ingest_one(conn,
                               file_path=local,
                               source_file_type="annualized",
                               source_year=year,
                               source_borough_code=code,
                               source_filename=fname,
                               source_url=url,
                               bytes_downloaded=bytes_downloaded,
                               source_etag=meta.get("etag"),
                               source_last_modified=meta.get("last_modified"))
                except Exception as e:
                    LOG.error("backfill_ingest_failed",
                              extra={"year": year, "borough": BOROUGH_NAME[code],
                                     "err": repr(e)})
                finally:
                    if not args.keep_cache and local.exists():
                        local.unlink()

        # 2) Archive-only years (2003–2008)
        for year in ARCHIVE_ONLY_YEARS:
            for code in (1, 2, 3, 4, 5):
                if already_completed(conn, source_file_type="annualized",
                                     source_year=year, source_borough_code=code):
                    LOG.info("backfill_skip_completed",
                             extra={"year": year, "borough": BOROUGH_NAME[code]})
                    continue
                if archive_zip is None:
                    archive_zip = _ensure_archive_zip(cache)
                inner_name = archive_filename_for(year, code)
                with zipfile.ZipFile(archive_zip) as zf:
                    if inner_name not in zf.namelist():
                        LOG.error("archive_member_missing",
                                  extra={"year": year, "borough": BOROUGH_NAME[code],
                                         "name": inner_name})
                        continue
                    local = cache / inner_name
                    with zf.open(inner_name) as src, open(local, "wb") as dst:
                        dst.write(src.read())
                try:
                    ingest_one(conn,
                               file_path=local,
                               source_file_type="annualized",
                               source_year=year,
                               source_borough_code=code,
                               source_filename=inner_name,
                               source_url=ARCHIVE_ZIP_URL,
                               bytes_downloaded=local.stat().st_size)
                except Exception as e:
                    LOG.error("backfill_ingest_failed",
                              extra={"year": year, "borough": BOROUGH_NAME[code],
                                     "err": repr(e)})
                finally:
                    if not args.keep_cache and local.exists():
                        local.unlink()


# ---------------------------------------------------------------------------
# CLI

def main(argv=None):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    p = argparse.ArgumentParser(description="NYC DOF Property Sales ingest.")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_ann = sub.add_parser("annualized", help="Single annualized file (year+borough).")
    p_ann.add_argument("--year", type=int, required=True)
    p_ann.add_argument("--borough", required=True,
                       help="manhattan | bronx | brooklyn | queens | staten_island")
    p_ann.add_argument("--xlsx-path", help="Local file (skip download).")
    p_ann.set_defaults(func=cmd_annualized_single)

    p_rs = sub.add_parser("rolling", help="Single rolling file for one borough.")
    p_rs.add_argument("--borough", required=True)
    p_rs.add_argument("--xlsx-path", help="Local file (skip download).")
    p_rs.set_defaults(func=cmd_rolling_single)

    p_rr = sub.add_parser("rolling-refresh", help="Refresh all 5 rolling files.")
    p_rr.set_defaults(func=cmd_rolling_refresh)

    p_bf = sub.add_parser("annualized-backfill",
                          help="Walk every (year × borough) annualized file and ingest.")
    p_bf.add_argument("--cache-dir", default="/tmp/dof_sales_cache")
    p_bf.add_argument("--keep-cache", action="store_true",
                      help="Don't delete files after ingest (debug).")
    p_bf.set_defaults(func=cmd_annualized_backfill)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()

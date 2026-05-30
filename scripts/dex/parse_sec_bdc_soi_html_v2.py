#!/usr/bin/env python3
"""SEC BDC Schedule of Investments — v2 parse.

Column expansion + quality-gap fixes + audit instrumentation on top of v1's
sec-bdc/soi-parsed/.

PRIMARY source: sec-bdc/soi/release=<period>/data.parquet (225-col structured
TSV from SEC's BDC Data Sets — XBRL-tagged facts: principal, amortized_cost,
interest-rate components, investment_identifier, BDC registrant name). Use as
PRIMARY source where the column is populated — auto-tags
parse_confidence='verified_exact' (XBRL-tagged = no parser-confidence
ambiguity).

FALLBACK source: per-filing Inline-XBRL HTML via the soi.tsv `inlineurl`
column. Reuses v1's parser primitives (parse_sec_bdc_soi_html.py):
  - _resolve_filing_url      — strips /ix?doc= viewer prefix
  - _build_textblock_html    — concatenates ix:nonNumeric anchor + ix:continuation
                               chain via continuedAt, HTML-unescapes
  - colspan→logical-grid     — header/data column alignment (Golub-style
                               misalignment fix)
  - _DATE_TOKEN_RE / _MONTH_YEAR_RE / _normalize_date — heterogeneous-format dates
Invoked when (a) soi.tsv lacks a value for a required v2 column AND (b) the row
appears in the SOI HTML table at all (HTML-only filers like Ares/Blue
Owl/Blackstone/Golub).

Output: ZSTD Parquet to R2 sec-bdc/soi-parsed-v2/release=<period>/data.parquet.
Schema per directive §"Schema — NEW columns to extract":
  adsh, cik, name (BDC registrant),
  portfolio_company_name, portfolio_company_name_clean,
  portfolio_company_name_normalized,
  instrument_type, is_debt_instrument,
  principal (double), principal_raw,
  amortized_cost (double), amortized_cost_raw,
  investment_interest_rate_raw,
  investment_interest_rate_base, investment_interest_rate_spread_bps (int),
  investment_interest_rate_floor_bps (int), investment_interest_rate_pik_bps (int),
  investment_identifier, fair_value, acquisition_date,
  maturity_date_raw, maturity_date_typed (DATE — NULL for non-debt),
  parse_confidence (string enum),
  parse_demotion_reason (string, pipe-joined codes when multiple fire),
  demoted_by_rule_ids (string, pipe-joined IDs),
  source_filing_url, source_html_content_hash,
  parser_version, extracted_at, inlineurl, release.

Audit-ledger writes to ops.bdc_soi_parsed_v2_runs (status pending → running →
completed/failed/no_change/skipped/dry_run).

L54 Lance compatibility: parse_demotion_reason + demoted_by_rule_ids are
pipe-joined VARCHAR (NOT array) so the downstream Lance re-emission cycle
adopts the schema verbatim.

CLI mirrors v1:
  --apply / --dry-run
  --periods 2025q1,...           # subset
  --out-prefix sec-bdc/soi-parsed-v2  # default; overridable for e5 determinism check
  --max-filings N
  --skip-if-unchanged            # checks ops.bdc_soi_parsed_v2_runs for prior 'completed'

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/parse_sec_bdc_soi_html_v2.py --apply --periods 2025q1
"""
from __future__ import annotations

__version__ = "v2.0.0"

import argparse
import hashlib
import html as htmlmod
import io
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

import boto3
import duckdb
import httpx
import psycopg

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:
    pa = None  # type: ignore
    pq = None  # type: ignore

try:
    from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
    import warnings
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
except ImportError:
    BeautifulSoup = None  # type: ignore

# Import classifier (s4)
try:
    from scripts._lib.bdc_soi_classifier import (
        classify_name,
        classify_maturity_date,
        classify_principal,
        classify_interest_rate,
        classify_cusip,
        classify_instrument_type,
        classify_amortized_cost,
        detect_sentinel,
        pipe_join,
    )
except ImportError:
    # When running from repo root with sys.path insert (Modal)
    from _lib.bdc_soi_classifier import (  # type: ignore
        classify_name,
        classify_maturity_date,
        classify_principal,
        classify_interest_rate,
        classify_cusip,
        classify_instrument_type,
        classify_amortized_cost,
        detect_sentinel,
        pipe_join,
    )

LOG = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)

USER_AGENT = "Mozilla/5.0 (compatible; data-engine-x/1.0; +tools@substrate.build)"
R2_BUCKET = "dex-raw-landing-zone"
R2_SOI_SOURCE_PREFIX = "sec-bdc/soi"
R2_PARSED_V2_PREFIX = "sec-bdc/soi-parsed-v2"
R2_V1_PARSED_PREFIX = "sec-bdc/soi-parsed"

# soi.tsv structured-column → v2-output-column mapping (validator finding #1).
# These XBRL-tagged columns are PRIMARY source; HTML is FALLBACK.
SOI_TSV_COLUMN_MAP: dict[str, str] = {
    "Investment Owned, Balance, Principal Amount": "principal_raw_structured",
    "Investment Owned, Cost": "amortized_cost_raw_structured",
    "Investment Interest Rate": "investment_interest_rate_raw",
    "Investment, Basis Spread, Variable Rate": "investment_interest_rate_spread_raw",
    "Investment, Interest Rate, Floor": "investment_interest_rate_floor_raw",
    "Investment, Interest Rate, Paid in Kind": "investment_interest_rate_pik_raw",
    "Investment, Interest Rate, Paid in Cash": "investment_interest_rate_cash_raw",
    "Investment, Identifier Axis": "investment_identifier_raw",
    "Investment, Issuer Name Axis": "issuer_name_axis",
    "Investment, Issuer Name [Extensible Enumeration]": "issuer_name_ext",
    "name": "name",  # top-level BDC registrant name
    "adsh": "adsh",
    "cik": "cik",
    "inlineurl": "inlineurl",
    "portfolio_company_name": "portfolio_company_name",
    "instrument_type": "instrument_type",
    "fair_value": "fair_value",
    "maturity_date": "maturity_date_v1",  # keep for v1 comparison (e6)
    "acquisition_date": "acquisition_date",
    "business_description": "business_description",
}

# The SOI TextBlock ix element names (escape="true" continued-chain variant).
_SOI_TEXTBLOCK_NAMES = (
    "us-gaap:InvestmentHoldingsScheduleOfInvestmentsTextBlock",
    "us-gaap:InvestmentHoldingsScheduleOfInvestmentsTableTextBlock",
)

# Date parsing (verbatim from v1)
_DATE_TOKEN_RE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{2,4}|\d{1,2}/\d{2,4})\b")
_MONTH_YEAR_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+(\d{4})\b",
    re.IGNORECASE,
)
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

# Holdings-table header cell hints
_HDR_MATURITY = re.compile(r"maturity", re.IGNORECASE)
_HDR_COMPANY = re.compile(r"(company|portfolio|issuer)", re.IGNORECASE)
_HDR_INSTRUMENT = re.compile(
    r"(investment|instrument|type of investment|security)", re.IGNORECASE
)
_HDR_FAIR_VALUE = re.compile(r"fair\s*value", re.IGNORECASE)
_HDR_PRINCIPAL = re.compile(r"(principal|par\s*value)", re.IGNORECASE)
_HDR_AMORT_COST = re.compile(r"(amortized\s*cost|cost)", re.IGNORECASE)
_HDR_INTEREST = re.compile(r"(interest\s*rate|coupon|rate)", re.IGNORECASE)
_HDR_IDENTIFIER = re.compile(r"(cusip|identifier|id)", re.IGNORECASE)

# Instrument-type keywords for holdings-table detection
_INSTRUMENT_HINT = re.compile(
    r"(?i)\b(lien|loan|note|bond|debt|term|revolver|equity|warrant|preferred|"
    r"common|unit|subordinat|mezzanine|delayed draw|unitranche)\b"
)


# ── HTTP ──────────────────────────────────────────────────────────────────────

def _make_client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
        timeout=180.0,
    )


def _resolve_filing_url(inlineurl: str) -> str:
    """Strip the Inline-XBRL /ix?doc= viewer prefix to get the raw filing URL."""
    u = (inlineurl or "").strip()
    m = re.search(r"[?&]doc=(/?[^&]+)", u)
    if m:
        doc = m.group(1)
        if not doc.startswith("/"):
            doc = "/" + doc
        return "https://www.sec.gov" + doc
    return u


# ── Inline-XBRL continuation-chain reconstruction (verbatim from v1) ─────────

def _element_inner(html: str, tagname: str, fid: str) -> tuple[str | None, str | None]:
    """Return (inner_html, continuedAt) for the <tagname id="fid"> element."""
    open_at_id = re.compile(
        r"<" + re.escape(tagname) + r"\b[^>]*\bid=\"" + re.escape(fid) + r"\"[^>]*>"
    )
    om = open_at_id.search(html)
    if not om:
        return None, None
    depth = 1
    i = om.end()
    opentag = re.compile(r"<" + re.escape(tagname) + r"\b", re.IGNORECASE)
    closetag = re.compile(r"</" + re.escape(tagname) + r"\s*>", re.IGNORECASE)
    while depth > 0 and i < len(html):
        no = opentag.search(html, i)
        nc = closetag.search(html, i)
        if nc is None:
            break
        if no is not None and no.start() < nc.start():
            depth += 1
            i = no.end()
        else:
            depth -= 1
            i = nc.end()
            if depth == 0:
                cont = re.search(r'\bcontinuedAt="([^"]+)"', om.group(0))
                return html[om.end():nc.start()], (cont.group(1) if cont else None)
    return None, None


def _build_textblock_html(doc: str) -> str:
    """Reconstruct the full SOI TextBlock HTML via continuedAt chain."""
    out_parts: list[str] = []
    for name in _SOI_TEXTBLOCK_NAMES:
        anchor = re.search(
            r'<ix:nonNumeric\b[^>]*\bname="' + re.escape(name) + r'"[^>]*>',
            doc,
        )
        if not anchor:
            continue
        fid_m = re.search(r'\bid="([^"]+)"', anchor.group(0))
        if not fid_m:
            continue
        fid = fid_m.group(1)
        inner, nxt = _element_inner(doc, "ix:nonNumeric", fid)
        parts = [inner]
        seen = {fid}
        while nxt and nxt not in seen:
            seen.add(nxt)
            cinner, cnxt = _element_inner(doc, "ix:continuation", nxt)
            if cinner is None:
                break
            parts.append(cinner)
            nxt = cnxt
        out_parts.append("".join(p for p in parts if p))
    return htmlmod.unescape("".join(out_parts))


# ── Date parsing (verbatim from v1) ──────────────────────────────────────────

def _normalize_date(raw: str) -> str | None:
    """Normalize a heterogeneous date token to ISO YYYY-MM-DD."""
    if not raw:
        return None
    s = raw.strip()
    mm = _MONTH_YEAR_RE.search(s)
    if mm:
        month = _MONTHS[mm.group(1).lower()]
        year = int(mm.group(2))
        return f"{year:04d}-{month:02d}-01"
    iso = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)
    if iso:
        y, mo, d = int(iso.group(1)), int(iso.group(2)), int(iso.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    tok = _DATE_TOKEN_RE.search(s)
    if not tok:
        return None
    t = tok.group(1)
    parts = t.split("/")
    try:
        if len(parts) == 2:
            mo, y = int(parts[0]), int(parts[1])
            if y < 100:
                y += 2000
            if 1 <= mo <= 12:
                return f"{y:04d}-{mo:02d}-01"
        elif len(parts) == 3:
            mo, d, y = int(parts[0]), int(parts[1]), int(parts[2])
            if y < 100:
                y += 2000
            if 1 <= mo <= 12 and 1 <= d <= 31:
                return f"{y:04d}-{mo:02d}-{d:02d}"
    except ValueError:
        return None
    return None


# ── HTML table parsing ────────────────────────────────────────────────────────

def _cell_text(cell: Any) -> str:
    return cell.get_text(" ", strip=True) if cell is not None else ""


def _row_grid(row: Any) -> list[str]:
    """Expand a <tr> to a fixed-width LOGICAL GRID, honoring colspan."""
    out: list[str] = []
    for c in row.find_all(["td", "th"]):
        try:
            span = int(c.get("colspan", "1") or 1)
        except ValueError:
            span = 1
        span = max(1, span)
        out.append(_cell_text(c))
        out.extend([""] * (span - 1))
    return out


def _is_holdings_table(table: Any) -> bool:
    """A <table> is a SOI holdings table iff it carries cost/spread ix cells AND dates."""
    cost = table.find_all("ix:nonfraction", attrs={"name": "us-gaap:InvestmentOwnedAtCost"})
    spread = table.find_all(
        "ix:nonfraction", attrs={"name": "us-gaap:InvestmentBasisSpreadVariableRate"}
    )
    if len(cost) < 3 and len(spread) < 3:
        return False
    txt = table.get_text(" ", strip=True)
    return len(_DATE_TOKEN_RE.findall(txt)) >= 3


def _find_header_map(table: Any) -> dict[str, int] | None:
    """Locate the holdings-table header row and return a logical-grid column-index map."""
    for row in table.find_all("tr")[:6]:
        grid = _row_grid(row)
        if not any(_HDR_MATURITY.search(t) for t in grid):
            continue
        cmap: dict[str, int] = {}
        for i, t in enumerate(grid):
            if not t:
                continue
            if "maturity" not in cmap and _HDR_MATURITY.search(t):
                cmap["maturity"] = i
            if "company" not in cmap and _HDR_COMPANY.search(t):
                cmap["company"] = i
            if "instrument" not in cmap and _HDR_INSTRUMENT.search(t):
                cmap["instrument"] = i
            if "fair_value" not in cmap and _HDR_FAIR_VALUE.search(t):
                cmap["fair_value"] = i
            if "principal" not in cmap and _HDR_PRINCIPAL.search(t):
                cmap["principal"] = i
            if "amort_cost" not in cmap and _HDR_AMORT_COST.search(t):
                cmap["amort_cost"] = i
            if "interest" not in cmap and _HDR_INTEREST.search(t):
                cmap["interest"] = i
            if "identifier" not in cmap and _HDR_IDENTIFIER.search(t):
                cmap["identifier"] = i
        if "maturity" in cmap:
            return cmap
    return None


def _latest_date_in(texts: list[str]) -> tuple[str | None, str | None]:
    """Return (normalized_iso, raw_token) for the LATEST date token across texts."""
    best_iso: str | None = None
    best_raw: str | None = None
    for t in texts:
        for tok in _DATE_TOKEN_RE.findall(t):
            iso = _normalize_date(tok)
            if iso and (best_iso is None or iso > best_iso):
                best_iso, best_raw = iso, tok
        for mmt in _MONTH_YEAR_RE.finditer(t):
            iso = _normalize_date(mmt.group(0))
            if iso and (best_iso is None or iso > best_iso):
                best_iso, best_raw = iso, mmt.group(0)
    return best_iso, best_raw


def _parse_html_holdings(
    html_bytes: bytes,
    source_url: str,
) -> list[dict[str, Any]]:
    """Parse SOI holdings rows from an Inline-XBRL filing HTML.

    Returns a list of dicts with keys matching v2 schema fields.
    Uses the same TextBlock + colspan→logical-grid approach as v1.
    """
    if BeautifulSoup is None:
        LOG.warning("BeautifulSoup not available; HTML fallback disabled")
        return []

    doc = html_bytes.decode("utf-8", errors="replace")
    content_hash = hashlib.sha256(html_bytes).hexdigest()

    # Try TextBlock path first, then full-doc table scan
    textblock_html = _build_textblock_html(doc)
    soup_source = textblock_html if textblock_html.strip() else doc
    soup = BeautifulSoup(soup_source, "lxml")

    rows_out: list[dict[str, Any]] = []
    last_company: str | None = None
    last_instrument: str | None = None
    last_header_map: dict[str, int] | None = None

    for table in soup.find_all("table"):
        if not _is_holdings_table(table):
            continue
        header_map = _find_header_map(table)
        if header_map:
            last_header_map = header_map
            last_company = None  # reset carry-down at new header
        if last_header_map is None:
            continue

        cmap = last_header_map

        for row in table.find_all("tr"):
            grid = _row_grid(row)
            if not grid:
                continue

            # Skip header rows
            if any(_HDR_MATURITY.search(t) for t in grid[:3]):
                continue

            # Company carry-down
            company_idx = cmap.get("company", 0)
            company_cell = grid[company_idx] if company_idx < len(grid) else ""
            if company_cell.strip():
                last_company = company_cell.strip()

            instrument_idx = cmap.get("instrument")
            instrument_cell = (
                grid[instrument_idx].strip()
                if instrument_idx is not None and instrument_idx < len(grid)
                else ""
            )
            if instrument_cell:
                last_instrument = instrument_cell
            elif not instrument_cell and last_instrument:
                instrument_cell = last_instrument

            # Maturity
            mat_idx = cmap.get("maturity")
            maturity_raw = (
                grid[mat_idx].strip()
                if mat_idx is not None and mat_idx < len(grid)
                else ""
            )
            if not maturity_raw:
                # fallback: latest date in row
                maturity_raw, _ = _latest_date_in(grid)  # type: ignore

            # Fair value
            fv_idx = cmap.get("fair_value")
            fair_value_raw = (
                grid[fv_idx].strip()
                if fv_idx is not None and fv_idx < len(grid)
                else ""
            )

            # Principal (HTML fallback)
            prin_idx = cmap.get("principal")
            principal_raw = (
                grid[prin_idx].strip()
                if prin_idx is not None and prin_idx < len(grid)
                else ""
            )

            # Amortized cost (HTML fallback)
            ac_idx = cmap.get("amort_cost")
            amort_cost_raw = (
                grid[ac_idx].strip()
                if ac_idx is not None and ac_idx < len(grid)
                else ""
            )

            # Interest rate (HTML fallback)
            int_idx = cmap.get("interest")
            interest_raw = (
                grid[int_idx].strip()
                if int_idx is not None and int_idx < len(grid)
                else ""
            )

            # Identifier / CUSIP (HTML fallback)
            id_idx = cmap.get("identifier")
            identifier_raw = (
                grid[id_idx].strip()
                if id_idx is not None and id_idx < len(grid)
                else ""
            )

            if not last_company and not instrument_cell:
                continue

            rows_out.append({
                "portfolio_company_name_html": last_company,
                "instrument_type_html": instrument_cell,
                "maturity_date_raw_html": maturity_raw or None,
                "fair_value_raw_html": fair_value_raw or None,
                "principal_raw_html": principal_raw or None,
                "amort_cost_raw_html": amort_cost_raw or None,
                "interest_raw_html": interest_raw or None,
                "identifier_raw_html": identifier_raw or None,
                "source_filing_url": source_url,
                "source_html_content_hash": content_hash,
                "_column_alignment_anomaly": len(grid) != len(cmap),
            })

    return rows_out


# ── R2 helpers ────────────────────────────────────────────────────────────────

def _get_r2_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
    )


def _list_source_periods(s3) -> list[str]:
    """List all release=<period> prefixes in sec-bdc/soi/."""
    paginator = s3.get_paginator("list_objects_v2")
    periods: list[str] = []
    for page in paginator.paginate(
        Bucket=R2_BUCKET, Prefix=f"{R2_SOI_SOURCE_PREFIX}/", Delimiter="/"
    ):
        for pfx in page.get("CommonPrefixes", []):
            p = pfx["Prefix"].rstrip("/")
            rel = p.split("release=")[-1] if "release=" in p else None
            if rel:
                periods.append(rel)
    return sorted(periods)


def _read_source_parquet(s3, period: str) -> list[dict[str, Any]]:
    """DuckDB-read the soi.tsv Parquet for a period and return as list of dicts."""
    key = f"{R2_SOI_SOURCE_PREFIX}/release={period}/data.parquet"
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    endpoint = os.environ["R2_ENDPOINT"].replace("https://", "")
    con.execute(f"SET s3_endpoint='{endpoint}'")
    con.execute(f"SET s3_access_key_id='{os.environ['R2_ACCESS_KEY_ID']}'")
    con.execute(f"SET s3_secret_access_key='{os.environ['R2_SECRET_ACCESS_KEY']}'")
    con.execute("SET s3_url_style='path'")
    con.execute("SET s3_region='auto'")
    url = f"s3://{R2_BUCKET}/{key}"
    # Read all columns; project only what we need
    cols = ", ".join(f'"{c}"' for c in SOI_TSV_COLUMN_MAP.keys() if c != "name")
    # Add 'name' separately since it may be 'name' vs 'company_name'
    try:
        rows = con.execute(
            f"SELECT * FROM read_parquet('{url}')"
        ).fetchall()
        colnames = [d[0] for d in con.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{url}') LIMIT 1"
        ).fetchall()]
        return [dict(zip(colnames, row)) for row in rows]
    except Exception as exc:
        LOG.warning("Failed to read source Parquet for period %s: %s", period, exc)
        return []


def _upload_parquet(s3, key: str, table: "pa.Table") -> None:
    """Upload a PyArrow table to R2 as ZSTD Parquet."""
    buf = io.BytesIO()
    pq.write_table(
        table,
        buf,
        compression="zstd",
        compression_level=9,
        row_group_size=100_000,
    )
    buf.seek(0)
    s3.put_object(
        Bucket=R2_BUCKET,
        Key=key,
        Body=buf.getvalue(),
        ContentType="application/x-parquet",
    )


# ── DB helpers ────────────────────────────────────────────────────────────────

def _db_conn():
    return psycopg.connect(
        os.environ.get("DEX_DB_URL_DIRECT") or os.environ.get("DATABASE_URL"),
        autocommit=False,
    )


def _insert_run(conn, release: str, status: str) -> str:
    """Insert a new run row; return run_id."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.bdc_soi_parsed_v2_runs
              (release, status, started_at, parser_version)
            VALUES (%s, %s, now(), %s)
            RETURNING run_id::text
            """,
            (release, status, __version__),
        )
        run_id = cur.fetchone()[0]
    conn.commit()
    return run_id


def _update_run(conn, run_id: str, status: str, rows_written: int | None = None,
                r2_key: str | None = None, error_message: str | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ops.bdc_soi_parsed_v2_runs
            SET status=%s, completed_at=now(), rows_written=%s,
                r2_key=%s, error_message=%s
            WHERE run_id=%s::uuid
            """,
            (status, rows_written, r2_key, error_message, run_id),
        )
    conn.commit()


def _check_prior_completed(conn, release: str) -> bool:
    """Return True if there is already a 'completed' run for this release."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM ops.bdc_soi_parsed_v2_runs WHERE release=%s AND status='completed' LIMIT 1",
            (release,),
        )
        return cur.fetchone() is not None


# ── Per-row processing ────────────────────────────────────────────────────────

def _process_row(
    src_row: dict[str, Any],
    html_row: dict[str, Any] | None,
    extracted_at: str,
    release: str,
) -> dict[str, Any]:
    """Merge structured + HTML fields into a v2 output row."""
    all_reasons: list[str] = []
    all_rule_ids: list[str] = []

    # BDC registrant name (always from structured column — top-level 'name')
    bdc_name = src_row.get("name") or None

    # Portfolio company name
    raw_pcn = src_row.get("portfolio_company_name") or ""
    # Prefer XBRL issuer-name axis when available
    issuer_axis = src_row.get("Investment, Issuer Name Axis") or src_row.get("issuer_name_axis") or ""
    if issuer_axis.strip():
        raw_pcn = issuer_axis.strip()
    # Classify name
    cleaned_name, normalized_name, name_conf, name_reasons, name_rule_ids = classify_name(raw_pcn or None)
    all_reasons.extend(name_reasons)
    all_rule_ids.extend(name_rule_ids)

    # Instrument type
    raw_instrument = src_row.get("instrument_type") or (html_row or {}).get("instrument_type_html") or ""
    norm_instrument, is_debt, inst_conf, inst_reasons, inst_rule_ids = classify_instrument_type(raw_instrument or None)
    all_reasons.extend(inst_reasons)
    all_rule_ids.extend(inst_rule_ids)

    # Principal — structured PRIMARY, HTML FALLBACK
    principal_raw_structured = (
        src_row.get("Investment Owned, Balance, Principal Amount") or
        src_row.get("principal_raw_structured") or ""
    )
    principal_raw_html = (html_row or {}).get("principal_raw_html") or ""
    principal_raw = principal_raw_structured.strip() or principal_raw_html.strip() or None
    principal_confidence = "verified_exact" if principal_raw_structured.strip() else "inferred_anchored"

    principal_val: float | None = None
    if principal_raw:
        principal_val, p_conf, p_reasons, p_rule_ids = classify_principal(principal_raw)
        if principal_raw_structured.strip():
            p_conf = "verified_exact"  # structured column always verified_exact
        all_reasons.extend(p_reasons)
        all_rule_ids.extend(p_rule_ids)

    # Amortized cost — structured PRIMARY, HTML FALLBACK
    amort_raw_structured = (
        src_row.get("Investment Owned, Cost") or
        src_row.get("amortized_cost_raw_structured") or ""
    )
    amort_raw_html = (html_row or {}).get("amort_cost_raw_html") or ""
    amort_raw = amort_raw_structured.strip() or amort_raw_html.strip() or None
    amort_val: float | None = None
    if amort_raw:
        amort_val, ac_conf, ac_reasons, ac_rule_ids = classify_amortized_cost(amort_raw)
        if amort_raw_structured.strip():
            ac_conf = "verified_exact"
        all_reasons.extend(ac_reasons)
        all_rule_ids.extend(ac_rule_ids)

    # Interest rate — structured PRIMARY, HTML FALLBACK
    interest_raw_structured = (
        src_row.get("Investment Interest Rate") or
        src_row.get("investment_interest_rate_raw") or ""
    )
    interest_raw_html = (html_row or {}).get("interest_raw_html") or ""
    interest_raw = interest_raw_structured.strip() or interest_raw_html.strip() or None

    # Additional rate components from structured columns
    spread_raw_structured = (
        src_row.get("Investment, Basis Spread, Variable Rate") or
        src_row.get("investment_interest_rate_spread_raw") or ""
    ).strip()
    floor_raw_structured = (
        src_row.get("Investment, Interest Rate, Floor") or
        src_row.get("investment_interest_rate_floor_raw") or ""
    ).strip()
    pik_raw_structured = (
        src_row.get("Investment, Interest Rate, Paid in Kind") or
        src_row.get("investment_interest_rate_pik_raw") or ""
    ).strip()

    # Parse interest rate
    rate_base: str | None = None
    spread_bps: int | None = None
    floor_bps: int | None = None
    pik_bps: int | None = None

    if interest_raw:
        (rate_base, spread_bps, floor_bps, pik_bps), rate_conf, rate_reasons, rate_rule_ids = \
            classify_interest_rate(interest_raw)
        if interest_raw_structured.strip():
            rate_conf = "verified_exact"
        all_reasons.extend(rate_reasons)
        all_rule_ids.extend(rate_rule_ids)

    # Override bps from structured if available (more precise)
    if spread_raw_structured:
        try:
            spread_bps = round(float(spread_raw_structured.replace("%", "").strip()) * 100)
        except ValueError:
            pass
    if floor_raw_structured:
        try:
            floor_bps = round(float(floor_raw_structured.replace("%", "").strip()) * 100)
        except ValueError:
            pass
    if pik_raw_structured:
        try:
            pik_bps = round(float(pik_raw_structured.replace("%", "").strip()) * 100)
        except ValueError:
            pass

    # Investment identifier — structured PRIMARY, HTML FALLBACK
    id_raw_structured = (
        src_row.get("Investment, Identifier Axis") or
        src_row.get("investment_identifier_raw") or ""
    ).strip()
    id_raw_html = (html_row or {}).get("identifier_raw_html") or ""
    id_raw = id_raw_structured or id_raw_html or None

    investment_identifier: str | None = None
    if id_raw:
        investment_identifier, id_conf, id_reasons, id_rule_ids = classify_cusip(id_raw)
        if not investment_identifier:
            # Not a valid CUSIP — keep raw as identifier (may be tranche descriptor)
            investment_identifier = id_raw
            id_conf = "inferred_anchored"
        if id_raw_structured:
            id_conf = "verified_exact"
        all_reasons.extend(id_reasons)
        all_rule_ids.extend(id_rule_ids)

    # Fair value (from structured — already in soi.tsv)
    fair_value_raw = str(src_row.get("fair_value") or "").strip() or None

    # Acquisition date (from structured)
    acquisition_date_raw = str(src_row.get("acquisition_date") or "").strip() or None

    # Maturity date — HTML is primary (validator-confirmed 0% structured for top BDCs)
    maturity_raw_html = (html_row or {}).get("maturity_date_raw_html") or ""
    maturity_raw_v1 = str(src_row.get("maturity_date_v1") or src_row.get("maturity_date") or "").strip()
    maturity_raw = maturity_raw_html.strip() or maturity_raw_v1 or None

    maturity_typed: str | None = None
    if maturity_raw:
        maturity_typed, mat_conf, mat_reasons, mat_rule_ids = classify_maturity_date(
            maturity_raw, norm_instrument, _normalize_date
        )
        all_reasons.extend(mat_reasons)
        all_rule_ids.extend(mat_rule_ids)

    # Column alignment anomaly detection
    if html_row and html_row.get("_column_alignment_anomaly"):
        all_reasons.append("column_alignment_anomaly")
        all_rule_ids.append("rule_col_alignment")

    # Compute row-level parse_confidence
    # verified_exact if sourced entirely from structured columns;
    # inferred_anchored if any HTML-derived value was used;
    # rejected if a critical field was rejected.
    has_html_fallback = html_row is not None and (
        maturity_raw_html.strip() or
        principal_raw_html.strip() or
        amort_raw_html.strip() or
        interest_raw_html.strip()
    )
    critical_rejected = (
        "principal_unparseable" in all_reasons or
        "interest_rate_format_unrecognized" in all_reasons
    ) and not principal_val and not interest_raw

    if critical_rejected:
        row_confidence = "rejected"
    elif has_html_fallback or "name_fallback_placeholder" in all_reasons:
        row_confidence = "inferred_anchored"
    else:
        row_confidence = "verified_exact"

    source_filing_url = (html_row or {}).get("source_filing_url") or None
    source_html_content_hash = (html_row or {}).get("source_html_content_hash") or None

    return {
        "adsh": src_row.get("adsh"),
        "cik": str(src_row.get("cik") or ""),
        "name": bdc_name,
        "portfolio_company_name": raw_pcn or None,
        "portfolio_company_name_clean": cleaned_name,
        "portfolio_company_name_normalized": normalized_name,
        "instrument_type": norm_instrument,
        "is_debt_instrument": is_debt,
        "principal": principal_val,
        "principal_raw": principal_raw,
        "amortized_cost": amort_val,
        "amortized_cost_raw": amort_raw,
        "investment_interest_rate_raw": interest_raw,
        "investment_interest_rate_base": rate_base,
        "investment_interest_rate_spread_bps": spread_bps,
        "investment_interest_rate_floor_bps": floor_bps,
        "investment_interest_rate_pik_bps": pik_bps,
        "investment_identifier": investment_identifier,
        "fair_value": fair_value_raw,
        "acquisition_date": acquisition_date_raw,
        "maturity_date_raw": maturity_raw,
        "maturity_date_typed": maturity_typed,
        "parse_confidence": row_confidence,
        # L54: pipe-joined VARCHAR (not LIST<VARCHAR>)
        "parse_demotion_reason": pipe_join(all_reasons),
        "demoted_by_rule_ids": pipe_join(all_rule_ids),
        "source_filing_url": source_filing_url,
        "source_html_content_hash": source_html_content_hash,
        "parser_version": __version__,
        "extracted_at": extracted_at,
        "inlineurl": src_row.get("inlineurl"),
        "release": release,
    }


# ── Per-period processing ─────────────────────────────────────────────────────

def _process_period(
    s3,
    client: httpx.Client,
    period: str,
    out_prefix: str,
    dry_run: bool,
    max_filings: int | None,
    extracted_at: str,
) -> tuple[str, int]:
    """Process one release period. Returns (status, rows_written)."""
    LOG.info("Processing period %s", period)
    src_rows = _read_source_parquet(s3, period)
    if not src_rows:
        LOG.warning("No source rows for period %s", period)
        return "failed", 0

    # Group source rows by (adsh, inlineurl) for HTML fetch deduplication
    from collections import defaultdict
    by_filing: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in src_rows:
        adsh = str(row.get("adsh") or "")
        inlineurl = str(row.get("inlineurl") or "")
        by_filing[(adsh, inlineurl)].append(row)

    LOG.info("Period %s: %d source rows, %d unique filings", period, len(src_rows), len(by_filing))

    output_rows: list[dict[str, Any]] = []
    filings_processed = 0

    for (adsh, inlineurl), filing_rows in sorted(by_filing.items()):
        if max_filings is not None and filings_processed >= max_filings:
            break

        # Fetch HTML only when needed (maturity_date or fallback fields missing)
        html_rows_by_company: dict[str, dict] = {}
        needs_html = any(
            not str(row.get("maturity_date") or row.get("maturity_date_v1") or "").strip()
            for row in filing_rows
        )

        if needs_html and inlineurl:
            raw_url = _resolve_filing_url(inlineurl)
            try:
                resp = client.get(raw_url)
                if resp.status_code == 200:
                    html_rows = _parse_html_holdings(resp.content, raw_url)
                    # Index by portfolio_company_name (lower) for rough matching
                    for hr in html_rows:
                        key = (hr.get("portfolio_company_name_html") or "").lower().strip()
                        if key:
                            html_rows_by_company[key] = hr
                else:
                    LOG.warning("HTTP %s fetching %s", resp.status_code, raw_url)
            except Exception as exc:
                LOG.warning("Failed to fetch %s: %s", raw_url, exc)

        for src_row in filing_rows:
            # Match HTML row by portfolio company name (rough)
            pcn_key = (src_row.get("portfolio_company_name") or "").lower().strip()
            html_row = html_rows_by_company.get(pcn_key) if html_rows_by_company else None

            out_row = _process_row(src_row, html_row, extracted_at, period)
            output_rows.append(out_row)

        filings_processed += 1

    if not output_rows:
        return "no_change", 0

    if dry_run:
        LOG.info("[DRY RUN] Would write %d rows for period %s", len(output_rows), period)
        return "dry_run", len(output_rows)

    # Build PyArrow table
    schema = pa.schema([
        pa.field("adsh", pa.string()),
        pa.field("cik", pa.string()),
        pa.field("name", pa.string()),
        pa.field("portfolio_company_name", pa.string()),
        pa.field("portfolio_company_name_clean", pa.string()),
        pa.field("portfolio_company_name_normalized", pa.string()),
        pa.field("instrument_type", pa.string()),
        pa.field("is_debt_instrument", pa.bool_()),
        pa.field("principal", pa.float64()),
        pa.field("principal_raw", pa.string()),
        pa.field("amortized_cost", pa.float64()),
        pa.field("amortized_cost_raw", pa.string()),
        pa.field("investment_interest_rate_raw", pa.string()),
        pa.field("investment_interest_rate_base", pa.string()),
        pa.field("investment_interest_rate_spread_bps", pa.int32()),
        pa.field("investment_interest_rate_floor_bps", pa.int32()),
        pa.field("investment_interest_rate_pik_bps", pa.int32()),
        pa.field("investment_identifier", pa.string()),
        pa.field("fair_value", pa.string()),
        pa.field("acquisition_date", pa.string()),
        pa.field("maturity_date_raw", pa.string()),
        pa.field("maturity_date_typed", pa.string()),
        pa.field("parse_confidence", pa.string()),
        pa.field("parse_demotion_reason", pa.string()),   # pipe-joined (L54)
        pa.field("demoted_by_rule_ids", pa.string()),     # pipe-joined (L54)
        pa.field("source_filing_url", pa.string()),
        pa.field("source_html_content_hash", pa.string()),
        pa.field("parser_version", pa.string()),
        pa.field("extracted_at", pa.string()),
        pa.field("inlineurl", pa.string()),
        pa.field("release", pa.string()),
    ])

    arrays = {}
    for field in schema:
        arrays[field.name] = pa.array(
            [row.get(field.name) for row in output_rows],
            type=field.type,
        )
    table = pa.table(arrays, schema=schema)

    r2_key = f"{out_prefix}/release={period}/data.parquet"
    _upload_parquet(s3, r2_key, table)
    LOG.info("Uploaded %d rows to s3://%s/%s", len(output_rows), R2_BUCKET, r2_key)
    return "completed", len(output_rows)


# ── Main entry point ──────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="BDC SOI v2 parse")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--apply", action="store_true", help="Apply changes to R2")
    mode.add_argument("--dry-run", action="store_true", help="Show what would be written")
    parser.add_argument("--periods", default="", help="Comma-sep period filter (e.g. 2025q1,2026_04)")
    parser.add_argument("--out-prefix", default=R2_PARSED_V2_PREFIX, help="R2 output prefix")
    parser.add_argument("--max-filings", type=int, default=None, help="Hard cap on filings per period")
    parser.add_argument("--skip-if-unchanged", action="store_true",
                        help="Skip periods with a prior 'completed' run")
    args = parser.parse_args(argv)

    dry_run = args.dry_run
    out_prefix = args.out_prefix
    period_filter = [p.strip() for p in args.periods.split(",") if p.strip()]

    s3 = _get_r2_client()
    client = _make_client()

    # Stamp extracted_at ONCE per run (not per-row) for determinism (e5)
    extracted_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    conn = _db_conn() if not dry_run else None

    all_periods = _list_source_periods(s3)
    periods = [p for p in all_periods if not period_filter or p in period_filter]
    LOG.info("Processing %d periods: %s", len(periods), periods)

    for period in periods:
        if conn and args.skip_if_unchanged:
            if _check_prior_completed(conn, period):
                LOG.info("Skipping period %s (already completed)", period)
                continue

        run_id = None
        if conn:
            run_id = _insert_run(conn, period, "running")

        try:
            status, rows_written = _process_period(
                s3, client, period, out_prefix, dry_run, args.max_filings, extracted_at
            )
        except Exception as exc:
            LOG.exception("Error processing period %s: %s", period, exc)
            if conn and run_id:
                _update_run(conn, run_id, "failed", error_message=str(exc))
            continue

        r2_key = f"{out_prefix}/release={period}/data.parquet"
        if conn and run_id:
            _update_run(conn, run_id, status, rows_written=rows_written,
                        r2_key=r2_key if status == "completed" else None)
        LOG.info("Period %s: status=%s rows=%d", period, status, rows_written)

    if conn:
        conn.close()
    client.close()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""SEC BDC Schedule of Investments — Inline-XBRL filing-HTML parse.

Extracts per-investment maturity_date + a clean portfolio_company_name +
instrument_type from each BDC filing's Inline-XBRL HTML, keyed for join-back to
the structured soi.tsv Parquet (s3 output).

WHY THIS SURFACE EXISTS
-----------------------
The structured soi.tsv carries an `Investment Maturity Date` column that is only
~18% populated overall and effectively 0% for the largest BDCs (Ares, Blackstone
Private Credit, Golub, Main Street, ...). For those filers the per-instrument
maturity exists ONLY inside the visual Schedule of Investments holdings table in
the Inline-XBRL filing HTML — and it is NOT a tagged XBRL fact (validator-probed
2026-05-20: `InvestmentMaturityDate` XBRL tag count = 0 for Ares). It appears as
plain text in a holdings-table cell. So this parser reads the rendered HTML
holdings table, not XBRL tags.

SOURCE
------
The `soi.tsv` `inlineurl` column (100% populated) — e.g.
  https://www.sec.gov/ix?doc=/Archives/edgar/data/1287750/000128775025000007/arcc-20241231.htm
The `/ix?doc=` prefix is the Inline-XBRL viewer; the parser strips it to fetch
the raw filing document.

HOLDINGS-TABLE DETECTION + PARSE
--------------------------------
1. The BDC Schedule of Investments holdings table is split across many small
   <table> elements (one per rendered page) — verified 2026-05-20: Ares = 114
   such tables. A <table> is a holdings table iff it carries >=3
   `<ix:nonfraction name="us-gaap:InvestmentOwnedAtCost">` or
   `InvestmentBasisSpreadVariableRate` cells AND >=3 date tokens.
2. Each holdings table opens with a header row, e.g.:
     Company | Business Description | Investment | Coupon | Reference | Spread |
     Acquisition Date | Maturity Date | Shares/Units | Principal |
     Amortized Cost | Fair Value | % of Net Assets
   COLSPAN MODEL: BDC filers pad these tables with empty colspan cells, so the
   raw <td>/<th> index of "Maturity Date" in the header row does NOT match the
   raw index of the maturity value in data rows (verified 2026-05-20: Golub
   header "Maturity Date" at raw <td> 8, data value at raw <td> 13). The parser
   therefore expands every row to a fixed-width LOGICAL GRID — each cell
   contributes `colspan` grid columns — so header and data column positions
   align. It maps the grid column of the "Maturity Date" cell, the
   company/portfolio cell, the instrument/"Investment" cell, and the "Fair
   Value" cell. Continuation tables that repeat the grid without a header
   inherit the previous table's column map.
3. Per data row, maturity_date is read from the maturity grid column; the
   portfolio_company_name carries DOWN (a company name appears only in the
   first row of that company's block — subsequent instrument rows for the same
   company leave the company cell blank). instrument_type comes from the
   "Investment" column. If the maturity grid cell yields no date, the parser
   falls back to the LATEST date token in the row (acquisition dates are in the
   past, maturity dates in the future — the max date is the maturity).
4. The escape="true" `InvestmentHoldingsScheduleOfInvestmentsTextBlock` /
   `...TableTextBlock` ix element + its `continuedAt` continuation chain is also
   reconstructed and parsed as a fallback holdings-table source for filers that
   place the grid inside that element rather than inline (the directive's
   originally-assumed shape). Both paths feed the same row extractor.

OUTPUT
------
ZSTD Parquet to R2 sec-bdc/soi-parsed/release=<period>/data.parquet — one row
per parsed investment line, columns:
  adsh, cik, portfolio_company_name, instrument_type, maturity_date (DATE-ready
  ISO string), maturity_date_raw, fair_value (the join disambiguator), inlineurl.
The join key for s5's LEFT JOIN back to soi.tsv is
  (adsh, portfolio_company_name + instrument_type, fair_value).

DATE FORMATS
------------
Heterogeneous — handled: MM/YYYY (Ares: "10/2026"), MM/DD/YYYY, M/D/YY,
"Month YYYY" ("January 2027"), YYYY-MM-DD. A bare MM/YYYY normalizes to the
first of the month.

ITERATION
---------
This is the single build-to-threshold surface (directive §"Iteration budget").
Its verify gate is e2 (>=55% maturity coverage of debt-like rows across the
top-20 BDCs). Run --sample to measure coverage on a filing sample before the
full backfill.

CLI:
  --apply / --dry-run
  --periods 2025q1,...           # subset
  --sample N                     # parse only N filings (coverage measurement)
  --sample-ciks cik1,cik2,...    # restrict the sample to specific BDC CIKs
  --max-filings N                # hard cap on filings processed

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/parse_sec_bdc_soi_html.py --apply
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/parse_sec_bdc_soi_html.py --apply --periods 2025q1 --sample 20
"""
from __future__ import annotations

import argparse
import html as htmlmod
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
import duckdb
import httpx

try:
    from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
    import warnings
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
except ImportError:  # pragma: no cover
    BeautifulSoup = None  # type: ignore

LOG = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)

USER_AGENT = "Mozilla/5.0 (compatible; data-engine-x/1.0; +tools@substrate.build)"
R2_BUCKET = "dex-raw-landing-zone"
R2_RAW_PREFIX = "sec-bdc"
R2_PARSED_PREFIX = "sec-bdc/soi-parsed"

# A date token in a holdings-table cell. MM/DD/YYYY, M/D/YY, MM/YYYY and the
# bare MM/YY form (Barings et al. write "04/28") all match. The MM/DD/YYYY
# alternative MUST come first — regex alternation is ordered, so listing the
# shorter MM/YY form first would make it greedily capture only the "MM/DD"
# prefix of a full MM/DD/YYYY token.
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

# Holdings-table header cell hints — case-insensitive substring match.
_HDR_MATURITY = re.compile(r"maturity", re.IGNORECASE)
_HDR_COMPANY = re.compile(r"(company|portfolio|issuer)", re.IGNORECASE)
_HDR_INSTRUMENT = re.compile(
    r"(investment|instrument|type of investment|security)", re.IGNORECASE
)
_HDR_FAIR_VALUE = re.compile(r"fair\s*value", re.IGNORECASE)

# Instrument-type keywords — a cell that matches is a debt/equity instrument
# label, used to detect the instrument column when the header is ambiguous.
_INSTRUMENT_HINT = re.compile(
    r"(?i)\b(lien|loan|note|bond|debt|term|revolver|equity|warrant|preferred|"
    r"common|unit|subordinat|mezzanine|delayed draw)\b"
)

# The SOI TextBlock ix element names (escape="true" continued-chain variant).
_SOI_TEXTBLOCK_NAMES = (
    "us-gaap:InvestmentHoldingsScheduleOfInvestmentsTextBlock",
    "us-gaap:InvestmentHoldingsScheduleOfInvestmentsTableTextBlock",
)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _make_client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
        timeout=180.0,
    )


def _resolve_filing_url(inlineurl: str) -> str:
    """Strip the Inline-XBRL `/ix?doc=` viewer prefix to get the raw filing URL.

    inlineurl example:
      https://www.sec.gov/ix?doc=/Archives/edgar/data/1287750/000.../arcc-...htm
    → https://www.sec.gov/Archives/edgar/data/1287750/000.../arcc-...htm
    """
    u = (inlineurl or "").strip()
    m = re.search(r"[?&]doc=(/?[^&]+)", u)
    if m:
        doc = m.group(1)
        if not doc.startswith("/"):
            doc = "/" + doc
        return "https://www.sec.gov" + doc
    return u


# ---------------------------------------------------------------------------
# Inline-XBRL continuation-chain reconstruction
# ---------------------------------------------------------------------------

def _element_inner(html: str, tagname: str, fid: str) -> tuple[str | None, str | None]:
    """Return (inner_html, continuedAt) for the <tagname id="fid"> element.

    Walks the raw HTML doing nesting-aware open/close matching for the same
    tag name — robust to nested same-tag elements inside the body.
    """
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
    """Reconstruct the full SOI TextBlock HTML by concatenating the anchor ix
    element body + every `<ix:continuation>` body in chain order, then
    HTML-unescaping (the element is escape="true").

    Returns "" when the filing carries no SOI TextBlock ix element.
    """
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


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

def _normalize_date(raw: str) -> str | None:
    """Normalize a heterogeneous date token to an ISO YYYY-MM-DD string.

    Handles: MM/YYYY (→ first of month), MM/DD/YYYY, M/D/YY, "Month YYYY",
    YYYY-MM-DD. Returns None if no date is recognized.
    """
    if not raw:
        return None
    s = raw.strip()

    # "Month YYYY"
    mm = _MONTH_YEAR_RE.search(s)
    if mm:
        month = _MONTHS[mm.group(1).lower()]
        year = int(mm.group(2))
        return f"{year:04d}-{month:02d}-01"

    # already ISO
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
            # MM/YYYY → first of month
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


def _cell_text(cell: Any) -> str:
    return cell.get_text(" ", strip=True) if cell is not None else ""


def _row_grid(row: Any) -> list[str]:
    """Expand a <tr> to a fixed-width LOGICAL GRID, honoring colspan.

    Each <td>/<th> contributes `colspan` grid columns: the cell's text in the
    first, empty strings in the spanned remainder. This makes header and data
    column positions align even when filers pad the table with colspan cells
    (the raw <td> index of a value differs between header and data rows; the
    grid index does not).
    """
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


def _latest_date_in(texts: list[str]) -> tuple[str | None, str | None]:
    """Return (normalized_iso, raw_token) for the LATEST date token across the
    given cell texts. Holdings rows carry an acquisition date (past) and a
    maturity date (future); the max date is the maturity. Returns (None, None)
    when no date token is present (e.g. equity rows with no maturity)."""
    best_iso: str | None = None
    best_raw: str | None = None
    for t in texts:
        for tok in _DATE_TOKEN_RE.findall(t):
            iso = _normalize_date(tok)
            if iso and (best_iso is None or iso > best_iso):
                best_iso, best_raw = iso, tok
        for mm in _MONTH_YEAR_RE.finditer(t):
            iso = _normalize_date(mm.group(0))
            if iso and (best_iso is None or iso > best_iso):
                best_iso, best_raw = iso, mm.group(0)
    return best_iso, best_raw


# ---------------------------------------------------------------------------
# Holdings-table detection + per-row extraction
# ---------------------------------------------------------------------------

def _is_holdings_table(table: Any) -> bool:
    """A <table> is a SOI holdings table iff it carries >=3 per-instrument
    cost/spread ix cells AND >=3 date tokens (rules out balance-sheet and
    summary tables, which carry InvestmentOwnedAtFairValue only for aggregates
    and have no per-investment date column)."""
    cost = table.find_all("ix:nonfraction", attrs={"name": "us-gaap:InvestmentOwnedAtCost"})
    spread = table.find_all(
        "ix:nonfraction", attrs={"name": "us-gaap:InvestmentBasisSpreadVariableRate"}
    )
    if len(cost) < 3 and len(spread) < 3:
        return False
    txt = table.get_text(" ", strip=True)
    return len(_DATE_TOKEN_RE.findall(txt)) >= 3


def _find_header_map(table: Any) -> dict[str, int] | None:
    """Locate the holdings-table header row and return a LOGICAL-GRID column-index
    map for {maturity, company, instrument, fair_value}. Indices are on the
    colspan-expanded grid (see `_row_grid`) so they align with data rows.
    Returns None if no row in the first few rows carries a 'Maturity' header
    cell."""
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
        if "maturity" in cmap:
            return cmap
    return None


def _infer_company_col(table: Any, instr_i: int | None, mat_i: int) -> int | None:
    """Infer the company-name grid column when the header carries no label
    above it (verified: Golub's header starts at the instrument column, the
    portfolio-company name sits one column to its left, unlabeled).

    Heuristic: across data rows that carry a fair-value cell, the company name
    is the leftmost populated grid column that is left of the instrument column
    (or left of the maturity column when instrument is unknown). Return the most
    common such column index, or None."""
    bound = instr_i if instr_i is not None else mat_i
    from collections import Counter
    votes: Counter[int] = Counter()
    for row in table.find_all("tr"):
        if _fair_value_from_row(row) is None:
            continue
        grid = _row_grid(row)
        for i, t in enumerate(grid):
            if i >= bound:
                break
            if t and not re.match(r"(?i)\s*(total|subtotal|net assets|\$)\s*$", t):
                votes[i] += 1
            if t:
                break
    return votes.most_common(1)[0][0] if votes else None


def _fair_value_from_row(row: Any) -> float | None:
    """Pull the InvestmentOwnedAtFairValue ix cell value (scale/sign applied)
    from a holdings-table row — the join disambiguator back to soi.tsv."""
    fv = row.find("ix:nonfraction", attrs={"name": "us-gaap:InvestmentOwnedAtFairValue"})
    if fv is None:
        return None
    raw = fv.get_text("", strip=True).replace(",", "").replace("(", "-").replace(")", "")
    try:
        val = float(raw)
    except ValueError:
        return None
    scale = fv.get("scale")
    if scale:
        try:
            val *= 10 ** int(scale)
        except ValueError:
            pass
    if fv.get("sign") == "-":
        val = -val
    return val


def parse_filing_holdings(doc: str) -> list[dict]:
    """Parse one filing's HTML; return a list of per-investment dicts with
    portfolio_company_name, instrument_type, maturity_date, maturity_date_raw,
    fair_value.

    Two table sources are scanned: (a) holdings tables embedded inline in the
    filing body, and (b) holdings tables inside the reconstructed SOI TextBlock
    ix element (the directive's originally-assumed shape — kept as a fallback
    for filers that place the grid there).
    """
    if BeautifulSoup is None:
        raise RuntimeError("beautifulsoup4 + lxml are required")

    rows_out: list[dict] = []
    seen_tables: set[int] = set()

    def scan(soup: Any) -> None:
        prev_map: dict[str, int] | None = None
        for table in soup.find_all("table"):
            if id(table) in seen_tables:
                continue
            if not _is_holdings_table(table):
                continue
            seen_tables.add(id(table))
            cmap = _find_header_map(table)
            if cmap is None:
                # continuation table — repeats the grid without a header.
                cmap = prev_map
            else:
                prev_map = cmap
            if cmap is None or "maturity" not in cmap:
                continue
            mat_i = cmap["maturity"]
            instr_i = cmap.get("instrument")
            # company column: the header often has NO label above the company
            # cell (verified: Golub). When the header lacks "company", infer it
            # from data rows — the leftmost populated grid column that sits left
            # of the instrument column, in a row that carries a fair-value cell.
            comp_i = cmap.get("company")
            if comp_i is None:
                comp_i = _infer_company_col(table, instr_i, mat_i)
            current_company = ""
            for row in table.find_all("tr"):
                grid = _row_grid(row)
                if not any(grid):
                    continue
                # company name in THIS row (None when blank/spanned-down)
                row_company = ""
                if comp_i is not None and comp_i < len(grid) and grid[comp_i]:
                    cand = grid[comp_i]
                    if not _HDR_MATURITY.search(cand) and not re.match(
                        r"(?i)\s*(total|subtotal|net assets)\b", cand
                    ):
                        row_company = cand
                        current_company = cand
                # a real investment row carries a fair-value ix cell
                fv = _fair_value_from_row(row)
                if fv is None:
                    continue
                # maturity: prefer the grid maturity column; fall back to the
                # latest date token in the row (acquisition < maturity).
                mat_raw = grid[mat_i] if mat_i < len(grid) else ""
                maturity = _normalize_date(mat_raw)
                if maturity is None:
                    maturity, fallback_raw = _latest_date_in(grid)
                    if maturity is not None and not mat_raw:
                        mat_raw = fallback_raw or ""
                instrument = ""
                if instr_i is not None and instr_i < len(grid):
                    instrument = grid[instr_i]
                if not instrument:
                    # fall back: first cell that looks like an instrument label
                    for t in grid:
                        if _INSTRUMENT_HINT.search(t):
                            instrument = t
                            break
                # skip subtotal / aggregate rows: a fair-value cell with NO
                # instrument text, NO own-row company name, and NO date token
                # is a per-company or per-section subtotal line, not a holding.
                if not instrument and not row_company and maturity is None:
                    continue
                rows_out.append({
                    "portfolio_company_name": current_company.strip(),
                    "instrument_type": instrument.strip(),
                    "maturity_date": maturity,
                    "maturity_date_raw": (mat_raw or "").strip(),
                    "fair_value": fv,
                })

    full_soup = BeautifulSoup(doc, "lxml")
    scan(full_soup)

    # Fallback: the SOI TextBlock continued-chain element.
    tb_html = _build_textblock_html(doc)
    if tb_html.strip():
        scan(BeautifulSoup(tb_html, "lxml"))

    return rows_out


# ---------------------------------------------------------------------------
# R2 helpers
# ---------------------------------------------------------------------------

def _r2_account_id() -> str:
    return os.environ["R2_ENDPOINT"].split("//")[-1].split(".")[0]


def _make_s3_client() -> Any:
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="us-east-1",
    )


def _connect_duckdb_to_r2() -> Any:
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(
        f"""
        CREATE SECRET (
            TYPE r2,
            KEY_ID '{os.environ["R2_ACCESS_KEY_ID"]}',
            SECRET '{os.environ["R2_SECRET_ACCESS_KEY"]}',
            ACCOUNT_ID '{_r2_account_id()}'
        )
        """
    )
    return con


def _discover_soi_periods(con: Any) -> list[str]:
    """List the release=<period> partitions present under R2 sec-bdc/soi/."""
    glob_pat = f"r2://{R2_BUCKET}/{R2_RAW_PREFIX}/soi/release=*/data.parquet"
    rows = con.execute(f"SELECT file FROM glob('{glob_pat}')").fetchall()
    periods: set[str] = set()
    for (path,) in rows:
        for seg in path.split("/"):
            if seg.startswith("release="):
                periods.add(seg[len("release="):])
    return sorted(periods)


def _load_soi_filings(con: Any, period: str) -> list[dict]:
    """Read R2 sec-bdc/soi/release=<period>/data.parquet; return the distinct
    (adsh, cik, inlineurl) filing rows. soi.tsv carries one row per investment;
    the parser fetches each filing's HTML once."""
    uri = f"r2://{R2_BUCKET}/{R2_RAW_PREFIX}/soi/release={period}/data.parquet"
    rows = con.execute(
        f"""
        SELECT DISTINCT adsh, cik, inlineurl
        FROM read_parquet('{uri}')
        WHERE inlineurl IS NOT NULL AND adsh IS NOT NULL
        """
    ).fetchall()
    return [{"adsh": r[0], "cik": r[1], "inlineurl": r[2]} for r in rows]


def _filing_row_counts(con: Any, period: str) -> dict[str, int]:
    """adsh → SOI investment-row count, for top-N filing selection in --sample."""
    uri = f"r2://{R2_BUCKET}/{R2_RAW_PREFIX}/soi/release={period}/data.parquet"
    rows = con.execute(
        f"SELECT adsh, COUNT(*) FROM read_parquet('{uri}') GROUP BY adsh"
    ).fetchall()
    return {r[0]: r[1] for r in rows}


# ---------------------------------------------------------------------------
# Per-period parse
# ---------------------------------------------------------------------------

def parse_period(
    period: str,
    con: Any,
    client: httpx.Client,
    s3: Any,
    *,
    apply: bool,
    sample: int | None,
    sample_ciks: set[str] | None,
    max_filings: int | None,
) -> dict:
    """Parse every filing in one period's soi.tsv; upload the parsed Parquet to
    R2. Returns coverage metrics."""
    filings = _load_soi_filings(con, period)
    LOG.info("[%s] %d filings in soi.tsv", period, len(filings))

    if sample_ciks:
        filings = [f for f in filings if str(f["cik"]) in sample_ciks]
        LOG.info("[%s] %d filings after --sample-ciks filter", period, len(filings))

    if sample:
        # bias the sample toward the largest filers (the 0%-structured BDCs the
        # e2 floor is measured on) — directive §"Volume floors §e2".
        counts = _filing_row_counts(con, period)
        filings.sort(key=lambda f: counts.get(f["adsh"], 0), reverse=True)
        filings = filings[:sample]
        LOG.info("[%s] sampling %d filings (largest by SOI row count)", period, len(filings))

    if max_filings:
        filings = filings[:max_filings]

    parsed_rows: list[dict] = []
    n_ok = n_err = 0
    for i, f in enumerate(filings):
        url = _resolve_filing_url(f["inlineurl"])
        try:
            resp = client.get(url)
            resp.raise_for_status()
            doc = resp.text
        except Exception as e:
            LOG.warning("[%s] fetch failed adsh=%s: %s", period, f["adsh"], e)
            n_err += 1
            continue
        try:
            invs = parse_filing_holdings(doc)
        except Exception as e:
            LOG.warning("[%s] parse failed adsh=%s: %s", period, f["adsh"], e)
            n_err += 1
            continue
        for inv in invs:
            inv["adsh"] = f["adsh"]
            inv["cik"] = f["cik"]
            inv["inlineurl"] = f["inlineurl"]
            inv["release"] = period
            parsed_rows.append(inv)
        n_ok += 1
        if (i + 1) % 20 == 0:
            LOG.info("[%s] %d/%d filings parsed", period, i + 1, len(filings))
        time.sleep(0.2)  # be polite to SEC servers

    total = len(parsed_rows)
    with_mat = sum(1 for r in parsed_rows if r["maturity_date"])
    metrics = {
        "period": period,
        "filings_ok": n_ok,
        "filings_err": n_err,
        "parsed_rows": total,
        "rows_with_maturity": with_mat,
        "maturity_coverage_pct": round(100.0 * with_mat / total, 1) if total else 0.0,
    }
    LOG.info("[%s] metrics: %s", period, metrics)

    if apply and parsed_rows:
        _write_parsed_parquet(con, s3, period, parsed_rows)

    return metrics


def _write_parsed_parquet(con: Any, s3: Any, period: str, parsed_rows: list[dict]) -> str:
    """Write the parsed investment rows to ZSTD Parquet and upload to R2
    sec-bdc/soi-parsed/release=<period>/data.parquet."""
    staging = Path("/tmp/sec-bdc-soi-parsed")
    staging.mkdir(parents=True, exist_ok=True)
    parquet_path = staging / f"{period}.parquet"

    con.execute("DROP TABLE IF EXISTS _parsed")
    con.execute(
        """
        CREATE TABLE _parsed (
            adsh VARCHAR, cik VARCHAR, release VARCHAR,
            portfolio_company_name VARCHAR, instrument_type VARCHAR,
            maturity_date VARCHAR, maturity_date_raw VARCHAR,
            fair_value DOUBLE, inlineurl VARCHAR
        )
        """
    )
    con.executemany(
        "INSERT INTO _parsed VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (
                r["adsh"], r["cik"], r["release"], r["portfolio_company_name"],
                r["instrument_type"], r["maturity_date"], r["maturity_date_raw"],
                r["fair_value"], r["inlineurl"],
            )
            for r in parsed_rows
        ],
    )
    con.execute(
        f"COPY _parsed TO '{parquet_path}' "
        f"(FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)"
    )
    key = f"{R2_PARSED_PREFIX}/release={period}/data.parquet"
    s3.upload_file(
        str(parquet_path), R2_BUCKET, key,
        ExtraArgs={"ContentType": "application/x-parquet"},  # NO ContentEncoding (L42)
    )
    LOG.info("[%s] uploaded %d parsed rows → R2 %s", period, len(parsed_rows), key)
    return key


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="SEC BDC SOI Inline-XBRL HTML parse")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--apply", action="store_true", help="Parse and upload to R2")
    mode.add_argument(
        "--dry-run", action="store_true",
        help="Parse + report coverage; no R2 upload",
    )
    ap.add_argument(
        "--periods", type=str, default=None,
        help="Comma-separated subset of release periods (default: all under "
             "R2 sec-bdc/soi/).",
    )
    ap.add_argument(
        "--sample", type=int, default=None,
        help="Parse only the N largest filings per period (coverage tuning).",
    )
    ap.add_argument(
        "--sample-ciks", type=str, default=None,
        help="Comma-separated BDC CIKs to restrict the sample to.",
    )
    ap.add_argument(
        "--max-filings", type=int, default=None,
        help="Hard cap on filings processed per period.",
    )
    args = ap.parse_args(argv)

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            LOG.error("FAIL: %s not set in environment", var)
            sys.exit(64)

    con = _connect_duckdb_to_r2()
    periods = _discover_soi_periods(con)
    if not periods:
        LOG.error(
            "FAIL: no periods found under r2://%s/%s/soi/release=*/ — run "
            "run_sec_bdc_soi_r2_ingest.py first", R2_BUCKET, R2_RAW_PREFIX,
        )
        sys.exit(1)

    if args.periods:
        requested = [p.strip().lower() for p in args.periods.split(",")]
        periods = [p for p in periods if p in requested]
        if not periods:
            LOG.error("FAIL: none of the requested periods present in R2: %s", requested)
            sys.exit(1)
    LOG.info("Periods to parse: %s", periods)

    sample_ciks = (
        {c.strip() for c in args.sample_ciks.split(",")} if args.sample_ciks else None
    )

    s3 = _make_s3_client()
    all_metrics: list[dict] = []
    with _make_client() as client:
        for period in periods:
            LOG.info("=" * 60)
            m = parse_period(
                period, con, client, s3,
                apply=args.apply,
                sample=args.sample,
                sample_ciks=sample_ciks,
                max_filings=args.max_filings,
            )
            all_metrics.append(m)

    # aggregate coverage across periods
    tot = sum(m["parsed_rows"] for m in all_metrics)
    tot_mat = sum(m["rows_with_maturity"] for m in all_metrics)
    agg_pct = round(100.0 * tot_mat / tot, 1) if tot else 0.0
    LOG.info("=" * 60)
    LOG.info(
        "DONE. parsed_rows=%d rows_with_maturity=%d aggregate_coverage=%.1f%% "
        "(generated_at=%s)",
        tot, tot_mat, agg_pct, datetime.now(timezone.utc).isoformat(),
    )


if __name__ == "__main__":
    main()

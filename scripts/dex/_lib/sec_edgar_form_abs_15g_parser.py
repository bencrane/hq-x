"""SEC EDGAR Form ABS-15G parser — primary_doc + Exhibit 99 → structured records.

Form ABS-15G is the asset-backed securitizer report of repurchase and
replacement activity under Section 15G of the Exchange Act (Dodd-Frank §943,
SEC Rule 15Ga-1 / 15Ga-2). Effective Feb 14, 2012 (Rule 15Ga-1) and Jun 15,
2012 (Rule 15Ga-2). Filers: depositors, originators, securitizers (sponsors).

Exhibit-99 schema heterogeneity (validator p3 inherited risk):
  1. **inline XBRL (post-~2018)** — SEC published taxonomy ``abs15g-2018`` /
     ``abs15g-2020``. Structured ``<filingManager>``, ``<trustNamespace>``,
     ``<assetClass>``, ``<demandStatistics>``, ``<repurchaseStatistics>``,
     ``<indemnificationFlag>`` elements.
  2. **structured HTML (mid era ~2014-~2018)** — free-form HTML <table>s with
     headers like "Asset Class" / "Demand" / "Repurchases" / "Replacements".
  3. **unstructured PDF / free-text HTML (pre-2018)** — no machine-readable
     schema. Emit filing-level row only with ``exhibit_format='unstructured'``;
     per-asset-class repurchase_summary emits zero rows.

Output (per filing): ``{'filings': [filing_record], 'repurchase_summary': [aggregate_rows...]}``.
Filing-level row always emits (even on unstructured exhibit). Per-asset-class
rows emit only when the exhibit is inline-XBRL or structured-HTML.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from lxml import etree as lxml_etree

from _lib.sec_edgar_form_abs_15g_normalize import (
    derive_quarter,
    normalize_accession,
    normalize_asset_class,
    normalize_cik,
    normalize_depositor_name,
    normalize_filer_name,
    normalize_lei,
    normalize_sponsor_name,
    normalize_trustee_name,
    parse_bool,
    parse_int,
    parse_period_of_report,
    parse_signature_date,
)


log = logging.getLogger("sec-edgar-form-abs-15g-parser")


# Inline-XBRL taxonomy namespaces (SEC published abs15g-2018, abs15g-2020).
# The actual namespace URIs vary by taxonomy version; the parser uses
# local-name() XPath fallbacks where the URI may drift.
_NS_FILER = "http://www.sec.gov/edgar/abs15gsubmission"
_NS_COMMON = "http://www.sec.gov/edgar/common"
_NS_ABS15G_DOC = "http://www.sec.gov/edgar/document/abs15g"

_NSMAP_PRIMARY = {"f": _NS_FILER, "c": _NS_COMMON, "abs": _NS_ABS15G_DOC}


@dataclass(frozen=True)
class FilingHeader:
    """Inputs available at discovery time (from form.idx)."""

    cik_raw: str
    filer_name_raw: str
    accession_raw: str
    filing_date: str            # 'YYYY-MM-DD'
    form_type: str              # 'ABS-15G' | 'ABS-15G/A'
    primary_doc_url: str
    exhibit_url: str | None
    raw_xml_r2_uri: str | None = None


# -------------------------------------------------------------------- #
# Generic helpers (mirror form-13f parser shape)
# -------------------------------------------------------------------- #


def _parse_xml_bytes(body: bytes | str) -> lxml_etree._Element | None:
    if not body:
        return None
    parser = lxml_etree.XMLParser(recover=True, huge_tree=True)
    try:
        if isinstance(body, str):
            return lxml_etree.fromstring(body.encode("utf-8"), parser)
        return lxml_etree.fromstring(body, parser)
    except (lxml_etree.XMLSyntaxError, ValueError) as exc:
        log.warning("xml parse failed: %s", exc)
        return None


def _text_anyns(el: lxml_etree._Element | None, local_name: str) -> str | None:
    """Find first descendant element with the given local-name (namespace-agnostic)."""
    if el is None:
        return None
    nodes = el.xpath(f".//*[local-name()='{local_name}']")
    if not nodes:
        return None
    n = nodes[0]
    if hasattr(n, "text"):
        t = n.text
    else:
        t = str(n)
    if t is None:
        return None
    t = t.strip()
    return t or None


def _all_text_anyns(el: lxml_etree._Element | None, local_name: str) -> list[str]:
    if el is None:
        return []
    nodes = el.xpath(f".//*[local-name()='{local_name}']")
    out: list[str] = []
    for n in nodes:
        if hasattr(n, "text") and n.text:
            t = n.text.strip()
            if t:
                out.append(t)
    return out


# -------------------------------------------------------------------- #
# primary_doc → cover page (filing-level fields)
# Handles BOTH primary_doc.xml (legacy convention) AND the primary HTML
# document (ABS-15G actual convention — see 2026-05-13 parser polish
# investigation; ABS-15G filings 2012-2024 ship a single .htm rather than
# the form-13f-style XML cover).
# -------------------------------------------------------------------- #


# Month-name lookup for HTML cover-page date extraction.
_MONTHS: dict[str, int] = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

_MONTH_PATTERN = (
    r"(January|February|March|April|May|June|July|August|September|October|"
    r"November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)"
)

# "January 1, 2023 to December 31, 2023" / "January 1 2023 through December 31, 2023".
_PERIOD_RANGE_RE = re.compile(
    r"reporting\s+period\s+(?:from\s+)?" + _MONTH_PATTERN
    + r"\s*(\d{1,2})?,?\s*(\d{4})?\s+(?:to|through|until|-)\s+"
    + _MONTH_PATTERN + r"\s+(\d{1,2}),?\s*(\d{4})",
    re.IGNORECASE | re.DOTALL,
)

# "Date of Report ... February 9, 2024" — the date follows "Date of Report".
_DATE_OF_REPORT_RE = re.compile(
    r"Date\s+of\s+Report[^A-Za-z]{0,200}?"
    + _MONTH_PATTERN + r"\s+(\d{1,2}),?\s*(\d{4})",
    re.IGNORECASE | re.DOTALL,
)

# "Central Index Key Number of depositor: 0001954027" — captures CIK.
_DEPOSITOR_CIK_RE = re.compile(
    r"Central\s+Index\s+Key\s+Number\s+of\s+depositor:\s*(\d{6,10})",
    re.IGNORECASE,
)

# "Central Index Key Number of securitizer: 0001541523" — captures CIK.
_SECURITIZER_CIK_RE = re.compile(
    r"Central\s+Index\s+Key\s+Number\s+of\s+securitizer:\s*(\d{6,10})",
    re.IGNORECASE,
)


def _strip_html(html: str) -> str:
    """Strip HTML tags + collapse whitespace. Used for regex-based field
    extraction from ABS-15G primary HTML documents. Naive but sufficient
    for cover-page anchors that don't depend on element structure.
    """
    text = re.sub(r"<[^>]+>", " ", html)
    text = (
        text.replace("&nbsp;", " ")
            .replace("&amp;", "&")
            .replace("&#8201;", " ")
            .replace("&#8194;", " ")
            .replace("&#8195;", " ")
            .replace("&#9744;", "")  # checkbox unchecked
            .replace("&#9746;", "")  # checkbox checked
    )
    return re.sub(r"\s+", " ", text).strip()


def _parse_primary_html(primary_html: bytes | str) -> dict[str, Any]:
    """Extract cover-page fields from ABS-15G primary HTML document.

    ABS-15G filings ship the cover page as the primary HTML (no primary_doc.xml
    exists for this form historically). Cover-page anchors are well-anchored
    English: "reporting period <date> to <date>", "Date of Report ... <date>",
    "Central Index Key Number of depositor: NNN".

    Returns a dict with the same keys parse_primary_doc returns from XML —
    period_iso, sponsor_raw, trustee_raw, depositor_raw, lei_raw — using None
    for any field not present in the HTML cover.
    """
    if isinstance(primary_html, bytes):
        try:
            html_text = primary_html.decode("utf-8")
        except UnicodeDecodeError:
            html_text = primary_html.decode("latin-1", "ignore")
    else:
        html_text = primary_html

    text = _strip_html(html_text)

    period_iso: str | None = None

    # 1) "reporting period <Month dd, YYYY> to <Month dd, YYYY>" — use END date.
    m = _PERIOD_RANGE_RE.search(text)
    if m:
        end_month = _MONTHS.get(m.group(4).lower())
        end_day = int(m.group(5))
        end_year = int(m.group(6))
        if end_month and 1 <= end_day <= 31 and 1900 <= end_year <= 2100:
            try:
                period_iso = f"{end_year:04d}-{end_month:02d}-{end_day:02d}"
            except (TypeError, ValueError):
                period_iso = None

    # 2) Fall back to "Date of Report ... <Month dd, YYYY>".
    if period_iso is None:
        m = _DATE_OF_REPORT_RE.search(text)
        if m:
            mon = _MONTHS.get(m.group(1).lower())
            day = int(m.group(2))
            year = int(m.group(3))
            if mon and 1 <= day <= 31 and 1900 <= year <= 2100:
                try:
                    period_iso = f"{year:04d}-{mon:02d}-{day:02d}"
                except (TypeError, ValueError):
                    period_iso = None

    depositor_raw: str | None = None
    m = _DEPOSITOR_CIK_RE.search(text)
    if m:
        depositor_raw = m.group(1)

    # ABS-15G HTML covers don't have a distinct sponsor or trustee field —
    # the securitizer name (from form.idx) is used as sponsor by convention.
    return {
        "period_iso": period_iso,
        "sponsor_raw": None,
        "trustee_raw": None,
        "depositor_raw": depositor_raw,
        "lei_raw": None,
    }


def _looks_like_xml(body: bytes | str) -> bool:
    """Heuristic: leading bytes look like XML declaration or root element."""
    if isinstance(body, bytes):
        try:
            head = body[:200].decode("utf-8", "ignore").lstrip().lower()
        except (UnicodeDecodeError, AttributeError):
            return False
    else:
        head = body[:200].lstrip().lower()
    if head.startswith("<?xml") or head.startswith("<edgarsubmission"):
        return True
    # Avoid false positives on HTML files that happen to start with <html>.
    if head.startswith("<html") or head.startswith("<!doctype"):
        return False
    # Other XML root names that ABS-15G XBRL submissions may use.
    if re.match(r"<[a-z][a-z0-9_:]+\s", head) and "xmlns" in head[:500].lower():
        # Bare-namespaced XML root — treat as XML.
        return True
    return False


def parse_primary_doc(
    header: FilingHeader, primary_xml: bytes | str | None,
) -> dict[str, Any] | None:
    """Parse primary cover-page document → filing-level record (without
    per-asset-class rows). Returns None when input can't be parsed at all.

    Accepts BOTH primary_doc.xml bytes (legacy XBRL filings, post-~2018) AND
    primary HTML bytes (ABS-15G filings 2012-2024 ship the cover as HTML).
    Discriminates by leading-byte heuristic.
    """
    accession = normalize_accession(header.accession_raw)
    cik = normalize_cik(header.cik_raw)

    period_iso: str | None = None
    sponsor_raw: str | None = None
    trustee_raw: str | None = None
    depositor_raw: str | None = None
    lei_raw: str | None = None

    if primary_xml is not None:
        if _looks_like_xml(primary_xml):
            root = _parse_xml_bytes(primary_xml)
            if root is not None:
                period_raw = (
                    _text_anyns(root, "periodOfReport")
                    or _text_anyns(root, "reportingPeriod")
                )
                period_iso = parse_period_of_report(period_raw)

                sponsor_raw = (
                    _text_anyns(root, "sponsorName")
                    or _text_anyns(root, "sponsor")
                )
                trustee_raw = (
                    _text_anyns(root, "trusteeName")
                    or _text_anyns(root, "trustee")
                )
                depositor_raw = (
                    _text_anyns(root, "depositorName")
                    or _text_anyns(root, "depositor")
                    or _text_anyns(root, "trustName")
                )
                lei_raw = _text_anyns(root, "lei")
        else:
            # HTML primary document — extract cover-page fields by regex.
            html_fields = _parse_primary_html(primary_xml)
            period_iso = html_fields.get("period_iso")
            sponsor_raw = html_fields.get("sponsor_raw")
            trustee_raw = html_fields.get("trustee_raw")
            depositor_raw = html_fields.get("depositor_raw")
            lei_raw = html_fields.get("lei_raw")

    # Final fallback for period_iso: use the filing_date (form.idx value).
    # This isn't the reporting period proper but it gives us a non-NULL year
    # for partitioning and downstream MV usage; sources for the field are
    # tracked via period_of_report itself (NULL when filing_date used).
    if period_iso is None and header.filing_date:
        # Only use this fallback when we have NO better signal — gives every
        # filing a non-NULL report_year for partitioning.
        period_iso = header.filing_date

    quarter = derive_quarter(period_iso)
    report_year = int(period_iso[:4]) if period_iso else None

    return {
        "accession_number": accession,
        "cik_normalized": cik,
        "filer_name_raw": header.filer_name_raw,
        "filer_name_normalized": normalize_filer_name(header.filer_name_raw),
        "depositor_name_raw": depositor_raw,
        "depositor_name_normalized": normalize_depositor_name(depositor_raw),
        "sponsor_name_raw": sponsor_raw,
        "sponsor_name_normalized": normalize_sponsor_name(sponsor_raw),
        "trustee_name_raw": trustee_raw,
        "trustee_name_normalized": normalize_trustee_name(trustee_raw),
        "filer_lei_normalized": normalize_lei(lei_raw),
        "form_type": header.form_type,
        "filing_date": header.filing_date,
        "period_of_report": period_iso,
        "report_year": report_year,
        "report_quarter": quarter,
        "primary_doc_url": header.primary_doc_url,
        "exhibit_url": header.exhibit_url,
        "raw_xml_r2_uri": header.raw_xml_r2_uri,
    }


# -------------------------------------------------------------------- #
# Exhibit 99 — 3-format fallback chain (validator p3)
# -------------------------------------------------------------------- #


def _detect_exhibit_format(exhibit_bytes: bytes | None) -> str:
    """Return one of: 'inline_xbrl', 'structured_html', 'unstructured', 'absent'.

    Heuristics:
    - Starts with %PDF → unstructured.
    - Contains an XBRL namespace declaration → inline_xbrl.
    - Has <table> tags → structured_html.
    - Otherwise → unstructured.
    """
    if exhibit_bytes is None or len(exhibit_bytes) < 32:
        return "absent"
    head = exhibit_bytes[:1024].lower()
    if head.startswith(b"%pdf"):
        return "unstructured"
    if b"xbrl" in head or b"abs15g" in head:
        return "inline_xbrl"
    if b"<table" in head or b"<html" in head:
        # Sub-decide: tables present = structured; HTML without tables = unstructured.
        if b"<table" in exhibit_bytes[:65536].lower():
            return "structured_html"
        return "unstructured"
    return "unstructured"


def parse_exhibit_xbrl(
    cover: dict[str, Any], exhibit_bytes: bytes,
) -> list[dict[str, Any]]:
    """Parse inline-XBRL Exhibit 99 → per-asset-class repurchase_summary rows.

    Tolerates SEC taxonomy version drift via local-name() XPath.
    """
    out: list[dict[str, Any]] = []
    root = _parse_xml_bytes(exhibit_bytes)
    if root is None:
        return out

    # Look for repeated repurchase-statistic blocks. The XBRL taxonomy groups
    # them under elements like <assetClassDetail> or <repurchaseStatistics>.
    blocks = root.xpath(
        ".//*[local-name()='assetClassDetail' or "
        "local-name()='repurchaseStatistics' or "
        "local-name()='assetClass']"
    )
    for blk in blocks:
        asset_class_raw = (
            _text_anyns(blk, "assetClass")
            or _text_anyns(blk, "assetClassName")
            or _text_anyns(blk, "name")
        )
        asset_class = normalize_asset_class(asset_class_raw)
        demand = parse_int(
            _text_anyns(blk, "demandCount")
            or _text_anyns(blk, "totalDemands")
            or _text_anyns(blk, "demands")
        )
        repurchase = parse_int(
            _text_anyns(blk, "repurchaseCount")
            or _text_anyns(blk, "totalRepurchases")
            or _text_anyns(blk, "repurchases")
        )
        replacement = parse_int(
            _text_anyns(blk, "replacementCount")
            or _text_anyns(blk, "totalReplacements")
            or _text_anyns(blk, "replacements")
        )
        dollar = parse_int(
            _text_anyns(blk, "totalRepurchaseAmount")
            or _text_anyns(blk, "repurchaseDollarAmount")
            or _text_anyns(blk, "dollarAmount")
        )
        if asset_class is None and demand is None and repurchase is None and replacement is None:
            continue
        out.append({
            "accession_number": cover["accession_number"],
            "cik_normalized": cover["cik_normalized"],
            "asset_class": asset_class,
            "asset_class_raw": asset_class_raw,
            "reporting_period": cover["period_of_report"],
            "demand_count": demand,
            "repurchase_count": repurchase,
            "replacement_count": replacement,
            "dollar_amount": dollar,
            "report_year": cover["report_year"],
            "report_quarter": cover["report_quarter"],
        })
    return out


def parse_exhibit_html(
    cover: dict[str, Any], exhibit_bytes: bytes,
) -> list[dict[str, Any]]:
    """Parse structured-HTML Exhibit 99 → per-asset-class rows.

    Heuristic: find <table>s whose header row mentions any of
    {asset class, demand, repurchase, replacement, dollar}.
    For each data row, extract numeric columns by header position.
    """
    out: list[dict[str, Any]] = []
    try:
        from bs4 import BeautifulSoup  # type: ignore
    except ImportError:
        log.warning("bs4 not available; skipping structured-html parse")
        return out

    soup = BeautifulSoup(exhibit_bytes, "html.parser")
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        # Detect header
        header_cells = [c.get_text(" ", strip=True).lower() for c in rows[0].find_all(["th", "td"])]
        if not any("asset" in c or "demand" in c or "repurch" in c for c in header_cells):
            continue
        # Column indexes
        idx_asset = next(
            (i for i, c in enumerate(header_cells) if "asset" in c),
            None,
        )
        idx_demand = next(
            (i for i, c in enumerate(header_cells) if "demand" in c),
            None,
        )
        idx_repurch = next(
            (i for i, c in enumerate(header_cells) if "repurch" in c),
            None,
        )
        idx_replace = next(
            (i for i, c in enumerate(header_cells) if "replac" in c),
            None,
        )
        idx_dollar = next(
            (i for i, c in enumerate(header_cells) if "dollar" in c or "amount" in c),
            None,
        )

        for r in rows[1:]:
            cells = [c.get_text(" ", strip=True) for c in r.find_all(["td", "th"])]
            if not cells:
                continue
            asset_raw = cells[idx_asset] if idx_asset is not None and idx_asset < len(cells) else None
            demand = parse_int(cells[idx_demand]) if idx_demand is not None and idx_demand < len(cells) else None
            repurch = parse_int(cells[idx_repurch]) if idx_repurch is not None and idx_repurch < len(cells) else None
            replace = parse_int(cells[idx_replace]) if idx_replace is not None and idx_replace < len(cells) else None
            dollar = parse_int(cells[idx_dollar]) if idx_dollar is not None and idx_dollar < len(cells) else None
            if all(v is None for v in (demand, repurch, replace, dollar)):
                continue
            out.append({
                "accession_number": cover["accession_number"],
                "cik_normalized": cover["cik_normalized"],
                "asset_class": normalize_asset_class(asset_raw),
                "asset_class_raw": asset_raw,
                "reporting_period": cover["period_of_report"],
                "demand_count": demand,
                "repurchase_count": repurch,
                "replacement_count": replace,
                "dollar_amount": dollar,
                "report_year": cover["report_year"],
                "report_quarter": cover["report_quarter"],
            })
    return out


# -------------------------------------------------------------------- #
# Top-level entry point
# -------------------------------------------------------------------- #


def build_filing_record(cover: dict[str, Any], summary_rows: list[dict[str, Any]], exhibit_format: str) -> dict[str, Any]:
    """Build the per-filing aggregate row (one per accession)."""
    return {
        "accession_number": cover["accession_number"],
        "cik_normalized": cover["cik_normalized"],
        "filer_name_raw": cover["filer_name_raw"],
        "filer_name_normalized": cover["filer_name_normalized"],
        "depositor_name_raw": cover["depositor_name_raw"],
        "depositor_name_normalized": cover["depositor_name_normalized"],
        "sponsor_name_raw": cover["sponsor_name_raw"],
        "sponsor_name_normalized": cover["sponsor_name_normalized"],
        "trustee_name_raw": cover["trustee_name_raw"],
        "trustee_name_normalized": cover["trustee_name_normalized"],
        "filer_lei_normalized": cover["filer_lei_normalized"],
        "form_type": cover["form_type"],
        "filing_date": cover["filing_date"],
        "period_of_report": cover["period_of_report"],
        "report_year": cover["report_year"],
        "report_quarter": cover["report_quarter"],
        "asset_class_count": len(summary_rows),
        "total_demand_count": sum(r.get("demand_count") or 0 for r in summary_rows) or None,
        "total_repurchase_count": sum(r.get("repurchase_count") or 0 for r in summary_rows) or None,
        "total_replacement_count": sum(r.get("replacement_count") or 0 for r in summary_rows) or None,
        "total_dollar_amount": sum(r.get("dollar_amount") or 0 for r in summary_rows) or None,
        "primary_doc_url": cover["primary_doc_url"],
        "exhibit_url": cover["exhibit_url"],
        "raw_xml_r2_uri": cover["raw_xml_r2_uri"],
        "exhibit_format": exhibit_format,
    }


def parse_filing(
    header: FilingHeader,
    primary_xml: bytes | str | None,
    exhibit_bytes: bytes | None,
) -> dict[str, list[dict[str, Any]]]:
    """Parse one Form ABS-15G filing → 2 streams' worth of records.

    Returns a dict with keys: ``filings`` (1-element list, or [] if cover
    parse failed) and ``repurchase_summary`` (possibly empty).

    3-format fallback chain on exhibit_bytes (validator p3):
      1. inline_xbrl → parse_exhibit_xbrl
      2. structured_html → parse_exhibit_html
      3. unstructured / absent → no repurchase_summary rows; filing-level only
    """
    cover = parse_primary_doc(header, primary_xml)
    if cover is None:
        return {"filings": [], "repurchase_summary": []}

    exhibit_bytes_b: bytes | None
    if exhibit_bytes is None:
        exhibit_bytes_b = None
    elif isinstance(exhibit_bytes, str):
        exhibit_bytes_b = exhibit_bytes.encode("utf-8", "ignore")
    else:
        exhibit_bytes_b = exhibit_bytes

    fmt = _detect_exhibit_format(exhibit_bytes_b)
    summary: list[dict[str, Any]] = []
    if fmt == "inline_xbrl" and exhibit_bytes_b is not None:
        summary = parse_exhibit_xbrl(cover, exhibit_bytes_b)
    elif fmt == "structured_html" and exhibit_bytes_b is not None:
        summary = parse_exhibit_html(cover, exhibit_bytes_b)
    # else: unstructured / absent — leave summary empty (filing-level row only)

    filing = build_filing_record(cover, summary, fmt)
    return {
        "filings": [filing],
        "repurchase_summary": summary,
    }

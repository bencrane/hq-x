"""SEC EDGAR Form 144 parser — XML-first, HTML-fallback.

Form 144 is filed in two formats across the 2010–2024 era:

- **XML** (``primary_doc.xml`` etc.): the post-2013 SEC schema
  ``http://www.sec.gov/edgar/ownership``. Clean structured access; the
  vast majority of modern filings (2023–2024 e-filing-mandate era).
- **HTML** (legacy paper-form rendering): pre-mandate filings, plus any
  modern filing whose XML attachment is missing. Heuristic text-label
  anchoring on the standard Form 144 numbered fields.

Parser output (per filing) — four lists of dicts:

    filings: [filing_meta]                   # always exactly 1 record
    securities_to_be_sold: [...]             # forward-looking proposed sale (1)
    securities_sold_past_3_months: [...]     # backward 3-month window (0+)
    acquisition_info: [...]                  # acquisition lots (0+)

Schema details: see directive
~/Desktop/hq/directives/2026-05-09-sec-edgar-form-144-r2-ingest.md
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from lxml import etree as lxml_etree
from lxml import html as lxml_html

from _lib.sec_edgar_form_144_normalize import (
    normalize_accession,
    normalize_broker,
    normalize_cik,
    normalize_filer_name,
    normalize_person_name,
    normalize_relationship,
    parse_dollar_amount,
    parse_form_144_date,
)


log = logging.getLogger("sec-edgar-form-144-parser")


# -------------------------------------------------------------------- #
# Public types
# -------------------------------------------------------------------- #


@dataclass(frozen=True)
class FilingHeader:
    """Inputs available at discovery time (from form.idx + EDGAR index)."""

    cik_raw: str             # issuer CIK as found in form.idx
    filer_name_raw: str      # issuer/company name as found in form.idx
    accession_raw: str
    form_type: str           # "144" | "144/A"
    filing_date: str         # 'YYYY-MM-DD'
    primary_doc_url: str
    primary_doc_format: str  # "xml" | "html"
    raw_doc_r2_uri: str | None = None
    period_of_report: str | None = None


# -------------------------------------------------------------------- #
# XML parser (modern e-filing schema)
# -------------------------------------------------------------------- #


_NS = {
    "e": "http://www.sec.gov/edgar/ownership",
    "com": "http://www.sec.gov/edgar/common",
}


def _xml_text(el, tag: str, ns_prefix: str = "e") -> str | None:
    """Find single child by tag, return stripped text or None."""
    if el is None:
        return None
    found = el.find(f"{ns_prefix}:{tag}", _NS)
    if found is None:
        # Some filings omit the default namespace declaration.
        found = el.find(tag)
    if found is None or found.text is None:
        return None
    txt = found.text.strip()
    return txt or None


def _xml_findall(el, path: str, ns_prefix: str = "e") -> list:
    if el is None:
        return []
    found = el.findall(f"{ns_prefix}:{path}", _NS)
    if found:
        return found
    # Namespace-stripped fallback.
    return el.findall(path)


def _parse_xml_filing(
    header: FilingHeader,
    doc: Any,
) -> dict[str, list[dict[str, Any]]]:
    cik = normalize_cik(header.cik_raw)
    accession = normalize_accession(header.accession_raw)
    out: dict[str, list[dict[str, Any]]] = {
        "filings": [],
        "securities_to_be_sold": [],
        "securities_sold_past_3_months": [],
        "acquisition_info": [],
    }

    form_data = doc.find("e:formData", _NS)
    if form_data is None:
        form_data = doc.find("formData")
    if form_data is None:
        return out

    # ---- issuerInfo ----
    issuer_info = form_data.find("e:issuerInfo", _NS)
    if issuer_info is None:
        issuer_info = form_data.find("issuerInfo")

    issuer_cik = _xml_text(issuer_info, "issuerCik")
    issuer_name = _xml_text(issuer_info, "issuerName")
    person_signing = _xml_text(
        issuer_info, "nameOfPersonForWhoseAccountTheSecuritiesAreToBeSold"
    )

    relationships = []
    rels_el = issuer_info.find("e:relationshipsToIssuer", _NS) if issuer_info is not None else None
    if rels_el is None and issuer_info is not None:
        rels_el = issuer_info.find("relationshipsToIssuer")
    if rels_el is not None:
        for r in _xml_findall(rels_el, "relationshipToIssuer"):
            if r.text and r.text.strip():
                relationships.append(r.text.strip())
    relationship_raw = "; ".join(relationships) if relationships else None

    # Prefer the explicit issuer CIK from XML over the (filer-CIK) form.idx value;
    # form.idx records the FILER cik, not the issuer cik.
    cik_normalized = normalize_cik(issuer_cik) or cik
    issuer_legal_normalized = normalize_filer_name(issuer_name)

    person_first, person_last = normalize_person_name(person_signing)

    # ---- noticeSignature → noticeDate / period_of_report ----
    notice_sig = form_data.find("e:noticeSignature", _NS)
    if notice_sig is None:
        notice_sig = form_data.find("noticeSignature")
    notice_date_raw = _xml_text(notice_sig, "noticeDate") if notice_sig is not None else None
    notice_date = parse_form_144_date(notice_date_raw)

    form_144_year = (
        int(header.filing_date[:4])
        if header.filing_date and len(header.filing_date) >= 4
        else None
    )

    filing_meta = {
        "accession_number": accession,
        "cik_normalized": cik_normalized,
        "form_type": header.form_type,
        "amendment_number": 1 if header.form_type == "144/A" else 0,
        "issuer_legal_name_normalized": issuer_legal_normalized,
        "issuer_lei_normalized": None,
        "filer_legal_name_normalized": None,
        "person_signing_name": person_signing,
        "person_first_normalized": person_first,
        "person_last_normalized": person_last,
        "person_relationship_to_issuer": relationship_raw,
        "person_relationship_to_issuer_normalized": normalize_relationship(relationship_raw),
        "filing_date": header.filing_date,
        "period_of_report": notice_date,
        "form_144_year": form_144_year,
        "xml_or_html": "xml",
        "raw_doc_r2_uri": header.raw_doc_r2_uri,
    }
    out["filings"].append(filing_meta)

    # ---- securitiesInformation → securities_to_be_sold (forward-looking) ----
    sec_info = form_data.find("e:securitiesInformation", _NS)
    if sec_info is None:
        sec_info = form_data.find("securitiesInformation")
    if sec_info is not None:
        broker_el = sec_info.find("e:brokerOrMarketmakerDetails", _NS)
        if broker_el is None:
            broker_el = sec_info.find("brokerOrMarketmakerDetails")
        broker_name = _xml_text(broker_el, "name") if broker_el is not None else None

        out["securities_to_be_sold"].append({
            "accession_number": accession,
            "cik_normalized": cik_normalized,
            "person_signing_name": person_signing,
            "person_first_normalized": person_first,
            "person_last_normalized": person_last,
            "class_of_securities": _xml_text(sec_info, "securitiesClassTitle"),
            "aggregate_market_value": parse_dollar_amount(
                _xml_text(sec_info, "aggregateMarketValue")
            ),
            "number_of_shares": parse_dollar_amount(
                _xml_text(sec_info, "noOfUnitsSold")
            ),
            "name_of_broker": broker_name,
            "broker_normalized": normalize_broker(broker_name),
            "approximate_sale_date": parse_form_144_date(
                _xml_text(sec_info, "approxSaleDate")
            ),
            "form_144_year": form_144_year,
        })

    # ---- securitiesToBeSold (one per acquisition lot) → acquisition_info ----
    for lot in _xml_findall(form_data, "securitiesToBeSold"):
        nature_raw = _xml_text(lot, "natureOfAcquisitionTransaction")
        payor = _xml_text(lot, "nameOfPersonfromWhomAcquired")
        out["acquisition_info"].append({
            "accession_number": accession,
            "cik_normalized": cik_normalized,
            "person_signing_name": person_signing,
            "date_acquired": parse_form_144_date(_xml_text(lot, "acquiredDate")),
            "nature_of_acquisition": nature_raw,
            "nature_normalized": nature_raw.lower().strip() if nature_raw else None,
            "payor_identity": payor,
            "payor_normalized": normalize_filer_name(payor),
            "cost_basis_per_share": None,
            "form_144_year": form_144_year,
        })

    # ---- securitiesSoldInPast3Months → securities_sold_past_3_months ----
    for sold in _xml_findall(form_data, "securitiesSoldInPast3Months"):
        seller_el = sold.find("e:sellerDetails", _NS)
        if seller_el is None:
            seller_el = sold.find("sellerDetails")
        seller_name = _xml_text(seller_el, "name") if seller_el is not None else None
        out["securities_sold_past_3_months"].append({
            "accession_number": accession,
            "cik_normalized": cik_normalized,
            "person_signing_name": person_signing,
            "person_first_normalized": person_first,
            "person_last_normalized": person_last,
            "seller_name": seller_name,
            "seller_normalized": normalize_filer_name(seller_name),
            "class_of_securities": _xml_text(sold, "securitiesClassTitle"),
            "sale_date": parse_form_144_date(_xml_text(sold, "saleDate")),
            "shares_sold": parse_dollar_amount(_xml_text(sold, "amountOfSecuritiesSold")),
            "gross_proceeds": parse_dollar_amount(_xml_text(sold, "grossProceeds")),
            "form_144_year": form_144_year,
        })

    return out


# -------------------------------------------------------------------- #
# HTML parser (legacy paper-form rendering)
#
# The pre-2013 Form 144 HTML is a paper-form rendering: a wide table with
# label cells in one row and value cells in the row immediately below at
# the matching column index. The reliable extraction is per-table:
#   1. Walk each table's rows.
#   2. For each row containing a known Form 144 label, record (label, col).
#   3. For value cells, take the cell at the same col in the NEXT row(s).
# Document-text-flow heuristics fail because labels run consecutively in
# document order ("(a) NAME OF ISSUER", "(b) IRS IDENT. NO.", ...) BEFORE
# any of their values appear.
# -------------------------------------------------------------------- #


_NBSP_RE = re.compile(r"&nbsp;|\xa0")
_WS_INLINE_RE = re.compile(r"[ \t]+")


def _cell_text(el) -> str:
    """Whitespace-collapsed text content of an lxml cell."""
    if el is None:
        return ""
    txt = el.text_content() or ""
    txt = _NBSP_RE.sub(" ", txt)
    return _WS_INLINE_RE.sub(" ", txt).strip()


def _table_rows_with_cells(tbl) -> list[list[str]]:
    """Return [[cell_text, ...], ...] for every <tr> in the table."""
    out: list[list[str]] = []
    for tr in tbl.findall(".//tr"):
        cells = [_cell_text(c) for c in tr.findall(".//td") + tr.findall(".//th")]
        out.append(cells)
    return out


_LABEL_NAME_OF_ISSUER = re.compile(r"NAME\s+OF\s+ISSUER", re.I)
_LABEL_PERSON = re.compile(r"NAME\s+OF\s+PERSON\s+FOR\s+WHOSE\s+ACCOUNT", re.I)
_LABEL_RELATIONSHIP = re.compile(r"RELATIONSHIP\s+TO\s+ISSUER", re.I)
_LABEL_TITLE_OF_CLASS = re.compile(r"TITLE\s+OF\s+(?:THE\s+)?CLASS", re.I)
_LABEL_AGG_MARKET_VAL = re.compile(r"AGGREGATE\s+MARKET\s+VALUE", re.I)
_LABEL_APPROX_SALE = re.compile(r"APPROXIMATE\s+DATE\s+OF\s+SALE", re.I)
_LABEL_NAME_OF_BROKER = re.compile(r"NAME\s+(?:AND\s+ADDRESS\s+)?OF\s+(?:EACH\s+)?BROKER", re.I)
_LABEL_NUM_SHARES = re.compile(r"(?:NUMBER|AMOUNT)\s+OF\s+SHARES", re.I)
_LABEL_DATE_OF_ACQ = re.compile(r"DATE\s+(?:YOU\s+)?ACQUIRED", re.I)
_LABEL_NATURE_OF_ACQ = re.compile(r"NATURE\s+OF\s+(?:THE\s+)?ACQUISITION", re.I)

_SKIP_VALUE_TOKENS = frozenset({
    "STREET", "CITY", "STATE", "ZIP CODE", "AREA", "CODE", "NUMBER",
    "(B)", "(C)", "(D)", "(E)", "(A)", "WORK LOCATION", "CUSIP NUMBER",
    "DOCUMENT SEQUENCE NO.", "ATTENTION:", "SEC USE ONLY",
})

# Numeric-only / dash-only value cells. Used to skip when looking for textual
# values (issuer name, person name, broker name, etc.).
_NUMERIC_VALUE_RE = re.compile(r"^[\$\d,\.\s\-]+$")
# Anything that contains a sub-form letter marker like "(a)", "(b)" is a label.
_LABEL_MARKER_RE = re.compile(r"\b\([a-z]\)\s", re.I)


def _is_label_cell(text: str) -> bool:
    if not text:
        return True
    upper = text.upper().strip()
    if upper in _SKIP_VALUE_TOKENS:
        return True
    if _LABEL_MARKER_RE.search(text):
        return True
    return False


def _value_at_col_below(
    rows: list[list[str]], label_row: int, label_col: int,
    *, skip_numeric: bool = False, max_rows_below: int = 3,
) -> str | None:
    """For a label found at (label_row, label_col), look at the same column
    in subsequent rows for a non-label value cell. Returns the first
    matching cell text or None.
    """
    for r in range(label_row + 1, min(label_row + 1 + max_rows_below, len(rows))):
        if label_col >= len(rows[r]):
            continue
        cand = rows[r][label_col].strip()
        if _is_label_cell(cand):
            continue
        if skip_numeric and _NUMERIC_VALUE_RE.match(cand):
            continue
        if not skip_numeric and not _NUMERIC_VALUE_RE.match(cand) and not cand:
            continue
        return cand
    return None


def _find_label_in_tables(
    tables: list, label_re: re.Pattern[str],
) -> tuple[int, int, int] | None:
    """Walk every cell of every table; return (table_idx, row, col) of the
    first cell whose text matches ``label_re``."""
    for ti, tbl in enumerate(tables):
        for ri, row in enumerate(tbl):
            for ci, cell in enumerate(row):
                if cell and label_re.search(cell):
                    return (ti, ri, ci)
    return None


def _parse_html_filing(
    header: FilingHeader,
    doc: Any,
) -> dict[str, list[dict[str, Any]]]:
    cik = normalize_cik(header.cik_raw)
    accession = normalize_accession(header.accession_raw)
    out: dict[str, list[dict[str, Any]]] = {
        "filings": [],
        "securities_to_be_sold": [],
        "securities_sold_past_3_months": [],
        "acquisition_info": [],
    }

    tables_raw = list(doc.iter("table"))
    tables: list[list[list[str]]] = [
        _table_rows_with_cells(t) for t in tables_raw
    ]
    tables = [t for t in tables if t]

    def _lookup_text(
        label_re: re.Pattern[str], *, skip_numeric: bool = False,
    ) -> str | None:
        hit = _find_label_in_tables(tables, label_re)
        if hit is None:
            return None
        ti, ri, ci = hit
        return _value_at_col_below(
            tables[ti], ri, ci, skip_numeric=skip_numeric,
        )

    def _lookup_numeric(label_re: re.Pattern[str]) -> float | None:
        """For dollar/share value lookup: find the label, then take the first
        numeric-only cell at the same column in the next 5 rows. If nothing
        in the same column, scan the next row's cells for any numeric."""
        hit = _find_label_in_tables(tables, label_re)
        if hit is None:
            return None
        ti, ri, ci = hit
        rows = tables[ti]
        for r in range(ri + 1, min(ri + 1 + 5, len(rows))):
            row = rows[r]
            if ci < len(row) and _NUMERIC_VALUE_RE.match(row[ci]):
                v = parse_dollar_amount(row[ci])
                if v is not None and v > 0:
                    return v
        for r in range(ri + 1, min(ri + 1 + 3, len(rows))):
            for cand in rows[r]:
                if _NUMERIC_VALUE_RE.match(cand):
                    v = parse_dollar_amount(cand)
                    if v is not None and v > 1:
                        return v
        return None

    issuer_name = _lookup_text(_LABEL_NAME_OF_ISSUER, skip_numeric=True)
    # Fall back to the form.idx-supplied name when the HTML extraction misses;
    # the form.idx record IS authoritative for issuer name.
    if not issuer_name and header.filer_name_raw:
        issuer_name = header.filer_name_raw

    person_signing = _lookup_text(_LABEL_PERSON, skip_numeric=True)
    relationship_raw = _lookup_text(_LABEL_RELATIONSHIP, skip_numeric=True)

    person_first, person_last = normalize_person_name(person_signing)
    issuer_legal_normalized = normalize_filer_name(issuer_name)
    cik_normalized = cik

    form_144_year = (
        int(header.filing_date[:4])
        if header.filing_date and len(header.filing_date) >= 4
        else None
    )

    filing_meta = {
        "accession_number": accession,
        "cik_normalized": cik_normalized,
        "form_type": header.form_type,
        "amendment_number": 1 if header.form_type == "144/A" else 0,
        "issuer_legal_name_normalized": issuer_legal_normalized,
        "issuer_lei_normalized": None,
        "filer_legal_name_normalized": None,
        "person_signing_name": person_signing,
        "person_first_normalized": person_first,
        "person_last_normalized": person_last,
        "person_relationship_to_issuer": relationship_raw,
        "person_relationship_to_issuer_normalized": normalize_relationship(relationship_raw),
        "filing_date": header.filing_date,
        "period_of_report": None,
        "form_144_year": form_144_year,
        "xml_or_html": "html",
        "raw_doc_r2_uri": header.raw_doc_r2_uri,
    }
    out["filings"].append(filing_meta)

    class_of_securities = _lookup_text(_LABEL_TITLE_OF_CLASS, skip_numeric=True)
    agg_market_val = _lookup_numeric(_LABEL_AGG_MARKET_VAL)
    num_shares = _lookup_numeric(_LABEL_NUM_SHARES)
    approx_sale_date_raw = _lookup_text(_LABEL_APPROX_SALE)
    broker_name = _lookup_text(_LABEL_NAME_OF_BROKER, skip_numeric=True)

    out["securities_to_be_sold"].append({
        "accession_number": accession,
        "cik_normalized": cik_normalized,
        "person_signing_name": person_signing,
        "person_first_normalized": person_first,
        "person_last_normalized": person_last,
        "class_of_securities": class_of_securities,
        "aggregate_market_value": agg_market_val,
        "number_of_shares": num_shares,
        "name_of_broker": broker_name,
        "broker_normalized": normalize_broker(broker_name),
        "approximate_sale_date": parse_form_144_date(approx_sale_date_raw) if approx_sale_date_raw else None,
        "form_144_year": form_144_year,
    })

    date_acq_raw = _lookup_text(_LABEL_DATE_OF_ACQ)
    nature_raw = _lookup_text(_LABEL_NATURE_OF_ACQ, skip_numeric=True)
    if date_acq_raw or nature_raw:
        out["acquisition_info"].append({
            "accession_number": accession,
            "cik_normalized": cik_normalized,
            "person_signing_name": person_signing,
            "date_acquired": parse_form_144_date(date_acq_raw) if date_acq_raw else None,
            "nature_of_acquisition": nature_raw,
            "nature_normalized": nature_raw.lower().strip() if nature_raw else None,
            "payor_identity": None,
            "payor_normalized": None,
            "cost_basis_per_share": None,
            "form_144_year": form_144_year,
        })

    return out


# -------------------------------------------------------------------- #
# Top-level entry point
# -------------------------------------------------------------------- #


def parse_filing(
    header: FilingHeader,
    primary_doc: bytes,
) -> dict[str, list[dict[str, Any]]]:
    """Parse one Form 144 / 144/A filing → structured records.

    Routes on ``header.primary_doc_format``. On any parse exception, returns
    a header-only ``filings`` row (so failed extracts are still counted) plus
    empty secondary streams.
    """
    cik = normalize_cik(header.cik_raw)
    accession = normalize_accession(header.accession_raw)
    form_144_year = (
        int(header.filing_date[:4])
        if header.filing_date and len(header.filing_date) >= 4
        else None
    )

    fallback: dict[str, list[dict[str, Any]]] = {
        "filings": [{
            "accession_number": accession,
            "cik_normalized": cik,
            "form_type": header.form_type,
            "amendment_number": 1 if header.form_type == "144/A" else 0,
            "issuer_legal_name_normalized": normalize_filer_name(header.filer_name_raw),
            "issuer_lei_normalized": None,
            "filer_legal_name_normalized": None,
            "person_signing_name": None,
            "person_first_normalized": None,
            "person_last_normalized": None,
            "person_relationship_to_issuer": None,
            "person_relationship_to_issuer_normalized": None,
            "filing_date": header.filing_date,
            "period_of_report": None,
            "form_144_year": form_144_year,
            "xml_or_html": header.primary_doc_format,
            "raw_doc_r2_uri": header.raw_doc_r2_uri,
        }],
        "securities_to_be_sold": [],
        "securities_sold_past_3_months": [],
        "acquisition_info": [],
    }

    if not primary_doc:
        return fallback

    try:
        if header.primary_doc_format == "xml":
            try:
                doc = lxml_etree.fromstring(primary_doc)
            except lxml_etree.XMLSyntaxError:
                # Some XML attachments have mangled XML declarations / BOMs;
                # try a recovering parser.
                parser = lxml_etree.XMLParser(recover=True)
                doc = lxml_etree.fromstring(primary_doc, parser=parser)
            if doc is None:
                return fallback
            return _parse_xml_filing(header, doc)
        else:
            try:
                doc = lxml_html.fromstring(primary_doc)
            except (ValueError, lxml_html.etree.ParserError):
                doc = lxml_html.fromstring(
                    primary_doc.decode("utf-8", "ignore")
                )
            if doc is None:
                return fallback
            return _parse_html_filing(header, doc)
    except Exception as exc:
        log.warning(
            "parse %s/%s (%s) threw %s",
            cik, accession, header.primary_doc_format, exc,
        )
        return fallback

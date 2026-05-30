"""SEC EDGAR Form 13F parser — XML → structured records.

Parses the structured-XML era of Form 13F (2013-2024). Two XML files per
13F-HR/A filing, one per 13F-NT filing:

  - ``primary_doc.xml`` — cover-page metadata (manager name, address,
    period of report, signature, related-managers list).
    Namespaces:
      default = ``http://www.sec.gov/edgar/thirteenffiler``
      ``com`` = ``http://www.sec.gov/edgar/common``  (address fields)

  - informationtable XML — list of ``<infoTable>`` elements, one per held
    security. Filename is filer-chosen (e.g. ``infotable.xml``,
    ``<accession>-information-table.xml``, custom names like
    ``1060_13f-q42023.xml``). The orchestrator probes the per-accession
    directory listing to find it. ``13F-NT`` filings have no infotable.
    Namespace = ``http://www.sec.gov/edgar/document/thirteenf/informationtable``

Output (per filing): ``(filing_record, cover_page_record, [holdings...])``.
``13F-NT`` filings produce ``([], cover_page, [])`` — no filing aggregate
row (since there's nothing to aggregate) and no holdings.

NOTE on the `<value>` unit-of-measure: SEC instructions changed in 2022
amendments — for periods ending mid-2023 onward, ``<value>`` is in raw
dollars; for earlier periods it's in thousands of dollars. The parser
preserves the raw int as ``value_thousands_usd`` per directive
2026-05-09-sec-edgar-form-13f-r2-ingest.md and surfaces a derived
``value_usd = value_thousands_usd * 1000``. Downstream MV authors must
apply a date-conditional multiplier if dollar-grain accuracy matters.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from lxml import etree as lxml_etree

from _lib.sec_edgar_form_13f_normalize import (
    derive_quarter,
    normalize_accession,
    normalize_cik,
    normalize_issuer_name,
    normalize_lei,
    normalize_manager_name,
    parse_int,
    parse_period_of_report,
    parse_signature_date,
)


log = logging.getLogger("sec-edgar-form-13f-parser")


_NS_FILER = "http://www.sec.gov/edgar/thirteenffiler"
_NS_COMMON = "http://www.sec.gov/edgar/common"
_NS_INFOTABLE = "http://www.sec.gov/edgar/document/thirteenf/informationtable"

_NSMAP_PRIMARY = {"f": _NS_FILER, "c": _NS_COMMON}
_NSMAP_INFOTABLE = {"i": _NS_INFOTABLE}


@dataclass(frozen=True)
class FilingHeader:
    """Inputs available at discovery time (from form.idx)."""

    cik_raw: str
    manager_name_raw: str
    accession_raw: str
    filing_date: str            # 'YYYY-MM-DD'
    form_type: str              # '13F-HR' | '13F-HR/A' | '13F-NT'
    primary_doc_url: str
    info_table_url: str | None
    raw_xml_r2_uri: str | None = None


# -------------------------------------------------------------------- #
# Generic helpers
# -------------------------------------------------------------------- #


def _parse_xml_bytes(body: bytes | str) -> lxml_etree._Element | None:
    """Parse XML bytes/str → root element. Returns None on syntax failure.

    SEC XML is normally valid UTF-8 but some legacy filings have stray
    bytes in custom doc-stamp comments. Fall through with the lenient
    recover parser.
    """
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


def _text(el: lxml_etree._Element | None, xpath: str, nsmap: dict[str, str]) -> str | None:
    if el is None:
        return None
    nodes = el.xpath(xpath, namespaces=nsmap)
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


# -------------------------------------------------------------------- #
# primary_doc.xml → cover page
# -------------------------------------------------------------------- #


_AMENDMENT_NUMBER_RE = re.compile(r"\bAMENDMENT\b.*?(\d+)", re.I)


def parse_primary_doc(
    header: FilingHeader, primary_xml: bytes | str | None,
) -> dict[str, Any] | None:
    """Parse the primary submission XML → cover-page record.

    Returns None when the XML can't be parsed (caller treats as failed
    filing). Returns a sparse record (most fields None) when fields are
    structurally missing — common in the earliest 2013 filings.
    """
    accession = normalize_accession(header.accession_raw)
    cik = normalize_cik(header.cik_raw)

    if primary_xml is None:
        return None
    root = _parse_xml_bytes(primary_xml)
    if root is None:
        return None

    period_raw = _text(root, ".//f:periodOfReport", _NSMAP_PRIMARY)
    period_iso = parse_period_of_report(period_raw)
    quarter = derive_quarter(period_iso)
    report_year = int(period_iso[:4]) if period_iso else None

    manager_name_raw = _text(root, ".//f:filingManager/f:name", _NSMAP_PRIMARY)
    if manager_name_raw is None:
        # Some filings use coverPage/filingManager (different parent).
        manager_name_raw = _text(
            root, ".//f:coverPage/f:filingManager/f:name", _NSMAP_PRIMARY,
        )
    manager_name_raw = manager_name_raw or header.manager_name_raw

    addr = root.xpath(".//f:filingManager/f:address", namespaces=_NSMAP_PRIMARY)
    addr_node = addr[0] if addr else None

    def _addr(field: str) -> str | None:
        return _text(addr_node, f"./c:{field}", _NSMAP_PRIMARY) if addr_node is not None else None

    sig_node_list = root.xpath(".//f:signatureBlock", namespaces=_NSMAP_PRIMARY)
    sig_node = sig_node_list[0] if sig_node_list else None

    def _sig(field: str) -> str | None:
        return _text(sig_node, f"./f:{field}", _NSMAP_PRIMARY) if sig_node is not None else None

    summary_node_list = root.xpath(".//f:summaryPage", namespaces=_NSMAP_PRIMARY)
    summary_node = summary_node_list[0] if summary_node_list else None
    is_conf = _text(summary_node, "./f:isConfidentialOmitted", _NSMAP_PRIMARY) if summary_node is not None else None

    # Optional LEI — disclosed in some filings; SEC made LEI optional in
    # 2013 but a small fraction of filers populate it.
    lei_raw = _text(root, ".//f:filingManager/f:lei", _NSMAP_PRIMARY)
    if lei_raw is None:
        lei_raw = _text(root, ".//f:filer/f:credentials/f:lei", _NSMAP_PRIMARY)
    lei_norm = normalize_lei(lei_raw)

    # Other-managers list (related managers covered by this filing).
    other_managers_nodes = root.xpath(
        ".//f:otherManagers2Info/f:otherManager2", namespaces=_NSMAP_PRIMARY,
    )
    other_managers: list[dict[str, str | None]] = []
    for m in other_managers_nodes:
        seq_n = m.xpath("./f:sequenceNumber", namespaces=_NSMAP_PRIMARY)
        seq = seq_n[0].text.strip() if seq_n and seq_n[0].text else None
        name_n = m.xpath("./f:otherManager/f:name", namespaces=_NSMAP_PRIMARY)
        name = name_n[0].text.strip() if name_n and name_n[0].text else None
        fn_n = m.xpath("./f:otherManager/f:form13FFileNumber", namespaces=_NSMAP_PRIMARY)
        fn = fn_n[0].text.strip() if fn_n and fn_n[0].text else None
        crd_n = m.xpath("./f:otherManager/f:crdNumber", namespaces=_NSMAP_PRIMARY)
        crd = crd_n[0].text.strip() if crd_n and crd_n[0].text else None
        other_managers.append({
            "seq": seq, "name": name, "form13FFileNumber": fn,
            "crdNumber": crd,
        })

    report_type = _text(root, ".//f:reportType", _NSMAP_PRIMARY)
    report_calendar = _text(root, ".//f:reportCalendarOrQuarter", _NSMAP_PRIMARY)

    # Amendment number — appears in the reportType for 13F-HR/A.
    amendment_no: int | None = None
    if header.form_type == "13F-HR/A" and report_type:
        m = _AMENDMENT_NUMBER_RE.search(report_type)
        if m:
            try:
                amendment_no = int(m.group(1))
            except ValueError:
                amendment_no = None

    return {
        "accession_number": accession,
        "cik_normalized": cik,
        "manager_name_raw": manager_name_raw,
        "manager_name_normalized": normalize_manager_name(manager_name_raw),
        "manager_lei_normalized": lei_norm,
        "form_type": header.form_type,
        "amendment_number": amendment_no,
        "filing_date": header.filing_date,
        "period_of_report": period_iso,
        "report_year": report_year,
        "report_quarter": quarter,
        "report_type": report_type,
        "report_calendar_or_quarter": report_calendar,
        "address_street_1": _addr("street1"),
        "address_street_2": _addr("street2"),
        "address_city": _addr("city"),
        "address_state": _addr("stateOrCountry"),
        "address_zip": _addr("zipCode"),
        "address_country": _addr("country"),
        "signature_name": _sig("name"),
        "signature_title": _sig("title"),
        "signature_date": parse_signature_date(_sig("signatureDate")),
        "is_confidential_omitted": _bool(is_conf),
        "related_managers_json": json.dumps(other_managers) if other_managers else None,
        "primary_doc_url": header.primary_doc_url,
        "raw_xml_r2_uri": header.raw_xml_r2_uri,
    }


def _bool(s: str | None) -> bool | None:
    if s is None:
        return None
    s = s.strip().lower()
    if s in ("true", "1", "yes", "y"):
        return True
    if s in ("false", "0", "no", "n"):
        return False
    return None


# -------------------------------------------------------------------- #
# infotable.xml → holdings
# -------------------------------------------------------------------- #


def parse_information_table(
    cover: dict[str, Any], info_xml: bytes | str | None,
) -> list[dict[str, Any]]:
    """Parse the informationtable XML → list of per-holding records.

    Each ``<infoTable>`` element under the namespaced root is one row.
    Returns [] when the XML is missing or malformed.
    """
    if info_xml is None:
        return []
    root = _parse_xml_bytes(info_xml)
    if root is None:
        return []

    info_tables = root.xpath(".//i:infoTable", namespaces=_NSMAP_INFOTABLE)

    accession = cover["accession_number"]
    cik = cover["cik_normalized"]
    report_year = cover["report_year"]
    report_quarter = cover["report_quarter"]

    out: list[dict[str, Any]] = []
    for tbl in info_tables:
        def _t(name: str) -> str | None:
            return _text(tbl, f"./i:{name}", _NSMAP_INFOTABLE)

        issuer_name = _t("nameOfIssuer")
        title_class = _t("titleOfClass")
        cusip = _t("cusip")
        figi = _t("figi")
        value_raw = _t("value")

        shrs_nodes = tbl.xpath("./i:shrsOrPrnAmt", namespaces=_NSMAP_INFOTABLE)
        shrs_node = shrs_nodes[0] if shrs_nodes else None
        shrs_amt = _text(shrs_node, "./i:sshPrnamt", _NSMAP_INFOTABLE) if shrs_node is not None else None
        shrs_type = _text(shrs_node, "./i:sshPrnamtType", _NSMAP_INFOTABLE) if shrs_node is not None else None

        put_call = _t("putCall")
        invest_disc = _t("investmentDiscretion")
        other_managers = _t("otherManager")

        vote_nodes = tbl.xpath("./i:votingAuthority", namespaces=_NSMAP_INFOTABLE)
        vote_node = vote_nodes[0] if vote_nodes else None
        vote_sole = _text(vote_node, "./i:Sole", _NSMAP_INFOTABLE) if vote_node is not None else None
        vote_shared = _text(vote_node, "./i:Shared", _NSMAP_INFOTABLE) if vote_node is not None else None
        vote_none = _text(vote_node, "./i:None", _NSMAP_INFOTABLE) if vote_node is not None else None

        value_int = parse_int(value_raw)
        value_usd = float(value_int) * 1000.0 if value_int is not None else None

        out.append({
            "accession_number": accession,
            "cik_normalized": cik,
            "name_of_issuer": issuer_name,
            "name_of_issuer_normalized": normalize_issuer_name(issuer_name),
            "title_of_class": (title_class or "").strip() or None,
            "cusip": cusip,
            "figi": figi,
            "value_thousands_usd": value_int,
            "value_usd": value_usd,
            "shrs_or_prn_amt": parse_int(shrs_amt),
            "shrs_or_prn_amt_type": shrs_type,
            "put_call": put_call,
            "investment_discretion": invest_disc,
            "other_managers": other_managers,
            "voting_authority_sole": parse_int(vote_sole),
            "voting_authority_shared": parse_int(vote_shared),
            "voting_authority_none": parse_int(vote_none),
            "report_year": report_year,
            "report_quarter": report_quarter,
        })
    return out


# -------------------------------------------------------------------- #
# Top-level entry point
# -------------------------------------------------------------------- #


def build_filing_record(cover: dict[str, Any], holdings: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the per-filing aggregate row (one per accession)."""
    total_value = sum(
        (h["value_thousands_usd"] or 0) for h in holdings
    ) if holdings else None
    total_value_usd = (float(total_value) * 1000.0) if total_value is not None else None
    return {
        "accession_number": cover["accession_number"],
        "cik_normalized": cover["cik_normalized"],
        "manager_name": cover["manager_name_raw"],
        "manager_name_normalized": cover["manager_name_normalized"],
        "manager_lei_normalized": cover["manager_lei_normalized"],
        "form_type": cover["form_type"],
        "amendment_number": cover["amendment_number"],
        "filing_date": cover["filing_date"],
        "period_of_report": cover["period_of_report"],
        "total_holdings_count": len(holdings) if holdings else None,
        "total_holdings_value_thousands_usd": total_value,
        "total_holdings_value_usd": total_value_usd,
        "report_year": cover["report_year"],
        "report_quarter": cover["report_quarter"],
    }


def parse_filing(
    header: FilingHeader,
    primary_xml: bytes | str | None,
    info_xml: bytes | str | None,
) -> dict[str, list[dict[str, Any]] | dict[str, Any] | None]:
    """Parse one Form 13F filing → 3 streams' worth of records.

    Returns a dict with keys: ``filings`` (1-element list, or [] if cover
    parse failed), ``cover_page`` (1-element list, or []), ``holdings``
    (list, possibly empty). 13F-NT filings always have empty holdings.
    """
    cover = parse_primary_doc(header, primary_xml)
    if cover is None:
        return {"filings": [], "cover_page": [], "holdings": []}

    if header.form_type == "13F-NT":
        # Notice filings — no informationtable expected.
        holdings: list[dict[str, Any]] = []
    else:
        holdings = parse_information_table(cover, info_xml)

    filing = build_filing_record(cover, holdings)
    return {"filings": [filing], "cover_page": [cover], "holdings": holdings}

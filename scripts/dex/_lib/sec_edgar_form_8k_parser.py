"""SEC EDGAR Form 8-K parser — HTML → 8 structured streams.

Item-anchored extraction. Detects Item N.NN headings via heading-text anchors
(robust ≥95% per directive), then body-parses the 6 prioritized Items
(5.02, 1.01, 2.01, 2.03, 5.01, 8.01) heuristically. Items 5.07 / 4.02 / 9.01
/ 7.01 / 2.02 / 1.02 / 3.02 / etc. are recorded in `items_index` only with no
body parse — future directives can add per-Item streams without re-ingesting.

Parser output (per filing) — 8 lists of dicts:

    filings: [filing_meta]                              # always exactly 1 record
    items_index: [items_row, ...]                       # one per Item present
    item_5_02_officer_changes: [event_row, ...]         # multi-officer → multi-row
    item_1_01_material_agreement: [event_row, ...]
    item_2_01_acquisition_disposition: [event_row, ...]
    item_2_03_direct_financial_obligation: [event_row, ...]  # credit-facility/warehouse-line creation
    item_5_01_change_in_control: [event_row, ...]
    item_8_01_other_events: [event_row, ...]            # one per Item 8.01 occurrence

Pre-Aug-2004 filings are skipped — the Item taxonomy changed in August 2004.
parse_filing() returns None for those (caller logs and increments the
``filings_skipped_count`` audit field).

Schema details: see directive 2026-05-12-sec-8k-item-203-extension.md.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from lxml import html as lxml_html

from _lib.sec_edgar_form_8k_normalize import (
    classify_agreement_type,
    classify_event_type,
    classify_obligation_type,
    normalize_accession,
    normalize_cik,
    normalize_company_name,
    normalize_officer_name,
    normalize_role,
    parse_dollar_amount,
    parse_event_date,
    parse_filing_date,
)


log = logging.getLogger("sec-edgar-form-8k-parser")


# -------------------------------------------------------------------- #
# Item-taxonomy pivot
# -------------------------------------------------------------------- #

# The modern Form 8-K Item-numbering convention starts 2004-08-23 per the SEC's
# Form 8-K Improvement Project. Filings before this date use a different
# taxonomy (Items 1, 2, 3, etc.) and are out of scope per directive §3.
ITEM_TAXONOMY_PIVOT = date(2004, 8, 23)


# -------------------------------------------------------------------- #
# Item heading detection
# -------------------------------------------------------------------- #

# Match "Item 5.02", "Item 5.02.", "ITEM 5.02 ", etc. — anchor on `Item N.NN`.
# Captures the (item_no_str) and the trailing-text-up-to-newline heading.
_ITEM_HEADING_RE = re.compile(
    r"\bitem\s+(\d+\.\d{2})\b",
    re.I,
)

# Sentinel labels. The standard SEC text for the most common Items.
_ITEM_LABELS: dict[str, str] = {
    "1.01": "Entry into a Material Definitive Agreement",
    "1.02": "Termination of a Material Definitive Agreement",
    "1.03": "Bankruptcy or Receivership",
    "1.04": "Mine Safety - Reporting of Shutdowns and Patterns of Violations",
    "1.05": "Material Cybersecurity Incidents",
    "2.01": "Completion of Acquisition or Disposition of Assets",
    "2.02": "Results of Operations and Financial Condition",
    "2.03": "Creation of a Direct Financial Obligation",
    "2.04": "Triggering Events That Accelerate or Increase a Direct Financial Obligation",
    "2.05": "Costs Associated with Exit or Disposal Activities",
    "2.06": "Material Impairments",
    "3.01": "Notice of Delisting or Failure to Satisfy a Continued Listing Rule",
    "3.02": "Unregistered Sales of Equity Securities",
    "3.03": "Material Modification to Rights of Security Holders",
    "4.01": "Changes in Registrant's Certifying Accountant",
    "4.02": "Non-Reliance on Previously Issued Financial Statements",
    "5.01": "Changes in Control of Registrant",
    "5.02": "Departure of Directors or Certain Officers; Election of Directors; Appointment of Certain Officers; Compensatory Arrangements of Certain Officers",
    "5.03": "Amendments to Articles of Incorporation or Bylaws",
    "5.04": "Temporary Suspension of Trading Under Registrant's Employee Benefit Plans",
    "5.05": "Amendments to the Registrant's Code of Ethics",
    "5.06": "Change in Shell Company Status",
    "5.07": "Submission of Matters to a Vote of Security Holders",
    "5.08": "Shareholder Director Nominations",
    "6.01": "ABS Informational and Computational Material",
    "6.02": "Change of Servicer or Trustee",
    "6.03": "Change in Credit Enhancement or Other External Support",
    "6.04": "Failure to Make a Required Distribution",
    "6.05": "Securities Act Updating Disclosure",
    "7.01": "Regulation FD Disclosure",
    "8.01": "Other Events",
    "9.01": "Financial Statements and Exhibits",
}

ITEM_5_02 = "5.02"
ITEM_1_01 = "1.01"
ITEM_2_01 = "2.01"
ITEM_2_03 = "2.03"
ITEM_5_01 = "5.01"
ITEM_8_01 = "8.01"

BODY_PARSE_ITEMS: frozenset[str] = frozenset({
    ITEM_5_02, ITEM_1_01, ITEM_2_01, ITEM_2_03, ITEM_5_01, ITEM_8_01,
})


# -------------------------------------------------------------------- #
# Cover-page extraction
# -------------------------------------------------------------------- #

# "Date of report (Date of earliest event reported): December 31, 2023"
_PERIOD_OF_REPORT_RE = re.compile(
    r"date\s+of\s+(?:report|earliest\s+event\s+reported)[^:]*:\s*"
    r"([A-Z][a-z]+\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}|\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2})",
    re.I,
)
_AMENDMENT_OF_RE = re.compile(
    r"\b(amendment|amend(?:ing|s)?)\b.*?\b(8-K|Form\s+8-K)\b.*?"
    r"(?:filed|dated)\s+([A-Z][a-z]+\s+\d{1,2},?\s+\d{4}|\d{1,2}/\d{1,2}/\d{2,4})",
    re.I | re.DOTALL,
)
_ACC_NUMBER_RE = re.compile(r"\b(\d{10}-\d{2}-\d{6})\b")


# -------------------------------------------------------------------- #
# Officer-event extraction (Item 5.02)
# -------------------------------------------------------------------- #

# Heuristic patterns for finding "Person Name, role" or "Person Name (role)".
# We look for proper-noun runs near role-keywords inside Item 5.02 spans.
_PERSON_NAME_TOKEN = r"[A-Z][a-zA-Z\.\-']+(?:\s+[A-Z]\.?(?:\s+[A-Z][a-zA-Z\.\-']+)?)?\s+[A-Z][a-zA-Z\.\-']+"
_PERSON_PLUS_ROLE_RE = re.compile(
    # "Mr./Ms./Dr." optional + 2-4 capitalized name tokens + ", role" form
    r"\b(?:Mr\.|Ms\.|Mrs\.|Dr\.)?\s*"
    r"(" + _PERSON_NAME_TOKEN + r"(?:\s+(?:Jr|Sr|II|III|IV)\.?)?)"
    r"(?:\s*,?\s+(?:our|the\s+Company['']s|the\s+Registrant['']s|its)?\s*)?"
    r"(?:,\s*|\s+)(?:as|to\s+serve\s+as|will\s+serve\s+as|will\s+become|has\s+been\s+appointed\s+as|"
    r"served\s+as|has\s+served\s+as|currently\s+serves\s+as|to\s+be|appointed\s+as)"
    r"\s+(?:our|the\s+Company['']s|the\s+Registrant['']s|the|a|an)?\s*"
    r"([A-Z][^.\n]{2,80}?)(?=[.\n,;]|\s+(?:effective|of\s+the\s+Company))",
    re.I,
)
# Also catch "John Smith, Chief Executive Officer" style (capitalized role
# directly following a comma after the name).
_NAME_COMMA_ROLE_RE = re.compile(
    r"\b(" + _PERSON_NAME_TOKEN + r"(?:\s+(?:Jr|Sr|II|III|IV)\.?)?)\s*,\s*"
    r"((?:Chief|Chairman|President|Director|Executive|Senior|Vice|General|"
    r"Treasurer|Secretary)[A-Za-z\s,&\-/]{2,80}?)"
    r"(?=[\.\;\n]|\s+(?:will|has|is|of)\b)",
)
# Resignation-form: "Mr. Smith resigned as Chief Executive Officer..."
_RESIGNATION_RE = re.compile(
    r"\b(?:Mr\.|Ms\.|Mrs\.|Dr\.)?\s*"
    r"(" + _PERSON_NAME_TOKEN + r"(?:\s+(?:Jr|Sr|II|III|IV)\.?)?)"
    r"\s+(?:has\s+)?(?:resigned|retired|stepped\s+down|departed|will\s+resign|will\s+retire)"
    r"(?:\s+(?:as|from\s+(?:his|her|the)\s+(?:position|role)\s+(?:as|of)))?\s+"
    r"((?:our|the\s+Company['']s|the\s+Registrant['']s|the|a|an)?\s*"
    r"[A-Z][^.\n]{2,80}?)(?=[\.\;\n]|\s+(?:effective|of\s+the\s+Company))",
    re.I,
)


# -------------------------------------------------------------------- #
# Counterparty extraction (Item 1.01)
# -------------------------------------------------------------------- #

_AGREEMENT_COUNTERPARTY_RE = re.compile(
    r"\b(?:entered\s+into|executed)\s+(?:an?\s+|the\s+)?"
    r"([A-Z][^.\n]{4,120}?\s+(?:agreement|contract))\s+"
    r"(?:with|by\s+and\s+(?:among|between))\s+"
    r"([A-Z][^.\n]{4,200}?)(?=[\.\,\;]|\s+(?:dated|effective|on)\b)",
    re.I,
)


# -------------------------------------------------------------------- #
# M&A extraction (Item 2.01)
# -------------------------------------------------------------------- #

_MANDA_TARGET_RE = re.compile(
    r"\b(?:completed|consummated|closed)\s+(?:its\s+|the\s+)?"
    r"(acquisition|disposition|sale|divestiture|merger)\s+"
    r"(?:of\s+(?:all\s+(?:of\s+)?the\s+)?(?:assets\s+of\s+|outstanding\s+(?:shares|stock)\s+of\s+|capital\s+stock\s+of\s+|business\s+of\s+|interests\s+in\s+|membership\s+interests\s+in\s+)?)?"
    r"([A-Z][^.\n]{2,120}?)(?=[\.\,\;]|\s+(?:for|effective|on|pursuant)\b)",
    re.I,
)
_MANDA_CONSIDERATION_RE = re.compile(
    r"\bfor\s+(?:an\s+aggregate\s+(?:purchase\s+price|consideration)\s+of\s+)?"
    r"(?:approximately\s+)?\$([\d,\.]+)\s*"
    r"(million|billion|thousand)?",
    re.I,
)


# -------------------------------------------------------------------- #
# Direct-financial-obligation extraction (Item 2.03)
# -------------------------------------------------------------------- #

# Match the obligation-creditor counterparty: "entered into ... <obligation>
# ... with <counterparty>". The obligation-noun keywords cover credit facilities,
# term loans, revolving facilities, warehouse lines, indentures, notes, and
# guaranties — i.e., the credit-supply surfaces this cycle ingests for.
_OBLIGATION_CREDITOR_RE = re.compile(
    r"\b(?:entered\s+into|executed|consummated|closed)\s+(?:an?\s+|the\s+)?"
    r"([A-Z][^.\n]{4,180}?\s+(?:credit\s+(?:agreement|facility)|"
    r"loan\s+agreement|note\s+purchase\s+agreement|indenture|"
    r"revolving\s+credit\s+(?:facility|agreement)|term\s+loan(?:\s+agreement)?|"
    r"warehouse\s+(?:line|facility|agreement)|"
    r"senior\s+(?:secured|unsecured)?\s*(?:credit|note|notes)|"
    r"convertible\s+(?:note|notes|debenture)|"
    r"guaranty\s+agreement|guarantee\s+agreement|"
    r"securit(?:y|ies)\s+purchase\s+agreement))\s+"
    r"(?:with|by\s+and\s+(?:among|between)|among|from)\s+"
    r"([A-Z][^.\n]{4,200}?)(?=[\.\,\;]|\s+(?:dated|effective|on|pursuant|"
    r"in\s+an\s+aggregate|providing\s+for)\b)",
    re.I,
)

# Match the aggregate obligation amount: "aggregate principal amount of $X.X
# million/billion" or "$X.X million credit facility". Returns (amount_str,
# unit_suffix).
_OBLIGATION_AMOUNT_RE = re.compile(
    r"(?:aggregate\s+principal\s+amount\s+of\s+|in\s+an\s+aggregate\s+(?:principal\s+)?amount\s+of\s+|"
    r"a\s+(?:total\s+|maximum\s+)?(?:principal\s+)?amount\s+of\s+|"
    r"(?:total\s+|maximum\s+|aggregate\s+|principal\s+)?commitment\s+(?:amount\s+)?of\s+|"
    r"commitments?\s+(?:of|in\s+the\s+amount\s+of|in\s+an\s+aggregate\s+amount\s+of)\s+|"
    r"borrowing\s+capacity\s+of\s+(?:up\s+to\s+)?)"
    r"(?:up\s+to\s+|approximately\s+)?"
    r"\$([\d,\.]+)\s*"
    r"(million|billion|thousand)?\b",
    re.I,
)


# -------------------------------------------------------------------- #
# Change-in-control extraction (Item 5.01)
# -------------------------------------------------------------------- #

_CIC_ACQUIRER_RE = re.compile(
    r"\bchange\s+(?:in|of)\s+control\s+(?:of\s+(?:the\s+)?(?:Company|Registrant))?\s+"
    r"(?:occurred|was\s+effected|resulted\s+from)?\s*"
    r"(?:as\s+a\s+result\s+of\s+|pursuant\s+to\s+|when\s+|after\s+|in\s+connection\s+with\s+)?"
    r"(?:the\s+(?:acquisition|merger|combination|tender\s+offer)\s+(?:by|with)\s+)?"
    r"([A-Z][^.\n]{4,150}?)(?=[\.\,\;]|\s+(?:acquired|completed|consummated)\b)",
    re.I,
)


# -------------------------------------------------------------------- #
# Header dataclass
# -------------------------------------------------------------------- #


@dataclass
class FilingHeader:
    """Carrier struct for inputs known before parse — comes from form.idx +
    fetch wrapper.
    """
    cik_raw: str
    company_name_raw: str
    accession_raw: str
    form_type: str  # "8-K" or "8-K/A"
    filing_date: str  # YYYY-MM-DD from form.idx
    primary_doc_url: str


# -------------------------------------------------------------------- #
# Main entry point
# -------------------------------------------------------------------- #


def parse_filing(
    header: FilingHeader, html_bytes: bytes,
) -> dict[str, list[dict[str, Any]]] | None:
    """Parse one Form 8-K filing into the 7 structured streams.

    Returns None for pre-Aug-2004 filings (skip-by-design).
    """
    accession_norm = normalize_accession(header.accession_raw)
    cik_norm = normalize_cik(header.cik_raw)
    filing_date_norm = parse_filing_date(header.filing_date)
    company_norm = normalize_company_name(header.company_name_raw)
    is_amendment = header.form_type.upper() == "8-K/A"

    if accession_norm is None or cik_norm is None:
        return None

    try:
        doc = lxml_html.fromstring(html_bytes)
    except Exception:
        return None

    text = doc.text_content() or ""
    text = re.sub(r"[ \t]+", " ", text)
    # Preserve paragraph boundaries (single newlines collapsed to space).
    text = re.sub(r"\s*\n\s*\n\s*", "\n\n", text)
    text = re.sub(r"[ \t]*\n[ \t]*", " ", text)

    # Extract period_of_report from cover page (falls back to filing_date).
    period_of_report = _extract_period_of_report(text) or filing_date_norm

    # Pre-Aug-2004 filter (per directive §3 — Item taxonomy pivot).
    if period_of_report:
        try:
            por = date.fromisoformat(period_of_report)
            if por < ITEM_TAXONOMY_PIVOT:
                return None
        except ValueError:
            pass

    report_year = None
    report_quarter = None
    if period_of_report:
        try:
            por = date.fromisoformat(period_of_report)
            report_year = por.year
            report_quarter = (por.month - 1) // 3 + 1
        except ValueError:
            pass
    if report_year is None and filing_date_norm:
        try:
            fd = date.fromisoformat(filing_date_norm)
            report_year = fd.year
            report_quarter = (fd.month - 1) // 3 + 1
        except ValueError:
            pass

    # 1. Find all Item headings in document order; build items_index +
    #    per-Item span text.
    item_spans = _extract_item_spans(text)
    items_index_rows: list[dict[str, Any]] = []
    items_list_str = ",".join(item_no for item_no, _, _ in item_spans)
    for seq, (item_no, _label_unused, _span_unused) in enumerate(item_spans, start=1):
        items_index_rows.append({
            "accession_number": accession_norm,
            "cik_normalized": cik_norm,
            "item_no": item_no,
            "item_label": _ITEM_LABELS.get(item_no, ""),
            "item_seq": seq,
            "has_body_parse": item_no in BODY_PARSE_ITEMS,
            "report_year": report_year,
            "report_quarter": report_quarter,
        })

    # 2. filings row — exactly one per accession.
    original_acc = None
    if is_amendment:
        m = _ACC_NUMBER_RE.search(text)
        # Accession in body that's NOT the current one is plausibly the
        # amended-original. We don't strictly enforce.
        if m and m.group(1) != accession_norm:
            original_acc = m.group(1)

    filing_row = {
        "accession_number": accession_norm,
        "cik_normalized": cik_norm,
        "form_type": header.form_type.upper() if header.form_type.upper() in ("8-K", "8-K/A") else "8-K",
        "is_amendment": is_amendment,
        "original_accession_number": original_acc,
        "company_name_raw": header.company_name_raw,
        "company_name_normalized": company_norm,
        "filing_date": filing_date_norm,
        "period_of_report": period_of_report,
        "items_list": items_list_str,
        "report_year": report_year,
        "report_quarter": report_quarter,
    }

    # 3. Body-parse the 6 prioritized Items.
    item_5_02_rows: list[dict[str, Any]] = []
    item_1_01_rows: list[dict[str, Any]] = []
    item_2_01_rows: list[dict[str, Any]] = []
    item_2_03_rows: list[dict[str, Any]] = []
    item_5_01_rows: list[dict[str, Any]] = []
    item_8_01_rows: list[dict[str, Any]] = []

    for item_no, _label, span_text in item_spans:
        if item_no == ITEM_5_02:
            item_5_02_rows.extend(
                _parse_item_5_02(span_text, accession_norm, cik_norm, report_year)
            )
        elif item_no == ITEM_1_01:
            item_1_01_rows.extend(
                _parse_item_1_01(span_text, accession_norm, cik_norm, report_year)
            )
        elif item_no == ITEM_2_01:
            item_2_01_rows.extend(
                _parse_item_2_01(span_text, accession_norm, cik_norm, report_year)
            )
        elif item_no == ITEM_2_03:
            item_2_03_rows.extend(
                _parse_item_2_03(span_text, accession_norm, cik_norm, report_year)
            )
        elif item_no == ITEM_5_01:
            item_5_01_rows.extend(
                _parse_item_5_01(span_text, accession_norm, cik_norm, report_year)
            )
        elif item_no == ITEM_8_01:
            # No structured fields beyond cik + accession + full text.
            item_8_01_rows.append({
                "accession_number": accession_norm,
                "cik_normalized": cik_norm,
                "item_text_raw": _truncate(span_text, 60_000),
                "report_year": report_year,
                "report_quarter": report_quarter,
            })

    return {
        "filings": [filing_row],
        "items_index": items_index_rows,
        "item_5_02_officer_changes": item_5_02_rows,
        "item_1_01_material_agreement": item_1_01_rows,
        "item_2_01_acquisition_disposition": item_2_01_rows,
        "item_2_03_direct_financial_obligation": item_2_03_rows,
        "item_5_01_change_in_control": item_5_01_rows,
        "item_8_01_other_events": item_8_01_rows,
    }


# -------------------------------------------------------------------- #
# Cover-page helpers
# -------------------------------------------------------------------- #


def _extract_period_of_report(text: str) -> str | None:
    """Pull the "Date of report" from the 8-K cover page → ISO YYYY-MM-DD."""
    head = text[:4000]
    m = _PERIOD_OF_REPORT_RE.search(head)
    if m:
        return parse_event_date(m.group(1))
    return None


# -------------------------------------------------------------------- #
# Item-span splitting
# -------------------------------------------------------------------- #


def _extract_item_spans(text: str) -> list[tuple[str, str, str]]:
    """Find every Item heading, split the doc into per-Item text spans.

    Returns ``[(item_no, label_text, span_text), ...]`` in document order.
    Multiple headings of the same Item (rare) are kept separate.

    The Item-detection layer is intentionally lightweight: we anchor only on
    the ``Item N.NN`` token to avoid letting greedy regex consume the doc.
    Item heading appearances inside a table-of-contents at the very top of
    the filing are deduped by collapsing same-Item-no occurrences within the
    leading 1500 chars to a single "real" heading at the bigger downstream
    position (filings always include a TOC then a content section).
    """
    matches: list[tuple[int, str]] = []
    for m in _ITEM_HEADING_RE.finditer(text):
        item_no = m.group(1)
        if not _is_plausible_item(item_no):
            continue
        matches.append((m.end(), item_no))

    if not matches:
        return []

    # Dedup: Item-headings that appear in the filing's table of contents
    # (within the first 2000 chars) are usually mirrored by a "real" heading
    # later in the body. Keep only the latest occurrence of each item_no
    # for any duplicates that span both the TOC region and the body.
    if len(text) > 2500:
        toc_cut = 2000
        last_pos: dict[str, int] = {}
        for end_pos, item_no in matches:
            last_pos[item_no] = end_pos
        kept: list[tuple[int, str]] = []
        for end_pos, item_no in matches:
            # Always keep body-region matches; for TOC-region matches, keep
            # only if no later body-region match exists for the same item.
            if end_pos > toc_cut or last_pos.get(item_no, 0) <= toc_cut:
                kept.append((end_pos, item_no))
        matches = kept

    out: list[tuple[str, str, str]] = []
    for i, (end_pos, item_no) in enumerate(matches):
        next_start = matches[i + 1][0] if i + 1 < len(matches) else len(text)
        span = text[end_pos:next_start].strip()
        # Trim leading "." or "." + label-line for cleanliness
        span = re.sub(r"^[\.\s]*", "", span, count=1)
        # The label is the first ~150 chars of the span (after any leading
        # "Item description." text). Useful for items_index.item_label.
        label_match = re.match(r"([^.\n]{0,200})", span)
        label = label_match.group(1).strip() if label_match else ""
        out.append((item_no, label, span))
    return out


def _is_plausible_item(item_no: str) -> bool:
    """Reject malformed item numbers. Plausible 8-K items are 1.01-9.99."""
    try:
        major, minor = item_no.split(".", 1)
        return 1 <= int(major) <= 9 and 0 <= int(minor) <= 99 and len(minor) == 2
    except (ValueError, IndexError):
        return False


# -------------------------------------------------------------------- #
# Item 5.02 parser — officer changes
# -------------------------------------------------------------------- #


def _parse_item_5_02(
    span_text: str, accession: str, cik: str, report_year: int | None,
) -> list[dict[str, Any]]:
    """Heuristic body-parse of Item 5.02. Multi-officer events emit multi-row.

    Strategy: find candidate (name, role, event_type) tuples via regex on the
    span text. Dedupe by (name_normalized, role_normalized). Always preserve
    the full Item text in `item_text_raw` for downstream LLM refinement.
    """
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str | None, str | None]] = set()

    # Try multiple regex shapes. First catch resignation/retirement events,
    # then appointment/named events, then plain "Name, Title" pairs.
    candidates: list[tuple[str, str, str]] = []  # (name_raw, role_raw, sub_text)
    for m in _RESIGNATION_RE.finditer(span_text):
        sub = span_text[max(0, m.start() - 200): min(len(span_text), m.end() + 200)]
        candidates.append((m.group(1), m.group(2), sub))
    for m in _PERSON_PLUS_ROLE_RE.finditer(span_text):
        sub = span_text[max(0, m.start() - 200): min(len(span_text), m.end() + 200)]
        candidates.append((m.group(1), m.group(2), sub))
    for m in _NAME_COMMA_ROLE_RE.finditer(span_text):
        sub = span_text[max(0, m.start() - 200): min(len(span_text), m.end() + 200)]
        candidates.append((m.group(1), m.group(2), sub))

    eff_date = parse_event_date(span_text[:2000])
    item_text_raw = _truncate(span_text, 60_000)

    seq = 0
    for raw_name, raw_role, sub_text in candidates:
        name_norm, first_norm, last_norm = normalize_officer_name(raw_name)
        if not name_norm:
            continue
        role_norm = normalize_role(raw_role)
        key = (name_norm, role_norm)
        if key in seen:
            continue
        seen.add(key)
        seq += 1
        event_type = classify_event_type(sub_text)
        rows.append({
            "accession_number": accession,
            "cik_normalized": cik,
            "officer_event_seq": seq,
            "officer_name_raw": raw_name.strip(),
            "officer_name_normalized": name_norm,
            "officer_first_normalized": first_norm,
            "officer_last_normalized": last_norm,
            "role": (raw_role or "").strip()[:200] or None,
            "role_normalized": role_norm,
            "event_type": event_type,
            "effective_date": eff_date,
            "comp_arrangement_summary": _extract_comp_summary(sub_text),
            "item_text_raw": item_text_raw,
            "report_year": report_year,
        })

    # Always emit at least one row even if no candidate matched, so the full
    # text reaches downstream LLM extractors (per directive §2.7 — heuristic
    # may miss; raw text is preserved for refinement).
    if not rows:
        rows.append({
            "accession_number": accession,
            "cik_normalized": cik,
            "officer_event_seq": 1,
            "officer_name_raw": None,
            "officer_name_normalized": None,
            "officer_first_normalized": None,
            "officer_last_normalized": None,
            "role": None,
            "role_normalized": None,
            "event_type": classify_event_type(span_text[:5000]),
            "effective_date": eff_date,
            "comp_arrangement_summary": _extract_comp_summary(span_text[:5000]),
            "item_text_raw": item_text_raw,
            "report_year": report_year,
        })

    return rows


def _extract_comp_summary(text: str) -> str | None:
    """Find the comp-arrangement narrative span: the sentence(s) discussing
    bonus / equity / severance / sign-on. Returns up to 1.5KB.
    """
    if not text:
        return None
    keywords = re.compile(
        r"\b(bonus|equity\s+(?:grant|award)|sign[\-\s]on|severance|"
        r"retention\s+(?:award|bonus|grant)|stock\s+option|restricted\s+stock|"
        r"performance\s+(?:share|unit)|base\s+salary|target\s+annual)\b",
        re.I,
    )
    matches = list(keywords.finditer(text))
    if not matches:
        return None
    first = matches[0]
    start = max(0, first.start() - 150)
    end = min(len(text), first.end() + 800)
    return text[start:end].strip()[:1500] or None


# -------------------------------------------------------------------- #
# Item 1.01 parser — material definitive agreement
# -------------------------------------------------------------------- #


def _parse_item_1_01(
    span_text: str, accession: str, cik: str, report_year: int | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    item_text_raw = _truncate(span_text, 60_000)
    eff_date = parse_event_date(span_text[:3000])
    agreement_type = classify_agreement_type(span_text[:8000])

    seq = 0
    for m in _AGREEMENT_COUNTERPARTY_RE.finditer(span_text):
        counterparty = (m.group(2) or "").strip()
        # Trim trailing prepositional/relative clauses
        counterparty = re.split(r"\s+(?:dated|effective|pursuant)\b", counterparty, flags=re.I)[0]
        counterparty = counterparty.strip(" .,;")
        if not counterparty or len(counterparty) > 250:
            continue
        cp_norm = normalize_company_name(counterparty)
        if not cp_norm or cp_norm in seen:
            continue
        seen.add(cp_norm)
        seq += 1
        rows.append({
            "accession_number": accession,
            "cik_normalized": cik,
            "agreement_event_seq": seq,
            "counterparty_name_raw": counterparty,
            "counterparty_name_normalized": cp_norm,
            "agreement_type": agreement_type,
            "effective_date": eff_date,
            "item_text_raw": item_text_raw,
            "report_year": report_year,
        })

    if not rows:
        rows.append({
            "accession_number": accession,
            "cik_normalized": cik,
            "agreement_event_seq": 1,
            "counterparty_name_raw": None,
            "counterparty_name_normalized": None,
            "agreement_type": agreement_type,
            "effective_date": eff_date,
            "item_text_raw": item_text_raw,
            "report_year": report_year,
        })
    return rows


# -------------------------------------------------------------------- #
# Item 2.01 parser — acquisition / disposition
# -------------------------------------------------------------------- #


def _parse_item_2_01(
    span_text: str, accession: str, cik: str, report_year: int | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    item_text_raw = _truncate(span_text, 60_000)
    eff_date = parse_event_date(span_text[:3000])

    consideration_summary = None
    consideration_value = None
    cm = _MANDA_CONSIDERATION_RE.search(span_text[:5000])
    if cm:
        try:
            v = float(cm.group(1).replace(",", ""))
            unit = (cm.group(2) or "").lower()
            if unit == "billion":
                v *= 1_000_000_000
            elif unit == "million":
                v *= 1_000_000
            elif unit == "thousand":
                v *= 1_000
            consideration_value = v
            start = max(0, cm.start() - 100)
            end = min(len(span_text), cm.end() + 200)
            consideration_summary = span_text[start:end].strip()[:1500]
        except ValueError:
            pass

    seen: set[str] = set()
    seq = 0
    for m in _MANDA_TARGET_RE.finditer(span_text):
        verb = (m.group(1) or "").lower()
        target = (m.group(2) or "").strip().strip(" .,;")
        if not target or len(target) > 250:
            continue
        target_norm = normalize_company_name(target)
        if not target_norm or target_norm in seen:
            continue
        seen.add(target_norm)
        seq += 1
        event_type = "disposition" if verb in ("disposition", "sale", "divestiture") else "acquisition"
        rows.append({
            "accession_number": accession,
            "cik_normalized": cik,
            "event_type": event_type,
            "acquirer_name_raw": None,
            "acquirer_name_normalized": None,
            "target_name_raw": target,
            "target_name_normalized": target_norm,
            "consideration_summary": consideration_summary,
            "consideration_value_usd": consideration_value,
            "effective_date": eff_date,
            "item_text_raw": item_text_raw,
            "report_year": report_year,
        })

    if not rows:
        rows.append({
            "accession_number": accession,
            "cik_normalized": cik,
            "event_type": "acquisition",
            "acquirer_name_raw": None,
            "acquirer_name_normalized": None,
            "target_name_raw": None,
            "target_name_normalized": None,
            "consideration_summary": consideration_summary,
            "consideration_value_usd": consideration_value,
            "effective_date": eff_date,
            "item_text_raw": item_text_raw,
            "report_year": report_year,
        })
    return rows


# -------------------------------------------------------------------- #
# Item 5.01 parser — change in control
# -------------------------------------------------------------------- #


def _parse_item_5_01(
    span_text: str, accession: str, cik: str, report_year: int | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    item_text_raw = _truncate(span_text, 60_000)
    eff_date = parse_event_date(span_text[:3000])

    consideration_summary = None
    cm = _MANDA_CONSIDERATION_RE.search(span_text[:5000])
    if cm:
        start = max(0, cm.start() - 100)
        end = min(len(span_text), cm.end() + 200)
        consideration_summary = span_text[start:end].strip()[:1500]

    seen: set[str] = set()
    seq = 0
    for m in _CIC_ACQUIRER_RE.finditer(span_text):
        acquirer = (m.group(1) or "").strip().strip(" .,;")
        if not acquirer or len(acquirer) > 250:
            continue
        acq_norm = normalize_company_name(acquirer)
        if not acq_norm or acq_norm in seen:
            continue
        seen.add(acq_norm)
        seq += 1
        rows.append({
            "accession_number": accession,
            "cik_normalized": cik,
            "acquirer_name_raw": acquirer,
            "acquirer_name_normalized": acq_norm,
            "effective_date": eff_date,
            "consideration_summary": consideration_summary,
            "item_text_raw": item_text_raw,
            "report_year": report_year,
        })

    if not rows:
        rows.append({
            "accession_number": accession,
            "cik_normalized": cik,
            "acquirer_name_raw": None,
            "acquirer_name_normalized": None,
            "effective_date": eff_date,
            "consideration_summary": consideration_summary,
            "item_text_raw": item_text_raw,
            "report_year": report_year,
        })
    return rows


# -------------------------------------------------------------------- #
# Item 2.03 parser — Creation of a Direct Financial Obligation
# -------------------------------------------------------------------- #


_AMOUNT_MULTIPLIERS = {
    "thousand": 1_000.0,
    "million": 1_000_000.0,
    "billion": 1_000_000_000.0,
}


def _extract_obligation_amount(span_text: str) -> float | None:
    """Find the largest credit-facility / loan amount in the span.

    Returns USD as float. Tolerates "$500 million", "$1.25 billion",
    "$50,000,000". Multiple amounts may appear (e.g., commitment + accordion);
    we pick the largest, which is conservative for downstream targeting.
    """
    candidates: list[float] = []
    for m in _OBLIGATION_AMOUNT_RE.finditer(span_text[:10_000]):
        amount_str, unit = m.group(1), (m.group(2) or "").lower()
        # Reuse normalize.parse_dollar_amount to strip commas + parse.
        base = parse_dollar_amount(amount_str)
        if base is None:
            continue
        multiplier = _AMOUNT_MULTIPLIERS.get(unit, 1.0)
        candidates.append(base * multiplier)
    if not candidates:
        return None
    return max(candidates)


def _parse_item_2_03(
    span_text: str, accession: str, cik: str, report_year: int | None,
) -> list[dict[str, Any]]:
    """Heuristic body-parse of Item 2.03.

    Strategy mirrors Item 1.01: find candidate (obligation_type, creditor, amount)
    triples via regex. Dedupe by normalized creditor name. Always preserve full
    Item text for downstream LLM refinement.

    Multiple obligations in one 8-K (e.g., revolving credit facility +
    accordion sublimit) emit multiple rows.
    """
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    item_text_raw = _truncate(span_text, 60_000)
    eff_date = parse_event_date(span_text[:3000])
    obligation_type = classify_obligation_type(span_text[:8000])
    obligation_amount = _extract_obligation_amount(span_text)

    seq = 0
    for m in _OBLIGATION_CREDITOR_RE.finditer(span_text):
        creditor = (m.group(2) or "").strip()
        # Trim trailing prepositional/relative clauses.
        creditor = re.split(
            r"\s+(?:dated|effective|pursuant|providing|in\s+an\s+aggregate)\b",
            creditor, flags=re.I,
        )[0]
        creditor = creditor.strip(" .,;")
        if not creditor or len(creditor) > 250:
            continue
        cr_norm = normalize_company_name(creditor)
        if not cr_norm or cr_norm in seen:
            continue
        seen.add(cr_norm)
        seq += 1
        rows.append({
            "accession_number": accession,
            "cik_normalized": cik,
            "obligation_event_seq": seq,
            "obligation_type": obligation_type,
            "creditor_name_raw": creditor,
            "creditor_name_normalized": cr_norm,
            "obligation_amount_usd": obligation_amount,
            "effective_date": eff_date,
            "item_text_raw": item_text_raw,
            "report_year": report_year,
        })

    # Always emit at least one row even if no candidate matched, so the full
    # text reaches downstream LLM extractors (consistent with the 4 sibling
    # per-item parsers).
    if not rows:
        rows.append({
            "accession_number": accession,
            "cik_normalized": cik,
            "obligation_event_seq": 1,
            "obligation_type": obligation_type,
            "creditor_name_raw": None,
            "creditor_name_normalized": None,
            "obligation_amount_usd": obligation_amount,
            "effective_date": eff_date,
            "item_text_raw": item_text_raw,
            "report_year": report_year,
        })

    return rows


# -------------------------------------------------------------------- #
# Helpers
# -------------------------------------------------------------------- #


def _truncate(s: str | None, max_len: int) -> str | None:
    if s is None:
        return None
    if len(s) <= max_len:
        return s
    return s[:max_len]

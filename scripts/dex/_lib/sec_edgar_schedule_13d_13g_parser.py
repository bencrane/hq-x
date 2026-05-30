"""SEC EDGAR Schedule 13D + 13G parser — HTML → structured records.

Schedule 13D/G has no XBRL exhibit. Every modern filing is HTML-only with
a canonical cover-page structure (numbered rows 1-14) repeated once per
reporting person; multi-filer 13Ds concatenate cover pages.

Cover-page anchors (case-insensitive):

  (1)  NAMES OF REPORTING PERSONS                     → name + identifier
  (4)  SOURCE OF FUNDS                                 (13D-only, optional)
  (6)  CITIZENSHIP OR PLACE OF ORGANIZATION            → citizenship/jurisdiction
  (7)  SOLE VOTING POWER                               → int
  (8)  SHARED VOTING POWER                             → int
  (9)  SOLE DISPOSITIVE POWER                          → int
  (10) SHARED DISPOSITIVE POWER                        → int
  (11) AGGREGATE AMOUNT BENEFICIALLY OWNED ...         → int
  (13) PERCENT OF CLASS REPRESENTED BY AMOUNT IN ROW   → float
  (14) TYPE OF REPORTING PERSON                        → SEC code (IN, CO, ...)

Subject-company header (top of filing, before first cover page):

  Name of Issuer                                       → company name
  Title of Class of Securities                         → class
  CUSIP Number                                         → 9-char id

Item bodies (post-cover-page) anchored on "Item N. / ITEM N: / Item N -".

Parser output (per filing) — five lists of dicts:

  filings: [filing_meta]                       # always exactly 1 record
  reporting_persons: [{...}, ...]              # >=1 per filing in success case
  share_amounts: [{...}, ...]                  # 1:1 with reporting_persons
  items: [{item_number, item_text_raw}, ...]   # 0..7 (13D) or 0..10 (13G)
  subject_company: [{...}]                     # exactly 1

Schema details: see directive
~/Desktop/hq/directives/2026-05-09-sec-edgar-schedule-13d-13g-r2-ingest.md
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from lxml import html as lxml_html

try:
    # Runtime path: ``sys.path`` includes scripts/ (set by the orchestrator).
    from _lib.sec_edgar_schedule_13d_13g_normalize import (
        classify_reporting_person_type,
        normalize_accession,
        normalize_cik,
        normalize_cusip,
        normalize_ein,
        normalize_filer_name,
        normalize_lei,
        normalize_person_name,
        normalize_state,
        parse_percent,
        parse_share_amount,
    )
except ImportError:
    # Test path: imported as ``scripts._lib.…`` from repo root.
    from scripts._lib.sec_edgar_schedule_13d_13g_normalize import (  # type: ignore[no-redef]
        classify_reporting_person_type,
        normalize_accession,
        normalize_cik,
        normalize_cusip,
        normalize_ein,
        normalize_filer_name,
        normalize_lei,
        normalize_person_name,
        normalize_state,
        parse_percent,
        parse_share_amount,
    )

log = logging.getLogger("sec-edgar-schedule-13d-13g-parser")


# -------------------------------------------------------------------- #
# Public types
# -------------------------------------------------------------------- #


@dataclass(frozen=True)
class FilingHeader:
    """Inputs available at discovery time (from form.idx + EDGAR header).
    The orchestrator populates this and hands it to ``parse_filing``."""

    filer_cik_raw: str
    filer_name_raw: str
    accession_raw: str
    form_type: str               # 'SC 13D' | 'SC 13G' | 'SC 13D/A' | 'SC 13G/A'
    filing_date: str             # 'YYYY-MM-DD'
    primary_doc_url: str
    raw_html_r2_uri: str | None = None


# -------------------------------------------------------------------- #
# HTML → plaintext line stream
# -------------------------------------------------------------------- #


_NBSP_RE = re.compile(r"\xa0")
_WS_INLINE_RE = re.compile(r"[ \t]+")


def _doc_lines(primary_html: str | bytes) -> list[str]:
    """Render the HTML document to a list of non-empty trimmed lines.

    Tables, divs, paragraphs, and <br> all introduce line breaks. The
    output is suitable for label-anchored regex scanning.
    """
    try:
        if isinstance(primary_html, bytes):
            try:
                doc = lxml_html.fromstring(primary_html)
            except (ValueError, lxml_html.etree.ParserError):
                doc = lxml_html.fromstring(primary_html.decode("utf-8", "ignore"))
        else:
            doc = lxml_html.fromstring(primary_html)
    except (lxml_html.etree.ParserError, ValueError, lxml_html.etree.XMLSyntaxError):
        return []

    # Inject newlines around block-ish tags so text_content() preserves
    # boundaries.
    for el in doc.iter():
        if el.tag in ("br", "tr", "td", "th", "p", "div", "li", "h1", "h2",
                      "h3", "h4", "h5", "h6", "section", "table"):
            el.tail = "\n" + (el.tail or "")
            if el.text:
                el.text = "\n" + el.text

    raw = doc.text_content() or ""
    raw = _NBSP_RE.sub(" ", raw)
    out: list[str] = []
    for ln in raw.splitlines():
        ln = _WS_INLINE_RE.sub(" ", ln).strip()
        if ln:
            out.append(ln)
    return out


# -------------------------------------------------------------------- #
# Subject-company extractor
# -------------------------------------------------------------------- #


_ISSUER_LABEL_RE = re.compile(r"NAME\s+OF\s+ISSUER", re.I)
_CUSIP_LABEL_RE = re.compile(r"CUSIP\s+(NO\.?|NUMBER)", re.I)
_TITLE_OF_CLASS_RE = re.compile(r"TITLE\s+OF\s+CLASS\s+OF\s+SECURIT", re.I)
_PRINCIPAL_OFFICES_RE = re.compile(
    r"(ADDRESS\s+OF\s+(ITS|THE)\s+PRINCIPAL\s+EXECUTIVE\s+OFFICES?|"
    r"PRINCIPAL\s+EXECUTIVE\s+OFFICES?)",
    re.I,
)


def _next_nonempty(lines: list[str], idx: int, lookahead: int = 6) -> str | None:
    """Return the first non-label line within ``lookahead`` positions
    after ``idx``. Skips lines that are only labels themselves."""
    for j in range(idx + 1, min(idx + 1 + lookahead, len(lines))):
        candidate = lines[j].strip()
        if not candidate:
            continue
        # Skip "(NAME OF ISSUER)" / "(CUSIP NUMBER)" parenthetical style hints.
        if re.fullmatch(r"\(.+\)", candidate):
            continue
        return candidate
    return None


def _prev_nonempty(lines: list[str], idx: int, lookback: int = 6) -> str | None:
    """Return the first non-empty, non-parenthetical-label line within
    ``lookback`` positions before ``idx``. Used for the canonical SEC
    cover-page layout where the value precedes its parenthetical label."""
    for j in range(idx - 1, max(idx - 1 - lookback, -1), -1):
        candidate = lines[j].strip()
        if not candidate:
            continue
        # Skip "(NAME OF ISSUER)" / "(CUSIP NUMBER)" parenthetical hints.
        if re.fullmatch(r"\(.+\)", candidate):
            continue
        return candidate
    return None


def _extract_subject_company(lines: list[str]) -> dict[str, Any]:
    """Pull the issuer block from near the top of the filing.

    Returns dict with raw + normalized fields. Best-effort — fields default
    to None when the canonical anchors aren't found.
    """
    issuer_name: str | None = None
    cusip_raw: str | None = None
    title_of_class: str | None = None
    principal_office_block: str | None = None

    # Limit to first 200 lines; cover pages start after the issuer block.
    head = lines[:200]
    # SEC canonical layout: value precedes its "(Label)" parenthetical line.
    # Some filer-generated forms invert this. Try BEFORE first, AFTER as
    # fallback.
    for i, ln in enumerate(head):
        is_parenthetical = bool(re.fullmatch(r"\(.+\)", ln.strip()))

        if issuer_name is None and _ISSUER_LABEL_RE.search(ln):
            cand = _prev_nonempty(head, i, lookback=4) if is_parenthetical else None
            if not cand:
                cand = _next_nonempty(head, i, lookahead=8)
            if cand:
                issuer_name = cand
        if cusip_raw is None and _CUSIP_LABEL_RE.search(ln):
            cand_prev = _prev_nonempty(head, i, lookback=4) if is_parenthetical else None
            cand = normalize_cusip(cand_prev) if cand_prev else None
            if not cand:
                # CUSIP may be on the same line as the label.
                tail = ln[ln.upper().find("CUSIP"):]
                cand = normalize_cusip(tail)
            if not cand:
                nxt = _next_nonempty(head, i, lookahead=4)
                cand = normalize_cusip(nxt) if nxt else None
            if cand:
                cusip_raw = cand
        if title_of_class is None and _TITLE_OF_CLASS_RE.search(ln):
            cand = _prev_nonempty(head, i, lookback=4) if is_parenthetical else None
            if not cand:
                cand = _next_nonempty(head, i, lookahead=4)
            if cand:
                title_of_class = cand
        if principal_office_block is None and _PRINCIPAL_OFFICES_RE.search(ln):
            cand = _prev_nonempty(head, i, lookback=4) if is_parenthetical else None
            if not cand:
                cand = _next_nonempty(head, i, lookahead=4)
            if cand:
                principal_office_block = cand

    # Heuristic: if issuer name still missing but CUSIP is found, take the
    # second non-empty line of the document as a fallback (most filings
    # render "<ISSUER NAME>" as the second line after the SCHEDULE banner).
    if issuer_name is None and cusip_raw is not None and len(head) >= 3:
        for cand in head[:8]:
            if not re.search(r"SCHEDULE\s+13[DG]|UNDER\s+THE\s+SECURITIES", cand, re.I):
                if len(cand) >= 3 and not re.search(r"^[\d\(\)\.]+$", cand):
                    issuer_name = cand
                    break

    street, city, state, zip5 = _split_address_block(principal_office_block)

    return {
        "subject_company_name_raw": issuer_name,
        "subject_company_name_normalized": normalize_filer_name(issuer_name),
        "subject_company_cusip": cusip_raw,
        "subject_company_title_of_class": title_of_class,
        "subject_company_principal_office_street": street,
        "subject_company_principal_office_city": city,
        "subject_company_principal_office_state": state,
        "subject_company_principal_office_zip5": zip5,
    }


_ZIP5_RE = re.compile(r"\b(\d{5})(?:-?\d{4})?\b")
_STATE_2_RE = re.compile(r"\b([A-Z]{2})\b")


def _split_address_block(addr: str | None) -> tuple[str | None, str | None, str | None, str | None]:
    """Best-effort US-style address split: ``street, city, ST 12345``.

    Returns ``(street, city, state, zip5)``. None for any field that can't
    be confidently extracted.
    """
    if not addr:
        return (None, None, None, None)
    s = addr.strip()
    if not s:
        return (None, None, None, None)
    zip_m = _ZIP5_RE.search(s)
    zip5 = zip_m.group(1) if zip_m else None
    if zip_m:
        before_zip = s[:zip_m.start()].rstrip(", ")
    else:
        before_zip = s
    state = None
    state_m = _STATE_2_RE.search(before_zip[-12:]) if before_zip else None
    if state_m:
        state = state_m.group(1)
        before_state = before_zip[:before_zip.rfind(state)].rstrip(", ")
    else:
        before_state = before_zip
    if "," in before_state:
        head, _, tail = before_state.rpartition(",")
        return (head.strip() or None, tail.strip() or None, state, zip5)
    return (before_state or None, None, state, zip5)


# -------------------------------------------------------------------- #
# Cover-page extractor
# -------------------------------------------------------------------- #


_NAMES_RP_RE = re.compile(r"NAMES?\s+OF\s+REPORTING\s+PERSONS?", re.I)
_CITIZENSHIP_RE = re.compile(
    r"CITIZENSHIP\s+OR\s+PLACE\s+OF\s+ORGANIZATION", re.I,
)
_SOLE_VOTING_RE = re.compile(r"SOLE\s+VOTING\s+POWER", re.I)
_SHARED_VOTING_RE = re.compile(r"SHARED\s+VOTING\s+POWER", re.I)
_SOLE_DISP_RE = re.compile(r"SOLE\s+DISPOSITIVE\s+POWER", re.I)
_SHARED_DISP_RE = re.compile(r"SHARED\s+DISPOSITIVE\s+POWER", re.I)
_AGGREGATE_AMT_RE = re.compile(
    r"AGGREGATE\s+AMOUNT\s+BENEFICIALLY\s+OWNED",
    re.I,
)
_PERCENT_OF_CLASS_RE = re.compile(
    r"PERCENT\s+OF\s+CLASS\b", re.I,
)
_TYPE_OF_RP_RE = re.compile(
    r"TYPE\s+OF\s+REPORTING\s+PERSON", re.I,
)
_SOURCE_OF_FUNDS_RE = re.compile(
    r"SOURCE\s+OF\s+FUNDS", re.I,
)

# Boilerplate sub-labels that appear under the canonical row anchors:
#   - "I.R.S. IDENTIFICATION NO. OF ABOVE PERSON" / "OF ABOVE PERSONS"
#   - "S.S. OR I.R.S. IDENTIFICATION NO."
#   - "(ENTITIES ONLY)"
#   - "SEE INSTRUCTIONS"
#   - "MEMBER OF A GROUP" — item 2's printed text
#   - "DISCLOSURE OF LEGAL PROCEEDINGS" — item 5
_COVER_BOILERPLATE_RE = re.compile(
    r"\b(?:"
    r"I\.?R\.?S\.?\s+IDENTIFICATION"
    r"|"
    r"S\.?\s*S\.?\s+OR\s+I\.?R\.?S\.?"
    r"|"
    r"OF\s+ABOVE\s+PERSON"
    r"|"
    r"\(ENTITIES?\s+ONLY\)"
    r"|"
    r"\(SEE\s+INSTRUCTIONS?\)"
    r"|"
    r"MEMBER\s+OF\s+A\s+GROUP"
    r"|"
    r"DISCLOSURE\s+OF\s+LEGAL\s+PROCEEDINGS"
    r"|"
    r"CHECK\s+(THE\s+)?APPROPRIATE\s+BOX"
    r"|"
    r"CHECK\s+BOX\s+IF"
    r"|"
    r"SEC\s+USE\s+ONLY"
    r"|"
    r"EXCLUDES\s+CERTAIN\s+SHARES"
    r"|"
    r"NUMBER\s+OF\s+SHARES\s+BENEFICIALLY\s+OWNED\s+BY\s+EACH"
    r"|"
    r"WITH:?$"
    r")",
    re.I,
)

# Numeric-content lines: pure numbers w/ optional commas / footnote markers.
_NUMERIC_LINE_RE = re.compile(r"^[\-\s]*[\d,\.]+(?:\s*\(\d+\))?\s*\*?\*?\s*$")
_PERCENT_LINE_RE = re.compile(r"^\s*[\d,\.]+\s*%?\s*\*?\*?\s*$|^\s*\*\s*$|^\s*\*\*\s*$")


def _value_after_label(
    lines: list[str],
    idx: int,
    *,
    lookahead: int = 12,
    skip_labels: bool = True,
    label_pattern: re.Pattern[str] | None = None,
) -> str | None:
    """Return the first plausible value line after ``idx``.

    Skips: bare row numbers, parenthetical instructions, cover-page
    boilerplate sub-labels (I.R.S. IDENTIFICATION, S.S. OR I.R.S., etc.).
    Stops at the next cover-page anchor (different label) or item-body
    heading.
    """
    LABEL_PREFIX_RE = re.compile(r"^\s*\(?\s*\d{1,2}\s*\)?\s*$")
    next_label_anchors = (
        _NAMES_RP_RE, _CITIZENSHIP_RE, _SOLE_VOTING_RE, _SHARED_VOTING_RE,
        _SOLE_DISP_RE, _SHARED_DISP_RE, _AGGREGATE_AMT_RE,
        _PERCENT_OF_CLASS_RE, _TYPE_OF_RP_RE, _SOURCE_OF_FUNDS_RE,
    )
    for j in range(idx + 1, min(idx + 1 + lookahead, len(lines))):
        cand = lines[j].strip()
        if not cand:
            continue
        if skip_labels and LABEL_PREFIX_RE.match(cand):
            continue
        # Stop on item-body heading.
        if _ITEM_BODY_HEADER_RE.match(cand):
            return None
        # Skip cover-page boilerplate sub-labels.
        if _COVER_BOILERPLATE_RE.search(cand):
            continue
        # If we hit another anchor that's NOT the requested label_pattern,
        # bail out — the value didn't appear before the next label.
        if label_pattern is None or not label_pattern.search(cand):
            for anchor in next_label_anchors:
                if anchor is label_pattern:
                    continue
                if anchor.search(cand):
                    return None
        # Skip pure "(N)" / "(SEE INSTRUCTIONS)" hint lines.
        if re.fullmatch(r"\([^)]*\)", cand):
            continue
        if re.fullmatch(r"\d{1,2}", cand):  # bare row number
            continue
        return cand
    return None


_BARE_ROW_NUMBER_RE = re.compile(r"^\s*\(?\s*([1-9]|1[0-4])\s*\)?\s*$")

# SEC's 11 official "Type of Reporting Person" codes. Item 14 expects one
# (or several comma-separated). We keep only valid 2- or 3-letter codes
# from the value cell; everything else (e.g. trailing ``Instructions)``
# residue) is dropped.
_VALID_RP_TYPE_CODES: frozenset[str] = frozenset({
    "BD", "BK", "IC", "IV", "IA", "EP", "HC", "SA", "CP", "CO",
    "PN", "PF", "OO", "IN",
})


def _clean_rp_type_code(raw: str | None) -> str | None:
    """Reduce an item-14 cell value to comma-joined valid SEC codes.

    Real cells often look like ``"PN, OO"``, ``"IN"``, or
    ``"OO (Limited Liability Company)"``. Garbage like ``"Instructions)"``
    or ``"Calculated based on..."`` is dropped — only tokens in
    ``_VALID_RP_TYPE_CODES`` survive when valid codes are present.
    Returns None when neither a valid code NOR a plausible short
    free-text descriptor is found.
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    # Drop boilerplate residue.
    if re.search(r"INSTRUCTION", s, re.I):
        return None
    if re.search(r"CALCULATED\s+BASED\s+ON", s, re.I):
        return None
    tokens = re.findall(r"\b[A-Z]{2,3}\b", s.upper())
    valid = [t for t in tokens if t in _VALID_RP_TYPE_CODES]
    if valid:
        seen: list[str] = []
        for t in valid:
            if t not in seen:
                seen.append(t)
        return ", ".join(seen)
    # No valid code, but if the cell is short (≤ 60 chars) and isn't
    # an obvious anchor mismatch, keep it — operator can inspect.
    if len(s) <= 60 and not re.search(r"\d{4,}", s):
        return s
    return None
_COVER_PAGE_ANCHORS: tuple[re.Pattern[str], ...] = (
    _NAMES_RP_RE, _CITIZENSHIP_RE, _SOLE_VOTING_RE, _SHARED_VOTING_RE,
    _SOLE_DISP_RE, _SHARED_DISP_RE, _AGGREGATE_AMT_RE,
    _PERCENT_OF_CLASS_RE, _TYPE_OF_RP_RE, _SOURCE_OF_FUNDS_RE,
)


def _is_skippable_label_line(cand: str, *, exclude: re.Pattern[str] | None = None) -> bool:
    """Return True for cover-page label / row-number / instruction lines that
    must be skipped when scanning forward for a value."""
    if _BARE_ROW_NUMBER_RE.match(cand):
        return True
    if re.fullmatch(r"\([^)]*\)", cand):
        return True
    if _COVER_BOILERPLATE_RE.search(cand):
        return True
    for anchor in _COVER_PAGE_ANCHORS:
        if anchor is exclude:
            continue
        if anchor.search(cand):
            return True
    return False


_ITEM_BODY_HEADER_RE = re.compile(r"^\s*ITEM\s+\d+\s*[\.:\-]", re.I)

# Trailing same-line numeric — match ``"... ROW (11)    7,123,456"`` or
# ``"... ROW 11   7.4%"``. Used when the cover-page label and value share
# a single line/cell.
_TRAILING_NUMERIC_RE = re.compile(
    r"\b([\d][\d,\.]*)\s*\*?\*?\s*(?:\(\d+\))?\s*$"
)
_TRAILING_PERCENT_RE = re.compile(
    r"([\d][\d,\.]*)\s*%\s*\*?\*?\s*$"
)


_ROW_REF_RE = re.compile(r"(?:ROW|ITEM)\s*\(?\s*\d{1,2}\s*\)?", re.I)


def _same_line_extract_numeric(line: str) -> int | None:
    """Pull a numeric value from the trailing portion of an anchor line.

    Strips ``"ROW (N)"`` / ``"ROW N"`` / parenthetical row markers
    before scanning, so the anchor phrase doesn't bleed digits into
    the result.

    Returns a value only when the trailing token has at least 4 digits
    OR a comma — too-short bare numbers are almost always row labels.
    """
    s = re.sub(r"\(\s*\d{1,2}\s*\)", "", line)
    s = _ROW_REF_RE.sub("", s)
    m = _TRAILING_NUMERIC_RE.search(s)
    if not m:
        return None
    tok = m.group(1)
    # Reject tokens that are too small to be plausible share counts.
    digits_only = tok.replace(",", "").replace(".", "")
    if "," not in tok and len(digits_only) < 4:
        return None
    return parse_share_amount(tok)


def _same_line_extract_percent(line: str) -> float | None:
    s = re.sub(r"\(\s*\d{1,2}\s*\)", "", line)
    s = _ROW_REF_RE.sub("", s)
    m = _TRAILING_PERCENT_RE.search(s)
    if m:
        return parse_percent(m.group(1))
    return None


def _same_line_or_after_numeric(
    lines: list[str], idx: int, *, lookahead: int = 16,
) -> int | None:
    """Try same-line trailing extraction first; fall back to next-line scan."""
    same = _same_line_extract_numeric(lines[idx])
    if same is not None:
        return same
    return _numeric_value_after(lines, idx, lookahead=lookahead)


def _same_line_or_after_percent(
    lines: list[str], idx: int, *, lookahead: int = 16,
) -> float | None:
    same = _same_line_extract_percent(lines[idx])
    if same is not None:
        return same
    return _percent_value_after(lines, idx, lookahead=lookahead)


def _numeric_value_after(lines: list[str], idx: int, *, lookahead: int = 16) -> int | None:
    """Find the first plausible numeric value line after ``idx``.

    Skips: bare row-number lines (1..14), parenthetical instructions,
    other cover-page anchor labels. Continues past those (stacked-layout
    cover pages emit all labels then all values). Stops only at item-body
    headings ("Item 1.") which mark the end of the cover page."""
    for j in range(idx + 1, min(idx + 1 + lookahead, len(lines))):
        cand = lines[j].strip()
        if not cand:
            continue
        if _ITEM_BODY_HEADER_RE.match(cand):
            return None
        if _is_skippable_label_line(cand):
            continue
        if _NUMERIC_LINE_RE.match(cand):
            return parse_share_amount(cand)
        m = re.match(r"^\s*([\-\d,\.]+)(?:\s*\(\d+\))?\s*$", cand)
        if m:
            return parse_share_amount(m.group(1))
        # Long prose (e.g. an Item 4 narrative) means we've fallen out of
        # the cover-page region.
        if len(cand) > 60:
            return None
    return None


def _percent_value_after(lines: list[str], idx: int, *, lookahead: int = 16) -> float | None:
    for j in range(idx + 1, min(idx + 1 + lookahead, len(lines))):
        cand = lines[j].strip()
        if not cand:
            continue
        if _ITEM_BODY_HEADER_RE.match(cand):
            return None
        if _is_skippable_label_line(cand):
            continue
        if _PERCENT_LINE_RE.match(cand):
            return parse_percent(cand)
        m = re.match(r"^\s*([\d,\.\<\*\-—]+)\s*%?\s*$", cand)
        if m:
            return parse_percent(m.group(1))
        if len(cand) > 60:
            return None
    return None


def _segment_cover_pages(lines: list[str]) -> list[tuple[int, int]]:
    """Find ``(start, end)`` line-index pairs, one per reporting-person
    cover page. Boundaries are anchored on "NAMES OF REPORTING PERSONS"
    occurrences; each cover page extends until the next anchor or until
    the first item-body anchor.
    """
    starts: list[int] = []
    for i, ln in enumerate(lines):
        if _NAMES_RP_RE.search(ln):
            starts.append(i)

    if not starts:
        return []

    # Earliest item-body anchor (Item 1 or Item 2 etc.) — caps last cover page.
    item_anchor_idx = len(lines)
    item_re = re.compile(r"^\s*ITEM\s+\d+\s*[\.:\-]", re.I)
    for i, ln in enumerate(lines):
        if i > starts[-1] and item_re.match(ln):
            item_anchor_idx = i
            break

    out: list[tuple[int, int]] = []
    for k, s in enumerate(starts):
        end = starts[k + 1] if k + 1 < len(starts) else item_anchor_idx
        out.append((s, end))
    return out


def _extract_one_cover_page(
    lines: list[str], start: int, end: int,
) -> dict[str, Any] | None:
    """Extract a single reporting-person record + share-amount record from
    a cover-page slice."""
    block = lines[start:end]
    if not block:
        return None

    name_idx = None
    citizenship_idx = None
    sole_voting_idx = None
    shared_voting_idx = None
    sole_disp_idx = None
    shared_disp_idx = None
    aggregate_idx = None
    percent_idx = None
    type_idx = None
    source_funds_idx = None

    for i, ln in enumerate(block):
        if name_idx is None and _NAMES_RP_RE.search(ln):
            name_idx = i
            continue
        if citizenship_idx is None and _CITIZENSHIP_RE.search(ln):
            citizenship_idx = i
            continue
        if sole_voting_idx is None and _SOLE_VOTING_RE.search(ln):
            sole_voting_idx = i
            continue
        if shared_voting_idx is None and _SHARED_VOTING_RE.search(ln):
            shared_voting_idx = i
            continue
        if sole_disp_idx is None and _SOLE_DISP_RE.search(ln):
            sole_disp_idx = i
            continue
        if shared_disp_idx is None and _SHARED_DISP_RE.search(ln):
            shared_disp_idx = i
            continue
        if aggregate_idx is None and _AGGREGATE_AMT_RE.search(ln):
            aggregate_idx = i
            continue
        if percent_idx is None and _PERCENT_OF_CLASS_RE.search(ln):
            percent_idx = i
            continue
        if type_idx is None and _TYPE_OF_RP_RE.search(ln):
            type_idx = i
            continue
        if source_funds_idx is None and _SOURCE_OF_FUNDS_RE.search(ln):
            source_funds_idx = i

    if name_idx is None:
        return None

    name_raw = _value_after_label(
        block, name_idx, lookahead=10, label_pattern=_NAMES_RP_RE,
    )
    if not name_raw:
        return None

    # Inline EIN/SSN sometimes follows on its own line ("S.S. or I.R.S.
    # Identification No. of Above Person").
    ein_raw: str | None = None
    for j in range(name_idx + 1, min(name_idx + 8, len(block))):
        cand = block[j].strip()
        if not cand or cand == name_raw:
            continue
        if normalize_ein(cand):
            ein_raw = cand
            break
        if re.search(r"\b\d{2}-?\d{7}\b", cand):
            ein_raw = cand
            break
        # If the next anchored label appears, stop.
        if any(p.search(cand) for p in (
            _CITIZENSHIP_RE, _SOLE_VOTING_RE, _SOURCE_OF_FUNDS_RE,
        )):
            break

    citizenship = (
        _value_after_label(block, citizenship_idx, label_pattern=_CITIZENSHIP_RE)
        if citizenship_idx is not None else None
    )

    rp_type_code_raw = (
        _value_after_label(block, type_idx, label_pattern=_TYPE_OF_RP_RE)
        if type_idx is not None else None
    )
    rp_type_code = _clean_rp_type_code(rp_type_code_raw)

    sole_voting = (
        _same_line_or_after_numeric(block, sole_voting_idx)
        if sole_voting_idx is not None else None
    )
    shared_voting = (
        _same_line_or_after_numeric(block, shared_voting_idx)
        if shared_voting_idx is not None else None
    )
    sole_disp = (
        _same_line_or_after_numeric(block, sole_disp_idx)
        if sole_disp_idx is not None else None
    )
    shared_disp = (
        _same_line_or_after_numeric(block, shared_disp_idx)
        if shared_disp_idx is not None else None
    )
    aggregate = (
        _same_line_or_after_numeric(block, aggregate_idx)
        if aggregate_idx is not None else None
    )
    # Item 11 is the disclosed aggregate. When the parser misses it, fall
    # back to max(sole+shared voting, sole+shared dispositive) — the
    # SEC convention is that aggregate ≥ either sum.
    if aggregate is None:
        voting_total = (sole_voting or 0) + (shared_voting or 0)
        dispositive_total = (sole_disp or 0) + (shared_disp or 0)
        fallback = max(voting_total, dispositive_total)
        if fallback > 0:
            aggregate = fallback
    percent_of_class = (
        _same_line_or_after_percent(block, percent_idx)
        if percent_idx is not None else None
    )

    first, last = normalize_person_name(name_raw)
    rp_type = classify_reporting_person_type(
        name_raw, has_ein=bool(normalize_ein(ein_raw)),
    )

    return {
        "reporting_person_name_raw": name_raw,
        "reporting_person_name_normalized": normalize_filer_name(name_raw),
        "reporting_person_type": rp_type,
        "reporting_person_first_normalized": first if rp_type == "person" else None,
        "reporting_person_last_normalized": last if rp_type == "person" else None,
        "reporting_person_ein_normalized": normalize_ein(ein_raw),
        "reporting_person_lei_normalized": None,  # Schedule cover page rarely lists LEI; reserved.
        "citizenship": normalize_state(citizenship),
        "rp_type_code": rp_type_code,
        "sole_voting_power": sole_voting,
        "shared_voting_power": shared_voting,
        "sole_dispositive_power": sole_disp,
        "shared_dispositive_power": shared_disp,
        "shares_beneficially_owned": aggregate,
        "percent_of_class": percent_of_class,
    }


# -------------------------------------------------------------------- #
# Item bodies
# -------------------------------------------------------------------- #


_ITEM_HEADER_RE = re.compile(
    r"^\s*ITEM\s+(\d+)\s*[\.:\-]\s*(.{0,120}?)\s*$",
    re.I,
)


def _extract_items(lines: list[str]) -> list[dict[str, Any]]:
    """Find Item 1..7 (13D) / Item 1..10 (13G) headings and extract each
    item's body until the next item heading.

    Returns one record per detected item.
    """
    headings: list[tuple[int, int, str]] = []  # (line_idx, item_number, label)
    for i, ln in enumerate(lines):
        m = _ITEM_HEADER_RE.match(ln)
        if not m:
            continue
        try:
            n = int(m.group(1))
        except ValueError:
            continue
        if n < 1 or n > 12:  # 13D has 1-7, 13G has 1-10; cap at 12 generously.
            continue
        label = m.group(2).strip() or None
        headings.append((i, n, label or ""))

    if not headings:
        return []

    out: list[dict[str, Any]] = []
    for k, (i, n, label) in enumerate(headings):
        end = headings[k + 1][0] if k + 1 < len(headings) else len(lines)
        body_lines = lines[i + 1:end]
        body = "\n".join(body_lines).strip()
        # Cap item-text to avoid massive Item 4 / Item 6 dumps; keep first 32K.
        if len(body) > 32_000:
            body = body[:32_000] + " ... [truncated]"
        out.append({
            "item_number": n,
            "item_label": label[:200] if label else None,
            "item_text_raw": body,
        })
    return out


# -------------------------------------------------------------------- #
# Amendment heuristic
# -------------------------------------------------------------------- #


def _is_amendment(form_type: str) -> bool:
    return form_type.endswith("/A")


# -------------------------------------------------------------------- #
# Event-date heuristic
# -------------------------------------------------------------------- #


_EVENT_DATE_LINE_RE = re.compile(
    r"DATE\s+OF\s+(THE\s+)?EVENT\s+(WHICH\s+)?REQUIRES?\s+FILING", re.I,
)
_DATE_RE = re.compile(
    r"\b("
    r"(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+\d{1,2},?\s+\d{4}"
    r"|"
    r"\d{1,2}/\d{1,2}/\d{4}"
    r"|"
    r"\d{4}-\d{2}-\d{2}"
    r")\b",
    re.I,
)
_MONTH_TO_NUM = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
}


def _extract_event_date(lines: list[str]) -> str | None:
    head = lines[:200]
    for i, ln in enumerate(head):
        if not _EVENT_DATE_LINE_RE.search(ln):
            continue
        # Canonical SEC layout: date precedes "(Date of Event ...)" label.
        for j in range(i - 1, max(i - 6, -1), -1):
            m = _DATE_RE.search(head[j])
            if m:
                return _normalize_date_str(m.group(1))
        # Fallback: filer-generated layouts may put date AFTER the label.
        for j in range(i, min(i + 6, len(head))):
            m = _DATE_RE.search(head[j])
            if m:
                return _normalize_date_str(m.group(1))
    return None


def _normalize_date_str(s: str) -> str | None:
    """Normalize "January 5, 2024" / "1/5/2024" / "2024-01-05" → ISO."""
    s = s.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        mm, dd, yy = m.group(1).zfill(2), m.group(2).zfill(2), m.group(3)
        return f"{yy}-{mm}-{dd}"
    m = re.fullmatch(
        r"(?i)([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", s,
    )
    if m:
        mon = m.group(1).lower()
        mm = _MONTH_TO_NUM.get(mon)
        if mm is None:
            return None
        dd = m.group(2).zfill(2)
        yy = m.group(3)
        return f"{yy}-{mm}-{dd}"
    return None


# -------------------------------------------------------------------- #
# Top-level entry point
# -------------------------------------------------------------------- #


def parse_filing(
    header: FilingHeader,
    primary_html: str | bytes,
) -> dict[str, list[dict[str, Any]]]:
    """Parse one Schedule 13D/G filing → structured records.

    Always returns a non-empty ``filings`` list (1 row) and a
    ``subject_company`` list (1 row, possibly with NULL fields).
    Secondary streams (reporting_persons, share_amounts, items) are
    populated when the cover-page parser finds them.
    """
    filer_cik = normalize_cik(header.filer_cik_raw)
    accession = normalize_accession(header.accession_raw)
    filer_name = normalize_filer_name(header.filer_name_raw)

    schedule_year = (
        int(header.filing_date[:4])
        if header.filing_date and len(header.filing_date) >= 4
        else None
    )

    filing_meta = {
        "accession_number": accession,
        "filer_cik_normalized": filer_cik,
        "filer_legal_name_normalized": filer_name,
        "filer_legal_name_raw": header.filer_name_raw,
        "form_type": header.form_type,
        "is_amendment": _is_amendment(header.form_type),
        "original_accession_number": None,
        "filing_date": header.filing_date,
        "event_date": None,
        "schedule_year": schedule_year,
        "primary_doc_url": header.primary_doc_url,
        "raw_html_r2_uri": header.raw_html_r2_uri,
    }

    # Subject-company stub even if HTML parse fails entirely.
    subject_stub = {
        "accession_number": accession,
        "subject_company_cik_normalized": None,
        "subject_company_name_raw": None,
        "subject_company_name_normalized": None,
        "subject_company_cusip": None,
        "subject_company_ticker": None,
        "subject_company_title_of_class": None,
        "subject_company_principal_office_street": None,
        "subject_company_principal_office_city": None,
        "subject_company_principal_office_state": None,
        "subject_company_principal_office_zip5": None,
        "schedule_year": schedule_year,
    }

    out: dict[str, list[dict[str, Any]]] = {
        "filings": [filing_meta],
        "reporting_persons": [],
        "share_amounts": [],
        "items": [],
        "subject_company": [subject_stub],
    }

    if not primary_html:
        return out

    lines = _doc_lines(primary_html)
    if not lines:
        return out

    # Subject company.
    subject_extracted = _extract_subject_company(lines)
    out["subject_company"] = [{**subject_stub, **subject_extracted}]

    # Event date (cover page header).
    event_date = _extract_event_date(lines)
    if event_date:
        filing_meta["event_date"] = event_date

    # Cover pages → reporting_persons + share_amounts.
    cover_segments = _segment_cover_pages(lines)
    cusip = subject_extracted.get("subject_company_cusip")
    for seq, (start, end) in enumerate(cover_segments, start=1):
        rp = _extract_one_cover_page(lines, start, end)
        if rp is None:
            continue
        out["reporting_persons"].append({
            "accession_number": accession,
            "reporting_person_seq": seq,
            "reporting_person_name_raw": rp["reporting_person_name_raw"],
            "reporting_person_name_normalized": rp["reporting_person_name_normalized"],
            "reporting_person_type": rp["reporting_person_type"],
            "reporting_person_first_normalized": rp["reporting_person_first_normalized"],
            "reporting_person_last_normalized": rp["reporting_person_last_normalized"],
            "reporting_person_lei_normalized": rp["reporting_person_lei_normalized"],
            "reporting_person_ein_normalized": rp["reporting_person_ein_normalized"],
            "address_street": None,
            "address_city": None,
            "address_state": None,
            "address_zip5": None,
            "address_country": None,
            "jurisdiction_of_organization": (
                rp["citizenship"] if rp["reporting_person_type"] == "entity" else None
            ),
            "citizenship": (
                rp["citizenship"] if rp["reporting_person_type"] == "person" else None
            ),
            "rp_type_code": rp["rp_type_code"],
            "schedule_year": schedule_year,
        })
        out["share_amounts"].append({
            "accession_number": accession,
            "reporting_person_seq": seq,
            "cusip": cusip,
            "shares_beneficially_owned": rp["shares_beneficially_owned"],
            "sole_voting_power": rp["sole_voting_power"],
            "shared_voting_power": rp["shared_voting_power"],
            "sole_dispositive_power": rp["sole_dispositive_power"],
            "shared_dispositive_power": rp["shared_dispositive_power"],
            "percent_of_class": rp["percent_of_class"],
            "schedule_year": schedule_year,
        })

    # Items 1..N.
    for item_rec in _extract_items(lines):
        out["items"].append({
            "accession_number": accession,
            "item_number": item_rec["item_number"],
            "item_label": item_rec["item_label"],
            "item_text_raw": item_rec["item_text_raw"],
            "schedule_year": schedule_year,
        })

    return out


__all__ = [
    "FilingHeader",
    "parse_filing",
]

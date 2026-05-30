"""SEC EDGAR Form 10-K parser — HTML → 7 structured streams.

Form 10-K primary documents are HTML reports of 10-50 MB with a section
structure organized by Items (1, 1A, 1B, 2, 3, 4, ..., 10, 11, 12, ...).
This parser dispatches per-Item:

    Item 1A  → ``risk_factors``           text-anchor + bolded-heading split
    Item 2   → ``properties``             table-first, paragraph fallback
    Item 3   → ``legal_proceedings``      paragraph extraction
    Item 10  → ``officers_directors``     officers/directors table heuristic
    Item 11  → ``executive_compensation`` Summary Compensation Table (lifted
                                          DEF 14A header patterns)
    Item 12  → ``security_ownership``     Beneficial Ownership Table (lifted
                                          DEF 14A header patterns) + holder
                                          type classifier

Always returns a non-empty ``filings`` list (1 record); secondary streams may
be empty when the parser can't locate the canonical Item anchor or when the
filing incorporates Items 11/12 by reference to the registrant's DEF 14A.

Schema details: directive
~/Desktop/hq/directives/2026-05-09-sec-edgar-form-10k-r2-ingest.md
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Iterator

from lxml import html as lxml_html

from _lib.sec_edgar_form_10k_normalize import (
    normalize_accession,
    normalize_cik,
    normalize_filer_name,
    normalize_holder_name,
    normalize_person_name,
    normalize_title,
    parse_dollar_amount,
    parse_percent,
    parse_property_description,
    parse_share_count,
)


log = logging.getLogger("sec-edgar-form-10k-parser")


# -------------------------------------------------------------------- #
# Public types
# -------------------------------------------------------------------- #


@dataclass(frozen=True)
class FilingHeader:
    """Inputs available at discovery time (from form.idx + EDGAR header)."""

    cik_raw: str
    filer_name_raw: str
    accession_raw: str
    form_type: str
    filing_date: str            # 'YYYY-MM-DD'
    primary_doc_url: str
    raw_html_r2_uri: str | None = None
    filer_lei: str | None = None
    filer_ein: str | None = None
    period_of_report: str | None = None
    fiscal_year_end: str | None = None
    original_accession_number: str | None = None


# -------------------------------------------------------------------- #
# Cell / row helpers (lifted from DEF 14A parser; same semantics)
# -------------------------------------------------------------------- #


_NBSP_RE = re.compile(r"&nbsp;|\xa0")
_WS_INLINE_RE = re.compile(r"[ \t]+")


def _cell_text(el, *, preserve_lines: bool = False) -> str:
    if el is None:
        return ""
    if preserve_lines:
        for br in el.iter("br"):
            br.tail = "\n" + (br.tail or "")
        for p in el.iter("p"):
            if p.text:
                p.text = "\n" + p.text
        for div in el.iter("div"):
            if div.text:
                div.text = "\n" + div.text
        txt = el.text_content() or ""
        txt = _NBSP_RE.sub(" ", txt)
        lines = [_WS_INLINE_RE.sub(" ", ln).strip() for ln in txt.splitlines()]
        return "\n".join(ln for ln in lines if ln)
    txt = el.text_content() or ""
    txt = _NBSP_RE.sub(" ", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt


def _row_cells(tr, *, preserve_lines: bool = False) -> list[str]:
    return [
        _cell_text(c, preserve_lines=preserve_lines)
        for c in tr.findall(".//td") + tr.findall(".//th")
    ]


def _row_cells_compact(tr, *, preserve_lines: bool = False) -> list[str]:
    out: list[str] = []
    for c in tr.findall(".//td") + tr.findall(".//th"):
        txt = _cell_text(c, preserve_lines=preserve_lines)
        if not txt:
            continue
        if txt.strip() in ("$", "(", ")", "*", "%"):
            continue
        out.append(txt)
    return out


def _table_iter(doc) -> Iterator[Any]:
    for t in doc.iter("table"):
        yield t


def _classify_header_row(
    row_cells_upper: list[str],
    keys: dict[str, tuple[re.Pattern[str], ...]],
    *, min_matches: int = 3,
) -> dict[str, int] | None:
    name_to_idx: dict[str, int] = {}
    for canonical, patterns in keys.items():
        for idx, cell in enumerate(row_cells_upper):
            if any(p.search(cell) for p in patterns):
                if canonical not in name_to_idx:
                    name_to_idx[canonical] = idx
                break
    return name_to_idx if len(name_to_idx) >= min_matches else None


# -------------------------------------------------------------------- #
# Item-section finder
# -------------------------------------------------------------------- #


# Match "ITEM N." / "Item N." with optional letter suffix (1A, 7A, etc.).
_ITEM_HEADER_RE = re.compile(
    r"\bITEM\s+(?P<num>\d{1,2}[A-Z]?)\b\.?",
    re.IGNORECASE,
)

# Plain-text anchors for the Items we care about — when the per-section finder
# fails, we fall back to text-substring extraction from the full document.
_RISK_FACTORS_HEADER_RE = re.compile(r"\bRISK\s+FACTORS\b", re.I)
_PROPERTIES_HEADER_RE = re.compile(r"\bPROPERTIES\b", re.I)
_LEGAL_PROCEEDINGS_HEADER_RE = re.compile(r"\bLEGAL\s+PROCEEDINGS\b", re.I)
_INCORPORATED_BY_REFERENCE_RE = re.compile(
    r"\bincorporated\s+(?:herein\s+)?by\s+reference\b", re.I,
)


@dataclass
class ItemSection:
    """One Item section of a 10-K — bounded by an opening Item-N header and
    the next Item-N header (or end of document)."""

    item_num: str               # e.g. "1A", "2", "10"
    start_offset: int           # text-offset in the flattened document
    end_offset: int
    text: str                   # flattened plain text of the section


def _flatten_doc_text(doc) -> str:
    """Convert the lxml document to a flat plain-text string with single
    spaces between elements. Used for Item-anchor offset finding."""
    txt = doc.text_content() or ""
    txt = _NBSP_RE.sub(" ", txt)
    return txt


def _find_item_sections(
    doc, target_items: tuple[str, ...] = ("1A", "2", "3", "10", "11", "12"),
) -> dict[str, ItemSection]:
    """Locate Item sections of interest via flat-text Item-N anchor matching.

    Strategy:
      1. Flatten doc to text + collect all "ITEM N" header offsets.
      2. For each requested item, take the LAST occurrence (10-Ks have a TOC
         at the top with Item numbers + the actual Item header further down;
         the actual content is at the last occurrence).
      3. Section text spans from the chosen offset to the next Item-anchor
         (any item, not just the requested ones), or to end of doc.

    Returns a dict mapping ``item_num`` → ``ItemSection``. Missing items
    aren't in the dict.
    """
    text = _flatten_doc_text(doc)
    if not text:
        return {}

    # Collect every Item-N anchor with offset.
    anchors: list[tuple[int, str]] = []
    for m in _ITEM_HEADER_RE.finditer(text):
        anchors.append((m.start(), m.group("num").upper()))
    if not anchors:
        return {}

    # For each requested item, pick the LAST occurrence (skips TOC entry).
    chosen: dict[str, int] = {}
    for off, num in anchors:
        if num in target_items:
            chosen[num] = off

    # Sort all anchor offsets to find next-anchor boundary.
    sorted_offsets = sorted({off for off, _ in anchors})

    out: dict[str, ItemSection] = {}
    for item_num, start in chosen.items():
        next_offsets = [o for o in sorted_offsets if o > start]
        end = next_offsets[0] if next_offsets else len(text)
        out[item_num] = ItemSection(
            item_num=item_num,
            start_offset=start,
            end_offset=end,
            text=text[start:end],
        )
    return out


# -------------------------------------------------------------------- #
# Item 1A — Risk Factors
# -------------------------------------------------------------------- #


# A risk-factor heading usually appears in BOLD or ALL-CAPS at the start of
# its block. We scan the section text for paragraph-shaped chunks that begin
# with a candidate heading line.
_HEADING_LINE_RE = re.compile(
    r"(?P<heading>[^\n]{8,300}?)(?:\n+|\.\s)(?P<body>.{40,})",
    re.S,
)
# Risk-factor heading heuristic candidates — paragraphs whose first line is
# one of: ALL-CAPS-ish (>=70% upper), starts with a leading bullet/number,
# or ends with a period AND is short relative to the body.
_BULLET_RE = re.compile(r"^[\s\-•‣◦•·]+")


def _looks_like_risk_factor_heading(line: str) -> bool:
    line = line.strip()
    if len(line) < 8 or len(line) > 280:
        return False
    # Skip obvious non-headings.
    if line.lower().startswith(("we ", "our ", "the company ", "if ")):
        # These are typical risk-factor heading STARTS; let them through.
        return True
    if line.endswith("."):
        # Sentence-style headings ("Risks Related to Our Business.").
        return True
    # ALL-CAPS with a few mixed-case words is OK.
    letters = [c for c in line if c.isalpha()]
    if not letters:
        return False
    upper_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
    return upper_ratio > 0.55


def _extract_risk_factors_from_section(section_text: str) -> list[dict[str, Any]]:
    """Heuristic: split the section into paragraphs (double-newline boundaries
    or sentence breaks), then keep paragraphs whose first sentence /
    line looks like a risk-factor heading. Emit one row per heading +
    body block.

    Failure mode: zero-row return for filings where the section is dominated
    by intro boilerplate without bolded headings (common pre-2008).
    """
    out: list[dict[str, Any]] = []
    if not section_text or len(section_text) < 200:
        return out

    # Strip the leading "Item 1A. Risk Factors" header line.
    stripped = re.sub(r"^[^\n]{0,200}RISK\s+FACTORS[^\n]{0,80}", "",
                      section_text, count=1, flags=re.I)

    # Split into candidate paragraphs.
    paragraphs = re.split(r"\n\s*\n+|(?<=\.)\s{2,}", stripped)
    seq = 0
    for para in paragraphs:
        para = para.strip()
        if len(para) < 60 or len(para) > 8000:
            continue
        # First line / first sentence as candidate heading.
        first_break = re.search(r"(?:\n|(?<=\.)\s)", para)
        if first_break:
            heading = para[:first_break.start()].strip()
            body = para[first_break.end():].strip()
        else:
            # Single-line paragraph — treat first 200 chars as heading,
            # rest as body. Skip if too short.
            if len(para) < 120:
                continue
            heading = para[:200].rstrip()
            body = para[200:].strip()
        heading = _BULLET_RE.sub("", heading).strip()
        if not _looks_like_risk_factor_heading(heading):
            continue
        if not body or len(body) < 40:
            continue
        seq += 1
        out.append({
            "risk_factor_seq": seq,
            "risk_factor_heading": heading[:500],
            "risk_factor_text_raw": para[:8000],
        })
        # Cap per-filing risk-factor count to avoid pathological filings
        # blowing up the row count.
        if seq >= 200:
            break
    return out


# -------------------------------------------------------------------- #
# Item 2 — Properties
# -------------------------------------------------------------------- #


_PROPERTIES_TABLE_KEYS: dict[str, tuple[re.Pattern[str], ...]] = {
    "location": (
        re.compile(r"\bLOCATION\b"),
        re.compile(r"\bPROPERTY\b"),
        re.compile(r"\bFACILITY\b"),
        re.compile(r"\bSITE\b"),
        re.compile(r"\bADDRESS\b"),
    ),
    "size": (
        re.compile(r"\bSQUARE\s+FEET?\b"),
        re.compile(r"\bSQ\.?\s*FT\b"),
        re.compile(r"\bSIZE\b"),
        re.compile(r"\bAREA\b"),
        re.compile(r"\bACRES?\b"),
    ),
    "use": (
        re.compile(r"\bUSE\b"),
        re.compile(r"\bDESCRIPTION\b"),
        re.compile(r"\bPURPOSE\b"),
        re.compile(r"\bTYPE\b"),
        re.compile(r"\bFUNCTION\b"),
    ),
    "tenure": (
        re.compile(r"\bOWNED\b"),
        re.compile(r"\bLEASED\b"),
        re.compile(r"\bTENURE\b"),
        re.compile(r"\bSTATUS\b"),
        re.compile(r"\bOWNERSHIP\b"),
    ),
}


def _find_properties_tables(doc) -> list[tuple[Any, dict[str, int], int]]:
    """Find all tables that look like a Properties (Item-2) table."""
    hits: list[tuple[Any, dict[str, int], int]] = []
    for tbl in _table_iter(doc):
        rows = tbl.findall(".//tr")
        for hdr_idx, tr in enumerate(rows[:8]):
            cells_upper = [c.upper() for c in _row_cells_compact(tr)]
            if not cells_upper:
                continue
            cmap = _classify_header_row(
                cells_upper, _PROPERTIES_TABLE_KEYS, min_matches=2,
            )
            if cmap and "location" in cmap:
                hits.append((tbl, cmap, hdr_idx))
                break
    return hits


def _extract_properties_from_table(
    tbl, cmap: dict[str, int], hdr_idx: int,
) -> list[dict[str, Any]]:
    rows = tbl.findall(".//tr")[hdr_idx + 1:]
    out: list[dict[str, Any]] = []
    loc_idx = cmap.get("location")
    if loc_idx is None:
        return out
    seq_offset = 0
    for tr in rows:
        cells = _row_cells_compact(tr)
        if not cells or loc_idx >= len(cells):
            continue
        loc_cell = cells[loc_idx].strip()
        if not loc_cell or loc_cell.upper() in (
            "LOCATION", "PROPERTY", "TOTAL", "TOTALS",
            "FACILITY", "SITE", "ADDRESS",
        ):
            continue
        # Build full description by joining all non-empty cells.
        full_desc = " — ".join(c for c in cells if c)[:2000]
        parsed = parse_property_description(full_desc)
        seq_offset += 1
        out.append({
            "property_seq": seq_offset,
            "property_description_raw": full_desc,
            "property_city": parsed["city"],
            "property_state": parsed["state"],
            "property_country": parsed["country"],
            "property_size_sqft": parsed["size_sqft"],
            "property_owned_or_leased": parsed["owned_or_leased"],
            "property_use": parsed["use"],
        })
    return out


def _extract_properties_from_paragraphs(
    section_text: str,
) -> list[dict[str, Any]]:
    """Fallback: when no table is found, split the section into sentences and
    emit a row for each sentence that looks like a property disclosure.
    Gives at-least-1 coverage for narrative-style Item 2 sections.
    """
    out: list[dict[str, Any]] = []
    if not section_text:
        return out
    # Strip leading "Item 2. Properties" header.
    stripped = re.sub(r"^[^\n]{0,200}PROPERTIES[^\n]{0,80}", "",
                      section_text, count=1, flags=re.I)
    sentences = re.split(r"(?<=\.)\s+", stripped)
    seq = 0
    for sent in sentences:
        sent = sent.strip()
        if len(sent) < 30 or len(sent) > 800:
            continue
        # Property-like sentence heuristic: mentions square feet OR
        # owned/leased OR a city,state pair.
        if not (
            _SQFT_HINT.search(sent) or _OWNED_LEASED_HINT.search(sent)
            or _CITY_STATE_HINT.search(sent)
        ):
            continue
        parsed = parse_property_description(sent)
        seq += 1
        out.append({
            "property_seq": seq,
            "property_description_raw": sent[:2000],
            "property_city": parsed["city"],
            "property_state": parsed["state"],
            "property_country": parsed["country"],
            "property_size_sqft": parsed["size_sqft"],
            "property_owned_or_leased": parsed["owned_or_leased"],
            "property_use": parsed["use"],
        })
        if seq >= 100:
            break
    return out


_SQFT_HINT = re.compile(r"\bsquare\s+feet?\b|\bsq\.?\s*ft\b", re.I)
_OWNED_LEASED_HINT = re.compile(r"\b(owned|own|leased|lease)\b", re.I)
_CITY_STATE_HINT = re.compile(r"\b[A-Z][a-zA-Z\.\- ]{1,40}?,\s+[A-Z]{2}\b")


# -------------------------------------------------------------------- #
# Item 3 — Legal Proceedings
# -------------------------------------------------------------------- #


_CASE_CAPTION_RE = re.compile(
    r"\b([A-Z][A-Za-z\.\&\'\- ]{1,80}?)\s+v[\.s]?\s+([A-Z][A-Za-z\.\&\'\- ]{1,80}?)\b",
)
_COURT_RE = re.compile(
    r"\b(?:United States District Court|U\.S\. District Court|District Court|"
    r"Court of Appeals|Court of Chancery|Superior Court|Supreme Court|"
    r"Bankruptcy Court|Circuit Court)\s+(?:for\s+)?(?:the\s+)?"
    r"([A-Z][A-Za-z\.\- ]{1,60})",
    re.I,
)
_FILED_DATE_RE = re.compile(
    r"\b(?:filed|commenced|brought)\s+(?:on\s+)?"
    r"(?P<month>January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+(?P<day>\d{1,2}),?\s+"
    r"(?P<year>\d{4})",
    re.I,
)
_STATUS_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:settled|settlement\s+(?:was\s+)?reached)\b", re.I), "settled"),
    (re.compile(r"\bdismissed\b", re.I), "dismissed"),
    (re.compile(r"\b(?:pending|currently\s+pending)\b", re.I), "pending"),
)
_MONTH_NUM = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}


def _extract_legal_proceedings_from_section(
    section_text: str,
) -> list[dict[str, Any]]:
    """Extract one row per disclosed legal proceeding from the Item 3 section
    text. A "proceeding" is heuristically a paragraph that mentions:
      - ``v.`` (case caption) OR
      - "lawsuit" / "complaint" / "litigation" / "action" / "claim" / "suit"
        / "matter" / "proceeding" / "investigation"
    """
    out: list[dict[str, Any]] = []
    if not section_text or len(section_text) < 60:
        return out
    stripped = re.sub(r"^[^\n]{0,200}LEGAL\s+PROCEEDINGS[^\n]{0,80}", "",
                      section_text, count=1, flags=re.I)
    paragraphs = re.split(r"\n\s*\n+|(?<=\.)\s{2,}", stripped)
    seq = 0
    for para in paragraphs:
        para = para.strip()
        if len(para) < 40 or len(para) > 6000:
            continue
        # Filter to paragraphs with legal-action language.
        has_caption = bool(_CASE_CAPTION_RE.search(para))
        has_keyword = bool(re.search(
            r"\b(?:lawsuit|complaint|litigation|action|claim|suit|matter|"
            r"proceeding|investigation|petition|writ|injunction)\b",
            para, re.I,
        ))
        # Skip the common "we are not currently a party to any material legal
        # proceedings" boilerplate — it's a single short paragraph that ends
        # the section. Keep it as one row, but capture the verbatim text.
        if not (has_caption or has_keyword):
            continue
        # Caption
        cap_m = _CASE_CAPTION_RE.search(para)
        caption = cap_m.group(0).strip() if cap_m else None
        # Court
        court_m = _COURT_RE.search(para)
        court = court_m.group(0).strip() if court_m else None
        # Filed date
        filed_date: str | None = None
        d_m = _FILED_DATE_RE.search(para)
        if d_m:
            try:
                month = _MONTH_NUM[d_m.group("month").lower()]
                day = int(d_m.group("day"))
                year = int(d_m.group("year"))
                if 1900 < year < 2100 and 1 <= month <= 12 and 1 <= day <= 31:
                    filed_date = f"{year:04d}-{month:02d}-{day:02d}"
            except (KeyError, ValueError):
                pass
        # Status
        status: str | None = None
        for pat, label in _STATUS_PATTERNS:
            if pat.search(para):
                status = label
                break
        # Named individual defendants — heuristic: capture names that follow
        # "v." / "against" patterns. Non-empty only when distinguishable.
        named_persons: list[str] = []
        if cap_m:
            # Both sides of "X v. Y" — try splitting and stripping.
            for side in (cap_m.group(1), cap_m.group(2)):
                if not side:
                    continue
                tokens = side.split()
                if len(tokens) <= 4:
                    named_persons.append(side.strip())
        named = ";".join(named_persons) if named_persons else None
        seq += 1
        out.append({
            "proceeding_seq": seq,
            "proceeding_text_raw": para[:6000],
            "proceeding_caption": caption,
            "proceeding_jurisdiction": court,
            "proceeding_filed_date": filed_date,
            "proceeding_status": status,
            "proceeding_named_persons": named,
        })
        if seq >= 50:
            break
    return out


# -------------------------------------------------------------------- #
# Item 10 — Officers + Directors
# -------------------------------------------------------------------- #


_OFFICERS_DIRECTORS_KEYS: dict[str, tuple[re.Pattern[str], ...]] = {
    "name": (
        re.compile(r"\bNAME\b"),
        re.compile(r"\bDIRECTOR\b"),
    ),
    "age": (re.compile(r"\bAGE\b"),),
    "position": (
        re.compile(r"\bPOSITION\b"),
        re.compile(r"\bTITLE\b"),
        re.compile(r"\bOFFICE\b"),
        re.compile(r"\bPRINCIPAL\s+OCCUPATION\b"),
    ),
    "since": (
        re.compile(r"\bDIRECTOR\s+SINCE\b"),
        re.compile(r"\bSINCE\b"),
        re.compile(r"\bAPPOINTED\b"),
    ),
}


def _find_officers_directors_table(
    doc,
) -> list[tuple[Any, dict[str, int], int]]:
    hits: list[tuple[Any, dict[str, int], int]] = []
    for tbl in _table_iter(doc):
        rows = tbl.findall(".//tr")
        for hdr_idx, tr in enumerate(rows[:8]):
            cells_upper = [c.upper() for c in _row_cells_compact(tr)]
            if not cells_upper:
                continue
            cmap = _classify_header_row(
                cells_upper, _OFFICERS_DIRECTORS_KEYS, min_matches=2,
            )
            if cmap and "name" in cmap and "age" in cmap:
                hits.append((tbl, cmap, hdr_idx))
                break
    return hits


_OFFICER_TITLE_HINT = re.compile(
    r"\b(?:CEO|CFO|COO|CIO|CTO|President|Vice\s+President|Chairman|Chief|"
    r"Executive|Officer|Treasurer|Secretary|Controller|EVP|SVP|VP)\b",
    re.I,
)
_DIRECTOR_TITLE_HINT = re.compile(
    r"\b(?:Director|Board\s+Member)\b",
    re.I,
)


def _extract_officers_directors_from_table(
    tbl, cmap: dict[str, int], hdr_idx: int,
) -> list[dict[str, Any]]:
    rows = tbl.findall(".//tr")[hdr_idx + 1:]
    out: list[dict[str, Any]] = []
    name_idx = cmap["name"]
    age_idx = cmap.get("age")
    pos_idx = cmap.get("position")
    seq = 0
    for tr in rows:
        cells = _row_cells_compact(tr, preserve_lines=True)
        if not cells or name_idx >= len(cells):
            continue
        name_cell = cells[name_idx].strip()
        if not name_cell or name_cell.upper() in ("NAME", "TOTAL", "TOTALS"):
            continue
        # Skip rows where the "name" cell is purely numeric.
        if re.match(r"^[\d\s\-]+$", name_cell):
            continue
        # Multi-line name cell may carry "Name\nTitle" — split on first newline.
        if "\n" in name_cell:
            lines = [ln.strip() for ln in name_cell.splitlines() if ln.strip()]
            name_only = lines[0]
            title_inline = " ".join(lines[1:]) if len(lines) > 1 else None
        else:
            name_only = name_cell
            title_inline = None

        first, last = normalize_person_name(name_only)
        if not first or not last:
            continue

        age: int | None = None
        if age_idx is not None and age_idx < len(cells):
            age_cell = cells[age_idx].strip()
            m = re.match(r"^\s*(\d{2,3})\s*$", age_cell)
            if m:
                try:
                    age = int(m.group(1))
                    if not (18 <= age <= 110):
                        age = None
                except ValueError:
                    age = None

        title_raw: str | None = None
        if pos_idx is not None and pos_idx < len(cells):
            title_raw = cells[pos_idx].strip()
        if not title_raw and title_inline:
            title_raw = title_inline
        if title_raw:
            # Sometimes the position cell contains the entire bio paragraph.
            title_raw = title_raw[:500]

        # Role classifier.
        scope_text = (
            f"{title_raw or ''} {title_inline or ''} {name_cell}"
        )
        is_officer = bool(_OFFICER_TITLE_HINT.search(scope_text))
        is_director = bool(_DIRECTOR_TITLE_HINT.search(scope_text))
        if is_officer and is_director:
            role = "both"
        elif is_officer:
            role = "officer"
        elif is_director:
            role = "director"
        else:
            role = "officer"  # default in the Item-10 context

        seq += 1
        out.append({
            "person_seq": seq,
            "person_first_name": first.title() if first else None,
            "person_last_name": last.title() if last else None,
            "person_first_normalized": first,
            "person_last_normalized": last,
            "person_title_raw": title_raw,
            "person_title_normalized": normalize_title(title_raw),
            "person_age": age,
            "person_role_type": role,
            "is_independent_director": None,
            "biography_text_raw": None,
        })
        if seq >= 60:
            break
    return out


# -------------------------------------------------------------------- #
# Item 11 — Executive Compensation (Summary Compensation Table)
# Lifted from DEF 14A parser.
# -------------------------------------------------------------------- #


_COMP_HEADER_KEYS: dict[str, tuple[re.Pattern[str], ...]] = {
    "name": (
        re.compile(r"\bNAME\b"),
        re.compile(r"\bAND\s+PRINCIPAL\s+POSITION\b"),
    ),
    "year": (re.compile(r"\bYEAR\b"),),
    "salary": (re.compile(r"\bSALARY\b"),),
    "bonus": (re.compile(r"\bBONUS\b"),),
    "stock_awards": (re.compile(r"\bSTOCK\s+AWARDS?\b"),),
    "option_awards": (re.compile(r"\bOPTION\s+AWARDS?\b"),),
    "non_equity_incentive": (
        re.compile(r"\bNON[-\s]?EQUITY\s+INCENTIVE\b"),
        re.compile(r"\bINCENTIVE\s+PLAN\s+COMPENSATION\b"),
    ),
    "pension_change": (
        re.compile(r"\bCHANGE\s+IN\s+PENSION\b"),
        re.compile(r"\bPENSION\s+VALUE\b"),
        re.compile(r"\bNONQUALIFIED\s+DEFERRED\b"),
    ),
    "all_other": (re.compile(r"\bALL\s+OTHER\s+COMPENSATION\b"),),
    "total": (re.compile(r"\bTOTAL\b"),),
}


_NAME_TITLE_SPLIT_RE = re.compile(
    r"\s*(?=Chief|President|Vice|Senior|Executive|Director|Treasurer|Secretary|"
    r"Chairman|Founder|General|EVP|SVP|CEO|CFO|COO|CIO|CTO|"
    r"Managing|Principal|Head)",
    re.I,
)


def _split_name_title(raw: str) -> tuple[str, str | None]:
    raw = raw.strip()
    if not raw:
        return ("", None)
    if "\n" in raw:
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        return (lines[0], " ".join(lines[1:]) if len(lines) > 1 else None)
    parts = _NAME_TITLE_SPLIT_RE.split(raw, maxsplit=1)
    if len(parts) == 2 and parts[0].strip():
        return (parts[0].strip(), parts[1].strip())
    return (raw, None)


def _find_executive_compensation_table(
    doc,
) -> tuple[Any, dict[str, int], int] | None:
    for tbl in _table_iter(doc):
        rows = tbl.findall(".//tr")
        for hdr_idx, tr in enumerate(rows[:8]):
            cells_upper = [c.upper() for c in _row_cells_compact(tr)]
            if not cells_upper:
                continue
            cmap = _classify_header_row(cells_upper, _COMP_HEADER_KEYS)
            if cmap and "total" in cmap and "salary" in cmap:
                return tbl, cmap, hdr_idx
    return None


def _extract_executive_compensation_rows(
    tbl, cmap: dict[str, int], hdr_idx: int,
) -> list[dict[str, Any]]:
    rows = tbl.findall(".//tr")[hdr_idx + 1:]
    out: list[dict[str, Any]] = []
    current_name: str | None = None

    name_idx = cmap.get("name")
    year_idx = cmap.get("year")
    if name_idx is None or year_idx is None:
        return out
    expected_with_name = max(cmap.values()) + 1

    for tr in rows:
        cells = _row_cells_compact(tr, preserve_lines=True)
        if not cells:
            continue
        offset = 0
        if name_idx < len(cells):
            head_cell = cells[name_idx].strip()
            head_year = re.match(r"^\s*(\d{4})\s*$", head_cell)
            if head_year:
                offset = -1
        if offset == 0 and len(cells) >= expected_with_name:
            name_cell = cells[name_idx].strip()
            if name_cell:
                nm, _ttl = _split_name_title(name_cell)
                if nm:
                    current_name = nm
        eff_year_idx = year_idx + offset
        if eff_year_idx < 0 or eff_year_idx >= len(cells):
            continue
        year_cell = cells[eff_year_idx].strip()
        year_match = re.match(r"^(\d{4})", year_cell)
        if not year_match:
            continue
        comp_year = int(year_match.group(1))
        if not (1990 <= comp_year <= 2100):
            continue
        if not current_name:
            continue
        first, last = normalize_person_name(current_name)
        if not first or not last:
            continue

        def _g(key: str) -> float | None:
            idx = cmap.get(key)
            if idx is None:
                return None
            eff = idx + offset
            if eff < 0 or eff >= len(cells):
                return None
            return parse_dollar_amount(cells[eff])

        out.append({
            "person_first_normalized": first,
            "person_last_normalized": last,
            "compensation_year": comp_year,
            "comp_base_salary": _g("salary"),
            "comp_bonus": _g("bonus"),
            "comp_stock_awards": _g("stock_awards"),
            "comp_option_awards": _g("option_awards"),
            "comp_non_equity_incentive": _g("non_equity_incentive"),
            "comp_pension_change": _g("pension_change"),
            "comp_all_other": _g("all_other"),
            "comp_total": _g("total"),
        })
    return out


# -------------------------------------------------------------------- #
# Item 12 — Security Ownership (Beneficial Ownership Table)
# Lifted from DEF 14A parser, with holder-type classifier.
# -------------------------------------------------------------------- #


_BO_HEADER_KEYS: dict[str, tuple[re.Pattern[str], ...]] = {
    "name": (
        re.compile(r"\bNAME\s+AND\s+ADDRESS\b"),
        re.compile(r"\bNAME\s+OF\s+BENEFICIAL\b"),
        re.compile(r"\bBENEFICIAL\s+OWNER\b"),
        re.compile(r"^NAME$"),
    ),
    "shares": (
        re.compile(r"\bAMOUNT\s+AND\s+NATURE\b"),
        re.compile(r"\bSHARES\s+BENEFICIALLY\s+OWNED\b"),
        re.compile(r"\bNUMBER\s+OF\s+SHARES\b"),
        re.compile(r"^NUMBER$"),
    ),
    "percent": (
        re.compile(r"\bPERCENT\s+OF\s+CLASS\b"),
        re.compile(r"\bPERCENT\s+OWNERSHIP\b"),
        re.compile(r"\b%\s+OF\s+CLASS\b"),
        re.compile(r"^PERCENT$"),
        re.compile(r"^PERCENT\s+\(\s*%\s*\)$"),
    ),
}

_GROUP_ROW_HINTS = (
    "AS A GROUP",
    "OFFICERS AND DIRECTORS",
    "DIRECTORS AND OFFICERS",
    "DIRECTORS, EXECUTIVE OFFICERS",
    "DIRECTORS AND EXECUTIVE OFFICERS",
    "ALL DIRECTORS",
    "ALL EXECUTIVE OFFICERS",
)
_FIVE_PCT_HINTS = (
    " LLC", " LP", " L.P.", " INC", " INC.", " CORP", " CORPORATION",
    " COMPANY", " TRUST", " FUND", " FUNDS", " HOLDINGS", " GROUP",
    " PARTNERS", " CAPITAL", " MANAGEMENT", " ASSOCIATES", " ADVISORS",
    " ADVISERS", " SECURITIES", " INVESTMENTS", " ETF", "VANGUARD",
    "BLACKROCK", "STATE STREET", "FMR", "DIMENSIONAL",
)


def _classify_holder_type(holder_norm: str) -> str:
    if not holder_norm:
        return "officer_director"
    upper = holder_norm.upper()
    if any(h in upper for h in _GROUP_ROW_HINTS):
        return "all_officers_directors_group"
    if any(h in upper for h in _FIVE_PCT_HINTS):
        return "5%_holder"
    return "officer_director"


def _find_security_ownership_table(
    doc,
) -> tuple[Any, dict[str, int], int] | None:
    for tbl in _table_iter(doc):
        rows = tbl.findall(".//tr")
        for hdr_idx, tr in enumerate(rows[:8]):
            cells_upper = [c.upper() for c in _row_cells_compact(tr)]
            if not cells_upper:
                continue
            cmap = _classify_header_row(cells_upper, _BO_HEADER_KEYS)
            if cmap and "name" in cmap and ("shares" in cmap or "percent" in cmap):
                return tbl, cmap, hdr_idx
    return None


def _extract_security_ownership_rows(
    tbl, cmap: dict[str, int], hdr_idx: int,
) -> list[dict[str, Any]]:
    rows = tbl.findall(".//tr")[hdr_idx + 1:]
    out: list[dict[str, Any]] = []
    name_idx = cmap.get("name")
    shares_idx = cmap.get("shares")
    percent_idx = cmap.get("percent")
    if name_idx is None:
        return out

    for tr in rows:
        cells = _row_cells_compact(tr)
        if not cells or name_idx >= len(cells):
            continue
        name_cell = cells[name_idx].strip()
        if not name_cell or name_cell.upper() in ("NAME", "TOTAL", "TOTALS"):
            continue
        if re.match(r"^[\$\d\(\),\s\.\-—%]+$", name_cell):
            continue
        holder_norm = normalize_holder_name(name_cell)
        if not holder_norm:
            continue
        first, last = normalize_person_name(name_cell)
        holder_type = _classify_holder_type(holder_norm)
        shares = (
            parse_share_count(cells[shares_idx])
            if shares_idx is not None and shares_idx < len(cells) else None
        )
        percent = (
            parse_percent(cells[percent_idx])
            if percent_idx is not None and percent_idx < len(cells) else None
        )
        out.append({
            "holder_name_raw": name_cell,
            "holder_name_normalized": holder_norm,
            "holder_type": holder_type,
            "person_first_normalized": first if holder_type != "5%_holder" else None,
            "person_last_normalized": last if holder_type != "5%_holder" else None,
            "security_class": "Common Stock",
            "shares_beneficially_owned": shares,
            "percent_of_class": percent,
        })
    return out


# -------------------------------------------------------------------- #
# Top-level entry point
# -------------------------------------------------------------------- #


def _is_amendment(form_type: str) -> bool:
    return form_type.upper().endswith("/A")


def parse_filing(
    header: FilingHeader,
    primary_html: str | bytes,
) -> dict[str, list[dict[str, Any]]]:
    """Parse one Form 10-K filing → 7 structured streams.

    Always returns a non-empty ``filings`` list (1 row); secondary streams may
    be empty when the parser fails to find the canonical Item anchors / tables
    or when the filing incorporates Items 11/12 by reference.
    """
    cik = normalize_cik(header.cik_raw)
    accession = normalize_accession(header.accession_raw)
    filer_name = normalize_filer_name(header.filer_name_raw)

    form_10k_year = (
        int(header.filing_date[:4])
        if header.filing_date and len(header.filing_date) >= 4
        else None
    )

    is_amendment = _is_amendment(header.form_type)

    filing_meta = {
        "accession_number": accession,
        "cik_normalized": cik,
        "filer_legal_name_normalized": filer_name,
        "filer_legal_name_raw": header.filer_name_raw,
        "filer_lei_normalized": (
            header.filer_lei.upper() if header.filer_lei else None
        ),
        "filer_ein_normalized": (
            re.sub(r"\D", "", header.filer_ein) if header.filer_ein else None
        ),
        "form_type": header.form_type,
        "is_amendment": is_amendment,
        "original_accession_number": (
            normalize_accession(header.original_accession_number)
            if header.original_accession_number else None
        ),
        "filing_date": header.filing_date,
        "period_of_report": header.period_of_report,
        "fiscal_year_end": header.fiscal_year_end,
        "form_10k_year": form_10k_year,
        "primary_doc_url": header.primary_doc_url,
        "raw_html_r2_uri": header.raw_html_r2_uri,
    }

    out: dict[str, list[dict[str, Any]]] = {
        "filings": [filing_meta],
        "officers_directors": [],
        "executive_compensation": [],
        "security_ownership": [],
        "properties": [],
        "legal_proceedings": [],
        "risk_factors": [],
    }

    if not primary_html:
        return out

    try:
        if isinstance(primary_html, bytes):
            try:
                doc = lxml_html.fromstring(primary_html)
            except (ValueError, lxml_html.etree.ParserError):
                doc = lxml_html.fromstring(primary_html.decode("utf-8", "ignore"))
        else:
            doc = lxml_html.fromstring(primary_html)
    except (lxml_html.etree.ParserError, ValueError, lxml_html.etree.XMLSyntaxError) as exc:
        log.warning("parse: %s/%s html parse failed: %s", cik, accession, exc)
        return out

    sections = _find_item_sections(doc)

    common = {
        "accession_number": accession,
        "cik_normalized": cik,
        "form_10k_year": form_10k_year,
    }

    # 1. Item 1A — Risk Factors.
    rf_section = sections.get("1A")
    if rf_section is not None:
        for r in _extract_risk_factors_from_section(rf_section.text):
            out["risk_factors"].append({**common, **r})

    # 2. Item 2 — Properties. Try table first; fallback to paragraphs.
    prop_section = sections.get("2")
    table_hits = _find_properties_tables(doc)
    properties_added = 0
    if table_hits:
        for tbl, cmap, hdr_idx in table_hits[:4]:
            for r in _extract_properties_from_table(tbl, cmap, hdr_idx):
                out["properties"].append({**common, **r})
                properties_added += 1
    if properties_added == 0 and prop_section is not None:
        for r in _extract_properties_from_paragraphs(prop_section.text):
            out["properties"].append({**common, **r})

    # 3. Item 3 — Legal Proceedings.
    lp_section = sections.get("3")
    if lp_section is not None:
        for r in _extract_legal_proceedings_from_section(lp_section.text):
            out["legal_proceedings"].append({**common, **r})

    # 4. Item 10 — Officers + Directors.
    od_hits = _find_officers_directors_table(doc)
    for tbl, cmap, hdr_idx in od_hits[:2]:
        for r in _extract_officers_directors_from_table(tbl, cmap, hdr_idx):
            out["officers_directors"].append({**common, **r})

    # 5. Item 11 — Executive Compensation.
    item_11_section = sections.get("11")
    incorp_by_ref_11 = bool(
        item_11_section
        and _INCORPORATED_BY_REFERENCE_RE.search(item_11_section.text)
    )
    if not incorp_by_ref_11:
        comp_hit = _find_executive_compensation_table(doc)
        if comp_hit is not None:
            tbl, cmap, hdr_idx = comp_hit
            for r in _extract_executive_compensation_rows(tbl, cmap, hdr_idx):
                out["executive_compensation"].append({**common, **r})

    # 6. Item 12 — Security Ownership.
    item_12_section = sections.get("12")
    incorp_by_ref_12 = bool(
        item_12_section
        and _INCORPORATED_BY_REFERENCE_RE.search(item_12_section.text)
    )
    if not incorp_by_ref_12:
        bo_hit = _find_security_ownership_table(doc)
        if bo_hit is not None:
            tbl, cmap, hdr_idx = bo_hit
            for r in _extract_security_ownership_rows(tbl, cmap, hdr_idx):
                out["security_ownership"].append({**common, **r})

    return out

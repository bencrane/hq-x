"""SEC EDGAR DEF 14A parser — HTML → structured records.

DEF 14A filings vary substantially across filers and years. This parser
pursues a **best-effort, table-anchored** strategy: find the canonical
disclosure tables (Summary Compensation Table, Director Compensation Table,
Beneficial Ownership Table) by header-row pattern matching, then extract
rows. Filings that don't yield these tables still produce a filings-row;
secondary streams are populated only when a table is detected.

Parser output (per filing) — five lists of dicts:

    filings: [filing_meta]                   # always exactly 1 record
    executives: [exec_record, ...]           # NEOs from Summary Comp Table + officer text
    directors: [director_record, ...]        # from Director Comp Table + governance section
    compensation: [comp_record, ...]         # per (person, fiscal-year) row
    beneficial_ownership: [bo_record, ...]   # per (person, security class) row

Schema details: see directive
~/Desktop/hq/directives/2026-05-08-sec-edgar-def-14a-r2-ingest.md
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Iterator

from lxml import html as lxml_html

from _lib.sec_edgar_def_14a_normalize import (
    normalize_accession,
    normalize_cik,
    normalize_filer_name,
    normalize_person_name,
    normalize_title,
    parse_dollar_amount,
    parse_percent,
    parse_share_count,
)


log = logging.getLogger("sec-edgar-def-14a-parser")


# -------------------------------------------------------------------- #
# Header-row patterns
# -------------------------------------------------------------------- #

# Summary Compensation Table column patterns. Match against an upper-cased,
# whitespace-collapsed concatenation of the header cells.
_COMP_HEADER_KEYS: dict[str, tuple[re.Pattern[str], ...]] = {
    "name": (re.compile(r"\bNAME\b"), re.compile(r"\bAND\s+PRINCIPAL\s+POSITION\b")),
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

_DIRECTOR_COMP_HEADER_KEYS: dict[str, tuple[re.Pattern[str], ...]] = {
    "name": (re.compile(r"\bNAME\b"),),
    "fees_earned": (
        re.compile(r"\bFEES\s+EARNED\b"),
        re.compile(r"\bFEES\s+PAID\b"),
        re.compile(r"\bDIRECTORS?\s+FEES\b"),
    ),
    "stock_awards": (re.compile(r"\bSTOCK\s+AWARDS?\b"),),
    "option_awards": (re.compile(r"\bOPTION\s+AWARDS?\b"),),
    "all_other": (re.compile(r"\bALL\s+OTHER\b"),),
    "total": (re.compile(r"\bTOTAL\b"),),
}

# Beneficial-ownership column patterns. SEC convention: a "Name" col, an
# "Amount and Nature of Beneficial Ownership" (shares) col, and a "Percent
# of Class" col.
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

# Filings whose primary doc is a long-form HTML report; the table-of-contents
# anchor for the comp table varies. These are the strings we anchor on.
_COMP_TABLE_ANCHORS: tuple[re.Pattern[str], ...] = (
    re.compile(r"SUMMARY\s+COMPENSATION\s+TABLE", re.I),
    re.compile(r"COMPENSATION\s+OF\s+NAMED\s+EXECUTIVE\s+OFFICERS", re.I),
)


# -------------------------------------------------------------------- #
# Public types
# -------------------------------------------------------------------- #


@dataclass(frozen=True)
class FilingHeader:
    """Inputs available at discovery time (from form.idx + EDGAR header).
    The orchestrator populates this and hands it to ``parse_filing``."""

    cik_raw: str
    filer_name_raw: str
    accession_raw: str
    filing_date: str            # 'YYYY-MM-DD'
    primary_doc_url: str
    raw_html_r2_uri: str | None = None
    filer_lei: str | None = None
    filer_ein: str | None = None
    period_of_report: str | None = None


# -------------------------------------------------------------------- #
# Internal helpers
# -------------------------------------------------------------------- #


_NBSP_RE = re.compile(r"&nbsp;|\xa0")
_WS_INLINE_RE = re.compile(r"[ \t]+")
_WS_NL_RE = re.compile(r"\s*\n\s*")


def _cell_text(el, *, preserve_lines: bool = False) -> str:
    """Extract text from an lxml table-cell, optionally preserving <br>
    boundaries as newlines so callers can split a multi-line cell into
    name vs. title (DEF 14A common pattern)."""
    if el is None:
        return ""
    if preserve_lines:
        # Replace <br> tags with newlines before text_content().
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
        # Collapse intra-line whitespace but keep newlines.
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
    """Cell list with empty/spacer/dollar-sign-only cells removed.

    SEC DEF 14A tables routinely insert spacer ``<td>`` cells between
    data ``<td>`` cells, plus single-character ``$`` and currency-symbol
    cells. ``_row_cells_compact`` strips these so a logical column
    position can be matched against the header.
    """
    out: list[str] = []
    for c in tr.findall(".//td") + tr.findall(".//th"):
        txt = _cell_text(c, preserve_lines=preserve_lines)
        if not txt:
            continue
        # Strip cells that are ONLY a currency / asterisk marker
        if txt.strip() in ("$", "(", ")", "*", "%"):
            continue
        out.append(txt)
    return out


def _row_cells_joined(tr) -> str:
    return " | ".join(_row_cells(tr)).upper()


def _table_iter(doc) -> Iterator[Any]:
    """Yield every <table> in document order."""
    for t in doc.iter("table"):
        yield t


def _classify_header_row(
    row_cells_upper: list[str],
    keys: dict[str, tuple[re.Pattern[str], ...]],
) -> dict[str, int] | None:
    """Try to map header cells → canonical column names.

    Returns a dict of ``{canonical_name: column_index}`` if at least 3 of the
    ``keys`` map cleanly. Returns None if the row doesn't look like a
    Compensation Table header.
    """
    name_to_idx: dict[str, int] = {}
    for canonical, patterns in keys.items():
        for idx, cell in enumerate(row_cells_upper):
            if any(p.search(cell) for p in patterns):
                if canonical not in name_to_idx:
                    name_to_idx[canonical] = idx
                break
    return name_to_idx if len(name_to_idx) >= 3 else None


def _find_comp_table(doc) -> tuple[Any, dict[str, int], int] | None:
    """Locate the Summary Compensation Table.

    Returns ``(table_element, column_map, header_row_index)``. The column
    map is in *compact* coordinates — empty/spacer cells removed.
    """
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


def _find_director_comp_table(doc) -> tuple[Any, dict[str, int], int] | None:
    for tbl in _table_iter(doc):
        rows = tbl.findall(".//tr")
        for hdr_idx, tr in enumerate(rows[:8]):
            cells_upper = [c.upper() for c in _row_cells_compact(tr)]
            if not cells_upper:
                continue
            cmap = _classify_header_row(cells_upper, _DIRECTOR_COMP_HEADER_KEYS)
            if cmap and "fees_earned" in cmap and "total" in cmap:
                return tbl, cmap, hdr_idx
    return None


def _find_beneficial_ownership_table(doc) -> tuple[Any, dict[str, int], int] | None:
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


# -------------------------------------------------------------------- #
# Per-table extractors
# -------------------------------------------------------------------- #


_NAME_TITLE_SPLIT_RE = re.compile(
    r"\s*(?=Chief|President|Vice|Senior|Executive|Director|Treasurer|Secretary|"
    r"Chairman|Founder|General|EVP|SVP|CEO|CFO|COO|CIO|CTO|"
    r"Managing|Principal|Head)",
    re.I,
)


def _split_name_title(raw: str) -> tuple[str, str | None]:
    """Split a "Name<sep>Title" cell where ``<sep>`` may be ``\\n``,
    multiple spaces, or no whitespace at all (HTML rendering artifact).

    Strategy:
      1. If raw contains a newline, first line is name, rest is title.
      2. Else split on whitespace before the first title-keyword token
         (CEO/CFO/President/etc.). Token before the split is name,
         remainder is title.
      3. Fallback: whole string is the name.
    """
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


def _extract_comp_rows(
    tbl, cmap: dict[str, int], hdr_idx: int,
) -> list[dict[str, Any]]:
    """Yield Summary Compensation Table rows using compact-cell coordinates.

    Each NEO typically has 3 rows (3 years of comp). The first row may
    carry the name+title; subsequent rows have one fewer compact cell.
    """
    rows = tbl.findall(".//tr")[hdr_idx + 1:]
    out: list[dict[str, Any]] = []
    current_name: str | None = None
    current_title: str | None = None

    name_idx = cmap.get("name")
    year_idx = cmap.get("year")
    if name_idx is None or year_idx is None:
        return out
    # Header had ``name_idx`` and ``year_idx`` in compact coords. For data
    # rows that drop the name col, every other compact column shifts left
    # by one. Detect by row length vs. expected-with-name length.
    expected_with_name = max(cmap.values()) + 1

    for tr in rows:
        cells = _row_cells_compact(tr, preserve_lines=True)
        if not cells:
            continue
        # Determine offset: 0 if name present, -1 if name absent.
        # Heuristic: if the year is at name_idx (instead of year_idx) AND
        # cell at year_idx looks like a year, this row dropped the name.
        offset = 0
        if name_idx < len(cells):
            head_cell = cells[name_idx].strip()
            head_year = re.match(r"^\s*(\d{4})\s*$", head_cell)
            if head_year:
                # Name is missing; entire row is offset by -1 from the
                # name-included alignment.
                offset = -1
        if offset == 0 and len(cells) >= expected_with_name:
            # Name col present; pull name + title.
            name_cell = cells[name_idx].strip()
            if name_cell:
                nm, ttl = _split_name_title(name_cell)
                if nm:
                    current_name = nm
                    current_title = ttl
        # Resolve year cell.
        eff_year_idx = year_idx + offset
        if eff_year_idx < 0 or eff_year_idx >= len(cells):
            continue
        year_cell = cells[eff_year_idx].strip()
        year_match = re.match(r"^(\d{4})", year_cell)
        if not year_match:
            continue
        comp_year = int(year_match.group(1))
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
            "person_name_raw": current_name,
            "person_first_normalized": first,
            "person_last_normalized": last,
            "person_title_raw": current_title,
            "person_title_normalized": normalize_title(current_title),
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


def _extract_director_comp_rows(
    tbl, cmap: dict[str, int], hdr_idx: int,
) -> list[dict[str, Any]]:
    rows = tbl.findall(".//tr")[hdr_idx + 1:]
    out: list[dict[str, Any]] = []
    name_idx = cmap.get("name")
    if name_idx is None:
        return out

    for tr in rows:
        cells = _row_cells_compact(tr)
        if not cells or name_idx >= len(cells):
            continue
        name_cell = cells[name_idx].strip()
        if not name_cell or name_cell.upper() in ("NAME", "TOTAL", "TOTALS"):
            continue
        # Skip rows where the "name" cell is actually a number / dollar amount.
        if re.match(r"^[\$\d\(\),\s\.\-—]+$", name_cell):
            continue
        first, last = normalize_person_name(name_cell)
        if not first or not last:
            continue

        def _g(key: str) -> float | None:
            idx = cmap.get(key)
            if idx is None or idx >= len(cells):
                return None
            return parse_dollar_amount(cells[idx])

        out.append({
            "person_name_raw": name_cell,
            "person_first_normalized": first,
            "person_last_normalized": last,
            "comp_fees_earned": _g("fees_earned"),
            "comp_stock_awards": _g("stock_awards"),
            "comp_option_awards": _g("option_awards"),
            "comp_all_other": _g("all_other"),
            "comp_total": _g("total"),
        })
    return out


def _extract_beneficial_ownership_rows(
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
        if not name_cell or name_cell.upper() in (
            "NAME", "TOTAL", "TOTALS", "DIRECTORS AND EXECUTIVE OFFICERS",
            "DIRECTORS, EXECUTIVE OFFICERS AND 5% STOCKHOLDERS",
        ):
            continue
        if re.match(r"^[\$\d\(\),\s\.\-—%]+$", name_cell):
            continue
        first, last = normalize_person_name(name_cell)
        if not first or not last:
            continue
        shares = (
            parse_share_count(cells[shares_idx])
            if shares_idx is not None and shares_idx < len(cells) else None
        )
        percent = (
            parse_percent(cells[percent_idx])
            if percent_idx is not None and percent_idx < len(cells) else None
        )
        out.append({
            "person_name_raw": name_cell,
            "person_first_normalized": first,
            "person_last_normalized": last,
            "shares_beneficially_owned": shares,
            "percent_of_class": percent,
        })
    return out


# -------------------------------------------------------------------- #
# Top-level entry point
# -------------------------------------------------------------------- #


def parse_filing(
    header: FilingHeader,
    primary_html: str | bytes,
) -> dict[str, list[dict[str, Any]]]:
    """Parse one DEF 14A filing → structured records.

    Always returns a non-empty ``filings`` list (1 row). Secondary streams
    may be empty when the parser fails to find the canonical tables.
    """
    cik = normalize_cik(header.cik_raw)
    accession = normalize_accession(header.accession_raw)
    filer_name = normalize_filer_name(header.filer_name_raw)

    def_14a_year = (
        int(header.filing_date[:4])
        if header.filing_date and len(header.filing_date) >= 4
        else None
    )

    filing_meta = {
        "accession_number": accession,
        "cik_normalized": cik,
        "filer_legal_name_normalized": filer_name,
        "filer_lei_normalized": (
            header.filer_lei.upper() if header.filer_lei else None
        ),
        "filer_ein_normalized": (
            re.sub(r"\D", "", header.filer_ein) if header.filer_ein else None
        ),
        "filing_date": header.filing_date,
        "period_of_report": header.period_of_report,
        "def_14a_year": def_14a_year,
        "primary_doc_url": header.primary_doc_url,
        "raw_html_r2_uri": header.raw_html_r2_uri,
        "filer_legal_name_raw": header.filer_name_raw,
    }

    out: dict[str, list[dict[str, Any]]] = {
        "filings": [filing_meta],
        "executives": [],
        "directors": [],
        "compensation": [],
        "beneficial_ownership": [],
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

    # 1. Summary Compensation Table → executives + compensation streams.
    comp_hit = _find_comp_table(doc)
    if comp_hit is not None:
        tbl, cmap, hdr_idx = comp_hit
        rows = _extract_comp_rows(tbl, cmap, hdr_idx)
        # Dedup executives by (first, last); keep latest title.
        seen_neos: dict[tuple[str, str], dict[str, Any]] = {}
        for r in rows:
            key = (r["person_first_normalized"], r["person_last_normalized"])
            seen_neos[key] = {
                "accession_number": accession,
                "cik_normalized": cik,
                "person_name_raw": r["person_name_raw"],
                "person_first_normalized": r["person_first_normalized"],
                "person_last_normalized": r["person_last_normalized"],
                "person_title_raw": r["person_title_raw"],
                "person_title_normalized": r["person_title_normalized"],
                "is_named_executive_officer": True,
                "def_14a_year": def_14a_year,
            }
            out["compensation"].append({
                "accession_number": accession,
                "cik_normalized": cik,
                "person_first_normalized": r["person_first_normalized"],
                "person_last_normalized": r["person_last_normalized"],
                "compensation_year": r["compensation_year"],
                "comp_base_salary": r["comp_base_salary"],
                "comp_bonus": r["comp_bonus"],
                "comp_stock_awards": r["comp_stock_awards"],
                "comp_option_awards": r["comp_option_awards"],
                "comp_non_equity_incentive": r["comp_non_equity_incentive"],
                "comp_pension_change": r["comp_pension_change"],
                "comp_all_other": r["comp_all_other"],
                "comp_total": r["comp_total"],
                "def_14a_year": def_14a_year,
            })
        out["executives"].extend(seen_neos.values())

    # 2. Director Compensation Table → directors stream.
    dir_hit = _find_director_comp_table(doc)
    if dir_hit is not None:
        tbl, cmap, hdr_idx = dir_hit
        rows = _extract_director_comp_rows(tbl, cmap, hdr_idx)
        for r in rows:
            out["directors"].append({
                "accession_number": accession,
                "cik_normalized": cik,
                "person_name_raw": r["person_name_raw"],
                "person_first_normalized": r["person_first_normalized"],
                "person_last_normalized": r["person_last_normalized"],
                "is_independent_director": None,
                "committees_set": None,
                "comp_fees_earned": r["comp_fees_earned"],
                "comp_stock_awards": r["comp_stock_awards"],
                "comp_option_awards": r["comp_option_awards"],
                "comp_all_other": r["comp_all_other"],
                "comp_total": r["comp_total"],
                "def_14a_year": def_14a_year,
            })

    # 3. Beneficial Ownership Table → beneficial_ownership stream.
    bo_hit = _find_beneficial_ownership_table(doc)
    if bo_hit is not None:
        tbl, cmap, hdr_idx = bo_hit
        rows = _extract_beneficial_ownership_rows(tbl, cmap, hdr_idx)
        for r in rows:
            out["beneficial_ownership"].append({
                "accession_number": accession,
                "cik_normalized": cik,
                "person_name_raw": r["person_name_raw"],
                "person_first_normalized": r["person_first_normalized"],
                "person_last_normalized": r["person_last_normalized"],
                "shares_beneficially_owned": r["shares_beneficially_owned"],
                "percent_of_class": r["percent_of_class"],
                "def_14a_year": def_14a_year,
            })

    return out

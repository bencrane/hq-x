"""Shared parsing library for SEC IAPD Form ADV Part 2 brochures.

Imported by:
  - apps/data-engine-x/modal/sec_iapd_brochure_parse_app.py (Tier 1 + Tier 2 regex)
  - apps/data-engine-x/scripts/dispatch_sec_iapd_tier2_fallback.py (Tier 2 Opus fallback)
"""
from __future__ import annotations
import re
from typing import Any

__version__ = "1.1.0"

# The 18 SEC-mandated Items in Form ADV Part 2A. Names are canonical and
# match the column names in the downstream Lance dataset (snake_case, item_N_<slug>).
ITEM_TITLES: dict[int, str] = {
    1:  "cover_page",
    2:  "material_changes",
    3:  "table_of_contents",
    4:  "advisory_business",
    5:  "fees_and_compensation",
    6:  "performance_based_fees",
    7:  "types_of_clients",
    8:  "methods_of_analysis",
    9:  "disciplinary_information",
    10: "other_affiliations",
    11: "code_of_ethics",
    12: "brokerage_practices",
    13: "review_of_accounts",
    14: "client_referrals",
    15: "custody",
    16: "discretion",
    17: "voting_securities",
    18: "financial_information",
}

# SEC mandates the header form "Item N" at the start of each section.
# Tolerant variants: leading whitespace, optional period/colon, case-insensitive.
# (?!\.[A-Za-z0-9]) rejects sub-question refs ("Item 5.A.") — not section headers.
ITEM_HEADER_RE = re.compile(
    r"^\s*Item\s+(\d{1,2})\b(?!\.[A-Za-z0-9])[\.\:]?\s*",
    re.IGNORECASE | re.MULTILINE,
)

# A run of dot-leaders (ASCII periods or the unicode ellipsis) is the
# unambiguous signature of a Table-of-Contents line ("<title> ..... <page>").
_DOT_LEADER_RE = re.compile(r"[.…]{4,}")


def _looks_like_toc_entry(body: str) -> bool:
    """True if a captured body's first line is a dotted Table-of-Contents line.

    Form ADV brochures repeat every "Item N" heading inside the Item 3 Table
    of Contents. A dotted TOC block can run long (many indented sub-entries),
    so it must be dropped explicitly rather than out-competed on body length.
    """
    return bool(_DOT_LEADER_RE.search(body.lstrip().split("\n", 1)[0]))


def extract_text_and_tables(pdf_path: str) -> tuple[str, list[dict[str, Any]]]:
    """Tier 1: per-page text + table extraction via pdfplumber.

    Returns (full_text_md, tables_metadata) where:
      - full_text_md is per-page text joined with form-feed (\\x0c) separator
      - tables_metadata is a list of {page_num: int, rows: list[list[str]]} dicts
    """
    import pdfplumber
    pages_text: list[str] = []
    tables: list[dict[str, Any]] = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            pages_text.append(page.extract_text() or "")
            for tbl in (page.extract_tables() or []):
                tables.append({"page_num": i, "rows": tbl})
    return ("\x0c".join(pages_text), tables)


def split_sections_regex(full_text: str) -> dict[int, str]:
    """Tier 2 main pass: regex split on Items 1-18 with SEC-mandated anchoring.

    Returns a dict {1: <item_1_text>, 2: <item_2_text>, ...} with only the items
    that were detected. Missing items are NOT present in the dict (callers
    test `len(items_dict)` or `items_dict.get(N)` to interrogate).

    For each Item N the LONGEST non-TOC body wins: the same "Item N" string
    recurs in the Table of Contents, in repeated running page headers, and as
    in-text cross-references — but the real section body dwarfs all of them.
    """
    # pdfplumber joins pages with form-feed (\x0c); normalize to \n so the
    # MULTILINE ^ anchor matches section headers that begin a new page.
    full_text = full_text.replace("\x0c", "\n")
    matches = list(ITEM_HEADER_RE.finditer(full_text))
    items: dict[int, str] = {}
    for i, m in enumerate(matches):
        n = int(m.group(1))
        if not (1 <= n <= 18):
            continue
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(full_text)
        body = full_text[start:end].strip()
        if _looks_like_toc_entry(body):
            continue
        if n not in items or len(body) > len(items[n]):
            items[n] = body
    return items


def score_section_confidence(item_dict: dict[int, str]) -> int:
    """Count of Items 1-18 with non-empty content. Range: 0-18."""
    return sum(1 for n in range(1, 19) if item_dict.get(n))

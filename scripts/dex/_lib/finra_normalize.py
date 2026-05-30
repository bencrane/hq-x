"""Pure-functional normalizers for FINRA BrokerCheck R2 ingest.

Produces the join keys that downstream identity-resolution MVs use to
bridge FINRA firms + individuals against FEC, SBA, IRS BMF, SEC ADV. Keep
these pure (no I/O), deterministic, and unit-tested — the ingest re-runs
cheaply if the rules change.

Per the directive: normalization happens at INGEST time so the Parquet
carries both raw and normalized columns. RW joins on the normalized
columns; the raw columns stay as ground-truth.

Future LLM-assisted canonicalization (firm-name disambiguation, alias
clustering) belongs in a downstream MV that consumes the raw columns —
NOT here.
"""

from __future__ import annotations

import re
from typing import Any, Final


_WHITESPACE_RUN: Final = re.compile(r"\s+")
_NAME_PUNCT: Final = re.compile(r"[.,'\-]")
_FIRM_PUNCT: Final = re.compile(r"[.,&]")
_NON_DIGIT: Final = re.compile(r"\D")

# Trailing tokens stripped from a firm name. Industry-specific suffixes
# (securities, capital, brokerage, financial, advisors) are listed per the
# directive — broker-dealer / RIA names commonly carry them. Strip
# iteratively from the right so "ACME SECURITIES LLC" → "acme".
_FIRM_SUFFIXES: Final = frozenset({
    "llc", "inc", "incorporated", "corp", "corporation",
    "co", "company", "ltd", "limited",
    "lp", "llp", "pa", "pc", "pllc",
    "securities", "capital", "brokerage", "financial", "advisors",
})


def normalize_firm_name(raw: str | None) -> str | None:
    """Normalize a FINRA firm name for cross-source identity joining.

    Steps:
      1. Lowercase + trim.
      2. Replace ".", ",", "&" with spaces.
      3. Collapse whitespace.
      4. Iteratively strip trailing tokens listed in `_FIRM_SUFFIXES`
         (so "ACME SECURITIES LLC" → "acme").

    "ACME, INC."                 → "acme"
    "Acme Securities, LLC"       → "acme"
    "Smith Barney & Co."         → "smith barney"
    "LPL FINANCIAL LLC"          → "lpl"
    "Morgan Stanley"             → "morgan stanley"
    None / empty                 → None
    """
    if raw is None:
        return None
    s = raw.strip().lower()
    if not s:
        return None
    s = _FIRM_PUNCT.sub(" ", s)
    s = _WHITESPACE_RUN.sub(" ", s).strip()
    if not s:
        return None
    parts = s.split(" ")
    while len(parts) >= 2 and parts[-1] in _FIRM_SUFFIXES:
        parts = parts[:-1]
    s = " ".join(parts).strip()
    return s or None


def normalize_person_name_part(raw: str | None) -> str | None:
    """Normalize an individual's first or last name component.

    Steps:
      1. Lowercase + trim.
      2. Replace ".", ",", "'", "-" with spaces.
      3. Collapse whitespace.

    "Smith"            → "smith"
    "O'BRIEN"          → "o brien"
    "ANNE-MARIE"       → "anne marie"
    "  John  "         → "john"
    None / empty       → None
    """
    if raw is None:
        return None
    s = raw.strip().lower()
    if not s:
        return None
    s = _NAME_PUNCT.sub(" ", s)
    s = _WHITESPACE_RUN.sub(" ", s).strip()
    return s or None


def zip5(raw: Any) -> str | None:
    """Extract the 5-digit ZIP from a postal code string.

    "12345"        → "12345"
    "12345-6789"   → "12345"
    "07940"        → "07940"
    "1234"         → None    (too short)
    "abcde"        → None    (no digits)
    None / empty   → None
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    digits = _NON_DIGIT.sub("", s)
    if len(digits) < 5:
        return None
    return digits[:5]


def normalize_state(raw: Any) -> str | None:
    """Normalize a US state code to uppercase 2-letter, or None if invalid.

    "nj"      → "NJ"
    "  ny "   → "NY"
    "Calif"   → None    (not 2 letters)
    "12"      → None    (not alpha)
    None      → None
    """
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if len(s) != 2 or not s.isalpha():
        return None
    return s


def extract_current_employer_crd(current_employments: Any) -> int | None:
    """Pull the firmId of the first current employment, if any.

    The detail endpoint returns currentEmployments as a list of dicts; the
    first entry is the primary current employment. We accept both
    "firmId" (detail JSON) and "firm_id" (search JSON) key spellings.
    """
    if not isinstance(current_employments, list) or not current_employments:
        return None
    first = current_employments[0]
    if not isinstance(first, dict):
        return None
    fid = first.get("firmId")
    if fid is None:
        fid = first.get("firm_id")
    if fid is None or fid == "":
        return None
    try:
        return int(fid)
    except (TypeError, ValueError):
        return None


def extract_branch_zip_state(current_employments: Any) -> tuple[str | None, str | None]:
    """Return (zipCode, state) from the first current employment's first
    branchOfficeLocations entry, if available. Falls back to the flat
    branch_zip / branch_state hints (search-side denormalization) when the
    detail nested structure isn't present.

    The detail endpoint nests address under
    `currentEmployments[0].branchOfficeLocations[0]`; the search endpoint
    flattens it onto `ind_current_employments[0].branch_zip` etc.
    """
    if not isinstance(current_employments, list) or not current_employments:
        return (None, None)
    first = current_employments[0]
    if not isinstance(first, dict):
        return (None, None)
    locs = first.get("branchOfficeLocations")
    if isinstance(locs, list) and locs:
        loc = locs[0]
        if isinstance(loc, dict):
            return (loc.get("zipCode"), loc.get("state"))
    flat_zip = first.get("branch_zip")
    flat_state = first.get("branch_state")
    return (flat_zip, flat_state)

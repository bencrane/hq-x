"""Pure-functional normalizers for USDA Rural Development R2 ingest.

Produces the join keys downstream identity-resolution MVs use to bridge
USDA RD lenders + borrowers against FDIC banks, NCUA credit unions, FFIEC
Panel, GLEIF, SBA borrowers, and USAspending recipients. Keep these pure
(no I/O), deterministic, and unit-tested — the ingest re-runs cheaply if
the rules change.

Per the directive: normalization happens at INGEST time so the Parquet
carries both raw and normalized columns. RW joins on the normalized
columns; the raw columns stay as ground-truth.
"""

from __future__ import annotations

import re
from typing import Final


_WHITESPACE_RUN: Final = re.compile(r"\s+")
_NAME_PUNCT: Final = re.compile(r"[.,&]")
_NON_DIGIT: Final = re.compile(r"\D")

# Common org suffixes stripped from the END of a lender/borrower string
# after punctuation normalization. Iteratively applied — so
# "ACME HOLDINGS LLC, INC" → "acme holdings".
_ORG_SUFFIXES: Final = frozenset({
    "llc",
    "inc",
    "incorporated",
    "corp",
    "corporation",
    "co",
    "company",
    "ltd",
    "limited",
    "lp",
    "llp",
    "pa",
    "pc",
    "pllc",
    "na",  # National Association — common for federally-chartered banks
})


def _normalize_org_name(raw: str | None) -> str | None:
    """Internal: lowercase + corp-suffix-strip used by lender + borrower.

    Two-token "n a" (from "N.A." punctuation-split) is coalesced to "na"
    before the iterative single-token strip — so "First National Bank,
    N.A." → "first national bank" rather than "first national bank n a".
    """
    if raw is None:
        return None
    s = raw.strip().lower()
    if not s:
        return None
    s = _NAME_PUNCT.sub(" ", s)
    s = _WHITESPACE_RUN.sub(" ", s).strip()
    if not s:
        return None
    parts = s.split(" ")
    if len(parts) >= 3 and parts[-1] == "a" and parts[-2] == "n":
        parts = parts[:-2] + ["na"]
    while len(parts) >= 2 and parts[-1] in _ORG_SUFFIXES:
        parts = parts[:-1]
    s = " ".join(parts).strip()
    return s or None


def normalize_lender_name(raw: str | None) -> str | None:
    """Normalize a USDA RD lender name for cross-source identity joining.

    Steps:
      1. Lowercase + trim.
      2. Replace ".", ",", "&" with spaces.
      3. Collapse whitespace.
      4. Iteratively strip trailing org-suffix tokens (LLC, INC, NA, ...).

    "FIRST NATIONAL BANK, N.A."   → "first national bank"
    "Acme Holdings, LLC"          → "acme holdings"
    "JPMorgan Chase Bank, N.A."   → "jpmorgan chase bank"
    "Bank of America Corp."       → "bank of america"
    None / empty                  → None
    """
    return _normalize_org_name(raw)


def normalize_borrower_name(raw: str | None) -> str | None:
    """Normalize a USDA RD borrower name. Same rules as lender."""
    return _normalize_org_name(raw)


def normalize_county_fips(raw: str | None) -> str | None:
    """Coerce a county FIPS to a 5-digit zero-padded string.

    Some USDA RD source rows publish FIPS as integers (leading zeros
    stripped) — "1003" instead of "01003" for Baldwin County, AL. This
    normalizer left-pads to 5 digits when 4 numeric chars are present, and
    returns NULL when fewer than 4 or more than 5 numeric chars exist.

    "01003"   → "01003"
    "1003"    → "01003"   (zero-padded)
    "12345"   → "12345"
    "123"     → None      (too short)
    "abc"     → None      (no digits)
    None      → None
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    digits = _NON_DIGIT.sub("", s)
    if not digits or len(digits) > 5 or len(digits) < 4:
        return None
    return digits.zfill(5)


def normalize_state(raw: str | None) -> str | None:
    """Normalize a US state name OR 2-letter code to uppercase 2-letter
    abbreviation, or None if invalid. USDA RD publishes state as full name
    ("ALABAMA") in some streams and as 2-letter ("AL") in others.

    "AL"           → "AL"
    "alabama"      → "AL"
    "  ALABAMA "   → "AL"
    "12"           → None
    None           → None
    """
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if not s:
        return None
    if len(s) == 2 and s.isalpha():
        return s
    abbr = _US_STATES.get(s)
    return abbr


def normalize_program(raw: str | None) -> str | None:
    """Normalize a USDA RD program name to UPPERCASE + whitespace-collapsed.

    "Business and Industry Guaranteed Loan"   → "BUSINESS AND INDUSTRY GUARANTEED LOAN"
    "  Single Family Housing Direct  "         → "SINGLE FAMILY HOUSING DIRECT"
    None / empty                               → None
    """
    if raw is None:
        return None
    s = raw.strip().upper()
    if not s:
        return None
    s = _WHITESPACE_RUN.sub(" ", s).strip()
    return s or None


def naics_2digit(raw: str | None) -> str | None:
    """Extract the 2-digit NAICS sector code from a NAICS code string.

    USDA RD's `naics_industry_sector_code` is typically a 2-digit code
    already ("31-33", "11", "62") but defensive: take the first 2
    numeric chars. Returns None for non-numeric or too-short input.

    "11"          → "11"
    "1133"        → "11"
    "31-33"       → "31"
    "abc"         → None
    None          → None
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    digits = _NON_DIGIT.sub("", s)
    if len(digits) < 2:
        return None
    return digits[:2]


def zip5(raw: str | None) -> str | None:
    """Extract the 5-digit ZIP from a ZIP code string.

    "12345"        → "12345"
    "12345-6789"   → "12345"
    "123456789"    → "12345"
    "1234"         → None    (too short)
    None           → None
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


# State name → 2-letter code lookup. Includes DC + US territories that
# USDA RD reports on (PR, VI, GU, AS, MP, FM, MH, PW).
_US_STATES: Final[dict[str, str]] = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
    "CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT",
    "DELAWARE": "DE", "DISTRICT OF COLUMBIA": "DC", "FLORIDA": "FL",
    "GEORGIA": "GA", "HAWAII": "HI", "IDAHO": "ID", "ILLINOIS": "IL",
    "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS", "KENTUCKY": "KY",
    "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD",
    "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN",
    "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT",
    "NEBRASKA": "NE", "NEVADA": "NV", "NEW HAMPSHIRE": "NH",
    "NEW JERSEY": "NJ", "NEW MEXICO": "NM", "NEW YORK": "NY",
    "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH",
    "OKLAHOMA": "OK", "OREGON": "OR", "PENNSYLVANIA": "PA",
    "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD",
    "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT", "VERMONT": "VT",
    "VIRGINIA": "VA", "WASHINGTON": "WA", "WEST VIRGINIA": "WV",
    "WISCONSIN": "WI", "WYOMING": "WY",
    "PUERTO RICO": "PR", "VIRGIN ISLANDS": "VI", "GUAM": "GU",
    "AMERICAN SAMOA": "AS", "NORTHERN MARIANA ISLANDS": "MP",
    "FEDERATED STATES OF MICRONESIA": "FM", "MARSHALL ISLANDS": "MH",
    "PALAU": "PW",
}

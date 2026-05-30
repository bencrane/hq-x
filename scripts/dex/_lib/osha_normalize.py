"""Pure-functional normalizers for OSHA Inspection Data R2 ingest.

Source is the DOL Open Data Portal (apiprod.dol.gov/v4) — replacement for the
deprecated enforcedata.dol.gov bulk archive. OSHA enforcement records ship
through 4 streams whose grain is tied to inspection events:

  inspection      one row per OSHA inspection event
  violation       one row per citation (FK activity_nr → inspection)
  accident_injury one row per injured person (FK activity_nr → inspection)
  establishments  derived: DISTINCT projection over inspection's site_*
                  fields. No separate endpoint on the new portal.

These normalizers produce identity-spine columns for downstream MVs to bridge
inspected employers against:

  - USAspending construction recipients (NAICS=23 filter)
  - SBA borrowers (PPP + EIDL)
  - USPTO trademark filers
  - IRS BMF
  - FMCSA carriers
  - GLEIF

Construction (NAICS=23) is high-priority for OSHA enforcement (~15-25% of all
inspections). The `is_construction_naics` flag accelerates the construction-
quality-signal MV downstream.

Per-source contract:
  - All raw API columns preserved verbatim as VARCHAR.
  - Numeric / date / boolean casts surface alongside as typed columns.
  - Normalized identity-spine columns join keys for cross-source MVs.

Future LLM-assisted canonicalization (alias clustering, DBA disambiguation)
belongs in a downstream MV that consumes these columns — NOT here.
"""

from __future__ import annotations

import re
from typing import Any, Final


_WHITESPACE_RUN: Final = re.compile(r"\s+")
_NON_DIGIT: Final = re.compile(r"\D")
_NAME_PUNCT: Final = re.compile(r"[^\w\s]+")

# Corp-form suffixes stripped from establishment legal names. Same set as
# the EIDL / SBA normalizers so cross-source identity joins are clean
# equality matches on `establishment_name_normalized`.
_ESTAB_SUFFIX_TOKENS: Final = (
    "incorporated", "corporation", "company", "limited",
    "pllc", "llp", "lp", "llc", "inc", "ltd", "corp", "co", "pa",
    "holdings", "group", "associates",
)
_ESTAB_SUFFIX_RE: Final = re.compile(
    r"\b(?:" + "|".join(re.escape(t) for t in _ESTAB_SUFFIX_TOKENS) + r")\b\.?",
    re.IGNORECASE,
)

# OSHA `insp_type` single-letter codes → directive's 6-bin enum.
# Source: DOL OIS data dictionary; codes have been stable since the
# IMIS-era schema and the new ODP carries them forward.
#   A = Accident          → 'ACCIDENT'
#   B = Complaint         → 'COMPLAINT'
#   C = Referral          → 'REFERRAL'
#   D = Monitoring        → 'OTHER'   (compliance monitoring; rare)
#   E = Variance          → 'OTHER'
#   F = Follow-up         → 'FOLLOW_UP'
#   G = Unprogrammed Othr → 'OTHER'
#   H = Planned           → 'PROGRAMMED'
#   I = Programmed Other  → 'PROGRAMMED'
#   J = Programmed Related→ 'PROGRAMMED'
#   K = Unprogrammed Rel'd→ 'OTHER'
#   L = Unprog. Other Rel.→ 'OTHER'
#   M = Fat/Cat           → 'ACCIDENT'  (fatality/catastrophe — accident-class)
#   N = Programmed Insp.  → 'PROGRAMMED'
#   O = Unprog. Other     → 'OTHER'
_INSP_TYPE_TO_BUCKET: Final = {
    "A": "ACCIDENT",
    "B": "COMPLAINT",
    "C": "REFERRAL",
    "D": "OTHER",
    "E": "OTHER",
    "F": "FOLLOW_UP",
    "G": "OTHER",
    "H": "PROGRAMMED",
    "I": "PROGRAMMED",
    "J": "PROGRAMMED",
    "K": "OTHER",
    "L": "OTHER",
    "M": "ACCIDENT",
    "N": "PROGRAMMED",
    "O": "OTHER",
}

# OSHA violation-severity codes → directive's 5-bin enum.
#   S = Serious           → 'SERIOUS'
#   W = Willful           → 'WILLFUL'
#   R = Repeat            → 'REPEAT'
#   O = Other-than-Serious → 'OTHER'
#   U = Unclassified      → 'UNCLASSIFIED'
# Some legacy data uses lowercase or full-word codes ('serious', 'willful')
# — handle case-insensitively and accept both single-letter and full-name.
_VIOL_SEVERITY_TO_BUCKET: Final = {
    "S": "SERIOUS",
    "SERIOUS": "SERIOUS",
    "W": "WILLFUL",
    "WILLFUL": "WILLFUL",
    "R": "REPEAT",
    "REPEAT": "REPEAT",
    "O": "OTHER",
    "OTHER": "OTHER",
    "OTHER-THAN-SERIOUS": "OTHER",
    "U": "UNCLASSIFIED",
    "UNCLASSIFIED": "UNCLASSIFIED",
}

# OSHA `degree_of_inj` codes → directive's 4-bin outcome enum.
#   1 = Fatality
#   2 = Hospitalized injury
#   3 = Non-hospitalized injury (medical treatment beyond first aid)
#   4 = Other or no specific injury
# Some sources use the descriptive form. Handle both numeric and text.
_OUTCOME_TO_BUCKET: Final = {
    "1": "FATALITY",
    "FATALITY": "FATALITY",
    "FATAL": "FATALITY",
    "DEATH": "FATALITY",
    "2": "HOSPITALIZATION",
    "HOSPITALIZATION": "HOSPITALIZATION",
    "HOSPITALIZED": "HOSPITALIZATION",
    "3": "INJURY",
    "INJURY": "INJURY",
    "NONHOSPITALIZED": "INJURY",
    "NON-HOSPITALIZED": "INJURY",
    "4": "OTHER",
    "OTHER": "OTHER",
}


def normalize_establishment_name(raw: str | None) -> str | None:
    """Normalize an OSHA `estab_name` for cross-source identity joins.

    Same shape as EIDL borrower-name and PECOS org-name normalizers so
    cross-source MV joins on `establishment_name_normalized` are clean
    equality matches.

    Steps:
      1. Lowercase + trim.
      2. Strip corp-form suffixes (LLC, Inc, Corp, ...) wherever they appear.
      3. Replace non-word punctuation with spaces.
      4. Collapse whitespace.

    "CHRISTMAS TREE SHOPS"               → "christmas tree shops"
    "ACME, INC."                         → "acme"
    "Smith & Associates LLC"             → "smith"
    "U.S. STEEL CORPORATION"             → "u s steel"
    "John's Carpentry, LP"               → "john s carpentry"
    None / empty                         → None
    """
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if not s:
        return None
    s = _ESTAB_SUFFIX_RE.sub(" ", s)
    s = _NAME_PUNCT.sub(" ", s)
    s = _WHITESPACE_RUN.sub(" ", s).strip()
    return s or None


def normalize_naics_code(raw: Any) -> str | None:
    """Normalize a NAICS code to 6-digit zero-padded form, or None.

    OSHA's `naics_code` field is typically already 6-digit numeric, but:
      - Pre-2003 inspections have NAICS empty (SIC-only era; check
        `legacy_sic_code` separately).
      - Some rows ship "000000" as a sentinel for "unspecified" — treat
        as None.
      - Some rows ship 4- or 5-digit NAICS (older / legacy reporters).

    "452990"   → "452990"
    "23"       → "230000"   (left-pad to 6, treat 2-digit as sector-only)
    "2362"     → "236200"   (left-pad)
    "000000"   → None       (sentinel for "unspecified")
    "0"        → None       (sentinel)
    None       → None
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    digits = _NON_DIGIT.sub("", s)
    if not digits:
        return None
    # Drop sentinel values
    if int(digits) == 0:
        return None
    if len(digits) > 6:
        return None
    return digits.ljust(6, "0")


def naics_2digit(raw: Any) -> str | None:
    """Extract the 2-digit NAICS sector code (the segmentation primary).

    "452990"   → "45"   (Retail Trade)
    "236220"   → "23"   (Construction)
    "11"       → "11"   (Agriculture)
    "0"        → None
    None       → None
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    digits = _NON_DIGIT.sub("", s)
    if not digits or int(digits) == 0:
        return None
    if len(digits) < 2:
        return None
    return digits[:2]


def is_construction_naics(raw: Any) -> bool:
    """Predicate: does this NAICS code fall under sector 23 (Construction)?

    Construction is one of OSHA's primary enforcement targets — expect
    ~15-25% of all inspections to be NAICS=23. The flag is the basis for
    the downstream construction-quality-signal MV that joins to
    USAspending federal-construction recipients.

    "236220"   → True
    "237310"   → True   (heavy & civil engineering construction)
    "452990"   → False  (retail)
    "0"        → False
    None       → False
    """
    sector = naics_2digit(raw)
    return sector == "23"


def zip5(raw: Any) -> str | None:
    """Extract the 5-digit ZIP from a postal code string.

    "12345"        → "12345"
    "12345-6789"   → "12345"
    "12345 6789"   → "12345"
    "1234"         → None   (too short)
    "abcde"        → None   (no digits)
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
    """Normalize a US state code to uppercase 2-letter, or None.

    OSHA reports cover US states + territories (PR, VI, GU, AS, MP).
    Anything else is invalid.

    "ca"      → "CA"
    "  ny "   → "NY"
    "PR"      → "PR"
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


def classify_inspection_type(raw: Any) -> str | None:
    """Classify OSHA `insp_type` code into the directive's 6-bin enum.

    Returns one of:
      'PROGRAMMED' | 'COMPLAINT' | 'ACCIDENT' | 'REFERRAL'
      | 'FOLLOW_UP' | 'OTHER'
    or None if the input is missing/empty (so downstream WHERE-clauses
    on `inspection_type_normalized IS NOT NULL` work).

    "A"   → 'ACCIDENT'
    "B"   → 'COMPLAINT'
    "F"   → 'FOLLOW_UP'
    "H"   → 'PROGRAMMED'
    "M"   → 'ACCIDENT'   (fatality/catastrophe)
    "z"   → 'OTHER'      (unknown letter — bucket as OTHER, not None)
    None  → None
    ""    → None
    """
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if not s:
        return None
    return _INSP_TYPE_TO_BUCKET.get(s, "OTHER")


def classify_violation_severity(raw: Any) -> str | None:
    """Classify OSHA violation-severity code into the 5-bin enum.

    Returns one of:
      'WILLFUL' | 'SERIOUS' | 'REPEAT' | 'OTHER' | 'UNCLASSIFIED'
    or None if the input is missing/empty.

    "S"           → 'SERIOUS'
    "Serious"     → 'SERIOUS'
    "W"           → 'WILLFUL'
    "willful"     → 'WILLFUL'
    "R"           → 'REPEAT'
    "O"           → 'OTHER'
    "U"           → 'UNCLASSIFIED'
    "x"           → 'UNCLASSIFIED'  (unknown — fall through to UNCLASSIFIED)
    None / empty  → None
    """
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if not s:
        return None
    return _VIOL_SEVERITY_TO_BUCKET.get(s, "UNCLASSIFIED")


def normalize_osha_standard(raw: Any) -> str | None:
    """Normalize an OSHA standard citation (e.g. '1926.501') for join-key use.

    OSHA standards are CFR-style strings like '1926.501' (fall protection),
    '1910.147' (LOTO), '1904.0' (recordkeeping). Some legacy rows ship them
    with whitespace or lowercase prefixes (`5a1` for general-duty clause).

    "1926.501"          → "1926.501"
    "  1910.147 "       → "1910.147"
    "1926501"           → "1926501"   (no period — preserve raw shape)
    "5A0001"            → "5A0001"    (general-duty clause, uppercased)
    None / empty        → None
    """
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if not s:
        return None
    s = _WHITESPACE_RUN.sub(" ", s).strip()
    return s or None


def classify_accident_outcome(raw: Any) -> str | None:
    """Classify OSHA `degree_of_inj` code into the 4-bin outcome enum.

    Returns one of:
      'FATALITY' | 'HOSPITALIZATION' | 'INJURY' | 'OTHER'
    or None if missing.

    "1"           → 'FATALITY'
    "fatality"    → 'FATALITY'
    "2"           → 'HOSPITALIZATION'
    "3"           → 'INJURY'
    "4"           → 'OTHER'
    "9"           → 'OTHER'   (unknown numeric — fall through)
    None / empty  → None
    """
    if raw is None:
        return None
    s = str(raw).strip().upper().replace(" ", "")
    if not s:
        return None
    return _OUTCOME_TO_BUCKET.get(s, "OTHER")

"""Pure-functional normalizers for EPA NPDES CGP R2 ingest.

Used by `scripts/run_epa_npdes_cgp_r2_ingest.py` to compute identity-spine
join keys for downstream MVs that bridge the EPA Construction General
Permit registry against USAspending construction recipients (NAICS=23),
OSHA establishments, and SBA borrowers.

Per the directive: normalization happens at INGEST time so the Parquet
carries both raw and normalized columns. RW joins on the normalized
columns; the raw columns stay as ground-truth.

CGP scope: only rows where EXTERNAL_PERMIT_NMBR is in the SWC subset of
NPDES_PERM_COMPONENTS (COMPONENT_TYPE_CODE = 'SWC' = "Storm Water
Construction"). Filter is applied at the DuckDB layer in the ingest
script — these helpers don't perform it.
"""

from __future__ import annotations

import re
from typing import Any, Final


_WHITESPACE_RUN: Final = re.compile(r"\s+")
_ORG_PUNCT: Final = re.compile(r"[.,&]")
_NON_DIGIT: Final = re.compile(r"\D")
_NON_ALNUM: Final = re.compile(r"[^A-Z0-9]")

# Trailing tokens stripped from a construction-site operator's legal
# business name. CGP filings are dominated by general contractors,
# property developers, and homebuilders carrying these suffixes; the
# operator field also includes plenty of state/municipal entities
# ("CITY OF DALLAS", "TXDOT") — those are deliberately preserved.
_ORG_SUFFIXES: Final = frozenset({
    "llc", "inc", "incorporated", "corp", "corporation",
    "co", "company", "ltd", "limited",
    "lp", "llp", "pa", "pc", "pllc",
    "group", "associates", "association", "partners",
})

# Permit-status classifier. The ICIS_PERMITS source ships a
# PERMIT_STATUS_CODE column with these one-letter codes. Mapping
# captured from the ECHO data dictionary.
_PERMIT_STATUS_RULES: Final = {
    "EFF": "EFFECTIVE",
    "EFFECTIVE": "EFFECTIVE",
    "ADC": "EFFECTIVE",   # "Administratively continued" — still effective
    "TRM": "TERMINATED",
    "TERMINATED": "TERMINATED",
    "EXP": "EXPIRED",
    "EXPIRED": "EXPIRED",
    "RET": "EXPIRED",     # "Retired" — treated as EXPIRED for join purposes
    "PND": "PENDING",
    "PENDING": "PENDING",
    "NON": "PENDING",     # "Non-applicable" — surface as PENDING
}


def normalize_operator_name(raw: str | None) -> str | None:
    """Normalize a construction-site operator's legal business name.

    The CGP `PERMIT_NAME` column carries a mixed bag: corporate names
    ("PULTE HOMES INC"), municipal entities ("CITY OF DALLAS"), state
    DOTs ("TXDOT - DISTRICT 12"), federal agencies ("USACE FORT WORTH
    DISTRICT"), individual property owners.

    Steps:
      1. Lowercase + trim.
      2. Replace ".", ",", "&" with spaces.
      3. Collapse whitespace.
      4. Iteratively strip trailing tokens listed in `_ORG_SUFFIXES`
         (so "PULTE HOMES INC" → "pulte homes").

    Mirrors `cms_pecos_normalize.normalize_org_name` for parity with
    the cross-source identity-spine recipes.

    "Pulte Homes Inc."           → "pulte homes"
    "ACME CONSTRUCTION LLC"      → "acme construction"
    "CITY OF DALLAS"             → "city of dallas"
    "TXDOT - DISTRICT 12"        → "txdot - district 12"
    "St. Mary's Hospital, Inc."  → "st mary's hospital"
    None / empty                 → None
    """
    if raw is None:
        return None
    s = raw.strip().lower()
    if not s:
        return None
    s = _ORG_PUNCT.sub(" ", s)
    s = _WHITESPACE_RUN.sub(" ", s).strip()
    if not s:
        return None
    parts = s.split(" ")
    while len(parts) >= 2 and parts[-1] in _ORG_SUFFIXES:
        parts = parts[:-1]
    s = " ".join(parts).strip()
    return s or None


def normalize_permit_number(raw: Any) -> str | None:
    """Normalize an EPA-issued NPDES permit number.

    EPA permit numbers follow the pattern `<state-prefix><serial>` —
    e.g. "TXR15ABCD", "AKR10GENR1", "FL0019920". They're case-sensitive
    in the source but we uppercase + strip non-alphanumeric to make
    them join-stable across upstream typos.

    "txr15abcd  "   → "TXR15ABCD"
    "AKR-10-GENR1"  → "AKR10GENR1"
    "FL 0019920"    → "FL0019920"
    "  "            → None
    None            → None
    """
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if not s:
        return None
    s = _NON_ALNUM.sub("", s)
    return s or None


def classify_permit_status(raw: Any) -> str | None:
    """Bucket an ICIS PERMIT_STATUS_CODE into a canonical status.

    Returns one of {EFFECTIVE, TERMINATED, EXPIRED, PENDING}, or None
    if the input doesn't match any known code.

    "EFF"        → "EFFECTIVE"
    "Effective"  → "EFFECTIVE"
    "TRM"        → "TERMINATED"
    "EXP"        → "EXPIRED"
    "RET"        → "EXPIRED"
    "PND"        → "PENDING"
    "ADC"        → "EFFECTIVE"
    None / empty → None
    "ZZZ"        → None     (unknown code)
    """
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if not s:
        return None
    return _PERMIT_STATUS_RULES.get(s)


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

    ICIS state codes are 2-letter postal codes including territories
    (PR, VI, GU, AS, MP). Anything else is invalid.

    "tx"      → "TX"
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


def normalize_naics_code(raw: Any) -> str | None:
    """Normalize a NAICS code to its 6-digit form.

    EPA's NPDES_NAICS bulk feed already ships 6-digit codes, but a few
    legacy rows arrive with leading zeros stripped or with trailing
    whitespace. Reject anything that isn't exactly 6 digits after
    cleanup — partial codes are downstream-noise.

    "236220"     → "236220"
    "  236220 "  → "236220"
    "23622"      → "023622"  (left-pad to 6)
    "2362200"    → None      (>6 digits)
    "abc"        → None      (no digits)
    None / empty → None
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    digits = _NON_DIGIT.sub("", s)
    if not digits or len(digits) > 6:
        return None
    return digits.rjust(6, "0")

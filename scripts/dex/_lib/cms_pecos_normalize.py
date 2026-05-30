"""Pure-functional normalizers for CMS PECOS R2 ingest.

Used by `scripts/run_cms_pecos_r2_ingest.py` to compute identity-spine join
keys for downstream MVs that bridge PECOS practitioners + organizations
against NPPES, FEC (medical-occupation donor cohort), and CMS Open Payments.

Per the directive: normalization happens at INGEST time so the Parquet
carries both raw and normalized columns. RW joins on the normalized
columns; the raw columns stay as ground-truth.

Future LLM-assisted canonicalization (org-name disambiguation, specialty
clustering) belongs in a downstream MV — NOT here.
"""

from __future__ import annotations

import re
from typing import Any, Final


_WHITESPACE_RUN: Final = re.compile(r"\s+")
_NAME_PUNCT: Final = re.compile(r"[.,'\-]")
_ORG_PUNCT: Final = re.compile(r"[.,&]")
_NON_DIGIT: Final = re.compile(r"\D")

# Trailing tokens stripped from a Medicare-enrolled organization's legal
# business name. PECOS org names are dominated by group practices,
# hospitals, and supplier corporations carrying these suffixes.
_ORG_SUFFIXES: Final = frozenset({
    "llc", "inc", "incorporated", "corp", "corporation",
    "co", "company", "ltd", "limited",
    "lp", "llp", "pa", "pc", "pllc",
    "group", "associates", "association", "practice",
})


def normalize_npi(raw: Any) -> str | None:
    """Normalize a National Provider Identifier to the 10-digit form.

    NPPES allocates NPIs as 10-digit numbers; CMS bulk extracts ship them
    as integers OR strings, sometimes with leading zeros stripped. We:
      1. Drop non-digits.
      2. Left-pad to 10 with zeros if shorter.
      3. Reject if longer than 10 digits.

    "1003879883"   → "1003879883"
    1003879883     → "1003879883"
    "  1003879883" → "1003879883"
    "12345"        → "0000012345"
    "12345678901"  → None    (too long; not a valid NPI)
    ""             → None
    None           → None
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    digits = _NON_DIGIT.sub("", s)
    if not digits:
        return None
    if len(digits) > 10:
        return None
    return digits.rjust(10, "0")


def normalize_provider_name(raw: str | None) -> str | None:
    """Normalize an individual provider's first or last name component.

    Matches FINRA / FEC name conventions: lowercase + collapse whitespace +
    strip name-internal punctuation. Conservative; downstream MVs decide
    whether to fold accents.

    "Smith"            → "smith"
    "O'BRIEN"          → "o brien"
    "ALVAREZ RODRIGUEZ"→ "alvarez rodriguez"
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


def normalize_org_name(raw: str | None) -> str | None:
    """Normalize a Medicare-enrolled organization legal business name.

    Steps:
      1. Lowercase + trim.
      2. Replace ".", ",", "&" with spaces.
      3. Collapse whitespace.
      4. Iteratively strip trailing tokens listed in `_ORG_SUFFIXES`
         (so "ACME MEDICAL GROUP LLC" → "acme medical").

    "1 Care Partners Ky Llc"     → "1 care partners ky"
    "BAYSHORE MEDICAL GROUP PC"  → "bayshore medical"
    "St. Mary's Hospital, Inc."  → "st mary's hospital"
    "ACME, INC."                 → "acme"
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

    PECOS state codes are 2-letter postal codes including territories
    (PR, VI, GU). Anything else is invalid.

    "nj"      → "NJ"
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


def normalize_specialty(raw: str | None) -> str | None:
    """Normalize a PECOS PROVIDER_TYPE_DESC or specialty string.

    PECOS publishes 325+ unique provider type descriptions like
    "PRACTITIONER - INTERNAL MEDICINE", "DME SUPPLIER - PHARMACY", or
    "PART A PROVIDER - HOSPITAL". We lowercase + collapse whitespace
    only — bucketing into canonical specialty taxonomies is a downstream
    MV concern.

    "PRACTITIONER - INTERNAL MEDICINE"  → "practitioner - internal medicine"
    "DME SUPPLIER - PHARMACY"           → "dme supplier - pharmacy"
    "  Internal  Medicine  "            → "internal medicine"
    None / empty                        → None
    """
    if raw is None:
        return None
    s = raw.strip().lower()
    if not s:
        return None
    s = _WHITESPACE_RUN.sub(" ", s).strip()
    return s or None


# Bucketing rules for PROVIDER_TYPE_DESC → coarse organization category.
# Conservative — prefix-keyed dispatch covers the long tail uniformly.
_ORG_TYPE_RULES: Final = (
    ("PART A PROVIDER - HOSPITAL", "HOSPITAL"),
    ("PART A PROVIDER - CRITICAL ACCESS HOSPITAL", "HOSPITAL"),
    ("PART A PROVIDER - SKILLED NURSING FACILITY", "SNF"),
    ("PART A PROVIDER - HOME HEALTH AGENCY", "HOME_HEALTH_AGENCY"),
    ("PART A PROVIDER - HOSPICE", "HOSPICE"),
    ("PART A PROVIDER - FEDERALLY QUALIFIED HEALTH CENTER", "FQHC"),
    ("PART A PROVIDER - RURAL HEALTH CLINIC", "RURAL_HEALTH_CLINIC"),
    ("PART A PROVIDER - END-STAGE RENAL DISEASE FACILITY", "ESRD_FACILITY"),
    ("PART B SUPPLIER - CLINIC/GROUP PRACTICE", "GROUP_PRACTICE"),
    ("PART B SUPPLIER - AMBULATORY SURGICAL CENTER", "ASC"),
    ("PART B SUPPLIER - INDEPENDENT DIAGNOSTIC TESTING FACILITY", "IDTF"),
    ("PART B SUPPLIER - INDEPENDENT CLINICAL LABORATORY", "CLINICAL_LAB"),
    ("PART B SUPPLIER - AMBULANCE", "AMBULANCE"),
    ("PART B SUPPLIER - PHARMACY", "PHARMACY"),
    ("DME SUPPLIER", "DME_SUPPLIER"),
    ("PART A PROVIDER", "OTHER_PART_A"),
    ("PART B SUPPLIER", "OTHER_PART_B"),
)


def derive_org_type(raw: str | None) -> str | None:
    """Bucket a PECOS PROVIDER_TYPE_DESC into a canonical org-type string.

    Uses prefix-matched rules over the 325+ source descriptions. Returns
    None if the input doesn't match any rule (typically practitioner-
    grain rows, which are filtered out before this is called).

    "PART A PROVIDER - HOSPITAL"            → "HOSPITAL"
    "PART B SUPPLIER - CLINIC/GROUP PRACTICE" → "GROUP_PRACTICE"
    "DME SUPPLIER - PHARMACY"               → "DME_SUPPLIER"
    "PRACTITIONER - INTERNAL MEDICINE"      → None
    None / empty                            → None
    """
    if raw is None:
        return None
    s = raw.strip().upper()
    if not s:
        return None
    for prefix, bucket in _ORG_TYPE_RULES:
        if s.startswith(prefix):
            return bucket
    return None


def is_dmepos_supplier(raw: str | None) -> bool:
    """Predicate: does this PROVIDER_TYPE_DESC describe a DMEPOS supplier?

    Used to split the org-grain stream into a separate dmepos_suppliers
    Parquet for downstream surety-bond / accreditation MV scoping
    (surety bond columns themselves are NOT in the public PECOS extract).

    "DME SUPPLIER - PHARMACY"           → True
    "DME SUPPLIER - PHYSICIAN - PODIATRY" → True
    "PART A PROVIDER - HOSPITAL"        → False
    None / empty                        → False
    """
    if raw is None:
        return False
    return raw.strip().upper().startswith("DME SUPPLIER")

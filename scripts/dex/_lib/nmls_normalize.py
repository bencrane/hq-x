"""Pure-functional normalizers for NMLS Consumer Access bulk export ingest.

The downstream identity-spine joins:

  individual MLO side
    - nmls_id_normalized            BIGINT (canonical national PK)
    - mlo_first_normalized          lowercased + punctuation-stripped
    - mlo_middle_normalized         lowercased + punctuation-stripped
    - mlo_last_normalized           lowercased + punctuation-stripped
    - mlo_address_zip5              5-digit ZIP slice
    - mlo_address_state_normalized  2-letter US/territory code

  employer side
    - employer_nmls_id_normalized   BIGINT (parent institution PK; FK from MLO)
    - employer_name_normalized      lowercased + corp-form-suffix-stripped
    - employer_address_zip5
    - employer_address_state_normalized
    - employer_kind_normalized      BANK | CREDIT_UNION | MORTGAGE_BANK
                                      | MORTGAGE_BROKER | OTHER

  status side
    - mlo_status_normalized         ACTIVE | INACTIVE | TERMINATED
                                      | SUSPENDED | REVOKED | EXPIRED | OTHER
    - employer_status_normalized    same enum (NMLS reuses the codes)

These functions are pure (no I/O), deterministic, and unit-tested. They mirror
the structure of `_lib/insurance_producers_normalize.py` and `_lib/fec_normalize.py`.

Future LLM-assisted canonicalization (employer disambiguation across NMLS ⨝
FDIC ⨝ NCUA ⨝ FEC, fuzzy MLO-to-FEC-donor name matching) belongs in a downstream
MV that consumes raw + normalized columns — NOT here.
"""

from __future__ import annotations

import re
from typing import Final


_WHITESPACE_RUN: Final = re.compile(r"\s+")
_NAME_PUNCT: Final = re.compile(r"[,.&'\"]")
_NA_SUFFIX_VARIANTS: Final = re.compile(
    r"\bn\s*\.\s*a\s*\.?\B|\bn\.a\.?\b", re.IGNORECASE,
)

# Common org suffixes stripped from the END of an employer string after
# punctuation normalization. Tracks the FEC + insurance-producer suffix list
# plus depository-bank suffixes ("FSB", "NA", "FA").
_ORG_SUFFIXES: Final = (
    "llc",
    "inc",
    "incorporated",
    "corp",
    "corporation",
    "ltd",
    "limited",
    "lp",
    "llp",
    "pc",
    "pa",
    "pllc",
    "co",
    "company",
    "na",
    "fsb",
    "fa",
    "ssb",
    "trust",
    "group",
    "holdings",
    "associates",
    "partners",
    "partnership",
)


_STATE_CODES: Final = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC", "PR", "VI", "GU", "AS", "MP",
})


# --------------------------------------------------------------------------- #
# Canonical employer-kind enum.
# --------------------------------------------------------------------------- #
# Keep in sync with the validation gate's expected employer_kind_normalized
# distribution.

EMPLOYER_BANK: Final = "BANK"
EMPLOYER_CREDIT_UNION: Final = "CREDIT_UNION"
EMPLOYER_MORTGAGE_BANK: Final = "MORTGAGE_BANK"
EMPLOYER_MORTGAGE_BROKER: Final = "MORTGAGE_BROKER"
EMPLOYER_OTHER: Final = "OTHER"


# --------------------------------------------------------------------------- #
# Canonical status enum (shared between MLO and employer rows).
# --------------------------------------------------------------------------- #

STATUS_ACTIVE: Final = "ACTIVE"
STATUS_INACTIVE: Final = "INACTIVE"
STATUS_TERMINATED: Final = "TERMINATED"
STATUS_SUSPENDED: Final = "SUSPENDED"
STATUS_REVOKED: Final = "REVOKED"
STATUS_EXPIRED: Final = "EXPIRED"
STATUS_OTHER: Final = "OTHER"


# --------------------------------------------------------------------------- #
# NMLS-ID normalizer.
# --------------------------------------------------------------------------- #


def normalize_nmls_id(raw: str | None) -> int | None:
    """Coerce an NMLS ID string to BIGINT.

    NMLS IDs are 4-7 digit numerics; bulk extracts may emit them with
    surrounding whitespace, an Excel-protect wrapper, or as quoted text.
    Anything non-numeric returns None.

    '12345'        → 12345
    ' 12345 '      → 12345
    '="12345"'     → 12345
    'N/A'          → None
    None / ''      → None
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # Strip Excel-protect wrapper if present.
    if s.startswith('="') and s.endswith('"'):
        s = s[2:-1].strip()
    digits = re.sub(r"\D", "", s)
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Name normalizers.
# --------------------------------------------------------------------------- #


def _strip_punct_collapse(s: str) -> str:
    s = _NAME_PUNCT.sub(" ", s)
    s = _WHITESPACE_RUN.sub(" ", s).strip()
    return s


def normalize_mlo_name_part(raw: str | None) -> str | None:
    """Normalize a single MLO name component (first / middle / last).

    NMLS publishes structured first/middle/last columns for individuals — we
    do not need the FEC-style "LAST, FIRST" comma-reverse heuristic here.
    Strip punctuation, lowercase, collapse whitespace.

    'JOHN'              → 'john'
    ' John A. '         → 'john a'
    'O''Brien'          → 'o brien'
    None / ''           → None
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    s = s.lower()
    s = _strip_punct_collapse(s)
    return s or None


def normalize_employer_name(raw: str | None) -> str | None:
    """Normalize an employer / institution name for cross-source joins.

    Steps:
      1. Lowercase + trim.
      2. Collapse N.A. / N. A. → "na".
      3. Replace ".", ",", "&", "'", "\"" with spaces.
      4. Collapse whitespace.
      5. Strip ONE trailing org suffix.

    'BANK OF AMERICA, N.A.'         → 'bank of america'
    'JPMORGAN CHASE BANK, N.A.'     → 'jpmorgan chase bank'
    'Quicken Loans, LLC'            → 'quicken loans'
    'Wells Fargo Home Mortgage'     → 'wells fargo home mortgage'
    None / ''                        → None
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    s = s.lower()
    s = _NA_SUFFIX_VARIANTS.sub("na", s)
    s = _strip_punct_collapse(s)
    if not s:
        return None
    parts = s.split(" ")
    if len(parts) >= 2 and parts[-1] in _ORG_SUFFIXES:
        parts = parts[:-1]
        s = " ".join(parts).strip()
    return s or None


# --------------------------------------------------------------------------- #
# Address normalizers.
# --------------------------------------------------------------------------- #


def zip5(raw: str | None) -> str | None:
    """Extract a 5-digit ZIP from a postal-code field.

    '12345'            → '12345'
    '12345-6789'       → '12345'
    '123456789'        → '12345'
    'K1A 0B1'          → None      (Canadian)
    None / ''          → None
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) < 5:
        return None
    return digits[:5]


def normalize_state_code(raw: str | None) -> str | None:
    """Normalize a state field to a 2-letter US/territory code.

    'CO'        → 'CO'
    ' tx '      → 'TX'
    'Texas'     → None
    'ZZ'        → None
    None / ''   → None
    """
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if len(s) != 2:
        return None
    return s if s in _STATE_CODES else None


# --------------------------------------------------------------------------- #
# Employer-kind classifier.
# --------------------------------------------------------------------------- #
# Dispatch on raw entity-type / authority-type strings published by NMLS.
# Substring match — NMLS's exact code values vary by report stream and have
# drifted historically (e.g., "BANK", "FEDERAL DEPOSITORY INSTITUTION",
# "CREDIT UNION", "STATE-CHARTERED CREDIT UNION", etc.).

_EMPLOYER_KIND_PATTERNS: Final = (
    # Credit unions FIRST — substring "credit" can otherwise collide with
    # "credit reporting agency" elsewhere; the pattern here requires "union"
    # context.
    ("credit union", EMPLOYER_CREDIT_UNION),
    ("federal credit", EMPLOYER_CREDIT_UNION),
    ("state-chartered credit", EMPLOYER_CREDIT_UNION),
    ("cu", EMPLOYER_CREDIT_UNION),  # 2-letter authority code
    # Depository banks.
    ("federal depository", EMPLOYER_BANK),
    ("national bank", EMPLOYER_BANK),
    ("state bank", EMPLOYER_BANK),
    ("savings bank", EMPLOYER_BANK),
    ("savings and loan", EMPLOYER_BANK),
    ("savings & loan", EMPLOYER_BANK),
    ("commercial bank", EMPLOYER_BANK),
    ("trust company", EMPLOYER_BANK),
    ("federal savings", EMPLOYER_BANK),
    ("bank holding", EMPLOYER_BANK),
    # Mortgage-bank (non-depository lender that funds its own loans).
    ("mortgage bank", EMPLOYER_MORTGAGE_BANK),
    ("mortgage banker", EMPLOYER_MORTGAGE_BANK),
    ("mortgage lender", EMPLOYER_MORTGAGE_BANK),
    ("non-depository", EMPLOYER_MORTGAGE_BANK),
    # Mortgage-broker (places loans with third-party lenders).
    ("mortgage broker", EMPLOYER_MORTGAGE_BROKER),
    ("loan broker", EMPLOYER_MORTGAGE_BROKER),
    ("broker", EMPLOYER_MORTGAGE_BROKER),
    # Generic "bank" fallback — must come AFTER the more-specific patterns
    # so "mortgage bank" wins over the bare "bank" suffix.
    ("bank", EMPLOYER_BANK),
)


def classify_employer_kind(raw: str | None) -> str | None:
    """Map a raw NMLS entity-type / authority-type string to canonical enum.

    'Federal Depository Institution'         → 'BANK'
    'NATIONAL BANK'                          → 'BANK'
    'Mortgage Bank/Mortgage Banker'          → 'MORTGAGE_BANK'
    'State Chartered Credit Union'           → 'CREDIT_UNION'
    'Mortgage Broker'                        → 'MORTGAGE_BROKER'
    None / ''                                → None
    Anything else (recognized but unmapped)  → 'OTHER'
    """
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if not s:
        return None
    for fragment, canonical in _EMPLOYER_KIND_PATTERNS:
        if fragment in s:
            return canonical
    return EMPLOYER_OTHER


# --------------------------------------------------------------------------- #
# MLO / employer status classifier.
# --------------------------------------------------------------------------- #


_STATUS_MAP: Final = {
    "active": STATUS_ACTIVE,
    "approved": STATUS_ACTIVE,
    "approved-conditional": STATUS_ACTIVE,
    "approved - conditional": STATUS_ACTIVE,
    "approved-deficient": STATUS_ACTIVE,
    "approved - deficient": STATUS_ACTIVE,
    "approved-renewal-required": STATUS_ACTIVE,
    "approved deficient": STATUS_ACTIVE,
    "approved deficient renewal required": STATUS_ACTIVE,
    "in good standing": STATUS_ACTIVE,
    "good standing": STATUS_ACTIVE,
    "valid": STATUS_ACTIVE,
    "current": STATUS_ACTIVE,
    "inactive": STATUS_INACTIVE,
    "deactivated": STATUS_INACTIVE,
    "withdrawn": STATUS_INACTIVE,
    "voluntarily surrendered": STATUS_INACTIVE,
    "surrendered": STATUS_INACTIVE,
    "terminated": STATUS_TERMINATED,
    "terminated-deceased": STATUS_TERMINATED,
    "terminated - deceased": STATUS_TERMINATED,
    "terminated for cause": STATUS_TERMINATED,
    "terminated-cause": STATUS_TERMINATED,
    "abandoned": STATUS_TERMINATED,
    "suspended": STATUS_SUSPENDED,
    "revoked": STATUS_REVOKED,
    "expired": STATUS_EXPIRED,
    "lapsed": STATUS_EXPIRED,
    "expired - eligible to renew": STATUS_EXPIRED,
    "expired-eligible-to-renew": STATUS_EXPIRED,
}


def classify_mlo_status(raw: str | None) -> str | None:
    """Map a state/federal MLO status string to canonical enum.

    'Active'                          → 'ACTIVE'
    'APPROVED'                        → 'ACTIVE'
    'Approved-Conditional'            → 'ACTIVE'
    'Suspended'                       → 'SUSPENDED'
    'Terminated'                      → 'TERMINATED'
    'Terminated-Deceased'             → 'TERMINATED'
    'Expired - Eligible to Renew'     → 'EXPIRED'
    None / ''                         → None
    Anything unrecognized             → 'OTHER'
    """
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if not s:
        return None
    return _STATUS_MAP.get(s, STATUS_OTHER)


def is_active_status(canonical_status: str | None) -> bool:
    """Boolean derived flag from canonical status enum value."""
    return canonical_status == STATUS_ACTIVE


# --------------------------------------------------------------------------- #
# State-licenses-set joiner.
# --------------------------------------------------------------------------- #


def join_state_licenses_set(state_codes: list[str | None]) -> str | None:
    """Join a list of (possibly None / unnormalized) state codes into a
    semicolon-separated, deduplicated, sorted set string.

    ['TX', 'fl', 'TX', None, 'CA', 'ZZ']  → 'CA;FL;TX'   (ZZ dropped)
    []                                    → None
    [None, None]                          → None
    """
    if not state_codes:
        return None
    canonical: set[str] = set()
    for raw in state_codes:
        norm = normalize_state_code(raw)
        if norm:
            canonical.add(norm)
    if not canonical:
        return None
    return ";".join(sorted(canonical))

"""Pure-functional normalizers for SBA EIDL R2 ingest.

EIDL bulk on data.sba.gov ships in the federal DATA Act / FFATA schema (NOT
SBA's typical FOIA loan-level field names). Identity-spine columns surface
as:

  AWARDEEORRECIPIENTLEGALENTITYNAME[ANDDOINGBUSINESSAS]  →  borrower_name
  AWARDEEORRECIPIENTUNIQUEIDENTIFIER                     →  DUNS (NOT EIN)
  LEGALENTITYZIP5                                        →  borrower_zip5 input
  LEGALENTITYSTATECD                                     →  borrower_state input
  BUSINESSTYPES                                          →  borrower_kind code
  ACTIONDATE  (YYYYMMDD)                                 →  loan_action_date

These normalizers produce the join keys downstream identity MVs will use to
bridge EIDL borrowers against PPP, FEC, USPTO, IRS BMF, USAspending recipients.

Borrower-name normalization mirrors `scripts/build_sba_ppp_parquet.py`'s
`_normalize_borrower_name` shape (any-position suffix strip) for PPP↔EIDL
join-key parity — they're the COVID-era companion programs.

Future LLM-assisted canonicalization (alias clustering, DBA disambiguation)
belongs in a downstream MV that consumes the raw columns — NOT here.
"""

from __future__ import annotations

import re
from typing import Any, Final


_WHITESPACE_RUN: Final = re.compile(r"\s+")
_NON_DIGIT: Final = re.compile(r"\D")
_NAME_PUNCT: Final = re.compile(r"[^\w\s]+")

# Corp-form suffixes stripped from borrower names (any position). Matches
# the PPP normalizer set (scripts/build_sba_ppp_parquet.py:_SUFFIX_TOKENS) so
# EIDL↔PPP joins on borrower_name_normalized are a clean equality match.
_BORROWER_SUFFIX_TOKENS: Final = (
    "incorporated", "corporation", "company", "limited",
    "pllc", "llp", "lp", "llc", "inc", "ltd", "corp", "co", "pa",
    "holdings", "group", "associates",
)
_BORROWER_SUFFIX_RE: Final = re.compile(
    r"\b(?:" + "|".join(re.escape(t) for t in _BORROWER_SUFFIX_TOKENS) + r")\b\.?",
    re.IGNORECASE,
)

# DATA Act BUSINESSTYPES single-letter codes that appear in EIDL data. Source:
# OMB FFATA schema; see https://www.fpds.gov/. We map to the directive's
# canonical 5-bin enum: sole_prop / llc / corporation / partnership / nonprofit
# / unknown. The DATA Act schema doesn't subdivide LLC vs Inc — corp-form
# discrimination has to come from name-pattern heuristics (corp suffix tokens).
_BUSINESSTYPES_TO_KIND: Final = {
    "PR": "sole_prop",        # individual / sole proprietor
    "I": "sole_prop",         # individual
    "N": "nonprofit",         # nonprofit
    "O": "nonprofit",         # nonprofit (alt code)
    "P": "partnership",       # partnership
    "R": "small_business",    # for-profit small business (most common EIDL code)
    "Q": "small_business",    # for-profit organization
    "23": "small_business",   # small business (numeric DATA Act code)
}

# Trailing tokens that classify a name as a partnership / LLP regardless of
# the BUSINESSTYPES code. Used as a name-pattern fallback when BUSINESSTYPES
# is missing or coded ambiguously.
_PARTNERSHIP_HINTS: Final = frozenset({"llp", "lp"})
_NONPROFIT_HINTS: Final = frozenset({
    "foundation", "trust", "ministry", "ministries", "church",
    "charity", "fund",
})


def normalize_borrower_name(raw: str | None) -> str | None:
    """Normalize an EIDL borrower (legal entity) name for cross-source joining.

    Steps:
      1. Lowercase + trim.
      2. Strip corp-form suffixes (LLC, Inc, Corp, …) wherever they appear.
      3. Replace non-word punctuation with spaces.
      4. Collapse whitespace.

    "ACME, INC."                 → "acme"
    "Acme Holdings, LLC"         → "acme"
    "TimberPine, Inc DBA TimberPine, Inc" → "timberpine dba timberpine"
    "American Mortgage Corporation DBA American Mortgage Corporation"
                                  → "american mortgage dba american mortgage"
    "John Smith"                 → "john smith"
    None / empty                 → None
    """
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if not s:
        return None
    s = _BORROWER_SUFFIX_RE.sub(" ", s)
    s = _NAME_PUNCT.sub(" ", s)
    s = _WHITESPACE_RUN.sub(" ", s).strip()
    return s or None


def parse_sole_prop_first_last(
    raw: str | None,
) -> tuple[str | None, str | None]:
    """Parse a sole-prop borrower name into (first_normalized, last_normalized).

    EIDL DATA Act sole-prop names appear in two patterns:
      "First Last"           — most common
      "Last, First [Suffix]" — older convention

    Returns (None, None) for org-shaped names (multi-token after suffix-strip,
    no comma, last token is a corp suffix). Caller decides whether to use
    these fields based on borrower_kind_normalized.

    "John Smith"           → ("john", "smith")
    "Smith, John A."       → ("john", "smith")          (drops middle initial)
    "Les R. Rager,DDS"     → ("les", "rager")           (drops trailing post-nom)
    "ACME LLC"             → (None, None)               (corp-shaped)
    None / empty           → (None, None)
    """
    if raw is None:
        return (None, None)
    s = str(raw).strip()
    if not s:
        return (None, None)

    s_lower = s.lower()
    # Strip post-nominals separated by comma (",DDS", ",JR.", ", III").
    if "," in s_lower:
        head, _, _tail = s_lower.partition(",")
        # If the head split has 1 token, it's "Last, First" form: reverse.
        # Otherwise it's "First Last, Suffix" form: drop the tail.
        head = head.strip()
        tail = _tail.strip()
        head_tokens = head.split()
        if len(head_tokens) == 1 and tail:
            # "Last, First [Middle]" form
            tail_tokens = tail.split()
            first = tail_tokens[0]
            last = head_tokens[0]
            first = _NAME_PUNCT.sub("", first).strip()
            last = _NAME_PUNCT.sub("", last).strip()
            if not first or not last:
                return (None, None)
            # Disqualify if either side carries a corp-form token. Catches
            # the "ACME, INC." anti-pattern where the comma split looks like
            # "Last, First" but the head is the org name and the tail is
            # the corp suffix (org-shaped, not sole-prop).
            if (
                first in _BORROWER_SUFFIX_TOKENS
                or last in _BORROWER_SUFFIX_TOKENS
            ):
                return (None, None)
            return (first, last)
        # "First Last, Tail" form — keep head only
        s_lower = head

    s_lower = _NAME_PUNCT.sub(" ", s_lower)
    s_lower = _WHITESPACE_RUN.sub(" ", s_lower).strip()
    if not s_lower:
        return (None, None)
    parts = s_lower.split()
    # If any token is a corp-form suffix, this isn't a sole-prop.
    if any(p in _BORROWER_SUFFIX_TOKENS for p in parts):
        return (None, None)
    if len(parts) < 2:
        return (None, None)
    first = parts[0]
    last = parts[-1]
    return (first, last)


def normalize_duns(raw: Any) -> str | None:
    """Normalize a DATA Act AWARDEEORRECIPIENTUNIQUEIDENTIFIER (DUNS).

    DUNS is 9 digits. SAM later switched to UEI (12-char alphanumeric); EIDL
    bulk through 2020-12-01 predates that, but the field is widely empty
    because SBA didn't require it for COVID emergency funding.

    "123456789"     → "123456789"
    "12-3456789"    → "123456789"
    "123-456-789"   → "123456789"
    "12345"         → None         (too short to be a valid DUNS)
    "ABC123XYZ"     → None         (UEI shape — pre-UEI EIDL data should NOT
                                    have UEI; treat as malformed)
    None / empty    → None
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    digits = _NON_DIGIT.sub("", s)
    if len(digits) != 9:
        return None
    return digits


def zip5(raw: Any) -> str | None:
    """Extract the 5-digit ZIP from LEGALENTITYZIP5 (or any zip-shaped string).

    "12345"        → "12345"
    "12345-6789"   → "12345"
    "123456789"    → "12345"
    "1234"         → None         (too short)
    "abcde"        → None         (no digits)
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

    "ca"      → "CA"
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


def classify_borrower_kind(
    business_types: Any,
    borrower_name: str | None,
) -> str:
    """Classify borrower into the directive's 6-bin enum.

    Decision order:
      1. BUSINESSTYPES code (DATA Act enum, when present and known).
      2. Name-pattern fallback for partnership / nonprofit / corp shapes.
      3. Default to 'unknown'.

    Returns one of:
      'sole_prop' | 'small_business' | 'corporation' | 'partnership'
      | 'nonprofit' | 'unknown'

    Note: DATA Act doesn't subdivide LLC / Inc / Corp at the BUSINESSTYPES
    level, so we map to 'small_business' as the catch-all for-profit bucket.
    Downstream MVs that need finer corp-form distinction can re-classify
    from the raw name.
    """
    if isinstance(business_types, str):
        bt = business_types.strip().upper()
        # BUSINESSTYPES can be a single code or a comma/space-delimited list.
        # Pick the first known code; the directive's enum is single-valued.
        for tok in re.split(r"[,\s/;]+", bt):
            tok = tok.strip()
            if tok in _BUSINESSTYPES_TO_KIND:
                return _BUSINESSTYPES_TO_KIND[tok]

    # Name-pattern fallback (cheap heuristics; downstream MV can refine).
    if borrower_name:
        s = str(borrower_name).strip().lower()
        if s:
            tokens = set(_WHITESPACE_RUN.split(_NAME_PUNCT.sub(" ", s)))
            if tokens & _PARTNERSHIP_HINTS:
                return "partnership"
            if tokens & _NONPROFIT_HINTS:
                return "nonprofit"
            if tokens & {"llc", "inc", "corp", "corporation"}:
                return "small_business"

    return "unknown"


def naics_2digit(raw: Any) -> str | None:
    """Extract the 2-digit NAICS sector code.

    EIDL DATA Act bulk doesn't ship NAICS at the row level (NAICS is in
    `awarding_office_code`-adjacent metadata, not per-recipient). This helper
    is included for forward-compat with future SBA EIDL publications that
    add NAICS, and is exercised by `eidl_snapshot_date` partition rows that
    backfill NAICS from cross-source joins.

    "541110"   → "54"
    "11"       → "11"
    "1"        → None    (NAICS is always ≥ 2 digits)
    None       → None
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    digits = _NON_DIGIT.sub("", s)
    if len(digits) < 2:
        return None
    return digits[:2]

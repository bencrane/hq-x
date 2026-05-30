"""Pure-functional normalizers for the NCUA Credit Union officer registry.

The NCUA Credit Union Locator API
(`https://mapping.ncua.gov/api/CreditUnionDetails/GetCreditUnionDetails/{charter}`)
returns the current CEO of each CU as a single string in `LAST, FIRST MIDDLE`
form (e.g. `"MILLER, MALCOLM H "`). These helpers split the CEO string into
canonical first/last fields, normalize the CU's office address fields, and
derive a size class from the CU's reported assets — all join keys for the
downstream identity-spine MVs (`mv_ncua_cu_officers_unified`,
`mv_fec_donor_to_ncua_ceo`, `mv_nmls_to_ncua_cu_employer`).

These functions are pure (no I/O), deterministic, and unit-tested. They mirror
the structure of `_lib/insurance_producers_normalize.py`.

Manager / board-chair fields are placeholders for future data sources — the CU
Locator API surfaces only the CEO; if/when a separate per-CU board feed is
ingested, those columns can be populated by the same normalizer.
"""

from __future__ import annotations

import re
from typing import Final

_WHITESPACE_RUN: Final = re.compile(r"\s+")
_NAME_PUNCT: Final = re.compile(r"[.\"']")

_STATE_CODES: Final = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC", "PR", "VI", "GU", "AS", "MP",
})

# Honorific / generational tokens stripped from name parts before splitting.
_HONORIFICS: Final = frozenset({
    "mr", "mrs", "ms", "miss", "dr", "prof", "rev", "sr", "jr",
    "ii", "iii", "iv",
})


def _clean_token(s: str) -> str:
    """Lowercase, strip surrounding punctuation, drop trailing periods."""
    t = s.strip().lower()
    t = _NAME_PUNCT.sub("", t)
    return t.strip()


def normalize_cu_name(raw: str | None) -> str | None:
    """Lowercase + whitespace-collapse a credit-union name.

    Does NOT strip "Federal Credit Union" / "FCU" suffixes — those carry
    charter-type signal that downstream MVs use. Punctuation is removed only
    where it harms join stability (apostrophes, periods, double-quotes).

    Examples:
      'NAVY FEDERAL CREDIT UNION'            → 'navy federal credit union'
      "Pittsburgh Firefighters' FCU"         → 'pittsburgh firefighters fcu'
      'BANK-FUND STAFF FEDERAL CREDIT UNION' → 'bank-fund staff federal credit union'
      None / ''                              → None
    """
    if raw is None:
        return None
    s = raw.strip().lower()
    if not s:
        return None
    s = _NAME_PUNCT.sub("", s)
    s = _WHITESPACE_RUN.sub(" ", s).strip()
    return s or None


def normalize_officer_name(raw: str | None) -> tuple[str | None, str | None]:
    """Split a `LAST, FIRST MIDDLE` officer name into (first, last) lowercase.

    The NCUA Locator API returns CEO names as `"MILLER, MALCOLM H "` —
    last-name first, comma-separated, optional middle initial(s) trailing.
    Some CUs publish names without commas (e.g. `"MALCOLM MILLER"`); in that
    case the LAST whitespace-separated token becomes `last`, the FIRST becomes
    `first`. Honorifics ("Jr", "Sr", "II", "III") are stripped.

    Returns (None, None) if the input cannot be parsed into both first and
    last components (e.g. blank input or single-token CEO field).

    Examples:
      'MILLER, MALCOLM H '              → ('malcolm', 'miller')
      'SMITH JR, JANE Q.'               → ('jane', 'smith')
      'OBRIEN, KEVIN'                   → ('kevin', 'obrien')
      'JANE SMITH'                      → ('jane', 'smith')
      'MARY KAY VAN DER BERG'           → ('mary', 'van der berg')
      ''                                → (None, None)
      'CHIEF EXECUTIVE OFFICER'         → (None, None)  (no clean split)
      None                              → (None, None)
    """
    if raw is None:
        return (None, None)
    s = raw.strip()
    if not s:
        return (None, None)

    last: str | None = None
    first: str | None = None

    if "," in s:
        head, _, tail = s.partition(",")
        last_part = head.strip()
        rest_part = tail.strip()
        last_tokens = [t for t in last_part.split() if _clean_token(t) not in _HONORIFICS]
        rest_tokens = [t for t in rest_part.split() if _clean_token(t) not in _HONORIFICS]
        if last_tokens and rest_tokens:
            last = " ".join(_clean_token(t) for t in last_tokens if _clean_token(t)) or None
            first = _clean_token(rest_tokens[0]) or None
    else:
        tokens = [t for t in s.split() if _clean_token(t) not in _HONORIFICS]
        cleaned = [_clean_token(t) for t in tokens if _clean_token(t)]
        if len(cleaned) >= 2:
            first = cleaned[0]
            last = cleaned[-1]

    if not first or not last:
        return (None, None)
    return (first, last)


def normalize_state(raw: str | None) -> str | None:
    """Uppercase + validate against the US-state / -territory set.

    Examples:
      'ca'    → 'CA'
      ' GA '  → 'GA'
      'XX'    → None
      None    → None
    """
    if raw is None:
        return None
    s = raw.strip().upper()
    if not s or s not in _STATE_CODES:
        return None
    return s


def zip5(raw: str | None) -> str | None:
    """Extract a 5-digit US ZIP from a postal-code field.

    '12345'         → '12345'
    '12345-6789'    → '12345'
    '123456789'     → '12345'
    'K1A 0B1'       → None  (Canadian)
    ''              → None
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) < 5:
        return None
    return digits[:5]


def _parse_assets(raw: str | None) -> int | None:
    """Parse an NCUA assets string ('0', '1234567', '$1,234,567') to int."""
    if raw is None:
        return None
    s = raw.strip().lstrip("$").replace(",", "")
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def classify_cu_size_class(assets_raw: str | None) -> str | None:
    """Bucket a CU's reported total assets into a size class.

    Buckets follow NCUA's commonly-cited reporting tiers (matches the peer-group
    cuts used in FOICU/FS220 reports):

      < $50M           → 'small'
      $50M - $250M     → 'medium'
      $250M - $1B      → 'large'
      >= $1B           → 'very_large'

      None / unparseable → None

    Examples:
      '0'              → 'small'
      '49000000'       → 'small'
      '50000000'       → 'medium'
      '249999999'      → 'medium'
      '250000000'      → 'large'
      '1000000000'     → 'very_large'
      '$2,500,000,000' → 'very_large'
      None             → None
    """
    n = _parse_assets(assets_raw)
    if n is None:
        return None
    if n < 50_000_000:
        return "small"
    if n < 250_000_000:
        return "medium"
    if n < 1_000_000_000:
        return "large"
    return "very_large"

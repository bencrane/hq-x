"""Pure-functional normalizers for USAspending federal-spending R2 backfill.

These functions produce the join keys downstream identity-resolution MVs use to
bridge USAspending recipients against SBA borrowers, IRS BMF, FEC employer
fields, GLEIF LEIs, FMCSA carriers, and SAM.gov registrants.

Per the directive: normalization happens at INGEST time so the Parquet carries
both raw and normalized columns side-by-side. RW joins on the normalized
columns; the raw columns stay as ground truth.

Notes:
  - UEI replaced DUNS in 2022. The published bulk archive backfills BOTH columns
    onto every historical row (USAspending applied UEI retroactively where it
    could). Both normalizers are kept; pre-2022 rows reliably carry DUNS,
    2022+ rows reliably carry UEI, and overlap rows carry both.
  - Recipient EIN is not a published column on USAspending bulk; the EIN
    normalizer is left as a placeholder for future use if/when a column shows
    up in the schema.
"""

from __future__ import annotations

import re
from typing import Final


_WHITESPACE_RUN: Final = re.compile(r"\s+")
_NAME_PUNCT: Final = re.compile(r"[.,&]")

# Common org-form suffixes stripped from the END of a recipient name after
# punctuation normalization. Word-boundary, case-insensitive, terminal-only.
_RECIPIENT_SUFFIXES: Final = (
    "llc",
    "inc",
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
    "holdings",
    "group",
    "associates",
)


def normalize_recipient_name(raw: str | None) -> str | None:
    """Lowercase + corp-form-suffix-stripped + whitespace-collapsed.

    "ACME CORP."                → "acme"
    "Acme Holdings, LLC"        → "acme holdings"  (single-pass: only
                                                     terminal LLC stripped)
    "Acme Holdings, LLC HOLDINGS" → "acme holdings llc"  (terminal HOLDINGS
                                                     stripped, no recursion)
    "ACME"                      → "acme"
    "JOHN SMITH"                → "john smith"      (sole-prop passes through)
    None / empty                → None
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    s = s.lower()
    s = _NAME_PUNCT.sub(" ", s)
    s = _WHITESPACE_RUN.sub(" ", s).strip()
    if not s:
        return None

    parts = s.split(" ")
    if len(parts) >= 2 and parts[-1] in _RECIPIENT_SUFFIXES:
        parts = parts[:-1]
        s = " ".join(parts).strip()

    return s or None


def normalize_uei(raw: str | None) -> str | None:
    """12-char UEI: uppercased + non-alnum stripped.

    "ABCD12345EFG"              → "ABCD12345EFG"
    "abcd12345efg"              → "ABCD12345EFG"
    "ABCD-12345-EFG"            → "ABCD12345EFG"
    "ABCDEFG"                   → None    (too short)
    "ABCD12345EFG12"            → None    (too long)
    None / empty                → None
    """
    if raw is None:
        return None
    s = re.sub(r"[^A-Za-z0-9]", "", raw).upper()
    if len(s) != 12:
        return None
    return s


def normalize_duns(raw: str | None) -> str | None:
    """9-digit DUNS: strip non-digit + left-pad to 9.

    "123456789"                 → "123456789"
    "123-456-789"               → "123456789"
    "12345678"                  → "012345678"   (left-pad)
    "1234567890"                → None          (too long; not a DUNS)
    "abc"                       → None          (no digits)
    None / empty                → None
    """
    if raw is None:
        return None
    s = re.sub(r"\D", "", raw)
    if not s or len(s) > 9:
        return None
    return s.zfill(9)


def normalize_ein(raw: str | None) -> str | None:
    """9-digit EIN: strip non-digit + left-pad to 9.

    "12-3456789"                → "123456789"
    "123456789"                 → "123456789"
    "1234567"                   → "001234567"   (left-pad)
    "12345678901"               → None          (too long; not an EIN)
    None / empty                → None
    """
    if raw is None:
        return None
    s = re.sub(r"\D", "", raw)
    if not s or len(s) > 9:
        return None
    return s.zfill(9)


def recipient_zip5(raw: str | None) -> str | None:
    """First 5 chars of recipient ZIP, digits only.

    "12345"                     → "12345"
    "12345-6789"                → "12345"
    "123456789"                 → "12345"
    "12345 "                    → "12345"
    "1234"                      → None    (too short)
    "abcde"                     → None    (no digits)
    None / empty                → None
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


def normalize_state(raw: str | None) -> str | None:
    """Uppercased 2-letter state code; pass-through if non-2-letter.

    "ca"                        → "CA"
    "CA"                        → "CA"
    "California"                → "CALIFORNIA"   (passes through; RW silver
                                                  layer can decide canonical
                                                  bucketing)
    None / empty                → None
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    return s.upper()


def naics_2digit(raw: str | None) -> str | None:
    """First 2 chars of NAICS — industry segment.

    "541512"                    → "54"
    "11"                        → "11"
    "5"                         → None    (too short to be a NAICS)
    None / empty                → None
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s or len(s) < 2:
        return None
    return s[:2]


def normalize_funding_agency(raw: str | None) -> str | None:
    """Uppercased + whitespace-collapsed funding agency name.

    "Department of Defense"     → "DEPARTMENT OF DEFENSE"
    "  HHS  "                   → "HHS"
    None / empty                → None
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    return _WHITESPACE_RUN.sub(" ", s.upper()).strip() or None

"""Pure-functional normalizers for SAM.gov Entity Registration + Exclusions ingest.

These functions produce join keys for downstream identity-resolution MVs:
SAM ⨝ USAspending (UEI), SAM ⨝ FEC (legal_business_name + state),
SAM ⨝ entities.raw_entity_records.

Per the directive: normalization happens at INGEST time so the Parquet carries
both raw and normalized columns. Downstream MVs join on normalized columns;
the raw columns stay as ground-truth.

Pure (no I/O), deterministic, unit-tested. The reference implementation here
mirrors the SQL macros registered in run_sam_gov_r2_ingest.py at apply time —
keep them in lockstep. Future LLM-assisted canonicalization belongs in a
downstream MV that consumes the raw columns, NOT here.
"""

from __future__ import annotations

import re
from typing import Final


_WHITESPACE_RUN: Final = re.compile(r"\s+")
_NON_ALNUM: Final = re.compile(r"[^A-Za-z0-9]")
_NAME_PUNCT: Final = re.compile(r"[.,&]")

# Common org-form suffixes stripped from the END of a legal business name
# after punctuation normalization. Word-boundary, case-insensitive. Order
# matters only for repeat application — we strip ONE terminal suffix.
_LEGAL_NAME_SUFFIXES: Final = (
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
    "lc",
)


def normalize_uei(raw: str | None) -> str | None:
    """Normalize a SAM Unique Entity Identifier to 12 alphanumeric chars, uppercased.

    UEI replaced DUNS in 2022. Format: 12 alphanumeric chars, no separators.
    Returns None if the input cannot be coerced to a 12-char alphanumeric value.

    "ABC123XYZ456"   → "ABC123XYZ456"
    "abc123xyz456"   → "ABC123XYZ456"
    "ABC-123-XYZ456" → "ABC123XYZ456"
    " ABC 123 XYZ456 " → "ABC123XYZ456"
    "TOOSHORT"       → None
    "TOOLONGSTRING"  → None  (>12 chars after stripping non-alnum)
    None / empty     → None
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    cleaned = _NON_ALNUM.sub("", s).upper()
    if len(cleaned) != 12:
        return None
    return cleaned


def normalize_cage_code(raw: str | None) -> str | None:
    """Normalize a CAGE code to 5 alphanumeric chars, uppercased.

    Commercial and Government Entity (CAGE) codes are 5-char alphanumeric
    strings. SAM source data sometimes has whitespace or lowercase.

    "1ABC2"        → "1ABC2"
    "1abc2"        → "1ABC2"
    " 1abc2 "      → "1ABC2"
    "1ABC2X"       → None  (>5 alnum chars)
    "1AB2"         → None  (<5 alnum chars)
    None / empty   → None
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    cleaned = _NON_ALNUM.sub("", s).upper()
    if len(cleaned) != 5:
        return None
    return cleaned


def normalize_legal_business_name(raw: str | None) -> str | None:
    """Normalize a legal business name for cross-source matching.

    Steps:
      1. Lowercase + trim.
      2. Replace ".", ",", "&" with spaces.
      3. Collapse whitespace.
      4. Strip ONE trailing org-form suffix if present (LLC, INC, CORP, …).
      5. Re-collapse whitespace.

    "ACME, INC."                 → "acme"
    "Acme Holdings, LLC"         → "acme holdings"
    "  Acme   Holdings, LLC  "   → "acme holdings"
    "Acme & Sons Co."            → "acme sons"
    "Lockheed Martin Corp"       → "lockheed martin"
    "Department of Defense"      → "department of defense"
    None / empty                 → None
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
    if len(parts) >= 2 and parts[-1] in _LEGAL_NAME_SUFFIXES:
        parts = parts[:-1]
        s = " ".join(parts).strip()
    return s or None


def zip5(raw: str | None) -> str | None:
    """Extract first 5 chars of a postal code as ZIP5.

    SAM publishes ZIP separately from ZIP+4 (e.g., 'physical_address_zippostal_code'
    is the 5-digit ZIP, 'physical_address_zip_code_4' is the +4). Some
    international addresses publish non-numeric postal codes; this helper
    only returns a value when the first 5 chars are all digits.

    "12345"          → "12345"
    "12345-6789"     → "12345"
    "123456789"      → "12345"
    "K1A 0B1"        → None  (Canadian — non-numeric leading 5)
    "1234"           → None  (too short)
    "abcde"          → None
    None / empty     → None
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    digits = "".join(c for c in s if c.isdigit())
    if len(digits) < 5:
        return None
    return digits[:5]


def normalize_state(raw: str | None) -> str | None:
    """Uppercase a 2-letter state/province code, returning None if not 2 chars.

    "Va"      → "VA"
    "TX"      → "TX"
    "Texas"   → None  (long form; SAM publishes both this and 2-letter — caller
                       must pass the 2-letter column)
    None / "" → None
    """
    if raw is None:
        return None
    s = raw.strip().upper()
    if len(s) != 2:
        return None
    if not s.isalpha():
        return None
    return s


def naics_2digit(raw: str | None) -> str | None:
    """First 2 digits of a NAICS code; None if fewer than 2 digits leading.

    "541511"  → "54"
    "11"      → "11"
    "5"       → None
    "ABC"     → None
    None / "" → None
    """
    if raw is None:
        return None
    s = raw.strip()
    if len(s) < 2 or not s[:2].isdigit():
        return None
    return s[:2]


# ──────────────────────────────────────────────────────────────────────────────
# Self-test
# ──────────────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    # UEI
    assert normalize_uei("ABC123XYZ456") == "ABC123XYZ456"
    assert normalize_uei("abc123xyz456") == "ABC123XYZ456"
    assert normalize_uei("ABC-123-XYZ456") == "ABC123XYZ456"
    assert normalize_uei(" ABC 123 XYZ456 ") == "ABC123XYZ456"
    assert normalize_uei("TOOSHORT") is None
    assert normalize_uei("TOOLONGSTRING") is None
    assert normalize_uei("") is None
    assert normalize_uei(None) is None

    # CAGE
    assert normalize_cage_code("1ABC2") == "1ABC2"
    assert normalize_cage_code("1abc2") == "1ABC2"
    assert normalize_cage_code(" 1abc2 ") == "1ABC2"
    assert normalize_cage_code("1ABC2X") is None
    assert normalize_cage_code("1AB2") is None
    assert normalize_cage_code(None) is None

    # Legal business name
    assert normalize_legal_business_name("ACME, INC.") == "acme"
    assert normalize_legal_business_name("Acme Holdings, LLC") == "acme holdings"
    assert normalize_legal_business_name("  Acme   Holdings, LLC  ") == "acme holdings"
    assert normalize_legal_business_name("Acme & Sons Co.") == "acme sons"
    assert normalize_legal_business_name("Lockheed Martin Corp") == "lockheed martin"
    assert normalize_legal_business_name("Department of Defense") == "department of defense"
    assert normalize_legal_business_name("") is None
    assert normalize_legal_business_name(None) is None

    # ZIP5
    assert zip5("12345") == "12345"
    assert zip5("12345-6789") == "12345"
    assert zip5("123456789") == "12345"
    assert zip5("K1A 0B1") is None
    assert zip5("1234") is None
    assert zip5("abcde") is None
    assert zip5("") is None
    assert zip5(None) is None

    # State
    assert normalize_state("Va") == "VA"
    assert normalize_state("TX") == "TX"
    assert normalize_state("Texas") is None
    assert normalize_state(None) is None

    # NAICS 2-digit
    assert naics_2digit("541511") == "54"
    assert naics_2digit("11") == "11"
    assert naics_2digit("5") is None
    assert naics_2digit("ABC") is None
    assert naics_2digit(None) is None

    print("scripts/_lib/sam_gov_normalize.py: all self-tests passed")

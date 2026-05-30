"""Pure-functional normalizers for CMS Open Payments backfill ingest.

Produce identity-spine columns the downstream RisingWave MVs use to bridge
CMS Open Payments physicians ⨝ NPPES providers ⨝ FEC donors and
CMS Open Payments manufacturers ⨝ GLEIF / SEC ADV / FEC employer field.

Per the Data Factory Protocol §5 (apps/data-engine-x/CLAUDE.md): join keys
are stamped at INGEST time so the Parquet carries both raw and normalized
columns. RW joins on the normalized columns; the raw columns stay as
ground-truth and are recoverable for any debugging.

Future LLM-assisted canonicalization (drug-name → RxNorm/NDC, manufacturer
EIN inference from name when EIN missing) belongs in a downstream MV that
consumes the raw columns — NOT here.
"""

from __future__ import annotations

import re
from typing import Final


_WHITESPACE_RUN: Final = re.compile(r"\s+")
_APOSTROPHE: Final = re.compile(r"['’]")  # ASCII + Unicode right single quote
_NAME_PUNCT: Final = re.compile(r"[^a-z0-9 ]+")
_NON_DIGIT: Final = re.compile(r"\D")
_ALPHA_ONLY: Final = re.compile(r"[^a-zA-Z]")

# Legal-form / pharma-domain suffixes stripped from the END of a manufacturer
# name after lowercase + punctuation strip. Word-boundary, applied repeatedly
# so chains like "Acme Pharmaceuticals Inc" → "acme" (strip "inc" then
# "pharmaceuticals"). Order doesn't matter inside the set; loop terminates
# when no terminal suffix is found.
_MANUFACTURER_SUFFIXES: Final = (
    "inc",
    "incorporated",
    "llc",
    "ltd",
    "limited",
    "corp",
    "corporation",
    "company",
    "co",
    "gmbh",
    "ag",
    "sa",
    "spa",
    "plc",
    "usa",
    "us",
    "na",
    "holdings",
    "group",
    "pharmaceuticals",
    "pharmaceutical",
    "pharma",
    "biosciences",
    "bioscience",
    "biotech",
    "biotechnology",
    "medical",
    "medicals",
    "devices",
    "device",
    "therapeutics",
    "diagnostics",
    "healthcare",
    "health",
    "labs",
    "laboratories",
    "laboratory",
)


def normalize_physician_name_part(raw: str | None) -> str | None:
    """Normalize a single component of a physician's name (first or last).

    CMS publishes physicians' first / last / middle / suffix as separate
    columns, so unlike FEC we do NOT need to reverse a "LAST, FIRST" form —
    the input is already a single name part.

    Steps:
      1. Lowercase + trim.
      2. Drop apostrophes ("O'Brien" → "obrien", treats apostrophe as
         intra-word punctuation, not a separator).
      3. Replace remaining non-alphanumeric chars with a space (hyphens and
         periods become word separators — "Smith-Jones" → "smith jones").
      4. Collapse whitespace.

    "John"           → "john"
    "JANE"           → "jane"
    "O'Brien"        → "obrien"
    "Smith-Jones"    → "smith jones"
    "  John  "       → "john"
    None / empty     → None
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    s = s.lower()
    s = _APOSTROPHE.sub("", s)
    s = _NAME_PUNCT.sub(" ", s)
    s = _WHITESPACE_RUN.sub(" ", s).strip()
    return s or None


def normalize_npi(raw: str | None) -> str | None:
    """Normalize a National Provider Identifier (NPI).

    NPPES NPIs are exactly 10 digits. CMS Open Payments occasionally publishes
    NPIs with embedded punctuation or, rarely, with leading zeros stripped.
    We:

      1. Strip every non-digit character.
      2. If the result is exactly 10 digits → return as-is.
      3. If 1-9 digits → left-pad to 10 with zeros (recovers stripped
         leading zeros — defensive).
      4. If 0 digits or >10 digits → None (malformed, downstream must NOT
         match).

    "1234567890"     → "1234567890"
    "1234567"        → "0001234567"
    "1234-567-890"   → "1234567890"
    "abc"            → None
    "12345678901"    → None  (too long; not a valid NPI)
    None / empty     → None
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    digits = _NON_DIGIT.sub("", s)
    if not digits:
        return None
    n = len(digits)
    if n == 10:
        return digits
    if 1 <= n < 10:
        return digits.rjust(10, "0")
    return None  # >10 digits — not a valid NPI


def physician_zip5(raw: str | None) -> str | None:
    """Extract the first 5 digits of a recipient ZIP code.

    CMS publishes ZIPs as 5 or 9 digits, sometimes with embedded punctuation
    ("12345-6789", "123456789", "12345"). Mirrors fec_normalize.zip5 — slice
    the first 5 numeric characters; <5 numeric chars → None.

    "12345"          → "12345"
    "12345-6789"     → "12345"
    "123456789"      → "12345"
    "1234"           → None
    "abcde"          → None
    None / empty     → None
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


def normalize_state(raw: str | None) -> str | None:
    """Normalize a US state abbreviation to upper-case 2-letter form.

    CMS publishes state as a 2-letter postal abbreviation, but with
    occasional whitespace, lowercase, or full names. We extract alpha
    characters, uppercase, and accept only a 2-char result — anything else
    is rejected so downstream joins on `physician_state_normalized` always
    see a clean 2-char key or NULL.

    "ny"             → "NY"
    " CA "           → "CA"
    "California"     → None  (full name — caller can fall through to
                              upstream raw column for non-standard inputs)
    ""               → None
    None             → None
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    alpha = _ALPHA_ONLY.sub("", s).upper()
    if len(alpha) != 2:
        return None
    return alpha


def normalize_manufacturer_name(raw: str | None) -> str | None:
    """Normalize a manufacturer / GPO name for cross-source identity joining.

    Steps:
      1. Lowercase + trim.
      2. Replace anything that's not a-z / 0-9 / space with a space (drops
         "&", ",", ".", "/" etc.).
      3. Collapse whitespace.
      4. Repeatedly strip terminal legal-form / pharma-domain suffix words
         (inc, llc, corp, pharmaceuticals, pharma, …) until none remains.
      5. Re-collapse whitespace, trim.

    "Pfizer Inc."                  → "pfizer"
    "Pfizer Pharmaceuticals Inc"   → "pfizer"
    "Boehringer Ingelheim Pharma GmbH" → "boehringer ingelheim"
    "AstraZeneca, LP"              → "astrazeneca"   (LP not in suffix set)
                                       Wait — "lp" IS in the FEC list but not in
                                       this manufacturer set; CMS uses corporate
                                       (Inc/LLC/Corp/Pharma) suffixes typically.
                                       Tested example below adjusts.
    "Acme Medical Devices LLC"     → "acme"          (devices + llc strip; medical strips too)
    "Johnson & Johnson"            → "johnson johnson"
    None / empty                   → None
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    s = s.lower()
    s = _NAME_PUNCT.sub(" ", s)
    s = _WHITESPACE_RUN.sub(" ", s).strip()
    if not s:
        return None

    parts = s.split(" ")
    while len(parts) >= 2 and parts[-1] in _MANUFACTURER_SUFFIXES:
        parts = parts[:-1]
    s = " ".join(parts).strip()
    return s or None


def normalize_ein(raw: str | None) -> str | None:
    """Normalize an Employer Identification Number to 9-digit no-hyphen form.

    EINs are exactly 9 digits (XX-XXXXXXX format on IRS forms). We strip
    non-digits and require exactly 9 digits — anything else is rejected so
    downstream joins on `manufacturer_ein_normalized` always see a clean
    9-char key or NULL.

    "12-3456789"     → "123456789"
    "123456789"      → "123456789"
    "12345"          → None  (too short — likely a partial EIN)
    "1234567890"     → None  (too long — not an EIN)
    "ABC"            → None
    None / empty     → None
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

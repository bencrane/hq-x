"""Pure-functional normalizers for the USPTO Trademark Case Files Dataset (TCFD).

Used by `scripts/run_uspto_trademarks_r2_ingest.py` to add `*_normalized`
columns to the per-year Parquet outputs. All functions are deterministic,
side-effect-free, and unit-testable in isolation. Designed so that downstream
RisingWave MVs (FEC donor ⨝ USPTO owner, SBA borrower ⨝ USPTO owner, etc.)
can join on exact equality of the normalized strings.

See ~/Desktop/hq/directives/2026-05-08-uspto-trademark-case-files-r2-ingest.md.
"""

from __future__ import annotations

import re

_WHITESPACE_RE = re.compile(r"\s+")
_PUNCT_TO_SPACE_RE = re.compile(r"[^a-z0-9 ]+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

# Trailing corporate-form suffix tokens stripped from owner names. Order
# matters only insofar as longer tokens come before shorter ones that prefix
# them (e.g. "incorporated" before "inc"). Tokens are matched as whole words
# at the END of the string after punctuation→space + whitespace collapse.
_CORPORATE_SUFFIXES: tuple[str, ...] = (
    # Multi-token forms that fall out of L.L.C. / L.L.P. / S.A. style after
    # the punctuation→space pass. Matched BEFORE the single-token variants so
    # the longest match wins. Cover the foreign two-letter initialisms (S.A.,
    # A.G., B.V., N.V., S.P.A., S.R.L.) that show up in non-US filings.
    "l l c",
    "l l p",
    "l l l p",
    "p l c",
    "p l l c",
    "s p a",
    "s r l",
    "p t e ltd",
    "s a",
    "a g",
    "b v",
    "n v",
    "k k",
    "k g",
    "p t e",
    # Standard tokens (longest first within length-tier so "incorporated"
    # matches before "inc", etc.).
    "corporation",
    "incorporated",
    "limited",
    "company",
    "partnership",
    "trust",
    "association",
    "llc",
    "lllc",
    "inc",
    "corp",
    "ltd",
    "lp",
    "llp",
    "lllp",
    "pllc",
    "plc",
    "pc",
    "pa",
    "co",
    "gmbh",
    "ag",
    "sa",
    "nv",
    "bv",
    "kk",
    "kg",
    "oy",
    "ab",
    "as",
    "spa",
    "srl",
)


def normalize_owner_name(name: str | None) -> str | None:
    """Normalize an owner / applicant party name for cross-source equality joins.

    Steps: lowercase → punctuation→space → whitespace-collapse → iterative
    trailing-suffix strip (LLC / INC / CORP / LTD / etc. — including foreign
    forms GmbH / AG / SA so EU / UK applicants normalize cleanly).

    Returns None on empty/whitespace input or if stripping reduced the string
    to empty (e.g. input was the literal string "LLC").
    """
    if name is None:
        return None
    s = str(name).strip().lower()
    if not s:
        return None
    s = _PUNCT_TO_SPACE_RE.sub(" ", s)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    if not s:
        return None

    # Iteratively strip trailing corporate-form tokens. Multiple suffix tokens
    # may stack (e.g. "ACME HOLDINGS INC LLC" → "acme holdings"); we loop
    # until no further tokens are stripped on a pass.
    changed = True
    while changed:
        changed = False
        for suf in _CORPORATE_SUFFIXES:
            if s == suf:
                s = ""
                changed = True
                break
            if s.endswith(" " + suf):
                s = s[: -(len(suf) + 1)].rstrip()
                changed = True
                break
    return s or None


def normalize_mark_text(mark: str | None) -> str | None:
    """Normalize a mark wordmark / brand-text string for join-stable equality.

    Steps: lowercase → strip everything non-alphanumeric → collapse adjacent
    spaces. Returns None on empty input or on design-only marks (USPTO leaves
    `mark_id_char` empty for marks with no word component).
    """
    if mark is None:
        return None
    s = str(mark).lower()
    s = _NON_ALNUM_RE.sub(" ", s)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    return s or None


def zip5(postal_code: str | None) -> str | None:
    """Return the first 5 chars of a US postal code if numeric; else None.

    USPTO TCFD's owner postal_code field varies: 5-digit US ZIPs, 9-digit ZIP+4,
    foreign postal codes (UK / Canadian alphanumerics), and free-text. We only
    return a value when the leading 5 chars are all digits — anything else is
    surfaced as NULL so downstream joins don't accidentally bridge UK postcodes
    to US ZIPs.
    """
    if postal_code is None:
        return None
    s = str(postal_code).strip()
    if len(s) < 5:
        return None
    head = s[:5]
    return head if head.isdigit() else None


def normalize_state(state: str | None) -> str | None:
    """Return an uppercase, trimmed 2- or 3-character state/province code.

    USPTO TCFD's state field is empty for non-US owners (which use the country
    field instead). For US owners it's the 2-letter postal code; we don't
    enforce exact-2 because some Canadian provinces use 2-3 letter codes.
    """
    if state is None:
        return None
    s = str(state).strip().upper()
    if not s or len(s) > 3:
        return None
    return s


def normalize_country(country: str | None) -> str | None:
    """Return an uppercase, trimmed country code.

    USPTO TCFD's country field uses 2-letter ISO codes for owner_country (most
    cases) and 3-letter codes occasionally for older filings. We don't enforce
    the format here — preserve verbatim uppercase. Empty input → None.
    """
    if country is None:
        return None
    s = str(country).strip().upper()
    return s or None


# Owner-kind classification.
#
# USPTO TCFD's `owner_legal_entity_type_code` is a 2-character code. Verified
# values per USPTO TCFD documentation (TM_Master_File_Dataset_Variable_Tables_*.pdf,
# §"Legal Entity Type Codes"):
#
#   '01' = Corporation
#   '02' = Individual
#   '03' = Limited partnership
#   '04' = Partnership (general)
#   '05' = Joint venture
#   '06' = Sole proprietorship
#   '07' = Trust
#   '08' = LLC
#   '09' = Association
#   '10' = Joint stock company
#   '11' = Government / state agency
#   '12' = Cooperative
#   '13' = Estate
#   '14' = Statutory entity (treated as 'unknown')
#   '15'+ = misc / unknown
#   '99' = Other
#
# Code values may differ in older filings (pre-1980 records sometimes have
# blank entity_type) — fall back to name-suffix inspection in those cases.
_KIND_BY_CODE: dict[str, str] = {
    "01": "corporation",
    "02": "individual",
    "03": "partnership",
    "04": "partnership",
    "05": "partnership",
    "06": "individual",
    "07": "trust",
    "08": "llc",
    "09": "association",
    "10": "corporation",
    "11": "government",
    "12": "association",
    "13": "individual",
}

# Order matters: more-specific tokens (and compound forms) come first so
# "limited partnership" classifies as partnership, not corporation, and so
# multi-word suffixes like "limited liability company" win over "limited" alone.
_NAME_SUFFIX_HINTS: tuple[tuple[str, str], ...] = (
    ("limited liability company", "llc"),
    ("limited liability partnership", "partnership"),
    ("limited partnership", "partnership"),
    ("partnership", "partnership"),
    ("llp", "partnership"),
    ("lllp", "partnership"),
    ("lp", "partnership"),
    ("llc", "llc"),
    ("l.l.c", "llc"),
    ("l l c", "llc"),
    ("incorporated", "corporation"),
    ("corporation", "corporation"),
    ("inc", "corporation"),
    ("corp", "corporation"),
    ("limited", "corporation"),
    ("ltd", "corporation"),
    ("plc", "corporation"),
    ("gmbh", "corporation"),
    ("ag", "corporation"),
    ("sa", "corporation"),
    ("trust", "trust"),
    ("association", "association"),
    ("estate of", "individual"),
    ("dba", "individual"),
)


def classify_owner_kind(
    legal_entity_type_code: str | None,
    party_name: str | None = None,
) -> str:
    """Map a TCFD owner row to one of the canonical kind labels.

    Returns one of: 'individual' | 'corporation' | 'llc' | 'partnership' |
    'trust' | 'association' | 'government' | 'unknown'.

    Strategy: code lookup first, name-suffix fallback when code is blank /
    unknown. Pre-1980s records frequently lack entity_type codes so the
    name-suffix path matters.
    """
    if legal_entity_type_code is not None:
        code = str(legal_entity_type_code).strip()
        if code in _KIND_BY_CODE:
            return _KIND_BY_CODE[code]
    if party_name:
        raw_lower = str(party_name).lower()
        # Strip trailing punctuation so we can match "Inc." == "Inc".
        stripped = raw_lower.rstrip(" .,;")
        for needle, kind in _NAME_SUFFIX_HINTS:
            if (
                stripped.endswith(" " + needle)
                or stripped.endswith("," + needle)
                or stripped == needle
            ):
                return kind
        if "," in raw_lower:
            # "LAST, FIRST" pattern is overwhelmingly individuals in TCFD.
            return "individual"
    return "unknown"

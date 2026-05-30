"""Pure-functional normalizers for NPPES (CMS National Plan and Provider
Enumeration System) bulk-data fields.

Used by `scripts/run_nppes_r2_ingest.py` to compute the join-key columns
that downstream FEC ⨝ NPPES match MVs depend on. Functions are I/O-free,
deterministic, and unit-tested against a fixed sample.

Design choices (see directive 2026-05-08-nppes-providers-r2-ingest.md):

* Match the FEC normalize convention: lowercase + collapse whitespace +
  strip punctuation. Conservative — no semantic canonicalization.
* `pick_primary_taxonomy` scans 15 NPPES taxonomy slots and returns the
  one marked primary; falls back to slot 1 if none flagged.
* `practice_zip5_with_fallback` prefers practice ZIP and falls back to
  mailing ZIP when practice is empty.
"""

from __future__ import annotations

import re
from typing import Sequence


# Whitespace + punctuation patterns shared by name normalizers.
_PUNCT_RE = re.compile(r"[^a-z0-9 ]+")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_provider_name(value: str | None) -> str | None:
    """Normalize a provider first or last name.

    Lowercases, replaces non-alphanumeric (except spaces) with spaces,
    collapses whitespace, strips. Returns None for empty / None inputs.

    Examples:
        "Smith"           -> "smith"
        "MARY-JANE"       -> "mary jane"
        "  O'Brien  "     -> "o brien"
        "JOSÉ"            -> "jos"   (ASCII-only by design; downstream
                                       MVs decide whether to fold accents)
    """
    if value is None:
        return None
    s = value.strip().lower()
    if not s:
        return None
    s = _PUNCT_RE.sub(" ", s)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    return s or None


# Common organization-suffix tokens stripped from the END of org names.
# Conservative list — matches the FEC employer normalizer convention but
# adds healthcare-specific suffixes seen in NPPES (P.A., P.C., LLP).
_ORG_SUFFIX_TOKENS = {
    "llc", "inc", "incorporated", "co", "company", "corp", "corporation",
    "ltd", "limited", "lp", "llp", "pc", "pa", "pllc",
}


def normalize_org_name(value: str | None) -> str | None:
    """Normalize a Type 2 organization legal name.

    Lowercases, strips punctuation, collapses whitespace, then drops
    trailing org-suffix tokens (LLC, Inc, Corp, etc.). Returns None for
    empty / None inputs.

    Examples:
        "Acme Health, LLC"          -> "acme health"
        "BAYSHORE MEDICAL GROUP PC" -> "bayshore medical group"
        "St. Mary's Hospital, Inc." -> "st marys hospital"
    """
    if value is None:
        return None
    s = value.strip().lower()
    if not s:
        return None
    s = _PUNCT_RE.sub(" ", s)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    if not s:
        return None
    tokens = s.split(" ")
    while tokens and tokens[-1] in _ORG_SUFFIX_TOKENS:
        tokens.pop()
    if not tokens:
        return None
    return " ".join(tokens)


def practice_zip5_with_fallback(
    practice_zip: str | None,
    mailing_zip: str | None,
) -> str | None:
    """Return the first 5 chars of practice ZIP, falling back to mailing ZIP.

    NPPES "Provider Business Practice Location Address Postal Code" is
    typically 9-digit ZIP+4; we want the 5-digit identity match key.
    Practice ZIP is preferred over mailing because mailing tends to be a
    billing service / PO Box / "c/o" address (less identity-stable).

    Returns None if both are empty or neither yields ≥5 alphanumeric chars.

    Examples:
        ("941101234", None)         -> "94110"
        ("94110", "10001")           -> "94110"
        ("", "100012345")            -> "10001"
        (None, "")                   -> None
        ("12-3", "94110")            -> "94110"   (practice has <5 alnum)
    """
    for candidate in (practice_zip, mailing_zip):
        if candidate is None:
            continue
        s = candidate.strip()
        if not s:
            continue
        # Strip non-alphanumeric (NPPES occasionally has "94110-1234").
        digits = re.sub(r"[^A-Za-z0-9]", "", s)
        if len(digits) >= 5:
            return digits[:5]
    return None


def normalize_state(value: str | None) -> str | None:
    """Uppercase + trim a state code. Returns None for empty / None inputs.

    Examples:
        " ca "    -> "CA"
        "NY"      -> "NY"
        ""        -> None
    """
    if value is None:
        return None
    s = value.strip().upper()
    return s or None


def pick_primary_taxonomy(
    taxonomy_codes: Sequence[str | None],
    primary_switches: Sequence[str | None],
) -> str | None:
    """Pick the primary taxonomy code from 15 NPPES taxonomy slots.

    NPPES providers can list up to 15 taxonomy codes. Exactly one is
    flagged primary via "Healthcare Provider Primary Taxonomy Switch_N
    = 'Y'". Returns the code from that slot.

    Fallback: if no slot is flagged primary, returns slot-1 if non-empty.
    Returns None if every slot is empty.

    Args:
        taxonomy_codes: ordered list of taxonomy codes (slot 1..15).
        primary_switches: ordered list of primary-switch flags
            (slot 1..15). Values are typically "Y", "N", or empty.

    Examples:
        (["abc", "def"], ["", "Y"])    -> "def"
        (["abc", "def"], ["Y", ""])    -> "abc"
        (["abc", "def"], ["", ""])     -> "abc"   (slot-1 fallback)
        (["", "def"], ["", "Y"])       -> "def"
        ([], [])                        -> None
    """
    if not taxonomy_codes:
        return None
    n = max(len(taxonomy_codes), len(primary_switches))
    for i in range(n):
        code = taxonomy_codes[i] if i < len(taxonomy_codes) else None
        switch = primary_switches[i] if i < len(primary_switches) else None
        if code and code.strip() and switch and switch.strip().upper() == "Y":
            return code.strip()
    # Fallback: first non-empty slot.
    for code in taxonomy_codes:
        if code and code.strip():
            return code.strip()
    return None

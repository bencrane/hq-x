"""Pure-functional normalizers for UCC-1 ingest.

Identity-spine columns the downstream RW MVs will join on:

  debtor side
    - debtor_name_normalized       lowercased + corp-suffix-stripped
    - debtor_zip5                  first 5 numeric chars of postal code
    - debtor_state_normalized      uppercased 2-letter state of debtor address

  secured-party side
    - secured_party_name_normalized
    - secured_party_zip5
    - secured_party_state_normalized

State-of-FILING (`ucc_state` partition column) is distinct from state-of-debtor
ADDRESS (`debtor_state_normalized`). A UCC filed in CT may have a debtor in NY.

These functions are pure (no I/O), deterministic, and unit-tested. They mirror
the FEC normalizer's structure (`_lib/fec_normalize.py`).

Future LLM-assisted canonicalization (collateral-description classification,
secured-party identity disambiguation against FDIC bank entities, etc.)
belongs in a downstream MV that consumes the raw + normalized columns — NOT
here.
"""

from __future__ import annotations

import re
from typing import Final


_WHITESPACE_RUN: Final = re.compile(r"\s+")
_NAME_PUNCT: Final = re.compile(r"[,.&'\"]")
# Collapse "N.A." / "N. A." / "N A" bank-suffix variants into the bare token
# "na" so the downstream suffix-stripper handles them consistently with "NA".
# Boundaries are word-boundaries so we don't eat substrings like "Diana".
_NA_SUFFIX_VARIANTS: Final = re.compile(
    r"\bn\s*\.\s*a\s*\.?\B|\bn\.a\.?\b", re.IGNORECASE,
)

# Common org suffixes stripped from the END of an org-name string after
# punctuation normalization. Matches the FEC normalizer list, extended for
# UCC-typical secured-party names (banks, credit unions, financial-services).
# Word-boundary, case-insensitive. We only strip terminal suffixes — "ACME LLC
# HOLDINGS LLC" keeps the inner "LLC" untouched.
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
)


_STATE_CODES: Final = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC", "PR", "VI", "GU", "AS", "MP",
})


def normalize_party_name(raw: str | None) -> str | None:
    """Normalize a debtor or secured-party name for cross-source identity joining.

    UCC names come in two shapes:

      - Organization: "ACME CORP", "Wells Fargo Bank, N.A.", "JPMorgan Chase Bank, NA"
      - Individual:   "Smith, John A." OR "John A. Smith" — varies by state

    Steps:
      1. Lowercase + trim.
      2. Reverse on first comma (the FEC convention) ONLY when the second token
         is short — heuristic for "LAST, FIRST" individual names. Don't reverse
         org names like "Wells Fargo Bank, N.A." where the post-comma is a
         suffix, not a first name.
      3. Replace ".", ",", "&", "'", "\"" with spaces.
      4. Collapse whitespace runs.
      5. Strip ONE trailing org suffix if present.

    Examples:
      "ACME CORP"                    → "acme"
      "ACME, INC."                   → "acme"
      "Wells Fargo Bank, N.A."       → "wells fargo bank"
      "JPMorgan Chase Bank, NA"      → "jpmorgan chase bank"
      "BURT CHEVROLET INC"           → "burt chevrolet"
      "SMITH, JOHN A."               → "john a smith"
      None / empty                   → None
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    s = s.lower()

    # Collapse N.A. variants before the comma-reversal heuristic so the tail
    # detection treats "Bank, N.A." identically to "Bank, NA".
    s = _NA_SUFFIX_VARIANTS.sub("na", s)

    # Heuristic comma-reverse for "LAST, FIRST" individual names. Two guards
    # to avoid reordering org names that happen to have a comma:
    #   1. The head (before comma) must be a SINGLE word — multi-word heads
    #      ("KUBOTA CREDIT CORPORATION") are clearly orgs, not lastnames.
    #   2. The tail's first word must not be a known suffix marker (LLC, INC,
    #      NA, etc.) — those belong to the org and don't reorder.
    if "," in s:
        head, _, tail = s.partition(",")
        head_words = head.strip().split()
        tail_clean = tail.strip().rstrip(".").lower()
        tail_first_word = tail_clean.split(" ", 1)[0] if tail_clean else ""
        if (
            len(head_words) == 1
            and tail_first_word
            and tail_first_word.replace(".", "") not in _ORG_SUFFIXES
            and tail_first_word not in {"na", "n.a"}
        ):
            s = f"{tail.strip()} {head.strip()}"

    s = _NAME_PUNCT.sub(" ", s)
    s = _WHITESPACE_RUN.sub(" ", s).strip()
    if not s:
        return None

    parts = s.split(" ")
    if len(parts) >= 2 and parts[-1] in _ORG_SUFFIXES:
        parts = parts[:-1]
        s = " ".join(parts).strip()

    return s or None


def zip5(raw: str | None) -> str | None:
    """Extract a 5-digit ZIP from a postal-code field.

    Postal codes appear as 5 digits, 9 digits, or 5+4 with hyphen ("80110",
    "80110-1234", "801101234"). Some non-US addresses appear as alphanumeric
    Canadian postcodes — those return None.

    "12345"        → "12345"
    "12345-6789"   → "12345"
    "123456789"    → "12345"
    "K1A 0B1"      → None    (Canadian — fewer than 5 numeric chars)
    "1234"         → None    (too short)
    None / empty   → None
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


def normalize_state_code(raw: str | None) -> str | None:
    """Normalize a state field to a 2-letter US/territory code.

    Accepts:
      - 2-letter codes: "CO" / "co" → "CO"
      - 2-letter codes with whitespace: " CO " → "CO"
    Rejects:
      - Full names ("Colorado") — the source data already uses codes; we don't
        need a name-to-code lookup at ingest. If the source ever publishes full
        names in the state column, the per-state schema mapping should
        translate before calling this normalizer.
      - Unknown codes (validates against ISO/USPS list).

    "CO"           → "CO"
    "co"           → "CO"
    "  CT  "       → "CT"
    "Colorado"     → None    (full name not accepted)
    "ZZ"           → None    (not a known code)
    None / empty   → None
    """
    if raw is None:
        return None
    s = raw.strip().upper()
    if len(s) != 2:
        return None
    return s if s in _STATE_CODES else None


def normalize_collateral_description(raw: str | None) -> str | None:
    """Lightweight cleanup of the free-text collateral-description field.

    Preserves the substance of the description verbatim — just lowercases and
    collapses whitespace. Deeper canonicalization (classifying collateral as
    equipment / inventory / accounts-receivable / IP / vehicle / livestock,
    extracting VIN / serial numbers, etc.) is a downstream LLM-assisted MV
    concern that consumes this field; not done here.

    "ALL LIVESTOCK"            → "all livestock"
    "  ALL EQUIPMENT  "        → "all equipment"
    "Inventory; A/R"           → "inventory; a/r"
    None / empty               → None
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    s = s.lower()
    s = _WHITESPACE_RUN.sub(" ", s).strip()
    return s or None

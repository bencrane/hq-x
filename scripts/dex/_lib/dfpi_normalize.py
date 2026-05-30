r"""Python port of the DFPI franchise-name normalizer + document-kind classifier.

Bit-identical reproductions of two SQL functions defined in
`supabase/migrations/20260501042255_dfpi_franchise_filings_and_audit.sql`:

  1. entities.normalize_franchise_name(text)
  2. entities.classify_dfpi_document_kind(text)

Both sides of any future FEC R2 ⨝ DFPI R2 join MUST produce identical
normalized strings — parity is gated by `tests/test_dfpi_normalize_parity.py`,
which queries the live SQL function and asserts equality on a 30-pair sample.

Notes on regex translation:

- PostgreSQL `\m` (start-of-word) and `\M` (end-of-word) are zero-width
  word-boundary assertions. Python's `\b` is symmetric (matches both
  start- and end-of-word boundaries) and produces identical results for
  ASCII inputs, which is all DFPI ships.

- PostgreSQL `regexp_replace(s, p, r)` (no flags) replaces only the first
  match. `'g'` flag means global (all matches). Python `re.sub` is global
  by default; pass `count=1` to mimic single-replacement.

- PostgreSQL `~*` is case-insensitive partial match; Python equivalent is
  `re.search(p, s, re.IGNORECASE)`.
"""

from __future__ import annotations

import re

__all__ = [
    "normalize_franchise_name",
    "classify_document_kind",
]


# --------------------------------------------------------------------------- #
# Suffix tokens — append-only; calibrated against recon Gate 4 (30/30) sample.
# Order matches the SQL function for verification.
# --------------------------------------------------------------------------- #

_SUFFIX_TOKENS: frozenset[str] = frozenset({
    # US legal entity forms
    "llc", "lllc", "l3c", "inc", "corp", "corporation", "company", "co",
    "ltd", "lp", "lllp", "pllc", "pc", "plc", "incorporated", "limited",
    "spe", "spv",
    # Foreign legal entity forms
    "bv", "bvba", "nv", "sa", "gmbh", "ag", "pty", "oy", "ab", "kk",
    "sarl", "sas", "srl", "pte", "sdn", "bhd",
    # Franchise-domain noise
    "franchising", "franchise", "franchises", "franchisor", "franchisee",
    "systems", "system",
    "enterprises", "enterprise",
    "group",
    "international", "intl",
    "holdings", "holding",
    "usa", "america", "worldwide", "global",
    "brands", "brand",
})


# --------------------------------------------------------------------------- #
# Compiled regexes — compile-once for hot-loop performance.
# --------------------------------------------------------------------------- #

# 1. Strip parentheticals (global).
_RE_PARENS = re.compile(r"\([^)]*\)")

# 2. Strip f/k/a / d/b/a / a/k/a / dba / aka tail (case-insensitive, first match).
#    `\b` mirrors PG's `\m`+`\M` for ASCII inputs.
_RE_DBA_TAIL = re.compile(
    r"\b(f\s*/?k\s*/?a|d\s*/?b\s*/?a|a\s*/?k\s*/?a|dba|aka)\b.*$",
    re.IGNORECASE,
)

# 3. Strip "Brand - Subbrand" tail (case-sensitive, first match).
_RE_DASH_TAIL = re.compile(r"\s+-\s+.*$")

# 4. Collapse foreign legal forms (B.V., N.V., S.A.) → bv/nv/sa.
_RE_FOREIGN_LEGAL = re.compile(
    r"[,\s]+([bns])\s*\.\s*([vaz])\s*\.?\s*$",
    re.IGNORECASE,
)

# 5. Replace non-alphanumeric (ASCII) with single space (global).
_RE_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")

# 6. Whitespace splitter for the final token-filter pass.
_RE_WHITESPACE = re.compile(r"\s+")


def normalize_franchise_name(name: str | None) -> str:
    """Port of entities.normalize_franchise_name(text).

    Pipeline (innermost first, as in the SQL CTE):
      1. COALESCE(name, '')
      2. Strip parentheticals (global).
      3. Strip f/k/a / d/b/a / a/k/a tail (first match, case-insensitive).
      4. Strip ` - subbrand` tail (first match).
      5. Collapse foreign legal forms (B.V./N.V./S.A.) → 2-letter form.
      6. Replace non-alphanumeric with space (global).
      7. Lowercase.
      8. Split on whitespace.
      9. Drop empties + suffix tokens.
      10. Concatenate with no separator.

    Returns the empty string for None / empty / suffix-only inputs.
    """
    s = name or ""

    # Step 2: parentheticals (global).
    s = _RE_PARENS.sub(" ", s)

    # Step 3: dba/fka/aka tail (first match).
    s = _RE_DBA_TAIL.sub(" ", s, count=1)

    # Step 4: " - subbrand" tail (first match).
    s = _RE_DASH_TAIL.sub("", s, count=1)

    # Step 5: collapse foreign legal forms (first match).
    s = _RE_FOREIGN_LEGAL.sub(r" \1\2", s, count=1)

    # Step 6: non-alphanumeric → space (global).
    s = _RE_NON_ALNUM.sub(" ", s)

    # Step 7: lowercase.
    s = s.lower()

    # Steps 8-10: split, filter, join with no separator.
    tokens = (t for t in _RE_WHITESPACE.split(s.strip()) if t and t not in _SUFFIX_TOKENS)
    return "".join(tokens)


# --------------------------------------------------------------------------- #
# Document-kind classifier.
# --------------------------------------------------------------------------- #

# Patterns are anchored as PG `~*` partial-match with `re.IGNORECASE` semantics.
# Order matters: most-specific first (per SQL function CASE order).
_DOC_KIND_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bexhibit", re.IGNORECASE), "exhibit"),
    (re.compile(r"internet\s+ad(vertis(ing|ement)?)?\s+exemption", re.IGNORECASE),
     "internet_ad_exemption"),
    (re.compile(r"franchise\s+disclosure\s+document", re.IGNORECASE), "fdd"),
    (re.compile(r"\bfdd\b", re.IGNORECASE), "fdd"),
    (re.compile(r"\btransmittal", re.IGNORECASE), "transmittal"),
    (re.compile(r"service\s+of\s+process|consent\s+to\s+service", re.IGNORECASE),
     "consent_to_service"),
    (re.compile(
        r"corp(orate)?\s+acknowledg|acknowledg.*verification|"
        r"verification\s+with\s+acknowledg|"
        r"certification.{0,5}(verification|form)|verification\s+page",
        re.IGNORECASE,
    ), "corp_acknowledgement"),
    (re.compile(
        r"auditor.{0,4}s?\s+consent|financial\s+statement|audited\s+financial",
        re.IGNORECASE,
    ), "financials"),
    (re.compile(
        r"franchise\s+seller|seller\s+disclosure|sales\s+agent\s+disclosure",
        re.IGNORECASE,
    ), "franchise_seller"),
    (re.compile(
        r"costs?\s+and\s+source\s+of\s+funds|source\s+of\s+funds",
        re.IGNORECASE,
    ), "costs_and_funds"),
    (re.compile(r"\badvertis", re.IGNORECASE), "advertisement"),
)


def classify_document_kind(title: str | None) -> str:
    """Port of entities.classify_dfpi_document_kind(text).

    Returns one of:
      fdd | exhibit | transmittal | consent_to_service | corp_acknowledgement |
      financials | franchise_seller | internet_ad_exemption | costs_and_funds |
      advertisement | other

    Rule order matters — "FDD Exhibit B" classifies as 'exhibit', not 'fdd'.
    """
    if title is None or not title.strip():
        return "other"
    for pattern, kind in _DOC_KIND_RULES:
        if pattern.search(title):
            return kind
    return "other"

"""Shared entity-name normalizer for cross-source name+state bridges.

Lifted from the SBA EIDL/PPP `borrower_name_normalized` rule (validated at
44.44% PPP↔EIDL overlap — proves the rule produces a stable join key across
COVID-era SBA programs). Now exposed as the canonical normalizer for every
name+state bridge: FEC × SBA (via FEC `employer`), DOL Form 5500 × SBA (via
plan-sponsor name), SAM × SBA, GLEIF × any-named-entity, NPPES × SAM, etc.

Per L31 (lessons learned, directive 2026-05-09): this module exposes
`__version__`. Bumping the constant forces a version bump on any consumer's
`match_method_versions` row in `ops.match_methods`. Any output drift between
versions is provenance-tracked through the bridge_run → match_method_version
chain.

Per L33: generic non-entity strings (sole-prop self-references, "owner",
"retired", etc.) return None. These appear thousands of times in FEC's
`employer` field and produce massive false-positive cardinality if matched
naively. The blacklist is documented + versioned alongside the rule.

Normalization rule:
  1. Lowercase + trim.
  2. Strip corp-form suffixes (LLC, Inc, Corp, …) wherever they appear.
  3. Replace non-word punctuation with spaces.
  4. Collapse whitespace.
  5. Reject empty / single-char / generic-string results — return None.

Examples:
  "ACME TRUCKING LLC"      → "acme trucking"
  "Acme Trucking, Inc."    → "acme trucking"
  "TimberPine, Inc DBA TimberPine, Inc" → "timberpine dba timberpine"
  "self-employed"          → None
  "RETIRED"                → None
  ""                       → None
  None                     → None

DuckDB exposure (per L34): bridge generators register this as a UDF via
`con.create_function('py_normalize_entity', normalize_entity_name,
[VARCHAR], VARCHAR)` so SQL applies the SAME logic Python uses elsewhere.
Avoids the "DuckDB SQL approximation of the Python rule" drift trap.
"""

from __future__ import annotations

import re
from typing import Final


__version__: Final = "1.0.0"


# Corp-form suffixes stripped from entity names (any position). Mirrors the
# SBA PPP `_SUFFIX_TOKENS` set EXACTLY so that joins on the normalized name
# match the production PPP `borrower_name_normalized` column ≥99% (parity
# gate per L32). Tokens like "holdings", "group", "associates" are LEGAL
# parts of business names (e.g. "Plymouth Hotel Group LLC" — "group" is
# semantically meaningful, not a corp-form suffix), so they are NOT stripped.
_SUFFIX_TOKENS: Final = (
    "incorporated", "corporation", "company", "limited",
    "pllc", "llp", "lp", "llc", "inc", "ltd", "corp", "co", "pa",
)
_SUFFIX_RE: Final = re.compile(
    r"\b(?:" + "|".join(re.escape(t) for t in _SUFFIX_TOKENS) + r")\b\.?",
    re.IGNORECASE,
)
_PUNCT_RE: Final = re.compile(r"[^\w\s]+")
_WS_RE: Final = re.compile(r"\s+")


# Per L33: generic non-entity strings. These appear in FEC's employer field
# (and analogous columns elsewhere) thousands of times; matching them produces
# false-positive cardinality. The blacklist matches AFTER normalization (so
# "Self-Employed" and "self employed" both normalize to "self employed" and
# get rejected together).
GENERIC_NON_ENTITY_STRINGS: Final[frozenset[str]] = frozenset({
    # Sole-prop self-references
    "self employed",
    "selfemployed",
    "self",
    "owner",
    "owner operator",
    "sole proprietor",
    "proprietor",
    # Employment-status words
    "retired",
    "unemployed",
    "homemaker",
    "stay at home",
    "stay at home mom",
    "stay at home parent",
    "student",
    "disabled",
    # Catch-all / generic
    "n a",         # "N/A" → "n a" after punct strip
    "na",
    "none",
    "not applicable",
    "not employed",
    "various",
    "private",
    "information requested",
    "requested",
    # Common throwaway employer placeholders
    "info requested",
    "best efforts",
    "unknown",
})


# Empty-string / sentinel inputs that should be rejected before normalization.
# (Mirrors PPP `_EMPTY_SENTINELS`.)
_EMPTY_SENTINELS: Final[frozenset[str]] = frozenset({
    "", "Unknown", "Unknown/NotStated", "Unanswered", "N/A", "NA",
})


def normalize_entity_name(raw: str | None) -> str | None:
    """Normalize an entity name (legal-entity / employer / plan-sponsor) for
    cross-source name+state joining.

    Returns None for inputs that should not match anything:
      - None / empty / sentinel ("Unknown", "N/A", etc.)
      - Generic non-entity strings (per `GENERIC_NON_ENTITY_STRINGS`)
      - Single-character results after suffix-strip + punct-strip

    See module docstring for the full rule.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if s in _EMPTY_SENTINELS:
        return None
    s = s.lower()
    if not s:
        return None
    s = _SUFFIX_RE.sub(" ", s)
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    if not s:
        return None
    if len(s) < 2:
        # single-character results are too generic to match safely
        return None
    if s in GENERIC_NON_ENTITY_STRINGS:
        return None
    return s

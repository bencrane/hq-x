"""Pure-functional normalizer for GLEIF Level-1 LEI Legal Names.

Used by ``scripts/run_gleif_r2_ingest.py`` to compute the
``legal_name_normalized`` column that downstream identity-bridge MVs join
on. Functions are I/O-free, deterministic, and unit-tested against a
fixed sample.

Pipeline (matches the DFPI normalizer's posture, with a more general
international legal-form suffix list per the GLEIF directive):

  1. Strip parentheticals.
  2. Strip f/k/a / d/b/a / a/k/a / dba / aka tails.
  3. Strip ``" - subbrand"`` tail.
  4. Collapse 2-letter foreign forms with embedded periods (e.g. ``B.V.``
     -> ``bv``, ``S.A.`` -> ``sa``) at end-of-string.
  5. Replace non-alphanumeric (ASCII) with single space.
  6. Lowercase.
  7. Split on whitespace; drop empties + suffix tokens.
  8. Concatenate with no separator.

Empty / suffix-only inputs return the empty string. Returns ``str``
(never ``None``) so the caller can apply ``NULLIF(..., '')`` when
writing to Parquet. Keeping the return shape identical to DFPI's
``normalize_franchise_name`` lets downstream MVs use the same null
posture.
"""

from __future__ import annotations

import re

__all__ = ["normalize_legal_name"]


# International legal-form suffix tokens. Conservative — only forms that
# are unambiguously legal-entity markers, not industry / geographic noise.
# Comparison is case-insensitive (tokens are lowercased before lookup).
_SUFFIX_TOKENS: frozenset[str] = frozenset({
    # US / English
    "llc", "lllc", "l3c", "inc", "incorporated", "corp", "corporation",
    "co", "company", "ltd", "limited", "lp", "lllp", "llp",
    "pllc", "pc", "plc", "pa", "ulc",
    # Germany / Austria / Switzerland (German-speaking)
    "gmbh", "ag", "kg", "ohg", "eg", "se", "kgaa", "mbh",
    # France / Belgium / Luxembourg (French)
    "sa", "sas", "sasu", "sarl", "scs", "sca", "eurl", "snc", "gie",
    "sci", "scop", "scic", "selas", "selarl", "sprl", "bvba",
    # Spain / Latin America (Spanish)
    "sl", "sll", "slne", "slu", "scra", "slp", "sapi", "sab",
    # Italy
    "spa", "srl", "sapa", "scpa", "scrl", "snc", "sas",
    # Netherlands / Dutch-speaking Belgium
    "bv", "nv", "cv", "vof", "cvba",
    # Nordic — Sweden / Finland / Norway / Denmark / Iceland
    "ab", "hb", "kb",
    "as", "asa", "ans", "ks", "is", "ps",
    "aps", "ks",
    "oy", "oyj", "ky",
    "ehf", "hf", "ohf", "sf",
    # UK / Ireland (most overlap with US — already covered above)
    "cic", "cio",
    # Eastern Europe — Poland / Czech / Slovakia / Hungary / Russia
    "spzoo", "sp",
    "sro", "as", "ks", "vos",
    "kft", "bt", "rt", "zrt", "nyrt",
    "ooo", "oao", "zao", "pao", "ao",
    # Turkey
    "ti", "as",
    # Greece
    "ae", "epe", "oe", "ee", "mepe",
    # Iberia / Brazil — Portuguese
    "lda", "ltda", "sgps",
    # Asia — Japan / Korea / China / Singapore / Malaysia / India / HK
    "kk", "gk", "kabushiki",
    "co",
    "pte", "sdn", "bhd",
    "pvt", "private",
    # Australia / NZ / South Africa
    "pty", "cc",
    # Canada — same as US (inc, corp, ltd, ulc) plus
    "cie", "cee",
    # Israel
    "btm",
    # Mexico / specifically encountered LEI examples
    "sapi", "cv",
    # Other / catch-all corporate
    "limitada", "limitee", "limitee",
})


# 1. Strip parentheticals (global).
_RE_PARENS = re.compile(r"\([^)]*\)")

# 2. Strip f/k/a / d/b/a / a/k/a / dba / aka tail (case-insensitive,
#    first match). Mirrors the DFPI normalizer.
_RE_DBA_TAIL = re.compile(
    r"\b(f\s*/?k\s*/?a|d\s*/?b\s*/?a|a\s*/?k\s*/?a|dba|aka)\b.*$",
    re.IGNORECASE,
)

# 3. Strip "Brand - Subbrand" tail.
_RE_DASH_TAIL = re.compile(r"\s+-\s+.*$")

# 4. Collapse trailing dot-separated initials into a single token so they
#    can be matched against ``_SUFFIX_TOKENS``. Covers 2-4 letter forms:
#      "S.A."     -> "sa"
#      "S.p.A."   -> "spa"
#      "S.A.S."   -> "sas"
#      "B.V."     -> "bv"
#    Slash-separated forms (Danish "A/S") use a parallel pattern. Both
#    require a leading comma or whitespace so embedded patterns like
#    "U.S." inside a name aren't collapsed.
_RE_DOTTED_INITIALS = re.compile(
    r"(?:^|[,\s])\s*([A-Za-z](?:\s*\.\s*[A-Za-z]){1,3}\s*\.?)\s*$",
)
_RE_SLASH_INITIALS = re.compile(
    r"(?:^|[,\s])\s*([A-Za-z]\s*/\s*[A-Za-z])\s*$",
)


def _collapse_trailing_initials(s: str) -> str:
    """Replace trailing dotted/slashed single-letter sequences with a
    space-prefixed concatenation of the letters (e.g. "S.p.A." -> " spa").
    """
    m = _RE_DOTTED_INITIALS.search(s)
    if m:
        letters = re.sub(r"[^A-Za-z]", "", m.group(1))
        return s[: m.start()] + " " + letters
    m = _RE_SLASH_INITIALS.search(s)
    if m:
        letters = re.sub(r"[^A-Za-z]", "", m.group(1))
        return s[: m.start()] + " " + letters
    return s

# 5. Replace non-word characters (Unicode-aware: any script's letters and
#    digits are kept) with a single space. ``\w`` in Python 3 ``re`` is
#    Unicode by default for ``str`` patterns, so CJK / Cyrillic / Arabic
#    legal names retain their characters and end up with a non-empty
#    normalized form. ``[\W_]+`` catches punctuation, whitespace, and
#    underscore.
_RE_NON_ALNUM = re.compile(r"[\W_]+")

# 6. Whitespace splitter.
_RE_WHITESPACE = re.compile(r"\s+")


def normalize_legal_name(name: str | None) -> str:
    """Normalize a GLEIF Legal Name to the join-key form.

    Examples:
        "AFRINVEST SECURITIES LIMITED"        -> "afrinvestsecurities"
        "Acme Health, LLC"                    -> "acmehealth"
        "Stanbic IBTC Holdings PLC"           -> "stanbicibtc"
        "Volkswagen AG"                       -> "volkswagen"
        "Deutsche Bank AG"                    -> "deutschebank"
        "Banco Santander, S.A."               -> "bancosantander"
        "Heineken N.V."                       -> "heineken"
        "Toyota Motor Corporation"            -> "toyotamotor"
        "Phillips 66 Company"                 -> "phillips66"
        ""                                    -> ""
        None                                  -> ""

    Returns the empty string for None / empty / suffix-only inputs.
    """
    s = name or ""

    # Step 2: parentheticals.
    s = _RE_PARENS.sub(" ", s)

    # Step 3: dba/fka/aka tail.
    s = _RE_DBA_TAIL.sub(" ", s, count=1)

    # Step 4: " - subbrand" tail.
    s = _RE_DASH_TAIL.sub("", s, count=1)

    # Step 5: collapse trailing dotted/slashed initials (S.A. -> "sa",
    # S.p.A. -> "spa", A/S -> "as", etc.) so the suffix-token filter
    # below can match them.
    s = _collapse_trailing_initials(s)

    # Step 6: non-alphanumeric -> space.
    s = _RE_NON_ALNUM.sub(" ", s)

    # Step 7: lowercase.
    s = s.lower()

    # Steps 8-10: split, filter, join with no separator.
    tokens = (
        t for t in _RE_WHITESPACE.split(s.strip())
        if t and t not in _SUFFIX_TOKENS
    )
    return "".join(tokens)

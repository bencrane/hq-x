"""NYC property identity-layer normalizers.

Pure functions for ingest-time normalization of the NYC property R2 corpus
(scripts/run_nyc_property_r2_ingest.py). Three primitives:

  * normalize_owner_name(text)  — PLUTO OwnerName / DOF Condo Association name
                                  → lowercase + suffix-stripped + collapsed
                                  whitespace. Designed to match against the
                                  same downstream FEC name-normalize tier
                                  (organisational entity names — LLCs,
                                  trusts, corps).
  * compute_bbl(borough, block, lot)  — NYC's 10-digit Borough-Block-Lot
                                        composite key:
                                        borough * 1_000_000_000
                                        + block  * 10_000
                                        + lot
                                        → 10-character zero-padded VARCHAR.
                                        Returns None when any component is
                                        missing or non-numeric.
  * normalize_borough_code(value)  — Accepts integer (1-5), 1-letter code
                                    (M/X/B/Q/R), or full borough name
                                    ("MANHATTAN") and returns the SMALLINT
                                    1-5. Returns None on unrecognized input.

These run in Python at ingest time (not as Postgres SQL functions). There is
no SQL counterpart to parity-test against — the FEC ⨝ NYC join uses these
columns directly off Parquet via DuckDB / RisingWave. If a future SQL
function appears, the parity-test pattern from
`tests/scripts/test_dfpi_normalize_parity.py` is the template.
"""

from __future__ import annotations

import re

__all__ = [
    "normalize_owner_name",
    "compute_bbl",
    "normalize_borough_code",
]


# --------------------------------------------------------------------------- #
# Suffix tokens — append-only.
#
# Mirrors the suffix-stripping discipline from scripts/_lib/dfpi_normalize.py
# but tuned to the NYC property-owner cohort (heavy on trusts, individual
# names, LLCs, corps). Drops franchise-specific noise tokens (`franchising`,
# `enterprises`, `brands`) — those are not load-bearing for property owners
# and could overstrip ("ENTERPRISE PROPERTIES LLC" → "properties" after
# `enterprise` strip would be misleading).
# --------------------------------------------------------------------------- #

_SUFFIX_TOKENS: frozenset[str] = frozenset({
    # US legal entity forms
    "llc", "lllc", "l3c", "inc", "corp", "corporation", "company", "co",
    "ltd", "lp", "lllp", "pllc", "pc", "plc", "incorporated", "limited",
    "llp",
    # Foreign legal entity forms (rare in NYC property but seen in foreign
    # ownership of trophy assets).
    "bv", "nv", "sa", "gmbh", "ag",
    # Trust / estate forms
    "trust", "trustee", "trustees", "estate",
    # Real-estate-specific noise
    "associates", "partners", "partnership",
    "holdings", "holding",
    "realty", "properties", "property",
    # Generic suffixes
    "the",
})


# --------------------------------------------------------------------------- #
# Compiled regexes — compile-once for hot-loop performance over ~860K PLUTO
# rows + ~12K condo associations.
# --------------------------------------------------------------------------- #

# Collapse foreign legal forms (B.V., N.V., S.A.) at end of string into the
# 2-letter token form *before* the punctuation-strip step, so the
# downstream suffix-token filter can drop them. Without this step
# "Acme B.V." → "acme b v" → "acme b v" (b/v aren't in the suffix list
# individually). Matches DFPI's _RE_FOREIGN_LEGAL.
_RE_FOREIGN_LEGAL_TAIL = re.compile(
    r"[,\s]+([bns])\s*\.\s*([vaz])\s*\.?\s*$",
    re.IGNORECASE,
)

# Replace any non-alphanumeric run with a single space (treats `,`, `.`,
# `&`, `'`, `/`, `-`, `(`, `)`, `#`, etc. as separators). Identical
# behaviour to FEC's `name_normalized` separator handling.
_RE_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")

# Whitespace splitter for the final token-filter pass.
_RE_WHITESPACE = re.compile(r"\s+")


def normalize_owner_name(value: str | None) -> str | None:
    """Normalize a NYC property owner / condo association name.

    Pipeline:
      1. None / empty / whitespace-only → None.
      2. Replace any non-alphanumeric run with a single space.
      3. Lowercase + collapse whitespace.
      4. Drop suffix tokens (LLC, INC, TRUST, etc.). Tokens are matched
         word-for-word — `incorporate` is NOT stripped (only `incorporated`).
      5. Re-join the surviving tokens with a single space.
      6. If nothing survives (e.g. "LLC LLC LLC" or " "), return None.

    Returns the normalized form, or None on empty / suffix-only input.

    Examples:
      "JOHN A SMITH"                 → "john a smith"
      "Smith, John A."               → "smith john a"
      "123 MAIN ST LLC"              → "123 main st"
      "JOHN DOE TRUST"               → "john doe"
      "JOHN DOE TRUST, LLC"          → "john doe"
      "ABC HOLDINGS, INC."           → "abc"
      "  "                           → None
      "LLC, INC"                     → None
      None                           → None
    """
    if value is None:
        return None
    # Pre-collapse foreign legal forms ("Acme B.V." → "Acme bv") so the
    # suffix-token filter can drop them as a unit.
    s = _RE_FOREIGN_LEGAL_TAIL.sub(r" \1\2", value, count=1)
    s = _RE_NON_ALNUM.sub(" ", s).strip().lower()
    if not s:
        return None
    tokens = [t for t in _RE_WHITESPACE.split(s) if t and t not in _SUFFIX_TOKENS]
    if not tokens:
        return None
    return " ".join(tokens)


# --------------------------------------------------------------------------- #
# BBL composite — Borough-Block-Lot.
# --------------------------------------------------------------------------- #


_BOROUGH_LETTER_MAP: dict[str, int] = {
    "m": 1,  # Manhattan
    "x": 2,  # Bronx
    "b": 3,  # Brooklyn (single-letter ambiguity vs Bronx — DOF uses X for Bronx)
    "q": 4,  # Queens
    "r": 5,  # Staten Island (Richmond)
}

_BOROUGH_NAME_MAP: dict[str, int] = {
    "manhattan": 1, "mn": 1,
    "bronx": 2, "bx": 2, "the bronx": 2,
    "brooklyn": 3, "bk": 3, "kings": 3,
    "queens": 4, "qn": 4,
    "staten island": 5, "statenisland": 5, "staten_island": 5,
    "si": 5, "richmond": 5,
}


def normalize_borough_code(value: object) -> int | None:
    """Coerce DOF/PLUTO borough representations to the canonical 1-5 SMALLINT.

    Accepted inputs:
      - integer 1-5 (returned as-is)
      - numeric string "1"-"5"
      - 1-letter code "M" / "X" / "B" / "Q" / "R" (case-insensitive)
      - full borough name "MANHATTAN" / "BRONX" / "BROOKLYN" / "QUEENS" /
        "STATEN ISLAND" / "STATEN_ISLAND" (case-insensitive)
      - None / empty → None
      - Anything else → None

    Returns the SMALLINT 1-5 or None.
    """
    if value is None:
        return None
    if isinstance(value, bool):  # bool is int subclass — exclude.
        return None
    if isinstance(value, int) and 1 <= value <= 5:
        return value
    if isinstance(value, float):
        # Accept integral floats (1.0 → 1) — DuckDB sometimes reads numeric
        # CSV columns as DOUBLE; reject NaN / inf / non-integral.
        if value != value or value == float("inf") or value == -float("inf"):
            return None
        if not value.is_integer():
            return None
        iv = int(value)
        return iv if 1 <= iv <= 5 else None
    if isinstance(value, str):
        s = value.strip().lower()
        if not s:
            return None
        if s.isdigit():
            iv = int(s)
            return iv if 1 <= iv <= 5 else None
        if len(s) == 1:
            return _BOROUGH_LETTER_MAP.get(s)
        return _BOROUGH_NAME_MAP.get(s)
    return None


def compute_bbl(borough: object, block: object, lot: object) -> str | None:
    """Compute the 10-digit zero-padded BBL from (borough, block, lot).

    Encoding (per DOF / DCP convention):
      bbl_int = borough * 1_000_000_000 + block * 10_000 + lot

    Returns a 10-character zero-padded numeric string ("1000200011" for
    Manhattan / Block 20 / Lot 11).

    Component validation:
      - borough: must normalize via normalize_borough_code() to 1-5.
      - block: 0 ≤ block ≤ 99_999.
      - lot: 0 ≤ lot ≤ 9_999.

    Returns None on any out-of-range or unparseable input.
    """
    b = normalize_borough_code(borough)
    if b is None:
        return None
    block_i = _coerce_int(block)
    lot_i = _coerce_int(lot)
    if block_i is None or lot_i is None:
        return None
    if not (0 <= block_i <= 99_999):
        return None
    if not (0 <= lot_i <= 9_999):
        return None
    bbl = b * 1_000_000_000 + block_i * 10_000 + lot_i
    return f"{bbl:010d}"


def _coerce_int(value: object) -> int | None:
    """Coerce numeric / string input to int. Returns None on failure."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value == float("inf") or value == -float("inf"):
            return None
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return int(s)
        except ValueError:
            try:
                return int(float(s))
            except ValueError:
                return None
    return None

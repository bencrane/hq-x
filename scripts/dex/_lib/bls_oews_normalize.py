"""Pure-functional normalizers for BLS OEWS R2 ingest.

OEWS is a domain-code dataset (no person or company identity). The
"normalized" columns shipped in the Parquet are canonical encodings of
SOC occupation, BLS area, and NAICS industry codes — the join keys for
downstream MVs that bridge OEWS wage / employment cells against:

  - NPPES taxonomy (SOC ↔ NUCC for healthcare)
  - Census MSA boundaries (area_code ↔ census-tract aggregations)
  - NAICS-tagged datasets (USAspending, SBA borrowers, USPTO trademark
    filers, USDA RD)
  - FEC (occupation-field free-text → SOC bucket)

Per the directive: normalization happens at INGEST time so the Parquet
carries both raw and normalized columns. Downstream RW MVs join on the
normalized columns; the raw columns stay as ground-truth.

Future LLM-assisted disambiguation (occupation-title → SOC fuzzy match
for FEC donor occupation strings) belongs in a downstream MV.
"""

from __future__ import annotations

import re
from typing import Any, Final


_WHITESPACE_RUN: Final = re.compile(r"\s+")
_NON_DIGIT: Final = re.compile(r"\D")
_SOC_RE: Final = re.compile(r"^(\d{2})-?(\d{4})$")


def normalize_soc_code(raw: Any) -> str | None:
    """Normalize a Standard Occupational Classification code to 7-char canonical
    form `'NN-NNNN'`.

    BLS publishes SOC codes as 7-character strings like `'29-1141'` already
    formatted. Some upstream cells lose the hyphen or have leading-space
    issues. We coerce to the canonical hyphen form. Note: BLS publishes
    occupation aggregates with codes ending in `'0000'` (major group),
    `'XX00'` (minor group), `'XXX0'` (broad group). We preserve all of
    them — downstream MVs decide whether to filter to leaf occupations
    (last 4 digits != '0000', != 'XX00', != 'XXX0').

    "29-1141"      → "29-1141"
    "291141"       → "29-1141"
    " 29-1141 "    → "29-1141"
    "29-0000"      → "29-0000"     (major group, preserved)
    "00-0000"      → "00-0000"     (all occupations aggregate)
    "abc"          → None
    None / empty   → None
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    m = _SOC_RE.match(s)
    if not m:
        return None
    return f"{m.group(1)}-{m.group(2)}"


def normalize_area_code(raw: Any) -> str | None:
    """Normalize a BLS OEWS area code.

    BLS area codes are heterogeneous strings whose semantics depend on
    AREA_TYPE:

      AREA_TYPE=1 (national)        → AREA = "99" or "0000099"
      AREA_TYPE=2 (state)           → AREA = 2-digit FIPS state code
      AREA_TYPE=3 (territory)       → AREA = 2-digit FIPS code (PR=72 etc.)
      AREA_TYPE=4 (MSA)             → AREA = 5-digit OMB MSA code
      AREA_TYPE=5 (non-metro)       → AREA = 7-digit BOS area code

    We strip whitespace + collapse to canonical digit string. We do
    NOT enforce length here — different AREA_TYPEs use different lengths.
    Empty / non-digit input yields None.

    "  29420  "  → "29420"
    "0000099"    → "0000099"
    "12060"      → "12060"
    "0500001"    → "0500001"     (non-metro)
    "abc"        → None
    None / empty → None
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    digits = _NON_DIGIT.sub("", s)
    if not digits:
        return None
    return digits


def normalize_area_title(raw: Any) -> str | None:
    """Normalize an OEWS area title (e.g. MSA name) for cross-year fuzzy match.

    OMB occasionally redefines MSA boundaries. The numeric area_code may
    shift across years for the same geography while the area_title stays
    relatively stable. Downstream MVs use area_title_normalized as a
    best-effort name-match fallback when codes don't align.

    Steps:
      1. Uppercase + trim.
      2. Collapse whitespace.

    "Atlanta-Sandy Springs-Roswell, GA"   → "ATLANTA-SANDY SPRINGS-ROSWELL, GA"
    "  New York-Newark-Jersey City, NY-NJ-PA" → "NEW YORK-NEWARK-JERSEY CITY, NY-NJ-PA"
    None / empty                          → None
    """
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if not s:
        return None
    s = _WHITESPACE_RUN.sub(" ", s)
    return s or None


def normalize_naics_code(raw: Any) -> str | None:
    """Normalize a NAICS industry code.

    BLS OEWS publishes NAICS codes at varying widths depending on I_GROUP:
      "cross-industry"    → "000000"  (all industries aggregate)
      "sector"            → 2-digit (e.g. "11", "21", "23", "62")
      "3-digit"           → 3-digit
      "4-digit"           → 4-digit
      "5-digit"           → 5-digit
      "6-digit"           → 6-digit (most granular)

    We preserve verbatim digit string (with leading zeros). Empty,
    non-digit, or whitespace-only inputs yield None.

    "62"          → "62"
    "611310"      → "611310"
    "000000"      → "000000"
    "  23  "      → "23"
    "abc"         → None
    None / empty  → None
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    digits = _NON_DIGIT.sub("", s)
    if not digits:
        return None
    return digits


def derive_area_kind(
    area_type: Any,
    i_group: Any,
    naics: Any,
) -> str | None:
    """Derive the 5-bin level partition for a (AREA_TYPE, I_GROUP, NAICS) tuple.

    BLS OEWS ships all five aggregation levels in one combined file
    (`oesm{YY}all.zip`). The directive's R2 partitioning splits them
    apart by deriving the level from BLS discriminator columns:

      national    AREA_TYPE = 1 AND I_GROUP = 'cross-industry'
      state       AREA_TYPE in (2, 3)
      msa         AREA_TYPE = 4
      non_metro   AREA_TYPE in (5, 6)
      industry    AREA_TYPE = 1 AND I_GROUP != 'cross-industry'

    AREA_TYPE = 3 (territory: PR, VI, GU) is grouped with state. State
    Departments of Labor publish OEWS at state grain for territories;
    treating them as states matches the directive's MSA / non-metro /
    state taxonomy.

    AREA_TYPE = 6 (metropolitan division) was retired in newer
    publications; we group historical AREA_TYPE=6 with non_metro since
    that's the closest semantic bucket.

    Returns None for unrecognized area_type values — downstream MVs
    filter on `area_kind IS NOT NULL` if needed.

    (1, "cross-industry", "000000") → "national"
    (1, "sector", "62")             → "industry"
    (2, "cross-industry", "000000") → "state"
    (4, "cross-industry", "000000") → "msa"
    (5, "cross-industry", "000000") → "non_metro"
    (None, ..., ...)                → None
    """
    if area_type is None:
        return None
    try:
        at = int(str(area_type).strip())
    except (ValueError, AttributeError):
        return None

    ig = (str(i_group).strip().lower() if i_group is not None else "")

    if at == 1:
        if ig == "cross-industry" or ig == "" or ig == "1":
            return "national"
        return "industry"
    if at in (2, 3):
        return "state"
    if at == 4:
        return "msa"
    if at in (5, 6):
        return "non_metro"
    return None


def derive_soc_revision(release_year: int) -> str:
    """Map a BLS OEWS release year to its governing SOC revision.

    SOC is revised on a roughly-decadal cadence by OMB. BLS OEWS adopts
    the new revision the year after publication. Within the 2011-2024
    target range there's a 2018 transition where some occupation codes
    were renumbered. Downstream cross-year joins handle the transition
    via this column.

      ≤ 2017  → "SOC2010"
      ≥ 2018  → "SOC2018"

    The 2010 boundary (SOC2000 → SOC2010) is out of scope for this ingest;
    the function does not handle pre-2010 inputs.

    2017 → "SOC2010"
    2018 → "SOC2018"
    2024 → "SOC2018"
    """
    if release_year <= 2017:
        return "SOC2010"
    return "SOC2018"

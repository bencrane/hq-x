"""Pure-Python normalizers for HUD Multifamily + LIHTC ArcGIS ingest.

Used by scripts/run_hud_multifamily_r2_snapshot_ingest.py. Dependency-free +
unit-tested in tests/unit/test_hud_multifamily_normalize.py so the
identity-spine join key invariants stay stable before any DuckDB / Parquet /
R2 round-trip.

Five public functions:

  - normalize_owner_name(raw)  — lowercase + collapse-ws + iterative US
        corp-form / property-form suffix strip. Returns None on empty input.
  - pick_owner_field(table_key, attrs)  — given a dataset key
        ('insured' | 'assisted' | 'lihtc' | 'multifamily-pipeline') and the
        ArcGIS feature attributes dict, return the first non-empty owner /
        sponsor / project candidate. The candidate cascade is hard-coded
        per-table because HUD's actual ArcGIS schemas do NOT carry the
        directive's guessed OWNER_NAME / MORTGAGEE_NAME / MGR_NAME /
        LIHTC_OWNER_NAME columns — those fields were scrubbed years ago.
        Closest analogues per dataset:
          insured / assisted: MGMT_AGENT_ORG_NAME → CLIENT_GROUP_NAME →
                              PROPERTY_NAME_TEXT
          lihtc:              PROJECT  (HUD scrubbed CO_*; project name is
                              the closest named entity)
          multifamily-pipeline: Property_Name
  - normalize_state(raw)       — uppercased 2-letter; None if not 2 chars
        after strip.
  - normalize_zip5(raw)        — first 5 digits; None if <5 digits.
  - normalize_city(raw)        — lowercase + trimmed + collapse-ws.
  - coerce_arcgis_epoch_ms_to_dt(raw) — ArcGIS esriFieldTypeDate values are
        epoch-milliseconds ints. Returns tz-aware UTC datetime or None.

The corp-form / property-form suffix list is the directive's:
  LLC, Inc, Corp / Corporation, LP, LLP, LLLP, Co. / Company,
  Holdings, Properties, Apartments, Realty, Partners, Associates
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

# US-only legal-form / property-name suffixes. Order matters: longest forms
# (e.g. CORPORATION) match before short forms (CORP, CO) so we don't leave
# "ORPORATION" trailing. Iterative right-to-left strip handles stacked
# suffixes ("HOLDINGS LLC", "PROPERTIES INC", "PARTNERS LP").
_SUFFIXES: tuple[str, ...] = (
    "CORPORATION",
    "INCORPORATED",
    "ASSOCIATES",
    "APARTMENTS",
    "PROPERTIES",
    "PARTNERS",
    "HOLDINGS",
    "COMPANY",
    "REALTY",
    "LIMITED",
    "LLLP",
    "CORP",
    "INC",
    "LLC",
    "LLP",
    "LP",
    "CO",
    "LTD",
)

# Suffix regex anchored at end of string. Trailing punctuation absorbed by
# the strip(" ,.") afterward. Sorted longest-first so e.g. CORPORATION wins
# over CORP, ASSOCIATES wins over ASSOC, etc.
_SUFFIX_RE = re.compile(
    r"(?:[\s,.]+(?:" + "|".join(sorted(_SUFFIXES, key=len, reverse=True)) + r"))$",
    re.IGNORECASE,
)

_NON_DIGIT_RE = re.compile(r"\D+")
_WS_COLLAPSE_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]+")


# Per-dataset cascade of candidate owner/sponsor field names. Keys exactly
# match the dataset_key argument the ingest script passes through. The
# script reads attrs from the ArcGIS response (mixed case as published).
_OWNER_FIELD_CASCADE: dict[str, tuple[str, ...]] = {
    "insured": (
        "MGMT_AGENT_ORG_NAME",
        "CLIENT_GROUP_NAME",
        "PROPERTY_NAME_TEXT",
    ),
    "assisted": (
        "MGMT_AGENT_ORG_NAME",
        "CLIENT_GROUP_NAME",
        "PROPERTY_NAME_TEXT",
    ),
    "lihtc": (
        "PROJECT",
    ),
    "multifamily-pipeline": (
        "Property_Name",
    ),
}


def normalize_owner_name(raw: str | None) -> str | None:
    """Normalize an owner / sponsor / project name for join-key use.

    Steps:
      1. Strip + collapse whitespace.
      2. Iteratively strip trailing US legal-form / property-form suffixes.
      3. Lowercase.
      4. Strip non-word punctuation (keeps alphanumerics + whitespace).
      5. Collapse whitespace; empty result → None.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    s = _WS_COLLAPSE_RE.sub(" ", s)
    while True:
        new_s = _SUFFIX_RE.sub("", s).strip(" ,.")
        if new_s == s or not new_s:
            s = new_s or s
            break
        s = new_s
    s = s.lower()
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_COLLAPSE_RE.sub(" ", s).strip()
    if not s:
        return None
    return s


def pick_owner_field(table_key: str, attrs: dict[str, Any]) -> str | None:
    """Pick the first non-empty owner/sponsor/project candidate for the
    given dataset. Returns the raw (un-normalized) value, or None if every
    candidate is null/empty.

    `table_key` ∈ {'insured', 'assisted', 'lihtc', 'multifamily-pipeline'}.
    Unknown keys return None.
    """
    cascade = _OWNER_FIELD_CASCADE.get(table_key, ())
    for field in cascade:
        v = attrs.get(field)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return None


def normalize_state(raw: Any) -> str | None:
    """Uppercased 2-letter US state code. None if not exactly 2 chars after
    strip (filters single-letter junk and full-state-name typos).
    """
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if len(s) != 2:
        return None
    return s


def normalize_zip5(raw: Any) -> str | None:
    """First 5 digits of a ZIP code, with leading-zero recovery.

    Handles ZIP+4 ('94103-1234'), space variants, and lone '00000'. If the
    digit count is exactly 4, left-pads with a single '0' — a common
    artifact when sources export Northeast ZIPs (000xx-099xx) through
    spreadsheet pipelines that strip leading zeros (e.g. Boston 02119 →
    '2119'). HUD's LIHTC dataset has thousands of these.

    Returns None for empty / non-digit input or if the result has <4
    digits.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    digits = _NON_DIGIT_RE.sub("", s)
    if len(digits) >= 5:
        return digits[:5]
    if len(digits) == 4:
        return "0" + digits
    return None


def normalize_city(raw: Any) -> str | None:
    """Lowercase + trim + collapse internal whitespace."""
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if not s:
        return None
    return _WS_COLLAPSE_RE.sub(" ", s)


def coerce_arcgis_epoch_ms_to_dt(raw: Any) -> datetime | None:
    """ArcGIS esriFieldTypeDate values are epoch-milliseconds ints. Returns
    tz-aware UTC datetime, or None if value is null / non-numeric / out of
    representable range.
    """
    if raw is None:
        return None
    try:
        return datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None

"""Pure-Python normalizers for IRS Exempt Organizations Business Master File.

Used by scripts/run_irs_bmf_r2_ingest.py. Kept dependency-free + tested directly
in tests/unit/test_irs_bmf_normalize.py so the join-key invariants
(EIN length=9 > 99%, org_name_normalized NULL < 0.5%) are guaranteed before
any DuckDB / Parquet / R2 round-trip.

Two functions:
  - normalize_ein(raw): strip non-digit, left-pad to 9. Empty/None → None.
  - normalize_org_name(raw): lowercase, collapse whitespace, strip US legal-form
    suffixes. Empty/None → None.

The suffix list is US-only (this is IRS data — there is no GMBH / AG / BV /
SARL contamination to worry about).
"""

from __future__ import annotations

import re

# US-only legal-form / nonprofit-form suffixes. Order matters: longest forms
# (e.g. "INCORPORATED") must be matched before short forms ("INC") so we don't
# leave "ORPORATED" trailing in the name. We strip them iteratively from the
# right until none match, since some orgs stack suffixes ("FOUNDATION INC").
_SUFFIXES: tuple[str, ...] = (
    "INCORPORATED",
    "CORPORATION",
    "ASSOCIATION",
    "FOUNDATION",
    "MINISTRIES",
    "MINISTRY",
    "FELLOWSHIP",
    "INSTITUTE",
    "SOCIETY",
    "COUNCIL",
    "ALLIANCE",
    "FEDERATION",
    "COALITION",
    "NETWORK",
    "PARTNERSHIP",
    "FUND",
    "TRUST",
    "CHARITIES",
    "CHARITY",
    "CHURCH",
    "MISSION",
    "ASSEMBLY",
    "CENTER",
    "CENTRE",
    "GROUP",
    "INC",
    "CORP",
    "CO",
    "LLC",
    "LP",
    "LLP",
    "ORG",
)

# Compiled regex of all suffixes — ordered longest-first so "INCORPORATED"
# matches before "INC". The trailing word boundary plus optional dot/comma
# absorbs "Inc." and "Inc," variants. Anchored at end of string.
_SUFFIX_RE = re.compile(
    r"(?:[\s,.]+(?:" + "|".join(sorted(_SUFFIXES, key=len, reverse=True)) + r"))$",
    re.IGNORECASE,
)

_NON_DIGIT_RE = re.compile(r"\D+")
_WS_COLLAPSE_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]+")


def normalize_ein(raw: str | None) -> str | None:
    """Normalize an IRS EIN to a 9-digit string.

    IRS publishes EINs as 9-digit text but some rows arrive with hyphens
    (`12-3456789`) or have leading zeros stripped (`74874023` for `074874023`).
    Strip everything non-digit, then left-pad to 9.

    Returns None for empty / None / non-numeric input or if the result is
    longer than 9 digits (which would indicate corrupt source data, not a
    salvageable EIN).
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    digits = _NON_DIGIT_RE.sub("", s)
    if not digits:
        return None
    if len(digits) > 9:
        return None
    return digits.zfill(9)


def normalize_org_name(raw: str | None) -> str | None:
    """Normalize an IRS organization name for join-key use.

    Steps:
      1. Strip + collapse whitespace.
      2. Lowercase.
      3. Strip US legal-form / nonprofit-form suffixes from the right,
         iteratively, until none match.
      4. Strip trailing punctuation, collapse whitespace again.
      5. Empty result → None.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    s = _WS_COLLAPSE_RE.sub(" ", s)
    # Iteratively strip suffixes (handles "FOUNDATION INC", "TRUST FUND", etc.)
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

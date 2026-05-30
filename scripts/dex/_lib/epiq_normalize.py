"""Epiq-specific helpers for normalizing creditor identity from claims rows.

Sibling to `_lib/entity_name_normalize.py` and `_lib/address_normalize.py` —
those provide the canonical cross-source name + street normalizers. This
module owns the Epiq-shaped pre-processing:

  - parse_state_zip_from_address_list_json: extract (state_2letter, zip5,
    street_freeform) from Epiq's `creditor_address_list_json` payload.
    Epiq's structured stateCode/zipCode fields are nulled on a meaningful
    fraction of rows (esp. attorney C/O addresses), so we fall back to
    parsing the last array element ("CITY, STATE, ZIP" canonical layout).

  - is_epiq_generic_creditor_marker: Epiq-specific placeholder/redaction
    detection AFTER canonical name normalization. Catches the Epiq
    intake patterns ("NAME ON FILE", "REDACTED", "TBD", "various
    creditors", numbered claimant templates from mass-tort cases, etc.)
    that the FEC-shaped canonical generic-string blacklist doesn't cover.

Both helpers are pure functions, dependency-free beyond stdlib + the
canonical normalizer libs. Used by:

  - scripts/emit_epiq_claims_resolved_lance.py
  - scripts/emit_epiq_creditors_lance.py

Per the L31 normalizer-versioning rule, this module exposes `__version__`.
Bumping the constant forces a version bump on consumers' downstream
provenance.
"""
from __future__ import annotations

import json
import re
from typing import Optional

__version__ = "1.0.0"


# --------------------------------------------------------------------------- #
# State name → 2-letter code lookup
# --------------------------------------------------------------------------- #

STATE_NAME_TO_CODE: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC", "dc": "DC",
    "puerto rico": "PR", "guam": "GU", "virgin islands": "VI",
    "u s virgin islands": "VI", "american samoa": "AS",
    "northern mariana islands": "MP",
    "armed forces americas": "AA", "armed forces europe": "AE",
    "armed forces pacific": "AP",
}
US_2LETTER: frozenset[str] = frozenset(STATE_NAME_TO_CODE.values())

_ZIP5_RE: re.Pattern[str] = re.compile(r"\b(\d{5})(?:-\d{4})?\b")


# --------------------------------------------------------------------------- #
# Epiq generic-name markers (post-canonical-normalization)
# --------------------------------------------------------------------------- #

# Strings that survive `_lib.entity_name_normalize` but are clearly NOT
# entity names — Epiq intake placeholders, mass-tort numbered claimants,
# redaction markers, etc. Match is exact against the normalized form.
EPIQ_GENERIC_NAME_MARKERS: frozenset[str] = frozenset({
    "name on file",
    "redacted",
    "name redacted",
    "address redacted",
    "claim number voided by agent",
    "****claim number voided by agent****",
    "individual creditor",
    "to be supplied",
    "tbd",
    "n a", "na", "none",
    "unknown",
    "various", "various creditors", "all known creditors",
    "no information provided", "not provided",
})

# Mass-tort placeholders: "CLAIMANT 1234", "CLAIMANT N", "JOHN DOE N", etc.
# These normalize to predictable lowercased forms; recognize them via regex.
_NUMBERED_CLAIMANT_RE: re.Pattern[str] = re.compile(
    r"^(claimant|john doe|jane doe|doe)\s*\d+$"
)


# --------------------------------------------------------------------------- #
# Public helpers
# --------------------------------------------------------------------------- #


def parse_state_zip_from_address_list_json(
    raw: Optional[str],
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Extract `(state_2letter, zip5, street_freeform)` from Epiq's
    creditor_address_list_json.

    Payload layout (verified across the Epiq universe):

        {
          "creditorAddress": ["line1", "line2", ..., "CITY, STATE, ZIP"],
          "city":  "NEW YORK" | null,
          "stateCode": "NY" | null,
          "zipCode":   "10153" | null,
          "countryCode": ...,
          "countryName": ...
        }

    Strategy: prefer the structured `stateCode`/`zipCode` fields when present
    (typically Stellar-class cases with clean intake). Fall back to
    position-aware parsing of the last array element when the structured
    fields are null (typically DM-class cases with attorney C/O addresses).

    Returns `(None, None, None)` on any unparseable input — never raises.
    """
    if not raw:
        return None, None, None
    try:
        d = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, None, None
    if not isinstance(d, dict):
        return None, None, None

    arr_raw = d.get("creditorAddress") or []
    if not isinstance(arr_raw, list):
        arr_raw = []
    arr: list[str] = [str(x).strip() for x in arr_raw if x is not None and str(x).strip()]

    # Stage 1 — structured fields
    state: Optional[str] = None
    raw_state = d.get("stateCode")
    if raw_state:
        sc = str(raw_state).strip().upper()
        if sc in US_2LETTER:
            state = sc

    zip5: Optional[str] = None
    raw_zip = d.get("zipCode")
    if raw_zip:
        zc = str(raw_zip).strip()
        if len(zc) >= 5 and zc[:5].isdigit():
            zip5 = zc[:5]

    # Stage 2 — fall back to last-array-line parsing
    if (state is None or zip5 is None) and arr:
        last = arr[-1]
        parts = [p.strip() for p in last.split(",") if p.strip()]

        # zip5 from anywhere in the last line
        if zip5 is None:
            for p in parts or [last]:
                m = _ZIP5_RE.search(p)
                if m:
                    zip5 = m.group(1)
                    break

        # State: canonical layout is "CITY, STATE, ZIP" or "CITY, STATE ZIP"
        # so parts[1] (the second element) is the most likely state carrier.
        if state is None:
            candidates: list[str] = []
            if len(parts) >= 2:
                candidates.append(parts[1])  # most common
            # Multi-word states ("North Carolina") may collapse into parts[1]
            # or, in rare layouts, span their own line. Add parts[0] only when
            # there's no separator (the whole address collapsed to one line).
            if len(parts) == 1 and parts:
                candidates.append(parts[0])

            for cand in candidates:
                cl_full = cand.strip().lower()
                # multi-word state full-match
                if cl_full in STATE_NAME_TO_CODE:
                    state = STATE_NAME_TO_CODE[cl_full]
                    break
                # tokenize: "TX 76010" → ["TX", "76010"]
                for tk in cand.split():
                    tk_clean = tk.strip(".,").strip()
                    if not tk_clean:
                        continue
                    lc = tk_clean.lower()
                    if lc in STATE_NAME_TO_CODE:
                        state = STATE_NAME_TO_CODE[lc]
                        break
                    uc = tk_clean.upper()
                    if uc in US_2LETTER:
                        state = uc
                        break
                if state is not None:
                    break

    # Stage 3 — street freeform (lines 1..N-1; entire array if 1 line)
    if len(arr) > 1:
        street_freeform: Optional[str] = ", ".join(arr[:-1])
    elif arr:
        street_freeform = arr[0]
    else:
        street_freeform = None

    return state, zip5, street_freeform


def is_epiq_generic_creditor_marker(name_normalized: Optional[str]) -> bool:
    """Return True iff `name_normalized` (post-canonical-normalization) is an
    Epiq-shaped placeholder/redaction marker that cannot resolve to a real
    creditor entity.

    A NULL/empty input also returns True — the canonical normalizer returns
    None for L33 generic strings (self-employed, owner, etc.) and for the
    empty/single-char post-normalize results, so NULL == not-a-real-entity.
    """
    if not name_normalized:
        return True
    s = name_normalized.strip().lower()
    if not s:
        return True
    if s in EPIQ_GENERIC_NAME_MARKERS:
        return True
    if _NUMBERED_CLAIMANT_RE.match(s):
        return True
    return False

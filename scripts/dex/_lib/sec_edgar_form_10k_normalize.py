"""Pure-functional normalizers for SEC EDGAR Form 10-K ingest.

The identity-spine contract is shared with DEF 14A: same CIK, same
accession-number canonicalization, same person-name + filer-name + title
normalizers, same dollar / percent / share parsers. Re-export those.

Form 10-K-specific helpers added here:

- ``normalize_holder_name`` — Item-12 Security Ownership holders may be
  individual persons (officers / directors) or entities (institutional 5%
  holders like ``BlackRock Inc.``, ``The Vanguard Group``). Returns an
  uppercased, whitespace-collapsed canonical string. Caller can additionally
  attempt ``normalize_person_name`` and inspect whether two name tokens
  survive — if so, classify as person; otherwise classify as entity.

- ``parse_property_description`` — Item-2 Properties descriptions are highly
  variable in format. Best-effort extraction of (city, state, country,
  size_sqft, owned_or_leased, use). Returns a dict with all fields possibly
  None. Heuristic-only — not authoritative geocoding.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

from _lib.sec_edgar_def_14a_normalize import (
    normalize_accession,
    normalize_cik,
    normalize_filer_name,
    normalize_person_name,
    normalize_title,
    parse_dollar_amount,
    parse_percent,
    parse_share_count,
)

__all__ = [
    "normalize_accession",
    "normalize_cik",
    "normalize_filer_name",
    "normalize_holder_name",
    "normalize_person_name",
    "normalize_title",
    "parse_dollar_amount",
    "parse_percent",
    "parse_property_description",
    "parse_share_count",
]


_WHITESPACE_RE: Final = re.compile(r"\s+")
_HOLDER_PUNCT_RE: Final = re.compile(r"[^\w\s\-\.&,]")


def normalize_holder_name(raw: str | None) -> str | None:
    """Uppercase + collapse whitespace + strip non-name punctuation.

    Holder names in the Item-12 Security Ownership table can be:
      - persons (``"John Smith"``, ``"Smith, John A."``)
      - entities (``"BlackRock Inc."``, ``"The Vanguard Group, Inc."``)
      - role-buckets (``"All directors and executive officers as a group"``)
      - 5%-holder entities (``"FMR LLC"``, ``"State Street Corporation"``)

    This normalizer doesn't try to disambiguate person vs entity — the caller
    runs ``normalize_person_name`` separately and inspects the result. This
    returns an uppercased, ASCII-folded, whitespace-collapsed canonical form
    suitable for cross-source string matching.
    """
    if raw is None:
        return None
    s = unicodedata.normalize("NFKD", raw)
    s = s.encode("ascii", "ignore").decode("ascii")
    s = s.strip().upper()
    if not s:
        return None
    s = re.sub(r"\([^)]*\)", " ", s)  # strip parenthetical asides
    s = _HOLDER_PUNCT_RE.sub(" ", s)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    return s or None


# Map state full-names → 2-letter abbreviations for property-state extraction.
_STATE_ABBR: Final[dict[str, str]] = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
    "CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT",
    "DELAWARE": "DE", "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI",
    "IDAHO": "ID", "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA",
    "KANSAS": "KS", "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME",
    "MARYLAND": "MD", "MASSACHUSETTS": "MA", "MICHIGAN": "MI",
    "MINNESOTA": "MN", "MISSISSIPPI": "MS", "MISSOURI": "MO",
    "MONTANA": "MT", "NEBRASKA": "NE", "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM",
    "NEW YORK": "NY", "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND",
    "OHIO": "OH", "OKLAHOMA": "OK", "OREGON": "OR", "PENNSYLVANIA": "PA",
    "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD",
    "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT", "VERMONT": "VT",
    "VIRGINIA": "VA", "WASHINGTON": "WA", "WEST VIRGINIA": "WV",
    "WISCONSIN": "WI", "WYOMING": "WY", "DISTRICT OF COLUMBIA": "DC",
    "PUERTO RICO": "PR",
}
_STATE_ABBRS_VALID: Final[frozenset[str]] = frozenset(_STATE_ABBR.values())

# city, ST → captures "Manhattan, NY" / "Houston, TX 77002".
_CITY_STATE_RE: Final = re.compile(
    r"\b([A-Z][a-zA-Z\.\- ]{1,40}?),\s+([A-Z]{2})\b(?:\s+\d{5})?",
)
# city, FullStateName.
_CITY_FULL_STATE_RE: Final = re.compile(
    r"\b([A-Z][a-zA-Z\.\- ]{1,40}?),\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){0,2})\b",
)
# size in square feet — variants:  "approximately 250,000 square feet",
# "250000 sq. ft.", "1.2 million square feet".
_SQFT_RE: Final = re.compile(
    r"(?:approximately\s+)?"
    r"(?P<num>\d{1,3}(?:,\d{3})+|\d{1,7}(?:\.\d+)?)\s*"
    r"(?P<unit>million\s+)?"
    r"(?:square[\s\-]+(?:feet|foot|ft\.?)|sq\.?\s*ft\.?|sf)\b",
    re.I,
)
# Country hints — restrict to common non-US disclosures.
_NON_US_COUNTRY_RE: Final = re.compile(
    r"\b(?:Canada|Mexico|United Kingdom|England|Scotland|Ireland|"
    r"Germany|France|Italy|Spain|Netherlands|Belgium|Switzerland|"
    r"Sweden|Norway|Denmark|Finland|Poland|Russia|"
    r"Japan|China|Taiwan|Hong Kong|Singapore|South Korea|India|Australia|"
    r"Brazil|Argentina|Chile|Colombia|"
    r"South Africa|Egypt|Israel|United Arab Emirates|Saudi Arabia)\b",
    re.I,
)
# Owned/leased classifier.
_OWNED_RE: Final = re.compile(r"\b(owned|own|owns|owned by)\b", re.I)
_LEASED_RE: Final = re.compile(r"\b(leased|lease|leases|leasing)\b", re.I)
# Use classifier — match in priority order.
_USE_PATTERNS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (re.compile(
        r"\b(corporate\s+)?(headquarters|head\s+office|principal\s+(?:executive\s+)?offices?)\b",
        re.I), "headquarters"),
    (re.compile(r"\b(manufacturing|production|plant|factory|mill)\b", re.I),
     "manufacturing"),
    (re.compile(r"\b(distribution|warehouse|fulfillment|logistics)\b", re.I),
     "distribution"),
    (re.compile(r"\b(retail|store|shop|showroom)\b", re.I), "retail"),
    (re.compile(r"\b(research|laboratory|lab|R&D|development)\b", re.I),
     "research"),
    (re.compile(r"\b(data\s+center|server\s+farm|hosting)\b", re.I),
     "data_center"),
    (re.compile(r"\b(office|administrative|executive)\b", re.I), "office"),
)


def parse_property_description(raw: str | None) -> dict[str, str | int | None]:
    """Heuristic best-effort extraction of structured fields from an Item-2
    property-description string.

    Returns a dict with keys: city, state, country, size_sqft, owned_or_leased,
    use. Each value may be None.

    Caller stores the verbatim ``raw`` text as ``property_description_raw`` so
    nothing is lost — these structured fields are convenience denormalizations.
    """
    out: dict[str, str | int | None] = {
        "city": None, "state": None, "country": None,
        "size_sqft": None, "owned_or_leased": None, "use": None,
    }
    if raw is None:
        return out
    s = str(raw).strip()
    if not s:
        return out

    # 1. State + city (try 2-letter abbreviation form first)
    m = _CITY_STATE_RE.search(s)
    if m:
        candidate_state = m.group(2).upper()
        if candidate_state in _STATE_ABBRS_VALID:
            out["state"] = candidate_state
            out["city"] = m.group(1).strip()
    if out["state"] is None:
        m2 = _CITY_FULL_STATE_RE.search(s)
        if m2:
            full_state = m2.group(2).strip().upper()
            abbr = _STATE_ABBR.get(full_state)
            if abbr:
                out["state"] = abbr
                out["city"] = m2.group(1).strip()

    # 2. Country (only when explicitly non-US)
    m_country = _NON_US_COUNTRY_RE.search(s)
    if m_country:
        out["country"] = m_country.group(0).title()
    elif out["state"] is not None:
        out["country"] = "US"

    # 3. Square footage
    m_sqft = _SQFT_RE.search(s)
    if m_sqft:
        try:
            num_str = m_sqft.group("num").replace(",", "")
            num = float(num_str)
            if m_sqft.group("unit") and "million" in m_sqft.group("unit").lower():
                num *= 1_000_000
            out["size_sqft"] = int(num)
        except (ValueError, AttributeError):
            pass

    # 4. Owned vs leased — only set if exactly one matches strongly.
    owned_hit = bool(_OWNED_RE.search(s))
    leased_hit = bool(_LEASED_RE.search(s))
    if owned_hit and leased_hit:
        out["owned_or_leased"] = "mixed"
    elif owned_hit:
        out["owned_or_leased"] = "owned"
    elif leased_hit:
        out["owned_or_leased"] = "leased"

    # 5. Property use
    for pat, label in _USE_PATTERNS:
        if pat.search(s):
            out["use"] = label
            break

    return out

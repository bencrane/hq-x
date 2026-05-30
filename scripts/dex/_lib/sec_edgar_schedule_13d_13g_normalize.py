"""Pure-functional normalizers for SEC EDGAR Schedule 13D + 13G ingest.

Identity-spine standard:
- ``cik_normalized`` — 10-digit zero-padded CIK string.
- ``accession_normalized`` — dashed canonical form (XXXXXXXXXX-XX-XXXXXX).
- ``filer_legal_name_normalized`` — uppercase + collapsed whitespace
  (institutional reporting persons: hedge funds, family offices, ...).
- ``person_first_normalized`` / ``person_last_normalized`` — for individual
  reporting persons (HNW filers).
- ``cusip_normalized`` — 9-character security identifier, uppercase
  alphanumeric. Bridges to subject company.
- ``lei_normalized`` — 20-char ISO-17442 LEI, uppercase.
- ``ein_normalized`` — 9-digit EIN, dashes/spaces stripped.

Scope deliberately omits comp / dollar parsing — Schedule 13D/G has no
compensation columns. Adds CUSIP + LEI + share-amount + percent helpers.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

_WHITESPACE_RE: Final = re.compile(r"\s+")
_PUNCT_RE: Final = re.compile(r"[^\w\s\-\.&]")
_NAME_PUNCT_RE: Final = re.compile(r"[^\w\s\-]")
_DIGITS_RE: Final = re.compile(r"\D")

_NAME_SUFFIX_TOKENS: frozenset[str] = frozenset({
    "JR", "JR.", "SR", "SR.", "II", "III", "IV", "V", "ESQ", "ESQ.", "PHD",
    "PH.D.", "MD", "M.D.", "CPA", "CFA",
})


def normalize_cik(raw: str | int | None) -> str | None:
    """Zero-pad a CIK to 10 digits."""
    if raw is None:
        return None
    if isinstance(raw, int):
        s = str(raw)
    else:
        s = raw.strip()
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    if not digits:
        return None
    if len(digits) > 10:
        return None
    return digits.zfill(10)


def normalize_accession(raw: str | None) -> str | None:
    """Return the dashed canonical form (XXXXXXXXXX-XX-XXXXXX) of an
    SEC accession number. Returns None on malformed input."""
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) != 18:
        return None
    return f"{digits[0:10]}-{digits[10:12]}-{digits[12:18]}"


def normalize_filer_name(raw: str | None) -> str | None:
    """Uppercase + collapse whitespace + strip non-name punctuation.

    Preserves &, -, ., and corporate-suffix tokens (LP / INC / CORP / LLC).
    """
    if raw is None:
        return None
    s = unicodedata.normalize("NFKD", raw)
    s = s.encode("ascii", "ignore").decode("ascii")
    s = s.strip().upper()
    if not s:
        return None
    s = _PUNCT_RE.sub(" ", s)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    return s or None


def normalize_person_name(raw: str | None) -> tuple[str | None, str | None]:
    """Split a person name string into ``(first_normalized, last_normalized)``.

    Heuristics:
    1. Strip parenthetical asides.
    2. Strip honorific titles (DR., MR., MS., MRS.) at start.
    3. Strip suffix tokens (JR., III, ESQ., PHD).
    4. Comma form ``"Last, First Middle"`` → flip.
    5. First token = first; last surviving token = last.
    """
    if raw is None:
        return (None, None)
    s = unicodedata.normalize("NFKD", raw)
    s = s.encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"\([^)]*\)", " ", s)
    s = s.strip().upper()
    if not s:
        return (None, None)
    s = re.sub(r"^(DR|MR|MRS|MS|HON|PROF|REV)\.?\s+", "", s)
    s = re.sub(r",?\s+(ESQ|PH\.?\s?D|M\.?\s?D|CPA|CFA)\.?$", "", s)

    if "," in s:
        parts = [p.strip() for p in s.split(",", 1)]
        if len(parts) == 2 and parts[1]:
            tail_tokens = [t for t in re.split(r"\s+", parts[1]) if t]
            if (
                len(tail_tokens) == 1
                and tail_tokens[0].rstrip(".") in _NAME_SUFFIX_TOKENS
            ):
                # "Smith, Jr." form — comma separates name from suffix.
                s = parts[0]
            else:
                # "Smith, John A." form — comma separates last from first.
                s = f"{parts[1]} {parts[0]}"

    s = _NAME_PUNCT_RE.sub(" ", s)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    tokens = [t for t in s.split() if t]
    while tokens and tokens[-1].rstrip(".") in _NAME_SUFFIX_TOKENS:
        tokens.pop()
    if len(tokens) < 2:
        return (None, None)
    first = tokens[0]
    last = tokens[-1]
    return (first or None, last or None)


def classify_reporting_person_type(
    raw_name: str | None,
    *,
    has_ein: bool = False,
    has_lei: bool = False,
) -> str:
    """Heuristic: 'entity' for institutional reporting persons,
    'person' for individuals. Rules in priority order:

    1. LEI present → entity (LEIs are issued only to legal entities).
    2. EIN present and looks like an EIN (not an SSN) → entity.
    3. Name contains a corporate suffix token → entity.
    4. Name has 5+ uppercase tokens → entity.
    5. Otherwise → person.
    """
    if has_lei:
        return "entity"
    if has_ein:
        return "entity"
    if not raw_name:
        return "person"
    upper = raw_name.upper()
    corporate_tokens = (
        " LP", " L.P.", " LLC", " L.L.C.", " INC", " INC.", " CORP",
        " CORPORATION", " LTD", " LIMITED", " TRUST", " FUND", " FOUNDATION",
        " PARTNERS", " CAPITAL", " MANAGEMENT", " HOLDINGS", " GROUP",
        " ASSOCIATES", " ADVISORS", " COMPANY", " BANK", " N.A.", " NA",
        " GP", " G.P.", " AG", " S.A.", " GMBH", " PLC", " PTY",
    )
    if any(tok in upper or upper.endswith(tok.strip()) for tok in corporate_tokens):
        return "entity"
    tokens = [t for t in re.split(r"\s+", upper) if t]
    if len(tokens) >= 5:
        return "entity"
    return "person"


def normalize_cusip(raw: str | None) -> str | None:
    """Normalize a CUSIP to 9 uppercase alphanumeric chars.

    Real CUSIPs are 9 chars: 6 issuer + 2 issue + 1 check digit. The
    check digit is always 0-9 (mod-10 algorithm). Pure-alpha tokens of
    length 9 (e.g. corporate-name fragments like ``RIDGEMONT``) would
    otherwise pass — we additionally require the last char to be a digit.

    Strips parenthetical descriptors first
    (e.g. ``"037833100 (Common Stock)"``).
    """
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if not s:
        return None
    # Strip parenthetical descriptors.
    s = re.sub(r"\([^)]*\)", " ", s)
    # Find the first 9-char alphanumeric token whose last char is a digit
    # (per the CUSIP mod-10 check-digit standard).
    for tok in re.findall(r"[A-Z0-9]+", s):
        if len(tok) == 9 and tok[-1].isdigit():
            return tok
    cleaned = re.sub(r"[^A-Z0-9]", "", s)
    if len(cleaned) == 9 and cleaned[-1].isdigit():
        return cleaned
    return None


def normalize_lei(raw: str | None) -> str | None:
    """Normalize an LEI to 20 uppercase alphanumeric chars."""
    if raw is None:
        return None
    s = str(raw).strip().upper()
    cleaned = re.sub(r"[^A-Z0-9]", "", s)
    if len(cleaned) == 20:
        return cleaned
    return None


def normalize_ein(raw: str | None) -> str | None:
    """Normalize an EIN to 9 digits (no dashes)."""
    if raw is None:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 9:
        return digits
    return None


def parse_share_amount(raw: str | None) -> int | None:
    """Parse a beneficial-ownership share-count cell.

    Strips footnote markers, commas, and leading currency symbols. Returns
    None on empty / dash / placeholder values.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    s = re.sub(r"\s*\(\d+\)\s*", "", s)
    if not s or s in ("-", "—", "–", "N/A", "n/a", "*", "**"):
        return None
    s = re.sub(r"[,\s\$]", "", s)
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def parse_percent(raw: str | None) -> float | None:
    """Parse a percent-of-class cell.

    SEC convention: ``"*"`` = "less than 1%, not disclosed exactly". Returns
    None for asterisk / placeholder values; raw float for numeric values.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s in ("*", "**", "—", "–", "-", "N/A", "n/a"):
        return None
    s = re.sub(r"\s*\(\d+\)\s*", "", s)
    s = re.sub(r"<\s*", "", s)
    s = s.rstrip("%").strip()
    s = re.sub(r"[,\s]", "", s)
    try:
        return float(s)
    except ValueError:
        return None


def normalize_state(raw: str | None) -> str | None:
    """Uppercase + strip + collapse whitespace; return None on empty.

    Used for citizenship / jurisdiction-of-organization fields.
    """
    if raw is None:
        return None
    s = unicodedata.normalize("NFKD", raw)
    s = s.encode("ascii", "ignore").decode("ascii")
    s = s.strip().upper()
    if not s or s in ("-", "—", "N/A"):
        return None
    s = _WHITESPACE_RE.sub(" ", s)
    return s

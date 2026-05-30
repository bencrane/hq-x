"""Pure-functional normalizers for SEC EDGAR Form 144 ingest.

Form 144 is the "Notice of Proposed Sale of Securities" — Rule 144 mandate
that an insider/affiliate/control person announcing intent to sell restricted
or control stock files this notice with the SEC. The dataset is the
canonical "imminent HNW liquidity event" signal in US public markets.

Identity-spine standard:
- ``cik_normalized`` — 10-digit zero-padded CIK string of the **issuer**
  (the public company whose stock is being sold).
- ``filer_legal_name_normalized`` — uppercase, single-space, corporate-suffix
  preserved (joins to GLEIF / FDIC / Form 990 / DEF 14A).
- ``person_first_normalized`` / ``person_last_normalized`` — uppercase,
  punctuation stripped, "Jr."/"Sr."/"III" suffixes preserved as a separate
  tail token. Joins to FEC donors / Form 990 board members / DEF 14A executives
  on the (first, last) pair.
- ``relationship_normalized`` — lowercase canonical token set
  ("affiliate" / "officer" / "director" / "10% owner" / "control person" /
  "trust" / "other") when source matches a recognized pattern; otherwise the
  lowercase-stripped source string.
- ``broker_normalized`` — uppercase + corporate-suffix-strip variant of
  ``name_of_broker`` (joins to other broker references, e.g. CMS Open Payments).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final


_WHITESPACE_RE: Final = re.compile(r"\s+")
_PUNCT_RE: Final = re.compile(r"[^\w\s\-\.&]")
_NAME_PUNCT_RE: Final = re.compile(r"[^\w\s\-]")
_DOLLAR_RE: Final = re.compile(r"[\$,\s]")

_NAME_SUFFIX_TOKENS: frozenset[str] = frozenset({
    "JR", "JR.", "SR", "SR.", "II", "III", "IV", "V", "ESQ", "ESQ.", "PHD",
    "PH.D.", "MD", "M.D.", "CPA", "CFA",
})

_BROKER_SUFFIX_TOKENS: frozenset[str] = frozenset({
    "LLC", "LP", "INC", "INC.", "CORP", "CORP.", "CORPORATION", "CO",
    "COMPANY", "PLC", "LTD", "LTD.", "LIMITED", "AG", "SA", "GMBH",
    "&", "AND",
})

_RELATIONSHIP_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b10\s*%?\s*owner\b", re.I), "10% owner"),
    (re.compile(r"\bten\s*percent\s*owner\b", re.I), "10% owner"),
    (re.compile(r"\bbeneficial\s+owner\b", re.I), "10% owner"),
    (re.compile(r"\bofficer\b", re.I), "officer"),
    (re.compile(r"\bdirector\b", re.I), "director"),
    (re.compile(r"\baffiliate\b", re.I), "affiliate"),
    (re.compile(r"\bcontrol\s+person\b", re.I), "control person"),
    (re.compile(r"\btrust\b", re.I), "trust"),
    (re.compile(r"\bestate\b", re.I), "estate"),
    (re.compile(r"\bother\b", re.I), "other"),
)


def normalize_cik(raw: str | int | None) -> str | None:
    """Zero-pad CIK to 10 digits."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    if not digits:
        return None
    if len(digits) > 10:
        return None
    return digits.zfill(10)


def normalize_accession(raw: str | None) -> str | None:
    """Return canonical dashed form ``XXXXXXXXXX-XX-XXXXXX``."""
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
    """Uppercase + collapse whitespace + ASCII-fold + strip non-name punctuation."""
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
    """Split person name into ``(first_normalized, last_normalized)``.

    Form 144 person names typically arrive in "First Middle Last" form
    (XML) or in mixed-case rendered form (HTML). Honorific titles + suffix
    tokens are stripped before keying. Returns ``(None, None)`` for
    single-token / empty input.
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
            s = f"{parts[1]} {parts[0]}"

    s = _NAME_PUNCT_RE.sub(" ", s)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    tokens = [t for t in s.split() if t]
    while tokens and tokens[-1].rstrip(".") in _NAME_SUFFIX_TOKENS:
        tokens.pop()
    if len(tokens) < 2:
        return (None, None)
    return (tokens[0] or None, tokens[-1] or None)


def normalize_relationship(raw: str | None) -> str | None:
    """Map a relationship-to-issuer string to a canonical token."""
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    for pat, canonical in _RELATIONSHIP_PATTERNS:
        if pat.search(s):
            return canonical
    return s.lower()


def normalize_broker(raw: str | None) -> str | None:
    """Uppercase + suffix-strip broker name. Drops trailing LLC / LP / INC
    so that ``"Morgan Stanley & Co. LLC"`` and ``"Morgan Stanley LLC"``
    both collapse to ``"MORGAN STANLEY"`` when grouped.
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
    if not s:
        return None
    tokens = s.split()
    while tokens and tokens[-1].rstrip(".") in _BROKER_SUFFIX_TOKENS:
        tokens.pop()
    return " ".join(tokens) if tokens else None


def parse_dollar_amount(raw: str | None) -> float | None:
    """Parse a Form 144 dollar/share amount field.

    Handles XML's bare numerics (``"890200"``), HTML's currency-formatted
    (``"$890,200.00"``), and the various dash / NA placeholders.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    s = re.sub(r"\s*\(\d+\)\s*", "", s)
    if s in ("-", "—", "–", "N/A", "n/a", "$", "*"):
        return None
    sign = -1.0 if s.startswith("(") and s.endswith(")") else 1.0
    if sign < 0:
        s = s[1:-1]
    s = _DOLLAR_RE.sub("", s)
    if not s or s in ("-", "—", "–"):
        return None
    try:
        return sign * float(s)
    except ValueError:
        return None


def parse_form_144_date(raw: str | None) -> str | None:
    """Normalize a Form 144 date into ``YYYY-MM-DD``.

    Handles multiple input forms encountered across XML + HTML eras:
    - XML modern: ``"01/16/2024"`` (MM/DD/YYYY)
    - XML alt:    ``"2024-01-16"`` (already ISO)
    - HTML:       ``"January 16, 2024"`` / ``"1/16/14"`` / ``"01-16-2024"``

    Returns None if the input doesn't match any known shape (caller decides
    whether to retry with a looser regex or write NULL).
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    s = re.sub(r"\s+", " ", s)

    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1900 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"

    m = re.match(r"^(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})$", s)
    if m:
        mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000 if y < 50 else 1900
        if 1900 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"

    months = {
        "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
        "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
        "november": 11, "december": 12,
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
        "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    m = re.match(r"^([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})$", s)
    if m:
        mo_name = m.group(1).lower().rstrip(".")
        mo = months.get(mo_name)
        d = int(m.group(2))
        y = int(m.group(3))
        if mo and 1900 <= y <= 2100 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"

    return None

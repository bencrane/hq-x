"""Pure-functional normalizers for SEC EDGAR DEF 14A ingest.

All functions accept the source-string-as-found in the filing and return a
normalized form suitable for cross-source identity joins. Untestable I/O is
deliberately excluded — callers handle fetch + DB writes.

Identity-spine standard:
- ``cik_normalized`` — 10-digit zero-padded CIK string.
- ``filer_legal_name_normalized`` — uppercase, single-space, with the corporate
  suffix preserved (joins to GLEIF / FDIC / Form 990).
- ``person_first_normalized`` / ``person_last_normalized`` — uppercase,
  punctuation stripped, "Jr."/"Sr."/"III" suffixes preserved as a separate
  tail token. Joins to FEC donors / Form 990 board members on the (first, last)
  pair plus an employer/filer key.
- ``person_title_normalized`` — collapsed to a small canonical set
  ("CEO", "CFO", "PRESIDENT", "DIRECTOR", "EVP", "CIO", "COO", "CHAIRMAN",
  "CHIEF CREDIT OFFICER", "CHIEF RISK OFFICER", ...) when the source string
  matches an unambiguous abbreviation; otherwise the uppercased source string
  is preserved verbatim.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final


_WHITESPACE_RE: Final = re.compile(r"\s+")
_PUNCT_RE: Final = re.compile(r"[^\w\s\-\.&]")
_NAME_PUNCT_RE: Final = re.compile(r"[^\w\s\-]")
_DOLLAR_RE: Final = re.compile(r"[\$,\s]")

# Canonical title map. Keys are uppercased / collapsed; matched against the
# uppercase-stripped source string. Order matters for substring fall-throughs:
# longest patterns first.
_TITLE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bCHIEF\s+EXECUTIVE\s+OFFICER\b"), "CEO"),
    (re.compile(r"\bCHIEF\s+FINANCIAL\s+OFFICER\b"), "CFO"),
    (re.compile(r"\bCHIEF\s+OPERATING\s+OFFICER\b"), "COO"),
    (re.compile(r"\bCHIEF\s+INFORMATION\s+OFFICER\b"), "CIO"),
    (re.compile(r"\bCHIEF\s+TECHNOLOGY\s+OFFICER\b"), "CTO"),
    (re.compile(r"\bCHIEF\s+CREDIT\s+OFFICER\b"), "CHIEF CREDIT OFFICER"),
    (re.compile(r"\bCHIEF\s+RISK\s+OFFICER\b"), "CHIEF RISK OFFICER"),
    (re.compile(r"\bCHIEF\s+LEGAL\s+OFFICER\b"), "CHIEF LEGAL OFFICER"),
    (re.compile(r"\bGENERAL\s+COUNSEL\b"), "GENERAL COUNSEL"),
    (re.compile(r"\bEXECUTIVE\s+VICE\s+PRESIDENT\b"), "EVP"),
    (re.compile(r"\bSENIOR\s+VICE\s+PRESIDENT\b"), "SVP"),
    (re.compile(r"\bVICE\s+CHAIRMAN\b"), "VICE CHAIRMAN"),
    (re.compile(r"\bCHAIRMAN(\s+OF\s+THE\s+BOARD)?\b"), "CHAIRMAN"),
    (re.compile(r"\bPRESIDENT\b"), "PRESIDENT"),
    (re.compile(r"\bTREASURER\b"), "TREASURER"),
    (re.compile(r"\bSECRETARY\b"), "SECRETARY"),
    (re.compile(r"\bDIRECTOR\b"), "DIRECTOR"),
)

_NAME_SUFFIX_TOKENS: frozenset[str] = frozenset({
    "JR", "JR.", "SR", "SR.", "II", "III", "IV", "V", "ESQ", "ESQ.", "PHD",
    "PH.D.", "MD", "M.D.", "CPA", "CFA",
})


def normalize_cik(raw: str | int | None) -> str | None:
    """Zero-pad a CIK to 10 digits. Strips leading zeros first to handle the
    EDGAR practice of optionally embedding non-zero-padded CIKs in URLs.
    """
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
    """SEC accession numbers are emitted in two forms:
      - dashed: ``0001193125-24-123456``
      - flat:   ``0001193125-24-123456`` → ``000119312524123456``
    Always return the dashed canonical form (XXXXXXXXXX-XX-XXXXXX).
    """
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
    Preserves &, -, ., and corporate-suffix tokens (INC / CORP / LLC / NA).

    Note: caller may want a ``filer_legal_name_normalized_compact`` variant
    that strips the suffix; that's a downstream concern.
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
    1. Strip all parenthetical asides, e.g. ``"(2)"``, ``"(Director)"``.
    2. Strip honorific titles (DR., MR., MS., MRS.) at start.
    3. Strip suffix tokens (JR., III, ESQ., PHD).
    4. Comma form ``"Last, First Middle"`` → flip to ``"First Middle Last"``.
    5. First token = first; last surviving token = last; middle tokens dropped
       for join purposes (kept verbatim by caller in raw column).

    Returns ``(None, None)`` for empty / single-token input.
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

    # Strip honorific tail. Allow dotted forms: "PH.D.", "M.D.", "PH D".
    s = re.sub(r",?\s+(ESQ|PH\.?\s?D|M\.?\s?D|CPA|CFA)\.?$", "", s)

    if "," in s:
        # "Smith, John A." → "John A. Smith"
        parts = [p.strip() for p in s.split(",", 1)]
        if len(parts) == 2 and parts[1]:
            s = f"{parts[1]} {parts[0]}"

    s = _NAME_PUNCT_RE.sub(" ", s)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    tokens = [t for t in s.split() if t]
    # Drop trailing suffix tokens for join key purposes
    while tokens and tokens[-1].rstrip(".") in _NAME_SUFFIX_TOKENS:
        tokens.pop()
    if len(tokens) < 2:
        return (None, None)
    first = tokens[0]
    last = tokens[-1]
    return (first or None, last or None)


def normalize_title(raw: str | None) -> str | None:
    """Map a title string to a canonical form. Falls back to upper-stripped
    source if no canonical match.
    """
    if raw is None:
        return None
    s = unicodedata.normalize("NFKD", raw)
    s = s.encode("ascii", "ignore").decode("ascii").strip().upper()
    if not s:
        return None
    s = _WHITESPACE_RE.sub(" ", s)
    for pat, canonical in _TITLE_PATTERNS:
        if pat.search(s):
            return canonical
    return s


def parse_dollar_amount(raw: str | None) -> float | None:
    """Parse SEC compensation dollar strings:
      - ``"$1,234,567"`` → 1234567.0
      - ``"1,234,567(1)"`` → 1234567.0  (footnote markers stripped)
      - ``"—"`` / ``"-"`` / ``"N/A"`` → None
      - ``"$"``-only or empty → None
    Negative values (rare — claw-back) preserved.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # Strip footnote markers like ``(1)``, ``(2)``
    s = re.sub(r"\s*\(\d+\)\s*", "", s)
    if s in ("-", "—", "–", "N/A", "n/a", "—", "$"):
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


def parse_percent(raw: str | None) -> float | None:
    """Parse beneficial-ownership percent strings:
      - ``"1.2%"`` → 1.2
      - ``"*"`` → None  (SEC convention: less than 1%, not disclosed exactly)
      - ``"<1%"`` → None
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s in ("*", "—", "-", "N/A"):
        return None
    s = re.sub(r"<\s*", "", s)
    s = s.rstrip("%").strip()
    s = _DOLLAR_RE.sub("", s)
    try:
        return float(s)
    except ValueError:
        return None


def parse_share_count(raw: str | None) -> int | None:
    """Parse beneficial-ownership share-count strings.
    Strips footnote markers and commas. Returns None on empty / dash."""
    if raw is None:
        return None
    s = str(raw).strip()
    s = re.sub(r"\s*\(\d+\)\s*", "", s)
    if not s or s in ("-", "—", "N/A"):
        return None
    s = re.sub(r"[,\s]", "", s)
    try:
        return int(float(s))
    except ValueError:
        return None

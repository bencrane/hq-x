"""Pure-functional normalizers for SEC EDGAR Form 8-K ingest.

Identity-spine standard:
- ``cik_normalized`` — 10-digit zero-padded CIK string.
- ``company_name_normalized`` — uppercase, single-space, suffix preserved.
- ``officer_name_normalized`` / ``officer_first_normalized`` /
  ``officer_last_normalized`` — uppercase, punctuation stripped, suffix tokens
  (Jr/Sr/III/etc.) dropped from the join key.
- ``role_normalized`` — collapsed lowercase canonical form
  (``"ceo"``, ``"cfo"``, ``"director"``, ``"president"``, etc.) — matches the
  Item 5.02 GTM signal lexicon.
- ``event_type`` — for Item 5.02 events:
  ``"departure" | "appointment" | "election" | "compensation_amendment" | "unknown"``.
- ``agreement_type`` — for Item 1.01 events:
  ``"merger" | "acquisition" | "joint_venture" | "licensing" |
   "credit_agreement" | "employment" | "settlement" | "unknown"``.
- ``parse_event_date`` — extracts an effective date from the cover-page or
  Item-narrative free-text spans. Tolerates "January 15, 2024",
  "1/15/2024", "January 15th 2024", "as of January 15".
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from typing import Final


_WHITESPACE_RE: Final = re.compile(r"\s+")
_PUNCT_RE: Final = re.compile(r"[^\w\s\-\.&]")
_NAME_PUNCT_RE: Final = re.compile(r"[^\w\s\-]")
_DOLLAR_RE: Final = re.compile(r"[\$,\s]")

_NAME_SUFFIX_TOKENS: frozenset[str] = frozenset({
    "JR", "JR.", "SR", "SR.", "II", "III", "IV", "V", "ESQ", "ESQ.", "PHD",
    "PH.D.", "MD", "M.D.", "CPA", "CFA",
})

# Role classification — order matters: longest match wins.
_ROLE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bchief\s+executive\s+officer\b", re.I), "ceo"),
    (re.compile(r"\bchief\s+financial\s+officer\b", re.I), "cfo"),
    (re.compile(r"\bchief\s+operating\s+officer\b", re.I), "coo"),
    (re.compile(r"\bchief\s+information\s+officer\b", re.I), "cio"),
    (re.compile(r"\bchief\s+technology\s+officer\b", re.I), "cto"),
    (re.compile(r"\bchief\s+accounting\s+officer\b", re.I), "cao"),
    (re.compile(r"\bchief\s+credit\s+officer\b", re.I), "chief_credit_officer"),
    (re.compile(r"\bchief\s+risk\s+officer\b", re.I), "chief_risk_officer"),
    (re.compile(r"\bchief\s+legal\s+officer\b", re.I), "chief_legal_officer"),
    (re.compile(r"\bchief\s+compliance\s+officer\b", re.I), "chief_compliance_officer"),
    (re.compile(r"\bchief\s+investment\s+officer\b", re.I), "chief_investment_officer"),
    (re.compile(r"\bchief\s+marketing\s+officer\b", re.I), "chief_marketing_officer"),
    (re.compile(r"\bgeneral\s+counsel\b", re.I), "general_counsel"),
    (re.compile(r"\bexecutive\s+vice\s+president\b", re.I), "evp"),
    (re.compile(r"\bsenior\s+vice\s+president\b", re.I), "svp"),
    (re.compile(r"\bvice\s+chairman\b", re.I), "vice_chairman"),
    (re.compile(r"\bchairman(\s+of\s+the\s+board)?\b", re.I), "chairman"),
    (re.compile(r"\bpresident\b", re.I), "president"),
    (re.compile(r"\btreasurer\b", re.I), "treasurer"),
    (re.compile(r"\bsecretary\b", re.I), "secretary"),
    (re.compile(r"\bdirector\b", re.I), "director"),
    (re.compile(r"\bvice\s+president\b", re.I), "vp"),
)

# Event-type classification for Item 5.02 narrative spans.
_DEPARTURE_PATTERNS = (
    re.compile(r"\bresign(ed|ation|s|ing)\b", re.I),
    re.compile(r"\bretire(d|s|ment|ing)\b", re.I),
    re.compile(r"\bstep(s|ped)\s+down\b", re.I),
    re.compile(r"\bdepart(ed|ure|s|ing)\b", re.I),
    re.compile(r"\bterminat(ed|ion|es|ing)\b", re.I),
    re.compile(r"\bremoved\s+from\s+(office|position)\b", re.I),
    re.compile(r"\bno\s+longer\s+(serve|be|remain)\b", re.I),
)
_APPOINTMENT_PATTERNS = (
    re.compile(r"\bappoint(ed|ment|s|ing)\b", re.I),
    re.compile(r"\bnamed\s+(as\s+)?(the\s+)?(new\s+)?(chief|president|chairman|director|chair)\b", re.I),
    re.compile(r"\bhired\b", re.I),
    re.compile(r"\bbegin(s|ning)?\s+service\s+as\b", re.I),
    re.compile(r"\bwill\s+(serve|become|assume)\b", re.I),
    re.compile(r"\bnew(ly)?\s+(appointed|hired|named)\b", re.I),
)
_ELECTION_PATTERNS = (
    re.compile(r"\belect(ed|ion|s|ing)\s+(to\s+the\s+board|as\s+a\s+director)\b", re.I),
    re.compile(r"\bnominated\s+for\s+election\b", re.I),
)
_COMP_AMEND_PATTERNS = (
    re.compile(r"\bamend(ed|ment|s|ing)\b.*\b(compensat|employment\s+agreement|severance|equity|grant)\b", re.I),
    re.compile(r"\bcompensat\w+\s+(arrangement|agreement|plan)\b", re.I),
    re.compile(r"\baward(ed|s)?\s+(equity|stock|options|rsu|psu)\b", re.I),
    re.compile(r"\bgrant(ed|s)?\s+(equity|stock|options|rsu|psu)\b", re.I),
    re.compile(r"\bsign[\-\s]on\s+bonus\b", re.I),
    re.compile(r"\bretention\s+(award|bonus|agreement|grant)\b", re.I),
)

# Agreement-type classification for Item 1.01 narrative spans.
_AGREEMENT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bmerger\s+agreement\b", re.I), "merger"),
    (re.compile(r"\bagreement\s+and\s+plan\s+of\s+merger\b", re.I), "merger"),
    (re.compile(r"\b(asset|stock|share|equity)\s+purchase\s+agreement\b", re.I), "acquisition"),
    (re.compile(r"\bacquisition\s+agreement\b", re.I), "acquisition"),
    (re.compile(r"\bjoint\s+venture\b", re.I), "joint_venture"),
    (re.compile(r"\b(license|licensing)\s+agreement\b", re.I), "licensing"),
    (re.compile(r"\b(credit\s+agreement|loan\s+agreement|note\s+purchase\s+agreement|indenture)\b", re.I), "credit_agreement"),
    (re.compile(r"\b(employment\s+agreement|consulting\s+agreement)\b", re.I), "employment"),
    (re.compile(r"\b(settlement\s+agreement|stipulation)\b", re.I), "settlement"),
)

# Obligation-type classification for Item 2.03 narrative spans. Order matters —
# longest / most-specific match wins. Reflects credit-facility taxonomy seen
# across the 8-K corpus: revolving credit / warehouse line are the GTM signals;
# senior notes + indentures are the canonical bond shapes; guaranty is the
# sponsor-pledge shape.
_OBLIGATION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bwarehouse\s+(?:line|facility|agreement)\b", re.I), "warehouse_line"),
    (re.compile(r"\brevolving\s+credit\s+(?:facility|agreement)\b", re.I), "revolving_credit"),
    (re.compile(r"\bcredit\s+(?:agreement|facility)\b", re.I), "credit_facility"),
    (re.compile(r"\bterm\s+loan(?:\s+agreement)?\b", re.I), "term_loan"),
    (re.compile(r"\bconvertible\s+(?:note|notes|debenture)\b", re.I), "convertible_note"),
    (re.compile(r"\bsenior\s+(?:secured|unsecured)?\s*(?:notes?|debenture)\b", re.I), "senior_note"),
    (re.compile(r"\bsubordinated\s+(?:notes?|debenture)\b", re.I), "subordinated_note"),
    (re.compile(r"\bguarant(?:y|ee)\s+agreement\b", re.I), "guaranty"),
    (re.compile(r"\bindenture\b", re.I), "indenture"),
    (re.compile(r"\bnote\s+purchase\s+agreement\b", re.I), "note_purchase"),
    (re.compile(r"\bsecurit(?:y|ies)\s+purchase\s+agreement\b", re.I), "securities_purchase"),
    (re.compile(r"\bloan\s+agreement\b", re.I), "loan_agreement"),
    (re.compile(r"\boff[\-\s]balance[\-\s]sheet\s+arrangement\b", re.I), "off_balance_sheet"),
)

# Date patterns for parse_event_date.
_MONTH_NAME_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+"
    r"(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b",
    re.I,
)
_NUMERIC_DATE_RE = re.compile(
    r"\b(0?[1-9]|1[0-2])[\/\-](0?[1-9]|[12]\d|3[01])[\/\-](\d{2}|\d{4})\b"
)
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")

_MONTH_NAME_TO_NUM = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}


def normalize_cik(raw: str | int | None) -> str | None:
    """Zero-pad a CIK to 10 digits."""
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
    """Return SEC accession in dashed canonical form (XXXXXXXXXX-XX-XXXXXX)."""
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) != 18:
        return None
    return f"{digits[0:10]}-{digits[10:12]}-{digits[12:18]}"


def normalize_company_name(raw: str | None) -> str | None:
    """Uppercase + collapse whitespace + strip non-name punctuation.
    Preserves &, -, ., and corporate-suffix tokens (INC / CORP / LLC / NA).
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


def normalize_officer_name(raw: str | None) -> tuple[str | None, str | None, str | None]:
    """Return ``(name_normalized, first_normalized, last_normalized)``.

    Heuristics mirror the DEF 14A normalizer plus a name-collapse for the join
    key:
    1. Strip parentheticals (``"(2)"``, ``"(Director)"``).
    2. Strip honorifics (DR/MR/MS/MRS) at the start.
    3. Strip suffix tokens (JR/III/ESQ/PHD).
    4. Comma form ``"Last, First Middle"`` → flip.
    5. First token = first; last surviving token = last.

    Returns ``(None, None, None)`` for empty / single-token input.
    """
    if raw is None:
        return (None, None, None)
    s = unicodedata.normalize("NFKD", raw)
    s = s.encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"\([^)]*\)", " ", s)
    s = s.strip().upper()
    if not s:
        return (None, None, None)
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
        return (None, None, None)
    first = tokens[0]
    last = tokens[-1]
    name_normalized = " ".join(tokens)
    return (name_normalized, first, last)


def normalize_role(raw: str | None) -> str | None:
    """Map a role string to a canonical lowercase form. Falls back to a
    lower-stripped source if no canonical pattern matches.
    """
    if raw is None:
        return None
    s = unicodedata.normalize("NFKD", raw)
    s = s.encode("ascii", "ignore").decode("ascii").strip()
    if not s:
        return None
    s = _WHITESPACE_RE.sub(" ", s)
    for pat, canonical in _ROLE_PATTERNS:
        if pat.search(s):
            return canonical
    # Fallback: lowercase compact form, words separated by underscores. Keep
    # it short so it joins cleanly downstream without explosion.
    fallback = re.sub(r"[^\w\s]", " ", s.lower())
    fallback = _WHITESPACE_RE.sub(" ", fallback).strip()
    return fallback.replace(" ", "_") or None


def classify_event_type(text: str | None) -> str:
    """Heuristic classification for an Item 5.02 narrative span.

    Ordering: comp-amendment > departure > appointment > election > unknown.
    A single Item 5.02 may contain multiple events; the classifier returns the
    most specific match against the *containing* span the parser hands in
    (typically a paragraph or sentence).
    """
    if not text:
        return "unknown"
    if any(p.search(text) for p in _COMP_AMEND_PATTERNS):
        return "compensation_amendment"
    if any(p.search(text) for p in _DEPARTURE_PATTERNS):
        return "departure"
    if any(p.search(text) for p in _APPOINTMENT_PATTERNS):
        return "appointment"
    if any(p.search(text) for p in _ELECTION_PATTERNS):
        return "election"
    return "unknown"


def classify_agreement_type(text: str | None) -> str:
    """Heuristic classification for an Item 1.01 material-agreement narrative."""
    if not text:
        return "unknown"
    for pat, kind in _AGREEMENT_PATTERNS:
        if pat.search(text):
            return kind
    return "unknown"


def classify_obligation_type(text: str | None) -> str:
    """Heuristic classification for an Item 2.03 direct-financial-obligation narrative.

    Returns one of: warehouse_line | revolving_credit | credit_facility |
    term_loan | convertible_note | senior_note | subordinated_note | guaranty |
    indenture | note_purchase | securities_purchase | loan_agreement |
    off_balance_sheet | unknown.

    Order matters in `_OBLIGATION_PATTERNS` — longest / most-specific match wins.
    """
    if not text:
        return "unknown"
    for pat, kind in _OBLIGATION_PATTERNS:
        if pat.search(text):
            return kind
    return "unknown"


def parse_event_date(text: str | None) -> str | None:
    """Extract an ISO YYYY-MM-DD date from an Item-narrative span. Returns the
    first plausible date. Tolerates "January 15, 2024", "1/15/2024",
    "2024-01-15".
    """
    if not text:
        return None
    m = _MONTH_NAME_RE.search(text)
    if m:
        month_name, day, year = m.group(1).lower(), m.group(2), m.group(3)
        month = _MONTH_NAME_TO_NUM.get(month_name)
        if month:
            try:
                return date(int(year), month, int(day)).isoformat()
            except (ValueError, OverflowError):
                pass
    m = _ISO_DATE_RE.search(text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except (ValueError, OverflowError):
            pass
    m = _NUMERIC_DATE_RE.search(text)
    if m:
        mm, dd, yy = m.group(1), m.group(2), m.group(3)
        if len(yy) == 2:
            yy_int = int(yy)
            year = 2000 + yy_int if yy_int < 50 else 1900 + yy_int
        else:
            year = int(yy)
        try:
            return date(year, int(mm), int(dd)).isoformat()
        except (ValueError, OverflowError):
            pass
    return None


def parse_filing_date(raw: str | None) -> str | None:
    """Parse EDGAR form.idx ``Date Filed`` field (canonical YYYY-MM-DD)."""
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


def parse_dollar_amount(raw: str | None) -> float | None:
    """Parse SEC compensation dollar strings:
      - ``"$1,234,567"`` → 1234567.0
      - ``"1,234,567(1)"`` → 1234567.0
      - ``"-"`` / ``"—"`` / ``"N/A"`` / empty → None
    Negative values (claw-back) preserved.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    s = re.sub(r"\s*\(\d+\)\s*", "", s)
    if s in ("-", "—", "–", "N/A", "n/a", "$"):
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

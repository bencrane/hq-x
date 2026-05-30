"""Pure-functional normalizers for SEC EDGAR Form ABS-15G ingest.

Mirrors the shape of sec_edgar_form_13f_normalize.py. Light-touch only —
deep canonicalization belongs in downstream MVs.

Normalizers:
- ``normalize_cik`` — 10-digit zero-padded.
- ``normalize_accession`` — canonical dashed form ``XXXXXXXXXX-XX-XXXXXX``.
- ``normalize_sponsor_name`` / ``normalize_trustee_name`` / ``normalize_filer_name``
  — uppercase + ASCII + collapse whitespace + strip non-name punctuation.
- ``normalize_lei`` — uppercased 20-char alphanumeric or None.
- ``normalize_asset_class`` — map raw ABS asset-class text → canonical token.
- ``parse_period_of_report`` — SEC writes the period as ``MM-DD-YYYY``;
  emit ``YYYY-MM-DD``.
- ``parse_signature_date`` — same MM-DD-YYYY → ISO conversion.
- ``derive_quarter`` — quarter (1..4) from a YYYY-MM-DD period_of_report.
- ``parse_int`` — lenient int parse.
- ``parse_bool`` — lenient true/false parse for indemnification flags.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final


_WHITESPACE_RE: Final = re.compile(r"\s+")
_PUNCT_RE: Final = re.compile(r"[^\w\s\-\.&]")
_LEI_RE: Final = re.compile(r"^[A-Z0-9]{20}$")


# Canonical asset-class token map. Keys are normalized (lowercase, trimmed,
# no punctuation); values are the canonical tokens emitted to parquet.
_ASSET_CLASS_MAP: Final[dict[str, str]] = {
    # Residential mortgage variants
    "residential mortgage": "residential_mortgage",
    "residential mortgages": "residential_mortgage",
    "residential mortgage loan": "residential_mortgage",
    "residential mortgage loans": "residential_mortgage",
    "rmbs": "residential_mortgage",
    "rmbs prime": "residential_mortgage",
    "rmbs subprime": "residential_mortgage",
    "rmbs altA": "residential_mortgage",
    # Commercial mortgage
    "commercial mortgage": "commercial_mortgage",
    "commercial mortgages": "commercial_mortgage",
    "commercial mortgage loan": "commercial_mortgage",
    "commercial mortgage loans": "commercial_mortgage",
    "cmbs": "commercial_mortgage",
    # Auto loan / lease
    "auto loan": "auto_loan",
    "auto loans": "auto_loan",
    "automobile loan": "auto_loan",
    "automobile loans": "auto_loan",
    "auto lease": "auto_lease",
    "auto leases": "auto_lease",
    "automobile lease": "auto_lease",
    "automobile leases": "auto_lease",
    # Credit card
    "credit card": "credit_card",
    "credit cards": "credit_card",
    "credit card receivables": "credit_card",
    # Student loan
    "student loan": "student_loan",
    "student loans": "student_loan",
    "private student loan": "student_loan",
    "private student loans": "student_loan",
    # Equipment lease / floorplan
    "equipment lease": "equipment_lease",
    "equipment leases": "equipment_lease",
    "equipment loan": "equipment_lease",
    "equipment loans": "equipment_lease",
    "floorplan": "floorplan",
    "floor plan": "floorplan",
    "dealer floorplan": "floorplan",
    "dealer floor plan": "floorplan",
}


def normalize_cik(raw: str | int | None) -> str | None:
    """Zero-pad a CIK to 10 digits."""
    if raw is None:
        return None
    s = str(raw).strip() if not isinstance(raw, int) else str(raw)
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    if not digits or len(digits) > 10:
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


def _normalize_name(raw: str | None) -> str | None:
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


def normalize_filer_name(raw: str | None) -> str | None:
    """Uppercase + ASCII-fold + collapse whitespace."""
    return _normalize_name(raw)


def normalize_sponsor_name(raw: str | None) -> str | None:
    return _normalize_name(raw)


def normalize_trustee_name(raw: str | None) -> str | None:
    return _normalize_name(raw)


def normalize_depositor_name(raw: str | None) -> str | None:
    return _normalize_name(raw)


def normalize_lei(raw: str | None) -> str | None:
    """LEI canonical form is 20-char alphanumeric uppercase."""
    if raw is None:
        return None
    s = raw.strip().upper()
    if not s:
        return None
    return s if _LEI_RE.match(s) else None


def normalize_asset_class(raw: str | None) -> str | None:
    """Map raw ABS asset-class text → canonical token.

    Returns one of:
      residential_mortgage, commercial_mortgage, auto_loan, auto_lease,
      credit_card, student_loan, equipment_lease, floorplan, other

    Returns None on empty/missing input. Returns ``other`` for non-empty
    but unrecognized text — keeps the canonical token enum closed without
    silently dropping unfamiliar asset classes.
    """
    if raw is None:
        return None
    s = raw.strip().lower()
    if not s:
        return None
    # Strip surrounding punctuation, collapse whitespace.
    s = re.sub(r"[^\w\s]+", " ", s)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    if not s:
        return None
    if s in _ASSET_CLASS_MAP:
        return _ASSET_CLASS_MAP[s]
    # Substring fallback (e.g. "subprime residential mortgage loans 2014-A")
    for key, val in _ASSET_CLASS_MAP.items():
        if key in s:
            return val
    return "other"


def parse_period_of_report(raw: str | None) -> str | None:
    """SEC writes ABS-15G periods as ``MM-DD-YYYY``; emit ``YYYY-MM-DD``.

    Returns None on malformed input. Tolerates ``YYYY-MM-DD`` already-ISO
    inputs (some ABS-15G headers are pre-converted upstream).
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    m = re.match(r"^(\d{2})-(\d{2})-(\d{4})$", s)
    if m:
        mm, dd, yyyy = m.group(1), m.group(2), m.group(3)
        return f"{yyyy}-{mm}-{dd}"
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s
    return None


def parse_signature_date(raw: str | None) -> str | None:
    return parse_period_of_report(raw)


def derive_quarter(iso_date: str | None) -> int | None:
    """Map a YYYY-MM-DD date to its calendar quarter (1..4)."""
    if iso_date is None:
        return None
    m = re.match(r"^\d{4}-(\d{2})-\d{2}$", iso_date)
    if not m:
        return None
    month = int(m.group(1))
    if 1 <= month <= 3:
        return 1
    if 4 <= month <= 6:
        return 2
    if 7 <= month <= 9:
        return 3
    if 10 <= month <= 12:
        return 4
    return None


def parse_int(raw: str | int | None) -> int | None:
    """Lenient int parse — strips commas/whitespace/dollar signs."""
    if raw is None:
        return None
    if isinstance(raw, int):
        return raw
    s = re.sub(r"[,\s$]", "", str(raw).strip())
    if not s or s in ("-", "—"):
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def parse_bool(raw: str | None) -> bool | None:
    """Lenient true/false for indemnification flags."""
    if raw is None:
        return None
    s = raw.strip().lower()
    if not s:
        return None
    if s in ("true", "1", "yes", "y", "t"):
        return True
    if s in ("false", "0", "no", "n", "f"):
        return False
    return None

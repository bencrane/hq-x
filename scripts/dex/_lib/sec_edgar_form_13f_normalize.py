"""Pure-functional normalizers for SEC EDGAR Form 13F ingest.

Light-touch only. Per directive 2026-05-09-sec-edgar-form-13f-r2-ingest.md
§Out-of-scope: deep canonicalization (e.g., disambiguating "JPMORGAN CHASE
& CO" vs "JP MORGAN CHASE & CO") belongs in a downstream LLM-assisted MV,
not here. The normalizers here only:

- ``cik_normalized`` — 10-digit zero-padded.
- ``manager_name_normalized`` / ``issuer_name_normalized`` — uppercase + ASCII
  + collapse whitespace + strip non-name punctuation. Suffix tokens (LLC, INC,
  CORP, LP) preserved.
- ``lei_normalized`` — uppercased 20-char alphanumeric or None.
- ``parse_period_of_report`` — SEC writes the period as ``MM-DD-YYYY``
  (e.g. ``12-31-2023``); convert to ``YYYY-MM-DD``.
- ``parse_signature_date`` — same MM-DD-YYYY → ISO conversion.
- ``derive_quarter`` — quarter (1..4) from a YYYY-MM-DD period_of_report.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final


_WHITESPACE_RE: Final = re.compile(r"\s+")
_PUNCT_RE: Final = re.compile(r"[^\w\s\-\.&]")
_LEI_RE: Final = re.compile(r"^[A-Z0-9]{20}$")


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


def normalize_manager_name(raw: str | None) -> str | None:
    """Uppercase + ASCII-fold + collapse whitespace.

    Light-touch: corporate-suffix tokens (LLC, INC, CORP, LP) preserved.
    Caller's responsibility to do deeper canonicalization downstream.
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


def normalize_issuer_name(raw: str | None) -> str | None:
    """Same shape as ``normalize_manager_name``; separate symbol so callers
    can grep for "issuer" vs "manager" normalization separately.
    """
    return normalize_manager_name(raw)


def normalize_lei(raw: str | None) -> str | None:
    """LEI canonical form is 20-char alphanumeric uppercase."""
    if raw is None:
        return None
    s = raw.strip().upper()
    if not s:
        return None
    return s if _LEI_RE.match(s) else None


def parse_period_of_report(raw: str | None) -> str | None:
    """SEC writes 13F periods as ``MM-DD-YYYY``; emit ``YYYY-MM-DD``.

    Returns None on malformed input. Tolerates ``YYYY-MM-DD`` already-ISO
    inputs (some 13F headers are pre-converted upstream).
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

"""SEC BDC SOI v2 — field-level classifier.

Stateless classifier module consumed by parse_sec_bdc_soi_html_v2.py (s3).
Each rule is a pure function: input a raw cell or row context, output
(cleaned_value, parse_confidence, demotion_reason_or_None, rule_id).

Per L54 Lance compatibility: parse_demotion_reason + demoted_by_rule_ids are
pipe-joined VARCHAR; the classifier returns lists per field which the parser
pipe-joins at row-emit time. Lance 1.5.x consumes pipe-joined strings without
the definition-buffer cap.

Pinned constants:
- IS_DEBT_INSTRUMENT_RE (reviewer-corrected 2026-05-22) — extends v1's pattern.
  The original `r'(?i)\\b(lien|loan|note|bond|debt|term|unitranche|revolv)\\b'`
  FAILS to match "Revolving Credit Facility" / "Revolver" because the trailing
  \\b (word boundary) requires `revolv` to end the token. Corrected pattern
  allows the optional suffix:
    r'(?i)\\b(lien|loan|note|bond|debt|term|unitranche|revolv\\w*)'
  Ares 2025q1 sampling: first lien=3168, second lien=226, delayed draw=55,
  unitranche=21, revolv*=927 (a population that the original pattern would
  miss entirely → 927 rows would incorrectly classify as non-debt and have
  maturity_date suppressed). Pattern MUST match unitranche and revolv-prefix
  tokens (validator+reviewer-pinned).

parse_confidence enum:
  verified_exact    — sourced from soi.tsv XBRL-tagged column
  inferred_anchored — HTML-extracted; classifier-validated location
  rejected          — classifier rejected; column emits NULL + parse_demotion_reason set

Enumerated parse_demotion_reason codes (per directive §"Schema"):
  name_footnote_ref_stripped                  — informational; NOT a confidence demotion
  name_fallback_placeholder                   — 'Company (N)' pattern; demotes to inferred_anchored
  maturity_date_suppressed_for_non_debt_instrument — equity/preferred/units/warrants row
  principal_unparseable                       — numeric parse failed; raw preserved
  interest_rate_format_unrecognized           — regex decompose failed on rate string
  cusip_checksum_invalid                      — CUSIP present but checksum digit mismatch
  column_alignment_anomaly                    — HTML colspan→logical-grid row width mismatch
  sentinel_value_detected                     — REDACTED / N/A / [NULL] / em-dash
  parser_partial_confidence                   — catch-all for unspecified low-confidence cases
"""
from __future__ import annotations

import re
from typing import Optional

__version__ = "v2.0.0"

# ── Public constants ──────────────────────────────────────────────────────────

PARSE_CONFIDENCE_VALUES = ("verified_exact", "inferred_anchored", "rejected")

DEMOTION_CODES = (
    "name_footnote_ref_stripped",
    "name_fallback_placeholder",
    "maturity_date_suppressed_for_non_debt_instrument",
    "principal_unparseable",
    "interest_rate_format_unrecognized",
    "cusip_checksum_invalid",
    "column_alignment_anomaly",
    "sentinel_value_detected",
    "parser_partial_confidence",
)

# Reviewer-corrected 2026-05-22: original `revolv\b` failed to match
# "Revolving" / "Revolver" because \b requires non-word char after revolv.
# `revolv\w*` allows the trailing suffix; \b before the alternation still
# anchors a token boundary at the start.
IS_DEBT_INSTRUMENT_RE = re.compile(
    r"(?i)\b(lien|loan|note|bond|debt|term|unitranche|revolv\w*)"
)

# Footnote-reference pattern: trailing "(15)" or "(15)(16)" suffixes
_FOOTNOTE_REF_RE = re.compile(r"(\(\d+\)(\(\d+\))*\s*)$")

# Placeholder pattern: "Company (N)" where N is one or more digits
_PLACEHOLDER_RE = re.compile(r"^Company\s*\(\d+\)\s*$", re.IGNORECASE)

# Sentinel values
_SENTINEL_RE = re.compile(
    r"^\s*(REDACTED|N/A|NA|\[NULL\]|NULL|—|–|-{2,})\s*$", re.IGNORECASE
)

# Interest-rate patterns
_RATE_BASE_SOFR = re.compile(r"\bSOFR\b", re.IGNORECASE)
_RATE_BASE_PRIME = re.compile(r"\bPRIME\b", re.IGNORECASE)
_RATE_BASE_LIBOR = re.compile(r"\b(LIBOR|L)\b", re.IGNORECASE)
_RATE_FIXED = re.compile(r"\bFIXED\b", re.IGNORECASE)
_RATE_PIK = re.compile(r"\bPIK\b", re.IGNORECASE)
_RATE_SPREAD_RE = re.compile(r"[+\s]+(\d+\.?\d*)\s*%")
_RATE_FLOOR_RE = re.compile(r"(\d+\.?\d*)\s*%\s*floor", re.IGNORECASE)
_RATE_PIK_VAL_RE = re.compile(r"(\d+\.?\d*)\s*%\s*PIK", re.IGNORECASE)

# Numeric parsing: $123.4M / 123,456 / 1.23B etc.
_NUM_SUFFIX_RE = re.compile(
    r"^\$?\s*([\d,]+\.?\d*)\s*([KkMmBbTt]?)\s*$"
)
_SUFFIX_MULTIPLIERS = {
    "": 1, "k": 1_000, "K": 1_000, "m": 1_000_000, "M": 1_000_000,
    "b": 1_000_000_000, "B": 1_000_000_000, "t": 1_000_000_000_000,
    "T": 1_000_000_000_000,
}

# CUSIP checksum table
_CUSIP_CHECK_TABLE = {str(i): i for i in range(10)}
_CUSIP_CHECK_TABLE.update({c: i + 10 for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ")})
_CUSIP_CHECK_TABLE["*"] = 36
_CUSIP_CHECK_TABLE["@"] = 37
_CUSIP_CHECK_TABLE["#"] = 38


# ── Rule functions (pure, stateless) ─────────────────────────────────────────

def classify_name(
    name: Optional[str],
) -> tuple[Optional[str], Optional[str], str, list[str], list[str]]:
    """Return (cleaned, normalized, confidence, demotion_reasons, rule_ids).

    cleaned strips footnote refs; normalized is lower-cased for downstream use.
    Fires name_fallback_placeholder if `Company (N)` pattern detected.
    Fires name_footnote_ref_stripped if footnote ref was stripped (informational).
    """
    if not name:
        return None, None, "rejected", ["sentinel_value_detected"], ["rule_name_none"]
    raw = name.strip()
    if detect_sentinel(raw):
        return None, None, "rejected", ["sentinel_value_detected"], ["rule_sentinel"]

    reasons: list[str] = []
    rule_ids: list[str] = []
    cleaned = raw

    # Strip trailing footnote refs
    m = _FOOTNOTE_REF_RE.search(cleaned)
    if m:
        cleaned = cleaned[: m.start()].strip()
        reasons.append("name_footnote_ref_stripped")
        rule_ids.append("rule_name_footnote_strip")

    # Check for placeholder
    if _PLACEHOLDER_RE.match(cleaned):
        reasons.append("name_fallback_placeholder")
        rule_ids.append("rule_name_placeholder")
        confidence = "inferred_anchored"
    else:
        confidence = "verified_exact"

    normalized = cleaned.lower().strip() if cleaned else None
    return cleaned or None, normalized, confidence, reasons, rule_ids


def classify_maturity_date(
    raw: Optional[str],
    instrument_type: Optional[str],
    normalize_date_fn,
) -> tuple[Optional[str], str, list[str], list[str]]:
    """Return (typed_iso_date_or_None, confidence, demotion_reasons, rule_ids).

    If is_debt_instrument(instrument_type) is False → NULL +
      maturity_date_suppressed_for_non_debt_instrument.
    Else parse raw via normalize_date helpers.
    """
    reasons: list[str] = []
    rule_ids: list[str] = []

    if instrument_type and not _is_debt_instrument(instrument_type):
        return (
            None,
            "rejected",
            ["maturity_date_suppressed_for_non_debt_instrument"],
            ["rule_maturity_non_debt"],
        )

    if not raw or detect_sentinel(raw):
        return None, "rejected", ["sentinel_value_detected"], ["rule_sentinel"]

    typed = normalize_date_fn(raw)
    if typed:
        return typed, "inferred_anchored", reasons, rule_ids
    return None, "rejected", ["parser_partial_confidence"], ["rule_maturity_parse_fail"]


def classify_principal(
    raw: Optional[str],
) -> tuple[Optional[float], str, list[str], list[str]]:
    """Numeric parse with thousand/million/billion suffix support."""
    if not raw or detect_sentinel(raw):
        return None, "rejected", ["sentinel_value_detected"], ["rule_sentinel"]
    stripped = raw.strip().replace(",", "")
    m = _NUM_SUFFIX_RE.match(stripped)
    if m:
        val_str, suffix = m.group(1), m.group(2)
        try:
            val = float(val_str.replace(",", "")) * _SUFFIX_MULTIPLIERS.get(suffix, 1)
            return val, "verified_exact", [], []
        except ValueError:
            pass
    return (
        None,
        "inferred_anchored",
        ["principal_unparseable"],
        ["rule_principal_parse_fail"],
    )


def classify_interest_rate(
    raw: Optional[str],
) -> tuple[
    tuple[Optional[str], Optional[int], Optional[int], Optional[int]],
    str,
    list[str],
    list[str],
]:
    """Return ((base, spread_bps, floor_bps, pik_bps), confidence, reasons, rule_ids).

    Patterns:
      'SOFR + 5.50%'               → ('SOFR', 550, None, None)
      'SOFR + 5.50%, 1.00% PIK'    → ('SOFR', 550, None, 100)
      'PRIME + 4.00%, 5.50% floor' → ('PRIME', 400, 550, None)
      'Fixed 8.50%'                → ('Fixed', 850, None, None)
      'PIK 10%'                    → ('PIK', None, None, 1000)
    """
    if not raw or detect_sentinel(raw):
        return (None, None, None, None), "rejected", ["sentinel_value_detected"], ["rule_sentinel"]

    s = raw.strip()
    base: Optional[str] = None
    spread_bps: Optional[int] = None
    floor_bps: Optional[int] = None
    pik_bps: Optional[int] = None

    # Detect base rate
    if _RATE_BASE_SOFR.search(s):
        base = "SOFR"
    elif _RATE_BASE_PRIME.search(s):
        base = "PRIME"
    elif _RATE_BASE_LIBOR.search(s):
        base = "LIBOR"
    elif _RATE_FIXED.search(s):
        base = "Fixed"
    elif _RATE_PIK.search(s):
        base = "PIK"

    # Spread
    sm = _RATE_SPREAD_RE.search(s)
    if sm:
        try:
            spread_bps = round(float(sm.group(1)) * 100)
        except ValueError:
            pass

    # Floor
    fm = _RATE_FLOOR_RE.search(s)
    if fm:
        try:
            floor_bps = round(float(fm.group(1)) * 100)
        except ValueError:
            pass

    # PIK component
    pm = _RATE_PIK_VAL_RE.search(s)
    if pm:
        try:
            pik_bps = round(float(pm.group(1)) * 100)
        except ValueError:
            pass

    if base is None and spread_bps is None and floor_bps is None and pik_bps is None:
        return (
            None, None, None, None,
        ), "rejected", ["interest_rate_format_unrecognized"], ["rule_rate_unrecognized"]

    confidence = "verified_exact" if base is not None else "inferred_anchored"
    return (base, spread_bps, floor_bps, pik_bps), confidence, [], []


def classify_cusip(
    raw: Optional[str],
) -> tuple[Optional[str], str, list[str], list[str]]:
    """CUSIP checksum validation (Luhn-like algorithm for CUSIPs).

    The XBRL "Investment, Identifier Axis" field that feeds this classifier
    is a free-text axis BDC filers populate with whatever uniquely identifies
    each investment row — most often a company-name shorthand, tranche
    descriptor, geography ("Canada"), or sector label. CUSIPs themselves are
    rare in BDC SOIs because portfolio companies are typically private.

    Only tag cusip_checksum_invalid when the input plausibly looks like a
    CUSIP attempt (9 chars, all CUSIP-charset, digit check digit). For
    anything else, return without a demotion reason — it was never a CUSIP
    claim in the first place, and tagging it as a "checksum failure"
    pollutes the demotion-reason audit signal.
    """
    if not raw or detect_sentinel(raw):
        return None, "rejected", ["sentinel_value_detected"], ["rule_sentinel"]
    cusip = raw.strip().upper()

    if not _looks_like_cusip(cusip):
        # Not a CUSIP attempt; parser preserves raw as a tracking identifier.
        return None, "inferred_anchored", [], []

    total = 0
    for i, c in enumerate(cusip[:8]):
        v = _CUSIP_CHECK_TABLE[c]
        if i % 2 == 1:
            v *= 2
        total += (v // 10) + (v % 10)
    check = (10 - (total % 10)) % 10
    if str(check) != cusip[8]:
        return cusip, "rejected", ["cusip_checksum_invalid"], ["rule_cusip_check"]
    return cusip, "verified_exact", [], []


def _looks_like_cusip(s: str) -> bool:
    """True iff s could plausibly be a CUSIP attempt.

    CUSIPs are exactly 9 chars from the CUSIP charset (0-9, A-Z, *, @, #),
    with the 9th char being the check digit (always 0-9). Free-text values
    that happen to be 9 chars but contain spaces / punctuation / letter-only
    suffixes (e.g. "ARMSTRONG", "MAJESCO 1") are filtered out.
    """
    if len(s) != 9:
        return False
    for c in s:
        if c not in _CUSIP_CHECK_TABLE:
            return False
    return s[8].isdigit()


def classify_instrument_type(
    raw: Optional[str],
) -> tuple[Optional[str], bool, str, list[str], list[str]]:
    """Return (normalized, is_debt_instrument, confidence, reasons, rule_ids).

    is_debt_instrument applies IS_DEBT_INSTRUMENT_RE.
    """
    if not raw or detect_sentinel(raw):
        return None, False, "rejected", ["sentinel_value_detected"], ["rule_sentinel"]
    normalized = raw.strip()
    is_debt = _is_debt_instrument(normalized)
    return normalized, is_debt, "verified_exact", [], []


def classify_amortized_cost(
    raw: Optional[str],
) -> tuple[Optional[float], str, list[str], list[str]]:
    """Parse amortized cost from raw string."""
    if not raw or detect_sentinel(raw):
        return None, "rejected", ["sentinel_value_detected"], ["rule_sentinel"]
    stripped = raw.strip().replace(",", "")
    m = _NUM_SUFFIX_RE.match(stripped)
    if m:
        val_str, suffix = m.group(1), m.group(2)
        try:
            val = float(val_str.replace(",", "")) * _SUFFIX_MULTIPLIERS.get(suffix, 1)
            return val, "verified_exact", [], []
        except ValueError:
            pass
    return None, "inferred_anchored", ["parser_partial_confidence"], ["rule_cost_parse_fail"]


def detect_sentinel(raw: Optional[str]) -> bool:
    """REDACTED / N/A / [NULL] / em-dash detection."""
    if not raw:
        return True
    return bool(_SENTINEL_RE.match(raw.strip()))


# ── Internal helpers ──────────────────────────────────────────────────────────

def _is_debt_instrument(instrument_type: Optional[str]) -> bool:
    """Return True iff the instrument_type string matches a debt-like pattern."""
    if not instrument_type:
        return False
    return bool(IS_DEBT_INSTRUMENT_RE.search(instrument_type))


def pipe_join(values: list[str]) -> Optional[str]:
    """Join a list of demotion-reason codes with pipe separator (L54 Lance advisory)."""
    filtered = [v for v in values if v]
    return "|".join(filtered) if filtered else None

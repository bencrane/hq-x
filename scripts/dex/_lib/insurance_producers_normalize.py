"""Pure-functional normalizers for Insurance Producers ingest.

Identity-spine columns the downstream RW MVs will join on:

  individual side
    - producer_first_normalized   lowercased + trimmed
    - producer_last_normalized    lowercased + trimmed
    - producer_kind_normalized    'INDIVIDUAL' | 'AGENCY'
    - npn_normalized              digits-only (NPN is numeric)

  agency side
    - agency_name_normalized      lowercased + corp-suffix-stripped
    - agency_zip5
    - agency_state_normalized

  license side
    - license_number_normalized   uppercased + trimmed
    - license_status_normalized   ACTIVE | INACTIVE | EXPIRED | SUSPENDED
                                    | REVOKED | CANCELLED | PENDING | OTHER
    - lines_of_authority_set      semicolon-joined canonical LOA enum values
    - is_life_writer / is_health_writer / is_p_and_c_writer / is_surplus_writer
                                    booleans derived from lines_of_authority_set

State-of-FILING (`producer_state_filing` partition) is distinct from state-of-
RESIDENCE (`home_state_normalized`) — a TX-resident producer holding a non-
resident FL license appears in the FL partition with `home_state_normalized=TX`.

These functions are pure (no I/O), deterministic, and unit-tested. They mirror
the structure of `_lib/ucc_normalize.py`.

Future LLM-assisted canonicalization (multi-LOA fuzzy mapping, agency-name
identity resolution against UCC debtors / SBA borrowers / GLEIF, etc.) belongs
in a downstream MV that consumes these raw + normalized columns — NOT here.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Final


_WHITESPACE_RUN: Final = re.compile(r"\s+")
_NAME_PUNCT: Final = re.compile(r"[,.&'\"]")
_NA_SUFFIX_VARIANTS: Final = re.compile(
    r"\bn\s*\.\s*a\s*\.?\B|\bn\.a\.?\b", re.IGNORECASE,
)
# Strip the FL DFS Excel CSV protection wrapper: ="123456" → 123456.
_EXCEL_PROTECT: Final = re.compile(r'^="(.*)"$')

# Common org suffixes stripped from the END of an agency-name string after
# punctuation normalization. Includes insurance-industry-specific tokens
# ("AGENCY", "GROUP") on top of the FEC/UCC list.
_ORG_SUFFIXES: Final = (
    "llc",
    "inc",
    "incorporated",
    "corp",
    "corporation",
    "ltd",
    "limited",
    "lp",
    "llp",
    "pc",
    "pa",
    "pllc",
    "co",
    "company",
    "na",
    "fsb",
    "fa",
    "agency",
    "agencies",
    "group",
    "holdings",
    "associates",
    "partners",
    "partnership",
)


_STATE_CODES: Final = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC", "PR", "VI", "GU", "AS", "MP",
})


# --------------------------------------------------------------------------- #
# Canonical Lines-of-Authority enum.
# --------------------------------------------------------------------------- #
# These constants are the canonical values written to
# `lines_of_authority_set` (semicolon-joined when a single state cell carries
# multiple LOAs — e.g., FL's "LIFE INCL VAR ANNUITY & HEALTH" emits
# "VARIABLE_LIFE;HEALTH"). Downstream MVs aggregate further via UNION ALL of
# rows for the same producer.

LOA_LIFE: Final = "LIFE"
LOA_HEALTH: Final = "HEALTH"
LOA_P_AND_C: Final = "P_AND_C"
LOA_SURPLUS_LINES: Final = "SURPLUS_LINES"
LOA_TITLE: Final = "TITLE"
LOA_BAIL: Final = "BAIL"
LOA_PERSONAL_LINES: Final = "PERSONAL_LINES"
LOA_ANNUITIES: Final = "ANNUITIES"
LOA_VARIABLE_LIFE: Final = "VARIABLE_LIFE"
LOA_CROP: Final = "CROP"
LOA_CASUALTY: Final = "CASUALTY"
LOA_AUTO: Final = "AUTO"
LOA_HOME: Final = "HOME"
LOA_PROPERTY: Final = "PROPERTY"
LOA_WORKERS_COMP: Final = "WORKERS_COMP"
LOA_ADJUSTER: Final = "ADJUSTER"
LOA_PUBLIC_ADJUSTER: Final = "PUBLIC_ADJUSTER"
LOA_HOME_WARRANTY: Final = "HOME_WARRANTY"
LOA_AUTO_WARRANTY: Final = "AUTO_WARRANTY"
LOA_VIATICAL: Final = "VIATICAL"
LOA_MOTOR_VEHICLE: Final = "MOTOR_VEHICLE"
LOA_LIMITED_LINES: Final = "LIMITED_LINES"
LOA_FIRE: Final = "FIRE"
LOA_TRAVEL: Final = "TRAVEL"
LOA_CREDIT: Final = "CREDIT"
LOA_CARGO: Final = "CARGO"
LOA_RENTAL: Final = "RENTAL"
LOA_SERVICE_WARRANTY: Final = "SERVICE_WARRANTY"
LOA_PORTABLE_ELECTRONICS: Final = "PORTABLE_ELECTRONICS"
LOA_REINSURANCE: Final = "REINSURANCE"
LOA_MGA: Final = "MGA"
LOA_AGENCY_LICENSE: Final = "AGENCY_LICENSE"
LOA_IN_TRANSIT: Final = "IN_TRANSIT"
LOA_OTHER: Final = "OTHER"


# --------------------------------------------------------------------------- #
# Canonical License-Status enum.
# --------------------------------------------------------------------------- #

STATUS_ACTIVE: Final = "ACTIVE"
STATUS_INACTIVE: Final = "INACTIVE"
STATUS_EXPIRED: Final = "EXPIRED"
STATUS_SUSPENDED: Final = "SUSPENDED"
STATUS_REVOKED: Final = "REVOKED"
STATUS_CANCELLED: Final = "CANCELLED"
STATUS_PENDING: Final = "PENDING"
STATUS_OTHER: Final = "OTHER"


# --------------------------------------------------------------------------- #
# Producer kind.
# --------------------------------------------------------------------------- #

KIND_INDIVIDUAL: Final = "INDIVIDUAL"
KIND_AGENCY: Final = "AGENCY"


# --------------------------------------------------------------------------- #
# Excel CSV protection-wrapper strip.
# --------------------------------------------------------------------------- #


def strip_excel_protect(raw: str | None) -> str | None:
    """Strip the Excel-CSV `="value"` protection wrapper used by FL DFS.

    FL bulk CSVs prefix numeric-looking text fields with `="..."` to keep Excel
    from interpreting them as numbers and dropping leading zeros. This wrapper
    has to come off before any normalization runs.

    '="636278"'   → '636278'
    'Resident'    → 'Resident'    (no wrapper, passthrough)
    None / ''     → None
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    m = _EXCEL_PROTECT.match(s)
    if m:
        s = m.group(1).strip()
    return s or None


# --------------------------------------------------------------------------- #
# Name normalizers.
# --------------------------------------------------------------------------- #


def normalize_producer_name(raw: str | None) -> str | None:
    """Normalize a single producer-name input (works for both individuals and
    org names, but applies the comma-reverse heuristic for "LAST, FIRST"
    individual names).

    Steps mirror `ucc_normalize.normalize_party_name`:
      1. Lowercase + trim + Excel-protect strip.
      2. Collapse N.A. / N. A. → "na".
      3. Comma-reverse heuristic for individual "LAST, FIRST" forms.
      4. Replace ".", ",", "&", "'", "\"" with spaces.
      5. Collapse whitespace.
      6. Strip ONE trailing org suffix.

    Examples:
      'WALKER, KEITH J'              → 'keith j walker'
      'KEITH WALKER'                 → 'keith walker'
      'PEOPLES CHOICE REALTY LLC'    → 'peoples choice realty'
      'Wells Fargo Bank, N.A.'       → 'wells fargo bank'
      None / ''                       → None
    """
    if raw is None:
        return None
    s = strip_excel_protect(raw)
    if not s:
        return None
    s = s.lower()
    s = _NA_SUFFIX_VARIANTS.sub("na", s)

    # Comma-reverse heuristic — same guards as UCC normalizer.
    if "," in s:
        head, _, tail = s.partition(",")
        head_words = head.strip().split()
        tail_clean = tail.strip().rstrip(".").lower()
        tail_first_word = tail_clean.split(" ", 1)[0] if tail_clean else ""
        if (
            len(head_words) == 1
            and tail_first_word
            and tail_first_word.replace(".", "") not in _ORG_SUFFIXES
            and tail_first_word not in {"na", "n.a"}
        ):
            s = f"{tail.strip()} {head.strip()}"

    s = _NAME_PUNCT.sub(" ", s)
    s = _WHITESPACE_RUN.sub(" ", s).strip()
    if not s:
        return None

    parts = s.split(" ")
    if len(parts) >= 2 and parts[-1] in _ORG_SUFFIXES:
        parts = parts[:-1]
        s = " ".join(parts).strip()

    return s or None


def normalize_agency_name(raw: str | None) -> str | None:
    """Normalize an agency / business-entity producer name.

    Always treats the input as an org — no comma-reverse heuristic. Strips ONE
    trailing org suffix.

    Examples:
      'PEOPLES CHOICE REALT Y SERVICES LLC'  → 'peoples choice realt y services'
      'SCANLON IMPORTS INC'                  → 'scanlon imports'
      'AAA Insurance Agency, Inc.'           → 'aaa insurance agency'  (Inc. wins)
      'AAA Insurance Agency'                 → 'aaa insurance'         (Agency wins)
      None / ''                              → None
    """
    if raw is None:
        return None
    s = strip_excel_protect(raw)
    if not s:
        return None
    s = s.lower()
    s = _NA_SUFFIX_VARIANTS.sub("na", s)
    s = _NAME_PUNCT.sub(" ", s)
    s = _WHITESPACE_RUN.sub(" ", s).strip()
    if not s:
        return None
    parts = s.split(" ")
    if len(parts) >= 2 and parts[-1] in _ORG_SUFFIXES:
        parts = parts[:-1]
        s = " ".join(parts).strip()
    return s or None


# --------------------------------------------------------------------------- #
# Address normalizers.
# --------------------------------------------------------------------------- #


def zip5(raw: str | None) -> str | None:
    """Extract a 5-digit ZIP from a postal-code field.

    '12345'              → '12345'
    '12345-6789'         → '12345'
    '123456789'          → '12345'
    '="123456789"'       → '12345'  (FL Excel-protect wrapper stripped first)
    'K1A 0B1'            → None     (Canadian — fewer than 5 numeric)
    None / ''            → None
    """
    if raw is None:
        return None
    s = strip_excel_protect(raw)
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) < 5:
        return None
    return digits[:5]


def normalize_state_code(raw: str | None) -> str | None:
    """Normalize a state field to a 2-letter US/territory code.

    'CO'       → 'CO'
    ' tx '     → 'TX'
    '="FL"'    → 'FL'   (FL Excel-protect wrapper stripped first)
    'Texas'    → None   (full name not accepted at this layer)
    'ZZ'       → None   (not a known code)
    None / ''  → None
    """
    if raw is None:
        return None
    s = strip_excel_protect(raw)
    if not s:
        return None
    s = s.strip().upper()
    if len(s) != 2:
        return None
    return s if s in _STATE_CODES else None


def normalize_city(raw: str | None) -> str | None:
    """Lowercase + collapse-whitespace city name (for join-spine consistency).

    'NAPLES'         → 'naples'
    'Fort Myers'     → 'fort myers'
    None / ''        → None
    """
    if raw is None:
        return None
    s = strip_excel_protect(raw)
    if not s:
        return None
    s = s.lower()
    s = _WHITESPACE_RUN.sub(" ", s).strip()
    return s or None


# --------------------------------------------------------------------------- #
# Date parsers.
# --------------------------------------------------------------------------- #


def parse_us_date(raw: str | None) -> date | None:
    """Parse a US-style or ISO date string, tolerating common formats:

      'M/D/YYYY 12:00:00 AM'         (FL DFS)
      'M/D/YYYY'                      (US legacy)
      'YYYY-MM-DDTHH:MM:SS.000'      (Socrata Calendar Date — TX, IL)
      'YYYY-MM-DD'                    (ISO)

    Returns None on unparseable input. Strips trailing time component first
    (we only carry DATE precision).
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    if " " in s and ":" in s.split(" ", 1)[1]:
        s = s.split(" ", 1)[0]
    if "T" in s:
        s = s.split("T", 1)[0]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------- #
# License-status classifier.
# --------------------------------------------------------------------------- #


_LICENSE_STATUS_MAP: Final = {
    "active": STATUS_ACTIVE,
    "valid": STATUS_ACTIVE,
    "current": STATUS_ACTIVE,
    "ok": STATUS_ACTIVE,
    "approved": STATUS_ACTIVE,
    "in good standing": STATUS_ACTIVE,
    "good standing": STATUS_ACTIVE,
    "inactive": STATUS_INACTIVE,
    "lapsed": STATUS_EXPIRED,
    "expired": STATUS_EXPIRED,
    "expired - eligible to renew": STATUS_EXPIRED,
    "expired-eligible-to-renew": STATUS_EXPIRED,
    "suspended": STATUS_SUSPENDED,
    "revoked": STATUS_REVOKED,
    "cancelled": STATUS_CANCELLED,
    "canceled": STATUS_CANCELLED,
    "surrendered": STATUS_CANCELLED,
    "withdrawn": STATUS_CANCELLED,
    "voluntarily surrendered": STATUS_CANCELLED,
    "pending": STATUS_PENDING,
    "applied": STATUS_PENDING,
}


def classify_license_status(raw: str | None) -> str | None:
    """Map a state-specific license-status string to the canonical enum.

    'VALID'                       → 'ACTIVE'   (FL)
    'Active'                      → 'ACTIVE'
    'EXPIRED'                     → 'EXPIRED'
    'Suspended'                   → 'SUSPENDED'
    None / ''                     → None
    Any unrecognized value        → 'OTHER'
    """
    if raw is None:
        return None
    s = raw.strip().lower()
    if not s:
        return None
    return _LICENSE_STATUS_MAP.get(s, STATUS_OTHER)


# --------------------------------------------------------------------------- #
# Lines-of-Authority classifier.
# --------------------------------------------------------------------------- #
# Each entry: (substring_to_match, canonical_LOA). Order matters — longer/more-
# specific strings come first so they win over their shorter prefixes (e.g.,
# "personal lines" matches before bare "lines").
#
# A single state cell may carry multiple LOAs — "Life and Health" emits
# "LIFE;HEALTH". The matcher walks the input, accumulating canonical values
# from non-overlapping fragment hits.

_LOA_FRAGMENTS: Final = (
    # --- Multi-word / specialty (longest first, to win overlaps) ----------
    ("public adjuster", LOA_PUBLIC_ADJUSTER),
    ("public-adjuster", LOA_PUBLIC_ADJUSTER),
    ("reinsurance intermediary", LOA_REINSURANCE),
    ("reinsurance", LOA_REINSURANCE),
    ("managing general agent", LOA_MGA),
    ("managing general", LOA_MGA),
    ("portable electronics", LOA_PORTABLE_ELECTRONICS),
    ("service warranty", LOA_SERVICE_WARRANTY),
    ("home warranty", LOA_HOME_WARRANTY),
    ("automobile warranty", LOA_AUTO_WARRANTY),
    ("auto warranty", LOA_AUTO_WARRANTY),
    ("in-transit & storage", LOA_IN_TRANSIT),
    ("in-transit", LOA_IN_TRANSIT),
    ("agency license", LOA_AGENCY_LICENSE),
    ("motor vehicle physical damage", LOA_MOTOR_VEHICLE),
    ("motor vehicle", LOA_MOTOR_VEHICLE),
    ("variable life", LOA_VARIABLE_LIFE),
    ("variable annuity", LOA_VARIABLE_LIFE),
    ("variable contracts", LOA_VARIABLE_LIFE),
    ("var annuity", LOA_VARIABLE_LIFE),
    ("personal lines", LOA_PERSONAL_LINES),
    ("workers comp", LOA_WORKERS_COMP),
    ("workers' comp", LOA_WORKERS_COMP),
    ("workers compensation", LOA_WORKERS_COMP),
    ("workers' compensation", LOA_WORKERS_COMP),
    ("surplus line", LOA_SURPLUS_LINES),
    ("surplus lines", LOA_SURPLUS_LINES),
    ("excess and surplus", LOA_SURPLUS_LINES),
    ("annuities", LOA_ANNUITIES),
    ("annuity", LOA_ANNUITIES),
    ("limited lines", LOA_LIMITED_LINES),
    ("limited line", LOA_LIMITED_LINES),
    ("title insurance", LOA_TITLE),
    ("title agent", LOA_TITLE),
    ("title agency", LOA_TITLE),
    ("title", LOA_TITLE),
    # FL TYCL Desc samples — "GENERAL LINES (PROP & CAS)", "LIFE INCL VAR ANNUITY & HEALTH"
    ("general lines", LOA_P_AND_C),
    ("property and casualty", LOA_P_AND_C),
    ("property/casualty", LOA_P_AND_C),
    ("prop & cas", LOA_P_AND_C),
    ("p&c", LOA_P_AND_C),
    ("p & c", LOA_P_AND_C),
    # 'life and health' → fall through to bare 'life' + 'health' matchers
    # so the multi-LOA emerges as 'LIFE;HEALTH' rather than collapsing to LIFE.
    ("life insurance", LOA_LIFE),
    ("life agent", LOA_LIFE),
    ("life producer", LOA_LIFE),
    ("life only", LOA_LIFE),
    ("accident and health", LOA_HEALTH),
    ("accident & health", LOA_HEALTH),
    ("a & h", LOA_HEALTH),
    ("a&h", LOA_HEALTH),
    ("health agent", LOA_HEALTH),
    ("health producer", LOA_HEALTH),
    ("health insurance", LOA_HEALTH),
    ("independent adjuster", LOA_ADJUSTER),
    ("staff adjuster", LOA_ADJUSTER),
    ("adjuster", LOA_ADJUSTER),
    ("bail bond", LOA_BAIL),
    ("bail", LOA_BAIL),
    ("crop", LOA_CROP),
    ("travel", LOA_TRAVEL),
    ("credit life", LOA_CREDIT),
    ("credit insurance", LOA_CREDIT),
    ("credit", LOA_CREDIT),
    ("cargo", LOA_CARGO),
    ("rental", LOA_RENTAL),
    ("viatical", LOA_VIATICAL),
    # --- Single-word fallbacks (short — must come AFTER everything above) -
    ("automobile", LOA_AUTO),
    ("auto", LOA_AUTO),
    ("homeowner", LOA_HOME),
    ("home", LOA_HOME),
    ("property", LOA_PROPERTY),
    ("fire", LOA_FIRE),
    ("casualty", LOA_CASUALTY),
    ("life", LOA_LIFE),
    ("health", LOA_HEALTH),
)


def normalize_lines_of_authority(raw: str | None) -> str | None:
    """Map a state-specific LOA string to a semicolon-joined canonical-enum set.

    Detects and emits ALL canonical LOAs that appear in the input string, in
    insertion-order (deterministic across runs). Single-cell multi-LOA strings
    like FL's "LIFE INCL VAR ANNUITY & HEALTH" emit "VARIABLE_LIFE;HEALTH".
    Single-LOA strings emit just one value, e.g. "Casualty" → "CASUALTY".

    'Life'                          → 'LIFE'
    'Casualty'                      → 'CASUALTY'
    'Adjuster - P&C'                → 'ADJUSTER;P_AND_C'
    'GENERAL LINES (PROP & CAS)'    → 'P_AND_C'
    'LIFE INCL VAR ANNUITY & HEALTH'→ 'VARIABLE_LIFE;HEALTH'
    'Life and Health'               → 'LIFE;HEALTH'
    None / ''                       → None
    Anything unrecognized           → 'OTHER'
    """
    if raw is None:
        return None
    s = raw.strip().lower()
    if not s:
        return None
    # Each accepted match: (input-position, canonical-LOA). Sorting by
    # input-position gives deterministic, reading-order output.
    accepted: list[tuple[int, str]] = []
    matched_spans: list[tuple[int, int]] = []
    for fragment, canonical in _LOA_FRAGMENTS:
        idx = s.find(fragment)
        while idx != -1:
            end = idx + len(fragment)
            if not any(idx < e and end > b for b, e in matched_spans):
                if canonical not in {c for _, c in accepted}:
                    accepted.append((idx, canonical))
                matched_spans.append((idx, end))
            idx = s.find(fragment, end)
    if not accepted:
        return LOA_OTHER
    accepted.sort(key=lambda x: x[0])
    return ";".join(c for _, c in accepted)


def derive_loa_flags(loa_set: str | None) -> dict[str, bool]:
    """Derive boolean writer-flags from a normalized LOA set string.

    is_life_writer:    LIFE | VARIABLE_LIFE | ANNUITIES
    is_health_writer:  HEALTH
    is_p_and_c_writer: P_AND_C | PERSONAL_LINES | AUTO | HOME | PROPERTY
                       | CASUALTY | FIRE
    is_surplus_writer: SURPLUS_LINES
    """
    if not loa_set:
        return {
            "is_life_writer": False,
            "is_health_writer": False,
            "is_p_and_c_writer": False,
            "is_surplus_writer": False,
        }
    parts = set(loa_set.split(";"))
    return {
        "is_life_writer": bool(
            parts & {LOA_LIFE, LOA_VARIABLE_LIFE, LOA_ANNUITIES}
        ),
        "is_health_writer": LOA_HEALTH in parts,
        "is_p_and_c_writer": bool(
            parts & {
                LOA_P_AND_C, LOA_PERSONAL_LINES, LOA_AUTO,
                LOA_HOME, LOA_PROPERTY, LOA_CASUALTY, LOA_FIRE,
            }
        ),
        "is_surplus_writer": LOA_SURPLUS_LINES in parts,
    }


# --------------------------------------------------------------------------- #
# NPN normalizer.
# --------------------------------------------------------------------------- #


def normalize_npn(raw: str | None) -> str | None:
    """Strip non-digits from an NPN field; return None if no digits remain.

    NPN (National Producer Number) is numeric. State CSVs sometimes wrap it in
    Excel-protect quotes ('="636278"') or pad with whitespace.

    '7352575'      → '7352575'
    '="636278"'    → '636278'
    'NA'           → None
    None / ''      → None
    """
    if raw is None:
        return None
    s = strip_excel_protect(raw)
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    return digits or None


# --------------------------------------------------------------------------- #
# License-number normalizer.
# --------------------------------------------------------------------------- #


def normalize_license_number(raw: str | None) -> str | None:
    """Normalize a state-issued license number — uppercase + trim only.

    License numbers are NOT comparable across states (each state has its own
    numbering scheme), so cross-source identity joins should never key on this
    field alone — pair with `producer_state_filing` partition value.

    'A276085'      → 'A276085'
    ' a276085 '    → 'A276085'
    None / ''      → None
    """
    if raw is None:
        return None
    s = strip_excel_protect(raw)
    if not s:
        return None
    return s.upper() or None

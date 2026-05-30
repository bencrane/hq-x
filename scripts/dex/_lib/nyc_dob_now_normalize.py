"""NYC DOB Now identity-layer normalizers.

Pure functions for ingest-time normalization of the NYC DOB Now R2 corpus
(scripts/run_nyc_dob_now_r2_ingest.py). The shared property primitives —
`normalize_owner_name`, `compute_bbl`, `normalize_borough_code` — are
re-exported from `nyc_property_normalize` so the FEC / NYC Property /
NYC DOB Now spines all key off the same normalized columns.

DOB-Now-specific additions:

  * normalize_business_name(text)  — applicant / contractor / filing-rep
                                     organizational name. Currently aliased
                                     to normalize_owner_name (same suffix-
                                     stripping discipline); kept distinct so
                                     it can diverge if NYC-DOB-licensed-firm
                                     suffixes differ from NYC-property-owner
                                     suffixes.
  * classify_license_kind(text)    — DOB license-type → canonical token
                                     ('GC', 'ELEC', 'PLUMB', 'HVAC', 'PE',
                                     'RA', etc.). Falls through to None on
                                     unrecognized input.
  * classify_work_type(text)       — DOB work-type → canonical token
                                     ('NEW_BUILDING', 'ALTERATION',
                                     'DEMOLITION', etc.). Falls through to
                                     None on unrecognized input.
"""

from __future__ import annotations

import re

from scripts._lib.nyc_property_normalize import (
    compute_bbl,
    normalize_borough_code,
    normalize_owner_name,
)

__all__ = [
    "normalize_business_name",
    "normalize_owner_name",
    "compute_bbl",
    "normalize_borough_code",
    "classify_license_kind",
    "classify_work_type",
]


# `normalize_business_name` is a logical alias of `normalize_owner_name`. NYC-
# DOB-licensed-firm names follow the same suffix conventions (LLC / Inc /
# Corp / Trust / Holdings / Realty) as NYC property owners. Keeping it as a
# separate exported name lets callers express intent at the use-site and
# leaves room for divergence if construction-firm suffix tokens accumulate.
normalize_business_name = normalize_owner_name


# --------------------------------------------------------------------------- #
# License-kind classifier — DOB license-class → canonical token.
#
# Source field: `permittee_s_license_type` (approved permits) /
# `applicant_professional_title` (job applications). The SODA feed mixes
# 2-letter codes (GC, EC, MP, …) with descriptive text ("Electrical
# Contractor", "Master Plumber"). Two-stage match:
#   1. Whole-string equality against the 2-letter code map.
#   2. Substring match (first hit wins, ordered specific → general).
# Unrecognized inputs return None.
# --------------------------------------------------------------------------- #


# Whole-string equality map — for the 2-letter codes used in the
# permittee_s_license_type field. Substring matching on these short
# tokens would over-fire ("GC" inside "GCOM" or arbitrary text).
_LICENSE_SHORT_CODE_MAP: dict[str, str] = {
    "GC":     "GC",
    "EC":     "ELEC",
    "MP":     "PLUMB",
    "MR":     "HVAC",       # Master Refrigeration
    "OB":     "HVAC",       # Oil Burner
    "MFS":    "FS",         # Master Fire Suppression
    "PE":     "PE",
    "RA":     "RA",
    "SSM":    "SSM",
    "SSC":    "SSC",
    "CS":     "CS",
    "SI":     "SI",
    "HMO":    "HMO",
    "FR":     "FILING_REP",
    "FRP":    "FILING_REP",
    "MES":    "SIGN",       # Master Electric Sign hanger (rare)
    "HPB":    "HVAC",       # High Pressure Boiler
    "RGR":    "RIG",        # Rigger
}


# Order matters — more-specific patterns must come before more-general ones
# (e.g. "ELECTRICAL CONTRACTOR" must beat the generic "CONTRACTOR" → GC
# fallback). Patterns are uppercase substrings; we test each against the
# uppercased + whitespace-collapsed input.
_LICENSE_KIND_RULES: tuple[tuple[str, str], ...] = (
    # Trade contractors.
    ("ELECTRICAL",                      "ELEC"),
    ("ELECTRICIAN",                     "ELEC"),
    ("MASTER PLUMBER",                  "PLUMB"),
    ("PLUMBING",                        "PLUMB"),
    ("PLUMBER",                         "PLUMB"),
    ("HIGH PRESSURE BOILER",            "HVAC"),
    ("OIL BURNER",                      "HVAC"),
    ("REFRIGERATION",                   "HVAC"),
    ("HVAC",                            "HVAC"),
    # Design professionals.
    ("PROFESSIONAL ENGINEER",           "PE"),
    ("REGISTERED ARCHITECT",            "RA"),
    ("ARCHITECT",                       "RA"),
    ("ENGINEER",                        "PE"),
    # Safety / inspection.
    ("SITE SAFETY MANAGER",             "SSM"),
    ("SITE SAFETY COORDINATOR",         "SSC"),
    ("CONSTRUCTION SUPERINTENDENT",     "CS"),
    ("SPECIAL INSPECTOR",               "SI"),
    # Hoists / sign / rigging / fire.
    ("HOISTING MACHINE OPERATOR",       "HMO"),
    ("RIGGER",                          "RIG"),
    ("MASTER FIRE SUPPRESSION",         "FS"),
    ("FIRE SUPPRESSION",                "FS"),
    ("MASTER SIGN HANGER",              "SIGN"),
    ("SIGN HANGER",                     "SIGN"),
    # Filing / general construction.
    ("FILING REPRESENTATIVE",           "FILING_REP"),
    ("GENERAL CONTRACTOR",              "GC"),
    ("CONTRACTOR",                      "GC"),
)


def classify_license_kind(value: str | None) -> str | None:
    """Map a DOB license-type string to a canonical token.

    Returns None on empty / unrecognized input. Two-stage match:
      1. Whole-string equality against the 2-letter NYC-DOB code map
         (GC, EC, MP, MR, OB, PE, RA, SSM, SSC, CS, SI, HMO, FR, …).
      2. Substring match against the longer descriptive forms; first hit
         wins (rules ordered most-specific → most-general).

    Examples:
      "GC"                      → "GC"      (short-code)
      "EC"                      → "ELEC"    (short-code)
      "ELECTRICAL CONTRACTOR"   → "ELEC"    (substring)
      "Master Plumber"          → "PLUMB"   (substring)
    """
    if value is None:
        return None
    s = _RE_COLLAPSE_WS.sub(" ", value.strip().upper())
    if not s:
        return None
    if s in _LICENSE_SHORT_CODE_MAP:
        return _LICENSE_SHORT_CODE_MAP[s]
    for needle, canonical in _LICENSE_KIND_RULES:
        if needle in s:
            return canonical
    return None


# --------------------------------------------------------------------------- #
# Work-type classifier — DOB work-type → canonical token.
#
# Source field: `work_type` (approved permits) / various job-type columns
# (applications). NYC DOB ships ~30 work-type codes; we collapse to a smaller
# canonical set so downstream MVs can group cleanly.
# --------------------------------------------------------------------------- #


_WORK_TYPE_RULES: tuple[tuple[str, str], ...] = (
    # Specific work types — must match before the broad fallthroughs.
    ("NEW BUILDING",                "NEW_BUILDING"),
    ("DEMOLITION",                  "DEMOLITION"),
    ("EARTHWORK",                   "EARTHWORK"),
    ("FOUNDATION",                  "FOUNDATION"),
    ("BOILER",                      "MECHANICAL"),
    ("MECHANICAL",                  "MECHANICAL"),
    ("SPRINKLER",                   "SPRINKLER"),
    ("STANDPIPE",                   "STANDPIPE"),
    ("SCAFFOLD",                    "SCAFFOLD"),
    ("SIDEWALK SHED",               "SIDEWALK_SHED"),
    ("FENCE",                       "FENCE"),
    ("CURB CUT",                    "CURB_CUT"),
    ("ANTENNA",                     "ANTENNA"),
    ("SIGN",                        "SIGN"),
    ("STRUCTURAL",                  "STRUCTURAL"),
    ("PLUMBING",                    "PLUMBING"),
    ("ELECTRICAL",                  "ELECTRICAL"),
    ("PLACE OF ASSEMBLY",           "PLACE_OF_ASSEMBLY"),
    ("SUPPORT OF EXCAVATION",       "SUPPORT_OF_EXCAVATION"),
    ("GENERAL CONSTRUCTION",        "GENERAL_CONSTRUCTION"),
    ("PROTECTION",                  "PROTECTION"),
    ("ALTERATION",                  "ALTERATION"),
)

# Compiled prefix-match for the 2-letter codes — substring match on a free-
# text "NB" or "DM" would over-match anything containing those letters. Use
# whole-string equality for the short codes after uppercasing.
_SHORT_CODE_MAP: dict[str, str] = {
    "NB":  "NEW_BUILDING",
    "DM":  "DEMOLITION",
    "A1":  "ALTERATION",
    "A2":  "ALTERATION",
    "A3":  "ALTERATION",
    "GC":  "GENERAL_CONSTRUCTION",
    "EQ":  "MECHANICAL",
    "FA":  "PROTECTION",
    "SP":  "SPRINKLER",
    "ST":  "STANDPIPE",
}

_RE_COLLAPSE_WS = re.compile(r"\s+")


def classify_work_type(value: str | None) -> str | None:
    """Map a DOB work-type string to a canonical token.

    Returns None on empty / unrecognized input. Two-stage match:
      1. Whole-string equality against the 2-letter NYC-DOB codes
         (NB, DM, A1, A2, A3, GC, EQ, FA, SP, ST).
      2. Substring match against the longer descriptive forms; first hit
         wins (rules ordered most-specific → most-general).
    """
    if value is None:
        return None
    s = _RE_COLLAPSE_WS.sub(" ", value.strip().upper())
    if not s:
        return None
    if s in _SHORT_CODE_MAP:
        return _SHORT_CODE_MAP[s]
    for needle, canonical in _WORK_TYPE_RULES:
        if needle in s:
            return canonical
    return None

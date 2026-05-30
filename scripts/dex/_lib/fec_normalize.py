"""Pure-functional normalizers for FEC Individual Contributions ingest.

These functions produce the join keys the downstream identity-resolution MVs
will use to bridge FEC donors against `source_sba_historical.borrname_normalized`,
NPPES providers, and `entities.raw_entity_records`. Keep them pure (no I/O),
deterministic, and unit-tested — the ingest re-runs cheaply if the rules change.

Per the directive: normalization happens at INGEST time so the Parquet carries
both raw and normalized columns. RW joins on the normalized columns; the raw
columns stay as ground-truth.

Future LLM-assisted canonicalization (employer disambiguation, occupation
clustering) belongs in a downstream MV that consumes the raw columns — NOT here.
"""

from __future__ import annotations

import re
from typing import Final


_WHITESPACE_RUN: Final = re.compile(r"\s+")
_NAME_PUNCT: Final = re.compile(r"[,.]")
_EMPLOYER_PUNCT: Final = re.compile(r"[.,&]")

# Common org suffixes stripped from the END of an employer string after
# punctuation normalization. Word-boundary, case-insensitive. Order matters
# for repeat application (LLC inside an "ACME LLC HOLDINGS LLC" string would
# be ambiguous; we only strip terminal suffixes).
_EMPLOYER_SUFFIXES: Final = (
    "llc",
    "inc",
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
)


def normalize_name(raw: str | None) -> str | None:
    """Normalize an FEC donor NAME field for cross-source identity joining.

    FEC convention: most NAME entries are "LAST, FIRST [MIDDLE]" (organization
    contributors and PACs sometimes break this, but the corpus is
    overwhelmingly individuals). We:

      1. Lowercase + trim.
      2. Reverse on the first comma → "first middle last".
      3. Strip "." and remaining "," (suffixes like "JR.", "III").
      4. Collapse whitespace runs.

    "SMITH, JOHN A."          → "john a smith"
    "Smith, John A."          → "john a smith"
    "  SMITH, JOHN A. JR. "   → "john a jr smith"
    "ACME CORP"               → "acme corp"
    "Acme, Inc."              → "inc acme"  (org form — caller knows entity_tp)
    None / empty              → None
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    s = s.lower()

    if "," in s:
        last, _, rest = s.partition(",")
        s = f"{rest.strip()} {last.strip()}"

    s = _NAME_PUNCT.sub(" ", s)
    s = _WHITESPACE_RUN.sub(" ", s).strip()
    return s or None


def normalize_employer(raw: str | None) -> str | None:
    """Normalize an FEC EMPLOYER field for cross-source matching.

    FEC sentinels like "INFORMATION REQUESTED", "NOT EMPLOYED", "RETIRED",
    "SELF EMPLOYED", "N/A" pass through with only case-folding + whitespace
    collapse — we do NOT bucket them into a canonical "unknown" bin here. That
    decision belongs in a downstream MV (where the canonicalization rules can
    be tuned without re-running the ingest).

    Steps:
      1. Lowercase + trim.
      2. Replace ".", ",", "&" with spaces.
      3. Collapse whitespace.
      4. Strip ONE trailing org suffix if present (LLC, INC, CORP, …).
      5. Re-collapse whitespace, trim.

    "ACME, INC."               → "acme"
    "Acme Holdings, LLC"       → "acme holdings"
    "  Acme   Holdings, LLC  " → "acme holdings"
    "INFORMATION REQUESTED"    → "information requested"
    "N/A"                      → "n/a"
    None / empty               → None
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    s = s.lower()
    s = _EMPLOYER_PUNCT.sub(" ", s)
    s = _WHITESPACE_RUN.sub(" ", s).strip()
    if not s:
        return None

    parts = s.split(" ")
    if len(parts) >= 2 and parts[-1] in _EMPLOYER_SUFFIXES:
        parts = parts[:-1]
        s = " ".join(parts).strip()

    return s or None


def zip5(raw: str | None) -> str | None:
    """Extract the 5-digit ZIP from FEC ZIP_CODE.

    FEC ZIP_CODE is 5 or 9 digits, sometimes with embedded punctuation
    ("12345-6789", "123456789", "12345"). We slice the first 5 numeric chars;
    if fewer than 5 numeric chars are available, return None (the row's ZIP
    is malformed).

    "12345"        → "12345"
    "12345-6789"   → "12345"
    "123456789"    → "12345"
    "1234"         → None    (too short)
    "abcde"        → None    (no digits)
    None / empty   → None
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) < 5:
        return None
    return digits[:5]


def normalize_occupation(raw: str | None) -> str | None:
    """Normalize an FEC OCCUPATION field. Lightweight by design.

    The FEC corpus has thousands of free-text occupation strings ("ATTORNEY",
    "Self-Employed Attorney", "ATTORNEY/PARTNER"). Deeper canonicalization
    (clustering "ATTORNEY" / "LAWYER" / "ESQ" into a single bucket) is a
    downstream LLM-assisted MV concern. This function only does:

      1. Lowercase + trim.
      2. Collapse whitespace.

    "ATTORNEY"             → "attorney"
    "Self-Employed"        → "self-employed"
    "  ATTORNEY/PARTNER  " → "attorney/partner"
    None / empty           → None
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    s = s.lower()
    s = _WHITESPACE_RUN.sub(" ", s).strip()
    return s or None

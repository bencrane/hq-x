"""Pure-Python normalizers for IRS Form 990 e-File ingest.

Used by scripts/run_irs_990_r2_ingest.py. Tested directly in
tests/scripts/test_irs_990_normalize.py so the join-key invariants
(EIN length=9 > 99%, person_first/last_normalized jointly NULL < 0.5%) are
guaranteed before any DuckDB / Parquet / R2 round-trip.

Three functions:
  - normalize_ein(raw): strip non-digit, left-pad to 9. Empty/None → None.
  - normalize_org_name(raw): lowercase, collapse whitespace, strip US legal
    + nonprofit-form suffixes. Empty/None → None.
  - normalize_person_name(raw): split a single PersonNm into
    (first, middle, last, suffix) tuple. Falls back gracefully on edge cases.

`normalize_org_name` is a thin re-export of the IRS BMF normalizer — the
suffix rules are identical between IRS BMF (organizations table) and IRS 990
filer org names (filings table). Keeping a single source-of-truth means the
join keys agree at MV-refresh time.
"""

from __future__ import annotations

import re

from . import irs_bmf_normalize


# Re-export the BMF normalizers verbatim — Form 990 filer names normalize the
# same way IRS BMF organization names do; sharing the implementation
# guarantees join-key parity between the two corpora.
normalize_ein = irs_bmf_normalize.normalize_ein
normalize_org_name = irs_bmf_normalize.normalize_org_name


_WS_COLLAPSE_RE = re.compile(r"\s+")
_PUNCT_STRIP_RE = re.compile(r"[.,;:]+")


# Common suffixes that should be parked in the suffix slot, not in last name.
# Order matters: longest first.
_NAME_SUFFIXES: tuple[str, ...] = (
    "iii",
    "ii",
    "iv",
    "jr",
    "sr",
    "phd",
    "md",
    "esq",
    "cpa",
    "dds",
    "dvm",
    "rn",
    "do",
    "jd",
)

# Common honorifics / titles to strip from the FRONT of the name. Order
# matters: longest first.
_NAME_PREFIXES: tuple[str, ...] = (
    "reverend",
    "father",
    "rabbi",
    "sister",
    "brother",
    "deacon",
    "pastor",
    "doctor",
    "mister",
    "professor",
    "mrs",
    "ms",
    "mr",
    "dr",
    "rev",
    "fr",
    "prof",
    "hon",
    "sr",
    "br",
)


def normalize_person_name(
    raw: str | None,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Parse + normalize a single Form 990 ``PersonNm`` string.

    Form 990 publishes board-member / officer names as a single ``<PersonNm>``
    free-text field. Returns ``(first, middle, last, suffix)`` lowercase, all
    None when the input is empty or pure punctuation. The normalized first +
    last are the join keys against FEC contributors (`fec_normalize.py`).

    Heuristics:
      1. Lowercase + strip + collapse whitespace.
      2. If the string contains a single ``,``, treat as ``"LAST, FIRST [..]"``
         and reverse to ``"FIRST [..] LAST"``.
      3. Strip a trailing suffix from {jr, sr, ii, iii, iv, phd, md, esq, …}
         into the suffix slot.
      4. Strip a leading honorific from {mr, mrs, ms, dr, rev, fr, …}.
      5. Split remaining on whitespace:
            ≥2 tokens → first=tokens[0], last=tokens[-1], middle=" ".join(rest)
            1 token   → first=token, last=None.
            0 tokens  → all None.
      6. Strip period/comma punctuation inside each component.

    Examples
    --------
    "JOHN A. SMITH"            → ("john", "a", "smith", None)
    "Smith, John A."           → ("john", "a", "smith", None)
    "John Smith Jr."           → ("john", None, "smith", "jr")
    "DR. JANE QUINCY ADAMS"    → ("jane", "quincy", "adams", None)
    "MADONNA"                  → ("madonna", None, None, None)
    "  "                       → (None, None, None, None)
    None                       → (None, None, None, None)
    """
    if raw is None:
        return (None, None, None, None)
    s = str(raw).strip()
    if not s:
        return (None, None, None, None)

    s = _WS_COLLAPSE_RE.sub(" ", s).lower().strip()
    if not s:
        return (None, None, None, None)

    # "Last, First Middle" → reverse on the comma. Only handle ONE comma —
    # anything more complex stays as-is. Must run BEFORE punctuation strip
    # so the comma is still present.
    if "," in s:
        last_part, _, rest = s.partition(",")
        s = f"{rest.strip()} {last_part.strip()}".strip()
        s = _WS_COLLAPSE_RE.sub(" ", s)

    s = _PUNCT_STRIP_RE.sub("", s)
    s = _WS_COLLAPSE_RE.sub(" ", s).strip()
    if not s:
        return (None, None, None, None)

    tokens = [t for t in s.split(" ") if t]
    if not tokens:
        return (None, None, None, None)

    # Strip leading honorific.
    if len(tokens) >= 2:
        head = tokens[0]
        if head in _NAME_PREFIXES:
            tokens = tokens[1:]

    # Strip trailing suffix.
    suffix: str | None = None
    if len(tokens) >= 2:
        tail = tokens[-1]
        if tail in _NAME_SUFFIXES:
            suffix = tail
            tokens = tokens[:-1]

    if not tokens:
        return (None, None, None, suffix)

    if len(tokens) == 1:
        return (tokens[0] or None, None, None, suffix)

    first = tokens[0]
    last = tokens[-1]
    middle = " ".join(tokens[1:-1]) if len(tokens) > 2 else None
    return (first or None, middle or None, last or None, suffix)


def zip5(raw: str | None) -> str | None:
    """Extract a 5-digit ZIP from an IRS-formatted ZIPCd field.

    IRS XML publishes ZIPCd as either 5-digit ("80237"), 9-digit no hyphen
    ("507042100"), or 9-digit with hyphen ("80237-1234"). Strip non-digits,
    take the first 5; if fewer than 5 numeric chars, return None.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) < 5:
        return None
    return digits[:5]

"""SEC BDC Schedule-of-Investments portfolio-company name cleaner.

The `portfolio_company_name` column in `sec_bdc/soi_lance` is raw "company" cell
text scraped from each BDC filing's SOI holdings table. It is contaminated:

  - footnote markers — "(5)", "(6)(21)", "(m)(v)", unicode superscripts,
    stray symbols "^ + ~ * # < >", junk "(<)" / "(####)"
  - loan-tranche descriptors appended to the name — "- Revolving Credit Line",
    "Closing Date Term Loan", "(Delayed Draw)", "Fourth Amendment Delayed Draw
    Term Loan"
  - unit / share / equity-interest / percentage parentheticals —
    "( 2,992 preferred units)", "( 50.0 % of the equity interests)"
  - DBA / former-name parentheticals — "(dba Humanetics)", "(f/k/a Centrify)"
  - multi-borrower cells — "X Inc. and Y LLC"
  - embedded newlines, non-breaking + zero-width whitespace

This module is the deterministic pre-cleaner that turns that raw cell into a
clean, human-readable legal name (case + corp-form suffix PRESERVED), with the
DBA/former name split into its own field and an entity-type classification.

WHERE THIS SITS
---------------
First stage only. It does NOT produce a join key — it produces a clean legal
name. The downstream bridge join key is produced by
`scripts/_lib/entity_name_normalize.normalize_entity_name` applied to the
`cleaned_name` output here. The two compose:

    normalize_entity_name(normalize(raw)["entities"][0]["cleaned_name"])

Do not lowercase / strip suffixes here — that is `entity_name_normalize`'s job.

Per L31: this module exposes `__version__`; bump it on any output-drift change
so consumer `match_method_versions` rows bump with it.
"""

from __future__ import annotations

import re
from typing import Final

__version__: Final = "1.0.0"


# --- whitespace / zero-width normalisation ---------------------------------
# U+200B/200C/200D zero-width chars + U+FEFF BOM.
_ZERO_WIDTH_RE: Final = re.compile(
    "[" + "".join(map(chr, (0x200B, 0x200C, 0x200D, 0xFEFF))) + "]"
)
_WS_RE: Final = re.compile(r"\s+")  # \s matches \xa0 et al. on Python-3 str

# --- unicode superscript footnotes (U+00B2/B3/B9 + the U+2070 block) -------
_SUPERSCRIPT_RE: Final = re.compile(
    "[" + chr(0xB2) + chr(0xB3) + chr(0xB9) + chr(0x2070) + "-" + chr(0x207F) + "]+"
)

# --- DBA / former-name parenthetical ---------------------------------------
_DBA_RE: Final = re.compile(
    r"\(\s*(?:d\.?/?b\.?/?a\.?|f\.?/?k\.?/?a\.?|n\.?/?k\.?/?a\.?|a\.?/?k\.?/?a\.?"
    r"|formerly(?:\s+known\s+as)?|now\s+known\s+as|doing\s+business\s+as)"
    r"\b[\s:]*(?P<brand>[^()]*?)\s*\)",
    re.IGNORECASE,
)

# --- footnote-marker parens ------------------------------------------------
# A footnote atom: a digit run, 1-3 lowercase letters, a roman numeral, or junk
# symbols. Atoms inside one paren must be COMMA-separated — this stops a long
# word like "(Delaware)" / "(Corporate)" being chopped into [a-z]{1,3} pieces.
_FN_ATOM: Final = (
    r"(?:[0-9]+|[a-z]{1,3}|[ivxlcdm]{1,7}|[IVXLCDM]{1,7}|[#<>*+~^]+)"
)
_FOOTNOTE_PAREN_RE: Final = re.compile(
    r"\(\s*" + _FN_ATOM + r"(?:\s*,\s*" + _FN_ATOM + r")*\s*\)"
)

# --- unit / share / equity-interest / percentage parens --------------------
_UNIT_PAREN_RE: Final = re.compile(
    r"\(\s*[\d,]+(?:\.\d+)?\s*%?\s*[\w%.\- ]*?\b"
    r"(?:units?|shares?|warrants?|interests?|commitments?|equity|membership"
    r"|preferred|common|ownership|profit)\b[\w%.\- ]*\)",
    re.IGNORECASE,
)
# any parenthetical that opens with a "<number> %" annotation
_PCT_PAREN_RE: Final = re.compile(r"\(\s*[\d,]+(?:\.\d+)?\s*%[^()]*\)")

# --- loan-tranche / instrument descriptors ---------------------------------
_TRANCHE_KW: Final = (
    r"(?:revolv\w*|term\s+loan\w*|delayed\s+draw\w*|unfunded\w*|credit\s+line"
    r"|first\s+lien|second\s+lien|1st\s+lien|2nd\s+lien|first\s+out|second\s+out"
    r"|last\s+out|letter\s+of\s+credit|incremental|closing\s+date"
    r"|initial\s+term\s+loan|dip\s+(?:loan|facility))"
)
# tranche vocabulary — words a trailing loan-tranche phrase is built from
_TR_VOCAB: Final = (
    r"loan|loans|revolver|revolving|draw|lien|facility|facilities|unfunded"
    r"|funded|tranche|incremental|term|credit|line|delayed|initial|additional"
    r"|new|general|purpose|closing|date|amendment|amend|first|second|third"
    r"|fourth|fifth|sixth|seventh|eighth|ninth|tenth|out|secured|senior"
    r"|priority|subordinated|dollar|euro|sterling|[a-f]|\d+(?:st|nd|rd|th)?"
)
_TR_STRONG_RE: Final = re.compile(
    r"\b(?:loan|loans|revolver|revolving|draw|lien|facilit(?:y|ies)|unfunded"
    r"|tranche|incremental)\b",
    re.IGNORECASE,
)
_TRAIL_VOCAB_RE: Final = re.compile(
    r"(?:\s+(?:" + _TR_VOCAB + r")\b[.,]*)+\s*$", re.IGNORECASE
)
# dash that introduces a tranche — must have whitespace BEFORE it so an
# in-name hyphen ("G-A-I Consultants", "SV-Aero") is never a split point.
_TRANCHE_DASH_RE: Final = re.compile(
    r"\s+[-–—]\s*[^()]*?\b" + _TRANCHE_KW + r"\b.*$", re.IGNORECASE
)
_TRANCHE_DASH_VOCAB_RE: Final = re.compile(
    r"\s+[-–—]\s*(?:(?:" + _TR_VOCAB + r")\b[.,]*\s*)+$", re.IGNORECASE
)
_TRANCHE_PAREN_RE: Final = re.compile(
    r"\s*\(\s*(?:" + _TRANCHE_KW + r")[^()]*\)", re.IGNORECASE
)

# --- trailing junk (stray symbols + leftover footnote parens + bare paren) --
_TRAIL_JUNK_RE: Final = re.compile(
    r"(?:\s|[*&#+,^~<>(–—-]|\(\s*" + _FN_ATOM + r"\s*\))+$"
)

# --- legal-entity suffix (multi-borrower split anchor) ---------------------
_SUFFIX_ALT: Final = (
    r"(?:incorporated|inc|corporation|corp|company|co|limited|ltd|l\.?l\.?c|llc"
    r"|lllp|l\.?l\.?p|llp|l\.?p|lp|p\.?l\.?c|plc|s\.?a\.?r\.?l|sarl|s\.?c\.?s\.?p"
    r"|scsp|gmbh|s\.?a|a\.?g|ag|b\.?v|n\.?v|s\.?r\.?l|srl|ulc|aps)"
)
_ENDS_WITH_SUFFIX_RE: Final = re.compile(
    r"\b" + _SUFFIX_ALT + r"\.?(?:\s*\(\s*" + _FN_ATOM + r"\s*\))*\s*$",
    re.IGNORECASE,
)
_AND_RE: Final = re.compile(r"\s+and\s+", re.IGNORECASE)

# --- entity-type classification --------------------------------------------
_RE_NONENTITY: Final = re.compile(
    r"^(?:total\b|sub\s*total\b|net\s+assets\b|liabilities\s+in\s+excess"
    r"|unfunded\s+commitments?\b)|investments?\s+and\s+cash",
    re.IGNORECASE,
)
# a "name" that is built ENTIRELY of instrument / filler words is not a company
_RE_INSTRUMENT_ONLY: Final = re.compile(
    r"^(?:first|second|third|fourth|fifth|senior|secured|subordinated|unsecured"
    r"|lien|term|loan|loans|debt|notes?|bonds?|revolving|revolver|delayed|draw"
    r"|unfunded|funded|credit|line|facilit(?:y|ies)|priority|equity|securities"
    r"|warrants?|investments?|preferred|common|and|of|the|in|a|[\W\d])+$",
    re.IGNORECASE,
)
_RE_CASH: Final = re.compile(
    r"\b(?:u\.?\s?s\.?\s+treasury|treasury\s+bill|treasury\s+note|t-bill"
    r"|money\s+market|cash\s+and\s+cash\s+equivalent|cash\s+equivalents?)\b",
    re.IGNORECASE,
)
_RE_CLO: Final = re.compile(
    r"\bCLO\b|collateralized\s+loan\s+obligation", re.IGNORECASE
)
_RE_FUND: Final = re.compile(
    r"\bfund\b(?:\s+(?:i|ii|iii|iv|v|vi|vii|viii|ix|x|\d+|[a-z]))?"
    r"[\s,]*(?:l\.?p\.?|ltd\.?|lp)?\s*$",
    re.IGNORECASE,
)


def _norm_ws(s: str) -> str:
    """Drop zero-width chars; collapse all whitespace (incl. \\xa0, newlines)."""
    return _WS_RE.sub(" ", _ZERO_WIDTH_RE.sub("", s)).strip()


def _extract_dba(s: str) -> tuple[str, str | None]:
    """Return (name_without_dba_parenthetical, brand_or_None)."""
    brand: str | None = None
    m = _DBA_RE.search(s)
    if m:
        brand = (m.group("brand") or "").strip().strip(",.;: ") or None
    return _DBA_RE.sub(" ", s), brand


def _strip_tranche(s: str) -> str:
    s = _WS_RE.sub(" ", s).strip()  # $-anchored rules below need clean tails
    s = _TRANCHE_PAREN_RE.sub(" ", s)
    s = _TRANCHE_DASH_RE.sub("", s)
    s = _TRANCHE_DASH_VOCAB_RE.sub("", s)
    m = _TRAIL_VOCAB_RE.search(s)
    if m and _TR_STRONG_RE.search(m.group(0)):
        s = s[:m.start()]
    return s


def _clean_one(piece: str) -> tuple[str, str | None]:
    """Clean a single-entity cell → (cleaned_name, dba)."""
    s = _SUPERSCRIPT_RE.sub("", piece)
    s, dba = _extract_dba(s)
    s = _PCT_PAREN_RE.sub(" ", s)
    s = _UNIT_PAREN_RE.sub(" ", s)
    s = _FOOTNOTE_PAREN_RE.sub(" ", s)
    s = _strip_tranche(s)
    s = _FOOTNOTE_PAREN_RE.sub(" ", s)
    s = s.replace("^", "").replace("~", "")
    prev = None
    while prev != s:
        prev = s
        s = _TRAIL_JUNK_RE.sub("", s)
    return _WS_RE.sub(" ", s).strip().strip(",").strip(), dba


def _classify(name: str) -> str:
    if not name:
        return "non_entity"
    if _RE_NONENTITY.search(name) or _RE_INSTRUMENT_ONLY.match(name):
        return "non_entity"
    if _RE_CASH.search(name):
        return "cash_or_treasury"
    if _RE_CLO.search(name):
        return "clo"
    if _RE_FUND.search(name):
        return "fund"
    return "company"


def _split_multi(raw: str) -> list[str]:
    """Split a multi-borrower cell on ' and ' — only where the text before the
    ' and ' ends in a legal-entity suffix (so 'Sako and Partners Lower Holdings
    LLC' stays one entity, 'A Inc. and B LLC' splits into two)."""
    parts: list[str] = []
    last = 0
    for m in _AND_RE.finditer(raw):
        left = raw[last:m.start()]
        if _ENDS_WITH_SUFFIX_RE.search(left):
            parts.append(left)
            last = m.end()
    parts.append(raw[last:])
    return parts


def normalize(raw: str | None) -> dict:
    """Clean a raw BDC SOI portfolio-company cell.

    Returns:
      {"multi_borrower": bool,
       "entities": [{"cleaned_name": str, "dba": str|None, "entity_type": str}]}

    entity_type ∈ {company, clo, cash_or_treasury, fund, non_entity}.
    """
    if raw is None:
        return {"multi_borrower": False, "entities": []}
    raw = _norm_ws(str(raw))
    if not raw:
        return {"multi_borrower": False, "entities": []}
    pieces = _split_multi(raw)
    entities = []
    for p in pieces:
        cleaned, dba = _clean_one(p)
        etype = _classify(cleaned)
        if not cleaned:  # fully stripped — keep a readable fallback, type it junk
            cleaned = _WS_RE.sub(" ", _SUPERSCRIPT_RE.sub("", p)).strip()
            etype = "non_entity"
        entities.append(
            {"cleaned_name": cleaned, "dba": dba, "entity_type": etype}
        )
    return {"multi_borrower": len(entities) > 1, "entities": entities}

"""UCC CA lender classifier — bank / credit_union / non_bank / unknown.

Three-layer classification for a secured-party ORG_NAME:

  Layer 1: FDIC name corpus fuzzy match → bank
  Layer 2: NCUA name corpus fuzzy match → credit_union
  Layer 3: Name heuristics (keyword rules) → bank | credit_union | non_bank | unknown

The corpora (FDIC / NCUA) are loaded lazily from Lance datasets on R2 and
cached in-process for bulk-classification runs (one load for the full sweep,
not one per name).

Category inference (separate from bank_classification):
  category_inferred_from_name ∈ {equipment_finance, factoring_ar, working_capital,
  real_estate, merchant_cash_advance, general_specialty_finance, unknown}

Usage:
    from scripts._lib.ucc_ca_classifier import classify_lender, classify_category

    bank_class = classify_lender("U.S. BANK NATIONAL ASSOCIATION")  # → "bank"
    category   = classify_category("CRESTRIDGE EQUIPMENT FINANCE")  # → "equipment_finance"

Constraint P5 (hard gate): ≤5 of top-100 lenders by total_filings may remain
"unknown" for bank_classification. If this gate fails the s16 surface fails.

Notes:
  - Trigram / token-set similarity via `rapidfuzz` (installed via uv extras).
  - Fallthrough to name-heuristic when corpus-match score < FUZZY_THRESHOLD.
  - Pure-Python; no I/O at import time. Corpus load happens on first call to
    classify_lender_bulk() or explicit classify_lender(name, corpora=...).
"""
from __future__ import annotations

import logging
import os
import re
from functools import lru_cache
from typing import NamedTuple

LOG = logging.getLogger(__name__)

FUZZY_THRESHOLD = 85  # rapidfuzz token_set_ratio score; 0–100

# ── Keyword heuristics ─────────────────────────────────────────────────────── #

# Patterns that suggest "bank" even without FDIC corpus match.
_BANK_KEYWORDS = re.compile(
    r"\b(bank|bancorp|bancorporation|savings\s+bank|savings\s+association|savings\s+&\s+loan"
    r"|federal\s+savings|national\s+association|national\s+bank|trust\s+company"
    r"|finance\s+company|investment\s+bank|commercial\s+bank"
    r"|citibank|citicorp|citigroup|wells\s+fargo|jpmorgan|chase\s+bank"
    r"|bank\s+of\s+america|bank\s+of\s+the\s+west|us\s+bank|u\.s\.\s+bank"
    r"|pnc\s+bank|td\s+bank|regions\s+bank|fifth\s+third|suntrust|truist)\b",
    re.IGNORECASE,
)

# Patterns that unambiguously suggest "credit_union" (also NCUA fallback).
_CU_KEYWORDS = re.compile(
    r"\b(credit\s+union|fcu|cu|federal\s+credit|employees\s+credit|member\s+credit"
    r"|municipal\s+credit|service\s+credit|teachers\s+credit|workers\s+credit"
    r"|community\s+credit|first\s+choice\s+cu|educational\s+employees\s+cu)\b",
    re.IGNORECASE,
)

# Patterns associated with non-bank specialty finance.
_NONBANK_INDICATORS = re.compile(
    r"\b(equipment|leasing|lease|capital|funding|finance|financial|factoring"
    r"|lending|lender|ventures|investors|investments|asset|mortgage"
    r"|commercial\s+credit|receivables|advance|acceptance)\b",
    re.IGNORECASE,
)

# Government agencies — UCC filers for tax liens, not traditional secured lenders.
# These should classify as non_bank (they are not banks/CUs, but are secured parties
# by operation of law). Examples: EDD, IRS, CDTFA, SBA, Board of Equalization.
_GOVERNMENT_INDICATORS = re.compile(
    r"\b(employment\s+development|small\s+business\s+administration|department\s+of\s+"
    r"tax|board\s+of\s+equalization|franchise\s+tax|internal\s+revenue|irs"
    r"|state\s+of\s+california|department\s+of\s+labor|secretary\s+of\s+state"
    r"|franchise\s+tax\s+board|state\s+tax|federal\s+tax|tax\s+lien"
    r"|california\s+department|united\s+states\s+of\s+america"
    r"|u\.s\.\s+department|u\.s\s+department|small\s+business\s+admin"
    r"|u\.s\.\s+small\s+business|u\.s\s+small\s+business)\b",
    re.IGNORECASE,
)

# Registered agent / representative placeholders — not lenders, but file as secured
# party as agent-of-record. Classify as non_bank (not a bank/CU, is a commercial entity).
_AGENT_INDICATORS = re.compile(
    r"\b(corporation\s+service\s+company|ct\s+corporation|c\s+t\s+corporation"
    r"|national\s+registered\s+agents|northwest\s+registered\s+agent"
    r"|cogency\s+global|incorp\s+services|first\s+corporate\s+solutions"
    r"|national\s+corporate\s+research|registered\s+agent|as\s+representative"
    r"|as\s+collateral\s+agent|as\s+administrative\s+agent)\b",
    re.IGNORECASE,
)

# Fintech / specialty lenders that don't have traditional keywords but are non-bank
_FINTECH_INDICATORS = re.compile(
    r"\b(solar|energy|green|mosaic|goodleap|loanpal|sunrun|sunpower|vivint"
    r"|sunnova|solarworld|enphase|sungevity|clean\s+energy"
    r"|kubota|deere|john\s+deere|caterpillar|cnh|case\s+new|agco"
    r"|ford\s+motor|general\s+motors|gm\s+financial|ally\s+financial"
    r"|american\s+express|synchrony|cit\s+group|marlin|blue\s+vine"
    r"|kabbage|ondeck|funding\s+circle|square\s+capital|paypal)\b",
    re.IGNORECASE,
)

# Additional non-bank patterns for entities common in UCC secured-party filings
_ADDITIONAL_NONBANK = re.compile(
    r"\b(tesla|matco|snap.on|snap on|patterson\s+dental|patterson\s+medical"
    r"|henry\s+schein|benco|midmark|dentsply|enfin|navitas"
    r"|chtd\s+company|chtd|inc\.\s*as\s*representative"
    r"|farm\s+credit|fannie\s+mae|freddie\s+mac|sallie\s+mae|ginnie\s+mae"
    r"|federal\s+home\s+loan|federal\s+reserve|federal\s+deposit"
    r"|flca|pca|aca\s*$|american\s+agcredit|agwest|agfirst"
    r"|cit\s+bank|cit\s+finance)\b",
    re.IGNORECASE,
)

# ── Category inference keywords ────────────────────────────────────────────── #

_CAT_EQUIPMENT = re.compile(
    r"\b(equipment|machinery|technology|hardware|fleet|vehicle|truck|forklift)\b",
    re.IGNORECASE,
)
_CAT_FACTORING = re.compile(
    r"\b(factor|factoring|receivable|accounts\s+receivable|ar\s+finance|invoice)\b",
    re.IGNORECASE,
)
_CAT_WORKING_CAPITAL = re.compile(
    r"\b(working\s+capital|business\s+capital|small\s+business|sba|microloan)\b",
    re.IGNORECASE,
)
_CAT_REAL_ESTATE = re.compile(
    r"\b(mortgage|real\s+estate|property|realty|commercial\s+real|multifamily"
    r"|residential\s+mortgage|trust\s+deed)\b",
    re.IGNORECASE,
)
_CAT_MCA = re.compile(
    r"\b(merchant\s+cash|merchant\s+advance|mca|daily\s+advance|revenue\s+advance"
    r"|pos\s+advance|batch\s+advance)\b",
    re.IGNORECASE,
)


def classify_category(name: str | None) -> str:
    """Infer a lending-category label from a normalized lender name.

    Returns one of: equipment_finance, factoring_ar, working_capital,
    real_estate, merchant_cash_advance, general_specialty_finance, unknown.
    """
    if not name:
        return "unknown"
    if _CAT_MCA.search(name):
        return "merchant_cash_advance"
    if _CAT_REAL_ESTATE.search(name):
        return "real_estate"
    if _CAT_FACTORING.search(name):
        return "factoring_ar"
    if _CAT_EQUIPMENT.search(name):
        return "equipment_finance"
    if _CAT_WORKING_CAPITAL.search(name):
        return "working_capital"
    if _NONBANK_INDICATORS.search(name):
        return "general_specialty_finance"
    return "unknown"


# ── Corpus-backed classification ───────────────────────────────────────────── #

class _Corpora(NamedTuple):
    fdic_names: list[str]   # uppercased normalized bank names
    ncua_names: list[str]   # uppercased normalized credit-union names


_CACHED_CORPORA: _Corpora | None = None


def _load_corpora() -> _Corpora:
    """Load FDIC + NCUA name corpora from R2 Lance datasets. Cached in-process."""
    global _CACHED_CORPORA
    if _CACHED_CORPORA is not None:
        return _CACHED_CORPORA

    import lance

    storage_options = {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
    }

    fdic_names: list[str] = []
    try:
        ds = lance.dataset(
            "s3://dex-raw-landing-zone/polaris-warehouse/fdic/institutions_lance/",
            storage_options=storage_options,
        )
        tbl = ds.to_table(columns=["NAME"])
        fdic_names = [
            n.as_py().upper()
            for n in tbl.column("NAME")
            if n.as_py()
        ]
        LOG.info("Loaded %d FDIC institution names", len(fdic_names))
    except Exception as e:
        LOG.warning("Could not load FDIC corpus (falling back to heuristics): %s", e)

    ncua_names: list[str] = []
    try:
        ds = lance.dataset(
            "s3://dex-raw-landing-zone/polaris-warehouse/ncua/credit_unions_lance/",
            storage_options=storage_options,
        )
        # NCUA uses CU_NAME column (from NCUA Call Report CSV)
        schema_cols = set(ds.schema.names)
        name_col = "CU_NAME" if "CU_NAME" in schema_cols else "CREDIT_UNION_NAME"
        if name_col not in schema_cols:
            # Fallback: try any column with 'name' in it
            for col in schema_cols:
                if "name" in col.lower():
                    name_col = col
                    break
        tbl = ds.to_table(columns=[name_col])
        ncua_names = [
            n.as_py().upper()
            for n in tbl.column(name_col)
            if n.as_py()
        ]
        LOG.info("Loaded %d NCUA credit-union names", len(ncua_names))
    except Exception as e:
        LOG.warning("Could not load NCUA corpus (falling back to heuristics): %s", e)

    _CACHED_CORPORA = _Corpora(fdic_names=fdic_names, ncua_names=ncua_names)
    return _CACHED_CORPORA


def _fuzzy_match_any(name: str, corpus: list[str], threshold: int = FUZZY_THRESHOLD) -> bool:
    """Check if `name` fuzzy-matches any entry in corpus above threshold."""
    if not corpus:
        return False
    try:
        from rapidfuzz import process, fuzz

        result = process.extractOne(
            name.upper(),
            corpus,
            scorer=fuzz.token_set_ratio,
            score_cutoff=threshold,
        )
        return result is not None
    except ImportError:
        # rapidfuzz not installed — fall through to heuristic-only
        return False


def classify_lender(
    name: str | None,
    corpora: _Corpora | None = None,
) -> str:
    """Classify a lender name as bank / credit_union / non_bank / unknown.

    When called with `corpora=None`, loads corpora lazily on first call
    (R2 Lance datasets must be available in the current Doppler env).
    Pass `corpora=_load_corpora()` for bulk runs to avoid repeated I/O.

    Constraint P5: if the corpora are unavailable, falls back to name heuristics.
    """
    if not name or not name.strip():
        return "unknown"

    name_upper = name.upper().strip()

    # Layer 1: FDIC corpus fuzzy match
    try:
        c = corpora if corpora is not None else _load_corpora()
    except Exception:
        c = _Corpora(fdic_names=[], ncua_names=[])

    if c.fdic_names and _fuzzy_match_any(name_upper, c.fdic_names):
        return "bank"

    # Layer 2: NCUA corpus fuzzy match
    if c.ncua_names and _fuzzy_match_any(name_upper, c.ncua_names):
        return "credit_union"

    # Layer 3: name heuristics
    if _CU_KEYWORDS.search(name_upper):
        return "credit_union"
    if _BANK_KEYWORDS.search(name_upper):
        return "bank"
    # Government agencies, registered agents, and fintech/specialty lenders
    if _GOVERNMENT_INDICATORS.search(name_upper):
        return "non_bank"
    if _AGENT_INDICATORS.search(name_upper):
        return "non_bank"
    if _ADDITIONAL_NONBANK.search(name_upper):
        return "non_bank"
    if _FINTECH_INDICATORS.search(name_upper):
        return "non_bank"
    if _NONBANK_INDICATORS.search(name_upper):
        return "non_bank"

    return "unknown"


def classify_lender_bulk(names: list[str]) -> list[str]:
    """Classify a list of lender names in bulk (one corpus load for all)."""
    try:
        corpora = _load_corpora()
    except Exception as e:
        LOG.warning("Corpus load failed; falling back to heuristics: %s", e)
        corpora = _Corpora(fdic_names=[], ncua_names=[])

    return [classify_lender(n, corpora=corpora) for n in names]

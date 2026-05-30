"""CA UCC lender LLM website-classification → Lance emit (Pattern A enriched-cohort).

Builds an LLM-classified Lance dataset of California UCC secured parties (lenders),
from which the operator's target — independent equipment-finance lenders — filters
out by `subtype` + `equipment_finance_confidence`.

This is the CORRECTED equipment-finance-lender classifier. The prior approach
(scoring lenders by their debtors' construction/manufacturing industry mix —
PRs #590/#593) was disproven and reverted (#595). The corrected, proof-validated
approach: rank UCC secured parties by FILING VOLUME (isolates a lender-dense
population), then classify each by website + name with an LLM.

Mechanism (per directive 2026-05-20-ucc-ca-lender-llm-classification.md):
 1. Spine — ucc_ca/lenders_lance ranked by total_filings desc, EXCLUDING
    government tax authorities + registered-agent companies, take
    total_filings >= 100 → ~2,111 lenders. lenders_lance is strictly 1:1 on
    lender_name_normalized, so the spine is one row per lender.
 2. Website resolution — join to bridges/ucc_pdl_lance for pdl_website /
    pdl_name / pdl_industry. The name keys differ: lenders_lance.
    lender_name_normalized is UPPER(TRIM(ORG_NAME)); ucc_pdl_lance.
    secured_party_name_normalized is normalize_entity_name(...). Apply
    normalize_entity_name() to the spine key, then equality-join. ~32% match.
 3. LLM classification — BATCH: parallel async Anthropic API calls at bounded
    concurrency (asyncio.Semaphore). For each lender with a website: HTTP-fetch
    the site (browser User-Agent, follow redirects; fetch_status is a
    first-class column), build a classification prompt (raw UCC name + PDL name
    + website text), call claude-sonnet-4-6 with a structured tool-use schema
    and prompt caching on the static instructions block. No-website lenders get
    a metadata-only pass (name + PDL fields + bank_classification /
    category_inferred_from_name), classification_method='metadata_only',
    equipment_finance_confidence capped low.
 4. Emit — Pattern A Lance dataset, one row per spine lender. BTREE on the
    lender key; compact_files() + cleanup_old_versions(); per-row generated_at
    + a run UUID. Polaris registration.

Inputs:
- s3://.../polaris-warehouse/ucc_ca/lenders_lance          (101,037 rows, 1:1)
- s3://.../polaris-warehouse/bridges/ucc_pdl_lance         (18,020 rows)

Output:
- s3://.../polaris-warehouse/ucc_ca/lender_classification_lance
  one row per spine lender, BTREE on lender_name_normalized.

Per DATA-FACTORY-ARCHITECTURE-PATTERNS.md §"Pattern A enriched-cohort emit":
NOT a new identity bridge — no registry / match-method rows (L28). Per-row
generated_at + a fresh run_id UUID for provenance.

Run via:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        uv run python scripts/build_ucc_ca_lender_classification_lance.py --apply

    # smoke (classify only the first N lenders, still emits a Lance dataset):
    ... --apply --limit 50
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._lib.entity_name_normalize import normalize_entity_name
from scripts._lib.lance_commit_lock import lance_commit_lock

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)

# ── load-bearing constants ──────────────────────────────────────────────────

R2_BUCKET = "dex-raw-landing-zone"

LENDERS_LANCE_URI = f"s3://{R2_BUCKET}/polaris-warehouse/ucc_ca/lenders_lance"
PDL_BRIDGE_LANCE_URI = f"s3://{R2_BUCKET}/polaris-warehouse/bridges/ucc_pdl_lance"

OUTPUT_LANCE_URI = (
    f"s3://{R2_BUCKET}/polaris-warehouse/ucc_ca/lender_classification_lance"
)
DATASET_SLUG = "lender_classification_lance"
POLARIS_NAMESPACE = "ucc_ca"
LENDER_KEY = "lender_name_normalized"

# Validator-frozen numbers (see validator.json / directive — do NOT re-litigate).
# N = total_filings >= 100 → spine ≈ 2,111 lenders.
MIN_TOTAL_FILINGS = 100
MIN_ROW_FLOOR = 2000

EMIT_VERSION = "1.0.0"
ANTHROPIC_MODEL = "claude-sonnet-4-6"

# Bounded async concurrency for the Anthropic batch (directive §LLM batch shape).
LLM_CONCURRENCY = 12
# Bounded async concurrency for HTTP website fetches (kinder to dead hosts).
FETCH_CONCURRENCY = 16
FETCH_TIMEOUT_SEC = 15.0
# Cap the website text fed to the model (chars) — keeps prompts bounded.
MAX_SITE_TEXT_CHARS = 12_000

TMP_DIR = "/tmp/lance"

# Browser User-Agent for website fetches (directive §Website fetch).
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# ── classification taxonomy ─────────────────────────────────────────────────
# The directive does not enumerate the full subtype taxonomy (executor latitude
# per validator.json P5 / §"One open executor-discretion item"). It MUST include
# `independent_equipment_finance_or_leasing` (the operator's target) plus the
# non-pre-excluded subtypes. The LLM structured-output schema constrains to this
# set; the verify harness asserts every emitted value ∈ this declared set.

# The smoke-gate target literal — pinned once, reused everywhere.
TARGET_SUBTYPE = "independent_equipment_finance_or_leasing"

SUBTYPE_ENUM = [
    # ---- equipment finance / leasing ----
    "independent_equipment_finance_or_leasing",  # the operator's target
    "oem_captive_finance",                       # manufacturer-owned finance arm
    # ---- banks & depositories ----
    "bank_or_depository",                        # banks, thrifts, credit unions
    # ---- other finance verticals ----
    "general_commercial_finance",                # broad commercial lending / factoring
    "specialty_or_consumer_finance",             # consumer / auto / merchant-cash / niche
    "solar_or_clean_energy_finance",             # solar PPA / clean-energy lending
    "real_estate_or_mortgage_finance",           # CRE / mortgage / hard-money
    "agricultural_or_farm_credit",               # ag lenders, Farm Credit System
    # ---- non-lender entities ----
    "government_or_tax_authority",               # leaked-through gov body
    "registered_agent_or_filing_service",        # leaked-through agent
    "vendor_or_dealer_or_manufacturer",          # files UCC-1s as a vendor, not a lender
    "other_non_lender",                          # any other non-lender entity
    "unknown",                                   # genuinely undeterminable
]

IS_LENDER_ENUM = ["true", "false", "unknown"]

# fetch_status value set — directive §"Website fetch" (7 values).
FETCH_STATUS_ENUM = [
    "ok", "dead", "parked", "bot_walled",
    "redirected_offsite", "dns_fail", "no_website",
]

CLASSIFICATION_METHOD_ENUM = ["website", "metadata_only"]

# Cap the equipment_finance_confidence of metadata-only classifications (a
# name + PDL-industry pass cannot be high-confidence about equipment finance).
METADATA_ONLY_CONFIDENCE_CAP = 0.50

# Government tax-authority + registered-agent exclusion patterns (directive
# Goal §1). Matched case-insensitively against the raw lender_name_normalized
# (which is already UPPER(TRIM(...))).
GOV_PATTERNS = [
    "EMPLOYMENT DEVELOPMENT DEPARTMENT", "EDD", "INTERNAL REVENUE SERVICE",
    "FRANCHISE TAX BOARD", "CDTFA", "BOARD OF EQUALIZATION",
    "SMALL BUSINESS ADMINISTRATION",
    "STATE OF ", "CITY OF ", "COUNTY OF ", "DEPARTMENT OF ",
]
AGENT_PATTERNS = [
    "CT CORPORATION", "CSC ", "CORPORATION SERVICE COMPANY", "CHTD", "COGENCY",
    "NATIONAL REGISTERED AGENT", "NORTHWEST REGISTERED AGENT",
    "FIRST CORPORATE SOLUTIONS", "INCORP",
]


# ── LLM tool schema ─────────────────────────────────────────────────────────
# Anthropic structured output: tool_choice forces exactly one call of this tool.

CLASSIFY_TOOL = {
    "name": "record_lender_classification",
    "description": (
        "Record the classification of a California UCC secured party. "
        "Always call this exactly once."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "is_lender": {
                "type": "string",
                "enum": IS_LENDER_ENUM,
                "description": (
                    "Is this entity a lender / finance company / leasing "
                    "company (an extender of credit)? 'true' = yes, "
                    "'false' = not a lender (vendor, gov body, agent, etc.), "
                    "'unknown' = undeterminable."
                ),
            },
            "subtype": {
                "type": "string",
                "enum": SUBTYPE_ENUM,
                "description": (
                    "The most specific finance subtype, chosen by the "
                    "entity's CORE OPERATING BUSINESS, not its charter type. "
                    "'independent_equipment_finance_or_leasing' = a company "
                    "whose core business is financing or leasing business "
                    "equipment / vehicles / machinery / technology, not tied "
                    "to one manufacturer — INCLUDING bank-chartered or "
                    "bank-owned equipment-finance companies. "
                    "'oem_captive_finance' = a finance arm dedicated to one "
                    "equipment manufacturer it belongs to (Caterpillar "
                    "Financial, John Deere Financial). 'bank_or_depository' = "
                    "an entity whose core business is general banking / "
                    "deposit-taking (a broad retail/commercial bank, thrift, "
                    "or credit union) — NOT a single-purpose equipment-finance "
                    "company that merely happens to hold a bank charter. Pick "
                    "'unknown' only if genuinely undeterminable."
                ),
            },
            "equipment_finance_confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": (
                    "Confidence (0..1) that this entity's core business is "
                    "equipment finance / equipment leasing (i.e. that "
                    "subtype='independent_equipment_finance_or_leasing' is "
                    "correct). 1 = certainly an equipment-finance company; "
                    "0 = certainly not. A bank-chartered or bank-owned company "
                    "that is genuinely an equipment-finance business scores "
                    "HIGH. A general-purpose bank and an OEM captive score LOW."
                ),
            },
            "rationale": {
                "type": "string",
                "description": (
                    "Brief (1-3 sentence) explanation citing the specific "
                    "evidence (website language, company name, PDL industry) "
                    "that drove the classification."
                ),
            },
        },
        "required": [
            "is_lender", "subtype", "equipment_finance_confidence", "rationale",
        ],
    },
}

SYSTEM_PROMPT = (
    "You are an analyst classifying California UCC-1 secured parties. A UCC-1 "
    "financing statement is filed when a creditor takes a security interest in "
    "a debtor's collateral — so high-volume UCC-1 filers are overwhelmingly "
    "lenders, finance companies, and leasing companies.\n\n"
    "Your job: classify each secured party by its CORE OPERATING BUSINESS — "
    "what the company actually does day-to-day — NOT by its legal charter "
    "type and NOT by who its corporate parent is. The operator is hunting for "
    "one specific population: equipment-finance and equipment-leasing lenders "
    "whose primary business is putting businesses into equipment via financing "
    "or leasing.\n\n"
    "THE CENTRAL RULE — classify by core business, not charter:\n"
    "Many equipment-finance companies are bank-chartered (e.g. a Utah "
    "industrial bank / ILC) or are a wholly-owned subsidiary or division of a "
    "bank. That does NOT make them a 'bank' for this taxonomy. If the "
    "company's core operating business is equipment finance / equipment "
    "leasing — its website and name are about financing/leasing business "
    "equipment, vehicles, or machinery — classify it "
    "'independent_equipment_finance_or_leasing', EVEN IF it holds a bank "
    "charter or is owned by a bank. Reserve 'bank_or_depository' for entities "
    "whose core business is general banking / deposit-taking — branch banking, "
    "checking and savings accounts, consumer and commercial deposits, a broad "
    "retail/commercial banking franchise. A general-purpose bank that happens "
    "to have an equipment-finance division is 'bank_or_depository'; a company "
    "whose whole reason for existing is equipment finance is "
    "'independent_equipment_finance_or_leasing' regardless of charter. When a "
    "company's name itself says 'Equipment Finance' / 'Equipment Leasing' / "
    "'Capital' and the site confirms an equipment-finance business, that is a "
    "strong signal for 'independent_equipment_finance_or_leasing'.\n\n"
    "Subtypes:\n"
    "- 'independent_equipment_finance_or_leasing' — core business is financing "
    "or leasing a broad range of business equipment, vehicles, machinery, or "
    "technology to businesses; NOT tied to a single equipment manufacturer. "
    "Bank-chartered or bank-owned equipment-finance companies belong HERE, not "
    "in bank_or_depository.\n"
    "- 'oem_captive_finance' — a finance arm whose purpose is to finance the "
    "products of ONE equipment manufacturer it belongs to (Caterpillar "
    "Financial, John Deere Financial, Komatsu Financial, Kubota Credit, etc.).\n"
    "- 'bank_or_depository' — core business is general banking / deposit-"
    "taking: a retail or commercial bank, thrift, savings institution, or "
    "credit union with a broad banking franchise. NOT a single-purpose "
    "equipment-finance company that merely happens to be chartered as a bank.\n"
    "- 'solar_or_clean_energy_finance' — solar PPA / clean-energy lending.\n"
    "- 'real_estate_or_mortgage_finance' — CRE / mortgage / hard-money "
    "lending.\n"
    "- 'general_commercial_finance' — broad commercial lending / factoring / "
    "working-capital finance not specialized in equipment.\n"
    "- 'specialty_or_consumer_finance' — consumer / auto-retail / merchant-"
    "cash-advance / other niche consumer or specialty lending.\n"
    "- 'agricultural_or_farm_credit' — agricultural lenders / Farm Credit "
    "System entities.\n"
    "- If the entity is plainly NOT a lender — a government body, a registered "
    "agent / corporate filing service, or a vendor/dealer/manufacturer that "
    "files UCC-1s on its own equipment sales — use the matching non-lender "
    "subtype and set is_lender='false'.\n\n"
    "Set equipment_finance_confidence to your probability that the entity's "
    "core business is equipment finance / equipment leasing (i.e. that "
    "subtype='independent_equipment_finance_or_leasing' is correct). A bank-"
    "chartered or bank-owned company that is genuinely an equipment-finance "
    "business should score HIGH here. A general-purpose bank and an OEM "
    "captive should score LOW. Be decisive but honest: use 'unknown' and low "
    "confidence when the evidence is genuinely thin. Record your answer with "
    "the record_lender_classification tool; do not reply in prose."
)


def _storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _register_polaris(table_name: str, doc: str) -> None:
    script = Path(__file__).resolve().parent / "init_polaris_lance_generic.py"
    cmd = [
        sys.executable, str(script),
        "--namespace", POLARIS_NAMESPACE,
        "--table", table_name,
        "--doc", doc,
    ]
    logger.info("registering Polaris: %s.%s", POLARIS_NAMESPACE, table_name)
    try:
        subprocess.run(cmd, check=True, timeout=90)
        logger.info("Polaris registration OK: %s.%s", POLARIS_NAMESPACE, table_name)
    except Exception as exc:  # SOFT — Polaris may 502; non-fatal per directive.
        logger.warning("Polaris registration error (non-fatal): %s", exc)


# ── Step 1+2: spine build + website resolution ──────────────────────────────


def build_spine(storage_options: dict, limit: int | None) -> list[dict]:
    """Build the volume-ranked lender spine joined to PDL website data.

    Returns a list of per-lender dicts (one row per spine lender), each with
    the spine columns + the resolved PDL website fields (None where no match).
    """
    import duckdb
    import lance

    logger.info("scanning lenders_lance (spine source) ...")
    lenders_tbl = lance.dataset(
        LENDERS_LANCE_URI, storage_options=storage_options
    ).scanner(
        columns=[
            "lender_name_normalized", "total_filings", "active_filings",
            "first_filing_date", "last_filing_date",
            "top_debtor_states", "top_debtor_cities",
            "bank_classification", "category_inferred_from_name",
            "address_sample",
        ],
    ).to_table()
    logger.info("lenders_lance rows read: %d", lenders_tbl.num_rows)

    logger.info("scanning ucc_pdl_lance (website source) ...")
    pdl_tbl = lance.dataset(
        PDL_BRIDGE_LANCE_URI, storage_options=storage_options
    ).scanner(
        columns=[
            "secured_party_name_normalized", "pdl_name", "pdl_website",
            "pdl_industry", "pdl_size", "pdl_linkedin_url", "pdl_locality",
        ],
    ).to_table()
    logger.info("ucc_pdl_lance rows read: %d", pdl_tbl.num_rows)

    con = duckdb.connect()
    con.execute("SET threads=4")
    con.register("lenders_raw", lenders_tbl)
    con.register("pdl_raw", pdl_tbl)

    # The PDL-side join key is normalize_entity_name(...). Apply the SAME Python
    # normalizer to BOTH sides as a DuckDB UDF so the SQL join uses identical
    # logic (per probe_inputs.py — the reference implementation of this join,
    # and validator.json prediction P1).
    con.create_function(
        "py_norm", normalize_entity_name, ["VARCHAR"], "VARCHAR"
    )

    excl_sql = " OR ".join(
        f"upper(lender_name_normalized) LIKE '%{p}%'"
        for p in GOV_PATTERNS + AGENT_PATTERNS
    )

    # PDL side: collapse to one row per normalized key, preferring a row that
    # has a non-empty pdl_website. ROW_NUMBER over (website-present-first).
    con.execute(
        """
        CREATE TEMP TABLE pdl_by_key AS
        WITH ranked AS (
            SELECT
                py_norm(secured_party_name_normalized) AS norm_key,
                pdl_name, pdl_website, pdl_industry, pdl_size,
                pdl_linkedin_url, pdl_locality,
                ROW_NUMBER() OVER (
                    PARTITION BY py_norm(secured_party_name_normalized)
                    ORDER BY
                        CASE WHEN pdl_website IS NOT NULL
                                  AND length(trim(pdl_website)) > 0
                             THEN 0 ELSE 1 END,
                        pdl_name
                ) AS rn
            FROM pdl_raw
            WHERE secured_party_name_normalized IS NOT NULL
              AND py_norm(secured_party_name_normalized) IS NOT NULL
        )
        SELECT norm_key, pdl_name, pdl_website, pdl_industry, pdl_size,
               pdl_linkedin_url, pdl_locality
        FROM ranked
        WHERE rn = 1
        """
    )

    limit_sql = f"LIMIT {int(limit)}" if limit else ""
    rows = con.execute(
        f"""
        SELECT
            l.lender_name_normalized,
            TRY_CAST(l.total_filings AS BIGINT)  AS total_filings,
            TRY_CAST(l.active_filings AS BIGINT) AS active_filings,
            l.first_filing_date,
            l.last_filing_date,
            l.top_debtor_states,
            l.top_debtor_cities,
            l.bank_classification,
            l.category_inferred_from_name,
            l.address_sample,
            py_norm(l.lender_name_normalized)   AS norm_key,
            p.pdl_name,
            p.pdl_website,
            p.pdl_industry,
            p.pdl_size,
            p.pdl_linkedin_url,
            p.pdl_locality
        FROM lenders_raw l
        LEFT JOIN pdl_by_key p
               ON p.norm_key = py_norm(l.lender_name_normalized)
        WHERE l.lender_name_normalized IS NOT NULL
          AND TRY_CAST(l.total_filings AS BIGINT) >= {MIN_TOTAL_FILINGS}
          AND NOT ({excl_sql})
        ORDER BY TRY_CAST(l.total_filings AS BIGINT) DESC
        {limit_sql}
        """
    ).arrow().read_all().to_pylist()

    n_with_site = sum(
        1 for r in rows
        if r.get("pdl_website") and str(r["pdl_website"]).strip()
    )
    pct = (100.0 * n_with_site / len(rows)) if rows else 0.0
    logger.info(
        "spine built: %d lenders (total_filings >= %d); "
        "%d have a PDL website (%.1f%%)",
        len(rows), MIN_TOTAL_FILINGS, n_with_site, pct,
    )
    # Pre-emit website-join probe (validator.json P1 risk_task): fail loudly if
    # the join collapsed to near-zero (would mean the normalize_entity_name
    # join is silently wrong). Expect ~32% on the full N=100 spine; relax the
    # floor to 15% so a --limit smoke run on the very top of the spine (where
    # coverage runs higher) and normal variance don't false-fail.
    if not limit and pct < 15.0:
        raise RuntimeError(
            f"website-join rate {pct:.1f}% far below the measured ~32% — "
            "the normalize_entity_name join is likely broken (P1)."
        )
    return rows


# ── Step 3a: website fetch ──────────────────────────────────────────────────


def _normalize_url(raw: str) -> str:
    """Coerce a PDL website value into an https:// URL."""
    u = str(raw).strip()
    if not u:
        return ""
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    return u


def _registrable_host(url: str) -> str:
    """Lowercase host with a leading 'www.' stripped — for offsite detection."""
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _html_to_text(html: str) -> str:
    """Strip tags/scripts/styles from HTML → visible-ish text. Stdlib only."""
    import re
    from html import unescape

    html = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


_PARKED_MARKERS = (
    "domain is for sale", "buy this domain", "this domain may be for sale",
    "parked domain", "domain parking", "godaddy.com/domainsearch",
    "is for sale!", "hugedomains", "sedo.com", "courtesy of",
)


async def fetch_website(client, url: str) -> tuple[str, str]:
    """Fetch a website. Returns (fetch_status, site_text).

    fetch_status ∈ FETCH_STATUS_ENUM. Never raises — a dead/blocked site is
    recorded, not fatal (directive §"Website fetch", validator.json P4).
    """
    import httpx

    norm = _normalize_url(url)
    if not norm:
        return "no_website", ""
    origin_host = _registrable_host(norm)
    try:
        resp = await client.get(
            norm,
            headers={
                "User-Agent": BROWSER_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            follow_redirects=True,
            timeout=FETCH_TIMEOUT_SEC,
        )
    except httpx.ConnectError as exc:
        msg = str(exc).lower()
        if "name or service not known" in msg or "nodename nor servname" in msg \
           or "getaddrinfo" in msg or "no address associated" in msg:
            return "dns_fail", ""
        return "dead", ""
    except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPError):
        return "dead", ""
    except Exception:  # noqa: BLE001 — fetch must never abort the run.
        return "dead", ""

    status = resp.status_code
    if status in (401, 403, 429) or status == 503:
        return "bot_walled", ""
    if status >= 400:
        return "dead", ""

    final_host = _registrable_host(str(resp.url))
    text = _html_to_text(resp.text or "")
    low = text.lower()

    if any(m in low for m in _PARKED_MARKERS) and len(text) < 2_500:
        return "parked", text[:MAX_SITE_TEXT_CHARS]
    if final_host and origin_host and final_host != origin_host:
        # Redirected to a different registrable host — record but still keep
        # the text (an acquired company may legitimately redirect).
        return "redirected_offsite", text[:MAX_SITE_TEXT_CHARS]
    if len(text) < 60:
        # Almost no extractable text (JS-only SPA or near-empty) — treat as
        # dead for classification purposes (v1 has no headless renderer).
        return "dead", text[:MAX_SITE_TEXT_CHARS]
    return "ok", text[:MAX_SITE_TEXT_CHARS]


# ── Step 3b: prompt construction ────────────────────────────────────────────


def _build_user_prompt(rec: dict, fetch_status: str, site_text: str) -> str:
    """Render the per-lender classification prompt."""
    lines = [
        "Classify the following California UCC secured party.",
        "",
        "== Identity ==",
        f"UCC secured-party name (raw): {rec.get('lender_name_normalized')}",
    ]
    if rec.get("pdl_name"):
        lines.append(f"Company name (People Data Labs match): {rec['pdl_name']}")
    if rec.get("pdl_industry"):
        lines.append(f"PDL self-reported industry: {rec['pdl_industry']}")
    if rec.get("pdl_size"):
        lines.append(f"PDL company size bucket: {rec['pdl_size']}")
    if rec.get("pdl_locality"):
        lines.append(f"PDL locality: {rec['pdl_locality']}")

    lines += [
        "",
        "== UCC filing profile ==",
        f"Total UCC-1 filings (all-time): {rec.get('total_filings')}",
        f"Active UCC-1 filings: {rec.get('active_filings')}",
    ]
    if rec.get("bank_classification"):
        lines.append(
            "Heuristic bank classification (low-trust prior): "
            f"{rec['bank_classification']}"
        )
    if rec.get("category_inferred_from_name"):
        lines.append(
            "Heuristic category inferred from name (low-trust prior): "
            f"{rec['category_inferred_from_name']}"
        )

    lines += ["", "== Website =="]
    if fetch_status == "no_website":
        lines.append(
            "No website is available for this entity. Classify from the name "
            "and PDL/heuristic fields above only. Be appropriately cautious — "
            "without a website you cannot be highly confident about equipment "
            "finance specifically."
        )
    elif site_text:
        label = (
            "Website text (extracted)" if fetch_status == "ok"
            else f"Website text (fetch_status={fetch_status}; may be partial)"
        )
        lines.append(f"{label}:")
        lines.append(site_text)
    else:
        lines.append(
            f"The website could not be retrieved (fetch_status={fetch_status}). "
            "Classify from the name and PDL/heuristic fields above only, with "
            "appropriate caution."
        )
    return "\n".join(lines)


# ── Step 3c: the async classification batch ─────────────────────────────────


def _extract_tool_result(msg) -> dict | None:
    """Pull the record_lender_classification tool_use input from a Message."""
    for block in msg.content:
        if getattr(block, "type", None) == "tool_use" and \
                getattr(block, "name", None) == "record_lender_classification":
            return block.input or {}
    return None


async def classify_one(
    anthropic_client, http_client, sem_llm, sem_fetch, rec: dict,
) -> dict:
    """Fetch (if a website exists) + classify one lender. Never raises.

    Returns the record dict extended with the classification columns.
    """
    import anthropic as anthropic_sdk

    website = rec.get("pdl_website")
    has_site = bool(website and str(website).strip())

    # --- website fetch (bounded concurrency) ---
    if has_site:
        async with sem_fetch:
            fetch_status, site_text = await fetch_website(http_client, website)
    else:
        fetch_status, site_text = "no_website", ""

    # classification_method: website-based only if we actually have usable text.
    website_text_usable = bool(site_text and len(site_text) >= 60)
    method = "website" if website_text_usable else "metadata_only"

    user_prompt = _build_user_prompt(rec, fetch_status, site_text)

    # --- Anthropic call (bounded concurrency, retry transient errors) ---
    parsed: dict | None = None
    api_error: str | None = None
    async with sem_llm:
        for attempt in range(1, 5):
            try:
                msg = await anthropic_client.messages.create(
                    model=ANTHROPIC_MODEL,
                    max_tokens=700,
                    system=[
                        {
                            "type": "text",
                            "text": SYSTEM_PROMPT,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    tools=[CLASSIFY_TOOL],
                    tool_choice={
                        "type": "tool",
                        "name": "record_lender_classification",
                    },
                    messages=[{"role": "user", "content": user_prompt}],
                )
                parsed = _extract_tool_result(msg)
                break
            except anthropic_sdk.APIStatusError as exc:
                api_error = f"{exc.status_code}"
                if exc.status_code in (429, 500, 502, 503, 529) and attempt < 4:
                    await asyncio.sleep(min(2 ** attempt, 30))
                    continue
                break
            except (anthropic_sdk.APIConnectionError,
                    anthropic_sdk.APITimeoutError) as exc:
                api_error = type(exc).__name__
                if attempt < 4:
                    await asyncio.sleep(min(2 ** attempt, 30))
                    continue
                break
            except Exception as exc:  # noqa: BLE001
                api_error = f"unexpected:{type(exc).__name__}"
                break

    # --- normalize the model output into typed columns ---
    if parsed is None:
        # API failed entirely — emit an honest 'unknown' row, never drop it.
        logger.warning(
            "classification failed for %r (api_error=%s) — emitting unknown",
            rec.get("lender_name_normalized"), api_error,
        )
        is_lender = "unknown"
        subtype = "unknown"
        confidence = 0.0
        rationale = f"classification unavailable (api_error={api_error})"
    else:
        is_lender = str(parsed.get("is_lender", "unknown")).strip().lower()
        if is_lender not in IS_LENDER_ENUM:
            is_lender = "unknown"
        subtype = str(parsed.get("subtype", "unknown")).strip()
        if subtype not in SUBTYPE_ENUM:
            subtype = "unknown"
        try:
            confidence = float(parsed.get("equipment_finance_confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        rationale = str(parsed.get("rationale", "") or "")

    # Metadata-only classifications cannot be high-confidence about equipment
    # finance specifically — cap the confidence (directive §Mechanism 3).
    if method == "metadata_only" and confidence > METADATA_ONLY_CONFIDENCE_CAP:
        confidence = METADATA_ONLY_CONFIDENCE_CAP

    out = dict(rec)
    out["is_lender"] = is_lender
    out["subtype"] = subtype
    out["equipment_finance_confidence"] = confidence
    out["fetch_status"] = fetch_status
    out["classification_method"] = method
    out["rationale"] = rationale
    return out


async def classify_batch(spine: list[dict]) -> list[dict]:
    """Run the full parallel async classification batch over the spine."""
    import anthropic as anthropic_sdk
    import httpx

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is not set in the environment.")

    sem_llm = asyncio.Semaphore(LLM_CONCURRENCY)
    sem_fetch = asyncio.Semaphore(FETCH_CONCURRENCY)

    anthropic_client = anthropic_sdk.AsyncAnthropic(max_retries=0)
    results: list[dict] = []
    done = 0
    total = len(spine)
    logger.info(
        "starting classification batch: %d lenders, "
        "LLM concurrency=%d, fetch concurrency=%d, model=%s",
        total, LLM_CONCURRENCY, FETCH_CONCURRENCY, ANTHROPIC_MODEL,
    )
    async with httpx.AsyncClient(
        http2=False, max_redirects=5,
        limits=httpx.Limits(max_connections=FETCH_CONCURRENCY + 4),
    ) as http_client:
        tasks = [
            asyncio.create_task(
                classify_one(
                    anthropic_client, http_client, sem_llm, sem_fetch, rec,
                )
            )
            for rec in spine
        ]
        for coro in asyncio.as_completed(tasks):
            results.append(await coro)
            done += 1
            if done % 100 == 0 or done == total:
                logger.info("  classified %d / %d", done, total)

    await anthropic_client.close()
    return results


# ── Step 4: Lance emit ──────────────────────────────────────────────────────


def emit(
    spine: list[dict], classified: list[dict], *, enforce_floor: bool = True,
) -> int:
    """Project the classified rows into a Pattern A Lance dataset.

    enforce_floor: when True (the production run) the row count must clear
    MIN_ROW_FLOOR or the emit HARD-fails. A --limit smoke run passes
    enforce_floor=False (it deliberately classifies a small slice).
    """
    import duckdb
    import lance

    os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")
    os.environ["TMPDIR"] = TMP_DIR
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)

    run_id = str(uuid.uuid4())
    generated_at = datetime.now(timezone.utc).isoformat()
    logger.info("run_id=%s generated_at=%s", run_id, generated_at)

    storage_options = _storage_options()

    # Build an Arrow table from the classified dicts via DuckDB so we get
    # deterministic typing + can stamp provenance columns.
    con = duckdb.connect()
    con.execute("SET threads=4")

    import pyarrow as pa

    # Multi-value columns (top_debtor_states/cities) arrive from lenders_lance
    # as pipe-delimited VARCHAR already — keep them VARCHAR (L54: never
    # LIST<VARCHAR>). Coerce any list-typed value defensively to a pipe string.
    def _pipe(v) -> str | None:
        if v is None:
            return None
        if isinstance(v, (list, tuple)):
            return "|".join(str(x) for x in v if x is not None) or None
        return str(v)

    records = []
    for r in classified:
        records.append({
            "lender_name_normalized": r.get("lender_name_normalized"),
            "total_filings": r.get("total_filings"),
            "active_filings": r.get("active_filings"),
            "first_filing_date": _pipe(r.get("first_filing_date")),
            "last_filing_date": _pipe(r.get("last_filing_date")),
            "top_debtor_states": _pipe(r.get("top_debtor_states")),
            "top_debtor_cities": _pipe(r.get("top_debtor_cities")),
            "bank_classification": _pipe(r.get("bank_classification")),
            "category_inferred_from_name": _pipe(
                r.get("category_inferred_from_name")
            ),
            "address_sample": _pipe(r.get("address_sample")),
            "pdl_name": r.get("pdl_name"),
            "pdl_website": r.get("pdl_website"),
            "pdl_industry": r.get("pdl_industry"),
            "pdl_size": _pipe(r.get("pdl_size")),
            "pdl_linkedin_url": r.get("pdl_linkedin_url"),
            "pdl_locality": r.get("pdl_locality"),
            "has_pdl_website": bool(
                r.get("pdl_website") and str(r["pdl_website"]).strip()
            ),
            # ---- classification columns ----
            "is_lender": r.get("is_lender"),
            "subtype": r.get("subtype"),
            "equipment_finance_confidence": float(
                r.get("equipment_finance_confidence", 0.0)
            ),
            "fetch_status": r.get("fetch_status"),
            "classification_method": r.get("classification_method"),
            "rationale": r.get("rationale"),
            # ---- provenance ----
            "classification_model": ANTHROPIC_MODEL,
            "emit_version": EMIT_VERSION,
            "run_id": run_id,
        })

    # Explicit Arrow schema — typed identity/numeric columns, VARCHAR rest.
    schema = pa.schema([
        ("lender_name_normalized", pa.string()),
        ("total_filings", pa.int64()),
        ("active_filings", pa.int64()),
        ("first_filing_date", pa.string()),
        ("last_filing_date", pa.string()),
        ("top_debtor_states", pa.string()),
        ("top_debtor_cities", pa.string()),
        ("bank_classification", pa.string()),
        ("category_inferred_from_name", pa.string()),
        ("address_sample", pa.string()),
        ("pdl_name", pa.string()),
        ("pdl_website", pa.string()),
        ("pdl_industry", pa.string()),
        ("pdl_size", pa.string()),
        ("pdl_linkedin_url", pa.string()),
        ("pdl_locality", pa.string()),
        ("has_pdl_website", pa.bool_()),
        ("is_lender", pa.string()),
        ("subtype", pa.string()),
        ("equipment_finance_confidence", pa.float64()),
        ("fetch_status", pa.string()),
        ("classification_method", pa.string()),
        ("rationale", pa.string()),
        ("classification_model", pa.string()),
        ("emit_version", pa.string()),
        ("run_id", pa.string()),
    ])
    table = pa.Table.from_pylist(records, schema=schema)

    if enforce_floor and table.num_rows < MIN_ROW_FLOOR:
        raise RuntimeError(
            f"HARD FAIL: classified rows ({table.num_rows}) < "
            f"floor ({MIN_ROW_FLOOR})"
        )

    with lance_commit_lock(DATASET_SLUG):
        logger.info("writing Lance dataset to %s ...", OUTPUT_LANCE_URI)
        ds = lance.write_dataset(
            table,
            OUTPUT_LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
            max_rows_per_file=10_000,
        )
        rows = ds.count_rows()
        logger.info("Lance written: %d rows (version %s)", rows, ds.version)
        if enforce_floor and rows < MIN_ROW_FLOOR:
            raise RuntimeError(
                f"HARD FAIL: rows ({rows}) < floor ({MIN_ROW_FLOOR})"
            )

        logger.info("BTREE on %s ...", LENDER_KEY)
        ds.create_scalar_index(LENDER_KEY, index_type="BTREE", replace=True)
        logger.info("BTREE on %s OK", LENDER_KEY)

        try:
            ds.optimize.compact_files()
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as exc:  # noqa: BLE001
            logger.warning("optimize failed (non-fatal): %s", exc)

    # --- emit-time distribution recon (the operator's deliverable) ---
    con.register("emitted", table)
    logger.info("=== classification distribution by subtype ===")
    for subtype, n in con.execute(
        "SELECT subtype, count(*) AS n FROM emitted "
        "GROUP BY subtype ORDER BY n DESC"
    ).fetchall():
        logger.info("  %-44s %5d", subtype, n)

    smoke = con.execute(
        f"""
        SELECT count(*) FROM emitted
        WHERE subtype = '{TARGET_SUBTYPE}'
          AND classification_method <> 'metadata_only'
        """
    ).fetchone()[0]
    logger.info(
        "SMOKE GATE: %d website-classified '%s' rows (floor 60)",
        smoke, TARGET_SUBTYPE,
    )
    logger.info("=== fetch_status distribution ===")
    for fs, n in con.execute(
        "SELECT fetch_status, count(*) AS n FROM emitted "
        "GROUP BY fetch_status ORDER BY n DESC"
    ).fetchall():
        logger.info("  %-20s %5d", fs, n)

    logger.info("cohort emit complete — rows=%d uri=%s", rows, OUTPUT_LANCE_URI)

    _register_polaris(
        DATASET_SLUG,
        "ucc_ca.lender_classification_lance — LLM website-classification of "
        "California UCC secured parties (lenders), volume-ranked spine "
        "(total_filings >= 100). One row per lender. Carries is_lender, "
        "subtype, equipment_finance_confidence, fetch_status, "
        "classification_method, rationale. Operator filters subtype="
        f"'{TARGET_SUBTYPE}' for independent equipment-finance lenders. "
        "Pattern A enriched-cohort emit per DATA-FACTORY-ARCHITECTURE-PATTERNS.md.",
    )
    return rows


# ── driver ──────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="CA UCC lender LLM website-classification → Lance emit"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Run the LLM batch and write the Lance dataset "
             "(default: dry-run — build the spine + print counts only).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Classify only the first N spine lenders (smoke test). "
             "Still emits a Lance dataset.",
    )
    args = parser.parse_args()

    storage_options = _storage_options()
    spine = build_spine(storage_options, args.limit)

    if not args.apply:
        logger.info(
            "DRY-RUN: %d lenders would be classified (pass --apply to emit). "
            "Subtype enum (%d values): %s",
            len(spine), len(SUBTYPE_ENUM), ", ".join(SUBTYPE_ENUM),
        )
        return 0

    classified = asyncio.run(classify_batch(spine))
    emit(spine, classified, enforce_floor=(args.limit is None))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""s4 - Operator-local Glassdoor resolution one-shot.

Resolves 10 hardcoded AE-tech-co names to Glassdoor company_ids via
/company-search. Best-match heuristic per directive:
  (1) exact lowercase name match (highest priority)
  (2) presence in PDL via domain match against
      pdl.free_companies_lance.pdl_website (normalized)
  (3) salary_count × review_count tiebreak (descending)

Writes resolution decisions to /tmp/glassdoor-resolved-cohort.json for the
hydration one-shot to consume.

Burns ~10 OpenWebNinja credits (1 per query × 10 queries).

Run via:
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      uv run python scripts/run_glassdoor_resolution_oneshot.py
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

# Ensure repo root (apps/data-engine-x) is on sys.path so `import app.*` works
# when invoked as `uv run python scripts/run_glassdoor_resolution_oneshot.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.glassdoor_ingest import run_company_search  # noqa: E402


NAMES: list[str] = [
    "Salesforce", "HubSpot", "Snowflake", "Databricks", "Gong",
    "Outreach", "Datadog", "MongoDB", "Atlassian", "Zendesk",
]

OUTPUT_PATH = "/tmp/glassdoor-resolved-cohort.json"
PDL_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/pdl/free_companies_lance"
)

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO, stream=sys.stdout,
)


def _normalize_domain(raw: str | None) -> str | None:
    if not raw:
        return None
    s = raw.strip().lower()
    if s.startswith("https://"):
        s = s[8:]
    elif s.startswith("http://"):
        s = s[7:]
    if s.startswith("www."):
        s = s[4:]
    if "/" in s:
        s = s.split("/", 1)[0]
    return s or None


def _load_pdl_websites() -> set[str]:
    """Probe PDL Lance for normalized website set. Bounded by the dataset
    columns BTREE; we read just pdl_website + scan.
    """
    try:
        import lance
    except ImportError:
        logger.warning("pylance not installed locally — skipping PDL domain match")
        return set()
    storage_options = {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
    }
    try:
        ds = lance.dataset(PDL_LANCE_URI, storage_options=storage_options)
        tbl = ds.scanner(columns=["pdl_website"]).to_table()
        websites: set[str] = set()
        col = tbl.column("pdl_website").to_pylist()
        for w in col:
            nd = _normalize_domain(w)
            if nd:
                websites.add(nd)
        logger.info("loaded %d normalized PDL websites", len(websites))
        return websites
    except Exception as exc:
        logger.warning("PDL Lance probe failed (%s) — skipping PDL domain rank",
                       exc)
        return set()


def _load_search_hits_from_db(
    db_url: str, input_query: str,
) -> list[dict[str, Any]]:
    import psycopg
    with psycopg.connect(db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT glassdoor_company_id, name, website, salary_count, "
            "       review_count, position_in_results "
            "FROM entities.source_glassdoor_company_search "
            "WHERE input_query_normalized = %s "
            "ORDER BY position_in_results",
            (input_query.strip().lower(),),
        )
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _rank_hits(
    hits: list[dict[str, Any]], name: str, pdl_websites: set[str],
) -> dict[str, Any] | None:
    """Apply 3-tier ranking heuristic; return the best hit or None."""
    if not hits:
        return None
    target_lower = name.lower()
    # filter to hits with non-null website (per directive)
    filtered = [h for h in hits if h.get("website")]
    if not filtered:
        filtered = hits  # fallback — pick from full set if all websites null

    def _key(h: dict[str, Any]) -> tuple:
        nm = (h.get("name") or "").lower()
        exact = 0 if nm == target_lower else 1
        nd = _normalize_domain(h.get("website"))
        pdl_match = 0 if nd in pdl_websites else 1
        salary_count = int(h.get("salary_count") or 0)
        review_count = int(h.get("review_count") or 0)
        tiebreak = -(salary_count * review_count)
        position = int(h.get("position_in_results") or 999)
        return (exact, pdl_match, tiebreak, position)

    return sorted(filtered, key=_key)[0]


def main() -> None:
    api_key = os.environ["OPENWEBNINJA_API_KEY"]
    db_url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ["DEX_DB_URL_POOLED"]

    logger.info("loading PDL website set for ranking ...")
    pdl_websites = _load_pdl_websites()

    resolved: list[dict[str, Any]] = []
    total_credits = 0
    for idx, name in enumerate(NAMES, start=1):
        logger.info("[%d/%d] resolving %r ...", idx, len(NAMES), name)
        try:
            result = run_company_search(
                query=name, limit=10, api_key=api_key, db_url=db_url,
            )
        except Exception as exc:
            logger.exception("run_company_search failed for %r: %s", name, exc)
            resolved.append({"input_name": name, "error": str(exc)})
            continue
        total_credits += int(result.get("credits_used") or 0)
        if result.get("status") != "completed":
            logger.warning("non-success for %r: %s", name, result)
            resolved.append({
                "input_name": name, "error": result.get("error"),
                "status": result.get("status"),
            })
            continue
        # load rows back from DB and rank
        hits = _load_search_hits_from_db(db_url, name)
        best = _rank_hits(hits, name, pdl_websites)
        if best is None:
            logger.warning("no hits for %r — skipping resolution", name)
            resolved.append({"input_name": name, "error": "no_hits"})
            continue
        decision = {
            "input_name": name,
            "glassdoor_company_id": int(best["glassdoor_company_id"]),
            "name": best.get("name"),
            "website": best.get("website"),
            "salary_count": best.get("salary_count"),
            "review_count": best.get("review_count"),
            "position_in_results": best.get("position_in_results"),
            "rows_returned": len(hits),
        }
        logger.info("  -> %s (company_id=%s)", decision["name"],
                    decision["glassdoor_company_id"])
        resolved.append(decision)
        # politeness between queries
        time.sleep(1.5)

    with open(OUTPUT_PATH, "w") as fh:
        json.dump(resolved, fh, indent=2)
    logger.info("wrote %d resolution rows to %s", len(resolved), OUTPUT_PATH)
    logger.info("total credits burned: %d", total_credits)
    logger.info("decisions: %s", json.dumps(resolved, indent=2, default=str))


if __name__ == "__main__":
    main()

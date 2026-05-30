"""s4 - Operator-local Glassdoor hydration one-shot.

For each resolved company_id in /tmp/glassdoor-resolved-cohort.json (or
queried from entities.source_glassdoor_company_search if the JSON is missing),
calls 3 endpoints:
    /company-overview
    /company-salaries (job_title="Account Executive", location_type="ANY")
    /company-salaries-v2 (page=1)

Burns ~30 OpenWebNinja credits (3 endpoints × ~10 resolved companies).
1.5s sleep between calls (politeness).

Run via:
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      uv run python scripts/run_glassdoor_hydration_oneshot.py
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

# Ensure repo root (apps/data-engine-x) is on sys.path so `import app.*` works
# when invoked as `uv run python scripts/run_glassdoor_hydration_oneshot.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.glassdoor_ingest import (  # noqa: E402
    run_company_overview, run_company_salaries, run_company_salaries_v2,
)

RESOLVED_PATH = "/tmp/glassdoor-resolved-cohort.json"

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO, stream=sys.stdout,
)


def _load_resolved(db_url: str) -> list[dict]:
    if os.path.exists(RESOLVED_PATH):
        with open(RESOLVED_PATH) as fh:
            data = json.load(fh)
        out = [
            r for r in data
            if isinstance(r, dict) and r.get("glassdoor_company_id")
        ]
        logger.info("loaded %d resolved rows from %s", len(out), RESOLVED_PATH)
        return out
    # Fallback: query DB for any rows that have a glassdoor_company_id.
    logger.info("%s missing — falling back to DB query", RESOLVED_PATH)
    import psycopg
    with psycopg.connect(db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT ON (glassdoor_company_id) "
            "  glassdoor_company_id, name "
            "FROM entities.source_glassdoor_company_search "
            "WHERE position_in_results = 1 "
            "ORDER BY glassdoor_company_id, ingested_at DESC"
        )
        return [
            {"glassdoor_company_id": int(r[0]), "name": r[1]}
            for r in cur.fetchall()
        ]


def main() -> None:
    api_key = os.environ["OPENWEBNINJA_API_KEY"]
    db_url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ["DEX_DB_URL_POOLED"]

    resolved = _load_resolved(db_url)
    if not resolved:
        logger.error("no resolved rows — run run_glassdoor_resolution_oneshot.py first")
        sys.exit(1)

    summary: list[dict] = []
    total_credits = 0
    for idx, entry in enumerate(resolved, start=1):
        company_id = int(entry["glassdoor_company_id"])
        name = entry.get("name") or entry.get("input_name") or "?"
        logger.info("[%d/%d] hydrating %s (company_id=%s) ...",
                    idx, len(resolved), name, company_id)

        per_company = {"input_name": name, "glassdoor_company_id": company_id}

        try:
            r_overview = run_company_overview(
                company_id=company_id, api_key=api_key, db_url=db_url,
            )
            total_credits += int(r_overview.get("credits_used") or 0)
            per_company["overview"] = {
                "status": r_overview.get("status"),
                "rows_upserted": r_overview.get("rows_upserted"),
                "error_class": r_overview.get("error_class"),
            }
        except Exception as exc:
            logger.exception("/company-overview failed for %s: %s", company_id, exc)
            per_company["overview"] = {"error": str(exc)}
        time.sleep(1.5)

        try:
            r_salaries = run_company_salaries(
                company_id=company_id, job_title="Account Executive",
                location_type="ANY",
                api_key=api_key, db_url=db_url,
            )
            total_credits += int(r_salaries.get("credits_used") or 0)
            per_company["salaries_ae"] = {
                "status": r_salaries.get("status"),
                "rows_upserted": r_salaries.get("rows_upserted"),
                "error_class": r_salaries.get("error_class"),
            }
        except Exception as exc:
            logger.exception("/company-salaries failed for %s: %s", company_id, exc)
            per_company["salaries_ae"] = {"error": str(exc)}
        time.sleep(1.5)

        try:
            r_v2 = run_company_salaries_v2(
                company_id=company_id, page=1,
                api_key=api_key, db_url=db_url,
            )
            total_credits += int(r_v2.get("credits_used") or 0)
            per_company["salaries_v2"] = {
                "status": r_v2.get("status"),
                "rows_seen": r_v2.get("rows_seen"),
                "rows_upserted": r_v2.get("rows_upserted"),
                "error_class": r_v2.get("error_class"),
            }
        except Exception as exc:
            logger.exception("/company-salaries-v2 failed for %s: %s",
                             company_id, exc)
            per_company["salaries_v2"] = {"error": str(exc)}
        time.sleep(1.5)

        summary.append(per_company)
        logger.info("  -> %s", json.dumps(per_company, default=str))

    logger.info("total credits burned: %d", total_credits)
    logger.info("hydration summary: %s",
                json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()

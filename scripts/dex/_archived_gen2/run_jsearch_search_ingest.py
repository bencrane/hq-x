#!/usr/bin/env python3
"""JSearch /v2/search ingest — CLI shim.

Ingest logic lives in `app.services.jsearch_ingest`; this file is the
thin argparse wrapper for local + Trigger.dev/Modal invocation.

Usage:
    PYTHONPATH=. doppler run -- python3 scripts/run_jsearch_search_ingest.py \\
        --query "trucking dispatcher dallas" --num-pages 1
    PYTHONPATH=. doppler run -- python3 scripts/run_jsearch_search_ingest.py \\
        --query "developer in chicago" --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from app.services.jsearch_ingest import NUM_PAGES_MAX, run_ingest


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("jsearch_search_ingest")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="JSearch /v2/search ingest into entities.source_jsearch_search.",
    )
    parser.add_argument("--query", required=True,
                        help="Free-form search query. Include job title + location, "
                             "e.g. 'trucking dispatcher dallas'.")
    parser.add_argument("--num-pages", type=int, default=1,
                        help=f"1-{NUM_PAGES_MAX}, default 1. Each page = 10 jobs = 1 credit.")
    parser.add_argument("--page", type=int, default=1,
                        help="Starting page (1-100). Default 1.")
    parser.add_argument("--country", default="us",
                        help="ISO 3166-1 alpha-2. Default 'us'.")
    parser.add_argument("--language", default=None,
                        help="ISO 639. Defaults to country's primary language.")
    parser.add_argument("--date-posted", default=None,
                        choices=("all", "today", "3days", "week", "month"),
                        help="Recency filter.")
    parser.add_argument("--work-from-home", action="store_true",
                        help="Only return remote jobs.")
    parser.add_argument("--employment-types", default=None,
                        help="CSV of FULLTIME, CONTRACTOR, PARTTIME, INTERN.")
    parser.add_argument("--job-requirements", default=None,
                        help="CSV of under_3_years_experience, "
                             "more_than_3_years_experience, no_experience, no_degree.")
    parser.add_argument("--radius", type=float, default=None,
                        help="Distance from query location, in km.")
    parser.add_argument("--exclude-job-publishers", default=None,
                        help="CSV of publisher names to exclude.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch + parse only; no DB writes.")
    args = parser.parse_args()

    api_key = os.environ.get("OPENWEBNINJA_API_KEY")
    if not api_key:
        log.error("OPENWEBNINJA_API_KEY env var must be set "
                  "(stored in Doppler hq-all/prd).")
        return 2

    db_url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ.get("DEX_DB_URL_POOLED")
    if not args.dry_run and not db_url:
        log.error("DEX_DB_URL_DIRECT or DEX_DB_URL_POOLED must be set "
                  "(or pass --dry-run).")
        return 2

    try:
        result = run_ingest(
            query=args.query,
            api_key=api_key,
            num_pages=args.num_pages,
            page=args.page,
            country=args.country,
            language=args.language,
            date_posted=args.date_posted,
            work_from_home=args.work_from_home,
            employment_types=args.employment_types,
            job_requirements=args.job_requirements,
            radius=args.radius,
            exclude_job_publishers=args.exclude_job_publishers,
            db_url=db_url,
            dry_run=args.dry_run,
            task_id=os.environ.get("TRIGGER_TASK_ID"),
            schedule_id=os.environ.get("TRIGGER_SCHEDULE_ID"),
        )
    except ValueError as exc:
        log.error("%s", exc)
        return 2

    log.info("done. status=%s rows_seen=%d rows_upserted=%d credits_used=%d",
             result["status"], result["rows_seen"],
             result["rows_upserted"], result["credits_used"])
    return 0 if result["status"] == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())

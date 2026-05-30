"""15-query AE catalog one-shot ingest via existing JSearch service.

Run via:
    cd apps/data-engine-x && doppler run --project hq-all --config prd -- \\
        uv run python scripts/run_jsearch_ae_catalog_oneshot.py

NOT Modal-hosted; runs locally. Burns ~320 OpenWebNinja credits.
Expected: ~3000 jobs gross, ~1500-2500 unique post-job_id-dedup.
Idempotent (entities.source_jsearch_search PK job_id; ON CONFLICT DO UPDATE
WHERE row IS DISTINCT FROM EXCLUDED).
"""
from __future__ import annotations
import os, sys, time, json
from app.services.jsearch_ingest import run_ingest

QUERIES = [
    # 6 title variants
    "Account Executive",
    "Enterprise Account Executive",
    "Mid-Market Account Executive",
    "SMB Account Executive",
    "Strategic Account Executive",
    "Senior Account Executive",
    # 10 geo cuts on baseline "Account Executive"
    "Account Executive New York",
    "Account Executive San Francisco",
    "Account Executive Austin",
    "Account Executive Boston",
    "Account Executive Seattle",
    "Account Executive Los Angeles",
    "Account Executive Chicago",
    "Account Executive Denver",
    "Account Executive Atlanta",
    "Account Executive Washington DC",
]


def main():
    api_key = os.environ["OPENWEBNINJA_API_KEY"]
    db_url = os.environ["DEX_DB_URL_DIRECT"]
    aggregate = {
        "queries": 0, "credits_total": 0,
        "rows_seen_total": 0, "rows_upserted_total": 0, "per_query": [],
    }
    for q in QUERIES:
        try:
            res = run_ingest(
                query=q, api_key=api_key,
                num_pages=20, country="us", db_url=db_url,
            )
            aggregate["queries"] += 1
            aggregate["credits_total"] += res["credits_used"]
            aggregate["rows_seen_total"] += res["rows_seen"]
            aggregate["rows_upserted_total"] += res["rows_upserted"]
            aggregate["per_query"].append({
                "query": q,
                **{k: res[k] for k in ("status", "rows_seen", "rows_upserted",
                                       "credits_used", "run_id", "error")},
            })
            print(json.dumps({
                "query": q, "status": res["status"],
                "rows_seen": res["rows_seen"],
                "rows_upserted": res["rows_upserted"],
                "credits": res["credits_used"],
                "run_id": res.get("run_id"),
            }))
        except Exception as e:
            print(json.dumps({
                "query": q, "status": "exception",
                "error": f"{type(e).__name__}: {e}",
            }), file=sys.stderr)
        time.sleep(2)  # rate-limit cushion
    print(json.dumps({"aggregate": aggregate}, indent=2, default=str))


if __name__ == "__main__":
    main()

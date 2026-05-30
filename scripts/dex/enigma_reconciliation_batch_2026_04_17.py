"""Enigma reconciliation batch — Commit 2 verification.

Directive: docs/EXECUTOR_DIRECTIVE_ENIGMA_CREDIT_ACCOUNTING_COMMIT_2.md §5.9

GOAL
----
Fire 3 varied live Enigma calls (Free / Core / Premium) using the Commit 2
two-stage cost model (pre-flight reserve from catalog.max_cost_declared,
post-response actualization via walker). Log each call's `credits_declared`
vs `credits_charged` so the human operator can compare the batch's
sum(credits_charged) to the Enigma dashboard delta.

SUBJECTS
--------
- Call 1: SEARCH_BRAND_QUERY against "McDonald's", first=5.
          Catalog declared=10 (pessimistic, 10 Brands × Core). Walker
          actual = (# Brand matches returned) × 1.
- Call 2: OPERATING_LOCATION_ENRICH_QUERY against the McDonald's OL UUID
          6b4c630b-29e9-40fd-af1f-b0d51d4db368 (used by Commit 1 probe).
          force_refresh=True. Declared=1, expected actual=1.
- Call 3: ENRICH_LEGAL_ENTITY_QUERY against Walmart LE UUID
          68691638-2e0d-4613-0000-10c000000000 (Phase 3 seed).
          force_refresh=True. Declared=5, expected actual=5.

BUDGET
------
Expected total credits_charged: ~11 (5 + 1 + 5 assuming 5 Brand matches).
Directive expected total: 6 (assuming SEARCH is Free tier). Hard cap: 12.

RUN
---
    doppler run -- bash -c 'source .venv/bin/activate && python3 \\
        scripts/enigma_reconciliation_batch_2026_04_17.py'

Before running, capture the Enigma dashboard "credits used today" number.
After the script prints its summary table, capture the dashboard number
again. dashboard_delta should equal sum(credits_charged) within ±1.
"""
from __future__ import annotations

import asyncio
import os
import sys

from psycopg_pool import ConnectionPool

from app.providers.enigma_adapter import (
    ENRICH_LEGAL_ENTITY_QUERY,
    EnigmaAdapter,
    OPERATING_LOCATION_ENRICH_QUERY,
    SEARCH_BRAND_QUERY,
)
from app.providers.enigma_adapter.errors import AdapterError, CreditCapExceeded
from app.providers.enigma_adapter.query_catalog import CATALOG, clamp_variables


MCDONALDS_OL_UUID = "6b4c630b-29e9-40fd-af1f-b0d51d4db368"
WALMART_LE_UUID = "68691638-2e0d-4613-0000-10c000000000"
SOURCE_OPERATION_ID = "ReconciliationBatch.2026_04_17"


def _row(call_idx: int, query_name: str, subject: str, declared: int, charged: int) -> str:
    ok = "OK" if charged <= declared else "OVER"
    return (
        f"| {call_idx} | {query_name:40s} | {subject:40s} "
        f"| {declared:>3} | {charged:>3} | {ok} |"
    )


async def _main() -> int:
    api_key = os.getenv("ENIGMA_API_KEY")
    database_url = os.getenv("DEX_DB_URL_POOLED")
    if not api_key or not database_url:
        print("Missing ENIGMA_API_KEY or DEX_DB_URL_POOLED — run under Doppler.", file=sys.stderr)
        return 2

    pool = ConnectionPool(conninfo=database_url, min_size=1, max_size=2, timeout=30.0)
    results: list[tuple[int, str, str, int, int]] = []
    try:
        adapter = EnigmaAdapter(api_key=api_key, db_pool=pool, hard_cap=12)

        # ------------------------------------------------------------------
        # Call 1: SEARCH_BRAND_QUERY "McDonald's" first=5
        # ------------------------------------------------------------------
        spec1 = CATALOG["SEARCH_BRAND_QUERY"]
        variables1 = clamp_variables(
            "SEARCH_BRAND_QUERY",
            {"searchInput": {"entityType": "BRAND", "name": "McDonald's"}},
        )
        print(f"[batch][1] SEARCH_BRAND_QUERY 'McDonald's' declared={spec1.max_cost_declared}")
        r1 = await adapter.execute(
            operation_name="SearchBrand",
            query=SEARCH_BRAND_QUERY,
            variables=variables1,
            subject_kind="search",
            subject_enigma_uuid=None,
            required_tier=1,
            source_operation_id=SOURCE_OPERATION_ID,
        )
        print(f"[batch][1] status={r1.status} charged={r1.credits_charged} log_id={r1.log_id}")
        results.append((1, "SEARCH_BRAND_QUERY", "McDonald's first=5",
                        spec1.max_cost_declared, int(r1.credits_charged)))

        # ------------------------------------------------------------------
        # Call 2: OPERATING_LOCATION_ENRICH_QUERY McDonald's OL force_refresh
        # ------------------------------------------------------------------
        spec2 = CATALOG["OPERATING_LOCATION_ENRICH_QUERY"]
        variables2 = clamp_variables(
            "OPERATING_LOCATION_ENRICH_QUERY",
            {"searchInput": {"entityType": "OPERATING_LOCATION", "id": MCDONALDS_OL_UUID}},
        )
        print(f"[batch][2] OL_ENRICH {MCDONALDS_OL_UUID} declared={spec2.max_cost_declared}")
        r2 = await adapter.execute(
            operation_name="EnrichOperatingLocation",
            query=OPERATING_LOCATION_ENRICH_QUERY,
            variables=variables2,
            subject_kind="operating_location",
            subject_enigma_uuid=MCDONALDS_OL_UUID,
            required_tier=1,
            source_operation_id=SOURCE_OPERATION_ID,
            force_refresh=True,
        )
        print(f"[batch][2] status={r2.status} charged={r2.credits_charged} log_id={r2.log_id}")
        results.append((2, "OPERATING_LOCATION_ENRICH_QUERY", MCDONALDS_OL_UUID,
                        spec2.max_cost_declared, int(r2.credits_charged)))

        # ------------------------------------------------------------------
        # Call 3: ENRICH_LEGAL_ENTITY_QUERY Walmart force_refresh
        # ------------------------------------------------------------------
        spec3 = CATALOG["ENRICH_LEGAL_ENTITY_QUERY"]
        variables3 = clamp_variables(
            "ENRICH_LEGAL_ENTITY_QUERY",
            {"searchInput": {"entityType": "LEGAL_ENTITY", "id": WALMART_LE_UUID}},
        )
        print(f"[batch][3] LE_ENRICH {WALMART_LE_UUID} declared={spec3.max_cost_declared}")
        r3 = await adapter.execute(
            operation_name="EnrichLegalEntity",
            query=ENRICH_LEGAL_ENTITY_QUERY,
            variables=variables3,
            subject_kind="legal_entity",
            subject_enigma_uuid=WALMART_LE_UUID,
            required_tier=5,
            source_operation_id=SOURCE_OPERATION_ID,
            force_refresh=True,
        )
        print(f"[batch][3] status={r3.status} charged={r3.credits_charged} log_id={r3.log_id}")
        results.append((3, "ENRICH_LEGAL_ENTITY_QUERY", WALMART_LE_UUID,
                        spec3.max_cost_declared, int(r3.credits_charged)))

        # ------------------------------------------------------------------
        # Summary
        # ------------------------------------------------------------------
        total_declared = sum(d for _, _, _, d, _ in results)
        total_charged = sum(c for _, _, _, _, c in results)
        print("\n[batch] SUMMARY")
        print(f"| # | Query                                    | Subject                                  | Dec | Chg | OK |")
        print(f"|---|------------------------------------------|------------------------------------------|-----|-----|----|")
        for row in results:
            print(_row(*row))
        print(f"\n[batch] total declared: {total_declared}")
        print(f"[batch] total charged:  {total_charged}")
        print(f"[batch] credits_spent_this_run: {adapter.credits_spent_this_run}")
        return 0
    except CreditCapExceeded as e:
        print(f"[batch] CreditCapExceeded: {e}", file=sys.stderr)
        return 2
    except AdapterError as e:
        print(f"[batch] AdapterError: {e}", file=sys.stderr)
        return 2
    finally:
        pool.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))

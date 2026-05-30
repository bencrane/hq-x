"""Narrow SEARCH_BRAND probe — Hypothesis 1 confirmation.

Fires ONE live call: SEARCH_BRAND_QUERY with conditions.limit=1 + first=5,
force_refresh=True, hard_cap=2. Captures walker-computed credits_charged.
Dashboard delta is captured manually by the human operator.

Hypothesis 1: Enigma's SEARCH_BRAND_QUERY bills per-EVALUATED candidate
(governed by conditions.limit), not per-RETURNED match. A limit=1 call
should bill exactly 1 credit. If dashboard delta = 1, Hypothesis 1 is
CONFIRMED and the catalog needs a billing_mode="per_evaluated" field.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

from psycopg_pool import ConnectionPool

from app.providers.enigma_adapter import EnigmaAdapter, SEARCH_BRAND_QUERY
from app.providers.enigma_adapter.errors import AdapterError, CreditCapExceeded
from app.providers.enigma_adapter.query_catalog import CATALOG, clamp_variables


SOURCE_OPERATION_ID = "ReconciliationProbe.2026_04_17.search_brand_limit1"


async def _main() -> int:
    api_key = os.getenv("ENIGMA_API_KEY")
    database_url = os.getenv("DEX_DB_URL_POOLED")
    if not api_key or not database_url:
        print("Missing ENIGMA_API_KEY or DEX_DB_URL_POOLED — run under Doppler.", file=sys.stderr)
        return 2

    pool = ConnectionPool(conninfo=database_url, min_size=1, max_size=2, timeout=30.0)
    try:
        # hard_cap=10 to let the pre-flight reservation pass (catalog's
        # max_cost_declared=10 for SEARCH_BRAND_QUERY). Actual billed credit
        # is what matters — if Hypothesis 1 is right, we expect ~1.
        adapter = EnigmaAdapter(api_key=api_key, db_pool=pool, hard_cap=10)
        spec = CATALOG["SEARCH_BRAND_QUERY"]

        # SearchInput with explicit conditions.limit=1 — the narrow-probe shape.
        variables = clamp_variables(
            "SEARCH_BRAND_QUERY",
            {
                "searchInput": {
                    "entityType": "BRAND",
                    "name": "McDonald's",
                    "conditions": {"limit": 1},
                }
            },
        )
        print(f"[probe] SEARCH_BRAND_QUERY name=McDonald's conditions.limit=1 declared={spec.max_cost_declared}")
        print(f"[probe] force_refresh=True hard_cap=2")

        result = await adapter.execute(
            operation_name="SearchBrand",
            query=SEARCH_BRAND_QUERY,
            variables=variables,
            subject_kind="search",
            subject_enigma_uuid=None,
            required_tier=1,
            source_operation_id=SOURCE_OPERATION_ID,
            force_refresh=True,
        )
        print(f"[probe] status={result.status} log_id={result.log_id}")
        print(f"[probe] credits_charged (walker)={result.credits_charged}")
        print(f"[probe] credits_spent_this_run={adapter.credits_spent_this_run}")

        # Show response body shape so we can cross-check __typename count.
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT raw_payload_ref FROM entities.enigma_enrichment_log WHERE id = %s",
                (result.log_id,),
            )
            ref = cur.fetchone()[0]
        if ref and ref.startswith("file://"):
            path = ref[len("file://"):]
            with open(path) as f:
                body = json.load(f)
            search = body.get("data", {}).get("search", [])
            print(f"[probe] response.data.search length: {len(search)}")
            print(f"[probe] response body: {json.dumps(body, indent=2)[:2000]}")
        return 0
    except CreditCapExceeded as e:
        print(f"[probe] CreditCapExceeded: {e}", file=sys.stderr)
        return 2
    except AdapterError as e:
        print(f"[probe] AdapterError: {e}", file=sys.stderr)
        return 2
    finally:
        pool.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))

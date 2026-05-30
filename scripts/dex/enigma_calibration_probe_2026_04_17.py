"""Enigma calibration probe — Commit 1 credit-accounting diagnostic.

Directive: docs/EXECUTOR_DIRECTIVE_ENIGMA_CREDIT_ACCOUNTING_COMMIT_1.md §5.7

GOAL
----
Fire exactly ONE live Enigma query with `force_refresh=True` against an
already-cached subject, persist the response envelope (new migration 083
column), and print the envelope verbatim so Commit 2's scope can be
decided against real response shape — not speculation.

SUBJECT
-------
McDonald's OperatingLocation `6b4c630b-29e9-40fd-af1f-b0d51d4db368`
(cached by Phase 1's live harness). Already has a success row in
enigma_enrichment_log at tier 1, so force_refresh=True is the ONLY way
to re-exercise the billing path without blowing the cap on a new subject.

COST ENVELOPE
-------------
OPERATING_LOCATION_ENRICH_QUERY is Core tier = 1 credit per call (per
catalog: max_tier=1, max_cost_declared=1). The directive authorizes ≤3
credits for this probe — we budget at 3 as a ceiling and expect 1 actual.

SECRET HANDLING
---------------
- ENIGMA_API_KEY from Doppler. Run under:
    doppler run -- bash -c 'source .venv/bin/activate && python3 scripts/enigma_calibration_probe_2026_04_17.py'
- Envelope redaction (app/providers/enigma_adapter/adapter.py::_redact_response_headers)
  is the same allowlist the adapter uses in production, so persisted output
  is safe to commit to a docs/ file afterward.

EXIT CODE
---------
0 on success (envelope captured + log row written).
2 on CreditCapExceeded / AdapterError.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pprint import pformat

from psycopg_pool import ConnectionPool

from app.providers.enigma_adapter import EnigmaAdapter, OPERATING_LOCATION_ENRICH_QUERY
from app.providers.enigma_adapter.errors import AdapterError, CreditCapExceeded
from app.providers.enigma_adapter.query_catalog import clamp_variables


SUBJECT_UUID = "6b4c630b-29e9-40fd-af1f-b0d51d4db368"  # McDonald's OL
SOURCE_OPERATION_ID = "CalibrationProbe.2026_04_17.ol_enrich"


async def _main() -> int:
    api_key = os.getenv("ENIGMA_API_KEY")
    database_url = os.getenv("DEX_DB_URL_POOLED")
    if not api_key:
        print("ENIGMA_API_KEY missing — run under Doppler.", file=sys.stderr)
        return 2
    if not database_url:
        print("DEX_DB_URL_POOLED missing — run under Doppler.", file=sys.stderr)
        return 2

    pool = ConnectionPool(conninfo=database_url, min_size=1, max_size=2, timeout=30.0)
    try:
        adapter = EnigmaAdapter(
            api_key=api_key,
            db_pool=pool,
            hard_cap=3,  # directive §5.7 ceiling
        )
        variables = clamp_variables(
            "OPERATING_LOCATION_ENRICH_QUERY",
            {"searchInput": {"entityType": "OPERATING_LOCATION", "id": SUBJECT_UUID}},
        )
        print(f"[probe] operation=OPERATING_LOCATION_ENRICH subject={SUBJECT_UUID}")
        print(f"[probe] force_refresh=True hard_cap=3 estimated_credits=1")

        result = await adapter.execute(
            operation_name="EnrichOperatingLocation",
            query=OPERATING_LOCATION_ENRICH_QUERY,
            variables=variables,
            subject_kind="operating_location",
            subject_enigma_uuid=SUBJECT_UUID,
            required_tier=1,
            source_operation_id=SOURCE_OPERATION_ID,
            force_refresh=True,
        )

        print(f"[probe] status={result.status} log_id={result.log_id}")
        print(f"[probe] credits_charged={result.credits_charged} tier={result.tier_reached}")
        print(f"[probe] credits_spent_this_run={adapter.credits_spent_this_run}")

        # Fetch the persisted envelope row.
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT response_envelope FROM entities.enigma_enrichment_log "
                "WHERE id = %s",
                (result.log_id,),
            )
            row = cur.fetchone()
        envelope = row[0] if row else None

        print("\n[probe] response_envelope (verbatim, redaction applied):")
        print(json.dumps(envelope, indent=2, sort_keys=True, default=str))

        # A compact summary the human operator can paste into the result doc.
        print("\n[probe] SUMMARY FOR RESULT DOC:")
        print(f"  log_id:                   {result.log_id}")
        print(f"  credits_charged_adapter:  {result.credits_charged}")
        print(f"  envelope.status_code:     {envelope.get('status_code') if envelope else None}")
        print(f"  envelope.headers:         {pformat(envelope.get('headers') if envelope else None)}")
        print(f"  envelope.extensions:      {pformat(envelope.get('extensions') if envelope else None)}")

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

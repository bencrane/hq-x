"""Phase 5.1 — live Person-enrich verification harness.

Closes the verification gap left by Phase 5: ENRICH_PERSON_QUERY was shipped
with cassette tests only and `persist_person_payload` has never been run
against a real Enigma payload. This harness:

  1. Picks a Person UUID via SEARCH_PERSON_QUERY (Free/Core, 0-1 credit per try).
     Falls back through a small list of common-name probes; halts after 3
     credits of search with no UUID.
  2. Calls ENRICH_PERSON_QUERY against that UUID via the adapter (exercises
     adapter cache + enrichment-log code paths).
  3. Calls persist_person_payload on the response.
  4. Re-runs the enrich with force_refresh=False to prove cache replay (0 credits).

Hard cap: 15 adapter credits (directive §Step 3 — target spend 1 + 3 = 4).
Run with:
    doppler run --project data-engine-x-api --config dev -- \\
        python3 scripts/phase5_1_person_enrich_live.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

# Repo root on sys.path so `import app` resolves.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from psycopg_pool import ConnectionPool

from app.providers.enigma_adapter import EnigmaAdapter
from app.providers.enigma_adapter.errors import AdapterError
from app.providers.enigma_adapter.queries import (
    ENRICH_PERSON_QUERY,
    SEARCH_LEGAL_ENTITY_QUERY,
    SEARCH_PERSON_QUERY,
)
from app.services.enigma_persistence import persist_person_payload


# Common-name search probes. First-attempt order chosen to maximize hit
# probability in the Enigma graph (surname-weight distribution biases toward
# common last names + common first names). Stop after first hit.
SEARCH_PROBES: list[dict[str, str]] = [
    {"firstName": "Michael", "lastName": "Smith"},
    {"firstName": "Robert", "lastName": "Johnson"},
    {"firstName": "John", "lastName": "Smith"},
]


# Fallback probe — LegalEntity.persons on Walmart. Harness-only one-off
# (NOT a canonical query shipped to queries.py). Used when PersonInput-by-name
# yields no hits, to harvest a real Person UUID for the live enrich test.
# LegalEntity.persons is a Plus-tier connection (~3 credits).
WALMART_LEGAL_ENTITY_UUID = "68691638-2e0d-4613-0000-10c000000000"
LEGAL_ENTITY_PERSONS_PROBE = """
query ProbeLegalEntityPersons($searchInput: SearchInput!) {
  search(searchInput: $searchInput) {
    ... on LegalEntity {
      id
      persons(first: 5) {
        edges {
          node {
            id
            names(first: 1) {
              edges {
                node { firstName lastName fullName }
              }
            }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
""".strip()


def _banner(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}", flush=True)


async def _try_search_for_person_uuid(
    adapter: EnigmaAdapter,
    source_run_id: uuid.UUID,
) -> tuple[str | None, int]:
    """Return (person_uuid, credits_spent_on_search)."""
    credits_used = 0
    for probe in SEARCH_PROBES:
        _banner(f"SEARCH probe — {probe}")
        try:
            result = await adapter.execute(
                operation_name="SearchPerson",
                query=SEARCH_PERSON_QUERY,
                variables={
                    "searchInput": {
                        "entityType": "PERSON",
                        "person": probe,
                    }
                },
                subject_kind="search",
                subject_enigma_uuid=None,
                required_tier=1,
                source_operation_id="phase5_1.live.search",
                pipeline_run_id=None,
                submission_id=source_run_id,
            )
            credits_used += int(result.credits_charged or 0)
            hits = (result.payload or {}).get("search") or []
            print(f"status={result.status} credits={result.credits_charged} hits={len(hits)}")
            person_ids = [
                h.get("id") for h in hits
                if isinstance(h, dict) and h.get("id")
            ]
            if person_ids:
                chosen = person_ids[0]
                sample = hits[0]
                print(f"chosen Person UUID: {chosen}")
                print(f"sample node: {json.dumps(sample, default=str)[:400]}")
                return chosen, credits_used
            if credits_used >= 3:
                print(f"HALT — search credit budget (3) exhausted without a UUID")
                return None, credits_used
        except AdapterError as exc:
            print(f"search probe failed: {exc}")
    return None, credits_used


async def main() -> None:
    api_key = os.environ.get("ENIGMA_API_KEY")
    db_url = os.environ.get("DEX_DB_URL_POOLED")
    if not api_key:
        print("ENIGMA_API_KEY missing", file=sys.stderr)
        sys.exit(1)
    if not db_url:
        print("DEX_DB_URL_POOLED missing", file=sys.stderr)
        sys.exit(1)

    source_run_id = uuid.uuid4()
    pool = ConnectionPool(conninfo=db_url, min_size=1, max_size=2, timeout=30.0)
    try:
        adapter = EnigmaAdapter(api_key=api_key, db_pool=pool, hard_cap=15)

        # PersonInput-by-name SEARCH is known to return 0 hits for the common
        # names we can guess at (verified in a prior run; directive §Step 3
        # Option A "preferred" path exhausted). Skip directly to the LE-based
        # fallback to conserve the shared 20-credit cap.
        skip_search = os.environ.get("PHASE5_1_SKIP_SEARCH") == "1"
        if skip_search:
            print(
                "[skip] PersonInput-by-name SEARCH_PERSON probes (PHASE5_1_SKIP_SEARCH=1)"
            )
            person_uuid, search_credits = None, 0
        else:
            person_uuid, search_credits = await _try_search_for_person_uuid(
                adapter, source_run_id
            )
        fallback_credits = 0
        if not person_uuid:
            _banner(
                "FALLBACK — SEARCH_PERSON returned 0 hits across common names. "
                "Searching LegalEntity by name, then probing LegalEntity.persons."
            )
            # Try each candidate LE name until we find one with persons.
            # SEARCH_LEGAL_ENTITY_QUERY is Free tier (0 credits); persons probe
            # is Plus (~3 credits). Budget check before each persons probe.
            le_name_candidates = [
                "McDonald's Corporation",
                "Shake Shack Inc",
                "Chipotle Mexican Grill, Inc.",
                "Starbucks Corporation",
                "Target Corporation",
            ]
            for le_name in le_name_candidates:
                print(f"\n-- SEARCH LegalEntity name={le_name!r}")
                try:
                    le_search = await adapter.execute(
                        operation_name="SearchLegalEntity",
                        query=SEARCH_LEGAL_ENTITY_QUERY,
                        variables={
                            "searchInput": {
                                "entityType": "LEGAL_ENTITY",
                                "name": le_name,
                            }
                        },
                        subject_kind="search",
                        subject_enigma_uuid=None,
                        required_tier=1,
                        source_operation_id="phase5_1.live.search_le",
                        pipeline_run_id=None,
                        submission_id=source_run_id,
                    )
                    fallback_credits += int(le_search.credits_charged or 0)
                    le_hits = (le_search.payload or {}).get("search") or []
                    if not le_hits:
                        print(f"  no LE hits, credits={le_search.credits_charged}")
                        continue
                    le_id = le_hits[0].get("id")
                    print(f"  LE id: {le_id} credits={le_search.credits_charged}")
                    if not le_id:
                        continue
                    # Budget guard — 3 more credits for the persons probe.
                    if adapter.credits_spent_this_run + 3 > 14:
                        print(
                            f"  halting LE fallback — "
                            f"spent={adapter.credits_spent_this_run}, "
                            f"next probe would exceed 14"
                        )
                        break
                    print(f"  probing LegalEntity.persons on {le_id}")
                    probe = await adapter.execute(
                        operation_name="ProbeLegalEntityPersons",
                        query=LEGAL_ENTITY_PERSONS_PROBE,
                        variables={
                            "searchInput": {
                                "entityType": "LEGAL_ENTITY",
                                "id": le_id,
                            }
                        },
                        subject_kind="legal_entity",
                        subject_enigma_uuid=le_id,
                        required_tier=3,
                        source_operation_id="phase5_1.live.probe_persons",
                        pipeline_run_id=None,
                        submission_id=source_run_id,
                        force_refresh=True,
                    )
                    fallback_credits += int(probe.credits_charged or 0)
                    print(
                        f"  persons status={probe.status} "
                        f"credits={probe.credits_charged}"
                    )
                    hits = (probe.payload or {}).get("search") or []
                    for hit in hits:
                        if not isinstance(hit, dict):
                            continue
                        persons_conn = hit.get("persons") or {}
                        edges = persons_conn.get("edges") or []
                        print(f"  persons edges: {len(edges)}")
                        for edge in edges:
                            node = edge.get("node") or {}
                            uid = node.get("id")
                            if uid:
                                name_edges = (
                                    (node.get("names") or {}).get("edges") or []
                                )
                                name_node = (
                                    name_edges[0].get("node")
                                    if name_edges
                                    else {}
                                )
                                print(
                                    f"  harvested Person UUID: {uid} "
                                    f"name={name_node!r}"
                                )
                                person_uuid = uid
                                break
                        if person_uuid:
                            break
                    if person_uuid:
                        break
                except AdapterError as exc:
                    print(f"  LE probe failed: {exc}")

        if not person_uuid:
            print("\nFATAL — could not resolve a Person UUID in budget. Halt.")
            sys.exit(2)

        # ----- ENRICH_PERSON_QUERY (first call, live charge) -----
        _banner(f"ENRICH_PERSON live call against {person_uuid}")
        enrich_result = await adapter.execute(
            operation_name="EnrichPerson",
            query=ENRICH_PERSON_QUERY,
            variables={
                "searchInput": {
                    "entityType": "PERSON",
                    "id": person_uuid,
                }
            },
            subject_kind="person",
            subject_enigma_uuid=person_uuid,
            required_tier=1,
            source_operation_id="phase5_1.live.enrich",
            pipeline_run_id=None,
            submission_id=source_run_id,
        )
        print(
            f"status={enrich_result.status} "
            f"tier_reached={enrich_result.tier_reached} "
            f"credits={enrich_result.credits_charged} "
            f"log_id={enrich_result.log_id}"
        )
        if enrich_result.status != "success":
            print(f"FATAL — enrich did not succeed: status={enrich_result.status}")
            sys.exit(3)

        # ----- persist_person_payload -----
        _banner("persist_person_payload")
        persist_result = persist_person_payload(
            pool,
            enrich_result.payload,
            source_log_id=enrich_result.log_id,
            tier=enrich_result.tier_reached,
            source_operation_id="phase5_1.live.enrich",
            source_run_id=source_run_id,
        )
        print(
            f"upserted={persist_result.upserted} "
            f"person_enigma_uuid={persist_result.person_enigma_uuid} "
            f"internal_id={persist_result.person_internal_id} "
            f"name_edges={persist_result.name_edge_count} "
            f"le_edges={persist_result.legal_entity_edge_count} "
            f"truncation_warning={persist_result.truncation_warning}"
        )

        # ----- DB snapshot -----
        _banner("DB snapshot — entities.enigma_persons (most recent 5)")
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, enigma_person_uuid, full_name, first_name, last_name,
                           date_of_birth,
                           COALESCE(jsonb_array_length(legal_entities_json), 0) AS le_count,
                           COALESCE(jsonb_array_length(names_json), 0) AS name_count,
                           highest_tier_reached, last_enriched_at
                    FROM entities.enigma_persons
                    ORDER BY last_enriched_at DESC NULLS LAST
                    LIMIT 5
                    """
                )
                for row in cur.fetchall():
                    print(row)

        # ----- Cache-hit replay -----
        _banner("Cache-hit replay — same EnrichPerson call, force_refresh=False")
        replay = await adapter.execute(
            operation_name="EnrichPerson",
            query=ENRICH_PERSON_QUERY,
            variables={
                "searchInput": {
                    "entityType": "PERSON",
                    "id": person_uuid,
                }
            },
            subject_kind="person",
            subject_enigma_uuid=person_uuid,
            required_tier=1,
            source_operation_id="phase5_1.live.enrich.replay",
            pipeline_run_id=None,
            submission_id=source_run_id,
        )
        print(
            f"status={replay.status} "
            f"credits={replay.credits_charged} "
            f"cache_hit_log_id={replay.cache_hit_log_id}"
        )

        # ----- Summary -----
        _banner("PHASE 5.1 LIVE HARNESS SUMMARY")
        print(json.dumps({
            "person_uuid": person_uuid,
            "search_credits": search_credits,
            "fallback_probe_credits": fallback_credits,
            "enrich_credits": int(enrich_result.credits_charged),
            "replay_credits": int(replay.credits_charged),
            "total_credits_this_run": adapter.credits_spent_this_run,
            "enrich_log_id": str(enrich_result.log_id),
            "persist_name_edges": persist_result.name_edge_count,
            "persist_le_edges": persist_result.legal_entity_edge_count,
            "persist_truncation_warning": persist_result.truncation_warning,
        }, indent=2, default=str))
    finally:
        pool.close()


if __name__ == "__main__":
    asyncio.run(main())

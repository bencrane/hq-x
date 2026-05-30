"""Phase 5 Step 0 — plan-access + schema-shape probes.

Runs under `doppler run --project data-engine-x-api --config dev -- python3`
so ENIGMA_API_KEY + DEX_DB_URL_POOLED are injected.

Probes (per docs/EXECUTOR_DIRECTIVE_... Phase 5 Step 0):
  A. Introspect Role type              (0 credits)
  B. Introspect Person + PersonName    (0 credits)
  C. LegalEntity.roles on Walmart      (~3 credits Plus)
  D. Person search by firstName+lastName (Free/Core, likely ~1 credit once payload includes PersonName scalars)
  E. Person direct enrich by UUID      (~1 credit Core — attribute map says Person.names = Core)
  F. Cache semantics — re-run Probe C  (0 credits)

Findings printed to stdout for capture into agent-summary-enigma-phase-5.md.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# Add repo root to sys.path so `import app` works.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from psycopg_pool import ConnectionPool

from app.providers.enigma_adapter import EnigmaAdapter
from app.providers.enigma_adapter.errors import AdapterError


WALMART_LEGAL_ENTITY_UUID = "68691638-2e0d-4613-0000-10c000000000"

INTROSPECT_ROLE = """
query IntrospectRole {
  __type(name: "Role") {
    name
    kind
    fields {
      name
      type { name kind ofType { name kind ofType { name kind } } }
    }
  }
}
""".strip()

INTROSPECT_PERSON = """
query IntrospectPerson {
  __type(name: "Person") {
    name
    kind
    fields {
      name
      type { name kind ofType { name kind ofType { name kind } } }
    }
  }
}
""".strip()

INTROSPECT_PERSON_NAME = """
query IntrospectPersonName {
  __type(name: "PersonName") {
    name
    kind
    fields {
      name
      type { name kind ofType { name kind ofType { name kind } } }
    }
  }
}
""".strip()

INTROSPECT_PERSON_NAME_EDGE = """
query IntrospectPersonNameEdge {
  __type(name: "PersonNameEdge") {
    name
    kind
    fields {
      name
      type { name kind ofType { name kind ofType { name kind } } }
    }
  }
}
""".strip()

PROBE_ROLE_ACCESS = """
query ProbeRoleAccess($searchInput: SearchInput!) {
  search(searchInput: $searchInput) {
    ... on LegalEntity {
      id
      roles(first: 10) {
        edges {
          node {
            id
            jobTitle
            jobFunction
            managementLevel
            firstObservedDate
            lastObservedDate
            internalId
            internalRoleId
            legalEntities(first: 5) {
              edges {
                node { id }
                legalEntityPerformsRoleId
              }
              pageInfo { hasNextPage endCursor }
            }
          }
          legalEntityPerformsRoleId
          datasetIds
          firstObservedDate
          lastObservedDate
          rank
          internalId
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
""".strip()

PROBE_PERSON_SEARCH = """
query ProbePersonSearch($searchInput: SearchInput!) {
  search(searchInput: $searchInput) {
    ... on Person {
      id
      internalId
      enigmaId
      names(first: 3) {
        edges {
          node {
            firstName
            lastName
            fullName
          }
        }
      }
    }
  }
}
""".strip()

PROBE_PERSON_ENRICH = """
query ProbePersonEnrich($searchInput: SearchInput!) {
  search(searchInput: $searchInput) {
    ... on Person {
      id
      internalId
      enigmaId
      names(first: 10) {
        edges {
          node {
            firstName
            lastName
            fullName
            dateOfBirth
          }
        }
        pageInfo { hasNextPage endCursor }
      }
      legalEntities(first: 100) {
        edges {
          node { id }
          personIsInstanceOfLegalEntityId
          datasetIds
          firstObservedDate
          lastObservedDate
          rank
          internalId
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
""".strip()


def _banner(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}", flush=True)


async def main() -> None:
    api_key = os.environ.get("ENIGMA_API_KEY")
    db_url = os.environ.get("DEX_DB_URL_POOLED")
    if not api_key:
        print("ENIGMA_API_KEY missing", file=sys.stderr)
        sys.exit(1)
    if not db_url:
        print("DEX_DB_URL_POOLED missing", file=sys.stderr)
        sys.exit(1)

    pool = ConnectionPool(conninfo=db_url, min_size=1, max_size=2, timeout=30.0)
    try:
        adapter = EnigmaAdapter(api_key=api_key, db_pool=pool, hard_cap=25)

        findings: dict[str, object] = {}

        # ----- Probe A: Role introspection (0 credits) -----
        _banner("PROBE A — Introspect Role type (0 credits)")
        try:
            result = await adapter.execute(
                operation_name="IntrospectRole",
                query=INTROSPECT_ROLE,
                variables={},
                subject_kind="search",
                subject_enigma_uuid=None,
                required_tier=0,
                source_operation_id="phase5.step0.probe_a",
            )
            role_type = result.payload["__type"] if result.payload else None
            fields = [f["name"] for f in (role_type or {}).get("fields", [])]
            findings["role_fields"] = fields
            findings["role_has_name"] = "name" in fields
            print(f"status={result.status} credits={result.credits_charged}")
            print(f"Role fields: {fields}")
            print(f"Role.name present? {findings['role_has_name']}")
        except AdapterError as exc:
            print(f"PROBE A FAILED: {exc}")
            findings["probe_a_error"] = str(exc)

        # ----- Probe B: Person + PersonName introspection (0 credits) -----
        _banner("PROBE B — Introspect Person + PersonName types (0 credits)")
        try:
            result = await adapter.execute(
                operation_name="IntrospectPerson",
                query=INTROSPECT_PERSON,
                variables={},
                subject_kind="search",
                subject_enigma_uuid=None,
                required_tier=0,
                source_operation_id="phase5.step0.probe_b_person",
            )
            person_type = result.payload["__type"] if result.payload else None
            person_fields = [f["name"] for f in (person_type or {}).get("fields", [])]
            findings["person_fields"] = person_fields
            print(f"Person fields: {person_fields}")
        except AdapterError as exc:
            print(f"PROBE B (Person) FAILED: {exc}")
            findings["probe_b_person_error"] = str(exc)

        try:
            result = await adapter.execute(
                operation_name="IntrospectPersonName",
                query=INTROSPECT_PERSON_NAME,
                variables={},
                subject_kind="search",
                subject_enigma_uuid=None,
                required_tier=0,
                source_operation_id="phase5.step0.probe_b_personname",
            )
            pn_type = result.payload["__type"] if result.payload else None
            pn_fields = [f["name"] for f in (pn_type or {}).get("fields", [])] if pn_type else []
            findings["personname_fields"] = pn_fields
            print(f"PersonName fields: {pn_fields}")
        except AdapterError as exc:
            print(f"PROBE B (PersonName) FAILED: {exc}")
            findings["probe_b_personname_error"] = str(exc)

        try:
            result = await adapter.execute(
                operation_name="IntrospectPersonNameEdge",
                query=INTROSPECT_PERSON_NAME_EDGE,
                variables={},
                subject_kind="search",
                subject_enigma_uuid=None,
                required_tier=0,
                source_operation_id="phase5.step0.probe_b_personname_edge",
            )
            pne_type = result.payload["__type"] if result.payload else None
            pne_fields = [f["name"] for f in (pne_type or {}).get("fields", [])] if pne_type else []
            findings["personname_edge_fields"] = pne_fields
            print(f"PersonNameEdge fields: {pne_fields}")
        except AdapterError as exc:
            print(f"PROBE B (PersonNameEdge) FAILED: {exc}")

        # ----- Probe C: LegalEntity.roles on Walmart (Plus tier ~3 credits) -----
        _banner("PROBE C — LegalEntity.roles on Walmart (Plus ~3 credits)")
        try:
            result = await adapter.execute(
                operation_name="ProbeRoleAccess",
                query=PROBE_ROLE_ACCESS,
                variables={
                    "searchInput": {
                        "entityType": "LEGAL_ENTITY",
                        "id": WALMART_LEGAL_ENTITY_UUID,
                    }
                },
                subject_kind="legal_entity",
                subject_enigma_uuid=WALMART_LEGAL_ENTITY_UUID,
                required_tier=3,
                source_operation_id="phase5.step0.probe_c",
                force_refresh=True,
            )
            findings["probe_c_status"] = result.status
            findings["probe_c_credits"] = result.credits_charged
            role_payload = None
            if result.payload:
                search = result.payload.get("search") or []
                if search and isinstance(search[0], dict):
                    role_payload = search[0].get("roles")
            role_edges = (role_payload or {}).get("edges") or []
            findings["probe_c_role_edge_count"] = len(role_edges)
            findings["probe_c_has_next_page"] = (role_payload or {}).get("pageInfo", {}).get("hasNextPage")
            print(f"status={result.status} credits={result.credits_charged}")
            print(f"role edges: {len(role_edges)} hasNextPage={findings['probe_c_has_next_page']}")
            if role_edges:
                first = role_edges[0].get("node") or {}
                print(f"sample role node: jobTitle={first.get('jobTitle')!r} mgmtLevel={first.get('managementLevel')!r}")
                first_edge = role_edges[0]
                print(f"sample edge scalars: rank={first_edge.get('rank')!r} datasetIds={first_edge.get('datasetIds')!r}")
            findings["probe_c_total_spent_so_far"] = adapter.credits_spent_this_run
            findings["probe_c_log_id"] = str(result.log_id) if result.log_id else None
        except AdapterError as exc:
            print(f"PROBE C FAILED: {exc}")
            findings["probe_c_error"] = str(exc)

        # ----- Probe D: Person search (Free/Core tier, ~0-1 credit) -----
        _banner("PROBE D — Person search by firstName+lastName (~0-1 credits)")
        try:
            result = await adapter.execute(
                operation_name="ProbePersonSearch",
                query=PROBE_PERSON_SEARCH,
                variables={
                    "searchInput": {
                        "entityType": "PERSON",
                        "person": {"firstName": "John", "lastName": "Smith"},
                    }
                },
                subject_kind="search",
                subject_enigma_uuid=None,
                required_tier=1,
                source_operation_id="phase5.step0.probe_d",
            )
            findings["probe_d_status"] = result.status
            findings["probe_d_credits"] = result.credits_charged
            search = (result.payload or {}).get("search") or []
            findings["probe_d_hit_count"] = len(search)
            print(f"status={result.status} credits={result.credits_charged}")
            print(f"search union hits: {len(search)}")
            person_ids = [s.get("id") for s in search if isinstance(s, dict) and s.get("id")]
            findings["probe_d_sample_person_ids"] = person_ids[:3]
            if person_ids:
                print(f"sample Person ids: {person_ids[:3]}")
                sample = search[0]
                print(f"sample shape: {json.dumps(sample, default=str)[:500]}")
        except AdapterError as exc:
            print(f"PROBE D FAILED: {exc}")
            findings["probe_d_error"] = str(exc)

        # ----- Probe E: Person enrich by UUID (~1 credit Core) -----
        _banner("PROBE E — Person direct enrich (~1 credit Core)")
        target_person_id = None
        if findings.get("probe_d_sample_person_ids"):
            candidates = findings["probe_d_sample_person_ids"]
            if isinstance(candidates, list) and candidates:
                target_person_id = candidates[0]
        if not target_person_id:
            print("SKIPPED — no Person UUID from Probe D")
            findings["probe_e_status"] = "skipped"
        else:
            try:
                result = await adapter.execute(
                    operation_name="ProbePersonEnrich",
                    query=PROBE_PERSON_ENRICH,
                    variables={
                        "searchInput": {
                            "entityType": "PERSON",
                            "id": target_person_id,
                        }
                    },
                    subject_kind="person",
                    subject_enigma_uuid=target_person_id,
                    required_tier=1,
                    source_operation_id="phase5.step0.probe_e",
                    force_refresh=True,
                )
                findings["probe_e_status"] = result.status
                findings["probe_e_credits"] = result.credits_charged
                search = (result.payload or {}).get("search") or []
                findings["probe_e_hit_count"] = len(search)
                print(f"status={result.status} credits={result.credits_charged}")
                if search:
                    first = search[0]
                    le_conn = first.get("legalEntities") or {}
                    le_edges = le_conn.get("edges") or []
                    names_conn = first.get("names") or {}
                    name_edges = names_conn.get("edges") or []
                    findings["probe_e_le_edge_count"] = len(le_edges)
                    findings["probe_e_name_edge_count"] = len(name_edges)
                    print(f"Person.legalEntities: {len(le_edges)} edges, hasNextPage={le_conn.get('pageInfo', {}).get('hasNextPage')}")
                    print(f"Person.names: {len(name_edges)} edges, hasNextPage={names_conn.get('pageInfo', {}).get('hasNextPage')}")
                    if name_edges:
                        print(f"sample name node: {json.dumps(name_edges[0].get('node'), default=str)}")
                    if le_edges:
                        print(f"sample LE edge: {json.dumps(le_edges[0], default=str)[:500]}")
            except AdapterError as exc:
                print(f"PROBE E FAILED: {exc}")
                findings["probe_e_error"] = str(exc)

        # ----- Probe F: cache semantics — re-run Probe C without force_refresh -----
        _banner("PROBE F — Re-run Probe C (expect cache hit, 0 credits)")
        try:
            result = await adapter.execute(
                operation_name="ProbeRoleAccess",
                query=PROBE_ROLE_ACCESS,
                variables={
                    "searchInput": {
                        "entityType": "LEGAL_ENTITY",
                        "id": WALMART_LEGAL_ENTITY_UUID,
                    }
                },
                subject_kind="legal_entity",
                subject_enigma_uuid=WALMART_LEGAL_ENTITY_UUID,
                required_tier=3,
                source_operation_id="phase5.step0.probe_f",
            )
            findings["probe_f_status"] = result.status
            findings["probe_f_credits"] = result.credits_charged
            print(f"status={result.status} credits={result.credits_charged}")
        except AdapterError as exc:
            print(f"PROBE F FAILED: {exc}")
            findings["probe_f_error"] = str(exc)

        # ----- Summary -----
        _banner("STEP 0 SUMMARY")
        findings["total_credits_this_run"] = adapter.credits_spent_this_run
        print(json.dumps(findings, default=str, indent=2))
        print(f"\nTOTAL CREDITS SPENT THIS RUN: {adapter.credits_spent_this_run}")

    finally:
        pool.close()


if __name__ == "__main__":
    asyncio.run(main())

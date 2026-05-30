#!/usr/bin/env python3
"""Backfill entity_relationships with works_at edges from person_entities canonical_payload."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.database import get_supabase_client
from app.services.entity_relationships import record_entity_relationship

PAGE_SIZE = 500


def _normalize_domain(value: str) -> str:
    candidate = value.strip().lower()
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    parsed = urlparse(candidate)
    netloc = parsed.netloc or parsed.path
    normalized = netloc.strip().lower()
    if normalized.startswith("www."):
        normalized = normalized[4:]
    return normalized.rstrip("/")


def _normalize_linkedin_url(value: str) -> str:
    normalized = value.strip().lower().rstrip("/")
    if normalized.startswith("https://"):
        normalized = normalized[len("https://"):]
    elif normalized.startswith("http://"):
        normalized = normalized[len("http://"):]
    if normalized.startswith("www."):
        normalized = normalized[4:]
    return normalized


def _extract_company_identifier(payload: dict[str, Any]) -> tuple[str | None, str]:
    """Extract company identifier from canonical_payload.

    Returns (identifier, identifier_type) where identifier_type is
    'domain', 'linkedin', or 'name'.
    """
    # Priority 1: domain
    domain_raw = payload.get("current_company_domain") or payload.get("company_domain")
    if domain_raw and isinstance(domain_raw, str) and domain_raw.strip():
        return _normalize_domain(domain_raw), "domain"

    # Priority 2: LinkedIn URL
    li_raw = payload.get("current_company_linkedin_url") or payload.get("company_linkedin_url")
    if li_raw and isinstance(li_raw, str) and li_raw.strip():
        return _normalize_linkedin_url(li_raw), "linkedin"

    # Priority 3: company name (fallback)
    name_raw = payload.get("current_company_name") or payload.get("company_name")
    if name_raw and isinstance(name_raw, str) and name_raw.strip():
        return name_raw.strip().lower(), "name"

    return None, "none"


def _resolve_company_entity(
    org_id: str,
    identifier: str,
    identifier_type: str,
) -> str | None:
    """Try to match identifier against company_entities. Return entity_id or None."""
    client = get_supabase_client()

    if identifier_type == "domain":
        result = (
            client.table("company_entities")
            .select("entity_id")
            .eq("org_id", org_id)
            .eq("canonical_domain", identifier)
            .limit(1)
            .execute()
        )
        if result.data:
            return result.data[0]["entity_id"]

    if identifier_type == "linkedin":
        result = (
            client.table("company_entities")
            .select("entity_id")
            .eq("org_id", org_id)
            .eq("linkedin_url", identifier)
            .limit(1)
            .execute()
        )
        if result.data:
            return result.data[0]["entity_id"]

    # For domain identifiers, also try LinkedIn match if domain match failed
    if identifier_type == "domain":
        # No secondary lookup needed — domain is authoritative
        pass

    return None


def _get_source_identifier(person: dict[str, Any]) -> str | None:
    """Get the best unique person identifier for source_identifier."""
    linkedin = person.get("linkedin_url")
    if linkedin and isinstance(linkedin, str) and linkedin.strip():
        return linkedin.strip()
    email = person.get("work_email")
    if email and isinstance(email, str) and email.strip():
        return email.strip()
    return None


def main() -> int:
    client = get_supabase_client()

    total_processed = 0
    total_created = 0
    total_skipped_no_identifier = 0
    total_skipped_no_source = 0
    total_matched = 0
    total_unmatched = 0

    offset = 0
    while True:
        result = (
            client.table("person_entities")
            .select("org_id, entity_id, linkedin_url, work_email, canonical_payload")
            .order("created_at")
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )
        persons = result.data or []
        if not persons:
            break

        for person in persons:
            total_processed += 1
            org_id = person["org_id"]
            payload = person.get("canonical_payload")
            if not isinstance(payload, dict):
                payload = {}

            # Extract company identifier
            identifier, identifier_type = _extract_company_identifier(payload)
            if identifier is None:
                total_skipped_no_identifier += 1
                print(f"[SKIP] entity_id={person['entity_id']} no company identifier in canonical_payload")
                continue

            # Get source identifier for the person
            source_identifier = _get_source_identifier(person)
            if source_identifier is None:
                total_skipped_no_source += 1
                print(f"[SKIP] entity_id={person['entity_id']} no linkedin_url or work_email for source_identifier")
                continue

            # Try to resolve company entity
            target_entity_id = _resolve_company_entity(org_id, identifier, identifier_type)

            # Build metadata
            metadata: dict[str, Any] = {
                "source": "backfill",
                "extracted_from": "canonical_payload",
            }
            # Include extra company fields for context
            company_name = payload.get("current_company_name") or payload.get("company_name")
            if company_name and isinstance(company_name, str) and company_name.strip():
                metadata["company_name"] = company_name.strip()

            try:
                record_entity_relationship(
                    org_id=org_id,
                    source_entity_type="person",
                    source_entity_id=str(person["entity_id"]),
                    source_identifier=source_identifier,
                    relationship="works_at",
                    target_entity_type="company",
                    target_entity_id=str(target_entity_id) if target_entity_id else None,
                    target_identifier=identifier,
                    metadata=metadata,
                    source_operation_id="backfill.entity_relationships.works_at",
                )
                total_created += 1
                if target_entity_id:
                    total_matched += 1
                    print(f"[MATCH] entity_id={person['entity_id']} -> company={target_entity_id} via {identifier_type}={identifier}")
                else:
                    total_unmatched += 1
                    print(f"[UNMATCHED] entity_id={person['entity_id']} -> {identifier_type}={identifier} (no company entity found)")
            except Exception as exc:
                print(f"[ERROR] entity_id={person['entity_id']} failed: {exc}")

        if len(persons) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    print("\nBackfill summary")
    print(f"- total_processed: {total_processed}")
    print(f"- relationships_created: {total_created}")
    print(f"- skipped_no_company_identifier: {total_skipped_no_identifier}")
    print(f"- skipped_no_source_identifier: {total_skipped_no_source}")
    print(f"- matched (with target_entity_id): {total_matched}")
    print(f"- unmatched (no company entity): {total_unmatched}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

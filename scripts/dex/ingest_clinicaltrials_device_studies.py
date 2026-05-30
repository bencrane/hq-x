#!/usr/bin/env python3
"""Ingest ClinicalTrials.gov device studies into entities.clinicaltrials_device_studies.

Usage:
SUPER_ADMIN_JWT_SECRET=unused-for-ingest doppler run --project data-engine-x-api --config prd -- python3 scripts/ingest_clinicaltrials_device_studies.py
"""

from __future__ import annotations

import json
import logging
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from psycopg import connect

# Allow running as `python3 scripts/...` from repo root without manual PYTHONPATH.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config import get_settings

API_URL = "https://clinicaltrials.gov/api/v2/studies"
SOURCE_PROVIDER = "clinicaltrials.gov"
PAGE_SIZE = 1000
REQUEST_TIMEOUT_SECONDS = 60
REQUEST_PAUSE_SECONDS = 0.35
MAX_RETRIES = 6
UPSERT_BATCH_SIZE = 250

STATUS_FILTERS = [
    "RECRUITING",
    "ACTIVE_NOT_RECRUITING",
    "COMPLETED",
    "ENROLLING_BY_INVITATION",
    "NOT_YET_RECRUITING",
    "APPROVED_FOR_MARKETING",
    "TERMINATED",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("clinicaltrials-device-ingest")


@dataclass
class IngestResult:
    pages_fetched: int
    studies_fetched: int
    studies_upserted: int
    studies_skipped: int
    total_count_reported_by_api: int | None


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _list_to_json_or_none(values: list[str]) -> str | None:
    if not values:
        return None
    return json.dumps(values, ensure_ascii=False)


def _dedupe_non_empty(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        text = _as_text(value)
        if not text:
            continue
        if text in seen:
            continue
        seen.add(text)
        output.append(text)
    return output


def _get_nested(payload: dict[str, Any], path: list[str], default: Any = None) -> Any:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    return current


def _extract_results_first_posted_date(status_module: dict[str, Any]) -> str | None:
    for path in (
        ["resultsFirstPostDateStruct", "date"],
        ["resultsFirstPostDate", "date"],
        ["resultsFirstPostDateStruct"],
        ["resultsFirstPostDate"],
    ):
        value = _get_nested(status_module, path)
        text = _as_text(value)
        if text:
            return text
    return None


def _build_row(study: dict[str, Any]) -> tuple[Any, ...] | None:
    protocol = study.get("protocolSection") if isinstance(study, dict) else None
    if not isinstance(protocol, dict):
        return None

    identification_module = protocol.get("identificationModule", {})
    status_module = protocol.get("statusModule", {})
    sponsor_module = protocol.get("sponsorCollaboratorsModule", {})
    arms_module = protocol.get("armsInterventionsModule", {})
    design_module = protocol.get("designModule", {})
    conditions_module = protocol.get("conditionsModule", {})
    contacts_module = protocol.get("contactsLocationsModule", {})

    nct_id = _as_text(identification_module.get("nctId"))
    if not nct_id:
        return None

    collaborators = sponsor_module.get("collaborators") or []
    collaborator_names = _dedupe_non_empty(
        [item.get("name") for item in collaborators if isinstance(item, dict)]
    )
    collaborator_classes = _dedupe_non_empty(
        [item.get("class") for item in collaborators if isinstance(item, dict)]
    )

    interventions = arms_module.get("interventions") or []
    device_interventions = [
        item
        for item in interventions
        if isinstance(item, dict) and _as_text(item.get("type")) == "DEVICE"
    ]
    device_intervention_names = _dedupe_non_empty(
        [item.get("name") for item in device_interventions]
    )
    device_intervention_types = _dedupe_non_empty(
        [item.get("type") for item in device_interventions]
    )

    locations = contacts_module.get("locations") or []
    location_countries = _dedupe_non_empty(
        [item.get("country") for item in locations if isinstance(item, dict)]
    )
    location_states = _dedupe_non_empty(
        [item.get("state") for item in locations if isinstance(item, dict)]
    )

    phases = _dedupe_non_empty(design_module.get("phases") or [])
    conditions = _dedupe_non_empty(conditions_module.get("conditions") or [])

    raw_json = json.dumps(study, ensure_ascii=False)

    return (
        nct_id,
        _as_text(identification_module.get("briefTitle")),
        _as_text(status_module.get("overallStatus")),
        _as_text(status_module.get("whyStopped")),
        _as_text(_get_nested(sponsor_module, ["leadSponsor", "name"])),
        _as_text(_get_nested(sponsor_module, ["leadSponsor", "class"])),
        _list_to_json_or_none(collaborator_names),
        _list_to_json_or_none(collaborator_classes),
        _list_to_json_or_none(device_intervention_names),
        _list_to_json_or_none(device_intervention_types),
        _as_text(design_module.get("studyType")),
        _list_to_json_or_none(phases),
        _as_text(_get_nested(design_module, ["enrollmentInfo", "count"])),
        _as_text(_get_nested(status_module, ["startDateStruct", "date"])),
        _as_text(_get_nested(status_module, ["completionDateStruct", "date"])),
        _as_text(_get_nested(status_module, ["studyFirstPostDateStruct", "date"])),
        _as_text(_get_nested(status_module, ["lastUpdatePostDateStruct", "date"])),
        _extract_results_first_posted_date(status_module),
        _list_to_json_or_none(location_countries),
        _list_to_json_or_none(location_states),
        _list_to_json_or_none(conditions),
        raw_json,
        SOURCE_PROVIDER,
    )


def _fetch_page(
    client: httpx.Client,
    params: dict[str, str],
) -> dict[str, Any]:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.get(API_URL, params=params)
            if response.status_code in {429, 500, 502, 503, 504}:
                raise httpx.HTTPStatusError(
                    f"Retryable status {response.status_code}",
                    request=response.request,
                    response=response,
                )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError("ClinicalTrials.gov API response is not an object.")
            studies = payload.get("studies")
            if studies is None or not isinstance(studies, list):
                raise RuntimeError("ClinicalTrials.gov API response missing studies array.")
            return payload
        except (httpx.RequestError, httpx.HTTPStatusError, json.JSONDecodeError, RuntimeError) as exc:
            if attempt >= MAX_RETRIES:
                raise RuntimeError(f"Failed to fetch page after {MAX_RETRIES} attempts: {exc}") from exc
            sleep_seconds = (2 ** (attempt - 1)) + random.uniform(0.1, 0.4)
            logger.warning(
                "Page request failed (attempt %d/%d): %s. Sleeping %.2fs before retry.",
                attempt,
                MAX_RETRIES,
                exc,
                sleep_seconds,
            )
            time.sleep(sleep_seconds)
    raise RuntimeError("Unreachable retry loop exit.")


def _upsert_rows(rows: list[tuple[Any, ...]]) -> int:
    if not rows:
        return 0

    settings = get_settings()
    upsert_sql = """
    INSERT INTO entities.clinicaltrials_device_studies (
        nct_id,
        study_title,
        study_status,
        why_stopped,
        lead_sponsor_name,
        lead_sponsor_class,
        collaborator_names,
        collaborator_classes,
        device_intervention_names,
        device_intervention_types,
        study_type,
        phases,
        enrollment_count,
        start_date,
        completion_date,
        first_posted_date,
        last_update_posted_date,
        results_first_posted_date,
        location_countries,
        location_states,
        conditions,
        raw_json,
        source_provider,
        ingested_at,
        updated_at
    )
    VALUES (
        %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s, NOW(), NOW()
    )
    ON CONFLICT (nct_id)
    DO UPDATE SET
        study_title = EXCLUDED.study_title,
        study_status = EXCLUDED.study_status,
        why_stopped = EXCLUDED.why_stopped,
        lead_sponsor_name = EXCLUDED.lead_sponsor_name,
        lead_sponsor_class = EXCLUDED.lead_sponsor_class,
        collaborator_names = EXCLUDED.collaborator_names,
        collaborator_classes = EXCLUDED.collaborator_classes,
        device_intervention_names = EXCLUDED.device_intervention_names,
        device_intervention_types = EXCLUDED.device_intervention_types,
        study_type = EXCLUDED.study_type,
        phases = EXCLUDED.phases,
        enrollment_count = EXCLUDED.enrollment_count,
        start_date = EXCLUDED.start_date,
        completion_date = EXCLUDED.completion_date,
        first_posted_date = EXCLUDED.first_posted_date,
        last_update_posted_date = EXCLUDED.last_update_posted_date,
        results_first_posted_date = EXCLUDED.results_first_posted_date,
        location_countries = EXCLUDED.location_countries,
        location_states = EXCLUDED.location_states,
        conditions = EXCLUDED.conditions,
        raw_json = EXCLUDED.raw_json,
        source_provider = EXCLUDED.source_provider,
        ingested_at = NOW(),
        updated_at = NOW()
    """

    with connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            for start in range(0, len(rows), UPSERT_BATCH_SIZE):
                batch = rows[start : start + UPSERT_BATCH_SIZE]
                cur.executemany(upsert_sql, batch)
            conn.commit()
    return len(rows)


def ingest() -> IngestResult:
    studies_fetched = 0
    studies_upserted = 0
    studies_skipped = 0
    pages_fetched = 0
    total_count_reported_by_api: int | None = None
    next_page_token: str | None = None

    with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        while True:
            params = {
                "query.intr": "device",
                "filter.overallStatus": ",".join(STATUS_FILTERS),
                "pageSize": str(PAGE_SIZE),
            }
            if pages_fetched == 0:
                params["countTotal"] = "true"
            if next_page_token:
                params["pageToken"] = next_page_token

            payload = _fetch_page(client, params)
            studies = payload.get("studies", [])
            if total_count_reported_by_api is None:
                total_count = payload.get("totalCount")
                if isinstance(total_count, int):
                    total_count_reported_by_api = total_count

            rows: list[tuple[Any, ...]] = []
            for study in studies:
                row = _build_row(study)
                if row is None:
                    studies_skipped += 1
                    continue
                rows.append(row)

            inserted = _upsert_rows(rows)
            studies_upserted += inserted
            studies_fetched += len(studies)
            pages_fetched += 1

            logger.info(
                "Fetched page %d: page_records=%d, total_fetched=%d, total_upserted=%d, skipped=%d",
                pages_fetched,
                len(studies),
                studies_fetched,
                studies_upserted,
                studies_skipped,
            )

            next_page_token = payload.get("nextPageToken")
            if not next_page_token:
                break
            time.sleep(REQUEST_PAUSE_SECONDS)

    return IngestResult(
        pages_fetched=pages_fetched,
        studies_fetched=studies_fetched,
        studies_upserted=studies_upserted,
        studies_skipped=studies_skipped,
        total_count_reported_by_api=total_count_reported_by_api,
    )


def _query_table_count() -> int:
    settings = get_settings()
    with connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM entities.clinicaltrials_device_studies")
            count = cur.fetchone()[0]
    return int(count)


def main() -> None:
    started_at = time.monotonic()
    result = ingest()
    row_count = _query_table_count()
    elapsed_seconds = time.monotonic() - started_at

    logger.info(
        "ClinicalTrials.gov device ingestion complete: pages=%d fetched=%d upserted=%d skipped=%d api_total=%s table_rows=%d elapsed=%.2fs",
        result.pages_fetched,
        result.studies_fetched,
        result.studies_upserted,
        result.studies_skipped,
        result.total_count_reported_by_api,
        row_count,
        elapsed_seconds,
    )

    print(
        json.dumps(
            {
                "pages_fetched": result.pages_fetched,
                "studies_fetched": result.studies_fetched,
                "studies_upserted": result.studies_upserted,
                "studies_skipped": result.studies_skipped,
                "api_total_count": result.total_count_reported_by_api,
                "table_row_count": row_count,
                "elapsed_seconds": round(elapsed_seconds, 2),
                "status_filters": STATUS_FILTERS,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

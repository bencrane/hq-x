#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.database import get_supabase_client
from app.services.person_intel_briefings import upsert_person_intel_briefing

TARGET_SUBMISSION_ID = "35d818c4-2159-4e5d-89a7-dddf92470c57"
TARGET_ORG_ID = "b0293785-aa7a-4234-8201-cc47305295f8"


def _extract_person_intel_payload(output_payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(output_payload, dict):
        return {}

    operation_result = output_payload.get("operation_result")
    if not isinstance(operation_result, dict):
        return {}

    output = operation_result.get("output")
    if not isinstance(output, dict):
        return {}

    person_full_name = output.get("full_name") or output.get("person_full_name")
    if not isinstance(person_full_name, str) or not person_full_name.strip():
        return {}

    person_linkedin_url = output.get("linkedin_url") or output.get("person_linkedin_url")
    if not isinstance(person_linkedin_url, str):
        person_linkedin_url = None

    person_current_company_name = output.get("person_current_company_name")
    if not isinstance(person_current_company_name, str):
        person_current_company_name = None

    person_current_job_title = output.get("title") or output.get("person_current_job_title")
    if not isinstance(person_current_job_title, str):
        person_current_job_title = None

    client_company_name = output.get("client_company_name")
    if not isinstance(client_company_name, str):
        client_company_name = None

    client_company_description = output.get("client_company_description")
    if not isinstance(client_company_description, str):
        client_company_description = None

    customer_company_name = output.get("customer_company_name")
    if not isinstance(customer_company_name, str):
        customer_company_name = None

    parallel_raw_response = output.get("parallel_raw_response")
    raw_parallel_output: dict[str, Any] = {}
    if isinstance(parallel_raw_response, dict):
        parallel_output = parallel_raw_response.get("output")
        if isinstance(parallel_output, dict):
            content = parallel_output.get("content")
            if isinstance(content, dict) and content:
                raw_parallel_output = content
    if not raw_parallel_output:
        return {}

    parallel_run_id = output.get("parallel_run_id")
    if not isinstance(parallel_run_id, str):
        parallel_run_id = None

    processor = output.get("processor")
    if not isinstance(processor, str):
        processor = None

    return {
        "person_full_name": person_full_name,
        "person_linkedin_url": person_linkedin_url,
        "person_current_company_name": person_current_company_name,
        "person_current_job_title": person_current_job_title,
        "client_company_name": client_company_name,
        "client_company_description": client_company_description,
        "customer_company_name": customer_company_name,
        "raw_parallel_output": raw_parallel_output,
        "parallel_run_id": parallel_run_id,
        "processor": processor,
    }


def main() -> int:
    client = get_supabase_client()
    pipeline_runs_result = (
        client.table("pipeline_runs")
        .select("id, submission_id, parent_pipeline_run_id, status")
        .eq("submission_id", TARGET_SUBMISSION_ID)
        .eq("status", "succeeded")
        .execute()
    )
    pipeline_runs = pipeline_runs_result.data or []

    total_processed = 0
    total_upserted = 0
    total_skipped = 0

    print(f"Found {len(pipeline_runs)} succeeded child pipeline runs for submission={TARGET_SUBMISSION_ID}")

    for pipeline_run in pipeline_runs:
        run_id = str(pipeline_run.get("id"))
        step_results_response = (
            client.table("step_results")
            .select("output_payload")
            .eq("pipeline_run_id", run_id)
            .eq("status", "succeeded")
            .execute()
        )
        step_results = step_results_response.data or []

        if not step_results:
            total_skipped += 1
            print(f"[SKIP] run_id={run_id} no succeeded step_results")
            continue

        for step_result in step_results:
            total_processed += 1
            payload = _extract_person_intel_payload(step_result.get("output_payload"))
            if not payload:
                total_skipped += 1
                print(f"[SKIP] run_id={run_id} missing required output fields")
                continue

            result = upsert_person_intel_briefing(
                org_id=TARGET_ORG_ID,
                person_full_name=payload["person_full_name"],
                person_linkedin_url=payload["person_linkedin_url"],
                person_current_company_name=payload["person_current_company_name"],
                person_current_job_title=payload["person_current_job_title"],
                client_company_name=payload["client_company_name"],
                client_company_description=payload["client_company_description"],
                customer_company_name=payload["customer_company_name"],
                raw_parallel_output=payload["raw_parallel_output"],
                parallel_run_id=payload["parallel_run_id"],
                processor=payload["processor"],
                source_submission_id=TARGET_SUBMISSION_ID,
                source_pipeline_run_id=run_id,
            )
            total_upserted += 1
            print(
                "[UPSERT] "
                f"person={payload['person_full_name']} "
                f"company={payload['person_current_company_name'] or '(unknown)'} "
                f"id={result.get('id')}"
            )

    print("\nBackfill summary")
    print(f"- total_processed: {total_processed}")
    print(f"- total_upserted: {total_upserted}")
    print(f"- total_skipped: {total_skipped}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

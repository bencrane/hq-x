#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.database import get_supabase_client
from app.services.icp_job_titles import upsert_icp_job_titles

TARGET_SUBMISSION_ID = "0921f10b-890b-47ab-8ceb-b1986df51cbb"
TARGET_ORG_ID = "b0293785-aa7a-4234-8201-cc47305295f8"


def _extract_icp_payload(output_payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(output_payload, dict):
        return {}

    operation_result = output_payload.get("operation_result")
    if not isinstance(operation_result, dict):
        return {}

    output = operation_result.get("output")
    if not isinstance(output, dict):
        return {}

    parallel_raw_response = output.get("parallel_raw_response")
    if not isinstance(parallel_raw_response, dict):
        return {}

    parallel_output = parallel_raw_response.get("output")
    if not isinstance(parallel_output, dict):
        return {}

    raw_parallel_output = parallel_output.get("content")
    if not isinstance(raw_parallel_output, dict) or not raw_parallel_output:
        return {}

    company_domain = output.get("domain")
    if not isinstance(company_domain, str) or not company_domain.strip():
        return {}

    company_name = output.get("company_name")
    if not isinstance(company_name, str):
        company_name = None

    company_description = output.get("company_description")
    if not isinstance(company_description, str):
        company_description = None

    parallel_run_id = output.get("parallel_run_id")
    if not isinstance(parallel_run_id, str):
        parallel_run_id = None

    processor = output.get("processor")
    if not isinstance(processor, str):
        processor = None

    return {
        "company_domain": company_domain,
        "company_name": company_name,
        "company_description": company_description,
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
        .not_.is_("parent_pipeline_run_id", "null")
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
            payload = _extract_icp_payload(step_result.get("output_payload"))
            if not payload:
                total_skipped += 1
                print(f"[SKIP] run_id={run_id} missing domain/raw_parallel_output")
                continue

            result = upsert_icp_job_titles(
                org_id=TARGET_ORG_ID,
                company_domain=payload["company_domain"],
                company_name=payload["company_name"],
                company_description=payload["company_description"],
                raw_parallel_output=payload["raw_parallel_output"],
                parallel_run_id=payload["parallel_run_id"],
                processor=payload["processor"],
                source_submission_id=TARGET_SUBMISSION_ID,
                source_pipeline_run_id=run_id,
            )
            total_upserted += 1
            print(
                "[UPSERT] "
                f"company_name={payload['company_name'] or '(unknown)'} "
                f"domain={payload['company_domain']} "
                f"id={result.get('id')}"
            )

    print("\nBackfill summary")
    print(f"- total_processed: {total_processed}")
    print(f"- total_upserted: {total_upserted}")
    print(f"- total_skipped: {total_skipped}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

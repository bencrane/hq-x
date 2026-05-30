#!/usr/bin/env python3
"""
Manual smoke test for end-to-end batch orchestration and entity persistence.

This script is an operator tool intended to run against a live FastAPI instance.
It creates a temporary blueprint, submits a small company batch, polls batch
status, and verifies persisted company entity state.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx


DEFAULT_BATCH_DOMAINS = ["stripe.com", "notion.so", "figma.com"]
DEFAULT_POLL_INTERVAL_SECONDS = 30
DEFAULT_TIMEOUT_SECONDS = 300


@dataclass
class ApiResult:
    ok: bool
    status_code: int | None
    data: Any | None
    error: str | None


class ApiClient:
    def __init__(self, api_url: str, api_token: str, timeout_seconds: int = 30) -> None:
        self.api_url = api_url.rstrip("/")
        self.http = httpx.Client(
            timeout=timeout_seconds,
            headers={
                "Authorization": f"Bearer {api_token}",
                "Content-Type": "application/json",
            },
        )

    def close(self) -> None:
        self.http.close()

    def post(self, path: str, payload: dict[str, Any]) -> ApiResult:
        url = f"{self.api_url}{path}"
        try:
            response = self.http.post(url, json=payload)
        except Exception as exc:  # noqa: BLE001
            return ApiResult(ok=False, status_code=None, data=None, error=f"{type(exc).__name__}: {exc}")

        try:
            body = response.json()
        except Exception:  # noqa: BLE001
            body = None

        if response.status_code >= 400:
            error = None
            if isinstance(body, dict):
                error = body.get("error")
            if not error:
                error = f"HTTP {response.status_code} from {path}"
            return ApiResult(ok=False, status_code=response.status_code, data=body, error=error)

        if isinstance(body, dict) and "data" in body:
            return ApiResult(ok=True, status_code=response.status_code, data=body["data"], error=None)
        return ApiResult(ok=True, status_code=response.status_code, data=body, error=None)


def _timestamp_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def _print_step(message: str) -> None:
    print(f"\n==> {message}")


def _print_error(message: str) -> None:
    print(f"[ERROR] {message}")


def _print_info(message: str) -> None:
    print(f"[INFO] {message}")


def _resolve_company_id(client: ApiClient, explicit_company_id: str | None) -> str | None:
    if explicit_company_id:
        return explicit_company_id

    result = client.post("/api/companies/list", {})
    if not result.ok:
        _print_error(f"Failed to list companies: {result.error}")
        return None
    if not isinstance(result.data, list) or not result.data:
        _print_error("No companies available for this auth context; provide --company-id.")
        return None
    first_company = result.data[0]
    company_id = first_company.get("id")
    company_name = first_company.get("name")
    if not company_id:
        _print_error("Company list response missing company id.")
        return None
    _print_info(f"Using company_id={company_id} ({company_name}) from /api/companies/list.")
    return company_id


def _create_blueprint(client: ApiClient) -> str | None:
    blueprint_name = f"smoke-batch-{_timestamp_slug()}"
    payload = {
        "name": blueprint_name,
        "description": "Operator smoke test blueprint for batch pipeline validation",
        "steps": [
            {"position": 1, "operation_id": "company.enrich.profile", "step_config": {}, "is_enabled": True},
            {"position": 2, "operation_id": "company.research.resolve_g2_url", "step_config": {}, "is_enabled": True},
            {
                "position": 3,
                "operation_id": "company.research.resolve_pricing_page_url",
                "step_config": {},
                "is_enabled": True,
            },
        ],
    }
    result = client.post("/api/blueprints/create", payload)
    if not result.ok:
        _print_error(f"Blueprint creation failed: {result.error}")
        return None
    if not isinstance(result.data, dict):
        _print_error("Blueprint creation returned unexpected response shape.")
        return None
    blueprint_id = result.data.get("id")
    if not blueprint_id:
        _print_error("Blueprint creation response missing id.")
        return None
    _print_info(f"Created blueprint: name={blueprint_name} id={blueprint_id}")
    return blueprint_id


def _submit_batch(client: ApiClient, blueprint_id: str, company_id: str, domains: list[str]) -> str | None:
    entities = [{"entity_type": "company", "input": {"domain": domain}} for domain in domains]
    payload = {
        "blueprint_id": blueprint_id,
        "company_id": company_id,
        "entities": entities,
        "source": "manual_smoke_test_batch",
        "metadata": {"script": "scripts/smoke_test_batch.py"},
    }
    result = client.post("/api/v1/batch/submit", payload)
    if not result.ok:
        _print_error(f"Batch submission failed: {result.error}")
        return None
    if not isinstance(result.data, dict):
        _print_error("Batch submission returned unexpected response shape.")
        return None
    submission_id = result.data.get("submission_id")
    if not submission_id:
        _print_error("Batch submission response missing submission_id.")
        return None
    _print_info(f"Submitted batch: submission_id={submission_id} entity_count={len(domains)}")
    return submission_id


def _poll_batch_status(
    client: ApiClient,
    submission_id: str,
    poll_interval_seconds: int,
    timeout_seconds: int,
) -> dict[str, Any] | None:
    started = time.monotonic()
    while True:
        elapsed = int(time.monotonic() - started)
        if elapsed > timeout_seconds:
            _print_error(f"Timed out after {timeout_seconds}s waiting for batch completion.")
            return None

        result = client.post("/api/v1/batch/status", {"submission_id": submission_id})
        if not result.ok:
            _print_error(f"Batch status request failed: {result.error}")
            return None
        if not isinstance(result.data, dict):
            _print_error("Batch status returned unexpected response shape.")
            return None

        summary = result.data.get("summary") or {}
        total = summary.get("total", 0)
        completed = summary.get("completed", 0)
        failed = summary.get("failed", 0)
        running = summary.get("running", 0)
        pending = summary.get("pending", 0)
        _print_info(
            f"Batch progress after {elapsed}s: total={total} completed={completed} failed={failed} running={running} pending={pending}"
        )

        if total > 0 and (completed + failed) >= total:
            return result.data

        time.sleep(poll_interval_seconds)


def _verify_company_entities(
    client: ApiClient,
    company_id: str,
    expected_domains: list[str],
) -> tuple[bool, list[str], int]:
    result = client.post(
        "/api/v1/entities/companies",
        {"company_id": company_id, "page": 1, "per_page": 100},
    )
    if not result.ok:
        _print_error(f"Entity query failed: {result.error}")
        return False, [], 0
    if not isinstance(result.data, dict):
        _print_error("Entity query returned unexpected response shape.")
        return False, [], 0

    items = result.data.get("items") or []
    seen_domains = {
        str(item.get("canonical_domain", "")).strip().lower()
        for item in items
        if isinstance(item, dict)
    }
    matched = [domain for domain in expected_domains if domain.lower() in seen_domains]
    return len(matched) > 0, matched, len(items)


def _print_run_summary(status_payload: dict[str, Any]) -> None:
    runs = status_payload.get("runs") or []
    print("\nPer-entity run summary:")
    if not runs:
        print("- (no runs returned)")
        return
    for run in runs:
        entity_index = run.get("entity_index")
        entity_type = run.get("entity_type")
        status = run.get("status")
        run_id = run.get("pipeline_run_id")
        error_message = run.get("error_message")
        print(
            f"- index={entity_index} type={entity_type} status={status} pipeline_run_id={run_id}"
            + (f" error={error_message}" if error_message else "")
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manual smoke test for batch orchestration and entity persistence.")
    parser.add_argument(
        "--api-url",
        default=os.getenv("DEX_API_BASE_URL"),
        help="FastAPI base URL (fallback: DEX_API_BASE_URL env var).",
    )
    parser.add_argument(
        "--api-token",
        default=os.getenv("DATA_ENGINE_API_TOKEN") or os.getenv("DATA_ENGINE_SMOKE_API_TOKEN"),
        help="Bearer token (API token or JWT). Fallback env: DATA_ENGINE_API_TOKEN or DATA_ENGINE_SMOKE_API_TOKEN.",
    )
    parser.add_argument(
        "--company-id",
        default=os.getenv("DATA_ENGINE_COMPANY_ID"),
        help="Tenant company UUID. If omitted, script tries /api/companies/list and picks first company.",
    )
    parser.add_argument(
        "--domains",
        nargs="+",
        default=DEFAULT_BATCH_DOMAINS,
        help="Company domains to test (default: stripe.com notion.so figma.com).",
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=int,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        help=f"Status polling interval in seconds (default: {DEFAULT_POLL_INTERVAL_SECONDS}).",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"Overall status timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS}).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.api_url:
        _print_error("Missing --api-url and DEX_API_BASE_URL is not set.")
        return 1
    if not args.api_token:
        _print_error("Missing --api-token and DATA_ENGINE_API_TOKEN is not set.")
        return 1
    if args.poll_interval_seconds <= 0:
        _print_error("--poll-interval-seconds must be > 0")
        return 1
    if args.timeout_seconds <= 0:
        _print_error("--timeout-seconds must be > 0")
        return 1

    _print_step("Starting smoke test")
    _print_info(f"API URL: {args.api_url}")
    _print_info(f"Domains: {', '.join(args.domains)}")
    _print_info(f"Polling: every {args.poll_interval_seconds}s up to {args.timeout_seconds}s")

    client = ApiClient(api_url=args.api_url, api_token=args.api_token)
    try:
        _print_step("Resolving company scope")
        company_id = _resolve_company_id(client, args.company_id)
        if not company_id:
            return 1

        _print_step("Creating smoke test blueprint")
        blueprint_id = _create_blueprint(client)
        if not blueprint_id:
            return 1

        _print_step("Submitting batch")
        submission_id = _submit_batch(client, blueprint_id=blueprint_id, company_id=company_id, domains=args.domains)
        if not submission_id:
            return 1

        _print_step("Polling batch status")
        status_payload = _poll_batch_status(
            client,
            submission_id=submission_id,
            poll_interval_seconds=args.poll_interval_seconds,
            timeout_seconds=args.timeout_seconds,
        )
        if not status_payload:
            return 1

        _print_run_summary(status_payload)
        summary = status_payload.get("summary") or {}
        completed = int(summary.get("completed", 0))
        failed = int(summary.get("failed", 0))
        total = int(summary.get("total", 0))

        _print_step("Verifying entity state persistence")
        persisted_ok, matched_domains, entity_count = _verify_company_entities(
            client,
            company_id=company_id,
            expected_domains=args.domains,
        )
        _print_info(f"company_entities rows returned: {entity_count}")
        _print_info(
            "Matched canonical domains: "
            + (", ".join(matched_domains) if matched_domains else "(none from smoke batch domains found)")
        )

        pass_batch = total > 0 and completed + failed >= total
        pass_entities = persisted_ok
        passed = pass_batch and pass_entities

        _print_step("Smoke test result")
        print(f"Batch completion check: {'PASS' if pass_batch else 'FAIL'}")
        print(f"Entity persistence check: {'PASS' if pass_entities else 'FAIL'}")
        print(f"Overall result: {'PASS' if passed else 'FAIL'}")
        if failed > 0:
            print(f"Runs with failure status: {failed}")
        return 0 if passed else 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())

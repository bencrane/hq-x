#!/usr/bin/env python3
"""End-to-end smoke test for the Exa research orchestration prototype.

Creates two demo jobs against the live Exa API:

  Job A: search → destination=hqx (customer-research stand-in)
  Job B: search → destination=dex (dataset-enrichment stand-in)

Drives both jobs through to terminal state by calling the same
internal entrypoint Trigger.dev would call. Then verifies the raw
payload landed in the right database (hq-x for A, DEX for B) and
prints a sample of the row.

Run via:

    doppler --project hq-x --config dev run -- \\
        uv run python -m scripts.seed_exa_research_demo
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any
from uuid import UUID

import psycopg

from app.config import settings
from app.db import close_pool, get_db_connection, init_pool
from app.routers.internal import exa_jobs as internal_router
from app.services import exa_research_jobs as exa_jobs_svc

ORG_SLUG = "exa-demo"
ORG_NAME = "Exa Demo"
ORG_PLAN = "prototype"


def _abort(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


async def _upsert_demo_org() -> UUID:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id FROM business.organizations WHERE slug = %s",
                (ORG_SLUG,),
            )
            row = await cur.fetchone()
            if row:
                org_id = row[0]
                print(f"[org] existing organizations.slug='{ORG_SLUG}' id={org_id}")
                return org_id
            await cur.execute(
                """
                INSERT INTO business.organizations (name, slug, status, plan, metadata)
                VALUES (%s, %s, 'active', %s, '{}'::jsonb)
                RETURNING id
                """,
                (ORG_NAME, ORG_SLUG, ORG_PLAN),
            )
            row = await cur.fetchone()
        await conn.commit()
    org_id = row[0]
    print(f"[org] inserted organizations.slug='{ORG_SLUG}' id={org_id}")
    return org_id


async def _create_and_run_job(
    *,
    org_id: UUID,
    endpoint: str,
    destination: str,
    objective: str,
    objective_ref: str | None,
    request_payload: dict[str, Any],
) -> dict[str, Any]:
    job = await exa_jobs_svc.create_job(
        organization_id=org_id,
        created_by_user_id=None,
        endpoint=endpoint,
        destination=destination,
        objective=objective,
        objective_ref=objective_ref,
        request_payload=request_payload,
    )
    job_id = job["id"]
    print(
        f"[job] created id={job_id} destination={destination} "
        f"endpoint={endpoint} objective={objective}"
    )
    # Drive the work synchronously through the same entrypoint Trigger.dev
    # would hit. Bypasses Trigger.dev for demo ergonomics; the real path
    # is identical.
    result = await internal_router.process_exa_job(
        job_id=job_id, body={"trigger_run_id": "seed_script"}
    )
    print(f"[job] {job_id} terminal: status={result['status']}")
    if result["status"] == "failed":
        print(f"[job] {job_id} error: {result.get('error')}")
    return {"job_id": job_id, "result": result}


async def _verify_local_row(job_id: UUID) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, endpoint, status, exa_request_id, duration_ms,
                       jsonb_typeof(response_payload) AS response_type,
                       (response_payload->'results'->0) AS first_result,
                       created_at
                FROM exa.exa_calls
                WHERE triggered_by_job_id = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (str(job_id),),
            )
            row = await cur.fetchone()
            cols = [c.name for c in cur.description] if cur.description else []
    if row is None:
        return None
    return dict(zip(cols, row))


def _verify_dex_row(job_id: UUID) -> dict[str, Any] | None:
    dex_url = os.environ.get("DEX_DB_URL_POOLED") or os.environ.get(
        "DEX_DB_URL_DIRECT"
    )
    if not dex_url:
        _abort(
            "DEX_DB_URL_POOLED / DEX_DB_URL_DIRECT not set; the seed script "
            "needs DEX DB access to verify the dex-destination row landed"
        )
    with psycopg.connect(dex_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, endpoint, status, exa_request_id, duration_ms,
                       jsonb_typeof(response_payload) AS response_type,
                       (response_payload->'results'->0) AS first_result,
                       created_at
                FROM exa.exa_calls
                WHERE triggered_by_job_id = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (str(job_id),),
            )
            row = cur.fetchone()
            cols = [c.name for c in cur.description] if cur.description else []
    if row is None:
        return None
    return dict(zip(cols, row))


def _print_row(label: str, row: dict[str, Any] | None) -> None:
    if row is None:
        print(f"[{label}] NO ROW FOUND — failure")
        return
    print(f"[{label}] id={row['id']}")
    print(f"[{label}]   endpoint={row['endpoint']} status={row['status']}")
    print(
        f"[{label}]   exa_request_id={row['exa_request_id']} "
        f"duration_ms={row['duration_ms']}"
    )
    fr = row.get("first_result")
    if isinstance(fr, dict):
        title = fr.get("title")
        url = fr.get("url")
        print(f"[{label}]   first_result.title={title!r}")
        print(f"[{label}]   first_result.url={url!r}")
    else:
        print(f"[{label}]   response.results[0]={fr!r}")
    print(f"[{label}]   created_at={row['created_at']}")


async def main() -> int:
    if not settings.EXA_API_KEY:
        _abort("EXA_API_KEY is not set; aborting")
    if not settings.DEX_BASE_URL:
        _abort("DEX_BASE_URL is not set; the dex-destination job will fail")
    if not settings.DEX_SUPER_ADMIN_API_KEY:
        _abort("DEX_SUPER_ADMIN_API_KEY is not set; the dex-destination job will fail")

    await init_pool()
    try:
        org_id = await _upsert_demo_org()

        job_a = await _create_and_run_job(
            org_id=org_id,
            endpoint="search",
            destination="hqx",
            objective="demo_customer_research",
            objective_ref=f"organization:{org_id}",
            request_payload={
                "query": "DAT trucking software competitors",
                "num_results": 5,
            },
        )
        job_b = await _create_and_run_job(
            org_id=org_id,
            endpoint="search",
            destination="dex",
            objective="demo_dataset_enrichment",
            objective_ref="topic:fmcsa-fast-growing-carriers",
            request_payload={
                "query": "fast-growing FMCSA motor carriers 2026",
                "num_results": 5,
            },
        )

        row_a = await _verify_local_row(job_a["job_id"])
        row_b = _verify_dex_row(job_b["job_id"])

        print("\n=== Job A (destination=hqx) ===")
        print(f"job_id     = {job_a['job_id']}")
        print(f"result_ref = {job_a['result'].get('result_ref')}")
        _print_row("hqx", row_a)

        print("\n=== Job B (destination=dex) ===")
        print(f"job_id     = {job_b['job_id']}")
        print(f"result_ref = {job_b['result'].get('result_ref')}")
        _print_row("dex", row_b)

        ok = (
            job_a["result"]["status"] == "succeeded"
            and job_b["result"]["status"] == "succeeded"
            and row_a is not None
            and row_b is not None
        )
        print(f"\n[exit] both_succeeded={ok}")
        return 0 if ok else 1
    finally:
        await close_pool()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

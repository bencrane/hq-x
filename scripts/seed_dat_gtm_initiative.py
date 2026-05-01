#!/usr/bin/env python3
"""End-to-end seed for the GTM-initiative pipeline (slice 1).

Drives the full path:

  acq-eng org → Capital Expansion brand → DAT demand-side partner →
  DAT partner contract → DAT audience spec (resolved from DEX) →
  partner-research Exa run (re-fired durably from /tmp instructions) →
  gtm_initiatives row → strategic-context-research Exa run →
  campaign_strategy.md synthesized via Anthropic.

Bypasses Trigger.dev for the two async runs by calling the same
internal endpoints Trigger would call. Mirrors seed_exa_research_demo's
pattern.

Idempotent: each upsert keys on natural identifiers (org slug, brand
name + org, partner name + org, contract pricing_model + partner). The
gtm_initiatives row is keyed off (org, partner_contract,
data_engine_audience_id, status='draft') for the most-recent run; on
re-run we create a fresh row so multi-run audit trails are clean (the
old row sticks around in `failed`/`strategy_ready`).

Run via:

    DEX_BASE_URL=https://api.dataengine.run \\
        doppler --project hq-x --config dev run -- \\
        uv run python -m scripts.seed_dat_gtm_initiative
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID

from app.config import settings
from app.db import close_pool, get_db_connection, init_pool
from app.routers.internal import exa_jobs as internal_exa_router
from app.routers.internal import gtm_initiatives as internal_gtm_router
from app.services import exa_research_jobs as exa_jobs_svc
from app.services import gtm_initiatives as gtm_svc
from app.services import strategic_context_researcher

# Constants
ORG_SLUG = "acq-eng"
ORG_NAME = "Acquisition Engineering"
BRAND_NAME = "Capital Expansion"
BRAND_DOMAIN = "capitalexpansion.com"
PARTNER_NAME = "DAT"
PARTNER_DOMAIN = "dat.com"
DAT_AUDIENCE_SPEC_NAME = "DAT — fast-growing carriers (prototype)"
PARTNER_CONTRACT_PRICING = "flat_90d"
PARTNER_CONTRACT_AMOUNT_CENTS = 2_500_000
PARTNER_CONTRACT_DURATION_DAYS = 90
PARTNER_QUALIFICATION_RULES = {
    "power_units_min": 10,
    "power_units_max": 50,
}
PARTNER_RESEARCH_INSTRUCTIONS_PATH = Path("/tmp/exa_run_dat_instructions.txt")


def _abort(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Org / brand / partner / contract resolution
# ---------------------------------------------------------------------------


async def _upsert_org() -> UUID:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id FROM business.organizations WHERE slug = %s",
                (ORG_SLUG,),
            )
            row = await cur.fetchone()
            if row:
                org_id = row[0]
                print(f"[org] existing slug='{ORG_SLUG}' id={org_id}")
                return org_id
            await cur.execute(
                """
                INSERT INTO business.organizations (name, slug, status, plan, metadata)
                VALUES (%s, %s, 'active', 'prototype', '{}'::jsonb)
                RETURNING id
                """,
                (ORG_NAME, ORG_SLUG),
            )
            row = await cur.fetchone()
        await conn.commit()
    org_id = row[0]
    print(f"[org] inserted slug='{ORG_SLUG}' id={org_id}")
    return org_id


async def _resolve_or_create_brand(org_id: UUID) -> UUID:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id FROM business.brands
                WHERE organization_id = %s AND name = %s
                """,
                (str(org_id), BRAND_NAME),
            )
            row = await cur.fetchone()
            if row:
                brand_id = row[0]
                print(f"[brand] existing name='{BRAND_NAME}' id={brand_id}")
                return brand_id
            await cur.execute(
                """
                INSERT INTO business.brands (organization_id, name, domain)
                VALUES (%s, %s, %s)
                RETURNING id
                """,
                (str(org_id), BRAND_NAME, BRAND_DOMAIN),
            )
            row = await cur.fetchone()
        await conn.commit()
    brand_id = row[0]
    print(f"[brand] inserted name='{BRAND_NAME}' id={brand_id}")
    return brand_id


async def _upsert_partner(org_id: UUID) -> UUID:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id FROM business.demand_side_partners
                WHERE organization_id = %s AND name = %s AND deleted_at IS NULL
                """,
                (str(org_id), PARTNER_NAME),
            )
            row = await cur.fetchone()
            if row:
                partner_id = row[0]
                print(f"[partner] existing name='{PARTNER_NAME}' id={partner_id}")
                return partner_id
            await cur.execute(
                """
                INSERT INTO business.demand_side_partners (
                    organization_id, name, domain,
                    primary_contact_name, primary_contact_email,
                    primary_phone, intro_email,
                    hours_of_operation_config, metadata
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
                RETURNING id
                """,
                (
                    str(org_id),
                    PARTNER_NAME,
                    PARTNER_DOMAIN,
                    "DAT Partner Lead",
                    "partner-lead@dat.com",
                    "+1-503-555-0100",
                    "partner-intro@dat.com",
                    json.dumps(
                        {
                            "timezone": "America/Los_Angeles",
                            "weekdays": {"start": "08:00", "end": "17:00"},
                        }
                    ),
                    json.dumps({"seed": True, "note": "DAT prototype seed"}),
                ),
            )
            row = await cur.fetchone()
        await conn.commit()
    partner_id = row[0]
    print(f"[partner] inserted name='{PARTNER_NAME}' id={partner_id}")
    return partner_id


async def _upsert_partner_contract(partner_id: UUID) -> UUID:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id FROM business.partner_contracts
                WHERE partner_id = %s
                  AND pricing_model = %s
                  AND status = 'active'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (str(partner_id), PARTNER_CONTRACT_PRICING),
            )
            row = await cur.fetchone()
            if row:
                contract_id = row[0]
                print(f"[contract] existing id={contract_id}")
                return contract_id
            await cur.execute(
                """
                INSERT INTO business.partner_contracts (
                    partner_id, pricing_model, amount_cents,
                    duration_days, max_capital_outlay_cents,
                    qualification_rules, terms_blob, status
                ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, 'active')
                RETURNING id
                """,
                (
                    str(partner_id),
                    PARTNER_CONTRACT_PRICING,
                    PARTNER_CONTRACT_AMOUNT_CENTS,
                    PARTNER_CONTRACT_DURATION_DAYS,
                    1_000_000,  # $10K cap on capital outlay per initiative
                    json.dumps(PARTNER_QUALIFICATION_RULES),
                    "Prototype contract: 90-day audience reservation, $25k flat fee.",
                ),
            )
            row = await cur.fetchone()
        await conn.commit()
    contract_id = row[0]
    print(f"[contract] inserted id={contract_id}")
    return contract_id


# ---------------------------------------------------------------------------
# DEX audience spec resolution
# ---------------------------------------------------------------------------


async def _resolve_audience_spec_id() -> UUID:
    """Find the DAT audience spec id in DEX by name. Falls back to the
    most-recent reservation in hq-x if DEX listing isn't available."""
    # First try the cached reservation row — it carries the spec id.
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT data_engine_audience_id
                FROM business.org_audience_reservations
                WHERE audience_name = %s
                ORDER BY reserved_at DESC
                LIMIT 1
                """,
                (DAT_AUDIENCE_SPEC_NAME,),
            )
            row = await cur.fetchone()
    if row:
        spec_id = UUID(str(row[0]))
        print(f"[audience] resolved from reservation cache: spec_id={spec_id}")
        return spec_id

    _abort(
        f"Could not find audience spec named {DAT_AUDIENCE_SPEC_NAME!r} in "
        "business.org_audience_reservations. Run "
        "`scripts/seed_dat_audience_reservation` first to materialize the "
        "DAT spec in DEX and the reservation row in hq-x."
    )
    raise SystemExit(1)  # unreachable, satisfies type-checker


# ---------------------------------------------------------------------------
# Partner-research Exa job
# ---------------------------------------------------------------------------


async def _fire_partner_research_job(
    *,
    org_id: UUID,
    partner_id: UUID,
) -> str:
    """Re-fire the partner-research Exa run durably as an
    exa_research_jobs row + exa.exa_calls archive. Returns the
    result_ref pointer."""
    if not PARTNER_RESEARCH_INSTRUCTIONS_PATH.exists():
        _abort(
            f"Partner-research instructions not found at "
            f"{PARTNER_RESEARCH_INSTRUCTIONS_PATH}. Re-fetch the prototype "
            "instructions from /tmp before running this seed."
        )
    instructions = PARTNER_RESEARCH_INSTRUCTIONS_PATH.read_text()
    objective_ref = f"partner:{partner_id}"
    idempotency = f"partner-research-{partner_id}"

    job = await exa_jobs_svc.create_job(
        organization_id=org_id,
        created_by_user_id=None,
        endpoint="research",
        destination="hqx",
        objective="partner_research",
        objective_ref=objective_ref,
        request_payload={"instructions": instructions},
        idempotency_key=idempotency,
    )
    print(
        f"[partner_research] job created id={job['id']} status={job['status']}"
    )

    if job["status"] in ("succeeded", "failed", "cancelled", "dead_lettered"):
        result_ref = job.get("result_ref")
        if not result_ref:
            _abort(
                f"partner-research job {job['id']} is terminal but has no "
                f"result_ref; status={job['status']}"
            )
        print(
            f"[partner_research] reusing existing terminal job: "
            f"result_ref={result_ref}"
        )
        return result_ref

    # Drive synchronously through the same internal endpoint Trigger
    # would call — bypasses Trigger for ergonomics; the path is
    # otherwise identical to production.
    print(
        f"[partner_research] driving job {job['id']} via internal endpoint…"
    )
    started = time.monotonic()
    result = await internal_exa_router.process_exa_job(
        job_id=job["id"], body={"trigger_run_id": "seed_script_partner_research"}
    )
    elapsed = time.monotonic() - started
    print(
        f"[partner_research] terminal: status={result.get('status')} "
        f"duration={elapsed:.1f}s"
    )
    if result.get("status") != "succeeded":
        _abort(
            f"partner-research job failed: {result.get('error')!r}"
        )
    return result["result_ref"]


# ---------------------------------------------------------------------------
# Subagent 1: strategic-context research
# ---------------------------------------------------------------------------


async def _run_strategic_research(initiative_id: UUID, org_id: UUID) -> None:
    print(
        f"[strategic_research] firing subagent 1 for initiative={initiative_id}…"
    )
    result = await strategic_context_researcher.run_strategic_context_research(
        initiative_id=initiative_id,
        organization_id=org_id,
        created_by_user_id=None,
    )
    exa_job_id = result["exa_job_id"]
    print(f"[strategic_research] exa_job_id={exa_job_id} status={result['status']}")

    # Drive the underlying exa job synchronously — bypass Trigger.
    print(
        f"[strategic_research] driving exa job {exa_job_id} via internal endpoint…"
    )
    started = time.monotonic()
    exa_result = await internal_exa_router.process_exa_job(
        job_id=exa_job_id,
        body={"trigger_run_id": "seed_script_strategic_research"},
    )
    elapsed = time.monotonic() - started
    print(
        f"[strategic_research] exa terminal: status={exa_result.get('status')} "
        f"duration={elapsed:.1f}s"
    )
    if exa_result.get("status") != "succeeded":
        _abort(
            f"strategic-context-research exa job failed: {exa_result.get('error')!r}"
        )

    # The post-process-by-objective dispatcher should have written the
    # result_ref onto the initiative + flipped status to
    # `strategic_research_ready`. Verify.
    initiative = await gtm_svc.get_initiative(initiative_id)
    if initiative is None:
        _abort(f"initiative {initiative_id} not found after strategic research")
    print(
        f"[strategic_research] initiative status={initiative['status']} "
        f"strategic_context_research_ref={initiative['strategic_context_research_ref']!r}"
    )
    if initiative["status"] != "strategic_research_ready":
        _abort(
            "expected initiative.status='strategic_research_ready'; got "
            f"{initiative['status']!r}"
        )


# ---------------------------------------------------------------------------
# Subagent 2: strategy synthesis
# ---------------------------------------------------------------------------


async def _run_synthesis(initiative_id: UUID) -> dict[str, Any]:
    # Transition the initiative to awaiting_strategy_synthesis (the
    # public router does this; mirroring it here since we bypass it).
    await gtm_svc.transition_status(
        initiative_id,
        new_status="awaiting_strategy_synthesis",
        history_event={"trigger": "seed_script"},
    )
    print(
        "[synthesis] initiative transitioned to awaiting_strategy_synthesis"
    )

    print("[synthesis] driving synthesizer via internal endpoint…")
    started = time.monotonic()
    result = await internal_gtm_router.process_synthesis(
        initiative_id=initiative_id,
        body={"trigger_run_id": "seed_script_synthesis"},
    )
    elapsed = time.monotonic() - started
    print(
        f"[synthesis] terminal: status={result.get('status')} "
        f"duration={elapsed:.1f}s"
    )
    if result.get("status") != "succeeded":
        _abort(
            f"synthesis failed: {result.get('error')!r}"
        )
    return result


# ---------------------------------------------------------------------------
# Pretty-printing the output
# ---------------------------------------------------------------------------


def _print_strategy_head(path: str, n_lines: int = 50) -> None:
    p = Path(path)
    if not p.exists():
        print(f"[strategy] WARNING: expected file at {path} but it does not exist")
        return
    lines = p.read_text().splitlines()
    head = "\n".join(lines[:n_lines])
    print(f"\n=== campaign_strategy.md (first {n_lines} lines) ===\n{head}")
    if len(lines) > n_lines:
        print(f"… ({len(lines) - n_lines} more lines truncated)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> int:
    if not settings.EXA_API_KEY:
        _abort("EXA_API_KEY is not set; aborting")
    if not settings.ANTHROPIC_API_KEY:
        _abort("ANTHROPIC_API_KEY is not set; aborting")
    if not settings.DEX_BASE_URL:
        print(
            "[warn] DEX_BASE_URL is not set; audience descriptor calls will "
            "fall through to a sentinel in the prompts (not fatal)."
        )

    await init_pool()
    overall_started = time.monotonic()
    try:
        org_id = await _upsert_org()
        brand_id = await _resolve_or_create_brand(org_id)
        partner_id = await _upsert_partner(org_id)
        contract_id = await _upsert_partner_contract(partner_id)
        audience_spec_id = await _resolve_audience_spec_id()

        partner_research_ref = await _fire_partner_research_job(
            org_id=org_id, partner_id=partner_id
        )

        initiative = await gtm_svc.create_initiative(
            organization_id=org_id,
            brand_id=brand_id,
            partner_id=partner_id,
            partner_contract_id=contract_id,
            data_engine_audience_id=audience_spec_id,
            partner_research_ref=partner_research_ref,
            metadata={"seed": True, "note": "seed_dat_gtm_initiative.py"},
        )
        initiative_id = initiative["id"]
        print(f"[initiative] created id={initiative_id} status={initiative['status']}")

        await _run_strategic_research(initiative_id, org_id)

        result = await _run_synthesis(initiative_id)

        # Re-read final state.
        final = await gtm_svc.get_initiative(initiative_id)

        elapsed = time.monotonic() - overall_started
        print()
        print("=== summary ===")
        print(f"initiative_id                  = {initiative_id}")
        print(f"partner_research_ref           = {partner_research_ref}")
        print(
            f"strategic_context_research_ref = "
            f"{(final or {}).get('strategic_context_research_ref')}"
        )
        print(f"campaign_strategy_path         = {result.get('path')}")
        print(f"final initiative status        = {(final or {}).get('status')}")
        print(f"model                          = {result.get('model')}")
        print(f"tokens_used                    = {result.get('tokens_used')}")
        print(
            f"cache_read_input_tokens        = "
            f"{result.get('cache_read_input_tokens')}"
        )
        print(
            f"cache_creation_input_tokens    = "
            f"{result.get('cache_creation_input_tokens')}"
        )
        print(f"total runtime                  = {elapsed:.1f}s")

        if result.get("path"):
            _print_strategy_head(result["path"])

        return 0
    finally:
        await close_pool()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

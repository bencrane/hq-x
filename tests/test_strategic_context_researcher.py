"""Tests for app.services.strategic_context_researcher.

Mocks every dependency at the module seam (initiative loader, partner /
contract / brand DB loaders, exa job creator, dex client) so the
researcher's prompt-rendering + idempotency-key + state-transition
behavior can be exercised without DB or HTTP.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.services import activation_jobs as jobs_svc
from app.services import dex_client
from app.services import exa_research_jobs as exa_jobs_svc
from app.services import gtm_initiatives as gtm_svc
from app.services import strategic_context_researcher as scr

ORG = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
INITIATIVE = UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
BRAND = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
PARTNER = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
CONTRACT = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
AUDIENCE = UUID("99999999-9999-9999-9999-999999999999")


@pytest.fixture
def stub(monkeypatch):
    state: dict[str, Any] = {
        "initiative_status": "draft",
        "exa_create_calls": [],
        "enqueue_calls": [],
        "transitions": [],
        "exa_job_existing_run": None,
    }

    initiative_row = {
        "id": INITIATIVE,
        "organization_id": ORG,
        "brand_id": BRAND,
        "partner_id": PARTNER,
        "partner_contract_id": CONTRACT,
        "data_engine_audience_id": AUDIENCE,
        "partner_research_ref": "hqx://exa.exa_calls/00000000-0000-0000-0000-000000000001",
        "strategic_context_research_ref": None,
        "campaign_strategy_path": None,
        "status": state["initiative_status"],
        "history": [],
        "metadata": {},
        "reservation_window_start": None,
        "reservation_window_end": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }

    async def fake_get_initiative(initiative_id, *, organization_id=None):
        if initiative_id != INITIATIVE:
            return None
        initiative_row["status"] = state["initiative_status"]
        return initiative_row

    async def fake_load_partner(partner_id):
        return {
            "id": PARTNER,
            "name": "DAT",
            "domain": "dat.com",
            "primary_contact_name": "Lead",
            "primary_contact_email": "lead@dat.com",
            "primary_phone": "+1-503-555-0100",
            "intro_email": "intro@dat.com",
            "hours_of_operation_config": {},
            "metadata": {},
        }

    async def fake_load_contract(contract_id):
        return {
            "id": CONTRACT,
            "partner_id": PARTNER,
            "pricing_model": "flat_90d",
            "amount_cents": 2_500_000,
            "duration_days": 90,
            "max_capital_outlay_cents": 1_000_000,
            "qualification_rules": {"power_units_min": 10, "power_units_max": 50},
            "terms_blob": None,
            "status": "active",
            "starts_at": None,
            "ends_at": None,
        }

    async def fake_load_brand(brand_id):
        return {"id": BRAND, "name": "Capital Expansion", "domain": "capitalexpansion.com"}

    async def fake_get_audience_descriptor(spec_id, *, bearer_token=None):
        return {
            "spec": {"name": "DAT — fast-growing carriers (prototype)"},
            "template": {
                "name": "Fast-growing carriers (FMCSA, 90d)",
                "slug": "fmcsa-fast-growing-carriers",
                "description": "FMCSA carriers with rapid power-unit growth in 90d.",
            },
            "audience_attributes": [
                {
                    "key": "power_unit_count",
                    "schema": {"type": "integer"},
                    "value": {"min": 10, "max": 50},
                }
            ],
        }

    async def fake_fetch_partner_research_payload(ref):
        if ref is None:
            return None
        return {
            "output": {
                "content": "DAT is North America's largest freight marketplace. "
                "Their target market is freight brokers and motor carriers.",
            }
        }

    async def fake_create_exa_job(**kwargs):
        state["exa_create_calls"].append(kwargs)
        existing_run = state["exa_job_existing_run"]
        return {
            "id": uuid4(),
            "organization_id": kwargs["organization_id"],
            "endpoint": kwargs["endpoint"],
            "destination": kwargs["destination"],
            "objective": kwargs["objective"],
            "objective_ref": kwargs["objective_ref"],
            "request_payload": kwargs["request_payload"],
            "idempotency_key": kwargs.get("idempotency_key"),
            "status": "queued",
            "trigger_run_id": existing_run,
        }

    async def fake_enqueue(*, task_identifier, payload_override=None, **kwargs):
        state["enqueue_calls"].append(
            {"task_identifier": task_identifier, "payload": payload_override}
        )
        return "run_test_xyz"

    async def fake_update_run_id(job_id, run_id):
        state["enqueue_calls"][-1]["run_id"] = run_id

    async def fake_transition(initiative_id, *, new_status, history_event=None):
        state["transitions"].append(new_status)
        state["initiative_status"] = new_status
        return {**initiative_row, "status": new_status}

    async def fake_mark_failed(job_id, error):
        state["transitions"].append(("exa_failed", error))

    monkeypatch.setattr(gtm_svc, "get_initiative", fake_get_initiative)
    monkeypatch.setattr(scr.gtm_svc, "get_initiative", fake_get_initiative)
    monkeypatch.setattr(scr, "_load_partner", fake_load_partner)
    monkeypatch.setattr(scr, "_load_partner_contract", fake_load_contract)
    monkeypatch.setattr(scr, "_load_brand", fake_load_brand)
    monkeypatch.setattr(
        scr, "_fetch_partner_research_payload", fake_fetch_partner_research_payload
    )
    monkeypatch.setattr(
        dex_client, "get_audience_descriptor", fake_get_audience_descriptor
    )
    monkeypatch.setattr(
        scr.dex_client, "get_audience_descriptor", fake_get_audience_descriptor
    )
    monkeypatch.setattr(exa_jobs_svc, "create_job", fake_create_exa_job)
    monkeypatch.setattr(scr.exa_jobs_svc, "create_job", fake_create_exa_job)
    monkeypatch.setattr(exa_jobs_svc, "update_trigger_run_id", fake_update_run_id)
    monkeypatch.setattr(scr.exa_jobs_svc, "update_trigger_run_id", fake_update_run_id)
    monkeypatch.setattr(exa_jobs_svc, "mark_failed", fake_mark_failed)
    monkeypatch.setattr(scr.exa_jobs_svc, "mark_failed", fake_mark_failed)
    monkeypatch.setattr(jobs_svc, "enqueue_via_trigger", fake_enqueue)
    monkeypatch.setattr(scr.jobs_svc, "enqueue_via_trigger", fake_enqueue)
    monkeypatch.setattr(gtm_svc, "transition_status", fake_transition)
    monkeypatch.setattr(scr.gtm_svc, "transition_status", fake_transition)

    return state


# ── happy path ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_creates_job_and_transitions_initiative(stub):
    result = await scr.run_strategic_context_research(
        initiative_id=INITIATIVE,
        organization_id=ORG,
        created_by_user_id=None,
    )

    assert "exa_job_id" in result
    assert result["status"] == "queued"
    # One exa job created.
    assert len(stub["exa_create_calls"]) == 1
    create_kwargs = stub["exa_create_calls"][0]
    assert create_kwargs["endpoint"] == "research"
    assert create_kwargs["destination"] == "hqx"
    assert create_kwargs["objective"] == "strategic_context_research"
    assert create_kwargs["objective_ref"] == f"initiative:{INITIATIVE}"
    assert create_kwargs["idempotency_key"] == f"strategic-context-{INITIATIVE}"

    # The state machine should have flipped to awaiting_strategic_research.
    assert "awaiting_strategic_research" in stub["transitions"]


# ── prompt content invariants ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rendered_instruction_includes_audience_brand_partner(stub):
    await scr.run_strategic_context_research(
        initiative_id=INITIATIVE,
        organization_id=ORG,
        created_by_user_id=None,
    )
    instructions = stub["exa_create_calls"][0]["request_payload"]["instructions"]
    # Brand surface
    assert "Capital Expansion" in instructions
    # Audience surface
    assert "DAT" in instructions
    assert "fast-growing carriers" in instructions or "FMCSA" in instructions
    # Time-window scope (operator-voice + recent)
    assert "6–12 months" in instructions or "operator-voice" in instructions.lower()
    # Partner-research summary slot was filled (fake returns the freight
    # marketplace blurb).
    assert "freight" in instructions.lower()


# ── trigger enqueue ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_enqueues_trigger_task_when_no_existing_run(stub):
    await scr.run_strategic_context_research(
        initiative_id=INITIATIVE,
        organization_id=ORG,
        created_by_user_id=None,
    )
    assert len(stub["enqueue_calls"]) == 1
    assert stub["enqueue_calls"][0]["task_identifier"] == (
        "exa.process_research_job"
    )


@pytest.mark.asyncio
async def test_run_skips_enqueue_on_idempotency_replay(stub):
    # Simulate the existing exa job already having a trigger run id.
    stub["exa_job_existing_run"] = "run_already_enqueued"
    await scr.run_strategic_context_research(
        initiative_id=INITIATIVE,
        organization_id=ORG,
        created_by_user_id=None,
    )
    assert stub["enqueue_calls"] == []

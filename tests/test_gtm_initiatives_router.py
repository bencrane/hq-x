"""Tests for /api/v1/initiatives.

Auth uses FastAPI dependency_overrides. The gtm_initiatives service +
strategic_context_researcher + activation_jobs.enqueue_via_trigger are
all stubbed so the async-202 contracts and 409-when-no-research case
can be exercised without a real DB or Trigger.dev.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from app.auth.roles import require_org_context
from app.auth.supabase_jwt import UserContext, verify_supabase_jwt
from app.main import app
from app.routers import gtm_initiatives as router_mod
from app.services import activation_jobs as jobs_svc
from app.services import gtm_initiatives as gtm_svc
from app.services import strategic_context_researcher

ORG = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ORG_OTHER = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
USER = UUID("11111111-1111-1111-1111-111111111111")


def _user(org_id: UUID | None) -> UserContext:
    return UserContext(
        auth_user_id=USER,
        business_user_id=USER,
        email="op@example.com",
        platform_role="platform_operator",
        active_organization_id=org_id,
        org_role=None,
        role="operator",
        client_id=None,
    )


@pytest.fixture
def auth_org_a():
    user = _user(ORG)
    app.dependency_overrides[verify_supabase_jwt] = lambda: user
    app.dependency_overrides[require_org_context] = lambda: user
    yield
    app.dependency_overrides.clear()


def _initiative_row(
    *,
    id_: UUID | None = None,
    organization_id: UUID = ORG,
    status: str = "draft",
    strategic_context_research_ref: str | None = None,
    partner_research_ref: str | None = None,
    campaign_strategy_path: str | None = None,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "id": id_ or uuid4(),
        "organization_id": organization_id,
        "brand_id": uuid4(),
        "partner_id": uuid4(),
        "partner_contract_id": uuid4(),
        "data_engine_audience_id": uuid4(),
        "partner_research_ref": partner_research_ref,
        "strategic_context_research_ref": strategic_context_research_ref,
        "campaign_strategy_path": campaign_strategy_path,
        "status": status,
        "history": [],
        "metadata": {},
        "reservation_window_start": None,
        "reservation_window_end": None,
        "created_at": now,
        "updated_at": now,
    }


@pytest.fixture
def stub(monkeypatch):
    state: dict[str, Any] = {
        "initiatives": {},
        "create_calls": [],
        "transitions": [],
        "research_calls": [],
        "enqueue_calls": [],
        "history_appends": [],
        "research_should_raise": False,
        "enqueue_should_raise": False,
    }

    async def fake_create_initiative(**kwargs):
        state["create_calls"].append(kwargs)
        row = _initiative_row(
            organization_id=kwargs["organization_id"],
            partner_research_ref=kwargs.get("partner_research_ref"),
        )
        state["initiatives"][row["id"]] = row
        return row

    async def fake_get_initiative(initiative_id, *, organization_id=None):
        row = state["initiatives"].get(initiative_id)
        if row is None:
            return None
        if organization_id is not None and row["organization_id"] != organization_id:
            return None
        return row

    async def fake_transition_status(
        initiative_id, *, new_status, history_event=None
    ):
        state["transitions"].append((str(initiative_id), new_status))
        row = state["initiatives"][initiative_id]
        row["status"] = new_status
        return row

    async def fake_append_history(initiative_id, event):
        state["history_appends"].append((str(initiative_id), event))

    async def fake_run_strategic(*, initiative_id, organization_id, created_by_user_id):
        state["research_calls"].append(initiative_id)
        if state["research_should_raise"]:
            raise strategic_context_researcher.StrategicContextResearcherError(
                "boom"
            )
        # Mirror the production path: transition the initiative.
        await fake_transition_status(
            initiative_id, new_status="awaiting_strategic_research"
        )
        return {"exa_job_id": uuid4(), "status": "queued"}

    async def fake_enqueue(*, task_identifier, payload_override=None, **kwargs):
        state["enqueue_calls"].append(
            {"task_identifier": task_identifier, "payload": payload_override}
        )
        if state["enqueue_should_raise"]:
            raise jobs_svc.TriggerEnqueueError("trigger.dev down")
        return "run_xyz"

    monkeypatch.setattr(gtm_svc, "create_initiative", fake_create_initiative)
    monkeypatch.setattr(gtm_svc, "get_initiative", fake_get_initiative)
    monkeypatch.setattr(gtm_svc, "transition_status", fake_transition_status)
    monkeypatch.setattr(gtm_svc, "append_history", fake_append_history)
    monkeypatch.setattr(router_mod.gtm_svc, "create_initiative", fake_create_initiative)
    monkeypatch.setattr(router_mod.gtm_svc, "get_initiative", fake_get_initiative)
    monkeypatch.setattr(router_mod.gtm_svc, "transition_status", fake_transition_status)
    monkeypatch.setattr(router_mod.gtm_svc, "append_history", fake_append_history)
    monkeypatch.setattr(
        strategic_context_researcher,
        "run_strategic_context_research",
        fake_run_strategic,
    )
    monkeypatch.setattr(
        router_mod.strategic_context_researcher,
        "run_strategic_context_research",
        fake_run_strategic,
    )
    monkeypatch.setattr(jobs_svc, "enqueue_via_trigger", fake_enqueue)
    monkeypatch.setattr(router_mod.jobs_svc, "enqueue_via_trigger", fake_enqueue)
    return state


async def _request(method: str, path: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=30.0
    ) as c:
        return await c.request(method, path, **kwargs)


# ── POST / ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_initiative_returns_201(auth_org_a, stub):
    body = {
        "brand_id": str(uuid4()),
        "partner_id": str(uuid4()),
        "partner_contract_id": str(uuid4()),
        "data_engine_audience_id": str(uuid4()),
        "partner_research_ref": "hqx://exa.exa_calls/abc",
    }
    resp = await _request("POST", "/api/v1/initiatives", json=body)
    assert resp.status_code == 201, resp.text
    payload = resp.json()
    assert payload["status"] == "draft"
    assert payload["organization_id"] == str(ORG)
    assert payload["partner_research_ref"] == "hqx://exa.exa_calls/abc"
    assert len(stub["create_calls"]) == 1


# ── GET /{id} ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_initiative_cross_org_returns_404(auth_org_a, stub):
    foreign = _initiative_row(organization_id=ORG_OTHER)
    stub["initiatives"][foreign["id"]] = foreign
    resp = await _request("GET", f"/api/v1/initiatives/{foreign['id']}")
    assert resp.status_code == 404


# ── POST /{id}/run-strategic-research ──────────────────────────────────────


@pytest.mark.asyncio
async def test_run_strategic_research_returns_202(auth_org_a, stub):
    initiative = _initiative_row(status="draft")
    stub["initiatives"][initiative["id"]] = initiative

    resp = await _request(
        "POST", f"/api/v1/initiatives/{initiative['id']}/run-strategic-research"
    )
    assert resp.status_code == 202, resp.text
    payload = resp.json()
    assert payload["status"] == "queued"
    assert "exa_job_id" in payload
    assert initiative["id"] in stub["research_calls"]
    # The fake transitioned the initiative.
    assert (
        str(initiative["id"]),
        "awaiting_strategic_research",
    ) in stub["transitions"]


@pytest.mark.asyncio
async def test_run_strategic_research_refuses_invalid_state(auth_org_a, stub):
    # ready_to_launch is not a valid starting state for strategic research.
    initiative = _initiative_row(status="ready_to_launch")
    stub["initiatives"][initiative["id"]] = initiative

    resp = await _request(
        "POST", f"/api/v1/initiatives/{initiative['id']}/run-strategic-research"
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_run_strategic_research_503_on_failure(auth_org_a, stub):
    initiative = _initiative_row(status="draft")
    stub["initiatives"][initiative["id"]] = initiative
    stub["research_should_raise"] = True

    resp = await _request(
        "POST", f"/api/v1/initiatives/{initiative['id']}/run-strategic-research"
    )
    assert resp.status_code == 503


# ── POST /{id}/synthesize-strategy ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_synthesize_returns_202(auth_org_a, stub):
    initiative = _initiative_row(
        status="strategic_research_ready",
        strategic_context_research_ref="hqx://exa.exa_calls/zzz",
    )
    stub["initiatives"][initiative["id"]] = initiative

    resp = await _request(
        "POST", f"/api/v1/initiatives/{initiative['id']}/synthesize-strategy"
    )
    assert resp.status_code == 202, resp.text
    payload = resp.json()
    assert payload["status"] == "queued"
    assert (
        str(initiative["id"]),
        "awaiting_strategy_synthesis",
    ) in stub["transitions"]
    assert len(stub["enqueue_calls"]) == 1
    assert stub["enqueue_calls"][0]["task_identifier"] == (
        "gtm.synthesize_initiative_strategy"
    )


@pytest.mark.asyncio
async def test_synthesize_409_when_no_strategic_research(auth_org_a, stub):
    initiative = _initiative_row(
        status="strategic_research_ready",
        strategic_context_research_ref=None,  # subagent 1 hasn't completed
    )
    stub["initiatives"][initiative["id"]] = initiative

    resp = await _request(
        "POST", f"/api/v1/initiatives/{initiative['id']}/synthesize-strategy"
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_synthesize_409_when_initiative_in_wrong_state(auth_org_a, stub):
    # draft is not a valid starting state for synthesis.
    initiative = _initiative_row(
        status="draft",
        strategic_context_research_ref="hqx://exa.exa_calls/zzz",
    )
    stub["initiatives"][initiative["id"]] = initiative

    resp = await _request(
        "POST", f"/api/v1/initiatives/{initiative['id']}/synthesize-strategy"
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_synthesize_503_when_trigger_enqueue_fails(auth_org_a, stub):
    initiative = _initiative_row(
        status="strategic_research_ready",
        strategic_context_research_ref="hqx://exa.exa_calls/zzz",
    )
    stub["initiatives"][initiative["id"]] = initiative
    stub["enqueue_should_raise"] = True

    resp = await _request(
        "POST", f"/api/v1/initiatives/{initiative['id']}/synthesize-strategy"
    )
    assert resp.status_code == 503

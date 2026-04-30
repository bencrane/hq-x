"""Tests for the async POST /api/v1/dmaas/campaigns + /jobs endpoints.

Slice 1 of the orchestration directive moved the endpoint behind an
``activation_jobs`` row + Trigger.dev task. The router itself is now
~stateless: it validates, persists a job, and enqueues. Pipeline
execution lives in ``app.services.dmaas_campaign_activation`` and is
covered by the internal-endpoint tests.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest

from app.auth.roles import require_org_context
from app.auth.supabase_jwt import UserContext, verify_supabase_jwt
from app.main import app
from app.models.activation_jobs import ActivationJobResponse
from app.routers import dmaas_campaigns as router_mod
from app.services import activation_jobs as jobs_svc
from app.services import campaigns as campaigns_svc

ORG = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ORG_OTHER = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
USER = UUID("11111111-1111-1111-1111-111111111111")
BRAND = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
JOB = UUID("99999999-9999-9999-9999-999999999999")


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


def _job_response(
    *,
    organization_id: UUID = ORG,
    status: str = "queued",
    trigger_run_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> ActivationJobResponse:
    now = datetime.now(UTC)
    return ActivationJobResponse(
        id=JOB,
        organization_id=organization_id,
        brand_id=BRAND,
        kind="dmaas_campaign_activation",
        status=status,
        idempotency_key=None,
        payload=payload or {},
        result=None,
        error=None,
        history=[],
        trigger_run_id=trigger_run_id,
        attempts=0,
        created_at=now,
        started_at=None,
        completed_at=None,
        dead_lettered_at=None,
    )


@pytest.fixture
def stub_jobs(monkeypatch):
    """Stub activation_jobs + brand check so we can assert async wiring
    without DB or Trigger.dev."""
    state: dict[str, Any] = {
        "create_calls": [],
        "enqueue_calls": [],
        "transition_calls": [],
        "cancel_calls": [],
        "get_calls": [],
        "current_job": _job_response(),
        "enqueue_should_raise": False,
    }

    async def fake_assert_brand(*, brand_id, organization_id):
        return None

    async def fake_create_job(**kwargs):
        state["create_calls"].append(kwargs)
        return state["current_job"]

    async def fake_enqueue(**kwargs):
        state["enqueue_calls"].append(kwargs)
        if state["enqueue_should_raise"]:
            raise jobs_svc.TriggerEnqueueError("trigger.dev down")
        return "run_test_abc"

    async def fake_transition(**kwargs):
        state["transition_calls"].append(kwargs)
        # Reflect transition into the in-memory current_job so subsequent
        # reads see the new status / trigger_run_id.
        existing = state["current_job"]
        new = _job_response(
            organization_id=existing.organization_id,
            status=kwargs.get("status", existing.status),
            trigger_run_id=kwargs.get("trigger_run_id") or existing.trigger_run_id,
            payload=existing.payload,
        )
        state["current_job"] = new
        return new

    async def fake_get_job(*, job_id, organization_id=None):
        state["get_calls"].append({"job_id": job_id, "organization_id": organization_id})
        if organization_id is not None and organization_id != state["current_job"].organization_id:
            raise jobs_svc.ActivationJobNotFound(f"job {job_id}")
        return state["current_job"]

    async def fake_cancel_job(**kwargs):
        state["cancel_calls"].append(kwargs)
        existing = state["current_job"]
        new = _job_response(
            organization_id=existing.organization_id,
            status="cancelled",
            trigger_run_id=existing.trigger_run_id,
            payload=existing.payload,
        )
        state["current_job"] = new
        return new

    monkeypatch.setattr(campaigns_svc, "assert_brand_in_organization", fake_assert_brand)
    monkeypatch.setattr(
        router_mod.campaigns_svc, "assert_brand_in_organization", fake_assert_brand
    )
    monkeypatch.setattr(jobs_svc, "create_job", fake_create_job)
    monkeypatch.setattr(jobs_svc, "enqueue_via_trigger", fake_enqueue)
    monkeypatch.setattr(jobs_svc, "transition_job", fake_transition)
    monkeypatch.setattr(jobs_svc, "get_job", fake_get_job)
    monkeypatch.setattr(jobs_svc, "cancel_job", fake_cancel_job)
    monkeypatch.setattr(router_mod.jobs_svc, "create_job", fake_create_job)
    monkeypatch.setattr(router_mod.jobs_svc, "enqueue_via_trigger", fake_enqueue)
    monkeypatch.setattr(router_mod.jobs_svc, "transition_job", fake_transition)
    monkeypatch.setattr(router_mod.jobs_svc, "get_job", fake_get_job)
    monkeypatch.setattr(router_mod.jobs_svc, "cancel_job", fake_cancel_job)
    return state


def _request_body_with_landing_page() -> dict[str, Any]:
    return {
        "name": "Q2 lapsed",
        "brand_id": str(BRAND),
        "send_date": "2026-05-15",
        "creative": {
            "lob_creative_payload": {"front_html": "<html/>", "back_html": "<html/>"}
        },
        "use_landing_page": True,
        "landing_page": {
            "headline": "Hi {recipient.display_name}",
            "body": "We pulled your data.",
            "cta": {
                "type": "form",
                "label": "Schedule",
                "form_schema": {
                    "fields": [
                        {"name": "name", "label": "Name", "type": "text", "required": True},
                        {"name": "email", "label": "Email", "type": "email", "required": True},
                    ]
                },
                "thank_you_message": "Thanks!",
            },
        },
        "recipients": [
            {
                "external_source": "fmcsa",
                "external_id": "100001",
                "display_name": "ACME Trucking",
                "mailing_address": {"city": "Brooklyn"},
            },
            {
                "external_source": "fmcsa",
                "external_id": "100002",
                "display_name": "Fleet Co",
                "mailing_address": {"city": "Queens"},
            },
        ],
    }


async def _request(method: str, path: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=30.0
    ) as c:
        return await c.request(method, path, **kwargs)


# ── Happy path ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_returns_202_with_job_id(monkeypatch, auth_org_a, stub_jobs):
    body = _request_body_with_landing_page()
    resp = await _request("POST", "/api/v1/dmaas/campaigns", json=body)

    assert resp.status_code == 202, resp.text
    payload = resp.json()
    assert payload["job_id"] == str(JOB)
    assert payload["status"] == "queued"

    # Enqueue + transition (with trigger_run_id) called exactly once.
    assert len(stub_jobs["create_calls"]) == 1
    assert len(stub_jobs["enqueue_calls"]) == 1
    transitions = stub_jobs["transition_calls"]
    assert any(t.get("trigger_run_id") == "run_test_abc" for t in transitions)


@pytest.mark.asyncio
async def test_post_persists_payload_with_recipients(
    monkeypatch, auth_org_a, stub_jobs
):
    body = _request_body_with_landing_page()
    await _request("POST", "/api/v1/dmaas/campaigns", json=body)
    create_kwargs = stub_jobs["create_calls"][0]
    assert create_kwargs["organization_id"] == ORG
    assert create_kwargs["brand_id"] == BRAND
    assert create_kwargs["kind"] == "dmaas_campaign_activation"
    persisted_payload = create_kwargs["payload"]
    assert persisted_payload["name"] == "Q2 lapsed"
    assert len(persisted_payload["recipients"]) == 2
    assert persisted_payload["recipients"][0]["external_id"] == "100001"
    assert persisted_payload["use_landing_page"] is True
    assert persisted_payload["landing_page_config"]["headline"].startswith("Hi {")


@pytest.mark.asyncio
async def test_idempotency_key_returns_existing_job(
    monkeypatch, auth_org_a, stub_jobs
):
    # Pre-set current_job to look like a replay where trigger_run_id is
    # already populated — the router must NOT re-enqueue.
    stub_jobs["current_job"] = _job_response(
        status="running", trigger_run_id="run_existing"
    )

    body = _request_body_with_landing_page()
    resp = await _request(
        "POST",
        "/api/v1/dmaas/campaigns",
        json=body,
        headers={"Idempotency-Key": "abc-123"},
    )
    assert resp.status_code == 202
    body_json = resp.json()
    assert body_json["job_id"] == str(JOB)
    assert body_json["status"] == "running"
    assert stub_jobs["enqueue_calls"] == []


# ── Validation ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_landing_page_required_when_use_landing_page_true(
    auth_org_a, stub_jobs
):
    body = _request_body_with_landing_page()
    body["landing_page"] = None
    resp = await _request("POST", "/api/v1/dmaas/campaigns", json=body)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_destination_url_required_when_use_landing_page_false(
    auth_org_a, stub_jobs
):
    body = _request_body_with_landing_page()
    body["use_landing_page"] = False
    body["landing_page"] = None
    body["destination_url_override"] = None
    resp = await _request("POST", "/api/v1/dmaas/campaigns", json=body)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_recipients_cap_enforced(auth_org_a, stub_jobs):
    body = _request_body_with_landing_page()
    body["recipients"] = [
        {
            "external_source": "fmcsa",
            "external_id": str(i),
            "display_name": "x",
            "mailing_address": {},
        }
        for i in range(50_001)
    ]
    resp = await _request("POST", "/api/v1/dmaas/campaigns", json=body)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_empty_recipients_rejected(auth_org_a, stub_jobs):
    body = _request_body_with_landing_page()
    body["recipients"] = []
    resp = await _request("POST", "/api/v1/dmaas/campaigns", json=body)
    assert resp.status_code == 422


# ── Cross-org guard ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_brand_in_other_org_returns_404(monkeypatch, auth_org_a, stub_jobs):
    async def boom(*, brand_id, organization_id):
        raise campaigns_svc.CampaignBrandMismatch("brand not in org")

    monkeypatch.setattr(campaigns_svc, "assert_brand_in_organization", boom)
    monkeypatch.setattr(router_mod.campaigns_svc, "assert_brand_in_organization", boom)

    body = _request_body_with_landing_page()
    resp = await _request("POST", "/api/v1/dmaas/campaigns", json=body)
    assert resp.status_code == 404


# ── Enqueue failure ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_enqueue_failure_returns_503(monkeypatch, auth_org_a, stub_jobs):
    stub_jobs["enqueue_should_raise"] = True
    body = _request_body_with_landing_page()
    resp = await _request("POST", "/api/v1/dmaas/campaigns", json=body)
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert detail["error"] == "job_enqueue_failed"
    assert detail["job_id"] == str(JOB)
    # The job row was transitioned to failed.
    failed_transitions = [
        t for t in stub_jobs["transition_calls"] if t.get("status") == "failed"
    ]
    assert len(failed_transitions) == 1


# ── GET /jobs/{id} ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_job_returns_row(auth_org_a, stub_jobs):
    resp = await _request("GET", f"/api/v1/dmaas/jobs/{JOB}")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["id"] == str(JOB)
    assert payload["organization_id"] == str(ORG)


@pytest.mark.asyncio
async def test_get_job_cross_org_returns_404(auth_org_a, stub_jobs):
    # Set the stored job to belong to a different org.
    stub_jobs["current_job"] = _job_response(organization_id=ORG_OTHER)
    resp = await _request("GET", f"/api/v1/dmaas/jobs/{JOB}")
    assert resp.status_code == 404


# ── POST /jobs/{id}/cancel ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_job_transitions_to_cancelled(auth_org_a, stub_jobs):
    resp = await _request("POST", f"/api/v1/dmaas/jobs/{JOB}/cancel")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "cancelled"
    assert len(stub_jobs["cancel_calls"]) == 1


@pytest.mark.asyncio
async def test_cancel_job_already_terminal_returns_409(
    monkeypatch, auth_org_a, stub_jobs
):
    async def boom(**kwargs):
        raise jobs_svc.ActivationJobInvalidTransition("cannot cancel")

    monkeypatch.setattr(jobs_svc, "cancel_job", boom)
    monkeypatch.setattr(router_mod.jobs_svc, "cancel_job", boom)

    resp = await _request("POST", f"/api/v1/dmaas/jobs/{JOB}/cancel")
    assert resp.status_code == 409

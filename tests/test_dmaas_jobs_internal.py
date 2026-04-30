"""Tests for /internal/dmaas/process-job (the Trigger.dev callback)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest

from app.main import app
from app.models.activation_jobs import ActivationJobResponse
from app.routers.internal import dmaas_jobs as router_mod
from app.services import activation_jobs as jobs_svc
from app.services import dmaas_campaign_activation as activation_mod

JOB_ID = UUID("99999999-9999-9999-9999-999999999999")
ORG = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
BRAND = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def _job(
    *,
    status: str = "queued",
    kind: str = "dmaas_campaign_activation",
    payload: dict[str, Any] | None = None,
) -> ActivationJobResponse:
    now = datetime.now(UTC)
    return ActivationJobResponse(
        id=JOB_ID,
        organization_id=ORG,
        brand_id=BRAND,
        kind=kind,
        status=status,
        idempotency_key=None,
        payload=payload or {
            "name": "Test",
            "brand_id": str(BRAND),
            "send_date": None,
            "description": None,
            "creative_payload": {},
            "use_landing_page": False,
            "landing_page_config": None,
            "destination_url_override": "https://acme.com/promo",
            "recipients": [
                {
                    "external_source": "fmcsa",
                    "external_id": "1",
                    "display_name": "Acme",
                    "mailing_address": {},
                }
            ],
            "user_id": None,
        },
        result=None,
        error=None,
        history=[],
        trigger_run_id=None,
        attempts=0,
        created_at=now,
        started_at=None,
        completed_at=None,
        dead_lettered_at=None,
    )


@pytest.fixture
def stub_jobs(monkeypatch):
    """Stub jobs_svc.get_job + transition_job + activation runner."""
    state: dict[str, Any] = {
        "current_job": _job(),
        "transitions": [],
        "histories": [],
        "activation_should_raise": None,
    }

    async def fake_get_job(*, job_id, organization_id=None):
        return state["current_job"]

    async def fake_transition(**kwargs):
        state["transitions"].append(kwargs)
        existing = state["current_job"]
        new = ActivationJobResponse(
            **{
                **existing.model_dump(),
                "status": kwargs.get("status", existing.status),
                "trigger_run_id": kwargs.get("trigger_run_id") or existing.trigger_run_id,
                "result": kwargs.get("result") or existing.result,
                "error": kwargs.get("error") or existing.error,
            }
        )
        state["current_job"] = new
        return new

    async def fake_append_history(**kwargs):
        state["histories"].append(kwargs)

    async def fake_run_campaign_activation(**kwargs):
        if state["activation_should_raise"] is not None:
            raise state["activation_should_raise"]
        return {
            "campaign_id": "00000000-0000-0000-0000-000000000001",
            "channel_campaign_id": "00000000-0000-0000-0000-000000000002",
            "step_id": "00000000-0000-0000-0000-000000000003",
            "external_provider_id": "lob_cmp_x",
            "scheduled_send_at": None,
            "recipient_count": 1,
            "landing_page_url": None,
            "status": "scheduled",
        }

    monkeypatch.setattr(jobs_svc, "get_job", fake_get_job)
    monkeypatch.setattr(jobs_svc, "transition_job", fake_transition)
    monkeypatch.setattr(jobs_svc, "append_history", fake_append_history)
    monkeypatch.setattr(router_mod.jobs_svc, "get_job", fake_get_job)
    monkeypatch.setattr(router_mod.jobs_svc, "transition_job", fake_transition)
    monkeypatch.setattr(router_mod.jobs_svc, "append_history", fake_append_history)
    monkeypatch.setattr(
        activation_mod, "run_campaign_activation", fake_run_campaign_activation
    )
    monkeypatch.setattr(
        router_mod, "run_campaign_activation", fake_run_campaign_activation
    )
    return state


async def _post_internal(
    body: dict[str, Any], *, headers: dict[str, str] | None = None
) -> httpx.Response:
    if headers is None:
        headers = {"Authorization": "Bearer test-trigger-secret"}
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=30.0
    ) as c:
        return await c.post(
            "/internal/dmaas/process-job",
            json=body,
            headers=headers,
        )


# ── Auth ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_job_requires_secret(stub_jobs):
    resp = await _post_internal({"job_id": str(JOB_ID)}, headers={})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_process_job_rejects_wrong_secret(stub_jobs):
    resp = await _post_internal(
        {"job_id": str(JOB_ID)},
        headers={"Authorization": "Bearer wrong"},
    )
    assert resp.status_code == 401


# ── Happy path ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_job_runs_activation_and_succeeds(stub_jobs):
    resp = await _post_internal({"job_id": str(JOB_ID), "trigger_run_id": "run_abc"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "succeeded"
    # First transition is to running, last is to succeeded.
    statuses = [t.get("status") for t in stub_jobs["transitions"]]
    assert statuses[0] == "running"
    assert statuses[-1] == "succeeded"
    # trigger_run_id was persisted on the running transition.
    running_t = next(t for t in stub_jobs["transitions"] if t.get("status") == "running")
    assert running_t.get("trigger_run_id") == "run_abc"
    assert running_t.get("increment_attempts") is True


@pytest.mark.asyncio
async def test_process_job_idempotent_when_already_terminal(stub_jobs):
    stub_jobs["current_job"] = _job(status="succeeded")
    resp = await _post_internal({"job_id": str(JOB_ID)})
    assert resp.status_code == 200
    body = resp.json()
    assert body["skipped"] is True
    assert body["reason"] == "job_already_terminal"
    # Should not transition again.
    assert stub_jobs["transitions"] == []


# ── Failure modes ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_job_business_failure_marks_failed_no_raise(stub_jobs):
    stub_jobs["activation_should_raise"] = activation_mod.DMaaSActivationError(
        "voice not wired",
        error_code="activation_not_implemented",
        detail={"step_id": "abc"},
    )
    resp = await _post_internal({"job_id": str(JOB_ID)})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed"
    assert body["error"] == "activation_not_implemented"
    failed_t = next(
        t for t in stub_jobs["transitions"] if t.get("status") == "failed"
    )
    assert failed_t["error"]["reason"] == "activation_not_implemented"


@pytest.mark.asyncio
async def test_process_job_transient_failure_re_raises_for_retry(stub_jobs):
    stub_jobs["activation_should_raise"] = RuntimeError("DB blip")
    resp = await _post_internal({"job_id": str(JOB_ID)})
    # Re-raised → FastAPI returns 500.
    assert resp.status_code == 500
    # Job remained in 'running' (no terminal transition); history got an entry.
    assert len(stub_jobs["histories"]) == 1
    assert stub_jobs["histories"][0]["kind"] == "retry"
    statuses = [t.get("status") for t in stub_jobs["transitions"]]
    assert "failed" not in statuses


@pytest.mark.asyncio
async def test_process_job_missing_job_id_400(stub_jobs):
    resp = await _post_internal({})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_process_job_unknown_job_id_404(stub_jobs, monkeypatch):
    async def boom(*, job_id, organization_id=None):
        raise jobs_svc.ActivationJobNotFound(f"job {job_id}")

    monkeypatch.setattr(jobs_svc, "get_job", boom)
    monkeypatch.setattr(router_mod.jobs_svc, "get_job", boom)

    resp = await _post_internal({"job_id": str(JOB_ID)})
    assert resp.status_code == 404


# ── step_activation kind ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_job_step_activation_dispatches_correctly(
    stub_jobs, monkeypatch
):
    stub_jobs["current_job"] = _job(
        kind="step_activation",
        payload={"step_id": "00000000-0000-0000-0000-000000000099"},
    )

    activated = type(
        "S",
        (),
        {
            "id": UUID("00000000-0000-0000-0000-000000000099"),
            "status": "scheduled",
            "external_provider_id": "lob_cmp_xy",
        },
    )()

    async def fake_activate(*, step_id, organization_id):
        return activated

    from app.services import channel_campaign_steps as steps_svc

    monkeypatch.setattr(steps_svc, "activate_step", fake_activate)

    resp = await _post_internal({"job_id": str(JOB_ID)})
    assert resp.status_code == 200
    statuses = [t.get("status") for t in stub_jobs["transitions"]]
    assert statuses[-1] == "succeeded"

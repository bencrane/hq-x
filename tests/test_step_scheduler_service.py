"""Pure-logic tests for app.services.step_scheduler."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from app.models.activation_jobs import ActivationJobResponse
from app.services import activation_jobs as jobs_svc
from app.services import step_scheduler as svc

ORG = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
BRAND = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
STEP_1 = UUID("11111111-1111-1111-1111-111111111111")
STEP_2 = UUID("22222222-2222-2222-2222-222222222222")
JOB = UUID("99999999-9999-9999-9999-999999999999")


def _job(*, status: str = "queued", payload: dict[str, Any] | None = None) -> ActivationJobResponse:
    now = datetime.now(UTC)
    return ActivationJobResponse(
        id=JOB,
        organization_id=ORG,
        brand_id=BRAND,
        kind="step_scheduled_activation",
        status=status,
        idempotency_key=None,
        payload=payload or {"step_id": str(STEP_2)},
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


# ── is_step_complete ─────────────────────────────────────────────────────


def test_is_step_complete_empty_returns_false():
    assert svc.is_step_complete({}) == (False, None)


def test_is_step_complete_with_pending_returns_false():
    assert svc.is_step_complete({"sent": 5, "pending": 1}) == (False, None)


def test_is_step_complete_all_sent():
    assert svc.is_step_complete({"sent": 100}) == (True, "sent")


def test_is_step_complete_mostly_sent_some_failed():
    # Mix of sent + failed = sent (some succeeded).
    assert svc.is_step_complete({"sent": 90, "failed": 10}) == (True, "sent")


def test_is_step_complete_all_failed_returns_failed():
    assert svc.is_step_complete({"failed": 100}) == (True, "failed")


def test_is_step_complete_all_suppressed_returns_failed():
    # Edge case: every recipient was suppressed pre-send.
    assert svc.is_step_complete({"suppressed": 100}) == (True, "failed")


def test_is_step_complete_cancelled_only_returns_sent():
    # All cancelled = step is complete; treated as sent (no actual failure).
    # Edge case worth pinning: cancelled means caller deliberately stopped
    # the step, so we shouldn't surface as failed.
    assert svc.is_step_complete({"cancelled": 100}) == (True, "sent")


# ── schedule_next_step ───────────────────────────────────────────────────


@pytest.fixture
def stub_schedule_next(monkeypatch):
    state: dict[str, Any] = {
        "next_step": {
            "step_id": STEP_2,
            "organization_id": ORG,
            "brand_id": BRAND,
            "step_order": 2,
            "delay_days_from_previous": 7,
            "status": "pending",
            "channel_campaign_id": UUID("ccccccc1-cccc-cccc-cccc-cccccccccccc"),
            "campaign_id": UUID("ccccccc2-cccc-cccc-cccc-cccccccccccc"),
        },
        "existing_job": None,
        "create_calls": [],
        "enqueue_calls": [],
        "transitions": [],
    }

    async def fake_find_next(*, completed_step_id):
        return state["next_step"]

    async def fake_find_existing(*, step_id):
        return state["existing_job"]

    async def fake_create_job(**kwargs):
        state["create_calls"].append(kwargs)
        return _job()

    async def fake_enqueue(**kwargs):
        state["enqueue_calls"].append(kwargs)
        return "run_xyz"

    async def fake_transition(**kwargs):
        state["transitions"].append(kwargs)
        return _job(
            status=kwargs.get("status", "queued"),
            payload={"step_id": str(STEP_2)},
        )

    monkeypatch.setattr(svc, "_find_next_step", fake_find_next)
    monkeypatch.setattr(svc, "_find_existing_scheduled_job", fake_find_existing)
    monkeypatch.setattr(jobs_svc, "create_job", fake_create_job)
    monkeypatch.setattr(jobs_svc, "enqueue_via_trigger", fake_enqueue)
    monkeypatch.setattr(jobs_svc, "transition_job", fake_transition)
    monkeypatch.setattr(svc.jobs_svc, "create_job", fake_create_job)
    monkeypatch.setattr(svc.jobs_svc, "enqueue_via_trigger", fake_enqueue)
    monkeypatch.setattr(svc.jobs_svc, "transition_job", fake_transition)

    # Stub out emit_event so we don't need the analytics chain.
    async def fake_emit(**kwargs):
        return None

    from app.services import analytics as analytics_svc
    monkeypatch.setattr(analytics_svc, "emit_event", fake_emit)

    return state


@pytest.mark.asyncio
async def test_schedule_next_step_creates_job_and_enqueues(stub_schedule_next):
    job = await svc.schedule_next_step(completed_step_id=STEP_1)
    assert job is not None
    assert len(stub_schedule_next["create_calls"]) == 1
    create_kwargs = stub_schedule_next["create_calls"][0]
    assert create_kwargs["kind"] == "step_scheduled_activation"
    assert create_kwargs["brand_id"] == BRAND
    assert create_kwargs["payload"]["step_id"] == str(STEP_2)
    # Enqueue passed delay_seconds in the payload (so the task uses wait.for).
    enqueue = stub_schedule_next["enqueue_calls"][0]
    assert enqueue["task_identifier"] == "dmaas.scheduled_step_activation"
    assert enqueue["payload_override"]["delay_seconds"] == 7 * 86_400


@pytest.mark.asyncio
async def test_schedule_next_step_idempotent_when_existing(stub_schedule_next):
    stub_schedule_next["existing_job"] = _job(status="queued")
    result = await svc.schedule_next_step(completed_step_id=STEP_1)
    assert result is not None
    # No new job created or enqueued.
    assert stub_schedule_next["create_calls"] == []
    assert stub_schedule_next["enqueue_calls"] == []


@pytest.mark.asyncio
async def test_schedule_next_step_returns_none_when_no_next(stub_schedule_next):
    stub_schedule_next["next_step"] = None
    result = await svc.schedule_next_step(completed_step_id=STEP_1)
    assert result is None


@pytest.mark.asyncio
async def test_schedule_next_step_skips_already_activated(stub_schedule_next):
    stub_schedule_next["next_step"]["status"] = "scheduled"
    result = await svc.schedule_next_step(completed_step_id=STEP_1)
    assert result is None


# ── cancel_scheduled_step ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_scheduled_step_no_job_returns_none(monkeypatch):
    async def fake_find_existing(*, step_id):
        return None

    monkeypatch.setattr(svc, "_find_existing_scheduled_job", fake_find_existing)
    result = await svc.cancel_scheduled_step(step_id=STEP_2, organization_id=ORG)
    assert result is None


@pytest.mark.asyncio
async def test_cancel_scheduled_step_cancels_existing(monkeypatch):
    cancelled: dict[str, Any] = {}

    async def fake_find_existing(*, step_id):
        return _job()

    async def fake_cancel(**kwargs):
        cancelled.update(kwargs)
        return _job(status="cancelled")

    monkeypatch.setattr(svc, "_find_existing_scheduled_job", fake_find_existing)
    monkeypatch.setattr(jobs_svc, "cancel_job", fake_cancel)
    monkeypatch.setattr(svc.jobs_svc, "cancel_job", fake_cancel)

    result = await svc.cancel_scheduled_step(
        step_id=STEP_2, organization_id=ORG, reason="campaign_paused"
    )
    assert result is not None
    assert result.status == "cancelled"
    assert cancelled["reason"] == "campaign_paused"

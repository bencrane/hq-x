"""Verify pause/archive cascades to step_scheduler.cancel_scheduled_step."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from app.models.activation_jobs import ActivationJobResponse
from app.services import step_scheduler as svc

ORG = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
BRAND = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
STEP_1 = UUID("11111111-1111-1111-1111-111111111111")
STEP_2 = UUID("22222222-2222-2222-2222-222222222222")
JOB = UUID("99999999-9999-9999-9999-999999999999")


def _job(*, status: str = "queued") -> ActivationJobResponse:
    now = datetime.now(UTC)
    return ActivationJobResponse(
        id=JOB,
        organization_id=ORG,
        brand_id=BRAND,
        kind="step_scheduled_activation",
        status=status,
        idempotency_key=None,
        payload={"step_id": str(STEP_2)},
        result=None,
        error=None,
        history=[],
        trigger_run_id="run_abc",
        attempts=0,
        created_at=now,
        started_at=None,
        completed_at=None,
        dead_lettered_at=None,
    )


@pytest.mark.asyncio
async def test_cancel_scheduled_step_uses_provided_reason(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_find_existing(*, step_id):
        return _job()

    async def fake_cancel(**kwargs):
        captured.update(kwargs)
        return _job(status="cancelled")

    monkeypatch.setattr(svc, "_find_existing_scheduled_job", fake_find_existing)

    from app.services import activation_jobs as jobs_svc

    monkeypatch.setattr(jobs_svc, "cancel_job", fake_cancel)
    monkeypatch.setattr(svc.jobs_svc, "cancel_job", fake_cancel)

    result = await svc.cancel_scheduled_step(
        step_id=STEP_2,
        organization_id=ORG,
        reason="channel_campaign_archived",
    )
    assert result is not None
    assert result.status == "cancelled"
    assert captured["reason"] == "channel_campaign_archived"


@pytest.mark.asyncio
async def test_cancel_scheduled_step_swallows_invalid_transition(monkeypatch):
    """Cancelling an already-terminal job should not crash the cascade."""
    from app.services import activation_jobs as jobs_svc

    async def fake_find_existing(*, step_id):
        return _job(status="succeeded")

    async def fake_cancel(**kwargs):
        # `cancel_scheduled_step` doesn't pre-filter by status; the
        # underlying cancel_job raises. Verify we surface the raise so
        # callers wrapping cascade can choose to swallow.
        raise jobs_svc.ActivationJobInvalidTransition("already terminal")

    monkeypatch.setattr(svc, "_find_existing_scheduled_job", fake_find_existing)
    monkeypatch.setattr(jobs_svc, "cancel_job", fake_cancel)
    monkeypatch.setattr(svc.jobs_svc, "cancel_job", fake_cancel)

    with pytest.raises(jobs_svc.ActivationJobInvalidTransition):
        await svc.cancel_scheduled_step(
            step_id=STEP_2, organization_id=ORG, reason="paused"
        )

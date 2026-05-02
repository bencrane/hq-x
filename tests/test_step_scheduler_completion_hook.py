"""Tests for the maybe_complete_step_and_schedule_next hook."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from app.services import step_scheduler as svc

STEP_1 = UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture
def stub_pipeline(monkeypatch):
    state: dict[str, Any] = {
        "counts": {"sent": 5, "failed": 1, "suppressed": 1},
        "step_status": "scheduled",
        "scheduled_calls": [],
        "update_status_calls": [],
        "emit_calls": [],
    }

    async def fake_count(*, step_id):
        return state["counts"]

    from app.services import channel_campaign_steps as steps_svc

    async def fake_get_simple(*, step_id):
        return state["step_status"]

    async def fake_update_status(*, step_id, new_status):
        state["update_status_calls"].append({"step_id": step_id, "new_status": new_status})
        state["step_status"] = new_status

    async def fake_schedule_next(*, completed_step_id):
        state["scheduled_calls"].append(completed_step_id)
        return None  # mimic no next step

    from app.services import analytics as analytics_svc

    async def fake_emit(**kwargs):
        state["emit_calls"].append(kwargs)

    monkeypatch.setattr(svc, "count_step_memberships_by_status", fake_count)
    monkeypatch.setattr(steps_svc, "get_step_simple", fake_get_simple)
    monkeypatch.setattr(steps_svc, "update_step_status", fake_update_status)
    monkeypatch.setattr(svc, "schedule_next_step", fake_schedule_next)
    monkeypatch.setattr(analytics_svc, "emit_event", fake_emit)
    return state


@pytest.mark.asyncio
async def test_hook_completes_step_when_all_terminal_then_schedules(stub_pipeline):
    result = await svc.maybe_complete_step_and_schedule_next(step_id=STEP_1)
    assert result["completed"] is True
    assert result["terminal_status"] == "sent"
    assert stub_pipeline["update_status_calls"] == [
        {"step_id": STEP_1, "new_status": "sent"}
    ]
    # Schedule next step was called.
    assert stub_pipeline["scheduled_calls"] == [STEP_1]
    # step.completed event emitted.
    assert any(c["event_name"] == "step.completed" for c in stub_pipeline["emit_calls"])


@pytest.mark.asyncio
async def test_hook_no_op_when_pending_remains(stub_pipeline):
    stub_pipeline["counts"] = {"sent": 5, "pending": 2}
    result = await svc.maybe_complete_step_and_schedule_next(step_id=STEP_1)
    assert result["completed"] is False
    assert stub_pipeline["scheduled_calls"] == []


@pytest.mark.asyncio
async def test_hook_marks_failed_when_no_sends(stub_pipeline):
    stub_pipeline["counts"] = {"failed": 10}
    result = await svc.maybe_complete_step_and_schedule_next(step_id=STEP_1)
    assert result["terminal_status"] == "failed"
    # step.failed event emitted, NOT step.completed.
    events = [c["event_name"] for c in stub_pipeline["emit_calls"]]
    assert "step.failed" in events
    assert "step.completed" not in events
    # No scheduling on failure.
    assert stub_pipeline["scheduled_calls"] == []


@pytest.mark.asyncio
async def test_hook_idempotent_when_step_already_terminal(stub_pipeline):
    stub_pipeline["step_status"] = "sent"
    result = await svc.maybe_complete_step_and_schedule_next(step_id=STEP_1)
    # Step was already sent; no further status update or scheduling.
    assert result["completed"] is True
    assert stub_pipeline["update_status_calls"] == []
    assert stub_pipeline["scheduled_calls"] == []


@pytest.mark.asyncio
async def test_hook_returns_step_not_found_when_step_missing(stub_pipeline):
    stub_pipeline["step_status"] = None
    result = await svc.maybe_complete_step_and_schedule_next(step_id=STEP_1)
    assert result["completed"] is False
    assert result["reason"] == "step_not_found"

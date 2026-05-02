"""Router-level tests for /internal/gtm/initiatives/{id}/run-step etc.

Asserts:
  * 401 without TRIGGER_SHARED_SECRET bearer.
  * 400 when agent_slug missing.
  * Round-trip happy path returns the StepResult shape Trigger consumes.
  * 500 + structured error envelope when run_step raises.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import gtm_initiatives as gtm_svc
from app.services import gtm_pipeline as pipeline


INITIATIVE_ID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def trigger_headers():
    return {"Authorization": "Bearer test-trigger-secret"}


@pytest.fixture
def stub_run_step(monkeypatch):
    state: dict[str, Any] = {"calls": [], "raise_kind": None}

    async def fake_run_step(
        *,
        initiative_id,
        agent_slug,
        hint=None,
        upstream_outputs=None,
        recipient_id=None,
        channel_campaign_step_id=None,
    ):
        state["calls"].append(
            {
                "initiative_id": str(initiative_id),
                "agent_slug": agent_slug,
                "hint": hint,
                "upstream_outputs": upstream_outputs,
                "recipient_id": recipient_id,
                "channel_campaign_step_id": channel_campaign_step_id,
            }
        )
        if state["raise_kind"] == "not_registered":
            raise pipeline.AgentSlugNotRegistered(
                f"agent_slug={agent_slug!r} not registered"
            )
        if state["raise_kind"] == "run_step_error":
            raise pipeline.RunStepError("anthropic blew up")
        return {
            "run_id": str(uuid4()),
            "run_index": 1,
            "status": "succeeded",
            "output_blob": {"shape": "json", "value": {"decision": "ship"}},
            "output_artifact_path": None,
            "prompt_version_id": str(uuid4()),
            "anthropic_session_id": "ses_xyz",
            "anthropic_request_ids": ["req_1"],
            "cost_cents": None,
        }

    monkeypatch.setattr(pipeline, "run_step", fake_run_step)
    return state


@pytest.fixture
def stub_initiative_lookup(monkeypatch):
    async def fake_get(initiative_id, *, organization_id=None):
        return {
            "id": initiative_id,
            "organization_id": uuid4(),
            "brand_id": uuid4(),
            "partner_id": uuid4(),
            "partner_contract_id": uuid4(),
            "data_engine_audience_id": uuid4(),
            "partner_research_ref": None,
            "strategic_context_research_ref": None,
            "campaign_strategy_path": None,
            "status": "strategy_ready",
            "history": [],
            "metadata": {},
            "reservation_window_start": None,
            "reservation_window_end": None,
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }

    async def fake_set_pipeline_status(initiative_id, status, *, bump_started_at=False):
        pass

    async def fake_append_history(initiative_id, event):
        pass

    monkeypatch.setattr(gtm_svc, "get_initiative", fake_get)
    monkeypatch.setattr(gtm_svc, "append_history", fake_append_history)
    monkeypatch.setattr(pipeline, "set_pipeline_status", fake_set_pipeline_status)


# ── auth ──────────────────────────────────────────────────────────────────


def test_run_step_requires_trigger_secret(client):
    resp = client.post(
        f"/internal/gtm/initiatives/{INITIATIVE_ID}/run-step",
        json={"agent_slug": "gtm-sequence-definer"},
    )
    assert resp.status_code == 401


def test_run_step_rejects_wrong_bearer(client):
    resp = client.post(
        f"/internal/gtm/initiatives/{INITIATIVE_ID}/run-step",
        headers={"Authorization": "Bearer wrong"},
        json={"agent_slug": "gtm-sequence-definer"},
    )
    assert resp.status_code == 401


# ── happy path ────────────────────────────────────────────────────────────


def test_run_step_round_trip(client, trigger_headers, stub_run_step):
    resp = client.post(
        f"/internal/gtm/initiatives/{INITIATIVE_ID}/run-step",
        headers=trigger_headers,
        json={
            "agent_slug": "gtm-sequence-definer",
            "hint": "tighten outlay",
            "upstream_outputs": {"gtm-sequence-definer": {"foo": "bar"}},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "succeeded"
    assert body["output_blob"]["value"]["decision"] == "ship"
    assert body["anthropic_session_id"] == "ses_xyz"
    # The service was called with the right shape.
    call = stub_run_step["calls"][0]
    assert call["agent_slug"] == "gtm-sequence-definer"
    assert call["hint"] == "tighten outlay"
    assert call["upstream_outputs"]["gtm-sequence-definer"] == {"foo": "bar"}


def test_run_step_missing_agent_slug_returns_400(client, trigger_headers):
    resp = client.post(
        f"/internal/gtm/initiatives/{INITIATIVE_ID}/run-step",
        headers=trigger_headers,
        json={},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "agent_slug_required"


# ── error paths ───────────────────────────────────────────────────────────


def test_run_step_404_for_unregistered_agent(client, trigger_headers, stub_run_step):
    stub_run_step["raise_kind"] = "not_registered"
    resp = client.post(
        f"/internal/gtm/initiatives/{INITIATIVE_ID}/run-step",
        headers=trigger_headers,
        json={"agent_slug": "made-up-slug"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "agent_not_registered"


def test_run_step_500_for_run_step_error(client, trigger_headers, stub_run_step):
    stub_run_step["raise_kind"] = "run_step_error"
    resp = client.post(
        f"/internal/gtm/initiatives/{INITIATIVE_ID}/run-step",
        headers=trigger_headers,
        json={"agent_slug": "gtm-sequence-definer"},
    )
    assert resp.status_code == 500
    assert resp.json()["detail"]["error"] == "run_step_failed"


# ── pipeline-completed / pipeline-failed ──────────────────────────────────


def test_pipeline_completed_marks_status(
    client, trigger_headers, stub_initiative_lookup,
):
    resp = client.post(
        f"/internal/gtm/initiatives/{INITIATIVE_ID}/pipeline-completed",
        headers=trigger_headers,
        json={"trigger_run_id": "run_abc"},
    )
    assert resp.status_code == 200
    assert resp.json()["pipeline_status"] == "completed"


def test_run_step_passes_per_recipient_kwargs(
    client, trigger_headers, stub_run_step,
):
    rid = "44444444-4444-4444-4444-444444444444"
    sid = "55555555-5555-5555-5555-555555555555"
    resp = client.post(
        f"/internal/gtm/initiatives/{INITIATIVE_ID}/run-step",
        headers=trigger_headers,
        json={
            "agent_slug": "gtm-per-recipient-creative",
            "recipient_id": rid,
            "channel_campaign_step_id": sid,
        },
    )
    assert resp.status_code == 200
    call = stub_run_step["calls"][0]
    assert str(call["recipient_id"]) == rid
    assert str(call["channel_campaign_step_id"]) == sid


def test_run_step_rejects_invalid_uuid_kwargs(
    client, trigger_headers, stub_run_step,
):
    resp = client.post(
        f"/internal/gtm/initiatives/{INITIATIVE_ID}/run-step",
        headers=trigger_headers,
        json={
            "agent_slug": "gtm-per-recipient-creative",
            "recipient_id": "not-a-uuid",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_uuid_kwarg"


def test_pipeline_failed_marks_status_with_reason(
    client, trigger_headers, stub_initiative_lookup,
):
    resp = client.post(
        f"/internal/gtm/initiatives/{INITIATIVE_ID}/pipeline-failed",
        headers=trigger_headers,
        json={
            "trigger_run_id": "run_abc",
            "failed_at_slug": "gtm-master-strategist-verdict",
            "reason": "verdict_block_after_retries",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["pipeline_status"] == "failed"
    assert body["failed_at_slug"] == "gtm-master-strategist-verdict"
    assert body["reason"] == "verdict_block_after_retries"

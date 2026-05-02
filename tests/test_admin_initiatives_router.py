"""Router tests for /api/v1/admin/initiatives — list / get /
start-pipeline / runs / rerun. Auth gated to platform_operator.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.auth import roles
from app.auth.supabase_jwt import UserContext
from app.main import app
from app.services import gtm_initiatives as gtm_svc
from app.services import gtm_pipeline as pipeline


INITIATIVE_ID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def operator_user():
    user = UserContext(
        auth_user_id=uuid4(),
        business_user_id=uuid4(),
        email="ops@example.com",
        platform_role="platform_operator",
        active_organization_id=None,
        org_role=None,
        role="operator",
        client_id=None,
    )

    def fake_require_platform_operator():
        return user

    app.dependency_overrides[roles.require_platform_operator] = (
        fake_require_platform_operator
    )
    yield user
    app.dependency_overrides.pop(roles.require_platform_operator, None)


@pytest.fixture
def stub_pipeline_router(monkeypatch):
    state: dict[str, Any] = {
        "kickoff_calls": [],
        "rerun_calls": [],
    }

    async def fake_get_initiative(iid, *, organization_id=None):
        return {
            "id": iid,
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

    async def fake_kickoff(initiative_id, *, gating_mode="auto", start_from=None):
        state["kickoff_calls"].append(
            {
                "initiative_id": str(initiative_id),
                "gating_mode": gating_mode,
                "start_from": start_from,
            }
        )
        return {
            "trigger_run_id": "run_xyz",
            "pipeline_status": "running",
            "gating_mode": gating_mode,
            "start_from": start_from,
        }

    async def fake_rerun(initiative_id, slug):
        state["rerun_calls"].append(
            {"initiative_id": str(initiative_id), "slug": slug}
        )
        return {
            "trigger_run_id": "run_rerun_xyz",
            "pipeline_status": "running",
            "gating_mode": "auto",
            "start_from": slug,
        }

    async def fake_list_runs(initiative_id, *, agent_slug=None, limit=50, offset=0):
        return [
            {
                "id": uuid4(),
                "initiative_id": initiative_id,
                "agent_slug": "gtm-sequence-definer",
                "run_index": 1,
                "parent_run_id": None,
                "status": "succeeded",
                "input_blob": {},
                "output_blob": {"shape": "json", "value": {"decision": "ship"}},
                "output_artifact_path": None,
                "prompt_version_id": uuid4(),
                "anthropic_agent_id": "agt_seq_test",
                "anthropic_session_id": "ses_xyz",
                "anthropic_request_ids": ["req_1"],
                "mcp_calls": [],
                "cost_cents": None,
                "model": "claude-opus-4-7",
                "started_at": datetime.now(UTC),
                "completed_at": datetime.now(UTC),
                "error_blob": None,
            }
        ]

    async def fake_append_history(iid, event):
        pass

    monkeypatch.setattr(gtm_svc, "get_initiative", fake_get_initiative)
    monkeypatch.setattr(gtm_svc, "append_history", fake_append_history)
    monkeypatch.setattr(pipeline, "kickoff_pipeline", fake_kickoff)
    monkeypatch.setattr(pipeline, "request_rerun", fake_rerun)
    monkeypatch.setattr(pipeline, "list_runs_for_initiative", fake_list_runs)
    return state


def test_initiatives_requires_operator(client):
    resp = client.get(f"/api/v1/admin/initiatives/{INITIATIVE_ID}")
    assert resp.status_code in (401, 403)


def test_start_pipeline_returns_trigger_run_id(
    client, operator_user, stub_pipeline_router,
):
    resp = client.post(
        f"/api/v1/admin/initiatives/{INITIATIVE_ID}/start-pipeline",
        json={"gating_mode": "auto"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["trigger_run_id"] == "run_xyz"
    assert body["pipeline_status"] == "running"
    assert stub_pipeline_router["kickoff_calls"][0]["gating_mode"] == "auto"


def test_start_pipeline_rejects_invalid_gating_mode(
    client, operator_user, stub_pipeline_router,
):
    resp = client.post(
        f"/api/v1/admin/initiatives/{INITIATIVE_ID}/start-pipeline",
        json={"gating_mode": "weird"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_gating_mode"


def test_rerun_step_calls_request_rerun(
    client, operator_user, stub_pipeline_router,
):
    resp = client.post(
        f"/api/v1/admin/initiatives/{INITIATIVE_ID}/runs/gtm-master-strategist/rerun",
    )
    assert resp.status_code == 200
    assert stub_pipeline_router["rerun_calls"][0]["slug"] == "gtm-master-strategist"


def test_list_runs_returns_items(
    client, operator_user, stub_pipeline_router,
):
    resp = client.get(f"/api/v1/admin/initiatives/{INITIATIVE_ID}/runs")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["agent_slug"] == "gtm-sequence-definer"

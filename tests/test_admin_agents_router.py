"""Router tests for /api/v1/admin/agents — list / get / activate /
rollback / versions. Auth gated to platform_operator.
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
from app.services import agent_prompts


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def operator_user(monkeypatch):
    """Stub require_platform_operator to return a synthetic operator
    UserContext so router tests don't have to mint real Supabase JWTs."""
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
def stub_agent_prompts(monkeypatch):
    state: dict[str, Any] = {
        "registry_rows": [
            {
                "id": uuid4(),
                "agent_slug": "gtm-sequence-definer",
                "anthropic_agent_id": "agt_seq_test",
                "role": "actor",
                "parent_actor_slug": None,
                "model": "claude-opus-4-7",
                "description": None,
                "deactivated_at": None,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
            },
        ],
        "activate_calls": [],
        "rollback_calls": [],
        "raise_kind": None,
    }

    async def fake_list(include_deactivated=False):
        return state["registry_rows"]

    async def fake_get_current(slug):
        if slug == "missing":
            return None
        return {
            "registry": state["registry_rows"][0],
            "current_system_prompt": "PROMPT_BODY",
            "anthropic_state": {
                "name": slug,
                "model": "claude-opus-4-7",
                "version": 1,
            },
            "latest_version": {
                "id": uuid4(),
                "agent_slug": slug,
                "anthropic_agent_id": "agt_seq_test",
                "system_prompt": "PROMPT_BODY",
                "version_index": 1,
                "activation_source": "setup_script",
                "parent_version_id": None,
                "activated_by_user_id": None,
                "notes": None,
                "created_at": datetime.now(UTC),
            },
        }

    async def fake_activate(*, agent_slug, new_system_prompt, activated_by_user_id, notes):
        if state["raise_kind"] == "not_registered":
            raise agent_prompts.AgentNotRegistered(f"{agent_slug} not registered")
        state["activate_calls"].append(
            {
                "agent_slug": agent_slug,
                "new_system_prompt": new_system_prompt,
                "activated_by_user_id": activated_by_user_id,
                "notes": notes,
            }
        )
        return {
            "snapshot_version": {"id": uuid4(), "version_index": 2},
            "new_version": {"id": uuid4(), "version_index": 3},
        }

    async def fake_rollback(*, agent_slug, version_index, activated_by_user_id, notes=None):
        if state["raise_kind"] == "version_not_found":
            raise agent_prompts.VersionNotFound(
                f"no version_index={version_index}"
            )
        state["rollback_calls"].append(
            {
                "agent_slug": agent_slug,
                "version_index": version_index,
            }
        )
        return {
            "snapshot_version": {"id": uuid4(), "version_index": 4},
            "new_version": {"id": uuid4(), "version_index": 5},
        }

    async def fake_list_versions(slug, *, limit=50, offset=0):
        return [
            {
                "id": uuid4(),
                "agent_slug": slug,
                "anthropic_agent_id": "agt_seq_test",
                "system_prompt": "PROMPT_V1",
                "version_index": 1,
                "activation_source": "setup_script",
                "parent_version_id": None,
                "activated_by_user_id": None,
                "notes": None,
                "created_at": datetime.now(UTC),
            }
        ]

    monkeypatch.setattr(agent_prompts, "list_registry_rows", fake_list)
    monkeypatch.setattr(agent_prompts, "get_current_for_admin", fake_get_current)
    monkeypatch.setattr(agent_prompts, "activate_prompt", fake_activate)
    monkeypatch.setattr(agent_prompts, "rollback_prompt", fake_rollback)
    monkeypatch.setattr(agent_prompts, "list_versions", fake_list_versions)
    return state


# ── auth ──────────────────────────────────────────────────────────────────


def test_admin_agents_requires_platform_operator(client):
    resp = client.get("/api/v1/admin/agents")
    # No JWT → 401 (or 403 if dependency runs after auth). Either is "deny".
    assert resp.status_code in (401, 403)


# ── list ──────────────────────────────────────────────────────────────────


def test_list_agents_returns_registry(client, operator_user, stub_agent_prompts):
    resp = client.get("/api/v1/admin/agents")
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert body["items"][0]["agent_slug"] == "gtm-sequence-definer"


# ── get ───────────────────────────────────────────────────────────────────


def test_get_agent_returns_composite(client, operator_user, stub_agent_prompts):
    resp = client.get("/api/v1/admin/agents/gtm-sequence-definer")
    assert resp.status_code == 200
    body = resp.json()
    assert body["registry"]["agent_slug"] == "gtm-sequence-definer"
    assert body["current_system_prompt"] == "PROMPT_BODY"


def test_get_agent_404_unknown(client, operator_user, stub_agent_prompts):
    resp = client.get("/api/v1/admin/agents/missing")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "agent_not_registered"


# ── activate ──────────────────────────────────────────────────────────────


def test_activate_writes_two_versions(client, operator_user, stub_agent_prompts):
    resp = client.post(
        "/api/v1/admin/agents/gtm-sequence-definer/activate",
        json={"system_prompt": "NEW_PROMPT_BODY", "notes": "iter 7"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "snapshot_version" in body
    assert "new_version" in body
    assert stub_agent_prompts["activate_calls"][0]["notes"] == "iter 7"


def test_activate_rejects_empty_prompt(client, operator_user, stub_agent_prompts):
    resp = client.post(
        "/api/v1/admin/agents/gtm-sequence-definer/activate",
        json={"system_prompt": ""},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "system_prompt_required"


def test_activate_404_when_slug_unknown(client, operator_user, stub_agent_prompts):
    stub_agent_prompts["raise_kind"] = "not_registered"
    resp = client.post(
        "/api/v1/admin/agents/whatever/activate",
        json={"system_prompt": "X"},
    )
    assert resp.status_code == 404


# ── rollback ──────────────────────────────────────────────────────────────


def test_rollback_resolves_version(client, operator_user, stub_agent_prompts):
    resp = client.post(
        "/api/v1/admin/agents/gtm-sequence-definer/rollback",
        json={"version_index": 2},
    )
    assert resp.status_code == 200
    assert stub_agent_prompts["rollback_calls"][0]["version_index"] == 2


def test_rollback_404_when_version_missing(client, operator_user, stub_agent_prompts):
    stub_agent_prompts["raise_kind"] = "version_not_found"
    resp = client.post(
        "/api/v1/admin/agents/gtm-sequence-definer/rollback",
        json={"version_index": 999},
    )
    assert resp.status_code == 404


def test_rollback_400_when_version_index_missing(
    client, operator_user, stub_agent_prompts,
):
    resp = client.post(
        "/api/v1/admin/agents/gtm-sequence-definer/rollback",
        json={},
    )
    assert resp.status_code == 400


# ── versions ──────────────────────────────────────────────────────────────


def test_list_versions(client, operator_user, stub_agent_prompts):
    resp = client.get("/api/v1/admin/agents/gtm-sequence-definer/versions")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["version_index"] == 1

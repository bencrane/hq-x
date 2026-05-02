"""Router tests for /api/v1/admin/doctrine — get / upsert.
Auth gated to platform_operator. Validation passes through to
org_doctrine.validate_parameters.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.auth import roles
from app.auth.supabase_jwt import UserContext
from app.main import app
from app.services import org_doctrine


ORG_ID = "4482eb19-f961-48e1-a957-41939d042908"


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
def stub_doctrine(monkeypatch):
    state: dict[str, Any] = {"upsert_calls": [], "stored": None}

    async def fake_get(org_id):
        return state["stored"]

    async def fake_upsert(*, organization_id, doctrine_markdown, parameters, updated_by_user_id):
        state["upsert_calls"].append(
            {
                "organization_id": str(organization_id),
                "doctrine_markdown": doctrine_markdown,
                "parameters": parameters,
            }
        )
        row = {
            "organization_id": organization_id,
            "doctrine_markdown": doctrine_markdown,
            "parameters": parameters,
            "updated_at": datetime.now(UTC),
            "updated_by_user_id": updated_by_user_id,
        }
        state["stored"] = row
        return row

    monkeypatch.setattr(org_doctrine, "get_for_org", fake_get)
    monkeypatch.setattr(org_doctrine, "upsert", fake_upsert)
    return state


def test_doctrine_requires_operator(client):
    resp = client.get(f"/api/v1/admin/doctrine/{ORG_ID}")
    assert resp.status_code in (401, 403)


def test_get_doctrine_returns_404_when_missing(
    client, operator_user, stub_doctrine,
):
    stub_doctrine["stored"] = None
    resp = client.get(f"/api/v1/admin/doctrine/{ORG_ID}")
    assert resp.status_code == 404


def test_upsert_doctrine_with_object_parameters(
    client, operator_user, stub_doctrine,
):
    resp = client.post(
        f"/api/v1/admin/doctrine/{ORG_ID}",
        json={
            "doctrine_markdown": "# doctrine body",
            "parameters": {
                "target_margin_pct": 0.40,
                "min_per_piece_cents": 100,
            },
        },
    )
    assert resp.status_code == 200
    upsert = stub_doctrine["upsert_calls"][0]
    assert upsert["doctrine_markdown"] == "# doctrine body"
    assert upsert["parameters"]["target_margin_pct"] == 0.40


def test_upsert_doctrine_parses_string_parameters(
    client, operator_user, stub_doctrine,
):
    resp = client.post(
        f"/api/v1/admin/doctrine/{ORG_ID}",
        json={
            "doctrine_markdown": "# body",
            "parameters": json.dumps({"target_margin_pct": 0.45}),
        },
    )
    assert resp.status_code == 200
    assert stub_doctrine["upsert_calls"][0]["parameters"]["target_margin_pct"] == 0.45


def test_upsert_rejects_empty_markdown(
    client, operator_user, stub_doctrine,
):
    resp = client.post(
        f"/api/v1/admin/doctrine/{ORG_ID}",
        json={"doctrine_markdown": "", "parameters": {}},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "doctrine_markdown_required"


def test_upsert_rejects_non_object_parameters(
    client, operator_user, stub_doctrine,
):
    resp = client.post(
        f"/api/v1/admin/doctrine/{ORG_ID}",
        json={"doctrine_markdown": "# body", "parameters": [1, 2, 3]},
    )
    assert resp.status_code == 400


def test_upsert_rejects_invalid_json_string(
    client, operator_user, stub_doctrine,
):
    resp = client.post(
        f"/api/v1/admin/doctrine/{ORG_ID}",
        json={
            "doctrine_markdown": "# body",
            "parameters": "{not json,",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "parameters_not_json"

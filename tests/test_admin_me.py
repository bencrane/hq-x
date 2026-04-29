from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest

from app.auth import supabase_jwt as auth_module
from app.main import app
from tests._jwt_helpers import PUBLIC_KEY, make_token


@pytest.fixture
def fake_business_user(monkeypatch):
    holder: dict = {"row": None, "memberships": []}

    async def fake_lookup(auth_user_id: UUID):
        return holder["row"]

    async def fake_memberships(business_user_id: UUID):
        return holder["memberships"]

    def fake_signing_key(token: str):
        return PUBLIC_KEY

    monkeypatch.setattr(auth_module, "_lookup_business_user", fake_lookup)
    monkeypatch.setattr(auth_module, "_lookup_memberships", fake_memberships)
    monkeypatch.setattr(auth_module, "_get_signing_key", fake_signing_key)
    return holder


async def _get(headers: dict[str, str] | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/admin/me", headers=headers or {})


async def test_admin_me_requires_auth(fake_business_user) -> None:
    resp = await _get()
    assert resp.status_code == 401


async def test_admin_me_rejects_client_role(fake_business_user) -> None:
    auth_user_id = uuid4()
    fake_business_user["row"] = {
        "id": uuid4(),
        "email": "client@example.com",
        "role": "client",
        "client_id": uuid4(),
        "platform_role": None,
    }
    token = make_token(sub=str(auth_user_id))
    resp = await _get({"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "platform_operator_required"


async def test_admin_me_returns_operator_identity(fake_business_user) -> None:
    auth_user_id = uuid4()
    business_user_id = uuid4()
    fake_business_user["row"] = {
        "id": business_user_id,
        "email": "admin@acquisitionengineering.com",
        "role": "operator",
        "client_id": None,
        "platform_role": "platform_operator",
    }
    token = make_token(sub=str(auth_user_id))
    resp = await _get({"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "user_id": str(auth_user_id),
        "business_user_id": str(business_user_id),
        "email": "admin@acquisitionengineering.com",
        "role": "operator",
    }

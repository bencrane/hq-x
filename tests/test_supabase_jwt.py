from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest

from app.auth import supabase_jwt as auth_module
from app.main import app
from tests._jwt_helpers import PUBLIC_KEY, WRONG_SIGNER_PRIVATE_KEY, make_token


@pytest.fixture
def fake_business_user(monkeypatch):
    """Patch DB lookup + JWKS lookup. Tests set the row dict via the holder."""
    holder: dict = {"row": None}

    async def fake_lookup(auth_user_id: UUID):
        return holder["row"]

    def fake_signing_key(token: str):
        return PUBLIC_KEY

    monkeypatch.setattr(auth_module, "_lookup_business_user", fake_lookup)
    monkeypatch.setattr(auth_module, "_get_signing_key", fake_signing_key)
    return holder


async def _get(headers: dict[str, str] | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/admin/me", headers=headers or {})


async def test_jwt_missing_returns_401(fake_business_user) -> None:
    resp = await _get()
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "missing_auth"


async def test_jwt_malformed_returns_401(fake_business_user) -> None:
    resp = await _get({"Authorization": "Bearer not-a-real-jwt"})
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "malformed_token"


async def test_jwt_invalid_signature_returns_401(fake_business_user) -> None:
    token = make_token(signer=WRONG_SIGNER_PRIVATE_KEY)
    resp = await _get({"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "invalid_signature"


async def test_jwt_expired_returns_401(fake_business_user) -> None:
    token = make_token(exp_offset=-60)
    resp = await _get({"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "token_expired"


async def test_jwt_wrong_audience_returns_401(fake_business_user) -> None:
    token = make_token(aud="anon")
    resp = await _get({"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "malformed_token"


async def test_jwt_valid_no_user_row_returns_403(fake_business_user) -> None:
    fake_business_user["row"] = None
    token = make_token(sub=str(uuid4()))
    resp = await _get({"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "user_not_provisioned"


async def test_jwt_valid_with_user_row_returns_user_context(fake_business_user) -> None:
    auth_user_id = uuid4()
    business_user_id = uuid4()
    fake_business_user["row"] = {
        "id": business_user_id,
        "email": "ops@example.com",
        "role": "operator",
        "client_id": None,
    }
    token = make_token(sub=str(auth_user_id))
    resp = await _get({"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == str(auth_user_id)
    assert body["business_user_id"] == str(business_user_id)
    assert body["email"] == "ops@example.com"
    assert body["role"] == "operator"

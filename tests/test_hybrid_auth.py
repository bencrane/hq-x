"""Tests for the hybrid edge dependency (app/auth/hybrid.py).

Door A — X-Service-Token vs HQ_X_SERVICE_TOKEN.
Door B — Supabase RS256 JWT verified via JWKS (issuer + audience enforced).

The JWKS network call is bypassed by monkeypatching ``_get_signing_key`` to
return a locally generated RSA public key; tokens are signed with the
matching private key.
"""

from __future__ import annotations

import time
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI

from app.auth import hybrid as hybrid_module
from app.auth.hybrid import AuthPrincipal, authenticate
from app.config import settings

_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PUBLIC_KEY = _PRIVATE_KEY.public_key()
_OTHER_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)

_ISSUER = "https://hybrid-test.supabase.co/auth/v1"
_SERVICE_TOKEN = "test-hq-x-service-token"


_test_app = FastAPI()


@_test_app.get("/api/me")
async def _me(principal: AuthPrincipal = Depends(authenticate)) -> dict:
    return {
        "kind": principal.kind,
        "user_id": str(principal.user_id) if principal.user_id else None,
        "email": (principal.claims or {}).get("email"),
    }


def _make_token(
    *,
    sub: str | None = None,
    aud: str = "authenticated",
    iss: str = _ISSUER,
    exp_offset: int = 3600,
    signer=_PRIVATE_KEY,
) -> str:
    now = int(time.time())
    payload = {
        "sub": sub or str(uuid4()),
        "aud": aud,
        "iss": iss,
        "iat": now,
        "exp": now + exp_offset,
        "email": "test@example.com",
    }
    return jwt.encode(payload, signer, algorithm="RS256")


@pytest.fixture(autouse=True)
def _wire(monkeypatch):
    monkeypatch.setattr(hybrid_module, "_get_signing_key", lambda token: _PUBLIC_KEY)
    monkeypatch.setattr(settings, "HQX_SUPABASE_ISSUER", _ISSUER)
    monkeypatch.setattr(settings, "HQ_X_SERVICE_TOKEN", _SERVICE_TOKEN)


async def _get(headers: dict[str, str]) -> httpx.Response:
    transport = httpx.ASGITransport(app=_test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.get("/api/me", headers=headers)


# ── Door A: service token ────────────────────────────────────────────────


async def test_valid_service_token_authorizes() -> None:
    resp = await _get({"X-Service-Token": _SERVICE_TOKEN})
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "service"
    assert body["user_id"] is None


async def test_wrong_service_token_401_no_fallthrough() -> None:
    # Present-but-wrong service token is rejected, not fallen through to JWT.
    resp = await _get(
        {
            "X-Service-Token": "nope",
            "Authorization": f"Bearer {_make_token()}",
        }
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["reason"] == "invalid_service_token"


# ── Door B: Supabase RS256 JWT ───────────────────────────────────────────


async def test_valid_jwt_authorizes() -> None:
    sub = str(uuid4())
    resp = await _get({"Authorization": f"Bearer {_make_token(sub=sub)}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "user"
    assert body["user_id"] == sub
    assert body["email"] == "test@example.com"


async def test_expired_jwt_401() -> None:
    resp = await _get({"Authorization": f"Bearer {_make_token(exp_offset=-10)}"})
    assert resp.status_code == 401
    assert resp.json()["detail"]["reason"] == "token_expired"


async def test_wrong_issuer_401() -> None:
    token = _make_token(iss="https://evil.example.com/auth/v1")
    resp = await _get({"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert resp.json()["detail"]["reason"] == "invalid_issuer"


async def test_wrong_audience_401() -> None:
    resp = await _get({"Authorization": f"Bearer {_make_token(aud='anon')}"})
    assert resp.status_code == 401
    assert resp.json()["detail"]["reason"] == "invalid_audience"


async def test_bad_signature_401() -> None:
    token = _make_token(signer=_OTHER_PRIVATE_KEY)
    resp = await _get({"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert resp.json()["detail"]["reason"] == "invalid_signature"


async def test_missing_credentials_401() -> None:
    resp = await _get({})
    assert resp.status_code == 401
    assert resp.json()["detail"]["reason"] == "missing_credentials"

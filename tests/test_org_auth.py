"""Tests for the organization-aware auth dependencies introduced in 0020.

Covers:
  * UserContext.platform_role / active_organization_id / org_role wiring.
  * X-Organization-Id header resolution rules.
  * require_platform_operator / require_org_context / require_org_role.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import Depends, FastAPI

from app.auth import supabase_jwt as auth_module
from app.auth.roles import (
    require_org_context,
    require_org_role,
    require_platform_operator,
)
from app.auth.supabase_jwt import UserContext, verify_supabase_jwt
from tests._jwt_helpers import PUBLIC_KEY, make_token


# ── Test app ─────────────────────────────────────────────────────────────

_test_app = FastAPI()


@_test_app.get("/whoami")
async def whoami(user: UserContext = Depends(verify_supabase_jwt)) -> dict:
    return {
        "platform_role": user.platform_role,
        "active_organization_id": (
            str(user.active_organization_id) if user.active_organization_id else None
        ),
        "org_role": user.org_role,
    }


@_test_app.get("/platform-only")
async def platform_only(
    _user: UserContext = Depends(require_platform_operator),
) -> dict:
    return {"ok": True}


@_test_app.get("/needs-org")
async def needs_org(_user: UserContext = Depends(require_org_context)) -> dict:
    return {"ok": True}


@_test_app.get("/needs-admin")
async def needs_admin(
    _user: UserContext = Depends(require_org_role("owner", "admin")),
) -> dict:
    return {"ok": True}


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def auth_holder(monkeypatch):
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


async def _request(path: str, headers: dict[str, str]) -> httpx.Response:
    transport = httpx.ASGITransport(app=_test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.get(path, headers=headers)


def _user_row(*, role: str = "client", platform_role: str | None = None) -> dict:
    return {
        "id": uuid4(),
        "email": "u@example.com",
        "role": role,
        "client_id": None,
        "platform_role": platform_role,
    }


def _bearer() -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(sub=str(uuid4()))}"}


# ── Org context resolution ───────────────────────────────────────────────


async def test_no_membership_no_header_active_org_is_none(auth_holder) -> None:
    auth_holder["row"] = _user_row()
    auth_holder["memberships"] = []
    resp = await _request("/whoami", _bearer())
    assert resp.status_code == 200
    assert resp.json()["active_organization_id"] is None
    assert resp.json()["org_role"] is None


async def test_single_membership_auto_selects(auth_holder) -> None:
    auth_holder["row"] = _user_row()
    org_id = uuid4()
    auth_holder["memberships"] = [{"organization_id": org_id, "org_role": "owner"}]
    resp = await _request("/whoami", _bearer())
    body = resp.json()
    assert body["active_organization_id"] == str(org_id)
    assert body["org_role"] == "owner"


async def test_multi_membership_no_header_leaves_org_none(auth_holder) -> None:
    auth_holder["row"] = _user_row()
    auth_holder["memberships"] = [
        {"organization_id": uuid4(), "org_role": "owner"},
        {"organization_id": uuid4(), "org_role": "member"},
    ]
    resp = await _request("/whoami", _bearer())
    assert resp.json()["active_organization_id"] is None


async def test_header_selects_membership(auth_holder) -> None:
    auth_holder["row"] = _user_row()
    a = uuid4()
    b = uuid4()
    auth_holder["memberships"] = [
        {"organization_id": a, "org_role": "owner"},
        {"organization_id": b, "org_role": "member"},
    ]
    resp = await _request(
        "/whoami", {**_bearer(), "X-Organization-Id": str(b)}
    )
    body = resp.json()
    assert body["active_organization_id"] == str(b)
    assert body["org_role"] == "member"


async def test_header_for_non_member_is_403(auth_holder) -> None:
    auth_holder["row"] = _user_row()
    auth_holder["memberships"] = []
    resp = await _request(
        "/whoami", {**_bearer(), "X-Organization-Id": str(uuid4())}
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "not_a_member_of_organization"


async def test_platform_operator_can_target_any_org(auth_holder) -> None:
    auth_holder["row"] = _user_row(role="operator", platform_role="platform_operator")
    auth_holder["memberships"] = []
    target = uuid4()
    resp = await _request(
        "/whoami", {**_bearer(), "X-Organization-Id": str(target)}
    )
    body = resp.json()
    assert body["active_organization_id"] == str(target)
    assert body["org_role"] is None  # platform operator without membership


async def test_invalid_org_header_is_400(auth_holder) -> None:
    auth_holder["row"] = _user_row()
    resp = await _request(
        "/whoami", {**_bearer(), "X-Organization-Id": "not-a-uuid"}
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_organization_id"


# ── require_platform_operator ────────────────────────────────────────────


async def test_platform_only_blocks_non_operator(auth_holder) -> None:
    auth_holder["row"] = _user_row()
    resp = await _request("/platform-only", _bearer())
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "platform_operator_required"


async def test_platform_only_allows_platform_operator(auth_holder) -> None:
    auth_holder["row"] = _user_row(role="operator", platform_role="platform_operator")
    resp = await _request("/platform-only", _bearer())
    assert resp.status_code == 200


# ── require_org_context ──────────────────────────────────────────────────


async def test_needs_org_400_when_unresolved(auth_holder) -> None:
    auth_holder["row"] = _user_row()
    auth_holder["memberships"] = []
    resp = await _request("/needs-org", _bearer())
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "organization_context_required"


async def test_needs_org_passes_with_single_membership(auth_holder) -> None:
    auth_holder["row"] = _user_row()
    auth_holder["memberships"] = [
        {"organization_id": uuid4(), "org_role": "member"}
    ]
    resp = await _request("/needs-org", _bearer())
    assert resp.status_code == 200


# ── require_org_role ─────────────────────────────────────────────────────


async def test_org_role_rejects_member_when_admin_required(auth_holder) -> None:
    auth_holder["row"] = _user_row()
    auth_holder["memberships"] = [
        {"organization_id": uuid4(), "org_role": "member"}
    ]
    resp = await _request("/needs-admin", _bearer())
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "insufficient_org_role"


async def test_org_role_accepts_admin(auth_holder) -> None:
    auth_holder["row"] = _user_row()
    auth_holder["memberships"] = [
        {"organization_id": uuid4(), "org_role": "admin"}
    ]
    resp = await _request("/needs-admin", _bearer())
    assert resp.status_code == 200


async def test_platform_operator_bypasses_org_role(auth_holder) -> None:
    auth_holder["row"] = _user_row(role="operator", platform_role="platform_operator")
    target = uuid4()
    auth_holder["memberships"] = []  # no membership, but plat-op bypass
    resp = await _request(
        "/needs-admin", {**_bearer(), "X-Organization-Id": str(target)}
    )
    assert resp.status_code == 200

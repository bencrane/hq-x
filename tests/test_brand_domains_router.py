"""Tests for /api/v1/brands/{brand_id}/domains/* endpoints.

Stubs the brand_domains service so the router contract (paths, response
shape, error mapping, auth, cross-org guard) is exercised without DB or
Dub HTTP traffic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest

from app.auth.roles import require_org_context
from app.auth.supabase_jwt import UserContext, verify_supabase_jwt
from app.main import app
from app.providers.dub.client import DubProviderError
from app.services import brand_domains as svc

ORG_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
USER_A = UUID("11111111-1111-1111-1111-111111111111")
BRAND_A = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
ENTRI_ID = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")


def _user(org_id: UUID | None) -> UserContext:
    return UserContext(
        auth_user_id=USER_A,
        business_user_id=USER_A,
        email="op@example.com",
        platform_role="platform_operator",
        active_organization_id=org_id,
        org_role=None,
        role="operator",
        client_id=None,
    )


@pytest.fixture
def auth_org_a():
    user = _user(ORG_A)
    app.dependency_overrides[verify_supabase_jwt] = lambda: user
    app.dependency_overrides[require_org_context] = lambda: user
    yield
    app.dependency_overrides.clear()


async def _client_request(method: str, path: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, **kwargs)


# ── GET /domains ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_returns_both_bindings_when_set(monkeypatch, auth_org_a):
    async def fake_get(*, brand_id, organization_id):
        return svc.BrandDomainConfigs(
            brand_id=brand_id,
            dub=svc.DubDomainBinding(
                domain="track.acme.com",
                dub_domain_id="dom_abc",
                verified_at=datetime(2026, 4, 30, tzinfo=UTC),
            ),
            landing_page=svc.LandingPageDomainBinding(
                domain="pages.acme.com",
                entri_connection_id=ENTRI_ID,
                verified_at=datetime(2026, 4, 30, tzinfo=UTC),
            ),
        )

    monkeypatch.setattr(svc, "get_brand_domain_configs", fake_get)
    resp = await _client_request("GET", f"/api/v1/brands/{BRAND_A}/domains")
    assert resp.status_code == 200
    body = resp.json()
    assert body["brand_id"] == str(BRAND_A)
    assert body["dub"]["domain"] == "track.acme.com"
    assert body["landing_page"]["domain"] == "pages.acme.com"


@pytest.mark.asyncio
async def test_get_returns_404_for_brand_in_other_org(monkeypatch, auth_org_a):
    async def fake_get(*, brand_id, organization_id):
        raise svc.BrandNotFoundError("nope")

    monkeypatch.setattr(svc, "get_brand_domain_configs", fake_get)
    resp = await _client_request("GET", f"/api/v1/brands/{BRAND_A}/domains")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "brand_not_found"


# ── POST /domains/dub ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_dub_domain_returns_201_and_binding(monkeypatch, auth_org_a):
    captured: dict[str, Any] = {}

    async def fake_register(*, brand_id, organization_id, domain):
        captured["brand_id"] = brand_id
        captured["organization_id"] = organization_id
        captured["domain"] = domain
        return svc.DubDomainBinding(
            domain=domain,
            dub_domain_id="dom_new",
            verified_at=datetime(2026, 4, 30, tzinfo=UTC),
        )

    monkeypatch.setattr(svc, "register_dub_domain_for_brand", fake_register)

    resp = await _client_request(
        "POST",
        f"/api/v1/brands/{BRAND_A}/domains/dub",
        json={"domain": "Track.Acme.com"},
    )
    assert resp.status_code == 201
    assert resp.json()["dub_domain_id"] == "dom_new"
    # Domain is normalized lowercase before reaching the service.
    assert captured["domain"] == "track.acme.com"


@pytest.mark.asyncio
async def test_post_dub_domain_rejects_invalid_fqdn(monkeypatch, auth_org_a):
    resp = await _client_request(
        "POST",
        f"/api/v1/brands/{BRAND_A}/domains/dub",
        json={"domain": "not_a_fqdn"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_post_dub_domain_returns_503_when_dub_unconfigured(
    monkeypatch, auth_org_a
):
    async def fake_register(**_):
        raise svc.DubNotConfiguredError("DUB_API_KEY missing")

    monkeypatch.setattr(svc, "register_dub_domain_for_brand", fake_register)

    resp = await _client_request(
        "POST",
        f"/api/v1/brands/{BRAND_A}/domains/dub",
        json={"domain": "track.acme.com"},
    )
    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "dub_not_configured"


@pytest.mark.asyncio
async def test_post_dub_domain_maps_dub_provider_error_to_502(
    monkeypatch, auth_org_a
):
    async def fake_register(**_):
        raise DubProviderError("upstream blew up", status=500)

    monkeypatch.setattr(svc, "register_dub_domain_for_brand", fake_register)

    resp = await _client_request(
        "POST",
        f"/api/v1/brands/{BRAND_A}/domains/dub",
        json={"domain": "track.acme.com"},
    )
    assert resp.status_code == 502
    assert resp.json()["detail"]["error"] == "dub_upstream_error"


@pytest.mark.asyncio
async def test_post_dub_domain_returns_404_for_other_org_brand(
    monkeypatch, auth_org_a
):
    async def fake_register(**_):
        raise svc.BrandNotFoundError("not found")

    monkeypatch.setattr(svc, "register_dub_domain_for_brand", fake_register)

    resp = await _client_request(
        "POST",
        f"/api/v1/brands/{BRAND_A}/domains/dub",
        json={"domain": "track.acme.com"},
    )
    assert resp.status_code == 404


# ── DELETE /domains/dub ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_dub_domain_returns_204(monkeypatch, auth_org_a):
    async def fake_dereg(*, brand_id, organization_id):
        return True

    monkeypatch.setattr(svc, "deregister_dub_domain_for_brand", fake_dereg)
    resp = await _client_request(
        "DELETE", f"/api/v1/brands/{BRAND_A}/domains/dub"
    )
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_delete_dub_domain_for_other_org_returns_404(
    monkeypatch, auth_org_a
):
    async def fake_dereg(**_):
        raise svc.BrandNotFoundError("nope")

    monkeypatch.setattr(svc, "deregister_dub_domain_for_brand", fake_dereg)
    resp = await _client_request(
        "DELETE", f"/api/v1/brands/{BRAND_A}/domains/dub"
    )
    assert resp.status_code == 404


# ── POST /domains/landing-page ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_landing_page_returns_201_and_binding(
    monkeypatch, auth_org_a
):
    async def fake_register(*, brand_id, organization_id, entri_connection_id):
        return svc.LandingPageDomainBinding(
            domain="pages.acme.com",
            entri_connection_id=entri_connection_id,
            verified_at=datetime(2026, 4, 30, tzinfo=UTC),
        )

    monkeypatch.setattr(svc, "register_landing_page_domain_for_brand", fake_register)
    resp = await _client_request(
        "POST",
        f"/api/v1/brands/{BRAND_A}/domains/landing-page",
        json={"entri_connection_id": str(ENTRI_ID)},
    )
    assert resp.status_code == 201
    assert resp.json()["entri_connection_id"] == str(ENTRI_ID)


@pytest.mark.asyncio
async def test_post_landing_page_returns_404_for_unknown_entri(
    monkeypatch, auth_org_a
):
    async def fake_register(**_):
        raise svc.EntriConnectionNotFoundError("not found")

    monkeypatch.setattr(svc, "register_landing_page_domain_for_brand", fake_register)
    resp = await _client_request(
        "POST",
        f"/api/v1/brands/{BRAND_A}/domains/landing-page",
        json={"entri_connection_id": str(ENTRI_ID)},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "entri_connection_not_found"


@pytest.mark.asyncio
async def test_post_landing_page_returns_404_for_other_org_brand(
    monkeypatch, auth_org_a
):
    async def fake_register(**_):
        raise svc.BrandNotFoundError("not found")

    monkeypatch.setattr(svc, "register_landing_page_domain_for_brand", fake_register)
    resp = await _client_request(
        "POST",
        f"/api/v1/brands/{BRAND_A}/domains/landing-page",
        json={"entri_connection_id": str(ENTRI_ID)},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "brand_not_found"


# ── DELETE /domains/landing-page ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_landing_page_returns_204(monkeypatch, auth_org_a):
    async def fake_dereg(*, brand_id, organization_id):
        return True

    monkeypatch.setattr(svc, "deregister_landing_page_domain_for_brand", fake_dereg)
    resp = await _client_request(
        "DELETE", f"/api/v1/brands/{BRAND_A}/domains/landing-page"
    )
    assert resp.status_code == 204


# ── Auth: no org context ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_without_org_context_returns_400():
    from fastapi import HTTPException, status

    user = _user(None)
    app.dependency_overrides[verify_supabase_jwt] = lambda: user

    def _deny() -> UserContext:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "organization_context_required"},
        )

    app.dependency_overrides[require_org_context] = _deny
    try:
        resp = await _client_request("GET", f"/api/v1/brands/{BRAND_A}/domains")
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "organization_context_required"
    finally:
        app.dependency_overrides.clear()

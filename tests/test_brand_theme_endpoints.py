"""End-to-end tests for the brand theme + step landing-page-config
PATCH endpoints.

Stubs the underlying services so the router contract (URL paths,
422 mapping, 404 mapping, 204 on clear) is exercised without DB.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx
import pytest

from app.auth.flexible import require_flexible_auth
from app.auth.roles import require_org_context
from app.auth.supabase_jwt import UserContext, verify_supabase_jwt
from app.main import app
from app.services import brands as brands_svc
from app.services import channel_campaign_steps as steps_svc

BRAND_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ORG_A = UUID("11111111-1111-1111-1111-111111111111")
USER_A = UUID("22222222-2222-2222-2222-222222222222")
STEP_A = UUID("33333333-3333-3333-3333-333333333333")


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
def auth_admin():
    user = _user(ORG_A)
    app.dependency_overrides[verify_supabase_jwt] = lambda: user
    app.dependency_overrides[require_flexible_auth] = lambda: user
    app.dependency_overrides[require_org_context] = lambda: user
    yield
    app.dependency_overrides.clear()


async def _request(method: str, path: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.request(method, path, **kwargs)


# ── Brand theme PUT/GET/DELETE ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_put_brand_theme_persists_full_payload(monkeypatch, auth_admin):
    persisted: dict[str, Any] = {}

    async def fake_get_brand(brand_id):
        return brands_svc.Brand(
            id=brand_id,
            name="Acme",
            display_name=None,
            domain=None,
            twilio_messaging_service_sid=None,
            primary_customer_profile_sid=None,
            trust_hub_registration_id=None,
        )

    async def fake_set_theme(brand_id, *, theme):
        persisted["brand_id"] = brand_id
        persisted["theme"] = theme
        return True

    monkeypatch.setattr(brands_svc, "get_brand", fake_get_brand)
    monkeypatch.setattr(brands_svc, "set_theme", fake_set_theme)

    resp = await _request(
        "PUT",
        f"/admin/brands/{BRAND_A}/theme",
        json={
            "logo_url": "https://cdn.acme.com/logo.png",
            "primary_color": "#FF6B35",
            "background_color": "#FFFFFF",
            "font_family": "Inter",
        },
    )
    assert resp.status_code == 200
    assert persisted["theme"]["primary_color"] == "#FF6B35"
    assert persisted["theme"]["logo_url"] == "https://cdn.acme.com/logo.png"


@pytest.mark.asyncio
async def test_put_brand_theme_rejects_invalid_color(monkeypatch, auth_admin):
    async def fake_get_brand(brand_id):
        return brands_svc.Brand(
            id=brand_id,
            name="Acme",
            display_name=None,
            domain=None,
            twilio_messaging_service_sid=None,
            primary_customer_profile_sid=None,
            trust_hub_registration_id=None,
        )

    monkeypatch.setattr(brands_svc, "get_brand", fake_get_brand)

    resp = await _request(
        "PUT",
        f"/admin/brands/{BRAND_A}/theme",
        json={"primary_color": "red"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_put_brand_theme_returns_404_for_unknown_brand(
    monkeypatch, auth_admin
):
    async def fake_get_brand(brand_id):
        return None

    monkeypatch.setattr(brands_svc, "get_brand", fake_get_brand)
    resp = await _request(
        "PUT",
        f"/admin/brands/{BRAND_A}/theme",
        json={"primary_color": "#FF6B35"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_brand_theme_returns_persisted(monkeypatch, auth_admin):
    async def fake_get_brand(brand_id):
        return brands_svc.Brand(
            id=brand_id,
            name="Acme",
            display_name=None,
            domain=None,
            twilio_messaging_service_sid=None,
            primary_customer_profile_sid=None,
            trust_hub_registration_id=None,
        )

    async def fake_get_theme(brand_id):
        return {"primary_color": "#FF6B35", "font_family": "Inter"}

    monkeypatch.setattr(brands_svc, "get_brand", fake_get_brand)
    monkeypatch.setattr(brands_svc, "get_theme", fake_get_theme)

    resp = await _request("GET", f"/admin/brands/{BRAND_A}/theme")
    assert resp.status_code == 200
    body = resp.json()
    assert body["primary_color"] == "#FF6B35"


@pytest.mark.asyncio
async def test_delete_brand_theme_returns_204(monkeypatch, auth_admin):
    cleared: dict[str, Any] = {}

    async def fake_get_brand(brand_id):
        return brands_svc.Brand(
            id=brand_id,
            name="Acme",
            display_name=None,
            domain=None,
            twilio_messaging_service_sid=None,
            primary_customer_profile_sid=None,
            trust_hub_registration_id=None,
        )

    async def fake_set_theme(brand_id, *, theme):
        cleared["theme"] = theme
        return True

    monkeypatch.setattr(brands_svc, "get_brand", fake_get_brand)
    monkeypatch.setattr(brands_svc, "set_theme", fake_set_theme)

    resp = await _request("DELETE", f"/admin/brands/{BRAND_A}/theme")
    assert resp.status_code == 204
    assert cleared["theme"] is None


# ── Step landing-page-config PUT/GET/DELETE ─────────────────────────────


def _step_response_stub(step_id: UUID, organization_id: UUID):
    from datetime import UTC, datetime

    from app.models.campaigns import ChannelCampaignStepResponse

    return ChannelCampaignStepResponse(
        id=step_id,
        channel_campaign_id=UUID("44444444-4444-4444-4444-444444444444"),
        campaign_id=UUID("55555555-5555-5555-5555-555555555555"),
        organization_id=organization_id,
        brand_id=BRAND_A,
        step_order=1,
        name=None,
        delay_days_from_previous=0,
        scheduled_send_at=None,
        creative_ref=None,
        channel_specific_config={},
        external_provider_id=None,
        external_provider_metadata={},
        status="pending",
        activated_at=None,
        metadata={},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_put_step_landing_page_config_persists(monkeypatch, auth_admin):
    persisted: dict[str, Any] = {}

    async def fake_get_step(*, step_id, organization_id):
        return _step_response_stub(step_id, organization_id)

    async def fake_set(*, step_id, organization_id, config):
        persisted["config"] = config
        return True

    # Patch the names imported into the router module.
    from app.routers import channel_campaign_steps as router_mod

    monkeypatch.setattr(router_mod, "get_step", fake_get_step)
    monkeypatch.setattr(router_mod, "set_step_landing_page_config", fake_set)
    monkeypatch.setattr(steps_svc, "get_step", fake_get_step)
    monkeypatch.setattr(steps_svc, "set_step_landing_page_config", fake_set)

    body = {
        "headline": "Hi {recipient.display_name}",
        "body": "Welcome.",
        "cta": {
            "type": "form",
            "label": "Confirm",
            "form_schema": {
                "fields": [
                    {"name": "email", "label": "Email", "type": "email", "required": True}
                ]
            },
            "thank_you_message": "Thanks!",
        },
    }
    resp = await _request(
        "PUT",
        f"/api/v1/channel-campaign-steps/{STEP_A}/landing-page-config",
        json=body,
    )
    assert resp.status_code == 200
    assert persisted["config"]["headline"].startswith("Hi {")
    assert persisted["config"]["cta"]["form_schema"]["fields"][0]["name"] == "email"


@pytest.mark.asyncio
async def test_put_step_landing_page_config_404_for_other_org(
    monkeypatch, auth_admin
):
    async def fake_get_step(*, step_id, organization_id):
        raise steps_svc.StepNotFound("not in org")

    from app.routers import channel_campaign_steps as router_mod

    monkeypatch.setattr(router_mod, "get_step", fake_get_step)
    monkeypatch.setattr(steps_svc, "get_step", fake_get_step)

    body = {
        "headline": "x",
        "body": "y",
        "cta": {
            "type": "form",
            "label": "Confirm",
            "form_schema": {
                "fields": [{"name": "email", "label": "E", "type": "email"}]
            },
        },
    }
    resp = await _request(
        "PUT",
        f"/api/v1/channel-campaign-steps/{STEP_A}/landing-page-config",
        json=body,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_put_step_landing_page_config_rejects_form_without_schema(
    monkeypatch, auth_admin
):
    async def fake_get_step(*, step_id, organization_id):
        return _step_response_stub(step_id, organization_id)

    from app.routers import channel_campaign_steps as router_mod

    monkeypatch.setattr(router_mod, "get_step", fake_get_step)
    monkeypatch.setattr(steps_svc, "get_step", fake_get_step)

    body = {
        "headline": "x",
        "body": "y",
        "cta": {"type": "form", "label": "Confirm"},  # missing form_schema
    }
    resp = await _request(
        "PUT",
        f"/api/v1/channel-campaign-steps/{STEP_A}/landing-page-config",
        json=body,
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_get_step_landing_page_config_returns_jsonb(
    monkeypatch, auth_admin
):
    async def fake_get_step(*, step_id, organization_id):
        return _step_response_stub(step_id, organization_id)

    async def fake_get_config(*, step_id, organization_id):
        return {"headline": "Hi", "body": "x", "cta": {"type": "form", "label": "Go"}}

    from app.routers import channel_campaign_steps as router_mod

    monkeypatch.setattr(router_mod, "get_step", fake_get_step)
    monkeypatch.setattr(router_mod, "get_step_landing_page_config", fake_get_config)
    monkeypatch.setattr(steps_svc, "get_step", fake_get_step)
    monkeypatch.setattr(steps_svc, "get_step_landing_page_config", fake_get_config)

    resp = await _request(
        "GET",
        f"/api/v1/channel-campaign-steps/{STEP_A}/landing-page-config",
    )
    assert resp.status_code == 200
    assert resp.json()["headline"] == "Hi"


@pytest.mark.asyncio
async def test_delete_step_landing_page_config_returns_204(
    monkeypatch, auth_admin
):
    cleared: dict[str, Any] = {}

    async def fake_get_step(*, step_id, organization_id):
        return _step_response_stub(step_id, organization_id)

    async def fake_set(*, step_id, organization_id, config):
        cleared["config"] = config
        return True

    from app.routers import channel_campaign_steps as router_mod

    monkeypatch.setattr(router_mod, "get_step", fake_get_step)
    monkeypatch.setattr(router_mod, "set_step_landing_page_config", fake_set)
    monkeypatch.setattr(steps_svc, "get_step", fake_get_step)
    monkeypatch.setattr(steps_svc, "set_step_landing_page_config", fake_set)

    resp = await _request(
        "DELETE",
        f"/api/v1/channel-campaign-steps/{STEP_A}/landing-page-config",
    )
    assert resp.status_code == 204
    assert cleared["config"] is None

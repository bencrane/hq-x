"""Tests for POST /api/v1/dmaas/campaigns.

Stubs every service the router calls so the contract (validation,
ordering of stages, response shape, error mapping, recipient cap) is
exercised without DB or external providers.
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
from app.models.campaigns import (
    CampaignResponse,
    ChannelCampaignResponse,
    ChannelCampaignStepResponse,
)
from app.routers import dmaas_campaigns as router_mod
from app.services import campaigns as campaigns_svc
from app.services import channel_campaign_steps as steps_svc
from app.services import channel_campaigns as channel_campaigns_svc

# brand_domains is in a sibling PR (Slice 1); skip stubbing it when the
# module isn't present yet — the router falls back to settings-based
# URL resolution anyway.
try:
    from app.services import brand_domains as brand_domains_svc  # type: ignore[import]
except ImportError:
    brand_domains_svc = None  # type: ignore[assignment]

ORG = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
USER = UUID("11111111-1111-1111-1111-111111111111")
BRAND = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
CAMP = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
CC = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
STEP = UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")


def _user(org_id: UUID | None) -> UserContext:
    return UserContext(
        auth_user_id=USER,
        business_user_id=USER,
        email="op@example.com",
        platform_role="platform_operator",
        active_organization_id=org_id,
        org_role=None,
        role="operator",
        client_id=None,
    )


@pytest.fixture
def auth_org_a():
    user = _user(ORG)
    app.dependency_overrides[verify_supabase_jwt] = lambda: user
    app.dependency_overrides[require_org_context] = lambda: user
    yield
    app.dependency_overrides.clear()


def _campaign_response(*, organization_id=ORG, brand_id=BRAND, **kw) -> CampaignResponse:
    now = datetime.now(UTC)
    return CampaignResponse(
        id=kw.get("id", CAMP),
        organization_id=organization_id,
        brand_id=brand_id,
        name=kw.get("name", "Test"),
        description=kw.get("description"),
        status="draft",
        start_date=kw.get("start_date"),
        metadata={},
        created_by_user_id=USER,
        created_at=now,
        updated_at=now,
        archived_at=None,
    )


def _channel_campaign_response() -> ChannelCampaignResponse:
    now = datetime.now(UTC)
    return ChannelCampaignResponse(
        id=CC,
        campaign_id=CAMP,
        organization_id=ORG,
        brand_id=BRAND,
        name="Test",
        channel="direct_mail",
        provider="lob",
        audience_spec_id=None,
        audience_snapshot_count=3,
        status="draft",
        start_offset_days=0,
        scheduled_send_at=None,
        schedule_config={},
        provider_config={},
        design_id=None,
        metadata={},
        created_by_user_id=USER,
        created_at=now,
        updated_at=now,
        archived_at=None,
    )


def _step_response(status_="scheduled", external_provider_id="lob_cmp_abc"):
    now = datetime.now(UTC)
    return ChannelCampaignStepResponse(
        id=STEP,
        channel_campaign_id=CC,
        campaign_id=CAMP,
        organization_id=ORG,
        brand_id=BRAND,
        step_order=1,
        name="Test",
        delay_days_from_previous=0,
        scheduled_send_at=now,
        creative_ref=None,
        channel_specific_config={},
        external_provider_id=external_provider_id,
        external_provider_metadata={},
        status=status_,
        activated_at=now,
        metadata={},
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def stub_pipeline(monkeypatch):
    """Stub out every service the router calls. Returns a state dict the
    test can introspect after the request to assert ordering."""
    state: dict[str, Any] = {
        "create_campaign_calls": [],
        "create_cc_calls": [],
        "create_step_calls": [],
        "set_lp_config_calls": [],
        "materialize_calls": [],
        "activate_calls": [],
        "landing_page_domain": None,
    }

    async def fake_create_campaign(**kwargs):
        state["create_campaign_calls"].append(kwargs)
        return _campaign_response()

    async def fake_create_cc(**kwargs):
        state["create_cc_calls"].append(kwargs)
        return _channel_campaign_response()

    async def fake_create_step(**kwargs):
        state["create_step_calls"].append(kwargs)
        return _step_response(status_="pending", external_provider_id=None)

    async def fake_set_lp(**kwargs):
        state["set_lp_config_calls"].append(kwargs)
        return True

    async def fake_materialize(**kwargs):
        state["materialize_calls"].append(kwargs)
        return []

    async def fake_activate(**kwargs):
        state["activate_calls"].append(kwargs)
        return _step_response()

    async def fake_get_landing_domain(*, brand_id):
        return state["landing_page_domain"]

    monkeypatch.setattr(campaigns_svc, "create_campaign", fake_create_campaign)
    monkeypatch.setattr(router_mod.campaigns_svc, "create_campaign", fake_create_campaign)
    monkeypatch.setattr(channel_campaigns_svc, "create_channel_campaign", fake_create_cc)
    monkeypatch.setattr(
        router_mod.channel_campaigns_svc, "create_channel_campaign", fake_create_cc
    )
    monkeypatch.setattr(steps_svc, "create_step", fake_create_step)
    monkeypatch.setattr(steps_svc, "materialize_step_audience", fake_materialize)
    monkeypatch.setattr(steps_svc, "set_step_landing_page_config", fake_set_lp)
    monkeypatch.setattr(steps_svc, "activate_step", fake_activate)
    monkeypatch.setattr(router_mod.steps_svc, "create_step", fake_create_step)
    monkeypatch.setattr(
        router_mod.steps_svc, "materialize_step_audience", fake_materialize
    )
    monkeypatch.setattr(
        router_mod.steps_svc, "set_step_landing_page_config", fake_set_lp
    )
    monkeypatch.setattr(router_mod.steps_svc, "activate_step", fake_activate)
    if brand_domains_svc is not None:
        monkeypatch.setattr(
            brand_domains_svc,
            "get_brand_landing_page_domain",
            fake_get_landing_domain,
        )
    if router_mod.brand_domains_svc is not None:
        monkeypatch.setattr(
            router_mod.brand_domains_svc,
            "get_brand_landing_page_domain",
            fake_get_landing_domain,
        )

    return state


def _request_body_with_landing_page() -> dict[str, Any]:
    return {
        "name": "Q2 lapsed",
        "brand_id": str(BRAND),
        "send_date": "2026-05-15",
        "creative": {
            "lob_creative_payload": {"front_html": "<html/>", "back_html": "<html/>"}
        },
        "use_landing_page": True,
        "landing_page": {
            "headline": "Hi {recipient.display_name}",
            "body": "We pulled your data.",
            "cta": {
                "type": "form",
                "label": "Schedule",
                "form_schema": {
                    "fields": [
                        {"name": "name", "label": "Name", "type": "text", "required": True},
                        {"name": "email", "label": "Email", "type": "email", "required": True},
                    ]
                },
                "thank_you_message": "Thanks!",
            },
        },
        "recipients": [
            {
                "external_source": "fmcsa",
                "external_id": "100001",
                "display_name": "ACME Trucking",
                "mailing_address": {"city": "Brooklyn"},
            },
            {
                "external_source": "fmcsa",
                "external_id": "100002",
                "display_name": "Fleet Co",
                "mailing_address": {"city": "Queens"},
            },
        ],
    }


async def _post(path: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=30.0
    ) as c:
        return await c.post(path, **kwargs)


# ── Happy path ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_creates_full_hierarchy(
    monkeypatch, auth_org_a, stub_pipeline
):
    body = _request_body_with_landing_page()
    resp = await _post("/api/v1/dmaas/campaigns", json=body)
    assert resp.status_code == 201, resp.text

    payload = resp.json()
    assert payload["campaign_id"] == str(CAMP)
    assert payload["channel_campaign_id"] == str(CC)
    assert payload["step_id"] == str(STEP)
    assert payload["external_provider_id"] == "lob_cmp_abc"
    assert payload["recipient_count"] == 2
    assert payload["status"] == "scheduled"

    # Ordering assertion: campaign → channel_campaign → step → set_lp →
    # materialize → activate. Not interleaved.
    assert len(stub_pipeline["create_campaign_calls"]) == 1
    assert len(stub_pipeline["create_cc_calls"]) == 1
    assert len(stub_pipeline["create_step_calls"]) == 1
    assert len(stub_pipeline["set_lp_config_calls"]) == 1
    assert len(stub_pipeline["materialize_calls"]) == 1
    assert len(stub_pipeline["activate_calls"]) == 1

    # The landing-page-config persisted the validated payload.
    persisted_lp = stub_pipeline["set_lp_config_calls"][0]["config"]
    assert persisted_lp["headline"].startswith("Hi {")
    assert persisted_lp["cta"]["form_schema"]["fields"][0]["name"] == "name"


@pytest.mark.asyncio
@pytest.mark.skipif(
    brand_domains_svc is None,
    reason="brand_domains_svc not present (Slice 1 sibling PR)",
)
async def test_happy_path_returns_landing_url_with_brand_domain(
    monkeypatch, auth_org_a, stub_pipeline
):
    stub_pipeline["landing_page_domain"] = "pages.acme.com"
    resp = await _post(
        "/api/v1/dmaas/campaigns", json=_request_body_with_landing_page()
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["landing_page_url"] == f"https://pages.acme.com/lp/{STEP}"


@pytest.mark.asyncio
async def test_happy_path_uses_destination_url_when_landing_disabled(
    monkeypatch, auth_org_a, stub_pipeline
):
    body = _request_body_with_landing_page()
    body["use_landing_page"] = False
    body["landing_page"] = None
    body["destination_url_override"] = "https://acme.com/promo"
    resp = await _post("/api/v1/dmaas/campaigns", json=body)
    assert resp.status_code == 201
    payload = resp.json()
    assert payload["landing_page_url"] is None
    # set_step_landing_page_config NOT called when use_landing_page=False.
    assert stub_pipeline["set_lp_config_calls"] == []


# ── Validation ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_landing_page_required_when_use_landing_page_true(
    monkeypatch, auth_org_a, stub_pipeline
):
    body = _request_body_with_landing_page()
    body["landing_page"] = None
    resp = await _post("/api/v1/dmaas/campaigns", json=body)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_destination_url_required_when_use_landing_page_false(
    monkeypatch, auth_org_a, stub_pipeline
):
    body = _request_body_with_landing_page()
    body["use_landing_page"] = False
    body["landing_page"] = None
    body["destination_url_override"] = None
    resp = await _post("/api/v1/dmaas/campaigns", json=body)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_mutual_exclusivity_landing_page_and_override(
    monkeypatch, auth_org_a, stub_pipeline
):
    body = _request_body_with_landing_page()
    body["destination_url_override"] = "https://acme.com/promo"
    resp = await _post("/api/v1/dmaas/campaigns", json=body)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_recipients_cap_enforced(monkeypatch, auth_org_a, stub_pipeline):
    body = _request_body_with_landing_page()
    body["recipients"] = [
        {
            "external_source": "fmcsa",
            "external_id": str(i),
            "display_name": "x",
            "mailing_address": {},
        }
        for i in range(50_001)
    ]
    resp = await _post("/api/v1/dmaas/campaigns", json=body)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_empty_recipients_rejected(
    monkeypatch, auth_org_a, stub_pipeline
):
    body = _request_body_with_landing_page()
    body["recipients"] = []
    resp = await _post("/api/v1/dmaas/campaigns", json=body)
    assert resp.status_code == 422


# ── Cross-org guard ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_brand_in_other_org_returns_404(
    monkeypatch, auth_org_a, stub_pipeline
):
    async def fake_create_campaign(**kwargs):
        raise campaigns_svc.CampaignBrandMismatch("brand not in org")

    monkeypatch.setattr(campaigns_svc, "create_campaign", fake_create_campaign)
    monkeypatch.setattr(router_mod.campaigns_svc, "create_campaign", fake_create_campaign)

    body = _request_body_with_landing_page()
    resp = await _post("/api/v1/dmaas/campaigns", json=body)
    assert resp.status_code == 404


# ── Activation failure ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_activation_not_implemented_returns_501(
    monkeypatch, auth_org_a, stub_pipeline
):
    async def fake_activate(**kwargs):
        raise steps_svc.StepActivationNotImplemented("voice not wired")

    monkeypatch.setattr(steps_svc, "activate_step", fake_activate)
    monkeypatch.setattr(router_mod.steps_svc, "activate_step", fake_activate)

    body = _request_body_with_landing_page()
    resp = await _post("/api/v1/dmaas/campaigns", json=body)
    assert resp.status_code == 501
    detail = resp.json()["detail"]
    # Response carries the partially-created ids so the operator can resume.
    assert detail["campaign_id"] == str(CAMP)
    assert detail["step_id"] == str(STEP)

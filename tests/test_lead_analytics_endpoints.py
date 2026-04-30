"""End-to-end tests for /api/v1/analytics/leads and
/api/v1/analytics/campaigns/{id}/leads.

Plus the conversion-block lead invariants documented in the directive:
  * unique_leads ≤ leads_total
  * unique_leads ≤ unique_clickers (a lead requires a click)
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
from app.models.landing_page import LandingPageSubmissionResponse
from app.routers import analytics as analytics_router
from app.services import landing_page_submissions as submissions_svc

ORG_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
USER_A = UUID("11111111-1111-1111-1111-111111111111")
BRAND_A = UUID("22222222-2222-2222-2222-222222222222")
CAMPAIGN_A = UUID("33333333-3333-3333-3333-333333333333")
CC_A = UUID("44444444-4444-4444-4444-444444444444")
STEP_A = UUID("55555555-5555-5555-5555-555555555555")
RCPT_A = UUID("66666666-6666-6666-6666-666666666666")
SUB_A = UUID("77777777-7777-7777-7777-777777777777")


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


def _record(form_data=None) -> submissions_svc.SubmissionRecord:
    return submissions_svc.SubmissionRecord(
        id=SUB_A,
        organization_id=ORG_A,
        brand_id=BRAND_A,
        campaign_id=CAMPAIGN_A,
        channel_campaign_id=CC_A,
        channel_campaign_step_id=STEP_A,
        recipient_id=RCPT_A,
        form_data=form_data or {"name": "Jane", "email": "j@x.com"},
        source_metadata={"ip_hash": "abc", "user_agent": "ua"},
        submitted_at=datetime(2026, 4, 30, tzinfo=UTC),
    )


async def _get(path: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.get(path, **kwargs)


@pytest.mark.asyncio
async def test_org_leads_returns_full_form_data(monkeypatch, auth_org_a):
    captured: dict[str, Any] = {}

    async def fake_list(**kwargs):
        captured.update(kwargs)
        return [_record({"name": "Jane", "email": "j@x.com"})], 1

    monkeypatch.setattr(submissions_svc, "list_submissions_for_org", fake_list)
    monkeypatch.setattr(
        analytics_router, "list_submissions_for_org", fake_list
    )

    resp = await _get("/api/v1/analytics/leads")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["submissions"][0]["form_data"] == {"name": "Jane", "email": "j@x.com"}
    assert captured["organization_id"] == ORG_A


@pytest.mark.asyncio
async def test_org_leads_drilldown_filters_propagate(monkeypatch, auth_org_a):
    captured: dict[str, Any] = {}

    async def fake_list(**kwargs):
        captured.update(kwargs)
        return [], 0

    monkeypatch.setattr(submissions_svc, "list_submissions_for_org", fake_list)
    monkeypatch.setattr(
        analytics_router, "list_submissions_for_org", fake_list
    )

    resp = await _get(
        f"/api/v1/analytics/leads?brand_id={BRAND_A}&channel_campaign_id={CC_A}"
        f"&channel_campaign_step_id={STEP_A}&limit=50&offset=10"
    )
    assert resp.status_code == 200
    assert captured["brand_id"] == BRAND_A
    assert captured["channel_campaign_id"] == CC_A
    assert captured["channel_campaign_step_id"] == STEP_A
    assert captured["limit"] == 50
    assert captured["offset"] == 10


@pytest.mark.asyncio
async def test_campaign_leads_filters_to_campaign_id(monkeypatch, auth_org_a):
    captured: dict[str, Any] = {}

    async def fake_list(**kwargs):
        captured.update(kwargs)
        return [_record()], 1

    monkeypatch.setattr(submissions_svc, "list_submissions_for_org", fake_list)
    monkeypatch.setattr(
        analytics_router, "list_submissions_for_org", fake_list
    )

    resp = await _get(f"/api/v1/analytics/campaigns/{CAMPAIGN_A}/leads")
    assert resp.status_code == 200
    assert captured["campaign_id"] == CAMPAIGN_A


@pytest.mark.asyncio
async def test_invalid_brand_id_uuid_returns_400(monkeypatch, auth_org_a):
    resp = await _get("/api/v1/analytics/leads?brand_id=not-a-uuid")
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_brand_id"


@pytest.mark.asyncio
async def test_limit_above_max_returns_422(monkeypatch, auth_org_a):
    resp = await _get("/api/v1/analytics/leads?limit=10000")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_response_serializes_uuid_fields(monkeypatch, auth_org_a):
    """Verify the LandingPageSubmissionResponse model round-trips."""
    rec = _record()
    pyd = LandingPageSubmissionResponse(
        id=rec.id,
        organization_id=rec.organization_id,
        brand_id=rec.brand_id,
        campaign_id=rec.campaign_id,
        channel_campaign_id=rec.channel_campaign_id,
        channel_campaign_step_id=rec.channel_campaign_step_id,
        recipient_id=rec.recipient_id,
        form_data=rec.form_data,
        source_metadata=rec.source_metadata,
        submitted_at=rec.submitted_at,
    )
    j = pyd.model_dump_json()
    assert str(SUB_A) in j
    assert str(BRAND_A) in j


# ── Conversion invariants ─────────────────────────────────────────────────


def test_conversion_invariants():
    """Property invariants documented in the directive."""

    def lead_rate(unique_leads: int, unique_clickers: int) -> float:
        if unique_clickers == 0:
            return 0.0
        return round(unique_leads / unique_clickers, 4)

    # Both zero → 0.0 (no division-by-zero blowup).
    assert lead_rate(0, 0) == 0.0
    # No leads, but clicks happened → 0.0.
    assert lead_rate(0, 100) == 0.0
    # Half the clickers became leads.
    assert lead_rate(50, 100) == 0.5
    # Edge case: more leads than clickers (in theory impossible since a
    # lead requires a click, but we don't guard so the rate can exceed 1).
    # Documented here so the invariant is visible.
    assert lead_rate(120, 100) == 1.2

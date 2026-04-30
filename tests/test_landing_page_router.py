"""Tests for `GET /lp/{step_id}/{short_code}`.

Stubs every dependency the route reaches into (dub_links repo, step
context, brand theme, recipient lookup, landing_page_views repo,
emit_event) so we exercise the contract without DB or RudderStack.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest

from app.dmaas import dub_links as dub_links_repo
from app.dmaas import landing_page_views as views_repo
from app.dmaas.dub_links import DubLinkRecord
from app.main import app
from app.models.recipients import RecipientResponse
from app.routers import landing_pages as router_mod
from app.services import brands as brands_svc
from app.services import channel_campaign_steps as steps_svc
from app.services import landing_page_render as render_svc

STEP = UUID("11111111-1111-1111-1111-111111111111")
ORG = UUID("22222222-2222-2222-2222-222222222222")
BRAND = UUID("33333333-3333-3333-3333-333333333333")
CAMP = UUID("44444444-4444-4444-4444-444444444444")
CC = UUID("55555555-5555-5555-5555-555555555555")
RCPT = UUID("66666666-6666-6666-6666-666666666666")


@pytest.fixture
def stub_render_path(monkeypatch):
    """Wire all DB reads through in-memory state so the render path can
    resolve without a real DB."""
    state: dict[str, Any] = {
        "link": DubLinkRecord(
            id=UUID("77777777-7777-7777-7777-777777777777"),
            dub_link_id="link_x",
            dub_external_id=f"step:{STEP}:rcpt:{RCPT}",
            dub_short_url="https://track.acme.com/abc123",
            dub_domain="track.acme.com",
            dub_key="abc123",
            destination_url="https://landing.example.com",
            dmaas_design_id=None,
            direct_mail_piece_id=None,
            brand_id=BRAND,
            channel_campaign_step_id=STEP,
            recipient_id=RCPT,
            dub_folder_id=None,
            dub_tag_ids=[],
            attribution_context={},
            created_by_user_id=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        ),
        "step_context": {
            "organization_id": str(ORG),
            "brand_id": str(BRAND),
            "campaign_id": str(CAMP),
            "channel_campaign_id": str(CC),
            "channel_campaign_step_id": str(STEP),
            "channel": "direct_mail",
            "provider": "lob",
        },
        "page_config": {
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
            },
        },
        "theme": {"primary_color": "#FF6B35"},
        "recipient": RecipientResponse(
            id=RCPT,
            organization_id=ORG,
            recipient_type="business",
            external_source="fmcsa",
            external_id="123",
            display_name="Jane Smith",
            mailing_address={"city": "Brooklyn"},
            phone=None,
            email="jane@example.com",
            metadata={},
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        ),
        "view_inserts": [],
        "emit_calls": [],
        "deduped": False,
    }

    async def fake_find_link(*, channel_campaign_step_id, short_code):
        if short_code != "abc123" or channel_campaign_step_id != STEP:
            return None
        return state["link"]

    async def fake_get_step_context(*, step_id):
        if step_id != STEP:
            return None
        return state["step_context"]

    async def fake_get_lp_config(*, step_id, organization_id):
        if step_id != STEP or organization_id != ORG:
            return None
        return state["page_config"]

    async def fake_get_recipient(*, recipient_id, organization_id):
        if recipient_id == RCPT and organization_id == ORG:
            return state["recipient"]
        return None

    async def fake_get_theme(brand_id):
        return state["theme"]

    async def fake_has_recent(*, channel_campaign_step_id, ip_hash, within_seconds):
        return state["deduped"]

    async def fake_insert_view(**kwargs):
        state["view_inserts"].append(kwargs)
        return None

    async def fake_emit(**kwargs):
        state["emit_calls"].append(kwargs)
        return None

    monkeypatch.setattr(
        dub_links_repo, "find_dub_link_for_step_short_code", fake_find_link
    )
    monkeypatch.setattr(
        render_svc.dub_links_repo,
        "find_dub_link_for_step_short_code",
        fake_find_link,
    )
    monkeypatch.setattr(steps_svc, "get_step_context", fake_get_step_context)
    monkeypatch.setattr(
        render_svc.steps_svc, "get_step_context", fake_get_step_context
    )
    monkeypatch.setattr(
        steps_svc, "get_step_landing_page_config", fake_get_lp_config
    )
    monkeypatch.setattr(
        render_svc.steps_svc,
        "get_step_landing_page_config",
        fake_get_lp_config,
    )
    monkeypatch.setattr(
        render_svc.recipients_svc, "get_recipient", fake_get_recipient
    )
    monkeypatch.setattr(brands_svc, "get_theme", fake_get_theme)
    monkeypatch.setattr(render_svc.brands_svc, "get_theme", fake_get_theme)
    monkeypatch.setattr(views_repo, "has_recent_view_for_ip", fake_has_recent)
    monkeypatch.setattr(views_repo, "insert_view", fake_insert_view)
    monkeypatch.setattr(
        router_mod.landing_page_views, "has_recent_view_for_ip", fake_has_recent
    )
    monkeypatch.setattr(
        router_mod.landing_page_views, "insert_view", fake_insert_view
    )
    monkeypatch.setattr(router_mod, "emit_event", fake_emit)

    return state


async def _request(path: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.get(path, **kwargs)


@pytest.mark.asyncio
async def test_render_returns_html_with_personalized_headline(stub_render_path):
    resp = await _request(f"/lp/{STEP}/abc123")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "Hi Jane Smith" in resp.text
    assert "Welcome." in resp.text


@pytest.mark.asyncio
async def test_render_emits_page_viewed_with_six_tuple(stub_render_path):
    await _request(f"/lp/{STEP}/abc123")
    assert len(stub_render_path["emit_calls"]) == 1
    call = stub_render_path["emit_calls"][0]
    assert call["event_name"] == "page.viewed"
    assert call["channel_campaign_step_id"] == STEP
    assert call["recipient_id"] == RCPT
    assert "ip_hash" in call["properties"]
    assert "user_agent" in call["properties"]


@pytest.mark.asyncio
async def test_render_inserts_landing_page_view(stub_render_path):
    await _request(f"/lp/{STEP}/abc123")
    assert len(stub_render_path["view_inserts"]) == 1
    inserted = stub_render_path["view_inserts"][0]
    assert inserted["organization_id"] == ORG
    assert inserted["brand_id"] == BRAND
    assert inserted["channel_campaign_step_id"] == STEP
    assert inserted["recipient_id"] == RCPT
    # Raw IP never lands; only the hash.
    assert "ip_hash" in inserted["source_metadata"]
    assert "ip" not in inserted["source_metadata"]


@pytest.mark.asyncio
async def test_render_dedupes_recent_views(stub_render_path):
    """When has_recent_view_for_ip returns True, page still renders but
    no second emit_event / insert_view fires."""
    stub_render_path["deduped"] = True
    resp = await _request(f"/lp/{STEP}/abc123")
    assert resp.status_code == 200
    assert "Hi Jane Smith" in resp.text
    assert stub_render_path["emit_calls"] == []
    assert stub_render_path["view_inserts"] == []


@pytest.mark.asyncio
async def test_unknown_short_code_returns_branded_404(stub_render_path):
    resp = await _request(f"/lp/{STEP}/does_not_exist")
    assert resp.status_code == 404
    assert "Page not found" in resp.text
    # Brand theme came through (404 page renders with its colors).
    assert "<html" in resp.text


@pytest.mark.asyncio
async def test_unknown_step_returns_404(stub_render_path):
    other_step = UUID("99999999-9999-9999-9999-999999999999")
    resp = await _request(f"/lp/{other_step}/abc123")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_render_xss_safe_in_personalization(stub_render_path):
    """Operator-supplied page text is escaped; recipient-supplied display
    name (untrusted) is also escaped before substitution lands in HTML."""
    stub_render_path["recipient"] = RecipientResponse(
        id=RCPT,
        organization_id=ORG,
        recipient_type="business",
        external_source="fmcsa",
        external_id="123",
        display_name="<script>alert(1)</script>",
        mailing_address={},
        phone=None,
        email=None,
        metadata={},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    resp = await _request(f"/lp/{STEP}/abc123")
    assert resp.status_code == 200
    assert "<script>alert(1)</script>" not in resp.text
    assert "&lt;script&gt;" in resp.text


@pytest.mark.asyncio
async def test_step_without_landing_page_config_returns_404(
    stub_render_path,
):
    stub_render_path["page_config"] = None
    resp = await _request(f"/lp/{STEP}/abc123")
    assert resp.status_code == 404

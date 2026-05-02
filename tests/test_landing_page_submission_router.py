"""End-to-end tests for `POST /lp/{step_id}/{short_code}/submit`.

Stubs every dependency the route reaches into (dub_links, step context,
landing_page_config lookup, brand theme, record_submission, emit_event)
so we exercise the contract without DB or RudderStack.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest

from app.dmaas import dub_links as dub_links_repo
from app.dmaas.dub_links import DubLinkRecord
from app.main import app
from app.routers import landing_pages as router_mod
from app.services import brands as brands_svc
from app.services import channel_campaign_steps as steps_svc
from app.services import landing_page_submissions as submissions_svc

STEP = UUID("11111111-1111-1111-1111-111111111111")
ORG = UUID("22222222-2222-2222-2222-222222222222")
BRAND = UUID("33333333-3333-3333-3333-333333333333")
CAMP = UUID("44444444-4444-4444-4444-444444444444")
CC = UUID("55555555-5555-5555-5555-555555555555")
RCPT = UUID("66666666-6666-6666-6666-666666666666")


@pytest.fixture
def stub_submit_path(monkeypatch):
    state: dict[str, Any] = {
        "page_config": {
            "headline": "Hi",
            "body": "x",
            "cta": {
                "type": "form",
                "label": "Confirm",
                "form_schema": {
                    "fields": [
                        {"name": "name", "label": "Name", "type": "text", "required": True},
                        {"name": "email", "label": "Email", "type": "email", "required": True},
                    ]
                },
                "thank_you_message": "Got it!",
            },
        },
        "submissions": [],
        "emit_calls": [],
        "rate_state": False,
    }

    async def fake_find_link(*, channel_campaign_step_id, short_code):
        if channel_campaign_step_id != STEP or short_code != "abc123":
            return None
        return DubLinkRecord(
            id=UUID("77777777-7777-7777-7777-777777777777"),
            dub_link_id="link_x",
            dub_external_id=None,
            dub_short_url="https://t.acme.com/abc123",
            dub_domain="t.acme.com",
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
        )

    async def fake_get_step_context(*, step_id):
        if step_id != STEP:
            return None
        return {
            "organization_id": str(ORG),
            "brand_id": str(BRAND),
            "campaign_id": str(CAMP),
            "channel_campaign_id": str(CC),
            "channel_campaign_step_id": str(STEP),
            "channel": "direct_mail",
            "provider": "lob",
        }

    async def fake_get_lp_config(*, step_id, organization_id):
        return state["page_config"]

    async def fake_record_submission(**kwargs):
        state["submissions"].append(kwargs)
        return submissions_svc.SubmissionRecord(
            id=UUID("88888888-8888-8888-8888-888888888888"),
            organization_id=kwargs["organization_id"],
            brand_id=kwargs["brand_id"],
            campaign_id=kwargs["campaign_id"],
            channel_campaign_id=kwargs["channel_campaign_id"],
            channel_campaign_step_id=kwargs["channel_campaign_step_id"],
            recipient_id=kwargs["recipient_id"],
            form_data=kwargs["form_data"],
            source_metadata=kwargs.get("source_metadata"),
            submitted_at=datetime.now(UTC),
        )

    async def fake_emit(**kwargs):
        state["emit_calls"].append(kwargs)
        return None

    async def fake_get_theme(brand_id):
        return {"primary_color": "#FF6B35"}

    monkeypatch.setattr(
        dub_links_repo, "find_dub_link_for_step_short_code", fake_find_link
    )
    monkeypatch.setattr(
        router_mod.dub_links_repo,
        "find_dub_link_for_step_short_code",
        fake_find_link,
    )
    monkeypatch.setattr(steps_svc, "get_step_context", fake_get_step_context)
    monkeypatch.setattr(
        router_mod.steps_svc, "get_step_context", fake_get_step_context
    )
    monkeypatch.setattr(
        steps_svc, "get_step_landing_page_config", fake_get_lp_config
    )
    monkeypatch.setattr(
        router_mod.steps_svc,
        "get_step_landing_page_config",
        fake_get_lp_config,
    )
    monkeypatch.setattr(submissions_svc, "record_submission", fake_record_submission)
    monkeypatch.setattr(
        router_mod.submissions_svc, "record_submission", fake_record_submission
    )
    monkeypatch.setattr(brands_svc, "get_theme", fake_get_theme)
    monkeypatch.setattr(router_mod.brands_svc, "get_theme", fake_get_theme)
    monkeypatch.setattr(router_mod, "emit_event", fake_emit)

    # Reset rate-limit state across tests.
    router_mod._SUBMISSION_LAST_SEEN.clear()

    return state


async def _post(path: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.post(path, **kwargs)


@pytest.mark.asyncio
async def test_happy_path_persists_and_emits(stub_submit_path):
    resp = await _post(
        f"/lp/{STEP}/abc123/submit",
        data={"name": "Jane", "email": "Jane@Example.com"},
    )
    assert resp.status_code == 200
    assert "Got it!" in resp.text
    assert len(stub_submit_path["submissions"]) == 1
    persisted = stub_submit_path["submissions"][0]
    assert persisted["form_data"]["name"] == "Jane"
    assert persisted["form_data"]["email"] == "jane@example.com"  # lowercased
    # Source metadata only carries hashed IP — no raw IP.
    assert "ip_hash" in persisted["source_metadata"]
    assert "ip" not in persisted["source_metadata"]

    assert len(stub_submit_path["emit_calls"]) == 1
    emit = stub_submit_path["emit_calls"][0]
    assert emit["event_name"] == "page.submitted"
    assert emit["channel_campaign_step_id"] == STEP
    assert emit["recipient_id"] == RCPT
    # PII never lands in the event payload — only field NAMES.
    assert emit["properties"]["form_field_names"] == ["email", "name"]
    assert "Jane" not in str(emit["properties"])
    assert "jane@example.com" not in str(emit["properties"])


@pytest.mark.asyncio
async def test_validation_failure_returns_422_with_field_map(stub_submit_path):
    resp = await _post(
        f"/lp/{STEP}/abc123/submit",
        data={"name": "Jane"},  # missing email
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["detail"]["error"] == "form_validation_failed"
    assert "email" in body["detail"]["errors"]
    assert stub_submit_path["submissions"] == []


@pytest.mark.asyncio
async def test_honeypot_trip_returns_silent_200(stub_submit_path):
    resp = await _post(
        f"/lp/{STEP}/abc123/submit",
        data={
            "name": "Jane",
            "email": "j@x.com",
            "company_website": "http://botspam.test/",
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    # Crucially: nothing persisted. Bot doesn't learn anything.
    assert stub_submit_path["submissions"] == []
    assert stub_submit_path["emit_calls"] == []


@pytest.mark.asyncio
async def test_unknown_short_code_returns_404(stub_submit_path):
    resp = await _post(
        f"/lp/{STEP}/does_not_exist/submit",
        data={"name": "x", "email": "j@x.com"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_rate_limit_second_submit_blocked(stub_submit_path):
    """Two submits from the same hashed IP within 30s — second is 429."""
    headers = {"x-forwarded-for": "1.2.3.4"}
    r1 = await _post(
        f"/lp/{STEP}/abc123/submit",
        data={"name": "Jane", "email": "j@x.com"},
        headers=headers,
    )
    assert r1.status_code == 200
    r2 = await _post(
        f"/lp/{STEP}/abc123/submit",
        data={"name": "Jane", "email": "j2@x.com"},
        headers=headers,
    )
    assert r2.status_code == 429
    assert r2.json()["detail"]["error"] == "rate_limited"


@pytest.mark.asyncio
async def test_redirect_when_thank_you_url_set(stub_submit_path):
    stub_submit_path["page_config"]["cta"]["thank_you_redirect_url"] = (
        "https://acme.com/thanks"
    )
    resp = await _post(
        f"/lp/{STEP}/abc123/submit",
        data={"name": "Jane", "email": "j@x.com"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "https://acme.com/thanks"


@pytest.mark.asyncio
async def test_json_body_also_accepted(stub_submit_path):
    resp = await _post(
        f"/lp/{STEP}/abc123/submit",
        json={"name": "Jane", "email": "j@x.com"},
    )
    assert resp.status_code == 200
    assert len(stub_submit_path["submissions"]) == 1


@pytest.mark.asyncio
async def test_submit_with_non_form_cta_returns_400(stub_submit_path):
    stub_submit_path["page_config"]["cta"] = {
        "type": "external_url",
        "label": "Visit",
        "target_url": "https://acme.com/x",
    }
    resp = await _post(
        f"/lp/{STEP}/abc123/submit",
        data={"name": "x"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "submit_not_supported"

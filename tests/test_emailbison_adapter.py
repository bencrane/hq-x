"""EmailBison adapter tests — pure logic, no DB, no network.

The HTTP client is patched via monkeypatch on the module-level functions
the adapter calls. Webhook parsing is exercised against the §5 sample
envelopes verbatim.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from app.models.campaigns import (
    ChannelCampaignResponse,
    ChannelCampaignStepResponse,
)
from app.providers.emailbison import client as eb_client
from app.providers.emailbison.adapter import (
    EmailBisonAdapter,
    EmailBisonProviderError,
    _parse_six_tuple_from_tags,
    _step_tag_strings,
)


def _make_step(**overrides) -> ChannelCampaignStepResponse:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    base = dict(
        id=uuid4(),
        channel_campaign_id=uuid4(),
        campaign_id=uuid4(),
        organization_id=uuid4(),
        brand_id=uuid4(),
        step_order=1,
        name="Outreach v1",
        delay_days_from_previous=0,
        scheduled_send_at=None,
        creative_ref=None,
        channel_specific_config={},
        external_provider_id=None,
        external_provider_metadata={},
        status="pending",
        activated_at=None,
        metadata={},
        created_at=now,
        updated_at=now,
    )
    base.update(overrides)
    return ChannelCampaignStepResponse(**base)


def _make_cc(*, channel="email", provider="emailbison", **overrides) -> ChannelCampaignResponse:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    base = dict(
        id=uuid4(),
        campaign_id=uuid4(),
        organization_id=uuid4(),
        brand_id=uuid4(),
        name="cc",
        channel=channel,
        provider=provider,
        audience_spec_id=None,
        audience_snapshot_count=None,
        status="draft",
        start_offset_days=0,
        scheduled_send_at=None,
        schedule_config={},
        provider_config={},
        design_id=None,
        metadata={},
        created_by_user_id=None,
        created_at=now,
        updated_at=now,
        archived_at=None,
    )
    base.update(overrides)
    return ChannelCampaignResponse(**base)


@pytest.fixture
def patch_api_key(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "EMAILBISON_API_KEY", "id|tok", raising=False)


# ── activate_step ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_activate_step_creates_campaign_and_returns_scheduled(
    monkeypatch, patch_api_key
):
    captured: dict[str, Any] = {}

    def fake_create(api_key, payload):
        captured["create"] = (api_key, payload)
        return {"id": 42, "name": payload["name"], "type": payload["type"]}

    def fake_attach_tags(api_key, *, campaign_ids, tag_names):
        captured["tags"] = (campaign_ids, tag_names)
        return {"ok": True}

    monkeypatch.setattr(eb_client, "create_campaign", fake_create)
    monkeypatch.setattr(eb_client, "attach_tags_to_campaigns", fake_attach_tags)

    step = _make_step(name="Touch 1")
    cc = _make_cc()

    result = await EmailBisonAdapter().activate_step(step=step, channel_campaign=cc)

    assert result.status == "scheduled"
    assert result.external_provider_id == "42"
    assert captured["create"][1]["name"] == "Touch 1"
    assert captured["create"][1]["type"] == "sequence"

    # Six-tuple tags computed correctly and attached.
    expected_tags = _step_tag_strings(step=step)
    assert captured["tags"] == ([42], expected_tags)
    assert len(expected_tags) == 5
    assert any(t.startswith("hqx:org=") for t in expected_tags)
    assert any(t.startswith("hqx:step=") for t in expected_tags)


@pytest.mark.asyncio
async def test_activate_step_wrong_channel_raises(patch_api_key):
    step = _make_step()
    cc = _make_cc(channel="direct_mail", provider="lob")
    with pytest.raises(EmailBisonProviderError):
        await EmailBisonAdapter().activate_step(step=step, channel_campaign=cc)


@pytest.mark.asyncio
async def test_activate_step_provider_error_returns_failed_result(
    monkeypatch, patch_api_key
):
    def fake_create(api_key, payload):
        raise EmailBisonProviderError("emailbison http 502: gateway")

    monkeypatch.setattr(eb_client, "create_campaign", fake_create)

    step = _make_step()
    cc = _make_cc()
    result = await EmailBisonAdapter().activate_step(step=step, channel_campaign=cc)
    assert result.status == "failed"
    assert result.external_provider_id is None
    assert "gateway" in result.metadata.get("error", "")


@pytest.mark.asyncio
async def test_activate_step_tag_attach_failure_does_not_fail_activation(
    monkeypatch, patch_api_key
):
    monkeypatch.setattr(
        eb_client,
        "create_campaign",
        lambda k, p: {"id": 99, "name": p["name"], "type": p["type"]},
    )

    def fake_attach_tags(api_key, *, campaign_ids, tag_names):
        raise EmailBisonProviderError("emailbison http 422: tag missing")

    monkeypatch.setattr(eb_client, "attach_tags_to_campaigns", fake_attach_tags)

    result = await EmailBisonAdapter().activate_step(
        step=_make_step(), channel_campaign=_make_cc()
    )
    assert result.status == "scheduled"
    assert result.external_provider_id == "99"
    assert result.metadata["tag_attach"]["attached"] is False


# ── cancel_step ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_step_pauses_then_archives(monkeypatch, patch_api_key):
    order: list[str] = []

    def fake_pause(api_key, cid):
        order.append("pause")
        return {"status": "paused"}

    def fake_archive(api_key, cid):
        order.append("archive")
        return {"status": "archived"}

    monkeypatch.setattr(eb_client, "pause_campaign", fake_pause)
    monkeypatch.setattr(eb_client, "archive_campaign", fake_archive)

    step = _make_step(external_provider_id="55")
    result = await EmailBisonAdapter().cancel_step(step=step)
    assert result.cancelled is True
    assert order == ["pause", "archive"]


@pytest.mark.asyncio
async def test_cancel_step_archives_even_when_pause_fails(
    monkeypatch, patch_api_key
):
    def fake_pause(api_key, cid):
        raise EmailBisonProviderError("emailbison http 500")

    archived = {"called": False}

    def fake_archive(api_key, cid):
        archived["called"] = True
        return {"status": "archived"}

    monkeypatch.setattr(eb_client, "pause_campaign", fake_pause)
    monkeypatch.setattr(eb_client, "archive_campaign", fake_archive)

    step = _make_step(external_provider_id="55")
    result = await EmailBisonAdapter().cancel_step(step=step)
    assert archived["called"] is True
    assert result.cancelled is True


# ── parse_webhook_event ──────────────────────────────────────────────────


def test_parse_webhook_event_email_sent_envelope():
    """§5 sample EMAIL_SENT envelope verbatim."""
    payload = {
        "event": {
            "type": "EMAIL_SENT",
            "name": "Email Sent",
            "workspace_id": 1,
        },
        "data": {
            "scheduled_email": {
                "id": 4,
                "lead_id": 1,
                "sequence_step_id": 2,
                "sequence_step_order": 1,
                "sequence_step_variant": 2,
                "email_subject": "test subject",
                "email_body": "<p>test</p>",
                "status": "sent",
                "sent_at": "2024-08-02T06:08:38.000000Z",
                "opens": 0,
                "replies": 0,
                "raw_message_id": "<abc@emailguardalpha.com>",
            },
            "campaign_event": {
                "id": 6,
                "type": "sent",
                "created_at": "2024-08-02T06:08:38.000000Z",
            },
            "lead": {"id": 1, "email": "lead@example.com"},
            "campaign": {"id": 2, "name": "test"},
            "sender_email": {
                "id": 3,
                "email": "from@example.com",
                "status": "connected",
            },
        },
    }
    parsed = EmailBisonAdapter.parse_webhook_event(payload)
    assert parsed.event_type == "sent"
    assert parsed.raw_event_name == "EMAIL_SENT"
    assert parsed.eb_workspace_id == "1"
    assert parsed.eb_scheduled_email_id == 4
    assert parsed.eb_campaign_id == 2
    assert parsed.eb_lead_id == 1
    assert parsed.eb_sender_email_id == 3
    assert parsed.occurred_at_raw is not None
    assert parsed.metadata["raw_message_id"] == "<abc@emailguardalpha.com>"
    assert parsed.metadata["subject_snapshot"] == "test subject"
    assert parsed.metadata["sender_email_snapshot"] == "from@example.com"


def test_parse_webhook_event_lead_replied():
    """§5 sample LEAD_REPLIED envelope verbatim."""
    payload = {
        "event": {
            "type": "LEAD_REPLIED",
            "name": "Lead Replied",
            "instance_url": "https://dedi.emailbison.com",
            "workspace_id": 1,
            "workspace_name": "main",
        },
        "data": {
            "reply": {
                "id": 725,
                "uuid": "abc-def",
                "raw_message_id": "<reply@x.com>",
                "parent_id": None,
                "date_received": "2026-04-01T10:00:00Z",
                "interested": False,
                "automated_reply": False,
                "folder": "Inbox",
                "type": "Reply",
            },
            "campaign_event": {
                "id": 7,
                "type": "replied",
                "created_at": "2026-04-01T10:00:00Z",
            },
            "scheduled_email": {
                "id": 4,
                "sequence_step_id": 2,
                "raw_message_id": "<orig@x.com>",
            },
            "lead": {"id": 1, "email": "lead@example.com"},
            "campaign": {"id": 2},
            "sender_email": {"id": 3, "email": "from@example.com"},
        },
    }
    parsed = EmailBisonAdapter.parse_webhook_event(payload)
    assert parsed.event_type == "replied"
    assert parsed.eb_reply_id == 725
    assert parsed.eb_scheduled_email_id == 4
    assert parsed.eb_campaign_id == 2
    assert parsed.metadata["campaign_event_type"] == "replied"


def test_parse_webhook_event_email_bounced():
    """§5 sample EMAIL_BOUNCED envelope shape."""
    payload = {
        "event": {"type": "EMAIL_BOUNCED", "workspace_id": 1},
        "data": {
            "reply": {
                "id": 800,
                "type": "Bounced",
                "folder": "Bounced",
                "automated_reply": True,
                "from_email_address": "mailer-daemon@googlemail.com",
            },
            "campaign_event": {"id": 8, "type": "bounce"},
            "scheduled_email": {"id": 4, "lead_id": 1},
            "campaign": {"id": 2},
            "lead": {"id": 1, "email": "lead@example.com"},
        },
    }
    parsed = EmailBisonAdapter.parse_webhook_event(payload)
    assert parsed.event_type == "bounced"
    assert parsed.eb_scheduled_email_id == 4
    assert parsed.eb_campaign_id == 2
    assert parsed.eb_reply_id == 800
    assert parsed.metadata["campaign_event_type"] == "bounce"


def test_parse_webhook_event_extracts_six_tuple_tags():
    step_uuid = "11111111-2222-3333-4444-555555555555"
    org_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    payload = {
        "event": {"type": "EMAIL_SENT", "workspace_id": 1},
        "data": {
            "scheduled_email": {"id": 1, "lead_id": 1},
            "campaign": {
                "id": 2,
                "tags": [
                    {"id": 1, "name": f"hqx:step={step_uuid}"},
                    {"id": 2, "name": f"hqx:org={org_uuid}"},
                    {"id": 3, "name": "general"},
                ],
            },
            "lead": {"id": 1, "email": "x@y.com"},
            "sender_email": {"id": 3},
        },
    }
    parsed = EmailBisonAdapter.parse_webhook_event(payload)
    assert parsed.six_tuple_tags["step"] == step_uuid
    assert parsed.six_tuple_tags["channel_campaign_step_id"] == step_uuid
    assert parsed.six_tuple_tags["org"] == org_uuid
    assert parsed.six_tuple_tags["organization_id"] == org_uuid


def test_parse_webhook_event_no_tags_six_tuple_empty():
    payload = {
        "event": {"type": "EMAIL_OPENED", "workspace_id": 1},
        "data": {
            "scheduled_email": {"id": 4},
            "campaign": {"id": 2, "tags": []},
        },
    }
    parsed = EmailBisonAdapter.parse_webhook_event(payload)
    assert parsed.six_tuple_tags == {}
    assert parsed.event_type == "opened"


def test_parse_six_tuple_from_tags_string_form():
    tags = ["hqx:step=abc", "regular-tag"]
    out = _parse_six_tuple_from_tags(tags)
    assert out["step"] == "abc"


def test_parse_six_tuple_from_tags_handles_none():
    assert _parse_six_tuple_from_tags(None) == {}
    assert _parse_six_tuple_from_tags([]) == {}

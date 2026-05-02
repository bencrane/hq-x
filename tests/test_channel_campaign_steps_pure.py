"""Pure unit tests for channel_campaign_steps logic."""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.models.campaigns import (
    ChannelCampaignStepCreate,
    ChannelCampaignStepUpdate,
)
from app.providers.lob.adapter import LobAdapter
from app.services.channel_campaign_steps import compute_step_scheduled_send_at

# ── compute_step_scheduled_send_at ───────────────────────────────────────


def test_first_step_with_campaign_start_date() -> None:
    out = compute_step_scheduled_send_at(
        campaign_start_date=date(2026, 5, 1),
        previous_step_send_at=None,
        delay_days_from_previous=0,
        step_order=1,
    )
    assert out == datetime(2026, 5, 1, 0, 0, tzinfo=UTC)


def test_first_step_with_no_campaign_start_date_is_none() -> None:
    out = compute_step_scheduled_send_at(
        campaign_start_date=None,
        previous_step_send_at=None,
        delay_days_from_previous=0,
        step_order=1,
    )
    assert out is None


def test_subsequent_step_offsets_from_previous() -> None:
    prev = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    out = compute_step_scheduled_send_at(
        campaign_start_date=date(2026, 5, 1),
        previous_step_send_at=prev,
        delay_days_from_previous=14,
        step_order=2,
    )
    assert out == datetime(2026, 5, 15, 0, 0, tzinfo=UTC)


def test_subsequent_step_with_no_previous_send_at_propagates_none() -> None:
    out = compute_step_scheduled_send_at(
        campaign_start_date=None,
        previous_step_send_at=None,
        delay_days_from_previous=7,
        step_order=2,
    )
    assert out is None


# ── Pydantic guards ─────────────────────────────────────────────────────


def test_step_create_rejects_zero_step_order() -> None:
    with pytest.raises(ValidationError):
        ChannelCampaignStepCreate(step_order=0, creative_ref=uuid4())


def test_step_create_rejects_negative_delay() -> None:
    with pytest.raises(ValidationError):
        ChannelCampaignStepCreate(
            step_order=1, delay_days_from_previous=-1, creative_ref=uuid4()
        )


def test_step_update_allows_partial_payload() -> None:
    update = ChannelCampaignStepUpdate(name="renamed")
    assert update.model_dump(exclude_unset=True) == {"name": "renamed"}


# ── Adapter webhook parser ──────────────────────────────────────────────


def test_parse_webhook_event_extracts_piece_id() -> None:
    payload = {
        "event_type": {"id": "postcard.delivered", "resource": "postcards"},
        "body": {"id": "psc_abc123"},
        "date_created": "2026-04-30T12:00:00Z",
    }
    parsed = LobAdapter.parse_webhook_event(payload)
    # lob_normalization collapses postcard/letter/self_mailer → "piece".
    assert parsed.event_type == "piece.delivered"
    assert parsed.lob_piece_id == "psc_abc123"
    assert parsed.metadata["raw_event_name"] == "postcard.delivered"


def test_parse_webhook_event_extracts_campaign_id_from_body() -> None:
    payload = {
        "event_type": {"id": "campaign.deleted"},
        "body": {"campaign": "cmp_xyz789"},
    }
    parsed = LobAdapter.parse_webhook_event(payload)
    assert parsed.lob_campaign_id == "cmp_xyz789"


def test_parse_webhook_event_no_resource_id_returns_nones() -> None:
    parsed = LobAdapter.parse_webhook_event({"event_type": {"id": "ping"}})
    assert parsed.lob_piece_id is None
    assert parsed.lob_campaign_id is None

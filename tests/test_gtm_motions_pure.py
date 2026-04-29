"""Unit tests for the pure pieces of gtm_motions / campaigns logic.

Anything that needs to hit Postgres is covered by integration tests run
against a real DB (out of scope for this file). Here we only test the
behaviour that doesn't require a connection: schedule arithmetic, channel
/ provider validation, and Pydantic model coercion.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.models.gtm import (
    VALID_CHANNEL_PROVIDER_PAIRS,
    CampaignCreate,
    CampaignUpdate,
    GtmMotionCreate,
)
from app.services.campaigns import (
    CampaignChannelProviderInvalid,
    CampaignDesignRequired,
    _validate_channel_provider,
    _validate_channel_specific,
)
from app.services.gtm_motions import compute_scheduled_send_at

# ── compute_scheduled_send_at ─────────────────────────────────────────────


def test_scheduled_send_at_zero_offset_returns_motion_start_midnight() -> None:
    out = compute_scheduled_send_at(
        motion_start_date=date(2026, 5, 1),
        start_offset_days=0,
    )
    assert out == datetime(2026, 5, 1, 0, 0, tzinfo=UTC)


def test_scheduled_send_at_offset_adds_days() -> None:
    out = compute_scheduled_send_at(
        motion_start_date=date(2026, 5, 1),
        start_offset_days=12,
    )
    assert out == datetime(2026, 5, 13, 0, 0, tzinfo=UTC)


def test_scheduled_send_at_no_motion_start_date_is_none() -> None:
    out = compute_scheduled_send_at(
        motion_start_date=None,
        start_offset_days=7,
    )
    assert out is None


# ── channel/provider validation ───────────────────────────────────────────


@pytest.mark.parametrize(("channel", "provider"), sorted(VALID_CHANNEL_PROVIDER_PAIRS))
def test_valid_channel_provider_pairs_accepted(channel: str, provider: str) -> None:
    _validate_channel_provider(channel, provider)


def test_invalid_channel_provider_pair_rejected() -> None:
    with pytest.raises(CampaignChannelProviderInvalid):
        _validate_channel_provider("voice_outbound", "lob")


def test_direct_mail_without_design_id_rejected() -> None:
    payload = CampaignCreate(
        gtm_motion_id=uuid4(),
        name="dm-no-design",
        channel="direct_mail",
        provider="lob",
    )
    with pytest.raises(CampaignDesignRequired):
        _validate_channel_specific(payload)


def test_direct_mail_with_design_id_accepted() -> None:
    payload = CampaignCreate(
        gtm_motion_id=uuid4(),
        name="dm-ok",
        channel="direct_mail",
        provider="lob",
        design_id=uuid4(),
    )
    _validate_channel_specific(payload)  # no raise


def test_voice_outbound_does_not_require_design_id() -> None:
    payload = CampaignCreate(
        gtm_motion_id=uuid4(),
        name="vo",
        channel="voice_outbound",
        provider="vapi",
    )
    _validate_channel_specific(payload)  # no raise


# ── Pydantic guards (extra=forbid + ge constraints) ───────────────────────


def test_motion_create_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        GtmMotionCreate(
            brand_id=uuid4(),
            name="x",
            unexpected_field="boom",  # type: ignore[call-arg]
        )


def test_campaign_create_rejects_negative_offset() -> None:
    with pytest.raises(ValidationError):
        CampaignCreate(
            gtm_motion_id=uuid4(),
            name="x",
            channel="email",
            provider="emailbison",
            start_offset_days=-1,
        )


def test_campaign_update_allows_partial_payload() -> None:
    update = CampaignUpdate(name="renamed")
    dumped = update.model_dump(exclude_unset=True)
    assert dumped == {"name": "renamed"}

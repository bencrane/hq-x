"""Tests for ``app.services.analytics.emit_event``'s RudderStack fan-out.

The other fan-out paths (log + ClickHouse) are exercised indirectly by
the existing webhook/projector tests; this file zeros in on the new
RudderStack hook and the org-isolation contract: the SDK call must be
made with ``anonymous_id = organization_id`` and the full payload
(six-tuple + recipient_id when present + caller properties) as
``properties``.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from app import rudderstack
from app.services import analytics as analytics_service

ORG_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
BRAND_A = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
CAMP_A = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
CC_A = UUID("e1111111-1111-1111-1111-111111111111")
STEP_A = UUID("f1111111-1111-1111-1111-111111111111")
RECIP_A = UUID("12121212-1212-1212-1212-121212121212")


def _step_context() -> dict[str, Any]:
    return {
        "organization_id": str(ORG_A),
        "brand_id": str(BRAND_A),
        "campaign_id": str(CAMP_A),
        "channel_campaign_id": str(CC_A),
        "channel_campaign_step_id": str(STEP_A),
        "channel": "direct_mail",
        "provider": "lob",
    }


@pytest.fixture(autouse=True)
def _reset_rudder():
    rudderstack._reset_for_tests()
    yield
    rudderstack._reset_for_tests()


async def test_emit_event_calls_rudderstack_track_with_six_tuple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []

    async def _fake_resolve_step(step_id: UUID) -> dict[str, Any]:
        return _step_context()

    def _fake_track(
        *, event_name: str, anonymous_id: str, properties: dict[str, Any]
    ) -> None:
        captured.append(
            {
                "event_name": event_name,
                "anonymous_id": anonymous_id,
                "properties": properties,
            }
        )

    monkeypatch.setattr(
        analytics_service, "resolve_step_context", _fake_resolve_step
    )
    monkeypatch.setattr(analytics_service.rudderstack, "track", _fake_track)

    await analytics_service.emit_event(
        event_name="piece.delivered",
        channel_campaign_step_id=STEP_A,
        recipient_id=RECIP_A,
        properties={"piece_id": "psc_xyz"},
    )

    assert len(captured) == 1
    call = captured[0]
    assert call["event_name"] == "piece.delivered"
    # anonymous_id is the org id from the context, not the URL or a
    # caller-supplied value.
    assert call["anonymous_id"] == str(ORG_A)

    props = call["properties"]
    # Six-tuple + recipient_id + caller props all in properties.
    assert props["organization_id"] == str(ORG_A)
    assert props["brand_id"] == str(BRAND_A)
    assert props["campaign_id"] == str(CAMP_A)
    assert props["channel_campaign_id"] == str(CC_A)
    assert props["channel_campaign_step_id"] == str(STEP_A)
    assert props["channel"] == "direct_mail"
    assert props["provider"] == "lob"
    assert props["recipient_id"] == str(RECIP_A)
    assert props["piece_id"] == "psc_xyz"
    assert props["event"] == "piece.delivered"
    assert "occurred_at" in props


async def test_emit_event_falls_back_to_cc_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Channel_campaign-only callers (no step) still get a track call,
    with the partial context (no ``channel_campaign_step_id``)."""
    captured: list[dict[str, Any]] = []

    async def _fake_resolve_cc(channel_campaign_id: UUID) -> dict[str, Any]:
        ctx = _step_context()
        ctx.pop("channel_campaign_step_id", None)
        return ctx

    monkeypatch.setattr(
        analytics_service,
        "resolve_channel_campaign_context",
        _fake_resolve_cc,
    )
    monkeypatch.setattr(
        analytics_service.rudderstack,
        "track",
        lambda **kwargs: captured.append(kwargs),
    )

    await analytics_service.emit_event(
        event_name="campaign.created",
        channel_campaign_id=CC_A,
    )

    assert len(captured) == 1
    props = captured[0]["properties"]
    assert props["organization_id"] == str(ORG_A)
    assert "channel_campaign_step_id" not in props


async def test_emit_event_does_not_raise_when_track_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defence in depth: even if ``rudderstack.track`` throws, emit_event
    must not propagate the exception (the rudder module already swallows;
    this guarantees the wrapper guard is in place too)."""

    async def _fake_resolve_step(step_id: UUID) -> dict[str, Any]:
        return _step_context()

    def _boom(**_: Any) -> None:
        raise RuntimeError("sdk explodes")

    monkeypatch.setattr(
        analytics_service, "resolve_step_context", _fake_resolve_step
    )
    monkeypatch.setattr(analytics_service.rudderstack, "track", _boom)

    # Should not raise.
    await analytics_service.emit_event(
        event_name="piece.delivered",
        channel_campaign_step_id=STEP_A,
    )


async def test_emit_event_skips_track_without_org_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the resolved context somehow lacks organization_id, the rudder
    call is skipped (we don't fabricate an anonymous_id from nothing)."""
    captured: list[dict[str, Any]] = []

    async def _fake_resolve_step(step_id: UUID) -> dict[str, Any]:
        return {"channel": "direct_mail", "provider": "lob"}

    monkeypatch.setattr(
        analytics_service, "resolve_step_context", _fake_resolve_step
    )
    monkeypatch.setattr(
        analytics_service.rudderstack,
        "track",
        lambda **kwargs: captured.append(kwargs),
    )

    await analytics_service.emit_event(
        event_name="x",
        channel_campaign_step_id=STEP_A,
    )
    assert captured == []

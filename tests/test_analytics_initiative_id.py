"""Tests for the ``initiative_id`` extension to the analytics resolvers
and ``emit_event``'s payload.

The substrate change: ``channel_campaigns.initiative_id`` is now
denormalized so both ``resolve_channel_campaign_context`` and
``resolve_step_context`` return it without a join. ``None`` for legacy
rows; downstream consumers must handle that.

These tests stub the get_*_context helpers (the SQL itself is exercised
by the existing service tests / the new migration test) and assert the
resolvers + emit_event surface ``initiative_id`` cleanly.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from app import rudderstack
from app.services import analytics as analytics_service
from app.services import channel_campaign_steps as steps_service
from app.services import channel_campaigns as cc_service

ORG = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
BRAND = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
CAMP = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
CC = UUID("e1111111-1111-1111-1111-111111111111")
STEP = UUID("f1111111-1111-1111-1111-111111111111")
INIT = UUID("11111111-2222-3333-4444-555555555555")


@pytest.fixture(autouse=True)
def _reset_rudder():
    rudderstack._reset_for_tests()
    yield
    rudderstack._reset_for_tests()


@pytest.mark.asyncio
async def test_resolve_channel_campaign_context_returns_initiative_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_get(*, channel_campaign_id: UUID) -> dict[str, Any]:
        return {
            "organization_id": str(ORG),
            "brand_id": str(BRAND),
            "campaign_id": str(CAMP),
            "channel_campaign_id": str(channel_campaign_id),
            "channel": "direct_mail",
            "provider": "lob",
            "initiative_id": str(INIT),
        }

    monkeypatch.setattr(
        cc_service, "get_channel_campaign_context", _fake_get
    )
    monkeypatch.setattr(
        analytics_service, "get_channel_campaign_context", _fake_get
    )
    ctx = await analytics_service.resolve_channel_campaign_context(CC)
    assert ctx["initiative_id"] == str(INIT)


@pytest.mark.asyncio
async def test_resolve_channel_campaign_context_initiative_id_none_for_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_get(*, channel_campaign_id: UUID) -> dict[str, Any]:
        return {
            "organization_id": str(ORG),
            "brand_id": str(BRAND),
            "campaign_id": str(CAMP),
            "channel_campaign_id": str(channel_campaign_id),
            "channel": "direct_mail",
            "provider": "lob",
            "initiative_id": None,
        }

    monkeypatch.setattr(
        cc_service, "get_channel_campaign_context", _fake_get
    )
    monkeypatch.setattr(
        analytics_service, "get_channel_campaign_context", _fake_get
    )
    ctx = await analytics_service.resolve_channel_campaign_context(CC)
    assert ctx["initiative_id"] is None


@pytest.mark.asyncio
async def test_resolve_step_context_returns_initiative_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_get(*, step_id: UUID) -> dict[str, Any]:
        return {
            "organization_id": str(ORG),
            "brand_id": str(BRAND),
            "campaign_id": str(CAMP),
            "channel_campaign_id": str(CC),
            "channel_campaign_step_id": str(step_id),
            "channel": "direct_mail",
            "provider": "lob",
            "initiative_id": str(INIT),
        }

    monkeypatch.setattr(steps_service, "get_step_context", _fake_get)
    monkeypatch.setattr(analytics_service, "get_step_context", _fake_get)
    ctx = await analytics_service.resolve_step_context(STEP)
    assert ctx["initiative_id"] == str(INIT)


@pytest.mark.asyncio
async def test_emit_event_includes_initiative_id_in_rudderstack_props(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []

    async def _fake_resolve_step(step_id: UUID) -> dict[str, Any]:
        return {
            "organization_id": str(ORG),
            "brand_id": str(BRAND),
            "campaign_id": str(CAMP),
            "channel_campaign_id": str(CC),
            "channel_campaign_step_id": str(step_id),
            "channel": "direct_mail",
            "provider": "lob",
            "initiative_id": str(INIT),
        }

    monkeypatch.setattr(
        analytics_service, "resolve_step_context", _fake_resolve_step
    )
    monkeypatch.setattr(
        analytics_service.rudderstack,
        "track",
        lambda **kwargs: captured.append(kwargs),
    )

    await analytics_service.emit_event(
        event_name="piece.delivered",
        channel_campaign_step_id=STEP,
        recipient_id=uuid4(),
    )

    assert len(captured) == 1
    props = captured[0]["properties"]
    assert props["initiative_id"] == str(INIT)


@pytest.mark.asyncio
async def test_emit_event_carries_null_initiative_for_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []

    async def _fake_resolve_step(step_id: UUID) -> dict[str, Any]:
        return {
            "organization_id": str(ORG),
            "brand_id": str(BRAND),
            "campaign_id": str(CAMP),
            "channel_campaign_id": str(CC),
            "channel_campaign_step_id": str(step_id),
            "channel": "direct_mail",
            "provider": "lob",
            "initiative_id": None,
        }

    monkeypatch.setattr(
        analytics_service, "resolve_step_context", _fake_resolve_step
    )
    monkeypatch.setattr(
        analytics_service.rudderstack,
        "track",
        lambda **kwargs: captured.append(kwargs),
    )

    await analytics_service.emit_event(
        event_name="piece.delivered",
        channel_campaign_step_id=STEP,
    )
    props = captured[0]["properties"]
    assert "initiative_id" in props
    assert props["initiative_id"] is None

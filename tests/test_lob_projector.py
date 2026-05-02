"""Projector tests — covers the routing logic added in 0023:

  * piece-scoped events go to direct_mail_pieces lookup first
  * step-scoped fallback for campaign-level events
  * orphans return status='orphaned' instead of silently dropping
  * idempotency: replaying the same event is safe (handled by the
    webhook_events UNIQUE constraint upstream; here we verify the
    projector's own writes don't duplicate state when the resolved
    piece status hasn't changed)

The DB layer is mocked at the service-function boundary; we don't run a
real DB. That keeps the test fast and lets us assert the routing
decisions directly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from app.direct_mail import persistence as dmp_persistence
from app.services import channel_campaign_steps as steps_service
from app.webhooks import lob_processor


@pytest.fixture
def projector_mocks(monkeypatch):
    """Replace every DB-touching helper the projector calls with in-memory
    fakes. Returns a state dict the test reads + asserts on."""
    state: dict[str, Any] = {
        "piece_by_external_id": {},
        "step_by_external_id": {},
        "appended_events": [],
        "status_updates": [],
        "step_status_updates": [],
        "suppressions": [],
        "analytics_calls": [],
    }

    async def fake_get_piece_by_external_id(*, external_piece_id: str, **_):
        return state["piece_by_external_id"].get(external_piece_id)

    async def fake_append_piece_event(**kwargs):
        state["appended_events"].append(kwargs)
        return uuid4()

    async def fake_update_piece_status(*, piece_id, new_status):
        state["status_updates"].append((piece_id, new_status))
        return new_status

    async def fake_insert_suppression(**kwargs):
        state["suppressions"].append(kwargs)
        return True

    async def fake_lookup_step(*, external_provider_id: str):
        return state["step_by_external_id"].get(external_provider_id)

    async def fake_update_step_status(*, step_id, new_status):
        state["step_status_updates"].append((step_id, new_status))

    async def fake_emit_event(**kwargs):
        state["analytics_calls"].append(kwargs)

    monkeypatch.setattr(
        lob_processor, "get_piece_by_external_id", fake_get_piece_by_external_id
    )
    monkeypatch.setattr(
        lob_processor, "append_piece_event", fake_append_piece_event
    )
    monkeypatch.setattr(
        lob_processor, "update_piece_status", fake_update_piece_status
    )
    monkeypatch.setattr(
        lob_processor, "insert_suppression", fake_insert_suppression
    )
    monkeypatch.setattr(
        steps_service,
        "lookup_step_by_external_provider_id",
        fake_lookup_step,
    )
    monkeypatch.setattr(
        steps_service, "update_step_status", fake_update_step_status
    )
    # The projector imports emit_event from app.services.analytics inside
    # _emit_analytics_for_step. Patch that module's symbol so the helper
    # picks it up.
    import app.services.analytics as analytics_module

    monkeypatch.setattr(analytics_module, "emit_event", fake_emit_event)
    return state


def _make_piece(piece_id, *, status="mailed", step_id=None):
    return dmp_persistence.UpsertedPiece(
        id=piece_id,
        external_piece_id="psc_abc123",
        piece_type="postcard",
        status=status,
        cost_cents=84,
        deliverability="deliverable",
        is_test_mode=False,
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
        raw_payload={},
        metadata=None,
        channel_campaign_step_id=step_id,
    )


@pytest.mark.asyncio
async def test_piece_event_applies_status_and_emits_analytics(
    projector_mocks,
) -> None:
    piece_id = uuid4()
    step_id = uuid4()
    projector_mocks["piece_by_external_id"]["psc_abc123"] = _make_piece(
        piece_id, status="mailed", step_id=step_id
    )

    result = await lob_processor.project_lob_event(
        payload={
            "event_type": {"id": "postcard.delivered"},
            "body": {"id": "psc_abc123"},
            "date_created": "2026-04-30T12:00:00Z",
        },
        event_id="evt_1",
    )
    assert result["status"] == "applied"
    assert result["scope"] == "piece"
    assert projector_mocks["status_updates"] == [(piece_id, "delivered")]
    assert len(projector_mocks["analytics_calls"]) == 1
    call = projector_mocks["analytics_calls"][0]
    assert call["channel_campaign_step_id"] == step_id
    assert call["event_name"] == "lob.piece.delivered"


@pytest.mark.asyncio
async def test_step_fallback_when_piece_missing(projector_mocks) -> None:
    step_id = uuid4()
    projector_mocks["step_by_external_id"]["cmp_xyz"] = {
        "step_id": step_id,
        "channel_campaign_id": uuid4(),
        "campaign_id": uuid4(),
        "organization_id": uuid4(),
        "brand_id": uuid4(),
        "status": "scheduled",
    }
    result = await lob_processor.project_lob_event(
        payload={
            "event_type": {"id": "campaign.deleted"},
            "body": {"campaign": "cmp_xyz"},
        },
        event_id="evt_2",
    )
    assert result["status"] == "applied"
    assert result["scope"] == "step"
    assert projector_mocks["step_status_updates"] == [(step_id, "cancelled")]
    assert len(projector_mocks["analytics_calls"]) == 1


@pytest.mark.asyncio
async def test_orphaned_when_no_lookup_matches(projector_mocks) -> None:
    # Neither piece nor step is registered, but the payload has a piece id.
    result = await lob_processor.project_lob_event(
        payload={
            "event_type": {"id": "postcard.delivered"},
            "body": {"id": "psc_unknown"},
        },
        event_id="evt_3",
    )
    assert result["status"] == "orphaned"
    assert result["external_piece_id"] == "psc_unknown"
    assert projector_mocks["analytics_calls"] == []


@pytest.mark.asyncio
async def test_skipped_when_no_resource_id(projector_mocks) -> None:
    result = await lob_processor.project_lob_event(
        payload={"event_type": {"id": "ping"}},
        event_id="evt_4",
    )
    assert result["status"] == "skipped"
    assert result["reason"] == "missing_resource_id"


@pytest.mark.asyncio
async def test_idempotent_replay_no_duplicate_status_update(
    projector_mocks,
) -> None:
    """Replaying the same delivered event twice on a piece that's already
    'delivered' must not re-issue update_piece_status — the projector
    only writes when previous_status != new_status."""
    piece_id = uuid4()
    step_id = uuid4()
    projector_mocks["piece_by_external_id"]["psc_xyz"] = _make_piece(
        piece_id, status="delivered", step_id=step_id
    )
    payload = {
        "event_type": {"id": "postcard.delivered"},
        "body": {"id": "psc_xyz"},
    }
    # Replay twice.
    r1 = await lob_processor.project_lob_event(payload=payload, event_id="evt_a")
    r2 = await lob_processor.project_lob_event(payload=payload, event_id="evt_b")
    assert r1["status"] == r2["status"] == "applied"
    # No status transitions were written (already delivered).
    assert projector_mocks["status_updates"] == []
    # But the audit log row was appended both times — that's correct, the
    # raw event is preserved even when status doesn't change.
    assert len(projector_mocks["appended_events"]) == 2

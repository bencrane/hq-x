"""Pure tests for the recipients identity layer.

Covers: model validation, projector recipient_id propagation, and the
membership-transition decision tree in the Lob projector. DB-touching
upsert paths are exercised in integration tests (require a real Postgres
schema) — these tests are pure-logic only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from app.direct_mail import persistence as dmp_persistence
from app.models.recipients import (
    RecipientResponse,
    RecipientSpec,
    StepRecipientResponse,
)
from app.services import channel_campaign_steps as steps_service
from app.services import recipients as recipients_service
from app.webhooks import lob_processor


# ── Model validation ─────────────────────────────────────────────────────


def test_recipient_spec_rejects_blank_natural_key() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RecipientSpec(external_source="", external_id="123")
    with pytest.raises(ValidationError):
        RecipientSpec(external_source="fmcsa", external_id="")


def test_recipient_spec_defaults_to_business() -> None:
    spec = RecipientSpec(external_source="fmcsa", external_id="123456")
    assert spec.recipient_type == "business"
    assert spec.mailing_address == {}
    assert spec.metadata == {}


def test_recipient_spec_rejects_unknown_type() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RecipientSpec(
            external_source="fmcsa",
            external_id="1",
            recipient_type="lead",  # type: ignore[arg-type]
        )


# ── Projector membership-transition decision ─────────────────────────────


def _make_piece(piece_id, *, step_id, recipient_id, status="mailed"):
    return dmp_persistence.UpsertedPiece(
        id=piece_id,
        external_piece_id="psc_recip_001",
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
        recipient_id=recipient_id,
    )


@pytest.fixture
def projector_with_membership(monkeypatch):
    """Projector fakes that capture both analytics + membership transitions."""
    state: dict[str, Any] = {
        "piece_by_external_id": {},
        "step_by_external_id": {},
        "appended_events": [],
        "status_updates": [],
        "membership_lookups": [],
        "membership_updates": [],
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
        return False

    async def fake_emit_event(**kwargs):
        state["analytics_calls"].append(kwargs)

    async def fake_find_membership(*, channel_campaign_step_id, recipient_id):
        state["membership_lookups"].append(
            (channel_campaign_step_id, recipient_id)
        )
        return state.get("membership")

    async def fake_update_membership_status(**kwargs):
        state["membership_updates"].append(kwargs)

    monkeypatch.setattr(
        lob_processor, "get_piece_by_external_id", fake_get_piece_by_external_id
    )
    monkeypatch.setattr(lob_processor, "append_piece_event", fake_append_piece_event)
    monkeypatch.setattr(lob_processor, "update_piece_status", fake_update_piece_status)
    monkeypatch.setattr(lob_processor, "insert_suppression", fake_insert_suppression)

    import app.services.analytics as analytics_module

    monkeypatch.setattr(analytics_module, "emit_event", fake_emit_event)
    monkeypatch.setattr(
        recipients_service, "find_membership_for_recipient", fake_find_membership
    )
    monkeypatch.setattr(
        recipients_service, "update_membership_status", fake_update_membership_status
    )
    return state


@pytest.mark.asyncio
async def test_terminal_delivered_event_transitions_membership_to_sent(
    projector_with_membership,
) -> None:
    state = projector_with_membership
    piece_id = uuid4()
    step_id = uuid4()
    recipient_id = uuid4()
    membership_id = uuid4()
    state["piece_by_external_id"]["psc_recip_001"] = _make_piece(
        piece_id, step_id=step_id, recipient_id=recipient_id, status="mailed"
    )
    state["membership"] = StepRecipientResponse(
        id=membership_id,
        channel_campaign_step_id=step_id,
        recipient_id=recipient_id,
        organization_id=uuid4(),
        status="scheduled",
        scheduled_for=None,
        processed_at=None,
        error_reason=None,
        metadata={},
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )

    result = await lob_processor.project_lob_event(
        payload={
            "event_type": {"id": "postcard.delivered"},
            "body": {"id": "psc_recip_001"},
            "date_created": "2026-04-30T12:00:00Z",
        },
        event_id="evt_recip_1",
    )
    assert result["status"] == "applied"
    assert state["membership_lookups"] == [(step_id, recipient_id)]
    assert len(state["membership_updates"]) == 1
    upd = state["membership_updates"][0]
    assert upd["membership_id"] == membership_id
    assert upd["new_status"] == "sent"
    assert upd["set_processed_at"] is True

    # And the analytics call carries recipient_id.
    assert state["analytics_calls"][0]["recipient_id"] == recipient_id
    assert (
        state["analytics_calls"][0]["properties"]["recipient_id"]
        == str(recipient_id)
    )


@pytest.mark.asyncio
async def test_terminal_returned_event_transitions_membership_to_failed(
    projector_with_membership,
) -> None:
    state = projector_with_membership
    piece_id = uuid4()
    step_id = uuid4()
    recipient_id = uuid4()
    state["piece_by_external_id"]["psc_recip_001"] = _make_piece(
        piece_id, step_id=step_id, recipient_id=recipient_id, status="mailed"
    )
    state["membership"] = StepRecipientResponse(
        id=uuid4(),
        channel_campaign_step_id=step_id,
        recipient_id=recipient_id,
        organization_id=uuid4(),
        status="scheduled",
        scheduled_for=None,
        processed_at=None,
        error_reason=None,
        metadata={},
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )

    await lob_processor.project_lob_event(
        payload={
            "event_type": {"id": "postcard.returned_to_sender"},
            "body": {"id": "psc_recip_001"},
        },
        event_id="evt_recip_2",
    )
    # Note: normalized event for `returned_to_sender` is `piece.returned`,
    # which lives in _PIECE_TERMINAL_FAILED.
    assert len(state["membership_updates"]) == 1
    assert state["membership_updates"][0]["new_status"] == "failed"


@pytest.mark.asyncio
async def test_non_terminal_event_does_not_transition_membership(
    projector_with_membership,
) -> None:
    state = projector_with_membership
    piece_id = uuid4()
    step_id = uuid4()
    recipient_id = uuid4()
    state["piece_by_external_id"]["psc_recip_001"] = _make_piece(
        piece_id, step_id=step_id, recipient_id=recipient_id, status="mailed"
    )
    state["membership"] = StepRecipientResponse(
        id=uuid4(),
        channel_campaign_step_id=step_id,
        recipient_id=recipient_id,
        organization_id=uuid4(),
        status="scheduled",
        scheduled_for=None,
        processed_at=None,
        error_reason=None,
        metadata={},
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )

    # 'viewed' is engagement-only; not in either terminal set.
    await lob_processor.project_lob_event(
        payload={
            "event_type": {"id": "postcard.viewed"},
            "body": {"id": "psc_recip_001"},
        },
        event_id="evt_recip_3",
    )
    assert state["membership_updates"] == []
    # Lookup is also skipped — early-return before find_membership_for_recipient.
    assert state["membership_lookups"] == []


@pytest.mark.asyncio
async def test_already_terminal_membership_is_not_overwritten(
    projector_with_membership,
) -> None:
    state = projector_with_membership
    piece_id = uuid4()
    step_id = uuid4()
    recipient_id = uuid4()
    state["piece_by_external_id"]["psc_recip_001"] = _make_piece(
        piece_id, step_id=step_id, recipient_id=recipient_id, status="delivered"
    )
    state["membership"] = StepRecipientResponse(
        id=uuid4(),
        channel_campaign_step_id=step_id,
        recipient_id=recipient_id,
        organization_id=uuid4(),
        status="sent",  # already terminal
        scheduled_for=None,
        processed_at=datetime.now(tz=UTC),
        error_reason=None,
        metadata={},
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )

    await lob_processor.project_lob_event(
        payload={
            "event_type": {"id": "postcard.delivered"},
            "body": {"id": "psc_recip_001"},
        },
        event_id="evt_recip_4",
    )
    # Lookup happened, but no update issued.
    assert state["membership_lookups"] == [(step_id, recipient_id)]
    assert state["membership_updates"] == []


# ── Bulk upsert dedupe (pure) ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bulk_upsert_dedupes_input_by_natural_key(monkeypatch) -> None:
    """``bulk_upsert_recipients`` should collapse duplicate (source, id)
    pairs in the input list to a single DB upsert call."""
    org_id = uuid4()
    calls: list[RecipientSpec] = []

    async def fake_upsert(*, organization_id, spec):
        calls.append(spec)
        return RecipientResponse(
            id=uuid4(),
            organization_id=organization_id,
            recipient_type=spec.recipient_type,
            external_source=spec.external_source,
            external_id=spec.external_id,
            display_name=spec.display_name,
            mailing_address=spec.mailing_address,
            phone=spec.phone,
            email=spec.email,
            metadata=spec.metadata,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )

    monkeypatch.setattr(recipients_service, "upsert_recipient", fake_upsert)

    result = await recipients_service.bulk_upsert_recipients(
        organization_id=org_id,
        specs=[
            RecipientSpec(external_source="fmcsa", external_id="111"),
            RecipientSpec(external_source="fmcsa", external_id="222"),
            RecipientSpec(external_source="fmcsa", external_id="111"),  # dup
            RecipientSpec(external_source="nyc_re", external_id="111"),  # diff source
        ],
    )
    assert len(result) == 3
    assert len(calls) == 3
    seen_keys = {(s.external_source, s.external_id) for s in calls}
    assert seen_keys == {("fmcsa", "111"), ("fmcsa", "222"), ("nyc_re", "111")}


# ── materialize_step_audience requires pending step ──────────────────────


@pytest.mark.asyncio
async def test_materialize_audience_rejects_non_pending_step(monkeypatch) -> None:
    org_id = uuid4()
    step_id = uuid4()

    async def fake_get_step(*, step_id, organization_id):
        from app.models.campaigns import ChannelCampaignStepResponse

        return ChannelCampaignStepResponse(
            id=step_id,
            channel_campaign_id=uuid4(),
            campaign_id=uuid4(),
            organization_id=organization_id,
            brand_id=uuid4(),
            step_order=1,
            name=None,
            delay_days_from_previous=0,
            scheduled_send_at=None,
            creative_ref=None,
            channel_specific_config={},
            external_provider_id=None,
            external_provider_metadata={},
            status="scheduled",  # NOT pending
            activated_at=None,
            metadata={},
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )

    monkeypatch.setattr(steps_service, "get_step", fake_get_step)

    with pytest.raises(steps_service.StepAudienceImmutable):
        await steps_service.materialize_step_audience(
            step_id=step_id,
            organization_id=org_id,
            recipients=[
                RecipientSpec(external_source="fmcsa", external_id="123")
            ],
        )

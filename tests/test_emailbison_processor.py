"""EmailBison projector tests.

The DB layer is mocked at the projector's own helper-function boundary;
we don't run a real DB. That keeps the suite fast and lets us assert the
routing decisions, sticky-terminal guards, idempotency, and analytics
emission directly.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from app.webhooks import emailbison_processor as processor


@pytest.fixture
def projector_mocks(monkeypatch):
    state: dict[str, Any] = {
        # external_provider_id (str) → step ctx dict
        "step_by_external_id": {},
        # uuid str → step ctx dict
        "step_by_uuid": {},
        # email_message_id (UUID) → row dict
        "email_messages": {},
        # (workspace_id, scheduled_email_id) → email_message_id (UUID)
        "messages_by_eb_key": {},
        # list of insert events
        "inserted_events": [],
        # list of (email_message_id, current_status, event_type, occurred_at)
        "aggregate_updates": [],
        # list of (event_id, status)
        "webhook_status_updates": [],
        # list of analytics calls
        "analytics_calls": [],
        # list of (step_id, recipient_id, event_type)
        "membership_calls": [],
    }

    async def fake_resolve_step_by_external_id(*, eb_campaign_id):
        if eb_campaign_id is None:
            return None
        return state["step_by_external_id"].get(str(eb_campaign_id))

    async def fake_resolve_step_by_tag_uuid(*, step_uuid):
        return state["step_by_uuid"].get(step_uuid)

    async def fake_find_email_message(*, eb_workspace_id, eb_scheduled_email_id):
        if eb_scheduled_email_id is None:
            return None
        return state["messages_by_eb_key"].get(
            (eb_workspace_id, eb_scheduled_email_id)
        )

    async def fake_resolve_recipient_by_email(
        *, channel_campaign_step_id, email
    ):
        recipients = state.get("recipients_by_email", {})
        return recipients.get((str(channel_campaign_step_id), email))

    async def fake_insert_email_message(
        *, step_ctx, parsed, initial_status, recipient_id
    ):
        msg_id = uuid4()
        row = {
            "id": msg_id,
            "status": initial_status,
            "recipient_id": recipient_id,
            "channel_campaign_step_id": step_ctx["step_id"],
            "open_count": 0,
            "eb_workspace_id": parsed.eb_workspace_id,
            "eb_scheduled_email_id": parsed.eb_scheduled_email_id,
        }
        state["email_messages"][msg_id] = row
        state["messages_by_eb_key"][
            (parsed.eb_workspace_id, parsed.eb_scheduled_email_id)
        ] = row
        return msg_id

    async def fake_append_email_event(
        *, email_message_id, event_type, raw_event_name, occurred_at, payload
    ):
        key = (email_message_id, raw_event_name, occurred_at)
        for existing_key in state["inserted_events"]:
            if existing_key["key"] == key:
                return False
        state["inserted_events"].append(
            {
                "key": key,
                "email_message_id": email_message_id,
                "event_type": event_type,
                "raw_event_name": raw_event_name,
                "occurred_at": occurred_at,
            }
        )
        return True

    async def fake_apply_aggregate_update(
        *, email_message_id, current_status, event_type, occurred_at
    ):
        state["aggregate_updates"].append(
            (email_message_id, current_status, event_type, occurred_at)
        )
        # Mirror the real reducer logic so other assertions can rely on
        # final status.
        new_status = current_status
        terminal = {"replied", "bounced", "unsubscribed", "failed"}
        pre_opened = {"pending", "scheduled", "sent"}
        msg = state["email_messages"][email_message_id]
        if event_type == "sent":
            msg.setdefault("sent_at", occurred_at)
            if current_status not in terminal and current_status != "opened":
                new_status = "sent"
        elif event_type == "opened":
            msg["open_count"] = msg.get("open_count", 0) + 1
            msg["last_opened_at"] = occurred_at
            if current_status in pre_opened:
                new_status = "opened"
        elif event_type == "replied":
            msg["replied_at"] = occurred_at
            new_status = "replied"
        elif event_type == "bounced":
            msg["bounced_at"] = occurred_at
            if current_status not in terminal or current_status == "bounced":
                new_status = "bounced"
        elif event_type == "unsubscribed":
            msg["unsubscribed_at"] = occurred_at
            if (
                current_status not in terminal
                or current_status == "unsubscribed"
            ):
                new_status = "unsubscribed"
        elif event_type == "manual_sent":
            msg.setdefault("sent_at", occurred_at)
            if current_status not in terminal and current_status != "opened":
                new_status = "sent"
        msg["status"] = new_status
        return new_status

    async def fake_maybe_transition_membership(
        *, step_id, recipient_id, event_type
    ):
        state["membership_calls"].append((step_id, recipient_id, event_type))

    async def fake_emit_analytics(
        *, step_id, event_type, parsed, occurred_at, recipient_id
    ):
        state["analytics_calls"].append(
            {
                "step_id": step_id,
                "event_type": event_type,
                "recipient_id": recipient_id,
                "raw_event_name": parsed.raw_event_name,
            }
        )

    async def fake_update_webhook_status(*, event_id, status):
        state["webhook_status_updates"].append((event_id, status))

    monkeypatch.setattr(
        processor,
        "_resolve_step_by_external_id",
        fake_resolve_step_by_external_id,
    )
    monkeypatch.setattr(
        processor, "_resolve_step_by_tag_uuid", fake_resolve_step_by_tag_uuid
    )
    monkeypatch.setattr(
        processor, "_find_email_message", fake_find_email_message
    )
    monkeypatch.setattr(
        processor,
        "_resolve_recipient_by_email",
        fake_resolve_recipient_by_email,
    )
    monkeypatch.setattr(
        processor, "_insert_email_message", fake_insert_email_message
    )
    monkeypatch.setattr(
        processor, "_append_email_event", fake_append_email_event
    )
    monkeypatch.setattr(
        processor, "_apply_aggregate_update", fake_apply_aggregate_update
    )
    monkeypatch.setattr(
        processor,
        "_maybe_transition_membership",
        fake_maybe_transition_membership,
    )
    monkeypatch.setattr(processor, "_emit_analytics", fake_emit_analytics)

    from app.webhooks import storage as webhook_storage

    monkeypatch.setattr(
        webhook_storage,
        "update_webhook_event_status",
        fake_update_webhook_status,
    )
    return state


def _step_ctx(**overrides) -> dict[str, Any]:
    base = {
        "step_id": uuid4(),
        "channel_campaign_id": uuid4(),
        "campaign_id": uuid4(),
        "organization_id": uuid4(),
        "brand_id": uuid4(),
        "status": "scheduled",
        "channel": "email",
        "provider": "emailbison",
    }
    base.update(overrides)
    return base


def _email_sent_payload(*, eb_campaign_id=99, scheduled_email_id=4, lead_email="lead@example.com"):
    return {
        "event": {"type": "EMAIL_SENT", "workspace_id": 1},
        "data": {
            "scheduled_email": {
                "id": scheduled_email_id,
                "lead_id": 1,
                "sequence_step_id": 2,
                "email_subject": "subj",
                "email_body": "<p>body</p>",
                "sent_at": "2026-04-01T10:00:00Z",
                "raw_message_id": "<m@x>",
            },
            "campaign_event": {"id": 6, "type": "sent", "created_at": "2026-04-01T10:00:00Z"},
            "campaign": {"id": eb_campaign_id, "name": "x"},
            "lead": {"id": 1, "email": lead_email},
            "sender_email": {"id": 3, "email": "from@x.com"},
        },
    }


def _opened_payload(*, eb_campaign_id=99, scheduled_email_id=4, occurred_at="2026-04-01T11:00:00Z"):
    return {
        "event": {"type": "EMAIL_OPENED", "workspace_id": 1},
        "data": {
            "scheduled_email": {"id": scheduled_email_id, "lead_id": 1},
            "campaign_event": {"id": 7, "type": "open", "created_at": occurred_at},
            "campaign": {"id": eb_campaign_id},
            "lead": {"id": 1, "email": "lead@example.com"},
        },
    }


def _replied_payload(*, eb_campaign_id=99, scheduled_email_id=4):
    return {
        "event": {"type": "LEAD_REPLIED", "workspace_id": 1},
        "data": {
            "reply": {"id": 725},
            "campaign_event": {"id": 7, "type": "replied", "created_at": "2026-04-01T12:00:00Z"},
            "scheduled_email": {"id": scheduled_email_id, "lead_id": 1},
            "campaign": {"id": eb_campaign_id},
            "lead": {"id": 1, "email": "lead@example.com"},
            "sender_email": {"id": 3},
        },
    }


def _bounced_payload(*, eb_campaign_id=99, scheduled_email_id=4):
    return {
        "event": {"type": "EMAIL_BOUNCED", "workspace_id": 1},
        "data": {
            "reply": {"id": 800, "type": "Bounced", "folder": "Bounced"},
            "campaign_event": {"id": 8, "type": "bounce", "created_at": "2026-04-01T13:00:00Z"},
            "scheduled_email": {"id": scheduled_email_id, "lead_id": 1},
            "campaign": {"id": eb_campaign_id},
            "lead": {"id": 1, "email": "lead@example.com"},
        },
    }


# ── tests ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_project_email_sent_inserts_email_message_and_emits_analytics(
    projector_mocks,
):
    ctx = _step_ctx()
    projector_mocks["step_by_external_id"]["99"] = ctx
    event_id = uuid4()

    result = await processor.project_emailbison_event(
        webhook_event_id=event_id, payload=_email_sent_payload()
    )
    assert result["status"] == "applied"
    assert result["event_type"] == "sent"
    assert result["new_status"] == "sent"
    assert len(projector_mocks["inserted_events"]) == 1
    assert projector_mocks["analytics_calls"][0]["step_id"] == ctx["step_id"]
    assert projector_mocks["webhook_status_updates"] == [(event_id, "processed")]


@pytest.mark.asyncio
async def test_project_email_opened_increments_count(projector_mocks):
    ctx = _step_ctx()
    projector_mocks["step_by_external_id"]["99"] = ctx

    # Pre-seed an email_message in 'sent' status.
    msg_id = uuid4()
    msg_row = {
        "id": msg_id,
        "status": "sent",
        "recipient_id": None,
        "channel_campaign_step_id": ctx["step_id"],
        "open_count": 0,
    }
    projector_mocks["email_messages"][msg_id] = msg_row
    projector_mocks["messages_by_eb_key"][("1", 4)] = msg_row

    # First open event.
    await processor.project_emailbison_event(
        webhook_event_id=uuid4(), payload=_opened_payload()
    )
    assert msg_row["open_count"] == 1
    assert msg_row["status"] == "opened"

    # Second open event with a later timestamp.
    await processor.project_emailbison_event(
        webhook_event_id=uuid4(),
        payload=_opened_payload(occurred_at="2026-04-01T12:30:00Z"),
    )
    assert msg_row["open_count"] == 2


@pytest.mark.asyncio
async def test_project_lead_replied_sets_replied_at_and_membership_sent(
    projector_mocks,
):
    ctx = _step_ctx()
    projector_mocks["step_by_external_id"]["99"] = ctx

    recipient_id = uuid4()
    msg_id = uuid4()
    msg_row = {
        "id": msg_id,
        "status": "sent",
        "recipient_id": recipient_id,
        "channel_campaign_step_id": ctx["step_id"],
        "open_count": 0,
    }
    projector_mocks["email_messages"][msg_id] = msg_row
    projector_mocks["messages_by_eb_key"][("1", 4)] = msg_row

    await processor.project_emailbison_event(
        webhook_event_id=uuid4(), payload=_replied_payload()
    )
    assert msg_row["status"] == "replied"
    assert "replied_at" in msg_row
    assert (ctx["step_id"], recipient_id, "replied") in projector_mocks[
        "membership_calls"
    ]


@pytest.mark.asyncio
async def test_project_email_bounced_sets_failed_membership(projector_mocks):
    ctx = _step_ctx()
    projector_mocks["step_by_external_id"]["99"] = ctx

    recipient_id = uuid4()
    msg_id = uuid4()
    msg_row = {
        "id": msg_id,
        "status": "sent",
        "recipient_id": recipient_id,
        "channel_campaign_step_id": ctx["step_id"],
        "open_count": 0,
    }
    projector_mocks["email_messages"][msg_id] = msg_row
    projector_mocks["messages_by_eb_key"][("1", 4)] = msg_row

    await processor.project_emailbison_event(
        webhook_event_id=uuid4(), payload=_bounced_payload()
    )
    assert msg_row["status"] == "bounced"
    # Membership call carries the bounced event_type — the helper itself
    # maps bounced/unsubscribed → 'failed'. We only need to verify the
    # call happened with the right event_type.
    assert projector_mocks["membership_calls"][-1] == (
        ctx["step_id"],
        recipient_id,
        "bounced",
    )


@pytest.mark.asyncio
async def test_project_unresolvable_event_marks_orphaned(projector_mocks):
    event_id = uuid4()
    result = await processor.project_emailbison_event(
        webhook_event_id=event_id,
        payload=_email_sent_payload(eb_campaign_id=99999),
    )
    assert result["status"] == "orphaned"
    assert projector_mocks["webhook_status_updates"] == [(event_id, "orphaned")]
    assert projector_mocks["email_messages"] == {}
    assert projector_mocks["analytics_calls"] == []


@pytest.mark.asyncio
async def test_sticky_terminal_replied_does_not_regress_on_later_opened(
    projector_mocks,
):
    ctx = _step_ctx()
    projector_mocks["step_by_external_id"]["99"] = ctx

    msg_id = uuid4()
    msg_row = {
        "id": msg_id,
        "status": "replied",
        "recipient_id": None,
        "channel_campaign_step_id": ctx["step_id"],
        "open_count": 0,
    }
    projector_mocks["email_messages"][msg_id] = msg_row
    projector_mocks["messages_by_eb_key"][("1", 4)] = msg_row

    await processor.project_emailbison_event(
        webhook_event_id=uuid4(), payload=_opened_payload()
    )
    assert msg_row["status"] == "replied"
    assert msg_row["open_count"] == 1


@pytest.mark.asyncio
async def test_idempotent_re_projection_of_same_event(projector_mocks):
    ctx = _step_ctx()
    projector_mocks["step_by_external_id"]["99"] = ctx

    payload = _email_sent_payload()
    event_id_1 = uuid4()
    event_id_2 = uuid4()

    await processor.project_emailbison_event(
        webhook_event_id=event_id_1, payload=payload
    )
    await processor.project_emailbison_event(
        webhook_event_id=event_id_2, payload=payload
    )
    assert len(projector_mocks["inserted_events"]) == 1


@pytest.mark.asyncio
async def test_tag_fallback_resolves_when_external_id_misses(projector_mocks):
    """If primary lookup fails, the projector falls back to hqx:step tag."""
    ctx = _step_ctx()
    projector_mocks["step_by_uuid"][str(ctx["step_id"])] = ctx

    payload = {
        "event": {"type": "EMAIL_SENT", "workspace_id": 1},
        "data": {
            "scheduled_email": {"id": 4, "lead_id": 1, "sent_at": "2026-04-01T10:00:00Z"},
            "campaign_event": {"id": 6, "type": "sent", "created_at": "2026-04-01T10:00:00Z"},
            "campaign": {
                "id": 9999,  # Not registered
                "tags": [{"id": 1, "name": f"hqx:step={ctx['step_id']}"}],
            },
            "lead": {"id": 1, "email": "lead@example.com"},
            "sender_email": {"id": 3},
        },
    }
    result = await processor.project_emailbison_event(
        webhook_event_id=uuid4(), payload=payload
    )
    assert result["status"] == "applied"

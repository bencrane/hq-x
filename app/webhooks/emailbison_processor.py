"""EmailBison webhook event projection.

Per-message events (email_sent, email_opened, lead_replied,
email_bounced, lead_unsubscribed, lead_interested, manual_email_sent):
  1. Resolve the channel_campaign_step (primary: external_provider_id ==
     EB campaign id; fallback: hqx:step=<uuid> tag from data.campaign.tags).
  2. Upsert into business.email_messages keyed on
     (eb_workspace_id, eb_scheduled_email_id).
  3. Append a row to business.email_message_events (idempotent on
     (email_message_id, raw_event_name, occurred_at)).
  4. Update aggregate columns and status with sticky-terminal guards.
  5. Transition the matching channel_campaign_step_recipients row.
  6. Emit a fully-tagged analytics event.
  7. Mark webhook_events.status = 'processed' (or 'orphaned' if no step
     matched, 'dead_letter' on unhandled exception).

The other 10 EB event types (account_*, warmup_*, tag_*,
untracked_reply_received, lead_first_contacted) are accepted without
error: if a step + message can be resolved we still append the audit row,
but no status / counter mutation happens. Observability-only.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.db import get_db_connection
from app.providers.emailbison.adapter import (
    EmailBisonAdapter,
    ParsedEmailBisonEvent,
)
from app.webhooks import storage as webhook_storage

logger = logging.getLogger(__name__)


# Event types that mutate status / counters on email_messages. Everything
# else is observability-only (audit row only).
_PROJECTABLE_EVENT_TYPES = {
    "sent",
    "opened",
    "replied",
    "bounced",
    "unsubscribed",
    "interested",
    "manual_sent",
}

_TERMINAL_STATUSES = {"replied", "bounced", "unsubscribed", "failed"}
_PRE_OPENED_STATUSES = {"pending", "scheduled", "sent"}


def _occurred_at(parsed: ParsedEmailBisonEvent) -> datetime:
    raw = parsed.occurred_at_raw
    if raw is None:
        return datetime.now(UTC)
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(int(raw), tz=UTC)
        except (ValueError, OSError):
            return datetime.now(UTC)
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(UTC)
    return datetime.now(UTC)


# ── DB helpers (kept module-level so tests can monkeypatch) ───────────────


async def _resolve_step_by_external_id(
    *, eb_campaign_id: int | None
) -> dict[str, Any] | None:
    if eb_campaign_id is None:
        return None
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT s.id, s.channel_campaign_id, s.campaign_id,
                       s.organization_id, s.brand_id, s.status,
                       cc.channel, cc.provider
                FROM business.channel_campaign_steps s
                JOIN business.channel_campaigns cc
                  ON cc.id = s.channel_campaign_id
                WHERE s.external_provider_id = %s
                LIMIT 1
                """,
                (str(eb_campaign_id),),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    return {
        "step_id": row[0],
        "channel_campaign_id": row[1],
        "campaign_id": row[2],
        "organization_id": row[3],
        "brand_id": row[4],
        "status": row[5],
        "channel": row[6],
        "provider": row[7],
    }


async def _resolve_step_by_tag_uuid(
    *, step_uuid: str
) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT s.id, s.channel_campaign_id, s.campaign_id,
                       s.organization_id, s.brand_id, s.status,
                       cc.channel, cc.provider
                FROM business.channel_campaign_steps s
                JOIN business.channel_campaigns cc
                  ON cc.id = s.channel_campaign_id
                WHERE s.id = %s
                LIMIT 1
                """,
                (step_uuid,),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    return {
        "step_id": row[0],
        "channel_campaign_id": row[1],
        "campaign_id": row[2],
        "organization_id": row[3],
        "brand_id": row[4],
        "status": row[5],
        "channel": row[6],
        "provider": row[7],
    }


async def _find_email_message(
    *,
    eb_workspace_id: str | None,
    eb_scheduled_email_id: int | None,
) -> dict[str, Any] | None:
    if eb_scheduled_email_id is None:
        return None
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, status, recipient_id, channel_campaign_step_id,
                       open_count
                FROM business.email_messages
                WHERE eb_scheduled_email_id = %s
                  AND (eb_workspace_id IS NOT DISTINCT FROM %s)
                LIMIT 1
                """,
                (eb_scheduled_email_id, eb_workspace_id),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "status": row[1],
        "recipient_id": row[2],
        "channel_campaign_step_id": row[3],
        "open_count": row[4],
    }


async def _resolve_recipient_by_email(
    *,
    channel_campaign_step_id: UUID,
    email: str | None,
) -> UUID | None:
    if not email:
        return None
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT r.id
                FROM business.recipients r
                JOIN business.channel_campaign_step_recipients m
                  ON m.recipient_id = r.id
                WHERE m.channel_campaign_step_id = %s
                  AND lower(r.email) = lower(%s)
                LIMIT 1
                """,
                (str(channel_campaign_step_id), email),
            )
            row = await cur.fetchone()
    return row[0] if row else None


async def _insert_email_message(
    *,
    step_ctx: dict[str, Any],
    parsed: ParsedEmailBisonEvent,
    initial_status: str,
    recipient_id: UUID | None,
) -> UUID:
    from psycopg.types.json import Jsonb

    metadata = parsed.metadata or {}
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO business.email_messages (
                    organization_id, brand_id, campaign_id,
                    channel_campaign_id, channel_campaign_step_id,
                    recipient_id,
                    eb_workspace_id, eb_lead_id, eb_campaign_id,
                    eb_scheduled_email_id, eb_sequence_step_id,
                    eb_sender_email_id, raw_message_id,
                    subject_snapshot, body_snapshot, sender_email_snapshot,
                    status, metadata
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s)
                ON CONFLICT (eb_workspace_id, eb_scheduled_email_id)
                  WHERE eb_scheduled_email_id IS NOT NULL
                  DO UPDATE SET updated_at = NOW()
                RETURNING id
                """,
                (
                    str(step_ctx["organization_id"]),
                    str(step_ctx["brand_id"]),
                    str(step_ctx["campaign_id"]),
                    str(step_ctx["channel_campaign_id"]),
                    str(step_ctx["step_id"]),
                    str(recipient_id) if recipient_id else None,
                    parsed.eb_workspace_id,
                    parsed.eb_lead_id,
                    parsed.eb_campaign_id,
                    parsed.eb_scheduled_email_id,
                    metadata.get("sequence_step_id"),
                    parsed.eb_sender_email_id,
                    metadata.get("raw_message_id"),
                    metadata.get("subject_snapshot"),
                    metadata.get("body_snapshot"),
                    metadata.get("sender_email_snapshot"),
                    initial_status,
                    Jsonb({}),
                ),
            )
            row = await cur.fetchone()
    assert row is not None
    return row[0]


async def _append_email_event(
    *,
    email_message_id: UUID,
    event_type: str,
    raw_event_name: str,
    occurred_at: datetime,
    payload: dict[str, Any],
) -> bool:
    """Insert into email_message_events idempotent on the dedup tuple.

    Returns True if a new row was inserted, False if the conflict path
    was taken (replay of the same event).
    """
    from psycopg.types.json import Jsonb

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO business.email_message_events
                    (email_message_id, event_type, raw_event_name,
                     occurred_at, payload)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (email_message_id, raw_event_name, occurred_at)
                  DO NOTHING
                RETURNING id
                """,
                (
                    str(email_message_id),
                    event_type,
                    raw_event_name,
                    occurred_at,
                    Jsonb(payload),
                ),
            )
            row = await cur.fetchone()
    return row is not None


async def _apply_aggregate_update(
    *,
    email_message_id: UUID,
    current_status: str,
    event_type: str,
    occurred_at: datetime,
) -> str:
    """Update email_messages aggregate columns + status with sticky guards.

    Returns the resulting status (callers use it to drive membership
    transitions). No-ops on event types that aren't projectable.
    """
    if event_type not in _PROJECTABLE_EVENT_TYPES:
        return current_status

    new_status = current_status

    # Compute set clauses for status + the right counter columns.
    set_parts: list[str] = []
    args: list[Any] = []

    if event_type == "sent":
        set_parts.append("sent_at = COALESCE(sent_at, %s)")
        args.append(occurred_at)
        if current_status not in _TERMINAL_STATUSES and current_status != "opened":
            new_status = "sent"
    elif event_type == "opened":
        set_parts.append("last_opened_at = GREATEST(COALESCE(last_opened_at, %s), %s)")
        args.append(occurred_at)
        args.append(occurred_at)
        set_parts.append("open_count = open_count + 1")
        if current_status in _PRE_OPENED_STATUSES:
            new_status = "opened"
    elif event_type == "replied":
        set_parts.append("replied_at = COALESCE(replied_at, %s)")
        args.append(occurred_at)
        new_status = "replied"
    elif event_type == "bounced":
        set_parts.append("bounced_at = COALESCE(bounced_at, %s)")
        args.append(occurred_at)
        if current_status not in _TERMINAL_STATUSES or current_status == "bounced":
            new_status = "bounced"
    elif event_type == "unsubscribed":
        set_parts.append("unsubscribed_at = COALESCE(unsubscribed_at, %s)")
        args.append(occurred_at)
        if current_status not in _TERMINAL_STATUSES or current_status == "unsubscribed":
            new_status = "unsubscribed"
    elif event_type == "interested":
        # No counter change; promote pre-opened statuses to 'opened' if we
        # haven't already replied/bounced. The "interested" signal is not
        # itself a status transition — analytics-only.
        pass
    elif event_type == "manual_sent":
        set_parts.append("sent_at = COALESCE(sent_at, %s)")
        args.append(occurred_at)
        if current_status not in _TERMINAL_STATUSES and current_status != "opened":
            new_status = "sent"

    if new_status != current_status:
        set_parts.append("status = %s")
        args.append(new_status)

    if not set_parts:
        return new_status

    set_parts.append("updated_at = NOW()")
    args.append(str(email_message_id))

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE business.email_messages
                SET {', '.join(set_parts)}
                WHERE id = %s
                """,
                args,
            )
    return new_status


async def _maybe_transition_membership(
    *,
    step_id: UUID,
    recipient_id: UUID | None,
    event_type: str,
) -> None:
    """Transition the step_recipients row in lockstep with email_messages.

    sent / opened / replied / interested / manual_sent → 'sent' (if not
        already 'sent' / terminal)
    bounced / unsubscribed → 'failed'
    """
    if recipient_id is None:
        return
    if event_type not in _PROJECTABLE_EVENT_TYPES:
        return

    if event_type in ("bounced", "unsubscribed"):
        new_membership_status = "failed"
    else:
        new_membership_status = "sent"

    try:
        from app.services.recipients import (
            find_membership_for_recipient,
            update_membership_status,
        )

        membership = await find_membership_for_recipient(
            channel_campaign_step_id=step_id, recipient_id=recipient_id
        )
        if membership is None:
            return
        if membership.status in ("sent", "failed", "cancelled", "suppressed"):
            return
        await update_membership_status(
            membership_id=membership.id,
            new_status=new_membership_status,
            set_processed_at=True,
        )
    except Exception:  # pragma: no cover — best effort
        logger.exception(
            "membership transition failed step=%s recipient=%s",
            step_id,
            recipient_id,
        )


async def _emit_analytics(
    *,
    step_id: UUID,
    event_type: str,
    parsed: ParsedEmailBisonEvent,
    occurred_at: datetime,
    recipient_id: UUID | None,
) -> None:
    """Fire-and-forget analytics. Must never block ack."""
    try:
        from app.services.analytics import emit_event

        properties: dict[str, Any] = {
            "raw_event_name": parsed.raw_event_name,
            "eb_workspace_id": parsed.eb_workspace_id,
            "eb_campaign_id": parsed.eb_campaign_id,
            "eb_scheduled_email_id": parsed.eb_scheduled_email_id,
            "eb_lead_id": parsed.eb_lead_id,
            "eb_sender_email_id": parsed.eb_sender_email_id,
            "eb_reply_id": parsed.eb_reply_id,
            "occurred_at": occurred_at.isoformat(),
        }
        if recipient_id is not None:
            properties["recipient_id"] = str(recipient_id)
        await emit_event(
            event_name=f"emailbison.{event_type}",
            channel_campaign_step_id=step_id,
            recipient_id=recipient_id,
            properties=properties,
        )
    except Exception:  # pragma: no cover — analytics swallows
        logger.exception(
            "analytics emit failed for emailbison step=%s", step_id
        )


# ── Entry point ───────────────────────────────────────────────────────────


async def project_emailbison_event(
    *, webhook_event_id: UUID, payload: dict[str, Any]
) -> dict[str, Any]:
    """Project a single EmailBison webhook event.

    Returns a small status dict for observability:
      * ``applied``  — state was updated
      * ``orphaned`` — payload understood but no step matched
      * ``skipped``  — payload had no resource ids the projector cares about
    """
    try:
        return await _project(
            webhook_event_id=webhook_event_id, payload=payload
        )
    except Exception:
        try:
            await webhook_storage.update_webhook_event_status(
                event_id=webhook_event_id, status="dead_letter"
            )
        except Exception:  # pragma: no cover
            logger.exception(
                "failed to mark webhook_event=%s dead_letter", webhook_event_id
            )
        raise


async def _project(
    *, webhook_event_id: UUID, payload: dict[str, Any]
) -> dict[str, Any]:
    parsed = EmailBisonAdapter.parse_webhook_event(payload)
    occurred_at = _occurred_at(parsed)

    # 1. Resolve to a channel_campaign_step.
    step_ctx = await _resolve_step_by_external_id(
        eb_campaign_id=parsed.eb_campaign_id
    )
    if step_ctx is None:
        step_uuid = parsed.six_tuple_tags.get("step")
        if step_uuid:
            step_ctx = await _resolve_step_by_tag_uuid(step_uuid=step_uuid)
    if step_ctx is None:
        await webhook_storage.update_webhook_event_status(
            event_id=webhook_event_id, status="orphaned"
        )
        return {
            "status": "orphaned",
            "raw_event_name": parsed.raw_event_name,
            "eb_campaign_id": parsed.eb_campaign_id,
            "eb_scheduled_email_id": parsed.eb_scheduled_email_id,
        }

    # 2. Resolve / insert email_messages row.
    if parsed.eb_scheduled_email_id is None:
        # Pure step-level event (e.g. tag_attached on the campaign).
        # Nothing to do at the per-message granularity; we still emit
        # analytics so step-level signals surface.
        await _emit_analytics(
            step_id=step_ctx["step_id"],
            event_type=parsed.event_type,
            parsed=parsed,
            occurred_at=occurred_at,
            recipient_id=None,
        )
        await webhook_storage.update_webhook_event_status(
            event_id=webhook_event_id, status="processed"
        )
        return {
            "status": "applied",
            "scope": "step",
            "step_id": str(step_ctx["step_id"]),
            "event_type": parsed.event_type,
        }

    existing = await _find_email_message(
        eb_workspace_id=parsed.eb_workspace_id,
        eb_scheduled_email_id=parsed.eb_scheduled_email_id,
    )
    if existing is None:
        recipient_id = await _resolve_recipient_by_email(
            channel_campaign_step_id=step_ctx["step_id"],
            email=parsed.metadata.get("lead_email")
            if isinstance(parsed.metadata, dict)
            else None,
        )
        initial_status = "sent" if parsed.event_type == "sent" else "pending"
        email_message_id = await _insert_email_message(
            step_ctx=step_ctx,
            parsed=parsed,
            initial_status=initial_status,
            recipient_id=recipient_id,
        )
        current_status = initial_status
    else:
        email_message_id = existing["id"]
        current_status = existing["status"]
        recipient_id = existing.get("recipient_id")

    # 3. Append audit event (idempotent on dedup tuple).
    appended = await _append_email_event(
        email_message_id=email_message_id,
        event_type=parsed.event_type,
        raw_event_name=parsed.raw_event_name,
        occurred_at=occurred_at,
        payload=payload,
    )

    # 4. Apply aggregate column / status update.
    new_status = await _apply_aggregate_update(
        email_message_id=email_message_id,
        current_status=current_status,
        event_type=parsed.event_type,
        occurred_at=occurred_at,
    )

    # 5. Membership transition.
    await _maybe_transition_membership(
        step_id=step_ctx["step_id"],
        recipient_id=recipient_id if isinstance(recipient_id, UUID) else None,
        event_type=parsed.event_type,
    )

    # 6. Analytics.
    await _emit_analytics(
        step_id=step_ctx["step_id"],
        event_type=parsed.event_type,
        parsed=parsed,
        occurred_at=occurred_at,
        recipient_id=recipient_id if isinstance(recipient_id, UUID) else None,
    )

    # 7. Mark the webhook_events row processed.
    await webhook_storage.update_webhook_event_status(
        event_id=webhook_event_id, status="processed"
    )

    return {
        "status": "applied",
        "scope": "message",
        "step_id": str(step_ctx["step_id"]),
        "email_message_id": str(email_message_id),
        "event_type": parsed.event_type,
        "previous_status": current_status,
        "new_status": new_status,
        "audit_inserted": appended,
    }


__all__ = ["project_emailbison_event"]

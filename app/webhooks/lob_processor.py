"""Lob webhook event projection.

Per-piece events (postcard.delivered, etc.):
  1. Find the existing direct_mail_pieces row by external_piece_id.
  2. Append a row to direct_mail_piece_events.
  3. Update the piece's `status` if the event's mapping says to.
  4. On suppression-trigger events (returned/failed and certified-returned),
     populate suppressed_addresses idempotently.
  5. Emit an analytics event tagged with the full six-tuple
     (organization_id, brand_id, campaign_id, channel_campaign_id,
     channel_campaign_step_id, channel='direct_mail', provider='lob').

Campaign-level events (campaign.created, campaign.deleted, etc.):
  Resolve to a channel_campaign_step row by external_provider_id, update
  the step's status if applicable, and emit an analytics event scoped to
  that step.

Lookup failures result in ``status='orphaned'`` rather than silent skip
so operators can dashboard them.

`normalize_lob_piece_status` returns None for events that should not change
the piece's status (engagement events like `viewed`, `informed_delivery.*`,
and the `return_envelope.*` family). The append to direct_mail_piece_events
still happens for those — full audit log.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.direct_mail.addresses import insert_suppression
from app.direct_mail.persistence import (
    append_piece_event,
    get_piece_by_external_id,
    update_piece_status,
)
from app.observability import incr_metric, log_event
from app.webhooks.lob_normalization import (
    SUPPRESSION_TRIGGERS,
    extract_lob_event_name,
    extract_lob_piece_address,
    extract_lob_piece_id,
    normalize_lob_event_type,
    normalize_lob_piece_status,
)

logger = logging.getLogger(__name__)


def _occurred_at(payload: dict[str, Any]) -> datetime:
    raw = payload.get("date_created") or payload.get("created_at") or payload.get("time")
    if not raw:
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


async def _emit_analytics_for_step(
    *,
    step_id: UUID,
    event_name: str,
    properties: dict[str, Any],
    recipient_id: UUID | None = None,
) -> None:
    """Emit a fully-tagged analytics event for a webhook projection.

    Swallows any exception — analytics is fire-and-forget and must never
    block webhook acknowledgement.
    """
    try:
        from app.services.analytics import emit_event

        await emit_event(
            event_name=event_name,
            channel_campaign_step_id=step_id,
            recipient_id=recipient_id,
            properties=properties,
        )
    except Exception:  # pragma: no cover — analytics layer also swallows
        logger.exception("analytics emit failed for step=%s", step_id)


# Lob piece events that end the membership lifecycle for that recipient.
# Keys are normalized internal event names (see lob_normalization).
_PIECE_TERMINAL_SENT = {
    "piece.mailed",
    "piece.in_transit",
    "piece.in_local_area",
    "piece.processed_for_delivery",
    "piece.delivered",
    "piece.certified.mailed",
    "piece.certified.in_transit",
    "piece.certified.delivered",
}
_PIECE_TERMINAL_FAILED = {
    "piece.failed",
    "piece.rejected",
    "piece.returned",
    "piece.certified.returned",
}


async def _maybe_transition_membership(
    *,
    step_id: UUID,
    recipient_id: UUID,
    normalized_event: str,
) -> None:
    """If the event is a per-piece terminal signal, advance the recipient's
    step membership status. Best-effort."""
    if (
        normalized_event not in _PIECE_TERMINAL_SENT
        and normalized_event not in _PIECE_TERMINAL_FAILED
    ):
        return
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
        new_status = (
            "failed" if normalized_event in _PIECE_TERMINAL_FAILED else "sent"
        )
        await update_membership_status(
            membership_id=membership.id,
            new_status=new_status,
            set_processed_at=True,
        )
        # Slice 4 — multi-step scheduler hook. After flipping a membership
        # to terminal, check if the step itself is now complete; if so,
        # schedule step N+1 with durable sleep.
        try:
            from app.services.step_scheduler import (
                maybe_complete_step_and_schedule_next,
            )

            await maybe_complete_step_and_schedule_next(step_id=step_id)
        except Exception:  # pragma: no cover — observability hard rule
            logger.exception(
                "step_scheduler.completion_hook_failed step=%s",
                step_id,
            )
    except Exception:  # pragma: no cover — best effort
        logger.exception(
            "membership transition failed step=%s recipient=%s",
            step_id,
            recipient_id,
        )


async def _project_piece_event(
    *,
    payload: dict[str, Any],
    event_id: str,
    external_piece_id: str,
    normalized_event: str,
    occurred_at: datetime,
) -> dict[str, Any]:
    """Per-piece projection (postcard.delivered, letter.in_transit, ...)."""
    new_status = normalize_lob_piece_status(normalized_event)
    existing = await get_piece_by_external_id(external_piece_id=external_piece_id)
    if existing is None:
        # Fall back to a step-level lookup before declaring orphan: a
        # campaign-level event can fire before pieces have been projected.
        return None  # type: ignore[return-value]  # signal fall-through to step lookup

    previous_status = existing.status
    await append_piece_event(
        piece_id=existing.id,
        event_type=normalized_event,
        previous_status=previous_status,
        new_status=new_status,
        occurred_at=occurred_at,
        source_event_id=event_id,
        raw_payload=payload,
    )

    if new_status is not None and new_status != previous_status:
        await update_piece_status(piece_id=existing.id, new_status=new_status)
        incr_metric(
            "direct_mail.piece.status_transition",
            from_status=previous_status,
            to_status=new_status,
        )

    suppressed_inserted = False
    suppression_reason = SUPPRESSION_TRIGGERS.get(normalized_event)
    if suppression_reason is not None:
        address = extract_lob_piece_address(payload)
        if address is None and isinstance(existing.raw_payload, dict):
            existing_to = existing.raw_payload.get("to")
            if isinstance(existing_to, dict):
                address = existing_to
        if address is not None:
            suppressed_inserted = await insert_suppression(
                address=address,
                reason=suppression_reason,
                source_event_id=event_id,
                source_piece_id=existing.id,
                notes=f"Auto-suppressed from Lob {normalized_event} event.",
            )

    # Emit analytics if the piece carries a step id (it should, post-0023).
    step_id = getattr(existing, "channel_campaign_step_id", None)
    recipient_id = getattr(existing, "recipient_id", None)
    if step_id is not None:
        await _emit_analytics_for_step(
            step_id=step_id,
            event_name=f"lob.{normalized_event}",
            recipient_id=recipient_id,
            properties={
                "direct_mail_piece_id": str(existing.id),
                "external_piece_id": external_piece_id,
                "previous_status": previous_status,
                "new_status": new_status,
                "occurred_at": occurred_at.isoformat(),
                **(
                    {"recipient_id": str(recipient_id)}
                    if recipient_id is not None
                    else {}
                ),
            },
        )
        if recipient_id is not None:
            await _maybe_transition_membership(
                step_id=step_id,
                recipient_id=recipient_id,
                normalized_event=normalized_event,
            )

    incr_metric(
        "webhook.projection.applied", provider_slug="lob", normalized_event=normalized_event
    )
    return {
        "status": "applied",
        "scope": "piece",
        "piece_id": str(existing.id),
        "previous_status": previous_status,
        "new_status": new_status,
        "normalized_event": normalized_event,
        "suppression_inserted": suppressed_inserted,
    }


async def _project_step_event(
    *,
    payload: dict[str, Any],
    event_id: str,
    lob_campaign_id: str,
    normalized_event: str,
    raw_event_name: str | None,
    occurred_at: datetime,
) -> dict[str, Any] | None:
    """Step-level projection. Returns None if the lob_campaign_id does not
    map to any step (callers handle as orphan)."""
    from app.services.channel_campaign_steps import (
        lookup_step_by_external_provider_id,
    )

    found = await lookup_step_by_external_provider_id(
        external_provider_id=lob_campaign_id
    )
    if found is None:
        return None

    # Lob campaign-level events that imply a state change for the step.
    # We use the raw event name here because normalize_lob_event_type is
    # piece-centric and collapses unknown events to 'piece.unknown'.
    # Conservative defaults: only the 'failed' family flips status to
    # failed; 'deleted' / 'cancelled' map to cancelled.
    new_status: str | None = None
    raw = (raw_event_name or "").lower()
    if "failed" in raw:
        new_status = "failed"
    elif "deleted" in raw or "cancel" in raw:
        new_status = "cancelled"

    if new_status is not None and found["status"] != new_status:
        from app.services.channel_campaign_steps import update_step_status

        await update_step_status(step_id=found["step_id"], new_status=new_status)
        incr_metric(
            "channel_campaign_step.status_transition",
            from_status=found["status"],
            to_status=new_status,
        )

    await _emit_analytics_for_step(
        step_id=found["step_id"],
        event_name=f"lob.{normalized_event}",
        properties={
            "lob_campaign_id": lob_campaign_id,
            "previous_status": found["status"],
            "new_status": new_status or found["status"],
            "occurred_at": occurred_at.isoformat(),
        },
    )

    incr_metric(
        "webhook.projection.applied",
        provider_slug="lob",
        normalized_event=normalized_event,
        scope="step",
    )
    return {
        "status": "applied",
        "scope": "step",
        "step_id": str(found["step_id"]),
        "lob_campaign_id": lob_campaign_id,
        "normalized_event": normalized_event,
        "new_status": new_status,
    }


async def project_lob_event(*, payload: dict[str, Any], event_id: str) -> dict[str, Any]:
    """Project one Lob webhook event into hq-x state.

    Returns a small status dict describing what was done. The caller
    (the webhook receiver) inspects the ``status`` key:
      * ``applied``  — state was updated (piece or step)
      * ``orphaned`` — payload was understood but no internal entity matched;
                       the receiver marks the webhook_events row accordingly
      * ``skipped``  — payload could not be parsed (no resource id at all)
    """
    from app.providers.lob.adapter import LobAdapter

    raw_event_name = extract_lob_event_name(payload)
    normalized_event = normalize_lob_event_type(raw_event_name)
    occurred_at = _occurred_at(payload)

    parsed = LobAdapter.parse_webhook_event(payload)
    external_piece_id = parsed.lob_piece_id or extract_lob_piece_id(payload)
    external_piece_id = str(external_piece_id) if external_piece_id else None
    lob_campaign_id = parsed.lob_campaign_id

    # 1. Piece-scoped path (most events). If we find the piece, apply.
    if external_piece_id:
        piece_result = await _project_piece_event(
            payload=payload,
            event_id=event_id,
            external_piece_id=external_piece_id,
            normalized_event=normalized_event,
            occurred_at=occurred_at,
        )
        if piece_result is not None:
            return piece_result
        # Piece didn't match — try step-level fallback below.

    # 2. Step-scoped fallback (campaign-level events, or piece events
    #    received before the piece row was written).
    if lob_campaign_id:
        step_result = await _project_step_event(
            payload=payload,
            event_id=event_id,
            lob_campaign_id=lob_campaign_id,
            normalized_event=normalized_event,
            raw_event_name=raw_event_name,
            occurred_at=occurred_at,
        )
        if step_result is not None:
            return step_result

    # 3. No resource id at all — projector cannot do anything.
    if not external_piece_id and not lob_campaign_id:
        incr_metric(
            "webhook.projection.failure",
            provider_slug="lob",
            reason="missing_resource_id",
        )
        log_event(
            "lob_projection_missing_resource_id",
            level=logging.WARNING,
            event_id=event_id,
            normalized_event=normalized_event,
        )
        return {
            "status": "skipped",
            "reason": "missing_resource_id",
            "normalized_event": normalized_event,
        }

    # 4. Resource id present but neither piece nor step found — orphan.
    incr_metric(
        "webhook.projection.failure", provider_slug="lob", reason="orphaned"
    )
    log_event(
        "lob_projection_orphaned",
        level=logging.WARNING,
        event_id=event_id,
        external_piece_id=external_piece_id,
        lob_campaign_id=lob_campaign_id,
        normalized_event=normalized_event,
    )
    return {
        "status": "orphaned",
        "external_piece_id": external_piece_id,
        "lob_campaign_id": lob_campaign_id,
        "normalized_event": normalized_event,
    }

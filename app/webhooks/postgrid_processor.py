"""PostGrid webhook event projection.

Mirrors lob_processor.py in structure. Projects PostGrid webhook events
into hq-x state (direct_mail_pieces, direct_mail_piece_events, suppressed_addresses).

Projection logic:
  1. Find the direct_mail_pieces row by external_piece_id (the PostGrid resource id).
  2. Append a row to direct_mail_piece_events.
  3. Update piece status if the event mapping says to.
  4. On suppression-trigger events, populate suppressed_addresses idempotently.
  5. Emit analytics event.

PostGrid IDs use verbose prefixes: letter_*, postcard_*, selfmailer_*, etc.
These are distinct from Lob's ltr_*, psc_*, sfm_* prefixes, so there is no
cross-provider ID collision.
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
    normalize_lob_piece_status,
)
from app.webhooks.postgrid_normalization import (
    extract_postgrid_event_type,
    extract_postgrid_resource_address,
    extract_postgrid_resource_id,
    normalize_postgrid_event_type,
)

logger = logging.getLogger(__name__)


def _occurred_at(payload: dict[str, Any]) -> datetime:
    raw = payload.get("created_at") or payload.get("date_created") or payload.get("time")
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
    try:
        from app.services.analytics import emit_event

        await emit_event(
            event_name=event_name,
            channel_campaign_step_id=step_id,
            recipient_id=recipient_id,
            properties=properties,
        )
    except Exception:
        logger.exception("analytics emit failed for step=%s", step_id)


async def project_postgrid_event(
    *, payload: dict[str, Any], event_id: str
) -> dict[str, Any]:
    """Project one PostGrid webhook event into hq-x state.

    Returns a status dict:
      * 'applied'  — state was updated
      * 'orphaned' — payload understood but no internal entity matched
      * 'skipped'  — payload could not be parsed (no resource id)
    """
    raw_event_type = extract_postgrid_event_type(payload)
    normalized_event = normalize_postgrid_event_type(raw_event_type)
    occurred_at = _occurred_at(payload)
    new_status = normalize_lob_piece_status(normalized_event)

    external_piece_id = extract_postgrid_resource_id(payload)

    if not external_piece_id:
        incr_metric(
            "webhook.projection.failure",
            provider_slug="postgrid",
            reason="missing_resource_id",
        )
        log_event(
            "postgrid_projection_missing_resource_id",
            level=logging.WARNING,
            event_id=event_id,
            normalized_event=normalized_event,
        )
        return {
            "status": "skipped",
            "reason": "missing_resource_id",
            "normalized_event": normalized_event,
        }

    existing = await get_piece_by_external_id(external_piece_id=external_piece_id)
    if existing is None:
        incr_metric(
            "webhook.projection.failure",
            provider_slug="postgrid",
            reason="orphaned",
        )
        log_event(
            "postgrid_projection_orphaned",
            level=logging.WARNING,
            event_id=event_id,
            external_piece_id=external_piece_id,
            normalized_event=normalized_event,
        )
        return {
            "status": "orphaned",
            "external_piece_id": external_piece_id,
            "normalized_event": normalized_event,
        }

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
        address = extract_postgrid_resource_address(payload)
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
                notes=f"Auto-suppressed from PostGrid {normalized_event} event.",
            )

    step_id = getattr(existing, "channel_campaign_step_id", None)
    recipient_id = getattr(existing, "recipient_id", None)
    if step_id is not None:
        await _emit_analytics_for_step(
            step_id=step_id,
            event_name=f"postgrid.{normalized_event}",
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

    incr_metric(
        "webhook.projection.applied",
        provider_slug="postgrid",
        normalized_event=normalized_event,
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

"""Lob webhook event projection.

Steps:
  1. Find the existing direct_mail_pieces row by external_piece_id.
  2. Append a row to direct_mail_piece_events.
  3. Update the piece's `status` if the event implies a transition.
  4. On returned/failed events, populate suppressed_addresses (idempotently).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app.direct_mail.addresses import insert_suppression
from app.direct_mail.persistence import (
    append_piece_event,
    get_piece_by_external_id,
    update_piece_status,
)
from app.observability import incr_metric, log_event
from app.webhooks.lob_normalization import (
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


_SUPPRESSION_REASONS = {
    "piece.returned": "returned_to_sender",
    "piece.failed": "failed",
}


async def project_lob_event(*, payload: dict[str, Any], event_id: str) -> dict[str, Any]:
    """Project one Lob webhook event into hq-x state.

    Returns a small status dict describing what was done. Caller decides
    whether to treat any of the outcomes as a dead-letter signal.
    """
    raw_event_type = payload.get("type") or payload.get("event_type") or payload.get("event")
    normalized_event = normalize_lob_event_type(
        raw_event_type if isinstance(raw_event_type, str) else None
    )
    new_status = normalize_lob_piece_status(normalized_event)
    occurred_at = _occurred_at(payload)
    external_piece_id = extract_lob_piece_id(payload)

    if not external_piece_id:
        incr_metric("webhook.projection.failure", provider_slug="lob", reason="missing_resource_id")
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

    existing = await get_piece_by_external_id(external_piece_id=str(external_piece_id))
    if existing is None:
        incr_metric("webhook.projection.failure", provider_slug="lob", reason="unknown_piece")
        log_event(
            "lob_projection_unknown_piece",
            level=logging.WARNING,
            event_id=event_id,
            external_piece_id=external_piece_id,
            normalized_event=normalized_event,
        )
        return {
            "status": "skipped",
            "reason": "unknown_piece",
            "external_piece_id": str(external_piece_id),
            "normalized_event": normalized_event,
        }

    previous_status = existing.status
    await append_piece_event(
        piece_id=existing.id,
        event_type=normalized_event,
        previous_status=previous_status,
        new_status=new_status if new_status != "unknown" else None,
        occurred_at=occurred_at,
        source_event_id=event_id,
        raw_payload=payload,
    )

    if new_status != "unknown" and new_status != previous_status:
        await update_piece_status(piece_id=existing.id, new_status=new_status)
        incr_metric(
            "direct_mail.piece.status_transition", from_status=previous_status, to_status=new_status
        )

    suppressed_inserted = False
    suppression_reason = _SUPPRESSION_REASONS.get(normalized_event)
    if suppression_reason is not None:
        address = extract_lob_piece_address(payload)
        if address is None and isinstance(existing.raw_payload, dict):
            address = (
                existing.raw_payload.get("to")
                if isinstance(existing.raw_payload.get("to"), dict)
                else None
            )
        if address is not None:
            suppressed_inserted = await insert_suppression(
                address=address,
                reason=suppression_reason,
                source_event_id=event_id,
                source_piece_id=existing.id,
                notes=f"Auto-suppressed from Lob {normalized_event} event.",
            )

    incr_metric(
        "webhook.projection.applied", provider_slug="lob", normalized_event=normalized_event
    )
    return {
        "status": "applied",
        "piece_id": str(existing.id),
        "previous_status": previous_status,
        "new_status": new_status,
        "normalized_event": normalized_event,
        "suppression_inserted": suppressed_inserted,
    }

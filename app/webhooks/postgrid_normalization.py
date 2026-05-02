"""PostGrid webhook event-type normalization.

Maps PostGrid's event type strings → the canonical `piece.*` taxonomy
defined in app/webhooks/lob_normalization.py. This file does NOT modify
that taxonomy — PostGrid events normalize INTO it.

PostGrid event payload shape:
  {
    "id": "event_xxx",
    "type": "letter.delivered",
    "data": {"object": {<full resource>}},
    "created_at": "<iso8601>"
  }

The `type` field is a dotted string: `{resource}.{status}`.

Cross-reference: docs/research/postgrid-print-mail-api-notes.md §5.
"""

from __future__ import annotations

import hashlib
from typing import Any


def extract_postgrid_event_type(payload: dict[str, Any]) -> str | None:
    """Return the PostGrid event type string (e.g. 'letter.delivered')."""
    raw = payload.get("type")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def extract_postgrid_resource_id(payload: dict[str, Any]) -> str | None:
    """Return the resource (piece) id from the webhook payload.

    PostGrid's documented path: payload.data.object.id.
    """
    data = payload.get("data")
    if isinstance(data, dict):
        obj = data.get("object")
        if isinstance(obj, dict):
            rid = obj.get("id")
            if rid:
                return str(rid)
    # Fallback: top-level id as a last resort (non-standard replays)
    top_id = payload.get("resource_id")
    if top_id:
        return str(top_id)
    return None


def extract_postgrid_resource_address(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Return the piece's recipient address dict from the payload, if present."""
    data = payload.get("data")
    if isinstance(data, dict):
        obj = data.get("object")
        if isinstance(obj, dict):
            to = obj.get("to")
            if isinstance(to, dict):
                return to
    return None


# Maps PostGrid event type suffix → canonical hq-x piece.* event.
# PostGrid uses the same suffix set across resource types (letter.*, postcard.*, etc.).
# We strip the resource prefix and look up in this table.
_POSTGRID_EVENT_MAPPING: dict[str, str] = {
    # core lifecycle
    "created": "piece.created",
    "cancelled": "piece.canceled",
    "ready": "piece.rendered_pdf",       # PostGrid "ready" = printable PDF rendered
    "printing": "piece.rendered_pdf",    # collapse into rendered (no direct Lob analog)
    "mailed": "piece.mailed",
    "in_transit": "piece.in_transit",
    "in_local_area": "piece.in_local_area",
    "processed_for_delivery": "piece.processed_for_delivery",
    "delivered": "piece.delivered",
    "failed": "piece.failed",
    "re_routed": "piece.re_routed",
    "returned_to_sender": "piece.returned",
    "international_exit": "piece.international_exit",
}


def _strip_resource_prefix(event_type: str) -> str:
    """Drop the resource prefix from 'letter.delivered' → 'delivered'."""
    text = event_type.strip().lower().replace("-", "_")
    if "." in text:
        return text.split(".", 1)[1]
    return text


def normalize_postgrid_event_type(event_type: str | None) -> str:
    """Return the canonical internal event name. Unknown → 'piece.unknown'."""
    if not event_type:
        return "piece.unknown"
    suffix = _strip_resource_prefix(event_type)
    return _POSTGRID_EVENT_MAPPING.get(suffix, "piece.unknown")


def compute_postgrid_event_key(payload: dict[str, Any], raw_body: bytes) -> str:
    """Return a stable per-event key for dedup.

    PostGrid always sends `id` as the event id.
    """
    event_id = payload.get("id")
    if event_id:
        return f"postgrid:{event_id}"
    return f"postgrid:{hashlib.sha256(raw_body).hexdigest()}"

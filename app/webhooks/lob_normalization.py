"""Lob webhook event-type and piece-status normalization.

Aligned with the actual Lob webhook payload shape documented at
api-reference-docs-new/lob/api-reference/07-webhooks/. The relevant top-level
fields:
    id            — event id (evt_xxx)        — used as the dedup key
    event_type    — object {id, resource, …}  — id holds the event name
    reference_id  — resource id (psc_xxx etc) — top-level pointer to the piece
    date_created  — ISO-8601 timestamp
    body          — the full resource object  — has body.id and body.to

Earlier OEX-derived code looked at `payload.type` / `payload.event_type`
(treated as a string) / `payload.body.resource.id` — none of which Lob emits.
Those bugs would have dead-lettered every real webhook.
"""

from __future__ import annotations

import hashlib
from typing import Any


def extract_lob_event_name(payload: dict[str, Any]) -> str | None:
    """Return the Lob event name as a string (e.g. "postcard.delivered").

    Lob sends `event_type` as an object: {id, resource, object}. The id is
    the canonical name. We also accept a bare string (defensive — older
    webhooks or third-party replays sometimes flatten it).
    """
    raw = payload.get("event_type")
    if isinstance(raw, dict):
        value = raw.get("id")
        return str(value) if value else None
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    # Last-resort fallbacks (in case a non-Lob source is replaying).
    for key in ("type", "event"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _strip_resource(event_name: str) -> str:
    """Drop the resource prefix.

    "postcard.created"                          -> "created"
    "letter.certified.delivered"                -> "certified.delivered"
    "postcard.informed_delivery.email_sent"     -> "informed_delivery.email_sent"
    "self_mailer.returned_to_sender"            -> "returned_to_sender"
    """
    text = event_name.strip().lower().replace("-", "_")
    if "." in text:
        return text.split(".", 1)[1]
    return text


# Maps Lob's resource-stripped event suffix → our internal canonical event.
# Every value is prefixed `piece.*` so the event log is self-describing.
_EVENT_TYPE_MAPPING: dict[str, str] = {
    # core lifecycle
    "created": "piece.created",
    "rejected": "piece.rejected",
    "rendered_pdf": "piece.rendered_pdf",
    "rendered_thumbnails": "piece.rendered_thumbnails",
    "deleted": "piece.canceled",
    "mailed": "piece.mailed",
    "in_transit": "piece.in_transit",
    "in_local_area": "piece.in_local_area",
    "processed_for_delivery": "piece.processed_for_delivery",
    "delivered": "piece.delivered",
    "failed": "piece.failed",
    "re_routed": "piece.re_routed",
    "returned_to_sender": "piece.returned",
    "international_exit": "piece.international_exit",
    "viewed": "piece.viewed",
    # informed delivery (recipient-facing email engagement)
    "informed_delivery.email_sent": "piece.informed_delivery.email_sent",
    "informed_delivery.email_opened": "piece.informed_delivery.email_opened",
    "informed_delivery.email_clicked_through": "piece.informed_delivery.email_clicked_through",
    # certified-mail variants (letters only)
    "certified.mailed": "piece.certified.mailed",
    "certified.in_transit": "piece.certified.in_transit",
    "certified.in_local_area": "piece.certified.in_local_area",
    "certified.processed_for_delivery": "piece.certified.processed_for_delivery",
    "certified.re_routed": "piece.certified.re_routed",
    "certified.returned_to_sender": "piece.certified.returned",
    "certified.delivered": "piece.certified.delivered",
    "certified.pickup_available": "piece.certified.pickup_available",
    "certified.issue": "piece.certified.issue",
    # return-envelope events (letters with return envelopes)
    "return_envelope.created": "piece.return_envelope.created",
    "return_envelope.in_transit": "piece.return_envelope.in_transit",
    "return_envelope.in_local_area": "piece.return_envelope.in_local_area",
    "return_envelope.processed_for_delivery": "piece.return_envelope.processed_for_delivery",
    "return_envelope.re_routed": "piece.return_envelope.re_routed",
    "return_envelope.returned_to_sender": "piece.return_envelope.returned",
}


def normalize_lob_event_type(event_name: str | None) -> str:
    """Return the canonical internal event name. Unknown → 'piece.unknown'."""
    if not event_name:
        return "piece.unknown"
    return _EVENT_TYPE_MAPPING.get(_strip_resource(event_name), "piece.unknown")


# Maps internal canonical event → new piece.status value, or None if this
# event should NOT update the piece's status (e.g. engagement events).
_PIECE_STATUS_MAPPING: dict[str, str] = {
    "piece.created": "queued",
    "piece.rejected": "rejected",
    "piece.rendered_pdf": "rendered",
    "piece.rendered_thumbnails": "rendered",
    "piece.canceled": "canceled",
    "piece.mailed": "in_transit",
    "piece.in_transit": "in_transit",
    "piece.in_local_area": "in_transit",
    "piece.processed_for_delivery": "in_transit",
    "piece.delivered": "delivered",
    "piece.failed": "failed",
    "piece.re_routed": "in_transit",
    "piece.returned": "returned",
    "piece.international_exit": "in_transit",
    # certified-mail tracking — same buckets as the regular delivery path
    "piece.certified.mailed": "in_transit",
    "piece.certified.in_transit": "in_transit",
    "piece.certified.in_local_area": "in_transit",
    "piece.certified.processed_for_delivery": "in_transit",
    "piece.certified.re_routed": "in_transit",
    "piece.certified.returned": "returned",
    "piece.certified.delivered": "delivered",
    "piece.certified.pickup_available": "pickup_available",
    "piece.certified.issue": "issue",
    # `piece.viewed`, `piece.informed_delivery.*`, and `piece.return_envelope.*`
    # are intentionally absent — they don't change the piece's delivery state.
}

# Internal events that should populate `suppressed_addresses` on receipt.
SUPPRESSION_TRIGGERS: dict[str, str] = {
    "piece.returned": "returned_to_sender",
    "piece.failed": "failed",
    "piece.certified.returned": "returned_to_sender",
}


def normalize_lob_piece_status(normalized_event_type: str) -> str | None:
    """Return the new piece status, or None if the event doesn't change status."""
    return _PIECE_STATUS_MAPPING.get(normalized_event_type)


def compute_lob_event_key(payload: dict[str, Any], raw_body: bytes) -> str:
    """Return a stable per-event key for dedup.

    Lob always sends `id` (evt_xxx). The hash fallback is purely defensive.
    """
    for key in ("id", "event_id"):
        value = payload.get(key)
        if value:
            return f"lob:{value}"
    return f"lob:{hashlib.sha256(raw_body).hexdigest()}"


def extract_lob_piece_id(payload: dict[str, Any]) -> str | None:
    """Return the resource (piece) id.

    Lob's documented field is `reference_id` at the top level. `body.id`
    is the in-resource fallback that should always agree.
    """
    ref = payload.get("reference_id")
    if ref:
        return str(ref)
    body = payload.get("body")
    if isinstance(body, dict):
        bid = body.get("id")
        if bid:
            return str(bid)
    return None


def extract_lob_piece_address(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Return the piece's recipient address dict, if present.

    Lob's webhook body is the full resource object — `body.to` is the
    address (also a full object with id, name, address_line1, etc.).
    """
    body = payload.get("body")
    if isinstance(body, dict):
        to = body.get("to")
        if isinstance(to, dict):
            return to
    return None

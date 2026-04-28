"""Lob webhook event-type and piece-status normalization.

Direct port from outbound-engine-x. Same mapping → same canonical event /
status strings, so historical data lines up if anyone ever migrates rows.
"""

from __future__ import annotations

import hashlib
from typing import Any


def normalize_lob_event_type(value: str | None) -> str:
    if not value:
        return "piece.unknown"
    key = str(value).strip().lower().replace("-", "_")
    if "." in key:
        key = key.split(".")[-1]
    mapping = {
        "created": "piece.created",
        "updated": "piece.updated",
        "processed": "piece.processed",
        "in_transit": "piece.in_transit",
        "in_transit_local": "piece.in_transit",
        "delivered": "piece.delivered",
        "returned": "piece.returned",
        "returned_to_sender": "piece.returned",
        "canceled": "piece.canceled",
        "cancelled": "piece.canceled",
        "re_routed": "piece.re-routed",
        "rerouted": "piece.re-routed",
        "failed": "piece.failed",
    }
    return mapping.get(key, "piece.unknown")


def normalize_lob_piece_status(normalized_event_type: str) -> str:
    mapping = {
        "piece.created": "queued",
        "piece.updated": "processing",
        "piece.processed": "ready_for_mail",
        "piece.in_transit": "in_transit",
        "piece.delivered": "delivered",
        "piece.returned": "returned",
        "piece.canceled": "canceled",
        "piece.re-routed": "in_transit",
        "piece.failed": "failed",
        "piece.unknown": "unknown",
    }
    return mapping.get(normalized_event_type, "unknown")


def compute_lob_event_key(payload: dict[str, Any], raw_body: bytes) -> str:
    for key in ("id", "event_id"):
        if payload.get(key):
            return f"lob:{payload[key]}"

    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    resource = body.get("resource") if isinstance(body.get("resource"), dict) else {}
    resource_id = (
        resource.get("id")
        or payload.get("resource_id")
        or payload.get("object_id")
        or payload.get("piece_id")
        or payload.get("mailpiece_id")
    )
    event_type = payload.get("type") or payload.get("event_type") or payload.get("event")
    timestamp = payload.get("date_created") or payload.get("created_at") or payload.get("time")
    if resource_id and event_type and timestamp:
        return f"lob:{resource_id}:{event_type}:{timestamp}"
    return f"lob:{hashlib.sha256(raw_body).hexdigest()}"


def extract_lob_piece_id(payload: dict[str, Any]) -> str | None:
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    resource = body.get("resource") if isinstance(body.get("resource"), dict) else {}
    return (
        resource.get("id")
        or payload.get("resource_id")
        or payload.get("object_id")
        or payload.get("piece_id")
        or payload.get("mailpiece_id")
    )


def extract_lob_piece_address(payload: dict[str, Any]) -> dict[str, Any] | None:
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    resource = body.get("resource") if isinstance(body.get("resource"), dict) else {}
    to = resource.get("to") if isinstance(resource.get("to"), dict) else None
    if to:
        return to
    direct = payload.get("to")
    return direct if isinstance(direct, dict) else None

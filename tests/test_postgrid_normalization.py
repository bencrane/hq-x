"""PostGrid event normalization tests.

Verifies that PostGrid event types map correctly to the canonical piece.* taxonomy
without modifying lob_normalization.py.
"""

from __future__ import annotations

import pytest

from app.webhooks.postgrid_normalization import (
    compute_postgrid_event_key,
    extract_postgrid_event_type,
    extract_postgrid_resource_id,
    normalize_postgrid_event_type,
)


def _pg_payload(event_type: str, resource_id: str) -> dict:
    return {
        "id": f"event_{event_type.replace('.', '_')}",
        "type": event_type,
        "data": {"object": {"id": resource_id, "object": event_type.split(".")[0]}},
        "created_at": "2026-05-02T12:00:00Z",
    }


# ---------------------------------------------------------------------------
# extract_postgrid_event_type
# ---------------------------------------------------------------------------


def test_extract_event_type_from_type_field():
    payload = _pg_payload("letter.delivered", "letter_abc")
    assert extract_postgrid_event_type(payload) == "letter.delivered"


def test_extract_event_type_missing_returns_none():
    assert extract_postgrid_event_type({}) is None


# ---------------------------------------------------------------------------
# extract_postgrid_resource_id
# ---------------------------------------------------------------------------


def test_extract_resource_id_from_data_object():
    payload = _pg_payload("letter.delivered", "letter_abc123")
    assert extract_postgrid_resource_id(payload) == "letter_abc123"


def test_extract_resource_id_missing_returns_none():
    assert extract_postgrid_resource_id({}) is None


def test_extract_resource_id_from_resource_id_fallback():
    payload = {"id": "evt_x", "resource_id": "postcard_xyz"}
    assert extract_postgrid_resource_id(payload) == "postcard_xyz"


# ---------------------------------------------------------------------------
# normalize_postgrid_event_type — canonical mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pg_event,expected_canonical",
    [
        ("letter.created", "piece.created"),
        ("letter.cancelled", "piece.canceled"),
        ("letter.in_transit", "piece.in_transit"),
        ("letter.in_local_area", "piece.in_local_area"),
        ("letter.processed_for_delivery", "piece.processed_for_delivery"),
        ("letter.delivered", "piece.delivered"),
        ("letter.returned_to_sender", "piece.returned"),
        ("letter.failed", "piece.failed"),
        ("letter.ready", "piece.rendered_pdf"),
        ("letter.printing", "piece.rendered_pdf"),
        ("letter.mailed", "piece.mailed"),
        ("letter.re_routed", "piece.re_routed"),
        ("postcard.delivered", "piece.delivered"),
        ("postcard.in_transit", "piece.in_transit"),
        ("selfmailer.delivered", "piece.delivered"),
        ("letter.unknown_event_xyz", "piece.unknown"),
    ],
)
def test_normalize_event_type(pg_event, expected_canonical):
    assert normalize_postgrid_event_type(pg_event) == expected_canonical


def test_normalize_none_returns_unknown():
    assert normalize_postgrid_event_type(None) == "piece.unknown"


# ---------------------------------------------------------------------------
# compute_postgrid_event_key
# ---------------------------------------------------------------------------


def test_event_key_uses_id_field():
    payload = {"id": "event_abc123", "type": "letter.delivered"}
    key = compute_postgrid_event_key(payload, b"{}")
    assert key == "postgrid:event_abc123"


def test_event_key_falls_back_to_body_hash():
    payload = {"type": "letter.delivered"}
    key = compute_postgrid_event_key(payload, b'{"type":"letter.delivered"}')
    assert key.startswith("postgrid:")
    assert len(key) > 10

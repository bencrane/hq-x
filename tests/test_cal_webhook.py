import hashlib
import hmac
import json
from uuid import uuid4

import httpx
import pytest

from app.config import settings
from app.main import app
from app.webhooks import storage


@pytest.fixture
def captured_inserts(monkeypatch):
    captured: list[dict] = []

    async def fake_insert(*, fields, payload):
        new_id = uuid4()
        captured.append({"id": new_id, "fields": fields, "payload": payload})
        return new_id

    monkeypatch.setattr(storage, "insert_cal_raw_event", fake_insert)
    return captured


async def _post(body: bytes, headers: dict[str, str] | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/webhooks/cal", content=body, headers=headers or {})


def _sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def test_valid_signature_returns_200_and_stores(captured_inserts, monkeypatch):
    monkeypatch.setattr(settings, "CAL_WEBHOOK_SECRET", "topsecret")
    body = json.dumps(
        {
            "triggerEvent": "BOOKING_CREATED",
            "payload": {
                "uid": "evt_123",
                "eventTypeId": 42,
                "hosts": [{"email": "host@example.com"}],
                "attendees": [{"email": "guest@example.com"}],
                "guests": ["walkin@example.com"],
            },
        }
    ).encode()
    resp = await _post(
        body,
        {
            "Content-Type": "application/json",
            "X-Cal-Signature-256": _sign(body, "topsecret"),
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["trigger_event"] == "BOOKING_CREATED"
    assert data["event_id"]

    assert len(captured_inserts) == 1
    fields = captured_inserts[0]["fields"]
    assert fields["cal_event_uid"] == "evt_123"
    assert fields["organizer_email"] == "host@example.com"
    assert fields["attendee_emails"] == ["guest@example.com", "walkin@example.com"]
    assert fields["event_type_id"] == 42


async def test_invalid_signature_returns_401(captured_inserts, monkeypatch):
    monkeypatch.setattr(settings, "CAL_WEBHOOK_SECRET", "topsecret")
    body = b'{"triggerEvent":"BOOKING_CREATED","payload":{}}'
    resp = await _post(
        body,
        {"Content-Type": "application/json", "X-Cal-Signature-256": "deadbeef"},
    )
    assert resp.status_code == 401
    assert captured_inserts == []


async def test_missing_signature_with_secret_returns_401(captured_inserts, monkeypatch):
    monkeypatch.setattr(settings, "CAL_WEBHOOK_SECRET", "topsecret")
    body = b'{"triggerEvent":"BOOKING_CREATED","payload":{}}'
    resp = await _post(body, {"Content-Type": "application/json"})
    assert resp.status_code == 401
    assert captured_inserts == []


async def test_empty_secret_skips_verification(captured_inserts, monkeypatch):
    monkeypatch.setattr(settings, "CAL_WEBHOOK_SECRET", "")
    body = b'{"triggerEvent":"BOOKING_CREATED","payload":{"uid":"x"}}'
    resp = await _post(body, {"Content-Type": "application/json"})
    assert resp.status_code == 200
    assert len(captured_inserts) == 1


async def test_malformed_json_returns_400(captured_inserts, monkeypatch):
    monkeypatch.setattr(settings, "CAL_WEBHOOK_SECRET", "")
    resp = await _post(b"not-json{", {"Content-Type": "application/json"})
    assert resp.status_code == 400
    assert captured_inserts == []


async def test_flat_payload_meeting_ended(captured_inserts, monkeypatch):
    monkeypatch.setattr(settings, "CAL_WEBHOOK_SECRET", "")
    body = json.dumps(
        {"triggerEvent": "MEETING_ENDED", "bookingId": 99, "roomName": "abc"}
    ).encode()
    resp = await _post(body, {"Content-Type": "application/json"})
    assert resp.status_code == 200
    fields = captured_inserts[0]["fields"]
    assert fields["trigger_event"] == "MEETING_ENDED"
    assert fields["cal_event_uid"] is None
    assert fields["attendee_emails"] == []
    assert fields["event_type_id"] is None

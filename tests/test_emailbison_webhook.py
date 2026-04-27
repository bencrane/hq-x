import hashlib
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
    seen_keys: set[str] = set()

    async def fake_insert(*, event_key, event_type, payload):
        if event_key in seen_keys:
            raise storage.DuplicateEventError(event_key)
        seen_keys.add(event_key)
        new_id = uuid4()
        captured.append(
            {"id": new_id, "event_key": event_key, "event_type": event_type, "payload": payload}
        )
        return new_id

    monkeypatch.setattr(storage, "insert_emailbison_event", fake_insert)
    return captured


async def _post(path: str, body: bytes, headers: dict[str, str] | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, content=body, headers=headers or {})


VALID_HEADERS = {"Content-Type": "application/json", "Origin": "https://app.emailbison.com"}


async def test_valid_token_and_origin_returns_202(captured_inserts):
    body = json.dumps({"event": "sent", "event_id": "abc"}).encode()
    resp = await _post("/webhooks/emailbison/test-path-token", body, VALID_HEADERS)
    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "accepted"
    assert data["event_type"] == "sent"
    assert data["event_key"] == "abc"
    assert data["trust_mode"] == "unsigned_origin_plus_path_token"
    assert data["non_cryptographic_trust"] is True

    assert len(captured_inserts) == 1
    stored = captured_inserts[0]
    assert stored["event_type"] == "sent"
    assert stored["event_key"] == "abc"
    assert stored["payload"]["_ingestion"]["origin_host"] == "app.emailbison.com"
    assert stored["payload"]["_ingestion"]["provider_slug"] == "emailbison"


async def test_missing_path_token_returns_401(captured_inserts):
    resp = await _post("/webhooks/emailbison", b'{"event":"x"}', VALID_HEADERS)
    assert resp.status_code == 401
    assert resp.json()["detail"]["reason"] == "missing_path_token"
    assert captured_inserts == []


async def test_invalid_path_token_returns_401(captured_inserts):
    resp = await _post("/webhooks/emailbison/wrong", b'{"event":"x"}', VALID_HEADERS)
    assert resp.status_code == 401
    assert resp.json()["detail"]["reason"] == "invalid_path_token"
    assert captured_inserts == []


async def test_disallowed_origin_returns_401(captured_inserts):
    headers = {"Content-Type": "application/json", "Origin": "https://attacker.example.com"}
    resp = await _post("/webhooks/emailbison/test-path-token", b'{"event":"x"}', headers)
    assert resp.status_code == 401
    assert resp.json()["detail"]["reason"] == "disallowed_origin"
    assert captured_inserts == []


async def test_token_not_configured_returns_503(captured_inserts, monkeypatch):
    monkeypatch.setattr(settings, "EMAILBISON_WEBHOOK_PATH_TOKEN", None)
    resp = await _post("/webhooks/emailbison/anything", b'{"event":"x"}', VALID_HEADERS)
    assert resp.status_code == 503
    assert resp.json()["detail"]["reason"] == "path_token_not_configured"
    assert captured_inserts == []


async def test_duplicate_event_key_returns_200(captured_inserts):
    body = json.dumps({"event": "sent", "event_id": "dup-1"}).encode()
    r1 = await _post("/webhooks/emailbison/test-path-token", body, VALID_HEADERS)
    assert r1.status_code == 202
    r2 = await _post("/webhooks/emailbison/test-path-token", body, VALID_HEADERS)
    assert r2.status_code == 200
    assert r2.json()["status"] == "duplicate_ignored"
    assert r2.json()["event_key"] == "dup-1"
    assert len(captured_inserts) == 1


async def test_malformed_json_still_accepted(captured_inserts):
    resp = await _post(
        "/webhooks/emailbison/test-path-token", b"not json{", VALID_HEADERS
    )
    assert resp.status_code == 202
    stored = captured_inserts[0]
    assert stored["payload"]["malformed_json"] is True
    assert stored["payload"]["raw_body"] == "not json{"


async def test_event_key_falls_back_to_sha256(captured_inserts):
    body = json.dumps({"event": "open"}).encode()
    expected = hashlib.sha256(body).hexdigest()
    resp = await _post("/webhooks/emailbison/test-path-token", body, VALID_HEADERS)
    assert resp.status_code == 202
    assert resp.json()["event_key"] == expected
    assert captured_inserts[0]["event_key"] == expected

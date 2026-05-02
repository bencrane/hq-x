"""Entri webhook receiver tests.

Stubs the webhook_events store and the projector so the suite has no DB
dependency. Covers V2 signature verification (enforce vs permissive),
timestamp tolerance, payload schema validation, idempotency on event id,
and dead-letter on projection failure.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any
from uuid import uuid4

import httpx
import pytest

from app.config import settings
from app.main import app
from app.routers.webhooks import entri as entri_webhook


@pytest.fixture
def fake_storage(monkeypatch):
    state: dict[str, Any] = {
        "events": {},
        "by_key": {},
        "projection": {"status": "projected", "new_state": "live"},
        "raise_projection": None,
    }

    async def fake_store(
        *, event_key, event_type, schema_version, request_id, payload
    ):
        if event_key in state["by_key"]:
            return state["by_key"][event_key], False
        new_id = uuid4()
        state["events"][new_id] = {
            "event_key": event_key,
            "event_type": event_type,
            "status": "received",
        }
        state["by_key"][event_key] = new_id
        return new_id, True

    async def fake_mark(*, event_db_id, status_value, reason_code=None, error=None):
        state["events"][event_db_id]["status"] = status_value

    async def fake_project(*, payload, event_id, event_type, webhook_event_id):
        state.setdefault("projection_calls", []).append(
            {"event_id": event_id, "event_type": event_type}
        )
        if state["raise_projection"]:
            raise state["raise_projection"]
        return state["projection"]

    monkeypatch.setattr(entri_webhook, "_store_webhook_event", fake_store)
    monkeypatch.setattr(entri_webhook, "_mark_webhook_event", fake_mark)
    monkeypatch.setattr(entri_webhook, "project_entri_event", fake_project)
    return state


async def _client():
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _payload(event_id: str = "evt_001", event_type: str = "domain.added") -> dict[str, Any]:
    return {
        "id": event_id,
        "type": event_type,
        "user_id": "org_1:step_1",
        "domain": "qr.acme.com",
        "provider": "godaddy",
        "setup_type": "automatic",
        "propagation_status": "success",
        "power_status": "success",
        "secure_status": "success",
    }


def _sign_v2(*, webhook_id: str, timestamp: str, secret: str) -> str:
    return hashlib.sha256(
        webhook_id.encode() + timestamp.encode() + secret.encode()
    ).hexdigest()


def _headers(payload: dict[str, Any], *, secret: str = "entri_test_secret") -> dict[str, str]:
    ts = str(int(time.time()))
    sig = _sign_v2(webhook_id=str(payload["id"]), timestamp=ts, secret=secret)
    return {
        "Content-Type": "application/json",
        "Entri-Timestamp": ts,
        "Entri-Signature-V2": sig,
    }


@pytest.fixture(autouse=True)
def _set_secret(monkeypatch):
    """All tests get a known webhook secret."""
    from pydantic import SecretStr

    monkeypatch.setattr(
        settings, "ENTRI_WEBHOOK_SECRET", SecretStr("entri_test_secret")
    )


@pytest.mark.asyncio
async def test_domain_added_processed(fake_storage, monkeypatch):
    monkeypatch.setattr(settings, "ENTRI_WEBHOOK_SIGNATURE_MODE", "enforce")
    payload = _payload()
    body = json.dumps(payload).encode()
    async with await _client() as c:
        r = await c.post("/webhooks/entri", content=body, headers=_headers(payload))
    assert r.status_code == 202, r.text
    j = r.json()
    assert j["status"] == "processed"
    assert j["event_type"] == "domain.added"
    assert j["signature"]["signature_verified"] is True


@pytest.mark.asyncio
async def test_duplicate_event_ignored(fake_storage, monkeypatch):
    monkeypatch.setattr(settings, "ENTRI_WEBHOOK_SIGNATURE_MODE", "enforce")
    payload = _payload(event_id="evt_dup")
    body = json.dumps(payload).encode()
    async with await _client() as c:
        r1 = await c.post("/webhooks/entri", content=body, headers=_headers(payload))
        r2 = await c.post("/webhooks/entri", content=body, headers=_headers(payload))
    assert r1.status_code == 202
    assert r2.status_code == 200
    assert r2.json()["status"] == "duplicate_ignored"
    # Projector ran exactly once.
    assert len(fake_storage["projection_calls"]) == 1


@pytest.mark.asyncio
async def test_enforce_rejects_missing_signature(fake_storage, monkeypatch):
    monkeypatch.setattr(settings, "ENTRI_WEBHOOK_SIGNATURE_MODE", "enforce")
    body = json.dumps(_payload()).encode()
    async with await _client() as c:
        r = await c.post(
            "/webhooks/entri",
            content=body,
            headers={"Content-Type": "application/json"},
        )
    assert r.status_code == 401
    assert r.json()["detail"]["reason"] == "missing_signature"


@pytest.mark.asyncio
async def test_enforce_rejects_invalid_signature(fake_storage, monkeypatch):
    monkeypatch.setattr(settings, "ENTRI_WEBHOOK_SIGNATURE_MODE", "enforce")
    payload = _payload()
    body = json.dumps(payload).encode()
    headers = _headers(payload)
    headers["Entri-Signature-V2"] = "0" * 64
    async with await _client() as c:
        r = await c.post("/webhooks/entri", content=body, headers=headers)
    assert r.status_code == 401
    assert r.json()["detail"]["reason"] == "invalid_signature"


@pytest.mark.asyncio
async def test_enforce_rejects_stale_timestamp(fake_storage, monkeypatch):
    monkeypatch.setattr(settings, "ENTRI_WEBHOOK_SIGNATURE_MODE", "enforce")
    monkeypatch.setattr(settings, "ENTRI_WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS", 60)
    payload = _payload()
    body = json.dumps(payload).encode()
    stale_ts = str(int(time.time()) - 3600)
    sig = _sign_v2(
        webhook_id=str(payload["id"]),
        timestamp=stale_ts,
        secret="entri_test_secret",
    )
    headers = {
        "Content-Type": "application/json",
        "Entri-Timestamp": stale_ts,
        "Entri-Signature-V2": sig,
    }
    async with await _client() as c:
        r = await c.post("/webhooks/entri", content=body, headers=headers)
    assert r.status_code == 401
    assert r.json()["detail"]["reason"] == "timestamp_outside_tolerance"


@pytest.mark.asyncio
async def test_permissive_audit_accepts_unsigned(fake_storage, monkeypatch):
    monkeypatch.setattr(settings, "ENTRI_WEBHOOK_SIGNATURE_MODE", "permissive_audit")
    body = json.dumps(_payload()).encode()
    async with await _client() as c:
        r = await c.post(
            "/webhooks/entri",
            content=body,
            headers={"Content-Type": "application/json"},
        )
    assert r.status_code == 202
    assert r.json()["signature"]["signature_verified"] is False


@pytest.mark.asyncio
async def test_schema_invalid_missing_id(fake_storage, monkeypatch):
    monkeypatch.setattr(settings, "ENTRI_WEBHOOK_SIGNATURE_MODE", "permissive_audit")
    body = json.dumps({"type": "domain.added"}).encode()
    async with await _client() as c:
        r = await c.post(
            "/webhooks/entri",
            content=body,
            headers={"Content-Type": "application/json"},
        )
    assert r.status_code == 400
    assert "id" in r.json()["detail"]["reason"]


@pytest.mark.asyncio
async def test_projection_failure_dead_letters(fake_storage, monkeypatch):
    monkeypatch.setattr(settings, "ENTRI_WEBHOOK_SIGNATURE_MODE", "enforce")
    fake_storage["raise_projection"] = RuntimeError("db down")
    payload = _payload()
    body = json.dumps(payload).encode()
    async with await _client() as c:
        r = await c.post("/webhooks/entri", content=body, headers=_headers(payload))
    assert r.status_code == 202
    assert r.json()["status"] == "dead_letter"

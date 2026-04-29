"""Dub webhook receiver tests.

Stubs the webhook_events store and the dmaas_dub_events projector so the
suite has no DB dependency. Covers signature verification (enforce vs
permissive), payload schema validation, idempotency on event id, and
dead-letter on projection failure.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any
from uuid import uuid4

import httpx
import pytest

from app.config import settings
from app.main import app
from app.routers.webhooks import dub as dub_webhook


@pytest.fixture
def fake_storage(monkeypatch):
    state: dict[str, Any] = {
        "events": {},  # event_db_id → record
        "by_key": {},  # event_key → event_db_id
        "projection": {"status": "processed", "dub_link_id": "link_abc"},
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
            "schema_version": schema_version,
            "request_id": request_id,
            "payload": payload,
            "status": "received",
            "reason_code": None,
            "error": None,
        }
        state["by_key"][event_key] = new_id
        return new_id, True

    async def fake_mark(*, event_db_id, status_value, reason_code=None, error=None):
        rec = state["events"][event_db_id]
        rec["status"] = status_value
        if reason_code is not None:
            rec["reason_code"] = reason_code
        if error is not None:
            rec["error"] = error

    async def fake_project(
        *, payload, event_id, event_type, occurred_at, webhook_event_id
    ):
        state.setdefault("projection_calls", []).append(
            {
                "event_id": event_id,
                "event_type": event_type,
                "occurred_at": occurred_at,
                "webhook_event_id": webhook_event_id,
            }
        )
        if state["raise_projection"]:
            raise state["raise_projection"]
        return state["projection"]

    monkeypatch.setattr(dub_webhook, "_store_webhook_event", fake_store)
    monkeypatch.setattr(dub_webhook, "_mark_webhook_event", fake_mark)
    monkeypatch.setattr(dub_webhook, "project_dub_event", fake_project)
    return state


async def _client():
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _payload(event_id: str = "evt_001", event: str = "link.clicked") -> dict[str, Any]:
    return {
        "id": event_id,
        "event": event,
        "createdAt": "2026-04-29T12:00:00Z",
        "data": {
            "link": {"id": "link_abc"},
            "country": "US",
            "city": "San Francisco",
            "device": "Desktop",
            "browser": "Chrome",
            "os": "macOS",
            "referer": "https://example.com",
        },
    }


def _sign(body: bytes, secret: str = "dub_test_webhook_secret") -> dict[str, str]:
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"Dub-Signature": sig, "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_link_clicked_processed(fake_storage, monkeypatch):
    monkeypatch.setattr(settings, "DUB_WEBHOOK_SIGNATURE_MODE", "enforce")
    body = json.dumps(_payload()).encode()
    async with await _client() as c:
        r = await c.post("/webhooks/dub", content=body, headers=_sign(body))
    assert r.status_code == 202, r.text
    j = r.json()
    assert j["status"] == "processed"
    assert j["event_type"] == "link.clicked"
    assert j["signature"]["signature_verified"] is True
    # Stored once, projector called once.
    assert len(fake_storage["events"]) == 1
    assert len(fake_storage["projection_calls"]) == 1


@pytest.mark.asyncio
async def test_lead_and_sale_event_types(fake_storage, monkeypatch):
    monkeypatch.setattr(settings, "DUB_WEBHOOK_SIGNATURE_MODE", "enforce")
    for ev_id, ev_type in (("evt_lead", "lead.created"), ("evt_sale", "sale.created")):
        body = json.dumps(_payload(event_id=ev_id, event=ev_type)).encode()
        async with await _client() as c:
            r = await c.post("/webhooks/dub", content=body, headers=_sign(body))
        assert r.status_code == 202, r.text
        assert r.json()["event_type"] == ev_type


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enforce_rejects_missing_signature(fake_storage, monkeypatch):
    monkeypatch.setattr(settings, "DUB_WEBHOOK_SIGNATURE_MODE", "enforce")
    body = json.dumps(_payload()).encode()
    async with await _client() as c:
        r = await c.post(
            "/webhooks/dub", content=body, headers={"Content-Type": "application/json"}
        )
    assert r.status_code == 401
    assert r.json()["detail"]["reason"] == "missing_signature"
    assert fake_storage["events"] == {}


@pytest.mark.asyncio
async def test_enforce_rejects_invalid_signature(fake_storage, monkeypatch):
    monkeypatch.setattr(settings, "DUB_WEBHOOK_SIGNATURE_MODE", "enforce")
    body = json.dumps(_payload()).encode()
    headers = {"Dub-Signature": "deadbeef" * 8, "Content-Type": "application/json"}
    async with await _client() as c:
        r = await c.post("/webhooks/dub", content=body, headers=headers)
    assert r.status_code == 401
    assert r.json()["detail"]["reason"] == "invalid_signature"
    assert fake_storage["events"] == {}


@pytest.mark.asyncio
async def test_permissive_audit_accepts_invalid_signature(fake_storage, monkeypatch):
    monkeypatch.setattr(settings, "DUB_WEBHOOK_SIGNATURE_MODE", "permissive_audit")
    body = json.dumps(_payload()).encode()
    headers = {"Dub-Signature": "deadbeef" * 8, "Content-Type": "application/json"}
    async with await _client() as c:
        r = await c.post("/webhooks/dub", content=body, headers=headers)
    assert r.status_code == 202
    j = r.json()
    assert j["status"] == "processed"
    assert j["signature"]["signature_verified"] is False
    assert j["signature"]["signature_reason"] == "invalid_signature"


@pytest.mark.asyncio
async def test_disabled_mode_skips_verification(fake_storage, monkeypatch):
    monkeypatch.setattr(settings, "DUB_WEBHOOK_SIGNATURE_MODE", "disabled")
    body = json.dumps(_payload()).encode()
    async with await _client() as c:
        r = await c.post(
            "/webhooks/dub", content=body, headers={"Content-Type": "application/json"}
        )
    assert r.status_code == 202
    assert r.json()["signature"]["signature_reason"] == "disabled"


# ---------------------------------------------------------------------------
# Payload validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_body_400(fake_storage, monkeypatch):
    monkeypatch.setattr(settings, "DUB_WEBHOOK_SIGNATURE_MODE", "permissive_audit")
    body = b"{not json"
    async with await _client() as c:
        r = await c.post(
            "/webhooks/dub", content=body, headers={"Content-Type": "application/json"}
        )
    assert r.status_code == 400
    assert r.json()["detail"]["reason"] == "malformed_body"


@pytest.mark.asyncio
async def test_missing_required_fields_400(fake_storage, monkeypatch):
    monkeypatch.setattr(settings, "DUB_WEBHOOK_SIGNATURE_MODE", "permissive_audit")
    body = json.dumps({"event": "link.clicked"}).encode()  # missing id/createdAt/data
    async with await _client() as c:
        r = await c.post(
            "/webhooks/dub", content=body, headers={"Content-Type": "application/json"}
        )
    assert r.status_code == 400
    assert r.json()["detail"]["reason"].startswith("schema_invalid:")


@pytest.mark.asyncio
async def test_unknown_event_type_400(fake_storage, monkeypatch):
    monkeypatch.setattr(settings, "DUB_WEBHOOK_SIGNATURE_MODE", "permissive_audit")
    body = json.dumps(_payload(event="link.deleted")).encode()
    async with await _client() as c:
        r = await c.post(
            "/webhooks/dub", content=body, headers={"Content-Type": "application/json"}
        )
    assert r.status_code == 400
    assert "event_type_unknown" in r.json()["detail"]["reason"]


# ---------------------------------------------------------------------------
# Idempotency + dead-letter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_event_id_returns_200(fake_storage, monkeypatch):
    monkeypatch.setattr(settings, "DUB_WEBHOOK_SIGNATURE_MODE", "enforce")
    body = json.dumps(_payload()).encode()
    async with await _client() as c:
        r1 = await c.post("/webhooks/dub", content=body, headers=_sign(body))
        r2 = await c.post("/webhooks/dub", content=body, headers=_sign(body))
    assert r1.status_code == 202
    assert r1.json()["status"] == "processed"
    assert r2.status_code == 200
    assert r2.json()["status"] == "duplicate_ignored"
    # Projector ran exactly once.
    assert len(fake_storage["projection_calls"]) == 1


@pytest.mark.asyncio
async def test_projection_failure_marks_dead_letter(fake_storage, monkeypatch):
    monkeypatch.setattr(settings, "DUB_WEBHOOK_SIGNATURE_MODE", "enforce")
    fake_storage["raise_projection"] = RuntimeError("DB exploded")
    body = json.dumps(_payload()).encode()
    async with await _client() as c:
        r = await c.post("/webhooks/dub", content=body, headers=_sign(body))
    assert r.status_code == 202
    j = r.json()
    assert j["status"] == "dead_letter"
    assert j["reason"] == "projection_failed"
    # webhook_events row was marked.
    rec = next(iter(fake_storage["events"].values()))
    assert rec["status"] == "dead_letter"
    assert rec["reason_code"] == "projection_failed"
    assert "DB exploded" in (rec["error"] or "")


# ---------------------------------------------------------------------------
# Projector unit tests (idempotency on dub_event_id)
# ---------------------------------------------------------------------------


def test_extract_link_id_from_nested_link():
    from app.webhooks.dub_processor import _extract_link_id

    assert _extract_link_id({"link": {"id": "link_abc"}}) == "link_abc"
    assert _extract_link_id({"linkId": "link_abc"}) == "link_abc"
    assert _extract_link_id({}) is None


def test_extract_click_fields_flat_or_nested():
    from app.webhooks.dub_processor import _extract_click_fields

    flat = _extract_click_fields({"country": "US", "device": "Mobile"})
    assert flat["click_country"] == "US"
    assert flat["click_device"] == "Mobile"

    nested = _extract_click_fields(
        {"click": {"country": "DE", "device": "Tablet", "referrer": "https://x"}}
    )
    assert nested["click_country"] == "DE"
    assert nested["click_referer"] == "https://x"


def test_extract_sale_fields_handles_missing():
    from app.webhooks.dub_processor import _extract_sale_fields

    assert _extract_sale_fields({}) == {"sale_amount_cents": None, "sale_currency": None}
    out = _extract_sale_fields({"sale": {"amount": 4999, "currency": "USD"}})
    assert out == {"sale_amount_cents": 4999, "sale_currency": "USD"}

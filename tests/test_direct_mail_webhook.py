"""Lob webhook receiver tests."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from uuid import uuid4

import httpx
import pytest

from app.config import settings
from app.main import app
from app.routers.webhooks import lob as lob_webhook
from app.webhooks import lob_normalization, lob_signature


@pytest.fixture
def fake_storage(monkeypatch):
    state = {
        "events": {},  # event_db_id → record
        "by_key": {},  # event_key → event_db_id
        "projection": {
            "status": "applied",
            "piece_id": str(uuid4()),
            "previous_status": "queued",
            "new_status": "delivered",
            "normalized_event": "piece.delivered",
            "suppression_inserted": False,
        },
        "raise_projection": None,
    }

    async def fake_store(
        *, event_key, event_type, schema_version, request_id, payload, initial_status="received"
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
            "status": initial_status,
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

    async def fake_project(*, payload, event_id):
        if state["raise_projection"]:
            raise state["raise_projection"]
        return state["projection"]

    monkeypatch.setattr(lob_webhook, "_store_webhook_event", fake_store)
    monkeypatch.setattr(lob_webhook, "_mark_webhook_event", fake_mark)
    monkeypatch.setattr(lob_webhook, "project_lob_event", fake_project)
    return state


def _signed_headers(body: bytes, *, secret: str | None = None, ts: str | None = None) -> dict:
    secret = secret or settings.LOB_WEBHOOK_SECRET or "test_lob_webhook_secret"
    ts = ts or str(int(time.time()))
    sig = hmac.new(secret.encode(), f"{ts}.{body.decode()}".encode(), hashlib.sha256).hexdigest()
    return {"Content-Type": "application/json", "Lob-Signature": sig, "Lob-Signature-Timestamp": ts}


def _payload(
    event_id: str = "evt_x", event_type: str = "postcard.delivered", piece_id: str = "psc_1"
) -> dict:
    return {
        "id": event_id,
        "type": event_type,
        "date_created": "2026-01-01T00:00:00Z",
        "body": {"resource": {"id": piece_id}},
    }


async def _post(path: str, body: bytes, headers: dict[str, str]) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, content=body, headers=headers)


# ---- normalization ----


def test_normalize_event_type_buckets():
    assert lob_normalization.normalize_lob_event_type("postcard.delivered") == "piece.delivered"
    assert lob_normalization.normalize_lob_event_type("letter.in_transit") == "piece.in_transit"
    assert lob_normalization.normalize_lob_event_type("returned_to_sender") == "piece.returned"
    assert lob_normalization.normalize_lob_event_type("Failed") == "piece.failed"
    assert lob_normalization.normalize_lob_event_type(None) == "piece.unknown"
    assert lob_normalization.normalize_lob_event_type("totally_unknown") == "piece.unknown"


def test_normalize_status_mapping():
    assert lob_normalization.normalize_lob_piece_status("piece.delivered") == "delivered"
    assert lob_normalization.normalize_lob_piece_status("piece.failed") == "failed"
    assert lob_normalization.normalize_lob_piece_status("piece.unknown") == "unknown"


def test_event_key_uses_explicit_id():
    payload = _payload(event_id="evt_explicit")
    assert lob_normalization.compute_lob_event_key(payload, b"raw") == "lob:evt_explicit"


def test_event_key_falls_back_to_resource_type_ts():
    payload = {"type": "x", "date_created": "ts", "body": {"resource": {"id": "r1"}}}
    assert lob_normalization.compute_lob_event_key(payload, b"raw") == "lob:r1:x:ts"


def test_event_key_falls_back_to_sha256_when_thin():
    raw = b'{"junk":1}'
    expected = "lob:" + hashlib.sha256(raw).hexdigest()
    assert lob_normalization.compute_lob_event_key({"junk": 1}, raw) == expected


# ---- schema validation ----


def test_schema_validation_rejects_missing_id():
    with pytest.raises(ValueError) as exc:
        lob_signature.validate_lob_payload_schema(
            {"type": "x", "date_created": "t", "body": {"resource": {"id": "r"}}}
        )
    assert "schema_invalid" in str(exc.value)


def test_schema_validation_rejects_unsupported_version():
    payload = {"version": "v999", **_payload()}
    with pytest.raises(ValueError) as exc:
        lob_signature.validate_lob_payload_schema(payload)
    assert "version_unsupported" in str(exc.value)


def test_schema_validation_accepts_v1_default():
    version, identity = lob_signature.validate_lob_payload_schema(_payload())
    assert version == "v1"
    assert identity["event_id"] == "evt_x"


# ---- signature + receiver flows ----


@pytest.mark.asyncio
async def test_enforce_rejects_bad_signature(fake_storage, monkeypatch):
    monkeypatch.setattr(settings, "LOB_WEBHOOK_SIGNATURE_MODE", "enforce")
    body = json.dumps(_payload()).encode()
    headers = {
        "Content-Type": "application/json",
        "Lob-Signature": "0" * 64,
        "Lob-Signature-Timestamp": str(int(time.time())),
    }
    resp = await _post("/webhooks/lob", body, headers)
    assert resp.status_code == 401
    assert resp.json()["detail"]["reason"] == "invalid_signature"
    assert fake_storage["events"] == {}


@pytest.mark.asyncio
async def test_enforce_accepts_valid_signature(fake_storage, monkeypatch):
    monkeypatch.setattr(settings, "LOB_WEBHOOK_SIGNATURE_MODE", "enforce")
    body = json.dumps(_payload()).encode()
    resp = await _post("/webhooks/lob", body, _signed_headers(body))
    assert resp.status_code == 202
    assert resp.json()["status"] == "processed"


@pytest.mark.asyncio
async def test_enforce_rejects_stale_timestamp(fake_storage, monkeypatch):
    monkeypatch.setattr(settings, "LOB_WEBHOOK_SIGNATURE_MODE", "enforce")
    monkeypatch.setattr(settings, "LOB_WEBHOOK_SIGNATURE_TOLERANCE_SECONDS", 30)
    body = json.dumps(_payload()).encode()
    stale_ts = str(int(time.time()) - 3600)
    resp = await _post("/webhooks/lob", body, _signed_headers(body, ts=stale_ts))
    assert resp.status_code == 401
    assert resp.json()["detail"]["reason"] == "stale_timestamp"


@pytest.mark.asyncio
async def test_permissive_audit_accepts_bad_sig(fake_storage, monkeypatch):
    monkeypatch.setattr(settings, "LOB_WEBHOOK_SIGNATURE_MODE", "permissive_audit")
    body = json.dumps(_payload()).encode()
    headers = {
        "Content-Type": "application/json",
        "Lob-Signature": "0" * 64,
        "Lob-Signature-Timestamp": str(int(time.time())),
    }
    resp = await _post("/webhooks/lob", body, headers)
    assert resp.status_code == 202
    body_resp = resp.json()
    assert body_resp["status"] == "processed"
    assert body_resp["signature"]["signature_verified"] is False


@pytest.mark.asyncio
async def test_unsupported_schema_rejected(fake_storage, monkeypatch):
    monkeypatch.setattr(settings, "LOB_WEBHOOK_SIGNATURE_MODE", "permissive_audit")
    payload = {**_payload(), "version": "v999"}
    body = json.dumps(payload).encode()
    resp = await _post("/webhooks/lob", body, _signed_headers(body))
    assert resp.status_code == 400
    assert "version_unsupported" in resp.json()["detail"]["reason"]


@pytest.mark.asyncio
async def test_duplicate_event_returns_200(fake_storage, monkeypatch):
    monkeypatch.setattr(settings, "LOB_WEBHOOK_SIGNATURE_MODE", "permissive_audit")
    body = json.dumps(_payload(event_id="dup-1")).encode()
    headers = _signed_headers(body)
    r1 = await _post("/webhooks/lob", body, headers)
    r2 = await _post("/webhooks/lob", body, headers)
    assert r1.status_code == 202
    assert r2.status_code == 200
    assert r2.json()["status"] == "duplicate_ignored"
    assert len(fake_storage["events"]) == 1


@pytest.mark.asyncio
async def test_projection_skipped_marks_dead_letter(fake_storage, monkeypatch):
    monkeypatch.setattr(settings, "LOB_WEBHOOK_SIGNATURE_MODE", "permissive_audit")
    fake_storage["projection"] = {"status": "skipped", "reason": "unknown_piece"}
    body = json.dumps(_payload()).encode()
    resp = await _post("/webhooks/lob", body, _signed_headers(body))
    assert resp.status_code == 202
    assert resp.json()["status"] == "dead_letter"
    rec = next(iter(fake_storage["events"].values()))
    assert rec["status"] == "dead_letter"
    assert rec["reason_code"] == "unknown_piece"


@pytest.mark.asyncio
async def test_projection_exception_marks_dead_letter(fake_storage, monkeypatch):
    monkeypatch.setattr(settings, "LOB_WEBHOOK_SIGNATURE_MODE", "permissive_audit")
    fake_storage["raise_projection"] = RuntimeError("db down")
    body = json.dumps(_payload()).encode()
    resp = await _post("/webhooks/lob", body, _signed_headers(body))
    assert resp.status_code == 202
    assert resp.json()["status"] == "dead_letter"
    rec = next(iter(fake_storage["events"].values()))
    assert rec["reason_code"] == "projection_failed"
    assert "db down" in rec["error"]


@pytest.mark.asyncio
async def test_malformed_json_400(fake_storage, monkeypatch):
    monkeypatch.setattr(settings, "LOB_WEBHOOK_SIGNATURE_MODE", "permissive_audit")
    body = b"not json{"
    headers = _signed_headers(body)
    resp = await _post("/webhooks/lob", body, headers)
    assert resp.status_code == 400
    assert resp.json()["detail"]["reason"] == "malformed_body"

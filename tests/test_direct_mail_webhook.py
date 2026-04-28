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
    """Realistic Lob webhook payload shape.

    See api-reference-docs-new/lob/api-reference/07-webhooks/02-events/02-events-webhook.md
    """
    return {
        "id": event_id,
        "event_type": {
            "id": event_type,
            "resource": event_type.split(".", 1)[0] + "s",
            "object": "event_type",
        },
        "reference_id": piece_id,
        "date_created": "2026-01-01T00:00:00Z",
        "body": {"id": piece_id, "object": event_type.split(".", 1)[0]},
        "object": "event",
    }


async def _post(path: str, body: bytes, headers: dict[str, str]) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, content=body, headers=headers)


# ---- normalization ----


def test_extract_event_name_reads_event_type_id():
    payload = {"event_type": {"id": "postcard.delivered", "resource": "postcards"}}
    assert lob_normalization.extract_lob_event_name(payload) == "postcard.delivered"


def test_extract_event_name_accepts_string_form_defensively():
    payload = {"event_type": "postcard.delivered"}
    assert lob_normalization.extract_lob_event_name(payload) == "postcard.delivered"


def test_extract_event_name_returns_none_when_absent():
    assert lob_normalization.extract_lob_event_name({}) is None


def test_extract_piece_id_reads_reference_id():
    payload = {"reference_id": "psc_abc", "body": {"id": "psc_xyz"}}
    assert lob_normalization.extract_lob_piece_id(payload) == "psc_abc"


def test_extract_piece_id_falls_back_to_body_id():
    payload = {"body": {"id": "psc_xyz"}}
    assert lob_normalization.extract_lob_piece_id(payload) == "psc_xyz"


def test_extract_piece_id_returns_none_when_thin():
    assert lob_normalization.extract_lob_piece_id({"body": {}}) is None


def test_extract_piece_address_reads_body_to():
    payload = {"body": {"to": {"address_line1": "1 Main"}}}
    assert lob_normalization.extract_lob_piece_address(payload) == {"address_line1": "1 Main"}


def test_normalize_event_type_buckets():
    n = lob_normalization.normalize_lob_event_type
    # Core lifecycle
    assert n("postcard.created") == "piece.created"
    assert n("letter.in_transit") == "piece.in_transit"
    assert n("self_mailer.delivered") == "piece.delivered"
    assert n("postcard.returned_to_sender") == "piece.returned"
    assert n("postcard.failed") == "piece.failed"
    assert n("postcard.deleted") == "piece.canceled"
    assert n("postcard.rejected") == "piece.rejected"
    assert n("postcard.rendered_pdf") == "piece.rendered_pdf"
    assert n("postcard.mailed") == "piece.mailed"
    assert n("postcard.in_local_area") == "piece.in_local_area"
    assert n("postcard.processed_for_delivery") == "piece.processed_for_delivery"
    assert n("postcard.re-routed") == "piece.re_routed"
    assert n("postcard.international_exit") == "piece.international_exit"
    assert n("postcard.viewed") == "piece.viewed"
    # Informed delivery (engagement)
    assert n("postcard.informed_delivery.email_sent") == "piece.informed_delivery.email_sent"
    assert (
        n("letter.informed_delivery.email_clicked_through")
        == "piece.informed_delivery.email_clicked_through"
    )
    # Certified mail
    assert n("letter.certified.delivered") == "piece.certified.delivered"
    assert n("letter.certified.returned_to_sender") == "piece.certified.returned"
    assert n("letter.certified.pickup_available") == "piece.certified.pickup_available"
    assert n("letter.certified.issue") == "piece.certified.issue"
    # Return envelope
    assert n("letter.return_envelope.created") == "piece.return_envelope.created"
    assert n("letter.return_envelope.returned_to_sender") == "piece.return_envelope.returned"
    # Edge cases
    assert n(None) == "piece.unknown"
    assert n("") == "piece.unknown"
    assert n("totally_unknown") == "piece.unknown"
    assert n("postcard.brand_new_event_lob_added_yesterday") == "piece.unknown"


def test_normalize_status_mapping():
    s = lob_normalization.normalize_lob_piece_status
    assert s("piece.created") == "queued"
    assert s("piece.delivered") == "delivered"
    assert s("piece.failed") == "failed"
    assert s("piece.returned") == "returned"
    assert s("piece.canceled") == "canceled"
    assert s("piece.in_transit") == "in_transit"
    assert s("piece.in_local_area") == "in_transit"
    assert s("piece.processed_for_delivery") == "in_transit"
    assert s("piece.mailed") == "in_transit"
    assert s("piece.international_exit") == "in_transit"
    assert s("piece.re_routed") == "in_transit"
    assert s("piece.certified.delivered") == "delivered"
    assert s("piece.certified.returned") == "returned"
    assert s("piece.certified.pickup_available") == "pickup_available"
    # None → engagement events; piece status is NOT updated
    assert s("piece.viewed") is None
    assert s("piece.informed_delivery.email_sent") is None
    assert s("piece.informed_delivery.email_opened") is None
    assert s("piece.informed_delivery.email_clicked_through") is None
    assert s("piece.return_envelope.created") is None
    assert s("piece.return_envelope.returned") is None
    assert s("piece.unknown") is None
    assert s("piece.something.we.never.heard.of") is None


def test_event_key_uses_explicit_id():
    payload = _payload(event_id="evt_explicit")
    assert lob_normalization.compute_lob_event_key(payload, b"raw") == "lob:evt_explicit"


def test_event_key_falls_back_to_sha256_when_thin():
    raw = b'{"junk":1}'
    expected = "lob:" + hashlib.sha256(raw).hexdigest()
    assert lob_normalization.compute_lob_event_key({"junk": 1}, raw) == expected


def test_suppression_triggers_cover_returned_and_failed():
    triggers = lob_normalization.SUPPRESSION_TRIGGERS
    assert triggers["piece.returned"] == "returned_to_sender"
    assert triggers["piece.failed"] == "failed"
    assert triggers["piece.certified.returned"] == "returned_to_sender"
    # Engagement events DO NOT trigger suppression
    assert "piece.viewed" not in triggers
    assert "piece.informed_delivery.email_sent" not in triggers


# ---- schema validation ----


def test_schema_validation_rejects_missing_event_id():
    payload = {**_payload()}
    del payload["id"]
    with pytest.raises(ValueError) as exc:
        lob_signature.validate_lob_payload_schema(payload)
    assert "schema_invalid:id" in str(exc.value)


def test_schema_validation_rejects_missing_event_type_id():
    payload = {**_payload()}
    payload["event_type"] = {"resource": "postcards"}  # no `id`
    with pytest.raises(ValueError) as exc:
        lob_signature.validate_lob_payload_schema(payload)
    assert "event_type.id" in str(exc.value)


def test_schema_validation_rejects_missing_reference_id():
    payload = {**_payload()}
    del payload["reference_id"]
    del payload["body"]
    with pytest.raises(ValueError) as exc:
        lob_signature.validate_lob_payload_schema(payload)
    assert "reference_id" in str(exc.value)


def test_schema_validation_rejects_unsupported_version():
    payload = {"version": "v999", **_payload()}
    with pytest.raises(ValueError) as exc:
        lob_signature.validate_lob_payload_schema(payload)
    assert "version_unsupported" in str(exc.value)


def test_schema_validation_accepts_v1_default():
    version, identity = lob_signature.validate_lob_payload_schema(_payload())
    assert version == "v1"
    assert identity["event_id"] == "evt_x"
    assert identity["event_type"] == "postcard.delivered"
    assert identity["resource_id"] == "psc_1"


def test_schema_validation_accepts_body_id_when_no_reference_id():
    payload = {**_payload()}
    del payload["reference_id"]
    version, identity = lob_signature.validate_lob_payload_schema(payload)
    assert identity["resource_id"] == "psc_1"


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

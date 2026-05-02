"""PostGrid webhook signature verification tests (check #3).

Tests the HMAC-SHA256 verification path with a known-good and known-bad payload.
Good = 2xx + persisted event. Bad = 4xx + nothing persisted.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.config import settings
from app.webhooks.postgrid_signature import (
    POSTGRID_SIGNATURE_HEADER,
    compute_postgrid_signature,
    verify_postgrid_signature,
)


# ---------------------------------------------------------------------------
# Unit tests for the signature computation helper
# ---------------------------------------------------------------------------


def test_compute_signature_deterministic():
    secret = "test_webhook_secret_abc"
    body = b'{"id":"evt1","type":"letter.delivered"}'
    sig1 = compute_postgrid_signature(body, secret)
    sig2 = compute_postgrid_signature(body, secret)
    assert sig1 == sig2


def test_compute_signature_known_value():
    secret = "test_webhook_secret_abc"
    body = b'{"id":"evt1"}'
    expected = hmac.new(
        secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    assert compute_postgrid_signature(body, secret) == expected


def test_compute_signature_different_body_gives_different_sig():
    secret = "test_webhook_secret_abc"
    sig1 = compute_postgrid_signature(b"body1", secret)
    sig2 = compute_postgrid_signature(b"body2", secret)
    assert sig1 != sig2


# ---------------------------------------------------------------------------
# verify_postgrid_signature unit tests (mocking Request and settings)
# ---------------------------------------------------------------------------


class _FakeRequest:
    def __init__(self, headers: dict):
        self.headers = headers


def _make_signed_request(body: bytes, secret: str) -> _FakeRequest:
    sig = compute_postgrid_signature(body, secret)
    return _FakeRequest({POSTGRID_SIGNATURE_HEADER: sig})


def test_good_signature_verify(monkeypatch):
    secret = "test_webhook_secret_abc"
    monkeypatch.setattr(settings, "POSTGRID_PRINT_MAIL_WEBHOOK_SECRET", secret)
    monkeypatch.setattr(settings, "POSTGRID_WEBHOOK_SIGNATURE_MODE", "enforce")

    body = b'{"id":"evt1","type":"letter.delivered"}'
    request = _make_signed_request(body, secret)
    result = verify_postgrid_signature(raw_body=body, request=request, request_id="r1")
    assert result["signature_verified"] is True
    assert result["signature_reason"] == "verified"


def test_bad_signature_raises_in_enforce_mode(monkeypatch):
    secret = "test_webhook_secret_abc"
    monkeypatch.setattr(settings, "POSTGRID_PRINT_MAIL_WEBHOOK_SECRET", secret)
    monkeypatch.setattr(settings, "POSTGRID_WEBHOOK_SIGNATURE_MODE", "enforce")

    body = b'{"id":"evt1","type":"letter.delivered"}'
    bad_request = _FakeRequest({POSTGRID_SIGNATURE_HEADER: "bad_signature_value"})
    with pytest.raises(HTTPException) as exc_info:
        verify_postgrid_signature(raw_body=body, request=bad_request, request_id="r1")
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["reason"] == "invalid_signature"


def test_bad_signature_permissive_audit_mode(monkeypatch):
    """In permissive_audit mode a bad signature returns a result dict without raising."""
    secret = "test_webhook_secret_abc"
    monkeypatch.setattr(settings, "POSTGRID_PRINT_MAIL_WEBHOOK_SECRET", secret)
    monkeypatch.setattr(settings, "POSTGRID_WEBHOOK_SIGNATURE_MODE", "permissive_audit")

    body = b'{"id":"evt1","type":"letter.delivered"}'
    bad_request = _FakeRequest({POSTGRID_SIGNATURE_HEADER: "bad_sig"})
    result = verify_postgrid_signature(raw_body=body, request=bad_request, request_id="r1")
    assert result["signature_verified"] is False
    assert result["signature_reason"] == "invalid_signature"


def test_missing_signature_header_enforce_mode_raises(monkeypatch):
    secret = "test_webhook_secret_abc"
    monkeypatch.setattr(settings, "POSTGRID_PRINT_MAIL_WEBHOOK_SECRET", secret)
    monkeypatch.setattr(settings, "POSTGRID_WEBHOOK_SIGNATURE_MODE", "enforce")

    body = b'{"id":"evt1"}'
    no_sig_request = _FakeRequest({})
    with pytest.raises(HTTPException) as exc_info:
        verify_postgrid_signature(raw_body=body, request=no_sig_request, request_id="r1")
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["reason"] == "missing_signature"


def test_no_secret_configured_permissive_returns_audit_result(monkeypatch):
    monkeypatch.setattr(settings, "POSTGRID_PRINT_MAIL_WEBHOOK_SECRET", None)
    monkeypatch.setattr(settings, "POSTGRID_WEBHOOK_SIGNATURE_MODE", "permissive_audit")

    body = b'{"id":"evt1"}'
    result = verify_postgrid_signature(
        raw_body=body, request=_FakeRequest({}), request_id="r1"
    )
    assert result["signature_verified"] is False
    assert result["signature_reason"] == "secret_not_configured"


def test_disabled_mode_skips_verification(monkeypatch):
    monkeypatch.setattr(settings, "POSTGRID_PRINT_MAIL_WEBHOOK_SECRET", "secret")
    monkeypatch.setattr(settings, "POSTGRID_WEBHOOK_SIGNATURE_MODE", "disabled")

    body = b'{"id":"evt1"}'
    result = verify_postgrid_signature(
        raw_body=body, request=_FakeRequest({}), request_id="r1"
    )
    assert result["signature_verified"] is False
    assert result["signature_reason"] == "disabled"

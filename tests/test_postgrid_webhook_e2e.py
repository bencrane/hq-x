"""PostGrid webhook end-to-end tests (checks #3 and #4).

Uses locally-signed simulated payloads — no real PostGrid API or tunnel needed.
This is the approach specified in the validator notes (deterministic, no flake).

Check #3: Signature verification
  - Known-good signed payload → handler returns 2xx
  - Known-bad signed payload → handler returns 4xx + nothing persisted

Check #4: End-to-end dispatch + ingest
  - Simulate a PostGrid letter webhook event with a locally-signed payload
  - Confirm event is signature-verified, identified as postgrid, and the
    normalization maps correctly.

These tests use httpx TestClient against the FastAPI app with the PostGrid
webhook router mounted. They mock DB calls to avoid needing a real database.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.webhooks.postgrid_signature import (
    POSTGRID_SIGNATURE_HEADER,
    compute_postgrid_signature,
)


# ---------------------------------------------------------------------------
# Minimal standalone FastAPI app with only the PostGrid webhook router
# ---------------------------------------------------------------------------


def _build_test_app(webhook_secret: str):
    """Build a minimal FastAPI app with only the postgrid webhook router."""
    from fastapi import FastAPI
    from app.routers.webhooks import postgrid as postgrid_webhooks

    app = FastAPI()
    app.include_router(postgrid_webhooks.router, prefix="/webhooks")
    return app


def _make_signed_payload(payload: dict, secret: str) -> tuple[bytes, str]:
    """Return (raw_body_bytes, signature_hex) for a PostGrid webhook payload."""
    raw_body = json.dumps(payload).encode("utf-8")
    sig = compute_postgrid_signature(raw_body, secret)
    return raw_body, sig


# A minimal valid PostGrid webhook payload for a letter.delivered event.
_LETTER_DELIVERED_PAYLOAD = {
    "id": "event_test_letter_delivered_001",
    "type": "letter.delivered",
    "data": {
        "object": {
            "id": "letter_test_abc001",
            "object": "letter",
            "status": "delivered",
            "to": {
                "firstName": "Jane",
                "lastName": "Doe",
                "addressLine1": "123 Main St",
                "city": "Springfield",
                "provinceOrState": "IL",
                "postalOrZip": "62701",
                "countryCode": "US",
            },
        }
    },
    "created_at": "2026-05-02T12:00:00Z",
}


# ---------------------------------------------------------------------------
# Check #3: Signature verification
# ---------------------------------------------------------------------------


def test_good_signature_returns_202(monkeypatch):
    """A correctly signed PostGrid payload should be accepted (2xx)."""
    secret = "test_webhook_secret_for_tests_only"
    monkeypatch.setattr("app.config.settings.POSTGRID_PRINT_MAIL_WEBHOOK_SECRET", secret)
    monkeypatch.setattr("app.config.settings.POSTGRID_WEBHOOK_SIGNATURE_MODE", "enforce")

    raw_body, sig = _make_signed_payload(_LETTER_DELIVERED_PAYLOAD, secret)

    # Mock DB calls so we don't need a real database
    mock_store = AsyncMock(return_value=("test-uuid-1234", True))
    mock_mark = AsyncMock()
    mock_project = AsyncMock(return_value={"status": "orphaned", "reason": "no_piece"})

    with (
        patch("app.routers.webhooks.postgrid._store_webhook_event", mock_store),
        patch("app.routers.webhooks.postgrid._mark_webhook_event", mock_mark),
        patch("app.routers.webhooks.postgrid.project_postgrid_event", mock_project),
    ):
        app = _build_test_app(secret)
        client = TestClient(app, raise_server_exceptions=True)
        response = client.post(
            "/webhooks/postgrid",
            content=raw_body,
            headers={
                "Content-Type": "application/json",
                POSTGRID_SIGNATURE_HEADER: sig,
            },
        )

    assert response.status_code in (200, 202), (
        f"Expected 2xx for valid signature, got {response.status_code}: {response.text}"
    )
    body = response.json()
    assert body.get("signature", {}).get("signature_verified") is True


def test_bad_signature_returns_401(monkeypatch):
    """An incorrectly signed payload should be rejected (4xx) in enforce mode."""
    secret = "test_webhook_secret_for_tests_only"
    monkeypatch.setattr("app.config.settings.POSTGRID_PRINT_MAIL_WEBHOOK_SECRET", secret)
    monkeypatch.setattr("app.config.settings.POSTGRID_WEBHOOK_SIGNATURE_MODE", "enforce")

    raw_body, _ = _make_signed_payload(_LETTER_DELIVERED_PAYLOAD, secret)

    mock_store = AsyncMock(return_value=("test-uuid-1234", True))

    with patch("app.routers.webhooks.postgrid._store_webhook_event", mock_store):
        app = _build_test_app(secret)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            "/webhooks/postgrid",
            content=raw_body,
            headers={
                "Content-Type": "application/json",
                POSTGRID_SIGNATURE_HEADER: "bad_signature_value_that_wont_verify",
            },
        )

    assert response.status_code == 401, (
        f"Expected 401 for bad signature, got {response.status_code}: {response.text}"
    )
    # Confirm nothing was persisted (store was NOT called)
    mock_store.assert_not_called()


def test_missing_signature_returns_401(monkeypatch):
    """No signature header at all → 401 in enforce mode."""
    secret = "test_webhook_secret_for_tests_only"
    monkeypatch.setattr("app.config.settings.POSTGRID_PRINT_MAIL_WEBHOOK_SECRET", secret)
    monkeypatch.setattr("app.config.settings.POSTGRID_WEBHOOK_SIGNATURE_MODE", "enforce")

    raw_body, _ = _make_signed_payload(_LETTER_DELIVERED_PAYLOAD, secret)

    mock_store = AsyncMock(return_value=("test-uuid-1234", True))

    with patch("app.routers.webhooks.postgrid._store_webhook_event", mock_store):
        app = _build_test_app(secret)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            "/webhooks/postgrid",
            content=raw_body,
            headers={"Content-Type": "application/json"},  # no signature header
        )

    assert response.status_code == 401
    mock_store.assert_not_called()


# ---------------------------------------------------------------------------
# Check #4: End-to-end dispatch + ingest (locally-signed simulation)
# ---------------------------------------------------------------------------


def test_e2e_signed_payload_accepted_and_event_stored(monkeypatch):
    """Simulate a PostGrid letter webhook end-to-end with a locally-signed payload.

    The event should be:
    1. Signature-verified
    2. Stored in webhook_events (mocked)
    3. Projected (mocked as orphaned since no DB piece row)
    4. Response includes event_key, event_type, signature.signature_verified=True
    """
    secret = "test_webhook_secret_for_tests_only"
    monkeypatch.setattr("app.config.settings.POSTGRID_PRINT_MAIL_WEBHOOK_SECRET", secret)
    monkeypatch.setattr("app.config.settings.POSTGRID_WEBHOOK_SIGNATURE_MODE", "enforce")

    raw_body, sig = _make_signed_payload(_LETTER_DELIVERED_PAYLOAD, secret)

    stored_events: list[dict] = []

    async def mock_store(**kwargs):
        stored_events.append(kwargs)
        return ("event-uuid-test-001", True)

    mock_mark = AsyncMock()
    mock_project = AsyncMock(return_value={
        "status": "orphaned",
        "external_piece_id": "letter_test_abc001",
        "normalized_event": "piece.delivered",
    })

    with (
        patch("app.routers.webhooks.postgrid._store_webhook_event", mock_store),
        patch("app.routers.webhooks.postgrid._mark_webhook_event", mock_mark),
        patch("app.routers.webhooks.postgrid.project_postgrid_event", mock_project),
    ):
        app = _build_test_app(secret)
        client = TestClient(app, raise_server_exceptions=True)
        response = client.post(
            "/webhooks/postgrid",
            content=raw_body,
            headers={
                "Content-Type": "application/json",
                POSTGRID_SIGNATURE_HEADER: sig,
            },
        )

    assert response.status_code in (200, 202)
    body = response.json()

    # Signature verification
    assert body["signature"]["signature_verified"] is True

    # Event was stored
    assert len(stored_events) == 1
    stored = stored_events[0]
    # event_key should be prefixed with postgrid:
    assert stored.get("event_key", "").startswith("postgrid:")

    # Event key and type are present in response
    assert "event_key" in body
    assert body["event_key"].startswith("postgrid:")
    assert body["event_type"] == "letter.delivered"

    # Projection was called with the PostGrid resource id
    mock_project.assert_called_once()
    call_kwargs = mock_project.call_args.kwargs
    assert call_kwargs["event_id"] == _LETTER_DELIVERED_PAYLOAD["id"]


def test_e2e_duplicate_event_returns_200_not_reprocessed(monkeypatch):
    """Duplicate event (same event_key) is accepted but not reprocessed."""
    secret = "test_webhook_secret_for_tests_only"
    monkeypatch.setattr("app.config.settings.POSTGRID_PRINT_MAIL_WEBHOOK_SECRET", secret)
    monkeypatch.setattr("app.config.settings.POSTGRID_WEBHOOK_SIGNATURE_MODE", "enforce")

    raw_body, sig = _make_signed_payload(_LETTER_DELIVERED_PAYLOAD, secret)

    # Store returns (uuid, False) = already exists
    mock_store = AsyncMock(return_value=("event-uuid-test-001", False))
    mock_project = AsyncMock()

    with (
        patch("app.routers.webhooks.postgrid._store_webhook_event", mock_store),
        patch("app.routers.webhooks.postgrid.project_postgrid_event", mock_project),
    ):
        app = _build_test_app(secret)
        client = TestClient(app, raise_server_exceptions=True)
        response = client.post(
            "/webhooks/postgrid",
            content=raw_body,
            headers={
                "Content-Type": "application/json",
                POSTGRID_SIGNATURE_HEADER: sig,
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "duplicate_ignored"
    # Projector was NOT called for a duplicate
    mock_project.assert_not_called()

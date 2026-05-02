"""PostGrid HTTP client tests.

Mirrors test_lob_client.py in structure. Mocks httpx.Client.request
so we never hit PostGrid's real API. Covers:
  - 9 resource-family namespaces (check #1: client surface)
  - CRUD method existence per namespace (check #1)
  - Test-mode guard (key prefix check) (check #6)
  - Error category logic
  - Idempotency key handling
"""

from __future__ import annotations

import os

import httpx
import pytest

from app.providers.postgrid import client as pg_client
from app.providers.postgrid.client import PostGridProviderError


class _FakeResponse:
    def __init__(self, status_code: int, body: dict | None = None):
        self.status_code = status_code
        self._body = body
        self.text = str(body) if body is not None else ""

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def _patch_request(monkeypatch, sequence):
    calls: list[dict] = []

    def fake_request(self, **kwargs):
        calls.append(kwargs)
        item = sequence[len(calls) - 1] if len(calls) <= len(sequence) else sequence[-1]
        if callable(item):
            return item(**kwargs)
        status_code, body = item
        return _FakeResponse(status_code, body)

    monkeypatch.setattr(httpx.Client, "request", fake_request)
    return calls


# ---------------------------------------------------------------------------
# Check #1: 9 namespace surface — verify client exposes CRUD per family
# ---------------------------------------------------------------------------


def test_client_exposes_letter_namespace():
    """Letters namespace exposes create/list/get/cancel."""
    assert callable(pg_client.create_letter)
    assert callable(pg_client.list_letters)
    assert callable(pg_client.get_letter)
    assert callable(pg_client.cancel_letter)


def test_client_exposes_postcard_namespace():
    assert callable(pg_client.create_postcard)
    assert callable(pg_client.list_postcards)
    assert callable(pg_client.get_postcard)
    assert callable(pg_client.cancel_postcard)


def test_client_exposes_cheque_namespace():
    assert callable(pg_client.create_cheque)
    assert callable(pg_client.list_cheques)
    assert callable(pg_client.get_cheque)
    assert callable(pg_client.cancel_cheque)


def test_client_exposes_self_mailer_namespace():
    assert callable(pg_client.create_self_mailer)
    assert callable(pg_client.list_self_mailers)
    assert callable(pg_client.get_self_mailer)
    assert callable(pg_client.cancel_self_mailer)


def test_client_exposes_return_envelope_namespace():
    """Return-envelope: list/get only (it's a flag on letters, not separate resource)."""
    assert callable(pg_client.list_return_envelopes)
    assert callable(pg_client.get_return_envelope)


def test_client_exposes_template_namespace():
    assert callable(pg_client.create_template)
    assert callable(pg_client.list_templates)
    assert callable(pg_client.get_template)
    assert callable(pg_client.update_template)
    assert callable(pg_client.delete_template)


def test_client_exposes_contact_namespace():
    assert callable(pg_client.create_contact)
    assert callable(pg_client.list_contacts)
    assert callable(pg_client.get_contact)
    assert callable(pg_client.delete_contact)


def test_client_exposes_webhooks_namespace():
    assert callable(pg_client.create_webhook)
    assert callable(pg_client.list_webhooks)
    assert callable(pg_client.get_webhook)
    assert callable(pg_client.delete_webhook)


def test_client_exposes_tracking_events_namespace():
    assert callable(pg_client.list_tracking_events)
    assert callable(pg_client.get_tracking_event)


# ---------------------------------------------------------------------------
# Check #6: test-mode guard — client refuses live keys outside production
# ---------------------------------------------------------------------------


def test_guard_rejects_live_key_in_test_env(monkeypatch):
    """Client must refuse live_ keys when APP_ENV != production."""
    monkeypatch.setenv("APP_ENV", "test")
    with pytest.raises(PostGridProviderError, match="test key required"):
        pg_client.create_letter("live_abc123", {"to": "contact_test"})


def test_guard_rejects_no_prefix_key_in_dev_env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    with pytest.raises(PostGridProviderError, match="test key required"):
        pg_client.create_letter("sk_something", {"to": "contact_test"})


def test_guard_accepts_test_key_in_test_env(monkeypatch):
    """test_ keys are always accepted."""
    monkeypatch.setenv("APP_ENV", "test")
    # This will fail at the network level, not the key guard
    calls = _patch_request(monkeypatch, [(200, {"id": "letter_abc", "object": "letter"})])
    result = pg_client.create_letter("test_abc123", {"to": "contact_test"})
    assert result["id"] == "letter_abc"


def test_guard_accepts_live_key_in_production(monkeypatch):
    """Live keys are accepted in production."""
    monkeypatch.setenv("APP_ENV", "production")
    calls = _patch_request(monkeypatch, [(200, {"id": "letter_xyz", "object": "letter"})])
    result = pg_client.create_letter("live_abc123", {"to": "contact_test"})
    assert result["id"] == "letter_xyz"


# ---------------------------------------------------------------------------
# Happy-path CRUD for core families
# ---------------------------------------------------------------------------


def test_create_letter_happy(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    _patch_request(monkeypatch, [(200, {"id": "letter_abc", "object": "letter"})])
    result = pg_client.create_letter("test_k", {"to": {"firstName": "Jane"}})
    assert result["id"] == "letter_abc"


def test_create_postcard_happy(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    _patch_request(monkeypatch, [(200, {"id": "postcard_abc"})])
    result = pg_client.create_postcard("test_k", {"to": {"firstName": "Jane"}})
    assert result["id"] == "postcard_abc"


def test_create_cheque_happy(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    _patch_request(monkeypatch, [(200, {"id": "cheque_abc"})])
    result = pg_client.create_cheque("test_k", {"to": {"firstName": "Jane"}})
    assert result["id"] == "cheque_abc"


def test_create_self_mailer_happy(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    _patch_request(monkeypatch, [(200, {"id": "selfmailer_abc"})])
    result = pg_client.create_self_mailer("test_k", {"to": {"firstName": "Jane"}})
    assert result["id"] == "selfmailer_abc"


def test_create_template_happy(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    _patch_request(monkeypatch, [(200, {"id": "template_abc"})])
    result = pg_client.create_template("test_k", {"html": "<p>Hello</p>"})
    assert result["id"] == "template_abc"


def test_create_contact_happy(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    _patch_request(monkeypatch, [(200, {"id": "contact_abc"})])
    result = pg_client.create_contact(
        "test_k", {"firstName": "Jane", "addressLine1": "123 Main St"}
    )
    assert result["id"] == "contact_abc"


def test_create_webhook_happy(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    _patch_request(monkeypatch, [(200, {"id": "webhook_abc", "url": "https://ex.com/hook"})])
    result = pg_client.create_webhook(
        "test_k", {"url": "https://example.com/webhooks/postgrid", "secret": "s3cr3t"}
    )
    assert result["id"] == "webhook_abc"


def test_list_tracking_events_happy(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    _patch_request(monkeypatch, [(200, {"data": [], "object": "list"})])
    result = pg_client.list_tracking_events("test_k", "letter_abc")
    assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_create_letter_401_raises(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    _patch_request(monkeypatch, [(401, None)])
    with pytest.raises(PostGridProviderError, match="Invalid PostGrid API key"):
        pg_client.create_letter("test_k", {"to": "contact_abc"})


def test_create_letter_404_raises(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    _patch_request(monkeypatch, [(404, None)])
    with pytest.raises(PostGridProviderError, match="endpoint not found"):
        pg_client.create_letter("test_k", {"to": "contact_abc"})


def test_create_letter_500_retries(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setattr("time.sleep", lambda _: None)
    calls = _patch_request(
        monkeypatch,
        [
            (500, None),
            (500, None),
            (200, {"id": "letter_ok"}),
        ],
    )
    result = pg_client.create_letter("test_k", {"to": "contact_abc"})
    assert result["id"] == "letter_ok"
    assert len(calls) == 3


def test_missing_api_key_raises():
    with pytest.raises(PostGridProviderError, match="Missing PostGrid API key"):
        pg_client.create_letter("", {"to": "contact_abc"})


def test_idempotency_key_sent(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    calls = _patch_request(monkeypatch, [(200, {"id": "letter_abc"})])
    pg_client.create_letter("test_k", {"to": "contact_abc"}, idempotency_key="my-key")
    assert calls[0]["headers"].get("Idempotency-Key") == "my-key"


def test_empty_idempotency_key_raises(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    with pytest.raises(PostGridProviderError, match="non-empty"):
        pg_client.create_letter("test_k", {"to": "contact_abc"}, idempotency_key="   ")


# ---------------------------------------------------------------------------
# Check #6 extra: fail-fast at boot — missing key raises immediately
# ---------------------------------------------------------------------------


def test_missing_key_raises_immediately():
    """Client raises PostGridProviderError immediately when api_key is empty."""
    with pytest.raises(PostGridProviderError, match="Missing PostGrid API key"):
        pg_client.list_letters("")


def test_error_category_transient():
    err = PostGridProviderError("PostGrid connectivity error: timeout")
    assert err.category == "transient"
    assert err.retryable is True


def test_error_category_terminal():
    err = PostGridProviderError("Invalid PostGrid API key")
    assert err.category == "terminal"
    assert err.retryable is False

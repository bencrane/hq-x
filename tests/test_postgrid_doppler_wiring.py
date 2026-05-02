"""PostGrid Doppler wiring tests (check #6).

Verifies that the PostGrid client and adapter:
  1. Load API key from settings (Doppler-sourced in prod).
  2. Fail-fast at boot (first use) when key is not configured.
  3. Refuse live keys outside of production (test-mode guard).
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.providers.postgrid.adapter import PostGridAdapter
from app.providers.postgrid.client import PostGridProviderError


def test_adapter_raises_when_test_key_not_configured(monkeypatch):
    """Adapter must raise PostGridProviderError immediately when test key is absent."""
    monkeypatch.setattr(settings, "POSTGRID_PRINT_MAIL_API_KEY_TEST", None)
    monkeypatch.setenv("APP_ENV", "test")
    adapter = PostGridAdapter(test_mode=True)
    with pytest.raises(PostGridProviderError, match="POSTGRID_PRINT_MAIL_API_KEY_TEST not set"):
        adapter.create_piece(piece_type="letter", payload={"to": "contact_test"})


def test_adapter_raises_when_live_key_not_configured(monkeypatch):
    """Adapter must raise PostGridProviderError when live key is absent."""
    monkeypatch.setattr(settings, "POSTGRID_PRINT_MAIL_API_KEY_LIVE", None)
    monkeypatch.setenv("APP_ENV", "production")
    adapter = PostGridAdapter(test_mode=False)
    with pytest.raises(PostGridProviderError, match="POSTGRID_PRINT_MAIL_API_KEY_LIVE not set"):
        adapter.create_piece(piece_type="letter", payload={"to": "contact_live"})


def test_client_refuses_live_key_in_test(monkeypatch):
    """Client must refuse non-test_ keys when APP_ENV is 'test'."""
    monkeypatch.setenv("APP_ENV", "test")
    from app.providers.postgrid import client as pg_client

    with pytest.raises(PostGridProviderError, match="test key required"):
        pg_client.list_letters("live_abc")


def test_client_accepts_test_key_in_test(monkeypatch):
    """Client accepts test_ keys."""
    import httpx

    monkeypatch.setenv("APP_ENV", "test")

    class _FakeResp:
        status_code = 200

        def json(self):
            return {"data": [], "object": "list"}

    def fake_request(self, **kwargs):
        return _FakeResp()

    monkeypatch.setattr(httpx.Client, "request", fake_request)
    from app.providers.postgrid import client as pg_client

    result = pg_client.list_letters("test_abc123")
    assert isinstance(result, dict)


def test_settings_has_postgrid_fields():
    """Settings object exposes PostGrid key fields (populated from Doppler in prd)."""
    # The fields must exist — the actual values may be None in test environments.
    assert hasattr(settings, "POSTGRID_PRINT_MAIL_API_KEY_TEST")
    assert hasattr(settings, "POSTGRID_PRINT_MAIL_API_KEY_LIVE")
    assert hasattr(settings, "POSTGRID_PRINT_MAIL_WEBHOOK_SECRET")
    assert hasattr(settings, "POSTGRID_WEBHOOK_SIGNATURE_MODE")

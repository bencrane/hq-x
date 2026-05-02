"""Entri HTTP client tests.

Mocks `httpx.Client.request` so we never hit Entri's real API. Covers:
  * /token mint shape and headers,
  * /power GET/POST/PUT/DELETE shapes,
  * retry on 429/5xx, no retry on terminal 4xx,
  * Authorization header passes the JWT verbatim (Entri does not use Bearer).
"""

from __future__ import annotations

import httpx
import pytest

from app.providers.entri import client as entri_client
from app.providers.entri.client import EntriProviderError


class _FakeResponse:
    def __init__(self, status_code: int, body=None, text: str = ""):
        self.status_code = status_code
        self._body = body
        self.text = text or (str(body) if body is not None else "")

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


def test_mint_token_happy(monkeypatch):
    calls = _patch_request(monkeypatch, [(200, {"auth_token": "jwt-xyz"})])
    result = entri_client.mint_token(application_id="app_1", secret="s3cr3t")
    assert result["auth_token"] == "jwt-xyz"
    assert calls[0]["url"].endswith("/token")
    assert calls[0]["method"] == "POST"
    assert calls[0]["json"] == {"applicationId": "app_1", "secret": "s3cr3t"}
    # No Authorization on /token — auth happens via body.
    assert "Authorization" not in calls[0]["headers"]


def test_mint_token_missing_credentials():
    with pytest.raises(EntriProviderError):
        entri_client.mint_token(application_id="", secret="x")
    with pytest.raises(EntriProviderError):
        entri_client.mint_token(application_id="x", secret="")


def test_mint_token_4xx_terminal(monkeypatch):
    _patch_request(monkeypatch, [(401, {"message": "bad creds"})])
    with pytest.raises(EntriProviderError) as ei:
        entri_client.mint_token(application_id="a", secret="b")
    assert ei.value.status == 401
    assert ei.value.category == "terminal"


def test_mint_token_retries_on_5xx(monkeypatch):
    monkeypatch.setattr(entri_client.time, "sleep", lambda *_: None)
    calls = _patch_request(
        monkeypatch,
        [(503, None), (503, None), (200, {"auth_token": "ok"})],
    )
    result = entri_client.mint_token(application_id="a", secret="b")
    assert result["auth_token"] == "ok"
    assert len(calls) == 3


def test_check_eligibility_uses_jwt_and_app_id(monkeypatch):
    calls = _patch_request(monkeypatch, [(200, {"eligible": True})])
    result = entri_client.check_power_eligibility(
        application_id="app_1",
        jwt="jwt-xyz",
        domain="qr.acme.com",
        root_domain=False,
    )
    assert result["eligible"] is True
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"].endswith("/power")
    assert calls[0]["params"] == {"domain": "qr.acme.com", "rootDomain": "false"}
    headers = calls[0]["headers"]
    assert headers["Authorization"] == "jwt-xyz"  # raw, no Bearer prefix
    assert headers["applicationId"] == "app_1"


def test_register_power_domain_payload(monkeypatch):
    calls = _patch_request(monkeypatch, [(200, {"ok": True})])
    entri_client.register_power_domain(
        application_id="app_1",
        jwt="j",
        domain="qr.acme.com",
        application_url="https://app.example.com/lp/1",
        power_root_path_access=["/_next/", "/static/"],
    )
    assert calls[0]["method"] == "POST"
    assert calls[0]["json"] == {
        "domain": "qr.acme.com",
        "applicationUrl": "https://app.example.com/lp/1",
        "powerRootPathAccess": ["/_next/", "/static/"],
    }


def test_update_power_domain_uses_put(monkeypatch):
    calls = _patch_request(monkeypatch, [(200, {"ok": True})])
    entri_client.update_power_domain(
        application_id="app_1",
        jwt="j",
        domain="qr.acme.com",
        application_url="https://app.example.com/lp/1",
    )
    assert calls[0]["method"] == "PUT"
    # No power_root_path_access → omitted from body.
    assert "powerRootPathAccess" not in calls[0]["json"]


def test_delete_power_domain_uses_delete(monkeypatch):
    calls = _patch_request(monkeypatch, [(200, {"ok": True})])
    entri_client.delete_power_domain(
        application_id="app_1", jwt="j", domain="qr.acme.com"
    )
    assert calls[0]["method"] == "DELETE"
    assert calls[0]["json"] == {"domain": "qr.acme.com"}


def test_provider_error_categories():
    assert EntriProviderError("x", status=429).category == "transient"
    assert EntriProviderError("x", status=500).category == "transient"
    assert EntriProviderError("x", status=401).category == "terminal"
    assert EntriProviderError("x", status=404).category == "terminal"
    assert EntriProviderError("x", status=None).category == "unknown"
    assert EntriProviderError("x", status=503).retryable is True
    assert EntriProviderError("x", status=400).retryable is False

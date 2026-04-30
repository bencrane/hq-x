"""Dub HTTP client tests for /domains endpoints.

Mocks `httpx.Client.request` so we never hit Dub's real API. Mirrors
test_dub_client.py for style.
"""

from __future__ import annotations

import httpx
import pytest

from app.providers.dub import client as dub_client
from app.providers.dub.client import DubProviderError


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


def test_create_domain_happy(monkeypatch):
    calls = _patch_request(
        monkeypatch,
        [(200, {"id": "dom_abc", "slug": "track.acme.com"})],
    )
    result = dub_client.create_domain(api_key="k", slug="track.acme.com")
    assert result["id"] == "dom_abc"
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"].endswith("/domains")
    assert calls[0]["headers"]["Authorization"] == "Bearer k"
    body = calls[0]["json"]
    assert body == {"slug": "track.acme.com"}


def test_create_domain_extra_fields_camel_translate(monkeypatch):
    calls = _patch_request(monkeypatch, [(200, {"id": "dom_x"})])
    dub_client.create_domain(
        api_key="k",
        slug="track.acme.com",
        expired_url="https://acme.com/expired",
        not_found_url="https://acme.com/404",
        archived=False,
    )
    body = calls[0]["json"]
    assert body["slug"] == "track.acme.com"
    assert body["expiredUrl"] == "https://acme.com/expired"
    assert body["notFoundUrl"] == "https://acme.com/404"
    assert body["archived"] is False
    assert "expired_url" not in body


def test_create_domain_terminal_400(monkeypatch):
    _patch_request(
        monkeypatch,
        [(400, {"error": {"code": "validation", "message": "domain in use"}})],
    )
    monkeypatch.setattr("time.sleep", lambda _: None)
    with pytest.raises(DubProviderError) as exc_info:
        dub_client.create_domain(api_key="k", slug="track.acme.com")
    assert exc_info.value.status == 400
    assert exc_info.value.code == "validation"
    assert exc_info.value.category == "terminal"


def test_list_domains_happy(monkeypatch):
    calls = _patch_request(
        monkeypatch,
        [
            (
                200,
                [
                    {"id": "dom_a", "slug": "track.acme.com"},
                    {"id": "dom_b", "slug": "links.example.com"},
                ],
            )
        ],
    )
    rows = dub_client.list_domains(api_key="k")
    assert len(rows) == 2
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"].endswith("/domains")


def test_list_domains_with_filters(monkeypatch):
    calls = _patch_request(monkeypatch, [(200, [])])
    dub_client.list_domains(api_key="k", archived=False, search="acme")
    params = calls[0]["params"]
    assert params["archived"] is False
    assert params["search"] == "acme"


def test_get_domain_by_slug_finds_match(monkeypatch):
    _patch_request(
        monkeypatch,
        [
            (
                200,
                [
                    {"id": "dom_a", "slug": "track.acme.com"},
                    {"id": "dom_b", "slug": "links.example.com"},
                ],
            )
        ],
    )
    result = dub_client.get_domain_by_slug(api_key="k", slug="track.acme.com")
    assert result is not None
    assert result["id"] == "dom_a"


def test_get_domain_by_slug_case_insensitive(monkeypatch):
    _patch_request(
        monkeypatch,
        [(200, [{"id": "dom_a", "slug": "Track.Acme.com"}])],
    )
    result = dub_client.get_domain_by_slug(api_key="k", slug="track.acme.com")
    assert result is not None
    assert result["id"] == "dom_a"


def test_get_domain_by_slug_no_match(monkeypatch):
    _patch_request(monkeypatch, [(200, [{"id": "dom_a", "slug": "other.com"}])])
    result = dub_client.get_domain_by_slug(api_key="k", slug="track.acme.com")
    assert result is None


def test_get_domain_by_slug_empty_workspace(monkeypatch):
    _patch_request(monkeypatch, [(200, [])])
    result = dub_client.get_domain_by_slug(api_key="k", slug="track.acme.com")
    assert result is None


def test_delete_domain_204(monkeypatch):
    calls = _patch_request(monkeypatch, [(204, None)])
    result = dub_client.delete_domain(api_key="k", slug="track.acme.com")
    assert result is None
    assert calls[0]["method"] == "DELETE"
    assert calls[0]["url"].endswith("/domains/track.acme.com")


def test_delete_domain_404(monkeypatch):
    _patch_request(monkeypatch, [(404, {"error": {"code": "not_found"}})])
    monkeypatch.setattr("time.sleep", lambda _: None)
    with pytest.raises(DubProviderError) as exc_info:
        dub_client.delete_domain(api_key="k", slug="track.acme.com")
    assert exc_info.value.status == 404
    assert exc_info.value.category == "terminal"


def test_create_domain_retries_on_503(monkeypatch):
    calls = _patch_request(monkeypatch, [(503, None), (200, {"id": "dom_x"})])
    monkeypatch.setattr("time.sleep", lambda _: None)
    result = dub_client.create_domain(api_key="k", slug="track.acme.com")
    assert result["id"] == "dom_x"
    assert len(calls) == 2

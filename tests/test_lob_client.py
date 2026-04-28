"""Lob HTTP client tests.

Mocks `httpx.Client` via monkeypatching `httpx.Client.request` so we never
hit Lob's real API. Covers retry semantics, header-vs-query idempotency,
and `LobProviderError.category`.
"""

from __future__ import annotations

import httpx
import pytest

from app.providers.lob import client as lob_client
from app.providers.lob.client import LobProviderError


class _FakeResponse:
    def __init__(self, status_code: int, body: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._body = body
        self.text = text or (str(body) if body is not None else "")

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def _patch_request(monkeypatch, sequence):
    """sequence is a list of (status_code, body) or callables."""
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


def test_create_postcard_happy(monkeypatch):
    calls = _patch_request(monkeypatch, [(200, {"id": "psc_abc", "price": "0.84"})])
    result = lob_client.create_postcard("k", {"to": {"name": "x"}})
    assert result["id"] == "psc_abc"
    assert calls[0]["url"].endswith("/v1/postcards")
    assert calls[0]["method"] == "POST"


def test_create_postcard_idempotency_header_default(monkeypatch):
    calls = _patch_request(monkeypatch, [(200, {"id": "psc_abc"})])
    lob_client.create_postcard("k", {"to": "adr_x"}, idempotency_key="K1")
    assert calls[0]["headers"]["Idempotency-Key"] == "K1"
    assert "idempotency_key" not in (calls[0].get("params") or {})


def test_create_postcard_idempotency_query(monkeypatch):
    calls = _patch_request(monkeypatch, [(200, {"id": "psc_abc"})])
    lob_client.create_postcard(
        "k", {"to": "adr_x"}, idempotency_key="K1", idempotency_in_query=True
    )
    assert "Idempotency-Key" not in calls[0]["headers"]
    assert calls[0]["params"]["idempotency_key"] == "K1"


def test_idempotency_mutual_exclusivity_in_helper():
    with pytest.raises(LobProviderError):
        lob_client.build_idempotency_material(header_key="a", query_key="b")


def test_retries_on_429_then_succeeds(monkeypatch):
    calls = _patch_request(monkeypatch, [(429, None), (200, {"id": "x"})])
    monkeypatch.setattr("time.sleep", lambda _: None)
    result = lob_client.create_postcard("k", {"to": "adr_x"})
    assert result["id"] == "x"
    assert len(calls) == 2


def test_retries_on_503_then_succeeds(monkeypatch):
    calls = _patch_request(monkeypatch, [(503, None), (200, {"id": "x"})])
    monkeypatch.setattr("time.sleep", lambda _: None)
    lob_client.create_postcard("k", {"to": "adr_x"})
    assert len(calls) == 2


def test_no_retry_on_4xx(monkeypatch):
    calls = _patch_request(monkeypatch, [(400, None)])
    monkeypatch.setattr("time.sleep", lambda _: None)
    with pytest.raises(LobProviderError) as exc_info:
        lob_client.create_postcard("k", {"to": "adr_x"})
    assert len(calls) == 1
    assert "HTTP 400" in str(exc_info.value)


def test_unauthorized_raises_terminal(monkeypatch):
    _patch_request(monkeypatch, [(401, None)])
    monkeypatch.setattr("time.sleep", lambda _: None)
    with pytest.raises(LobProviderError) as exc_info:
        lob_client.create_postcard("k", {"to": "adr_x"})
    assert "Invalid Lob API key" in str(exc_info.value)
    assert exc_info.value.category == "terminal"


def test_404_terminal_category(monkeypatch):
    _patch_request(monkeypatch, [(404, None)])
    monkeypatch.setattr("time.sleep", lambda _: None)
    with pytest.raises(LobProviderError) as exc_info:
        lob_client.get_postcard("k", "psc_x")
    assert exc_info.value.category == "terminal"


def test_429_transient_category(monkeypatch):
    _patch_request(monkeypatch, [(429, None), (429, None), (429, None)])
    monkeypatch.setattr("time.sleep", lambda _: None)
    with pytest.raises(LobProviderError) as exc_info:
        lob_client.create_postcard("k", {"to": "adr_x"})
    assert exc_info.value.category == "transient"


def test_validate_api_key_calls_postcards(monkeypatch):
    calls = _patch_request(monkeypatch, [(200, {"data": [], "object": "list"})])
    lob_client.validate_api_key("k")
    assert calls[0]["url"].endswith("/v1/postcards")
    assert calls[0]["params"]["limit"] == 1


def test_validate_api_key_sad(monkeypatch):
    _patch_request(monkeypatch, [(401, None)])
    monkeypatch.setattr("time.sleep", lambda _: None)
    with pytest.raises(LobProviderError):
        lob_client.validate_api_key("bad")


def test_letter_letter_self_mailer_paths(monkeypatch):
    seqs = [
        ("/v1/letters", lob_client.create_letter),
        ("/v1/self_mailers", lob_client.create_self_mailer),
        ("/v1/snap_packs", lob_client.create_snap_pack),
        ("/v1/booklets", lob_client.create_booklet),
    ]
    for path, fn in seqs:
        calls = _patch_request(monkeypatch, [(200, {"id": "x"})])
        fn("k", {"to": "adr_x"})
        assert calls[0]["url"].endswith(path)


def test_missing_api_key_raises():
    with pytest.raises(LobProviderError) as exc:
        lob_client.create_postcard("", {"to": "adr_x"})
    assert "Missing Lob API key" in str(exc.value)


def test_empty_idempotency_key_rejected(monkeypatch):
    _patch_request(monkeypatch, [(200, {"id": "x"})])
    with pytest.raises(LobProviderError):
        lob_client.create_postcard("k", {"to": "adr_x"}, idempotency_key="")

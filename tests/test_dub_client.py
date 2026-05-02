"""Dub HTTP client tests.

Mocks `httpx.Client.request` so we never hit Dub's real API. Covers:
  * snake↔camel translation at the boundary,
  * bearer-token auth header,
  * retry semantics on 429/5xx with no retry on terminal 4xx,
  * Dub error envelope parsing into DubProviderError.code.
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


def test_create_link_happy(monkeypatch):
    calls = _patch_request(
        monkeypatch,
        [(200, {"id": "link_abc", "shortLink": "https://dub.sh/abc", "url": "https://x"})],
    )
    result = dub_client.create_link(api_key="k", url="https://example.com")
    assert result["id"] == "link_abc"
    assert calls[0]["url"].endswith("/links")
    assert calls[0]["method"] == "POST"
    assert calls[0]["headers"]["Authorization"] == "Bearer k"


def test_create_link_camel_translation(monkeypatch):
    calls = _patch_request(monkeypatch, [(200, {"id": "link_abc"})])
    dub_client.create_link(
        api_key="k",
        url="https://example.com",
        external_id="ext_42",
        tenant_id="hq-x",
        tag_ids=["t1", "t2"],
        track_conversion=True,
    )
    body = calls[0]["json"]
    assert body["externalId"] == "ext_42"
    assert body["tenantId"] == "hq-x"
    assert body["tagIds"] == ["t1", "t2"]
    assert body["trackConversion"] is True
    # Snake-case keys are translated, not passed through.
    assert "external_id" not in body
    assert "tenant_id" not in body


def test_create_link_drops_none(monkeypatch):
    calls = _patch_request(monkeypatch, [(200, {"id": "link_abc"})])
    dub_client.create_link(api_key="k", url="https://example.com")
    body = calls[0]["json"]
    # Only `url` should be sent — None values get dropped.
    assert body == {"url": "https://example.com"}


def test_retries_on_429_then_succeeds(monkeypatch):
    calls = _patch_request(monkeypatch, [(429, None), (200, {"id": "link_x"})])
    monkeypatch.setattr("time.sleep", lambda _: None)
    result = dub_client.create_link(api_key="k", url="https://x")
    assert result["id"] == "link_x"
    assert len(calls) == 2


def test_retries_on_503_then_succeeds(monkeypatch):
    calls = _patch_request(monkeypatch, [(503, None), (200, {"id": "link_x"})])
    monkeypatch.setattr("time.sleep", lambda _: None)
    dub_client.create_link(api_key="k", url="https://x")
    assert len(calls) == 2


def test_retries_exhausted_429_terminal(monkeypatch):
    calls = _patch_request(monkeypatch, [(429, None), (429, None), (429, None)])
    monkeypatch.setattr("time.sleep", lambda _: None)
    with pytest.raises(DubProviderError) as exc_info:
        dub_client.create_link(api_key="k", url="https://x")
    assert len(calls) == 3
    assert exc_info.value.category == "transient"


def test_no_retry_on_400(monkeypatch):
    calls = _patch_request(monkeypatch, [(400, {"error": {"code": "bad", "message": "nope"}})])
    monkeypatch.setattr("time.sleep", lambda _: None)
    with pytest.raises(DubProviderError) as exc_info:
        dub_client.create_link(api_key="k", url="https://x")
    assert len(calls) == 1
    assert exc_info.value.status == 400
    assert exc_info.value.code == "bad"
    assert exc_info.value.category == "terminal"


def test_no_retry_on_401(monkeypatch):
    calls = _patch_request(monkeypatch, [(401, {"error": {"code": "unauthorized"}})])
    monkeypatch.setattr("time.sleep", lambda _: None)
    with pytest.raises(DubProviderError) as exc_info:
        dub_client.create_link(api_key="k", url="https://x")
    assert len(calls) == 1
    assert exc_info.value.status == 401
    assert exc_info.value.category == "terminal"


def test_no_retry_on_404(monkeypatch):
    calls = _patch_request(monkeypatch, [(404, {"error": {"code": "not_found"}})])
    monkeypatch.setattr("time.sleep", lambda _: None)
    with pytest.raises(DubProviderError) as exc_info:
        dub_client.get_link(api_key="k", link_id="link_x")
    assert len(calls) == 1
    assert exc_info.value.category == "terminal"


def test_no_retry_on_422(monkeypatch):
    _patch_request(monkeypatch, [(422, {"error": {"code": "validation"}})])
    monkeypatch.setattr("time.sleep", lambda _: None)
    with pytest.raises(DubProviderError) as exc_info:
        dub_client.create_link(api_key="k", url="https://x")
    assert exc_info.value.category == "terminal"


def test_error_envelope_parsing(monkeypatch):
    body = {
        "error": {
            "code": "conflict",
            "message": "key in use",
            "doc_url": "https://dub.co/docs/errors/conflict",
        }
    }
    _patch_request(monkeypatch, [(409, body)])
    monkeypatch.setattr("time.sleep", lambda _: None)
    with pytest.raises(DubProviderError) as exc_info:
        dub_client.create_link(api_key="k", url="https://x")
    assert exc_info.value.code == "conflict"
    assert exc_info.value.doc_url == "https://dub.co/docs/errors/conflict"
    assert "key in use" in str(exc_info.value)


def test_delete_link_returns_none_on_204(monkeypatch):
    calls = _patch_request(monkeypatch, [(204, None)])
    result = dub_client.delete_link(api_key="k", link_id="link_x")
    assert result is None
    assert calls[0]["method"] == "DELETE"
    assert calls[0]["url"].endswith("/links/link_x")


def test_retrieve_analytics_query_params(monkeypatch):
    calls = _patch_request(monkeypatch, [(200, [{"date": "2026-04-01", "clicks": 5}])])
    dub_client.retrieve_analytics(
        api_key="k",
        event="clicks",
        group_by="timeseries",
        interval="7d",
        link_id="link_abc",
    )
    params = calls[0]["params"]
    assert params["event"] == "clicks"
    assert params["groupBy"] == "timeseries"
    assert params["interval"] == "7d"
    assert params["linkId"] == "link_abc"


def test_list_events_query_params(monkeypatch):
    calls = _patch_request(monkeypatch, [(200, [])])
    dub_client.list_events(
        api_key="k",
        event="leads",
        link_id="link_abc",
        page=2,
    )
    params = calls[0]["params"]
    assert params["event"] == "leads"
    assert params["linkId"] == "link_abc"
    assert params["page"] == 2


def test_get_link_by_external_id_uses_info_endpoint(monkeypatch):
    calls = _patch_request(monkeypatch, [(200, {"id": "link_abc"})])
    dub_client.get_link_by_external_id(api_key="k", external_id="ext_42")
    assert calls[0]["url"].endswith("/links/info")
    assert calls[0]["params"] == {"externalId": "ext_42"}


def test_list_links_camel_translation(monkeypatch):
    calls = _patch_request(monkeypatch, [(200, [{"id": "link_a"}, {"id": "link_b"}])])
    result = dub_client.list_links(api_key="k", tenant_id="hq-x", page_size=25)
    assert len(result) == 2
    params = calls[0]["params"]
    assert params["tenantId"] == "hq-x"
    assert params["pageSize"] == 25


def test_update_link_camel_translation(monkeypatch):
    calls = _patch_request(monkeypatch, [(200, {"id": "link_x", "archived": True})])
    dub_client.update_link(
        api_key="k",
        link_id="link_x",
        fields={"archived": True, "external_id": "ext_99"},
    )
    body = calls[0]["json"]
    assert body == {"archived": True, "externalId": "ext_99"}
    assert calls[0]["method"] == "PATCH"


def test_missing_api_key_raises():
    with pytest.raises(DubProviderError) as exc:
        dub_client.create_link(api_key="", url="https://x")
    assert "Missing Dub API key" in str(exc.value)


def test_non_json_error_falls_back(monkeypatch):
    # Body=None means _FakeResponse.json() raises ValueError; the client
    # falls back to f"HTTP {status}: {text}" for the error message.
    _patch_request(monkeypatch, [(500, None), (500, None), (500, None)])
    monkeypatch.setattr("time.sleep", lambda _: None)
    with pytest.raises(DubProviderError) as exc_info:
        dub_client.get_link(api_key="k", link_id="link_x")
    # 500 is retryable — exhausts attempts then raises with category transient.
    assert exc_info.value.category == "transient"
    assert exc_info.value.status == 500

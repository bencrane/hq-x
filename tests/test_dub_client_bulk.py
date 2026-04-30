"""Bulk link operations on the Dub HTTP client.

Mirrors the patching style of tests/test_dub_client.py — never hits Dub.
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


def test_bulk_create_links_sends_array_body(monkeypatch):
    calls = _patch_request(
        monkeypatch,
        [(200, [{"id": "link_a"}, {"id": "link_b"}])],
    )
    result = dub_client.bulk_create_links(
        api_key="k",
        links=[
            {"url": "https://a", "external_id": "ext_a", "tag_names": ["x"]},
            {"url": "https://b", "external_id": "ext_b"},
        ],
    )
    assert len(result) == 2
    body = calls[0]["json"]
    assert isinstance(body, list)
    assert body[0] == {
        "url": "https://a",
        "externalId": "ext_a",
        "tagNames": ["x"],
    }
    assert body[1] == {"url": "https://b", "externalId": "ext_b"}
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"].endswith("/links/bulk")
    assert calls[0]["headers"]["Authorization"] == "Bearer k"


def test_bulk_create_links_rejects_over_100(monkeypatch):
    _patch_request(monkeypatch, [(200, [])])
    links = [{"url": f"https://x/{i}"} for i in range(101)]
    with pytest.raises(DubProviderError) as exc:
        dub_client.bulk_create_links(api_key="k", links=links)
    assert "exceeds 100" in str(exc.value)


def test_bulk_create_links_returns_mixed_success_and_error_entries(monkeypatch):
    response_body = [
        {"id": "link_ok", "shortLink": "https://dub.sh/ok"},
        {"error": {"code": "conflict", "message": "key in use"}},
        {"id": "link_two"},
    ]
    _patch_request(monkeypatch, [(200, response_body)])
    result = dub_client.bulk_create_links(
        api_key="k",
        links=[
            {"url": "https://a"},
            {"url": "https://b"},
            {"url": "https://c"},
        ],
    )
    assert len(result) == 3
    assert result[0]["id"] == "link_ok"
    assert result[1]["error"]["code"] == "conflict"
    assert result[2]["id"] == "link_two"


def test_bulk_update_links(monkeypatch):
    calls = _patch_request(
        monkeypatch, [(200, [{"id": "link_a", "archived": True}])]
    )
    dub_client.bulk_update_links(
        api_key="k",
        link_ids=["link_a"],
        fields={"archived": True, "external_id": "ext_x"},
    )
    body = calls[0]["json"]
    assert body == {
        "linkIds": ["link_a"],
        "data": {"archived": True, "externalId": "ext_x"},
    }
    assert calls[0]["method"] == "PATCH"
    assert calls[0]["url"].endswith("/links/bulk")


def test_bulk_delete_links(monkeypatch):
    calls = _patch_request(monkeypatch, [(200, {"deletedCount": 2})])
    result = dub_client.bulk_delete_links(
        api_key="k", link_ids=["link_a", "link_b"]
    )
    assert result["deletedCount"] == 2
    assert calls[0]["method"] == "DELETE"
    assert calls[0]["url"].endswith("/links/bulk")
    assert calls[0]["params"]["linkIds"] == "link_a,link_b"


def test_upsert_link(monkeypatch):
    calls = _patch_request(monkeypatch, [(200, {"id": "link_x"})])
    dub_client.upsert_link(
        api_key="k",
        url="https://example.com",
        external_id="ext_42",
        folder_id="fold_abc",
    )
    body = calls[0]["json"]
    assert body == {
        "url": "https://example.com",
        "externalId": "ext_42",
        "folderId": "fold_abc",
    }
    assert calls[0]["method"] == "PUT"
    assert calls[0]["url"].endswith("/links")

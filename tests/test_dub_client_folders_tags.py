"""Dub HTTP client — folders + tags CRUD."""

from __future__ import annotations

import httpx

from app.providers.dub import client as dub_client


class _FakeResponse:
    def __init__(self, status_code: int, body=None, text: str = ""):
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def _patch_request(monkeypatch, sequence):
    calls: list[dict] = []

    def fake_request(self, **kwargs):
        calls.append(kwargs)
        item = sequence[len(calls) - 1] if len(calls) <= len(sequence) else sequence[-1]
        status_code, body = item
        return _FakeResponse(status_code, body)

    monkeypatch.setattr(httpx.Client, "request", fake_request)
    return calls


# ---------------------------------------------------------------------------
# Folders
# ---------------------------------------------------------------------------


def test_list_folders(monkeypatch):
    calls = _patch_request(monkeypatch, [(200, [{"id": "fold_a"}, {"id": "fold_b"}])])
    result = dub_client.list_folders(api_key="k")
    assert len(result) == 2
    assert calls[0]["url"].endswith("/folders")
    assert calls[0]["method"] == "GET"


def test_create_folder_camel(monkeypatch):
    calls = _patch_request(monkeypatch, [(200, {"id": "fold_a"})])
    dub_client.create_folder(api_key="k", name="campaign:abc", access_level="read")
    body = calls[0]["json"]
    assert body == {"name": "campaign:abc", "accessLevel": "read"}
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"].endswith("/folders")


def test_get_folder(monkeypatch):
    calls = _patch_request(monkeypatch, [(200, {"id": "fold_a"})])
    dub_client.get_folder(api_key="k", folder_id="fold_a")
    assert calls[0]["url"].endswith("/folders/fold_a")
    assert calls[0]["method"] == "GET"


def test_update_folder(monkeypatch):
    calls = _patch_request(monkeypatch, [(200, {"id": "fold_a", "name": "new"})])
    dub_client.update_folder(
        api_key="k", folder_id="fold_a", name="new", access_level="write"
    )
    body = calls[0]["json"]
    assert body == {"name": "new", "accessLevel": "write"}
    assert calls[0]["method"] == "PATCH"


def test_delete_folder(monkeypatch):
    calls = _patch_request(monkeypatch, [(204, None)])
    result = dub_client.delete_folder(api_key="k", folder_id="fold_a")
    assert result is None
    assert calls[0]["method"] == "DELETE"
    assert calls[0]["url"].endswith("/folders/fold_a")


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


def test_list_tags(monkeypatch):
    calls = _patch_request(monkeypatch, [(200, [{"id": "tag_a"}])])
    result = dub_client.list_tags(api_key="k")
    assert len(result) == 1
    assert calls[0]["url"].endswith("/tags")


def test_create_tag(monkeypatch):
    calls = _patch_request(monkeypatch, [(200, {"id": "tag_a"})])
    dub_client.create_tag(api_key="k", name="brand:hq-x", color="red")
    body = calls[0]["json"]
    assert body == {"name": "brand:hq-x", "color": "red"}


def test_update_tag(monkeypatch):
    calls = _patch_request(monkeypatch, [(200, {"id": "tag_a"})])
    dub_client.update_tag(api_key="k", tag_id="tag_a", color="blue")
    body = calls[0]["json"]
    assert body == {"color": "blue"}
    assert calls[0]["url"].endswith("/tags/tag_a")
    assert calls[0]["method"] == "PATCH"


def test_delete_tag(monkeypatch):
    calls = _patch_request(monkeypatch, [(204, None)])
    dub_client.delete_tag(api_key="k", tag_id="tag_a")
    assert calls[0]["method"] == "DELETE"
    assert calls[0]["url"].endswith("/tags/tag_a")

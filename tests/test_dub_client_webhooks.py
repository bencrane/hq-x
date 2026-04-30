"""Dub HTTP client — webhooks CRUD."""

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


def test_list_webhooks(monkeypatch):
    calls = _patch_request(monkeypatch, [(200, [{"id": "wh_a"}])])
    result = dub_client.list_webhooks(api_key="k")
    assert len(result) == 1
    assert calls[0]["url"].endswith("/webhooks")


def test_create_webhook_camel(monkeypatch):
    calls = _patch_request(monkeypatch, [(200, {"id": "wh_a"})])
    dub_client.create_webhook(
        api_key="k",
        name="hq-x:dev",
        url="https://api.hq-x.com/webhooks/dub",
        triggers=["link.clicked", "lead.created"],
        secret="s",
        link_ids=["link_a"],
    )
    body = calls[0]["json"]
    assert body == {
        "name": "hq-x:dev",
        "url": "https://api.hq-x.com/webhooks/dub",
        "triggers": ["link.clicked", "lead.created"],
        "secret": "s",
        "linkIds": ["link_a"],
    }
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"].endswith("/webhooks")


def test_get_webhook(monkeypatch):
    calls = _patch_request(monkeypatch, [(200, {"id": "wh_a"})])
    dub_client.get_webhook(api_key="k", webhook_id="wh_a")
    assert calls[0]["url"].endswith("/webhooks/wh_a")


def test_update_webhook(monkeypatch):
    calls = _patch_request(monkeypatch, [(200, {"id": "wh_a"})])
    dub_client.update_webhook(
        api_key="k",
        webhook_id="wh_a",
        triggers=["lead.created"],
        disabled=True,
    )
    body = calls[0]["json"]
    assert body == {"triggers": ["lead.created"], "disabled": True}
    assert calls[0]["method"] == "PATCH"


def test_delete_webhook(monkeypatch):
    calls = _patch_request(monkeypatch, [(204, None)])
    dub_client.delete_webhook(api_key="k", webhook_id="wh_a")
    assert calls[0]["method"] == "DELETE"
    assert calls[0]["url"].endswith("/webhooks/wh_a")

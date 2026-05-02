"""Dub HTTP client — analytics + events filter expansion.

Verifies snake → camel projection on the expanded filter dimensions added
in the Dub-at-scale directive.
"""

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


def test_analytics_forwards_all_new_filters(monkeypatch):
    calls = _patch_request(monkeypatch, [(200, [])])
    dub_client.retrieve_analytics(
        api_key="k",
        event="leads",
        group_by="countries",
        country="US",
        city="New York",
        region="NY",
        continent="NA",
        device="Mobile",
        browser="Safari",
        os="iOS",
        referer="t.co",
        referer_url="https://t.co/abc",
        url="https://landing.example.com",
        qr=True,
        trigger="qr",
        folder_id="fold_abc",
        customer_id="rcpt_42",
        timezone="America/New_York",
    )
    params = calls[0]["params"]
    # Snake-keys translate; verify each new dimension is forwarded as camel.
    assert params["country"] == "US"
    assert params["city"] == "New York"
    assert params["region"] == "NY"
    assert params["continent"] == "NA"
    assert params["device"] == "Mobile"
    assert params["browser"] == "Safari"
    assert params["os"] == "iOS"
    assert params["referer"] == "t.co"
    assert params["refererUrl"] == "https://t.co/abc"
    assert params["url"] == "https://landing.example.com"
    assert params["qr"] is True
    assert params["trigger"] == "qr"
    assert params["folderId"] == "fold_abc"
    assert params["customerId"] == "rcpt_42"
    assert params["timezone"] == "America/New_York"


def test_events_forwards_new_filters(monkeypatch):
    calls = _patch_request(monkeypatch, [(200, [])])
    dub_client.list_events(
        api_key="k",
        event="clicks",
        country="US",
        device="Desktop",
        folder_id="fold_abc",
        customer_id="rcpt_42",
    )
    params = calls[0]["params"]
    assert params["country"] == "US"
    assert params["device"] == "Desktop"
    assert params["folderId"] == "fold_abc"
    assert params["customerId"] == "rcpt_42"

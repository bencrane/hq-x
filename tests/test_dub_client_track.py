"""Dub HTTP client — server-side track_lead / track_sale.

Verifies snake → camel projection on the wire so Dub accepts our payloads.
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


def test_track_lead_camel_projection(monkeypatch):
    calls = _patch_request(monkeypatch, [(200, {"clickId": "c_1"})])
    dub_client.track_lead(
        api_key="k",
        click_id="c_1",
        event_name="Form Submitted",
        customer_external_id="rcpt_42",
        customer_name="Ada",
        customer_email="ada@example.com",
        metadata={"plan": "enterprise"},
    )
    body = calls[0]["json"]
    assert body == {
        "clickId": "c_1",
        "eventName": "Form Submitted",
        "customerExternalId": "rcpt_42",
        "customerName": "Ada",
        "customerEmail": "ada@example.com",
        "metadata": {"plan": "enterprise"},
    }
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"].endswith("/track/lead")


def test_track_sale_camel_projection(monkeypatch):
    calls = _patch_request(monkeypatch, [(200, {"sale": {"amount": 1999}})])
    dub_client.track_sale(
        api_key="k",
        customer_external_id="rcpt_42",
        amount=1999,
        currency="usd",
        event_name="Purchase",
        invoice_id="inv_7",
        payment_processor="stripe",
        metadata={"source": "direct_mail"},
    )
    body = calls[0]["json"]
    assert body == {
        "customerExternalId": "rcpt_42",
        "amount": 1999,
        "currency": "usd",
        "eventName": "Purchase",
        "invoiceId": "inv_7",
        "paymentProcessor": "stripe",
        "metadata": {"source": "direct_mail"},
    }
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"].endswith("/track/sale")

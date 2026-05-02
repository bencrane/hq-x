"""Provider attribution per dispatch (check #7).

Asserts that routing decisions produce the expected provider, provider_piece_id,
resource_family, and routing_decision fields for both PostGrid and Lob dispatches.

Uses httpx monkeypatching so no real API calls are made.
"""

from __future__ import annotations

import httpx
import pytest

from app.providers.routing.direct_mail import dispatch_piece


class _FakeResponse:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


def _patch_request(monkeypatch, body: dict, status_code: int = 200):
    def fake_request(self, **kwargs):
        return _FakeResponse(status_code, body)

    monkeypatch.setattr(httpx.Client, "request", fake_request)


def test_postgrid_dispatch_attribution(monkeypatch):
    """PostGrid dispatch sets provider=postgrid and routing_decision=preferred-postgrid-used."""
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setattr(
        "app.providers.routing.direct_mail._postgrid_test_key", lambda: "test_pg_key"
    )
    _patch_request(monkeypatch, {"id": "letter_test_abc", "object": "letter"})

    response, routing = dispatch_piece(
        resource_family="letter",
        payload={"to": {"firstName": "Jane"}, "html": "<p>Hello</p>"},
        test_mode=True,
    )

    assert routing.provider == "postgrid"
    assert routing.routing_decision == "preferred-postgrid-used"
    assert routing.resource_family == "letter"
    assert routing.provider_api_key == "test_pg_key"
    assert response["id"] == "letter_test_abc"


def test_lob_snap_pack_dispatch_attribution(monkeypatch):
    """Lob snap_pack dispatch sets provider=lob and routing_decision=lob-only-resource."""
    monkeypatch.setattr(
        "app.providers.routing.direct_mail._lob_test_key", lambda: "test_lob_key"
    )
    _patch_request(monkeypatch, {"id": "ord_test_abc"})

    response, routing = dispatch_piece(
        resource_family="snap_pack",
        payload={"to": "adr_test", "from": "adr_from"},
        test_mode=True,
    )

    assert routing.provider == "lob"
    assert routing.routing_decision == "lob-only-resource"
    assert routing.resource_family == "snap_pack"
    assert response["id"] == "ord_test_abc"


def test_lob_fallback_attribution(monkeypatch):
    """When PostGrid key is absent, Lob is used with routing-layer-default."""
    monkeypatch.setattr("app.providers.routing.direct_mail._postgrid_test_key", lambda: None)
    monkeypatch.setattr(
        "app.providers.routing.direct_mail._lob_test_key", lambda: "test_lob_key"
    )
    _patch_request(monkeypatch, {"id": "ltr_test_abc"})

    response, routing = dispatch_piece(
        resource_family="letter",
        payload={"to": "adr_test", "file": "tmpl_test"},
        test_mode=True,
    )

    assert routing.provider == "lob"
    assert routing.routing_decision == "routing-layer-default"
    assert routing.resource_family == "letter"
    assert response["id"] == "ltr_test_abc"


def test_attribution_fields_present_on_routing_result(monkeypatch):
    """RoutingResult always has provider, routing_decision, resource_family, provider_api_key."""
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setattr(
        "app.providers.routing.direct_mail._postgrid_test_key", lambda: "test_pg_key"
    )
    _patch_request(monkeypatch, {"id": "postcard_test_xyz"})

    response, routing = dispatch_piece(
        resource_family="postcard",
        payload={"to": {"firstName": "Bob"}, "frontHTML": "<p>Front</p>"},
        test_mode=True,
    )

    assert routing.provider is not None
    assert routing.routing_decision is not None
    assert routing.resource_family is not None
    assert routing.provider_api_key is not None

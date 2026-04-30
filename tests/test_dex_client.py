"""DEX client unit tests.

Use httpx.MockTransport to intercept requests so we can assert exact
header / URL / body shape without touching the network or any real DEX.
"""

from __future__ import annotations

from uuid import UUID

import httpx
import pytest

from app.config import settings
from app.services import dex_client


_SPEC_ID = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


def _build_mock_transport(handler):
    return httpx.MockTransport(handler)


def _patched_request(monkeypatch, transport):
    """Replace the AsyncClient inside dex_client._request with one bound to
    our mock transport, preserving the production code path."""
    real_async_client = httpx.AsyncClient

    class _PatchedClient(real_async_client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(dex_client.httpx, "AsyncClient", _PatchedClient)


async def test_client_uses_bearer_when_provided(monkeypatch):
    monkeypatch.setattr(settings, "DEX_BASE_URL", "http://dex.test")

    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "")
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"data": {"id": str(_SPEC_ID)}})

    _patched_request(monkeypatch, _build_mock_transport(handler))

    result = await dex_client.get_audience_spec(_SPEC_ID, bearer_token="user-jwt-abc")
    assert seen["auth"] == "Bearer user-jwt-abc"
    assert seen["url"].endswith(f"/api/v1/fmcsa/audience-specs/{_SPEC_ID}")
    assert result == {"id": str(_SPEC_ID)}


async def test_client_falls_back_to_super_admin_api_key(monkeypatch):
    monkeypatch.setattr(settings, "DEX_BASE_URL", "http://dex.test")

    from pydantic import SecretStr
    monkeypatch.setattr(
        settings, "DEX_SUPER_ADMIN_API_KEY", SecretStr("sa-key-xyz")
    )

    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"data": {"id": str(_SPEC_ID)}})

    _patched_request(monkeypatch, _build_mock_transport(handler))

    await dex_client.get_audience_spec(_SPEC_ID)
    # Same header name (Authorization: Bearer ...) — DEX's
    # _resolve_super_admin_from_api_key compares the bearer token string
    # against settings.super_admin_api_key.
    assert seen["auth"] == "Bearer sa-key-xyz"


async def test_client_raises_when_no_auth_available(monkeypatch):
    monkeypatch.setattr(settings, "DEX_BASE_URL", "http://dex.test")
    monkeypatch.setattr(settings, "DEX_SUPER_ADMIN_API_KEY", None)

    with pytest.raises(dex_client.DexAuthMissingError):
        await dex_client.get_audience_spec(_SPEC_ID)


async def test_client_raises_when_dex_base_url_unset(monkeypatch):
    monkeypatch.setattr(settings, "DEX_BASE_URL", None)

    with pytest.raises(dex_client.DexNotConfiguredError):
        await dex_client.get_audience_spec(_SPEC_ID, bearer_token="anything")


async def test_client_unwraps_data_envelope(monkeypatch):
    monkeypatch.setattr(settings, "DEX_BASE_URL", "http://dex.test")

    inner = {
        "spec": {"id": str(_SPEC_ID)},
        "template": {"slug": "x"},
        "audience_attributes": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": inner})

    _patched_request(monkeypatch, _build_mock_transport(handler))

    result = await dex_client.get_audience_descriptor(
        _SPEC_ID, bearer_token="anything"
    )
    assert result == inner


async def test_client_translates_non_2xx_to_dex_call_error(monkeypatch):
    monkeypatch.setattr(settings, "DEX_BASE_URL", "http://dex.test")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "audience spec not found: ..."})

    _patched_request(monkeypatch, _build_mock_transport(handler))

    with pytest.raises(dex_client.DexCallError) as exc:
        await dex_client.get_audience_spec(_SPEC_ID, bearer_token="x")
    assert exc.value.status_code == 404
    assert isinstance(exc.value.body, dict)
    assert "audience spec not found" in exc.value.body["error"]


async def test_create_audience_spec_sends_template_id_and_overrides(monkeypatch):
    monkeypatch.setattr(settings, "DEX_BASE_URL", "http://dex.test")

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        seen["body"] = _json.loads(request.content.decode("utf-8"))
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"data": {"id": "spec-1"}})

    _patched_request(monkeypatch, _build_mock_transport(handler))

    await dex_client.create_audience_spec(
        template_id=_SPEC_ID,
        filter_overrides={"physical_state": ["TX"]},
        name="DAT — TX carriers",
        bearer_token="x",
    )
    assert seen["url"].endswith("/api/v1/fmcsa/audience-specs")
    assert seen["body"]["template_id"] == str(_SPEC_ID)
    assert seen["body"]["filter_overrides"] == {"physical_state": ["TX"]}
    assert seen["body"]["name"] == "DAT — TX carriers"


async def test_list_audience_members_passes_pagination(monkeypatch):
    monkeypatch.setattr(settings, "DEX_BASE_URL", "http://dex.test")

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        seen["body"] = _json.loads(request.content.decode("utf-8"))
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            json={"data": {"items": [], "total": 0, "limit": 25, "offset": 50}},
        )

    _patched_request(monkeypatch, _build_mock_transport(handler))

    result = await dex_client.list_audience_members(
        _SPEC_ID, limit=25, offset=50, bearer_token="x",
    )
    assert seen["body"] == {"limit": 25, "offset": 50}
    assert seen["url"].endswith(f"/api/v1/fmcsa/audience-specs/{_SPEC_ID}/preview")
    assert result["limit"] == 25

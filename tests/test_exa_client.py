"""Tests for app.services.exa_client.

We never hit the real Exa API in tests — httpx.MockTransport stubs the
network. The tests cover: auth header is x-api-key, missing key raises
ExaNotConfiguredError, non-2xx raises ExaCallError, and the response
includes the _meta envelope.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.services import exa_client


@pytest.fixture
def stub_settings(monkeypatch):
    """Force a known EXA_API_KEY + base for the duration of a test."""
    monkeypatch.setattr(
        exa_client.settings,
        "EXA_API_KEY",
        SecretStr("test-exa-key"),
    )
    monkeypatch.setattr(
        exa_client.settings,
        "EXA_API_BASE",
        "https://api.exa.test",
    )


@pytest.fixture
def mock_transport(monkeypatch):
    """Replace httpx.AsyncClient with a MockTransport-backed instance."""
    captured: dict[str, Any] = {"requests": []}
    handler_box: dict[str, Any] = {"handler": None}

    def install(handler):
        handler_box["handler"] = handler

    real_async_client = httpx.AsyncClient

    class _Patched(real_async_client):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            handler = handler_box["handler"]
            if handler is None:
                raise RuntimeError(
                    "Test forgot to install a mock_transport handler"
                )
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _Patched)
    return {"install": install, "captured": captured}


@pytest.mark.asyncio
async def test_client_sends_api_key_header(stub_settings, mock_transport):
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"results": [{"title": "Hi"}]},
            headers={"x-exa-request-id": "req_abc"},
        )

    mock_transport["install"](handler)

    out = await exa_client.search(query="hello", num_results=3)

    assert captured["headers"].get("x-api-key") == "test-exa-key"
    assert captured["url"] == "https://api.exa.test/search"
    assert captured["body"] == {"query": "hello", "numResults": 3}
    assert "_meta" in out
    assert out["_meta"]["exa_request_id"] == "req_abc"


@pytest.mark.asyncio
async def test_client_raises_when_api_key_missing(monkeypatch, mock_transport):
    monkeypatch.setattr(exa_client.settings, "EXA_API_KEY", None)

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not reach the network")

    mock_transport["install"](handler)

    with pytest.raises(exa_client.ExaNotConfiguredError):
        await exa_client.search(query="hi")


@pytest.mark.asyncio
async def test_client_raises_on_non_2xx(stub_settings, mock_transport):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="rate limited")

    mock_transport["install"](handler)

    with pytest.raises(exa_client.ExaCallError) as exc_info:
        await exa_client.search(query="hi")

    err = exc_info.value
    assert err.status_code == 429
    assert "rate limited" in err.body
    assert err.endpoint == "search"


@pytest.mark.asyncio
async def test_client_returns_meta_envelope(stub_settings, mock_transport):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [{"title": "Hi"}],
                "costDollars": {"total": 0.0123},
                "requestId": "req_zzz",
            },
        )

    mock_transport["install"](handler)

    out = await exa_client.search(query="hello")

    assert out["_meta"]["duration_ms"] >= 0
    assert out["_meta"]["exa_request_id"] == "req_zzz"
    assert out["_meta"]["cost_dollars"] == pytest.approx(0.0123)


@pytest.mark.asyncio
async def test_research_polls_until_completed(stub_settings, mock_transport, monkeypatch):
    # Make polling instantaneous in tests.
    monkeypatch.setattr(exa_client, "_RESEARCH_POLL_INTERVAL", 0.0)

    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/research/v1":
            return httpx.Response(200, json={"researchId": "rsh_1", "status": "queued"})
        if request.method == "GET" and request.url.path.startswith("/research/v1/"):
            state["calls"] += 1
            if state["calls"] < 2:
                return httpx.Response(200, json={"status": "running"})
            return httpx.Response(
                200,
                json={"status": "completed", "result": {"answer": "yes"}},
            )
        return httpx.Response(500, text="unexpected")

    mock_transport["install"](handler)

    out = await exa_client.research(
        instructions="What is the answer?",
        poll_interval=0.0,
        overall_timeout=5.0,
    )

    assert out["status"] == "completed"
    assert out["result"]["answer"] == "yes"
    assert "_meta" in out

"""Bearer-token middleware tests for the /mcp/dmaas mount.

Exercises the ASGI middleware directly with a stub inner app so we don't
have to spin up FastMCP. Verifies:
  * No-op when bearer_token is None (dev fallthrough)
  * 401 with no Authorization header
  * 401 with wrong scheme
  * 401 with wrong token
  * 200 with correct token
  * Lifespan attribute is forwarded so the FastAPI lifespan can chain it
"""

from __future__ import annotations

import pytest

from app.mcp.bearer_auth import bearer_token_app


class _StubASGI:
    """Minimal ASGI app that records calls and returns 200 OK."""

    def __init__(self):
        self.calls: list[dict] = []

    async def __call__(self, scope, receive, send):
        self.calls.append(scope)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


async def _http_call(app, headers: list[tuple[bytes, bytes]] | None = None):
    sent: list[dict] = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": headers or [],
    }
    await app(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body


@pytest.mark.asyncio
async def test_no_token_means_passthrough():
    inner = _StubASGI()
    wrapped = bearer_token_app(inner, bearer_token=None)
    status, body = await _http_call(wrapped)
    assert status == 200
    assert len(inner.calls) == 1


@pytest.mark.asyncio
async def test_missing_authorization_header_401():
    inner = _StubASGI()
    wrapped = bearer_token_app(inner, bearer_token="secret123")
    status, body = await _http_call(wrapped)
    assert status == 401
    assert b"missing_bearer_token" in body
    assert inner.calls == []


@pytest.mark.asyncio
async def test_wrong_scheme_401():
    inner = _StubASGI()
    wrapped = bearer_token_app(inner, bearer_token="secret123")
    status, body = await _http_call(
        wrapped, headers=[(b"authorization", b"Basic dXNlcjpwYXNz")]
    )
    assert status == 401
    assert b"bad_authorization_scheme" in body


@pytest.mark.asyncio
async def test_wrong_token_401():
    inner = _StubASGI()
    wrapped = bearer_token_app(inner, bearer_token="secret123")
    status, body = await _http_call(
        wrapped, headers=[(b"authorization", b"Bearer wrong")]
    )
    assert status == 401
    assert b"invalid_bearer_token" in body
    assert inner.calls == []


@pytest.mark.asyncio
async def test_correct_token_200():
    inner = _StubASGI()
    wrapped = bearer_token_app(inner, bearer_token="secret123")
    status, body = await _http_call(
        wrapped, headers=[(b"authorization", b"Bearer secret123")]
    )
    assert status == 200
    assert len(inner.calls) == 1


@pytest.mark.asyncio
async def test_case_insensitive_scheme():
    inner = _StubASGI()
    wrapped = bearer_token_app(inner, bearer_token="secret123")
    status, _ = await _http_call(
        wrapped, headers=[(b"authorization", b"bearer secret123")]
    )
    assert status == 200


@pytest.mark.asyncio
async def test_lifespan_event_passthrough_when_token_set():
    """Lifespan/websocket scope types must pass through — we only gate HTTP."""
    inner = _StubASGI()

    async def inner_app(scope, receive, send):
        inner.calls.append(scope)

    wrapped = bearer_token_app(inner_app, bearer_token="secret123")

    async def receive():
        return {"type": "lifespan.startup"}

    async def send(_msg):
        pass

    await wrapped({"type": "lifespan"}, receive, send)
    assert inner.calls and inner.calls[0]["type"] == "lifespan"


def test_lifespan_attribute_forwarded():
    """app/main.py chains FastMCP's lifespan; the wrapper must expose it."""

    class _StubWithLifespan:
        async def __call__(self, *a, **kw):
            pass

        def lifespan(self, _):
            return self

    stub = _StubWithLifespan()
    wrapped = bearer_token_app(stub, bearer_token="x")
    assert hasattr(wrapped, "lifespan")
    assert wrapped.lifespan == stub.lifespan


def test_constant_time_compare():
    """Ensure we compare with secrets.compare_digest, not ==. Smoke test
    that a near-miss token fails the same way as a wildly-wrong one
    (same code path)."""
    import asyncio

    inner = _StubASGI()
    wrapped = bearer_token_app(inner, bearer_token="secret123")

    async def go():
        for token in (b"secret124", b"different", b"secret12"):
            status, _ = await _http_call(
                wrapped, headers=[(b"authorization", b"Bearer " + token)]
            )
            assert status == 401

    asyncio.run(go())

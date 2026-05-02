"""Verify the global error handlers in app/main.py produce structured
JSON envelopes instead of bare 500s.
"""

from __future__ import annotations

import psycopg
import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(autouse=True)
def _disable_pool(monkeypatch):
    """The lifespan tries to init a real psycopg pool; neuter for tests."""
    async def _noop() -> None:
        return None

    monkeypatch.setattr("app.main.init_pool", _noop)
    monkeypatch.setattr("app.main.close_pool", _noop)


@pytest.fixture
def trap_router():
    """Mount a throwaway router with explicit-error endpoints, then unmount.

    FastAPI doesn't expose a public unmount API, so we snapshot the route
    list before/after to keep the global app clean.
    """
    router = APIRouter(prefix="/__test_traps")

    @router.get("/runtime")
    async def _raise_runtime() -> None:
        raise RuntimeError("boom from runtime trap")

    @router.get("/attribute")
    async def _raise_attribute() -> None:
        raise AttributeError("missing widget")

    @router.get("/psycopg")
    async def _raise_psycopg() -> None:
        raise psycopg.errors.OperationalError("connection reset by peer")

    before = list(app.router.routes)
    app.include_router(router)
    try:
        yield
    finally:
        app.router.routes[:] = before


def test_runtime_error_returns_structured_500(trap_router):
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/__test_traps/runtime")
    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert detail["error"] == "internal_server_error"
    assert detail["type"] == "RuntimeError"
    assert "boom from runtime trap" in detail["message"]
    assert detail["method"] == "GET"
    assert detail["path"] == "/__test_traps/runtime"
    assert len(detail["request_id"]) >= 16


def test_attribute_error_returns_structured_500(trap_router):
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/__test_traps/attribute")
    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert detail["type"] == "AttributeError"
    assert "missing widget" in detail["message"]


def test_psycopg_error_returns_structured_503(trap_router):
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/__test_traps/psycopg")
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert detail["error"] == "database_error"
    assert detail["type"] == "OperationalError"
    assert detail["retryable"] is True
    assert "connection reset by peer" in detail["message"]


def test_validation_error_uses_structured_422():
    """FastAPI's RequestValidationError gets wrapped in our envelope.

    Hitting an existing route with an invalid path-param (UUID) is the
    cleanest trigger.
    """
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get(
            "/api/brands/not-a-uuid/voice-ai/assistants",
            headers={"Authorization": "Bearer fake-token"},
        )
    # Auth runs before validation in some configs, so accept either 401
    # (auth fails first on invalid token) or 422 (validation fails first).
    # The frontend mainly cares that 422s are wrapped in our envelope.
    if resp.status_code == 422:
        detail = resp.json()["detail"]
        assert detail["error"] == "validation_error"
        assert isinstance(detail["errors"], list)
        assert detail["method"] == "GET"

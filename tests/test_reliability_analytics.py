"""Tests for the analytics router's /reliability endpoint and the
underlying summarize_reliability service.

The service hits Postgres; tests use the same in-memory cursor fake
pattern as the existing voice analytics tests. Auth is bypassed via
dependency_overrides — we test endpoint behavior, not the JWT.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest

from app.auth.roles import require_org_context
from app.auth.supabase_jwt import UserContext, verify_supabase_jwt
from app.main import app
from app.services import reliability_analytics

ORG_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ORG_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
USER_A = UUID("11111111-1111-1111-1111-111111111111")


def _user(org_id: UUID | None) -> UserContext:
    return UserContext(
        auth_user_id=USER_A,
        business_user_id=USER_A,
        email="op@example.com",
        platform_role="platform_operator",
        active_organization_id=org_id,
        org_role=None,
        role="operator",
        client_id=None,
    )


@pytest.fixture
def auth_org_a():
    user = _user(ORG_A)
    app.dependency_overrides[verify_supabase_jwt] = lambda: user
    app.dependency_overrides[require_org_context] = lambda: user
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def auth_no_org():
    """User signed in but with no active organization context."""
    from fastapi import HTTPException, status

    user = _user(None)
    app.dependency_overrides[verify_supabase_jwt] = lambda: user

    def _deny() -> UserContext:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "organization_context_required"},
        )

    app.dependency_overrides[require_org_context] = _deny
    yield
    app.dependency_overrides.clear()


# ── Postgres fake ────────────────────────────────────────────────────────


class _FakeCursor:
    def __init__(self, rows: list[tuple], capture: dict[str, Any]) -> None:
        self._rows = rows
        self._capture = capture

    async def __aenter__(self) -> _FakeCursor:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def execute(self, sql: str, params: Any = None) -> None:
        self._capture["sql"] = sql
        self._capture["params"] = params

    async def fetchall(self) -> list[tuple]:
        return self._rows


class _FakeConn:
    def __init__(self, rows: list[tuple], capture: dict[str, Any]) -> None:
        self._rows = rows
        self._capture = capture

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._rows, self._capture)


def _patch_pg(monkeypatch: pytest.MonkeyPatch, rows: list[tuple]) -> dict[str, Any]:
    capture: dict[str, Any] = {}

    @asynccontextmanager
    async def _conn():
        yield _FakeConn(rows, capture)

    monkeypatch.setattr(reliability_analytics, "get_db_connection", _conn)
    return capture


async def _get(path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


# ─────────────────────────────────────────────────────────────────────────
# Service layer
# ─────────────────────────────────────────────────────────────────────────


async def test_summarize_groups_by_provider_and_status(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        ("lob", "processed", 8, 1),
        ("lob", "received", 2, 0),
        ("emailbison", "processed", 5, 0),
    ]
    _patch_pg(monkeypatch, rows)

    start = datetime(2026, 4, 1, tzinfo=UTC)
    end = datetime(2026, 4, 30, tzinfo=UTC)
    result = await reliability_analytics.summarize_reliability(
        organization_id=ORG_A, brand_id=None, start=start, end=end
    )

    assert result["source"] == "postgres"
    assert result["window"]["from"] == start.isoformat()
    assert result["window"]["to"] == end.isoformat()
    assert result["totals"] == {"events": 15, "replays": 1}

    by_provider = {p["provider_slug"]: p for p in result["providers"]}
    assert by_provider["lob"]["events_total"] == 10
    assert by_provider["lob"]["replays_total"] == 1
    assert by_provider["lob"]["by_status"] == {"processed": 8, "received": 2}
    assert by_provider["emailbison"]["events_total"] == 5
    assert by_provider["emailbison"]["replays_total"] == 0
    assert by_provider["emailbison"]["by_status"] == {"processed": 5}


async def test_summarize_filters_by_org_in_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = _patch_pg(monkeypatch, [])
    start = datetime(2026, 4, 1, tzinfo=UTC)
    end = datetime(2026, 4, 30, tzinfo=UTC)

    await reliability_analytics.summarize_reliability(
        organization_id=ORG_A, brand_id=None, start=start, end=end
    )

    assert "b.organization_id = %s" in capture["sql"]
    assert capture["params"][0] == str(ORG_A)
    # Cross-org leakage guard: the SQL ALWAYS goes through the brand→org
    # join. No path that filters by brand_id alone.
    assert "JOIN business.brands" in capture["sql"]


async def test_summarize_with_brand_filter_appends_clause(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = _patch_pg(monkeypatch, [])
    brand = UUID("33333333-3333-3333-3333-333333333333")
    await reliability_analytics.summarize_reliability(
        organization_id=ORG_A,
        brand_id=brand,
        start=datetime(2026, 4, 1, tzinfo=UTC),
        end=datetime(2026, 4, 30, tzinfo=UTC),
    )
    assert "we.brand_id = %s" in capture["sql"]
    assert str(brand) in capture["params"]


async def test_summarize_empty_returns_empty_provider_list(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pg(monkeypatch, [])
    result = await reliability_analytics.summarize_reliability(
        organization_id=ORG_A,
        brand_id=None,
        start=datetime(2026, 4, 1, tzinfo=UTC),
        end=datetime(2026, 4, 30, tzinfo=UTC),
    )
    assert result["providers"] == []
    assert result["totals"] == {"events": 0, "replays": 0}


# ─────────────────────────────────────────────────────────────────────────
# HTTP layer
# ─────────────────────────────────────────────────────────────────────────


async def test_reliability_endpoint_returns_payload(
    monkeypatch: pytest.MonkeyPatch, auth_org_a: None
) -> None:
    _patch_pg(monkeypatch, [("lob", "processed", 3, 0)])

    resp = await _get("/api/v1/analytics/reliability")

    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "postgres"
    assert body["totals"] == {"events": 3, "replays": 0}
    assert body["providers"][0]["provider_slug"] == "lob"


async def test_reliability_endpoint_uses_org_from_auth_not_query(
    monkeypatch: pytest.MonkeyPatch, auth_org_a: None
) -> None:
    """A user with active org A asking the endpoint must always filter
    by org A — there is no `organization_id` query param to abuse.
    """
    capture = _patch_pg(monkeypatch, [])

    # The user's auth context is org A. Even if the URL says nothing
    # about an org, the SQL params must carry org A.
    resp = await _get("/api/v1/analytics/reliability")
    assert resp.status_code == 200
    assert capture["params"][0] == str(ORG_A)


async def test_reliability_endpoint_requires_org_context(
    monkeypatch: pytest.MonkeyPatch, auth_no_org: None
) -> None:
    # Should not even reach the service layer.
    monkeypatch.setattr(
        reliability_analytics,
        "get_db_connection",
        lambda: pytest.fail("service should not be called without org"),
    )
    resp = await _get("/api/v1/analytics/reliability")
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "organization_context_required"


async def test_reliability_endpoint_rejects_invalid_window(
    monkeypatch: pytest.MonkeyPatch, auth_org_a: None
) -> None:
    monkeypatch.setattr(
        reliability_analytics,
        "get_db_connection",
        lambda: pytest.fail("invalid window should short-circuit"),
    )
    # end before start
    resp = await _get(
        "/api/v1/analytics/reliability?from=2026-04-30T00:00:00Z&to=2026-04-01T00:00:00Z"
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_window"


async def test_reliability_endpoint_rejects_window_too_large(
    monkeypatch: pytest.MonkeyPatch, auth_org_a: None
) -> None:
    monkeypatch.setattr(
        reliability_analytics,
        "get_db_connection",
        lambda: pytest.fail("oversized window should short-circuit"),
    )
    # Use Z suffix instead of +00:00 to avoid URL encoding issues.
    resp = await _get(
        "/api/v1/analytics/reliability?from=2026-01-01T00:00:00Z&to=2026-05-01T00:00:00Z"
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "window_too_large"


async def test_reliability_endpoint_rejects_invalid_brand_id(
    monkeypatch: pytest.MonkeyPatch, auth_org_a: None
) -> None:
    monkeypatch.setattr(
        reliability_analytics,
        "get_db_connection",
        lambda: pytest.fail("malformed brand_id should short-circuit"),
    )
    resp = await _get("/api/v1/analytics/reliability?brand_id=not-a-uuid")
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_brand_id"


async def test_reliability_endpoint_passes_brand_id_through(
    monkeypatch: pytest.MonkeyPatch, auth_org_a: None
) -> None:
    capture = _patch_pg(monkeypatch, [])
    brand = "44444444-4444-4444-4444-444444444444"
    resp = await _get(f"/api/v1/analytics/reliability?brand_id={brand}")
    assert resp.status_code == 200
    assert "we.brand_id = %s" in capture["sql"]
    assert brand in capture["params"]
